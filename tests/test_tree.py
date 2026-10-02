"""場所の木、印ごとの件数の表、印の絞り込み（tree.py。log と diff で共通）のテスト。"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402
from helpers import t  # noqa: E402

from fhashes.review import tree  # noqa: E402

MARKS = (("A", "追加"), ("D", "削除"), ("M", "変更"), ("T", "種別変更"), ("=", "変化後復元"), ("~", "追加後削除"),
         ("?", "状態不明"))


def items(*pairs) -> list:
    return [{"path": path, "mark": mark} for path, mark in pairs]


def printed(lines: list) -> list:
    out = io.StringIO()
    with redirect_stdout(out):
        tree.print_lines(lines)
    return out.getvalue().rstrip("\n").split("\n")


class RenderTest(unittest.TestCase):
    def test_tree(self):
        lines = printed(tree.render(items(("/app/index.php", "M"), ("/app/up/2024/05/x.php", "A"),
                                          ("/app/up/2026/09/y.php", "~"), ("/app/inc/a.php", "M"),
                                          ("/app/inc/b.php", "D")), []))
        self.assertEqual(lines, [
            "/app/",                    # 共通のディレクトリが根（起点がないとき）。根は字下げしない
            "    inc/",                 # 名前の順（ディレクトリとファイルを混ぜる）
            "        M a.php",
            "        D b.php",
            "    M index.php",
            "    up/",
            "        A 2024/05/x.php",  # 子が 1 つのディレクトリはつなげる
            "        ~ 2026/09/y.php",
        ])

    def test_roots_are_include_directories(self):
        roots = [{"path": "/etc/nginx", "file_only": False}, {"path": "/etc/passwd", "file_only": True},
                 {"path": "/var/www/siteA", "file_only": False}]
        lines = printed(tree.render(items(("/var/www/siteA/wp-content/uploads/2026/09/x.php", "A"),
                                          ("/etc/passwd", "M"), ("/etc/nginx/nginx.conf", "M"),
                                          ("/opt/old/y.php", "D")), roots))
        self.assertEqual(lines, [
            "/etc/nginx/",
            "    M nginx.conf",
            "M /etc/passwd",                                # 1 つのファイルを指す起点はフルパス 1 行
            "/var/www/siteA/",                              # 深いところに 1 件だけでも、根は起点のディレクトリ
            "    A wp-content/uploads/2026/09/x.php",
            "/opt/old/",                                    # 起点の外は、共通のディレクトリを根にして最後に
            "    D y.php",
        ])

    def test_same_period_is_written_once_on_the_upper_line(self):
        period_of = {"/app/inc/a.php": "P1", "/app/inc/b.php": "P1", "/app/x.php": "P2"}
        lines = printed(tree.render(items(("/app/inc/a.php", "M"), ("/app/inc/b.php", "M"), ("/app/x.php", "A")),
                                    [], lambda item: period_of[item["path"]]))
        self.assertEqual([line.split()[-1] if line.endswith(("P1", "P2")) else "" for line in lines],
                         ["", "P1", "", "", "P2"])          # inc/ の行に 1 回、x.php の行に 1 回
        lines = printed(tree.render(items(("/app/a", "M"), ("/app/b", "M")), [], lambda item: "P1"))
        self.assertTrue(lines[0].startswith("/app/ ") and lines[0].endswith("P1"))   # 全部同じなら根の行に
        self.assertEqual(lines[1:], ["    M a", "    M b"])


class LegendTest(unittest.TestCase):
    def test_legend_layout(self):
        out = io.StringIO()
        with redirect_stdout(out):
            tree.print_legend(items(("/a", "A"), ("/b", "M"), ("/c", "M"), ("/d", "~")), MARKS,
                              (("A", "D", "M", "T"), ("=", "~", "?")))
        # 列の位置は上下の段でそろえる。0 件の印も出す
        self.assertEqual(out.getvalue().split("\n")[:2], [
            "A  追加        1    D  削除        0    M  変更      2    T  種別変更  0",
            "=  変化後復元  0    ~  追加後削除  1    ?  状態不明  0",
        ])
        self.assertEqual(helpers.legend_counts(out.getvalue()),
                         {"A": 1, "D": 0, "M": 2, "T": 0, "=": 0, "~": 1, "?": 0})


class MarkFilterTest(unittest.TestCase):
    def test_mark_filter(self):
        everything = {mark for mark, _ in MARKS}
        self.assertEqual(tree.mark_filter(None, None, MARKS), everything)
        self.assertEqual(tree.mark_filter(["AM"], None, MARKS), {"A", "M"})
        self.assertEqual(tree.mark_filter(["A", "M"], None, MARKS), {"A", "M"})      # 何度も指定できる
        self.assertEqual(tree.mark_filter(["=~?"], None, MARKS), {"=", "~", "?"})
        self.assertEqual(tree.mark_filter(None, ["D"], MARKS), everything - {"D"})   # --not-type で除く
        self.assertEqual(tree.mark_filter(["AM"], ["M"], MARKS), {"A"})             # 絞ってから除く
        for bad in ("X", "a", "A M"):
            with self.assertRaises(ValueError, msg=bad):
                tree.mark_filter([bad], None, MARKS)
            with self.assertRaises(ValueError, msg=bad):
                tree.mark_filter(None, [bad], MARKS)
        with self.assertRaises(ValueError):
            tree.mark_filter(["="], None, MARKS[:4])                                # そのコマンドにない印


if __name__ == "__main__":
    unittest.main()
