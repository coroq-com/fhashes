"""調べる側（status / review / clean）のテスト。

スナップショットは、時刻や内容を指定して直接作る（snapshot.SnapshotWriter を使う）。
ストレージはローカルのディレクトリで、一覧とダウンロードには rclone を使う（見つからなければスキップ）。
"""

import gzip
import io
import os
from datetime import timedelta
import shutil
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402

from fhashes import cli, config, snapshot, timeutil  # noqa: E402
from fhashes.review import analysis, report, storage  # noqa: E402

RCLONE = helpers.find_rclone()
SCOPE = {"include": ["/app/**"], "exclude": []}
ALL_TIME = ("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")


def t(hour: int, minute: int = 0, second: int = 0) -> str:
    """テスト用の時刻（2026-09-01 の UTC）。"""
    return "2026-09-01T%02d:%02d:%02dZ" % (hour, minute, second)


@unittest.skipUnless(RCLONE, "rclone が見つかりません")
class ReviewTestBase(unittest.TestCase):
    def setUp(self):
        self.base = helpers.make_temp_dir("review-")
        self.storage = os.path.join(self.base, "storage")
        os.makedirs(self.storage)
        self.config_path = os.path.join(self.base, "review.yaml")
        with open(self.config_path, "w") as f:
            f.write("cache_dir: cache\n"
                    "rclone: %s\nhosts:\n  web1: %s\n  db1: %s\n" % (RCLONE, self.storage, self.storage))
        self.conf = config.load_review_config(self.config_path)
        self.last_sha = {}
        # 日時の入力と表示は実行環境のタイムゾーンに従うので、テストでは UTC に固定する
        self.timezone = helpers.TimeZone("UTC")
        self.timezone.enter()

    def tearDown(self):
        self.timezone.exit()
        helpers.remove_temp_dir(self.base)

    def make_snapshot(self, seq: int, started_at: str, rows: list, host="web1", scope=None,
                      prev_sha="auto", finished_after_seconds=60) -> str:
        """スナップショットを 1 つストレージに作る。

        rows: (パス, ハッシュ) か (パス, None, エラー種類 "file"/"dir") のリスト。
              ハッシュは "h1" のような短い文字列でよい。
        """
        scope = scope or SCOPE
        header = snapshot.make_header(
            host=host, seq=seq,
            prev_snapshot_sha256=self.last_sha.get(host) if prev_sha == "auto" else prev_sha,
            started_at=started_at, include=scope["include"], exclude=scope["exclude"],
            reread_cycle=24, algo="sha256", tool_version="test",
        )
        path = os.path.join(self.storage, snapshot.file_name(host, started_at, seq))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        writer = snapshot.SnapshotWriter(path, header)
        for row in sorted(rows, key=lambda r: r[0].encode()):
            if row[1] is not None:
                writer.write_file(row[0], "file", row[1])
            else:
                writer.write_error(row[0], row[2], "PermissionError: Permission denied")
        writer.close(timeutil.to_utc_text(timeutil.parse_utc(started_at) + timedelta(seconds=finished_after_seconds)))
        self.last_sha[host] = snapshot.file_sha256(path)
        return path

    def analyze(self, since=ALL_TIME[0], until=ALL_TIME[1], host="web1") -> dict:
        return report.analyze_host_period(self.conf, host, since, until)

    def changes(self, **kwargs) -> list:
        result = self.analyze(**kwargs)
        return sorted((c["type"], c["path"]) for c in result["changes"])

    def change(self, path: str) -> dict:
        for c in self.analyze()["changes"]:
            if c["path"] == path:
                return c
        return None

    def chains(self) -> list:
        return [s["chain"] for s in self.analyze()["snapshots"]]

    def cli(self, *args) -> tuple:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main([args[0], "--config", self.config_path] + list(args[1:]))
        return code, out.getvalue()


