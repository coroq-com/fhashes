"""テスト用の共通処理。"""

import os
import shutil
import stat
import sys
import tempfile
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

TEST_ROOT = os.environ.get("FHASHES_TEST_DIR", os.path.join(tempfile.gettempdir(), "fhashes-test"))


def find_rclone():
    """rclone のパス。見つからなければ None（そのテストはスキップする）。"""
    # ~/.local/bin は、rclone を管理者権限なしで入れたときのよくある置き場所（PATH に入っていないことがある）
    for candidate in [os.environ.get("FHASHES_TEST_RCLONE"), shutil.which("rclone"),
                      os.path.expanduser("~/.local/bin/rclone")]:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


class TimeZone:
    """テストの間だけ、実行環境のタイムゾーン（TZ）を変える。

    使い方: with helpers.TimeZone("Asia/Tokyo"): ...   または setUp / tearDown で enter() / exit()
    """

    def __init__(self, name: str):
        self.name = name
        self.saved = None

    def enter(self):
        self.saved = os.environ.get("TZ")
        os.environ["TZ"] = self.name
        time.tzset()

    def exit(self):
        if self.saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.saved
        time.tzset()

    def __enter__(self):
        self.enter()
        return self

    def __exit__(self, *args):
        self.exit()


def make_temp_dir(prefix: str) -> str:
    os.makedirs(TEST_ROOT, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=TEST_ROOT)


def remove_temp_dir(path: str) -> None:
    """chmod 0 にしたディレクトリがあっても消せるように、権限を戻してから消す。"""
    for root, dirs, files in os.walk(path):
        for d in dirs:
            full = os.path.join(root, d)
            if not os.path.islink(full):
                os.chmod(full, stat.S_IRWXU)
    shutil.rmtree(path)


def write_file(base: str, rel: str, content: str = "x") -> str:
    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return path
