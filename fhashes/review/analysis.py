"""指定期間のスナップショットを取ってきて検証し、隣り合うスナップショットを比べて変化を見つける。

何も保存しない。コマンドを実行するたびに、ストレージの一覧とスナップショット（キャッシュ）から計算する。
判定の規則は DESIGN.md §7.3 を参照。

用語:
  prev / cur  : 同じホストの、隣り合う 2 つのスナップショット（前・後）
  base        : そのファイルの「最後に確認できた状態」。ふつうは prev の行。
                prev で読み取りエラーだった場合は still_error（エラーになる前の状態）を使う。
  still_error : 読み取りエラーのままのファイル。エラーになる前の最後に分かっている状態を覚えておく
"""

import os

from fhashes import snapshot
from fhashes.patterns import Scope
from fhashes.progress import Progress
from fhashes.review import storage


# ----------------------------------------------------------------------
# 期間に必要なスナップショットを選ぶ
# ----------------------------------------------------------------------

def select_range(entries: list, since: str, until: str) -> list:
    """期間 [since, until) の変化を調べるのに必要なスナップショットを選ぶ。

    「期間の開始以前に始まった最後のスナップショット」の 1 つ前から、
    「期間の終了以後に始まった最初のスナップショット」までを選ぶ。
    （1 つ前まで含めるのは、開始直前のスナップショットが期間の開始後に終わっている場合に、
      その前との間の変化（変更期間は後のスナップショットの終了まで）も期間と重なり得るため）
    """
    start = 0
    for index, entry in enumerate(entries):
        if entry["name_time"] <= since:
            start = max(index - 1, 0)
    end = len(entries) - 1
    for index, entry in enumerate(entries):
        if entry["name_time"] >= until:
            end = index
            break
    return entries[start:end + 1]


# ----------------------------------------------------------------------
# 1 ホスト分の解析
# ----------------------------------------------------------------------

def analyze_host(conf: dict, entries: list, show_progress: bool = True) -> dict:
    """entries（古い順）のスナップショットを検証し、隣り合うものを比べる。

    返り値:
      snapshots : 各スナップショットの情報（dict のリスト。不正なものも含む）
      changes   : 見つかった変化（dict のリスト）
      still_error_count : 最後の時点でエラーのままのファイルの数
    """
    storage.download(conf, entries, show_progress)
    progress = Progress("記録の検証", len(entries) if show_progress else 0)
    try:
        return check_and_compare(conf, entries, progress)
    finally:
        progress.close()


def check_and_compare(conf: dict, entries: list, progress: Progress) -> dict:
    snapshots = []
    changes = []
    still_error = {}
    prev = None
    for index, entry in enumerate(entries, 1):
        progress.update(index)
        info = load_snapshot(conf, entry, prev)
        snapshots.append(info)
        if info["problem"] is not None:
            continue
        if prev is not None:
            found = []
            saved = dict(still_error)
            try:
                compare(prev, info, still_error, found)
            except (snapshot.SnapshotFormatError, KeyError, TypeError, ValueError) as e:
                # ヘッダーと終端は正しいが、中身が壊れていた（書き換えられた可能性もある）。
                # このスナップショットは比較に使わない
                info["problem"] = "中身が壊れています: " + type(e).__name__ + ": " + str(e)
                still_error.clear()
                still_error.update(saved)
                continue
            changes.extend(found)
        prev = info
    return {"snapshots": snapshots, "changes": changes, "still_error_count": len(still_error)}


def load_snapshot(conf: dict, entry: dict, prev) -> dict:
    """スナップショットのヘッダーと終端を読んで検証する。"""
    info = {
        "host": entry["host"],
        "seq": entry["seq"],
        "name": entry["name"],
        "local_path": storage.cache_path(conf, entry),
        "problem": None,
        "chain": None,
        "scope_changed": False,
    }
    try:
        summary = verify_file(info["local_path"])
    except OSError as e:
        info["problem"] = str(e)
        return info
    if summary["problem"] is not None:
        info["problem"] = summary["problem"]
        return info
    header = summary["header"]
    end = summary["end"]
    if header["host"] != entry["host"] or header["seq"] != entry["seq"]:
        info["problem"] = "ファイル名とヘッダーのホスト名・通し番号が合いません"
        return info

    info.update({
        "started_at": header["started_at"],
        "finished_at": end["finished_at"],
        "files": end["files"],
        "errors": end["errors"],
        "scope_sha256": header["scope_sha256"],
        "content_sha256": end["content_sha256"],
        "scope": {"include": header["include"], "exclude": header["exclude"]},
        "sha256": summary["sha256"],
    })
    info["chain"] = check_chain(prev, header)
    # 欠けている記録の数（gap のとき）。表示では通し番号を見せず、件数で伝える
    info["missing"] = header["seq"] - prev["seq"] - 1 if info["chain"] == "gap" else 0
    info["scope_changed"] = prev is not None and prev["scope_sha256"] != header["scope_sha256"]
    return info


