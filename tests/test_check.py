"""check（実際のファイルと記録の照らし合わせ）と、パスの対応（--map）のテスト。"""

import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402
from helpers import StorageTestCase, t  # noqa: E402

from fhashes.pathmap import OutsideMapError, PathMap  # noqa: E402


def sha(text: str) -> str:
    """中身（リンクならリンク先のパス文字列）のハッシュ。"""
    return hashlib.sha256(text.encode()).hexdigest()


class PathMapTest(unittest.TestCase):
    def setUp(self):
        self.base = helpers.make_temp_dir("pathmap-")

    def tearDown(self):
        helpers.remove_temp_dir(self.base)

    def test_real_of(self):
        pathmap = PathMap(["/:/mnt/root", "/var/www:/mnt/data/", "/srv:/"])
        self.assertEqual(pathmap.real_of("/"), "/mnt/root")
        self.assertEqual(pathmap.real_of("/etc/passwd"), "/mnt/root/etc/passwd")
        self.assertEqual(pathmap.real_of("/var/www"), "/mnt/data")          # 長く一致するほう
        self.assertEqual(pathmap.real_of("/var/www/a"), "/mnt/data/a")
        self.assertEqual(pathmap.real_of("/var/wwwx"), "/mnt/root/var/wwwx")
        self.assertEqual(pathmap.real_of("/srv/x"), "/x")                   # マウント先が / のとき
        self.assertIsNone(PathMap(["/app:/mnt"]).real_of("/etc"))

    def test_invalid(self):
        for spec in ["app:/mnt", "/app:mnt", "/app", "/a:/b:/c", "/a/../b:/mnt", "/a//b:/mnt"]:
            with self.assertRaises(ValueError, msg=spec):
                PathMap([spec])
        with self.assertRaises(ValueError):
            PathMap(["/app:/a", "/app/:/b"])                                 # 同じ元のパスが 2 回

    def test_resolve_links_inside_mount(self):
        # スナップショットの中の絶対パスのリンクは、マウント先の中で解決する
        mount = os.path.join(self.base, "snap")
        helpers.write_file(mount, "data/www/index.php")
        os.makedirs(os.path.join(mount, "var"))
        os.symlink("/data/www", os.path.join(mount, "var/www"))
        os.symlink("../www/index.php", os.path.join(mount, "data/www/rel"))
        os.symlink("/elsewhere", os.path.join(mount, "out"))
        pathmap = PathMap(["/:" + mount, "/elsewhere/x:/tmp"])
        self.assertEqual(pathmap.resolve("/var/www/index.php", follow_last=False),
                         os.path.join(mount, "data/www/index.php"))
        self.assertEqual(pathmap.resolve("/var/www", follow_last=False), os.path.join(mount, "var/www"))
        self.assertEqual(pathmap.resolve("/var/www/rel", follow_last=True), os.path.join(mount, "data/www/index.php"))
        with self.assertRaises(FileNotFoundError):
            pathmap.resolve("/out/y", follow_last=True)                       # /elsewhere は / の対応で、マウント先の中にない

        pathmap = PathMap(["/var:" + os.path.join(mount, "var")])
        with self.assertRaises(OutsideMapError):
            pathmap.resolve("/var/www/index.php", follow_last=True)          # /data は対応の外

        # 対応の元のパスの上（/srv）は、読まずにそのまま進む
        pathmap = PathMap(["/srv/app:" + os.path.join(mount, "data/www")])
        self.assertEqual(pathmap.resolve("/srv/app/index.php", follow_last=True),
                         os.path.join(mount, "data/www/index.php"))
        with self.assertRaises(OutsideMapError):
            pathmap.resolve("/srv", follow_last=True)


