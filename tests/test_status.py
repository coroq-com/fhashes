"""status（ホストごとの最新の記録と異常）のテスト。"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import StorageTestCase, t  # noqa: E402

from fhashes import timeutil  # noqa: E402


class StatusTest(StorageTestCase):
    def test_host_without_records(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        code, out = self.cli("status")
        self.assertRegex(out, r"db1 +- +- +- +- +- +記録なし")
        self.assertEqual(code, 1)   # db1 に 1 つも届いていない

    def test_intervals_are_reported_not_judged(self):
        # 直近 24 時間の記録の間隔（中央値と最大値）。判定はしないので、空きがあっても終了コードは 0
        now = datetime.now(timezone.utc)
        for host in ("web1", "db1"):
            for seq, hours_ago in enumerate([10, 9, 8, 4, 3], 1):
                self.make_snapshot(seq, timeutil.to_utc_text(now - timedelta(hours=hours_ago)), [("/app/a", "h1")],
                                   host=host)
        code, out = self.cli("status")
        self.assertIn("間隔の中央値  間隔の最大値", out)
        self.assertRegex(out, r"db1 +\S+ \S+ +60 分 +4\.0 時間 +1 +0 +OK")
        self.assertEqual(code, 0)

    def test_latest_record_problems(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")], host="db1")
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        self.make_snapshot(2, t(2), [("/app/a", "h2")], prev_sha="0" * 64)          # 記録の不一致
        code, out = self.cli("status")
        self.assertRegex(out, r"web1 .*記録の不一致")
        self.assertEqual(code, 1)

    def test_invalid_latest_record(self):
        # 最新の記録が不正（途中で切れているなど）でも止まらず、件数の欄は「-」にする
        self.make_snapshot(1, t(1), [("/app/a", "h1")], host="db1")
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        path = self.make_snapshot(2, t(2), [("/app/a", "h2")])
        with open(path, "rb") as f:
            data = f.read()
        with open(path, "wb") as f:
            f.write(data[: len(data) // 2])
        code, out = self.cli("status")
        self.assertRegex(out, r"web1 +2026-09-01 02:00 +- +- +- +- +最新の記録が不正")
        self.assertEqual(code, 1)

    def test_invalid_previous_record(self):
        # 直前の記録が不正なら、最新の記録のつながりを確かめられないので異常とする
        self.make_snapshot(1, t(1), [("/app/a", "h1")], host="db1")
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        path = self.make_snapshot(2, t(2), [("/app/a", "h2")])
        self.make_snapshot(3, t(3), [("/app/a", "h2")])
        with open(path, "rb") as f:
            data = f.read()
        with open(path, "wb") as f:
            f.write(data[: len(data) // 2])
        code, out = self.cli("status")
        self.assertRegex(out, r"web1 +2026-09-01 03:00 .*直前の記録が不正")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
