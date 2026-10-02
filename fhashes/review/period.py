"""期間を指定して 1 台のホストを調べるコマンド（log と diff）に共通の部分。

流れ:
  1. 絞り込みの指定（--path / --not-path など）を解釈する（誤りがあれば、ダウンロードの前に止める）
  2. open_period: 期間の解釈、記録の一覧の取得、必要な記録のダウンロードと解析
  3. 各コマンドが、Period から変化を取り出して表示する
  4. Period.print_header / print_monitoring で、見出しと [監視の状況] を出す（終了コードも決まる）
"""

import sys

from fhashes import config, patterns, timeutil
from fhashes.patterns import Scope
from fhashes.review import analysis, monitoring, storage

# --from / --to を省いたときの範囲の端。時刻の文字列どうしの大小比較にだけ使う（表示しない）
OPEN_START = "0000-01-01T00:00:00Z"
OPEN_END = "9999-12-31T23:59:59Z"


class Period:
    """1 台のホストの、指定された期間を調べた結果。

    since / until は --from / --to（省かれた端は None）。low / high は、省かれた端を
    OPEN_START / OPEN_END で埋めたもの（比較に使う）。
    """

    def __init__(self, host: str, since, until, result: dict):
        self.host = host
        self.since = since
        self.until = until
        self.low = since or OPEN_START
        self.high = until or OPEN_END
        self.result = result
        self.monitoring_lines, self.problems = monitoring.report(result, since, until)

    def changes(self, keep_path=None, detected: bool = False) -> list:
        """期間の変化（古い順）。

        既定は、変化した可能性のある時期が期間と重なるもの（取りこぼしがない）。
        変化は時期の終わり（記録の終了）より前に起きているので、終わりがちょうど期間の開始なら重ならない。
        detected なら、期間に検知したもの（時期の終わりが期間の中にあるもの）。
        """
        result = []
        for change in self.result["changes"]:
            if detected:
                if not (self.low <= change["changed_before"] < self.high):
                    continue
            elif change["changed_before"] <= self.low or change["changed_after"] >= self.high:
                continue
            if keep_path and not keep_path(change["path"]):
                continue
            result.append(change)
        result.sort(key=lambda ch: (ch["changed_before"], ch["path"]))
        return result

    def still_error(self, keep_path=None) -> list:
        """期間の終わりでも読めないままのファイル。"""
        return [item for item in self.result["still_error"] if not keep_path or keep_path(item["path"])]

    def roots(self) -> list:
        """期間の最後の正常な記録の監視範囲の起点（include のディレクトリ）。変化の木を分ける単位にする。"""
        valid = [r for r in self.result["snapshots"] if r["problem"] is None]
        if not valid:
            return []
        return Scope(valid[-1]["scope"]["include"], valid[-1]["scope"]["exclude"]).roots()

    def print_header(self) -> None:
        print("ホスト  : " + self.host)
        print("調査期間: %s - %s" % (timeutil.format_local(self.since) if self.since else "最初の記録",
                                      timeutil.format_local(self.until) if self.until else "最新の記録"))
        print()

    def print_monitoring(self) -> int:
        """[監視の状況] を出し、終了コード（問題があれば 1）を返す。"""
        print("[監視の状況]")
        for line in self.monitoring_lines:
            print(line)
        return self.exit_code()

    def exit_code(self) -> int:
        return 1 if self.problems else 0


def open_period(conf: dict, args):
    """args（host、--from / --to）の期間を調べる。そのホストの記録がストレージになければ None。

    期間の調べ方: 期間の直前・直後の記録まで含めてダウンロードし、検証して隣どうしを比べる
    （analysis.select_range）。
    """
    host = args.host
    if host not in conf["hosts"]:
        raise config.ConfigError(host + " は設定（" + conf["config_path"] + "）の hosts にありません")
    since, until = parse_period(args)
    entries = storage.list_snapshots(conf, host)
    if not entries:
        print("ストレージに " + host + " の記録がありません", file=sys.stderr)
        return None
    selected = analysis.select_range(entries, since or OPEN_START, until or OPEN_END)
    return Period(host, since, until, analysis.analyze_host(conf, selected))


def parse_period(args) -> tuple:
    """--from / --to を UTC の文字列にする。省かれた端は None。"""
    since = timeutil.parse_user_time(args.since) if args.since else None
    until = timeutil.parse_user_time(args.until, end_of_range=True) if args.until else None
    if since is not None and until is not None and since >= until:
        raise ValueError("--to は --from より後にしてください")
    return since, until


def path_filter(path_patterns, not_path_patterns):
    """--path（これだけ）と --not-path（これを除く）から、パスを残すかどうかを決める関数を作る。

    パターンは "/" で始まらなければ、どのディレクトリの下でもよいものとする。
    --path があれば、どれかに一致するものだけを残す。そこから、--not-path のどれかに一致するものを除く。
    どちらもなければ None（絞り込まない）。
    """
    wanted = [path_regex(p) for p in path_patterns or []]
    unwanted = [path_regex(p) for p in not_path_patterns or []]
    if not wanted and not unwanted:
        return None

    def keep(path: str) -> bool:
        if wanted and not any(r.match(path) for r in wanted):
            return False
        return not any(r.match(path) for r in unwanted)
    return keep


def path_regex(pattern: str):
    if not pattern.startswith("/"):
        pattern = "/**/" + pattern
    return patterns.glob_to_regex(pattern)
