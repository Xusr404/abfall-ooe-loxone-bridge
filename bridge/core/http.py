"""HTTP endpoint handlers for the Loxone bridge."""

from __future__ import annotations

import asyncio
import json
import hashlib
from pathlib import Path
from time import perf_counter
from typing import Any

from aiohttp import web

from .common import (
    LOGGER, LOCATION_FIELDS, NAME_FIELDS, normalized_name_key, normalize_name,
)

from .auth import require_api_key


def selection_map(request, name):
    try:
        value = json.loads(request.query.get(name, "{}"))
        if not isinstance(value, dict) or len(value) > 64 or any(
            not isinstance(k, str) or not k.strip() or len(k) > 128 or
            not isinstance(v, str) or not v or len(v) > 128
            for k, v in value.items()
        ):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_" + name}), content_type="application/json")


class BridgeHttpMixin:
    async def handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def handle_logs(self, request: web.Request) -> web.Response:
        require_api_key(request, self.api_key)

        try:
            line_count = int(request.query.get("lines", "100"))
        except ValueError as err:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "invalid_lines"}),
                content_type="application/json",
            ) from err
        if line_count < 1:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "invalid_lines"}),
                content_type="application/json",
            )
        line_count = min(line_count, 300)

        def read_tail() -> str:
            lines: list[str] = []
            # RotatingFileHandler names the newest archive .1 and older .2.
            paths = [Path(f"{self.log_file}.2"), Path(f"{self.log_file}.1"), self.log_file]
            for path in paths:
                try:
                    lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
                except FileNotFoundError:
                    continue
            return "\n".join(lines[-line_count:])

        log_text = await asyncio.to_thread(read_tail)
        return web.Response(
            text=log_text,
            content_type="text/plain",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )

    async def handle_pickups(self, request: web.Request) -> web.Response:
        started = perf_counter()
        supplied_names = {field: request.query.get(field, "").strip() for field in NAME_FIELDS}
        if all(supplied_names.values()):
            address_key = normalized_name_key(supplied_names)[:12]
            address_input = "names"
        else:
            address_key = "unknown"
            address_input = "incomplete"
        event: dict[str, Any] = {
            "address": address_key,
            "input": address_input,
        }
        try:
            response = await self._handle_pickups(request)
        except web.HTTPException as err:
            event.update(
                http_status=err.status,
                outcome="error",
                error=err.reason,
                duration_ms=round((perf_counter() - started) * 1000),
            )
            LOGGER.warning(
                "Pickup bridge request %s",
                json.dumps(event, ensure_ascii=False, separators=(",", ":")),
            )
            raise
        except Exception as err:
            event.update(
                http_status=500,
                outcome="error",
                error=type(err).__name__,
                duration_ms=round((perf_counter() - started) * 1000),
            )
            LOGGER.exception(
                "Pickup bridge request %s",
                json.dumps(event, ensure_ascii=False, separators=(",", ":")),
            )
            raise

        event.update(http_status=response.status, outcome="success")
        try:
            body = json.loads(response.body.decode("utf-8"))
            for field in ("status", "cache_stale", "cache_age_seconds"):
                if field in body:
                    event[field] = body[field]
            next_pickup = body.get("next_pickup")
            if isinstance(next_pickup, dict):
                event["next_date"] = next_pickup.get("date")
                event["next_types"] = ",".join(next_pickup.get("waste_types", []))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        event["duration_ms"] = round((perf_counter() - started) * 1000)
        LOGGER.info(
            "Pickup bridge request %s",
            json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        )
        return response

    async def _handle_pickups(self, request: web.Request) -> web.Response:
        require_api_key(request, self.api_key)

        intervals = selection_map(request, "intervals")
        labels = {field: request.query.get(field, "").strip() for field in NAME_FIELDS}
        if any(len(value) > 128 for value in labels.values()):
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "address_name_too_long"}),
                content_type="application/json",
            )

        if not all(labels.values()):
            raise web.HTTPBadRequest(text=json.dumps({"error": "provide_all_address_names", "fields": list(NAME_FIELDS)}), content_type="application/json")
        if any(field in request.query for field in LOCATION_FIELDS) or "interval_titles" in request.query:
            raise web.HTTPBadRequest(text=json.dumps({"error": "use_address_and_interval_names"}), content_type="application/json")
        key = normalized_name_key(labels)
        await self.invalidate_memory_if_cache_deleted()
        values = self.name_aliases.get(key, {})
        if not values and not any("|" in value for value in labels.values()):
            legacy = "names|" + "|".join(labels[f].strip().casefold() for f in NAME_FIELDS)
            values = self.name_aliases.get(hashlib.sha256(legacy.encode("utf-8")).hexdigest(), {})
        entry, stale, refresh_error = await self.get_calendar(values, labels)
        if request.path.endswith("/calendar-intervals"):
            names = entry.get("waste_type_names", {row["waste_type_id"]: row["waste_type"] for row in entry.get("pickups", [])})
            titles = [normalize_name(names[k]) for k in entry.get("type_intervals", {}) if k in names]
            if len(titles) != len(set(titles)):
                raise web.HTTPUnprocessableEntity(text=json.dumps({"error": "address_ambiguous", "field": "waste_type"}), content_type="application/json")
            choices = {names[k]: list(v.values()) for k, v in entry.get("type_intervals", {}).items() if k in names}
            return web.json_response({"choices": choices, "cache_stale": stale,
                                      "last_successful_fetch_at": entry["fetched_at"], "refresh_error": refresh_error},
                                     headers={"Cache-Control": "no-store"})
        data = self.response_data(
            entry,
            labels,
            stale,
            refresh_error,
            details=request.query.get("details") == "1",
            intervals=intervals,
        )
        return web.json_response(
            data,
            dumps=lambda value: json.dumps(value, ensure_ascii=False),
            headers={"Cache-Control": "no-store"},
        )
