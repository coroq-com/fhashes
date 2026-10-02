"""ストレージとのやり取りのテスト: 一覧、必要な記録だけのダウンロード、キャッシュ（記録と検証の結果）。"""

import os
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402
from helpers import t  # noqa: E402
from test_analysis import AnalysisTestCase  # noqa: E402

from fhashes import snapshot  # noqa: E402
from fhashes.review import storage  # noqa: E402


class StorageTest(AnalysisTestCase):
    def test_other_hosts_and_subdirectories_are_ignored(self):
        path = self.make_snapshot(1, t(1), [("/app/a", "h1")])
        self.make_snapshot(1, t(1), [("/app/a", "h1")], host="db1")     # 同じ場所にほかのホスト
        os.makedirs(os.path.join(self.storage, "old"))
        shutil.copy(path, os.path.join(self.storage, "old", "web1-20260801T000000Z-00000009.ndjson.gz"))
        helpers.write_file(self.storage, "README.txt", "x")
        entries = storage.list_snapshots(self.conf, "web1")
        self.assertEqual([(e["host"], e["seq"]) for e in entries], [("web1", 1)])

    def test_select_range_downloads_only_what_is_needed(self):
        for hour in range(1, 11):
            self.make_snapshot(hour, t(hour), [("/app/a", "h%d" % hour)])
        result = self.analyze(since=t(5, 30), until=t(6, 30))
        # 5:30 以前に始まった最後（5 時）の 1 つ前（4 時）から、6:30 以後に始まった最初（7 時）まで
        self.assertEqual([s["seq"] for s in result["snapshots"]], [4, 5, 6, 7])
        cached = []
        for root, dirs, files in os.walk(self.conf["cache_dir"]):
            cached.extend(name for name in files if name.endswith(snapshot.FILE_SUFFIX))
        self.assertEqual(len(cached), 4)

    def test_verification_result_is_reused(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        self.make_snapshot(2, t(2), [("/app/a", "h2")])
        first = self.analyze()
        # 2 回目は、保存した検証の結果を使う（展開しない）
        with mock.patch.object(snapshot, "read_summary", side_effect=AssertionError("展開した")):
            second = self.analyze()
        self.assertEqual([s["sha256"] for s in second["snapshots"]], [s["sha256"] for s in first["snapshots"]])
        self.assertEqual(len(second["changes"]), 1)

    def test_verification_result_is_not_used_when_file_changed(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        self.analyze()
        entry = storage.list_snapshots(self.conf, "web1")[0]
        local = storage.cache_path(self.conf, entry)
        os.utime(local, ns=(0, 12345))  # キャッシュのファイルが変わった（更新時刻が違う）
        with mock.patch.object(snapshot, "read_summary", wraps=snapshot.read_summary) as read_summary:
            self.analyze()
        self.assertEqual(read_summary.call_count, 1)   # 検証し直した
        # 壊れた結果のファイルは無視して、検証し直す
        with open(storage.summary_path(local), "w") as f:
            f.write("{broken")
        with mock.patch.object(snapshot, "read_summary", wraps=snapshot.read_summary) as read_summary:
            self.analyze()
        self.assertEqual(read_summary.call_count, 1)

    def test_invalid_result_is_also_reused(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        path = self.make_snapshot(2, t(2), [("/app/a", "h2")])
        with open(path, "rb") as f:
            data = f.read()
        with open(path, "wb") as f:
            f.write(data[: len(data) // 2])  # 途中で切れた記録
        problem = self.analyze()["snapshots"][1]["problem"]
        self.assertIsNotNone(problem)
        with mock.patch.object(snapshot, "read_summary", side_effect=AssertionError("展開した")):
            self.assertEqual(self.analyze()["snapshots"][1]["problem"], problem)

    def test_cache_is_reused_and_cleaned(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        self.analyze()
        entry = storage.list_snapshots(self.conf, "web1")[0]
        local = storage.cache_path(self.conf, entry)
        os.utime(local, (0, 0))
        self.analyze()
        self.assertEqual(os.path.getmtime(local), 0)  # 取り直していない
        code, out = self.cli("clean")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.conf["cache_dir"]))


if __name__ == "__main__":
    unittest.main()
