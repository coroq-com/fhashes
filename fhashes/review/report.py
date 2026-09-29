"""表示系のコマンド: status（ホストごとの状況）、review（1 台のホストの、期間を指定した見直し）、clean（キャッシュの削除）。"""

import csv
import json
import sys
import unicodedata
from datetime import timedelta

from fhashes import config, patterns, timeutil
from fhashes.progress import Progress
from fhashes.review import analysis, storage

CHANGE_TYPES = ("added", "modified", "deleted")
# ハッシュチェーンの異常（ok と first は異常ではないので表示しない）
# ハッシュチェーンで見つかる異常。表示の見出しは仕組み（ハッシュチェーン）ではなく、起きていることにする
CHAIN_PROBLEMS = {
    "gap": "記録の欠落",
    "broken": "記録の不一致",
    "restart": "記録のやり直し",
}
STATUS_RECENT_HOURS = 24    # status で記録の間隔を調べる範囲


# ----------------------------------------------------------------------
# 表の表示（日本語が混ざっても列がそろうように、表示幅で計算する）
# ----------------------------------------------------------------------

def display_width(text: str) -> int:
    width = 0
    for c in text:
        width += 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
    return width


def print_table(headers: list, rows: list, indent: str = "", right: tuple = ()) -> None:
    """表を出す。right は右にそろえる列の番号（数の列）。"""
    widths = [display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], display_width(cell))

    def format_row(cells):
        parts = []
        for i, cell in enumerate(cells):
            padding = " " * (widths[i] - display_width(cell))
            if i in right:
                parts.append(padding + cell)
            elif i == len(cells) - 1:
                parts.append(cell)  # 最後の列（パスなど）は右を埋めない
            else:
                parts.append(cell + padding)
        return indent + "  ".join(parts)

    print(format_row(headers))
    print(format_row(["-" * w for w in widths]))
    for row in rows:
        print(format_row(row))


# ----------------------------------------------------------------------
# 共通
# ----------------------------------------------------------------------

def duration_text(seconds: float) -> str:
    """秒数を読みやすい単位で表す（例: 45 秒、12 分、3.5 時間、6.3 日）。"""
    if seconds < 120:
        return "%.0f 秒" % seconds
    if seconds < 2 * 3600:
        return "%.0f 分" % (seconds / 60)
    if seconds < 2 * 86400:
        return "%.1f 時間" % (seconds / 3600)
    return "%.1f 日" % (seconds / 86400)


# ----------------------------------------------------------------------
# 記録の間隔
#   間隔は設定に持たず、「乱れ」とも判定しない（閾値に根拠がないため）。実際の記録の開始時刻から
#   数値を出し、人が判断する。空いた間の変更も、前後の記録を比べれば「その間のどこか」として見つかる。
# ----------------------------------------------------------------------

def interval_stats(start_times: list):
    """記録の開始時刻（古い順）から、間隔の中央値（秒）と、一番長い間隔の (始まり, 終わり) を求める。

    記録が 2 件未満なら None。平均ではなく中央値にするのは、大きな空きに引っ張られないため。
    最大を出すのは、一番長く空いたところでは、変更の時期を狭められないため。
    """
    if len(start_times) < 2:
        return None
    pairs = list(zip(start_times, start_times[1:]))
    intervals = sorted(timeutil.seconds_between(a, b) for a, b in pairs)
    middle = len(intervals) // 2
    median = intervals[middle] if len(intervals) % 2 else (intervals[middle - 1] + intervals[middle]) / 2
    longest = max(pairs, key=lambda pair: timeutil.seconds_between(pair[0], pair[1]))
    return median, longest


def interval_summary(start_times: list):
    """review の「記録の間隔」の表示。記録が 2 件未満なら None。"""
    stats = interval_stats(start_times)
    if stats is None:
        return None
    median, longest = stats
    return "中央値 %s、最大値 %s（%s - %s）" % (
        duration_text(median), duration_text(timeutil.seconds_between(longest[0], longest[1])),
        timeutil.format_local(longest[0]), timeutil.format_local(longest[1]))


# --from / --to を省いたときの範囲の端。時刻の文字列どうしの大小比較にだけ使う（表示しない）
OPEN_START = "0000-01-01T00:00:00Z"
OPEN_END = "9999-12-31T23:59:59Z"


