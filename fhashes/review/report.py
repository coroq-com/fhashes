"""表示系のコマンド: status（ホストごとの状況）、review（1 台のホストの、期間を指定した見直し）、clean（キャッシュの削除）。"""

import csv
import json
import sys
import unicodedata
from datetime import timedelta

from fhashes import config, patterns, timeutil
from fhashes.review import analysis, storage

CHANGE_TYPES = ("added", "modified", "deleted")
# ハッシュチェーンの異常（ok と first は異常ではないので表示しない）
CHAIN_PROBLEMS = {
    "restart": "状態ファイルが失われた後の最初のスナップショット",
    "gap": "直前のスナップショットが届いていない（通し番号が飛んでいる）",
    "broken": "直前のスナップショットとハッシュが合わない（書き換え・すり替え・巻き戻しの疑い）",
}
STATUS_RECENT_HOURS = 24    # status で間隔の乱れを調べる範囲
TYPICAL_SAMPLE = 48         # 普段の記録の間隔の推定に使う間隔の数（直近のもの）
MIN_INTERVALS = 3           # 普段の間隔を推定するのに必要な間隔の数
LONG_INTERVAL_RATIO = 1.5   # 普段の間隔のこの倍より空いたら「間隔の乱れ」とする


# ----------------------------------------------------------------------
# 表の表示（日本語が混ざっても列がそろうように、表示幅で計算する）
# ----------------------------------------------------------------------

def display_width(text: str) -> int:
    width = 0
    for c in text:
        width += 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
    return width


def print_table(headers: list, rows: list, indent: str = "") -> None:
    widths = [display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], display_width(cell))

    def format_row(cells):
        parts = []
        for i, cell in enumerate(cells):
            if i == len(cells) - 1:
                parts.append(cell)  # 最後の列（パスなど）は右を埋めない
            else:
                parts.append(cell + " " * (widths[i] - display_width(cell)))
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
#   記録の間隔は設定せず、実際の記録の開始時刻（ストレージの一覧のファイル名の時刻）から
#   「普段の間隔」を推定する。それより大きく空いたところを「間隔の乱れ」として報告するが、
#   深刻な異常とはみなさない（判定や終了コードには影響させない）。
#   空いた間の変更も、前後の記録を比べれば「その間のどこか」として見つかるため。
# ----------------------------------------------------------------------

def typical_interval(start_times: list, until: str):
    """記録の開始時刻（古い順）から、until のころの普段の間隔（間隔の中央値、秒）を推定する。

    until の後の最初の記録までのうち、直近 TYPICAL_SAMPLE 個の間隔を使う。数が少なければ None。
    """
    end = 0
    while end < len(start_times) and start_times[end] <= until:
        end += 1
    sample = start_times[max(0, end - TYPICAL_SAMPLE):end + 1]
    intervals = []
    for before, after in zip(sample, sample[1:]):
        intervals.append(timeutil.seconds_between(before, after))
    if len(intervals) < MIN_INTERVALS:
        return None
    intervals.sort()
    middle = len(intervals) // 2
    if len(intervals) % 2 == 1:
        return intervals[middle]
    return (intervals[middle - 1] + intervals[middle]) / 2


def long_intervals(start_times: list, typical: float, since: str, until: str) -> list:
    """普段の間隔の LONG_INTERVAL_RATIO 倍より空いたところのうち、[since, until) と重なるもの。"""
    result = []
    for before, after in zip(start_times, start_times[1:]):
        seconds = timeutil.seconds_between(before, after)
        if seconds > typical * LONG_INTERVAL_RATIO and after > since and before < until:
            result.append({"from": before, "to": after, "seconds": seconds})
    return result



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

