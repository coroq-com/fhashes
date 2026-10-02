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


# ----------------------------------------------------------------------
# 調べる側のテスト用: 記録を直接作ってストレージ（ローカルのディレクトリ）に置く
# ----------------------------------------------------------------------

import io  # noqa: E402
import re  # noqa: E402
import unittest  # noqa: E402
from contextlib import redirect_stderr, redirect_stdout  # noqa: E402
from datetime import timedelta  # noqa: E402

from fhashes import cli, config, snapshot, timeutil  # noqa: E402

RCLONE = find_rclone()
SCOPE = {"include": ["/app/**"], "exclude": []}


def t(hour: int, minute: int = 0, second: int = 0) -> str:
    """テスト用の時刻（2026-09-01 の UTC）。"""
    return "2026-09-01T%02d:%02d:%02dZ" % (hour, minute, second)


def legend_counts(out: str) -> dict:
    """出力の印ごとの件数の表から、{印: 件数} を取り出す（表の形に頼らずに件数を確かめるため）。"""
    return {mark: int(count.replace(",", "")) for mark, count in re.findall(r"(?:^|    )(\S)  \S+ +([\d,]+)", out, re.M)}


@unittest.skipUnless(RCLONE, "rclone が見つかりません")
class StorageTestCase(unittest.TestCase):
    """調べる側のテストの土台。ストレージに記録を作り、調べる側の設定（web1 と db1）を用意する。"""

    def setUp(self):
        self.base = make_temp_dir("review-")
        self.storage = os.path.join(self.base, "storage")
        os.makedirs(self.storage)
        self.config_path = os.path.join(self.base, "review.yaml")
        with open(self.config_path, "w") as f:
            f.write("cache_dir: cache\n"
                    "rclone: %s\nhosts:\n  web1: %s\n  db1: %s\n" % (RCLONE, self.storage, self.storage))
        self.conf = config.load_review_config(self.config_path)
        self.last_sha = {}
        # 日時の入力と表示は実行環境のタイムゾーンに従うので、テストでは UTC に固定する
        self.timezone = TimeZone("UTC")
        self.timezone.enter()

    def tearDown(self):
        self.timezone.exit()
        remove_temp_dir(self.base)

    def make_snapshot(self, seq: int, started_at: str, rows: list, host="web1", scope=None,
                      prev_sha="auto", finished_after_seconds=60) -> str:
        """記録を 1 つストレージに作る。終了は開始の finished_after_seconds 秒後。

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
        writer = snapshot.SnapshotWriter(path, header)
        for row in sorted(rows, key=lambda r: r[0].encode()):
            if row[1] is not None:
                writer.write_file(row[0], "file", row[1])
            else:
                writer.write_error(row[0], row[2], "PermissionError: Permission denied")
        writer.close(timeutil.to_utc_text(timeutil.parse_utc(started_at) + timedelta(seconds=finished_after_seconds)))
        self.last_sha[host] = snapshot.file_sha256(path)
        return path

    def cli(self, *args) -> tuple:
        """コマンドを実行して (終了コード, 標準出力) を返す。"""
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main([args[0], "--config", self.config_path] + list(args[1:]))
        return code, out.getvalue()