def parse_period(args) -> tuple:
    """--from / --to を UTC の文字列にする。省かれた端は None。"""
    since = timeutil.parse_user_time(args.since) if args.since else None
    until = timeutil.parse_user_time(args.until, end_of_range=True) if args.until else None
    if since is not None and until is not None and since >= until:
        raise ValueError("--to は --from より後にしてください")
    return since, until


def analyze_host_period(conf: dict, host: str, since: str, until: str):
    """1 台のホストについて、期間に必要なスナップショットを取ってきて解析する。スナップショットがなければ None。

    返り値は analysis.analyze_host の結果に、ストレージの一覧（entries）を加えたもの。
    """
    entries = storage.list_snapshots(conf, host)
    if not entries:
        return None
    result = analysis.analyze_host(conf, analysis.select_range(entries, since, until))
    result["entries"] = entries
    return result


# ----------------------------------------------------------------------
# status
# ----------------------------------------------------------------------

def status_row(conf: dict, host: str, recent_from: str) -> tuple:
    """status の 1 行分。(行, 問題があるか) を返す。"""
    entries = storage.list_snapshots(conf, host)
    if not entries:
        return [host, "-", "-", "-", "-", "-", "記録なし"], True
    times = [e["name_time"] for e in entries]
    stats = interval_stats([t for t in times if t >= recent_from])
    result = analysis.analyze_host(conf, entries[-2:], show_progress=False)
    last = result["snapshots"][-1]
    notes = []
    if last["problem"] is not None:
        notes.append("最新の記録が不正")
    elif last["chain"] in CHAIN_PROBLEMS:
        notes.append(CHAIN_PROBLEMS[last["chain"]])
    elif last["errors"]:
        notes.append("読み取りエラー")
    row = [
        host,
        timeutil.format_local(entries[-1]["name_time"]),
        duration_text(stats[0]) if stats else "-",
        duration_text(timeutil.seconds_between(*stats[1])) if stats else "-",
        "{:,}".format(last["files"]),
        "{:,}".format(last["errors"]),
        "、".join(notes) if notes else "OK",
    ]
    return row, bool(notes)


def cmd_status(conf: dict, args) -> int:
    """ホストごとの最新の記録と、直近 24 時間の記録の間隔、異常を表示する。

    間隔はストレージの一覧（ファイル名の時刻）だけで調べる（判定はせず、中央値と最大を出す）。
    ハッシュチェーンと読み取りエラーは、最新の 2 つだけダウンロードして調べる。
    問題（記録がない・不正・記録の欠落などハッシュチェーンの異常・読み取りエラー）があれば終了コード 1。
    """
    now = timeutil.now_utc()
    recent_from = timeutil.to_utc_text(timeutil.parse_utc(now) - timedelta(hours=STATUS_RECENT_HOURS))
    table = []
    has_problem = False
    hosts = sorted(conf["hosts"])
    progress = Progress("ホストの確認", len(hosts))
    try:
        for index, host in enumerate(hosts):
            progress.update(index, host, force=True)
            row, problem = status_row(conf, host, recent_from)
            table.append(row)
            has_problem = has_problem or problem
    finally:
        progress.close()
    print_table(["ホスト", "最新の記録", "間隔の中央値", "間隔の最大値", "ファイル数", "エラー", "状況"], table,
                right=(4, 5))
    return 1 if has_problem else 0


# ----------------------------------------------------------------------
# 変化の絞り込みと表示
# ----------------------------------------------------------------------

def path_filter_regexes(path_patterns) -> list:
    """--path の値を正規表現にする。"/" で始まらなければ、どのディレクトリの下でもよいものとする。"""
    regexes = []
    for pattern in path_patterns or []:
        if not pattern.startswith("/"):
            pattern = "/**/" + pattern
        regexes.append(patterns.glob_to_regex(pattern))
    return regexes


def parse_types(text) -> list:
    if not text:
        return []
    types = []
    for t in text.split(","):
        t = t.strip()
        if t not in CHANGE_TYPES:
            raise ValueError("--type には " + ", ".join(CHANGE_TYPES) + " を指定してください")
        types.append(t)
    return types


