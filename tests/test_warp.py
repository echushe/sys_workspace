"""Tests for Warp capture, restore planning and window matching.

Live testing against the real Warp (four windows, six tabs, nine panes, three
of the tabs split) already confirmed the parts that only a real install can
confirm, and is what turned up the facts these tests are built on:

* ``WARP_TERMINAL_SESSION_UUID`` in a pane's shell is the hex of
  ``terminal_panes.uuid`` -- all nine live shells matched all nine rows.
* ``terminal_panes.cwd`` is the *current* directory, not the initial one: one
  pane's shell had ``WARP_INITIAL_WORKING_DIR=/home/shengcn`` while the row
  recorded the project directory it had since moved to.
* The database's window ids do **not** line up with X11 XID order. Sorted by
  XID the live windows read stock_03_claude, ZERO, ~, ZERO; by database id they
  read stock_03_claude, ZERO, ZERO, ~. Pairing them positionally would have
  handed two windows each other's signatures, silently. WarpSignaturesTest
  pins that case specifically.
* Warp elides the front of a long path in the title bar
  ("..s/finance/stock_03_claude"), cutting mid-segment rather than at a "/".

The database tests build a real sqlite file using Warp's own DDL for the
columns being read, rather than mocking the query results -- the queries and
the multi-table join are the part worth testing, and a mock of
``conn.execute`` would only assert that the test agrees with itself.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sessionctl_lib import collect, layout, restore, snapshot, warp  # noqa: E402


# Column definitions copied from the live database's own schema, trimmed to the
# columns warp._read_db actually selects. Keeping the real names and the real
# foreign-key shape is the point: a rename in Warp's schema should break these
# tests, because it would break the collector.
SCHEMA = """
CREATE TABLE windows (
  id INTEGER PRIMARY KEY NOT NULL,
  active_tab_index INTEGER NOT NULL,
  window_width FLOAT, window_height FLOAT,
  origin_x FLOAT, origin_y FLOAT,
  fullscreen_state INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE tabs (
  id INTEGER PRIMARY KEY NOT NULL,
  window_id INTEGER NOT NULL,
  custom_title TEXT
);
CREATE TABLE pane_nodes (
  id INTEGER PRIMARY KEY NOT NULL,
  tab_id INTEGER NOT NULL,
  parent_pane_node_id INTEGER,
  flex FLOAT,
  is_leaf BOOLEAN NOT NULL
);
CREATE TABLE pane_leaves (
  pane_node_id INTEGER NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  is_focused BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE pane_branches (
  id INTEGER PRIMARY KEY NOT NULL,
  pane_node_id INTEGER NOT NULL UNIQUE,
  horizontal BOOLEAN NOT NULL
);
CREATE TABLE terminal_panes (
  id INTEGER PRIMARY KEY NOT NULL,
  kind TEXT NOT NULL DEFAULT 'terminal',
  uuid BLOB NOT NULL UNIQUE,
  cwd TEXT,
  is_active BOOLEAN NOT NULL DEFAULT FALSE,
  shell_launch_data TEXT
);
"""


def build_db(path):
    """A two-window Warp session, shaped like the real one.

    window 1: one tab split into two panes (the real desktop's split shape)
    window 2: two unsplit tabs, active_tab_index=1
    """
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO windows VALUES (1, 0, 2490.0, 1413.0, 70.0, 27.0, 2)"
    )
    conn.execute(
        "INSERT INTO windows VALUES (2, 1, NULL, NULL, NULL, NULL, 0)"
    )
    conn.execute("INSERT INTO tabs VALUES (1, 1, NULL)")
    conn.execute("INSERT INTO tabs VALUES (2, 2, NULL)")
    conn.execute("INSERT INTO tabs VALUES (3, 2, 'notes')")

    # tab 1: branch root (node 1) with two leaves (2, 3)
    conn.execute("INSERT INTO pane_nodes VALUES (1, 1, NULL, NULL, 0)")
    conn.execute("INSERT INTO pane_nodes VALUES (2, 1, 1, 0.5, 1)")
    conn.execute("INSERT INTO pane_nodes VALUES (3, 1, 1, 0.5, 1)")
    conn.execute("INSERT INTO pane_branches VALUES (1, 1, 1)")
    # tabs 2 and 3: single leaves
    conn.execute("INSERT INTO pane_nodes VALUES (4, 2, NULL, NULL, 1)")
    conn.execute("INSERT INTO pane_nodes VALUES (5, 3, NULL, NULL, 1)")

    for node_id, focused in ((2, 1), (3, 0), (4, 1), (5, 1)):
        conn.execute(
            "INSERT INTO pane_leaves VALUES (?, 'terminal', ?)",
            (node_id, focused),
        )

    panes = [
        (2, "aa" * 16, "/home/u/Projects/app"),
        (3, "bb" * 16, "/home/u/Projects/app/src"),
        (4, "cc" * 16, "/home/u"),
        (5, "dd" * 16, "/tmp"),
    ]
    for node_id, uuid_hex, cwd in panes:
        conn.execute(
            "INSERT INTO terminal_panes VALUES (?, 'terminal', ?, ?, 1, ?)",
            (node_id, bytes.fromhex(uuid_hex), cwd,
             json.dumps({"Executable": {"executable_path": "/bin/bash"}})),
        )
    conn.commit()
    conn.close()


class DbTempCase(unittest.TestCase):
    """Points warp at a throwaway database for the duration of a test."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "warp.sqlite")
        build_db(self.db)
        self.patches = [
            mock.patch.object(warp, "DB_PATH", self.db),
            mock.patch.object(warp, "running", lambda: True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)


class ReadDbTest(DbTempCase):
    def test_window_tab_pane_tree(self):
        windows = warp._read_db({})
        self.assertEqual([w["id"] for w in windows], [1, 2])

        w1, w2 = windows
        self.assertEqual(len(w1["tabs"]), 1)
        self.assertEqual(len(w1["tabs"][0]["panes"]), 2)
        self.assertEqual(len(w2["tabs"]), 2)
        self.assertEqual([len(t["panes"]) for t in w2["tabs"]], [1, 1])

    def test_pane_fields(self):
        w1 = warp._read_db({})[0]
        panes = w1["tabs"][0]["panes"]
        self.assertEqual([p["cwd"] for p in panes],
                         ["/home/u/Projects/app", "/home/u/Projects/app/src"])
        self.assertEqual([p["focused"] for p in panes], [True, False])
        self.assertEqual(panes[0]["uuid"], "aa" * 16)

    def test_split_orientation_and_custom_title(self):
        windows = warp._read_db({})
        self.assertIs(windows[0]["tabs"][0]["split_horizontal"], True)
        # An unsplit tab has no branch row at all, so no orientation.
        self.assertIsNone(windows[1]["tabs"][0]["split_horizontal"])
        self.assertEqual(windows[1]["tabs"][1]["custom_title"], "notes")

    def test_geometry_present_and_absent(self):
        windows = warp._read_db({})
        self.assertEqual(windows[0]["geometry"],
                         {"x": 70, "y": 27, "w": 2490, "h": 1413})
        # Warp leaves geometry null for a window it has not sized yet; that
        # must read as "unknown", not as a window at the origin.
        self.assertIsNone(windows[1]["geometry"])

    def test_conda_joined_by_uuid(self):
        windows = warp._read_db({"aa" * 16: "torch212"})
        panes = windows[0]["tabs"][0]["panes"]
        self.assertEqual(panes[0]["conda"], "torch212")
        self.assertIsNone(panes[1]["conda"])

    def test_read_only_connection_cannot_write(self):
        """The collector must never be able to alter Warp's live database."""
        conn = warp._connect()
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM windows")
        conn.close()


class CollectWarpTest(DbTempCase):
    def test_collects_full_state(self):
        state = warp.collect_warp()
        self.assertTrue(state["installed"])
        self.assertTrue(state["running"])
        self.assertEqual(len(state["windows"]), 2)
        self.assertEqual(warp.pane_count(state), 4)
        self.assertNotIn("error", state)

    def test_sources_include_the_wal(self):
        """Statting only the main file would miss every uncheckpointed change."""
        paths = [s["path"] for s in warp.collect_warp()["sources"]]
        self.assertEqual(paths, [self.db, self.db + "-wal"])

    def test_conda_from_shell_records(self):
        state = warp.collect_warp(shells=[
            {"warp_uuid": "AA" * 16, "conda": "torch212"},
            {"warp_uuid": "cc" * 16, "conda": ""},
        ])
        panes = state["windows"][0]["tabs"][0]["panes"]
        # Case-insensitive: the hook writes whatever Warp exported.
        self.assertEqual(panes[0]["conda"], "torch212")
        # An empty conda name is "no env", not an env called "".
        other = state["windows"][1]["tabs"][0]["panes"][0]
        self.assertIsNone(other["conda"])

    def test_unchanged_sources_skip_the_scan(self):
        first = warp.collect_warp()
        with mock.patch.object(warp, "_read_db",
                               side_effect=AssertionError("rescanned")):
            second = warp.collect_warp(previous=first)
        self.assertEqual(len(second["windows"]), 2)

    def test_skip_still_refreshes_conda(self):
        """The cwd comes from the database, but conda comes from the hook.

        A pane that only ran `conda activate` leaves both files' mtime and
        size untouched, so returning the cached record verbatim would hold a
        stale env forever.
        """
        first = warp.collect_warp()
        self.assertIsNone(first["windows"][0]["tabs"][0]["panes"][0]["conda"])
        with mock.patch.object(warp, "_read_db",
                               side_effect=AssertionError("rescanned")):
            second = warp.collect_warp(
                previous=first,
                shells=[{"warp_uuid": "aa" * 16, "conda": "torch212"}],
            )
        self.assertEqual(
            second["windows"][0]["tabs"][0]["panes"][0]["conda"], "torch212"
        )

    def test_changed_sources_rescan(self):
        first = warp.collect_warp()
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO windows VALUES (3, 0, NULL, NULL, NULL, NULL, 0)")
        conn.commit()
        conn.close()
        second = warp.collect_warp(previous=first)
        self.assertEqual(len(second["windows"]), 3)

    def test_missing_database_is_not_installed(self):
        with mock.patch.object(warp, "DB_PATH", "/nonexistent/warp.sqlite"):
            state = warp.collect_warp()
        self.assertFalse(state["installed"])
        self.assertEqual(state["windows"], [])

    def test_unreadable_database_degrades_without_raising(self):
        """One bad collector must not take down a whole save."""
        with mock.patch.object(warp, "_read_db",
                               side_effect=sqlite3.OperationalError("locked")):
            state = warp.collect_warp()
        self.assertEqual(state["windows"], [])
        self.assertIn("locked", state["error"])
        self.assertTrue(state["installed"])


class ActiveCwdTest(unittest.TestCase):
    def window(self, active_index, tabs):
        return {"active_tab_index": active_index, "tabs": tabs}

    def tab(self, panes, custom_title=None):
        return {"custom_title": custom_title, "panes": panes}

    def pane(self, cwd, focused=False):
        return {"cwd": cwd, "focused": focused}

    def test_focused_pane_of_active_tab(self):
        w = self.window(1, [
            self.tab([self.pane("/tmp", True)]),
            self.tab([self.pane("/home/u/a"), self.pane("/home/u/b", True)]),
        ])
        with mock.patch.object(os.path, "expanduser", return_value="/home/u"):
            self.assertEqual(warp.active_cwd(w), "~/b")

    def test_falls_back_when_active_index_is_out_of_range(self):
        """A window mid-restore can name a tab that is not in the list yet."""
        w = self.window(7, [self.tab([self.pane("/tmp", True)])])
        self.assertEqual(warp.active_cwd(w), "/tmp")

    def test_no_tabs_or_panes(self):
        self.assertIsNone(warp.active_cwd(self.window(0, [])))
        self.assertIsNone(warp.active_cwd(self.window(0, [self.tab([])])))


class SignatureTest(unittest.TestCase):
    def test_shape_matches_the_terminal_signature(self):
        """Same dict shape as gnome-terminal's, so match_windows needs no
        special case for Warp."""
        window = {"tabs": [
            {"panes": [{"cwd": "/home/u"}, {"cwd": "/home/u"}]},
            {"panes": [{"cwd": "/tmp"}]},
        ]}
        with mock.patch.object(os.path, "expanduser", return_value="/home/u"):
            sig = warp.signature(window)
        self.assertEqual(sig, {"tabs": 2, "cwds": ["/tmp", "~"]})
        self.assertEqual(set(sig), set(layout._terminal_signature([{"cwd": "x"}])))

    def test_counts_tabs_not_panes(self):
        window = {"tabs": [{"panes": [{"cwd": "/a"}, {"cwd": "/b"}]}]}
        self.assertEqual(warp.signature(window)["tabs"], 1)


class ParseRestoreSessionTest(unittest.TestCase):
    def test_absent_key_defaults_to_enabled(self):
        """Warp's schema documents restore_session as default true, so a
        settings file that says nothing about it has it on."""
        self.assertTrue(warp._parse_restore_session("[general]\nx = 1\n"))
        self.assertTrue(warp._parse_restore_session(""))

    def test_explicitly_disabled(self):
        self.assertFalse(warp._parse_restore_session(
            "[general]\nrestore_session = false\n"
        ))

    def test_explicitly_enabled(self):
        self.assertTrue(warp._parse_restore_session(
            "[general]\nrestore_session = true\n"
        ))

    def test_only_the_general_table_counts(self):
        text = "[session]\nrestore_session = false\n[general]\nfoo = 1\n"
        self.assertTrue(warp._parse_restore_session(text))

    def test_comments_are_ignored(self):
        text = "[general]\n# restore_session = false\nrestore_session = false\n"
        self.assertFalse(warp._parse_restore_session(text))

    def test_real_settings_file_shape(self):
        """The live file has default_session_mode and no restore_session."""
        text = (
            '[general]\ndefault_session_mode = "agent"\n\n'
            "[agents]\n\n[appearance.window]\nzoom_level = 90\n"
        )
        self.assertTrue(warp._parse_restore_session(text))


class LoginFileTest(unittest.TestCase):
    """Warp sources login files, NOT ~/.bashrc -- the opposite of
    gnome-terminal here, whose profile is login-shell=false."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.patch = mock.patch.dict(os.environ, {"HOME": self.home})
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.home, name), "w") as f:
            f.write(text)

    def test_first_login_file_wins(self):
        self.write(".profile", "x")
        self.assertTrue(warp.login_file().endswith(".profile"))
        self.write(".bash_profile", "y")
        self.assertTrue(warp.login_file().endswith(".bash_profile"))

    def test_profile_sourcing_bashrc_reaches_panes(self):
        self.write(".profile", 'if [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi')
        ok, message = warp.hook_reaches_panes()
        self.assertTrue(ok)
        self.assertIn(".profile", message)

    def test_shadowing_bash_profile_breaks_the_chain(self):
        """The real hazard: creating ~/.bash_profile for unrelated reasons
        stops Warp reaching ~/.bashrc, silently, while gnome-terminal keeps
        working."""
        self.write(".profile", '. "$HOME/.bashrc"')
        self.write(".bash_profile", "export EDITOR=vim")
        ok, message = warp.hook_reaches_panes()
        self.assertFalse(ok)
        self.assertIn(".bash_profile", message)

    def test_no_login_file_at_all(self):
        ok, message = warp.hook_reaches_panes()
        self.assertFalse(ok)
        self.assertIn("login file", message)


class WarpCommandsTest(unittest.TestCase):
    def state(self, windows=1, running=False):
        return {"running": running, "windows": [{"id": 1}] * windows}

    def test_launches_when_not_running(self):
        self.assertEqual(
            restore.warp_commands(self.state(), running=False),
            [["warp-terminal"]],
        )

    def test_silent_when_already_running(self):
        self.assertEqual(restore.warp_commands(self.state(), running=True), [])

    def test_force_overrides_running(self):
        self.assertEqual(
            restore.warp_commands(self.state(), running=True, force=True),
            [["warp-terminal"]],
        )

    def test_never_launches_for_a_desktop_that_had_no_warp(self):
        self.assertEqual(restore.warp_commands({}, running=False), [])
        self.assertEqual(
            restore.warp_commands({"windows": []}, running=False), []
        )

    def test_ignores_the_snapshots_own_running_flag(self):
        """The snapshot's `running` is from save time, which after a reboot is
        almost always True -- trusting it would skip the one launch that
        matters."""
        recorded_running = self.state(running=True)
        self.assertEqual(
            restore.warp_commands(recorded_running, running=False),
            [["warp-terminal"]],
        )


class FakeWnckWindow:
    def __init__(self, xid, wm_class, title):
        self.xid = xid
        self.wm_class = wm_class
        self.title = title

    def get_xid(self):
        return self.xid

    def get_class_group_name(self):
        return self.wm_class

    def get_name(self):
        return self.title


class WarpSignaturesTest(unittest.TestCase):
    """Pairing database windows to X11 windows, by content and never by order."""

    def warp_state(self, *specs):
        """specs: (active_cwd, tab_cwds...) -> a windows list."""
        windows = []
        for i, (active, tab_cwds) in enumerate(specs, start=1):
            tabs = [
                {"custom_title": None,
                 "panes": [{"cwd": c, "focused": c == active}]}
                for c in tab_cwds
            ]
            # Make sure the focused pane of the active tab is `active`.
            index = next(
                (n for n, t in enumerate(tabs)
                 if t["panes"][0]["cwd"] == active), 0
            )
            windows.append({"id": i, "active_tab_index": index, "tabs": tabs})
        return {"windows": windows}

    def test_pairs_by_content_not_by_id_order(self):
        """The regression that motivated this: database id order and XID order
        genuinely disagree on the live desktop.

        Database order here is A, ZERO, ZERO, HOME; XID order is
        A, ZERO, HOME, ZERO. Pairing index-for-index would give the HOME
        window a ZERO signature and vice versa.
        """
        state = self.warp_state(
            ("/a", ["/a"]),
            ("/zero", ["/zero"]),
            ("/zero", ["/zero"]),
            ("/home", ["/home", "/home", "/home"]),
        )
        live = [
            FakeWnckWindow(1, warp.WARP_CLASS, "/a"),
            FakeWnckWindow(2, warp.WARP_CLASS, "/zero"),
            FakeWnckWindow(3, warp.WARP_CLASS, "/home"),
            FakeWnckWindow(4, warp.WARP_CLASS, "/zero"),
        ]
        sigs = layout._warp_signatures(live, state)
        # The three-tab HOME window must get the three-tab signature.
        self.assertEqual(sigs[3]["tabs"], 3)
        self.assertEqual(sigs[1]["cwds"], ["/a"])
        self.assertEqual(sigs[2]["tabs"], 1)
        self.assertEqual(sigs[4]["tabs"], 1)

    def test_elided_title_matches_by_suffix(self):
        """Warp cuts the front off a long path, mid-segment."""
        state = self.warp_state(("/home/u/Projects/finance/stock_03", ["/home/u/Projects/finance/stock_03"]))
        live = [FakeWnckWindow(1, warp.WARP_CLASS, "..s/finance/stock_03")]
        sigs = layout._warp_signatures(live, state)
        self.assertEqual(sigs[1]["cwds"], ["/home/u/Projects/finance/stock_03"])

    def test_custom_tab_title_is_matched(self):
        state = {"windows": [{
            "id": 1, "active_tab_index": 0,
            "tabs": [{"custom_title": "deploy",
                      "panes": [{"cwd": "/srv", "focused": True}]}],
        }]}
        live = [FakeWnckWindow(1, warp.WARP_CLASS, "deploy")]
        self.assertEqual(layout._warp_signatures(live, state)[1]["cwds"], ["/srv"])

    def test_count_disagreement_abandons_the_whole_map(self):
        """Degrade to class+title rather than pair things up wrongly -- the
        same policy _terminal_signatures uses."""
        state = self.warp_state(("/a", ["/a"]), ("/b", ["/b"]))
        live = [FakeWnckWindow(1, warp.WARP_CLASS, "/a")]
        self.assertEqual(layout._warp_signatures(live, state), {})

    def test_unmatched_window_gets_no_guessed_signature(self):
        state = self.warp_state(("/a", ["/a"]), ("/b", ["/b"]))
        live = [
            FakeWnckWindow(1, warp.WARP_CLASS, "/a"),
            FakeWnckWindow(2, warp.WARP_CLASS, "totally unrelated"),
        ]
        sigs = layout._warp_signatures(live, state)
        self.assertIn(1, sigs)
        self.assertNotIn(2, sigs)

    def test_ignores_non_warp_windows(self):
        state = self.warp_state(("/a", ["/a"]))
        live = [
            FakeWnckWindow(1, warp.WARP_CLASS, "/a"),
            FakeWnckWindow(2, "Google-chrome", "/a"),
        ]
        sigs = layout._warp_signatures(live, state)
        self.assertEqual(list(sigs), [1])

    def test_no_recorded_warp_state(self):
        live = [FakeWnckWindow(1, warp.WARP_CLASS, "/a")]
        self.assertEqual(layout._warp_signatures(live, None), {})
        self.assertEqual(layout._warp_signatures(live, {"windows": []}), {})


class WindowSignaturesMergeTest(unittest.TestCase):
    def test_terminal_and_warp_signatures_coexist(self):
        state = {"windows": [{
            "id": 1, "active_tab_index": 0,
            "tabs": [{"custom_title": None,
                      "panes": [{"cwd": "/srv", "focused": True}]}],
        }]}
        live = [FakeWnckWindow(7, warp.WARP_CLASS, "/srv")]
        with mock.patch.object(layout, "_terminal_signatures",
                               return_value={9: {"tabs": 4, "cwds": ["~"]}}):
            sigs = layout._window_signatures(live, warp_state=state)
        self.assertEqual(sigs[9]["tabs"], 4)
        self.assertEqual(sigs[7]["cwds"], ["/srv"])


class ShellRecordKindTest(unittest.TestCase):
    """The hook now writes two kinds of record into one directory."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.patch = mock.patch.object(collect.paths, "SHELLS_DIR", self.dir)
        self.patch.start()
        self.dbus = mock.patch.object(collect, "dbus_tree",
                                      return_value=(set(), set()))
        self.dbus.start()

    def tearDown(self):
        self.patch.stop()
        self.dbus.stop()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, rec):
        with open(os.path.join(self.dir, name), "w") as f:
            json.dump(rec, f)

    def test_missing_kind_reads_as_gnome(self):
        """Records written before Warp support must not be discarded."""
        self.assertEqual(collect.record_kind({"pid": 1}), "gnome")
        self.assertEqual(collect.record_kind({"pid": 1, "kind": "warp"}), "warp")

    def test_kind_filter(self):
        self.write("1.json", {"pid": 1, "kind": "gnome", "screen": "s"})
        self.write("2.json", {"pid": 2, "kind": "warp", "warp_uuid": "u"})
        with mock.patch.object(collect, "_is_live_tab", return_value=True):
            everything = collect.read_shell_records()
            gnome = collect.read_shell_records(kind="gnome")
            warp_only = collect.read_shell_records(kind="warp")
        self.assertEqual(len(everything), 2)
        self.assertEqual([r["pid"] for r in gnome], [1])
        self.assertEqual([r["pid"] for r in warp_only], [2])

    def test_pruning_for_one_kind_does_not_delete_the_other(self):
        """The data-loss guard. Judging a Warp record by gnome-terminal's rules
        marks it dead -- and read_shell_records deletes what it judges dead, so
        asking for gnome tabs would have deleted a live pane's conda env.
        """
        self.write("1.json", {"pid": 1, "kind": "warp", "warp_uuid": "u"})
        with mock.patch.object(collect, "_is_live_tab", return_value=True):
            records = collect.read_shell_records(prune=True, kind="gnome")
        self.assertEqual(records, [])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "1.json")))

    def test_dead_records_are_still_pruned(self):
        self.write("1.json", {"pid": 1, "kind": "warp", "warp_uuid": "u"})
        with mock.patch.object(collect, "_is_live_tab", return_value=False):
            collect.read_shell_records(prune=True, kind="gnome")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "1.json")))

    def test_warp_shells_sorted_by_timestamp(self):
        self.write("1.json", {"pid": 1, "kind": "warp", "warp_uuid": "u", "ts": 20})
        self.write("2.json", {"pid": 2, "kind": "warp", "warp_uuid": "u", "ts": 10})
        with mock.patch.object(collect, "_is_live_tab", return_value=True):
            records = collect.read_warp_shells()
        self.assertEqual([r["ts"] for r in records], [10, 20])

    def test_live_tab_dispatches_on_kind(self):
        gnome = {"pid": os.getpid(), "kind": "gnome", "screen": "s"}
        warp_rec = {"pid": os.getpid(), "kind": "warp", "warp_uuid": "u"}
        with mock.patch.object(collect, "_is_live_gnome_tab",
                               return_value=True) as g, \
             mock.patch.object(collect, "_is_live_warp_pane",
                               return_value=True) as w:
            collect._is_live_tab(gnome, set())
            collect._is_live_tab(warp_rec, set())
        g.assert_called_once()
        w.assert_called_once()

    def test_unknown_kind_is_not_validated_by_either(self):
        rec = {"pid": os.getpid(), "kind": "kitty"}
        self.assertFalse(collect._is_live_tab(rec, set()))

    def test_warp_pane_needs_matching_uuid_and_parent(self):
        pid = os.getpid()
        rec = {"pid": pid, "kind": "warp", "warp_uuid": "abc"}
        with mock.patch.object(collect, "_proc_environ",
                              return_value={"WARP_TERMINAL_SESSION_UUID": "abc"}), \
             mock.patch.object(collect, "_parent_comm", return_value="warp"):
            self.assertTrue(collect._is_live_warp_pane(rec))
        # A nested `bash` inside a pane inherits the UUID but not the parent.
        with mock.patch.object(collect, "_proc_environ",
                              return_value={"WARP_TERMINAL_SESSION_UUID": "abc"}), \
             mock.patch.object(collect, "_parent_comm", return_value="bash"):
            self.assertFalse(collect._is_live_warp_pane(rec))
        # A recycled PID whose environ names a different pane.
        with mock.patch.object(collect, "_proc_environ",
                              return_value={"WARP_TERMINAL_SESSION_UUID": "xyz"}), \
             mock.patch.object(collect, "_parent_comm", return_value="warp"):
            self.assertFalse(collect._is_live_warp_pane(rec))

    def test_warp_record_without_a_uuid_is_rejected(self):
        rec = {"pid": os.getpid(), "kind": "warp"}
        self.assertFalse(collect._is_live_warp_pane(rec))


