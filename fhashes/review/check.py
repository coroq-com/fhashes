"""check: 実際のファイルを読んで、ある時点の記録と照らし合わせる（sha256sum --check に当たる）。

基本の使い方は、オプションなしで「最新の記録」と「今のファイル」を比べる（git status に近い）。
--at で、安全と分かっている時点の記録と比べる。マウントしたスナップショットのように、ファイルが
元の場所にないときは --map 元のパス:マウント先 で読む場所を置き換える。

流れ:
  1. 比べる記録を選び、検証する（記録の検証とハッシュチェーン）
  2. その記録の監視範囲（ヘッダーの include / exclude）のとおりに実際のファイルを走査し、
     全部を中身から読んで、一時的な記録を作る（状態ファイルは使わない。送らない）
  3. 記録と一時的な記録を、log / diff と同じ比較（analysis.compare）で比べる
  4. 差を diff と同じ場所の木で出す（時期はない）。--map の対応がない場所は「照合の範囲外」

印: A 追加（記録になく、実際にある）、D 削除、M 変更、T 種別変更、? 状態不明（読み取りエラー）
"""

import os
import shutil
import sys
import tempfile

from fhashes import VERSION, config, patterns, snapshot, timeutil, walker
from fhashes.pathmap import PathMap, is_under
from fhashes.patterns import Scope
from fhashes.progress import Progress
from fhashes.review import analysis, diff, monitoring, period, storage, tree

MARKS = (
    ("A", "追加"),
    ("D", "削除"),
    ("M", "変更"),
    ("T", "種別変更"),
    ("?", "状態不明"),
)
LEGEND_ROWS = (("A", "D", "M", "T", "?"),)
CANDIDATES = 3  # 比べる記録を選ぶために検証する数（直前の記録とのつながりも確かめるため、1 つ多めに取る）


class CheckScope:
    """走査の範囲: 記録の監視範囲を、--path（読む量を減らす）と --map（対応のある場所だけ読む）で絞ったもの。

    walker からは patterns.Scope と同じように使う。--not-path では絞らない（"*.php" が x.php という
    ディレクトリに一致して、中を読まなくなるため）。表示のときに除くだけにする。
    """

    def __init__(self, scope: Scope, pathmap, path_patterns):
        self.scope = scope
        self.pathmap = pathmap
        self.wanted = [patterns.compile_include(p if p.startswith("/") else "/**/" + p)
                       for p in path_patterns or []]
        self.outside = []        # 照合の範囲外になった起点: (起点, 一部だけか)
        self.walk_roots = self.compute_roots()

    def wanted_may_match(self, path: str) -> bool:
        """--path があるとき、path そのものか、その中にあるものが --path に一致しうるか。"""
        if not self.wanted:
            return True
        return any(w["regex"].match(path) or patterns.dir_may_contain_match(w, path) for w in self.wanted)

    def compute_roots(self) -> list:
        roots = []
        for root in self.scope.roots():
            if not self.wanted_may_match(root["path"]):
                continue                     # --path で絞った外（範囲外としては出さない）
            if self.pathmap is None or self.pathmap.covers(root["path"]):
                roots.append(root)
                continue
            # 起点そのものには対応がない。配下に対応の元のパスがあれば、そこから走査する
            subs = []
            for source in self.pathmap.sources:
                if (source != root["path"] and is_under(source, root["path"])
                        and not any(is_under(source, kept) for kept in subs)
                        and self.wanted_may_match(source)
                        and (self.scope.contains(source) or self.scope.should_descend(source))):
                    subs.append(source)
            roots += [{"path": source, "file_only": False} for source in subs]
            self.outside.append((root["path"], bool(subs)))
        return roots

    def roots(self) -> list:
        return self.walk_roots

    def is_excluded(self, path: str) -> bool:
        return self.scope.is_excluded(path)

    def contains(self, path: str) -> bool:
        return self.scope.contains(path) and (not self.wanted or any(w["regex"].match(path) for w in self.wanted))

    def should_descend(self, dir_path: str) -> bool:
        if not self.scope.should_descend(dir_path):
            return False
        return not self.wanted or any(patterns.dir_may_contain_match(w, dir_path) for w in self.wanted)

    def in_range(self, path: str) -> bool:
        """照合の範囲（--map の対応がある場所）か。"""
        return self.pathmap is None or self.pathmap.covers(path)


# ----------------------------------------------------------------------
# 比べる記録を選ぶ
# ----------------------------------------------------------------------

