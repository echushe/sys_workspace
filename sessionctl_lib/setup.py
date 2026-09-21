"""sessionctl setup / uninstall: package deployment, systemd units, and
Chrome's restore_on_startup preference.

The systemd unit files and the whole sessionctl_lib/ package (plus its entry
script) are deployed as real files under ~/.local/share/sessionctl/lib/, not
symlinked into the repo -- ~/.local/bin/sessionctl then symlinks to *that*
deployed copy, which is safe because both ends of that symlink live on the
always-mounted home partition.

bash-hook.sh is deployed as a real copy for the same reason, though it took a
fourth reboot to see it. It was the last symlink into the repo, kept on the
argument that it already degraded gracefully -- the ~/.bashrc line guards with
`[ -f ... ]`, so a dangling symlink "just silently no-ops rather than
erroring". That was true and precisely the problem: on a cold boot a terminal
opened before the data partition mounts (measured at 7 seconds early, once)
sources a ~/.bashrc whose guard fails, so the hook never loads *and never
loads again for the life of that shell*. It captures nothing, silently,
forever -- and the periodic save then records an empty terminal set as the
session's current truth. "Degrades gracefully" was the wrong frame: failing
silently is worse here than failing loudly, because nothing downstream can
tell the difference between "no tabs captured" and "no tabs open".

Why the package itself is copied, found the hard way across three real
reboots -- see _ensure_content's and _ensure_package_copy's docstrings for
the two bugs this shape fixes, and deploy_package's docstring for why a
straight symlink into the repo (the original approach, used through step 6)
could never fully solve the second one no matter how it was written: the
repo lives on the very data partition sessionctl exists to manage, so
`sessionctl mount` -- whose entire job is mounting that partition -- could
never run at all while it was unmounted, if sessionctl's own code lived
there too.

The cost of a real copy is the one this project has avoided everywhere else:
re-running `setup` from the repo (not from the deployed `~/.local/bin/
sessionctl`) is now required to propagate a code edit to the deployed copy.
Accepted here because the project is past its heaviest active-development
churn, and because the alternative -- sessionctl categorically unable to
mount its own home partition -- is worse.
"""

import json
import os
import subprocess

from . import collect, warp

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSIONCTL_BIN = os.path.join(REPO_ROOT, "sessionctl")
SESSIONCTL_LIB_SRC = os.path.join(REPO_ROOT, "sessionctl_lib")
BASH_HOOK_SRC = os.path.join(REPO_ROOT, "bash-hook.sh")
SYSTEMD_UNIT_DIR = os.path.join(REPO_ROOT, "systemd")

# Real files, always reachable regardless of the repo's own partition --
# see the module docstring. ~/.local/bin/sessionctl symlinks to the entry
# script deployed here, not to the repo.
LIB_DEPLOY_DIR = os.path.expanduser("~/.local/share/sessionctl/lib")
DEPLOYED_SESSIONCTL_BIN = os.path.join(LIB_DEPLOY_DIR, "sessionctl")
DEPLOYED_LIB_DIR = os.path.join(LIB_DEPLOY_DIR, "sessionctl_lib")

LOCAL_BIN_LINK = os.path.expanduser("~/.local/bin/sessionctl")
HOOK_LINK = os.path.expanduser("~/.local/share/sessionctl/bash-hook.sh")
SYSTEMD_USER_DIR = os.path.expanduser("~/.config/systemd/user")

UNIT_NAMES = (
    "sessionctl-save.service",
    "sessionctl-save.timer",
)
# Only the timer is ever (en/dis)abled directly -- sessionctl-save.service has
# no [Install] section (it's Type=oneshot, triggered by the timer, not
# something to enable on its own).
ENABLE_UNITS = ("sessionctl-save.timer",)

