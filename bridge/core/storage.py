"""Persistent cache handling for the Loxone bridge."""

from __future__ import annotations

import asyncio
import json
import os
import math
from datetime import date, datetime
from typing import Any

from aiohttp import web

from .common import CachePersistenceError, LOCAL_TZ, LOGGER, LOCATION_FIELDS, ID_RE, deserialize_pickup

class BridgeStorageMixin:
    @staticmethod
    def _prune_past_pickups(entry: dict[str, Any], today: date) -> bool:
        original = entry.get("pickups", [])
        if not isinstance(original, list):
            original = []
        entry.setdefault("had_pickups", bool(original))
        current = []
        for pickup in original:
            try:
                pickup_date = date.fromisoformat(str(pickup.get("date", "")))
            except (AttributeError, TypeError, ValueError):
                # Drop malformed rows so one bad cached record cannot break the endpoint.
                continue
            if pickup_date >= today:
                current.append(pickup)

        # Keep three future dates per waste type and interval. Interval is part
        # of the group because Loxone may select it later, when reading cache.
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for pickup in current:
            group_key = (
                str(pickup.get("waste_type_id", pickup.get("waste_type", ""))),
                str(pickup.get("interval_id") or ""),
            )
            groups.setdefault(group_key, []).append(pickup)
        limited: list[dict[str, Any]] = []
        for rows in groups.values():
            dates_seen: set[str] = set()
            for pickup in sorted(rows, key=lambda row: str(row.get("date", ""))):
                pickup_date = str(pickup.get("date", ""))
                if pickup_date in dates_seen:
                    continue
                dates_seen.add(pickup_date)
                limited.append(pickup)
                if len(dates_seen) == 3:
                    break
        limited.sort(
            key=lambda row: (
                str(row.get("date", "")),
                str(row.get("waste_type", "")).casefold(),
                str(row.get("interval_id", "")),
            )
        )
        if limited == original:
            return False
        entry["pickups"] = limited
        return True

    @staticmethod
    def _valid_ids(value):
        return isinstance(value, dict) and all(
            isinstance(value.get(field), str) and ID_RE.fullmatch(value[field])
            for field in LOCATION_FIELDS)

    @classmethod
    def _clean_entry(cls, entry):
        if not isinstance(entry, dict):
            raise ValueError("invalid entry")
        fetched = datetime.fromisoformat(entry["fetched_at"])
        if fetched.tzinfo is None or not cls._valid_ids(entry.get("address_ids")):
            raise ValueError("invalid timestamp or address")
        if "fetched_on" in entry:
            date.fromisoformat(entry["fetched_on"])
        choices = entry.get("type_intervals", {})
        if not isinstance(choices, dict) or any(
            not isinstance(k, str) or not ID_RE.fullmatch(k) or not isinstance(v, dict)
            or any(not isinstance(i, str) or not ID_RE.fullmatch(i) or not isinstance(t, str)
                   for i, t in v.items()) for k, v in choices.items()):
            raise ValueError("invalid intervals")
        if not isinstance(entry.get("pickups"), list):
            raise ValueError("invalid pickups")
        if "waste_type_names" in entry and (
            not isinstance(entry["waste_type_names"], dict) or any(
                not isinstance(k, str) or not isinstance(v, str)
                for k, v in entry["waste_type_names"].items())
        ):
            raise ValueError("invalid waste type names")
        rows = []
        for row in entry["pickups"]:
            try:
                if not isinstance(row, dict) or not all(isinstance(row.get(k), str)
                        for k in ("waste_type_id", "waste_type", "date")):
                    continue
                if not ID_RE.fullmatch(row["waste_type_id"]):
                    continue
                interval = row.get("interval_id")
                if interval is not None and (not isinstance(interval, str) or not ID_RE.fullmatch(interval)):
                    continue
                if not isinstance(row.get("interval", ""), str):
                    continue
                deserialize_pickup(row)
                rows.append(row)
            except (KeyError, ValueError, TypeError):
                continue
        return {**entry, "pickups": rows, "type_intervals": choices,
                "had_pickups": bool(entry.get("had_pickups") or entry["pickups"])}

    async def load(self, _app: web.Application) -> None:
        try:
            content = await asyncio.to_thread(self.cache_file.read_text, encoding="utf-8")
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("invalid cache envelope")
            if "entries" in parsed or "version" in parsed:
                if not isinstance(parsed.get("entries"), dict):
                    raise ValueError("invalid cache entries")
                entries = parsed["entries"]
            else:
                entries = parsed  # Supported legacy flat cache.
            self.cache = {}
            for key, value in entries.items():
                try:
                    self.cache[key] = self._clean_entry(value)
                except (ValueError, KeyError, TypeError, AttributeError):
                    LOGGER.warning("Discarding malformed cached calendar")
            def mapping(name):
                value = parsed.get(name, {})
                return value if isinstance(value, dict) else {}
            self.attempted_on = {}
            for key, value in mapping("attempted_on").items():
                try:
                    date.fromisoformat(value)
                    self.attempted_on[key] = value
                except (TypeError, ValueError):
                    pass
            self.last_errors = {k: v for k, v in mapping("last_errors").items() if isinstance(v, str)}
            self.name_aliases = {k: v for k, v in mapping("name_aliases").items() if self._valid_ids(v)}
            self.retry_at = {k: v for k, v in mapping("retry_at").items()
                             if isinstance(v, (int, float)) and math.isfinite(v)}
            LOGGER.info("Loaded %d cached address calendars", len(self.cache))
        except FileNotFoundError:
            LOGGER.info("No cache file yet; it will be created after the first fetch")
        except (OSError, ValueError) as err:
            LOGGER.warning("Ignoring unreadable cache file: %s", err)

        # Startup sanitizes memory only. Expiry alone must never cause a
        # calendar write; the next required write will prune every saved entry.
        today = datetime.now(LOCAL_TZ).date()
        interrupted_aliases = [key for key, error in self.last_errors.items()
                               if error == "address_resolution_in_progress"]
        for key in interrupted_aliases:
            self.last_errors.pop(key, None)
            self.attempted_on.pop(key, None)
        for entry in self.cache.values():
            if not entry.get("fetched_on") and entry.get("fetched_at"):
                entry["fetched_on"] = datetime.fromisoformat(entry["fetched_at"]).astimezone(LOCAL_TZ).date().isoformat()
            self._prune_past_pickups(entry, today)

    def _cache_payload(self, removed):
        today = datetime.now(LOCAL_TZ).date()
        for entry in self.cache.values():
            self._prune_past_pickups(entry, today)
        def retained(mapping):
            return {key: value for key, value in mapping.items() if key not in removed}
        return json.dumps({
            "version": 4,
            "entries": retained(self.cache),
            "attempted_on": retained(self.attempted_on),
            "last_errors": retained(self.last_errors),
            "name_aliases": retained(self.name_aliases),
            "retry_at": retained(self.retry_at),
        }, ensure_ascii=False, separators=(",", ":"))

    async def _persist_locked(self, cleanup=False):
        """Prepare off-loop, then validate and commit without yielding.

        Requests may record activity and calendar fetches may finish while the
        temporary file is prepared. Compare the entire prospective snapshot
        again before atomic replacement, rather than publishing an older copy
        over newer data or deleting an address requested during the write.
        """
        temporary = self.cache_file.with_suffix(self.cache_file.suffix + ".tmp")
        try:
            while True:
                removed = self._inactive_keys() if cleanup else set()
                if cleanup and not removed:
                    return set()
                snapshot = self._cache_payload(removed)
                def prepare():
                    self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                    temporary.write_text(snapshot, encoding="utf-8")
                publication = asyncio.create_task(asyncio.to_thread(prepare))
                try:
                    await asyncio.shield(publication)
                except asyncio.CancelledError:
                    await publication
                    raise
                current_removed = self._inactive_keys() if cleanup else set()
                if cleanup and not current_removed:
                    return set()
                if snapshot != self._cache_payload(current_removed):
                    continue
                # Only the atomic rename is synchronous. A request cannot run
                # between final validation, publication and memory deletion.
                os.replace(temporary, self.cache_file)
                if cleanup:
                    self._forget_inactive(current_removed)
                return current_removed
        finally:
            temporary.unlink(missing_ok=True)

    async def _persist(self) -> None:
        async with self.persist_lock:
            try:
                await self._persist_locked()
            except OSError as err:
                LOGGER.error("Cache write failed file=%s error=%s", self.cache_file.name, type(err).__name__)
                raise CachePersistenceError("cache_write_failed") from err

    async def invalidate_memory_if_cache_deleted(self) -> None:
        """Treat manual deletion of the JSON file as an explicit cache reset."""
        async with self.persist_lock:
            cache_exists = await asyncio.to_thread(self.cache_file.exists)
            if not cache_exists and (self.cache or self.attempted_on or self.last_errors):
                self.cache.clear()
                self.attempted_on.clear()
                self.last_errors.clear()
                self.name_aliases.clear()
                self.retry_at.clear()
                LOGGER.info("Cache file was removed; cleared the in-memory cache")
