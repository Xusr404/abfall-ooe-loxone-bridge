"""Offline address recovery, admission control and publication regressions."""

import asyncio
import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tests.support import ADDRESS, NAMES, BridgeTestCase, calendar, pickup
from abfall_ooe.api import AbfallOoeConnectionError, CalendarData, Location
from core.common import normalized_key, normalized_name_key, select_id, InvalidAddressError
from core.lookup import LookupPool
from core.common import deserialize_pickup
from loxone_bridge import create_app
import address_helper_server as helper
from refresh_address_data import write_snapshot, build_snapshot


NEW_IDS = dict(zip(ADDRESS, ("new-d", "new-c", "new-s", "new-h")))


def upstream(ids=NEW_IDS, fail=False, labels=NAMES):
    async def get_calendar(location):
        assert location == Location(*(ids[f] for f in ADDRESS))
        if fail:
            # The real client wraps HTTP 500 in this exception.
            raise AbfallOoeConnectionError("synthetic HTTP 500")
        return CalendarData([deserialize_pickup(pickup(interval="new-o"))],
                            {"t1": {"new-o": "2-wöchentlich", "other": "4 Wochen"}})
    return SimpleNamespace(
        get_districts=AsyncMock(return_value={ids["district_id"]: labels["district_name"]}),
        get_cities=AsyncMock(return_value={ids["city_id"]: labels["city_name"]}),
        get_streets=AsyncMock(return_value={ids["street_id"]: labels["street_name"]}),
        get_house_numbers=AsyncMock(return_value={ids["house_number_id"]: labels["house_number_name"]}),
        get_intervals=AsyncMock(return_value={"new-o": "2-wöchentlich"}),
        get_calendar=AsyncMock(side_effect=get_calendar),
    )


def snapshot(ids=ADDRESS):
    return {"schema_version": 1, "updated_at": "2026-11-04T10:00:00+00:00",
            "districts": [{"id": ids["district_id"], "name": NAMES["district_name"],
                           "cities": [{"id": ids["city_id"], "name": NAMES["city_name"],
                                       "streets": [{"id": ids["street_id"], "name": NAMES["street_name"]}]}]}]}


class RecoveryTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.bridge.recovery_backoff = 0
        self.bridge.address_refresh = Mock()
        self.addAsyncCleanup(self.bridge.close)
        self.key = normalized_name_key(NAMES)

    async def test_saved_ids_are_resolved_from_full_hierarchy_before_use(self):
        self.bridge.name_aliases[self.key] = dict(ADDRESS)
        client = upstream()
        sessions = []
        def factory(session):
            sessions.append(session)
            return client
        with patch("core.service.AbfallOoeClient", side_effect=factory):
            entry, stale, _ = await self.bridge.get_calendar(ADDRESS, NAMES)
        self.assertEqual(entry["address_ids"], NEW_IDS)
        self.assertFalse(stale)
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0].closed)
        client.get_cities.assert_awaited_once_with("new-d")
        client.get_streets.assert_awaited_once_with("new-c")
        client.get_house_numbers.assert_awaited_once_with("new-s")
        # A stale interval ID is unnecessary: the caller's interval name
        # resolves against the recovered calendar's current IDs.
        data = self.bridge.response_data(entry, NAMES, False, None,
                                         intervals={"Restabfall": "2-wöchentlich"})
        self.assertEqual(data["next_pickup"]["date"], "2026-11-05")

    async def test_500_re_resolves_in_a_fresh_session_and_retries_once(self):
        first, second = upstream(ADDRESS, fail=True), upstream()
        with patch("core.service.AbfallOoeClient", side_effect=[first, second]) as factory:
            entry, stale, error = await self.bridge.get_calendar(ADDRESS, NAMES)
        self.assertEqual(factory.call_count, 2)
        self.assertIsNot(factory.call_args_list[0].args[0], factory.call_args_list[1].args[0])
        self.assertEqual(entry["address_ids"], NEW_IDS)
        self.assertEqual(self.bridge.name_aliases[self.key], NEW_IDS)
        self.assertFalse(stale)
        self.assertIsNone(error)
        self.bridge.address_refresh.assert_called_once()

    async def test_house_lookup_500_recovers_changed_parent_ids(self):
        first, second = upstream(ADDRESS), upstream()
        first.get_house_numbers.side_effect = AbfallOoeConnectionError("synthetic house lookup HTTP 500")
        with patch("core.service.AbfallOoeClient", side_effect=[first, second]):
            entry, stale, _ = await self.bridge.get_calendar(ADDRESS, NAMES)
        first.get_calendar.assert_not_awaited()
        second.get_cities.assert_awaited_once_with("new-d")
        second.get_streets.assert_awaited_once_with("new-c")
        second.get_house_numbers.assert_awaited_once_with("new-s")
        self.assertEqual(entry["address_ids"], NEW_IDS)
        self.assertFalse(stale)

    async def test_empty_recovery_does_not_erase_saved_future_dates(self):
        saved = calendar(pickup(), fetched_on="2026-11-03")
        self.bridge.cache[self.key] = copy.deepcopy(saved)
        client = upstream()
        client.get_calendar.side_effect = None
        client.get_calendar.return_value = CalendarData([], {})
        with patch("core.service.AbfallOoeClient", return_value=client) as factory:
            entry, stale, _ = await self.bridge.get_calendar(ADDRESS, NAMES)
        self.assertTrue(stale)
        self.assertEqual(factory.call_count, 2)
        for field in ("pickups", "address_ids", "fetched_at", "fetched_on"):
            self.assertEqual(entry[field], saved[field])

    async def test_migrated_id_cache_dates_survive_failure_and_restart(self):
        saved = calendar(pickup())  # Today, but revalidation is required.
        self.bridge.cache[normalized_key(ADDRESS)] = saved
        with patch.object(self.bridge, "_fetch_calendar", AsyncMock(side_effect=TimeoutError())) as fetch:
            entry, stale, _ = await self.bridge.get_calendar(ADDRESS, NAMES)
        fetch.assert_awaited_once()
        self.assertTrue(stale)
        self.assertEqual(entry["pickups"], saved["pickups"])
        restarted = type(self.bridge)(self.bridge.cache_file, self.bridge.api_key, self.bridge.log_file)
        self.addAsyncCleanup(restarted.close)
        await restarted.load(None)
        with patch.object(restarted, "_fetch_calendar", AsyncMock()) as fetch:
            entry, stale, _ = await restarted.get_calendar(ADDRESS, NAMES)
        fetch.assert_not_awaited()
        self.assertTrue(stale)
        self.assertEqual(entry["pickups"], saved["pickups"])

    def test_interval_names_are_missing_or_ambiguous_without_guessing(self):
        for choices, wanted, code in (({"a": "2 Wochen", "b": "2 Wochen"}, "2 Wochen", "interval_ambiguous"),
                                       ({"a": "2 Wochen"}, "2", "interval_not_found")):
            entry = calendar(pickup())
            entry["type_intervals"] = {"t1": choices}
            with self.assertRaises(web.HTTPUnprocessableEntity) as caught:
                self.bridge.response_data(entry, NAMES, False, None, intervals={"Restabfall": wanted})
            self.assertEqual(json.loads(caught.exception.text)["error"], code)

    def test_future_dates_for_other_interval_do_not_hide_expired_selection(self):
        entry = calendar(pickup(interval="o2"))
        entry["type_intervals"] = {"t1": {"o1": "Weekly", "o2": "Fortnightly"}}
        with self.assertRaises(web.HTTPServiceUnavailable):
            self.bridge.response_data(entry, NAMES, True, "upstream_unavailable", intervals={"Restabfall": "Weekly"})

    async def test_failed_recovery_keeps_future_dates_timestamp_and_ids_across_restart(self):
        saved = calendar(pickup(), fetched_on="2026-11-03")
        self.bridge.cache[self.key] = copy.deepcopy(saved)
        self.bridge.name_aliases[self.key] = dict(ADDRESS)
        with patch("core.service.AbfallOoeClient", side_effect=lambda _: upstream(fail=True)) as factory:
            entry, stale, error = await self.bridge.get_calendar(ADDRESS, NAMES)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(entry["address_ids"], ADDRESS)
        self.assertEqual(self.bridge.name_aliases[self.key], ADDRESS)
        data = self.bridge.response_data(entry, NAMES, stale, error)
        self.assertTrue(data["cache_stale"])
        self.assertEqual(data["cache_status"], "stale")
        self.assertEqual(data["last_successful_fetch_at"], saved["fetched_at"])
        restarted = type(self.bridge)(self.bridge.cache_file, self.bridge.api_key, self.bridge.log_file)
        self.addAsyncCleanup(restarted.close)
        await restarted.load(None)
        with patch.object(restarted, "_fetch_calendar", AsyncMock()) as fetch:
            entry, stale, _ = await restarted.get_calendar(ADDRESS, NAMES)
        fetch.assert_not_awaited()
        self.assertTrue(stale)
        self.assertEqual(entry["pickups"], saved["pickups"])

    async def test_failed_recovery_without_future_dates_is_temporary_and_bounded(self):
        for saved in (None, calendar(pickup("2026-11-03"), fetched_on="2026-11-03")):
            self.bridge.retry_at.clear()
            self.bridge.cache.clear()
            if saved:
                self.bridge.cache[self.key] = saved
            with patch("core.service.AbfallOoeClient", side_effect=lambda _: upstream(fail=True)) as factory:
                for _ in range(3):
                    with self.assertRaises(web.HTTPServiceUnavailable) as caught:
                        await self.bridge.get_calendar(ADDRESS, NAMES)
                    data = json.loads(caught.exception.text)
                    self.assertTrue(data["temporary"])
                    self.assertIn("retry_at", data)
                    self.assertGreater(int(caught.exception.headers["Retry-After"]), 0)
            self.assertEqual(factory.call_count, 2)

    async def test_deadline_returns_saved_dates_or_temporary_error(self):
        self.bridge.lookups.timeout = 0.03
        for saved in (None, calendar(pickup(), fetched_on="2026-11-03")):
            self.bridge.retry_at.clear()
            if saved:
                self.bridge.cache[self.key] = saved
            async def blocked(*_):
                await asyncio.Event().wait()
            with patch.object(self.bridge, "_fetch_calendar", side_effect=blocked) as fetch:
                if saved:
                    _, stale, code = await self.bridge.get_calendar(ADDRESS, NAMES)
                    self.assertTrue(stale)
                    self.assertEqual(code, "lookup_timeout")
                else:
                    with self.assertRaises(web.HTTPServiceUnavailable) as caught:
                        await self.bridge.get_calendar(ADDRESS, NAMES)
                    self.assertEqual(json.loads(caught.exception.text)["error"], "lookup_timeout")
                # A timeout also observes per-address backoff.
                try:
                    await self.bridge.get_calendar(ADDRESS, NAMES)
                except web.HTTPServiceUnavailable:
                    pass
                fetch.assert_awaited_once()

    async def test_missing_or_ambiguous_at_every_level_is_explicit(self):
        for method, field in (("get_districts", "district"), ("get_cities", "municipality"),
                              ("get_streets", "street"), ("get_house_numbers", "house_number")):
            for ambiguous in (False, True):
                self.bridge.retry_at.clear()
                client = upstream()
                lookup = getattr(client, method)
                label = next(iter(lookup.return_value.values()))
                lookup.return_value = {"a": label, "b": label} if ambiguous else {"a": "Other"}
                with patch("core.service.AbfallOoeClient", return_value=client):
                    with self.assertRaises(web.HTTPUnprocessableEntity) as caught:
                        await self.bridge.get_calendar({}, NAMES)
                data = json.loads(caught.exception.text)
                self.assertEqual(data["field"], field)
                self.assertEqual(data["error"], "address_ambiguous" if ambiguous else "address_not_found")

    def test_safe_normalization_preserves_significant_characters(self):
        self.assertEqual(select_id({"a": " Ä_ / St. "}, "A\u0308_ / St.", "street"), "a")
        for query in ("ä_ / St.", "Ä  / St.", "Ä_", "Ä_ / St"):
            with self.assertRaises(InvalidAddressError):
                select_id({"a": "Ä_ / St."}, query, "street")
        with self.assertRaisesRegex(InvalidAddressError, "address_ambiguous"):
            select_id({"a": "Ä", "b": "A\u0308"}, "Ä", "street")
        self.assertNotEqual(normalized_name_key({**NAMES, "street_name": "A|B"}),
                            normalized_name_key({**NAMES, "street_name": "A", "house_number_name": "B|1"}))

    async def test_same_address_shares_work_when_one_caller_disconnects(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def fetch(*_):
            started.set()
            await finish.wait()
            return calendar(pickup())
        with patch.object(self.bridge, "_fetch_calendar", side_effect=fetch) as calls:
            tasks = [asyncio.create_task(self.bridge.get_calendar({}, NAMES)) for _ in range(12)]
            await started.wait()
            tasks[0].cancel()
            finish.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        calls.assert_awaited_once()
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertTrue(all(result[0]["pickups"] for result in results[1:]))

    async def test_different_addresses_have_bounded_queue_and_overload(self):
        self.bridge.lookups = LookupPool(active=1, queued=1, timeout=1)
        started, finish = asyncio.Event(), asyncio.Event()
        active, maximum = 0, 0
        async def fetch(*_):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            started.set()
            await finish.wait()
            active -= 1
            return calendar(pickup())
        with patch.object(self.bridge, "_fetch_calendar", side_effect=fetch):
            one = asyncio.create_task(self.bridge.get_calendar({}, NAMES))
            await started.wait()
            two = asyncio.create_task(self.bridge.get_calendar({}, {**NAMES, "house_number_name": "2"}))
            await asyncio.sleep(0)
            with self.assertRaises(web.HTTPServiceUnavailable) as caught:
                await self.bridge.get_calendar({}, {**NAMES, "house_number_name": "3"})
            self.assertEqual(json.loads(caught.exception.text)["error"], "lookup_overloaded")
            finish.set()
            await asyncio.gather(one, two)
        self.assertEqual(maximum, 1)
        self.assertFalse(self.bridge.lookups.inflight)

    async def test_waiting_queue_has_deadline_and_removes_cancelled_work(self):
        pool = LookupPool(active=1, queued=1, timeout=0.03)
        self.addAsyncCleanup(pool.close)
        await pool.slots.acquire()
        operation = AsyncMock()
        with self.assertRaises(TimeoutError):
            await pool.run("queued", operation)
        operation.assert_not_awaited()
        self.assertFalse(pool.inflight)
        pool.slots.release()

    async def test_current_cache_is_available_while_lookup_queue_is_full(self):
        self.bridge.cache[self.key] = calendar(pickup())
        self.bridge.lookups = LookupPool(active=1, queued=0)
        started, finish = asyncio.Event(), asyncio.Event()
        async def blocked():
            started.set()
            await finish.wait()
        task = asyncio.create_task(self.bridge.lookups.run("unrelated", blocked))
        await started.wait()
        entry, stale, _ = await self.bridge.get_calendar({}, NAMES)
        self.assertFalse(stale)
        self.assertTrue(entry["pickups"])
        finish.set()
        await task

    async def test_cancelled_cache_writer_cannot_overwrite_newer_saved_dates(self):
        started, finish = threading.Event(), threading.Event()
        original = Path.write_text
        first = True
        def slow_first_write(path, content, **kwargs):
            nonlocal first
            if first:
                first = False
                started.set()
                if not finish.wait(3):
                    raise OSError("test writer was not released")
            return original(path, content, **kwargs)
        self.bridge.cache[self.key] = calendar(pickup("2026-11-05"))
        with patch.object(Path, "write_text", slow_first_write):
            older = asyncio.create_task(self.bridge._persist())
            await asyncio.wait_for(asyncio.to_thread(started.wait), 1)
            older.cancel()
            self.bridge.cache[self.key] = calendar(pickup("2026-11-12"))
            newer = asyncio.create_task(self.bridge._persist())
            await asyncio.sleep(0.02)
            finish.set()
            await asyncio.gather(older, newer, return_exceptions=True)
        saved = json.loads(self.bridge.cache_file.read_text())
        self.assertEqual(saved["entries"][self.key]["pickups"][0]["date"], "2026-11-12")


class RecoveryHttpTests(BridgeTestCase):
    async def asyncSetUp(self):
        self.data_file = patch.object(helper, "ADDRESS_DATA_FILE", self.folder / "addresses.json")
        self.data_file.start()
        self.addCleanup(self.data_file.stop)
        self.client = TestClient(TestServer(create_app(self.bridge)))
        self.addAsyncCleanup(self.client.close)
        await self.client.start_server()
        self.bridge.recovery_backoff = 0
        self.headers = {"X-Api-Key": "test-only-key"}

    async def test_standard_query_encoding_round_trips_special_characters(self):
        names = {"district_name": "Bezirk Ä & _", "city_name": "St. Ö / Süd",
                 "street_name": "Straße_ + ? # % &", "house_number_name": "1/2_a"}
        client = upstream(labels=names)
        with patch("core.service.AbfallOoeClient", return_value=client):
            response = await self.client.get("/api/v1/pickups", params={**names, "details": "1", "intervals": json.dumps({"Restabfall": "2-wöchentlich"})}, headers=self.headers)
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["address"]["street"], names["street_name"])
        self.assertNotIn("street_id", data["address"])
        self.assertEqual(self.bridge.cache[normalized_name_key(names)]["address_names"], names)

    async def test_helper_ignores_snapshot_ids_and_resolves_full_names(self):
        write_snapshot(helper.ADDRESS_DATA_FILE, snapshot())
        client = upstream()
        with patch.object(helper, "AbfallOoeClient", return_value=client):
            response = await self.client.get("/setup/api/v1/locations/house-numbers", params=NAMES)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), [{"id": "1", "name": "1"}])
        client.get_streets.assert_awaited_once_with("new-c")
        client.get_house_numbers.assert_awaited_once_with("new-s")

    async def test_helper_interval_resolution_includes_current_house(self):
        client = upstream()
        with patch.object(helper, "AbfallOoeClient", return_value=client):
            response = await self.client.get("/setup/api/v1/locations/intervals", params=NAMES)
        self.assertEqual(response.status, 200)
        client.get_intervals.assert_awaited_once_with("new-s", "new-h")

    async def test_background_refresh_does_not_block_recovery_or_unrelated_requests(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def slow_build(**_):
            started.set()
            await finish.wait()
            return snapshot(ADDRESS)  # Older IDs finish after the recovery.
        with patch.object(helper, "build_snapshot", side_effect=slow_build) as build, \
             patch("core.service.AbfallOoeClient", side_effect=[upstream(ADDRESS, fail=True), upstream()]):
            response = await self.client.get("/api/v1/pickups", params={**NAMES, "intervals": json.dumps({"Restabfall": "2-wöchentlich"})}, headers=self.headers)
            self.assertEqual(response.status, 200)
            await started.wait()
            app = self.client.app._subapps[0]
            self.assertFalse(app["address_refresh_task"].done())
            self.assertEqual((await self.client.get("/health")).status, 200)
            with patch.object(self.bridge, "_fetch_calendar", AsyncMock(return_value=calendar(pickup()))):
                other = await self.client.get("/api/v1/pickups", params={**NAMES, "house_number_name": "99"}, headers=self.headers)
                self.assertEqual(other.status, 200)
            helper.start_address_refresh(app)
            build.assert_awaited_once()
            finish.set()
            await app["address_refresh_task"]
        self.assertEqual(self.bridge.name_aliases[normalized_name_key(NAMES)], NEW_IDS)
        self.assertEqual(self.bridge.cache[normalized_name_key(NAMES)]["address_ids"], NEW_IDS)
        self.assertEqual(json.loads(helper.ADDRESS_DATA_FILE.read_text())["districts"][0]["id"], "2")

    async def test_failed_or_invalid_full_refresh_keeps_last_valid_dataset_and_dates(self):
        write_snapshot(helper.ADDRESS_DATA_FILE, snapshot())
        before = helper.ADDRESS_DATA_FILE.read_bytes()
        self.bridge.cache[normalized_name_key(NAMES)] = calendar(pickup())
        for replacement in ({**snapshot(), "districts": []}, {**snapshot(), "districts": [{"id": "1", "name": "District", "cities": []}]}):
            marker = self.folder / "address-refresh-attempt.json"
            marker.unlink(missing_ok=True)
            with patch.object(helper, "build_snapshot", AsyncMock(return_value=replacement)):
                helper.start_address_refresh(self.client.app._subapps[0])
                await self.client.app._subapps[0]["address_refresh_task"]
            self.assertEqual(helper.ADDRESS_DATA_FILE.read_bytes(), before)
            self.assertEqual(self.bridge.cache[normalized_name_key(NAMES)]["pickups"], [pickup()])
        with self.assertRaises(web.HTTPTooManyRequests):
            helper.start_address_refresh(self.client.app._subapps[0])

    async def test_build_snapshot_does_not_accept_incomplete_upstream_branch(self):
        client = upstream()
        client.get_streets.return_value = {}
        with patch("refresh_address_data.AbfallOoeClient", return_value=client):
            with self.assertRaises(ValueError):
                await build_snapshot(delay=0)

    async def test_late_recovery_cannot_patch_newer_full_snapshot(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def slow_calendar(*_):
            started.set()
            await finish.wait()
            return calendar(pickup())  # Old IDs complete after snapshot publish.
        with patch.object(self.bridge, "_fetch_calendar", side_effect=slow_calendar), \
             patch.object(helper, "build_snapshot", AsyncMock(return_value=snapshot(NEW_IDS))):
            request = asyncio.create_task(self.client.get("/api/v1/pickups", params=NAMES, headers=self.headers))
            await started.wait()
            app = self.client.app._subapps[0]
            helper.start_address_refresh(app)
            await app["address_refresh_task"]
            finish.set()
            self.assertEqual((await request).status, 200)
        self.assertEqual(json.loads(helper.ADDRESS_DATA_FILE.read_text())["districts"][0]["id"], "new-d")
