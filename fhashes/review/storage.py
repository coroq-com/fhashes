"""ストレージとのやり取り: スナップショットの一覧、ダウンロード（キャッシュ付き）、キャッシュの削除。

ダウンロードしたスナップショットは cache_dir/<ホスト名>/<ファイル名> に置き、次からはそれを使う。
ストレージは書き換えできない（WORM）前提なので、同じ名前のファイルの中身は変わらない。
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile

from fhashes import rclone, snapshot

log = logging.getLogger("fhashes")


class StorageError(Exception):
    pass


def run_rclone(command: list) -> bytes:
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        raise StorageError("rclone を実行できません: " + str(e))
    if result.returncode != 0:
        raise StorageError("rclone が失敗しました: " + result.stderr.decode("utf-8", "replace").strip()[-1000:])
    return result.stdout


# ----------------------------------------------------------------------
# 一覧
# ----------------------------------------------------------------------

def list_snapshots(conf: dict, host: str) -> list:
    """ストレージにある、そのホストのスナップショットの一覧を古い順に返す。

    ホストのスナップショットは、hosts に書いた場所の直下にある。ほかのホストのものが同じ場所に
    あってもよい（ファイル名のホスト名で区別する）。サブディレクトリの中は見ない。
    entry は次の dict:
      remote（hosts に書いた場所）, name（ファイル名）, size,
      host, seq, name_time（ファイル名の時刻。スナップショットの開始時刻を秒まで）
    並び順は「ファイル名の時刻 → 通し番号」。通し番号だけだと、状態ファイルが失われて 1 から
    やり直したときに順番が崩れるため。
    """
    remote = conf["hosts"][host]
    # 時刻は使わない（ストレージによっては、記録する側が送った時刻しか返らないため）
    command = rclone.base_command(conf) + ["lsjson", "--files-only", "--no-modtime", remote]
    try:
        items = json.loads(run_rclone(command) or b"[]")
    except StorageError as e:
        if "directory not found" in str(e):
            return []  # まだ 1 つも届いていない
        raise
    entries = []
    for item in items:
        parsed = snapshot.parse_snapshot_name(item["Path"])
        if parsed is None or parsed["host"] != host:
            continue  # スナップショット以外のファイルと、ほかのホストのものは無視する
        entries.append({
            "remote": remote,
            "name": item["Path"],
            "size": item.get("Size"),
            "host": parsed["host"],
            "seq": parsed["seq"],
            "name_time": parsed["started_at"],
        })
    entries.sort(key=lambda e: (e["name_time"], e["seq"], e["name"]))
    return entries


# ----------------------------------------------------------------------
# ダウンロード（キャッシュ付き）
# ----------------------------------------------------------------------

def cache_path(conf: dict, entry: dict) -> str:
    return os.path.join(conf["cache_dir"], entry["host"], entry["name"])


DOWNLOAD_BATCH = 200  # 1 回の rclone で取ってくる数（進み具合を出すため、まとめすぎない）


def size_text(size: int) -> str:
    if size >= 1e9:
        return "%.1fGB" % (size / 1e9)
    return "%.0fMB" % (size / 1e6)


def is_cached(conf: dict, entry: dict) -> bool:
    local = cache_path(conf, entry)
    return os.path.exists(local) and os.path.getsize(local) == entry["size"]


def download(conf: dict, entries: list) -> None:
    """entries のスナップショットを手元に用意する。キャッシュにあって大きさが合うものは取り直さない。

    始める前に量を出す（全期間を調べると数 GB になることがあるので、中止するかどうかを判断できるように）。
    """
    missing = [e for e in entries if not is_cached(conf, e)]
    log.info("スナップショット %d 個（%s。うち未取得 %d 個・%s）を使います。中止するなら Ctrl-C",
             len(entries), size_text(sum(e["size"] for e in entries)),
             len(missing), size_text(sum(e["size"] for e in missing)))
    done = 0
    for start in range(0, len(missing), DOWNLOAD_BATCH):
        batch = missing[start:start + DOWNLOAD_BATCH]
        download_batch(conf, batch)
        done += len(batch)
        if len(missing) > DOWNLOAD_BATCH:
            log.info("ダウンロード: %d / %d", done, len(missing))


def download_batch(conf: dict, batch: list) -> None:
    """同じホストのスナップショットを、1 回の rclone でまとめて取ってくる。"""
    by_location = {}
    for entry in batch:
        by_location.setdefault((entry["remote"], entry["host"]), []).append(entry)
    for (remote, host), entries in by_location.items():
        local_dir = os.path.join(conf["cache_dir"], host)
        os.makedirs(local_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("\n".join(e["name"] for e in entries) + "\n")
            list_path = f.name
        try:
            command = rclone.base_command(conf) + ["copy", "--files-from", list_path, "--no-traverse",
                                                   remote, local_dir]
            run_rclone(command)
        finally:
            os.remove(list_path)
        for entry in entries:
            if not os.path.exists(cache_path(conf, entry)):
                raise StorageError(remote + ": ダウンロードできませんでした: " + entry["name"])


def clean_cache(conf: dict) -> int:
    """キャッシュを消す。消したバイト数を返す。"""
    cache_dir = conf["cache_dir"]
    if not os.path.isdir(cache_dir):
        return 0
    total = 0
    for root, dirs, files in os.walk(cache_dir):
        for name in files:
            total += os.path.getsize(os.path.join(root, name))
    shutil.rmtree(cache_dir)
    return total
