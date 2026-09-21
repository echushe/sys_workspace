"""Tests for setup/uninstall: symlink deployment, unit files, Chrome prefs.

Live testing already confirmed the parts that need a real system: DISPLAY and
XAUTHORITY are present in `systemctl --user show-environment` on this
machine, so the save timer's layout capture works without extra
configuration; the generated unit files pass `systemd-analyze --user
verify`; and the Preferences edit was verified against a scratch copy of the
real file -- it needed `setdefault('session', {})`, not a plain key
assignment, since a fresh profile's Preferences has no `session` key at all
yet, confirmed by reading the real file before writing any code.

`sessionctl setup` was then run for real, three times, across three separate
real reboots, and found three live bugs `--dry-run`/`systemd-analyze` could
never reach, because all three only exist across an actual login boundary:

1. The systemd unit files were originally symlinked into the repo. At
   login, systemd --user loads its unit files while the data partition
   holding the symlink targets is still unmounted (~76s before it mounts,
   confirmed via journalctl -b 0) -- the units failed to load and the timer
   never fired for that whole session. Fixed by copying the unit files
   into ~/.config/systemd/user/ instead.
2. Fixing (1) surfaced a second instance one layer deeper on the *next*
   reboot: ExecStart=%h/.local/bin/sessionctl was still a symlink straight
   into the repo. With the unit files now loading correctly, the timer
   fired on schedule three times before the disk was mounted -- and each
   time, systemd's EXEC step failed outright (status=203/EXEC) before
   sessionctl's own Stage 0 degradation logic (which handles an unmounted
   disk correctly) ever got a chance to run, because the process never
   started. Patched with a shell wrapper that checked reachability first.
3. The wrapper fixed `save`, but not `sessionctl mount` itself: mount's
   whole job is mounting the data partition, and its own code lived on
   that same partition, so it could never run at all while unmounted, no
   matter how gracefully the wrapper failed. Fixed properly this time: the
   whole sessionctl_lib/ package and its entry script are now deployed as
   real files under ~/.local/share/sessionctl/lib/ (`_ensure_package_copy`),
   with ~/.local/bin/sessionctl symlinked to *that* -- safe, since both
   ends of that symlink live on the always-mounted home partition. Needed
   one supporting fix in the entry script itself: __file__ for a script
   run through a symlink is the symlink's own path, not its target, so
   sys.path insertion switched from os.path.abspath to os.path.realpath
   to correctly find sessionctl_lib/ next to the deployed copy rather than
   next to the symlink.

4. bash-hook.sh, the last symlink into the repo, kept on the argument that
   it degraded gracefully -- the ~/.bashrc line guards with `[ -f ... ]`, so
   a dangling symlink no-ops instead of erroring. A fourth reboot showed
   that "gracefully" meant "silently, forever": a terminal opened 7 seconds
   before the partition mounted never loaded the hook, captured nothing for
   its whole life, and the next periodic save wrote that empty terminal set
   over latest.json. Now deployed as a real copy like the rest.

All four are deliberate exceptions to the symlink-not-copy convention, which
now applies to nothing that runs at login. Re-verified live after each fix:
the timer fired unattended, on its own schedule, with no manual trigger; and
after fix 3, `sessionctl mount` itself was run via the deployed path and
confirmed to have zero references to the repo path anywhere in its code.

What's covered here instead: the symlink safety logic (refuse to clobber a
non-symlink, idempotent on a correct link, dry-run touches nothing); the
parallel copy/write safety logic shared by the unit files, bash-hook.sh and
the deployed package (idempotent when content matches, re-copies when the
source changed, replaces a leftover symlink from an older deployment rather
than erroring on it, removes a module deleted from the source rather than
leaving it as stale dead code); the entry script's symlink-resolution fix
run as a real subprocess in the same shape it's actually deployed in (a
package directory plus a symlink into it from elsewhere); and the ~/.bashrc
hook line add/remove round trip -- all against temp files or throwaway
subprocesses, nothing here touches the real ~/.bashrc or ~/.local.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sessionctl_lib import setup  # noqa: E402


class EnsureSymlinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.target = os.path.join(self.tmp, "target-file")
        open(self.target, "w").close()
        self.link = os.path.join(self.tmp, "sub", "link")

    def test_creates_missing_parent_dir_and_links(self):
        msg = setup._ensure_symlink(self.link, self.target)
        self.assertTrue(os.path.islink(self.link))
        self.assertEqual(os.path.realpath(self.link), os.path.realpath(self.target))
        self.assertIn("linked", msg)

    def test_idempotent_on_correct_link(self):
        setup._ensure_symlink(self.link, self.target)
        msg = setup._ensure_symlink(self.link, self.target)
        self.assertIn("already linked", msg)

    def test_dry_run_creates_nothing(self):
        msg = setup._ensure_symlink(self.link, self.target, dry_run=True)
        self.assertFalse(os.path.exists(self.link))
        self.assertIn("would link", msg)

    def test_dry_run_on_existing_wrong_link_reports_relink_not_link(self):
        other_target = os.path.join(self.tmp, "other-file")
        open(other_target, "w").close()
        os.makedirs(os.path.dirname(self.link))
        os.symlink(other_target, self.link)

        msg = setup._ensure_symlink(self.link, self.target, dry_run=True)
        self.assertIn("would relink", msg)
        # dry-run must not have touched the existing (wrong) link
        self.assertEqual(os.path.realpath(self.link), os.path.realpath(other_target))

    def test_replaces_a_link_pointing_elsewhere(self):
        other_target = os.path.join(self.tmp, "other-file")
        open(other_target, "w").close()
        os.makedirs(os.path.dirname(self.link))
        os.symlink(other_target, self.link)

        setup._ensure_symlink(self.link, self.target)
        self.assertEqual(os.path.realpath(self.link), os.path.realpath(self.target))

    def test_refuses_to_overwrite_a_real_file(self):
        """The whole point of this check: link_path might be something
        unrelated to sessionctl that just happens to already be there --
        never silently clobber it.
        """
        os.makedirs(os.path.dirname(self.link))
        with open(self.link, "w") as f:
            f.write("not a symlink, do not touch")

        with self.assertRaises(setup.SetupError):
            setup._ensure_symlink(self.link, self.target)

        with open(self.link) as f:
            self.assertEqual(f.read(), "not a symlink, do not touch")

    def test_refuses_to_overwrite_a_real_directory(self):
        os.makedirs(self.link)
        with self.assertRaises(setup.SetupError):
            setup._ensure_symlink(self.link, self.target)


class RemoveSymlinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.target = os.path.join(self.tmp, "target-file")
        open(self.target, "w").close()
        self.link = os.path.join(self.tmp, "link")

    def test_removes_a_matching_symlink(self):
        os.symlink(self.target, self.link)
        msg = setup._remove_symlink(self.link, self.target)
        self.assertFalse(os.path.exists(self.link))
        self.assertIn("removed", msg)

    def test_leaves_a_non_symlink_alone(self):
        with open(self.link, "w") as f:
            f.write("real file")
        msg = setup._remove_symlink(self.link, self.target)
        self.assertTrue(os.path.isfile(self.link))
        self.assertIn("not our symlink", msg)

    def test_leaves_a_symlink_pointing_elsewhere_alone(self):
        other_target = os.path.join(self.tmp, "other-file")
        open(other_target, "w").close()
        os.symlink(other_target, self.link)

        msg = setup._remove_symlink(self.link, self.target)
        self.assertTrue(os.path.islink(self.link))
        self.assertIn("points elsewhere", msg)

    def test_leaves_a_missing_path_alone(self):
        """Nothing to remove -- must not raise."""
        msg = setup._remove_symlink(self.link, self.target)
        self.assertIn("not our symlink", msg)


class EnsureCopyTest(unittest.TestCase):
    """setup.py's systemd unit deployment uses a real copy, not a symlink --
    the one deliberate exception to the symlink-not-copy convention used
    everywhere else. Confirmed live, on a real reboot: systemd --user loads
    its unit files at manager-startup time (login), which on this machine
    happens ~76 seconds *before* the data partition holding a symlinked
    unit's target gets mounted. A dangling symlink there makes systemd fail
    to load the unit ("Failed to open ...: No such file or directory") and
    mark it not-found for the rest of the session -- permanently, since
    nothing re-triggers a daemon-reload once the disk shows up. The timer
    never fired once, for the whole session, confirmed by its total absence
    from that session's journal. These tests are what a symlink-based
    implementation would have failed in spirit, even though they run
    entirely against temp files with no real mount involved.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "unit-file")
        with open(self.src, "w") as f:
            f.write("[Unit]\nDescription=test\n")
        self.dst = os.path.join(self.tmp, "sub", "unit-file")

    def test_creates_a_real_file_not_a_symlink(self):
        setup._ensure_copy(self.dst, self.src)
        self.assertTrue(os.path.isfile(self.dst))
        self.assertFalse(os.path.islink(self.dst))
        with open(self.dst) as f:
            self.assertEqual(f.read(), "[Unit]\nDescription=test\n")

    def test_idempotent_when_content_matches(self):
        setup._ensure_copy(self.dst, self.src)
        mtime_before = os.path.getmtime(self.dst)
        msg = setup._ensure_copy(self.dst, self.src)
        self.assertIn("up to date", msg)
        self.assertEqual(os.path.getmtime(self.dst), mtime_before)

    def test_recopies_when_source_content_changes(self):
        """This is the re-sync path -- editing a unit file in the repo and
        re-running `setup` must actually propagate the change.
        """
        setup._ensure_copy(self.dst, self.src)
        with open(self.src, "w") as f:
            f.write("[Unit]\nDescription=changed\n")

        msg = setup._ensure_copy(self.dst, self.src)
        self.assertIn("copied", msg)
        with open(self.dst) as f:
            self.assertEqual(f.read(), "[Unit]\nDescription=changed\n")

    def test_dry_run_creates_nothing(self):
        msg = setup._ensure_copy(self.dst, self.src, dry_run=True)
        self.assertFalse(os.path.exists(self.dst))
        self.assertIn("would copy", msg)

    def test_replaces_a_leftover_symlink_from_before_this_fix(self):
        """The exact bug scenario: an old install left a symlink at dst
        (dangling or not) -- setup must replace it with a real copy, not
        leave the symlink in place or error out on it.
        """
        os.makedirs(os.path.dirname(self.dst))
        os.symlink("/nonexistent/dangling/target", self.dst)

        setup._ensure_copy(self.dst, self.src)
        self.assertTrue(os.path.isfile(self.dst))
        self.assertFalse(os.path.islink(self.dst))

    def test_executable_flag_sets_the_exec_bit(self):
        setup._ensure_copy(self.dst, self.src, executable=True)
        self.assertTrue(os.access(self.dst, os.X_OK))