def filter_changes(changes: list, since: str, until: str, detected: bool, path_regexes=None, types=None) -> list:
    result = []
    for change in changes:
        if detected:
            # 指定期間に検知したもの
            if not (since <= change["changed_before"] < until):
                continue
        else:
            # 変更された可能性のある期間が、指定期間と重なるもの（取りこぼしがない）
            if change["changed_before"] < since or change["changed_after"] >= until:
                continue
        if path_regexes and not any(r.match(change["path"]) for r in path_regexes):
            continue
        if types and change["type"] not in types:
            continue
        result.append(change)
    result.sort(key=lambda ch: (ch["changed_before"], ch["path"]))
    return result


def change_label(change: dict) -> str:
    """表示用の種別。種類が変わった場合は "modified(file→link)" のようにする。"""
    if change["type"] == "modified" and change["old_kind"] != change["new_kind"]:
        return "modified(" + change["old_kind"] + "→" + change["new_kind"] + ")"
    return change["type"]


def print_changes_table(changes: list) -> None:
    if not changes:
        print("変化なし")
        return
    table = []
    for change in changes:
        # 変化したのは、この時期（範囲）のどこか
        period = timeutil.format_local(change["changed_after"]) + " - " + timeutil.format_local(change["changed_before"])
        table.append([period, change_label(change), change["path"]])
    print_table(["時期", "変化", "パス"], table)
    print()
    print("{:,} 件".format(len(changes)))


def change_dict(change: dict) -> dict:
    """CSV / JSON 用。データとして受け渡すものなので、時刻は実行環境に左右されない UTC で出す。"""
    return {
        "host": change["host"],
        "path": change["path"],
        "type": change["type"],
        "changed_after": change["changed_after"],
        "changed_before": change["changed_before"],
        "old_kind": change["old_kind"],
        "new_kind": change["new_kind"],
        "old_hash": change["old_hash"],
        "new_hash": change["new_hash"],
    }


def print_changes_csv(changes: list) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow(["host", "path", "type", "changed_after", "changed_before",
                     "old_kind", "new_kind", "old_hash", "new_hash"])
    for change in changes:
        writer.writerow(list(change_dict(change).values()))


# ----------------------------------------------------------------------
# 監視の状況
# ----------------------------------------------------------------------

