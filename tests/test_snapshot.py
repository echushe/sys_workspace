"""Tests for snapshot digesting, write-avoidance and storage.

Write-avoidance is the reason snapshot.py exists as a separate module: the save
timer fires every couple of minutes for as long as the machine is up, and an
idle desktop must cost nothing. These tests are the ones that actually prove
that -- everything else in this module is plumbing around them.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sessionctl_lib import snapshot  # noqa: E402


def make_snapshot(
    cwd="/tmp", conda=None, mountpoint="/media/x",
    vscode_paths=(), chrome_urls=(), layout_windows=(),
):
    return snapshot.build(
        terminals={
            "window_count": 1,
            "windows": [{"index": 0, "tabs": [
                {"pid": 1, "screen": "/s", "cwd": cwd, "conda": conda}
            ]}],
            "hook_installed": True,
        },
        mounts_state={"degraded": False, "entries": [{"mountpoint": mountpoint}]},
        vscode={"windows": [{"folder": f"file://{p}", "path": p} for p in vscode_paths]},
        chrome={"profiles": [{"profile": "Default", "urls": list(chrome_urls)}]}
        if chrome_urls else {"profiles": []},
        layout={"workspace_count": 1, "windows": list(layout_windows)},
    )


def layout_window(wm_class="code", workspace=0, sticky=False, maximized=False,
                   geometry=None, title=""):
    return {
        "wm_class": wm_class,
        "title": title,
        "workspace": workspace,
        "sticky": sticky,
        "maximized": maximized,
        "geometry": geometry or {"x": 0, "y": 0, "w": 100, "h": 100},
    }


class DigestTest(unittest.TestCase):
    def test_same_state_same_digest(self):
        a = snapshot.digest(make_snapshot())
        b = snapshot.digest(make_snapshot())
        self.assertEqual(a, b)

    def test_cwd_change_changes_digest(self):
        a = snapshot.digest(make_snapshot(cwd="/tmp"))
        b = snapshot.digest(make_snapshot(cwd="/var"))
        self.assertNotEqual(a, b)

    def test_conda_change_changes_digest(self):
        a = snapshot.digest(make_snapshot(conda="torch209"))
        b = snapshot.digest(make_snapshot(conda="torch212"))
        self.assertNotEqual(a, b)

    def test_layout_window_title_is_not_part_of_the_digest(self):
        """A title changes on every browser tab or editor switch -- letting
        it feed the digest would defeat write-avoidance the moment someone
        looks at a different file. Layout capture (step 5) landed with
        exactly this exclusion; this is no longer a pinned-for-later
        contract, it is exercised against the real digest() code path.
        """
        s = make_snapshot(layout_windows=[layout_window(title="before")])
        d1 = snapshot.digest(s)
        s["layout"]["windows"][0]["title"] = "after"
        d2 = snapshot.digest(s)
        self.assertEqual(d1, d2)

    def test_layout_workspace_change_changes_digest(self):
        a = snapshot.digest(make_snapshot(layout_windows=[layout_window(workspace=0)]))
        b = snapshot.digest(make_snapshot(layout_windows=[layout_window(workspace=1)]))
        self.assertNotEqual(a, b)

    def test_layout_geometry_change_changes_digest(self):
        """Unlike a browser tab switch, a window actually being moved or
        resized IS restorable state a restore would reproduce -- geometry
        is deliberately not excluded from the digest the way title is.
        """
        a = snapshot.digest(make_snapshot(
            layout_windows=[layout_window(geometry={"x": 0, "y": 0, "w": 100, "h": 100})]
        ))
        b = snapshot.digest(make_snapshot(
            layout_windows=[layout_window(geometry={"x": 50, "y": 0, "w": 100, "h": 100})]
        ))
        self.assertNotEqual(a, b)

    def test_layout_maximized_change_changes_digest(self):
        a = snapshot.digest(make_snapshot(layout_windows=[layout_window(maximized=False)]))
        b = snapshot.digest(make_snapshot(layout_windows=[layout_window(maximized=True)]))
        self.assertNotEqual(a, b)

    def test_layout_sticky_change_changes_digest(self):
        a = snapshot.digest(make_snapshot(
            layout_windows=[layout_window(sticky=False, workspace=0)]
        ))
        b = snapshot.digest(make_snapshot(
            layout_windows=[layout_window(sticky=True, workspace=None)]
        ))
        self.assertNotEqual(a, b)

    def test_layout_window_order_does_not_matter(self):
        """Wnck's own enumeration order between two captures of an otherwise
        unchanged desktop is not guaranteed stable -- a reorder alone must
        not look like a change.
        """
        a = snapshot.digest(make_snapshot(layout_windows=[
            layout_window(wm_class="code", workspace=0),
            layout_window(wm_class="Google-chrome", workspace=1),
        ]))
        b = snapshot.digest(make_snapshot(layout_windows=[
            layout_window(wm_class="Google-chrome", workspace=1),
            layout_window(wm_class="code", workspace=0),
        ]))
        self.assertEqual(a, b)

    def test_timestamp_and_pid_are_not_part_of_the_digest(self):
        s1 = make_snapshot()
        s1["ts"] = "2020-01-01T00:00:00"
        s2 = make_snapshot()
        s2["ts"] = "2030-01-01T00:00:00"
        s2["terminals"]["windows"][0]["tabs"][0]["pid"] = 999
        self.assertEqual(snapshot.digest(s1), snapshot.digest(s2))

    def test_mount_change_changes_digest(self):
        a = snapshot.digest(make_snapshot(mountpoint="/media/a"))
        b = snapshot.digest(make_snapshot(mountpoint="/media/b"))
        self.assertNotEqual(a, b)

    def test_vscode_folder_change_changes_digest(self):
        a = snapshot.digest(make_snapshot(vscode_paths=["/a"]))
        b = snapshot.digest(make_snapshot(vscode_paths=["/b"]))
        self.assertNotEqual(a, b)

    def test_vscode_window_order_does_not_matter(self):
        """Two windows in a different order are the same set to restore."""
        a = snapshot.digest(make_snapshot(vscode_paths=["/a", "/b"]))
        b = snapshot.digest(make_snapshot(vscode_paths=["/b", "/a"]))
        self.assertEqual(a, b)

    def test_chrome_url_change_changes_digest(self):
        a = snapshot.digest(make_snapshot(chrome_urls=["https://a.example"]))
        b = snapshot.digest(make_snapshot(chrome_urls=["https://b.example"]))
        self.assertNotEqual(a, b)

    def test_vscode_source_stat_is_not_part_of_the_digest(self):
        """The mtime/size bookkeeping used to skip re-scanning is plumbing,
        not restorable state -- a source file being re-saved with identical
        content must not look like a change.
        """
        s1 = make_snapshot(vscode_paths=["/a"])
        s1["vscode"]["source"] = {"path": "/x", "mtime": 1.0, "size": 10}
        s2 = make_snapshot(vscode_paths=["/a"])
        s2["vscode"]["source"] = {"path": "/x", "mtime": 2.0, "size": 20}
        self.assertEqual(snapshot.digest(s1), snapshot.digest(s2))

    def test_firefox_running_change_changes_digest(self):
        a = make_snapshot()
        a["firefox"] = {"running": False, "profile": "p"}
        b = make_snapshot()
        b["firefox"] = {"running": True, "profile": "p"}
        self.assertNotEqual(snapshot.digest(a), snapshot.digest(b))


class LayoutSortKeyTest(unittest.TestCase):
    """`save` crashed on every run for 25 hours because of this sort.

    workspace is None for any window mutter gave no workspace -- a sticky
    one, or (with workspaces-only-on-primary) every window on a non-primary
    monitor. Sorting the raw tuples compares None against an int as soon as
    two windows share a wm_class, which Python 3 refuses. Nothing surfaced
    it until a restore replayed a day-old snapshot.

    It needs two *same-class* windows differing in kind, which is why one
    monitor never triggered it: the only workspace-less window was the GNOME
    Shell overlay, and its class is unique.
    """

    def mixed(self, workspaces, wm_class="code"):
        return make_snapshot(
            layout_windows=[layout_window(wm_class=wm_class, workspace=w)
                            for w in workspaces])

    def test_mixed_none_and_int_does_not_raise(self):
        snapshot.digest(self.mixed([0, None, 2]))

    def test_mixed_digest_is_order_independent(self):
        self.assertEqual(snapshot.digest(self.mixed([0, None, 2])),
                         snapshot.digest(self.mixed([None, 2, 0])))

    def test_all_none_is_fine(self):
        snapshot.digest(self.mixed([None, None]))

    def test_all_int_ordering_is_unchanged(self):
        """The key must reproduce plain tuple comparison when no None is
        present, so upgrading doesn't rewrite every existing snapshot.
        """
        entries = [("code", 2, False, True, ()), ("code", 0, False, True, ()),
                   ("code", 1, False, True, ())]
        self.assertEqual(sorted(entries, key=snapshot._layout_sort_key),
                         sorted(entries))

    def test_none_sorts_after_every_int(self):
        entries = [("code", None, False, True, ()), ("code", 5, False, True, ())]
        ordered = sorted(entries, key=snapshot._layout_sort_key)
        self.assertEqual(ordered[0][1], 5)
        self.assertIsNone(ordered[1][1])

    def test_distinct_layouts_still_differ(self):
        """The fix must not flatten real differences into one digest."""
        self.assertNotEqual(snapshot.digest(self.mixed([0, None])),
                            snapshot.digest(self.mixed([0, 1])))


class SaveTest(unittest.TestCase):
    """save() end to end, against a temp state dir so nothing touches the
    real ~/.local/state/sessionctl.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        p = mock.patch.multiple(
            "sessionctl_lib.paths",
            STATE_DIR=self.tmp,
            SNAPSHOTS_DIR=os.path.join(self.tmp, "snapshots"),
            LATEST_FILE=os.path.join(self.tmp, "latest.json"),
            HEARTBEAT_FILE=os.path.join(self.tmp, "heartbeat.json"),
        )
        p.start()
        self.addCleanup(p.stop)
        os.makedirs(os.path.join(self.tmp, "snapshots"))

    def test_first_save_writes(self):
        changed, snap_id, _ = snapshot.save(make_snapshot())
        self.assertTrue(changed)
        self.assertIsNotNone(snap_id)
        self.assertEqual(len(snapshot.list_snapshots()), 1)

    def test_unchanged_state_writes_nothing(self):
        snapshot.save(make_snapshot(cwd="/a"))
        before = snapshot.list_snapshots()
        changed, snap_id, _ = snapshot.save(make_snapshot(cwd="/a"))
        self.assertFalse(changed)
        self.assertIsNone(snap_id)
        self.assertEqual(snapshot.list_snapshots(), before)

    def test_unchanged_state_still_updates_heartbeat(self):
        """No snapshot write, but liveness must still be observable."""
        snapshot.save(make_snapshot(cwd="/a"))
        snapshot.write_heartbeat = mock.Mock(wraps=snapshot.write_heartbeat)
        snapshot.save(make_snapshot(cwd="/a"))
        snapshot.write_heartbeat.assert_called_once()

    def test_real_change_appends_a_new_snapshot(self):
        snapshot.save(make_snapshot(cwd="/a"))
        snapshot.save(make_snapshot(cwd="/b"))
        self.assertEqual(len(snapshot.list_snapshots()), 2)

    def test_prune_keeps_only_the_newest(self):
        for i in range(snapshot.KEEP + 5):
            snapshot.save(make_snapshot(cwd=f"/dir{i}"))
        self.assertEqual(len(snapshot.list_snapshots()), snapshot.KEEP)

    def test_latest_points_at_the_most_recent_change(self):
        snapshot.save(make_snapshot(cwd="/a"))
        snapshot.save(make_snapshot(cwd="/b"))
        latest = snapshot.load_latest()
        self.assertEqual(
            latest["terminals"]["windows"][0]["tabs"][0]["cwd"], "/b"
        )


