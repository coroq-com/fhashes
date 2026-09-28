"""ワイルドカード（監視範囲の include / exclude と、履歴の --path で使う）。

  *   : "/" 以外の 0 文字以上
  ?   : "/" 以外の 1 文字
  [ ] : 文字クラス（[!abc] で否定）
  **  : 0 個以上のディレクトリ（パスの区切り 1 つ分を丸ごと ** にしたときだけ有効）
"""

import re


def check_pattern(pattern: str) -> None:
    """パターンの書き方が正しいか調べる。おかしければ ValueError を投げる。"""
    if not pattern.startswith("/"):
        raise ValueError("絶対パスで書いてください: " + pattern)
    if pattern == "/":
        raise ValueError("'/' だけのパターンは使えません（'/**' と書いてください）")
    for segment in pattern[1:].split("/"):
        if segment == "":
            raise ValueError("'//' や末尾の '/' は使えません: " + pattern)
        if segment in (".", ".."):
            raise ValueError("'.' や '..' は使えません: " + pattern)
        if "**" in segment and segment != "**":
            raise ValueError("'**' は '/**/' のように単独で書いてください: " + pattern)


def has_wildcard(segment: str) -> bool:
    return "*" in segment or "?" in segment or "[" in segment


def segment_to_regex(segment: str) -> str:
    """パスの 1 区切り分（"/" を含まない）のワイルドカードを正規表現の文字列に変換する。"""
    result = ""
    i = 0
    while i < len(segment):
        c = segment[i]
        if c == "*":
            result += "[^/]*"
        elif c == "?":
            result += "[^/]"
        elif c == "[":
            end = segment.find("]", i + 2)  # "[]abc]" のように先頭の ] は文字として扱う
            if end == -1:
                result += re.escape(c)  # 閉じていない [ はただの文字
            else:
                inside = segment[i + 1:end].replace("\\", "\\\\")
                if inside.startswith("!"):
                    result += "[^/" + inside[1:] + "]"
                elif inside.startswith("^"):
                    result += "[\\" + inside + "]"  # 先頭の ^ はただの文字として扱う
                else:
                    result += "[" + inside + "]"
                i = end
        else:
            result += re.escape(c)
        i += 1
    return result


def glob_to_regex(pattern: str) -> "re.Pattern":
    """パターン全体を、パス全体にマッチする正規表現に変換する。

    例: "/etc/**/*.conf" は "/etc/a.conf" にも "/etc/x/y/a.conf" にもマッチする。
        "/var/cache/**" は "/var/cache" 自身とその配下すべてにマッチする。
    """
    check_pattern(pattern)
    regex = ""
    for segment in pattern[1:].split("/"):
        if segment == "**":
            regex += "(?:/[^/]+)*"
        else:
            regex += "/" + segment_to_regex(segment)
    return re.compile("^" + regex + "$", re.DOTALL)


def pattern_root(pattern: str) -> str:
    """パターンのうち、ワイルドカードが出てくる前の部分（走査を始めるパス）を返す。

    例: "/var/www/app/**/*.php" -> "/var/www/app"
        "/etc/passwd"           -> "/etc/passwd"
    """
    fixed = []
    for segment in pattern[1:].split("/"):
        if segment == "**" or has_wildcard(segment):
            break
        fixed.append(segment)
    return "/" + "/".join(fixed)


def compile_include(pattern: str) -> dict:
    """include パターン 1 つ分の情報をまとめた dict を作る。"""
    segments = []
    for segment in pattern[1:].split("/"):
        if segment == "**":
            segments.append(None)  # None は ** を表す
        else:
            segments.append(re.compile("^" + segment_to_regex(segment) + "$", re.DOTALL))
    return {
        "pattern": pattern,
        "regex": glob_to_regex(pattern),
        "segments": segments,
        "root": pattern_root(pattern),
    }


def dir_may_contain_match(include: dict, dir_path: str) -> bool:
    """ディレクトリ dir_path の中に、このパターンにマッチするファイルがあり得るか。

    False なら、そのディレクトリの中に降りる必要はない（負荷を下げるため）。
    例: パターン "/etc/*/conf" に対して "/etc/nginx" は True、"/etc/nginx/sub" は False。
    """
    segments = include["segments"]
    parts = dir_path[1:].split("/") if dir_path != "/" else []
    for index, part in enumerate(parts):
        if index >= len(segments):
            return False
        if segments[index] is None:
            return True  # ** から先は何が来てもよい
        if not segments[index].match(part):
            return False
    return len(segments) > len(parts)


def unique_roots(includes: list) -> list:
    """走査の起点の一覧を作る。ほかの起点の配下にある起点は除く（二重に数えないため）。

    返り値: [{"path": 起点のパス, "file_only": bool}, ...]
      file_only は、その起点がワイルドカードを含まないパターンそのもの（1 つのファイルを指す）で、
      ディレクトリを表す起点としては使われていないこと。起点が存在しないときの扱いが変わる。
    """
    file_only = {}
    for include in includes:
        is_literal = include["root"] == include["pattern"]
        file_only[include["root"]] = file_only.get(include["root"], True) and is_literal

    roots = []
    for path in sorted(file_only):
        covered = False
        for kept in roots:
            if kept["path"] == "/" or path.startswith(kept["path"] + "/"):
                covered = True
                break
        if not covered:
            roots.append({"path": path, "file_only": file_only[path]})
    return roots


class Scope:
    """監視範囲（include / exclude）。パスが範囲内かどうかを判定する。"""

    def __init__(self, include: list, exclude: list):
        self.includes = [compile_include(p) for p in include]
        self.excludes = [glob_to_regex(p) for p in exclude]

    def is_included(self, path: str) -> bool:
        for include in self.includes:
            if include["regex"].match(path):
                return True
        return False

    def is_excluded(self, path: str) -> bool:
        for regex in self.excludes:
            if regex.match(path):
                return True
        return False

    def contains(self, path: str) -> bool:
        """ファイルのパスが監視範囲に入るか。"""
        return self.is_included(path) and not self.is_excluded(path)

    def should_descend(self, dir_path: str) -> bool:
        """このディレクトリの中に降りる必要があるか。"""
        if self.is_excluded(dir_path):
            return False
        for include in self.includes:
            if dir_may_contain_match(include, dir_path):
                return True
        return False

    def roots(self) -> list:
        return unique_roots(self.includes)
