"""Synthetic calendars and a controllable clock shared by the tests."""

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))

from core.service import PickupBridge


NAMES = {"district_name": "Testbezirk", "city_name": "Testgemeinde", "street_name": "Teststraße", "house_number_name": "1"}

ADDRESS = {"district_id": "2", "city_id": "10", "street_id": "20", "house_number_id": "30"}


class Clock(datetime):
    instant = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)


def pickup(day="2026-11-05", waste_type="Restabfall", type_id="t1", interval="o1"):
    return {"waste_type_id": type_id, "waste_type": waste_type, "date": day,
            "interval_id": interval, "interval": "2-wöchentlich"}


def calendar(*rows, fetched_on="2026-11-04"):
    return {"fetched_at": "2026-11-04T10:00:00+00:00", "fetched_on": fetched_on,
            "address_ids": dict(ADDRESS), "pickups": list(rows), "type_intervals": {}}


class BridgeTestCase(IsolatedAsyncioTestCase):
    def setUp(self):
        original_connect = aiohttp.TCPConnector.connect

        async def local_connect(connector, request, *args, **kwargs):
            # Check each connection, including redirects, before DNS or network I/O.
            if request.url.host not in {"127.0.0.1", "::1"}:
                raise AssertionError("External HTTP connections are forbidden in tests")
            return await original_connect(connector, request, *args, **kwargs)

        patcher = patch.object(aiohttp.TCPConnector, "connect", local_connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.bridge = PickupBridge(self.folder / "cache.json", "test-only-key", self.folder / "bridge.log")
        Clock.instant = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
        for module in ("core.service", "core.storage", "core.common"):
            patcher = patch(module + ".datetime", Clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("core.service.utc_now", side_effect=lambda: Clock.now(timezone.utc))
        patcher.start()
        self.addCleanup(patcher.stop)
        # Fail immediately if a test accidentally attempts a real upstream lookup.
        for module in ("core.service", "address_helper_server"):
            if module not in sys.modules:
                continue
            patcher = patch(module + ".AbfallOoeClient", side_effect=AssertionError("Live upstream requests are forbidden in tests"))
            patcher.start()
            self.addCleanup(patcher.stop)
