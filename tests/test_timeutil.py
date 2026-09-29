"""日時の変換（fhashes/timeutil.py）のテスト。"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402

from fhashes import timeutil  # noqa: E402


class ParseTest(unittest.TestCase):
    def test_parse_iso(self):
        jst = timezone(timedelta(hours=9))
        cases = {
            "2026-09-23": datetime(2026, 9, 23),
            "2026-09-23 10:00": datetime(2026, 9, 23, 10, 0),
            "2026-09-23T10:00:05": datetime(2026, 9, 23, 10, 0, 5),
            "2026-09-23T10:00:05.25": datetime(2026, 9, 23, 10, 0, 5, 250000),
            "2026-09-23T10:00+09:00": datetime(2026, 9, 23, 10, 0, tzinfo=jst),
            "2026-09-23T10:00+0900": datetime(2026, 9, 23, 10, 0, tzinfo=jst),
            "2026-09-23T01:00Z": datetime(2026, 9, 23, 1, 0, tzinfo=timezone.utc),
            "2026-09-23T01:00-01:30": datetime(2026, 9, 23, 1, 0, tzinfo=timezone(timedelta(hours=-1, minutes=-30))),
        }
        for text, expected in cases.items():
            self.assertEqual(timeutil.parse_iso(text), expected, text)

    def test_parse_iso_rejects_other_forms(self):
        for text in ["2026-9-23", "2026-09-23T", "2026-02-30", "2026-09-23+09:00", "10:00", "x"]:
            with self.assertRaises(ValueError, msg=text):
                timeutil.parse_iso(text)

    def test_parse_user_time(self):
        with helpers.TimeZone("Asia/Tokyo"):
            self.assertEqual(timeutil.parse_user_time("2026-09-23"), "2026-09-22T15:00:00Z")
            self.assertEqual(timeutil.parse_user_time("2026-09-23", end_of_range=True), "2026-09-23T15:00:00Z")
            self.assertEqual(timeutil.parse_user_time("2026-09-23 10:00"), "2026-09-23T01:00:00Z")
            self.assertEqual(timeutil.parse_user_time("2026-09-23T01:00Z"), "2026-09-23T01:00:00Z")
        with self.assertRaises(ValueError):
            timeutil.parse_user_time("2026/09/23")


if __name__ == "__main__":
    unittest.main()
