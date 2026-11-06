from datetime import date
from pathlib import Path
from unittest import TestCase

from tests import support  # Make the standalone bridge modules importable.
from abfall_ooe.parser import filter_pickups, parse_pickups, parse_type_intervals
from core.common import select_id


class ParserTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = Path(__file__).with_name("fixtures").joinpath("calendar.html").read_text(encoding="utf-8")

    def test_calendar_decodes_names_dates_and_intervals(self):
        rows = parse_pickups(self.page)
        self.assertEqual(len(rows), 4)  # Malformed and incomplete rows are skipped.
        self.assertEqual(rows[0].waste_type, "Papier & Karton")
        self.assertEqual(rows[0].date, date(2026, 11, 5))
        self.assertIsNone(rows[0].interval_id)
        self.assertEqual(rows[1].interval_id, "o1")
        self.assertEqual(rows[1].interval, "2-wöchentlich")

    def test_all_intervals_keep_dates_but_remove_duplicates(self):
        rows = filter_pickups(parse_pickups(self.page), parse_type_intervals(self.page), None)
        self.assertEqual([(p.waste_type_id, p.date) for p in rows], [
            ("t2", date(2026, 11, 5)), ("t1", date(2026, 11, 6)), ("t1", date(2026, 11, 20)),
        ])

    def test_interval_title_recovers_renumbered_interval_id(self):
        rows = filter_pickups(parse_pickups(self.page), parse_type_intervals(self.page), "o999", "4-WÖCHENTLICH")
        self.assertEqual([p.interval_id for p in rows], [None, "o2", "o2"])

    def test_empty_calendar(self):
        self.assertEqual(parse_pickups("<html></html>"), [])

    def test_name_resolution_prefers_exact_match(self):
        self.assertEqual(select_id({"1": "Perg", "2": "Pergkirchen"}, " Perg ", "city"), "1")

    def test_name_resolution_rejects_ambiguous_or_missing_names(self):
        for query in ("Per", "Unknown"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                select_id({"1": "Perg", "2": "Pergkirchen"}, query, "city")
