from unittest.mock import AsyncMock, patch

from tests.support import ADDRESS, NAMES, BridgeTestCase, calendar, pickup
from aiohttp import ClientTimeout
from aiohttp.test_utils import TestClient, TestServer
from loxone_bridge import create_app


class HttpTests(BridgeTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(create_app(self.bridge)), timeout=ClientTimeout(total=5))
        self.addAsyncCleanup(self.client.close)
        await self.client.start_server()

    async def test_external_http_is_blocked_before_connecting(self):
        with self.assertRaisesRegex(AssertionError, "External HTTP connections are forbidden"):
            await self.client.session.get("https://example.invalid/")

    async def test_health_and_setup_are_available_without_key(self):
        response = await self.client.get("/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"status": "ok"})
        response = await self.client.get("/", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/setup/")
        response = await self.client.get("/setup/")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers["Content-Type"])

    async def test_pickups_and_logs_reject_missing_or_wrong_key(self):
        for endpoint in ("/api/v1/pickups", "/api/v1/logs"):
            for params in ({}, {"key": "wrong-key"}):
                with self.subTest(endpoint=endpoint, params=params):
                    response = await self.client.get(endpoint, params=params)
                    self.assertEqual(response.status, 401)
                    self.assertEqual(await response.json(), {"error": "unauthorized"})

    async def test_valid_names_accept_key_in_query_or_header_and_return_pickup(self):
        fetch = AsyncMock(side_effect=lambda *_: calendar(pickup()))
        with patch.object(self.bridge, "_fetch_calendar", fetch):
            credentials = (
                {"params": {**NAMES, "key": "test-only-key"}},
                {"params": NAMES, "headers": {"X-Api-Key": "test-only-key"}},
            )
            for options in credentials:
                response = await self.client.get("/api/v1/pickups", **options)
                self.assertEqual(response.status, 200)
                data = await response.json()
                self.assertEqual(data["by_waste_type"]["Restabfall"], {"date": "2026-11-05", "days_until": 1})
                self.assertFalse(data["cache_stale"])
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        fetch.assert_awaited_once()

    async def test_invalid_address_input_is_rejected_before_fetch(self):
        invalid = ({}, {"district_id": "2"}, {**ADDRESS, "street_id": "../invalid"}, {**ADDRESS, "city_name": "x" * 129})
        fetch = AsyncMock()
        with patch.object(self.bridge, "_fetch_calendar", fetch):
            for params in invalid:
                with self.subTest(params=params):
                    response = await self.client.get("/api/v1/pickups", params=params, headers={"X-Api-Key": "test-only-key"})
                    self.assertEqual(response.status, 400)
        fetch.assert_not_awaited()

    async def test_log_tail_includes_rotated_file_and_limits_lines(self):
        self.bridge.log_file.with_suffix(".log.1").write_text("older\nprevious\n", encoding="utf-8")
        self.bridge.log_file.write_text("latest\n", encoding="utf-8")
        response = await self.client.get("/api/v1/logs", params={"lines": "2"}, headers={"X-Api-Key": "test-only-key"})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), "previous\nlatest")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_invalid_log_line_count_returns_400(self):
        for lines in ("zero", "0", "-1"):
            with self.subTest(lines=lines):
                response = await self.client.get("/api/v1/logs", params={"lines": lines}, headers={"X-Api-Key": "test-only-key"})
                self.assertEqual(response.status, 400)

    async def test_authentication_can_be_disabled_explicitly(self):
        self.bridge.api_key = None
        response = await self.client.get("/api/v1/logs")
        self.assertEqual(response.status, 200)
        response = await self.client.get("/api/v1/pickups")
        self.assertEqual(response.status, 400)  # Authentication passed; address is missing.
