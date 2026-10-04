"""パスの対応（check の --map）: 元のパス（記録に書かれているパス）と、実際に読む場所（マウント先）。

例: --map /:/mnt/snap なら、/var/www/app/index.php は /mnt/snap/var/www/app/index.php から読む。
    --map /:/mnt/root --map /var/www:/mnt/data のように複数書けば、長く一致するほうを使う。

シンボリックリンクは、元のパスの世界で解決する（chroot と同じ考え方）。スナップショットの中の
絶対パスのリンク（current -> /var/www/releases/1 など）を、調べる側のマシンの本物の /var/www で
解決してしまわないため。対応のない場所を指すリンクは、たどらない（OutsideMapError）。
"""

import errno
import os
import posixpath
import stat

MAX_LINKS = 40  # リンクをたどる回数の上限（Linux の上限と同じ。ループしているリンクで止まらないように）


class OutsideMapError(OSError):
    """対応（--map）のない場所を読もうとした。"""
    pass


def normalize(path: str, what: str) -> str:
    """絶対パスで、//・.・.. を含まない形に限る。末尾の / は取る。"""
    if not path.startswith("/"):
        raise ValueError("%s は絶対パスで書いてください: %s" % (what, path))
    stripped = path.rstrip("/") or "/"
    if "//" in stripped or posixpath.normpath(stripped) != stripped:
        raise ValueError("%s に '//'・'.'・'..' は使えません: %s" % (what, path))
    return stripped


def is_under(path: str, base: str) -> bool:
    """path が base そのものか、その配下か。"""
    return path == base or base == "/" or path.startswith(base + "/")


class PathMap:
    def __init__(self, specs: list):
        """specs: "元のパス:マウント先" の文字列のリスト。"""
        self.mounts = {}
        self.specs = list(specs)  # 表示用（書かれたとおり）
        for spec in specs:
            if spec.count(":") != 1:
                raise ValueError("--map は「元のパス:マウント先」の形で書いてください（コロンは 1 つだけ）: " + spec)
            source, mount = spec.split(":")
            source = normalize(source, "--map の元のパス")
            # マウント先そのものがリンク（/media の自動マウントなど）でも、元のパスの世界で解決しないように、先に実体にする
            mount = os.path.realpath(normalize(mount, "--map のマウント先"))
            if source in self.mounts:
                raise ValueError("--map に同じ元のパスが 2 回あります: " + source)
            self.mounts[source] = mount
        self.sources = sorted(self.mounts)

    def source_of(self, path: str):
        """path に当てはまる対応の元のパス（一番長く一致するもの）。なければ None。"""
        found = None
        for source in self.sources:
            if is_under(path, source) and (found is None or len(source) > len(found)):
                found = source
        return found

    def covers(self, path: str) -> bool:
        return self.source_of(path) is not None

    def real_of(self, path: str):
        """元のパスを、実際に読む場所にする（リンクは解決しない）。対応がなければ None。"""
        source = self.source_of(path)
        if source is None:
            return None
        mount = self.mounts[source]
        rest = "" if path == source else (path if source == "/" else path[len(source):])  # "/" から始まる残り
        if mount == "/":
            return rest or "/"
        return mount + rest

    def mount_at(self, path: str):
        """path がちょうど対応の元のパスなら、そのマウント先（走査中に別のマウント先へ移るところ）。"""
        return self.mounts.get(path)

    def resolve(self, path: str, follow_last: bool) -> str:
        """元のパスの途中のリンクを、元のパスの世界で解決して、実際に読む場所を返す。

        follow_last が False なら、最後の部分がリンクでもたどらない（リンク自体を読む）。
        対応のない場所に入ったら OutsideMapError。途中が存在しなければ FileNotFoundError。
        """
        pending = path.split("/")[1:]
        done = "/"   # 解決し終えた部分（元のパス。リンクを含まない）
        links = 0
        while pending:
            name = pending.pop(0)
            if name in ("", "."):
                continue
            if name == "..":
                done = posixpath.dirname(done)
                continue
            candidate = posixpath.join(done, name)
            real = self.real_of(candidate)
            if real is None:
                if any(is_under(source, candidate) for source in self.sources):
                    done = candidate     # 対応の元のパスの上（/var/www:… の /var）。読まずにそのまま進む
                    continue
                raise OutsideMapError(errno.EXDEV, "対応（--map）のない場所です: " + candidate)
            if not pending and not follow_last:
                return real
            st = os.lstat(real)
            if stat.S_ISLNK(st.st_mode):
                links += 1
                if links > MAX_LINKS:
                    raise OSError(errno.ELOOP, "シンボリックリンクが多すぎます: " + path)
                target = os.readlink(real)
                if target.startswith("/"):
                    done = "/"
                pending = target.split("/") + pending
                continue
            done = candidate
        real = self.real_of(done)
        if real is None:
            raise OutsideMapError(errno.EXDEV, "対応（--map）のない場所です: " + done)
        return real
