#!/usr/bin/env bash
# Install, update, remove, or inspect pi.py as a systemd service.
# Usage: sudo bash setup_service.sh {install|update|uninstall|status|logs}

set -Eeuo pipefail

SERVICE_NAME="pi-service"
INSTALL_DIR="/opt/pi-service"
VENV_DIR="${INSTALL_DIR}/venv"
APP_FILE="${INSTALL_DIR}/pi.py"
REQUIREMENTS_FILE="${INSTALL_DIR}/requirements.txt"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

info() { printf '[INFO] %s\n' "$*"; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: sudo bash $(basename "$0") {install|update|uninstall|status|logs}

Commands:
  install    Copy pi.py, create a virtual environment, install requirements,
             and enable/start the service.
  update     Copy the current files, refresh requirements, and restart service.
  uninstall  Stop/disable the service and remove its installed files.
  status     Show systemd service status.
  logs       Follow the service journal.
EOF
}

need_root() {
    [[ "${EUID}" -eq 0 ]] || die "Run this command with sudo."
}

check_source_files() {
    [[ -f "${SCRIPT_DIR}/pi.py" ]] || die "pi.py was not found beside this installer: ${SCRIPT_DIR}/pi.py"
    [[ -f "${SCRIPT_DIR}/requirements.txt" ]] || die "requirements.txt was not found beside this installer: ${SCRIPT_DIR}/requirements.txt"
}

install_or_update() {
    need_root
    check_source_files

    command -v systemctl >/dev/null 2>&1 || die "systemd/systemctl is required."
    command -v apt-get >/dev/null 2>&1 || die "This installer expects a Debian-based Raspberry Pi OS with apt-get."

    info "Installing Python virtual environment support"
    apt-get update
    apt-get install -y python3-venv

    mkdir -p "${INSTALL_DIR}"
    install -m 0755 "${SCRIPT_DIR}/pi.py" "${APP_FILE}"
    install -m 0644 "${SCRIPT_DIR}/requirements.txt" "${REQUIREMENTS_FILE}"

    if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
        info "Creating virtual environment at ${VENV_DIR}"
        python3 -m venv "${VENV_DIR}"
    fi

    info "Installing Python dependencies"
    "${VENV_DIR}/bin/python" -m pip install --upgrade pip
    "${VENV_DIR}/bin/python" -m pip install -r "${REQUIREMENTS_FILE}"

    cat >"${SERVICE_FILE}" <<EOF
[Unit]
Description=Raspberry Pi Python Application (pi.py)
After=network.target NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${VENV_DIR}/bin/python ${APP_FILE}
Restart=on-failure
RestartSec=5
User=root
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}.service"
    systemctl restart "${SERVICE_NAME}.service"

    info "Service installed and started."
    printf 'Status:  sudo systemctl status %s\n' "${SERVICE_NAME}"
    printf 'Logs:    sudo journalctl -u %s -f\n' "${SERVICE_NAME}"
    printf 'Restart: sudo systemctl restart %s\n' "${SERVICE_NAME}"
}

uninstall() {
    need_root
    if systemctl is-active --quiet "${SERVICE_NAME}.service" 2>/dev/null; then
        systemctl stop "${SERVICE_NAME}.service"
    fi
    if systemctl is-enabled --quiet "${SERVICE_NAME}.service" 2>/dev/null; then
        systemctl disable "${SERVICE_NAME}.service"
    fi
    rm -f "${SERVICE_FILE}"
    systemctl daemon-reload
    rm -rf "${INSTALL_DIR}"
    info "Removed ${SERVICE_NAME} and ${INSTALL_DIR}."
}

ACTION="${1:-}"
case "${ACTION}" in
    install|update)
        install_or_update
        ;;
    uninstall)
        uninstall
        ;;
    status)
        systemctl status "${SERVICE_NAME}.service" --no-pager
        ;;
    logs)
        journalctl -u "${SERVICE_NAME}.service" -f
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

