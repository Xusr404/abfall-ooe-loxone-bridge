#!/usr/bin/env bash
set -euo pipefail

# Debian/Raspberry Pi OS installer. Never fetches address or calendar data.
INSTALL_DIR=/opt/loxone-bridge
CONFIG_DIR=/etc/loxone-bridge
STATE_DIR=/var/lib/loxone-bridge
LOG_DIR=/var/log/loxone-bridge
UNIT_FILE=/etc/systemd/system/loxone-bridge.service
SERVICE_USER=loxone-bridge

fail() { echo "Error: $*" >&2; exit 1; }
[[ "${EUID}" -eq 0 ]] || fail "Run with sudo: sudo bash $0"
umask 022
[[ $# -eq 0 ]] || fail "This installer does not accept arguments. See docs/INSTALLATION.md."
[[ -r /etc/os-release ]] || fail "Debian or Raspberry Pi OS is required."
. /etc/os-release
[[ "${ID:-} ${ID_LIKE:-}" =~ (debian|raspbian) ]] || fail "Debian or Raspberry Pi OS is required."
[[ -d /run/systemd/system ]] || fail "systemd must be running on this machine."
for tool in systemctl journalctl python3 tar install getent useradd groupadd; do
    command -v "$tool" >/dev/null || fail "Required command missing: $tool"
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || fail "Python 3.11 or newer is required."
python3 -c 'import venv; from zoneinfo import ZoneInfo; ZoneInfo("Europe/Vienna")' || \
    fail "Install Python environment support and timezone data: sudo apt install python3-venv tzdata"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
[[ -f "$PROJECT_DIR/tools/helpers/address-helper.html" ]] || fail "Run the installer from a complete project checkout."
if [[ -e "$INSTALL_DIR" || -L "$INSTALL_DIR" ]]; then
    [[ -d "$INSTALL_DIR" && ! -L "$INSTALL_DIR" && -f "$INSTALL_DIR/.managed-install" ]] || \
        fail "$INSTALL_DIR already exists and is not managed by this installer."
fi
[[ ! -e "$INSTALL_DIR/current" || -L "$INSTALL_DIR/current" ]] || fail "$INSTALL_DIR/current must be a symlink."
if [[ -e "$UNIT_FILE" ]]; then
    [[ ! -L "$UNIT_FILE" ]] || fail "Refusing to replace a symlink at $UNIT_FILE."
    grep -q '^Description=Abfall OÖ Loxone bridge' "$UNIT_FILE" || fail "An unrelated service already exists at $UNIT_FILE."
fi
if getent passwd "$SERVICE_USER" >/dev/null; then
    [[ "$(id -u "$SERVICE_USER")" -ne 0 ]] || fail "The service account must not be root."
    case "$(getent passwd "$SERVICE_USER" | cut -d: -f7)" in
        */nologin|*/false) ;;
        *) fail "The existing $SERVICE_USER account permits login; a dedicated service account is required." ;;
    esac
fi

# Prepare the complete release before stopping an existing service.
install -d -m 0755 "$INSTALL_DIR" "$INSTALL_DIR/releases"
touch "$INSTALL_DIR/.managed-install"
RELEASE_DIR="$(mktemp -d "$INSTALL_DIR/releases/release.XXXXXXXX")"
chmod 0755 "$RELEASE_DIR"
tar -C "$PROJECT_DIR" --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='bridge/data' --exclude='tools/helpers/data' \
    -cf - bridge tools/helpers docs README.md LICENSE | tar --no-same-owner -xf - -C "$RELEASE_DIR"
chmod -R a+rX,go-w "$RELEASE_DIR"
python3 -m venv "$RELEASE_DIR/.venv" || fail "Could not create Python environment. Install python3-venv and retry."
"$RELEASE_DIR/.venv/bin/python" -m pip install -r "$RELEASE_DIR/bridge/requirements.txt"
"$RELEASE_DIR/.venv/bin/python" -m pip check

getent group "$SERVICE_USER" >/dev/null || groupadd --system "$SERVICE_USER"
getent passwd "$SERVICE_USER" >/dev/null || useradd --system --gid "$SERVICE_USER" \
    --home-dir "$STATE_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -o root -g root -m 0700 "$CONFIG_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$STATE_DIR" "$LOG_DIR"
[[ ! -L "$CONFIG_DIR/bridge.env" ]] || fail "Configuration must not be a symlink."
if [[ ! -e "$CONFIG_DIR/bridge.env" ]]; then
    install -o root -g root -m 0600 "$RELEASE_DIR/bridge/bridge.env.example" "$CONFIG_DIR/bridge.env"