class SnapshotWarpTest(unittest.TestCase):
    def terminals(self):
        return {"window_count": 0, "windows": [], "hook_installed": True}

    def warp_state(self, cwd="/home/u", conda=None, window_id=1):
        return {
            "running": True,
            "windows": [{
                "id": window_id, "active_tab_index": 0,
                "tabs": [{"custom_title": None, "panes": [
                    {"cwd": cwd, "conda": conda, "uuid": "aa"},
                ]}],
            }],
            "sources": [{"path": "/x", "mtime": 1, "size": 2}],
        }

    def build(self, warp_state):
        return snapshot.build(self.terminals(), {"degraded": False, "entries": []},
                              warp=warp_state)

    def test_build_includes_warp(self):
        snap = self.build(self.warp_state())
        self.assertEqual(len(snap["warp"]["windows"]), 1)

    def test_build_defaults_when_warp_absent(self):
        snap = snapshot.build(self.terminals(), {"degraded": False, "entries": []})
        self.assertEqual(snap["warp"], {"running": False, "windows": []})

    def test_digest_tracks_a_pane_changing_directory(self):
        """Without this, a cd in a Warp pane would never trigger a save, and
        the recorded signatures placement relies on would go stale."""
        a = snapshot.digest(self.build(self.warp_state(cwd="/home/u")))
        b = snapshot.digest(self.build(self.warp_state(cwd="/home/u/x")))
        self.assertNotEqual(a, b)

    def test_digest_tracks_conda(self):
        a = snapshot.digest(self.build(self.warp_state(conda=None)))
        b = snapshot.digest(self.build(self.warp_state(conda="torch212")))
        self.assertNotEqual(a, b)

    def test_digest_ignores_warp_window_ids(self):
        """Warp reassigns window ids freely; hashing them would rewrite a
        snapshot for a session that had not changed."""
        a = snapshot.digest(self.build(self.warp_state(window_id=1)))
        b = snapshot.digest(self.build(self.warp_state(window_id=99)))
        self.assertEqual(a, b)

    def test_digest_ignores_source_stats(self):
        state = self.warp_state()
        a = snapshot.digest(self.build(state))
        state = self.warp_state()
        state["sources"] = [{"path": "/x", "mtime": 999, "size": 888}]
        self.assertEqual(a, snapshot.digest(self.build(state)))

    def test_digest_is_order_independent_across_windows(self):
        one = self.warp_state()
        one["windows"].append({
            "id": 2, "active_tab_index": 0,
            "tabs": [{"custom_title": None,
                      "panes": [{"cwd": "/tmp", "conda": None}]}],
        })
        other = {"running": True, "windows": list(reversed(one["windows"]))}
        self.assertEqual(snapshot.digest(self.build(one)),
                         snapshot.digest(self.build(other)))


if __name__ == "__main__":
    unittest.main()
