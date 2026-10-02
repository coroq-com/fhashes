"""解析のテスト: 記録の選択、検証、隣どうしの比較（変化の見つけ方）。

記録は、時刻や内容を指定して直接作る（helpers.StorageTestCase）。
"""

import gzip
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import StorageTestCase, t  # noqa: E402

from fhashes.review import analysis, storage  # noqa: E402

ALL_TIME = ("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")


class AnalysisTestCase(StorageTestCase):
    def analyze(self, since=ALL_TIME[0], until=ALL_TIME[1], host="web1") -> dict:
        entries = storage.list_snapshots(self.conf, host)
        return analysis.analyze_host(self.conf, analysis.select_range(entries, since, until))

    def changes(self, **kwargs) -> list:
        return sorted((c["type"], c["path"]) for c in self.analyze(**kwargs)["changes"])

    def change(self, path: str) -> dict:
        for c in self.analyze()["changes"]:
            if c["path"] == path:
                return c
        return None

    def chains(self) -> list:
        return [s["chain"] for s in self.analyze()["snapshots"]]


class AnalysisTest(AnalysisTestCase):
    def test_added_modified_deleted_with_periods(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1"), ("/app/b", "h1"), ("/app/c", "h1")])
        self.make_snapshot(2, t(2), [("/app/a", "h2"), ("/app/c", "h1"), ("/app/d", "h1")])
        self.assertEqual(self.changes(), [("added", "/app/d"), ("deleted", "/app/b"), ("modified", "/app/a")])
        # 変更期間: 前のスナップショットの開始 〜 後のスナップショットの終了（make_snapshot は開始の 60 秒後に終わる）
        for path in ("/app/a", "/app/d", "/app/b"):
            ch = self.change(path)
            self.assertEqual((ch["changed_after"], ch["changed_before"]), (t(1), t(2, 1, 0)), path)
        self.assertEqual(self.chains(), ["first", "ok"])

    def test_identical_snapshots_have_no_changes(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        self.make_snapshot(2, t(2), [("/app/a", "h1")])
        self.assertEqual(self.changes(), [])

    def test_unreadable_file_keeps_last_known_state(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1"), ("/app/b", "h1")])
        self.make_snapshot(2, t(2), [("/app/a", None, "file"), ("/app/b", None, "file")])
        self.make_snapshot(3, t(3), [("/app/a", None, "file"), ("/app/b", None, "file")])
        self.make_snapshot(4, t(4), [("/app/a", "h2"), ("/app/b", "h1")])
        self.assertEqual(self.changes(), [("modified", "/app/a")])
        a = self.change("/app/a")
        self.assertEqual((a["changed_after"], a["changed_before"]), (t(1), t(4, 1, 0)))
        self.assertEqual(len(self.analyze()["still_error"]), 0)

    def test_unreadable_dir_children_are_not_deleted(self):
        self.make_snapshot(1, t(1), [("/app/d/a", "h1"), ("/app/d/b", "h1"), ("/app/dx", "h1")])
        self.make_snapshot(2, t(2), [("/app/d", None, "dir"), ("/app/dx", "h1")])
        self.make_snapshot(3, t(3), [("/app/d", None, "dir"), ("/app/dx", "h1")])
        self.assertEqual(self.changes(), [])
        self.assertEqual(len(self.analyze()["still_error"]), 2)
        # 読めるようになったら、b は消えていた
        self.make_snapshot(4, t(4), [("/app/d/a", "h1"), ("/app/dx", "h1")])
        self.assertEqual(self.changes(), [("deleted", "/app/d/b")])
        b = self.change("/app/d/b")
        self.assertEqual((b["changed_after"], b["changed_before"]), (t(1), t(4, 1, 0)))

    def test_unreadable_then_deleted(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1"), ("/app/x", "h1")])
        self.make_snapshot(2, t(2), [("/app/a", None, "file"), ("/app/x", "h1")])
        self.make_snapshot(3, t(3), [("/app/x", "h1")])
        self.assertEqual(self.changes(), [("deleted", "/app/a")])
        a = self.change("/app/a")
        self.assertEqual((a["changed_after"], a["old_hash"]), (t(1), "h1"))

    def test_unreadable_file_then_parent_dir_unreadable(self):
        self.make_snapshot(1, t(1), [("/app/d/a", "h1")])
        self.make_snapshot(2, t(2), [("/app/d/a", None, "file")])
        self.make_snapshot(3, t(3), [("/app/d", None, "dir")])
        self.assertEqual(self.changes(), [])
        self.assertEqual(len(self.analyze()["still_error"]), 1)
        self.make_snapshot(4, t(4), [("/app/d/a", "h1")])
        self.assertEqual(self.changes(), [])

    def test_new_unreadable_file_is_added(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        self.make_snapshot(2, t(2), [("/app/a", "h1"), ("/app/secret", None, "file")])
        self.assertEqual(self.changes(), [("added", "/app/secret")])
        self.assertIsNone(self.change("/app/secret")["new_hash"])
        # 読めるようになったら、前の中身が不明の modified にする（読めるようになった中身を見逃さない）
        self.make_snapshot(3, t(3), [("/app/a", "h1"), ("/app/secret", "h9")])
        self.assertEqual(self.changes(), [("added", "/app/secret"), ("modified", "/app/secret")])
        modified = self.analyze()["changes"][-1]
        self.assertEqual((modified["old_hash"], modified["new_hash"], modified["changed_after"]), (None, "h9", t(2)))

    def test_scope_change_is_not_counted_as_changes(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1"), ("/app/cache/x", "h1")])
        narrow = {"include": ["/app/**"], "exclude": ["/app/cache/**"]}
        self.make_snapshot(2, t(2), [("/app/a", "h1")], scope=narrow)
        wide = {"include": ["/app/**", "/etc/**"], "exclude": ["/app/cache/**"]}
        self.make_snapshot(3, t(3), [("/app/a", "h2"), ("/etc/x", "h1")], scope=wide)
        # 範囲に出入りしたものは数えないが、範囲内のファイルの変更は記録する
        self.assertEqual(self.changes(), [("modified", "/app/a")])
        self.assertEqual([s["scope_changed"] for s in self.analyze()["snapshots"]], [False, True, True])

    def test_chain_problems(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        self.make_snapshot(2, t(2), [("/app/a", "h1")], prev_sha="0" * 64)          # 書き換えの疑い
        self.make_snapshot(4, t(4), [("/app/a", "h1")], prev_sha="1" * 64)          # 3 が届いていない
        self.make_snapshot(1, t(5), [("/app/a", "h1")], prev_sha=None)              # 状態ファイルが失われた
        self.assertEqual(self.chains(), ["first", "broken", "gap", "restart"])

    def test_missing_snapshot_arrives_later(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        path2 = self.make_snapshot(2, t(2), [("/app/a", "h2")])
        self.make_snapshot(3, t(3), [("/app/a", "h3")])
        hidden = path2 + ".hidden"
        os.rename(path2, hidden)  # 2 はまだ届いていない
        self.assertEqual(self.chains(), ["first", "gap"])
        os.rename(hidden, path2)
        self.assertEqual(self.chains(), ["first", "ok", "ok"])
        self.assertEqual(len(self.analyze()["changes"]), 2)

    def test_truncated_snapshot_is_skipped(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        path2 = self.make_snapshot(2, t(2), [("/app/a", "h2")])
        self.make_snapshot(3, t(3), [("/app/a", "h3")])
        with open(path2, "rb") as f:
            data = f.read()
        with open(path2, "wb") as f:
            f.write(data[: len(data) // 2])
        snaps = self.analyze()["snapshots"]
        self.assertIsNotNone(snaps[1]["problem"])
        self.assertEqual(snaps[2]["chain"], "gap")
        a = self.change("/app/a")  # 1 と 3 を比べる
        self.assertEqual((a["old_hash"], a["new_hash"]), ("h1", "h3"))

    def test_tampered_content_is_detected(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        path2 = self.make_snapshot(2, t(2), [("/app/a", "h2")])
        with gzip.open(path2, "rb") as f:
            text = f.read().replace(b'"hash":"h2"', b'"hash":"h1"')
        with gzip.open(path2, "wb") as f:
            f.write(text)
        self.assertIn("中身が壊れています", self.analyze()["snapshots"][1]["problem"])
        self.assertEqual(self.changes(), [])

    def test_bad_header_value_is_reported_not_crashing(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        path2 = self.make_snapshot(2, t(2), [("/app/a", "h2")])
        with gzip.open(path2, "rb") as f:
            text = f.read().replace(b'"started_at":"2026-09-01T02:00:00Z"', b'"started_at":"x"')
        with gzip.open(path2, "wb") as f:
            f.write(text)
        self.make_snapshot(3, t(3), [("/app/a", "h3")])
        self.assertIn("started_at", self.analyze()["snapshots"][1]["problem"])

    def test_bad_row_is_reported_not_crashing(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        path2 = self.make_snapshot(2, t(2), [("/app/a", "h2")])
        with gzip.open(path2, "rb") as f:
            text = f.read().replace(b',"kind":"file"', b'')
        with gzip.open(path2, "wb") as f:
            f.write(text)
        self.assertIn("中身が壊れています", self.analyze()["snapshots"][1]["problem"])


class SelectRangeTest(unittest.TestCase):
    """期間に必要なスナップショットの選び方（ダウンロードしない単体テスト）。"""

    def test_edges(self):
        entries = [{"name_time": t(h)} for h in (1, 2, 3)]
        self.assertEqual(analysis.select_range(entries, t(0), t(0, 30)), entries[0:1])   # 最初より前
        self.assertEqual(analysis.select_range(entries, t(5), t(6)), entries[1:3])       # 最後より後
        self.assertEqual(analysis.select_range(entries, t(2), t(2, 30)), entries[0:3])   # 真ん中


if __name__ == "__main__":
    unittest.main()
