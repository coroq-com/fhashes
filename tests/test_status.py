"""status（ホストごとの最新の記録と異常）のテスト。"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import StorageTestCase, t  # noqa: E402

from fhashes import timeutil  # noqa: E402
from fhashes.review import status  # noqa: E402


def ago(hours: float, minutes: float = 0) -> str:
    """今から hours 時間 minutes 分前（UTC の文字列）。記録の遅れが出ないように、今に近い時刻で記録を作る。"""
    return timeutil.to_utc_text(datetime.now(timezone.utc) - timedelta(hours=hours, minutes=minutes))


def cut_in_half(path: str) -> None:
    """記録のファイルを途中で切る（不正な記録にする）。"""
    with open(path, "rb") as f:
        data = f.read()
    with open(path, "wb") as f:
        f.write(data[: len(data) // 2])


class RecordDelayTest(unittest.TestCase):
    def test_record_delay(self):
        latest = {"finished_at": "2026-09-01T01:08:00Z"}      # 01:00 に始まり、走査に 8 分かかった
        # 次は 02:08 ごろに届くはず（走査の時間は遅れに数えない）
        self.assertEqual(status.record_delay(latest, "2026-09-01T01:00:00Z", 3600, "2026-09-01T02:10:00Z"), 120)
        self.assertEqual(status.record_delay(latest, "2026-09-01T01:00:00Z", 3600, "2026-09-01T02:00:00Z"), -480)
        # 最新の記録が不正で終了時刻が分からなければ、ファイル名の時刻（開始）から
        self.assertEqual(status.record_delay({}, "2026-09-01T01:00:00Z", 3600, "2026-09-01T02:10:00Z"), 600)


class StatusTest(StorageTestCase):
    def test_host_without_records(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        code, out = self.cli("status")
        self.assertRegex(out, r"db1 +- +- +- +- +- +記録なし")
        self.assertEqual(code, 1)   # db1 に 1 つも届いていない

    def test_intervals_are_reported_not_judged(self):
        # 直近の記録の間隔（中央値と最大値）。間隔の長さは判定しないので、空きがあっても終了コードは 0
        for host in ("web1", "db1"):
            for seq, hours_ago in enumerate([7, 6, 5, 1, 0], 1):
                self.make_snapshot(seq, ago(hours_ago), [("/app/a", "h1")], host=host)
        code, out = self.cli("status")
        self.assertIn("間隔の中央値  間隔の最大値", out)
        self.assertRegex(out, r"db1 +\S+ \S+ +60 分 +4\.0 時間 +1 +0 +OK")
        self.assertEqual(code, 0)

    def test_record_delay(self):
        # 中央値 60 分。最新の記録（終了は 1 分後）から 60 分 + 3 分なら遅れなし、60 分 + 7 分なら遅れ
        for seq, minutes_ago in enumerate([184, 124, 64], 1):
            self.make_snapshot(seq, ago(0, minutes_ago), [("/app/a", "h1")], host="db1")
        for seq, minutes_ago in enumerate([188, 128, 68], 1):
            self.make_snapshot(seq, ago(0, minutes_ago), [("/app/a", "h1")])
        code, out = self.cli("status")
        self.assertRegex(out, r"db1 .* OK\n")
        self.assertRegex(out, r"web1 .* 記録の遅れ（[67] 分）\n")
        self.assertEqual(code, 1)

    def test_delay_is_shown_with_other_problems(self):
        # 止まっているホストでも、最新の記録の問題も並べて出す
        self.make_snapshot(1, ago(0, 5), [("/app/a", "h1")], host="db1")
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        self.make_snapshot(2, t(2), [("/app/a", "h2")], prev_sha="0" * 64)          # 記録の不一致
        code, out = self.cli("status")
        self.assertRegex(out, r"web1 .* 記録の遅れ（[0-9.]+ 日）、記録の不一致\n")
        self.assertEqual(code, 1)

    def test_latest_record_problems(self):
        self.make_snapshot(1, ago(0, 5), [("/app/a", "h1")], host="db1")
        self.make_snapshot(1, ago(1, 5), [("/app/a", "h1")])
        self.make_snapshot(2, ago(0, 5), [("/app/a", "h2")], prev_sha="0" * 64)     # 記録の不一致
        code, out = self.cli("status")
        self.assertRegex(out, r"web1 .* 記録の不一致\n")
        self.assertEqual(code, 1)

    def test_invalid_latest_record(self):
        # 最新の記録が不正（途中で切れているなど）でも止まらず、件数の欄は「-」にする
        self.make_snapshot(1, ago(0, 5), [("/app/a", "h1")], host="db1")
        self.make_snapshot(1, ago(1, 5), [("/app/a", "h1")])
        cut_in_half(self.make_snapshot(2, ago(0, 5), [("/app/a", "h2")]))
        code, out = self.cli("status")
        self.assertRegex(out, r"web1 +\S+ \S+ +60 分 +60 分 +- +- +最新の記録が不正\n")
        self.assertEqual(code, 1)

    def test_invalid_previous_record(self):
        # 直前の記録が不正なら、最新の記録のつながりを確かめられないので異常とする
        self.make_snapshot(1, ago(0, 5), [("/app/a", "h1")], host="db1")
        self.make_snapshot(1, ago(2, 5), [("/app/a", "h1")])
        path = self.make_snapshot(2, ago(1, 5), [("/app/a", "h2")])
        self.make_snapshot(3, ago(0, 5), [("/app/a", "h2")])
        cut_in_half(path)
        code, out = self.cli("status")
        self.assertRegex(out, r"web1 .* 直前の記録が不正\n")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
