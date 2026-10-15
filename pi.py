#!/usr/bin/env python3
"""
Gym Access Sync Script (Raspberry Pi or Mac) - Daily Membership & Attendance Version (Optimized)

Flow:

1) Pi discovers ZKT device on LAN.
   - First tries cached IP from file.
   - If that fails, scans network, then caches found IP.

2) Every few seconds:
   - Pi asks Django: /api/device-sync-tasks/?processed=false
   - For each task:
       - ADD_ENABLE → ensure user exists, restore fingerprints from Django
       - DISABLE → backup fingerprints to Django, delete user from device (door blocked)
       - DELETE → permanently delete user from device
       - SYNC_ATTENDANCE (payload date is IGNORED):
           → read ALL logs from ZKT
           → send ALL logs to Django /api/attendance/
           → Django deduplicates using (device_user_id, punch_time)
           → mark task processed

3) DAILY JOB (PRODUCTION MODE):
   - Once per day at 23:40 Asia/Kathmandu time:
       - Call Django to auto-update memberships (expire/activate) and create
         DISABLE / ADD_ENABLE DeviceSyncTasks.
       - Perform automatic full attendance sync (no SYNC_ATTENDANCE task needed).
"""

import os
import socket
import time
from datetime import datetime, timezone as dt_timezone
import json

import requests
from zk import ZK  # pip install zk / pyzk

from base64 import b64encode, b64decode
from zk.finger import Finger

# ======================================================
# --------------- GLOBAL DEBUG & SESSION ---------------
# ======================================================

DEBUG = False  # Set True for verbose logs during development

# We'll reuse this session for all HTTP calls (less overhead / faster)
session = requests.Session()


# ------- Timezone: Asia/Kathmandu (UTC+5:45) ------- #
try:
    from zoneinfo import ZoneInfo
    KATHMANDU_TZ = ZoneInfo("Asia/Kathmandu")
except Exception:
    # Fallback if zoneinfo isn't available: use system local time
    KATHMANDU_TZ = None


def get_kathmandu_now():
    """
    Return current time in Asia/Kathmandu as a timezone-aware datetime.
    If ZoneInfo is unavailable, fall back to naive local datetime.
    """
    if KATHMANDU_TZ is not None:
        return datetime.now(KATHMANDU_TZ)
    return datetime.now()  # fallback: system local time


# ======================= CONFIG =======================

# Django server base URL (no trailing slash)
# IMPORTANT: if Django is on another machine, set its IP here (e.g. "http://192.168.1.50:8000")
DJANGO_BASE_URL = "https://7club.pi-maker.com"

# ZKT device port (most ZKT devices use 4370)
DEVICE_PORT = 4370

# Optional headers for auth (e.g. token) – leave empty for now
HEADERS = {
    # "Authorization": "Token YOUR_SECRET_TOKEN"
}

# Polling intervals (seconds)
TASK_POLL_IDLE_SECONDS = 10          # when no tasks recently
TASK_POLL_ACTIVE_SECONDS = 3         # shortly after a task was processed
TASK_POLL_LONG_IDLE_SECONDS = 45     # when no tasks for a long time (to reduce load)
NO_TASKS_LONG_IDLE_AFTER = 5 * 60    # 5 minutes with no tasks → long idle

# Port scan timeout per IP
PORT_SCAN_TIMEOUT = 0.3

# File to cache the last known ZKT device IP
ZKT_IP_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "zkt_device_ip.txt"
)

# DAILY JOB TIME (local, Asia/Kathmandu)
AUTO_JOB_HOUR = 14     # 00 = 12 AM
AUTO_JOB_MINUTE = 28


# ======================================================
# ------------------ CACHE & HELPERS ------------------ #
# ======================================================

def is_device_connection_error(e: Exception) -> bool:
    """
    Return True if the exception looks like a low-level connection error
    where we should drop 'conn' and reconnect.
    """
    msg = str(e).lower()
    return (
        isinstance(e, OSError)
        or "broken pipe" in msg
        or "timed out" in msg
        or "connection reset" in msg
        or "network is unreachable" in msg
    )
