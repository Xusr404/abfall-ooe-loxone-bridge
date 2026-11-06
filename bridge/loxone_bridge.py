"""Run the Abfall OÖ bridge HTTP service."""

from __future__ import annotations

import argparse
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys

from aiohttp import web

from core.common import LOGGER
from core.service import PickupBridge

HELPER_DIR = Path(__file__).resolve().parents[1] / "tools" / "helpers"
sys.path.insert(0, str(HELPER_DIR))
import address_helper_server


async def redirect_to_setup(_request: web.Request) -> web.HTTPFound:
    raise web.HTTPFound("/setup/")


def create_app(bridge: PickupBridge) -> web.Application:
    app = web.Application(client_max_size=16 * 1024)
    app.on_startup.append(bridge.start)
    app.on_cleanup.append(bridge.close)
    app.router.add_get("/", redirect_to_setup)
    app.router.add_get("/setup", redirect_to_setup)
    app.router.add_get("/health", bridge.handle_health)
    app.router.add_get("/api/v1/pickups", bridge.handle_pickups)
    app.router.add_get("/api/v1/logs", bridge.handle_logs)
    app.add_subapp("/setup/", address_helper_server.create_app(bridge))
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cached Abfall OÖ data bridge for Loxone.")
    parser.add_argument("--host", default=os.environ.get("LOXONE_BRIDGE_HOST", "0.0.0.0"), help="listen address (default: all interfaces; LOXONE_BRIDGE_HOST)")
    parser.add_argument("--port", type=int, default=os.environ.get("LOXONE_BRIDGE_PORT", "8765"))
    parser.add_argument(
        "--cache-hours",
        type=float,
        default=None,
        help="deprecated and ignored; successful fetches are cached for the Europe/Vienna day",
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=Path(__file__).resolve().parent / "data" / "loxone_bridge_cache.json",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=Path(__file__).resolve().parent / "data" / "loxone_bridge.log",
        help="rotating log file path (default: data/loxone_bridge.log)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("LOXONE_BRIDGE_API_KEY"),
        help="required shared key; can also be set with LOXONE_BRIDGE_API_KEY",
    )
    args = parser.parse_args()
    from core.auth import validate_api_key
    try:
        validate_api_key(args.api_key)
    except ValueError as err:
        parser.error(str(err))
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def main() -> None:
    args = parse_args()
    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                args.log_file,
                maxBytes=256 * 1024,
                backupCount=2,
                encoding="utf-8",
            ),
        ],
        force=True,
    )
    if args.cache_hours is not None:
        LOGGER.warning("--cache-hours is ignored; successful fetches are cached for the local calendar day")
    bridge = PickupBridge(
        cache_file=args.cache_file,
        api_key=args.api_key,
        log_file=args.log_file,
    )
    if not args.api_key:
        LOGGER.warning("No API key configured; pickup and log endpoint authentication is disabled")
    LOGGER.info("Listening on %s:%d; successful calendars are cached for the Europe/Vienna day", args.host, args.port)
    web.run_app(create_app(bridge), host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
