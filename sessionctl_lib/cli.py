"""Command-line entry point."""

import argparse
import datetime
import json
import os
import shlex
import sys

from . import (collect, config, layout, mounts, paths, restore, setup,
               snapshot, warp)

RESTORE_KINDS = ("terminals", "warp", "vscode", "chrome", "layout")


def _mounts_state():
    """Current state of every required mount, and whether it is usable."""
    entries = config.mount_entries()
    degraded = False
    out = []
    for e in entries:
        mounted = mounts.is_mounted(e)
        missing = mounts.verify_paths(e) if mounted else list(e.get("verify", []))
        if not mounted or missing:
            degraded = True
        out.append(dict(e, mounted=mounted, missing=missing))
    return {"degraded": degraded, "entries": out}


def cmd_mount(args):
    entries = config.mount_entries()
    home_entries = [] if args.no_scan_home else mounts.scan_home_links()

    if not entries and not home_entries:
        print(
            "No mounts to check: nothing in the config, nothing detected, and no\n"
            "dangling symlinks in $HOME. Every path a restore needs resolves already.",
            file=sys.stderr,
        )
        return 0

    rc = 0
    try:
        for line in mounts.ensure_all(entries, dry_run=args.dry_run):
            print(line)
    except mounts.MountError as e:
        print(f"error: {e}", file=sys.stderr)
        rc = 1

    report, failures = mounts.ensure_optional(home_entries, dry_run=args.dry_run)
    for line in report:
        print(line)
    for f in failures:
        print(f"skipped: {f}", file=sys.stderr)

    return rc


def cmd_detect(args):
    entries = mounts.detect()
    if not entries:
        print("Nothing detected -- all checked paths are on the root filesystem.")
        return 0
    for e in entries:
        print(f"  uuid:       {e['uuid']}")
        print(f"  mountpoint: {e['mountpoint']}")
        print(f"  fstype:     {e['fstype']}")
        print(f"  verify:     {', '.join(e['verify'])}")
        print(f"  mounted:    {mounts.is_mounted(e)}")
        print()
    if args.write:
        cfg = config.load()
        cfg["mounts"] = entries
        config.save(cfg)
        print(f"written to {paths.CONFIG_FILE}")
    else:
        print(f"(re-run with --write to save this to {paths.CONFIG_FILE})")
    return 0


def _capture_layout(terminals=None, warp_state=None):
    """layout.capture(), degrading to empty rather than crashing `save`.

    Wnck needs a working X11 DISPLAY. That's guaranteed in an interactive
    terminal, but not guaranteed for the systemd user timer driving the
    periodic save (step 6) -- its environment isn't automatically the same
    as the graphical session's unless explicitly imported. A save that loses
    terminal/VSCode/Chrome capture because window-layout capture happened to
    fail would be a strictly worse trade than just capturing everything else
    and leaving layout empty for that one run.
    """
    try:
        return layout.capture(terminals, warp_state)
    except Exception as e:
        print(f"warning: layout capture failed ({e}), continuing without it", file=sys.stderr)
        return {"workspace_count": 0, "windows": []}


