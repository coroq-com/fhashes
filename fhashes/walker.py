"""監視範囲のファイルの走査と、ハッシュ計算。

走査は深さ優先・名前順。見つけたものを 1 つずつコールバックに渡すだけで、結果をため込まない。
"""

import errno
import hashlib
import os
import stat
from typing import Optional

from fhashes.patterns import Scope

HASH_ALGO = "sha256"
READ_CHUNK_SIZE = 1024 * 1024


class MissingRootError(Exception):
    """ディレクトリを表す走査の起点が存在しない（設定の間違いやマウント外れの可能性がある）。"""
    pass


# ----------------------------------------------------------------------
# 走査
# ----------------------------------------------------------------------

def join_path(parent: str, name: str) -> str:
    if parent == "/":
        return "/" + name
    return parent + "/" + name


def check_roots(scope: Scope) -> None:
    """ディレクトリを表す走査の起点がすべて存在するか確かめる。なければ MissingRootError を投げる。

    途中まで走査してから失敗しないように、走査を始める前に確かめる。
    1 つのファイルを指す起点（/etc/passwd など）は、なくてもよい（そのファイルが消えただけなので、削除として記録する）。
    """
    missing = []
    for root in scope.roots():
        if root["file_only"] or scope.is_excluded(root["path"]):
            continue
        try:
            os.stat(root["path"])            # リンクならリンク先まで確かめる
        except FileNotFoundError:
            missing.append(root["path"])     # 存在しない、またはリンク先が存在しない
        except OSError:
            pass                             # 権限がない等は、走査の中で error 行として記録する
    if missing:
        raise MissingRootError("監視範囲の起点がありません: " + ", ".join(missing))


def walk(scope: Scope, on_file, on_error) -> None:
    """監視範囲のファイルとシンボリックリンクを 1 つずつ見つけて、コールバックを呼ぶ。

    on_file(path, kind, st)    : kind は "file" か "link"。st は os.lstat の結果
    on_error(path, kind, error): 調べられなかったもの。kind は "file" / "link" / "dir"
    デバイス・FIFO・ソケットは無視する。マウントポイント（別のファイルシステム）にも入る
    （/proc などを避けたい場合は exclude に書く）。
    シンボリックリンクは、走査の起点だけたどる（途中のリンクはたどらず、リンク自体を記録する）。
    """
    for root in scope.roots():
        walk_root(scope, root, on_file, on_error)


def walk_root(scope: Scope, root: dict, on_file, on_error) -> None:
    """走査の起点 1 つ分。起点はファイルのこともある。"""
    path = root["path"]
    if scope.is_excluded(path):
        return
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        missing_root(root, on_error)
        return
    except OSError as e:
        on_error(path, "dir", e)  # 親ディレクトリに入れない等。配下をまとめて調べられなかったことにする
        return

    if stat.S_ISLNK(st.st_mode):
        if scope.contains(path):
            on_file(path, "link", st)
        # 起点がディレクトリへのリンク（デプロイの current など）なら、たどって中を走査する
        try:
            target = os.stat(path)
        except FileNotFoundError:
            missing_root(root, on_error)
            return
        except OSError as e:
            on_error(path, "dir", e)
            return
        if stat.S_ISDIR(target.st_mode):
            walk_directory(scope, path, on_file, on_error)
    elif stat.S_ISREG(st.st_mode):
        if scope.contains(path):
            on_file(path, "file", st)
    elif stat.S_ISDIR(st.st_mode):
        walk_directory(scope, path, on_file, on_error)


def missing_root(root: dict, on_error) -> None:
    """起点が存在しないとき。

    1 つのファイルを指すパターン（/etc/passwd など）なら、そのファイルが消えただけなので何もしない
    （スナップショットにないので、削除として扱われる）。
    ディレクトリを表す起点なら、記録全体を失敗にする（中途半端なスナップショットは作らない）。
    ふつうは走査の前に check_roots で見つかる。ここに来るのは、走査中に消えた場合。
    """
    if not root["file_only"]:
        raise MissingRootError("監視範囲の起点がありません: " + root["path"])


def walk_directory(scope: Scope, root: str, on_file, on_error) -> None:
    stack = [root]
    while stack:
        dir_path = stack.pop()
        try:
            with os.scandir(dir_path) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except FileNotFoundError:
            continue  # 走査中に消えた
        except OSError as e:
            on_error(dir_path, "dir", e)
            continue

        subdirs = []
        for entry in entries:
            path = join_path(dir_path, entry.name)
            try:
                # stat は entry.stat() ではなく os.lstat() で取る。entry.stat() は結果を entry に残すので、
                # ディレクトリ 1 つ分の一覧を処理し終えるまでメモリが解放されない（15 万ファイルで約 100MB）
                if entry.is_symlink():
                    if scope.contains(path):
                        on_file(path, "link", os.lstat(path))
                elif entry.is_dir(follow_symlinks=False):
                    if scope.should_descend(path):
                        subdirs.append(path)
                elif entry.is_file(follow_symlinks=False):
                    if scope.contains(path):
                        on_file(path, "file", os.lstat(path))
            except FileNotFoundError:
                continue  # 走査中に消えた
            except OSError as e:
                on_error(path, guess_kind(entry), e)

        # 名前順に処理したいので、逆順にスタックへ積む
        for subdir in reversed(subdirs):
            stack.append(subdir)


def guess_kind(entry) -> str:
    try:
        return "dir" if entry.is_dir(follow_symlinks=False) else "file"
    except OSError:
        return "file"


# ----------------------------------------------------------------------
# ハッシュ計算
# ----------------------------------------------------------------------

def open_for_read(path: str) -> int:
    """ファイルを読み取り用に開いてファイル記述子を返す。

    O_NOFOLLOW : 途中でシンボリックリンクにすり替わっていたら開かない
    O_NONBLOCK : 途中で FIFO にすり替わっていても固まらない
    O_NOATIME  : 最終アクセス日時を更新しない（ディスクへの書き込みを減らす）。
                 自分が所有者でないファイルでは使えないことがあるので、その場合は付けずに開き直す。
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    noatime = getattr(os, "O_NOATIME", 0)
    try:
        return os.open(path, flags | noatime)
    except PermissionError as e:
        if noatime and e.errno == errno.EPERM:
            return os.open(path, flags)
        raise


def hash_file(path: str, kind: str) -> Optional[str]:
    """ファイルの中身（リンクならリンク先のパス文字列）のハッシュを返す。

    消えていたら None。読めなければ OSError を投げる。
    """
    if kind == "link":
        try:
            target = os.readlink(path)
        except FileNotFoundError:
            return None
        return hashlib.new(HASH_ALGO, os.fsencode(target)).hexdigest()

    try:
        fd = open_for_read(path)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EAGAIN, "file type changed during scan")
        hasher = hashlib.new(HASH_ALGO)
        while True:
            chunk = os.read(fd, READ_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    finally:
        os.close(fd)
    return hasher.hexdigest()


def error_message(error: Exception) -> str:
    if isinstance(error, OSError) and error.strerror:
        return type(error).__name__ + ": " + error.strerror
    return type(error).__name__ + ": " + str(error)
