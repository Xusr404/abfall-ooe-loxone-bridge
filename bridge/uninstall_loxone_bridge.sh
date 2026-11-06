#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR=/opt/loxone-bridge
CONFIG_DIR=/etc/loxone-bridge
STATE_DIR=/var/lib/loxone-bridge
LOG_DIR=/var/log/loxone-bridge
UNIT_FILE=/etc/systemd/system/loxone-bridge.service

fail() { echo "Error: $*" >&2; exit 1; }
[[ "${EUID}" -eq 0 ]] || fail "Run with sudo: sudo bash $0 [--purge]"
PURGE=false
if [[ $# -eq 1 && "$1" == --purge ]]; then PURGE=true
elif [[ $# -ne 0 ]]; then fail "Usage: sudo bash $0 [--purge]"; fi
[[ -d "$INSTALL_DIR" && ! -L "$INSTALL_DIR" && -f "$INSTALL_DIR/.managed-install" ]] || \
    fail "No managed installation found at $INSTALL_DIR."
if [[ -e "$UNIT_FILE" ]]; then
    [[ ! -L "$UNIT_FILE" ]] || fail "The service definition is a symlink; inspect it before removal."
    grep -q '^Description=Abfall OÖ Loxone bridge' "$UNIT_FILE" || fail "The service definition belongs to another application."
    systemctl disable --now loxone-bridge.service
    rm -f -- "$UNIT_FILE"
    systemctl daemon-reload
fi
# Fixed installer-owned location; never remove the user's source checkout.
rm -rf -- "$INSTALL_DIR"
if [[ "$PURGE" == true ]]; then
    rm -rf -- "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR"
    if getent passwd loxone-bridge >/dev/null; then userdel loxone-bridge; fi
    if getent group loxone-bridge >/dev/null; then groupdel loxone-bridge; fi
    echo "Bridge removed, including its settings, saved data, logs, and service account."
else
    echo "Bridge removed. Settings, saved data, logs, and the service account were retained."
    echo "To delete these later, reinstall and uninstall with --purge."
fi
