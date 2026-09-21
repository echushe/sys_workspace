"""Tests for Stage 0 mount handling.

The interesting branches cannot be exercised against the real disk: the
partition Stage 0 protects is the one holding this repo, the VSCode windows and
the running shells, so unmounting it to test is not an option. udisks loop
devices are no substitute either -- udev creates no /dev/disk/by-uuid entry for
them, and udisks auto-destroys them on unmount.

So the udisksctl call and the mount-state checks are stubbed, and what is tested
is the decision logic around them.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sessionctl_lib import mounts  # noqa: E402


def entry(**kw):
    e = {
        "uuid": "1111-2222",
        "mountpoint": "/media/test/disk",
        "fstype": "ext4",
        "verify": [],
    }
    e.update(kw)
    return e


class MountEntryTest(unittest.TestCase):
    """mount_entry's branching, with the device and udisksctl stubbed."""

    def setUp(self):
        # A fake /dev/disk/by-uuid containing exactly one device.
        self.by_uuid = tempfile.mkdtemp()
        open(os.path.join(self.by_uuid, "1111-2222"), "w").close()
        p = mock.patch.object(mounts, "BY_UUID", self.by_uuid)
        p.start()
        self.addCleanup(p.stop)

    def test_already_mounted_is_a_noop(self):
        with mock.patch.object(mounts, "is_mounted", return_value=True), \
             mock.patch.object(mounts, "_run") as run:
            msg = mounts.mount_entry(entry())
        self.assertIn("already mounted", msg)
        run.assert_not_called()

    def test_already_mounted_but_path_missing_is_an_error(self):
        """A mounted device whose contents are wrong must not pass Stage 0."""
        with mock.patch.object(mounts, "is_mounted", return_value=True), \
             mock.patch.object(mounts, "verify_paths", return_value=["~/Projects"]):
            with self.assertRaises(mounts.MountError) as cm:
                mounts.mount_entry(entry(verify=["~/Projects"]))
        self.assertIn("~/Projects", str(cm.exception))

    def test_missing_device_is_an_error(self):
        with mock.patch.object(mounts, "is_mounted", return_value=False):
            with self.assertRaises(mounts.MountError) as cm:
                mounts.mount_entry(entry(uuid="no-such-uuid"))
        self.assertIn("no-such-uuid", str(cm.exception))

    def test_dry_run_does_not_mount(self):
        with mock.patch.object(mounts, "is_mounted", return_value=False), \
             mock.patch.object(mounts, "_run") as run:
            msg = mounts.mount_entry(entry(), dry_run=True)
        self.assertIn("would mount", msg)
        self.assertIn("udisksctl mount -b", msg)
        run.assert_not_called()

    def test_successful_mount(self):
        states = iter([False, True])  # before the call, then after
        with mock.patch.object(mounts, "is_mounted", side_effect=lambda e: next(states)), \
             mock.patch.object(mounts, "_run", return_value=(0, "Mounted")) as run:
            msg = mounts.mount_entry(entry())
        self.assertIn("mounted", msg)
        self.assertEqual(run.call_args[0][0][:3], ["udisksctl", "mount", "-b"])

    def test_udisksctl_failure_is_an_error(self):
        with mock.patch.object(mounts, "is_mounted", return_value=False), \
             mock.patch.object(mounts, "_run", return_value=(1, "Not authorized")):
            with self.assertRaises(mounts.MountError) as cm:
                mounts.mount_entry(entry())
        self.assertIn("Not authorized", str(cm.exception))

    def test_already_mounted_message_is_tolerated(self):
        """udisksctl racing another mounter is success, not failure."""
        states = iter([False, True])
        with mock.patch.object(mounts, "is_mounted", side_effect=lambda e: next(states)), \
             mock.patch.object(mounts, "_run", return_value=(1, "Error: already mounted")):
            msg = mounts.mount_entry(entry())
        self.assertIn("mounted", msg)

    def test_silent_failure_is_caught(self):
        """udisksctl exiting 0 without actually mounting must not pass."""
        with mock.patch.object(mounts, "is_mounted", return_value=False), \
             mock.patch.object(mounts, "_run", return_value=(0, "Mounted")):
            with self.assertRaises(mounts.MountError) as cm:
                mounts.mount_entry(entry())
        self.assertIn("still not mounted", str(cm.exception))


class EnsureAllTest(unittest.TestCase):
    def test_stops_at_the_first_failure(self):
        """Never half-restore: one bad mount aborts the whole run."""
        calls = []

        def fake(e, dry_run=False):
            calls.append(e["uuid"])
            if e["uuid"] == "bad":
                raise mounts.MountError("boom")
            return "ok"

        with mock.patch.object(mounts, "mount_entry", side_effect=fake):
            with self.assertRaises(mounts.MountError):
                mounts.ensure_all(
                    [entry(uuid="good"), entry(uuid="bad"), entry(uuid="never")]
                )
        self.assertEqual(calls, ["good", "bad"])