# Shipped and enabled until it was found to be actively destructive, so
# `setup` has to actively tear down the copy an older install left behind --
# just dropping it from UNIT_NAMES would leave an enabled unit running
# forever with no source file to explain it.
#
# sessionctl-logout.service ran `save` from ExecStop at shutdown, intending a
# final exact snapshot. It cannot do that: by the time ExecStop runs, every
# shell has exited and its EXIT trap has already deleted the per-tab state
# file, and X is gone too. Caught live with three seconds between the two log
# lines -- a periodic save reporting "6 tabs, 5 windows placed" at 20:17:12,
# and the logout save writing "0 tabs, 0 windows placed" over latest.json at
# 20:17:15. The next restore then had a single empty session to replay.
#
# It did no damage while it was crashing on the dead X display; fixing that
# crash (see layout._screen) is what turned a broken unit into a harmful one.
# The 30s timer already bounds staleness far more tightly than this ever
# added, so it is removed rather than patched.
OBSOLETE_UNITS = ("sessionctl-logout.service",)

HOOK_MARKER = "# sessionctl session capture"
HOOK_LINE = (
    f"{HOOK_MARKER}\n"
    f'[ -f "$HOME/.local/share/sessionctl/bash-hook.sh" ] '
    f'&& . "$HOME/.local/share/sessionctl/bash-hook.sh"\n'
)

BASHRC = os.path.expanduser("~/.bashrc")


class SetupError(Exception):
    """A setup/uninstall step could not proceed safely."""


