"""スナップショットファイル（ストレージに置く正本）の読み書き。形式は DESIGN.md §5 を参照。

gzip で圧縮した NDJSON（1 行 1 つの JSON）。
  1 行目    : ヘッダー     {"fhashes_snapshot": 1, "host": ..., "seq": ..., ...}
  2 行目〜  : ファイル     {"path": ..., "kind": ..., "hash": ...}
                          読めなかったものは "hash" の代わりに "error"
  最後の行  : 終端         {"end": true, "finished_at": ..., "files": ..., "errors": ..., "content_sha256": ...}
ファイル行はパスの順（UTF-8 のバイト順）に並ぶ。
"""

import base64
import gzip
import hashlib
import json
import os
import re

from fhashes import timeutil

FORMAT_VERSION = 1
FILE_SUFFIX = ".ndjson.gz"
# ホスト名に使える文字。スナップショットのファイル名に入るので、この形に限る
HOST_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]*"
HOST_NAME_REGEX = re.compile("^" + HOST_NAME_PATTERN + "$")
NAME_REGEX = re.compile(r"^(?P<host>" + HOST_NAME_PATTERN + r")-(?P<time>\d{8}T\d{6}Z)-(?P<seq>\d{8,})"
                        + re.escape(FILE_SUFFIX) + "$")
REQUIRED_HEADER_KEYS = ("host", "seq", "prev_snapshot_sha256", "started_at", "scope_sha256", "include", "exclude")


class SnapshotFormatError(Exception):
    pass


# ----------------------------------------------------------------------
# 名前
# ----------------------------------------------------------------------

def file_name(host: str, started_at: str, seq: int) -> str:
    """スナップショットのファイル名。送り先（upload.remote）の直下にこの名前で置く。
    例: web1-20260924T010000Z-00000123.ndjson.gz

    ディレクトリは作らない（ホスト名も日付も）。名前にホスト名が入っているので、複数のホストを
    同じ場所に置いても区別できる。名前の先頭が「ホスト名-日時」で時刻の順に並ぶので、
    コンソールや CLI では名前の先頭（web1-20260924 など）で絞り込める。
    """
    return "%s-%s-%08d%s" % (host, timeutil.to_compact(started_at), seq, FILE_SUFFIX)


def parse_snapshot_name(path: str):
    """ファイル名からホスト名・開始時刻（秒まで）・通し番号を取り出す。スナップショットのファイル名でなければ None。"""
    match = NAME_REGEX.match(os.path.basename(path))
    if not match:
        return None
    try:
        started_at = timeutil.from_compact(match.group("time"))
    except ValueError:
        return None  # 形は合っているが、ありえない日時（13 月など）。スナップショットとして扱わない
    return {"host": match.group("host"), "started_at": started_at, "seq": int(match.group("seq"))}


# ----------------------------------------------------------------------
# ヘッダー
# ----------------------------------------------------------------------

