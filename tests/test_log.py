"""log（期間内の変化の履歴）のテスト。"""

import io
import json
import logging
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402
from helpers import StorageTestCase, t  # noqa: E402

from fhashes import cli  # noqa: E402
from fhashes.review import log  # noqa: E402


def change(kind, path="/app/a", old="h1", new="h2", old_kind="file", new_kind="file", after=None, before=None):
    return {"type": kind, "path": path, "old_hash": old, "new_hash": new, "old_kind": old_kind, "new_kind": new_kind,
            "changed_after": after or t(1), "changed_before": before or t(2, 1)}


class ChangeMarkTest(unittest.TestCase):
    def test_change_mark(self):
        self.assertEqual(log.change_mark(change("added", old=None, old_kind=None)), "A")
        self.assertEqual(log.change_mark(change("added", old=None, new=None, old_kind=None)), "?")   # 追加されたが読めない
        self.assertEqual(log.change_mark(change("deleted", new=None, new_kind=None)), "D")
        self.assertEqual(log.change_mark(change("modified")), "M")
        self.assertEqual(log.change_mark(change("modified", new_kind="link")), "T")
        self.assertEqual(log.change_mark(change("modified", old=None)), "?")       # 前の中身が分からない


class HistoryTest(unittest.TestCase):
    def history(self, changes) -> str:
        out = io.StringIO()
        with helpers.TimeZone("UTC"), redirect_stdout(out):
            log.print_history(changes, [{"path": "/app", "file_only": False}])
        return out.getvalue()

    def test_grouped_by_period(self):
        out = self.history([
            change("modified", "/app/b", before=t(2, 1)),
            change("added", "/app/c", old=None, old_kind=None, before=t(2, 1)),
            change("modified", "/app/a", after=t(0), before=t(2, 1)),   # 読み取りエラーの間に変わった（始まりが早い）
            change("deleted", "/app/d", new=None, new_kind=None, after=t(2), before=t(3, 1)),
        ])
        self.assertTrue(out.startswith(
            "2026-09-01 00:00 - 02:01\n/app/\n    M a\n\n"           # 始まりと終わりの組み合わせごとに別のまとまり
            "2026-09-01 01:00 - 02:01\n/app/\n    M b\n    A c\n\n"
            "2026-09-01 02:00 - 03:01\n/app/\n    D d\n\n"), out)
        self.assertEqual(helpers.legend_counts(out), {"A": 1, "D": 1, "M": 2, "T": 0, "?": 0})

    def test_nothing(self):
        out = self.history([])
        self.assertTrue(out.startswith("変化なし\n"))
        self.assertEqual(set(helpers.legend_counts(out).values()), {0})


class LogCommandTest(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.make_snapshot(1, t(1), [("/app/a", "h1"), ("/app/b", "h1")])
        self.make_snapshot(2, t(2), [("/app/a", "h2"), ("/app/b", "h1")])
        self.make_snapshot(3, t(3), [("/app/a", "h2"), ("/app/b", "h1"), ("/app/c", "h1")])

    def test_table(self):
        code, out = self.cli("log", "web1")
        self.assertEqual(code, 0, out)
        self.assertTrue(out.startswith("ホスト  : web1\n調査期間: 最初の記録 - 最新の記録\n\n[変化の履歴]\n"), out)
        self.assertIn("2026-09-01 01:00 - 02:01\n/app/\n    M a\n\n2026-09-01 02:00 - 03:01\n/app/\n    A c\n\n", out)
        # 変化の履歴の後に監視の状況を出す（変化が多くても、監視の状況が最後に目に入るように）
        self.assertLess(out.index("[変化の履歴]"), out.index("[監視の状況]"))

    def test_filters(self):
        code, out = self.cli("log", "web1", "--type", "A")
        self.assertNotIn("M a", out)
        self.assertIn("A c", out)
        code, out = self.cli("log", "web1", "--not-type", "A", "--not-path", "/app/c")
        self.assertIn("M a", out)
        self.assertNotIn(" c\n", out)
        code, out = self.cli("log", "web1", "--type", "=")      # log に = はない
        self.assertEqual(code, 2)

    def test_csv_and_json(self):
        with helpers.TimeZone("Asia/Tokyo"):
            code, out = self.cli("log", "web1", "--format", "csv")
        lines = out.strip().split("\r\n")
        self.assertEqual(lines[0], ",".join(log.CSV_COLUMNS))
        self.assertEqual(lines[1], "web1,/app/a,modified,2026-09-01T01:00:00Z,2026-09-01T02:01:00Z,file,file,h1,h2")  # UTC
        code, out = self.cli("log", "web1", "--format", "json", "--type", "A")
        self.assertEqual([item["path"] for item in json.loads(out)], ["/app/c"])

    def test_problems_set_exit_code_even_in_csv(self):
        self.make_snapshot(5, t(8), [("/app/a", "h2")], prev_sha="0" * 64)
        code, out = self.cli("log", "web1", "--format", "csv")
        self.assertEqual(code, 1)

    def test_time_zone_is_only_for_humans(self):
        with helpers.TimeZone("Asia/Tokyo"):
            # タイムゾーンを書かない入力は JST として解釈する（01:30〜01:40 UTC = 10:30〜10:40 JST）
            code, out = self.cli("log", "web1", "--from", "2026-09-01 10:30", "--to", "2026-09-01 10:40")
            self.assertIn("2026-09-01 10:00 - 11:01\n/app/\n    M a\n", out)   # 表は JST
            code, out = self.cli("log", "web1", "--from", "2026-09-01T01:30Z", "--to", "2026-09-01T01:40Z")
            self.assertIn("M a", out)

    def test_quiet_when_not_a_terminal(self):
        # 端末でないとき（パイプ、cron）は、標準エラー出力に何も書かず、INFO のログも出さない
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger("fhashes")
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.INFO)   # INFO のログがあれば、必ず捕まえられるように
        try:
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = cli.main(["log", "--config", self.config_path, "web1"])
                cli.main(["status", "--config", self.config_path])
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual([r for r in records if r.levelno >= logging.INFO], [])

    def test_unknown_host_and_no_records(self):
        self.assertEqual(self.cli("log", "nohost")[0], 2)    # 設定の hosts にない
        self.assertEqual(self.cli("log", "db1")[0], 1)       # 設定にはあるが、記録がまだない


if __name__ == "__main__":
    unittest.main()