def cmd_save(args):
    state = _mounts_state()

    # A snapshot taken while the disk is missing would record an empty desktop.
    # Refusing to write is what stops the timer from grinding a good snapshot
    # away during the window between boot and the disk being mounted.
    if state["degraded"]:
        for e in state["entries"]:
            if not e["mounted"]:
                print(f"not mounted: {e['mountpoint']}", file=sys.stderr)
            elif e["missing"]:
                print(
                    f"unresolved:  {', '.join(e['missing'])}", file=sys.stderr
                )
        print("refusing to snapshot a degraded session", file=sys.stderr)
        return 0

    # Reused so collect_vscode/collect_chrome can skip re-scanning a source
    # file whose mtime and size have not changed since the last snapshot.
    previous = snapshot.load_latest() or {}

    # The recorded session belongs to an earlier boot and hasn't been restored
    # yet, so latest.json is the thing you are about to restore *from*.
    # Overwriting it with the freshly booted desktop -- which is exactly what
    # the timer would otherwise do within 30s of you mounting the disk -- is
    # how a real session gets lost. Unlike the two refusals above, nothing
    # here is malfunctioning; the capture is accurate, it just describes the
    # wrong session.
    boot_id = paths.current_boot_id()
    restored_boot = snapshot.read_restored_boot()
    if not args.force and snapshot.session_not_yet_restored(
        previous or None, boot_id, restored_boot
    ):
        prev_tabs = sum(
            len(w["tabs"]) for w in previous.get("terminals", {}).get("windows", [])
        )
        print(
            f"latest.json holds a session from a previous boot ({prev_tabs} "
            f"tabs, saved {previous.get('ts')}) that hasn't been restored yet.",
            file=sys.stderr,
        )
        print(
            "Not overwriting it with the current desktop. Run `sessionctl "
            "restore` to bring that session back, or `sessionctl adopt` to "
            "keep the desktop as it is now and resume saving.",
            file=sys.stderr,
        )
        return 0

    terminals = collect.collect_terminals(prune=not args.no_prune)

    # Same principle as the mount check above, one layer in: terminal windows
    # are open but the hook reported no tabs at all, so this snapshot would
    # record an empty session that isn't real. Refuse rather than overwrite --
    # and refuse *before* storing anything, not merely by declining to update
    # latest.json, because a snapshot written every 30 seconds would otherwise
    # fill the 20-entry ring and evict the good snapshot anyway.
    hook_configured, _ = setup.ensure_hook_installed(install=False)
    if snapshot.terminal_capture_broken(terminals, hook_configured):
        print(
            f"{terminals['window_count']} terminal window(s) open but no tabs "
            "captured -- the ~/.bashrc hook is installed but isn't reporting.",
            file=sys.stderr,
        )
        print(
            "Most likely those terminals were opened before the data "
            "partition was mounted, so the hook never loaded for them; they "
            "will never report. Open a new terminal tab and this resolves "
            "itself.",
            file=sys.stderr,
        )
        print("refusing to overwrite a good snapshot with an empty one",
              file=sys.stderr)
        return 0

    vscode = collect.collect_vscode(previous=previous.get("vscode"))
    chrome = collect.collect_chrome(previous=previous.get("chrome"))
    firefox = collect.collect_firefox()
    # Warp records its own windows/tabs/panes/cwds, so this is a database read
    # rather than a reconstruction. The shell hook still supplies the one thing
    # Warp cannot see -- each pane's conda env -- joined on the pane UUID.
    warp_state = warp.collect_warp(
        previous=previous.get("warp"),
        shells=collect.read_warp_shells(prune=not args.no_prune),
    )
    # Both collections are handed to layout capture so terminal and Warp
    # windows get a trustworthy signature -- from the shell hook rather than
    # from whatever their prompts happen to show (layout._hook_window_cwds),
    # and from Warp's own database rather than from a repeated title
    # (layout._warp_signatures).
    layout_state = _capture_layout(terminals, warp_state)

    # Third refusal, same family as the two above: this run could not see the
    # desktop at all while the previous one could, so whatever it captured is
    # a picture of a session tearing down (or of a broken DISPLAY), not of a
    # desktop the user emptied. Storing it would overwrite a real session.
    if snapshot.desktop_not_visible(layout_state, previous or None):
        print(
            "layout capture came back empty but the previous snapshot had "
            f"{len((previous.get('layout') or {}).get('windows', []))} "
            "windows -- this save cannot see the desktop.",
            file=sys.stderr,
        )
        print("refusing to overwrite a good snapshot with an unobserved one",
              file=sys.stderr)
        return 0

    snap = snapshot.build(terminals, state, vscode, chrome, firefox,
                          layout_state, warp_state)

    tab_count = sum(len(w["tabs"]) for w in terminals["windows"])
    if args.dry_run:
        snap["digest"] = snapshot.digest(snap)
        print(json.dumps(snap, indent=2, ensure_ascii=False))
        return 0

    vs_count = len(vscode["windows"])
    chrome_count = sum(len(p["urls"]) for p in chrome["profiles"])
    win_count = len(layout_state["windows"])
    warp_panes = warp.pane_count(warp_state)
    warp_windows = len(warp_state.get("windows", []))
    summary = (
        f"{tab_count} tabs, {warp_panes} warp panes in {warp_windows} windows, "
        f"{vs_count} vscode windows, "
        f"{chrome_count} chrome urls, {win_count} windows placed"
    )

    changed, snap_id, dig = snapshot.save(snap)
    if changed:
        print(f"saved {snap_id}  ({summary})")
    else:
        print(f"unchanged  ({summary})  {dig[:19]}...")
    return 0