def choose_record(conf: dict, entries: list, at):
    """at（省けば None）までに終わった最後の正常な記録を選ぶ。(選んだ記録, 検証した記録の一覧) を返す。

    終了時刻はダウンロードして検証するまで分からないので、開始時刻が at より前のものから
    最後の CANDIDATES 件を検証して選ぶ。見つからなければ選んだ記録は None。
    """
    candidates = [e for e in entries if at is None or e["name_time"] < at][-CANDIDATES:]
    checked = analysis.check_records(conf, candidates)
    for info in reversed(checked):
        if info["problem"] is None and (at is None or info["finished_at"] <= at):
            return info, checked
    return None, checked


# ----------------------------------------------------------------------
# 実際のファイルを読んで、一時的な記録を作る
# ----------------------------------------------------------------------

def check_roots_exist(check_scope: CheckScope, pathmap) -> None:
    """ディレクトリを表す起点が、読む場所にあるか確かめる。なければ ValueError（終了コード 2）。

    --map の書き間違いやマウントのし忘れで、全部が「削除」と出るのを防ぐ。
    """
    paths = [root["path"] for root in check_scope.roots() if not root["file_only"]]
    if pathmap:
        # 入れ子の対応（--map /:… --map /var/www:…）のマウント先は、走査の途中で移るので、ここで確かめる
        paths += [source for source in pathmap.sources if source not in paths
                  and check_scope.should_descend(source)]
    missing = []
    for path in paths:
        if check_scope.is_excluded(path):
            continue
        try:
            os.stat(pathmap.resolve(path, follow_last=True) if pathmap else path)
        except FileNotFoundError:
            missing.append(path)
        except OSError:
            pass                             # 権限がない、対応の外を指すリンク等は、走査の中で error 行にする
    if missing:
        raise ValueError("比べる場所に、監視範囲の起点がありません: %s（--map のマウント先、--path を確かめてください）"
                         % ", ".join(missing))


def scan_to_record(path: str, host: str, record: dict, check_scope: CheckScope, pathmap) -> dict:
    """実際のファイルを全部読んで、一時的な記録を path に書く。analysis.compare に渡せる形の情報を返す。

    監視範囲は比べる記録と同じものをヘッダーに書く（範囲の変更として扱われないように）。
    """
    started_at = timeutil.now_utc()
    rows = {}   # パスのバイト列 → (種類, ハッシュ, エラー)。同じパスは後のもので置き換える（記録する側と同じ）
    progress = Progress("ファイルの読み取り", record["files"] + record["errors"])

    def on_file(file_path, kind, st, real):
        try:
            digest = walker.hash_file(real, kind)
        except OSError as e:
            on_error(file_path, kind, e)
            return
        if digest is None:
            return  # 走査中に消えた
        rows[os.fsencode(file_path)] = (kind, digest, None)
        progress.update(min(len(rows), progress.total))

    def on_error(file_path, kind, error):
        # 起点のリンクの先に入れなかったときは、リンク自体の行をエラーの行で置き換える
        rows[os.fsencode(file_path)] = (kind, None, walker.error_message(error))

    try:
        walker.walk(check_scope, on_file, on_error, pathmap)
    except walker.MissingRootError as e:
        raise ValueError(str(e))             # 走査の途中で起点が消えた
    finally:
        progress.close()

    header = snapshot.make_header(
        host=host, seq=1, prev_snapshot_sha256=None, started_at=started_at,
        include=record["scope"]["include"], exclude=record["scope"]["exclude"],
        reread_cycle=None, algo=walker.HASH_ALGO, tool_version=VERSION,
    )
    writer = snapshot.SnapshotWriter(path, header)
    for key in sorted(rows):
        kind, digest, error = rows[key]
        if error is None:
            writer.write_file(os.fsdecode(key), kind, digest)
        else:
            writer.write_error(os.fsdecode(key), kind, error)
    end = writer.close(timeutil.now_utc())
    return {
        "host": host,
        "local_path": path,
        "started_at": started_at,
        "finished_at": end["finished_at"],
        "errors": end["errors"],
        "content_sha256": end["content_sha256"],
        "scope_sha256": header["scope_sha256"],
        "scope": record["scope"],
    }


def compare_with_record(record: dict, current: dict, check_scope: CheckScope, keep_path) -> list:
    """記録と一時的な記録を比べて、パスごとの印の一覧（diff.net_changes の形）を返す。"""
    changes = []
    still_error = {}
    analysis.compare(record, current, still_error, changes)
    unreadable = [{"path": item["path"], "changed_after": "", "changed_before": ""}
                  for item in still_error.values()]

    def keep(path: str) -> bool:
        return check_scope.in_range(path) and (keep_path is None or keep_path(path))

    return diff.net_changes([ch for ch in changes if keep(ch["path"])],
                            [item for item in unreadable if keep(item["path"])])


# ----------------------------------------------------------------------
# 表示
# ----------------------------------------------------------------------