class AnalysisTest(ReviewTestBase):
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
        self.assertEqual(self.analyze()["still_error_count"], 0)

    def test_unreadable_dir_children_are_not_deleted(self):
        self.make_snapshot(1, t(1), [("/app/d/a", "h1"), ("/app/d/b", "h1"), ("/app/dx", "h1")])
        self.make_snapshot(2, t(2), [("/app/d", None, "dir"), ("/app/dx", "h1")])
        self.make_snapshot(3, t(3), [("/app/d", None, "dir"), ("/app/dx", "h1")])
        self.assertEqual(self.changes(), [])
        self.assertEqual(self.analyze()["still_error_count"], 2)
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
        self.assertEqual(self.analyze()["still_error_count"], 1)
        self.make_snapshot(4, t(4), [("/app/d/a", "h1")])
        self.assertEqual(self.changes(), [])

    def test_new_unreadable_file_is_added(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        self.make_snapshot(2, t(2), [("/app/a", "h1"), ("/app/secret", None, "file")])
        self.make_snapshot(3, t(3), [("/app/a", "h1"), ("/app/secret", "h9")])
        self.assertEqual(self.changes(), [("added", "/app/secret")])
        self.assertIsNone(self.change("/app/secret")["new_hash"])

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
        code, out = self.cli("review", "web1", "--from", "2026-09-01", "--to", "2026-09-01")
        self.assertEqual(code, 1)
        self.assertIn("✗ 不正なスナップショット", out)

    def test_bad_row_is_reported_not_crashing(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")])
        path2 = self.make_snapshot(2, t(2), [("/app/a", "h2")])
        with gzip.open(path2, "rb") as f:
            text = f.read().replace(b',"kind":"file"', b'')
        with gzip.open(path2, "wb") as f:
            f.write(text)
        self.assertIn("中身が壊れています", self.analyze()["snapshots"][1]["problem"])

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
            cached.extend(files)
        self.assertEqual(len(cached), 4)

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


class ReportTest(ReviewTestBase):
    def setUp(self):
        super().setUp()
        self.make_snapshot(1, t(1), [("/app/a", "h1"), ("/app/b", "h1")])
        self.make_snapshot(2, t(2), [("/app/a", "h2"), ("/app/b", "h1")])
        self.make_snapshot(3, t(3), [("/app/a", "h2"), ("/app/b", "h1")])
        self.make_snapshot(4, t(7), [("/app/a", "h2"), ("/app/b", "h2")])   # 普段は 1 時間おき。ここだけ 4 時間空く

    def test_review_filters_overlap_and_detected(self):
        code, out = self.cli("review", "web1", "--from", "2026-09-01 02:01:30", "--to", "2026-09-01 02:30",
                             "--format", "csv")
        self.assertNotIn("/app/a", out)       # 変更期間 01:00:00 〜 02:01:00 は 02:01:30 以降と重ならない
        code, out = self.cli("review", "web1", "--from", "2026-09-01 01:30", "--to", "2026-09-01 01:40",
                             "--format", "csv")
        self.assertIn("/app/a", out)          # 変更期間 01:00:00 〜 02:01:00 と重なる
        code, out = self.cli("review", "web1", "--from", "2026-09-01 01:30", "--to", "2026-09-01 01:40",
                             "--detected", "--format", "csv")
        self.assertNotIn("/app/a", out)       # 検知は 02:01:00 なので期間外
        code, out = self.cli("review", "web1", "--from", "2026-09-01", "--to", "2026-09-01",
                             "--path", "/app/b", "--format", "csv")
        self.assertEqual(out.count("/app/b"), 1)
        self.assertNotIn("/app/a", out)
        code, out = self.cli("review", "web1", "--from", "2026-09-01", "--to", "2026-09-01",
                             "--type", "added", "--format", "json")
        self.assertEqual(out.strip(), "[]")

    def test_timezone_is_only_for_humans(self):
        with helpers.TimeZone("Asia/Tokyo"):
            # タイムゾーンを書かない入力は JST として解釈する（01:30〜01:40 UTC = 10:30〜10:40 JST）
            code, out = self.cli("review", "web1", "--from", "2026-09-01 10:30", "--to", "2026-09-01 10:40",
                                 "--format", "csv")
            self.assertIn("/app/a", out)
            # CSV の時刻は UTC
            self.assertIn("2026-09-01T02:01:00Z", out)
            # タイムゾーンを明示した入力は、そのまま解釈する
            code, out = self.cli("review", "web1", "--from", "2026-09-01T01:30Z", "--to", "2026-09-01T01:40Z",
                                 "--format", "csv")
            self.assertIn("/app/a", out)
            # 表では JST で表示し、どのタイムゾーンかを書く
            code, out = self.cli("review", "web1", "--from", "2026-09-01T01:30Z", "--to", "2026-09-01T01:40Z")
            self.assertIn("2026-09-01 11:01:00", out)
            self.assertIn("JST（+09:00）", out)

    def test_period_can_be_omitted(self):
        # 全期間: 最初のスナップショットから最新のものまで。端がないことを問題にしない
        code, out = self.cli("review", "web1")
        self.assertEqual(code, 0, out)
        self.assertIn("調査期間: 最初のスナップショット 〜 最新のスナップショット", out)
        self.assertIn("✓ 調べた範囲の始まり: 最初のスナップショット 2026-09-01 01:00:00（seq 1）", out)
        self.assertIn("✓ 調べた範囲の終わり: 最新のスナップショット 2026-09-01 07:00:00（seq 4。", out)
        self.assertIn("/app/a", out)
        self.assertIn("/app/b", out)
        # --from だけ: その時刻から最新まで（a の変更期間 01:00〜02:01 は含まない）
        code, out = self.cli("review", "web1", "--from", "2026-09-01 02:30", "--format", "csv")
        self.assertEqual(code, 0)
        self.assertNotIn("/app/a", out)
        self.assertIn("/app/b", out)
        # --to だけ: 最初からその時刻まで（b の変更期間 03:00〜07:01 は含まない）
        code, out = self.cli("review", "web1", "--to", "2026-09-01 01:30", "--format", "csv")
        self.assertEqual(code, 0)
        self.assertIn("/app/a", out)
        self.assertNotIn("/app/b", out)

    def test_review_reports_irregular_interval(self):
        # 間隔の乱れは報告するが、深刻な異常とはみなさない
        code, out = self.cli("review", "web1", "--from", "2026-09-01 03:30", "--to", "2026-09-01 06:30")
        self.assertEqual(code, 0, out)
        self.assertIn("! 記録の間隔の乱れ: 2026-09-01 03:00:00 〜 2026-09-01 07:00:00", out)
        self.assertIn("普段は約 60 分", out)
        self.assertIn("/app/b", out)
        self.make_snapshot(5, t(8), [("/app/a", "h2"), ("/app/b", "h2")])
        code, out = self.cli("review", "web1", "--from", "2026-09-01 07:10", "--to", "2026-09-01 07:50")
        self.assertIn("✓ 記録の間隔: 乱れなし（普段は約 60 分）", out)

    def test_interval_is_not_checked_with_few_snapshots(self):
        self.make_snapshot(1, t(1), [("/app/a", "h1")], host="db1")
        self.make_snapshot(2, t(5), [("/app/a", "h1")], host="db1")
        code, out = self.cli("review", "db1", "--from", "2026-09-01", "--to", "2026-09-02")
        self.assertIn("記録の数が少ないため", out)

    def test_review_problem_sets_exit_code_even_in_csv(self):
        self.make_snapshot(5, t(8), [("/app/a", "h2"), ("/app/b", "h2")], prev_sha="0" * 64)
        code, out = self.cli("review", "web1", "--from", "2026-09-01 07:30", "--to", "2026-09-01 07:40",
                             "--format", "csv")
        self.assertEqual(code, 1)

    def test_review_clean_period(self):
        code, out = self.cli("review", "web1", "--from", "2026-09-01 02:10", "--to", "2026-09-01 02:50")
        self.assertEqual(code, 0, out)
        self.assertIn("該当する変化はありません", out)
        self.assertIn("説明のつかない変更はありません", out)

    def test_review_shows_still_error_files(self):
        self.make_snapshot(8, t(8), [("/app/a", None, "file"), ("/app/b", "h2")])
        code, out = self.cli("review", "web1", "--from", "2026-09-01 07:30", "--to", "2026-09-01 07:40")
        self.assertIn("期間の終わりでもエラーのまま: 1 件", out)

    def test_review_unknown_host(self):
        code, out = self.cli("review", "nohost", "--from", "2026-09-01", "--to", "2026-09-02")
        self.assertEqual(code, 2)   # 設定の hosts にない
        code, out = self.cli("review", "db1", "--from", "2026-09-01", "--to", "2026-09-02")
        self.assertEqual(code, 1)   # 設定にはあるが、スナップショットがまだない

    def test_status(self):
        code, out = self.cli("status")
        self.assertIn("web1", out)
        self.assertIn("最新のスナップショットが普段の間隔より古い", out)
        self.assertIn("db1", out)
        self.assertIn("スナップショットがない", out)
        self.assertEqual(code, 1)   # db1 に 1 つも届いていない
        self.make_snapshot(1, t(7), [("/app/a", "h1")], host="db1")
        code, out = self.cli("status")
        self.assertEqual(code, 0)   # 間隔の乱れだけなら問題にしない
        self.make_snapshot(5, t(8), [("/app/a", "h2"), ("/app/b", "h2")], prev_sha="0" * 64)
        code, out = self.cli("status")
        self.assertIn("チェーン異常（broken）", out)
        self.assertEqual(code, 1)


class ReviewConfigTest(unittest.TestCase):
    def load(self, text: str):
        base = helpers.make_temp_dir("review-config-")
        self.addCleanup(helpers.remove_temp_dir, base)
        path = os.path.join(base, "review.yaml")
        with open(path, "w") as f:
            f.write(text)
        return config.load_review_config(path)

    def test_hosts_is_required(self):
        for text in ("cache_dir: cache\n", "hosts: []\n", "hosts:\n  web1: ''\n", "hosts:\n  'web 1': 'x:y'\n"):
            with self.assertRaises(config.ConfigError, msg=text):
                self.load(text)
        self.assertEqual(self.load("hosts:\n  web1: 'gcs:b'\n")["hosts"], {"web1": "gcs:b"})

    def test_rclone_settings(self):
        conf = self.load("rclone_config: rclone.conf\nhosts:\n  web1: 'gcs:b'\n")
        self.assertEqual(os.path.basename(conf["rclone_config"]), "rclone.conf")
        self.assertTrue(os.path.isabs(conf["rclone_config"]))   # 設定ファイルのある場所が基準
        self.assertEqual((conf["rclone"], conf["rclone_options"]), ("rclone", []))
        for text in ("rclone: [rclone, --config, x]\n", "rclone_options: --fast-list\n", "rclone_config: 1\n"):
            with self.assertRaises(config.ConfigError, msg=text):
                self.load(text + "hosts:\n  web1: 'gcs:b'\n")


class SelectRangeTest(unittest.TestCase):
    """期間に必要なスナップショットの選び方（ダウンロードしない単体テスト）。"""

    def test_edges(self):
        entries = [{"name_time": t(h)} for h in (1, 2, 3)]
        self.assertEqual(analysis.select_range(entries, t(0), t(0, 30)), entries[0:1])   # 最初より前
        self.assertEqual(analysis.select_range(entries, t(5), t(6)), entries[1:3])       # 最後より後
        self.assertEqual(analysis.select_range(entries, t(2), t(2, 30)), entries[0:3])   # 真ん中


if __name__ == "__main__":
    unittest.main()