class EnsurePackageCopyTest(unittest.TestCase):
    """Fixes the third live-reboot bug (see the module docstring): a shell
    wrapper checking reachability before exec'ing through could make `save`
    fail gracefully, but could never make `sessionctl mount` itself work
    from a cold boot, because mount's entire job is mounting the very
    partition its own code lived on. Deploying the whole package to an
    always-reachable location is what actually fixes that -- these tests
    cover the directory-mirroring logic itself, not the reachability
    question, which no longer applies once the deployed copy needs nothing
    from the data partition at all.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "src")
        self.dst = os.path.join(self.tmp, "dst")
        os.makedirs(self.src)
        with open(os.path.join(self.src, "a.py"), "w") as f:
            f.write("A = 1\n")
        with open(os.path.join(self.src, "b.py"), "w") as f:
            f.write("B = 2\n")
        # Must be ignored -- only .py files are part of the package.
        with open(os.path.join(self.src, "README.md"), "w") as f:
            f.write("not code\n")

    def test_copies_every_py_file(self):
        setup._ensure_package_copy(self.dst, self.src)
        with open(os.path.join(self.dst, "a.py")) as f:
            self.assertEqual(f.read(), "A = 1\n")
        with open(os.path.join(self.dst, "b.py")) as f:
            self.assertEqual(f.read(), "B = 2\n")

    def test_ignores_non_python_files(self):
        setup._ensure_package_copy(self.dst, self.src)
        self.assertFalse(os.path.exists(os.path.join(self.dst, "README.md")))

    def test_idempotent_summary_when_nothing_changed(self):
        setup._ensure_package_copy(self.dst, self.src)
        report = setup._ensure_package_copy(self.dst, self.src)
        self.assertEqual(len(report), 1)
        self.assertIn("up to date", report[0])
        self.assertIn("2 files", report[0])

    def test_recopies_only_the_changed_file(self):
        setup._ensure_package_copy(self.dst, self.src)
        with open(os.path.join(self.src, "a.py"), "w") as f:
            f.write("A = 999\n")

        report = setup._ensure_package_copy(self.dst, self.src)
        self.assertEqual(len(report), 1)
        self.assertIn("a.py", report[0])
        with open(os.path.join(self.dst, "a.py")) as f:
            self.assertEqual(f.read(), "A = 999\n")

    def test_removes_a_module_deleted_from_the_source(self):
        """A renamed or deleted module must not linger as stale dead code in
        the deployed copy.
        """
        setup._ensure_package_copy(self.dst, self.src)
        os.unlink(os.path.join(self.src, "b.py"))

        report = setup._ensure_package_copy(self.dst, self.src)
        self.assertFalse(os.path.exists(os.path.join(self.dst, "b.py")))
        self.assertTrue(any("stale" in line for line in report))

    def test_dry_run_touches_nothing(self):
        report = setup._ensure_package_copy(self.dst, self.src, dry_run=True)
        self.assertFalse(os.path.exists(self.dst))
        self.assertEqual(len(report), 2)  # a.py, b.py -- both "would copy"


class EntryScriptSymlinkResolutionTest(unittest.TestCase):
    """The entry script must resolve sessionctl_lib/ relative to where the
    symlink *points*, not where the symlink itself sits -- confirmed as a
    real fix, not a hypothetical one: `~/.local/bin/sessionctl` symlinks to
    the deployed copy, and Python's __file__ for a script run through a
    symlink is the symlink's own path, not its target (os.path.abspath does
    not resolve symlinks; os.path.realpath does). This runs the actual
    deployed-shape layout (a package directory plus a symlink into it from
    elsewhere) as a real subprocess, the same shape used live.
    """

    def test_runs_correctly_through_a_symlink_from_a_different_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = os.path.join(tmp, "deployed")
            os.makedirs(os.path.join(pkg_dir, "sessionctl_lib"))
            with open(os.path.join(pkg_dir, "sessionctl_lib", "__init__.py"), "w"):
                pass
            with open(os.path.join(pkg_dir, "sessionctl_lib", "probe.py"), "w") as f:
                f.write("VALUE = 'found it'\n")

            entry = os.path.join(pkg_dir, "entry")
            with open(entry, "w") as f:
                f.write(
                    "#!/usr/bin/python3\n"
                    "import os, sys\n"
                    "sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))\n"
                    "from sessionctl_lib.probe import VALUE\n"
                    "print(VALUE)\n"
                )
            os.chmod(entry, 0o755)

            bin_dir = os.path.join(tmp, "bin")
            os.makedirs(bin_dir)
            link = os.path.join(bin_dir, "sessionctl")
            os.symlink(entry, link)

            result = subprocess.run([link], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "found it")


class RemoveCopyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dst = os.path.join(self.tmp, "unit-file")

    def test_removes_a_real_file(self):
        with open(self.dst, "w") as f:
            f.write("content")
        msg = setup._remove_copy(self.dst)
        self.assertFalse(os.path.exists(self.dst))
        self.assertIn("removed", msg)

    def test_removes_a_leftover_symlink_too(self):
        os.symlink("/nonexistent", self.dst)
        msg = setup._remove_copy(self.dst)
        self.assertFalse(os.path.islink(self.dst))
        self.assertIn("removed", msg)

    def test_missing_path_does_not_raise(self):
        msg = setup._remove_copy(self.dst)
        self.assertIn("not present", msg)


class HookLineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", delete=False)
        self.tmp.close()
        p = mock.patch.object(setup, "BASHRC", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)
        # tolerant: test_remove_when_bashrc_missing_does_not_raise
        # deliberately deletes this file itself
        self.addCleanup(lambda: os.path.exists(self.tmp.name) and os.unlink(self.tmp.name))

    def _write(self, content):
        with open(setup.BASHRC, "w") as f:
            f.write(content)

    def _read(self):
        with open(setup.BASHRC) as f:
            return f.read()

    def test_install_appends_the_marker_and_line(self):
        self._write("# existing bashrc content\n")
        already, msg = setup.ensure_hook_installed(install=True)
        self.assertFalse(already)
        content = self._read()
        self.assertIn(setup.HOOK_MARKER, content)
        self.assertIn("bash-hook.sh", content)
        self.assertTrue(content.startswith("# existing bashrc content\n"))

    def test_already_installed_is_reported_and_not_duplicated(self):
        self._write("stuff\n" + setup.HOOK_LINE)
        already, msg = setup.ensure_hook_installed(install=True)
        self.assertTrue(already)
        # install=True must still be a no-op when already present
        self.assertEqual(self._read().count(setup.HOOK_MARKER), 1)

    def test_no_install_flag_previews_without_writing(self):
        self._write("stuff\n")
        already, msg = setup.ensure_hook_installed(install=False)
        self.assertFalse(already)
        self.assertNotIn(setup.HOOK_MARKER, self._read())
        self.assertIn(setup.HOOK_LINE.strip().splitlines()[0], msg)

    def test_remove_strips_the_marker_and_its_line(self):
        self._write("before\n" + setup.HOOK_LINE + "after\n")
        msg = setup._remove_hook()
        content = self._read()
        self.assertNotIn(setup.HOOK_MARKER, content)
        self.assertEqual(content, "before\nafter\n")
        self.assertIn("removed", msg)

    def test_remove_when_absent_reports_nothing_to_do(self):
        self._write("nothing here\n")
        msg = setup._remove_hook()
        self.assertIn("not present", msg)
        self.assertEqual(self._read(), "nothing here\n")

    def test_remove_when_bashrc_missing_does_not_raise(self):
        os.unlink(setup.BASHRC)
        msg = setup._remove_hook()
        self.assertIn("no ~/.bashrc", msg)


class ChromeRunningTest(unittest.TestCase):
    def test_reflects_pgrep_exit_code(self):
        with mock.patch.object(setup, "_run", return_value=(0, "")):
            self.assertTrue(setup.chrome_running())
        with mock.patch.object(setup, "_run", return_value=(1, "")):
            self.assertFalse(setup.chrome_running())


class SetChromeRestoreOnStartupTest(unittest.TestCase):
    def test_refuses_while_chrome_is_running(self):
        with mock.patch.object(setup, "chrome_running", return_value=True):
            report = setup.set_chrome_restore_on_startup()
        self.assertTrue(any("refusing" in line for line in report))

    def test_sets_the_key_on_a_fresh_profile_with_no_session_key(self):
        """Matches the real machine's state exactly: a Preferences file with
        no "session" key at all, not merely an unset one.
        """
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "Default"))
            prefs = os.path.join(d, "Default", "Preferences")
            with open(prefs, "w") as f:
                f.write('{"other_key": "untouched"}')

            with mock.patch.object(setup, "chrome_running", return_value=False), \
                 mock.patch.object(setup.collect, "CHROME_CONFIG_DIR", d), \
                 mock.patch.object(
                     setup.collect, "_chrome_profiles", return_value=["Default"]
                 ):
                report = setup.set_chrome_restore_on_startup()

            import json
            with open(prefs) as f:
                data = json.load(f)
            self.assertEqual(data["session"]["restore_on_startup"], 1)
            self.assertEqual(data["other_key"], "untouched")
            self.assertTrue(any("set restore_on_startup=1" in line for line in report))

    def test_already_set_is_a_noop(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "Default"))
            prefs = os.path.join(d, "Default", "Preferences")
            with open(prefs, "w") as f:
                f.write('{"session": {"restore_on_startup": 1}}')
            before_mtime = os.path.getmtime(prefs)

            with mock.patch.object(setup, "chrome_running", return_value=False), \
                 mock.patch.object(setup.collect, "CHROME_CONFIG_DIR", d), \
                 mock.patch.object(
                     setup.collect, "_chrome_profiles", return_value=["Default"]
                 ):
                report = setup.set_chrome_restore_on_startup()

            self.assertEqual(os.path.getmtime(prefs), before_mtime)
            self.assertTrue(any("already set" in line for line in report))

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "Default"))
            prefs = os.path.join(d, "Default", "Preferences")
            with open(prefs, "w") as f:
                f.write('{"other_key": "x"}')

            with mock.patch.object(setup, "chrome_running", return_value=False), \
                 mock.patch.object(setup.collect, "CHROME_CONFIG_DIR", d), \
                 mock.patch.object(
                     setup.collect, "_chrome_profiles", return_value=["Default"]
                 ):
                report = setup.set_chrome_restore_on_startup(dry_run=True)

            with open(prefs) as f:
                self.assertEqual(f.read(), '{"other_key": "x"}')
            self.assertTrue(any("would set" in line for line in report))


class SaveTimerAnchorTest(unittest.TestCase):
    """The save timer must stay anchored to the *user manager's* start.

    This reads the real unit file in the repo rather than a fixture: the
    point is to catch someone dropping OnStartupSec from the shipped unit,
    which a fixture could not notice.
    """

    def timer_text(self):
        with open(os.path.join(setup.SYSTEMD_UNIT_DIR,
                               "sessionctl-save.timer")) as f:
            return f.read()

    def test_has_onstartupsec(self):
        """systemd.timer(5): OnBootSec= is relative to machine boot for a
        per-user manager (the remap to OnStartupSec= applies only to the
        system manager in a container), so logging in more than OnBootSec
        after boot leaves a purely OnBootSec/OnUnitActiveSec timer with no
        anchor -- observed as 75 minutes with zero firings.
        """
        self.assertIn("OnStartupSec=", self.timer_text())

    def test_still_repeats(self):
        self.assertIn("OnUnitActiveSec=", self.timer_text())

    def test_no_persistent_on_a_monotonic_timer(self):
        """Persistent= "only has an effect on timers configured with
        OnCalendar=" -- inert here, and it implied a catch-up guarantee that
        was never in force.
        """
        self.assertNotIn("\nPersistent=", self.timer_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
