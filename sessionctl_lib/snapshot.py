"""Building, digesting and storing snapshots.

Snapshots are plain uncompressed JSON: they are tens of KB, the ring holds 20,
and being able to cat, grep and diff one is worth far more than the bytes.

The save timer fires every 30 seconds for as long as the machine is up,
so the steady state must cost nothing. That is what `digest` is for -- when the
restore-relevant state has not changed, nothing is written at all.
"""

import datetime
import hashlib
import json
import os

from . import paths

VERSION = 1
KEEP = 20


def _now_iso():
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


def terminal_capture_broken(terminals, hook_configured):
    """True when terminal windows are open but not one tab was captured.

    This is provably a broken capture rather than an empty session, because
    the two facts come from independent sources: window_count is read from
    gnome-terminal's own D-Bus tree, while the tabs come from the shell
    hook's state files. Windows but no tabs means the hook isn't reporting.

    Why it matters: that state used to be written straight over latest.json,
    silently replacing a real session with an empty one. Seen for real -- a
    terminal opened 7 seconds before the data partition mounted sourced a
    ~/.bashrc whose `[ -f ... ]` guard failed, so the hook never loaded for
    that shell, and the next periodic save recorded "no tabs" as the truth.
    The restore that followed had nothing to restore. (The underlying cause
    is fixed too -- bash-hook.sh is now a real copy, see setup.py -- but a
    save must never destroy a good snapshot just because capture failed,
    whatever the reason.)

    hook_configured guards the inverse mistake: if the ~/.bashrc line was
    never installed, nothing is expected to capture tabs, so an empty
    terminal set is simply correct and must not block saving forever.
    """
    if not hook_configured:
        return False
    if not terminals.get("window_count"):
        return False
    return not any(w.get("tabs") for w in terminals.get("windows", []))


def desktop_not_visible(layout, previous):
    """True when this run couldn't see the desktop but the last one could.

    layout.capture() needs a working X display; cli._capture_layout() turns a
    failure into an empty window list so the rest of the snapshot survives.
    That's right for the layout section alone, but it also means a save
    running with no display records "no windows anywhere" -- and terminal
    tabs vanish at the same time, because the shells' EXIT traps delete their
    state files as the session tears down. The result looks exactly like a
    desktop where the user closed everything.

    Seen for real, three seconds apart in the journal: a periodic save
    reporting "6 tabs, 5 windows placed", then the shutdown save writing
    "0 tabs, 0 windows placed" over latest.json. The next restore had nothing
    to bring back. The unit responsible is gone (see setup.OBSOLETE_UNITS),
    but the rule belongs here regardless: never let a save that could not
    look at the desktop overwrite one that could.

    Deliberately *not* keyed on tab count alone -- genuinely closing every
    terminal is a real state worth recording, and it comes with a working
    layout capture that proves the desktop was actually observed.
    """
    if previous is None:
        return False
    if (previous.get("layout") or {}).get("windows"):
        return not (layout or {}).get("windows")
    return False


