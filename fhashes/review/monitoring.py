"""監視の状況: 変化の一覧を信用してよいかどうかの確認（log と diff の [監視の状況]、status の「状況」）。

記録の間隔は設定に持たず、「乱れ」とも判定しない（閾値に根拠がないため）。実際の記録の開始時刻から
中央値と最大値を出し、人が判断する。空いた間の変更も、前後の記録を比べれば「その間のどこか」として見つかる。
"""

from fhashes import timeutil

# ハッシュチェーンで見つかる異常。表示の見出しは仕組み（ハッシュチェーン）ではなく、起きていることにする
CHAIN_PROBLEMS = {
    "gap": "記録の欠落",
    "broken": "記録の不一致",
    "restart": "記録のやり直し",
}


def duration_text(seconds: float) -> str:
    """秒数を読みやすい単位で表す（例: 45 秒、12 分、3.5 時間、6.3 日）。"""
    if seconds < 120:
        return "%.0f 秒" % seconds
    if seconds < 2 * 3600:
        return "%.0f 分" % (seconds / 60)
    if seconds < 2 * 86400:
        return "%.1f 時間" % (seconds / 3600)
    return "%.1f 日" % (seconds / 86400)


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


def report(result: dict, since, until) -> tuple:
    """期間の監視の状況を調べる。(表示する行のリスト, 問題の名前のリスト) を返す。

    result は analysis.analyze_host の結果。since / until は、--from / --to を省いた場合は None
    （最初の記録から / 最新の記録まで）。
    行は「見出し: 値」で、問題のない項目も省かない。記号は付けない（値を見れば問題かどうか分かる）。
    終了コード 1 になる問題は problems に加える（記録の間隔と監視範囲の変更は、注意だけなので加えない）。
    """
    records = result["snapshots"]
    valid = [r for r in records if r["problem"] is None]
    lines = []
    problems = []

    # 期間の直前・直後の記録。端を省いた場合は、最初・最新の記録を出す（ないことを問題にしない）
    if since is None:
        if valid:
            lines.append("最初の記録: %s" % timeutil.format_local(valid[0]["started_at"]))
    else:
        before = [r for r in valid if r["finished_at"] <= since]
        if before:
            lines.append("期間の直前の記録: %s" % timeutil.format_local(before[-1]["started_at"]))
        else:
            problems.append("期間の直前の記録なし")
            lines.append("期間の直前の記録: なし")
    if until is None:
        if valid:
            lines.append("最新の記録: %s" % timeutil.format_local(valid[-1]["started_at"]))
    else:
        after = [r for r in valid if r["started_at"] >= until]
        if after:
            lines.append("期間の直後の記録: %s" % timeutil.format_local(after[0]["started_at"]))
        else:
            problems.append("期間の直後の記録なし")
            lines.append("期間の直後の記録: なし")
    if not valid:
        problems.append("有効な記録なし")
        lines.append("有効な記録: なし")

    # 記録の件数（期間の中に始まったもの。直前・直後の記録は数えない）と間隔（判定はしない）。
    # 間隔は直前・直後の記録も含めて求める（期間の端の変化の時期も、その間隔で決まるため）
    inside = [r for r in valid if (since is None or r["started_at"] >= since)
              and (until is None or r["started_at"] < until)]
    lines.append("記録の件数: {:,}".format(len(inside)))
    stats = interval_stats([r["started_at"] for r in valid])
    if stats is not None:
        median, longest = stats
        lines.append("記録の間隔: 中央値 %s、最大値 %s（%s）" % (
            duration_text(median), duration_text(timeutil.seconds_between(*longest)),
            timeutil.format_local_range(*longest)))

    # ハッシュチェーンの異常（欠落・不一致・やり直し）
    for chain, title in CHAIN_PROBLEMS.items():
        found = [(index, r) for index, r in enumerate(valid) if r["chain"] == chain]
        if not found:
            lines.append("%s: なし" % title)
            continue
        problems.append(title)
        for index, r in found:
            if chain == "gap":
                lines.append("%s: %s 件（%s）" % (
                    title, "{:,}".format(r["missing"]),
                    timeutil.format_local_range(valid[index - 1]["started_at"], r["started_at"])))
            else:
                lines.append("%s: %s" % (title, timeutil.format_local(r["started_at"])))

    invalid = [r for r in records if r["problem"] is not None]
    if invalid:
        problems.append("不正な記録")
        for r in invalid:
            lines.append("不正な記録: %s: %s" % (r["name"], r["problem"]))
    else:
        lines.append("不正な記録: なし")

    # 監視範囲の変更は、ふつうは設定を変えた結果（意図したもの）なので、問題とはみなさない
    scope_changes = [r for r in valid if r["scope_changed"]]
    for r in scope_changes:
        lines.append("監視範囲の変更: %s" % timeutil.format_local(r["started_at"]))
    if not scope_changes:
        lines.append("監視範囲の変更: なし")

    # 読み取りエラーは、読めない間は変化を見逃しうるので問題とみなす（権限の設定などを直すべき状況）
    with_errors = [r for r in valid if r["errors"]]
    if with_errors:
        problems.append("読み取りエラー")
        worst = max(with_errors, key=lambda r: r["errors"])
        lines.append("読み取りエラー: 記録 {:,} 件、最大 {:,} ファイル（{}）".format(
            len(with_errors), worst["errors"], timeutil.format_local(worst["started_at"])))
        if result["still_error"]:
            lines.append("期間の終わりでもエラーのまま: {:,} ファイル".format(len(result["still_error"])))
    else:
        lines.append("読み取りエラー: なし")

    return lines, problems


def latest_problem(record: dict):
    """status の「状況」に出す、最新の記録の問題（なければ None）。"""
    if record["problem"] is not None:
        return "最新の記録が不正"
    if record["chain"] in CHAIN_PROBLEMS:
        return CHAIN_PROBLEMS[record["chain"]]
    if record["errors"]:
        return "読み取りエラー"
    return None