def _load_snapshot(snapshot_id):
    if snapshot_id in (None, "latest"):
        return snapshot.load_latest()
    path = os.path.join(paths.SNAPSHOTS_DIR, snapshot_id + ".json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


STALE_AFTER = 3600  # seconds; a snapshot older than this is worth mentioning


def snapshot_age(snap, now=None):
    """Seconds between a snapshot's timestamp and now, or None if unreadable."""
    ts = (snap or {}).get("ts")
    if not ts:
        return None
    try:
        taken = datetime.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    now = now or datetime.datetime.now().astimezone()
    if taken.tzinfo is None:
        taken = taken.astimezone()
    return (now - taken).total_seconds()


def _format_age(seconds):
    if seconds >= 86400:
        return f"{seconds / 86400:.1f} days"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 60:.0f} minutes"


def _warn_if_stale(snap):
    """Say so when the snapshot about to be replayed is not recent.

    `save` runs every 30s, so a snapshot more than an hour old means saving
    stopped -- and the only thing that makes that visible is the moment you
    restore from it. It has happened: a TypeError in digest() killed every
    save for 25 hours, and the failure lived in the timer's journal where
    nobody was looking. The restore then replayed a day-old session and the
    windows came back wrong, which is a confusing way to learn that saving
    had stopped the day before.
    """
    age = snapshot_age(snap)
    if age is None or age < STALE_AFTER:
        return
    print(
        f"warning: this snapshot is {_format_age(age)} old (taken "
        f"{snap.get('ts')}).",
        file=sys.stderr,
    )
    print(
        "         Saving runs every 30s, so it has not been running since "
        "then.\n"
        "         Check `journalctl --user -u sessionctl-save.service -n 20` "
        "and `sessionctl list`.",
        file=sys.stderr,
    )


