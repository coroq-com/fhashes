"""rclone の決まりごと（fhashes/rclone.py）のテスト。rclone 自体は実行しない。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402,F401  （fhashes を import できるようにするため）

from fhashes import rclone  # noqa: E402


class RcloneTest(unittest.TestCase):
    def test_join_remote(self):
        self.assertEqual(rclone.join_remote("gcs:bucket/prefix", "web1/a.gz"), "gcs:bucket/prefix/web1/a.gz")
        self.assertEqual(rclone.join_remote("gcs:bucket/", "web1/a.gz"), "gcs:bucket/web1/a.gz")
        self.assertEqual(rclone.join_remote("gcs:", "web1/a.gz"), "gcs:web1/a.gz")
        self.assertEqual(rclone.join_remote("/local/dir", "web1/a.gz"), "/local/dir/web1/a.gz")

    def test_base_command(self):
        settings = {"rclone": "rclone", "rclone_config": None, "rclone_options": []}
        self.assertEqual(rclone.base_command(settings), ["rclone"])
        settings = {"rclone": "/opt/rclone", "rclone_config": "/x/rclone.conf", "rclone_options": ["--fast-list"]}
        self.assertEqual(rclone.base_command(settings), ["/opt/rclone", "--config", "/x/rclone.conf", "--fast-list"])


if __name__ == "__main__":
    unittest.main()
