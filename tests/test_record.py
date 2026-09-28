"""記録する側（fhashes record）のテスト。"""

import io
import os
import sys
import unittest
from unittest import mock
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402

from fhashes import cli, config, snapshot, walker  # noqa: E402
from fhashes.record import recorder, upload  # noqa: E402

RCLONE = helpers.find_rclone()


class RecordTestBase(unittest.TestCase):
    def setUp(self):
        self.base = helpers.make_temp_dir("record-")
        self.data = os.path.join(self.base, "data")
        self.storage = os.path.join(self.base, "storage")
        os.makedirs(self.data)
        self.write_config()

    def tearDown(self):
        helpers.remove_temp_dir(self.base)

    def write_config(self, include=None, exclude=None, extra=""):
        include = include or [self.data + "/**"]
        exclude = exclude or []
        lines = ["host: testhost", "state_dir: " + self.base + "/state", "include:"]
        lines += ["  - " + p for p in include]
        lines += ["exclude: []"] if not exclude else ["exclude:"] + ["  - " + p for p in exclude]
        lines += ["upload:", "  rclone: " + str(RCLONE or "rclone"), "  remote: " + self.storage]
        lines += ["reread_cycle: 3"]
        self.config_path = os.path.join(self.base, "record.yaml")
        with open(self.config_path, "w") as f:
            f.write("\n".join(lines) + "\n" + extra)

    def record(self) -> dict:
        """記録して、出力されたスナップショットを読む。read はこの回に中身を読んだファイルの集合。"""
        conf = config.load_record_config(self.config_path)
        with mock.patch.object(walker, "hash_file", wraps=walker.hash_file) as hash_file:
            path = recorder.record(conf)
        read = {call.args[0][len(self.data):] for call in hash_file.call_args_list}
        summary = snapshot.read_summary(path)
        rows = {}
        for row in snapshot.iter_rows(path):
            rows[row["path"][len(self.data):]] = row
        return {"path": path, "header": summary["header"], "end": summary["end"], "rows": rows, "read": read}


