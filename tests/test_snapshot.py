"""Tests for snapshot digesting, write-avoidance and storage.

Write-avoidance is the reason snapshot.py exists as a separate module: the save
timer fires every couple of minutes for as long as the machine is up, and an
idle desktop must cost nothing. These tests are the ones that actually prove
that -- everything else in this module is plumbing around them.
"""

import contextlib
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
            PREVIOUS_BOOT_FILE=os.path.join(self.tmp, "previous_boot.json"),
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


class _BootFixture(unittest.TestCase):
    """A throwaway state dir, plus control over the boot id and clock."""

    T0 = "2026-10-01T10:00:00+08:00"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        p = mock.patch.multiple(
            "sessionctl_lib.paths",
            STATE_DIR=self.tmp,
            SNAPSHOTS_DIR=os.path.join(self.tmp, "snapshots"),
            LATEST_FILE=os.path.join(self.tmp, "latest.json"),
            HEARTBEAT_FILE=os.path.join(self.tmp, "heartbeat.json"),
            PREVIOUS_BOOT_FILE=os.path.join(self.tmp, "previous_boot.json"),
        )
        p.start()
        self.addCleanup(p.stop)
        os.makedirs(os.path.join(self.tmp, "snapshots"))

    @contextlib.contextmanager
    def at(self, boot, when=T0):
        with mock.patch.object(snapshot.paths, "current_boot_id",
                               return_value=boot), \
             mock.patch.object(snapshot, "_now_iso", return_value=when):
            yield

    def save(self, boot, cwd, when=T0):
        with self.at(boot, when):
            return snapshot.save(make_snapshot(cwd=cwd))

    @staticmethod
    def cwd(snap):
        return snap["terminals"]["windows"][0]["tabs"][0]["cwd"]

    def target(self, boot):
        with self.at(boot):
            return snapshot.restore_target()


class PreviousBootTest(_BootFixture):
    """Keeping the session from before a reboot without ever pausing `save`.

    Two failures bracket this. First: right after login the save timer
    captured the freshly booted desktop -- accurately -- and promoted it to
    latest.json 47 seconds in. The 5-tab session was gone before `restore`
    ran, and the restore replayed a single `~` tab.

    The fix for that refused to save at all until `restore` or `adopt` had
    run, and that caused the second failure. One reboot without a restore
    stopped saving on 2026-09-15. It stayed stopped through two more reboots,
    72,777 refusals in all. On 2026-10-11, `restore` replayed a 26-day-old
    one-tab session, and everything open since Sep 27 was lost.

    Now saving always proceeds, and the pre-reboot session is copied aside
    the moment latest.json would first be replaced by the new boot's.
    """

    def test_saving_never_pauses_after_a_reboot(self):
        self.save("A", "/work")
        changed, _, _ = self.save("B", "/home")
        self.assertTrue(changed)
        self.assertEqual(self.cwd(snapshot.load_latest()), "/home")

    def test_first_store_of_a_new_boot_pins_the_previous_session(self):
        self.save("A", "/a")
        self.save("A", "/b")
        self.save("B", "/home")
        pinned = snapshot.load_previous_boot()
        self.assertEqual(self.cwd(pinned), "/b")
        self.assertEqual(pinned["boot_id"], "A")

    def test_later_stores_in_the_same_boot_do_not_repin(self):
        self.save("A", "/a")
        self.save("B", "/x")
        self.save("B", "/y")
        self.assertEqual(self.cwd(snapshot.load_previous_boot()), "/a")

    def test_restore_target_after_reboot_is_the_pre_reboot_session(self):
        """The 47-second race: the new desktop is latest, restore ignores it."""
        self.save("A", "/work")
        self.save("B", "/home")
        self.assertEqual(self.cwd(self.target("B")), "/work")

    def test_restore_target_before_anything_is_stored_is_latest(self):
        """E.g. data disk not mounted yet, so every save refuses as degraded."""
        self.save("A", "/work")
        self.assertEqual(self.cwd(self.target("B")), "/work")

    def test_latest_wins_over_an_older_pin(self):
        """A boot in which nothing was stored leaves latest.json newer than the
        pin. The pin is from two boots back and must not be chosen.
        """
        self.save("A", "/a")
        self.save("B", "/b")       # pins A
        self.assertEqual(self.cwd(self.target("C")), "/b")

    def test_repeated_reboots_without_restore_never_fall_behind(self):
        """The 2026-10-11 failure: three reboots, no restore in between."""
        self.save("A", "/a")
        self.save("B", "/b")
        self.save("C", "/c")
        self.assertEqual(self.cwd(self.target("D")), "/c")
        self.save("D", "/d")
        self.assertEqual(self.cwd(self.target("D")), "/c")

    def test_without_a_reboot_the_target_is_latest(self):
        self.save("A", "/a")
        self.save("A", "/b")
        self.assertEqual(self.cwd(self.target("A")), "/b")

    def test_unreadable_boot_id_falls_back_to_latest(self):
        self.save("A", "/a")
        self.save("B", "/b")
        self.assertEqual(self.cwd(self.target(None)), "/b")

    def test_snapshot_without_boot_id_is_never_pinned(self):
        """Written before boot ids were recorded: nothing to reason about."""
        legacy = make_snapshot(cwd="/old")
        legacy.pop("boot_id")
        legacy["digest"] = snapshot.digest(legacy)
        snapshot._write_json(snapshot.paths.LATEST_FILE, legacy)
        self.save("B", "/new")
        self.assertIsNone(snapshot.load_previous_boot())
        self.assertEqual(self.cwd(self.target("B")), "/new")

    def test_boot_id_spellings_compare_equal(self):
        """/proc's dashed UUID vs the journal's bare hex."""
        self.assertTrue(snapshot._same_boot(
            "296a354d-d969-4e5e-a0b3-60035e8c48a5",
            "296a354dd9694e5ea0b360035e8c48a5"))
        self.assertFalse(snapshot._same_boot("A", None))
        self.assertFalse(snapshot._same_boot(None, None))