def cmd_restore(args):
    only = RESTORE_KINDS
    if args.only:
        only = [k.strip() for k in args.only.split(",") if k.strip()]
        bad = [k for k in only if k not in RESTORE_KINDS]
        if bad:
            print(
                f"unknown --only value(s): {', '.join(bad)} "
                f"(choose from {', '.join(RESTORE_KINDS)})",
                file=sys.stderr,
            )
            return 1

    # Stage 0 first, always -- restoring into a dangling ~/Projects symlink
    # opens VSCode on nothing and drops terminals into $HOME while looking
    # like it worked. --skip-mounts is for when you've already mounted things
    # yourself and don't want the PolicyKit prompt udisksctl can trigger.
    if not args.skip_mounts:
        entries = config.mount_entries()
        try:
            for line in mounts.ensure_all(entries, dry_run=args.dry_run):
                print(line)
        except mounts.MountError as e:
            print(f"error: {e}", file=sys.stderr)
            print("refusing to restore into unmounted storage", file=sys.stderr)
            return 1

    snap = _load_snapshot(args.snapshot)
    if snap is None:
        target = args.snapshot or "latest"
        print(f"no such snapshot: {target}", file=sys.stderr)
        return 1

    _warn_if_stale(snap)

    plan = []
    if "terminals" in only:
        for argv in restore.terminal_commands(snap.get("terminals", {})):
            plan.append(("terminal", argv))
    if "warp" in only:
        # warp.running() is read now, not taken from the snapshot: the snapshot
        # says whether Warp was up when the session was saved, which after a
        # reboot is nearly always true and would skip the launch entirely.
        for argv in restore.warp_commands(
            snap.get("warp", {}),
            running=warp.running(),
            force=args.warp_force,
        ):
            plan.append(("warp", argv))
    if "vscode" in only:
        for argv in restore.vscode_commands(snap.get("vscode", {})):
            plan.append(("vscode", argv))
    if "chrome" in only:
        for argv in restore.chrome_commands(
            snap.get("chrome", {}), use_recorded_urls=args.chrome_urls
        ):
            plan.append(("chrome", argv))

    have_layout = "layout" in only and snap.get("layout", {}).get("windows")
    if not plan and not have_layout:
        print("nothing to restore in this snapshot")
        return 0

    # Baseline of what was already on screen, taken before a single window is
    # launched -- this is what lets placement tell "the window restore just
    # created" from "a same-class window that was already open", which
    # matters most for the terminal you're running this command from. Best
    # effort: if Wnck can't enumerate (no X11), fall back to None, which is
    # exactly the pre-existing class+title matching behaviour.
    preexisting = None
    if "layout" in only and not args.dry_run:
        try:
            preexisting = layout.live_xids()
        except Exception as e:
            print(f"warning: could not read current windows ({e}), placement "
                  "will fall back to class+title matching", file=sys.stderr)

    for kind, argv in plan:
        printable = " ".join(shlex.quote(a) for a in argv)
        if args.dry_run:
            print(f"[{kind}] {printable}")
        else:
            print(f"[{kind}] launching: {printable}")
            restore.launch(argv)

    # Placement runs after every launch above, using the same snapshot's
    # layout section -- this is what actually puts windows (including ones
    # this run just launched) back on the workspace they were recorded on.
    # Skipped under --dry-run: it doesn't launch anything, so there'd be
    # nothing new to place, and place() moves currently-live windows, which
    # --dry-run's "launch nothing" contract should not do either.
    if "layout" in only and have_layout and args.dry_run:
        print(
            f"[layout] would place {len(snap['layout']['windows'])} windows "
            "(use `sessionctl place` to preview this on its own)"
        )
    if "layout" in only and not args.dry_run:
        windows = snap.get("layout", {}).get("windows", [])
        if windows:
            try:
                result = layout.place(windows, preexisting=preexisting)
            except Exception as e:
                print(f"warning: layout placement failed ({e})", file=sys.stderr)
            else:
                print(
                    f"[layout] placed {len(result['placed'])} of {len(windows)} windows"
                )

    # The recorded session is now live under this boot, so the periodic save
    # may resume updating latest.json. Until this point it deliberately
    # refuses -- see cmd_save and snapshot.session_not_yet_restored.
    if not args.dry_run:
        marked = snapshot.mark_restored()
        if marked:
            print("[save] periodic snapshots resumed for this boot")
    return 0


def cmd_adopt(args):
    """Claim the current desktop as the session of record.

    The escape hatch for the boot guard: if you rebooted and would rather
    keep working with what's on screen than restore the saved session, this
    tells `save` to stop protecting the old one. It is the only way out of
    the guard that doesn't involve restoring, so it's a real command rather
    than a flag buried in `save`.
    """
    previous = snapshot.load_latest()
    boot_id = paths.current_boot_id()
    if boot_id is None:
        print("cannot read this boot's id, nothing to adopt", file=sys.stderr)
        return 1
    if previous is not None and not args.yes:
        prev_tabs = sum(
            len(w["tabs"]) for w in previous.get("terminals", {}).get("windows", [])
        )
        print(
            f"latest.json holds {prev_tabs} tabs saved {previous.get('ts')}.\n"
            "Adopting means the next save overwrites it with the current "
            "desktop, and that session is then only reachable via "
            "`sessionctl list` / `restore --snapshot ID`.\n"
            "Re-run with --yes to confirm.",
            file=sys.stderr,
        )
        return 1
    snapshot.mark_restored(boot_id)
    print(f"adopted the current desktop for boot {boot_id}")
    print("periodic snapshots will resume updating latest.json")
    return 0


