"""Tests for the VSCode, Chrome and Firefox collectors added in step 3.

Live testing (see the session log and README) already confirmed collect_vscode
against the two real open windows and collect_chrome against the real Chrome
Session_ file -- which is how a real bug got caught: an early URL regex used a
negated character class against the raw latin-1-decoded bytes, and since
almost every byte decodes to *some* printable latin-1 character, it glued each
URL to the binary pickle data that follows it in the file rather than stopping
at the URL's actual end. What is covered here is what live testing could not
safely reach: missing/malformed source files, multiple Chrome profiles, and
the VSCode openedWindows/lastActiveWindow dedup logic.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sessionctl_lib import collect  # noqa: E402


class VscodeTest(unittest.TestCase):
    def _with_storage(self, data):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        )
        json.dump(data, tmp)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return mock.patch.object(collect, "VSCODE_STORAGE", tmp.name)

    def test_missing_file_returns_empty(self):
        with mock.patch.object(collect, "VSCODE_STORAGE", "/no/such/file"):
            result = collect.collect_vscode()
        self.assertEqual(result["windows"], [])

    def test_malformed_json_returns_empty(self):
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        tmp.write("{not valid json")
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        with mock.patch.object(collect, "VSCODE_STORAGE", tmp.name):
            result = collect.collect_vscode()
        self.assertEqual(result["windows"], [])

    def test_reads_opened_windows(self):
        data = {
            "windowsState": {
                "openedWindows": [
                    {"folder": "file:///home/x/a"},
                    {"folder": "file:///home/x/b"},
                ],
                "lastActiveWindow": {"folder": "file:///home/x/b"},
            }
        }
        with self._with_storage(data):
            result = collect.collect_vscode()
        paths = [w["path"] for w in result["windows"]]
        self.assertEqual(paths, ["/home/x/a", "/home/x/b"])

    def test_last_active_window_added_when_not_duplicate(self):
        """The one-window case: nothing proves whether VSCode still populates
        openedWindows then, so lastActiveWindow is folded in defensively
        rather than assumed redundant.
        """
        data = {
            "windowsState": {
                "openedWindows": [],
                "lastActiveWindow": {"folder": "file:///home/x/only"},
            }
        }
        with self._with_storage(data):
            result = collect.collect_vscode()
        self.assertEqual(result["windows"], [
            {"folder": "file:///home/x/only", "path": "/home/x/only"}
        ])

    def test_no_windows_open(self):
        with self._with_storage({"windowsState": {}}):
            result = collect.collect_vscode()
        self.assertEqual(result["windows"], [])

    def test_unchanged_source_reuses_previous_without_reparsing(self):
        data = {"windowsState": {"openedWindows": [{"folder": "file:///a"}]}}
        with self._with_storage(data):
            first = collect.collect_vscode()
            with mock.patch("builtins.open", side_effect=AssertionError("reparsed")):
                second = collect.collect_vscode(previous=first)
        self.assertEqual(first["windows"], second["windows"])

    def test_changed_source_does_reparse(self):
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump({"windowsState": {"openedWindows": [{"folder": "file:///a"}]}}, tmp)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        with mock.patch.object(collect, "VSCODE_STORAGE", tmp.name):
            first = collect.collect_vscode()
            with open(tmp.name, "w") as f:
                json.dump({"windowsState": {"openedWindows": [{"folder": "file:///b"}]}}, f)
            second = collect.collect_vscode(previous=first)
        self.assertEqual(
            [w["path"] for w in second["windows"]], ["/b"]
        )


class SnssUrlsTest(unittest.TestCase):
    def test_extracts_clean_urls_from_binary_noise(self):
        """Regression test for the negated-character-class bug: a URL
        embedded in bytes that are NOT quotes/whitespace/backslash (the first
        version's stop condition) but also are not valid URL characters
        must not be glued onto the match.
        """
        blob = (
            b"junk\x00\x01https://example.com/path\xff\xfe\x02more junk"
        )
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(blob)
        self.addCleanup(os.unlink, f.name)
        urls = collect._snss_urls(f.name)
        self.assertEqual(urls, ["https://example.com/path"])

    def test_dedupes_preserving_first_seen_order(self):
        blob = b"https://b.com https://a.com https://b.com https://c.com"
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(blob)
        self.addCleanup(os.unlink, f.name)
        urls = collect._snss_urls(f.name)
        self.assertEqual(urls, ["https://b.com", "https://a.com", "https://c.com"])

    def test_strips_trailing_punctuation(self):
        blob = b"see https://example.com/x, and https://example.com/y."
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(blob)
        self.addCleanup(os.unlink, f.name)
        urls = collect._snss_urls(f.name)
        self.assertEqual(urls, ["https://example.com/x", "https://example.com/y"])

    def test_missing_file_returns_empty(self):
        self.assertEqual(collect._snss_urls("/no/such/file"), [])

    def test_respects_limit(self):
        blob = b" ".join(f"https://x.com/{i}".encode() for i in range(20))
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(blob)
        self.addCleanup(os.unlink, f.name)
        urls = collect._snss_urls(f.name, limit=5)
        self.assertEqual(len(urls), 5)


class ChromeProfilesTest(unittest.TestCase):
    def test_finds_default_and_numbered_profiles_with_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("Default", "Profile 1", "Profile 2", "System Profile"):
                os.makedirs(os.path.join(d, name, "Sessions"), exist_ok=True)
            # A profile-shaped dir with no Sessions subdir must be skipped --
            # it cannot have a current-session file to read.
            os.makedirs(os.path.join(d, "Profile 3"), exist_ok=True)
            with mock.patch.object(collect, "CHROME_CONFIG_DIR", d):
                found = collect._chrome_profiles()
        self.assertEqual(found, ["Default", "Profile 1", "Profile 2"])

    def test_no_config_dir_returns_empty(self):
        with mock.patch.object(collect, "CHROME_CONFIG_DIR", "/no/such/dir"):
            self.assertEqual(collect._chrome_profiles(), [])

    def test_collect_chrome_skips_profile_with_no_session_file(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "Default", "Sessions"))
            with mock.patch.object(collect, "CHROME_CONFIG_DIR", d):
                result = collect.collect_chrome()
        self.assertEqual(result["profiles"], [])

    def test_reuses_unchanged_profile_without_rescanning(self):
        with tempfile.TemporaryDirectory() as d:
            sess_dir = os.path.join(d, "Default", "Sessions")
            os.makedirs(sess_dir)
            sess_file = os.path.join(sess_dir, "Session_1")
            with open(sess_file, "wb") as f:
                f.write(b"https://example.com")

            with mock.patch.object(collect, "CHROME_CONFIG_DIR", d):
                first = collect.collect_chrome()
                with mock.patch.object(
                    collect, "_snss_urls",
                    side_effect=AssertionError("rescanned"),
                ):
                    second = collect.collect_chrome(previous=first)
        self.assertEqual(first["profiles"], second["profiles"])

    def test_new_session_file_triggers_rescan(self):
        with tempfile.TemporaryDirectory() as d:
            sess_dir = os.path.join(d, "Default", "Sessions")
            os.makedirs(sess_dir)
            with open(os.path.join(sess_dir, "Session_1"), "wb") as f:
                f.write(b"https://old.example.com")

            with mock.patch.object(collect, "CHROME_CONFIG_DIR", d):
                first = collect.collect_chrome()
                with open(os.path.join(sess_dir, "Session_2"), "wb") as f:
                    f.write(b"https://new.example.com")
                second = collect.collect_chrome(previous=first)

        self.assertEqual(first["profiles"][0]["urls"], ["https://old.example.com"])
        self.assertEqual(second["profiles"][0]["urls"], ["https://new.example.com"])


class FirefoxTest(unittest.TestCase):
    def test_not_running_no_profiles_ini(self):
        with mock.patch.object(collect, "_run", return_value=(1, "")), \
             mock.patch.object(collect, "FIREFOX_PROFILES_INI", "/no/such/file"):
            result = collect.collect_firefox()
        self.assertFalse(result["running"])
        self.assertIsNone(result["profile"])

    def test_picks_the_default_profile(self):
        ini = (
            "[Profile0]\nName=other\nPath=other.default\nDefault=0\n\n"
            "[Profile1]\nName=main\nPath=doe8v6so.default\nDefault=1\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False) as f:
            f.write(ini)
        self.addCleanup(os.unlink, f.name)
        with mock.patch.object(collect, "_run", return_value=(0, "")), \
             mock.patch.object(collect, "FIREFOX_PROFILES_INI", f.name):
            result = collect.collect_firefox()
        self.assertTrue(result["running"])
        self.assertEqual(result["profile"], "doe8v6so.default")


if __name__ == "__main__":
    unittest.main(verbosity=2)
