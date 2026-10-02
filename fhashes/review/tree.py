"""変化を場所の木で表示する部品（diff と log で共通）。

木は、監視範囲の起点（include のディレクトリ）ごとに分け、起点のディレクトリを根にする（字下げしない）。
その下は、子が 1 つしかないディレクトリを 1 行につなげ、名前の順（ls と同じ）に並べる。
ファイル名は丸めずに全部出す。

木に載せるものは {"path", "mark", ...} の dict。右に添える文字（時期など）は period_of で決める。
"""

from fhashes.review.display import display_width


def split_by_roots(items: list, roots: list) -> list:
    """監視範囲の起点ごとに分ける。返り値: [(起点, その下のもの)]。起点の外のものは、起点 None にまとめる。

    roots は patterns.unique_roots の形（起点どうしは入れ子にならない）。
    """
    groups = {}
    outside = []
    for item in items:
        for root in roots:
            path = root["path"]
            if item["path"] == path or path == "/" or item["path"].startswith(path + "/"):
                groups.setdefault(path, (root, []))[1].append(item)
                break
        else:
            outside.append(item)
    result = [groups[path] for path in sorted(groups)]
    if outside:
        result.append((None, outside))
    return result


def build_tree(items: list, base=None) -> tuple:
    """(木の根のディレクトリ, 入れ子の辞書) を返す。辞書のキーはディレクトリなら "名前/"、ファイルなら "名前"。

    根は base（監視範囲の起点のディレクトリ）。base がなければ、すべてのファイルに共通する一番深いディレクトリ。
    """
    split = [item["path"].split("/")[1:] for item in items]   # 先頭の "/" の分を除く
    if base is not None:
        common = [name for name in base.split("/") if name]
    else:
        common = []
        for level in zip(*[parts[:-1] for parts in split]):
            if len(set(level)) != 1:
                break
            common.append(level[0])
    root_name = "/" + "".join(name + "/" for name in common)
    tree = {}
    for parts, item in zip(split, items):
        node = tree
        for name in parts[len(common):-1]:
            node = node.setdefault(name + "/", {})
        node[parts[-1]] = item
    return root_name, tree


def is_dir(node) -> bool:
    return "mark" not in node


def leaves(node: dict) -> list:
    if not is_dir(node):
        return [node]
    return [leaf for child in node.values() for leaf in leaves(child)]


def tree_lines(node: dict, depth: int, parent_period: str, period_of) -> list:
    """木を (字下げの深さ, 文字列, 右に添える文字) の行にする。

    - 子が 1 つしかないディレクトリは 1 行につなげる。
    - ディレクトリの下がすべて同じ時期なら、時期はディレクトリの行に 1 回だけ書く。
    """
    lines = []
    for name in sorted(node):
        child, label = node[name], name
        while is_dir(child) and len(child) == 1:
            (sub, child), = child.items()
            label += sub
        if not is_dir(child):
            period = period_of(child)
            lines.append((depth, child["mark"] + " " + label, "" if period == parent_period else period))
            continue
        periods = {period_of(leaf) for leaf in leaves(child)}
        common = periods.pop() if len(periods) == 1 else None
        lines.append((depth, label, common if common and common != parent_period else ""))
        lines += tree_lines(child, depth + 1, parent_period if common is None else common, period_of)
    return lines


def render(items: list, roots: list, period_of=lambda item: "") -> list:
    """起点ごとの木の行（字下げの深さ, 文字列, 右に添える文字）を返す。起点の行は深さ 0（字下げしない）。"""
    lines = []
    for root, members in split_by_roots(items, list(roots)):
        if root is not None and root["path"] == members[0]["path"]:
            # 1 つのファイルを指す起点（/etc/passwd など）は、フルパスの 1 行
            lines.append((0, members[0]["mark"] + " " + members[0]["path"], period_of(members[0])))
            continue
        root_name, tree = build_tree(members, None if root is None else root["path"])
        # 根の下がすべて同じ時期なら、ディレクトリと同じく根の行に 1 回だけ書く
        periods = {period_of(item) for item in members}
        common = periods.pop() if len(periods) == 1 else ""
        lines += [(0, root_name, common)] + tree_lines(tree, 1, common, period_of)
    return lines


def print_lines(lines: list) -> None:
    """木の行を出す。右に添える文字は、列をそろえて出す。"""
    width = max(4 * depth + display_width(text) for depth, text, _ in lines)
    for depth, text, right in lines:
        body = "    " * depth + text
        if right:
            body += " " * (width - display_width(body) + 4) + right
        print(body)


def print_legend(items: list, marks: tuple, rows: tuple) -> None:
    """印ごとの件数の表（凡例を兼ねる）。rows の段に分け、列の位置は上下の段でそろえる。0 件の印も出す。"""
    counts = {mark: 0 for mark, _ in marks}
    for item in items:
        counts[item["mark"]] += 1
    names = dict(marks)
    columns = max(len(row) for row in rows)
    name_width = [max(display_width(names[row[i]]) for row in rows if i < len(row)) for i in range(columns)]
    count_width = [max(len("{:,}".format(counts[row[i]])) for row in rows if i < len(row)) for i in range(columns)]
    for row in rows:
        cells = []
        for i, mark in enumerate(row):
            name = names[mark]
            cells.append("%s  %s%s  %s" % (mark, name, " " * (name_width[i] - display_width(name)),
                                           "{:,}".format(counts[mark]).rjust(count_width[i])))
        print("    ".join(cells))


def mark_filter(type_values, not_type_values, marks: tuple) -> set:
    """表示する印の集合。--type（印をつなげて書く。何度も指定できる）があればそれだけにし、
    そこから --not-type の印を除く。marks はそのコマンドで使う印の一覧。"""
    def parse(values, option):
        allowed = {mark for mark, _ in marks}
        result = set()
        for text in values or []:
            for char in text:
                if char not in allowed:
                    raise ValueError("%s には印（%s）をつなげて書いてください: %s"
                                     % (option, " ".join(mark for mark, _ in marks), text))
                result.add(char)
        return result
    wanted = parse(type_values, "--type") or {mark for mark, _ in marks}
    return wanted - parse(not_type_values, "--not-type")
