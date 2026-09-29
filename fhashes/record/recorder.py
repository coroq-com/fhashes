"""記録（fhashes record）: 監視範囲のファイルを調べて、全ファイルの一覧をスナップショットに書く。

流れ（DESIGN.md §4.2）:
  1. ロックを取る（取れなければ、ほかの実行が動いているので何もせずに終わる）。
     ディレクトリを表す起点がなければ、ここで失敗する（スナップショットは作らない）
  2. 走査しながら、ハッシュを決めて一時テーブル current に入れる
       次のどれかに当てはまるファイルは、中身を読んでハッシュを計算する
         - 新しいファイル
         - 種類・ctime・サイズ・inode・デバイスのどれかが前回と違う
         - 今回が「読み直しの順番」のファイル（reread_group と通し番号で決まる）
       それ以外は前回のハッシュを使う
  3. current をパスの順に読んで、スナップショットを一時ファイルに書く
  4. 1 つのトランザクションで状態（state.sqlite3）を更新する
  5. スナップショットを outbox/ に移す
スナップショットを書き終える（ディスクに確実に書く）まで状態は変えない。途中で止まっても、次の実行が
普通に続きから行う。4 と 5 の間で止まった場合は、次の実行が tmp/ に残ったものを outbox/ へ移す。
"""

import fcntl
import hashlib
import logging
import os
import shutil
import sqlite3
import time

from fhashes import VERSION, snapshot, timeutil, walker
from fhashes.patterns import Scope

