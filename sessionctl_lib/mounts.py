"""Stage 0: make sure the storage holding the session's paths is mounted.

This runs before every other restore stage. ``~/Projects`` and
``~/extra_disk_2t`` are symlinks into a partition that is not in /etc/fstab
and that udisks reports as ``HintAuto: false``, so it is not mounted at boot --
it appears only when something opens it. Until then those symlinks dangle, and
a restore would open VSCode on non-existent folders and drop every terminal
into $HOME while looking like it succeeded.

So: mount first, verify the paths actually resolve, and refuse to go further if
they do not.
"""

import getpass
import os
import subprocess

# Paths whose existence a restore depends on. Used to auto-detect the config.
DEFAULT_VERIFY_PATHS = ["~/Projects", "~/extra_disk_2t"]

BY_UUID = "/dev/disk/by-uuid"
BY_LABEL = "/dev/disk/by-label"


class MountError(Exception):
    """A required mount could not be established or verified."""


def _run(cmd):
    """Run a command, returning (rc, stdout+stderr)."""
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def _findmnt(target):
    """Return (source_device, fstype) for a mount target, or (None, None)."""
    rc, out = _run(["findmnt", "-n", "-o", "SOURCE,FSTYPE", "--target", target])
    if rc != 0 or not out:
        return None, None
    parts = out.split()
    return (parts[0], parts[1]) if len(parts) >= 2 else (parts[0], None)


def _uuid_of_device(device):
    """Reverse-map a device node to its filesystem UUID."""
    if not os.path.isdir(BY_UUID):
        return None
    for uuid in os.listdir(BY_UUID):
        if os.path.realpath(os.path.join(BY_UUID, uuid)) == os.path.realpath(device):
            return uuid
    return None


def _media_root():
    """Where udisks mounts things: /media/<user>."""
    return os.path.join("/media", getpass.getuser())


def _device_for_name(name):
    """The block device that would mount at /media/<user>/<name>, or None.

    udisks names the mountpoint after the filesystem label, falling back to the
    UUID when there is no label, so the final path component is exactly what
    identifies the device.
    """
    for directory in (BY_LABEL, BY_UUID):
        candidate = os.path.join(directory, name)
        if os.path.exists(candidate):
            return os.path.realpath(candidate)
    return None


def _fstype_of_device(device):
    rc, out = _run(["lsblk", "-ndo", "FSTYPE", device])
    return out.strip() if rc == 0 and out.strip() else None


def scan_home_links(home=None):
    """First-level symlinks in $HOME left dangling by an unmounted disk.

    Returns entries shaped exactly like detect()'s, so they go through the same
    mount_entry() path. Only links pointing straight into udisks' own mount root
    are considered: udisks always mounts at /media/<user>/<label-or-uuid>, so a
    link aimed anywhere else would still dangle after a successful mount and
    reporting it as fixed would be a lie.

    Symlinks only. An empty directory sitting at a mountpoint looks identical to
    an empty directory, and carries no label to work back to a device from.
    """
    home = home or os.path.expanduser("~")
    media = _media_root()
    by_mountpoint = {}

    try:
        names = sorted(os.listdir(home))
    except OSError:
        return []

    for name in names:
        path = os.path.join(home, name)
        # os.path.exists follows the link, so this is "is a broken symlink".
        if not os.path.islink(path) or os.path.exists(path):
            continue
        target = os.path.normpath(os.path.realpath(path))
        if os.path.dirname(target) != media:
            continue
        device = _device_for_name(os.path.basename(target))
        if device is None:
            continue
        uuid = _uuid_of_device(device)
        if uuid is None:
            continue
        entry = by_mountpoint.setdefault(
            target,
            {
                "uuid": uuid,
                "mountpoint": target,
                "fstype": _fstype_of_device(device),
                "verify": [],
            },
        )
        entry["verify"].append(os.path.join("~", name))

    return list(by_mountpoint.values())


