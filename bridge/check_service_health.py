"""Verify the installed service using only its local health endpoint (Linux)."""

import ipaddress
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def health_url(environment: dict[str, str]) -> str:
    host = environment.get("LOXONE_BRIDGE_HOST", "0.0.0.0")
    if host == "localhost":
        host = "127.0.0.1"
    address = ipaddress.ip_address(host)
    if address.is_unspecified:
        address = ipaddress.ip_address("::1" if address.version == 6 else "127.0.0.1")
    host = f"[{address}]" if address.version == 6 else str(address)
    port = int(environment.get("LOXONE_BRIDGE_PORT", "8765"))
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    return f"http://{host}:{port}/health"


def main() -> int:
    # Read effective process settings instead of executing or sourcing bridge.env.
    # Disable proxies so local requests never go through an external proxy.
    opener = build_opener(ProxyHandler({}), NoRedirects())
    for _ in range(20):
        try:
            pid = int(subprocess.check_output(
                ["systemctl", "show", "loxone-bridge.service", "--property=MainPID", "--value"],
                text=True,
            ).strip())
            if pid <= 0:
                raise ValueError("Service has no running process")
            raw = Path(f"/proc/{pid}/environ").read_bytes()
            environment = dict(item.decode().split("=", 1) for item in raw.split(b"\0") if b"=" in item)
            with opener.open(health_url(environment), timeout=1) as response:
                if response.status == 200 and json.load(response) == {"status": "ok"}:
                    return 0
        except (OSError, ValueError, subprocess.CalledProcessError):
            pass
        time.sleep(1)
    print("The service did not pass its local health check within the startup window.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
