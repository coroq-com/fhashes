"""ワイルドカード処理（fhashes/patterns.py）のテスト。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fhashes import patterns  # noqa: E402


def matches(pattern, path):
    return patterns.glob_to_regex(pattern).match(path) is not None


class GlobToRegexTest(unittest.TestCase):
    def test_star_does_not_cross_slash(self):
        self.assertTrue(matches("/etc/*.conf", "/etc/a.conf"))
        self.assertFalse(matches("/etc/*.conf", "/etc/x/a.conf"))

    def test_double_star_matches_zero_or_more_dirs(self):
        self.assertTrue(matches("/etc/**/*.conf", "/etc/a.conf"))
        self.assertTrue(matches("/etc/**/*.conf", "/etc/x/y/a.conf"))
        self.assertFalse(matches("/etc/**/*.conf", "/etc/x/a.txt"))

    def test_trailing_double_star_matches_dir_itself_and_below(self):
        self.assertTrue(matches("/var/cache/**", "/var/cache"))
        self.assertTrue(matches("/var/cache/**", "/var/cache/a/b"))
        self.assertFalse(matches("/var/cache/**", "/var/cachex"))

    def test_question_and_class(self):
        self.assertTrue(matches("/a/?.txt", "/a/1.txt"))
        self.assertFalse(matches("/a/?.txt", "/a/12.txt"))
        self.assertTrue(matches("/a/[ab].txt", "/a/b.txt"))
        self.assertFalse(matches("/a/[!ab].txt", "/a/b.txt"))
        self.assertTrue(matches("/a/[!ab].txt", "/a/c.txt"))
        self.assertTrue(matches("/a/[^b].txt", "/a/^.txt"))  # ^ は文字として扱う

    def test_regex_special_chars_are_literal(self):
        self.assertTrue(matches("/a/b+c(1).txt", "/a/b+c(1).txt"))
        self.assertFalse(matches("/a/b.txt", "/a/bxtxt"))

    def test_invalid_patterns(self):
        for pattern in ["etc/*", "/", "/etc//a", "/etc/", "/etc/../a", "/etc/a**b"]:
            with self.assertRaises(ValueError, msg=pattern):
                patterns.check_pattern(pattern)


class PruningTest(unittest.TestCase):
    def test_pattern_root(self):
        self.assertEqual(patterns.pattern_root("/var/www/app/**/*.php"), "/var/www/app")
        self.assertEqual(patterns.pattern_root("/etc/passwd"), "/etc/passwd")
        self.assertEqual(patterns.pattern_root("/**"), "/")
        self.assertEqual(patterns.pattern_root("/home/*/.ssh/*"), "/home")

    def test_dir_may_contain_match(self):
        inc = patterns.compile_include("/home/*/.ssh/*")
        self.assertTrue(patterns.dir_may_contain_match(inc, "/home"))
        self.assertTrue(patterns.dir_may_contain_match(inc, "/home/alice"))
        self.assertTrue(patterns.dir_may_contain_match(inc, "/home/alice/.ssh"))
        self.assertFalse(patterns.dir_may_contain_match(inc, "/home/alice/src"))
        self.assertFalse(patterns.dir_may_contain_match(inc, "/home/alice/.ssh/sub"))

        inc = patterns.compile_include("/var/**/*.php")
        self.assertTrue(patterns.dir_may_contain_match(inc, "/var/a/b/c"))
        self.assertFalse(patterns.dir_may_contain_match(inc, "/usr"))

    def test_unique_roots(self):
        includes = [patterns.compile_include(p) for p in ["/etc/**", "/etc/nginx/*.conf", "/usr/local/bin/*"]]
        self.assertEqual([r["path"] for r in patterns.unique_roots(includes)], ["/etc", "/usr/local/bin"])
        includes = [patterns.compile_include(p) for p in ["/**", "/etc/*"]]
        self.assertEqual([r["path"] for r in patterns.unique_roots(includes)], ["/"])

    def test_unique_roots_file_only(self):
        # ワイルドカードのないパターンは 1 つのファイルを指す起点。同じ起点をディレクトリとしても使えば違う
        includes = [patterns.compile_include(p) for p in ["/etc/passwd", "/var/www/**"]]
        self.assertEqual(patterns.unique_roots(includes),
                         [{"path": "/etc/passwd", "file_only": True}, {"path": "/var/www", "file_only": False}])
        includes = [patterns.compile_include(p) for p in ["/opt/app", "/opt/app/**"]]
        self.assertEqual(patterns.unique_roots(includes), [{"path": "/opt/app", "file_only": False}])


if __name__ == "__main__":
    unittest.main()