def read_restored_boot():
    try:
        with open(paths.RESTORED_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def mark_restored(boot_id=None):
    """Record that the recorded session is live under this boot.

    Written by `restore` once it has replayed a snapshot, and by `adopt` when
    you want the current desktop to become the session of record without
    restoring anything.
    """
    boot_id = boot_id or paths.current_boot_id()
    if boot_id is None:
        return None
    paths.ensure_dirs()
    with open(paths.RESTORED_FILE, "w") as f:
        f.write(boot_id + "\n")
    return boot_id


def session_not_yet_restored(previous, boot_id, restored_boot):
    """True when latest.json belongs to an earlier boot we haven't restored.

    The failure this exists for: after a reboot, the save timer captures the
    freshly booted desktop -- accurately -- and promotes it to latest.json
    before you have had a chance to run `restore`. The session you actually
    wanted is then gone from latest, and restoring replays the blank slate
    instead. Measured once at 47 seconds from login to overwrite; at a 30s
    tick you have to beat the timer to the keyboard, which is not a design.

    Neither of the other two guards can catch this, because nothing is
    malfunctioning: there really is one terminal with one tab, and the layout
    really is visible. The only thing wrong is *which session* it describes,
    and the boot id is what distinguishes them.

    A previous snapshot with no boot_id predates this mechanism, so it can't
    be reasoned about and must not block saving.
    """
    if previous is None or boot_id is None:
        return False
    prev_boot = previous.get("boot_id")
    if not prev_boot:
        return False
    if prev_boot == boot_id:
        return False
    return restored_boot != boot_id


def build(terminals, mounts_state, vscode=None, chrome=None, firefox=None, layout=None):
    return {
        "version": VERSION,
        "ts": _now_iso(),
        "boot_id": paths.current_boot_id(),
        "degraded": mounts_state.get("degraded", False),
        "mounts": mounts_state.get("entries", []),
        "terminals": terminals,
        "vscode": vscode or {"windows": []},
        "chrome": chrome or {"profiles": []},
        "firefox": firefox or {"running": False, "profile": None},
        "layout": layout or {"workspace_count": 0, "windows": []},
    }


def _layout_sort_key(entry):
    """Order layout entries deterministically when a workspace may be None.

    `workspace` is None for any window mutter gave no workspace: a genuinely
    sticky one, or -- with workspaces-only-on-primary, which is the default
    -- every window on a non-primary monitor. Sorting the raw tuples
    compares that None against an int the moment two windows share a
    wm_class, and Python 3 refuses: "'<' not supported between instances of
    'int' and 'NoneType'". `save` then died on every single run. On this
    machine that went unnoticed for 25 hours, until a restore replayed a
    day-old snapshot and the session came back wrong.

    It stayed hidden until four monitors were attached because it needs two
    *same-class* windows whose workspaces differ in kind. With one monitor
    the only workspace-less window was the GNOME Shell overlay, whose class
    is unique, so the comparison never got past the first tuple element.

    None sorts after every int, and the key is otherwise element for element
    what tuple comparison already did, so a snapshot that used to sort fine
    still hashes to the same digest -- no spurious rewrite on upgrade.
    """
    wm_class, workspace, sticky, maximized, geometry = entry
    return (
        wm_class,
        workspace is None,
        workspace if workspace is not None else -1,
        sticky,
        maximized,
        geometry,
    )


def digest(snap):
    """Hash only what a restore actually consumes.

    Excluded on purpose: timestamps, PIDs, D-Bus screen paths, each source
    file's own (path, mtime, size) bookkeeping, and window titles -- all of
    which churn without changing what would be restored. A title changes on
    every browser tab or editor switch, and letting that drive the digest
    would defeat the whole write-avoidance mechanism (confirmed: this is
    also exactly why layout.py's own window-matching doesn't lean on titles
    for terminal windows either -- see layout.match_windows).

    Layout's per-window workspace/geometry/maximized/sticky ARE included,
    sorted for order-independence the same way vscode's window list is --
    Wnck's own enumeration order isn't guaranteed stable between two
    captures of an otherwise-unchanged desktop.
    """
    relevant = {
        "terminals": [
            [[t.get("cwd"), t.get("conda")] for t in w.get("tabs", [])]
            for w in snap.get("terminals", {}).get("windows", [])
        ],
        "mounts": sorted(
            m.get("mountpoint", "") for m in snap.get("mounts", [])
        ),
        "vscode": sorted(
            w.get("path", "") for w in snap.get("vscode", {}).get("windows", [])
        ),
        "chrome": sorted(
            (p.get("profile", ""), tuple(p.get("urls", [])))
            for p in snap.get("chrome", {}).get("profiles", [])
        ),
        "firefox": snap.get("firefox", {}).get("running", False),
        "layout": sorted(
            (
                (
                    w.get("wm_class", ""),
                    w.get("workspace"),
                    w.get("sticky", False),
                    w.get("maximized", False),
                    tuple(sorted((w.get("geometry") or {}).items())),
                )
                for w in snap.get("layout", {}).get("windows", [])
            ),
            key=_layout_sort_key,
        ),
    }
    canonical = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _write_json(path, data):
    """Write atomically, so a crash mid-write cannot truncate a good file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def load_latest():
    try:
        with open(paths.LATEST_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_heartbeat(dig):
    _write_json(
        paths.HEARTBEAT_FILE, {"digest": dig, "last_verified": _now_iso()}
    )


def load_heartbeat():
    try:
        with open(paths.HEARTBEAT_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def list_snapshots():
    """Snapshot file paths, newest first."""
    if not os.path.isdir(paths.SNAPSHOTS_DIR):
        return []
    files = [
        os.path.join(paths.SNAPSHOTS_DIR, n)
        for n in os.listdir(paths.SNAPSHOTS_DIR)
        if n.endswith(".json")
    ]
    return sorted(files, reverse=True)


def prune(keep=KEEP):
    """Drop the oldest snapshots beyond `keep`. Returns how many were removed."""
    removed = 0
    for path in list_snapshots()[keep:]:
        try:
            os.unlink(path)
            removed += 1
        except OSError:
            pass
    return removed


def store(snap):
    """Persist a snapshot and point latest.json at it.

    Returns the snapshot's id (its filename stem).
    """
    paths.ensure_dirs()
    # ts has only second resolution (_now_iso rounds off microseconds for
    # readability), so two real changes within the same second would collide
    # on filename and silently clobber each other. The digest is already
    # unique per distinct state, so a short slice of it disambiguates without
    # sacrificing the readable timestamp prefix.
    snap_id = snap["ts"].replace(":", "-") + "_" + snap["digest"][7:15]
    _write_json(os.path.join(paths.SNAPSHOTS_DIR, snap_id + ".json"), snap)
    _write_json(paths.LATEST_FILE, snap)
    write_heartbeat(snap["digest"])
    prune()
    return snap_id


def save(snap):
    """Store the snapshot only if the restore-relevant state changed.

    Returns (changed, snap_id_or_None, digest).
    """
    dig = digest(snap)
    snap["digest"] = dig

    previous = load_latest()
    if previous is not None and previous.get("digest") == dig:
        write_heartbeat(dig)
        return False, None, dig

    return True, store(snap), dig