def is_mounted(entry):
    """True if the entry's mountpoint holds a mount of the expected device.

    Checked by UUID rather than by path alone: a stale empty directory at the
    mountpoint would otherwise look mounted.
    """
    mountpoint = os.path.expanduser(entry["mountpoint"])
    if not os.path.ismount(mountpoint):
        return False
    source, _ = _findmnt(mountpoint)
    if source is None:
        return False
    return _uuid_of_device(source) == entry["uuid"]


def verify_paths(entry):
    """Return the list of the entry's verify paths that do not resolve."""
    missing = []
    for p in entry.get("verify", []):
        real = os.path.realpath(os.path.expanduser(p))
        if not os.path.exists(real):
            missing.append(p)
    return missing


def mount_entry(entry, dry_run=False):
    """Mount one entry via udisksctl. Returns a human-readable status line.

    Raises MountError if the mount or the post-mount verification fails.
    """
    uuid = entry["uuid"]
    label = entry.get("mountpoint", uuid)

    if is_mounted(entry):
        missing = verify_paths(entry)
        if missing:
            raise MountError(
                f"{label} is mounted but these paths do not resolve: "
                + ", ".join(missing)
            )
        return f"already mounted   {label}"

    device = os.path.join(BY_UUID, uuid)
    if not os.path.exists(device):
        raise MountError(
            f"no device with UUID {uuid} is present (looked for {device}). "
            "Is the disk connected?"
        )

    if dry_run:
        return f"would mount       {label}\n                  udisksctl mount -b {device}"

    rc, out = _run(["udisksctl", "mount", "-b", device])
    if rc != 0 and "already mounted" not in out.lower():
        raise MountError(f"udisksctl failed for {label}: {out}")

    if not is_mounted(entry):
        raise MountError(
            f"udisksctl reported success but {label} is still not mounted "
            f"({out})"
        )

    missing = verify_paths(entry)
    if missing:
        raise MountError(
            f"{label} mounted but these paths still do not resolve: "
            + ", ".join(missing)
        )
    return f"mounted           {label}"


def ensure_all(entries, dry_run=False):
    """Ensure every entry is mounted and verified.

    Returns a list of status lines. Raises MountError on the first failure --
    a partial restore into dangling symlinks is worse than no restore.
    """
    report = []
    for entry in entries:
        report.append(mount_entry(entry, dry_run=dry_run))
    return report


def ensure_optional(entries, dry_run=False):
    """Best-effort mount. Returns (report_lines, failure_messages), never raises.

    The counterpart to ensure_all, for entries found by scanning $HOME rather
    than named by the session's own recorded paths. A disk that is simply not
    plugged in is an ordinary state here, not a reason to take a restore down --
    so every entry is attempted and the failures are reported, not thrown.
    """
    report, failures = [], []
    for entry in entries:
        try:
            report.append(mount_entry(entry, dry_run=dry_run))
        except MountError as e:
            failures.append(str(e))
    return report, failures


def detect(verify_paths_list=None):
    """Build config entries by following the paths a restore depends on.

    Resolves each path, finds the mount that contains it, and reads back that
    device's UUID. Paths on the root filesystem are skipped -- root is always
    mounted, so there is nothing for Stage 0 to do.
    """
    if verify_paths_list is None:
        verify_paths_list = DEFAULT_VERIFY_PATHS

    root_source, _ = _findmnt("/")
    by_mountpoint = {}

    for p in verify_paths_list:
        real = os.path.realpath(os.path.expanduser(p))
        if not os.path.exists(real):
            continue
        source, fstype = _findmnt(real)
        if source is None or source == root_source:
            continue
        rc, out = _run(["findmnt", "-n", "-o", "TARGET", "--target", real])
        if rc != 0 or not out:
            continue
        mountpoint = out.splitlines()[0].strip()
        uuid = _uuid_of_device(source)
        if uuid is None:
            continue
        entry = by_mountpoint.setdefault(
            mountpoint,
            {"uuid": uuid, "mountpoint": mountpoint, "fstype": fstype, "verify": []},
        )
        entry["verify"].append(p)

    return list(by_mountpoint.values())