def monitoring_lines(record: dict, checked: list, check_scope: CheckScope, current: dict,
                     has_earlier: bool) -> tuple:
    """[監視の状況] の行と、問題の名前のリストを返す。has_earlier は、比べた記録より前の記録がストレージにあるか。"""
    lines = []
    problems = []
    valid = [info for info in checked if info["problem"] is None]

    # 比べた記録のハッシュチェーン（直前の記録とのつながり）。前の記録があるのに確かめられなかったら
    # （検証した範囲で、比べた記録より前のものが不正だった）、「なし」ではなく「未確認」とし、問題とみなす
    unverified = record["chain"] == "first" and has_earlier
    if unverified:
        problems.append("記録のつながりが未確認")
    for chain, title in monitoring.CHAIN_PROBLEMS.items():
        if unverified:
            lines.append("%s: 未確認" % title)
            continue
        if record["chain"] != chain:
            lines.append("%s: なし" % title)
            continue
        problems.append(title)
        if chain == "gap":
            prev = valid[valid.index(record) - 1]
            lines.append("%s: %s 件（%s）" % (title, "{:,}".format(record["missing"]),
                                              timeutil.format_local_range(prev["started_at"], record["started_at"])))
        else:
            lines.append("%s: %s" % (title, timeutil.format_local(record["started_at"])))

    invalid = [info for info in checked if info["problem"] is not None]
    if invalid:
        problems.append("不正な記録")
        for info in invalid:
            lines.append("不正な記録: %s: %s" % (info["name"], info["problem"]))
    else:
        lines.append("不正な記録: なし")

    # --map で読む場所を選んだ結果なので、問題とはみなさない（log の監視範囲の変更と同じ）
    if check_scope.outside:
        for root, partial in check_scope.outside:
            lines.append("照合の範囲外: %s%s" % (root, "（--map の対応がある場所を除く）" if partial else ""))
    else:
        lines.append("照合の範囲外: なし")

    # 記録の側で読めなかったものは、比べられない（? になる）
    if record["errors"]:
        problems.append("記録の読み取りエラー")
        lines.append("記録の読み取りエラー: {:,} ファイル".format(record["errors"]))
    else:
        lines.append("記録の読み取りエラー: なし")
    if current["errors"]:
        problems.append("照合の読み取りエラー")
        lines.append("照合の読み取りエラー: {:,} ファイル".format(current["errors"]))
    else:
        lines.append("照合の読み取りエラー: なし")
    return lines, problems


def cmd_check(conf: dict, args) -> int:
    """差があるか、監視の状況に問題があれば終了コード 1。"""
    host = args.host
    if host not in conf["hosts"]:
        raise config.ConfigError(host + " は設定（" + conf["config_path"] + "）の hosts にありません")
    keep_path = period.path_filter(args.path, args.not_path)
    marks = tree.mark_filter(args.type, args.not_type, MARKS)
    pathmap = PathMap(args.map) if args.map else None
    at = timeutil.parse_user_time(args.at) if args.at else None

    entries = storage.list_snapshots(conf, host)
    if not entries:
        print("ストレージに " + host + " の記録がありません", file=sys.stderr)
        return 1
    record, checked = choose_record(conf, entries, at)
    if record is None:
        print("比べられる記録がありません（%s）" % ("指定した日時までに終わった正常な記録がない" if at else "最新の記録が不正"),
              file=sys.stderr)
        return 1

    check_scope = CheckScope(Scope(record["scope"]["include"], record["scope"]["exclude"]), pathmap, args.path)
    check_roots_exist(check_scope, pathmap)
    work_dir = tempfile.mkdtemp(prefix="fhashes-check-")
    try:
        current = scan_to_record(os.path.join(work_dir, "current" + snapshot.FILE_SUFFIX),
                                 host, record, check_scope, pathmap)
        try:
            items = compare_with_record(record, current, check_scope, keep_path)
        except (snapshot.SnapshotFormatError, KeyError, TypeError, ValueError) as e:
            print("記録の中身が壊れています: %s: %s: %s" % (record["name"], type(e).__name__, e), file=sys.stderr)
            return 1
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    shown = [item for item in items if item["mark"] in marks]
    lines, problems = monitoring_lines(record, checked, check_scope, current,
                                       entries[0]["name"] != record["name"])

    print("ホスト    : " + host)
    print("比べる記録: " + timeutil.format_local(record["started_at"]))
    print("比べる場所: " + (", ".join(pathmap.specs)
                           if pathmap else "今のファイル"))
    print()
    print("[差分]")
    if not shown:
        print("差分なし")
    else:
        tree.print_lines(tree.render(shown, check_scope.scope.roots(), lambda item: ""))
    print()
    tree.print_legend(shown, MARKS, LEGEND_ROWS)
    print()
    print("[監視の状況]")
    for line in lines:
        print(line)
    return 1 if shown or problems else 0
