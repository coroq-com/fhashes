"""監視の状況（monitoring.report など）のテスト。記録の情報は作り物で、ダウンロードはしない。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402
from helpers import t  # noqa: E402

from fhashes.review import monitoring  # noqa: E402


def record(hour: int, chain="ok", errors=0, problem=None, scope_changed=False, missing=0, name=None) -> dict:
    """analysis.load_snapshot の結果の形の、作り物の記録（hour 時に始まり、1 分後に終わる）。"""
    if problem is not None:
        return {"name": name or "web1-%02d" % hour, "problem": problem}
    return {"name": name or "web1-%02d" % hour, "problem": None, "chain": chain, "errors": errors,
            "scope_changed": scope_changed, "missing": missing,
            "started_at": t(hour), "finished_at": t(hour, 1)}


def report(records: list, since=None, until=None, still_error=()) -> tuple:
    with helpers.TimeZone("UTC"):
        return monitoring.report({"snapshots": records, "still_error": list(still_error)}, since, until)


class ReportTest(unittest.TestCase):
    def test_no_problems(self):
        lines, problems = report([record(1, chain="first"), record(2), record(3)], since=t(1, 30), until=t(2, 30))
        self.assertEqual(problems, [])
        self.assertEqual(lines, [
            "期間の直前の記録: 2026-09-01 01:00",
            "期間の直後の記録: 2026-09-01 03:00",
            "記録の件数: 1",                           # 期間の中に始まったものだけ（直前・直後は数えない）
            "記録の間隔: 中央値 60 分、最大値 60 分（2026-09-01 01:00 - 02:00）",   # 間隔は直前・直後も含めて
            "記録の欠落: なし",
            "記録の不一致: なし",
            "記録のやり直し: なし",
            "不正な記録: なし",
            "監視範囲の変更: なし",
            "読み取りエラー: なし",                     # 問題のない項目も 1 行ずつ出す
        ])

    def test_open_ends_are_not_problems(self):
        lines, problems = report([record(1, chain="first"), record(2)])
        self.assertEqual(problems, [])
        self.assertEqual(lines[:2], ["最初の記録: 2026-09-01 01:00", "最新の記録: 2026-09-01 02:00"])

    def test_missing_ends_are_problems(self):
        lines, problems = report([record(5, chain="first")], since=t(4), until=t(6))
        self.assertIn("期間の直前の記録: なし", lines)
        self.assertIn("期間の直後の記録: なし", lines)
        self.assertEqual(problems, ["期間の直前の記録なし", "期間の直後の記録なし"])

    def test_interval_is_reported_not_judged(self):
        lines, problems = report([record(1, chain="first"), record(2), record(3), record(7)])
        self.assertIn("記録の件数: 4", lines)
        self.assertIn("記録の間隔: 中央値 60 分、最大値 4.0 時間（2026-09-01 03:00 - 07:00）", lines)
        self.assertEqual(problems, [])
        lines, _ = report([record(1, chain="first")])
        self.assertFalse(any(line.startswith("記録の間隔") for line in lines))   # 2 件未満なら出さない

    def test_chain_problems_are_shown_as_what_happened(self):
        records = [record(1, chain="first"), record(4, chain="gap", missing=2), record(5, chain="broken"),
                   record(6, chain="restart")]
        lines, problems = report(records)
        self.assertIn("記録の欠落: 2 件（2026-09-01 01:00 - 04:00）", lines)
        self.assertIn("記録の不一致: 2026-09-01 05:00", lines)
        self.assertIn("記録のやり直し: 2026-09-01 06:00", lines)
        self.assertEqual(problems, ["記録の欠落", "記録の不一致", "記録のやり直し"])

    def test_invalid_record(self):
        lines, problems = report([record(1, chain="first"), record(2, problem="行が足りません", name="web1-x.gz")])
        self.assertIn("不正な記録: web1-x.gz: 行が足りません", lines)
        self.assertEqual(problems, ["不正な記録"])

    def test_scope_change_is_only_a_note(self):
        lines, problems = report([record(1, chain="first"), record(2, scope_changed=True)])
        self.assertIn("監視範囲の変更: 2026-09-01 02:00", lines)
        self.assertEqual(problems, [])

    def test_read_errors_are_problems(self):
        still_error = [{"path": "/app/a", "changed_after": t(1), "changed_before": t(2, 1)}]
        lines, problems = report([record(1, chain="first"), record(2, errors=3), record(3, errors=1)],
                                 still_error=still_error)
        self.assertIn("読み取りエラー: 記録 2 件、最大 3 ファイル（2026-09-01 02:00）", lines)
        self.assertIn("期間の終わりでもエラーのまま: 1 ファイル", lines)
        self.assertEqual(problems, ["読み取りエラー"])

    def test_no_valid_record(self):
        lines, problems = report([record(1, problem="gzip として読めません")])
        self.assertIn("有効な記録: なし", lines)
        self.assertIn("有効な記録なし", problems)


class HelperTest(unittest.TestCase):
    def test_latest_problem(self):
        self.assertIsNone(monitoring.latest_problem(record(1)))
        self.assertEqual(monitoring.latest_problem(record(1, problem="x")), "最新の記録が不正")
        self.assertEqual(monitoring.latest_problem(record(1, chain="broken")), "記録の不一致")
        self.assertEqual(monitoring.latest_problem(record(1, errors=2)), "読み取りエラー")

    def test_duration_text(self):
        self.assertEqual(monitoring.duration_text(45), "45 秒")
        self.assertEqual(monitoring.duration_text(3600), "60 分")
        self.assertEqual(monitoring.duration_text(4 * 3600), "4.0 時間")
        self.assertEqual(monitoring.duration_text(3 * 86400), "3.0 日")

    def test_interval_stats(self):
        self.assertIsNone(monitoring.interval_stats([t(1)]))
        median, longest = monitoring.interval_stats([t(1), t(2), t(3), t(7)])
        self.assertEqual((median, longest), (3600, (t(3), t(7))))


if __name__ == "__main__":
    unittest.main()
