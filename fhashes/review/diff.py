"""diff: 期間の最初と最後の状態の差を、場所の木で表示する（git diff A B に当たる）。

log が「いつ何が変わったか」を時刻の順に出すのに対し、diff は「最初と最後で何が違うか」を
場所ごとに 1 ファイル 1 行で出す。システムの構造を知っている人が眺めて、見慣れない名前や、
変化するはずのない場所の変化に気づくためのもの。ファイル名は丸めずに全部出す。

印（A D M T は git と同じ）:
  A 追加       最初はなく、最後はある
  D 削除       最初はあり、最後はない
  M 変更       最初と最後で中身が違う
  T 種別変更   通常のファイル ⇔ シンボリックリンク
  = 変化後復元 最初と最後が同じだが、途中で変化があった
  ~ 追加後削除 最初も最後もないが、途中であった
  ? 状態不明   最初か最後の状態が分からない（読み取りエラー）
記号の印は「普通の追加・変更・削除ではない」ことを表すだけで、怪しいかどうかは見る人が判断する。
"""

from fhashes import timeutil
from fhashes.review import period, tree

MARKS = (
    ("A", "追加"),
    ("D", "削除"),
    ("M", "変更"),
    ("T", "種別変更"),
    ("=", "変化後復元"),
    ("~", "追加後削除"),
    ("?", "状態不明"),
)
# 印ごとの件数の表の段。1 段目は git と同じ普通の変化、2 段目は普通でない変化
LEGEND_ROWS = (("A", "D", "M", "T"), ("=", "~", "?"))


def net_changes(changes: list, still_error: list) -> list:
    """変化の一覧から、ファイルごとに最初と最後の状態を比べた差を作る。

    返り値: {"path", "mark", "changed_after", "changed_before", "count"} のリスト（パスの順）。
    changed_after / changed_before は、最初の変化の始まりと最後の変化の終わり。
    still_error は、期間の終わりでも読めないままのファイル（最後の状態が分からない）。
    """
    by_path = {}
    for change in sorted(changes, key=lambda ch: ch["changed_before"]):
        by_path.setdefault(change["path"], []).append(change)
    unreadable = {item["path"]: item for item in still_error}

    result = []
    for path in sorted(set(by_path) | set(unreadable)):
        history = by_path.get(path, [])
        times = [(ch["changed_after"], ch["changed_before"]) for ch in history]
        if path in unreadable:
            item = unreadable[path]
            times.append((item["changed_after"], item["changed_before"]))
            mark = "?"
        else:
            mark = net_mark(history[0], history[-1])
        result.append({
            "path": path,
            "mark": mark,
            "changed_after": min(t[0] for t in times),
            "changed_before": max(t[1] for t in times),
            "count": len(times),
        })
    return result


def net_mark(first: dict, last: dict) -> str:
    """最初の変化の前の状態と、最後の変化の後の状態を比べた印。"""
    start = None if first["type"] == "added" else (first["old_kind"], first["old_hash"])
    end = None if last["type"] == "deleted" else (last["new_kind"], last["new_hash"])
    if start is None and end is None:
        return "~"
    if start is None:
        return "?" if end[1] is None else "A"      # 追加されたが、中身が読めない
    if end is None:
        return "D"
    if start[1] is None or end[1] is None:
        return "?"                                 # どちらかの中身が分からない
    if start[0] != end[0]:
        return "T"
    return "M" if start[1] != end[1] else "="


def item_period(item: dict) -> str:
    """右に添える時期: 最初の変化の始まり - 最後の変化の終わり。何度も変わったら回数も。"""
    text = timeutil.format_local_range(item["changed_after"], item["changed_before"])
    if item["count"] > 1:
        text += "（%d 回）" % item["count"]
    return text


def print_diff(items: list, roots: list) -> None:
    """差分を出す。roots（監視範囲の起点）ごとに木を分け、起点のディレクトリを根にする。"""
    if not items:
        print("差分なし")
    else:
        tree.print_lines(tree.render(items, roots, item_period))
    print()
    tree.print_legend(items, MARKS, LEGEND_ROWS)


def cmd_diff(conf: dict, args) -> int:
    """1 台のホストについて、期間の最初と最後の状態の差を場所の木で表示する。

    監視の状況は log と同じものを出し、問題があれば終了コード 1。
    """
    keep_path = period.path_filter(args.path, args.not_path)
    marks = tree.mark_filter(args.type, args.not_type, MARKS)
    examined = period.open_period(conf, args)
    if examined is None:
        return 1
    items = net_changes(examined.changes(keep_path), examined.still_error(keep_path))

    examined.print_header()
    print("[差分]")
    print_diff([item for item in items if item["mark"] in marks], examined.roots())
    print()
    return examined.print_monitoring()
