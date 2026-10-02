"""status: 設定の hosts に書いたホストごとに、最新の記録と、直近 24 時間の記録の間隔、異常を表示する。

間隔はストレージの一覧（ファイル名の時刻）だけで調べる（判定はせず、中央値と最大値を出す）。
最新の記録の異常（不正、記録の欠落などハッシュチェーンの異常、読み取りエラー）は、最新の 2 つだけを
ダウンロードして調べる。直前の記録が不正なら、最新の記録のつながりを確かめられないので、それも異常とする。
"""

from datetime import timedelta

from fhashes import timeutil
from fhashes.progress import Progress
from fhashes.review import analysis, monitoring, storage
from fhashes.review.display import print_table

RECENT_HOURS = 24    # 記録の間隔を調べる範囲
HEADERS = ("ホスト", "最新の記録", "間隔の中央値", "間隔の最大値", "ファイル数", "エラー", "状況")
NUMBER_COLUMNS = (4, 5)


def status_row(conf: dict, host: str, recent_from: str) -> tuple:
    """status の 1 行分。(行, 問題があるか) を返す。"""
    entries = storage.list_snapshots(conf, host)
    if not entries:
        return [host, "-", "-", "-", "-", "-", "記録なし"], True
    times = [e["name_time"] for e in entries]
    stats = monitoring.interval_stats([t for t in times if t >= recent_from])
    snapshots = analysis.analyze_host(conf, entries[-2:], show_progress=False)["snapshots"]
    latest = snapshots[-1]
    problem = monitoring.latest_problem(latest)
    if problem is None and len(snapshots) == 2 and snapshots[0]["problem"] is not None:
        problem = "直前の記録が不正"    # 最新の記録のつながり（ハッシュチェーン）を確かめられない
    row = [
        host,
        timeutil.format_local(times[-1]),
        monitoring.duration_text(stats[0]) if stats else "-",
        monitoring.duration_text(timeutil.seconds_between(*stats[1])) if stats else "-",
        "{:,}".format(latest["files"]) if latest["problem"] is None else "-",
        "{:,}".format(latest["errors"]) if latest["problem"] is None else "-",
        problem or "OK",
    ]
    return row, problem is not None


def cmd_status(conf: dict, args) -> int:
    """問題（記録がない、最新の記録の異常）のあるホストが 1 台でもあれば終了コード 1。"""
    now = timeutil.parse_utc(timeutil.now_utc())
    recent_from = timeutil.to_utc_text(now - timedelta(hours=RECENT_HOURS))
    hosts = sorted(conf["hosts"])
    table = []
    has_problem = False
    progress = Progress("ホストの確認", len(hosts))
    try:
        for index, host in enumerate(hosts):
            progress.update(index, host, force=True)
            row, problem = status_row(conf, host, recent_from)
            table.append(row)
            has_problem = has_problem or problem
    finally:
        progress.close()
    print_table(HEADERS, table, right=NUMBER_COLUMNS)
    return 1 if has_problem else 0
