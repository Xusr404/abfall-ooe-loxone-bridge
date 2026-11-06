"""Retention, crash-safe activity baselines and daily lifecycle scheduling."""

import asyncio
import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.support import ADDRESS, NAMES, BridgeTestCase, Clock, calendar, pickup
from core.common import normalized_name_key
from core.activity import INACTIVITY_SECONDS, CLEANUP_RETRY_SECONDS


class ActivityTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.key = normalized_name_key(NAMES)
        self.addAsyncCleanup(self.bridge.close)

    def seed_cache(self, entries=None, **metadata):
        entries = entries if entries is not None else {self.key: calendar(pickup())}
        self.bridge.cache_file.write_text(json.dumps({"version": 4, "entries": entries, **metadata}), encoding="utf-8")

    def seed_activity(self, last_check=None, requested=None):
        stamp = Clock.instant if last_check is None else last_check
        requested = {self.key: Clock.instant} if requested is None else requested
        self.bridge.activity_file.write_text(json.dumps({
            "version": 1, "last_cleanup_at": stamp.isoformat(),
            "last_requested": {key: value.isoformat() for key, value in requested.items()},
        }), encoding="utf-8")

    def restarted_bridge(self):
        bridge = type(self.bridge)(self.bridge.cache_file, self.bridge.api_key, self.bridge.log_file)
        self.addAsyncCleanup(bridge.close)
        return bridge

    async def test_cached_requests_record_activity_without_any_disk_write(self):
        self.seed_cache()
        self.seed_activity(last_check=Clock.instant - timedelta(hours=23), requested={self.key: Clock.instant - timedelta(days=100)})
        await self.bridge.start()
        before_cache = self.bridge.cache_file.read_bytes()
        before_activity = self.bridge.activity_file.read_bytes()
        with patch.object(self.bridge, "_fetch_calendar", AsyncMock()) as fetch, \
             patch("core.storage.os.replace", wraps=os.replace) as replace:
            for minutes in (0, 10, 20):
                Clock.instant += timedelta(minutes=minutes)
                entry, stale, _ = await self.bridge.get_calendar({}, NAMES)
                self.assertFalse(stale)
                self.assertTrue(entry["pickups"])
            self.assertFalse(await self.bridge.check_calendar_cleanup())
            replace.assert_not_called()
            fetch.assert_not_awaited()
        self.assertEqual(self.bridge.last_requested[self.key], Clock.instant.timestamp())
        self.assertTrue(self.bridge.activity_dirty)
        self.assertEqual(self.bridge.cache_file.read_bytes(), before_cache)
        self.assertEqual(self.bridge.activity_file.read_bytes(), before_activity)

    async def test_lookup_requests_record_same_stable_identity_and_keep_refresh_policy(self):
        with patch.object(self.bridge, "_fetch_calendar", AsyncMock(return_value=calendar(pickup()))) as fetch:
            await self.bridge.get_calendar({}, NAMES)
            Clock.instant += timedelta(hours=2)
            await self.bridge.get_calendar({}, {**NAMES, "street_name": " " + NAMES["street_name"] + " "})
        fetch.assert_awaited_once()  # Still the normal successful daily cache.
        self.assertEqual(set(self.bridge.last_requested), {self.key})
        self.assertEqual(self.bridge.last_requested[self.key], Clock.instant.timestamp())
        self.assertFalse(self.bridge.activity_file.exists())  # No per-request activity I/O.

    async def test_threshold_includes_full_grace_day_and_removes_address_metadata(self):
        now = Clock.instant
        keep = ("recent", "90-days", "grace", "boundary-minus-second")
        delete = ("boundary", "older")
        entries = {key: calendar(pickup()) for key in (*keep, *delete)}
        metadata = {field: {key: (dict(ADDRESS) if field == "name_aliases" else 1 if field == "retry_at" else "2026-11-04" if field == "attempted_on" else "upstream_unavailable")
                             for key in entries}
                    for field in ("attempted_on", "last_errors", "name_aliases", "retry_at")}
        self.seed_cache(entries, **metadata)
        self.seed_activity(last_check=now - timedelta(days=2), requested={
            "recent": now - timedelta(days=1), "90-days": now - timedelta(days=90),
            "grace": now - timedelta(days=90, hours=23),
            "boundary-minus-second": now - timedelta(seconds=INACTIVITY_SECONDS - 1),
            "boundary": now - timedelta(seconds=INACTIVITY_SECONDS), "older": now - timedelta(days=120),
        })
        await self.bridge.start()
        self.assertEqual(set(self.bridge.cache), set(keep))
        saved = json.loads(self.bridge.cache_file.read_text(encoding="utf-8"))
        for field in ("entries", *metadata):
            self.assertEqual(set(saved[field]), set(keep))
        activity = json.loads(self.bridge.activity_file.read_text(encoding="utf-8"))
        self.assertEqual(set(activity["last_requested"]), set(keep))
        self.assertEqual(datetime.fromisoformat(activity["last_cleanup_at"]), now)

    async def test_repeated_restarts_preserve_due_time_and_do_not_write_before_24_hours(self):
        checked = Clock.instant - timedelta(hours=23, minutes=59, seconds=59, microseconds=500000)
        self.seed_cache()
        self.seed_activity(last_check=checked, requested={self.key: Clock.instant - timedelta(days=100)})
        original_cache = self.bridge.cache_file.read_bytes()
        original_activity = self.bridge.activity_file.read_bytes()
        with patch("core.storage.os.replace", wraps=os.replace) as replace:
            for _ in range(3):
                bridge = self.restarted_bridge()
                await bridge.start()
                await bridge.close()
                self.assertEqual(bridge.last_cleanup_at, checked.timestamp())
                self.assertIn(self.key, bridge.cache)
            replace.assert_not_called()
        self.assertEqual(self.bridge.cache_file.read_bytes(), original_cache)
        self.assertEqual(self.bridge.activity_file.read_bytes(), original_activity)
        Clock.instant = checked + timedelta(days=1)
        bridge = self.restarted_bridge()
        await bridge.start()
        self.assertNotIn(self.key, bridge.cache)

    async def test_due_cleanup_runs_on_startup_after_several_days_stopped(self):
        self.seed_cache()
        self.seed_activity(last_check=Clock.instant - timedelta(days=5), requested={self.key: Clock.instant - timedelta(days=96)})
        await self.bridge.start()
        self.assertNotIn(self.key, self.bridge.cache)
        self.assertEqual(self.bridge.last_cleanup_at, Clock.instant.timestamp())

    async def test_scheduler_runs_at_original_due_time_and_starts_only_one_task(self):
        self.seed_cache()
        self.seed_activity(last_check=Clock.instant - timedelta(hours=23), requested={self.key: Clock.instant - timedelta(days=100)})
        waits, advance = asyncio.Queue(), asyncio.Event()
        async def controlled_wait(seconds):
            waits.put_nowait(seconds)
            await advance.wait()
            advance.clear()
        with patch.object(self.bridge, "_maintenance_wait", side_effect=controlled_wait):
            await asyncio.gather(self.bridge.start(), self.bridge.start())
            task = self.bridge._maintenance_task
            self.assertAlmostEqual(await asyncio.wait_for(waits.get(), 1), 3600)
            await self.bridge.start()
            self.assertIs(self.bridge._maintenance_task, task)
            Clock.instant += timedelta(hours=1)
            advance.set()
            await asyncio.wait_for(waits.get(), 1)
            self.assertNotIn(self.key, self.bridge.cache)
            self.assertEqual(self.bridge.last_cleanup_at, Clock.instant.timestamp())
            await self.bridge.close()
            self.assertTrue(task.done())
            self.assertIsNone(self.bridge._maintenance_task)

    async def test_failed_cache_cleanup_is_not_recorded_and_retries_after_delay(self):
        self.seed_cache()
        checked = Clock.instant - timedelta(days=2)
        self.seed_activity(last_check=checked, requested={self.key: Clock.instant - timedelta(days=100)})
        await self.bridge.load(None)
        await self.bridge.load_activity()
        before_cache = self.bridge.cache_file.read_bytes()
        before_activity = self.bridge.activity_file.read_bytes()
        with patch("core.storage.os.replace", side_effect=OSError("synthetic disk failure")) as replace, \
             self.assertLogs("loxone_bridge", level="WARNING"):
            self.assertFalse(await self.bridge.check_calendar_cleanup())
            self.assertFalse(await self.bridge.check_calendar_cleanup())
            replace.assert_called_once()
        self.assertEqual(self.bridge.last_cleanup_at, checked.timestamp())
        self.assertIn(self.key, self.bridge.cache)
        self.assertEqual(self.bridge.cache_file.read_bytes(), before_cache)
        self.assertEqual(self.bridge.activity_file.read_bytes(), before_activity)
        Clock.instant += timedelta(seconds=CLEANUP_RETRY_SECONDS - 1)
        self.assertFalse(await self.bridge.check_calendar_cleanup())
        Clock.instant += timedelta(seconds=1)
        self.assertTrue(await self.bridge.check_calendar_cleanup())
        self.assertNotIn(self.key, self.bridge.cache)

    async def test_failed_activity_publication_does_not_advance_success_marker(self):
        self.seed_cache()
        checked = Clock.instant - timedelta(days=2)
        self.seed_activity(last_check=checked)
        await self.bridge.load(None)
        await self.bridge.load_activity()
        before = self.bridge.activity_file.read_bytes()
        with patch("core.activity.os.replace", side_effect=OSError("synthetic activity failure")), \
             self.assertLogs("loxone_bridge", level="WARNING"):
            self.assertFalse(await self.bridge.check_calendar_cleanup())
        self.assertEqual(self.bridge.last_cleanup_at, checked.timestamp())
        self.assertEqual(self.bridge.activity_file.read_bytes(), before)
        Clock.instant += timedelta(seconds=CLEANUP_RETRY_SECONDS)
        self.assertTrue(await self.bridge.check_calendar_cleanup())

    async def test_running_scheduler_retries_failed_check_after_delay(self):
        self.seed_cache()
        self.seed_activity(last_check=Clock.instant - timedelta(days=2), requested={self.key: Clock.instant - timedelta(days=100)})
        waits, advance = asyncio.Queue(), asyncio.Event()
        async def controlled_wait(seconds):
            waits.put_nowait(seconds)
            await advance.wait()
            advance.clear()
        with patch.object(self.bridge, "_maintenance_wait", side_effect=controlled_wait):
            with patch.object(self.bridge, "_persist_locked", AsyncMock(side_effect=OSError("synthetic failure"))) as persist, \
                 self.assertLogs("loxone_bridge", level="WARNING"):
                await self.bridge.start()
                self.assertEqual(await asyncio.wait_for(waits.get(), 1), CLEANUP_RETRY_SECONDS)
                persist.assert_awaited_once()
                self.assertIn(self.key, self.bridge.cache)
            Clock.instant += timedelta(seconds=CLEANUP_RETRY_SECONDS)
            advance.set()
            await asyncio.wait_for(waits.get(), 1)
            self.assertNotIn(self.key, self.bridge.cache)
            self.assertEqual(self.bridge.last_cleanup_at, Clock.instant.timestamp())

    async def test_main_and_standalone_apps_own_maintenance_lifecycle(self):
        from aiohttp.test_utils import TestClient, TestServer
        from loxone_bridge import create_app
        import address_helper_server
        for standalone in (False, True):
            bridge = self.restarted_bridge()
            if standalone:
                with patch("core.service.PickupBridge", return_value=bridge):
                    app = address_helper_server.create_app()
            else:
                app = create_app(bridge)
            async with TestClient(TestServer(app)):
                task = bridge._maintenance_task
                self.assertIsNotNone(task)
                self.assertFalse(task.done())
            self.assertTrue(task.done())
            self.assertIsNone(bridge._maintenance_task)

    async def test_missing_corrupt_or_unreadable_activity_uses_safe_baselines(self):
        for damage in (None, "{", '[]', '{"version":1,"last_requested":null}'):
            self.seed_cache()
            self.bridge.activity_file.unlink(missing_ok=True)
            if damage is not None:
                self.bridge.activity_file.write_text(damage, encoding="utf-8")
            bridge = self.restarted_bridge()
            with self.assertLogs("loxone_bridge", level="WARNING"):
                await bridge.start()
            self.assertIn(self.key, bridge.cache)
            self.assertEqual(bridge.last_requested[self.key], Clock.instant.timestamp())
            await bridge.close()
        bridge = self.restarted_bridge()
        await bridge.load(None)
        with patch.object(Path, "read_text", side_effect=PermissionError("synthetic unreadable activity")), \
             self.assertLogs("loxone_bridge", level="WARNING"):
            await bridge.load_activity()
        self.assertEqual(bridge.last_requested[self.key], Clock.instant.timestamp())

    async def test_invalid_or_missing_check_timestamps_are_repaired_without_deletion(self):
        for marker in (None, "bad", "2026-11-04T10:00:00", (Clock.instant + timedelta(days=1)).isoformat()):
            self.seed_cache()
            self.seed_activity(requested={self.key: Clock.instant - timedelta(days=120)})
            data = json.loads(self.bridge.activity_file.read_text())
            if marker is None:
                data.pop("last_cleanup_at")
            else:
                data["last_cleanup_at"] = marker
            self.bridge.activity_file.write_text(json.dumps(data), encoding="utf-8")
            bridge = self.restarted_bridge()
            with self.assertLogs("loxone_bridge", level="WARNING"):
                await bridge.start()
            self.assertIn(self.key, bridge.cache)
            self.assertEqual(bridge.last_cleanup_at, Clock.instant.timestamp())
            await bridge.close()

    async def test_bad_or_missing_per_address_timestamp_preserves_only_affected_addresses(self):
        entries = {key: calendar(pickup()) for key in ("old", "bad", "missing", "future", "naive")}
        self.seed_cache(entries)
        self.seed_activity(last_check=Clock.instant - timedelta(days=2), requested={"old": Clock.instant - timedelta(days=100)})
        data = json.loads(self.bridge.activity_file.read_text())
        data["last_requested"].update(bad="invalid", future=(Clock.instant + timedelta(hours=1)).isoformat(), naive="2020-01-01T00:00:00")
        self.bridge.activity_file.write_text(json.dumps(data), encoding="utf-8")
        with self.assertLogs("loxone_bridge", level="WARNING"):
            await self.bridge.start()
        self.assertEqual(set(self.bridge.cache), {"bad", "missing", "future", "naive"})
        self.assertTrue(all(value == Clock.instant.timestamp() for value in self.bridge.last_requested.values()))

    async def test_inflight_lookup_is_skipped_even_with_old_activity(self):
        self.bridge.cache[self.key] = calendar(pickup(), fetched_on="2026-11-03")
        self.bridge.last_requested[self.key] = Clock.instant.timestamp() - INACTIVITY_SECONDS - 1
        started, finish = asyncio.Event(), asyncio.Event()
        async def update():
            started.set()
            await finish.wait()
        task = asyncio.create_task(self.bridge.lookups.run("calendar:" + self.key, update))
        await started.wait()
        try:
            self.assertTrue(await self.bridge.check_calendar_cleanup())
            self.assertIn(self.key, self.bridge.cache)
        finally:
            finish.set()
            await task

    async def test_request_arriving_during_cleanup_write_rescues_address(self):
        self.seed_cache()
        await self.bridge.load(None)
        self.bridge.last_requested[self.key] = Clock.instant.timestamp() - INACTIVITY_SECONDS - 1
        before = self.bridge.cache_file.read_bytes()
        started, finish = threading.Event(), threading.Event()
        original = Path.write_text
        def slow_prepare(path, content, **kwargs):
            if path == self.bridge.cache_file.with_suffix(".json.tmp"):
                started.set()
                if not finish.wait(3):
                    raise OSError("test writer not released")
            return original(path, content, **kwargs)
        with patch.object(Path, "write_text", slow_prepare):
            cleanup = asyncio.create_task(self.bridge.check_calendar_cleanup())
            await asyncio.wait_for(asyncio.to_thread(started.wait), 1)
            with patch.object(self.bridge, "_fetch_calendar", AsyncMock()) as fetch:
                await self.bridge.get_calendar({}, NAMES)
                fetch.assert_not_awaited()
            finish.set()
            self.assertTrue(await cleanup)
        self.assertIn(self.key, self.bridge.cache)
        self.assertEqual(self.bridge.cache_file.read_bytes(), before)  # Prepared deletion was never published.
        data = json.loads(self.bridge.activity_file.read_text())
        self.assertEqual(datetime.fromisoformat(data["last_requested"][self.key]), Clock.instant)

    async def test_new_calendar_update_during_cleanup_is_not_lost(self):
        self.seed_cache({"inactive": calendar(pickup())})
        await self.bridge.load(None)
        self.bridge.last_requested["inactive"] = Clock.instant.timestamp() - INACTIVITY_SECONDS - 1
        started, finish = threading.Event(), threading.Event()
        original = Path.write_text
        first = True
        def slow_prepare(path, content, **kwargs):
            nonlocal first
            if first:
                first = False
                started.set()
                if not finish.wait(3):
                    raise OSError("test writer not released")
            return original(path, content, **kwargs)
        with patch.object(Path, "write_text", slow_prepare):
            cleanup = asyncio.create_task(self.bridge.check_calendar_cleanup())
            await asyncio.wait_for(asyncio.to_thread(started.wait), 1)
            fetched = asyncio.Event()
            async def fetch(*_):
                fetched.set()
                return calendar(pickup("2026-11-12"))
            with patch.object(self.bridge, "_fetch_calendar", side_effect=fetch):
                lookup = asyncio.create_task(self.bridge.get_calendar({}, NAMES))
                await fetched.wait()
                finish.set()
                await asyncio.gather(cleanup, lookup)
        saved = json.loads(self.bridge.cache_file.read_text())
        self.assertNotIn("inactive", saved["entries"])
        self.assertEqual(saved["entries"][self.key]["pickups"][0]["date"], "2026-11-12")

    async def test_activity_during_its_file_write_is_retained_for_next_daily_write(self):
        self.bridge.cache[self.key] = calendar(pickup())
        self.bridge.record_activity(self.key)
        original_stamp = Clock.instant
        started, finish = threading.Event(), threading.Event()
        original = Path.write_text
        def slow_prepare(path, content, **kwargs):
            started.set()
            if not finish.wait(3):
                raise OSError("test writer not released")
            return original(path, content, **kwargs)
        with patch.object(Path, "write_text", slow_prepare):
            check = asyncio.create_task(self.bridge.check_calendar_cleanup())
            await asyncio.wait_for(asyncio.to_thread(started.wait), 1)
            Clock.instant += timedelta(hours=1)
            await self.bridge.get_calendar({}, NAMES)
            finish.set()
            await check
        self.assertTrue(self.bridge.activity_dirty)
        self.assertEqual(self.bridge.last_requested[self.key], Clock.instant.timestamp())
        self.assertEqual(datetime.fromisoformat(json.loads(self.bridge.activity_file.read_text())["last_requested"][self.key]), original_stamp)
        Clock.instant = original_stamp + timedelta(days=1)
        await self.bridge.check_calendar_cleanup()
        self.assertFalse(self.bridge.activity_dirty)
        self.assertEqual(datetime.fromisoformat(json.loads(self.bridge.activity_file.read_text())["last_requested"][self.key]), original_stamp + timedelta(hours=1))

    async def test_expired_dates_prune_in_memory_on_startup_and_cached_reads_without_writing(self):
        self.seed_cache({self.key: calendar(pickup("2026-11-03"), pickup("2026-11-05"))})
        self.seed_activity()
        original_cache = self.bridge.cache_file.read_bytes()
        with patch("core.storage.os.replace", wraps=os.replace) as replace:
            await self.bridge.start()
            for _ in range(3):
                entry, stale, error = await self.bridge.get_calendar({}, NAMES)
                data = self.bridge.response_data(entry, NAMES, stale, error, details=True)
                self.assertEqual([row["date"] for row in data["pickups"]], ["2026-11-05"])
            replace.assert_not_called()
        self.assertEqual(self.bridge.cache_file.read_bytes(), original_cache)
        # An already required write also prunes dates for unrelated addresses.
        self.bridge.cache["unrelated"] = calendar(pickup("2026-11-03"), pickup("2026-11-12"))
        await self.bridge._persist()
        saved = json.loads(self.bridge.cache_file.read_text())
        self.assertEqual([row["date"] for row in saved["entries"][self.key]["pickups"]], ["2026-11-05"])
        self.assertEqual([row["date"] for row in saved["entries"]["unrelated"]["pickups"]], ["2026-11-12"])

    async def test_idle_due_check_only_writes_activity_marker_and_followups_write_nothing(self):
        self.seed_cache()
        self.seed_activity(last_check=Clock.instant - timedelta(days=1))
        await self.bridge.load(None)
        await self.bridge.load_activity()
        with patch("core.storage.os.replace", wraps=os.replace) as replace:
            self.assertTrue(await self.bridge.check_calendar_cleanup())
            self.assertFalse(await self.bridge.check_calendar_cleanup())
            replace.assert_called_once()
            self.assertEqual(replace.call_args.args[1], self.bridge.activity_file)
            Clock.instant += timedelta(hours=23)
            self.assertFalse(await self.bridge.check_calendar_cleanup())
            replace.assert_called_once()

    async def test_shared_address_dataset_is_independent(self):
        shared = self.folder / "address-data.json"
        content = b'{"shared":"districts, municipalities and streets"}'
        shared.write_bytes(content)
        self.bridge.cache[self.key] = calendar(pickup())
        self.bridge.last_requested[self.key] = Clock.instant.timestamp() - INACTIVITY_SECONDS - 1
        self.assertTrue(await self.bridge.check_calendar_cleanup())
        self.assertEqual(shared.read_bytes(), content)

    async def test_cleanup_waits_for_existing_cache_writer_without_losing_active_update(self):
        self.bridge.cache[self.key] = calendar(pickup())
        self.bridge.last_requested[self.key] = Clock.instant.timestamp() - INACTIVITY_SECONDS - 1
        async with self.bridge.persist_lock:
            check = asyncio.create_task(self.bridge.check_calendar_cleanup())
            await asyncio.sleep(0)
            self.bridge.record_activity(self.key)
            self.bridge.cache[self.key] = calendar(pickup("2026-11-12"))
        self.assertTrue(await check)
        self.assertIn(self.key, self.bridge.cache)
        self.assertEqual(self.bridge.cache[self.key]["pickups"][0]["date"], "2026-11-12")
