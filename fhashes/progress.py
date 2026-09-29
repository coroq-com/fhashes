"""進み具合の表示（git と同じ振る舞い）。

標準エラー出力が端末のときだけ、1 行を上書きしながら進み具合を出し、終わったらその行を消す。
パイプや cron など、端末でないときは何も出さない（スクリプトから使っても出力が汚れないように）。
"""

import sys
import time

UPDATE_INTERVAL = 0.1  # 書き直す間隔の下限（秒）。細かく書き直しすぎないように


class Progress:
    def __init__(self, label: str, total: int, stream=None):
        self.label = label
        self.total = total
        self.stream = stream or sys.stderr
        self.enabled = self.stream.isatty() and total > 0
        self.shown = False
        self.last_update = 0.0

    def update(self, done: int, detail: str = "", force: bool = False) -> None:
        """done 件まで終わったことを表示する。detail は後ろに付け加える文字列。"""
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and done < self.total and now - self.last_update < UPDATE_INTERVAL:
            return
        self.last_update = now
        percent = done * 100 // self.total
        text = "%s: %3d%% (%d/%d)" % (self.label, percent, done, self.total)
        if detail:
            text += "  " + detail
        # \r で行頭に戻り、\033[K で行末まで消してから書く（前の表示が長くても残らない）
        self.stream.write("\r" + text + "\033[K")
        self.stream.flush()
        self.shown = True

    def close(self) -> None:
        """表示した行を消す。"""
        if self.shown:
            self.stream.write("\r\033[K")
            self.stream.flush()
            self.shown = False
