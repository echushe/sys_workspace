"""Filesystem locations used by sessionctl.

Everything lives under the XDG user directories so nothing needs root and
nothing lands in the source tree.
"""

import os


def _xdg(env_var, default):
    return os.path.expanduser(os.environ.get(env_var) or default)


CONFIG_DIR = os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), "sessionctl")
STATE_DIR = os.path.join(_xdg("XDG_STATE_HOME", "~/.local/state"), "sessionctl")

CONFIG_FILE = os.path.join(CONFIG_DIR, "config.yaml")

# Per-tab state written by bash-hook.sh, one file per shell PID.
SHELLS_DIR = os.path.join(STATE_DIR, "shells")

# Rotated snapshots, plus the pointer to the newest good one.
SNAPSHOTS_DIR = os.path.join(STATE_DIR, "snapshots")
LATEST_FILE = os.path.join(STATE_DIR, "latest.json")

# Written when a save found nothing changed, so `list` can still show liveness
# without rewriting a whole snapshot. See "Write avoidance" in the plan.
HEARTBEAT_FILE = os.path.join(STATE_DIR, "heartbeat.json")

# Holds the boot id under which the recorded session was last restored (or
# explicitly adopted). Until it matches the running boot, `save` will not
# overwrite latest.json -- a freshly booted desktop is not the session of
# record, it's the blank slate you are about to restore into. See
# snapshot.session_not_yet_restored.
RESTORED_FILE = os.path.join(STATE_DIR, "restored_boot")

BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"


def current_boot_id():
    """This boot's kernel-assigned UUID, or None if it can't be read.

    Stable for the life of a boot and different after every reboot, which is
    exactly the signal needed to tell "the session I saved before rebooting"
    apart from "the desktop that exists now".
    """
    try:
        with open(BOOT_ID_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def ensure_dirs():
    """Create the state directories. Safe to call repeatedly."""
    for d in (CONFIG_DIR, STATE_DIR, SHELLS_DIR, SNAPSHOTS_DIR):
        os.makedirs(d, exist_ok=True)
