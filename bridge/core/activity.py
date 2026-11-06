"""Daily, single-process calendar retention; independent of address snapshots."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

from .common import LOGGER, utc_now

DAY_SECONDS = 24 * 60 * 60
INACTIVITY_SECONDS = 91 * DAY_SECONDS  # 90 days, plus one unpersisted-activity grace day.
CLEANUP_RETRY_SECONDS = 5 * 60


class BridgeActivityMixin:
    def _init_activity(self):
        self.activity_file = self.cache_file.with_name(self.cache_file.stem + ".activity.json")
        self.last_requested: dict[str, float] = {}
        self.last_cleanup_at: float | None = None
        self.activity_dirty = False
        self._activity_revision = 0
        self._cleanup_retry_at = 0.0
        self._cleanup_lock = asyncio.Lock()
        self._maintenance_lifecycle_lock = asyncio.Lock()
        self._maintenance_task = None

    def record_activity(self, key):
        stamp = utc_now().timestamp()
        if stamp > self.last_requested.get(key, float("-inf")):
            self.last_requested[key] = stamp
            self._activity_revision += 1
            self.activity_dirty = True

    @staticmethod
    def _activity_timestamp(value, now):
        if not isinstance(value, str):
            raise ValueError("missing or invalid activity timestamp")
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("activity timestamp has no timezone")
        seconds = stamp.timestamp()
        if seconds > now or seconds < 0:
            raise ValueError("activity timestamp outside valid range")
        return seconds

    async def load_activity(self):
        now = utc_now().timestamp()
        self.last_requested = {}
        self.last_cleanup_at = None
        self.activity_dirty = False
        try:
            parsed = json.loads(await asyncio.to_thread(self.activity_file.read_text, encoding="utf-8"))
            if not isinstance(parsed, dict) or parsed.get("version") != 1 or not isinstance(parsed.get("last_requested"), dict):
                raise ValueError("invalid activity envelope")
            for key, value in parsed["last_requested"].items():
                try:
                    self.last_requested[key] = self._activity_timestamp(value, now)
                except (ValueError, TypeError, OverflowError):
                    LOGGER.warning("Invalid address activity timestamp; establishing a safe baseline")
                    self.record_activity(key)
            try:
                self.last_cleanup_at = self._activity_timestamp(parsed.get("last_cleanup_at"), now)
            except (ValueError, TypeError, OverflowError):
                # A damaged check marker cannot prove anything about retention.
                # Reset saved addresses conservatively and repair it once.
                LOGGER.warning("Invalid cleanup timestamp; establishing a safe activity baseline")
                for key in self.cache:
                    self.record_activity(key)
        except (OSError, ValueError, TypeError) as err:
            LOGGER.warning("Cannot load calendar activity (%s); establishing a safe baseline", type(err).__name__)
        missing = set(self.cache) - self.last_requested.keys()
        if missing:
            LOGGER.warning("Missing activity for %d saved addresses; establishing a safe baseline", len(missing))
            for key in missing:
                self.record_activity(key)
        self._cleanup_retry_at = 0.0

    def _inactive_keys(self):
        now = utc_now().timestamp()
        cutoff = now - INACTIVITY_SECONDS
        result = set()
        for key in self.cache:
            if key not in self.last_requested:
                # Also covers an entry introduced by migration or tooling.
                self.record_activity(key)
            elif self.last_requested[key] <= cutoff and "calendar:" + key not in self.lookups.inflight:
                result.add(key)
        return result

    def _forget_inactive(self, removed):
        # Called synchronously with successful calendar publication. No await
        # between final eligibility validation and these memory changes.
        for key in removed:
            self.cache.pop(key, None)
            self.attempted_on.pop(key, None)
            self.last_errors.pop(key, None)
            self.name_aliases.pop(key, None)
            self.retry_at.pop(key, None)
            self.last_requested.pop(key, None)
        if removed:
            self._activity_revision += 1
            self.activity_dirty = True

    async def _persist_activity_locked(self, completed_at):
        revision = self._activity_revision
        payload = json.dumps({
            "version": 1,
            "last_cleanup_at": datetime.fromtimestamp(completed_at, timezone.utc).isoformat(),
            "last_requested": {
                key: datetime.fromtimestamp(stamp, timezone.utc).isoformat()
                for key, stamp in self.last_requested.items()
            },
        }, separators=(",", ":"), ensure_ascii=False)
        temporary = self.activity_file.with_suffix(self.activity_file.suffix + ".tmp")

        def prepare():
            self.activity_file.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(payload, encoding="utf-8")

        writer = asyncio.create_task(asyncio.to_thread(prepare))
        try:
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError:
                await writer
                raise
            # The only replacement is a short, synchronous rename, without
            # yielding between commit and updating the in-memory marker.
            os.replace(temporary, self.activity_file)
            self.last_cleanup_at = completed_at
            self.activity_dirty = revision != self._activity_revision
        finally:
            temporary.unlink(missing_ok=True)

    async def check_calendar_cleanup(self):
        """Return true only for a successfully completed, durably recorded check."""
        async with self._cleanup_lock:
            now = utc_now().timestamp()
            due = 0 if self.last_cleanup_at is None else self.last_cleanup_at + DAY_SECONDS
            if now < max(due, self._cleanup_retry_at):
                return False
            try:
                async with self.persist_lock:
                    # Cache publication revalidates this set after preparing
                    # the file, including activity and lookups that arrived meanwhile.
                    await self._persist_locked(cleanup=True)
                    await self._persist_activity_locked(utc_now().timestamp())
                self._cleanup_retry_at = 0.0
                return True
            except (OSError, ValueError) as err:
                LOGGER.warning("Calendar cleanup failed (%s); retrying in %d seconds", type(err).__name__, CLEANUP_RETRY_SECONDS)
                self._cleanup_retry_at = utc_now().timestamp() + CLEANUP_RETRY_SECONDS
                return False

    async def _maintenance_wait(self, seconds):
        await asyncio.sleep(seconds)

    async def _maintain_calendars(self):
        while True:
            await self.check_calendar_cleanup()
            due = 0 if self.last_cleanup_at is None else self.last_cleanup_at + DAY_SECONDS
            delay = max(due, self._cleanup_retry_at) - utc_now().timestamp()
            # Re-evaluate UTC after clock corrections instead of sleeping for
            # an arbitrarily long wall-clock interval.
            await self._maintenance_wait(max(0.01, min(delay, 3600)))

    async def start(self, app=None):
        async with self._maintenance_lifecycle_lock:
            if self._maintenance_task is not None and not self._maintenance_task.done():
                return
            await self.load(app)
            await self.load_activity()
            await self.check_calendar_cleanup()
            self._maintenance_task = asyncio.create_task(self._maintain_calendars())

    async def stop_calendar_maintenance(self):
        async with self._maintenance_lifecycle_lock:
            task = self._maintenance_task
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._maintenance_task = None