fi
[[ -f "$CONFIG_DIR/bridge.env" && ! -L "$CONFIG_DIR/bridge.env" ]] || fail "Configuration must be a regular file."
chown root:root "$CONFIG_DIR/bridge.env"
chmod 0600 "$CONFIG_DIR/bridge.env"

BACKUP_DIR="$(mktemp -d)"
trap 'rm -rf -- "$BACKUP_DIR"' EXIT
PREVIOUS_RELEASE="$(readlink "$INSTALL_DIR/current" || true)"
HAD_UNIT=false
WAS_ACTIVE=false
WAS_ENABLED=false
if [[ -f "$UNIT_FILE" ]]; then
    cp -p "$UNIT_FILE" "$BACKUP_DIR/previous.service"
    HAD_UNIT=true
    systemctl is-active --quiet loxone-bridge.service && WAS_ACTIVE=true
    systemctl is-enabled --quiet loxone-bridge.service && WAS_ENABLED=true
    systemctl stop loxone-bridge.service
fi
SWITCHED=false
rollback() {
    local status=$?
    trap - ERR
    echo "Installation failed. Restoring the previous service definition and release." >&2
    if [[ "$SWITCHED" == true ]]; then
        systemctl stop loxone-bridge.service || true
        if [[ -n "$PREVIOUS_RELEASE" ]]; then
            ln -sfnT "$PREVIOUS_RELEASE" "$INSTALL_DIR/current"
        else
            rm -f -- "$INSTALL_DIR/current"
        fi
    fi
    if [[ "$HAD_UNIT" == true ]]; then
        cp -p "$BACKUP_DIR/previous.service" "$UNIT_FILE"
    else
        systemctl disable loxone-bridge.service || true
        rm -f -- "$UNIT_FILE"
    fi
    systemctl daemon-reload || true
    if [[ "$HAD_UNIT" == true ]]; then
        if [[ "$WAS_ENABLED" == true ]]; then systemctl enable loxone-bridge.service || true
        else systemctl disable loxone-bridge.service || true; fi
        if [[ "$WAS_ACTIVE" == true ]]; then systemctl start loxone-bridge.service || true; fi
    fi
    echo "Settings and saved data were retained. Check: sudo journalctl -u loxone-bridge -n 50 --no-pager" >&2
    exit "$status"
}
trap rollback ERR

# Migrate old checkout data only when no installed data already exists.
for pair in "bridge/data/loxone_bridge_cache.json:loxone_bridge_cache.json" \
            "tools/helpers/data/address-data.json:address-data.json"; do
    source_file="$PROJECT_DIR/${pair%%:*}"
    destination="$STATE_DIR/${pair#*:}"
    if [[ -f "$source_file" && ! -e "$destination" ]]; then
        install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 "$source_file" "$destination"
    fi
done
if [[ -f "$PROJECT_DIR/bridge/data/loxone_bridge.log" && ! -e "$LOG_DIR/loxone_bridge.log" ]]; then
    install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 "$PROJECT_DIR/bridge/data/loxone_bridge.log" "$LOG_DIR/loxone_bridge.log"
fi

cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Abfall OÖ Loxone bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR/current
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
Environment=LOXONE_BRIDGE_ADDRESS_DATA_FILE=$STATE_DIR/address-data.json
EnvironmentFile=$CONFIG_DIR/bridge.env
ExecStart=$INSTALL_DIR/current/.venv/bin/python $INSTALL_DIR/current/bridge/loxone_bridge.py --cache-file $STATE_DIR/loxone_bridge_cache.json --log-file $LOG_DIR/loxone_bridge.log
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$STATE_DIR $LOG_DIR

[Install]
WantedBy=multi-user.target
EOF

ln -sfnT "$RELEASE_DIR" "$INSTALL_DIR/current"
SWITCHED=true
systemctl daemon-reload
systemctl enable loxone-bridge.service
systemctl restart loxone-bridge.service
if ! "$RELEASE_DIR/.venv/bin/python" "$RELEASE_DIR/bridge/check_service_health.py"; then
    journalctl -u loxone-bridge.service -n 25 --no-pager >&2 || true
    false # Trigger rollback on a failed local health check.
fi
systemctl is-active --quiet loxone-bridge.service
trap - ERR
echo "Bridge installed and its local health check passed."
echo "Settings: $CONFIG_DIR/bridge.env (preserved on updates)."
echo "Open http://SERVER_IP:YOUR_CONFIGURED_PORT (default: 8765) on your trusted home network."
echo "No address data was downloaded. Use Adressen aktualisieren on the setup page when ready."
echo "To require an API key, edit the settings and run: sudo systemctl restart loxone-bridge"
