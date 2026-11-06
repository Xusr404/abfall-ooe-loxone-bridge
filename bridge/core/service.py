"""Pickup calendar coordination for the Loxone bridge."""

from __future__ import annotations

import asyncio
import json
import copy
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from abfall_ooe.api import AbfallOoeClient, AbfallOoeError, Location
from abfall_ooe.parser import Pickup, filter_pickups
from .common import LOGGER, LOCAL_TZ, NAME_FIELDS, normalized_key, normalized_name_key, select_id, utc_now, iso_utc, InvalidAddressError, failure_category, serialize_pickup, deserialize_pickup
from .http import BridgeHttpMixin
from .storage import BridgeStorageMixin
from .activity import BridgeActivityMixin
from .auth import validate_api_key
from .address import resolve_address
from .lookup import LookupPool, temporary_error
from .common import LOCATION_FIELDS, normalize_name

class PickupBridge(BridgeActivityMixin, BridgeStorageMixin, BridgeHttpMixin):
    def __init__(self, cache_file: Path, api_key: str | None, log_file: Path) -> None:
        self.cache_file = cache_file
        validate_api_key(api_key)
        self.api_key = api_key
        self.log_file = log_file
        self.cache: dict[str, dict[str, Any]] = {}
        self.attempted_on: dict[str, str] = {}
        self.last_errors: dict[str, str] = {}
        self.name_aliases: dict[str, dict[str, str]] = {}
        self.persist_lock = asyncio.Lock()
        self.lookups = LookupPool()
        self.retry_at = {}
        self.retry_seconds = 60
        self.recovery_backoff = 0.2
        self.address_refresh = None
        self._init_activity()

    def request_address_refresh(self):
        # The setup app supplies the same refresh coordinator for manual and
        # automatic refreshes. It never writes calendar entries or aliases.
        if self.address_refresh is not None:
            try:
                self.address_refresh()
            except (OSError, ValueError, web.HTTPException) as err:
                LOGGER.warning("Could not schedule address refresh: %s", type(err).__name__)

    async def close(self, _app=None):
        await self.stop_calendar_maintenance()
        await self.lookups.close()

    async def _lookup_timed_out(self, key):
        self.request_address_refresh()
        self.retry_at[key] = utc_now().timestamp() + self.retry_seconds
        self.last_errors[key] = "lookup_timeout"
        await self._persist()

    async def _fetch_calendar(self, values, labels):
        for attempt in range(2):
            try:
                async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
                    client = AbfallOoeClient(session)
                    # Resolve every daily fetch, even if an alias is saved:
                    # upstream may reuse an old ID for another address.
                    resolved = await resolve_address(client, labels)
                    location = Location(*(resolved[field] for field in LOCATION_FIELDS))
                    calendar = await client.get_calendar(location)
                    saved = self.cache.get(normalized_name_key(labels), {})
                    if not calendar.pickups and any(
                        row.get("date", "") >= datetime.now(LOCAL_TZ).date().isoformat()
                        for row in saved.get("pickups", [])
                    ):
                        raise AbfallOoeError("Empty calendar while saved future dates still exist")
                    return {
                        "fetched_at": iso_utc(utc_now()), "address_ids": resolved,
                        "address_names": dict(labels),
                        "pickups": [serialize_pickup(item) for item in calendar.pickups],
                        "type_intervals": calendar.type_intervals,
                        "waste_type_names": {row.waste_type_id: row.waste_type for row in calendar.pickups},
                    }
            except InvalidAddressError:
                raise
            except (AbfallOoeError, aiohttp.ClientError, TimeoutError) as err:
                self.request_address_refresh()
                if attempt:
                    raise
                await asyncio.sleep(self.recovery_backoff)
        raise AssertionError("unreachable")

    async def get_calendar(self, values, labels):
        key = normalized_name_key(labels) if all(labels.get(f) for f in NAME_FIELDS) else normalized_key(values)
        self.record_activity(key)
        # Retain calendars written by the previous ID-keyed cache when a
        # saved name alias identifies them. Revalidate before treating them as
        # current; unknown ID-only records remain untouched, never guessed.
        if key not in self.cache and values:
            previous = self.cache.get(normalized_key(values))
            if previous:
                self.cache[key] = {**copy.deepcopy(previous), "address_names": dict(labels), "require_revalidation": True}
        entry = self.cache.get(key)
        if entry and not entry.get("require_revalidation") and entry.get("fetched_on") == datetime.now(LOCAL_TZ).date().isoformat():
            self._prune_past_pickups(entry, datetime.now(LOCAL_TZ).date())
            return entry, False, None
        try:
            return await self.lookups.run(
                "calendar:" + key, lambda: self._get_calendar(key, values, labels),
                on_timeout=lambda: self._lookup_timed_out(key),
            )
        except (TimeoutError, web.HTTPServiceUnavailable) as err:
            entry = self.cache.get(key)
            if entry:
                self._prune_past_pickups(entry, datetime.now(LOCAL_TZ).date())
                if entry.get("pickups"):
                    return entry, True, "lookup_timeout" if isinstance(err, TimeoutError) else json.loads(err.text)["error"]
            if isinstance(err, TimeoutError):
                raise temporary_error("lookup_timeout") from err
            raise

    async def _get_calendar(self, key, values, labels):
        today = datetime.now(LOCAL_TZ).date()
        entry = self.cache.get(key)
        if entry:
            self._prune_past_pickups(entry, today)
            if not entry.get("require_revalidation") and entry.get("fetched_on") == today.isoformat():
                return entry, False, None
        retry_at = self.retry_at.get(key, 0)
        if utc_now().timestamp() < retry_at:
            code = self.last_errors.get(key, "upstream_unavailable")
            if code in {"address_not_found", "address_ambiguous"}:
                raise web.HTTPUnprocessableEntity(text=json.dumps({"error": code}), content_type="application/json")
            if entry and entry.get("pickups"):
                return entry, True, code
            raise temporary_error(code, max(1, int(retry_at - utc_now().timestamp())))
        try:
            refreshed = await self._fetch_calendar(values, labels)
            if not refreshed.get("pickups") and entry and entry.get("pickups"):
                raise AbfallOoeError("Empty replacement for saved future dates")
            fetched_today = datetime.now(LOCAL_TZ).date()
            refreshed["fetched_on"] = fetched_today.isoformat()
            refreshed["had_pickups"] = bool(refreshed.get("pickups"))
            refreshed["address_names"] = dict(labels)
            refreshed.setdefault("waste_type_names", {row["waste_type_id"]: row["waste_type"] for row in refreshed.get("pickups", [])})
            self._prune_past_pickups(refreshed, fetched_today)
            self.cache[key] = refreshed
            # Only a completed fetch commits IDs. A full address refresh cannot
            # write this map; same-address requests share this task.
            self.name_aliases[key] = dict(refreshed["address_ids"])
            self.retry_at.pop(key, None)
            self.last_errors.pop(key, None)
            await self._persist()
            return refreshed, False, None
        except InvalidAddressError as err:
            self.last_errors[key] = str(err)
            self.retry_at[key] = utc_now().timestamp() + self.retry_seconds
            await self._persist()
            raise web.HTTPUnprocessableEntity(
                text=json.dumps({"error": str(err), "field": err.field}), content_type="application/json",
            ) from err
        except (AbfallOoeError, aiohttp.ClientError, TimeoutError, ValueError, OSError) as err:
            LOGGER.warning("Calendar fetch failed address=%s category=%s", key[:12], failure_category(err))
            self.last_errors[key] = "upstream_unavailable"
            self.retry_at[key] = utc_now().timestamp() + self.retry_seconds
            await self._persist()
            if entry and entry.get("pickups"):
                return entry, True, "upstream_unavailable"
            raise temporary_error(seconds=self.retry_seconds) from err

    def response_data(
        self,
        entry: dict[str, Any],
        labels: dict[str, str],
        stale: bool,
        refresh_error: str | None,
        details: bool = False,
        intervals: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        type_intervals: dict[str, dict[str, str]] = entry.get("type_intervals", {})
        status = "ok"
        pickups = [deserialize_pickup(item) for item in entry.get("pickups", [])]
        intervals = intervals or {}
        resolved = {}
        waste_names = entry.get("waste_type_names", {row.waste_type_id: row.waste_type for row in pickups})
        for name in intervals:
            try:
                select_id(waste_names, name, "waste_type")
            except InvalidAddressError as err:
                raise web.HTTPUnprocessableEntity(text=json.dumps({"error": str(err), "field": "waste_type"}), content_type="application/json") from err
        for type_id, choices in type_intervals.items():
            type_name = waste_names.get(type_id)
            if not type_name:
                continue
            # Both waste type and interval are names in the public API.
            supplied = [v for k, v in intervals.items() if normalize_name(k) == normalize_name(type_name)]
            if len(supplied) > 1:
                raise web.HTTPUnprocessableEntity(text=json.dumps({"error": "interval_ambiguous"}), content_type="application/json")
            wanted = None
            if supplied:
                try:
                    wanted = select_id(choices, supplied[0], "interval")
                except InvalidAddressError as err:
                    raise web.HTTPUnprocessableEntity(text=json.dumps({"error": "interval_ambiguous" if str(err) == "address_ambiguous" else "interval_not_found", "waste_type": type_name}), content_type="application/json") from err
            elif choices:
                # The upstream calendar lists its default interval first.
                # Keep that choice on the server so callers only need an address.
                wanted = next(iter(choices))
            if wanted:
                resolved[type_id] = wanted
        pickups = [row for row in pickups if row.waste_type_id not in resolved
                   or row.interval_id == resolved[row.waste_type_id]]
        pickups = filter_pickups(pickups, {}, None, None)

        today = datetime.now(LOCAL_TZ).date()
        upcoming = [item for item in pickups if item.date >= today]
        upcoming.sort(key=lambda item: (item.date, item.waste_type.casefold()))
        by_type: dict[str, list[Pickup]] = {}
        for pickup in upcoming:
            by_type.setdefault(pickup.waste_type, []).append(pickup)

        next_pickup = None
        if upcoming:
            first_date = upcoming[0].date
            next_pickup = {
                "date": first_date.isoformat(),
                "days_until": (first_date - today).days,
                "waste_types": sorted(
                    {item.waste_type for item in upcoming if item.date == first_date},
                    key=str.casefold,
                ),
            }

        by_waste_type: dict[str, dict[str, Any]] = {}
        for waste_type, items in by_type.items():
            first = items[0]
            by_waste_type[waste_type] = {
                "date": first.date.isoformat(),
                "days_until": (first.date - today).days,
            }
            if details:
                by_waste_type[waste_type]["upcoming"] = [
                    {"date": item.date.isoformat(), "days_until": (item.date - today).days}
                    for item in items[:3]
                ]

        if not upcoming:
            if stale:
                raise temporary_error(refresh_error or "upstream_unavailable")
            status = "no_upcoming_pickups" if entry.get("had_pickups") or entry.get("pickups") else "no_pickups"

        fetched = datetime.fromisoformat(entry["fetched_at"])
        age = max(0, int((utc_now() - fetched).total_seconds()))
        stale = stale or entry.get("fetched_on") != datetime.now(LOCAL_TZ).date().isoformat()
        result = {
            "ok": status == "ok",
            "status": status,
            "fetched_at": entry["fetched_at"],
            "last_successful_fetch_at": entry["fetched_at"],
            "cache_status": "stale" if stale else "current",
            "cache_age_seconds": age,
            "cache_stale": stale or entry.get("fetched_on") != datetime.now(LOCAL_TZ).date().isoformat(),
            "refresh_error": refresh_error,
            "next_pickup": next_pickup,
            "by_waste_type": by_waste_type,
        }
        if details:
            result["address"] = {
                "district": labels.get("district_name"),
                "city": labels.get("city_name"),
                "street": labels.get("street_name"),
                "house_number": labels.get("house_number_name"),
            }
            result["pickups"] = [
                {
                    "waste_type": item.waste_type,
                    "date": item.date.isoformat(),
                    "days_until": (item.date - today).days,
                }
                for item in upcoming
            ]
        return result
