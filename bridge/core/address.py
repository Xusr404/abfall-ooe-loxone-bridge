"""Resolve one hierarchy in the upstream session that will use its IDs."""

from .common import NAME_FIELDS, select_id


async def resolve_address(client, labels, depth=4):
    values = {}
    lookups = (
        ("district_id", "district", client.get_districts),
        ("city_id", "municipality", lambda: client.get_cities(values["district_id"])),
        ("street_id", "street", lambda: client.get_streets(values["city_id"])),
        ("house_number_id", "house_number", lambda: client.get_house_numbers(values["street_id"])),
    )
    for (id_field, field, lookup), name_field in zip(lookups[:depth], NAME_FIELDS):
        values[id_field] = select_id(await lookup(), labels[name_field], field)
    return values