def monitoring_report(conf: dict, result: dict, since, until) -> tuple:
    """期間の監視の状況を調べる。(表示する行のリスト, 問題の名前のリスト) を返す。

    since / until は、--from / --to を省いた場合は None（最初のスナップショットから / 最新のものまで）。
    記号は付けない（「記録の欠落: 2 件」のように、値を見れば問題かどうか分かる）。
    終了コード 1 になる問題は problems に加える（監視範囲の変更など、注意だけのものは加えない）。
    """
    snapshots = result["snapshots"]
    valid = [s for s in snapshots if s["problem"] is None]
    lines = []
    problems = []

    # 期間の直前・直後のスナップショット。端を省いた場合は、最初・最新のスナップショットが端になる
    if since is None:
        if valid:
            lines.append("最初の記録: %s" % timeutil.format_local(valid[0]["started_at"]))
    else:
        before = None
        for snap in valid:
            if snap["finished_at"] <= since:
                before = snap
        if before is None:
            problems.append("期間の直前の記録なし")
            lines.append("期間の直前の記録: なし")
        else:
            lines.append("期間の直前の記録: %s" % timeutil.format_local(before["started_at"]))
    if until is None:
        if valid:
            last = valid[-1]
            lines.append("最新の記録: %s" % timeutil.format_local(last["started_at"]))
    else:
        after = None
        for snap in valid:
            if snap["started_at"] >= until:
                after = snap
                break
        if after is None:
            problems.append("期間の直後の記録なし")
            lines.append("期間の直後の記録: なし")
        else:
            lines.append("期間の直後の記録: %s" % timeutil.format_local(after["started_at"]))
    if not valid:
        problems.append("有効な記録なし")
        lines.append("有効な記録: なし")

    # 記録の間隔: 判定はせず、件数・中央値・最大（とその場所）を出す
    lines.append("記録の件数: {:,}".format(len(valid)))
    intervals = interval_summary([s["started_at"] for s in valid])
    if intervals is not None:
        lines.append("記録の間隔: " + intervals)

    # ハッシュチェーンの異常（欠落・不一致・やり直し）
    for chain, title in CHAIN_PROBLEMS.items():
        found = [(index, s) for index, s in enumerate(valid) if s["chain"] == chain]
        if not found:
            lines.append("%s: なし" % title)
            continue
        problems.append(title)
        for index, s in found:
            if chain == "gap":
                lines.append("%s: %s 件（%s - %s）" % (title, "{:,}".format(s["missing"]),
                             timeutil.format_local(valid[index - 1]["started_at"]), timeutil.format_local(s["started_at"])))
            else:
                lines.append("%s: %s" % (title, timeutil.format_local(s["started_at"])))

    invalid = [s for s in snapshots if s["problem"] is not None]
    if invalid:
        problems.append("不正な記録")
        for s in invalid:
            lines.append("不正な記録: %s: %s" % (s["name"], s["problem"]))
    else:
        lines.append("不正な記録: なし")

    scope_changes = [s for s in valid if s["scope_changed"]]
    if scope_changes:
        # ふつうは設定を変えた結果（意図したもの）なので、問題とはみなさない
        for s in scope_changes:
            lines.append("監視範囲の変更: %s" % timeutil.format_local(s["started_at"]))
    else:
        lines.append("監視範囲の変更: なし")

    with_errors = [s for s in valid if s["errors"]]
    if with_errors:
        # 読めない間は変化を見逃しうるので、問題とみなす（権限の設定などを直すべき状況）
        problems.append("読み取りエラー")
        worst = max(with_errors, key=lambda s: s["errors"])
        lines.append("読み取りエラー: 記録 {:,} 件、最大 {:,} ファイル（{}）".format(
            len(with_errors), worst["errors"], timeutil.format_local(worst["started_at"])))
        if result["still_error_count"]:
            lines.append("期間の終わりでもエラーのまま: {:,} ファイル".format(result["still_error_count"]))
    else:
        lines.append("読み取りエラー: なし")

    return lines, problems


# ----------------------------------------------------------------------
# review
# ----------------------------------------------------------------------

def cmd_review(conf: dict, args) -> int:
    """1 台のホストについて、期間内の変化と監視の状況を表示する。

    表の形式では「変化」「監視の状況」の順に出す（変化が多くても、監視の状況が最後に目に入るように）。
    CSV・JSON では変化だけを出す（監視の状況に問題があれば、標準エラー出力に書く）。
    監視の状況に問題（✗）がなければ 0、あれば 1 を返す。
    """
    host = args.host
    if host not in conf["hosts"]:
        raise config.ConfigError(host + " は設定（" + conf["config_path"] + "）の hosts にありません")
    since, until = parse_period(args)
    low = since or OPEN_START
    high = until or OPEN_END
    path_regexes = path_filter_regexes(args.path)
    types = parse_types(args.type)

    result = analyze_host_period(conf, host, low, high)
    if result is None:
        print("ストレージに " + host + " の記録がありません", file=sys.stderr)
        return 1
    lines, problems = monitoring_report(conf, result, since, until)
    changes = filter_changes(result["changes"], low, high, args.detected, path_regexes, types)

    if args.format in ("csv", "json"):
        if args.format == "csv":
            print_changes_csv(changes)
        else:
            print(json.dumps([change_dict(ch) for ch in changes], ensure_ascii=False, indent=2))
        if problems:
            print("監視の状況に確認が必要な点があります: " + "、".join(problems), file=sys.stderr)
        return 1 if problems else 0

    print("ホスト  : " + host)
    print("調査期間: %s - %s" % (timeutil.format_local(since) if since else "最初の記録",
                                  timeutil.format_local(until) if until else "最新の記録"))
    print()
    print("[変化の履歴]")
    print_changes_table(changes)
    print()
    print("[監視の状況]")
    for line in lines:
        print(line)
    return 1 if problems else 0


# ----------------------------------------------------------------------
# clean
# ----------------------------------------------------------------------

def cmd_clean(conf: dict, args) -> int:
    removed = storage.clean_cache(conf)
    print("キャッシュを消しました: %s（%.1f MB）" % (conf["cache_dir"], removed / 1e6))
    return 0
