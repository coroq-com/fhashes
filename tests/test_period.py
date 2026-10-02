"""期間を指定して調べるコマンド（log と diff）に共通の部分のテスト。"""

import argparse
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402
from helpers import StorageTestCase, t  # noqa: E402

from fhashes import config  # noqa: E402
from fhashes.review import period  # noqa: E402


def examined(changes, since=None, until=None, records=None, still_error=()) -> period.Period:
    result = {"snapshots": records or [], "changes": changes, "still_error": list(still_error)}
    return period.Period("web1", since, until, result)


def change(path, after, before):
    return {"path": path, "changed_after": after, "changed_before": before}


class ChangesTest(unittest.TestCase):
    def test_overlap_and_detected(self):
        a = change("/app/a", t(1), t(2, 1))     # 変化した可能性のある時期: 01:00 - 02:01
        found = examined([a], since=t(2, 1, 30), until=t(2, 30))
        self.assertEqual(found.changes(), [])            # 02:01:30 以降とは重ならない
        found = examined([a], since=t(1, 30), until=t(1, 40))
        self.assertEqual(found.changes(), [a])           # 重なる（取りこぼしがない）
        self.assertEqual(found.changes(detected=True), [])   # 検知は 02:01 なので期間外
        found = examined([a], since=t(2), until=t(3))
        self.assertEqual(found.changes(detected=True), [a])
        # 変化は記録の終了（02:01）より前に起きているので、ちょうど 02:01 からの期間とは重ならない
        self.assertEqual(examined([a], since=t(2, 1), until=t(3)).changes(), [])

    def test_open_ends(self):
        a = change("/app/a", t(1), t(2, 1))
        self.assertEqual(examined([a]).changes(), [a])
        self.assertEqual(examined([a], since=t(3)).changes(), [])
        self.assertEqual(examined([a], until=t(0, 30)).changes(), [])

    def test_path_filter_and_order(self):
        b = change("/app/b", t(1), t(2, 1))
        a = change("/app/a", t(1), t(2, 1))
        early = change("/app/z", t(0), t(1, 1))
        found = examined([b, a, early])
        self.assertEqual(found.changes(), [early, a, b])     # 時期の終わり、パスの順
        self.assertEqual(found.changes(period.path_filter(["/app/a"], None)), [a])

    def test_still_error_follows_path_filter(self):
        items = [{"path": "/app/a"}, {"path": "/app/cache/x"}]
        found = examined([], still_error=items)
        self.assertEqual(found.still_error(period.path_filter(None, ["*/cache/**"])), [items[0]])


class PathFilterTest(unittest.TestCase):
    def test_path_filter(self):
        self.assertIsNone(period.path_filter(None, None))
        keep = period.path_filter(["/app/**"], ["*/cache/**", "*.log"])
        self.assertTrue(keep("/app/index.php"))
        self.assertFalse(keep("/app/cache/x.html"))     # 絞ってから除く
        self.assertFalse(keep("/app/error.log"))
        self.assertFalse(keep("/etc/passwd"))           # --path に一致しない
        keep = period.path_filter(None, ["/app/cache/**"])
        self.assertTrue(keep("/etc/passwd"))            # --not-path だけなら、全部から除く
        self.assertFalse(keep("/app/cache/x.html"))


class ParsePeriodTest(unittest.TestCase):
    def test_parse_period(self):
        args = argparse.Namespace(since="2026-09-01", until="2026-09-01")
        with helpers.TimeZone("UTC"):
            self.assertEqual(period.parse_period(args), ("2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"))
            self.assertEqual(period.parse_period(argparse.Namespace(since=None, until=None)), (None, None))
            with self.assertRaises(ValueError):
                period.parse_period(argparse.Namespace(since="2026-09-02", until="2026-09-01 10:00"))


class OpenPeriodTest(StorageTestCase):
    def test_open_period(self):
        args = argparse.Namespace(host="nohost", since=None, until=None)
        with self.assertRaises(config.ConfigError):
            period.open_period(self.conf, args)              # 設定の hosts にない
        self.assertIsNone(period.open_period(self.conf, argparse.Namespace(host="db1", since=None, until=None)))
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        self.make_snapshot(2, t(2), [("/app/a", "h2")], scope={"include": ["/app/**", "/etc/x"], "exclude": []})
        found = period.open_period(self.conf, argparse.Namespace(host="web1", since=None, until=None))
        self.assertEqual([ch["path"] for ch in found.changes()], ["/app/a"])
        # 木を分ける起点は、最後の記録の監視範囲から
        self.assertEqual([root["path"] for root in found.roots()], ["/app", "/etc/x"])


if __name__ == "__main__":
    unittest.main()
