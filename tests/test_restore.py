"""Tests for restore command-building.

Live testing already confirmed the mechanics these functions rely on:
gnome-terminal only honours ONE trailing "-- COMMAND" for the whole
invocation (a second "--tab ... -- ..." block is silently swallowed as
inert positional arguments to the first command, and every tab after the
first never opens -- confirmed by writing a marker file per tab and
observing only the first was created), --command=STRING works correctly
per-tab despite its deprecation notice, and a --working-directory containing
a space round-trips correctly through the shlex.quote-based command
construction. `sessionctl restore --dry-run` was also run against a real
captured snapshot end to end. What's covered here is the argv-construction
logic itself: window grouping, quoting, and the --chrome-urls /
new-window-flag branches, none of which need a real process to verify.
"""

import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sessionctl_lib import restore  # noqa: E402


class TerminalCommandsTest(unittest.TestCase):
    def test_no_windows_no_commands(self):
        self.assertEqual(restore.terminal_commands({"windows": []}), [])

    def test_empty_window_is_skipped(self):
        self.assertEqual(
            restore.terminal_commands({"windows": [{"index": 0, "tabs": []}]}), []
        )

    def test_one_window_one_tab(self):
        terminals = {"windows": [{"index": 0, "tabs": [
            {"cwd": "/tmp", "conda": None}
        ]}]}
        [argv] = restore.terminal_commands(terminals)
        self.assertEqual(argv[0], "gnome-terminal")
        self.assertEqual(argv[1], "--window")
        self.assertIn("--working-directory=/tmp", argv)
        self.assertEqual(sum(a == "--window" for a in argv), 1)
        self.assertEqual(sum(a == "--tab" for a in argv), 0)

    def test_second_tab_uses_tab_not_window(self):
        terminals = {"windows": [{"index": 0, "tabs": [
            {"cwd": "/a", "conda": None},
            {"cwd": "/b", "conda": None},
        ]}]}
        [argv] = restore.terminal_commands(terminals)
        self.assertEqual(argv.count("--window"), 1)
        self.assertEqual(argv.count("--tab"), 1)
        # --window must come before --tab, matching tab order
        self.assertLess(argv.index("--window"), argv.index("--tab"))

    def test_every_tab_gets_its_own_command_flag(self):
        """This is the property that live testing caught missing when a
        trailing "--" was used instead: every tab must carry its own
        --command=, not just the first one.
        """
        terminals = {"windows": [{"index": 0, "tabs": [
            {"cwd": "/a", "conda": None},
            {"cwd": "/b", "conda": None},
            {"cwd": "/c", "conda": None},
        ]}]}
        [argv] = restore.terminal_commands(terminals)
        command_flags = [a for a in argv if a.startswith("--command=")]
        self.assertEqual(len(command_flags), 3)

    def test_two_windows_produce_two_commands(self):
        terminals = {"windows": [
            {"index": 0, "tabs": [{"cwd": "/a", "conda": None}]},
            {"index": 1, "tabs": [{"cwd": "/b", "conda": None}]},
        ]}
        commands = restore.terminal_commands(terminals)
        self.assertEqual(len(commands), 2)

    def test_conda_env_is_embedded_in_the_command(self):
        terminals = {"windows": [{"index": 0, "tabs": [
            {"cwd": "/tmp", "conda": "torch212"}
        ]}]}
        [argv] = restore.terminal_commands(terminals)
        command = next(a for a in argv if a.startswith("--command="))
        self.assertIn("SESSIONCTL_CONDA=torch212", command)
        self.assertIn("exec bash -i", command)

    def test_no_conda_omits_sessionctl_conda(self):
        terminals = {"windows": [{"index": 0, "tabs": [
            {"cwd": "/tmp", "conda": None}
        ]}]}
        [argv] = restore.terminal_commands(terminals)
        command = next(a for a in argv if a.startswith("--command="))
        self.assertNotIn("SESSIONCTL_CONDA", command)

    def test_missing_cwd_omits_working_directory_flag(self):
        terminals = {"windows": [{"index": 0, "tabs": [
            {"cwd": None, "conda": None}
        ]}]}
        [argv] = restore.terminal_commands(terminals)
        self.assertFalse(any(a.startswith("--working-directory=") for a in argv))

    def test_cwd_with_special_characters_is_shell_safe(self):
        """--command=STRING is parsed twice before the conda name is ever
        used: once by gnome-terminal's own GLib shell-parser to split
        --command's value into ["bash", "-c", <script>], then again by
        bash -c itself to parse <script>'s "VAR=value" assignment. A conda
        env name with an embedded quote must survive both stages intact --
        confirmed here by actually performing both unwrapping passes, not
        just checking the raw string looks plausible.
        """
        terminals = {"windows": [{"index": 0, "tabs": [
            {"cwd": "/tmp/dir with spaces", "conda": "env'with'quotes"}
        ]}]}
        [argv] = restore.terminal_commands(terminals)
        import shlex
        command = next(a for a in argv if a.startswith("--command="))
        outer = command[len("--command="):]

        stage1 = shlex.split(outer)  # gnome-terminal's own parse
        self.assertEqual(stage1[:2], ["bash", "-c"])
        stage2 = shlex.split(stage1[2])  # bash -c's parse of the script
        recovered = stage2[0].split("=", 1)[1]
        self.assertEqual(recovered, "env'with'quotes")


