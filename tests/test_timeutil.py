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

    def test_format_local_range(self):
        with helpers.TimeZone("UTC"):
            # 同じ日なら終わりの日付を省く。分まで
            self.assertEqual(timeutil.format_local_range("2026-09-01T01:00:00Z", "2026-09-01T02:00:41Z"),
                             "2026-09-01 01:00 - 02:00")
            self.assertEqual(timeutil.format_local_range("2026-09-01T01:00:00Z", "2026-09-03T02:00:00Z"),
                             "2026-09-01 01:00 - 2026-09-03 02:00")
        with helpers.TimeZone("Asia/Tokyo"):
            # 日付の比較も実行環境のタイムゾーンで（UTC では同じ日でも、JST では日をまたぐ）
            self.assertEqual(timeutil.format_local_range("2026-09-01T14:00:00Z", "2026-09-01T16:00:00Z"),
                             "2026-09-01 23:00 - 2026-09-02 01:00")

    def test_relative_time(self):
        now = datetime(2026, 9, 30, 12, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(timeutil.parse_user_time("4d", now=now), "2026-09-26T12:00:30Z")   # ちょうど 96 時間前
        self.assertEqual(timeutil.parse_user_time("12h", now=now), "2026-09-30T00:00:30Z")
        self.assertEqual(timeutil.parse_user_time("30m", now=now), "2026-09-30T11:30:30Z")
        self.assertEqual(timeutil.parse_user_time("2w", now=now), "2026-09-16T12:00:30Z")
        self.assertEqual(timeutil.parse_user_time("4d", end_of_range=True, now=now), "2026-09-26T12:00:30Z")  # 日の終わりにはしない
        for bad in ("-4d", "4", "4y", "d", "4 d"):
            with self.assertRaises(ValueError, msg=bad):
                timeutil.parse_user_time(bad, now=now)


if __name__ == "__main__":
    unittest.main()