def cmd_place(args):
    """Re-apply only the workspace/geometry pass from a snapshot.

    Does not launch anything -- for testing the layout pass on its own, or
    for putting an already-open desktop back where a snapshot recorded it.
    """
    snap = _load_snapshot(args.snapshot)
    if snap is None:
        target = args.snapshot or "latest"
        print(f"no such snapshot: {target}", file=sys.stderr)
        return 1

    windows = snap.get("layout", {}).get("windows", [])
    if not windows:
        print("nothing recorded in this snapshot's layout")
        return 0

    try:
        result = layout.place(
            windows, timeout=args.timeout, static_workspaces=args.static_workspaces
        )
    except Exception as e:
        print(f"error: layout placement failed ({e})", file=sys.stderr)
        return 1

    print(f"placed {len(result['placed'])} of {len(windows)} windows")
    for rec in result["unmatched"]:
        print(f"  unmatched: {rec.get('wm_class')} {rec.get('title', '')[:60]!r}")
    return 0


def cmd_list(args):
    files = snapshot.list_snapshots()
    if not files:
        print("no snapshots yet")
    latest = snapshot.load_latest()
    latest_digest = latest.get("digest") if latest else None
    for path in files:
        try:
            with open(path) as f:
                snap = json.load(f)
        except (OSError, ValueError):
            continue
        tabs = sum(
            len(w.get("tabs", []))
            for w in snap.get("terminals", {}).get("windows", [])
        )
        vs_windows = len(snap.get("vscode", {}).get("windows", []))
        chrome_urls = sum(
            len(p.get("urls", []))
            for p in snap.get("chrome", {}).get("profiles", [])
        )
        win_count = len(snap.get("layout", {}).get("windows", []))
        mark = "*" if snap.get("digest") == latest_digest else " "
        print(
            f"{mark} {os.path.basename(path)[:-5]}  "
            f"{tabs:>3} tabs  {vs_windows:>2} vscode  {chrome_urls:>3} chrome urls  "
            f"{win_count:>2} windows"
        )

    # A paused save is otherwise invisible -- the refusal goes to the timer's
    # journal, not to a terminal -- and "my snapshots stopped updating" is
    # exactly the kind of silence this tool has been bitten by before.
    if snapshot.session_not_yet_restored(
        latest, paths.current_boot_id(), snapshot.read_restored_boot()
    ):
        print()
        print("NOTE: latest.json is from a previous boot and hasn't been "
              "restored, so periodic saves are not updating it.")
        print("      `sessionctl restore` to bring it back, or "
              "`sessionctl adopt --yes` to keep the current desktop.")

    hb = snapshot.load_heartbeat()
    if hb:
        print(f"\nlast verified live: {hb.get('last_verified')}")
    return 0


