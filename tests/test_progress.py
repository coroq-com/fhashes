"""進み具合の表示（fhashes/progress.py）のテスト。"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402,F401  （fhashes を import できるようにするため）

from fhashes.progress import Progress  # noqa: E402


class FakeTerminal(io.StringIO):
    def isatty(self):
        return True


class ProgressTest(unittest.TestCase):
    def test_terminal_shows_one_line_and_clears_it(self):
        stream = FakeTerminal()
        progress = Progress("記録の取得", 4, stream)
        progress.update(0, "0MB / 20MB", force=True)
        progress.update(4, "20MB / 20MB")
        progress.close()
        text = stream.getvalue()
        self.assertIn("\r記録の取得:   0% (0/4)  0MB / 20MB\033[K", text)
        self.assertIn("\r記録の取得: 100% (4/4)  20MB / 20MB\033[K", text)
        self.assertTrue(text.endswith("\r\033[K"))     # 終わったら行を消す
        self.assertNotIn("\n", text)                   # 改行しない（1 行を上書きする）

    def test_nothing_when_not_a_terminal(self):
        stream = io.StringIO()
        progress = Progress("記録の取得", 4, stream)
        progress.update(2, force=True)
        progress.close()
        self.assertEqual(stream.getvalue(), "")

    def test_nothing_when_total_is_zero(self):
        stream = FakeTerminal()
        progress = Progress("記録の取得", 0, stream)
        progress.update(0, force=True)
        progress.close()
        self.assertEqual(stream.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
