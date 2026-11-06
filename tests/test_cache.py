import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from tests.support import ADDRESS, BridgeTestCase, Clock, calendar, pickup
from aiohttp import web
from core.common import InvalidAddressError, normalized_key


class CacheTests(BridgeTestCase):
    async def test_concurrent_requests_fetch_only_once_per_day(self):
        fetch = AsyncMock(return_value=calendar(pickup()))
        with patch.object(self.bridge, "_fetch_calendar", fetch):
            results = await asyncio.gather(*(self.bridge.get_calendar(ADDRESS, {}) for _ in range(5)))
        fetch.assert_awaited_once()
        self.assertTrue(all(not stale and error is None for _, stale, error in results))
        saved = json.loads(self.bridge.cache_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["entries"][normalized_key(ADDRESS)]["pickups"][0]["date"], "2026-11-05")

    async def test_refreshes_when_vienna_day_changes_before_utc_midnight(self):
        Clock.instant = datetime(2026, 11, 4, 22, 59, tzinfo=timezone.utc)
        fetch = AsyncMock(side_effect=lambda *_: calendar(pickup()))
        with patch.object(self.bridge, "_fetch_calendar", fetch):
            await self.bridge.get_calendar(ADDRESS, {})
            Clock.instant = datetime(2026, 11, 4, 23, 1, tzinfo=timezone.utc)
            entry, stale, _ = await self.bridge.get_calendar(ADDRESS, {})
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(entry["fetched_on"], "2026-11-05")
        self.assertFalse(stale)

    async def test_dst_clock_change_does_not_trigger_second_daily_refresh(self):
        fetch = AsyncMock(side_effect=lambda *_: calendar(pickup("2026-10-26")))
        Clock.instant = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
        with patch.object(self.bridge, "_fetch_calendar", fetch):
            await self.bridge.get_calendar(ADDRESS, {})
            Clock.instant = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
            entry, _, _ = await self.bridge.get_calendar(ADDRESS, {})
        fetch.assert_awaited_once()
        self.assertEqual(entry["fetched_on"], "2026-10-25")

    async def test_outage_returns_cached_dates_and_survives_restart(self):
        self.bridge.cache[normalized_key(ADDRESS)] = calendar(pickup(), fetched_on="2026-11-03")
        fetch = AsyncMock(side_effect=TimeoutError("simulated upstream outage"))
        with patch.object(self.bridge, "_fetch_calendar", fetch):
            entry, stale, error = await self.bridge.get_calendar(ADDRESS, {})
        self.assertTrue(stale)
        self.assertEqual(error, "upstream_unavailable")
        self.assertEqual(entry["pickups"][0]["date"], "2026-11-05")
        restarted = type(self.bridge)(self.bridge.cache_file, self.bridge.api_key, self.bridge.log_file)
        await restarted.load(None)
        with patch.object(restarted, "_fetch_calendar", fetch):
            _, stale, error = await restarted.get_calendar(ADDRESS, {})
        fetch.assert_awaited_once()
        self.assertTrue(stale)
        self.assertEqual(error, "upstream_unavailable")

    async def test_outage_without_cache_retries_after_short_backoff(self):
        fetch = AsyncMock(side_effect=TimeoutError())
        with patch.object(self.bridge, "_fetch_calendar", fetch):
            for _ in range(2):
                with self.assertRaises(web.HTTPServiceUnavailable) as caught:
                    await self.bridge.get_calendar(ADDRESS, {})
                self.assertEqual(json.loads(caught.exception.text)["error"], "upstream_unavailable")
            Clock.instant = datetime(2026, 11, 4, 12, 1, tzinfo=timezone.utc)
            with self.assertRaises(web.HTTPServiceUnavailable):
                await self.bridge.get_calendar(ADDRESS, {})
        self.assertEqual(fetch.await_count, 2)

    async def test_invalid_address_returns_422_without_repeated_upstream_calls(self):
        fetch = AsyncMock(side_effect=InvalidAddressError("address_not_found"))
        with patch.object(self.bridge, "_fetch_calendar", fetch):
            for _ in range(2):
                with self.assertRaises(web.HTTPUnprocessableEntity):
                    await self.bridge.get_calendar(ADDRESS, {})
        fetch.assert_awaited_once()

    async def test_manual_cache_deletion_clears_memory_and_aliases(self):
        self.bridge.cache[normalized_key(ADDRESS)] = calendar(pickup())
        self.bridge.name_aliases["synthetic-address"] = dict(ADDRESS)
        await self.bridge._persist()
        self.bridge.cache_file.unlink()
        await self.bridge.invalidate_memory_if_cache_deleted()
        self.assertEqual(self.bridge.cache, {})
        self.assertEqual(self.bridge.name_aliases, {})

    async def test_unreadable_cache_does_not_prevent_startup(self):
        self.bridge.cache_file.write_text('{"entries":', encoding="utf-8")
        await self.bridge.load(None)
        self.assertEqual(self.bridge.cache, {})

    def test_pruning_keeps_today_and_three_distinct_dates_per_interval(self):
        entry = calendar(
            pickup("2026-11-03"), pickup("2026-11-07"), pickup("2026-11-04"),
            pickup("2026-11-05"), pickup("2026-11-05"), pickup("2026-11-06"),
            pickup("2026-11-06", interval="o2"), {"date": "invalid"}, None,
        )
        self.bridge._prune_past_pickups(entry, Clock.now().date())
        self.assertEqual([(p["date"], p["interval_id"]) for p in entry["pickups"]], [
            ("2026-11-04", "o1"), ("2026-11-05", "o1"),
            ("2026-11-06", "o1"), ("2026-11-06", "o2"),
        ])

    def test_response_groups_same_day_types_and_deduplicates_intervals(self):
        entry = calendar(pickup(), pickup(interval="o2"), pickup(waste_type="Bio", type_id="t2"))
        data = self.bridge.response_data(entry, {}, False, None, details=True)
        self.assertEqual(data["next_pickup"], {"date": "2026-11-05", "days_until": 1, "waste_types": ["Bio", "Restabfall"]})
        self.assertEqual(len(data["pickups"]), 2)
        self.assertEqual(data["by_waste_type"]["Restabfall"]["upcoming"], [{"date": "2026-11-05", "days_until": 1}])

    def test_empty_calendar_distinguishes_no_dates_from_all_dates_in_past(self):
        for had_pickups, expected in ((False, "no_pickups"), (True, "no_upcoming_pickups")):
            with self.subTest(had_pickups=had_pickups):
                entry = calendar()
                entry["had_pickups"] = had_pickups
                data = self.bridge.response_data(entry, {}, False, None)
                self.assertEqual(data["status"], expected)
                self.assertIsNone(data["next_pickup"])