class RecordTest(RecordTestBase):
    def test_snapshot_left_in_tmp_after_state_update_is_recovered(self):
        helpers.write_file(self.data, "a.txt", "1")
        self.record()                                                        # seq 1
        helpers.write_file(self.data, "a.txt", "22")
        real_rename = os.rename

        def crash_on_outbox(src, dst):
            if os.sep + "outbox" + os.sep in dst:
                raise RuntimeError("停電のつもり")
            return real_rename(src, dst)

        conf = config.load_record_config(self.config_path)
        with mock.patch.object(recorder.os, "rename", side_effect=crash_on_outbox):
            with self.assertRaises(RuntimeError):
                recorder.record(conf)                                        # seq 2: 状態の更新後に止まる
        third = self.record()                                                # seq 3
        outbox = os.path.join(conf["state_dir"], "outbox")
        names = sorted(os.listdir(outbox))
        self.assertEqual([snapshot.parse_snapshot_name(n)["seq"] for n in names], [1, 2, 3])
        # ハッシュチェーンがつながっている
        self.assertEqual(third["header"]["prev_snapshot_sha256"], snapshot.file_sha256(os.path.join(outbox, names[1])))
        second = snapshot.read_summary(os.path.join(outbox, names[1]))
        self.assertEqual(second["header"]["prev_snapshot_sha256"], snapshot.file_sha256(os.path.join(outbox, names[0])))

    def test_changed_files_are_read_and_chain_continues(self):
        helpers.write_file(self.data, "a.txt", "1")
        helpers.write_file(self.data, "b.txt", "1")
        first = self.record()
        self.assertIn("/a.txt", first["read"])
        self.assertEqual(first["header"]["seq"], 1)
        self.assertIsNone(first["header"]["prev_snapshot_sha256"])

        helpers.write_file(self.data, "b.txt", "2")
        second = self.record()
        self.assertIn("/b.txt", second["read"])  # ctime が変わったので読む
        self.assertNotEqual(first["rows"]["/b.txt"]["hash"], second["rows"]["/b.txt"]["hash"])
        # ハッシュチェーン
        self.assertEqual(second["header"]["seq"], 2)
        self.assertEqual(second["header"]["prev_snapshot_sha256"], snapshot.file_sha256(first["path"]))

    def test_header_describes_the_run(self):
        helpers.write_file(self.data, "a.txt")
        snap = self.record()
        header = snap["header"]
        self.assertEqual(header["host"], "testhost")
        self.assertEqual(header["include"], [self.data + "/**"])
        self.assertEqual(header["exclude"], [])
        self.assertEqual(header["reread_cycle"], 3)
        self.assertEqual(header["scope_sha256"], snapshot.scope_sha256([self.data + "/**"], []))
        self.assertEqual(header["algo"], "sha256")

    def test_every_file_is_reread_within_cycle(self):
        for i in range(60):
            helpers.write_file(self.data, "f%02d.txt" % i, "same content")  # 同じ内容でも組は散らばる
        self.record()  # 1 回目はすべて読む
        read_counts = {}
        for _ in range(3):  # reread_cycle = 3
            snap = self.record()
            read_this_time = snap["read"]
            self.assertLess(len(read_this_time), 60)  # 毎回全部読むわけではない
            for path in read_this_time:
                read_counts[path] = read_counts.get(path, 0) + 1
        # 3 回の実行で、どのファイルもちょうど 1 回読み直されている
        self.assertEqual(sorted(read_counts), sorted(snap["rows"]))
        self.assertEqual(set(read_counts.values()), {1})

    def test_reread_groups_are_spread(self):
        counts = [0] * 24
        for i in range(2400):
            counts[recorder.reread_group(b"/var/www/app/file%d.php" % i, 24)] += 1
        self.assertTrue(all(50 <= c <= 150 for c in counts), counts)   # 平均 100

    def test_rows_are_sorted_and_complete(self):
        for name in ["b/z.txt", "a.txt", "b-c.txt", "b/a.txt"]:
            helpers.write_file(self.data, name)
        os.symlink("a.txt", os.path.join(self.data, "link"))
        snap = self.record()
        keys = [snapshot.row_key({"path": self.data + p}) for p in snap["rows"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(snap["end"]["files"], 5)
        self.assertEqual(snap["rows"]["/link"]["kind"], "link")

    def test_unreadable_file_and_dir(self):
        helpers.write_file(self.data, "ok.txt")
        bad = helpers.write_file(self.data, "bad.txt")
        os.chmod(bad, 0)
        helpers.write_file(self.data, "locked/in.txt")
        os.chmod(os.path.join(self.data, "locked"), 0)
        snap = self.record()
        self.assertEqual(snap["rows"]["/bad.txt"]["error"], "PermissionError: Permission denied")
        self.assertEqual(snap["rows"]["/locked"]["kind"], "dir")
        self.assertEqual(snap["end"]["errors"], 2)
        # 次の回、読めるようになったら読む（キャッシュに残していない）
        os.chmod(bad, 0o644)
        snap = self.record()
        self.assertIn("/bad.txt", snap["read"])

    def test_skip_when_locked(self):
        helpers.write_file(self.data, "a.txt")
        conf = config.load_record_config(self.config_path)
        os.makedirs(conf["state_dir"], exist_ok=True)
        lock = recorder.acquire_lock(conf["state_dir"])
        try:
            with self.assertRaises(recorder.Skipped):
                recorder.record(conf)
        finally:
            lock.close()

    def test_failed_run_does_not_change_state(self):
        helpers.write_file(self.data, "a.txt", "1")
        self.record()
        helpers.write_file(self.data, "a.txt", "2")
        conf = config.load_record_config(self.config_path)
        original = recorder.write_snapshot

        def broken_write_snapshot(*args):
            raise RuntimeError("途中で止まった")
        recorder.write_snapshot = broken_write_snapshot
        try:
            with self.assertRaises(RuntimeError):
                recorder.record(conf)
        finally:
            recorder.write_snapshot = original
        # 状態は前回のまま → 次の実行で a.txt を読み直し、通し番号も続きになる
        snap = self.record()
        self.assertIn("/a.txt", snap["read"])
        self.assertEqual(snap["header"]["seq"], 2)

    def test_host_is_required(self):
        path = os.path.join(self.base, "nohost.yaml")
        with open(path, "w") as f:
            f.write("include:\n  - " + self.data + "/**\n")
        with self.assertRaises(config.ConfigError):
            config.load_record_config(path)

    def test_relative_state_dir_is_based_on_config_dir(self):
        config_dir = os.path.join(self.base, "conf")
        os.makedirs(config_dir)
        path = os.path.join(config_dir, "record.yaml")
        with open(path, "w") as f:
            f.write("host: h1\ninclude:\n  - " + self.data + "/**\n")
        conf = config.load_record_config(path)
        self.assertEqual(conf["state_dir"], os.path.join(self.base, "data", "record"))

    def test_symlinked_root_is_followed_but_inner_links_are_not(self):
        helpers.write_file(self.data, "releases/r1/index.php", "1")
        helpers.write_file(self.data, "shared/uploads/a.php", "1")
        os.symlink("releases/r1", os.path.join(self.data, "current"))
        os.symlink("../../shared/uploads", os.path.join(self.data, "releases/r1/uploads"))
        self.write_config(include=[self.data + "/current/**"])
        snap = self.record()
        self.assertEqual(sorted(snap["rows"]), ["/current", "/current/index.php", "/current/uploads"])
        self.assertEqual(snap["rows"]["/current"]["kind"], "link")           # リンク自体も記録する
        self.assertEqual(snap["rows"]["/current/index.php"]["kind"], "file")
        self.assertEqual(snap["rows"]["/current/uploads"]["kind"], "link")   # 途中のリンクはたどらない

    def test_missing_root_directory_fails_the_whole_run(self):
        helpers.write_file(self.data, "app/a.php")
        self.record()                                   # seq 1
        self.write_config(include=[self.data + "/app/**", self.data + "/nothere/**"])
        conf = config.load_record_config(self.config_path)
        with self.assertRaises(walker.MissingRootError):
            recorder.record(conf)
        outbox = os.path.join(conf["state_dir"], "outbox")
        self.assertEqual(sum(len(files) for _, _, files in os.walk(outbox)), 1)   # 新しいスナップショットはない
        # 起点を直せば、通し番号は続きから（状態ファイルは変わっていない）
        self.write_config(include=[self.data + "/app/**"])
        self.assertEqual(self.record()["header"]["seq"], 2)

    def test_missing_single_file_pattern_is_just_deleted(self):
        helpers.write_file(self.data, "app/a.php")
        self.write_config(include=[self.data + "/app/**", self.data + "/gone.txt"])
        snap = self.record()
        self.assertNotIn("/gone.txt", snap["rows"])
        self.assertEqual(snap["end"]["errors"], 0)

    def test_dangling_symlinked_root_fails(self):
        os.symlink("releases/none", os.path.join(self.data, "current"))
        self.write_config(include=[self.data + "/current/**"])
        with self.assertRaises(walker.MissingRootError):
            recorder.record(config.load_record_config(self.config_path))

    def test_broken_state_file_is_replaced(self):
        helpers.write_file(self.data, "a.txt")
        self.record()
        state = os.path.join(self.base, "state", "state.sqlite3")
        for suffix in ("-wal", "-shm"):
            if os.path.exists(state + suffix):
                os.remove(state + suffix)
        with open(state, "w") as f:
            f.write("broken")
        snap = self.record()
        self.assertEqual(snap["header"]["seq"], 1)                     # 通し番号はやり直し
        self.assertIsNone(snap["header"]["prev_snapshot_sha256"])     # 調べる側では restart と報告される
        self.assertIn("/a.txt", snap["read"])                # 全部読み直す
        broken = [n for n in os.listdir(os.path.join(self.base, "state")) if n.startswith("state.sqlite3.broken-")]
        self.assertEqual(len(broken), 1)                               # 壊れたものは残す

    def test_non_utf8_file_name(self):
        with open(os.fsencode(self.data) + b"/caf\xe9.txt", "wb") as f:
            f.write(b"x")
        snap = self.record()
        row = snap["rows"]["/caf\\xe9.txt"]
        self.assertIn("path_b64", row)
        self.assertEqual(snapshot.row_key(row), os.fsencode(self.data) + b"/caf\xe9.txt")


@unittest.skipUnless(RCLONE, "rclone が見つかりません")
class UploadTest(RecordTestBase):
    def test_upload_moves_outbox_to_storage(self):
        helpers.write_file(self.data, "a.txt")
        snap = self.record()
        conf = config.load_record_config(self.config_path)
        self.assertEqual(upload.upload_outbox(conf), 0)
        self.assertTrue(os.path.exists(os.path.join(self.storage, os.path.basename(snap["path"]))))
        self.assertEqual(upload.outbox_files(conf["state_dir"]), [])

    def test_failed_record_still_uploads_pending_snapshots(self):
        helpers.write_file(self.data, "app/a.php")
        self.record()                                   # 送信待ちが 1 つある状態
        self.write_config(include=[self.data + "/app/**", self.data + "/nothere/**"])
        with redirect_stderr(io.StringIO()):
            code = cli.main(["record", "--config", self.config_path])
        self.assertEqual(code, 1)                       # 記録は失敗
        conf = config.load_record_config(self.config_path)
        self.assertEqual(upload.outbox_files(conf["state_dir"]), [])   # 前のものは送られた

    def test_failed_upload_is_kept_and_retried(self):
        helpers.write_file(self.data, "a.txt")
        self.record()
        self.record()
        conf = config.load_record_config(self.config_path)
        conf["upload"]["rclone_options"] = ["--no-such-option"]
        self.assertEqual(upload.upload_outbox(conf), 2)
        self.assertEqual(len(upload.outbox_files(conf["state_dir"])), 2)
        conf["upload"]["rclone_options"] = []
        self.assertEqual(upload.upload_outbox(conf), 0)
        self.assertEqual(upload.outbox_files(conf["state_dir"]), [])


if __name__ == "__main__":
    unittest.main()