class VscodeCommandsTest(unittest.TestCase):
    def test_first_window_gets_no_new_window_flag(self):
        with tempfile.TemporaryDirectory() as d:
            vscode = {"windows": [{"path": d}]}
            [argv] = restore.vscode_commands(vscode)
        self.assertEqual(argv, ["code", d])

    def test_second_window_gets_new_window_flag(self):
        with tempfile.TemporaryDirectory() as d1, \
             tempfile.TemporaryDirectory() as d2:
            vscode = {"windows": [{"path": d1}, {"path": d2}]}
            commands = restore.vscode_commands(vscode)
        self.assertEqual(commands[0], ["code", d1])
        self.assertEqual(commands[1], ["code", "-n", d2])

    def test_nonexistent_folder_is_skipped(self):
        """A folder recorded in an old snapshot that no longer exists must
        not produce a `code` invocation that just errors out.
        """
        vscode = {"windows": [{"path": "/no/such/folder/at/all"}]}
        self.assertEqual(restore.vscode_commands(vscode), [])

    def test_no_windows(self):
        self.assertEqual(restore.vscode_commands({"windows": []}), [])


class ChromeCommandsTest(unittest.TestCase):
    def test_no_profiles_no_commands(self):
        self.assertEqual(restore.chrome_commands({"profiles": []}), [])

    def test_default_relies_on_own_restore_no_urls_passed(self):
        chrome = {"profiles": [{"profile": "Default", "urls": ["https://x.com"]}]}
        [argv] = restore.chrome_commands(chrome, use_recorded_urls=False)
        self.assertEqual(argv, ["google-chrome"])

    def test_default_profile_gets_no_profile_directory_flag(self):
        chrome = {"profiles": [{"profile": "Default", "urls": []}]}
        [argv] = restore.chrome_commands(chrome)
        self.assertNotIn("--profile-directory=Default", argv)

    def test_non_default_profile_gets_profile_directory_flag(self):
        chrome = {"profiles": [{"profile": "Profile 1", "urls": []}]}
        [argv] = restore.chrome_commands(chrome)
        self.assertIn("--profile-directory=Profile 1", argv)

    def test_use_recorded_urls_appends_them(self):
        chrome = {"profiles": [
            {"profile": "Default", "urls": ["https://a.com", "https://b.com"]}
        ]}
        [argv] = restore.chrome_commands(chrome, use_recorded_urls=True)
        self.assertEqual(argv, ["google-chrome", "https://a.com", "https://b.com"])

    def test_use_recorded_urls_skips_profile_with_none(self):
        chrome = {"profiles": [{"profile": "Default", "urls": []}]}
        self.assertEqual(
            restore.chrome_commands(chrome, use_recorded_urls=True), []
        )

    def test_multiple_profiles_produce_multiple_commands(self):
        chrome = {"profiles": [
            {"profile": "Default", "urls": []},
            {"profile": "Profile 1", "urls": []},
        ]}
        commands = restore.chrome_commands(chrome)
        self.assertEqual(len(commands), 2)


class LaunchTest(unittest.TestCase):
    def test_launch_does_not_block_and_discards_output(self):
        with mock.patch("subprocess.Popen") as popen:
            restore.launch(["true"])
        popen.assert_called_once()
        _, kwargs = popen.call_args
        self.assertTrue(kwargs.get("start_new_session"))
        import subprocess
        self.assertEqual(kwargs.get("stdout"), subprocess.DEVNULL)
        self.assertEqual(kwargs.get("stderr"), subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SnapshotAgeTest(unittest.TestCase):
    """Restoring is the moment a stopped `save` becomes visible.

    A TypeError in digest() killed every save for 25 hours and the failure
    lived only in the timer's journal. The restore that followed replayed a
    day-old session, and the windows coming back wrong was the first and
    only symptom. The age of what is about to be replayed is cheap to state.
    """

    def setUp(self):
        from sessionctl_lib import cli
        self.cli = cli
        self.now = datetime.datetime(2026, 9, 16, 16, 0, 0).astimezone()

    def snap(self, minutes_ago):
        taken = self.now - datetime.timedelta(minutes=minutes_ago)
        return {"ts": taken.isoformat()}

    def test_fresh_snapshot_has_small_age(self):
        self.assertLess(self.cli.snapshot_age(self.snap(1), self.now), 120)

    def test_day_old_snapshot_is_past_the_threshold(self):
        age = self.cli.snapshot_age(self.snap(25 * 60), self.now)
        self.assertGreater(age, self.cli.STALE_AFTER)

    def test_missing_timestamp_is_not_an_error(self):
        self.assertIsNone(self.cli.snapshot_age({}, self.now))
        self.assertIsNone(self.cli.snapshot_age(None, self.now))

    def test_unparseable_timestamp_is_not_an_error(self):
        self.assertIsNone(self.cli.snapshot_age({"ts": "not a date"}, self.now))

    def test_age_formatting_picks_sensible_units(self):
        self.assertIn("minutes", self.cli._format_age(300))
        self.assertIn("hours", self.cli._format_age(7200))
        self.assertIn("days", self.cli._format_age(200000))

    def test_warning_only_fires_when_stale(self):
        import io, contextlib
        for minutes, expect in ((1, False), (25 * 60, True)):
            buf = io.StringIO()
            snap = self.snap(minutes)
            with contextlib.redirect_stderr(buf), \
                 mock.patch.object(self.cli, "snapshot_age",
                                   return_value=minutes * 60):
                self.cli._warn_if_stale(snap)
            self.assertEqual("warning" in buf.getvalue(), expect)