class SessionNotYetRestoredTest(unittest.TestCase):
    """The post-reboot race that lost a session twice.

    After a reboot the save timer captures the freshly booted desktop --
    accurately -- and promotes it to latest.json before the user has run
    `restore`. Measured at 47 seconds from login to overwrite: latest went
    from 5 tabs (one of them in a project directory on torch209) to a single
    `~` tab, and the restore that followed faithfully replayed that one tab.

    Neither other guard applies, because nothing is broken: the terminal
    really exists, the layout really is visible. Only the boot id
    distinguishes "the session I saved before rebooting" from "the desktop
    that exists now".
    """

    OLD, NEW = "boot-old", "boot-new"

    def prev(self, boot_id=OLD):
        return {"boot_id": boot_id, "terminals": {"windows": []}}

    def test_previous_boot_and_not_restored_is_blocked(self):
        self.assertTrue(snapshot.session_not_yet_restored(
            self.prev(), self.NEW, restored_boot=None))

    def test_same_boot_is_never_blocked(self):
        """Ordinary saves during a session must be unaffected."""
        self.assertFalse(snapshot.session_not_yet_restored(
            self.prev(self.NEW), self.NEW, restored_boot=None))

    def test_restored_this_boot_unblocks(self):
        self.assertFalse(snapshot.session_not_yet_restored(
            self.prev(), self.NEW, restored_boot=self.NEW))

    def test_marker_from_an_older_boot_does_not_unblock(self):
        """A stale marker left by the previous boot must not count as having
        restored under this one.
        """
        self.assertTrue(snapshot.session_not_yet_restored(
            self.prev(), self.NEW, restored_boot=self.OLD))

    def test_previous_without_boot_id_is_not_blocked(self):
        """Snapshots written before this mechanism existed can't be reasoned
        about, and must not freeze saving after an upgrade.
        """
        self.assertFalse(snapshot.session_not_yet_restored(
            {"terminals": {"windows": []}}, self.NEW, restored_boot=None))

    def test_unreadable_boot_id_is_not_blocked(self):
        self.assertFalse(snapshot.session_not_yet_restored(
            self.prev(), None, restored_boot=None))

    def test_no_previous_snapshot_is_not_blocked(self):
        self.assertFalse(snapshot.session_not_yet_restored(
            None, self.NEW, restored_boot=None))


class MarkRestoredTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.multiple(
            snapshot.paths,
            STATE_DIR=self.tmp.name,
            CONFIG_DIR=os.path.join(self.tmp.name, "config"),
            SHELLS_DIR=os.path.join(self.tmp.name, "shells"),
            SNAPSHOTS_DIR=os.path.join(self.tmp.name, "snapshots"),
            RESTORED_FILE=os.path.join(self.tmp.name, "restored_boot"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_round_trip(self):
        snapshot.mark_restored("boot-xyz")
        self.assertEqual(snapshot.read_restored_boot(), "boot-xyz")

    def test_absent_marker_reads_as_none(self):
        self.assertIsNone(snapshot.read_restored_boot())

    def test_marking_twice_overwrites(self):
        snapshot.mark_restored("boot-1")
        snapshot.mark_restored("boot-2")
        self.assertEqual(snapshot.read_restored_boot(), "boot-2")

    def test_unreadable_boot_id_writes_nothing(self):
        """Passing None means "use the current boot"; the case that must
        write nothing is the current boot id being unreadable.
        """
        with mock.patch.object(snapshot.paths, "current_boot_id",
                               return_value=None):
            self.assertIsNone(snapshot.mark_restored())
        self.assertIsNone(snapshot.read_restored_boot())

    def test_defaults_to_the_current_boot(self):
        with mock.patch.object(snapshot.paths, "current_boot_id",
                               return_value="boot-current"):
            self.assertEqual(snapshot.mark_restored(), "boot-current")
        self.assertEqual(snapshot.read_restored_boot(), "boot-current")


class DesktopNotVisibleTest(unittest.TestCase):
    """The shutdown-save-destroys-the-session bug.

    sessionctl-logout.service ran `save` from ExecStop, after every shell had
    exited (EXIT traps already removed the per-tab state files) and after X
    was gone. It wrote "0 tabs, 0 windows placed" over a latest.json holding
    6 tabs and 5 windows, three seconds after the periodic save had recorded
    the real state. The unit is gone, but a save that could not observe the
    desktop must never overwrite one that did.
    """

    def prev(self, windows):
        return {"layout": {"windows": [{"wm_class": "code"}] * windows}}

    def test_empty_capture_over_a_populated_previous_is_refused(self):
        self.assertTrue(snapshot.desktop_not_visible(
            {"windows": []}, self.prev(5)))

    def test_populated_capture_is_fine(self):
        self.assertFalse(snapshot.desktop_not_visible(
            {"windows": [{"wm_class": "code"}]}, self.prev(5)))

    def test_no_previous_snapshot_is_fine(self):
        """First ever save on a machine with no X -- nothing to protect."""
        self.assertFalse(snapshot.desktop_not_visible({"windows": []}, None))

    def test_previous_also_empty_is_fine(self):
        """Never gets stuck: once latest itself has no windows, an empty
        capture is no longer a regression and saving resumes.
        """
        self.assertFalse(snapshot.desktop_not_visible(
            {"windows": []}, self.prev(0)))

    def test_missing_layout_sections_are_tolerated(self):
        self.assertFalse(snapshot.desktop_not_visible({}, {}))

    def test_closing_every_terminal_is_still_recorded(self):
        """The guard keys on layout visibility, not tab count, so genuinely
        closing all terminals while the desktop is observable still saves.
        """
        self.assertFalse(snapshot.desktop_not_visible(
            {"windows": [{"wm_class": "code"}]}, self.prev(3)))


class TerminalCaptureBrokenTest(unittest.TestCase):
    """The empty-session-overwrites-a-good-snapshot bug.

    A terminal opened before the data partition mounted never loaded the
    ~/.bashrc hook, so it reported no tabs for its whole life; the periodic
    save then wrote "0 tabs" over latest.json and the next restore had
    nothing to restore. window_count comes from gnome-terminal's D-Bus tree
    and the tabs come from the hook, so windows-but-no-tabs is provably a
    broken capture rather than an empty desktop.
    """

    def terminals(self, window_count, tab_counts=()):
        return {
            "window_count": window_count,
            "windows": [
                {"index": i, "tabs": [{"pid": i}] * n}
                for i, n in enumerate(tab_counts)
            ],
            "hook_installed": True,
        }

    def test_windows_open_but_no_tabs_is_broken(self):
        self.assertTrue(snapshot.terminal_capture_broken(
            self.terminals(1, []), hook_configured=True))

    def test_windows_open_with_only_empty_tab_lists_is_broken(self):
        """collect._group() still emits a window entry with an empty tab
        list, so "no tabs" has to be tested on the tabs, not the windows.
        """
        self.assertTrue(snapshot.terminal_capture_broken(
            self.terminals(2, [0, 0]), hook_configured=True))

    def test_tabs_captured_is_not_broken(self):
        self.assertFalse(snapshot.terminal_capture_broken(
            self.terminals(1, [4]), hook_configured=True))

    def test_no_terminal_windows_is_not_broken(self):
        """The user genuinely closed every terminal -- an empty terminal set
        is the truth here and must still be saved.
        """
        self.assertFalse(snapshot.terminal_capture_broken(
            self.terminals(0, []), hook_configured=True))

    def test_hook_not_configured_is_never_broken(self):
        """Without the ~/.bashrc line nothing is expected to report tabs, so
        this must not block saving forever.
        """
        self.assertFalse(snapshot.terminal_capture_broken(
            self.terminals(1, []), hook_configured=False))

    def test_partial_capture_is_not_broken(self):
        """One window reporting and another not is the documented
        multi-window grouping limitation, not a broken hook -- don't block
        saving on it.
        """
        self.assertFalse(snapshot.terminal_capture_broken(
            self.terminals(2, [0, 3]), hook_configured=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
