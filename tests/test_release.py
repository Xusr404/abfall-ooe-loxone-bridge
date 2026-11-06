"""Release regressions use only synthetic data and temporary state."""

import asyncio
import json
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

from aiohttp import web

from tests import test_http
from tests.support import BridgeTestCase, ADDRESS, NAMES, calendar, pickup
from core.auth import validate_api_key


def variants():
    entry = calendar(pickup("2026-11-05"), pickup("2026-11-12", interval="o2"))
    entry["type_intervals"] = {"t1": {"o1": "Weekly", "o2": "Fortnightly"}}
    return entry


class ScheduleTests(BridgeTestCase):
    def test_defaults_to_first_upstream_interval_and_supports_title_fallback(self):
        data = self.bridge.response_data(variants(), {}, False, None)
        self.assertEqual(data["next_pickup"]["date"], "2026-11-05")
        data = self.bridge.response_data(variants(), {}, False, None,
                                         intervals={"Restabfall": "Fortnightly"})
        self.assertEqual(data["next_pickup"]["date"], "2026-11-12")

    def test_sole_schedule_is_automatic(self):
        entry = variants()
        entry["type_intervals"]["t1"].pop("o2")
        self.assertEqual(self.bridge.response_data(entry, {}, False, None)["next_pickup"]["date"], "2026-11-05")

    def test_expired_stale_rows_are_not_ok(self):
        with self.assertRaises(web.HTTPServiceUnavailable):
            self.bridge.response_data(calendar(pickup("2026-11-03")), {}, True, "upstream_unavailable")

    async def test_cache_recovery_discards_bad_entries_and_metadata(self):
        entry = calendar(pickup())
        entry["pickups"].append({"date": "bad"})
        self.bridge.cache_file.write_text(json.dumps({"entries": {"good": entry, "bad": [],
            "bad_time": {**entry, "fetched_at": "yesterday"}}, "attempted_on": [],
            "last_errors": None, "name_aliases": {"bad": {}}}), encoding="utf-8")
        await self.bridge.load(None)
        self.assertEqual(set(self.bridge.cache), {"good"})
        self.assertEqual(len(self.bridge.cache["good"]["pickups"]), 1)
        self.assertEqual(self.bridge.name_aliases, {})

    async def test_invalid_envelope_and_legacy_cache(self):
        for payload in ({"entries": []}, [1], {"version": 3}):
            self.bridge.cache_file.write_text(json.dumps(payload), encoding="utf-8")
            await self.bridge.load(None)
            self.assertEqual(self.bridge.cache, {})
        self.bridge.cache_file.write_text(json.dumps({"legacy": calendar(pickup())}), encoding="utf-8")
        await self.bridge.load(None)
        self.assertIn("legacy", self.bridge.cache)

    def test_control_keys_rejected(self):
        for key in ("a\nb", "a\x7fb", "a\x85b"):
            with self.assertRaises(ValueError):
                validate_api_key(key)
        validate_api_key("schlüssel")


class ReleaseHttpTests(test_http.HttpTests):
    async def test_selection_endpoint_and_cache_reuse(self):
        fetch = AsyncMock(return_value=variants())
        headers = {"X-Api-Key": "test-only-key"}
        with patch.object(self.bridge, "_fetch_calendar", fetch):
            endpoint = "/setup/api/v1/calendar-intervals"
            self.assertEqual((await self.client.get(endpoint, params=NAMES)).status, 401)
            response = await self.client.get(endpoint, params=NAMES, headers=headers)
            self.assertEqual((await response.json())["choices"], {"Restabfall": ["Weekly", "Fortnightly"]})
            response = await self.client.get("/api/v1/pickups", params=NAMES, headers=headers)
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["next_pickup"]["date"], "2026-11-05")
            for value, day in (("Weekly", "2026-11-05"), ("Fortnightly", "2026-11-12")):
                response = await self.client.get("/api/v1/pickups", headers=headers,
                    params={**NAMES, "intervals": json.dumps({"Restabfall": value})})
                self.assertEqual((await response.json())["next_pickup"]["date"], day)
        fetch.assert_awaited_once()

    async def test_malformed_selection_does_not_fetch(self):
        with patch.object(self.bridge, "_fetch_calendar", AsyncMock()) as fetch:
            for value in ("null", "[]", "broken", '{"t1": 2}'):
                response = await self.client.get("/api/v1/pickups", params={**NAMES, "intervals": value},
                                                 headers={"X-Api-Key": "test-only-key"})
                self.assertEqual(response.status, 400)
            fetch.assert_not_awaited()

    async def test_unicode_keys_work_and_wrong_unicode_is_unauthorized(self):
        self.bridge.api_key = "schlüssel🔑"
        response = await self.client.get("/api/v1/logs", headers={
            "X-Api-Key": quote(self.bridge.api_key), "X-Api-Key-Encoding": "percent"})
        self.assertEqual(response.status, 200)
        self.bridge.api_key = "schlüssel"
        for value, status in (("schlüssel", 200), ("falschä", 401)):
            response = await self.client.get("/api/v1/logs", params={"key": value})
            self.assertEqual(response.status, status)

    async def test_refresh_auth_duplicate_and_persisted_failure_cooldown(self):
        import address_helper_server as helper
        from loxone_bridge import create_app
        from aiohttp.test_utils import TestClient, TestServer
        endpoint = "/setup/api/v1/address-data/refresh"
        headers = {"X-Refresh-Request": "1", "X-Api-Key": "test-only-key"}
        started, finish = asyncio.Event(), asyncio.Event()

        async def fail(**kwargs):
            started.set()
            await finish.wait()
            raise OSError("synthetic failure")

        with patch.object(helper, "ADDRESS_DATA_FILE", self.folder / "addresses.json"), \
             patch.object(helper, "build_snapshot", side_effect=fail) as build:
            self.assertEqual((await self.client.post(endpoint, headers={"X-Refresh-Request": "1"})).status, 401)
            self.assertEqual((await self.client.post(endpoint, headers=headers)).status, 202)
            await started.wait()
            self.assertEqual((await self.client.post(endpoint, headers=headers)).status, 202)
            build.assert_awaited_once()
            finish.set()
            await self.client.app._subapps[0]["address_refresh_task"]
            response = await self.client.post(endpoint, headers=headers)
            self.assertEqual(response.status, 429)
            self.assertGreater(int(response.headers["Retry-After"]), 0)
            async with TestClient(TestServer(create_app(self.bridge))) as restarted:
                self.assertEqual((await restarted.post(endpoint, headers=headers)).status, 429)

    async def test_refresh_without_key_after_cooldown_and_marker_failure(self):
        import address_helper_server as helper
        endpoint = "/setup/api/v1/address-data/refresh"
        headers = {"X-Refresh-Request": "1"}
        self.bridge.api_key = None
        marker = self.folder / "address-refresh-attempt.json"
        marker.write_text(json.dumps({"started_at": 1000}), encoding="utf-8")
        with patch.object(helper, "ADDRESS_DATA_FILE", self.folder / "addresses.json"), \
             patch.object(helper.time, "time", return_value=4601), \
             patch.object(helper, "build_snapshot", AsyncMock(return_value={"updated_at": "synthetic"})) as build:
            response = await self.client.post(endpoint, headers=headers)
            self.assertEqual(response.status, 202)
            await self.client.app._subapps[0]["address_refresh_task"]
            build.assert_awaited_once()
            self.assertEqual(json.loads(marker.read_text())["started_at"], 4601)
            marker.write_text("broken", encoding="utf-8")
            self.assertEqual((await self.client.post(endpoint, headers=headers)).status, 503)