def _run(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return p.returncode, (p.stdout + p.stderr).strip()


def _ensure_symlink(link_path, target_path, dry_run=False):
    """Idempotently point link_path at target_path via a symlink.

    Refuses to touch link_path if it exists and is not a symlink -- it could
    be a real file or directory unrelated to sessionctl, and this must never
    silently overwrite something it didn't create.
    """
    target_path = os.path.abspath(target_path)

    if os.path.islink(link_path) and os.path.realpath(link_path) == os.path.realpath(
        target_path
    ):
        return f"already linked   {link_path}"

    if os.path.exists(link_path) and not os.path.islink(link_path):
        raise SetupError(
            f"{link_path} already exists and is not a symlink -- "
            "refusing to overwrite it"
        )

    verb = "relink" if os.path.islink(link_path) else "link"
    if dry_run:
        return f"would {verb}   {link_path} -> {target_path}"

    if os.path.islink(link_path):
        os.unlink(link_path)
    os.makedirs(os.path.dirname(link_path), exist_ok=True)
    os.symlink(target_path, link_path)
    return f"{verb}ed          {link_path} -> {target_path}"


def _remove_symlink(link_path, expected_target):
    """Remove link_path only if it's a symlink pointing at expected_target --
    never remove a file setup.py didn't create.
    """
    if not os.path.islink(link_path):
        return f"not our symlink, left alone: {link_path}"
    if os.path.realpath(link_path) != os.path.realpath(expected_target):
        return f"points elsewhere, left alone: {link_path}"
    os.unlink(link_path)
    return f"removed           {link_path}"


def ensure_hook_installed(install=False):
    """Ensure the ~/.bashrc line that sources bash-hook.sh is present.

    Returns (already_installed, message). With install=False this only
    reports what would be needed -- it never edits ~/.bashrc on its own,
    since that's a live change to shell startup held for explicit
    confirmation (see the plan and README).
    """
    try:
        with open(BASHRC) as f:
            existing = f.read()
    except OSError:
        existing = ""

    if HOOK_MARKER in existing:
        return True, f"already installed in {BASHRC}"

    if not install:
        return False, (
            "Add this to the END of ~/.bashrc (after conda's init block):\n\n"
            f"{HOOK_LINE}\n"
            "Run `sessionctl hook --install` (or `sessionctl setup --install-hook`) "
            "to append it automatically."
        )

    with open(BASHRC, "a") as f:
        f.write("\n" + HOOK_LINE)
    return False, f"appended to {BASHRC} (open a new terminal tab for it to take effect)"


def _remove_hook():
    try:
        with open(BASHRC) as f:
            lines = f.readlines()
    except OSError:
        return "no ~/.bashrc to edit"

    out = []
    skip_next = False
    removed = False
    for line in lines:
        if skip_next:
            skip_next = False
            removed = True
            continue
        if line.rstrip("\n") == HOOK_MARKER:
            skip_next = True
            removed = True
            continue
        out.append(line)

    if not removed:
        return "hook line not present in ~/.bashrc"

    with open(BASHRC, "w") as f:
        f.writelines(out)
    return f"removed hook line from {BASHRC}"


def _systemctl(*args):
    return _run(["systemctl", "--user", *args])


def _ensure_content(dst, content, dry_run=False, executable=False, verb=("written", "write")):
    """Idempotently write `content` (bytes) to dst, rewriting only when it
    differs from what's already there. The base primitive: _ensure_copy
    (content read from a source file) and _ensure_package_copy build on this.

    Deliberately a real file, not a symlink -- the exception to the
    symlink-not-copy convention used for bash-hook.sh, and for a specific
    reason confirmed live, across three real reboots:

    1. systemd --user reads its unit files at manager-startup time
       (login), while the data partition holding a symlink's target isn't
       mounted yet -- confirmed ~76s later on this machine, triggered by a
       manual mount, not anything automatic. A symlinked unit file is
       unreadable during that window, so systemd fails to load it
       ("Failed to open ...: No such file or directory") and marks it
       not-found for the rest of the session, permanently -- systemd does
       not retry loading a failed unit file later on its own, and nothing
       in this chain runs daemon-reload once the disk shows up. This is
       what sessionctl-save.timer and sessionctl-logout.service are
       copied against.

    2. Fixing (1) surfaced a second instance one layer deeper, also live,
       also on a real reboot: ExecStart=%h/.local/bin/sessionctl was still
       a symlink straight into the repo. With the unit files now loading
       correctly, the timer fired on schedule three times before the disk
       was mounted -- and each time, systemd's EXEC step failed outright
       ("Failed to locate executable ...", status=203/EXEC) before
       sessionctl's own Stage 0 degradation logic (which handles an
       unmounted disk correctly) ever got a chance to run, because the
       process never started. Patched with a shell wrapper that checked
       reachability before exec'ing through -- which worked, but only ever
       treated the symptom.

    3. The wrapper's real problem: it could never make `sessionctl mount`
       itself work from a cold boot, because mount's entire job is mounting
       the very partition its own code lived on -- no amount of graceful
       failure changes that Python never got to run at all. Fixed properly
       by deploying the whole sessionctl_lib/ package (see
       _ensure_package_copy) to an always-reachable location instead of
       patching around a symlink into the repo.

    These small static files (the unit files; the package copy is handled
    separately) re-sync by re-running `setup` after an edit, rather than
    always reflecting the repo instantly -- a real but minor cost, not a
    meaningful regression of the symlink policy's purpose.
    """
    dst_content = None
    if os.path.isfile(dst) and not os.path.islink(dst):
        with open(dst, "rb") as f:
            dst_content = f.read()

    if dst_content == content:
        return f"up to date        {dst}"

    verb_past, verb_present = verb
    if dry_run:
        action = "update" if os.path.exists(dst) else verb_present
        return f"would {action}   {dst}"

    if os.path.islink(dst) or os.path.exists(dst):
        os.unlink(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(content)
    if executable:
        os.chmod(dst, 0o755)
    return f"{verb_past}            {dst}"


def _ensure_copy(dst, src, dry_run=False, executable=False):
    """_ensure_content, reading the desired content from a source file."""
    with open(src, "rb") as f:
        src_content = f.read()
    return _ensure_content(
        dst, src_content, dry_run=dry_run, executable=executable,
        verb=("copied", "copy"),
    )


def _remove_copy(dst):
    if os.path.islink(dst) or os.path.isfile(dst):
        os.unlink(dst)
        return f"removed           {dst}"
    return f"not present       {dst}"


def _ensure_package_copy(dst_dir, src_dir, dry_run=False):
    """Idempotently mirror src_dir's .py files into dst_dir: re-copies files
    whose content changed (via _ensure_copy), and removes any deployed .py
    file no longer present in the source, so a renamed or deleted module
    doesn't linger as stale dead code in the deployed copy forever.

    Returns a list of report lines. Collapsed to a single "up to date" line
    when nothing needed to change -- the common case on every re-run once
    the code has stabilized, and one line per module (currently
    sessionctl_lib has nine) would otherwise be pure noise for it.
    """
    src_files = sorted(f for f in os.listdir(src_dir) if f.endswith(".py"))
    changes = []
    for name in src_files:
        msg = _ensure_copy(
            os.path.join(dst_dir, name), os.path.join(src_dir, name), dry_run=dry_run
        )
        if "up to date" not in msg:
            changes.append(msg)

    if os.path.isdir(dst_dir):
        deployed = {f for f in os.listdir(dst_dir) if f.endswith(".py")}
        for name in sorted(deployed - set(src_files)):
            path = os.path.join(dst_dir, name)
            if dry_run:
                changes.append(f"would remove stale {path}")
            else:
                os.unlink(path)
                changes.append(f"removed stale     {path}")

    if not changes:
        return [f"up to date        {dst_dir}/ ({len(src_files)} files)"]
    return changes


def deploy_units(dry_run=False):
    report = []
    for name in UNIT_NAMES:
        src = os.path.join(SYSTEMD_UNIT_DIR, name)
        dst = os.path.join(SYSTEMD_USER_DIR, name)
        report.append(_ensure_copy(dst, src, dry_run=dry_run))
    report.extend(purge_obsolete_units(dry_run=dry_run))
    return report


def purge_obsolete_units(dry_run=False):
    """Disable and delete units this version no longer ships.

    Necessary because systemd keeps running what's already installed and
    enabled: dropping a unit from UNIT_NAMES only stops re-deploying it, it
    doesn't stop the copy already in ~/.config/systemd/user/. For
    sessionctl-logout.service that would mean the unit that overwrites
    latest.json with an empty session at every shutdown keeps doing so
    indefinitely -- see OBSOLETE_UNITS.
    """
    report = []
    for name in OBSOLETE_UNITS:
        dst = os.path.join(SYSTEMD_USER_DIR, name)
        if not os.path.exists(dst) and not os.path.islink(dst):
            continue
        if dry_run:
            report.append(f"would disable+remove   {dst}  (obsolete)")
            continue
        rc, out = _systemctl("disable", "--now", name)
        report.append(
            f"disable --now {name}: {'ok' if rc == 0 else out or 'ok (not enabled)'}"
        )
        report.append(_remove_copy(dst) + "  (obsolete)")
    return report


def enable_units(dry_run=False):
    report = []
    if dry_run:
        report.append("would run: systemctl --user daemon-reload")
        report.append(f"would run: systemctl --user enable --now {' '.join(ENABLE_UNITS)}")
        return report

    rc, out = _systemctl("daemon-reload")
    report.append(f"daemon-reload: {'ok' if rc == 0 else 'FAILED: ' + out}")
    rc, out = _systemctl("enable", "--now", *ENABLE_UNITS)
    report.append(f"enable --now {' '.join(ENABLE_UNITS)}: {'ok' if rc == 0 else 'FAILED: ' + out}")
    return report


def disable_units():
    """Best-effort disable -- tolerates units that were never enabled."""
    report = []
    rc, out = _systemctl("disable", "--now", *(ENABLE_UNITS + OBSOLETE_UNITS))
    report.append(f"disable --now: {'ok' if rc == 0 else out or 'ok (already disabled)'}")
    return report


def remove_units():
    report = []
    # OBSOLETE_UNITS too: uninstalling an install that predates their removal
    # must still clean them up.
    for name in UNIT_NAMES + OBSOLETE_UNITS:
        dst = os.path.join(SYSTEMD_USER_DIR, name)
        report.append(_remove_copy(dst))
    rc, out = _systemctl("daemon-reload")
    report.append(f"daemon-reload: {'ok' if rc == 0 else 'FAILED: ' + out}")
    return report


def chrome_running():
    rc, _ = _run(["pgrep", "-x", "chrome"])
    return rc == 0


def set_chrome_restore_on_startup(dry_run=False):
    """Set restore_on_startup=1 ("continue where you left off") for each
    Chrome profile that has a Preferences file.

    Chrome rewrites Preferences from its in-memory state when it exits, so
    editing it while Chrome is running would silently lose the change the
    next time Chrome closes -- confirmed as a real risk worth guarding
    against (not just a theoretical one) by checking the exact key this
    needs (session.restore_on_startup, absent entirely from a fresh profile,
    not just unset) against this machine's real Preferences file before
    writing any code, and testing the write against a scratch copy rather
    than live. So this refuses outright while Chrome is running.
    """
    if chrome_running():
        return [
            "Chrome is running -- refusing to edit Preferences under it "
            "(it rewrites the file from memory on exit, silently discarding "
            "changes made while open).",
            "Set it yourself instead: Settings -> On startup -> "
            '"Continue where you left off". Or close Chrome and re-run.',
        ]

    report = []
    for name in collect._chrome_profiles():
        prefs_path = os.path.join(collect.CHROME_CONFIG_DIR, name, "Preferences")
        if not os.path.isfile(prefs_path):
            continue
        try:
            with open(prefs_path) as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            report.append(f"{name}: could not read Preferences ({e})")
            continue

        current = data.get("session", {}).get("restore_on_startup")
        if current == 1:
            report.append(f"{name}: already set to restore on startup")
            continue

        if dry_run:
            report.append(f"{name}: would set restore_on_startup=1 (was {current!r})")
            continue

        data.setdefault("session", {})["restore_on_startup"] = 1
        with open(prefs_path, "w") as f:
            json.dump(data, f)
        report.append(f"{name}: set restore_on_startup=1 (was {current!r})")

    return report


def warp_report():
    """What Warp's own settings mean for restore. Reports; never writes.

    Deliberately not the Chrome treatment. set_chrome_restore_on_startup
    edits Chrome's Preferences because Chrome ships that setting *off* and a
    restore genuinely depends on turning it on. Warp's equivalent,
    general.restore_session, is documented in Warp's own settings schema as
    defaulting to **true**, and on this machine it is not in settings.toml at
    all -- so it is already doing the right thing and there is nothing to
    write. Writing anyway would mean hand-editing a live TOML file (Python
    3.10 has no tomllib, and /usr/bin/python3 is deliberately what this runs
    under) to set a value that is already in force, and would risk mangling a
    config file to no benefit. So this only speaks up when the setting has
    actually been turned off, and then tells you where to turn it back on
    rather than doing it for you.
    """
    report = []
    if not os.path.exists(warp.DB_PATH):
        return report

    enabled = warp.restore_session_enabled()
    if enabled is None:
        report.append(
            f"warp: could not read {warp.SETTINGS_PATH} -- cannot tell whether "
            "session restore is on"
        )
    elif enabled:
        report.append("warp: session restore is on (Warp reopens its own windows)")
    else:
        report.append(
            "warp: session restore is OFF in settings.toml -- Warp will start "
            "empty and `sessionctl restore` cannot rebuild its panes (it has "
            "no API to). Turn it back on: Settings -> Session -> "
            '"Restore previous session", or set restore_session = true under '
            "[general]."
        )

    ok, message = warp.hook_reaches_panes()
    report.append(f"warp hook: {message}" if ok
                  else f"warp hook: WARNING -- {message}")
    return report


def setup(dry_run=False, install_hook=False):
    """Run every setup step. Returns a flat list of report lines."""
    report = []

    if os.path.realpath(REPO_ROOT) == os.path.realpath(LIB_DEPLOY_DIR):
        # This setup.py is itself running from the deployed copy, not the
        # repo -- REPO_ROOT was computed from *this* module's own location,
        # so every "source" path below now points at the deployed copy too.
        # The steps below will still run correctly (they'll just re-sync the
        # deployed copy onto itself, a harmless no-op), but a code edit made
        # in the repo will NOT be picked up this way.
        report.append(
            "note: running from the deployed copy, not the repo -- this "
            "will not pick up repo edits. Re-run via the repo's own "
            "./sessionctl setup to apply a code change."
        )

    report.append(_ensure_copy(
        DEPLOYED_SESSIONCTL_BIN, SESSIONCTL_BIN, dry_run=dry_run, executable=True,
    ))
    report.extend(_ensure_package_copy(DEPLOYED_LIB_DIR, SESSIONCTL_LIB_SRC, dry_run=dry_run))
    report.append(_ensure_symlink(LOCAL_BIN_LINK, DEPLOYED_SESSIONCTL_BIN, dry_run=dry_run))
    report.append(_ensure_copy(HOOK_LINK, BASH_HOOK_SRC, dry_run=dry_run))

    already, msg = ensure_hook_installed(install=install_hook and not dry_run)
    if dry_run and not already:
        report.append("bashrc hook: not installed -- run with --install-hook, or "
                       "`sessionctl hook --install` separately")
    else:
        report.append(f"bashrc hook: {msg}")

    report.extend(deploy_units(dry_run=dry_run))
    report.extend(enable_units(dry_run=dry_run))
    report.extend(set_chrome_restore_on_startup(dry_run=dry_run))
    report.extend(warp_report())

    local_bin = os.path.expanduser("~/.local/bin")
    if local_bin not in os.environ.get("PATH", "").split(os.pathsep):
        # Checked on this machine, not assumed: ~/.profile does add this
        # (guarded on the directory already existing, which it may not have
        # before this run), but gnome-terminal's default profile here has
        # login-shell=false, so a plain new tab sources only ~/.bashrc, which
        # does not add it either. A new tab opened after this run will not
        # pick it up on its own.
        report.append(
            f"note: {local_bin} is not on PATH, and a new gnome-terminal tab "
            "here won't add it either (its default profile isn't a login "
            "shell, so ~/.profile is never read). Use the full path "
            f"({local_bin}/sessionctl) or add it to ~/.bashrc yourself if "
            "you want the bare `sessionctl` command."
        )

    return report


def _remove_deployed_lib():
    """Remove the deployed sessionctl_lib/ copy and its entry script.

    Unconditional, not gated behind --purge: unlike snapshots and mount
    config, a deployed code copy has no user data value once uninstalled --
    leaving it behind would just be an orphaned, silently stale copy of this
    package sitting on disk.

    Safe even when this function is itself executing from the very files it
    deletes (the normal case for `~/.local/bin/sessionctl uninstall`):
    Python already holds the imported modules as compiled objects in memory,
    and unlinking a file that's currently open/mapped on Linux only removes
    the directory entry -- the inode stays valid until every reference to it
    (including this running process's) is gone.
    """
    report = []
    if os.path.isfile(DEPLOYED_SESSIONCTL_BIN):
        os.unlink(DEPLOYED_SESSIONCTL_BIN)
        report.append(f"removed           {DEPLOYED_SESSIONCTL_BIN}")
    if os.path.isdir(DEPLOYED_LIB_DIR):
        import shutil

        shutil.rmtree(DEPLOYED_LIB_DIR)
        report.append(f"removed           {DEPLOYED_LIB_DIR}")
    return report


def uninstall(purge=False):
    """Reverse setup. Returns a flat list of report lines. Does not touch
    Chrome's restore_on_startup preference -- there's nothing unsafe about
    leaving it on, and the plan does not call for reverting it.
    """
    report = []
    report.extend(disable_units())
    report.extend(remove_units())
    report.append(_remove_symlink(LOCAL_BIN_LINK, DEPLOYED_SESSIONCTL_BIN))
    report.extend(_remove_deployed_lib())
    # _remove_copy, not _remove_symlink: this is a real file since the
    # cold-boot fix (see the module docstring). It still handles the
    # pre-fix symlink, so uninstalling an older deployment works too.
    report.append(_remove_copy(HOOK_LINK))
    report.append(_remove_hook())

    if purge:
        from . import paths

        for d in (paths.STATE_DIR, paths.CONFIG_DIR):
            if os.path.isdir(d):
                import shutil

                shutil.rmtree(d)
                report.append(f"purged            {d}")
    else:
        report.append("state and config left in place (use --purge to delete them)")

    return report