class CheckTest(StorageTestCase):
    """ストレージに記録を作り、snap（マウントしたスナップショットに見立てたディレクトリ）と比べる。"""

    def setUp(self):
        super().setUp()
        self.snap = os.path.join(self.base, "snap")
        os.makedirs(os.path.join(self.snap, "app"))

    def put(self, rel: str, content: str = "x") -> None:
        helpers.write_file(self.snap, rel, content)

    def check(self, *args) -> tuple:
        return self.cli("check", "web1", "--map", "/:" + self.snap, *args)

    def tree_lines(self, out: str) -> list:
        return [line.strip() for line in out.split("[差分]\n")[1].split("\n\n")[0].split("\n")]

    def test_same(self):
        self.put("app/a", "1")
        self.put("app/sub/b", "2")
        self.make_snapshot(1, t(1), [("/app/a", sha("1")), ("/app/sub/b", sha("2"))])
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("比べる記録: 2026-09-01 01:00\n", out)
        self.assertIn("比べる場所: /:" + self.snap + "\n", out)
        self.assertIn("[差分]\n差分なし\n", out)
        self.assertIn("照合の範囲外: なし\n", out)

    def test_marks(self):
        self.put("app/a", "changed")
        self.put("app/d", "new")
        os.symlink("a", os.path.join(self.snap, "app/c"))
        self.make_snapshot(1, t(1), [("/app/a", sha("1")), ("/app/b", sha("2")), ("/app/c", sha("3"))])
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(self.tree_lines(out), ["/app/", "M a", "D b", "T c", "A d"])
        self.assertEqual(helpers.legend_counts(out), {"A": 1, "D": 1, "M": 1, "T": 1, "?": 0})
        code, out = self.check("--type", "A", "--not-path", "*/b")
        self.assertEqual(self.tree_lines(out), ["/app/", "A d"])

    def test_at(self):
        self.put("app/a", "1")
        self.make_snapshot(1, t(1), [("/app/a", sha("1"))])
        self.make_snapshot(2, t(3), [("/app/a", sha("2"))])
        code, out = self.check()
        self.assertIn("比べる記録: 2026-09-01 03:00\n", out)
        self.assertEqual(code, 1)
        code, out = self.check("--at", "2026-09-01T02:00Z")
        self.assertIn("比べる記録: 2026-09-01 01:00\n", out)
        self.assertEqual(code, 0, out)
        # 01:00 に始まった記録は 01:01 に終わる。01:00:30 までに終わった記録はない
        code, out = self.check("--at", "2026-09-01T01:00:30Z")
        self.assertEqual(code, 1)

    def test_without_map_reads_files_in_place(self):
        # 基本の使い方: 記録のパスのまま、今のファイルを読む
        app = os.path.join(self.snap, "app")
        self.put("app/a", "1")
        self.make_snapshot(1, t(1), [(app + "/a", sha("1"))], scope={"include": [app + "/**"], "exclude": []})
        code, out = self.cli("check", "web1")
        self.assertEqual(code, 0, out)
        self.assertIn("比べる場所: 今のファイル\n", out)

    def test_unmapped_root_is_outside(self):
        # /etc の対応がないので、/etc の記録は削除として数えない
        self.put("app/a", "1")
        scope = {"include": ["/app/**", "/etc/**"], "exclude": []}
        self.make_snapshot(1, t(1), [("/app/a", sha("1")), ("/etc/hosts", sha("h"))], scope=scope)
        code, out = self.cli("check", "web1", "--map", "/app:" + os.path.join(self.snap, "app"))
        self.assertEqual(code, 0, out)
        self.assertIn("[差分]\n差分なし\n", out)
        self.assertIn("照合の読み取りエラー: なし\n", out)
        self.assertIn("照合の範囲外: /etc\n", out)

    def test_map_below_root(self):
        # include が /** で、対応は /app だけ。/app から読み、/ は一部だけ照合の範囲外
        self.put("app/a", "1")
        scope = {"include": ["/**"], "exclude": []}
        self.make_snapshot(1, t(1), [("/app/a", sha("1")), ("/etc/hosts", sha("h"))], scope=scope)
        code, out = self.cli("check", "web1", "--map", "/app:" + os.path.join(self.snap, "app"))
        self.assertEqual(code, 0, out)
        self.assertIn("照合の範囲外: /（--map の対応がある場所を除く）\n", out)

    def test_nested_maps(self):
        # ルートディスクの /app/data は空のマウントポイント。中身は別のディスク（data）にある
        os.makedirs(os.path.join(self.snap, "app/data"))
        self.put("app/a", "1")
        data = os.path.join(self.base, "data")
        helpers.write_file(data, "x", "2")
        self.make_snapshot(1, t(1), [("/app/a", sha("1")), ("/app/data/x", sha("2"))])
        code, out = self.check("--map", "/app/data:" + data)
        self.assertEqual(code, 0, out)

    def test_missing_nested_mount(self):
        # 入れ子の対応のマウント先がない（書き間違い）。黙って全部を削除と出さずに止まる
        os.makedirs(os.path.join(self.snap, "app/data"))
        self.put("app/a", "1")
        self.make_snapshot(1, t(1), [("/app/a", sha("1")), ("/app/data/x", sha("2"))])
        code, out = self.check("--map", "/app/data:" + os.path.join(self.base, "nothing"))
        self.assertEqual(code, 2)

    def test_mount_is_symlink(self):
        # マウント先そのものがリンクでも、調べる側のマシンの世界でたどる
        self.put("app/a", "1")
        link = os.path.join(self.base, "snaplink")
        os.symlink(self.snap, link)
        self.make_snapshot(1, t(1), [("/app/a", sha("1"))])
        code, out = self.cli("check", "web1", "--map", "/:" + link)
        self.assertEqual(code, 0, out)
        self.assertIn("比べる場所: /:" + link + "\n", out)

    def test_chain_unverified(self):
        # 比べた記録が、検証した範囲の先頭になった（後の記録は不正か、--at までに終わっていない）。
        # 直前の記録とのつながりを確かめていないので、「なし」ではなく「未確認」
        self.put("app/a", "1")
        for seq in range(1, 5):
            path = self.make_snapshot(seq, t(seq), [("/app/a", sha("1"))])
            if seq == 3:
                broken = path
        with open(broken, "wb") as f:
            f.write(b"broken")
        code, out = self.check("--at", "2026-09-01T04:00:30Z")
        self.assertIn("比べる記録: 2026-09-01 02:00\n", out)
        self.assertIn("記録の不一致: 未確認\n", out)
        self.assertEqual(code, 1)
        code, out = self.check("--at", "2026-09-01T02:30Z")              # 直前の記録 1 と比べられる
        self.assertNotIn("未確認", out)
        self.assertEqual(code, 0, out)

    def test_root_link_resolved_inside_mount(self):
        # 起点の current は絶対パスのリンク。調べる側のマシンの /app ではなく、マウント先の中でたどる
        self.put("app/releases/r1/index.php", "1")
        os.symlink("/app/releases/r1", os.path.join(self.snap, "app/current"))
        scope = {"include": ["/app/current/**"], "exclude": []}
        self.make_snapshot(1, t(1), [("/app/current", sha("/app/releases/r1"), "link"),
                                     ("/app/current/index.php", sha("1"))], scope=scope)
        code, out = self.check()
        self.assertEqual(code, 0, out)

    def test_root_link_outside_map_is_not_followed(self):
        self.put("app/x", "1")
        os.symlink("/srv/r1", os.path.join(self.snap, "app/current"))
        scope = {"include": ["/app/current/**"], "exclude": []}
        self.make_snapshot(1, t(1), [("/app/current", sha("/srv/r1"), "link"),
                                     ("/app/current/index.php", sha("1"))], scope=scope)
        code, out = self.cli("check", "web1", "--map", "/app:" + os.path.join(self.snap, "app"))
        self.assertEqual(code, 1)
        self.assertEqual(helpers.legend_counts(out)["?"], 1)               # 中を読めないので状態不明
        self.assertIn("照合の読み取りエラー: 1 ファイル\n", out)

    def test_path_limits_what_is_read(self):
        # 立て直したホストで、戻したデータの分だけを比べる。/etc は読まない（起点がなくても止まらない）
        self.put("app/a", "1")
        scope = {"include": ["/app/**", "/etc/**"], "exclude": []}
        self.make_snapshot(1, t(1), [("/app/a", sha("1")), ("/etc/hosts", sha("h"))], scope=scope)
        code, out = self.check()
        self.assertEqual(code, 2)                                          # /etc がない（--map の誤りの疑い）
        code, out = self.check("--path", "/app/**")
        self.assertEqual(code, 0, out)
        self.assertIn("照合の範囲外: なし\n", out)

    @unittest.skipIf(os.geteuid() == 0, "root はどのファイルも読めるため")
    def test_unreadable(self):
        self.put("app/locked/f", "1")
        self.put("app/e", "2")
        os.chmod(os.path.join(self.snap, "app/locked"), 0)
        self.make_snapshot(1, t(1), [("/app/locked/f", sha("1")), ("/app/e", None, "file")])
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(self.tree_lines(out), ["/app/", "? e", "? locked/f"])
        self.assertIn("記録の読み取りエラー: 1 ファイル\n", out)
        self.assertIn("照合の読み取りエラー: 1 ファイル\n", out)

    def test_errors(self):
        self.make_snapshot(1, t(1), [("/app/a", sha("1"))])
        self.assertEqual(self.check("--map", "app:/mnt")[0], 2)
        self.assertEqual(self.cli("check", "db1")[0], 1)                  # 記録がない
        self.assertEqual(self.cli("check", "nohost")[0], 2)


if __name__ == "__main__":
    unittest.main()
