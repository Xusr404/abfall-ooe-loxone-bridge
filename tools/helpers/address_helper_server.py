"""Run the local address picker used to build Abfall OÖ bridge URLs."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import time
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiohttp
from aiohttp import web

BRIDGE_DIR = Path(__file__).resolve().parents[2] / "bridge"
sys.path.insert(0, str(BRIDGE_DIR))

from core.auth import require_api_key, validate_api_key
from abfall_ooe.api import AbfallOoeClient, AbfallOoeError
from refresh_address_data import DEFAULT_OUTPUT, build_snapshot, write_snapshot, validate_snapshot
from core.address import resolve_address
from core.common import NAME_FIELDS, InvalidAddressError, normalized_name_key
from core.lookup import temporary_error

LOGGER = logging.getLogger("abfall_ooe.address_helper_server")
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
HTML_FILE = Path(__file__).resolve().with_name("address-helper.html")
ADDRESS_DATA_FILE = DEFAULT_OUTPUT
def _labels(request, depth):
    labels = {field: request.query.get(field, "").strip() for field in NAME_FIELDS[:depth]}
    if not all(labels.values()) or any(len(v) > 128 for v in labels.values()):
        raise web.HTTPBadRequest(text=json.dumps({"error": "provide_address_names", "fields": list(labels)}), content_type="application/json")
    return labels


async def _options(request, depth, lookup):
    labels = _labels(request, depth)
    bridge = request.app["bridge"]

    async def fetch():
        for attempt in range(2):
            try:
                async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
                    client = AbfallOoeClient(session)
                    values = await resolve_address(client, labels, depth)
                    return await lookup(client, values)
            except InvalidAddressError as err:
                raise web.HTTPUnprocessableEntity(text=json.dumps({"error": str(err), "field": err.field}), content_type="application/json") from err
            except (AbfallOoeError, aiohttp.ClientError, TimeoutError, OSError):
                bridge.request_address_refresh()
                if attempt:
                    raise
                await asyncio.sleep(bridge.recovery_backoff)
    try:
        options = await bridge.lookups.run("options:" + str(depth) + ":" + normalized_name_key(labels), fetch)
    except (AbfallOoeError, aiohttp.ClientError, TimeoutError, OSError) as err:
        raise temporary_error() from err
    return web.json_response(
        [{"id": name, "name": name} for name in options.values()],
        headers={"Cache-Control": "no-store"},
    )


async def handle_page(_request: web.Request) -> web.Response:
    return web.FileResponse(HTML_FILE, headers={"Cache-Control": "no-store"})


async def handle_address_data(_request: web.Request) -> web.Response:
    if not ADDRESS_DATA_FILE.is_file():
        raise web.HTTPNotFound(
            text=json.dumps({"error": "address_data_missing", "hint": "Use the address refresh button on the setup page"}),
            content_type="application/json",
        )
    return web.FileResponse(ADDRESS_DATA_FILE, headers={"Cache-Control": "no-store"})


async def _run_address_refresh(app: web.Application) -> None:
    state = app["address_refresh_state"]
    state.update(status="running", message="Adressliste wird geladen …", error=None)

    def report_progress(message: str) -> None:
        state["message"] = message

    try:
        async with asyncio.timeout(900):
            snapshot = await build_snapshot(delay=0.05, progress=report_progress)
        validate_snapshot(snapshot)
        # This dataset is deliberately separate from live name-to-ID mappings
        # and calendars. A slow full crawl cannot overwrite a newer recovery.
        publication = asyncio.create_task(asyncio.to_thread(write_snapshot, ADDRESS_DATA_FILE, snapshot))
        try:
            await asyncio.shield(publication)
        except asyncio.CancelledError:
            # Finish an already-started atomic publication before shutdown.
            await publication
            raise
        state.update(
            status="complete",
            message="Adressliste wurde aktualisiert.",
            updated_at=snapshot["updated_at"],
        )
    except asyncio.CancelledError:
        state.update(status="error", message="Aktualisierung wurde abgebrochen.", error="cancelled")
        raise
    except Exception as err:
        LOGGER.warning("Address list refresh failed (%s): %s", type(err).__name__, err)
        state.update(
            status="error",
            message="Adressliste konnte nicht aktualisiert werden. Prüfe die Internetverbindung und versuche es erneut.",
            error=type(err).__name__,
        )


async def handle_refresh_address_data(request: web.Request) -> web.Response:
    if request.headers.get("X-Refresh-Request") != "1":
        raise web.HTTPForbidden(text="Refresh request header required")
    bridge = request.app.get("bridge")
    require_api_key(request, bridge.api_key if bridge else request.app["api_key"])
    start_address_refresh(request.app)
    return web.json_response(
        request.app["address_refresh_state"],
        status=202,
        headers={"Cache-Control": "no-store"},
    )


def start_address_refresh(app):
    task = app.get("address_refresh_task")
    if task is None or task.done():
        marker = ADDRESS_DATA_FILE.with_name("address-refresh-attempt.json")
        try:
            last = json.loads(marker.read_text(encoding="utf-8"))["started_at"]
            if not isinstance(last, (int, float)) or not math.isfinite(last):
                raise ValueError("invalid marker")
        except FileNotFoundError:
            last = 0
        except (ValueError, KeyError, TypeError, OSError):
            raise web.HTTPServiceUnavailable(text="Refresh cooldown state unreadable")
        now = time.time()
        remaining = math.ceil(last + 3600 - now)
        if remaining > 0:
            raise web.HTTPTooManyRequests(
                text=json.dumps({"error": "refresh_cooldown"}), content_type="application/json",
                headers={"Retry-After": str(remaining)},
            )
        # No await between the task check, durable marker and task creation.
        try:
            write_snapshot(marker, {"started_at": now})
        except OSError:
            raise web.HTTPServiceUnavailable(text="Cannot persist refresh cooldown")
        app["address_refresh_state"].update(
            status="running",
            message="Adressliste wird vorbereitet …",
            error=None,
        )
        app["address_refresh_task"] = asyncio.create_task(
            _run_address_refresh(app)
        )


async def handle_refresh_status(request: web.Request) -> web.Response:
    return web.json_response(
        request.app["address_refresh_state"],
        headers={"Cache-Control": "no-store"},
    )


async def stop_address_refresh(app: web.Application) -> None:
    task = app.get("address_refresh_task")
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def handle_districts(request):
    return await _options(request, 0, lambda client, values: client.get_districts())


async def handle_cities(request):
    return await _options(request, 1, lambda client, v: client.get_cities(v["district_id"]))


async def handle_streets(request):
    return await _options(request, 2, lambda client, v: client.get_streets(v["city_id"]))


async def handle_house_numbers(request):
    return await _options(request, 3, lambda client, v: client.get_house_numbers(v["street_id"]))


async def handle_intervals(request):
    return await _options(request, 4, lambda client, v: client.get_intervals(v["street_id"], v["house_number_id"]))


def create_app(bridge=None) -> web.Application:
    standalone = bridge is None
    if standalone:
        from core.service import PickupBridge
        bridge = PickupBridge(BRIDGE_DIR / "data" / "loxone_bridge_cache.json",
                              os.environ.get("LOXONE_BRIDGE_API_KEY"),
                              BRIDGE_DIR / "data" / "loxone_bridge.log")
    app = web.Application(client_max_size=16 * 1024)
    if standalone:
        app.on_startup.append(bridge.start)
        app.on_cleanup.append(bridge.close)
    app["bridge"] = bridge
    app["api_key"] = bridge.api_key if bridge else os.environ.get("LOXONE_BRIDGE_API_KEY")
    validate_api_key(app["api_key"])
    if bridge is not None:
        app.router.add_get("/api/v1/calendar-intervals", bridge._handle_pickups)
    app["address_refresh_state"] = {"status": "idle", "message": "", "updated_at": None, "error": None}
    bridge.address_refresh = lambda: start_address_refresh(app)
    app.on_cleanup.append(stop_address_refresh)
    app.router.add_get("/", handle_page)
    app.router.add_get("/api/v1/address-data", handle_address_data)
    app.router.add_post("/api/v1/address-data/refresh", handle_refresh_address_data)
    app.router.add_get("/api/v1/address-data/refresh", handle_refresh_status)
    app.router.add_get("/api/v1/locations/districts", handle_districts)
    app.router.add_get("/api/v1/locations/cities", handle_cities)
    app.router.add_get("/api/v1/locations/streets", handle_streets)
    app.router.add_get("/api/v1/locations/house-numbers", handle_house_numbers)
    app.router.add_get("/api/v1/locations/intervals", handle_intervals)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Abfall OÖ address picker and URL builder.")
    parser.add_argument("--host", default="127.0.0.1", help="local helper listen address")
    parser.add_argument("--port", type=int, default=8766, help="local helper listen port")
    args = parser.parse_args()
    print(f"Open http://{args.host}:{args.port}/ in your browser")
    web.run_app(create_app(), host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
