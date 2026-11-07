"""表示の部品: 日本語が混ざっても列がそろうように、表示幅で計算する。"""

import unicodedata


def printable(text: str) -> str:
    """端末に出しても安全な形にする。表示できない文字は \\x1b・\\u202e のような表記にする。

    パスは監視対象のファイル名そのもので、侵入者が選べる（権限がなくても、アップロード先などに作れる）。
    制御文字（ESC・改行・CR など）をそのまま出すと、端末の表示を書き換えて変化の行を消したり、
    偽の行を足したりできる。双方向の制御文字（U+202E など）では、拡張子を偽って見せられる。
    表記にするのは、isprintable が表示できないとする文字のうち、空白（Zs: 全角の空白、ノーブレークスペースなど）
    以外のもの: 制御文字（Cc）、書式文字（Cf: 双方向の制御文字やゼロ幅の文字）、行・段落の区切り（U+2028 / U+2029。
    環境によっては改行として表示され、偽の行を足せる）など。空白は端末を操作できず、日本語のファイル名にふつうに入るので、そのまま出す。
    """
    if text.isprintable() or all(is_safe(c) for c in text):  # ふつうのパスは 1 文字ずつ見ずに済ませる
        return text
    result = []
    for c in text:
        if is_safe(c):
            result.append(c)
        elif ord(c) < 0x100:
            result.append("\\x%02x" % ord(c))
        elif ord(c) < 0x10000:
            result.append("\\u%04x" % ord(c))
        else:
            result.append("\\U%08x" % ord(c))
    return "".join(result)


def is_safe(c: str) -> bool:
    """そのまま端末に出してよい文字か（printable を参照）。"""
    return c.isprintable() or unicodedata.category(c) == "Zs"


def display_width(text: str) -> int:
    width = 0
    for c in text:
        width += 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
    return width


def print_table(headers, rows: list, right: tuple = ()) -> None:
    """表を出す。right は右にそろえる列の番号（数の列）。"""
    headers = [printable(h) for h in headers]
    rows = [[printable(cell) for cell in row] for row in rows]
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
        return "  ".join(parts)

    print(format_row(headers))
    print(format_row(["-" * w for w in widths]))
    for row in rows:
        print(format_row(row))
