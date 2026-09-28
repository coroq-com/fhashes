"""日時の変換。

保存するデータ（スナップショット、ファイル名）の時刻は、すべて UTC の秒までの文字列
（例: 2026-09-23T01:00:00Z）にする。1 秒未満は切り捨てる（記録は 1 時間おきなので、秒で足りる）。この形式は文字列の大小比較がそのまま時刻の前後になる。

タイムゾーンは、人とのやり取り（表の表示と、コマンドで日時を入力するとき）にだけ使う。
使うのは実行環境のタイムゾーン（環境変数 TZ か OS の設定）で、設定ファイルには持たない。
"""

from datetime import datetime, timedelta, timezone


def to_utc_text(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> str:
    return to_utc_text(datetime.now(timezone.utc))


def parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def seconds_between(earlier: str, later: str) -> float:
    return (parse_utc(later) - parse_utc(earlier)).total_seconds()


def to_compact(utc_text: str) -> str:
    """ファイル名用の短い形式（秒まで）: 20260923T010000Z"""
    return parse_utc(utc_text).strftime("%Y%m%dT%H%M%SZ")


def from_compact(compact_text: str) -> str:
    """to_compact の逆。20260923T010000Z → 2026-09-23T01:00:00Z"""
    dt = datetime.strptime(compact_text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return to_utc_text(dt)


# ----------------------------------------------------------------------
# 人とのやり取り（実行環境のタイムゾーンを使う）
# ----------------------------------------------------------------------

def format_local(utc_text) -> str:
    """表示用: 実行環境のタイムゾーンで "2026-09-23 10:00:00" の形式にする。None なら空文字。"""
    if utc_text is None:
        return ""
    return parse_utc(utc_text).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def local_zone_label(utc_text=None) -> str:
    """表示に使っているタイムゾーンの説明（例: "JST（+09:00）"）。utc_text の時点での値（省略時は今）。"""
    dt = parse_utc(utc_text) if utc_text else datetime.now(timezone.utc)
    local = dt.astimezone()
    offset = local.strftime("%z")
    return "%s（%s:%s）" % (local.tzname(), offset[:3], offset[3:])


def parse_user_time(text: str, end_of_range: bool = False) -> str:
    """コマンドで指定された日時を UTC の文字列にする。

    受け付ける形式:
      "2026-09-23"、"2026-09-23 10:00"、"2026-09-23T10:00:00"   … 実行環境のタイムゾーンで解釈する
      "2026-09-23T10:00+09:00"、"2026-09-23T01:00Z"             … 書かれたタイムゾーンで解釈する
    end_of_range=True で日付だけが指定された場合は、その日の終わり（翌日 0:00）を返す。
    """
    value = text.strip()
    if value.endswith("Z") or value.endswith("z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("日時の形式が不正です: " + text
                         + "（例: 2026-09-23、'2026-09-23 10:00'、2026-09-23T10:00+09:00、2026-09-23T01:00Z）")
    if end_of_range and len(value) == len("2026-09-23"):
        dt = dt + timedelta(days=1)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # タイムゾーンの書かれていない日時は、実行環境のタイムゾーンとみなす
    return to_utc_text(dt)