class LastVerifiedTest(_BootFixture):
    """A snapshot's ts is when the state last changed, not when it was last
    seen. An idle desktop can stay unchanged for hours while saving is
    healthy, so staleness has to be judged from the heartbeat.
    """

    T1 = "2026-10-01T18:00:00+08:00"
    T2 = "2026-10-02T09:00:00+08:00"

    def test_heartbeat_records_the_boot(self):
        self.save("A", "/a")
        self.assertEqual(snapshot.load_heartbeat()["boot_id"], "A")

    def test_unchanged_saves_advance_last_verified_but_not_ts(self):
        self.save("A", "/a", self.T0)
        self.save("A", "/a", self.T1)
        latest = snapshot.load_latest()
        self.assertEqual(latest["ts"], self.T0)
        self.assertEqual(snapshot.last_verified(latest), self.T1)

    def test_previous_boots_last_heartbeat_survives_the_reboot(self):
        self.save("A", "/a", self.T0)
        self.save("A", "/a", self.T1)          # last sign of life in boot A
        self.save("B", "/home", self.T2)
        pinned = snapshot.load_previous_boot()
        self.assertEqual(snapshot.last_verified(pinned), self.T1)

    def test_unchanged_first_save_of_a_new_boot_keeps_it_too(self):
        """Same desktop after reboot: latest.json isn't rewritten, but the
        heartbeat is, and must not lose boot A's last confirmation.
        """
        self.save("A", "/a", self.T0)
        self.save("A", "/a", self.T1)
        self.save("B", "/a", self.T2)
        latest = snapshot.load_latest()
        self.assertEqual(latest["boot_id"], "A")
        self.assertEqual(snapshot.last_verified(latest), self.T1)

    def test_falls_back_to_ts_without_a_heartbeat(self):
        self.save("A", "/a", self.T0)
        os.unlink(snapshot.paths.HEARTBEAT_FILE)
        self.assertEqual(
            snapshot.last_verified(snapshot.load_latest()), self.T0)


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
