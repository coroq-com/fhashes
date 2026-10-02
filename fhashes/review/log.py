"""log: 期間内の変化の履歴を、時期ごとに場所の木で表示する（git log --name-status に当たる）。

時期は「前の記録の開始 - その記録の終了」で、変化したのはこの範囲のどこか。古い順に出す。
CSV / JSON では、変化を 1 行 1 件で、時刻を UTC で出す（機械で処理するため）。
"""

import csv
import json
import sys

from fhashes import timeutil
from fhashes.review import period, tree

# 1 回 1 回の変化の印（diff の = と ~ は、期間の最初と最後を比べたときだけのもの）。A D M T は git と同じ
MARKS = (
    ("A", "追加"),
    ("D", "削除"),
    ("M", "変更"),
    ("T", "種別変更"),
    ("?", "状態不明"),
)
LEGEND_ROWS = (("A", "D", "M", "T"), ("?",))

CSV_COLUMNS = ("host", "path", "type", "changed_after", "changed_before",
               "old_kind", "new_kind", "old_hash", "new_hash")


def change_mark(change: dict) -> str:
    """1 回の変化の印。中身が読めず比べられないものは ?。"""
    if change["type"] == "added":
        return "A" if change["new_hash"] is not None else "?"
    if change["type"] == "deleted":
        return "D"
    if change["old_kind"] != change["new_kind"]:
        return "T"
    if change["old_hash"] is None or change["new_hash"] is None:
        return "?"
    return "M"


def print_history(changes: list, roots: list) -> None:
    """変化の履歴を、時期ごとに場所の木で出す。時期のまとまりの後に空行を入れる。

    読み取りエラーの間に変わったものは時期の始まりが早いので、始まりと終わりの組み合わせごとに
    別のまとまりにする（同じ見出しにまとめると情報が落ちるため）。
    """
    if not changes:
        print("変化なし")
        print()
    groups = {}
    for change in changes:
        groups.setdefault((change["changed_before"], change["changed_after"]), []).append(change)
    items = []
    for before, after in sorted(groups):
        members = [{"path": ch["path"], "mark": change_mark(ch)} for ch in groups[(before, after)]]
        items += members
        print(timeutil.format_local_range(after, before))
        tree.print_lines(tree.render(members, roots))
        print()
    tree.print_legend(items, MARKS, LEGEND_ROWS)


def print_csv(changes: list) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow(CSV_COLUMNS)
    for change in changes:
        writer.writerow([change[column] for column in CSV_COLUMNS])


def print_json(changes: list) -> None:
    print(json.dumps([{column: change[column] for column in CSV_COLUMNS} for change in changes],
                     ensure_ascii=False, indent=2))


def cmd_log(conf: dict, args) -> int:
    """1 台のホストについて、期間内の変化の履歴と監視の状況を表示する。

    表の形式では [変化の履歴] [監視の状況] の順に出す（変化が多くても、監視の状況が最後に目に入るように）。
    CSV / JSON では変化だけを出す（監視の状況に問題があれば、標準エラー出力に書く）。
    監視の状況に問題がなければ 0、あれば 1 を返す。
    """
    keep_path = period.path_filter(args.path, args.not_path)
    marks = tree.mark_filter(args.type, args.not_type, MARKS)
    examined = period.open_period(conf, args)
    if examined is None:
        return 1
    changes = [ch for ch in examined.changes(keep_path, args.detected) if change_mark(ch) in marks]

    if args.format in ("csv", "json"):
        if args.format == "csv":
            print_csv(changes)
        else:
            print_json(changes)
        if examined.problems:
            print("監視の状況に確認が必要な点があります: " + "、".join(examined.problems), file=sys.stderr)
        return examined.exit_code()

    examined.print_header()
    print("[変化の履歴]")
    print_history(changes, examined.roots())
    print()
    return examined.print_monitoring()
