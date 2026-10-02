"""diff（期間の最初と最後の状態の差）のテスト。木と件数の表の形は test_tree.py で確かめる。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402
from helpers import StorageTestCase, t  # noqa: E402

from fhashes.review import diff  # noqa: E402


def change(kind, path, old, new, after, before, old_kind="file", new_kind="file"):
    return {"type": kind, "path": path, "old_hash": old, "new_hash": new,
            "old_kind": None if kind == "added" else old_kind, "new_kind": None if kind == "deleted" else new_kind,
            "changed_after": after, "changed_before": before}


class NetChangeTest(unittest.TestCase):
    """ファイルごとの印（ダウンロードしない単体テスト）。"""

    def marks(self, changes, still_error=()):
        return {item["path"]: item["mark"] for item in diff.net_changes(changes, list(still_error))}

    def test_marks(self):
        changes = [
            change("added", "/a", None, "h1", t(1), t(2)),
            change("deleted", "/d", "h1", None, t(1), t(2)),
            change("modified", "/m", "h1", "h2", t(1), t(2)),
            change("modified", "/m", "h2", "h3", t(3), t(4)),
            change("modified", "/t", "h1", "h2", t(1), t(2), new_kind="link"),
            change("modified", "/r", "h1", "h9", t(1), t(2)),
            change("modified", "/r", "h9", "h1", t(3), t(4)),              # 元に戻った
            change("deleted", "/r2", "h1", None, t(1), t(2)),
            change("added", "/r2", None, "h1", t(3), t(4)),                # 消して同じ中身で作り直した
            change("added", "/x", None, "h1", t(1), t(2)),
            change("deleted", "/x", "h1", None, t(3), t(4)),               # 置かれて消された
            change("added", "/u", None, None, t(1), t(2)),                 # 追加されたが読めない
            change("added", "/v", None, None, t(1), t(2)),
            change("modified", "/v", None, "h1", t(1), t(3)),              # 追加されたときは読めず、後で読めた
            change("modified", "/w", None, "h1", t(1), t(2)),              # 期間の初めから読めず、後で読めた
        ]
        self.assertEqual(self.marks(changes), {"/a": "A", "/d": "D", "/m": "M", "/t": "T", "/r": "=", "/r2": "=",
                                               "/x": "~", "/u": "?", "/v": "A", "/w": "?"})

    def test_still_unreadable_at_end_is_unknown(self):
        changes = [change("modified", "/m", "h1", "h2", t(1), t(2))]
        still_error = [{"path": "/m", "changed_after": t(3), "changed_before": t(4)},
                       {"path": "/e", "changed_after": t(5), "changed_before": t(6)}]
        items = {item["path"]: item for item in diff.net_changes(changes, still_error)}
        self.assertEqual((items["/m"]["mark"], items["/m"]["count"]), ("?", 2))
        self.assertEqual((items["/e"]["mark"], items["/e"]["changed_after"]), ("?", t(5)))

    def test_period_is_first_to_last(self):
        changes = [change("modified", "/m", "h1", "h2", t(1), t(2)), change("modified", "/m", "h2", "h3", t(5), t(6))]
        item = diff.net_changes(changes, [])[0]
        self.assertEqual((item["changed_after"], item["changed_before"], item["count"]), (t(1), t(6), 2))


class DiffCommandTest(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.make_snapshot(1, t(1), [("/app/a", "h1"), ("/app/b", "h1"), ("/app/c", "h1")])
        self.make_snapshot(2, t(2), [("/app/a", "h2"), ("/app/c", "h1"), ("/app/d", "h1")])
        self.make_snapshot(3, t(3), [("/app/a", "h1"), ("/app/c", "h1"), ("/app/d", "h1")])

    def test_diff(self):
        code, out = self.cli("diff", "web1")
        self.assertEqual(code, 0, out)
        self.assertIn("[差分]\n/app/\n", out)
        self.assertIn("\n    = a    2026-09-01 01:00 - 03:01（2 回）\n", out)
        self.assertIn("\n    D b    2026-09-01 01:00 - 02:01\n", out)
        self.assertIn("\n    A d    2026-09-01 01:00 - 02:01\n", out)
        self.assertEqual(helpers.legend_counts(out), {"A": 1, "D": 1, "M": 0, "T": 0, "=": 1, "~": 0, "?": 0})
        self.assertLess(out.index("[差分]"), out.index("[監視の状況]"))

    def test_filters(self):
        code, out = self.cli("diff", "web1", "--type", "AD")
        self.assertNotIn("= a", out)
        self.assertEqual(helpers.legend_counts(out)["="], 0)        # 件数は表示したものの件数
        code, out = self.cli("diff", "web1", "--not-type", "=", "--not-path", "/app/d")
        self.assertEqual([line.strip() for line in out.split("[差分]\n")[1].split("\n\n")[0].split("\n")][1:],
                         ["D b"])
        code, out = self.cli("diff", "web1", "--type", "x")
        self.assertEqual(code, 2)

    def test_nothing(self):
        code, out = self.cli("diff", "web1", "--path", "/etc/**")
        self.assertIn("[差分]\n差分なし\n", out)


if __name__ == "__main__":
    unittest.main()
