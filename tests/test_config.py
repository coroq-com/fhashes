"""調べる側の設定（config/review.yaml）の読み込みのテスト。記録する側の設定は test_record.py で確かめる。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402

from fhashes import config  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
