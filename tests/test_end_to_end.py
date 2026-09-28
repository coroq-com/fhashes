"""記録する側が作ったスナップショットを、調べる側が読めることのテスト（両側の形式のずれを防ぐ）。

ストレージはローカルのディレクトリで、送信・一覧・ダウンロードに rclone を使う（見つからなければスキップ）。
"""

import io
import os
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402

from fhashes import cli  # noqa: E402

RCLONE = helpers.find_rclone()


@unittest.skipUnless(RCLONE, "rclone が見つかりません")
class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.base = helpers.make_temp_dir("e2e-")
        self.data = os.path.join(self.base, "data")
        os.makedirs(self.data)
        storage = os.path.join(self.base, "storage")
        self.record_config = os.path.join(self.base, "record.yaml")
        with open(self.record_config, "w") as f:
            f.write("host: web1\nstate_dir: state\ninclude:\n  - %s/**\n"
                    "upload:\n  rclone: %s\n  remote: %s\n" % (self.data, RCLONE, storage))
        self.review_config = os.path.join(self.base, "review.yaml")
        with open(self.review_config, "w") as f:
            f.write("cache_dir: cache\nrclone: %s\nhosts:\n  web1: %s\n"
                    % (RCLONE, storage))

    def tearDown(self):
        helpers.remove_temp_dir(self.base)

    def run_cli(self, *args) -> tuple:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(list(args))
        return code, out.getvalue()

    def test_record_then_review(self):
        helpers.write_file(self.data, "index.php", "1")
        helpers.write_file(self.data, "old.php", "1")
        self.assertEqual(self.run_cli("record", "--config", self.record_config)[0], 0)
        time.sleep(1.1)  # ファイル名の時刻は秒まで
        helpers.write_file(self.data, "index.php", "2")
        os.remove(os.path.join(self.data, "old.php"))
        helpers.write_file(self.data, "new.php", "1")
        self.assertEqual(self.run_cli("record", "--config", self.record_config)[0], 0)

        code, out = self.run_cli("review", "--config", self.review_config, "web1",
                                 "--from", "2000-01-01", "--to", "2999-01-01", "--format", "csv")
        lines = sorted(line.split(",")[2] + " " + os.path.basename(line.split(",")[1])
                       for line in out.strip().splitlines()[1:])
        self.assertEqual(lines, ["added new.php", "deleted old.php", "modified index.php"])

        code, out = self.run_cli("status", "--config", self.review_config)
        self.assertEqual(code, 0, out)
        self.assertIn("web1", out)


if __name__ == "__main__":
    unittest.main()