class VerifyPathsTest(unittest.TestCase):
    def test_reports_only_the_paths_that_do_not_resolve(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real")
            os.makedirs(real)
            dangling = os.path.join(d, "dangling")
            os.symlink(os.path.join(d, "gone"), dangling)

            missing = mounts.verify_paths(entry(verify=[real, dangling]))
        self.assertEqual(missing, [dangling])


class DetectTest(unittest.TestCase):
    def test_skips_paths_on_the_root_filesystem(self):
        """Root is always mounted, so it is not Stage 0's problem."""
        with mock.patch.object(mounts, "_findmnt", return_value=("/dev/root", "ext4")):
            self.assertEqual(mounts.detect(["/etc"]), [])

    def test_groups_several_paths_on_one_device(self):
        """~/Projects and ~/extra_disk_2t share a partition -> one entry."""

        def findmnt(target):
            return ("/", "ext4") if target == "/" else ("/dev/sdb1", "ext4")

        def run(cmd):
            return (0, "/media/test/disk")

        with mock.patch.object(mounts, "_findmnt", side_effect=findmnt), \
             mock.patch.object(mounts, "_run", side_effect=run), \
             mock.patch.object(mounts, "_uuid_of_device", return_value="1111-2222"), \
             mock.patch("os.path.exists", return_value=True):
            got = mounts.detect(["~/Projects", "~/extra_disk_2t"])

        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["uuid"], "1111-2222")
        self.assertEqual(got[0]["verify"], ["~/Projects", "~/extra_disk_2t"])


class EnsureOptionalTest(unittest.TestCase):
    """The best-effort counterpart: a disk that isn't there is not fatal."""

    def test_continues_past_a_failure_and_never_raises(self):
        def mount_entry(e, dry_run=False):
            if e["uuid"] == "bad":
                raise mounts.MountError("not connected")
            return f"mounted {e['uuid']}"

        with mock.patch.object(mounts, "mount_entry", side_effect=mount_entry):
            report, failures = mounts.ensure_optional(
                [entry(uuid="bad"), entry(uuid="good")]
            )

        self.assertEqual(report, ["mounted good"])
        self.assertEqual(failures, ["not connected"])


class ScanHomeLinksTest(unittest.TestCase):
    """Finding disks from the dangling symlinks that need them.

    Built against a fake $HOME, /media root and udev directories, since the real
    ones can't be unmounted to test and their contents differ per machine.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.media = os.path.join(self.tmp, "media", "tester")
        self.devdir = os.path.join(self.tmp, "dev")
        self.by_label = os.path.join(self.tmp, "by-label")
        self.by_uuid = os.path.join(self.tmp, "by-uuid")
        for d in (self.home, self.media, self.devdir, self.by_label, self.by_uuid):
            os.makedirs(d)

        for value, attr in ((self.by_label, "BY_LABEL"), (self.by_uuid, "BY_UUID")):
            p = mock.patch.object(mounts, attr, value)
            p.start()
            self.addCleanup(p.stop)
        for name, kw in (("_media_root", {"return_value": self.media}),
                         ("_fstype_of_device", {"return_value": "ext4"})):
            p = mock.patch.object(mounts, name, **kw)
            p.start()
            self.addCleanup(p.stop)

    def add_device(self, uuid, label=None):
        """A block device as udev exposes it: by-uuid always, by-label if named."""
        node = os.path.join(self.devdir, uuid)
        open(node, "w").close()
        os.symlink(node, os.path.join(self.by_uuid, uuid))
        if label:
            os.symlink(node, os.path.join(self.by_label, label))

    def link(self, name, target):
        os.symlink(target, os.path.join(self.home, name))

    def test_finds_a_dangling_link_and_maps_it_by_label(self):
        self.add_device("d66e0f19", label="Samsung2T")
        self.link("extra_disk_2t", os.path.join(self.media, "Samsung2T"))

        got = mounts.scan_home_links(home=self.home)

        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["uuid"], "d66e0f19")
        self.assertEqual(got[0]["mountpoint"], os.path.join(self.media, "Samsung2T"))
        self.assertEqual(got[0]["verify"], ["~/extra_disk_2t"])

    def test_ignores_a_link_that_already_resolves(self):
        self.add_device("d66e0f19", label="Samsung2T")
        os.makedirs(os.path.join(self.media, "Samsung2T"))
        self.link("extra_disk_2t", os.path.join(self.media, "Samsung2T"))

        self.assertEqual(mounts.scan_home_links(home=self.home), [])

    def test_ignores_a_link_pointing_outside_the_media_root(self):
        """Mounting would put the disk elsewhere, leaving the link still broken."""
        self.add_device("d66e0f19", label="Samsung2T")
        self.link("extra_disk_2t", os.path.join(self.tmp, "mnt", "Samsung2T"))

        self.assertEqual(mounts.scan_home_links(home=self.home), [])

    def test_ignores_directories_and_unknown_labels(self):
        os.makedirs(os.path.join(self.home, "Projects"))
        self.link("extra_disk_gone", os.path.join(self.media, "NoSuchDisk"))

        self.assertEqual(mounts.scan_home_links(home=self.home), [])

    def test_falls_back_to_uuid_when_the_filesystem_has_no_label(self):
        self.add_device("A6A2174CA2171FFB")
        self.link("extra_disk_1t", os.path.join(self.media, "A6A2174CA2171FFB"))

        got = mounts.scan_home_links(home=self.home)

        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["uuid"], "A6A2174CA2171FFB")

    def test_two_links_to_one_disk_collapse_into_one_entry(self):
        self.add_device("d66e0f19", label="Samsung2T")
        self.link("extra_disk_2t", os.path.join(self.media, "Samsung2T"))
        self.link("data", os.path.join(self.media, "Samsung2T"))

        got = mounts.scan_home_links(home=self.home)

        self.assertEqual(len(got), 1)
        self.assertEqual(sorted(got[0]["verify"]), ["~/data", "~/extra_disk_2t"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