def fetch_all_users_from_device(conn):
    """
    Read all users from the ZKT device and return a list of dicts:

    [
      { "uid": 1, "user_id": "1", "name": "Ram", "privilege": 0 },
      ...
    ]

    These keys match what api_device_users_sync expects.
    """
    print("[USERS] Reading ALL users from device...")
    try:
        users = conn.get_users()
    except Exception as e:
        print("[USERS] Error reading users from device:", e)
        return []

    payload = []
    for u in users:
        user_dict = {
            "uid": getattr(u, "uid", None),
            "user_id": getattr(u, "user_id", None),
            "name": getattr(u, "name", "") or "",
            "privilege": getattr(u, "privilege", 0),
        }
        payload.append(user_dict)

    print(f"[USERS] Prepared {len(payload)} user record(s) to send to Django.")
    return payload


def send_users_to_django(payload):
    """
    POST device users to Django:

      POST /device-users-sync/

    Body: [ {uid, user_id, name, privilege}, ... ]
    """
    if not payload:
        print("[USERS] No users to send.")
        return True  # treat as success

    url = f"{DJANGO_BASE_URL}/device-users-sync/"
    print(f"[USERS] POST {url} ({len(payload)} records)")

    if DEBUG:
        print("----- USERS PAYLOAD TO DJANGO -----")
        print(json.dumps(payload, indent=4))
        print("-----------------------------------")

    try:
        resp = session.post(url, json=payload, headers=HEADERS, timeout=30)
        if resp.status_code not in (200, 201):
            print("[USERS] Failed:", resp.status_code, resp.text)
            return False

        try:
            print("[USERS] Django response:", resp.json())
        except Exception:
            print("[USERS] Django response (non-JSON):", resp.text)
        return True
    except Exception as e:
        print("[USERS] Error sending to Django:", e)
        return False


def load_cached_device_ip():
    """
    Read cached ZKT device IP from file, if it exists.
    Returns IP string or None.
    """
    if not os.path.exists(ZKT_IP_CACHE_FILE):
        return None

    try:
        with open(ZKT_IP_CACHE_FILE, "r", encoding="utf-8") as f:
            ip = f.read().strip()
            if ip:
                print(f"[CACHE] Loaded cached ZKT IP: {ip}")
                return ip
    except Exception as e:
        print(f"[CACHE] Error reading cache file: {e}")

    return None


def save_cached_device_ip(ip):
    """
    Save ZKT device IP to cache file.
    """
    try:
        with open(ZKT_IP_CACHE_FILE, "w", encoding="utf-8") as f:
            f.write(str(ip).strip())
        print(f"[CACHE] Saved ZKT IP to cache: {ip}")
    except Exception as e:
        print(f"[CACHE] Error writing cache file: {e}")


# ======================================================
# ----------- FINGERPRINT BACKUP / RESTORE ------------
# ======================================================