def verify_file(local_path: str) -> dict:
    """記録のファイルを検証する。前に検証した結果が使えれば、それを使う（展開しなくて済む）。

    返り値: {"header", "end", "sha256", "problem"}。不正なら header / end は None で、problem に理由。
    """
    st = os.stat(local_path)
    result = storage.load_summary(local_path, st)
    if result is not None:
        return result
    try:
        summary = snapshot.read_summary(local_path)
        result = {"header": summary["header"], "end": summary["end"], "problem": None}
    except snapshot.SnapshotFormatError as e:
        result = {"header": None, "end": None, "problem": str(e)}
    result["sha256"] = snapshot.file_sha256(local_path)
    storage.save_summary(local_path, st, result)
    return result


def check_chain(prev, header: dict) -> str:
    """ハッシュチェーンを確かめる。prev は直前の正常なスナップショット（なければ None）。

    first   : 調べる範囲の最初のスナップショット（それより前とのつながりは見ていない）
    ok      : 直前のスナップショットのハッシュと一致する
    restart : 状態ファイルが失われた後の最初のスナップショット（prev_snapshot_sha256 が空）
    gap     : 直前のスナップショットとの間に、届いていないスナップショットがある（通し番号が飛んでいる）
    broken  : 直前のスナップショットのハッシュと一致しない（書き換え・すり替え・状態ファイルの巻き戻しの疑い）
    """
    if prev is None:
        return "first"
    if header.get("prev_snapshot_sha256") is None:
        return "restart"
    if header["prev_snapshot_sha256"] == prev["sha256"]:
        return "ok"
    if header["seq"] > prev["seq"] + 1:
        return "gap"
    return "broken"


# ----------------------------------------------------------------------
# 2 つのスナップショットの比較
# ----------------------------------------------------------------------

def is_ok(row) -> bool:
    return row is not None and "hash" in row


def under_error_dir(key: bytes, error_dirs: set) -> bool:
    """key が、読めなかったディレクトリの配下にあるか。"""
    parts = key.split(b"/")
    for i in range(2, len(parts)):
        if b"/".join(parts[:i]) in error_dirs:
            return True
    return False


def compare(prev: dict, cur: dict, still_error: dict, changes: list) -> None:
    """prev と cur を比べて、変化を changes に追加する。still_error も更新する。"""
    if prev["content_sha256"] == cur["content_sha256"] and prev["scope_sha256"] == cur["scope_sha256"]:
        return  # 中身がまったく同じ

    ctx = {
        "prev": prev,
        "cur": cur,
        "still_error": still_error,
        "changes": changes,
        "visited_still_error": set(),
        "error_dirs": set(),
        "scope_changed": prev["scope_sha256"] != cur["scope_sha256"],
        "prev_scope": Scope(prev["scope"]["include"], prev["scope"]["exclude"]),
        "cur_scope": Scope(cur["scope"]["include"], cur["scope"]["exclude"]),
    }

    # 2 つのスナップショットはどちらもパスの順に並んでいるので、先頭から同時に読み進めて突き合わせる
    prev_rows = snapshot.iter_rows(prev["local_path"])
    cur_rows = snapshot.iter_rows(cur["local_path"])
    p = next(prev_rows, None)
    c = next(cur_rows, None)
    while p is not None or c is not None:
        p_key = snapshot.row_key(p) if p is not None else None
        c_key = snapshot.row_key(c) if c is not None else None
        if p is None or (c is not None and c_key < p_key):
            handle_path(ctx, c_key, None, c)
            c = next(cur_rows, None)
        elif c is None or p_key < c_key:
            handle_path(ctx, p_key, p, None)
            p = next(prev_rows, None)
        else:
            handle_path(ctx, p_key, p, c)
            p = next(prev_rows, None)
            c = next(cur_rows, None)

    # どちらのスナップショットにも出てこなかったエラーのまま（読めないディレクトリの配下にあったもの）
    for key in list(still_error.keys()):
        if key in ctx["visited_still_error"]:
            continue
        if under_error_dir(key, ctx["error_dirs"]):
            continue  # まだ確認できない
        item = still_error.pop(key)
        if ctx["scope_changed"] and not ctx["cur_scope"].contains(os.fsdecode(key)):
            continue  # 監視範囲から外れた
        add_change(ctx, item["path"], "deleted", item["kind"], None, item["hash"], None,
                   item["seen_at"], cur["finished_at"])


