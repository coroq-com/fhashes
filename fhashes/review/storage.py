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
from fhashes.progress import Progress

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


def download(conf: dict, entries: list, show_progress: bool = True) -> None:
    """entries のスナップショットを手元に用意する。キャッシュにあって大きさが合うものは取り直さない。

    進み具合には、最初から全体の量を出す（全期間を調べると数 GB になることがあるので、
    中止するかどうかを判断できるように）。
    """
    missing = [e for e in entries if not is_cached(conf, e)]
    total_size = sum(e["size"] for e in missing)
    log.debug("記録 %d 件のうち、未取得 %d 件（%s）を取得します", len(entries), len(missing), size_text(total_size))
    progress = Progress("記録の取得", len(missing) if show_progress else 0)
    done = 0
    done_size = 0
    try:
        progress.update(0, size_text(0) + " / " + size_text(total_size), force=True)
        for start in range(0, len(missing), DOWNLOAD_BATCH):
            batch = missing[start:start + DOWNLOAD_BATCH]
            download_batch(conf, batch)
            done += len(batch)
            done_size += sum(e["size"] for e in batch)
            progress.update(done, size_text(done_size) + " / " + size_text(total_size))
    finally:
        progress.close()


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


# ----------------------------------------------------------------------
# 検証の結果のキャッシュ
#   記録を検証した結果（ヘッダー、終端、圧縮されたファイルのハッシュ、不正ならその理由）を、
#   キャッシュの記録の隣に <名前>.summary.json として残す。記録は書き換えられない（WORM）ので、
#   同じファイルなら結果は変わらない。2 回目以降は展開せずに済む。
#   使うのは、記録のファイルの大きさと更新時刻が保存したときと同じで、SUMMARY_VERSION も同じ場合だけ。
# ----------------------------------------------------------------------

SUMMARY_VERSION = 1  # 検証の仕方を変えたら上げる（古い結果を使わないため）


def summary_path(local_path: str) -> str:
    return local_path + ".summary.json"


def load_summary(local_path: str, st: os.stat_result):
    """保存した検証の結果を返す。使えなければ None。"""
    try:
        with open(summary_path(local_path), encoding="utf-8") as f:
            data = json.load(f)
        if (data["summary_version"] == SUMMARY_VERSION
                and data["size"] == st.st_size and data["mtime_ns"] == st.st_mtime_ns):
            return data["result"]
    except (OSError, ValueError, KeyError, TypeError):
        pass  # ない、壊れている → 検証し直す
    return None


def save_summary(local_path: str, st: os.stat_result, result: dict) -> None:
    """検証の結果を保存する。一時ファイルに書いてから名前を変える（途中で止まっても壊れた結果を残さない）。"""
    data = {"summary_version": SUMMARY_VERSION, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "result": result}
    path = summary_path(local_path)
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=os.path.dirname(path),
                                         suffix=".tmp", delete=False) as f:
            json.dump(data, f, ensure_ascii=False)
            tmp = f.name
        os.replace(tmp, path)
    except OSError as e:
        log.debug("検証の結果を保存できませんでした（次回また検証します）: %s: %s", path, e)


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