log = logging.getLogger("fhashes")

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path_key  BLOB PRIMARY KEY,   -- パスのバイト列
    kind      TEXT NOT NULL,      -- file / link
    hash      TEXT NOT NULL,
    dev       INTEGER NOT NULL,
    ino       INTEGER NOT NULL,
    size      INTEGER NOT NULL,
    ctime_ns  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,       -- next_seq / prev_snapshot_sha256
    value TEXT
);
"""

INSERT_BATCH_SIZE = 1000


class Skipped(Exception):
    """ほかの実行が動いているので、今回はスキップした。"""
    pass


# ----------------------------------------------------------------------
# 状態ファイル
# ----------------------------------------------------------------------

def open_state(state_dir: str) -> sqlite3.Connection:
    """状態ファイルを開く。壊れていたら別名で残して、新しく作り直す。

    状態ファイルは「ハッシュ計算を省くためのキャッシュ」と通し番号の管理だけなので、作り直しても
    次の実行で全ファイルを読み直すだけで困らない。通し番号とハッシュチェーンは途切れるが、
    調べる側で「状態ファイルが失われた（restart）」と報告されるので、何が起きたかは分かる。
    """
    path = os.path.join(state_dir, "state.sqlite3")
    try:
        return connect_state(path)
    except sqlite3.DatabaseError as e:
        broken = path + ".broken-" + timeutil.to_compact(timeutil.now_utc())
        log.warning("状態ファイルが壊れているので作り直します（壊れたものは %s に残します）: %s", broken, e)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.rename(path + suffix, broken + suffix)
        return connect_state(path)


def connect_state(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(STATE_SCHEMA)
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise sqlite3.DatabaseError("quick_check: " + result)
    except sqlite3.DatabaseError:
        conn.close()
        raise
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


# ----------------------------------------------------------------------
# ロック
# ----------------------------------------------------------------------

def acquire_lock(state_dir: str):
    """同時実行を防ぐロックを取る。取れなければ Skipped を投げる。

    flock はプロセスが終わると（異常終了でも）自動で外れるので、ロックが残り続けることはない。
    """
    lock_file = open(os.path.join(state_dir, "lock"), "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        raise Skipped()
    return lock_file


# ----------------------------------------------------------------------
# 読み直しの順番
# ----------------------------------------------------------------------

def reread_group(path_key: bytes, cycle: int) -> int:
    """そのファイルを読み直す組（0 〜 cycle-1）。パスのハッシュから決めるので、ファイルはどの組にもほぼ均等に散らばる。"""
    return int.from_bytes(hashlib.sha256(path_key).digest()[:4], "big") % cycle


def is_reread_turn(path_key: bytes, cycle: int, seq: int) -> bool:
    """今回（通し番号 seq）が、そのファイルを読み直す順番か。cycle 回の実行で必ず一度は回ってくる。"""
    return reread_group(path_key, cycle) == seq % cycle


# ----------------------------------------------------------------------
# 記録
# ----------------------------------------------------------------------

def same_stat(cached, kind: str, st) -> bool:
    """前回から、種類・ctime・サイズ・inode・デバイスが変わっていないか。"""
    return (cached["kind"] == kind
            and cached["ctime_ns"] == st.st_ctime_ns
            and cached["size"] == st.st_size
            and cached["ino"] == st.st_ino
            and cached["dev"] == st.st_dev)


def scan_into_current(conn: sqlite3.Connection, conf: dict, seq: int) -> dict:
    """走査して、一時テーブル current に全ファイルの結果を入れる。件数を返す。"""
    conn.execute("""
        CREATE TEMP TABLE current (
            path_key BLOB PRIMARY KEY,
            kind     TEXT NOT NULL,
            hash     TEXT,              -- 読めなかったものは NULL
            was_read INTEGER NOT NULL,  -- 1: 今回読んで計算した / 0: 前回の値を使った
            error    TEXT,
            dev INTEGER, ino INTEGER, size INTEGER, ctime_ns INTEGER
        )
    """)
    counts = {"files": 0, "read": 0, "errors": 0}
    cycle = conf["reread_cycle"]
    pending_rows = []  # まとめて INSERT するための一時置き場（1 行ずつより速い）

    def add_row(row: tuple) -> None:
        pending_rows.append(row)
        if len(pending_rows) >= INSERT_BATCH_SIZE:
            flush_rows()

    def flush_rows() -> None:
        conn.executemany("INSERT OR REPLACE INTO current VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", pending_rows)
        pending_rows.clear()

    def on_file(path, kind, st):
        key = os.fsencode(path)
        cached = conn.execute("SELECT * FROM files WHERE path_key = ?", (key,)).fetchone()
        if (cached is not None and same_stat(cached, kind, st)
                and not is_reread_turn(key, cycle, seq)):
            digest = cached["hash"]
            was_read = 0
        else:
            try:
                digest = walker.hash_file(path, kind)
            except OSError as e:
                on_error(path, kind, e)
                return
            if digest is None:
                return  # 走査中に消えた
            was_read = 1
            counts["read"] += 1
        add_row((key, kind, digest, was_read, None, st.st_dev, st.st_ino, st.st_size, st.st_ctime_ns))
        counts["files"] += 1

    def on_error(path, kind, error):
        message = walker.error_message(error)
        add_row((os.fsencode(path), kind, None, 0, message, None, None, None, None))
        counts["errors"] += 1
        log.warning("監視失敗: %s (%s): %s", path, kind, message)

    scope = Scope(conf["include"], conf["exclude"])
    conn.execute("BEGIN")
    walker.walk(scope, on_file, on_error)
    flush_rows()
    conn.execute("COMMIT")
    return counts


def write_snapshot(conn: sqlite3.Connection, path: str, header: dict) -> dict:
    """current をパスの順に読んで、スナップショットファイルを書く。終端の内容を返す。"""
    writer = snapshot.SnapshotWriter(path, header)
    for row in conn.execute("SELECT * FROM current ORDER BY path_key"):
        file_path = os.fsdecode(row["path_key"])
        if row["error"] is None:
            writer.write_file(file_path, row["kind"], row["hash"])
        else:
            writer.write_error(file_path, row["kind"], row["error"])
    return writer.close(timeutil.now_utc())


def save_state(conn: sqlite3.Connection, next_seq: int, snapshot_sha256: str) -> None:
    """状態を今回の結果で更新する（1 つのトランザクション）。"""
    conn.execute("BEGIN IMMEDIATE")
    # 今回見つからなかった・読めなかったファイルは消す（次回は読み直す）
    conn.execute("""
        DELETE FROM files
        WHERE path_key NOT IN (SELECT path_key FROM current WHERE error IS NULL)
    """)
    # 今回読んだファイルだけ書き込む（前回の値を使ったものは変わっていない）
    conn.execute("""
        INSERT OR REPLACE INTO files (path_key, kind, hash, dev, ino, size, ctime_ns)
        SELECT path_key, kind, hash, dev, ino, size, ctime_ns
        FROM current WHERE error IS NULL AND was_read = 1
    """)
    set_meta(conn, "next_seq", str(next_seq))
    set_meta(conn, "prev_snapshot_sha256", snapshot_sha256)
    conn.execute("COMMIT")


def fsync_dir(path: str) -> None:
    """ディレクトリの中身の変更（rename など）を、ディスクに確実に書く。"""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def recover_unmoved_snapshot(tmp_dir: str, outbox_dir: str, prev_snapshot_sha256) -> None:
    """状態を更新した後、outbox/ へ移す前に止まった回のスナップショットを outbox/ へ移す。

    状態には「そのスナップショットを書いた」と記録されている（prev_snapshot_sha256 がそのハッシュ）ので、
    tmp/ に同じハッシュのファイルがあれば、それが送られるべきもの。消すと、調べる側で欠番（gap）になる。
    """
    if prev_snapshot_sha256 is None or not os.path.isdir(tmp_dir):
        return
    for name in sorted(os.listdir(tmp_dir)):
        path = os.path.join(tmp_dir, name)
        if snapshot.parse_snapshot_name(name) is None or not os.path.isfile(path):
            continue
        if snapshot.file_sha256(path) == prev_snapshot_sha256:
            os.makedirs(outbox_dir, exist_ok=True)
            os.rename(path, os.path.join(outbox_dir, name))
            fsync_dir(outbox_dir)
            log.warning("前回、outbox に移す前に止まった記録を移しました: %s", name)
            return


def record(conf: dict) -> str:
    """1 回分の記録を行い、outbox に置いたスナップショットのパスを返す。"""
    state_dir = conf["state_dir"]
    os.makedirs(state_dir, exist_ok=True)
    lock = acquire_lock(state_dir)
    try:
        # 起点がない場合は、スナップショットを作らずに失敗する（状態ファイルも変えない）
        walker.check_roots(Scope(conf["include"], conf["exclude"]))

        conn = open_state(state_dir)
        try:
            tmp_dir = os.path.join(state_dir, "tmp")
            outbox_dir = os.path.join(state_dir, "outbox")
            recover_unmoved_snapshot(tmp_dir, outbox_dir, get_meta(conn, "prev_snapshot_sha256"))
            shutil.rmtree(tmp_dir, ignore_errors=True)  # 前回途中で止まったときの残り
            os.makedirs(tmp_dir)

            started_monotonic = time.monotonic()
            started_at = timeutil.now_utc()
            seq = int(get_meta(conn, "next_seq", "1"))
            log.info("記録開始: host=%s seq=%d", conf["host"], seq)

            counts = scan_into_current(conn, conf, seq)

            header = snapshot.make_header(
                host=conf["host"],
                seq=seq,
                prev_snapshot_sha256=get_meta(conn, "prev_snapshot_sha256"),
                started_at=started_at,
                include=conf["include"],
                exclude=conf["exclude"],
                reread_cycle=conf["reread_cycle"],
                algo=walker.HASH_ALGO,
                tool_version=VERSION,
            )
            name = snapshot.file_name(conf["host"], started_at, seq)
            tmp_path = os.path.join(tmp_dir, name)
            end = write_snapshot(conn, tmp_path, header)
            snapshot_sha256 = snapshot.file_sha256(tmp_path)

            save_state(conn, seq + 1, snapshot_sha256)

            outbox_path = os.path.join(outbox_dir, name)
            os.makedirs(outbox_dir, exist_ok=True)
            os.rename(tmp_path, outbox_path)
            fsync_dir(outbox_dir)
        finally:
            conn.close()

        log.info("記録完了: files=%d errors=%d 読んだファイル=%d (%.1f 秒) -> %s",
                 end["files"], end["errors"], counts["read"],
                 time.monotonic() - started_monotonic, name)
        return outbox_path
    finally:
        lock.close()
