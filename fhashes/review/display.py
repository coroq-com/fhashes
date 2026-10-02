"""表示の部品: 日本語が混ざっても列がそろうように、表示幅で計算する。"""

import unicodedata


def display_width(text: str) -> int:
    width = 0
    for c in text:
        width += 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
    return width


def print_table(headers, rows: list, right: tuple = ()) -> None:
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
        return "  ".join(parts)

    print(format_row(headers))
    print(format_row(["-" * w for w in widths]))
    for row in rows:
        print(format_row(row))