def cmd_status(conf: dict, args) -> int:
    """ホストごとの最新のスナップショットと、直近の間隔の乱れ・異常を表示する。

    間隔はストレージの一覧（ファイル名の時刻）だけで調べる。
    ハッシュチェーンと読み取りエラーは、最新の 2 つだけダウンロードして調べる。
    問題（不正・チェーン異常・読み取りエラー）があれば終了コード 1。間隔の乱れは報告するだけ。
    """
    now = timeutil.now_utc()
    recent_from = timeutil.to_utc_text(timeutil.parse_utc(now) - timedelta(hours=STATUS_RECENT_HOURS))
    table = []
    has_problem = False
    for host in sorted(conf["hosts"]):
        entries = storage.list_snapshots(conf, host)
        if not entries:
            has_problem = True
            table.append([host, "", "", "", "", "0", "スナップショットがない"])
            continue
        times = [e["name_time"] for e in entries]
        recent = [t for t in times if t >= recent_from]
        typical = typical_interval(times, now)
        result = analysis.analyze_host(conf, entries[-2:])
        last = result["snapshots"][-1]
        notes = []    # 深刻な異常
        remarks = []  # 間隔の乱れ（報告するだけ）
        age = timeutil.seconds_between(times[-1], now)
        if typical is not None:
            if age > typical * LONG_INTERVAL_RATIO:
                remarks.append("最新のスナップショットが普段の間隔より古い")
            if long_intervals(times, typical, recent_from, now):
                remarks.append("直近 %d 時間に間隔の乱れあり" % STATUS_RECENT_HOURS)
        if last["problem"] is not None:
            notes.append("最新のスナップショットが不正")
        elif last["chain"] in CHAIN_PROBLEMS:
            notes.append("チェーン異常（" + last["chain"] + "）")
        elif last["errors"]:
            notes.append("読み取りエラーあり")
        if notes:
            has_problem = True
        notes += ["（参考）" + r for r in remarks]
        table.append([
            host,
            timeutil.format_local(entries[-1]["name_time"]),
            duration_text(age),
            str(last.get("files", "")),
            str(last.get("errors", "")),
            str(len(recent)),
            "、".join(notes) if notes else "OK",
        ])
    print_table(["ホスト", "最新のスナップショット", "経過", "ファイル数", "エラー",
                 "直近 %d 時間の数" % STATUS_RECENT_HOURS, "状況"], table)
    print()
    print("時刻の表示: " + timeutil.local_zone_label() + "（実行環境のタイムゾーン。TZ で変えられる）")
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
        print("該当する変化はありません")
        return
    table = []
    for change in changes:
        table.append([
            timeutil.format_local(change["changed_after"]),
            timeutil.format_local(change["changed_before"]),
            change_label(change),
            change["path"],
        ])
    print_table(["この時点では元の状態", "この時点では新しい状態", "種別", "パス"], table)
    print()
    print(str(len(changes)) + " 件（変更されたのは、左 2 列の時刻の間のどこか）")


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
    行の先頭の記号: ✓ 問題なし / ✗ 問題あり / ! 注意（変化の見え方に影響する）
    """
    snapshots = result["snapshots"]
    valid = [s for s in snapshots if s["problem"] is None]
    lines = []
    problems = []

    # 期間の直前・直後のスナップショット。端を省いた場合は、最初・最新のスナップショットが端になる
    if since is None:
        if valid:
            lines.append("✓ 調べた範囲の始まり: 最初のスナップショット %s（seq %d）"
                         % (timeutil.format_local(valid[0]["started_at"]), valid[0]["seq"]))
    else:
        before = None
        for snap in valid:
            if snap["finished_at"] <= since:
                before = snap
        if before is None:
            problems.append("期間の開始前のスナップショットがない")
            lines.append("✗ 期間の開始前のスナップショットがありません（開始時点の状態が分かりません）")
        else:
            lines.append("✓ 期間の直前のスナップショット: %s（seq %d）"
                         % (timeutil.format_local(before["started_at"]), before["seq"]))
    if until is None:
        if valid:
            last = valid[-1]
            lines.append("✓ 調べた範囲の終わり: 最新のスナップショット %s（seq %d。%s前）"
                         % (timeutil.format_local(last["started_at"]), last["seq"],
                            duration_text(timeutil.seconds_between(last["started_at"], timeutil.now_utc()))))
    else:
        after = None
        for snap in valid:
            if snap["started_at"] >= until:
                after = snap
                break
        if after is None:
            problems.append("期間の終了後のスナップショットがない")
            lines.append("✗ 期間の終了後のスナップショットがまだありません（終了時点の状態が分かりません）")
        else:
            lines.append("✓ 期間の直後のスナップショット: %s（seq %d）"
                         % (timeutil.format_local(after["started_at"]), after["seq"]))
    if not valid:
        problems.append("有効なスナップショットがない")

    # 記録の間隔（深刻な異常とはみなさず、報告するだけ）
    low = since or OPEN_START
    high = until or OPEN_END
    times = [e["name_time"] for e in result["entries"]]
    typical = typical_interval(times, high)
    if typical is None:
        lines.append("- 記録の間隔: 記録の数が少ないため、確かめていません")
    else:
        irregular = long_intervals(times, typical, low, high)
        if irregular:
            for g in irregular:
                lines.append("! 記録の間隔の乱れ: %s 〜 %s（%s。普段は約 %s）。この間の変更は、前後の記録の間のどこかとしか分かりません"
                             % (timeutil.format_local(g["from"]), timeutil.format_local(g["to"]),
                                duration_text(g["seconds"]), duration_text(typical)))
        else:
            lines.append("✓ 記録の間隔: 乱れなし（普段は約 %s）" % duration_text(typical))

    chain_bad = [s for s in valid if s["chain"] in CHAIN_PROBLEMS]
    if chain_bad:
        problems.append("ハッシュチェーンの異常")
        for s in chain_bad:
            lines.append("✗ ハッシュチェーン: seq %d（%s）: %s"
                         % (s["seq"], timeutil.format_local(s["started_at"]), CHAIN_PROBLEMS[s["chain"]]))
    else:
        lines.append("✓ ハッシュチェーン: 正常")

    invalid = [s for s in snapshots if s["problem"] is not None]
    if invalid:
        problems.append("不正なスナップショット")
        for s in invalid:
            lines.append("✗ 不正なスナップショット: %s: %s" % (s["name"], s["problem"]))
    else:
        lines.append("✓ 不正なスナップショット: なし")

    scope_changes = [s for s in valid if s["scope_changed"]]
    if scope_changes:
        problems.append("監視範囲の変更")
        for s in scope_changes:
            lines.append("! 監視範囲の変更: %s（seq %d）。範囲に出入りしたファイルは変化として数えていません"
                         % (timeutil.format_local(s["started_at"]), s["seq"]))
    else:
        lines.append("✓ 監視範囲の変更: なし")

    with_errors = [s for s in valid if s["errors"]]
    if with_errors:
        problems.append("読み取りエラー")
        worst = max(with_errors, key=lambda s: s["errors"])
        lines.append("! 読み取りエラー: %d 個のスナップショットで発生（最大 %d 件, seq %d）。"
                     "エラーの間は、変更された期間が広がります" % (len(with_errors), worst["errors"], worst["seq"]))
        if result["still_error_count"]:
            lines.append("! 期間の終わりでもエラーのまま: %d 件。エラーになる前から変わったかどうかは分かりません"
                         % result["still_error_count"])
    else:
        lines.append("✓ 読み取りエラー: なし")

    return lines, problems


# ----------------------------------------------------------------------
# review
# ----------------------------------------------------------------------

def cmd_review(conf: dict, args) -> int:
    """1 台のホストについて、期間内の変化と監視の状況を表示する。

    表の形式では「監視の状況」「変化」「判定」を出す。CSV・JSON では変化だけを出す（監視の状況に
    問題があれば、標準エラー出力に書く）。監視の状況に問題がなければ 0、あれば 1 を返す。
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
        print("ストレージに " + host + " のスナップショットがありません", file=sys.stderr)
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
    print("調査期間: %s 〜 %s" % (timeutil.format_local(since) if since else "最初のスナップショット",
                                  timeutil.format_local(until) if until else "最新のスナップショット"))
    print("時刻の表示: " + timeutil.local_zone_label(since or until) + "（実行環境のタイムゾーン。TZ で変えられる）")
    conditions = ["指定期間に検知したもの" if args.detected else "変更された可能性のある期間が、調査期間と重なるもの"]
    if args.path:
        conditions.append("パスが " + " / ".join(args.path) + " のどれかに一致")
    if types:
        conditions.append("種別が " + ", ".join(types))
    print("変化の条件: " + "、".join(conditions))
    print()
    print("[監視の状況]")
    for line in lines:
        print(line)
    print()
    print("[変化]")
    print_changes_table(changes)
    print()
    if problems:
        print("[判定] 監視の状況に確認が必要な点があります: " + "、".join(problems))
    elif args.path or types or args.detected:
        print("[判定] 監視の状況に問題はありません（変化は条件で絞り込んでいます）。")
    else:
        print("[判定] 監視の状況に問題はありません。上の変化がすべて説明できれば、"
              "監視範囲のファイル内容について、この期間に説明のつかない変更はありません。")
    return 1 if problems else 0


# ----------------------------------------------------------------------
# clean
# ----------------------------------------------------------------------

def cmd_clean(conf: dict, args) -> int:
    removed = storage.clean_cache(conf)
    print("キャッシュを消しました: %s（%.1f MB）" % (conf["cache_dir"], removed / 1e6))
    return 0
