"""Tests for terminal collection.

Live testing (see sys_workspace/README.md and the session log) already exercised
the real acceptance and rejection paths against genuine gnome-terminal tabs,
including a truncated-/proc/comm bug that only a real child of
gnome-terminal-server could have surfaced. What is covered here is the
branching that live testing could not safely reach: behaviour when D-Bus is
unavailable, and the grouping of tabs across several windows.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sessionctl_lib import collect  # noqa: E402


class IsLiveTabTest(unittest.TestCase):
    def test_dead_pid_is_rejected(self):
        rec = {"pid": 999999999, "screen": "/x"}
        self.assertFalse(collect._is_live_tab(rec, set()))

    def test_truncated_comm_is_accepted(self):
        """Regression test: /proc/<pid>/comm truncates to 15 bytes, so the
        parent's real name "gnome-terminal-server" is read back as
        "gnome-terminal-". An exact-match comparison here would silently
        reject every real terminal tab -- which is exactly what the live test
        against a genuine gnome-terminal window caught.
        """
        rec = {"pid": 1, "screen": "/x"}
        with mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(collect, "_proc_environ",
                                return_value={"GNOME_TERMINAL_SCREEN": "/x"}), \
             mock.patch.object(collect, "_parent_comm",
                                return_value="gnome-terminal-"):
            self.assertTrue(collect._is_live_tab(rec, set()))

    def test_wrong_parent_is_rejected(self):
        rec = {"pid": 1, "screen": "/x"}
        with mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(collect, "_proc_environ",
                                return_value={"GNOME_TERMINAL_SCREEN": "/x"}), \
             mock.patch.object(collect, "_parent_comm", return_value="bash"):
            self.assertFalse(collect._is_live_tab(rec, set()))

    def test_environ_mismatch_is_rejected(self):
        """A dead PID reused by an unrelated process must not be trusted."""
        rec = {"pid": 1, "screen": "/x"}
        with mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(collect, "_proc_environ",
                                return_value={"GNOME_TERMINAL_SCREEN": "/other"}), \
             mock.patch.object(collect, "_parent_comm",
                                return_value="gnome-terminal-"):
            self.assertFalse(collect._is_live_tab(rec, set()))

    def test_screen_not_in_dbus_list_is_rejected_when_list_available(self):
        rec = {"pid": 1, "screen": "/x"}
        with mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(collect, "_proc_environ",
                                return_value={"GNOME_TERMINAL_SCREEN": "/x"}), \
             mock.patch.object(collect, "_parent_comm",
                                return_value="gnome-terminal-"):
            self.assertFalse(collect._is_live_tab(rec, {"/other-screen"}))

    def test_dbus_unavailable_falls_back_to_proc_checks(self):
        """busctl can be missing or the bus down; that must degrade gracefully
        rather than reject every tab. dbus_tree() signals this with an empty
        set, and _is_live_tab treats an empty live_screens as "cannot verify"
        rather than "nothing is live".
        """
        rec = {"pid": 1, "screen": "/x"}
        with mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(collect, "_proc_environ",
                                return_value={"GNOME_TERMINAL_SCREEN": "/x"}), \
             mock.patch.object(collect, "_parent_comm",
                                return_value="gnome-terminal-"):
            self.assertTrue(collect._is_live_tab(rec, set()))


class GroupTest(unittest.TestCase):
    def test_no_tabs(self):
        self.assertEqual(collect._group([], 0), [])

    def test_single_window_keeps_all_tabs_together(self):
        tabs = [{"pid": i} for i in range(4)]
        groups = collect._group(tabs, 1)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["tabs"], tabs)

    def test_even_split_across_windows(self):
        tabs = [{"pid": i} for i in range(4)]
        groups = collect._group(tabs, 2)
        self.assertEqual([g["tabs"] for g in groups], [tabs[:2], tabs[2:]])

    def test_uneven_split_never_drops_a_tab(self):
        """5 tabs, 2 windows: every tab must end up somewhere."""
        tabs = [{"pid": i} for i in range(5)]
        groups = collect._group(tabs, 2)
        seen = [t for g in groups for t in g["tabs"]]
        self.assertEqual(seen, tabs)

    def test_more_windows_than_tabs_produces_no_empty_groups(self):
        tabs = [{"pid": 1}]
        groups = collect._group(tabs, 3)
        self.assertEqual(len(groups), 1)

    def test_group_indices_are_dense(self):
        """Skipping empty slots must not leave gaps in the index sequence."""
        tabs = [{"pid": i} for i in range(3)]
        groups = collect._group(tabs, 5)
        self.assertEqual([g["index"] for g in groups], list(range(len(groups))))


class AtspiWindowTabsTest(unittest.TestCase):
    """Every failure mode has to return None, never raise or hang.

    Atspi.init() aborts the process (core dump, not an exception) when the
    accessibility bus is missing, so the probe runs in a subprocess and this
    covers how its outcomes are interpreted. Confirmed separately against a
    real bus-less environment: the child dumps core, the parent survives and
    gets None.
    """

    def run_with(self, **kwargs):
        completed = mock.Mock(**kwargs)
        with mock.patch.object(collect.subprocess, "run", return_value=completed):
            return collect.atspi_window_tabs()

    def test_valid_output_is_parsed(self):
        out = '[[{"title":"a","conda":"base","cwd":"~"}],[{"title":"b","conda":null,"cwd":null}]]'
        self.assertEqual(
            self.run_with(returncode=0, stdout=out),
            [[{"title": "a", "conda": "base", "cwd": "~"}],
             [{"title": "b", "conda": None, "cwd": None}]],
        )

    def test_child_crash_returns_none(self):
        self.assertIsNone(self.run_with(returncode=-6, stdout=""))

    def test_empty_output_returns_none(self):
        self.assertIsNone(self.run_with(returncode=0, stdout=""))

    def test_json_null_returns_none(self):
        """The probe prints null when no terminal app is on the bus."""
        self.assertIsNone(self.run_with(returncode=0, stdout="null"))

    def test_malformed_json_returns_none(self):
        self.assertIsNone(self.run_with(returncode=0, stdout="{not json"))

    def test_unexpected_shape_returns_none(self):
        self.assertIsNone(self.run_with(returncode=0, stdout='["a","b"]'))

    def test_non_dict_slots_return_none(self):
        """The older probe emitted bare title strings; refuse that shape
        rather than half-interpreting it."""
        self.assertIsNone(self.run_with(returncode=0, stdout='[["a","b"]]'))

    def test_timeout_returns_none(self):
        with mock.patch.object(
            collect.subprocess, "run",
            side_effect=collect.subprocess.TimeoutExpired("x", 15),
        ):
            self.assertIsNone(collect.atspi_window_tabs())

    def test_missing_interpreter_returns_none(self):
        with mock.patch.object(collect.subprocess, "run", side_effect=OSError):
            self.assertIsNone(collect.atspi_window_tabs())


class GroupWithAtspiTest(unittest.TestCase):
    """Uneven windows used to come back evened out, then evened-out contents.

    Three windows holding 5, 6 and 7 tabs were recorded as 6/6/6, because the
    split was ceiling division over the window count. Fixing the shape from
    AT-SPI left a second bug: matching used only the tab titles, which encode
    the cwd, so when every tab sat in ~ nothing discriminated a torch209 tab
    from a base one and slots were filled in PID order -- three of five
    windows came back with the wrong envs. The prompt carries the env even
    when the title doesn't.
    """

    HOME = os.path.expanduser("~")

    def tab(self, pid, conda, cwd=None):
        return {"pid": pid, "conda": conda, "cwd": cwd or self.HOME}

    def slot(self, conda, cwd="~"):
        return {"title": f"user@host: {cwd}", "conda": conda, "cwd": cwd}

    def test_uneven_shape_is_preserved(self):
        tabs = [self.tab(i, "torch209") for i in range(18)]
        slots = [[self.slot("torch209")] * n for n in (5, 6, 7)]
        groups = collect._group(tabs, 3, window_slots=slots)
        self.assertEqual([len(g["tabs"]) for g in groups], [5, 6, 7])

    def test_envs_land_in_the_right_windows(self):
        """The reported bug: same cwd everywhere, different envs."""
        tabs = [self.tab(1, "base"), self.tab(2, "torch209"),
                self.tab(3, "torch209"), self.tab(4, "base")]
        slots = [[self.slot("torch209"), self.slot("torch209")],
                 [self.slot("base"), self.slot("base")]]
        groups = collect._group(tabs, 2, window_slots=slots)
        self.assertEqual([t["conda"] for t in groups[0]["tabs"]],
                         ["torch209", "torch209"])
        self.assertEqual([t["conda"] for t in groups[1]["tabs"]],
                         ["base", "base"])

    def test_cwd_still_discriminates_when_envs_match(self):
        proj = self.tab(1, "torch209", f"{self.HOME}/Projects/thing")
        plain = self.tab(2, "torch209")
        slots = [[self.slot("torch209")],
                 [self.slot("torch209", "~/Projects/thing")]]
        groups = collect._group([plain, proj], 2, window_slots=slots)
        self.assertEqual(groups[0]["tabs"], [plain])
        self.assertEqual(groups[1]["tabs"], [proj])

    def test_exact_matches_win_over_cwd_only_matches_globally(self):
        """A cwd-only match must not consume a tab that another window needs
        for an exact (cwd, env) match -- hence two passes, not one.
        """
        base_tab = self.tab(1, "base")
        torch_tab = self.tab(2, "torch209")
        slots = [[self.slot("base")], [self.slot("torch209")]]
        groups = collect._group([base_tab, torch_tab], 2, window_slots=slots)
        self.assertEqual(groups[0]["tabs"], [base_tab])
        self.assertEqual(groups[1]["tabs"], [torch_tab])

    def test_unreadable_prompt_still_gets_a_tab(self):
        """A tab running a command has no prompt to parse; it must still be
        placed rather than dropped.
        """
        tabs = [self.tab(1, "base"), self.tab(2, "torch209")]
        slots = [[{"title": "", "conda": None, "cwd": None}],
                 [self.slot("torch209")]]
        groups = collect._group(tabs, 2, window_slots=slots)
        placed = [t for g in groups for t in g["tabs"]]
        self.assertEqual(len(placed), 2)
        self.assertEqual(groups[1]["tabs"][0]["conda"], "torch209")

    def test_empty_env_and_none_env_are_the_same(self):
        tabs = [{"pid": 1, "conda": None, "cwd": self.HOME}]
        slots = [[{"title": "", "conda": "", "cwd": "~"}]]
        groups = collect._group(tabs, 1, window_slots=slots)
        self.assertEqual(len(groups[0]["tabs"]), 1)

    def test_every_tab_is_placed_exactly_once(self):
        tabs = [self.tab(i, "torch209") for i in range(18)]
        slots = [[self.slot("torch209")] * n for n in (5, 6, 7)]
        groups = collect._group(tabs, 3, window_slots=slots)
        placed = [t for g in groups for t in g["tabs"]]
        self.assertEqual(len(placed), 18)
        self.assertEqual({id(t) for t in placed}, {id(t) for t in tabs})

    def test_falls_back_to_even_split_without_atspi(self):
        tabs = [self.tab(i, "base") for i in range(4)]
        groups = collect._group(tabs, 2, window_slots=None)
        self.assertEqual([len(g["tabs"]) for g in groups], [2, 2])

    def test_more_shells_than_slots_keeps_every_shell(self):
        tabs = [self.tab(i, "base") for i in range(5)]
        groups = collect._group(tabs, 1, window_slots=[[self.slot("base")] * 3])
        placed = [t for g in groups for t in g["tabs"]]
        self.assertEqual(len(placed), 5)

    def test_fewer_shells_than_slots_produces_no_empty_windows(self):
        tabs = [self.tab(i, "base") for i in range(2)]
        slots = [[self.slot("base")] * 2, [self.slot("base")] * 2]
        groups = collect._group(tabs, 2, window_slots=slots)
        self.assertTrue(all(g["tabs"] for g in groups))
        self.assertEqual(sum(len(g["tabs"]) for g in groups), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
