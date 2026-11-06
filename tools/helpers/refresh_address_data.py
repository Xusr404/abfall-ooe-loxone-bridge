"""Refresh a local JSON snapshot of Abfall OÖ districts, municipalities, and streets."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

BRIDGE_DIR = Path(__file__).resolve().parents[2] / "bridge"
sys.path.insert(0, str(BRIDGE_DIR))

from abfall_ooe.api import AbfallOoeClient, AbfallOoeError
from core.common import ID_RE, normalize_name

HELPER_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = Path(os.environ.get("LOXONE_BRIDGE_ADDRESS_DATA_FILE") or HELPER_DIR / "data" / "address-data.json")


async def build_snapshot(
    delay: float,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        client = AbfallOoeClient(session)
        districts = await client.get_districts()
        district_rows: list[dict[str, object]] = []
        city_count = 0
        street_count = 0

        for district_number, (district_id, district_name) in enumerate(districts.items(), 1):
            if progress:
                progress(f"Bezirk {district_number} von {len(districts)}: {district_name}")
            cities = await client.get_cities(district_id)
            city_rows: list[dict[str, object]] = []
            print(f"[{district_number}/{len(districts)}] {district_name}: {len(cities)} municipalities")
            city_count += len(cities)

            for city_number, (city_id, city_name) in enumerate(cities.items(), 1):
                if progress:
                    progress(f"{district_name}: Gemeinde {city_number} von {len(cities)} — {city_name}")
                streets = await client.get_streets(city_id)
                city_rows.append(
                    {
                        "id": city_id,
                        "name": city_name,
                        "streets": [
                            {"id": street_id, "name": street_name}
                            for street_id, street_name in streets.items()
                        ],
                    }
                )
                street_count += len(streets)
                if delay:
                    await asyncio.sleep(delay)

            district_rows.append(
                {
                    "id": district_id,
                    "name": district_name,
                    "cities": city_rows,
                }
            )

        print(f"Collected {len(district_rows)} districts, {city_count} municipalities, {street_count} streets.")
        snapshot = {
            "schema_version": 1,
            "source": "Umweltprofis Oberösterreich",
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "districts": district_rows,
        }
        validate_snapshot(snapshot)
        return snapshot


def validate_snapshot(snapshot: dict[str, object]) -> None:
    """Reject empty/partial branches and invalid IDs before publishing anything."""
    if snapshot.get("schema_version") != 1:
        raise ValueError("Unsupported address snapshot schema")
    stamp = datetime.fromisoformat(snapshot["updated_at"])
    if stamp.tzinfo is None:
        raise ValueError("Missing address snapshot timezone")

    def check(rows, child=None):
        if not isinstance(rows, list) or not rows:
            raise ValueError("Empty address hierarchy")
        ids = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not ID_RE.fullmatch(row["id"]):
                raise ValueError("Invalid address ID")
            if row["id"] in ids or not isinstance(row.get("name"), str) or not normalize_name(row["name"]):
                raise ValueError("Invalid address row")
            ids.add(row["id"])
            if child:
                check(row.get(child), "streets" if child == "cities" else None)
    check(snapshot.get("districts"), "cities")


def write_snapshot(path: Path, snapshot: dict[str, object]) -> None:
    if "schema_version" in snapshot or "districts" in snapshot:
        validate_snapshot(snapshot)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh the local districts, municipalities, and streets JSON for address-helper.html."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--delay",
        type=float,
        default=0.05,
        help="pause between municipality street lookups in seconds (default: 0.05)",
    )
    args = parser.parse_args()
    if args.delay < 0:
        parser.error("--delay must be zero or greater")

    try:
        snapshot = asyncio.run(build_snapshot(args.delay))
        write_snapshot(args.output, snapshot)
    except (AbfallOoeError, aiohttp.ClientError, TimeoutError, OSError, ValueError) as err:
        parser.exit(1, f"Could not refresh address data: {err}\n")

    print(f"Saved address data to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