def cmd_show(args):
    if args.id in (None, "latest"):
        snap = snapshot.load_latest()
        if snap is None:
            print("no snapshots yet", file=sys.stderr)
            return 1
    else:
        path = os.path.join(paths.SNAPSHOTS_DIR, args.id + ".json")
        try:
            with open(path) as f:
                snap = json.load(f)
        except (OSError, ValueError):
            print(f"no such snapshot: {args.id}", file=sys.stderr)
            return 1

    if args.json:
        print(json.dumps(snap, indent=2, ensure_ascii=False))
        return 0

    print(f"taken:  {snap.get('ts')}")
    print(f"digest: {snap.get('digest')}")
    for w in snap.get("terminals", {}).get("windows", []):
        print(f"\nterminal window {w['index']}:")
        for t in w.get("tabs", []):
            env = f"({t['conda']}) " if t.get("conda") else ""
            print(f"  {env}{t.get('cwd')}")

    warp_windows = snap.get("warp", {}).get("windows", [])
    if warp_windows:
        state = "running" if snap.get("warp", {}).get("running") else "not running"
        print(f"\nwarp ({state}):")
        for w in warp_windows:
            active = w.get("active_tab_index")
            print(f"  window {w.get('id')} (active tab {active}):")
            for i, tab in enumerate(w.get("tabs") or []):
                panes = tab.get("panes") or []
                split = ""
                if len(panes) > 1:
                    split = " split-h" if tab.get("split_horizontal") else " split-v"
                title = f" {tab['custom_title']!r}" if tab.get("custom_title") else ""
                print(f"    tab {i}{title}{split}:")
                for pane in panes:
                    env = f"({pane['conda']}) " if pane.get("conda") else ""
                    mark = "*" if pane.get("focused") else " "
                    print(f"     {mark} {env}{pane.get('cwd')}")

    vs_windows = snap.get("vscode", {}).get("windows", [])
    if vs_windows:
        print("\nvscode:")
        for w in vs_windows:
            print(f"  {w.get('path')}")

    for p in snap.get("chrome", {}).get("profiles", []):
        urls = p.get("urls", [])
        print(f"\nchrome ({p.get('profile')}), {len(urls)} urls:")
        for u in urls[:10]:
            print(f"  {u}")
        if len(urls) > 10:
            print(f"  ... and {len(urls) - 10} more")

    ff = snap.get("firefox", {})
    if ff.get("running"):
        print(f"\nfirefox: running (profile: {ff.get('profile')})")

    layout_windows = snap.get("layout", {}).get("windows", [])
    if layout_windows:
        wc = snap.get("layout", {}).get("workspace_count")
        print(f"\nlayout ({wc} workspaces):")
        for w in layout_windows:
            ws = "sticky" if w.get("sticky") else f"ws{w.get('workspace')}"
            mx = " max" if w.get("maximized") else ""
            print(f"  [{ws}{mx}] {w.get('wm_class')}: {w.get('title', '')[:60]!r}")

    return 0


def cmd_hook(args):
    already, message = setup.ensure_hook_installed(install=args.install)
    print(message)
    return 0


