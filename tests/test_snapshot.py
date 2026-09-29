"""スナップショットの形式（fhashes/snapshot.py）のテスト。"""

import gzip
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402

from fhashes import snapshot  # noqa: E402


def header(**overrides) -> dict:
    values = dict(host="web1", seq=3, prev_snapshot_sha256=None, started_at="2026-09-24T01:00:00Z",
                  include=["/app/**"], exclude=[], reread_cycle=24, algo="sha256", tool_version="test")
    values.update(overrides)
    return snapshot.make_header(**values)


class NameTest(unittest.TestCase):
    def test_file_name_and_parse(self):
        name = snapshot.file_name("web1", "2026-09-24T01:02:03Z", 123)
        self.assertEqual(name, "web1-20260924T010203Z-00000123.ndjson.gz")
        self.assertEqual(snapshot.parse_snapshot_name(name),
                         {"host": "web1", "started_at": "2026-09-24T01:02:03Z", "seq": 123})

    def test_not_a_snapshot_name(self):
        for name in ["README.txt", "web1-20260924T010203Z-00000123.ndjson", "web 1-20260924T010203Z-00000123.ndjson.gz",
                     "web1-20261399T000000Z-00000001.ndjson.gz"]:   # 形は合っているが、ありえない日時
            self.assertIsNone(snapshot.parse_snapshot_name(name), name)

    def test_host_name_rule(self):
        self.assertTrue(snapshot.HOST_NAME_REGEX.match("web-1.example"))
        self.assertFalse(snapshot.HOST_NAME_REGEX.match("-web"))
        self.assertTrue(snapshot.HOST_NAME_REGEX.match("web_1"))
        self.assertFalse(snapshot.HOST_NAME_REGEX.match("web 1"))
        self.assertFalse(snapshot.HOST_NAME_REGEX.match("web/1"))


class HeaderTest(unittest.TestCase):
    def test_scope_sha256_follows_scope(self):
        self.assertEqual(header()["scope_sha256"], header()["scope_sha256"])
        self.assertNotEqual(header()["scope_sha256"], header(exclude=["/app/cache/**"])["scope_sha256"])

    def test_bad_values(self):
        cases = {
            "started_at": "x",
            "seq": "3",
            "host": "web 1",
            "prev_snapshot_sha256": "abc",
            "scope_sha256": None,
            "include": "/app/**",
        }
        for key, value in cases.items():
            bad = header()
            bad["fhashes_snapshot"] = snapshot.FORMAT_VERSION
            bad[key] = value
            with self.assertRaises(snapshot.SnapshotFormatError, msg=key):
                snapshot.check_header(bad)

    def test_required_keys(self):
        good = header()
        good["fhashes_snapshot"] = snapshot.FORMAT_VERSION
        snapshot.check_header(good)
        for key in snapshot.REQUIRED_HEADER_KEYS:
            bad = dict(good)
            del bad[key]
            with self.assertRaises(snapshot.SnapshotFormatError, msg=key):
                snapshot.check_header(bad)


class ReadWriteTest(unittest.TestCase):
    def setUp(self):
        self.dir = helpers.make_temp_dir("snapshot-")
        self.path = os.path.join(self.dir, "s.ndjson.gz")

    def tearDown(self):
        helpers.remove_temp_dir(self.dir)

    def write(self):
        writer = snapshot.SnapshotWriter(self.path, header())
        # パスの順（バイト順）に書く
        writer.write_file("/app/a", "file", "h1")
        writer.write_file(os.fsdecode(b"/app/caf\xe9"), "file", "h2")   # UTF-8 でない名前
        writer.write_error("/app/locked", "dir", 'PermissionError: "x"')
        writer.write_file("/app/tab\there", "link", "h3")                 # タブを含む名前
        return writer.close("2026-09-24T01:00:05Z")

    def test_round_trip(self):
        end = self.write()
        summary = snapshot.read_summary(self.path)
        self.assertEqual(summary["end"], end)
        self.assertEqual((end["files"], end["errors"]), (3, 1))
        rows = list(snapshot.iter_rows(self.path))
        self.assertEqual([snapshot.row_key(r) for r in rows],
                         [b"/app/a", b"/app/caf\xe9", b"/app/locked", b"/app/tab\there"])
        self.assertEqual(rows[1]["path"], "/app/caf\\xe9")
        self.assertEqual(rows[2], {"path": "/app/locked", "kind": "dir", "error": 'PermissionError: "x"'})

    def test_count_mismatch_is_detected(self):
        self.write()
        with gzip.open(self.path, "rb") as f:
            lines = f.read().split(b"\n")
        del lines[1]  # ファイル行を 1 つ消す
        with gzip.open(self.path, "wb") as f:
            f.write(b"\n".join(lines))
        with self.assertRaises(snapshot.SnapshotFormatError):
            snapshot.read_summary(self.path)

    def test_summary_with_small_chunks(self):
        # 展開の区切りが行の途中に来ても、最初の行・最後の行・行数を正しく取り出せる
        end = self.write()
        original = (snapshot.SUMMARY_CHUNK, snapshot.SUMMARY_TAIL)
        try:
            for chunk, tail in ((7, 1024), (1, 1024), (1024, 300)):
                snapshot.SUMMARY_CHUNK, snapshot.SUMMARY_TAIL = chunk, tail
                summary = snapshot.read_summary(self.path)
                self.assertEqual(summary["end"], end, (chunk, tail))
                self.assertEqual(summary["header"]["seq"], 3)
        finally:
            snapshot.SUMMARY_CHUNK, snapshot.SUMMARY_TAIL = original

    def test_missing_last_newline_is_still_one_line(self):
        # 最後の改行がなくても、最後の行は 1 行と数える（以前の読み方と同じ）
        end = self.write()
        with gzip.open(self.path, "rb") as f:
            data = f.read()
        with gzip.open(self.path, "wb") as f:
            f.write(data.rstrip(b"\n"))
        self.assertEqual(snapshot.read_summary(self.path)["end"], end)

    def test_content_tampering_is_detected(self):
        self.write()
        with gzip.open(self.path, "rb") as f:
            text = f.read().replace(b'"hash":"h1"', b'"hash":"hX"')
        with gzip.open(self.path, "wb") as f:
            f.write(text)
        snapshot.read_summary(self.path)  # ヘッダーと終端だけでは分からない
        with self.assertRaises(snapshot.SnapshotFormatError):
            list(snapshot.iter_rows(self.path))

    def test_header_is_first_line(self):
        self.write()
        with gzip.open(self.path, "rb") as f:
            first = json.loads(f.readline())
        self.assertEqual(first["fhashes_snapshot"], snapshot.FORMAT_VERSION)
        self.assertEqual(first["host"], "web1")


if __name__ == "__main__":
    unittest.main()
