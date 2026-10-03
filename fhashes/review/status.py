"""status: 設定の hosts に書いたホストごとに、最新の記録と、直近の記録の間隔、異常を表示する。

間隔はストレージの一覧（ファイル名の時刻）だけで調べ、直近 RECENT_COUNT 件の間隔の中央値と最大値を出す。
最新の記録の異常（不正、記録の欠落などハッシュチェーンの異常、読み取りエラー）は、最新の 2 つだけを
ダウンロードして調べる（中身の突き合わせはしない。時間がかかり、status には要らないため）。
直前の記録が不正なら、最新の記録のつながりを確かめられないので、それも異常とする。

記録の遅れ: 次の記録は「最新の記録の終了 + 間隔の中央値」のころに届くはず。そこから DELAY_ALLOWANCE
を過ぎても届いていなければ、記録が止まっている（サーバーの停止、タイマーの不調など）か、走査が遅く
なっている（ファイル数の急増、負荷）。終了時刻を基準にするのは、ホストごとの走査の時間を差し引くため。
"""

from fhashes import timeutil
from fhashes.progress import Progress
from fhashes.review import analysis, monitoring, storage
from fhashes.review.display import print_table

RECENT_COUNT = 24          # 間隔を調べる、直近の間隔の数（時間で区切ると、止まっているときに求められないため）
DELAY_ALLOWANCE = 5 * 60   # 記録の遅れとみなすまでの余裕（秒）
HEADERS = ("ホスト", "最新の記録", "間隔の中央値", "間隔の最大値", "ファイル数", "エラー", "状況")
NUMBER_COLUMNS = (4, 5)


def record_delay(latest: dict, latest_time: str, median: float, now: str) -> float:
    """次の記録が届くはずの時刻（最新の記録の終了 + 間隔の中央値）から、今までの秒数（負なら、まだ届く前）。

    最新の記録が不正で終了時刻が分からなければ、ファイル名の時刻（開始）を使う。
    """
    base = latest.get("finished_at") or latest_time
    return timeutil.seconds_between(base, now) - median


def status_row(conf: dict, host: str, now: str) -> tuple:
    """status の 1 行分。(行, 問題があるか) を返す。"""
    entries = storage.list_snapshots(conf, host)
    if not entries:
        return [host, "-", "-", "-", "-", "-", "記録なし"], True
    times = [e["name_time"] for e in entries]
    stats = monitoring.interval_stats(times[-(RECENT_COUNT + 1):])
    snapshots = analysis.check_records(conf, entries[-2:])
    latest = snapshots[-1]

    problems = []
    if stats is not None:
        delay = record_delay(latest, times[-1], stats[0], now)
        if delay > DELAY_ALLOWANCE:
            problems.append("記録の遅れ（%s）" % monitoring.duration_text(delay))
    problem = monitoring.latest_problem(latest)
    if problem is None and len(snapshots) == 2 and snapshots[0]["problem"] is not None:
        problem = "直前の記録が不正"    # 最新の記録のつながり（ハッシュチェーン）を確かめられない
    if problem is not None:
        problems.append(problem)

    row = [
        host,
        timeutil.format_local(times[-1]),
        monitoring.duration_text(stats[0]) if stats else "-",
        monitoring.duration_text(timeutil.seconds_between(*stats[1])) if stats else "-",
        "{:,}".format(latest["files"]) if latest["problem"] is None else "-",
        "{:,}".format(latest["errors"]) if latest["problem"] is None else "-",
        "、".join(problems) or "OK",
    ]
    return row, bool(problems)


def cmd_status(conf: dict, args) -> int:
    """問題（記録がない、記録の遅れ、最新の記録の異常）のあるホストが 1 台でもあれば終了コード 1。"""
    now = timeutil.now_utc()
    hosts = sorted(conf["hosts"])
    table = []
    has_problem = False
    progress = Progress("ホストの確認", len(hosts))
    try:
        for index, host in enumerate(hosts):
            progress.update(index, host, force=True)
            row, problem = status_row(conf, host, now)
            table.append(row)
            has_problem = has_problem or problem
    finally:
        progress.close()
    print_table(HEADERS, table, right=NUMBER_COLUMNS)
    return 1 if has_problem else 0