def scope_sha256(include: list, exclude: list) -> str:
    """監視範囲（include / exclude）のハッシュ。監視範囲が変わったかどうかの判定に使う。"""
    text = json.dumps({"include": include, "exclude": exclude}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_header(host: str, seq: int, prev_snapshot_sha256, started_at: str, include: list, exclude: list,
                reread_cycle: int, algo: str, tool_version: str) -> dict:
    """スナップショットのヘッダー（1 行目）の中身を作る。"""
    return {
        "host": host,
        "seq": seq,
        "prev_snapshot_sha256": prev_snapshot_sha256,
        "started_at": started_at,
        "algo": algo,
        "tool_version": tool_version,
        "reread_cycle": reread_cycle,
        "scope_sha256": scope_sha256(include, exclude),
        "include": include,
        "exclude": exclude,
    }


# ----------------------------------------------------------------------
# パス（UTF-8 でないファイル名の扱い）
# ----------------------------------------------------------------------

def path_fields(path: str) -> dict:
    """ファイル行の "path"（と必要なら "path_b64"）を作る。

    Python ではファイル名の UTF-8 でないバイトは「サロゲート文字」になっている。
    その場合、"path" には "\\xff" のような表記を入れ、正確なバイト列を base64 で "path_b64" に入れる。
    """
    try:
        path.encode("utf-8")
        return {"path": path}
    except UnicodeEncodeError:
        raw = path.encode("utf-8", "surrogateescape")
        return {
            "path": raw.decode("utf-8", "backslashreplace"),
            "path_b64": base64.b64encode(raw).decode("ascii"),
        }


def row_key(row: dict) -> bytes:
    """ファイル行の並び順と同一性に使うキー（パスのバイト列）。"""
    if "path_b64" in row:
        return base64.b64decode(row["path_b64"])
    return row["path"].encode("utf-8")


def content_line(row: dict) -> bytes:
    """content_sha256 の計算に使う 1 行分。パス・種類・ハッシュ・エラーの有無だけを使う。"""
    return make_content_line(json_string(row.get("path_b64") or row["path"]), row["kind"],
                             row.get("hash"), "error" in row)


def make_content_line(path_json: str, kind: str, digest, has_error: bool) -> bytes:
    """例: "/var/www/a.php"<TAB>file<TAB>3b1a...<TAB>-  （パスは JSON の文字列表記なのでタブを含まない）"""
    return (path_json + "\t" + kind + "\t" + (digest or "") + "\t" + ("E" if has_error else "-") + "\n").encode("ascii")


def json_string(text: str) -> str:
    return json.dumps(text, ensure_ascii=True)


# ----------------------------------------------------------------------
# 書き込み
# ----------------------------------------------------------------------

class SnapshotWriter:
    """スナップショットファイルを 1 つ書く。

    使い方:
        writer = SnapshotWriter(path, header)
        writer.write_file(...) / writer.write_error(...)   # パスの順に呼ぶ
        writer.close(finished_at)
    """

    def __init__(self, path: str, header: dict):
        self.raw = open(path, "wb")
        self.file = gzip.open(self.raw, "wt", encoding="ascii", compresslevel=6)
        self.content_hasher = hashlib.sha256()
        self.file_count = 0
        self.error_count = 0
        first = {"fhashes_snapshot": FORMAT_VERSION}
        first.update(header)
        self.write_line(first)

    def write_line(self, record: dict) -> None:
        self.file.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")

    def write_row(self, path: str, kind: str, rest: str, digest, has_error: bool) -> None:
        """ファイル行を 1 行書く。

        10 万行以上を書くので速さのために、JSON は json.dumps で丸ごと作らず、文字列をつなげて作る。
        値のうち kind・hash は英数字だけなので、そのまま書いてよい。
        パスとエラーメッセージは json_string で JSON の文字列にする。
        """
        fields = path_fields(path)
        path_json = json_string(fields["path"])
        line = '{"path":' + path_json
        if "path_b64" in fields:
            line += ',"path_b64":"' + fields["path_b64"] + '"'
            path_json = json_string(fields["path_b64"])
        line += ',"kind":"' + kind + '"' + rest + "}\n"
        self.file.write(line)
        self.content_hasher.update(make_content_line(path_json, kind, digest, has_error))

    def write_file(self, path: str, kind: str, digest: str) -> None:
        rest = ',"hash":"%s"' % digest
        self.write_row(path, kind, rest, digest, False)
        self.file_count += 1

    def write_error(self, path: str, kind: str, message: str) -> None:
        rest = ',"error":%s' % json_string(message)
        self.write_row(path, kind, rest, None, True)
        self.error_count += 1

    def close(self, finished_at: str) -> dict:
        end = {
            "end": True,
            "finished_at": finished_at,
            "files": self.file_count,
            "errors": self.error_count,
            "content_sha256": self.content_hasher.hexdigest(),
        }
        self.write_line(end)
        self.file.close()  # gzip の終わりまで書く（self.raw は閉じない）
        # ディスクに確実に書いてから閉じる。この後で状態を更新するので、停電などで
        # 「状態は更新済みなのに、ファイルが途中まで」にならないようにする
        self.raw.flush()
        os.fsync(self.raw.fileno())
        self.raw.close()
        return end


def file_sha256(path: str) -> str:
    """ファイル（圧縮後）の SHA-256。ハッシュチェーンに使う。"""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


# ----------------------------------------------------------------------
# 読み込み
# ----------------------------------------------------------------------

SUMMARY_CHUNK = 1024 * 1024   # read_summary で一度に展開する大きさ
SUMMARY_TAIL = 64 * 1024      # 最後の行を探すために残しておく末尾の大きさ（終端の行はこれより十分短い）


def read_summary(path: str) -> dict:
    """ヘッダーと終端だけを読んで、形式を確かめる（ファイル行の JSON は解釈しない）。

    返り値: {"header": dict, "end": dict}。形式がおかしければ SnapshotFormatError。
    1 行ずつ読まずに、まとめて展開して改行の数を数える（変化がない記録も毎回検証するので、速さが効く）。
    覚えておくのは最初の行と末尾だけなので、メモリはファイル数に比例しない。
    """
    head = b""        # 最初の改行が見つかるまでの先頭
    first = None      # 最初の行（ヘッダー）
    tail = b""        # 末尾（最後の行を探すため）
    newlines = 0
    try:
        with gzip.open(path, "rb") as f:
            while True:
                chunk = f.read(SUMMARY_CHUNK)
                if not chunk:
                    break
                if first is None:
                    head += chunk
                    if b"\n" in head:
                        first = head[:head.index(b"\n")]
                        head = b""
                newlines += chunk.count(b"\n")
                tail = chunk[-SUMMARY_TAIL:] if len(chunk) >= SUMMARY_TAIL else (tail + chunk)[-SUMMARY_TAIL:]
    except (OSError, EOFError) as e:
        raise SnapshotFormatError("gzip として読めません: " + str(e))
    if first is None:
        first = head  # 改行が 1 つもない
    # 行数: 最後が改行で終わっていなければ、最後の行も 1 行と数える
    ends_with_newline = tail.endswith(b"\n")
    line_count = newlines + (0 if ends_with_newline or not tail else 1)
    if line_count < 2:
        raise SnapshotFormatError("行が足りません")
    body = tail[:-1] if ends_with_newline else tail
    last = body[body.rfind(b"\n") + 1:]
    header = parse_json_line(first, "ヘッダー")
    end = parse_json_line(last, "終端")
    check_header(header)
    check_end(end, line_count - 2)
    return {"header": header, "end": end}


def iter_rows(path: str):
    """ファイル行を 1 行ずつ dict で返す。最後に content_sha256 を確かめる。"""
    hasher = hashlib.sha256()
    end = None
    with gzip.open(path, "rb") as f:
        f.readline()  # ヘッダー
        for raw in f:
            record = parse_json_line(raw, "ファイル行")
            if record.get("end") is True:
                end = record
                break
            hasher.update(content_line(record))
            yield record
    if end is None:
        raise SnapshotFormatError("終端の行がありません")
    if hasher.hexdigest() != end.get("content_sha256"):
        raise SnapshotFormatError("content_sha256 が中身と合いません")


def parse_json_line(raw: bytes, what: str) -> dict:
    try:
        record = json.loads(raw)
    except ValueError:
        raise SnapshotFormatError(what + "が JSON として読めません")
    if not isinstance(record, dict):
        raise SnapshotFormatError(what + "が JSON のオブジェクトではありません")
    return record


def check_header(header: dict) -> None:
    """ヘッダーの項目がそろっていて、値の形が正しいか確かめる。

    書き換えられたスナップショットでも、調べる側が途中で止まらず「不正」と報告できるように、
    後で使う値はここで形まで確かめておく。
    """
    if header.get("fhashes_snapshot") != FORMAT_VERSION:
        raise SnapshotFormatError("対応していない形式のバージョンです: " + repr(header.get("fhashes_snapshot")))
    for key in REQUIRED_HEADER_KEYS:
        if key not in header:
            raise SnapshotFormatError("ヘッダーに " + key + " がありません")
    if not isinstance(header["host"], str) or not HOST_NAME_REGEX.match(header["host"]):
        raise SnapshotFormatError("ヘッダーの host が不正です")
    if not is_int(header["seq"]) or header["seq"] < 1:
        raise SnapshotFormatError("ヘッダーの seq が不正です")
    if header["prev_snapshot_sha256"] is not None and not is_sha256(header["prev_snapshot_sha256"]):
        raise SnapshotFormatError("ヘッダーの prev_snapshot_sha256 が不正です")
    if not is_sha256(header["scope_sha256"]):
        raise SnapshotFormatError("ヘッダーの scope_sha256 が不正です")
    check_time(header["started_at"], "ヘッダーの started_at")
    for key in ("include", "exclude"):
        if not isinstance(header[key], list) or not all(isinstance(p, str) for p in header[key]):
            raise SnapshotFormatError("ヘッダーの " + key + " が不正です")


def check_end(end: dict, row_count: int) -> None:
    """終端の行の値を確かめる。row_count はファイル行の数。"""
    if end.get("end") is not True:
        raise SnapshotFormatError("終端の行がありません（途中で切れている可能性があります）")
    if not is_int(end.get("files")) or not is_int(end.get("errors")):
        raise SnapshotFormatError("終端の files / errors が不正です")
    if end["files"] + end["errors"] != row_count:
        raise SnapshotFormatError("行数が終端の件数と合いません")
    if not is_sha256(end.get("content_sha256")):
        raise SnapshotFormatError("終端の content_sha256 が不正です")
    check_time(end.get("finished_at"), "終端の finished_at")


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_sha256(value) -> bool:
    return isinstance(value, str) and re.match(r"^[0-9a-f]{64}$", value) is not None


def check_time(value, what: str) -> None:
    try:
        timeutil.parse_utc(value)
    except (TypeError, ValueError):
        raise SnapshotFormatError(what + " が日時として読めません: " + repr(value))
