"""Shared constants, errors, and small helpers for the Loxone bridge."""

from __future__ import annotations

import aiohttp
import hashlib
import logging
import re
import json
import unicodedata
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from abfall_ooe.api import AbfallOoeConnectionError, AbfallOoeParseError
from abfall_ooe.parser import Pickup

LOGGER = logging.getLogger("loxone_bridge")
LOCAL_TZ = ZoneInfo("Europe/Vienna")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LOCATION_FIELDS = ("district_id", "city_id", "street_id", "house_number_id")
NAME_FIELDS = ("district_name", "city_name", "street_name", "house_number_name")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def normalized_key(values: dict[str, str]) -> str:
    stable = "|".join(values[field].strip().casefold() for field in LOCATION_FIELDS)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def normalized_name_key(labels: dict[str, str]) -> str:
    stable = json.dumps([normalize_name(labels.get(field, "")) for field in NAME_FIELDS], ensure_ascii=False)
    return hashlib.sha256(("names|" + stable).encode("utf-8")).hexdigest()


def select_id(choices: dict[str, str], query: str, field: str) -> str:
    """Only trim and canonicalize Unicode; never guess a partial match."""
    matches = [key for key, label in choices.items() if normalize_name(label) == normalize_name(query)]
    if len(matches) != 1:
        raise InvalidAddressError("address_ambiguous" if matches else "address_not_found", field)
    return matches[0]


def normalize_name(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip())


class InvalidAddressError(ValueError):
    """An address name is absent or not unique within its parent."""

    def __init__(self, code: str, field: str | None = None):
        super().__init__(code)
        self.field = field


class CachePersistenceError(Exception):
    """The bridge could not persist its cache or refresh-attempt marker."""


def failure_category(err: BaseException) -> str:
    root_cause = err
    while root_cause.__cause__ is not None:
        root_cause = root_cause.__cause__
    if isinstance(root_cause, TimeoutError):
        return "timeout"
    if isinstance(err, AbfallOoeParseError):
        return "source_parse_error"
    if isinstance(err, AbfallOoeConnectionError) or isinstance(root_cause, aiohttp.ClientError):
        return "connection_or_http"
    if isinstance(err, OSError):
        return "local_io_error"
    if isinstance(err, ValueError):
        return "invalid_source_data"
    return "upstream_error"


def serialize_pickup(pickup: Pickup) -> dict[str, Any]:
    return {
        "waste_type_id": pickup.waste_type_id,
        "waste_type": pickup.waste_type,
        "date": pickup.date.isoformat(),
        "interval_id": pickup.interval_id,
        "interval": pickup.interval,
    }


def deserialize_pickup(data: dict[str, Any]) -> Pickup:
    return Pickup(
        waste_type_id=str(data["waste_type_id"]),
        waste_type=str(data["waste_type"]),
        date=date.fromisoformat(str(data["date"])),
        interval_id=str(data["interval_id"]) if data.get("interval_id") else None,
        interval=str(data.get("interval", "")),
    )
