"""Tests for workspace layout matching and compaction.

Live testing already covered everything Wnck-dependent, and found real bugs a
pure unit test never could: Wnck's action methods (move_to_workspace,
maximize, set_geometry) are asynchronous X11 requests that silently no-op
unless the GLib main loop is actively pumped -- confirmed by calling one and
immediately re-querying the window's state, every time, with no change at
all. A second bug needed the same live discovery: unmaximize() followed
immediately by set_geometry() (no pump in between) landed geometry tens of
pixels short on width/height, because the request was computed against the
window's still-transitioning unmaximize state. Both are fixed in layout.py
and were re-verified afterward with a real disturb-then-restore round trip
(two real gnome-terminal windows moved to another workspace and maximized,
then put back via place() with pixel-perfect geometry). The dynamic-workspace
growth mechanism (moving onto the trailing empty workspace grows the count by
one) was verified the same way, as was `sessionctl place` at the CLI level
using a real snapshot file.

What's covered here instead: match_windows() and the workspace-compaction
math, both pure functions operating on plain dicts -- no Wnck, no X11, no
main loop needed to exercise the actual decision logic.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sessionctl_lib import layout  # noqa: E402


def rec(wm_class="code", title="", workspace=0, sticky=False, maximized=False,
        geometry=None):
    return {
        "wm_class": wm_class,
        "title": title,
        "workspace": workspace,
        "sticky": sticky,
        "maximized": maximized,
        "geometry": geometry or {"x": 0, "y": 0, "w": 100, "h": 100},
    }


def live(wm_class="code", title=""):
    return {"wm_class": wm_class, "title": title}


class MatchWindowsTest(unittest.TestCase):
    def test_no_recorded_windows(self):
        matches, unmatched_rec, unmatched_live = layout.match_windows([], [live()])
        self.assertEqual(matches, [])
        self.assertEqual(unmatched_rec, [])
        self.assertEqual(unmatched_live, [live()])

    def test_no_live_windows(self):
        r = rec()
        matches, unmatched_rec, unmatched_live = layout.match_windows([r], [])
        self.assertEqual(matches, [])
        self.assertEqual(unmatched_rec, [r])
        self.assertEqual(unmatched_live, [])

    def test_exact_single_class_match(self):
        r = rec(wm_class="Google-chrome")
        l = live(wm_class="Google-chrome")
        matches, unmatched_rec, unmatched_live = layout.match_windows([r], [l])
        self.assertEqual(matches, [(r, l)])
        self.assertEqual(unmatched_rec, [])
        self.assertEqual(unmatched_live, [])

    def test_wrong_class_is_unmatched(self):
        r = rec(wm_class="Google-chrome")
        l = live(wm_class="code")
        matches, unmatched_rec, unmatched_live = layout.match_windows([r], [l])
        self.assertEqual(matches, [])
        self.assertEqual(unmatched_rec, [r])
        self.assertEqual(unmatched_live, [l])

    def test_same_class_prefers_closer_title(self):
        r = rec(wm_class="code", title="foo.py - myproject - Visual Studio Code")
        exact = live(wm_class="code", title="foo.py - myproject - Visual Studio Code")
        other = live(wm_class="code", title="bar.py - otherproject - Visual Studio Code")
        matches, _, unmatched_live = layout.match_windows([r], [other, exact])
        self.assertEqual(matches, [(r, exact)])
        self.assertEqual(unmatched_live, [other])

    def test_every_window_consumed_at_most_once(self):
        """Two recorded windows of the same class must not both claim the
        same live window -- each live window is removed from the pool the
        moment it is matched.
        """
        r1 = rec(wm_class="Gnome-terminal", title="a")
        r2 = rec(wm_class="Gnome-terminal", title="b")
        l1 = live(wm_class="Gnome-terminal", title="x")
        matches, unmatched_rec, unmatched_live = layout.match_windows([r1, r2], [l1])
        self.assertEqual(len(matches), 1)
        self.assertEqual(len(unmatched_rec), 1)
        self.assertEqual(unmatched_live, [])

    def test_terminal_windows_fall_back_to_positional_pairing(self):
        """No title marker survives for terminals (see the module docstring
        in layout.py and the plan's limitations) -- with identical titles,
        matching still succeeds by class alone, consuming candidates in
        order rather than leaving anything unmatched.
        """
        r1 = rec(wm_class="Gnome-terminal", title="same title")
        r2 = rec(wm_class="Gnome-terminal", title="same title")
        l1 = live(wm_class="Gnome-terminal", title="same title")
        l2 = live(wm_class="Gnome-terminal", title="same title")
        matches, unmatched_rec, unmatched_live = layout.match_windows(
            [r1, r2], [l1, l2]
        )
        self.assertEqual(len(matches), 2)
        self.assertEqual(unmatched_rec, [])
        self.assertEqual(unmatched_live, [])

    def test_new_window_wins_over_identical_title_preexisting_one(self):
        """The reboot-restore bug, pinned exactly.

        Running `sessionctl restore` from a terminal means a same-class
        window is already open when placement runs, and every gnome-terminal
        title is byte-identical once a shell has drawn a prompt. Before the
        fix the similarity tie resolved to whichever came first in the live
        pool -- the older, pre-existing window -- so the record's workspace
        and maximized state landed on the terminal the user was sitting in
        while the freshly launched one was left at gnome-terminal's default
        size. The "new" flag has to outrank the tie.
        """
        r = rec(wm_class="Gnome-terminal", title="shengcn@host: ~", maximized=True)
        preexisting = live(wm_class="Gnome-terminal", title="shengcn@host: ~")
        launched = live(wm_class="Gnome-terminal", title="shengcn@host: ~")
        launched["new"] = True
        # pre-existing deliberately first: that ordering is what used to win.
        matches, _, unmatched_live = layout.match_windows(
            [r], [preexisting, launched]
        )
        self.assertIs(matches[0][1], launched)
        self.assertEqual(unmatched_live, [preexisting])

    def test_new_flag_outranks_a_better_title_match(self):
        """"new" is a stronger signal than any title similarity: the window
        was created specifically to satisfy these records, whereas a title
        can match perfectly by coincidence (or because PS1 overwrote it).
        """
        r = rec(wm_class="Gnome-terminal", title="exactly this")
        stale = live(wm_class="Gnome-terminal", title="exactly this")
        launched = live(wm_class="Gnome-terminal", title="something else")
        launched["new"] = True
        matches, _, _ = layout.match_windows([r], [stale, launched])
        self.assertIs(matches[0][1], launched)

    def test_title_still_decides_among_several_new_windows(self):
        """Preferring new windows must not throw away title matching *within*
        the newly launched set -- two restored VSCode windows still need the
        right folder on the right workspace.
        """
        r = rec(wm_class="code", title="alpha - Visual Studio Code")
        beta = live(wm_class="code", title="beta - Visual Studio Code")
        beta["new"] = True
        alpha = live(wm_class="code", title="alpha - Visual Studio Code")
        alpha["new"] = True
        matches, _, _ = layout.match_windows([r], [beta, alpha])
        self.assertIs(matches[0][1], alpha)

    def test_preexisting_used_when_no_new_window_appeared(self):
        """If a launch produced nothing (Chrome already running, say), the
        record must still bind to the live window rather than go unmatched.
        """
        r = rec(wm_class="Google-chrome", title="x")
        existing = live(wm_class="Google-chrome", title="x")
        matches, unmatched_rec, _ = layout.match_windows([r], [existing])
        self.assertIs(matches[0][1], existing)
        self.assertEqual(unmatched_rec, [])

    def test_absent_new_key_matches_exactly_as_before(self):
        """`sessionctl place` launches nothing and passes no baseline, so no
        live window carries "new" -- matching must be unchanged for it.
        """
        r1 = rec(wm_class="code", title="alpha")
        r2 = rec(wm_class="code", title="beta")
        l_beta = live(wm_class="code", title="beta")
        l_alpha = live(wm_class="code", title="alpha")
        matches, unmatched_rec, unmatched_live = layout.match_windows(
            [r1, r2], [l_beta, l_alpha]
        )
        self.assertIs(matches[0][1], l_alpha)
        self.assertIs(matches[1][1], l_beta)
        self.assertEqual(unmatched_rec, [])
        self.assertEqual(unmatched_live, [])

    def test_terminal_signature_beats_an_identical_title(self):
        """The workspace-restore bug.

        Terminal windows share wm_class, PID, maximized geometry and -- once
        their active tabs are in the same directory -- their title too. Four
        of them on four workspaces were restored onto each other's, because
        matching had nothing to tell them apart. The tabs do: one window
        holds six of them in ~, another three in a project directory.
        """
        r = rec(wm_class="Gnome-terminal", title="user@host: ~", workspace=3)
        r["sig"] = {"tabs": 6, "cwds": ["~"]}
        wrong = live(wm_class="Gnome-terminal", title="user@host: ~")
        wrong["sig"] = {"tabs": 3, "cwds": ["~/Projects"]}
        right = live(wm_class="Gnome-terminal", title="user@host: ~")
        right["sig"] = {"tabs": 6, "cwds": ["~"]}
        matches, _, _ = layout.match_windows([r], [wrong, right])
        self.assertIs(matches[0][1], right)

    def test_new_still_outranks_signature(self):
        """A pre-existing window matching by signature must not steal the
        record from the one this restore just launched -- that regression is
        pinned above, and the signature must not reintroduce it.
        """
        r = rec(wm_class="Gnome-terminal", title="user@host: ~")
        r["sig"] = {"tabs": 2, "cwds": ["~"]}
        preexisting = live(wm_class="Gnome-terminal", title="user@host: ~")
        preexisting["sig"] = {"tabs": 2, "cwds": ["~"]}
        launched = live(wm_class="Gnome-terminal", title="user@host: ~")
        launched["sig"] = {"tabs": 2, "cwds": ["~"]}
        launched["new"] = True
        matches, _, _ = layout.match_windows([r], [preexisting, launched])
        self.assertIs(matches[0][1], launched)

    def test_windows_are_not_placed_on_each_others_workspaces(self):
        """End to end through match_windows: three recorded terminal windows
        on three workspaces, live windows in a different order.
        """
        recs = []
        for tabs, ws in ((6, 0), (3, 1), (5, 2)):
            r = rec(wm_class="Gnome-terminal", title="user@host: ~", workspace=ws)
            r["sig"] = {"tabs": tabs, "cwds": ["~"]}
            recs.append(r)
        lives = []
        for tabs in (5, 6, 3):
            w = live(wm_class="Gnome-terminal", title="user@host: ~")
            w["sig"] = {"tabs": tabs, "cwds": ["~"]}
            lives.append(w)
        matches, unmatched, _ = layout.match_windows(recs, lives)
        self.assertEqual(unmatched, [])
        for r, w in matches:
            self.assertEqual(r["sig"]["tabs"], w["sig"]["tabs"])

    def test_extra_keys_on_live_windows_pass_through_untouched(self):
        """Callers (layout.place()) carry a real Wnck object reference
        alongside wm_class/title -- match_windows must not require or strip
        extra keys.
        """
        r = rec(wm_class="code")
        l = {"wm_class": "code", "title": "", "_wnck": object()}
        matches, _, _ = layout.match_windows([r], [l])
        self.assertIs(matches[0][1]["_wnck"], l["_wnck"])


class SignatureScoreTest(unittest.TestCase):
    """Tab count is scored separately from directories because a freshly
    launched terminal has its tabs before its shells have drawn prompts.
    """

    def test_exact_agreement_scores_highest(self):
        sig = {"tabs": 3, "cwds": ["~"]}
        self.assertEqual(layout._signature_score(sig, dict(sig)), 2)

    def test_tab_count_alone_still_counts(self):
        self.assertEqual(
            layout._signature_score({"tabs": 3, "cwds": ["~"]},
                                    {"tabs": 3, "cwds": [""]}), 1)

    def test_different_tab_counts_score_zero(self):
        self.assertEqual(
            layout._signature_score({"tabs": 3, "cwds": ["~"]},
                                    {"tabs": 4, "cwds": ["~"]}), 0)

    def test_missing_signature_scores_zero(self):
        """Non-terminal windows carry no signature at all."""
        self.assertEqual(layout._signature_score(None, {"tabs": 1, "cwds": []}), 0)
        self.assertEqual(layout._signature_score({"tabs": 1, "cwds": []}, None), 0)


class TerminalSignatureTest(unittest.TestCase):
    def test_signature_summarises_tabs(self):
        slots = [{"cwd": "~"}, {"cwd": "~/p"}, {"cwd": "~"}]
        self.assertEqual(layout._terminal_signature(slots),
                         {"tabs": 3, "cwds": ["~", "~/p"]})

    def test_unreadable_cwds_become_empty_strings(self):
        self.assertEqual(layout._terminal_signature([{"cwd": None}]),
                         {"tabs": 1, "cwds": [""]})


class FakeWindow:
    def __init__(self, maximized=False):
        self.moved_to = None
        self.pinned = False
        self.maximized = maximized
        self.geometry_set = False

    def pin(self):
        self.pinned = True

    def move_to_workspace(self, ws):
        self.moved_to = ws

    def is_maximized(self):
        return self.maximized

    def unmaximize(self):
        self.maximized = False

    def set_geometry(self, *a):
        self.geometry_set = True

    def maximize(self):
        self.maximized = True


class FakeScreen:
    """A desktop with `count` workspaces; higher indices don't exist yet."""

    def __init__(self, count, width=1920, height=1080):
        self.count = count
        self.width = width
        self.height = height

    def get_width(self):
        return self.width

    def get_height(self):
        return self.height

    def get_workspace(self, i):
        return f"ws{i}" if i < self.count else None

    def get_workspace_count(self):
        return self.count

    def get_windows(self):
        return []


class PlaceOneWorkspaceTest(unittest.TestCase):
    """A target workspace that doesn't exist yet must be reported, not
    swallowed.

    With dynamic-workspaces, workspace N exists only once 0..N-1 are
    occupied. After a reboot everything starts on workspace 0, so a snapshot
    with terminals on 2-5 asks for workspaces that cannot exist yet. This
    used to return nothing while the caller counted the record as placed, so
    those windows silently stayed on workspace 0 and `place` reported
    success.
    """

    def setUp(self):
        patcher = mock.patch.object(layout, "_pump")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_workspace_reports_failure(self):
        w = FakeWindow()
        ok = layout._place_one(rec(workspace=5), w, FakeScreen(2), 5)
        self.assertFalse(ok)
        self.assertIsNone(w.moved_to)

    def test_existing_workspace_moves_and_reports_success(self):
        w = FakeWindow()
        ok = layout._place_one(rec(workspace=1), w, FakeScreen(3), 1)
        self.assertTrue(ok)
        self.assertEqual(w.moved_to, "ws1")

    def test_geometry_still_applied_when_workspace_is_missing(self):
        """Geometry and maximized state are idempotent, so applying them on
        an attempt that will be retried costs nothing and helps if the
        workspace never appears.
        """
        w = FakeWindow(maximized=True)
        layout._place_one(rec(workspace=9, maximized=True), w, FakeScreen(1), 9)
        self.assertTrue(w.geometry_set)
        self.assertTrue(w.maximized)

    def test_sticky_window_pins_and_succeeds(self):
        w = FakeWindow()
        ok = layout._place_one(rec(sticky=True), w, FakeScreen(1), None)
        self.assertTrue(ok)
        self.assertTrue(w.pinned)

    def test_no_target_workspace_is_success(self):
        ok = layout._place_one(rec(workspace=None), FakeWindow(), FakeScreen(1), None)
        self.assertTrue(ok)


class PlaceRetriesDeferredTest(unittest.TestCase):
    """The record must come back on a later poll, once the cascade below it
    has grown the desktop far enough.
    """

    def test_record_is_retried_until_its_workspace_exists(self):
        window = FakeWindow()
        screens = [FakeScreen(1), FakeScreen(1), FakeScreen(4)]

        def next_screen():
            return screens.pop(0) if len(screens) > 1 else screens[0]

        live = [{"wm_class": "Gnome-terminal", "title": "t", "_wnck": window}]
        with mock.patch.object(layout, "_screen", side_effect=next_screen), \
             mock.patch.object(layout, "_terminal_signatures", return_value={}), \
             mock.patch.object(layout, "_pump"), \
             mock.patch.object(layout, "match_windows",
                               side_effect=lambda pend, _l, **kw: (
                                   [(r, live[0]) for r in pend], [], [])):
            r = rec(wm_class="Gnome-terminal", workspace=3)
            result = layout.place([r], timeout=5.0, static_workspaces=True)

        self.assertEqual(len(result["placed"]), 1)
        self.assertEqual(result["unmatched"], [])
        self.assertEqual(window.moved_to, "ws3")

    def test_record_that_never_becomes_placeable_is_reported_unplaced(self):
        window = FakeWindow()
        live = [{"wm_class": "Gnome-terminal", "title": "t", "_wnck": window}]
        with mock.patch.object(layout, "_screen", return_value=FakeScreen(1)), \
             mock.patch.object(layout, "_terminal_signatures", return_value={}), \
             mock.patch.object(layout, "_pump"), \
             mock.patch.object(layout, "match_windows",
                               side_effect=lambda pend, _l, **kw: (
                                   [(r, live[0]) for r in pend], [], [])):
            r = rec(wm_class="Gnome-terminal", workspace=7)
            result = layout.place([r], timeout=1.0, static_workspaces=True)

        self.assertEqual(result["placed"], [])
        self.assertEqual(len(result["unmatched"]), 1)


class CompactWorkspaceIndicesTest(unittest.TestCase):
    def test_no_gaps_maps_identically(self):
        recorded = [rec(workspace=0), rec(workspace=1), rec(workspace=2)]
        remap = layout._compact_workspace_indices(recorded)
        self.assertEqual(remap, {0: 0, 1: 1, 2: 2})

    def test_gap_is_compacted(self):
        """Windows on 0 and 3, nothing on 1-2: workspace 3 is not reachable
        by ascending placement until the gap is closed.
        """
        recorded = [rec(workspace=0), rec(workspace=3)]
        remap = layout._compact_workspace_indices(recorded)
        self.assertEqual(remap, {0: 0, 3: 1})

    def test_relative_order_preserved(self):
        recorded = [rec(workspace=5), rec(workspace=2), rec(workspace=9)]
        remap = layout._compact_workspace_indices(recorded)
        self.assertEqual(remap[2], 0)
        self.assertEqual(remap[5], 1)
        self.assertEqual(remap[9], 2)

    def test_sticky_windows_do_not_consume_a_workspace_slot(self):
        recorded = [rec(workspace=0), rec(sticky=True, workspace=None), rec(workspace=1)]
        remap = layout._compact_workspace_indices(recorded)
        self.assertEqual(remap, {0: 0, 1: 1})

    def test_duplicate_workspaces_collapse_to_one_slot(self):
        recorded = [rec(workspace=5), rec(workspace=5), rec(workspace=8)]
        remap = layout._compact_workspace_indices(recorded)
        self.assertEqual(remap, {5: 0, 8: 1})

    def test_empty_input(self):
        self.assertEqual(layout._compact_workspace_indices([]), {})


class TargetWorkspaceTest(unittest.TestCase):
    def test_sticky_has_no_target_workspace(self):
        r = rec(sticky=True, workspace=None)
        self.assertIsNone(layout._target_workspace(r, {}))

    def test_missing_workspace_has_no_target(self):
        r = rec(workspace=None)
        self.assertIsNone(layout._target_workspace(r, {}))

    def test_remapped_workspace_uses_the_remap(self):
        r = rec(workspace=3)
        self.assertEqual(layout._target_workspace(r, {3: 1}), 1)

    def test_unmapped_workspace_falls_back_to_raw_value(self):
        """static_workspaces=True passes an empty remap -- raw indices must
        still work rather than resolve to nothing.
        """
        r = rec(workspace=2)
        self.assertEqual(layout._target_workspace(r, {}), 2)


class PlaceEmptyInputTest(unittest.TestCase):
    def test_no_recorded_windows_returns_immediately(self):
        """Must not touch Wnck/Gtk at all when there is nothing to place --
        this is what makes it safe to call in a test environment with no
        real X11 windows worth placing.
        """
        result = layout.place([])
        self.assertEqual(result, {"placed": [], "unmatched": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)


HOME = os.path.expanduser("~")


class HookWindowCwdsTest(unittest.TestCase):
    """The signature's directories come from the shell hook, not the prompt.

    Reproduces the capture half of a real mis-restore: a five-tab window
    whose one interesting tab was busy, so AT-SPI read no prompt for it.
    """

    def test_cwds_are_abbreviated_per_window(self):
        terminals = {"windows": [
            {"tabs": [{"cwd": HOME}, {"cwd": HOME + "/Projects/agents"}]},
            {"tabs": [{"cwd": HOME}]},
        ]}
        self.assertEqual(
            layout._hook_window_cwds(terminals),
            [["~", "~/Projects/agents"], ["~"]],
        )

    def test_missing_cwd_becomes_empty_string(self):
        terminals = {"windows": [{"tabs": [{"cwd": None}, {"cwd": HOME}]}]}
        self.assertEqual(layout._hook_window_cwds(terminals), [["", "~"]])

    def test_no_terminals_section_yields_none(self):
        self.assertIsNone(layout._hook_window_cwds(None))
        self.assertIsNone(layout._hook_window_cwds({}))
        self.assertIsNone(layout._hook_window_cwds({"windows": []}))

    def test_signature_prefers_supplied_cwds_over_prompts(self):
        slots = [{"cwd": ""}, {"cwd": "~"}]
        self.assertEqual(
            layout._terminal_signature(slots, ["~/a", "~"]),
            {"tabs": 2, "cwds": ["~", "~/a"]},
        )

    def test_signature_falls_back_to_prompts(self):
        slots = [{"cwd": ""}, {"cwd": "~"}]
        self.assertEqual(layout._terminal_signature(slots, None),
                         {"tabs": 2, "cwds": ["", "~"]})


class BusyTabRegressionTest(unittest.TestCase):
    """Two five-tab terminals must not be placed on each other's workspaces.

    The live failure, from snapshot 2026-09-03T21-43-37 restored the next
    morning: one five-tab window sat in .../TensorRT-LLM/examples with that
    tab busy, so its prompt was unreadable and it was recorded as
    {tabs: 5, cwds: ["", "~"]}; the other five-tab window, all tabs in ~, was
    recorded as {tabs: 5, cwds: ["~"]}. Neither record could match its own
    restored window exactly, both scored 1 against both windows, and the tie
    fell to list order -- the two came back on workspaces 4 and 1 instead of
    1 and 4.
    """

    EXAMPLES = "~/Projects/agents/trtllm-work/TensorRT-LLM/examples"

    def _live(self):
        # What the restored windows look like once their shells are up.
        return [
            {"wm_class": "Gnome-terminal", "title": "shengcn@host: ~",
             "new": True, "sig": {"tabs": 5, "cwds": ["~"]}},
            {"wm_class": "Gnome-terminal", "title": "shengcn@host: ~",
             "new": True, "sig": {"tabs": 5, "cwds": ["~", self.EXAMPLES]}},
        ]

    def test_prompt_sourced_signature_is_ambiguous(self):
        """The old capture path: the tie that caused the swap."""
        stale = rec(wm_class="Gnome-terminal", workspace=1,
                    title="shengcn@host: " + self.EXAMPLES)
        stale["sig"] = {"tabs": 5, "cwds": ["", "~"]}
        live = self._live()
        scores = [layout._signature_score(stale["sig"], w["sig"]) for w in live]
        self.assertEqual(scores, [1, 1])  # indistinguishable

    def test_hook_sourced_signature_matches_the_right_window(self):
        """The fixed capture path records the busy tab's directory anyway."""
        examples_rec = rec(wm_class="Gnome-terminal", workspace=1,
                           title="shengcn@host: " + self.EXAMPLES)
        examples_rec["sig"] = {"tabs": 5, "cwds": ["~", self.EXAMPLES]}
        home_rec = rec(wm_class="Gnome-terminal", workspace=4,
                       title="shengcn@host: ~")
        home_rec["sig"] = {"tabs": 5, "cwds": ["~"]}

        live = self._live()
        matches, unmatched, _ = layout.match_windows(
            [examples_rec, home_rec], live, strict=True)

        self.assertEqual(unmatched, [])
        placement = {id(r): w["sig"]["cwds"] for r, w in matches}
        self.assertEqual(placement[id(examples_rec)], ["~", self.EXAMPLES])
        self.assertEqual(placement[id(home_rec)], ["~"])


class StrictMatchingTest(unittest.TestCase):
    """Ambiguity is deferred, not decided by list order."""

    def test_tie_is_left_pending_under_strict(self):
        r = rec(wm_class="Gnome-terminal", title="t")
        r["sig"] = {"tabs": 5, "cwds": ["~"]}
        live = [
            {"wm_class": "Gnome-terminal", "title": "t", "new": True,
             "sig": {"tabs": 5, "cwds": [""]}},
            {"wm_class": "Gnome-terminal", "title": "t", "new": True,
             "sig": {"tabs": 5, "cwds": [""]}},
        ]
        matches, unmatched, pool = layout.match_windows([r], live, strict=True)
        self.assertEqual(matches, [])
        self.assertEqual(unmatched, [r])
        self.assertEqual(len(pool), 2)  # neither window was claimed

    def test_same_tie_is_resolved_without_strict(self):
        """Once the settle window is over, a decision beats leaving it unplaced."""
        r = rec(wm_class="Gnome-terminal", title="t")
        r["sig"] = {"tabs": 5, "cwds": ["~"]}
        live = [
            {"wm_class": "Gnome-terminal", "title": "t", "new": True,
             "sig": {"tabs": 5, "cwds": [""]}},
            {"wm_class": "Gnome-terminal", "title": "t", "new": True,
             "sig": {"tabs": 5, "cwds": [""]}},
        ]
        matches, unmatched, _ = layout.match_windows([r], live, strict=False)
        self.assertEqual(len(matches), 1)
        self.assertEqual(unmatched, [])

    def test_unambiguous_record_still_matches_under_strict(self):
        r = rec(wm_class="Gnome-terminal", title="t")
        r["sig"] = {"tabs": 5, "cwds": ["~"]}
        live = [
            {"wm_class": "Gnome-terminal", "title": "t", "new": True,
             "sig": {"tabs": 5, "cwds": ["~"]}},
            {"wm_class": "Gnome-terminal", "title": "t", "new": True,
             "sig": {"tabs": 2, "cwds": ["~"]}},
        ]
        matches, unmatched, _ = layout.match_windows([r], live, strict=True)
        self.assertEqual(len(matches), 1)
        self.assertEqual(unmatched, [])


class VscodeFolderTest(unittest.TestCase):
    """VSCode windows are identified by the folder in their title.

    Every VSCode window shares one Electron PID, so PID cannot separate
    them, and the title was the only discriminator. But the title leads with
    the *open file*, which changes constantly, so whole-title similarity
    pairs windows by whichever filenames happen to look alike.
    """

    def test_file_and_folder(self):
        self.assertEqual(
            layout._vscode_folder("cli.py - stock_03_claude - Visual Studio Code"),
            "stock_03_claude")

    def test_folder_only(self):
        """No file open yet -- still identifiable."""
        self.assertEqual(
            layout._vscode_folder("stock_03_claude - Visual Studio Code"),
            "stock_03_claude")

    def test_still_loading_has_no_folder(self):
        """The signal to wait rather than guess."""
        self.assertIsNone(layout._vscode_folder("Visual Studio Code"))

    def test_unsaved_marker_does_not_disturb_the_folder(self):
        self.assertEqual(
            layout._vscode_folder("● cli.py - stock_03_claude - Visual Studio Code"),
            "stock_03_claude")

    def test_path_with_dashes_in_the_filename(self):
        self.assertEqual(
            layout._vscode_folder(
                "2025-10-27_16-51-45.log - stock_02_gen_data - Visual Studio Code"),
            "stock_02_gen_data")

    def test_non_vscode_title_is_not_parsed(self):
        self.assertIsNone(layout._vscode_folder("shengcn@host: ~"))
        self.assertIsNone(layout._vscode_folder(""))
        self.assertIsNone(layout._vscode_folder(None))


class VscodeMisplacementRegressionTest(unittest.TestCase):
    """The real mis-pairing, with the titles that produced it.

    Recorded LLM window scored 0.696 against the *resume* window and only
    0.639 against its own, because by restore time it was showing a
    different file. It claimed resume's window, resume took what was left,
    and both landed on the wrong monitor.
    """

    RECORDED = [
        "Preview PoC.zh.md - LLM - Visual Studio Code",
        "2025-10-27_16-51-45.log - stock_02_gen_data - Visual Studio Code",
        "interview_quant.md - resume - Visual Studio Code",
    ]
    LIVE = [
        "llama32-1b-trtllm-a2000.md - LLM - Visual Studio Code",
        "2025-10-27_16-51-45.log - stock_02_gen_data - Visual Studio Code",
        "interview_quant.md - resume - Visual Studio Code",
        "cli.py - stock_03_claude - Visual Studio Code",
    ]

    def test_title_similarity_alone_would_mis_pair(self):
        """Pins *why* the fix is needed, not just that it works."""
        own = layout._title_similarity(self.RECORDED[0], self.LIVE[0])
        other = layout._title_similarity(self.RECORDED[0], self.LIVE[2])
        self.assertGreater(other, own)

    def test_every_record_pairs_with_its_own_folder(self):
        recs = [rec(wm_class="code", title=t) for t in self.RECORDED]
        lives = [live(wm_class="code", title=t) for t in self.LIVE]
        matches, unmatched, _ = layout.match_windows(recs, lives)
        self.assertEqual(unmatched, [])
        for r, w in matches:
            self.assertEqual(layout._vscode_folder(r["title"]),
                             layout._vscode_folder(w["title"]))

    def test_folder_outranks_a_closer_title(self):
        r = rec(wm_class="code", title=self.RECORDED[0])
        closer_title = live(wm_class="code", title=self.LIVE[2])
        own_folder = live(wm_class="code", title=self.LIVE[0])
        matches, _, _ = layout.match_windows([r], [closer_title, own_folder])
        self.assertIs(matches[0][1], own_folder)


class VscodeStrictWaitTest(unittest.TestCase):
    def test_loading_window_is_deferred_not_guessed(self):
        r = rec(wm_class="code", title="cli.py - myproj - Visual Studio Code")
        loading = live(wm_class="code", title="Visual Studio Code")
        matches, unmatched, _ = layout.match_windows([r], [loading], strict=True)
        self.assertEqual(matches, [])
        self.assertEqual(unmatched, [r])

    def test_after_the_settle_window_it_is_placed_rather_than_dropped(self):
        r = rec(wm_class="code", title="cli.py - myproj - Visual Studio Code")
        loading = live(wm_class="code", title="Visual Studio Code")
        matches, unmatched, _ = layout.match_windows([r], [loading], strict=False)
        self.assertEqual(len(matches), 1)
        self.assertEqual(unmatched, [])

    def test_a_record_with_no_identity_is_not_held_back(self):
        """Nautilus and friends have nothing to wait for, so strict mode must
        not strand them.
        """
        r = rec(wm_class="Org.gnome.Nautilus", title="Downloads")
        w = live(wm_class="Org.gnome.Nautilus", title="Documents")
        matches, unmatched, _ = layout.match_windows([r], [w], strict=True)
        self.assertEqual(len(matches), 1)
        self.assertEqual(unmatched, [])

    def test_terminal_signature_still_takes_precedence(self):
        """A terminal carries an explicit signature; the VSCode path must not
        interfere with it.
        """
        r = rec(wm_class="Gnome-terminal", title="user@host: ~")
        r["sig"] = {"tabs": 3, "cwds": ["~"]}
        wrong = live(wm_class="Gnome-terminal", title="user@host: ~")
        wrong["sig"] = {"tabs": 5, "cwds": ["~"]}
        right = live(wm_class="Gnome-terminal", title="user@host: ~")
        right["sig"] = {"tabs": 3, "cwds": ["~"]}
        matches, _, _ = layout.match_windows([r], [wrong, right])
        self.assertIs(matches[0][1], right)


class SecondaryMonitorPlacementTest(unittest.TestCase):
    """A window mutter gave no workspace is not necessarily a pinned window.

    With workspaces-only-on-primary=true (mutter's default), every window on
    a non-primary monitor reports workspace None. Confirmed live on this
    machine: Chrome and two VSCode windows on the second monitor all report
    workspace None / is_pinned() True / is_sticky() False, while the actual
    sticky window -- the shell overlay -- reports is_sticky() True.
    """

    def test_no_workspace_and_not_sticky_is_not_pinned(self):
        w = FakeWindow()
        r = rec(wm_class="Google-chrome", workspace=None, sticky=False,
                geometry={"x": 1920, "y": 0, "w": 2560, "h": 1440})
        ok = layout._place_one(r, w, FakeScreen(4), None)
        self.assertTrue(ok)
        self.assertFalse(w.pinned)
        self.assertIsNone(w.moved_to)

    def test_genuinely_sticky_is_still_pinned(self):
        w = FakeWindow()
        r = rec(wm_class="Gjs", workspace=None, sticky=True)
        layout._place_one(r, w, FakeScreen(4), None)
        self.assertTrue(w.pinned)


class OnscreenTest(unittest.TestCase):
    """Coordinates from a monitor that is no longer attached must come back."""

    def test_second_monitor_coordinates_are_pulled_back(self):
        screen = FakeScreen(4, width=1920, height=1080)
        x, y = layout._onscreen({"x": 1920, "y": 0, "w": 2560, "h": 1440}, screen)
        self.assertLess(x, 1920)
        self.assertEqual(y, 0)

    def test_coordinates_that_still_fit_are_untouched(self):
        screen = FakeScreen(4, width=4480, height=1440)
        x, y = layout._onscreen({"x": 1920, "y": 0, "w": 2560, "h": 1440}, screen)
        self.assertEqual((x, y), (1920, 0))

    def test_ordinary_geometry_is_untouched(self):
        screen = FakeScreen(4)
        x, y = layout._onscreen({"x": 70, "y": 27, "w": 1850, "h": 1053}, screen)
        self.assertEqual((x, y), (70, 27))
