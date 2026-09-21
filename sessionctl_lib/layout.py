"""Workspace layout: capture per-window desktop placement via Wnck, and
replay it after a restore.

Driven entirely through libwnck3, importable from the system Python with
nothing to install (see the plan's Verified environment facts). X11-only --
Wnck cannot enumerate or move windows under Wayland.
"""

import difflib
import os
import time

import gi

gi.require_version("Wnck", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Wnck  # noqa: E402

from . import collect  # noqa: E402

DEFAULT_TIMEOUT = 45.0
POLL_INTERVAL = 0.3

# Give up early once nothing has been placed for this long. The hard timeout
# has to be generous -- the workspace cascade can only advance as fast as the
# apps below it appear, and VSCode takes seconds -- but a snapshot almost
# always names windows that aren't running (Awesun, WeChat), and waiting the
# full timeout for those every single time makes restore feel broken.
IDLE_GRACE = 15.0

# How long place() refuses to resolve an ambiguous match by coin flip. Long
# enough for restored shells to draw their first prompt (which is what makes
# two same-sized terminal windows tell themselves apart), short enough that a
# record which is ambiguous for real still gets placed well inside both
# IDLE_GRACE and the hard timeout. See match_windows(strict=...).
SETTLE_GRACE = 20.0


class NoDisplayError(RuntimeError):
    """X11 is not reachable, so no window state can be read or applied."""


def _screen():
    """Wnck screen handle, or NoDisplayError if X11 isn't reachable.

    init_check(), not init(): Gtk.init() *aborts the process* when it can't
    open the display -- it prints "cannot open display: :1" and exits, rather
    than raising something Python can catch. That cost a real snapshot. The
    logout unit's ExecStop runs during shutdown, by which point X is already
    gone, so `sessionctl save` died with status=1/FAILURE before writing
    anything at all -- and the caller's own try/except around layout capture
    (cli._capture_layout, written precisely to degrade to an empty layout
    rather than lose the snapshot) never got to run, because the process was
    killed underneath it. init_check() returns a boolean instead, which is
    what makes that degradation path reachable.
    """
    ok, _ = Gtk.init_check([])
    if not ok:
        raise NoDisplayError(
            "cannot open X display (DISPLAY="
            f"{os.environ.get('DISPLAY', '<unset>')!r}) -- window layout is "
            "unavailable; this is expected during shutdown or on a "
            "non-graphical session"
        )
    screen = Wnck.Screen.get_default()
    if screen is None:
        raise NoDisplayError("Wnck could not open the default screen")
    screen.force_update()
    return screen


def _pump(duration):
    """Process pending GLib/X11 events for `duration` seconds.

    Wnck's action methods (move_to_workspace, maximize, unmaximize,
    set_geometry, pin) send asynchronous X11 requests -- confirmed live:
    calling one and immediately re-querying the window's state shows no
    change at all, every single time, until the GLib main loop is actually
    given a chance to run. Without that, every placement action silently
    no-ops -- this would have shipped as a completely non-functional
    layout.py had it not been caught by testing the write side directly,
    not just the read side (which works fine without any pumping, since it
    reflects state already cached by force_update()).
    """
    end = time.monotonic() + duration
    while time.monotonic() < end:
        while Gtk.events_pending():
            Gtk.main_iteration()
        time.sleep(0.02)


TERMINAL_CLASS = "Gnome-terminal"


def _terminal_signature(slots, cwds=None):
    """A terminal window's identity, derived from the tabs it holds.

    Tab count first because it is available the instant a window maps; the
    directories only appear once each shell has drawn a prompt. See
    _signature_score for how the two are weighed.

    `cwds` overrides the directories read off the tabs themselves. Capture
    passes the shell hook's record (see _hook_window_cwds); the live side
    during placement has no hook data for shells that have only just started,
    so it passes None and falls back to the prompts.
    """
    if cwds is None:
        cwds = [(s.get("cwd") or "") for s in slots]
    return {"tabs": len(slots), "cwds": sorted(set(cwds))}


def _hook_window_cwds(terminals):
    """Per-window tab directories as recorded by the shell hook, or None.

    Why not just use the directories AT-SPI reads off each tab's prompt:
    those are only there when the tab *is* at a prompt. A tab running
    anything long-lived -- a training job, vim, less -- has no prompt on its
    last line, so prompt_of returns None and the tab contributes "" to the
    signature. A real snapshot on this machine recorded a five-tab window as
    {tabs: 5, cwds: ["", "~"]} when the tab that mattered was sitting in
    .../TensorRT-LLM/examples, and the only other five-tab window recorded
    {tabs: 5, cwds: ["~"]}. Neither could then match its own restored window
    exactly -- the restored tabs are all freshly at prompts, so they report
    the real directories -- and the two windows were placed on each other's
    workspaces.

    The hook does not have that blind spot: it writes each shell's cwd on
    every prompt, whatever the tab is doing now. Directories are abbreviated
    to match how a prompt renders them, since the live side still reads
    prompts.

    Ordering is the same view in both: collect_terminals groups the hook's
    per-tab records into AT-SPI's frames (collect._group), so index i is the
    same window here as in the frame list.
    """
    windows = (terminals or {}).get("windows")
    if not windows:
        return None
    return [
        [collect._abbreviated(t["cwd"]) if t.get("cwd") else ""
         for t in (w.get("tabs") or [])]
        for w in windows
    ]


def _terminal_signatures(windows, terminals=None):
    """Map XID -> tab signature for every gnome-terminal window.

    Terminal windows are otherwise indistinguishable to placement: same
    wm_class, same PID (they all belong to gnome-terminal-server), same
    geometry when maximized, and the same title whenever their active tabs
    share a directory. Four such windows on four different workspaces were
    restored onto each other's workspaces, because nothing told them apart.

    The link is AT-SPI's frame list, which is in the same order as Wnck's
    windows sorted by XID -- verified with a controlled marker: a throwaway
    terminal opened in a distinctive directory appeared at the same index in
    both views, and stayed matched after being moved to another workspace.
    Returns {} on any disagreement (different counts, no AT-SPI), which
    degrades placement to the previous class-and-title behaviour rather than
    pairing things up wrongly.
    """
    frames = collect.atspi_window_tabs()
    if not frames:
        return {}
    live_terminals = sorted(
        (w for w in windows
         if (w.get_class_group_name() or "") == TERMINAL_CLASS),
        key=lambda w: w.get_xid(),
    )
    if len(live_terminals) != len(frames):
        return {}
    hook = _hook_window_cwds(terminals)
    if hook is not None and len(hook) != len(frames):
        # The two views disagree about how many windows there are, so the
        # index-for-index pairing they rely on is not trustworthy. Same
        # policy as the frame-count check above: degrade, never guess.
        hook = None
    return {
        w.get_xid(): _terminal_signature(
            slots, None if hook is None else hook[i]
        )
        for i, (w, slots) in enumerate(zip(live_terminals, frames))
    }


def _signature_score(rec_sig, live_sig):
    """How strongly two tab signatures agree: 2 exact, 1 tab count only, 0 no.

    Split in two because a just-launched terminal has its tabs immediately
    but not yet its prompts, so the directories arrive a moment later. Tab
    count alone already separates most windows, and place() re-evaluates on
    every poll, so the match sharpens as the shells come up.
    """
    if not rec_sig or not live_sig:
        return 0
    if rec_sig.get("tabs") != live_sig.get("tabs"):
        return 0
    return 2 if rec_sig.get("cwds") == live_sig.get("cwds") else 1


VSCODE_SUFFIX = " - Visual Studio Code"


def _vscode_folder(title):
    """The folder a VSCode window has open, read off its title.

    VSCode titles the window "<file> - <folder> - Visual Studio Code", or
    "<folder> - Visual Studio Code" with no file open, so the folder is the
    last segment before the application name. Returns None while the window
    is still loading and titled just "Visual Studio Code", which is the
    signal to wait rather than to guess.

    Every VSCode window shares one Electron PID -- checked here: four
    windows, all pid 6349, all reporting the same cmdline -- so PID cannot
    separate them any more than it can separate gnome-terminal windows, and
    the title was the only discriminator left. Matching on the *whole* title
    fails because the open file is part of it and changes constantly.
    Measured on this desktop: the recorded LLM window, titled "Preview
    PoC.zh.md - LLM - Visual Studio Code", scored 0.696 against the *resume*
    window and only 0.639 against its own, because by restore time it was
    showing a different file. It claimed resume's window, resume took
    whatever was left, and both ended up on the wrong monitor. The folder
    segment does not move when the open file does.
    """
    if not title or not title.endswith(VSCODE_SUFFIX):
        return None
    head = title[: -len(VSCODE_SUFFIX)].strip()
    if not head:
        return None
    parts = [p.strip() for p in head.split(" - ") if p.strip()]
    return parts[-1] if parts else None


def _identity_score(rec, live):
    """How strongly a record and a live window agree on *which* window this
    is, independent of title similarity: 2 confident, 1 weak, 0 none.

    Terminals carry an explicit tab signature; VSCode windows are identified
    by the folder in their title. Both stay put under exactly the churn that
    makes a raw title unreliable -- a terminal's active tab changing, an
    editor's open file changing.
    """
    sig = _signature_score(rec.get("sig"), live.get("sig"))
    if sig:
        return sig
    rec_folder = _vscode_folder(rec.get("title"))
    live_folder = _vscode_folder(live.get("title"))
    if rec_folder and live_folder:
        return 2 if rec_folder == live_folder else 0
    return 0


def _has_identity(rec):
    """True when we know what this record is looking for, so failing to find
    it means "not yet" rather than "no such window". See match_windows's
    strict mode.
    """
    return bool(rec.get("sig")) or _vscode_folder(rec.get("title")) is not None


def capture(terminals=None):
    """Snapshot every window Wnck can see: class, title, pid, workspace,
    geometry, maximized, sticky.

    `terminals` is the collect_terminals() result for this same save, used
    only to give terminal windows a trustworthy tab signature -- see
    _hook_window_cwds. Optional: without it the signature falls back to
    whatever the tabs' prompts happen to show.

    Captures *every* window, including ones sessionctl doesn't launch
    (Nautilus, Slack, the GNOME Shell overlay) -- title is stored purely as
    a matching hint for placement and is deliberately excluded from the
    write-avoidance digest (see snapshot.digest), since it changes on every
    tab/file switch and would defeat write-avoidance if it didn't.
    """
    screen = _screen()
    live = screen.get_windows()
    sigs = _terminal_signatures(live, terminals)
    windows = []
    for w in live:
        ws = w.get_workspace()
        g = w.get_geometry()
        rec = {
            "wm_class": w.get_class_group_name() or "",
            "title": w.get_name() or "",
            "pid": w.get_pid(),
            "workspace": ws.get_number() if ws is not None else None,
            # is_sticky(), NOT `ws is None`. With two monitors and mutter's
            # default workspaces-only-on-primary=true, every window on a
            # non-primary monitor has no workspace at all -- confirmed live:
            # Chrome and two VSCode windows on the second monitor all report
            # workspace None, is_pinned() True, is_sticky() False, while the
            # genuinely sticky shell overlay reports is_sticky() True. Under
            # the old test those secondary-monitor windows were recorded as
            # sticky, and restoring such a record calls pin(), which really
            # does stick the window to every workspace. So a dual-monitor
            # session came back with windows that belonged to one workspace
            # now following the user everywhere -- and the next capture saw
            # a genuinely sticky window and recorded it as such, making the
            # damage permanent.
            "sticky": w.is_sticky(),
            "maximized": w.is_maximized(),
            "geometry": {"x": g[0], "y": g[1], "w": g[2], "h": g[3]},
        }
        sig = sigs.get(w.get_xid())
        if sig is not None:
            rec["sig"] = sig
        windows.append(rec)
    return {
        "workspace_count": screen.get_workspace_count(),
        "windows": windows,
    }


def live_xids():
    """XIDs of every window currently live.

    `restore` takes this as a baseline immediately *before* launching
    anything, then hands it to place() so matching can tell which windows
    this run actually created. Without that distinction a record binds to
    whichever same-class window happens to come first -- which, when you run
    `sessionctl restore` from a terminal, is the terminal you're sitting in
    rather than the one restore just launched. See match_windows.
    """
    return {w.get_xid() for w in _screen().get_windows()}


def _title_similarity(a, b):
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def match_windows(recorded, live, strict=False):
    """Pair recorded layout entries with live windows (plain dicts with at
    least "wm_class" and "title" keys -- callers may carry extra keys
    through, e.g. a live Wnck object reference, untouched).

    Class-first, title-second: candidates are pooled by exact wm_class match,
    then narrowed by title similarity when more than one candidate shares a
    class. Deliberately does NOT give terminal windows any special
    title-marker treatment, despite gnome-terminal supporting --title at
    launch -- tested live and found this system's default PS1 embeds a
    \\e]0;...\\a title-setting escape sequence that fires on the very first
    shell prompt, silently overwriting any launch-time title before a
    placement pass could ever observe it. So within a same-class pool with
    no useful title signal (every gnome-terminal window looks the same),
    this falls back to positional pairing, which is exactly the existing,
    already-documented limitation on multi-window terminal grouping (see the
    plan's Known limitations) -- not a new one introduced here.

    Terminal windows additionally carry a "sig" -- how many tabs they hold
    and which directories those tabs are in (see _terminal_signatures) --
    which ranks above title similarity and below "new". This is what finally
    tells four identical-looking terminal windows apart: same class, same
    PID, same maximized geometry, same title, but one holds six tabs in ~ and
    another three in a project directory. Without it their recorded
    workspaces were applied to whichever window happened to be first, so
    windows came back on each other's workspaces.

    A live window carrying "new": True outranks title similarity entirely.
    That flag means "this window did not exist before the current restore
    launched things" (see live_xids), and it is a far stronger signal than a
    title: the window was created *specifically* to satisfy these records.
    Found live -- restoring a maximized 4-tab terminal while already sitting
    in a terminal left the newly launched window untouched at gnome-terminal's
    default size, because both windows' titles are byte-identical once a shell
    has run ("user@host: ~"), so the similarity tie resolved to whichever came
    first in the live pool: the older, pre-existing window. The record's
    workspace and maximize landed on the wrong window and reported success.
    Only `restore` sets this flag; `sessionctl place` launches nothing, has no
    baseline to diff against, and so matches purely on class and title as
    before.

    `strict` refuses to place a record whose top score is achieved by more
    than one live window, returning it as unmatched instead. Ties are the
    whole failure mode here: a restore launches its terminals within a second
    of each other but their shells draw prompts a beat later, so for that
    beat every same-tab-count window looks identical and the score collapses
    to a tie. max() then resolves it by list order, which is arbitrary --
    two five-tab windows came back on each other's workspaces exactly this
    way. Since place() re-evaluates on every poll, deferring an ambiguous
    record costs a few hundred milliseconds and lets the prompts arrive; and
    once the records that *do* match unambiguously have claimed their
    windows, what is left for the ambiguous ones is usually a single
    candidate, which is no longer a tie at all. place() only asks for this
    during its settle window (see SETTLE_GRACE) so that a record which stays
    genuinely ambiguous still gets placed rather than dropped.

    Returns (matches, unmatched_recorded, unmatched_live). Each match is
    (recorded_window, live_window). Every recorded and live window is
    consumed at most once -- never double-placed, never double-claimed.
    """
    live_pool = list(live)
    matches = []
    unmatched_recorded = []

    def score(rec, w):
        return (
            bool(w.get("new")),
            _identity_score(rec, w),
            _title_similarity(rec.get("title"), w.get("title")),
        )

    for rec in recorded:
        cls = rec.get("wm_class", "")
        candidates = [w for w in live_pool if w.get("wm_class") == cls]
        if not candidates:
            unmatched_recorded.append(rec)
            continue

        scores = [score(rec, w) for w in candidates]
        top = max(scores)
        if strict and scores.count(top) > 1:
            unmatched_recorded.append(rec)
            continue
        if strict and top[1] == 0 and _has_identity(rec):
            # We know which window this record wants and no candidate is it
            # yet -- a VSCode window still titled "Visual Studio Code", a
            # terminal whose shells have not drawn prompts. Waiting a poll
            # is free; guessing here is what put windows on the wrong
            # monitor, because title similarity is dominated by the open
            # file rather than the folder.
            unmatched_recorded.append(rec)
            continue
        best = candidates[scores.index(top)]
        matches.append((rec, best))
        live_pool.remove(best)

    return matches, unmatched_recorded, live_pool


def _compact_workspace_indices(recorded):
    """Map recorded workspace indices to a gap-free ascending sequence.

    dynamic-workspaces=true means workspace N does not exist until 0..N-1 are
    occupied -- confirmed live: moving a window onto the current trailing
    empty workspace grows the count by one (4 -> 5), and a not-yet-existing
    index simply returns None rather than being created on demand. Placing
    straight into a snapshot's raw indices can therefore target a workspace
    that doesn't exist yet if the snapshot has gaps (e.g. windows on 0 and 3,
    nothing on 1-2). Compacting preserves relative order and grouping while
    remapping to consecutive indices, which are always reachable by placing
    in ascending order (see place()).
    """
    used = sorted({
        r["workspace"] for r in recorded
        if not r.get("sticky") and r.get("workspace") is not None
    })
    return {old: new for new, old in enumerate(used)}


def _target_workspace(rec, remap):
    if rec.get("sticky") or rec.get("workspace") is None:
        return None
    return remap.get(rec["workspace"], rec["workspace"])


def _onscreen(g, screen):
    """Recorded x/y, pulled back inside the screen if they now fall outside.

    A snapshot taken with a second monitor attached records the windows on it
    at coordinates past the first monitor's width -- x=1920 on this machine.
    Replayed with that monitor gone, those coordinates are still perfectly
    legal for X11 and the window is placed exactly where asked: nowhere the
    user can see. Nothing rescues it afterwards, since the window is not
    offscreen as far as the WM is concerned.

    Deliberately only clamps the origin, leaving width/height alone -- a
    window that was sized for a 2560x1440 monitor and comes back on a
    1920x1080 one is oversized but visible and draggable, which is recoverable
    in a way that being at x=1920 with no monitor there is not.
    """
    return (
        min(g["x"], max(0, screen.get_width() - 100)),
        min(g["y"], max(0, screen.get_height() - 100)),
    )


def _place_one(rec, wnck_window, screen, workspace_index):
    """Apply one recorded window's placement to a matched live window.

    Returns True when the whole record was applied, False when the target
    workspace does not exist yet -- the caller must retry rather than treat
    that as done. Geometry and maximized state are applied either way, since
    both are idempotent and a re-run costs nothing.

    Why a workspace can be missing at all: with dynamic-workspaces GNOME
    keeps exactly one trailing empty workspace, so workspace N exists only
    once 0..N-1 are occupied. Placing in ascending order is what grows the
    desktop one step at a time -- but only if each step actually happens.
    This used to return nothing and the caller counted every record as
    placed, so a target that didn't exist yet was skipped in silence and
    never revisited. After a reboot that is the normal case, not the corner
    case: every window starts on workspace 0, terminals map in under a
    second while VSCode takes several, so the first poll would try to put
    terminals on workspaces 2-5 that could not exist until VSCode had
    claimed workspace 1. They stayed on workspace 0 and `place` reported
    success.

    Order also matters within a record: setting geometry on a maximized
    window is a no-op (confirmed live), so unmaximize first if geometry
    needs to move, then re-apply maximize afterward if that's what was
    recorded.
    """
    workspace_ok = True
    if rec.get("sticky"):
        wnck_window.pin()
    elif workspace_index is None:
        # No workspace and not sticky: mutter gave this window no workspace
        # because it was on a non-primary monitor (see capture). Nothing to
        # apply -- geometry below puts it back on that monitor, and mutter
        # takes it off the workspace list again by itself. Pinning it here
        # would be wrong: it would stay on every workspace even once it is
        # back on the primary monitor.
        pass
    else:
        ws = screen.get_workspace(workspace_index)
        if ws is None:
            workspace_ok = False
        else:
            wnck_window.move_to_workspace(ws)
            # Let the move land so GNOME appends the next trailing empty
            # workspace before the following record asks for it.
            _pump(0.25)

    g = rec.get("geometry") or {}
    if all(k in g for k in ("x", "y", "w", "h")):
        if wnck_window.is_maximized():
            wnck_window.unmaximize()
            # Confirmed live: without a pump here, set_geometry's request is
            # computed against the window's still-transitioning unmaximize
            # state, landing tens of pixels short on width/height. A window
            # that was never maximized needs no such pause -- its geometry
            # was already settled.
            _pump(0.3)
        x, y = _onscreen(g, screen)
        wnck_window.set_geometry(
            Wnck.WindowGravity.CURRENT,
            Wnck.WindowMoveResizeMask.X
            | Wnck.WindowMoveResizeMask.Y
            | Wnck.WindowMoveResizeMask.WIDTH
            | Wnck.WindowMoveResizeMask.HEIGHT,
            x, y, g["w"], g["h"],
        )

    if rec.get("maximized"):
        wnck_window.maximize()

    return workspace_ok


def place(recorded_windows, timeout=DEFAULT_TIMEOUT, static_workspaces=False,
          preexisting=None):
    """Apply recorded workspace/geometry to whatever windows are currently
    live, matching them via match_windows().

    Apps do not map their windows instantly after being launched, so this
    polls rather than sleeping a fixed amount: each cycle re-scans live
    windows, places anything newly matched, and gives up on the rest after
    `timeout` seconds. The same poll loop also serves as the event-pump
    placement itself needs (see _pump) -- one mechanism does both jobs.

    Placement always proceeds in ascending (compacted) workspace order --
    this is what makes a dynamic-workspaces desktop reachable: placing a
    window on WS0, then WS1, then WS2 causes GNOME to append the next empty
    workspace each time, so the target always exists by the time it's
    needed. Reversing this order would target workspaces that don't exist
    yet. static_workspaces=True skips compaction and uses raw indices
    instead, for a desktop that has already been switched to
    dynamic-workspaces=false (see the plan's --static-workspaces escape
    hatch); sessionctl does not flip that gsetting itself.

    `preexisting` is the set of window XIDs that were live before the caller
    launched anything (layout.live_xids()). Any window not in it is flagged
    "new" for matching, which makes a record bind to the window this restore
    just created rather than to a same-class window that was already open --
    see match_windows for the live failure that motivated it. `sessionctl
    place` passes None (it launches nothing), leaving matching unchanged.

    Returns {"placed": [...recorded...], "unmatched": [...recorded...]}.
    """
    if not recorded_windows:
        return {"placed": [], "unmatched": []}

    remap = {} if static_workspaces else _compact_workspace_indices(recorded_windows)

    def sort_key(rec):
        ws = rec["workspace"] if static_workspaces else _target_workspace(rec, remap)
        return -1 if ws is None else ws

    pending = sorted(recorded_windows, key=sort_key)
    placed = []
    deadline = time.monotonic() + timeout
    last_progress = time.monotonic()
    settle_deadline = time.monotonic() + min(SETTLE_GRACE, timeout)

    while pending and time.monotonic() < deadline:
        settling = time.monotonic() < settle_deadline
        if not settling and time.monotonic() - last_progress > IDLE_GRACE:
            break
        screen = _screen()
        live_windows = screen.get_windows()
        sigs = _terminal_signatures(live_windows)
        live = [
            {
                "wm_class": w.get_class_group_name() or "",
                "title": w.get_name() or "",
                "new": preexisting is not None and w.get_xid() not in preexisting,
                "sig": sigs.get(w.get_xid()),
                "_wnck": w,
            }
            for w in live_windows
        ]
        matches, still_pending, _ = match_windows(pending, live, strict=settling)
        deferred = []
        placed_this_cycle = False
        for rec, live_entry in matches:
            ws = rec["workspace"] if static_workspaces else _target_workspace(rec, remap)
            if _place_one(rec, live_entry["_wnck"], screen, ws):
                placed.append(rec)
                placed_this_cycle = True
            else:
                # Target workspace doesn't exist yet. Keep the record pending
                # so it is retried once the windows below it have grown the
                # desktop far enough -- see _place_one.
                deferred.append(rec)
        if len(pending) != len(still_pending) + len(deferred) or placed_this_cycle:
            last_progress = time.monotonic()
        pending = still_pending + deferred
        if pending:
            _pump(POLL_INTERVAL)
        else:
            _pump(0.2)  # let the last batch of actions actually land before returning

    return {"placed": placed, "unmatched": pending}