def handle_path(ctx: dict, key: bytes, p, c) -> None:
    """1 つのパスについて、前（p）と後（c）を比べる。p・c はスナップショットの行（なければ None）。"""
    prev = ctx["prev"]
    cur = ctx["cur"]
    still_error = ctx["still_error"]
    if key in still_error:
        ctx["visited_still_error"].add(key)

    # 読めなかったディレクトリの行は、ファイルの状態ではない
    if c is not None and c["kind"] == "dir":
        ctx["error_dirs"].add(key)
        c = None
        if p is None:
            return
    if p is not None and p["kind"] == "dir":
        p = None
        if c is None:
            return

    display = (c or p)["path"]
    path = os.fsdecode(key)

    # base: 最後に確認できた状態
    if is_ok(p):
        base = {"kind": p["kind"], "hash": p["hash"], "seen_at": prev["started_at"], "path": p["path"]}
    else:
        base = still_error.get(key)

    if is_ok(c):
        still_error.pop(key, None)
        if base is None:
            if p is not None:
                return  # 前は読めず、それ以前の状態も分からない（現れたときに added を記録済み）
            if ctx["scope_changed"] and not ctx["prev_scope"].contains(path):
                return  # 監視範囲に入った
            add_change(ctx, display, "added", None, c["kind"], None, c["hash"],
                       prev["started_at"], cur["finished_at"])
        elif base["kind"] != c["kind"] or base["hash"] != c["hash"]:
            add_change(ctx, display, "modified", base["kind"], c["kind"], base["hash"], c["hash"],
                       base["seen_at"], cur["finished_at"])
        return

    if c is not None:
        # 今回は読めなかった
        if is_ok(p):
            remember_still_error(ctx, key, base)  # 読めなくなる前の状態を覚えておく
        elif p is None and key not in still_error:
            if ctx["scope_changed"] and not ctx["prev_scope"].contains(path):
                return
            add_change(ctx, display, "added", None, c["kind"], None, None,
                       prev["started_at"], cur["finished_at"])
        return

    # 今回のスナップショットにない
    if under_error_dir(key, ctx["error_dirs"]):
        if is_ok(p):
            remember_still_error(ctx, key, base)  # 読めないディレクトリの配下。まだ確認できない
        return
    still_error.pop(key, None)
    if ctx["scope_changed"] and not ctx["cur_scope"].contains(path):
        return  # 監視範囲から外れた
    if base is not None:
        add_change(ctx, display, "deleted", base["kind"], None, base["hash"], None,
                   base["seen_at"], cur["finished_at"])
    elif p is not None:
        # 読めないまま消えた
        add_change(ctx, display, "deleted", p["kind"], None, None, None,
                   prev["started_at"], cur["finished_at"])


def remember_still_error(ctx: dict, key: bytes, base: dict) -> None:
    ctx["still_error"][key] = base
    ctx["visited_still_error"].add(key)  # この回に扱い済み（比較の最後の後始末の対象にしない）


def add_change(ctx: dict, path: str, change_type: str, old_kind, new_kind, old_hash, new_hash,
               changed_after: str, changed_before: str) -> None:
    ctx["changes"].append({
        "host": ctx["cur"]["host"],
        "path": path,
        "type": change_type,
        "old_kind": old_kind,
        "new_kind": new_kind,
        "old_hash": old_hash,
        "new_hash": new_hash,
        "changed_after": changed_after,
        "changed_before": changed_before,
    })
