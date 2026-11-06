# Debian and Raspberry Pi OS installation

Use Debian or Raspberry Pi OS with systemd and Python 3.11 or newer (for example, Debian 12 or Raspberry Pi OS Bookworm). Install prerequisites if needed:

```sh
sudo apt update
sudo apt install python3 python3-venv tzdata
sudo bash bridge/install_loxone_bridge.sh
```

Run the installer from a complete source checkout. It prepares a Python environment and dependencies, installs the bridge, enables it at boot, and verifies its local `/health` endpoint. Installing Python packages requires access to the package index. Installation and verification never fetch Umweltprofis address or calendar data.

The bridge runs under the dedicated `loxone-bridge` account, without login or root privileges. Application code is read-only to the service; only its data and log directories are writable.

| Location | Contents |
| --- | --- |
| `/opt/loxone-bridge/releases/` | Installed application releases and their Python environments |
| `/opt/loxone-bridge/current` | Link to the active release |
| `/etc/loxone-bridge/bridge.env` | Protected settings, readable by root |
| `/var/lib/loxone-bridge/` | Pickup cache and address list |
| `/var/log/loxone-bridge/` | Rotating bridge logs |

Open `http://SERVER_IP:8765` on your home network. If no address list has been saved, use **Adressen aktualisieren** on the setup page when you want to download it. Loading the setup page and the installer's health check do not initiate that download.

## Settings

Edit the configuration and restart the service:

```sh
sudoedit /etc/loxone-bridge/bridge.env
sudo systemctl restart loxone-bridge
```

The file contains these settings:

```text
LOXONE_BRIDGE_HOST=0.0.0.0
LOXONE_BRIDGE_PORT=8765
LOXONE_BRIDGE_API_KEY=
```

Use a numeric listening IP address or `localhost`. The default listens on all IPv4 interfaces. Use `127.0.0.1` for access only from the server, or `::` for all IPv6 interfaces. Set the port to a number between 1024 and 65535; the service runs without privileges to bind low ports. The installer checks the listening address and port through the service's effective environment without executing the settings file.

Leave the API key empty to keep authentication optional, or enter a long, unique key and configure the same key in the Loxone setup page. This protects the pickup and log endpoints; it does not protect the setup page or ordinary address lookups. Starting a full refresh and loading calendar interval choices require the configured key. Keep the service on a trusted home network and do not forward its port to the internet.

The installer preserves this file during updates. Do not put shell commands in it. File permissions are reset to root-only access when the installer runs.

## Updates and migration

From your source checkout, download the desired project version and rerun:

```sh
git pull
sudo bash bridge/install_loxone_bridge.sh
```

Each run prepares a separate release before stopping the service. Settings, cached dates, downloaded addresses, and logs are retained. If the new service fails its startup check, the installer attempts to restore the previous release and service definition. Earlier releases remain available under `/opt/loxone-bridge/releases/`; they are not automatically deleted. After a successful update, you may remove old release directories, taking care to retain the target of `current` and any release you want for rollback.

When upgrading from the original installer, run the new installer from the old checkout. It copies the old pickup cache, address list, and current log only if their new destination files do not already exist. Original files are left in place. Custom settings previously added to a systemd service override are not migrated automatically: review them with `sudo systemctl cat loxone-bridge` and transfer the intended settings to `bridge.env`. Existing systemd overrides remain in effect.

The source checkout is independent of the installed release and can be moved after installation. Download future updates to the checkout and rerun the installer from there.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Setup page does not open | Confirm the server IP, configured port, service status, and local firewall settings. |
| Python environment creation fails | Install `python3-venv` for the selected Python version. |
| Service fails to start | Check the journal for an invalid listening address, occupied port, or settings error. |

```sh
sudo systemctl status loxone-bridge --no-pager
sudo journalctl -u loxone-bridge -n 50 --no-pager
```

No successful installation message is printed unless the local health check passes and the service is active. A successful health check confirms the bridge is responding locally; it does not verify upstream data or Loxone integration.

For missing addresses, authentication errors, stale dates, and pickup request errors, see [Loxone and API troubleshooting](LOXONE_BRIDGE.md#troubleshooting-requests-and-setup).

## Uninstall

Remove the service and installed application while keeping settings, saved data, logs, and the service account:

```sh
sudo bash /opt/loxone-bridge/current/bridge/uninstall_loxone_bridge.sh
```

To remove those saved files and the account as well, use this command instead. This permanently deletes saved settings, address data, cached dates, and logs:

```sh
sudo bash /opt/loxone-bridge/current/bridge/uninstall_loxone_bridge.sh --purge
```

Neither command removes your source checkout. If the application was previously uninstalled without purging, reinstall it first to use the purge option.

The one-hour address-refresh cooldown marker is stored in the state directory alongside the
address snapshot. It survives service restarts and updates. Use the configured key to start
refreshes and load interval choices; ordinary address lookups remain available on the trusted network.
See [schedule migration and refresh access](LOXONE_BRIDGE.md#schedule-selection-and-migration).