def backup_fingerprints_to_django(member_id, fingers):
    """
    Send fingerprint templates for a user to Django before we delete them
    from the device (DISABLE).
    """
    if not fingers:
        print("[FINGER BACKUP] No templates found on device.")
        return True  # nothing to backup, treat as OK

    url = f"{DJANGO_BASE_URL}/api/members/{member_id}/backup-fingerprints/"

    payload = {
        "templates": [
            {
                "finger_index": f.fid,  # finger index 0-9
                "template_data": b64encode(f.template).decode("ascii"),
            }
            for f in fingers
        ]
    }

    print(f"[FINGER BACKUP] POST {url}")
    try:
        resp = session.post(url, json=payload, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print("[FINGER BACKUP] Failed:", resp.status_code, resp.text)
            return False
        data = resp.json()
        print(f"[FINGER BACKUP] Saved {data.get('saved')} template(s) in Django.")
        return True
    except Exception as e:
        print("[FINGER BACKUP] Error sending to Django:", e)
        return False


def restore_fingerprints_from_django(conn, member_id, zkt_user_id):
    """
    Fetch stored templates from Django and write them back to the device.

    - Assumes user already exists on device (we call set_user before this).
    """
    url = f"{DJANGO_BASE_URL}/api/members/{member_id}/fingerprints/"
    print(f"[FINGER RESTORE] GET {url}")
    try:
        resp = session.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print("[FINGER RESTORE] Failed:", resp.status_code, resp.text)
            return
        data = resp.json()
    except Exception as e:
        print("[FINGER RESTORE] Error requesting templates:", e)
        return

    templates = data.get("templates", [])
    if not templates:
        print("[FINGER RESTORE] No stored templates for this member.")
        return

    # Find the user object from device
    try:
        users = conn.get_users()
    except Exception as e:
        print("[FINGER RESTORE] Could not fetch users from device:", e)
        return

    user = None
    for u in users:
        # Match either by our user_id or by internal uid
        if str(u.user_id) == str(zkt_user_id) or int(u.uid) == int(zkt_user_id):
            user = u
            break

    if not user:
        print(f"[FINGER RESTORE] User {zkt_user_id} not found on device; cannot restore templates.")
        return

    fingers = []
    for tpl in templates:
        idx = tpl.get("finger_index")
        data_b64 = tpl.get("template_data")
        if idx is None or data_b64 is None:
            continue

        try:
            raw = b64decode(data_b64)
        except Exception:
            continue

        fingers.append(Finger(uid=user.uid, fid=idx, valid=1, template=raw))

    if not fingers:
        print("[FINGER RESTORE] No valid templates to restore.")
        return

    try:
        conn.save_user_template(user, fingers)
        print(f"[FINGER RESTORE] Restored {len(fingers)} template(s) for user {zkt_user_id}.")
    except Exception as e:
        print("[FINGER RESTORE] Error saving templates to device:", e)


# ======================================================
# ---------------- NETWORK & DISCOVERY ----------------
# ======================================================

def get_pi_ip():
    """
    Get the primary LAN IP of this machine.
    Uses a dummy UDP connection to 8.8.8.8 to discover local IP.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def get_network_prefix_from_ip(ip):
    """
    From '192.168.1.23' -> '192.168.1.'
    """
    parts = ip.split(".")
    if len(parts) != 4:
        raise ValueError(f"Invalid IP address: {ip}")
    return ".".join(parts[:3]) + "."


def is_port_open(ip, port, timeout=PORT_SCAN_TIMEOUT):
    """
    Check if TCP port is open on given IP.
    """
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def connect_zkt(ip):
    """
    Try to connect to ZKT device at given IP.
    Returns (ip, zk_instance, conn) or (None, None, None) if failed.
    """
    print(f"[CONNECT] Trying ZKT at {ip}:{DEVICE_PORT} ...")
    try:
        zk = ZK(ip, port=DEVICE_PORT, timeout=5, password=0,
                force_udp=False, ommit_ping=False)
        conn = zk.connect()
        try:
            fw = conn.get_firmware_version()
            print(f"[CONFIRMED] ZKT at {ip}, firmware: {fw}")
        except Exception:
            print(f"[CONFIRMED] ZKT at {ip} (firmware check failed, but connection OK)")
        return ip, zk, conn
    except Exception as e:
        print(f"[CONNECT] Failed to connect to ZKT at {ip}: {e}")
        return None, None, None


def discover_zkt_device_scan():
    """
    Scan the local network for a ZKT device.

    1. Get local IP
    2. Derive network prefix (e.g. '192.168.1.')
    3. Scan 1–254 on port 4370
    4. Confirm ZKT by connecting with ZK library

    Returns (ip, zk, conn) or (None, None, None)
    """
    pi_ip = get_pi_ip()
    print(f"[INFO] Local machine IP: {pi_ip}")

    if pi_ip.startswith("127."):
        print("[WARN] Local IP is 127.x.x.x - are you connected to LAN/WiFi?")
        return None, None, None

    network_prefix = get_network_prefix_from_ip(pi_ip)
    print(f"[INFO] Scanning {network_prefix}1-254 for ZKT on port {DEVICE_PORT}...")

    skip_ip = pi_ip  # skip our own IP

    for last_octet in range(1, 255):
        ip = f"{network_prefix}{last_octet}"
        if ip == skip_ip:
            continue

        if not is_port_open(ip, DEVICE_PORT):
            continue

        print(f"[CANDIDATE] Port {DEVICE_PORT} open at {ip}, trying ZKT handshake...")
        ip_found, zk, conn = connect_zkt(ip)
        if conn:
            return ip_found, zk, conn

    print("[ERROR] No ZKT device found on this network.")
    return None, None, None


def discover_zkt_device_with_cache():
    """
    Optimized discovery:

    1. Try cached IP from file:
       - If connection succeeds → use it.
       - If fails → fallback to full scan.

    2. Full network scan:
       - If device found → save IP to cache and use it.
    """
    # 1) Try from cache with a couple of quick retries
    cached_ip = load_cached_device_ip()
    if cached_ip:
        for attempt in range(2):
            ip, zk, conn = connect_zkt(cached_ip)
            if conn:
                save_cached_device_ip(ip)
                return ip, zk, conn
            print(f"[CACHE] Cached IP {cached_ip} attempt {attempt + 1} failed, retrying...")
            time.sleep(2)

        print("[CACHE] Cached IP is not responding as ZKT device, will scan network...")

    # 2) Fallback – scan network
    ip, zk, conn = discover_zkt_device_scan()
    if conn:
        save_cached_device_ip(ip)
    return ip, zk, conn


# ======================================================
# ---------------- DJANGO <-> PI HELPERS --------------
# ======================================================

def fetch_device_sync_tasks():
    """
    Get pending tasks from Django: /api/device-sync-tasks/?processed=false
    and (optionally) print the raw JSON for debugging.
    """
    url = f"{DJANGO_BASE_URL}/api/device-sync-tasks/?processed=false"
    print(f"[SYNC TASKS] GET {url}")

    try:
        resp = session.get(url, headers=HEADERS, timeout=10)
    except Exception as e:
        print("[SYNC TASKS] Error requesting tasks:", e)
        return []

    if resp.status_code != 200:
        print("[SYNC TASKS] Failed:", resp.status_code, resp.text)
        return []

    try:
        tasks = resp.json()
    except Exception as e:
        print("[SYNC TASKS] JSON decode error:", e)
        if DEBUG:
            print("[SYNC TASKS] Raw Response:", resp.text)
        return []

    if DEBUG:
        print("----- RAW TASKS FROM DJANGO -----")
        print(json.dumps(tasks, indent=4))
        print("----------------------------------")

    print(f"[SYNC TASKS] Received {len(tasks)} task(s).")

    if DEBUG:
        for t in tasks:
            print(
                f"  -> Task ID {t.get('id')} | "
                f"Action: {t.get('action')} | "
                f"Member: {t.get('member')} | "
                f"Payload: {t.get('payload')}"
            )

    return tasks


def mark_task_processed(task_id):
    """
    Mark a DeviceSyncTask as processed in Django:
    POST /api/device-sync-tasks/<id>/  with body {"processed": true}
    """
    url = f"{DJANGO_BASE_URL}/api/device-sync-tasks/{task_id}/"
    payload = {"processed": True}
    print(f"[TASK DONE] POST {url} {payload}")
    try:
        resp = session.post(url, json=payload, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            print("[TASK DONE] Failed:", resp.status_code, resp.text)
        else:
            print("[TASK DONE] Success.")
    except Exception as e:
        print("[TASK DONE] Error:", e)


def delete_zk_user(conn, zkt_user_id):
    """
    Try to delete a user from device using BOTH:
    - user_id (string shown on machine, which we map from Django zkt_user_id)
    - uid (internal index)
    Return True if any succeeds, False otherwise.
    """
    # 1) Try by user_id
    try:
        print(f"[DEVICE] Trying delete_user(user_id='{zkt_user_id}')")
        conn.delete_user(user_id=str(zkt_user_id))
        print(f"[DEVICE] delete_user(user_id='{zkt_user_id}') OK.")
        return True
    except Exception as e1:
        print(f"[DEVICE] delete_user(user_id='{zkt_user_id}') failed: {e1}")

    # 2) Fallback: try by uid
    try:
        print(f"[DEVICE] Trying delete_user(uid={zkt_user_id})")
        conn.delete_user(uid=zkt_user_id)
        print(f"[DEVICE] delete_user(uid={zkt_user_id}) OK.")
        return True
    except Exception as e2:
        print(f"[DEVICE] delete_user(uid={zkt_user_id}) failed: {e2}")

    # 3) Debug help: list users on device
    try:
        users = conn.get_users()
        print("[DEVICE] Current users on device:")
        for u in users:
            print(f"  UID={u.uid} | user_id={u.user_id} | name={u.name}")
    except Exception as e3:
        print("[DEVICE] Also failed to list users:", e3)

    return False


def fetch_all_attendance_from_device(conn):
    """
    Get ALL attendance logs from device via conn.get_attendance().

    We assume the ZKT device clock is set to Asia/Kathmandu local time.
    We convert that local time -> UTC before sending to Django.
    Django stores UTC and can display in local time correctly.
    """
    print("[ATTENDANCE] Reading ALL logs from device...")
    try:
        logs = conn.get_attendance()
    except Exception as e:
        print("[ATTENDANCE] Error reading from device:", e)
        return []

    payload = []
    for log in logs:
        log_dt = log.timestamp  # naive datetime from device

        # --- IMPORTANT: interpret naive timestamp as Asia/Kathmandu ---
        if log_dt.tzinfo is None:
            try:
                from zoneinfo import ZoneInfo
                kathmandu_tz = ZoneInfo("Asia/Kathmandu")
                local_dt = log_dt.replace(tzinfo=kathmandu_tz)
                punch_utc = local_dt.astimezone(dt_timezone.utc)
            except Exception:
                # Fallback: old behavior (might shift times if TZ not available)
                punch_utc = log_dt.replace(tzinfo=dt_timezone.utc)
        else:
            # If somehow tz-aware already, just convert to UTC
            punch_utc = log_dt.astimezone(dt_timezone.utc)

        payload.append({
            "device_user_id": int(log.user_id),
            "punch_time": punch_utc.isoformat(),
        })

    print(f"[ATTENDANCE] Prepared {len(payload)} record(s) (ALL logs).")
    return payload


def send_attendance_to_django(payload):
    """
    POST attendance list to Django:
    POST /api/attendance/
    Body: [ {device_user_id, punch_time}, ... ]
    """
    if not payload:
        print("[ATTENDANCE] No logs to send.")
        return True  # treat as success, just nothing to sync

    url = f"{DJANGO_BASE_URL}/api/attendance/"
    print(f"[ATTENDANCE] POST {url} ({len(payload)} records)")

    if DEBUG:
        print("----- ATTENDANCE PAYLOAD TO DJANGO -----")
        print(json.dumps(payload, indent=4))
        print("----------------------------------------")

    try:
        resp = session.post(url, json=payload, headers=HEADERS, timeout=20)
        if resp.status_code not in (200, 201):
            print("[ATTENDANCE] Failed:", resp.status_code, resp.text)
            return False
        else:
            print("[ATTENDANCE] Sent successfully.")
            return True
    except Exception as e:
        print("[ATTENDANCE] Error sending to Django:", e)
        return False


# def run_membership_daily_update():
#     """
#     Ask Django to update memberships (expire/activate) based on dates.
#     Creates DISABLE / ADD_ENABLE DeviceSyncTasks, which we then process
#     as usual in apply_tasks_to_device().
#     """
#     url = f"{DJANGO_BASE_URL}/api/memberships/daily-update/"
#     print(f"[MEMBERSHIP UPDATE] POST {url}")
#     try:
#         resp = session.post(url, headers=HEADERS, timeout=20)
#         if resp.status_code != 200:
#             print("[MEMBERSHIP UPDATE] Failed:", resp.status_code, resp.text)
#             return
#         data = resp.json()
#         print(
#             f"[MEMBERSHIP UPDATE] Done. "
#             f"Expired={data.get('expired_count')}, "
#             f"DISABLE tasks={data.get('disabled_tasks')}, "
#             f"Activated={data.get('activated_count')}, "
#             f"ADD_ENABLE tasks={data.get('enable_tasks')}"
#         )
#     except Exception as e:
#         print("[MEMBERSHIP UPDATE] Error calling Django:", e)


def run_daily_attendance_auto_sync(conn):
    """
    Automatically sync ALL attendance logs.
    Safe because Django deduplicates using (device_user_id, punch_time).
    """
    print("[AUTO ATTENDANCE] Starting automatic full attendance sync...")
    payload = fetch_all_attendance_from_device(conn)
    if not payload:
        print("[AUTO ATTENDANCE] No logs to send.")
        return

    ok = send_attendance_to_django(payload)
    if ok:
        print("[AUTO ATTENDANCE] Automatic attendance sync completed.")
    else:
        print("[AUTO ATTENDANCE] Automatic attendance sync FAILED.")


# ======================================================
# --------------- APPLY TASKS TO DEVICE ----------------
# ======================================================

def apply_tasks_to_device(conn, tasks):
    """
    Apply DeviceSyncTask actions on the ZKT device.

    - SYNC_ATTENDANCE:
        → Read ALL logs and send to Django.
    - ADD_ENABLE:
        → Create/update user on device and restore fingerprint templates from Django.
    - DISABLE:
        → Backup fingerprints to Django, then delete user from device
          (so door is blocked).
    - DELETE:
        → Hard delete user + templates from device (no automatic restore).

    If a low-level device connection error (e.g. Broken pipe) occurs,
    we re-raise it so the main loop can reconnect.
    """
    any_success = False

    for task in tasks:
        task_id = task.get("id")
        action = task.get("action")
        payload = task.get("payload") or {}
        member = task.get("member") or None

        print(f"[TASK {task_id}] Action={action}")

        # -------- SYNC_ATTENDANCE --------
        if action == "SYNC_ATTENDANCE":
            date_str = payload.get("date")
            print(f"[TASK {task_id}] SYNC_ATTENDANCE (payload date={date_str}, but sending ALL logs)")

            try:
                att_payload = fetch_all_attendance_from_device(conn)
                if att_payload:
                    send_attendance_to_django(att_payload)
                else:
                    print(f"[TASK {task_id}] No attendance logs found on device.")
                mark_task_processed(task_id)
                any_success = True
            except Exception as e:
                print(f"[TASK {task_id}] Error during SYNC_ATTENDANCE:", e)
                if is_device_connection_error(e):
                    # Bubble up so sync_loop can reconnect
                    raise
            continue
        if action == "SYNC_USERS":
            print(f"[TASK {task_id}] SYNC_USERS → fetch all users from device and import into Django")

            try:
                users_payload = fetch_all_users_from_device(conn)
                ok = send_users_to_django(users_payload)
                if ok:
                    mark_task_processed(task_id)
                    any_success = True
                else:
                    print(f"[TASK {task_id}] Failed to send users to Django; task not marked processed.")
            except Exception as e:
                print(f"[TASK {task_id}] Error during SYNC_USERS:", e)
                if is_device_connection_error(e):
                    # Bubble up so sync_loop can reconnect
                    raise

            continue

        # -------- User-related actions --------
        zkt_user_id = None
        full_name = None
        member_id = None
        if member:
            zkt_user_id = member.get("zkt_user_id")
            full_name = member.get("full_name")
            member_id = member.get("id")

        if action in ("ADD_ENABLE", "DISABLE", "DELETE"):
            if not zkt_user_id:
                print(f"[TASK {task_id}] No zkt_user_id present for user action, skipping.")
                continue

            try:
                success = False

                # 1) ADD_ENABLE → ensure user exists and restore fingerprints
                if action == "ADD_ENABLE":
                    print(f"[TASK {task_id}] ADD_ENABLE for {full_name} (ZKT ID {zkt_user_id})")

                    conn.set_user(
                        uid=zkt_user_id,
                        name=(full_name or "")[:24],
                        privilege=0,
                        password="",
                        group_id="",
                        user_id=str(zkt_user_id),
                    )
                    print(f"[DEVICE] User {zkt_user_id} added/updated.")

                    if member_id is not None:
                        restore_fingerprints_from_django(conn, member_id, zkt_user_id)

                    success = True

                # 2) DISABLE → backup templates in Django, then delete user from device
                elif action == "DISABLE":
                    print(f"[TASK {task_id}] DISABLE for {full_name} (ZKT ID {zkt_user_id})")

                    if member_id is None:
                        print(f"[TASK {task_id}] No member_id, cannot backup fingerprints; skipping delete.")
                        success = False
                    else:
                        try:
                            all_templates = conn.get_templates() or []
                        except Exception as e:
                            print("[FINGER BACKUP] Error fetching templates from device:", e)
                            if is_device_connection_error(e):
                                raise
                            all_templates = []

                        user_fingers = [
                            f for f in all_templates
                            if int(f.uid) == int(zkt_user_id)
                        ]

                        print(
                            f"[FINGER BACKUP] Found {len(user_fingers)} template(s) on device "
                            f"for user uid={zkt_user_id}."
                        )

                        if backup_fingerprints_to_django(member_id, user_fingers):
                            if delete_zk_user(conn, zkt_user_id):
                                print(f"[DEVICE] User {zkt_user_id} deleted from device (door access revoked).")
                                success = True
                            else:
                                print(f"[TASK {task_id}] Failed to delete user {zkt_user_id} from device.")
                                success = False
                        else:
                            print(f"[TASK {task_id}] Backup failed, NOT deleting user {zkt_user_id}.")
                            success = False

                # 3) DELETE → hard delete from device (no template restore)
                elif action == "DELETE":
                    # Priority: use payload first, fallback to member DB value
                    payload_zkt_id = payload.get("zkt_user_id")
                    print(payload)
                    
                    delete_id = payload_zkt_id if payload_zkt_id is not None else zkt_user_id
                    
                    print(f"[TASK {task_id}] DELETE → Removing device user ID {delete_id}")

                    try:
                        success = delete_zk_user(conn, delete_id)

                        if success:
                            print(f"[TASK {task_id}] Successfully deleted device user {delete_id}")
                            mark_task_processed(task_id)
                            any_success = True
                        else:
                            print(f"[TASK {task_id}] Device deletion failed for ID {delete_id}")
                    except Exception as e:
                        print(f"[TASK {task_id}] Error while deleting: {e}")
                        if is_device_connection_error(e):
                            raise
                if success:
                    any_success = True
                    mark_task_processed(task_id)
                else:
                    print(f"[TASK {task_id}] Device operation failed; task not marked processed.")

            except Exception as e:
                print(f"[TASK {task_id}] Error applying on device:", e)
                if is_device_connection_error(e):
                    # Bubble up so sync_loop can reconnect
                    raise

    return any_success


# ======================================================
# -------------------- MAIN SYNC LOOP ------------------
# ======================================================

def sync_loop():
    """
    Main loop:

    - Uses cached ZKT IP for fast connect.
    - Falls back to network scan only when needed.
    - Polls Django for DeviceSyncTasks (users & manual SYNC_ATTENDANCE).
    - DAILY:
        * Once per day after AUTO_JOB_HOUR:AUTO_JOB_MINUTE (Asia/Kathmandu):
            - membership_daily_update on Django
            - automatic full attendance sync
    """
    ip, zk, conn = discover_zkt_device_with_cache()
    if not conn:
        print("[FATAL] Could not connect to any ZKT device. Exiting with failure so systemd can retry.")
        raise SystemExit(1)

    print(f"[OK] Connected to ZKT at {ip}")

    # Monotonic time for short intervals (task polling)
    now = time.monotonic()
    next_task_check_at = now
    task_interval = TASK_POLL_IDLE_SECONDS

    # Track daily jobs (by date tuple: (year, month, day))
    last_membership_update_date = None
    last_attendance_auto_sync_date = None

    # Track when we last saw *any* task (for long idle mode)
    last_task_seen_at = time.monotonic()

    while True:
        now = time.monotonic()
        now_dt = get_kathmandu_now()
        today_tuple = (now_dt.year, now_dt.month, now_dt.day)

        try:
            # 1) Regular device sync tasks (ADD_ENABLE / DISABLE / DELETE / SYNC_ATTENDANCE)
            if now >= next_task_check_at:
                tasks = fetch_device_sync_tasks()
                if tasks:
                    last_task_seen_at = now
                    any_success = apply_tasks_to_device(conn, tasks)
                    task_interval = TASK_POLL_ACTIVE_SECONDS if any_success else TASK_POLL_IDLE_SECONDS
                else:
                    # No tasks – maybe switch to long idle if quiet for a long time
                    idle_for = now - last_task_seen_at
                    if idle_for > NO_TASKS_LONG_IDLE_AFTER:
                        task_interval = TASK_POLL_LONG_IDLE_SECONDS
                    else:
                        task_interval = TASK_POLL_IDLE_SECONDS

                next_task_check_at = now + task_interval

            # 2) Once per day after 23:40 (membership update)
            # if (
            #     (last_membership_update_date != today_tuple) and
            #     (now_dt.hour > AUTO_JOB_HOUR or
            #      (now_dt.hour == AUTO_JOB_HOUR and now_dt.minute >= AUTO_JOB_MINUTE))
            # ):
            #     print("\n[SCHEDULER] Running daily membership update (Asia/Kathmandu)...")
            #     run_membership_daily_update()
            #     last_membership_update_date = today_tuple


        except KeyboardInterrupt:
            print("\n[INFO] KeyboardInterrupt received, exiting cleanly.")
            try:
                conn.disconnect()
            except Exception:
                pass
            break

        except Exception as e:
            print("[ERROR] In main sync loop:", e)
            print("[INFO] Attempting to reconnect to ZKT device in 5 seconds...")
            time.sleep(5)
            try:
                # Reuse the same logic: cached IP first, then scan
                ip, zk, conn = discover_zkt_device_with_cache()
                if not conn:
                    print("[WARN] Rediscovery failed, will retry in 10 seconds...")
                    time.sleep(10)
                    continue
                print(f"[OK] Reconnected to ZKT at {ip}")

                # Reset polling schedules
                now = time.monotonic()
                next_task_check_at = now
                # Daily flags remain; if jobs were already run today, they won't rerun.
            except Exception as e2:
                print("[FATAL] Rediscovery error:", e2)
                time.sleep(10)
                continue

        time.sleep(1)  # avoid busy loop


# ======================================================
# ------------------------- MAIN -----------------------
# ======================================================

if __name__ == "__main__":
    print("=== Gym Access Sync (Daily Membership & Attendance — Optimized) ===")
    print(f"Using Django backend at: {DJANGO_BASE_URL}")
    print(f"ZKT IP cache file: {ZKT_IP_CACHE_FILE}")
    if KATHMANDU_TZ is not None:
        print("Scheduler timezone: Asia/Kathmandu")
    else:
        print("Scheduler timezone: SYSTEM LOCALTIME (zoneinfo not available)")
    print(f"Daily job time: {AUTO_JOB_HOUR:02d}:{AUTO_JOB_MINUTE:02d} (Asia/Kathmandu)")
    print(f"DEBUG logging: {DEBUG}")
    sync_loop()