def cmd_setup(args):
    try:
        for line in setup.setup(dry_run=args.dry_run, install_hook=args.install_hook):
            print(line)
    except setup.SetupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_uninstall(args):
    if not args.yes and not args.dry_run:
        print(
            "This disables and removes sessionctl's systemd units, the "
            "~/.local/bin/sessionctl symlink, and the ~/.bashrc hook line.\n"
            "Re-run with --yes to actually do it, or --dry-run to preview.",
            file=sys.stderr,
        )
        return 1
    if args.dry_run:
        print("(uninstall has no --dry-run preview beyond this notice -- it is "
              "a small set of best-effort removals, each already safe to "
              "re-run: symlinks are only removed if they still point where "
              "setup put them, the deployed lib copy is only removed if it's "
              "sessionctl's own deploy directory, and disabling an "
              "already-disabled unit is a no-op)")
        return 0
    for line in setup.uninstall(purge=args.purge):
        print(line)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="sessionctl",
        description="Save and restore the desktop session across reboots.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    m = sub.add_parser("mount", help="ensure required storage is mounted (Stage 0)")
    m.add_argument("--dry-run", action="store_true", help="print, do not mount")
    m.add_argument(
        "--no-scan-home", action="store_true",
        help="only mount the configured set; skip $HOME's dangling symlinks",
    )
    m.set_defaults(func=cmd_mount)

    d = sub.add_parser(
        "detect",
        help="work out the mount config by following the paths a restore needs",
    )
    d.add_argument("--write", action="store_true", help="save to the config file")
    d.set_defaults(func=cmd_detect)

    s = sub.add_parser("save", help="snapshot the session if it changed")
    s.add_argument("--dry-run", action="store_true", help="print, do not store")
    s.add_argument(
        "--no-prune",
        action="store_true",
        help="keep state files of tabs that have exited",
    )
    s.add_argument(
        "--force", action="store_true",
        help="save even if the recorded session is from an unrestored boot "
             "(prefer `sessionctl adopt`, which says so explicitly)",
    )
    s.set_defaults(func=cmd_save)

    ad = sub.add_parser(
        "adopt",
        help="keep the current desktop instead of restoring the saved session",
    )
    ad.add_argument("--yes", action="store_true", help="skip the confirmation")
    ad.set_defaults(func=cmd_adopt)

    r = sub.add_parser("restore", help="replay a snapshot")
    r.add_argument(
        "--dry-run", action="store_true",
        help="print the exact commands, launch nothing",
    )
    r.add_argument(
        "--snapshot", metavar="ID", default=None,
        help="restore this snapshot instead of the latest",
    )
    r.add_argument(
        "--only", metavar="KINDS", default=None,
        help=f"comma-separated subset of {','.join(RESTORE_KINDS)}",
    )
    r.add_argument(
        "--skip-mounts", action="store_true",
        help="skip Stage 0 (you've already mounted things yourself)",
    )
    r.add_argument(
        "--warp-force",
        action="store_true",
        help="launch Warp even if it is already running (it restores its own "
             "session on startup, so this normally does nothing useful)",
    )
    r.add_argument(
        "--chrome-urls", action="store_true",
        help="open Chrome's recorded URLs explicitly instead of relying on "
             "its own restore_on_startup",
    )
    r.set_defaults(func=cmd_restore)

    pl = sub.add_parser(
        "place", help="re-apply a snapshot's workspace/geometry, launching nothing"
    )
    pl.add_argument(
        "--snapshot", metavar="ID", default=None,
        help="use this snapshot instead of the latest",
    )
    pl.add_argument(
        "--timeout", type=float, default=layout.DEFAULT_TIMEOUT,
        help=f"seconds to wait for windows to appear (default {layout.DEFAULT_TIMEOUT})",
    )
    pl.add_argument(
        "--static-workspaces", action="store_true",
        help="use raw recorded workspace indices instead of compacting gaps "
             "(only if you've set dynamic-workspaces=false yourself)",
    )
    pl.set_defaults(func=cmd_place)

    ls = sub.add_parser("list", help="list stored snapshots")
    ls.set_defaults(func=cmd_list)

    sh = sub.add_parser("show", help="show a snapshot (default: latest)")
    sh.add_argument("id", nargs="?", help="snapshot id, or 'latest'")
    sh.add_argument("--json", action="store_true", help="print raw JSON")
    sh.set_defaults(func=cmd_show)

    h = sub.add_parser("hook", help="show or install the ~/.bashrc line")
    h.add_argument("--install", action="store_true", help="append it to ~/.bashrc")
    h.set_defaults(func=cmd_hook)

    su = sub.add_parser(
        "setup",
        help="symlink deployment, systemd units, Chrome's restore_on_startup",
    )
    su.add_argument("--dry-run", action="store_true", help="print, do nothing")
    su.add_argument(
        "--install-hook", action="store_true",
        help="also append the ~/.bashrc line (not done by default -- see "
             "`sessionctl hook`)",
    )
    su.set_defaults(func=cmd_setup)

    un = sub.add_parser("uninstall", help="reverse `setup`")
    un.add_argument(
        "--yes", action="store_true",
        help="actually do it (refuses otherwise, to avoid an accidental run)",
    )
    un.add_argument("--dry-run", action="store_true", help="preview, do nothing")
    un.add_argument(
        "--purge", action="store_true",
        help="also delete state and config (snapshots, mount config)",
    )
    un.set_defaults(func=cmd_uninstall)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    paths.ensure_dirs()
    return args.func(args)
