"""Gather the live session state: terminals, VSCode windows, browser tabs."""

import configparser
import glob
import json
import os
import re
import subprocess
import sys

from . import paths

TERMINAL_SERVER = "gnome-terminal-server"

# Warp's process name, and the parent of every pane shell. Read off the live
# desktop: `warp terminal-server` reports comm "warp".
WARP_SERVER = "warp"

VSCODE_STORAGE = os.path.expanduser(
    "~/.config/Code/User/globalStorage/storage.json"
)
CHROME_CONFIG_DIR = os.path.expanduser("~/.config/google-chrome")
FIREFOX_PROFILES_INI = os.path.expanduser(
    "~/snap/firefox/common/.mozilla/firefox/profiles.ini"
)
CHROME_URL_LIMIT = 500


def _run(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return p.returncode, p.stdout


def _proc_environ(pid):
    """Return the process environment as a dict, or {} if unreadable."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return {}
    env = {}
    for item in raw.split("\0"):
        key, sep, value = item.partition("=")
        if sep:
            env[key] = value
    return env


def _parent_comm(pid):
    """Return the command name of a process's parent, or None.

    /proc/<pid>/comm truncates to 15 bytes (TASK_COMM_LEN-1), so
    "gnome-terminal-server" is read back as "gnome-terminal-". Callers must
    compare with that in mind -- see _is_live_tab.
    """
    try:
        with open(f"/proc/{pid}/status") as f:
            ppid = next(
                line.split()[1] for line in f if line.startswith("PPid:")
            )
        with open(f"/proc/{ppid}/comm") as f:
            return f.read().strip()
    except (OSError, StopIteration, IndexError):
        return None


def dbus_tree():
    """Screens and windows currently known to gnome-terminal-server.

    Returns (screen_paths, window_paths). Empty sets if the server is not
    running or busctl is unavailable -- callers treat that as "cannot verify"
    rather than "nothing is live".
    """
    rc, out = _run(["busctl", "--user", "tree", "org.gnome.Terminal"])
    if rc != 0:
        return set(), set()
    screens = set(re.findall(r"/org/gnome/Terminal/screen/\w+", out))
    windows = set(re.findall(r"/org/gnome/Terminal/window/\w+", out))
    return screens, windows


def record_kind(rec):
    """Which terminal a shell record came from: "gnome" or "warp".

    Records written before the hook learned about Warp have no "kind" at all,
    and every one of those is a gnome-terminal tab -- so the absent value
    reads as "gnome" rather than being discarded. That keeps a snapshot taken
    across the upgrade readable instead of quietly losing its terminals.
    """
    return rec.get("kind") or "gnome"


def _is_live_gnome_tab(rec, live_screens):
    """Is this record still a real, running gnome-terminal tab?"""
    pid = rec["pid"]
    if _proc_environ(pid).get("GNOME_TERMINAL_SCREEN") != rec.get("screen"):
        return False
    parent = _parent_comm(pid)
    if parent is None or not TERMINAL_SERVER.startswith(parent):
        return False
    # Only trust the D-Bus list when we actually got one.
    if live_screens and rec.get("screen") not in live_screens:
        return False
    return True


def _is_live_warp_pane(rec):
    """Is this record still a real, running Warp pane?

    Same shape of check as the gnome-terminal one, against Warp's equivalents:
    the per-pane UUID Warp exports into the shell's environment, and the
    parent process being Warp itself. Verified on the live desktop -- all nine
    pane shells had ppid pointing at `warp terminal-server`, whose comm is
    exactly "warp" (short enough that /proc/<pid>/comm's 15-byte truncation,
    which once broke the gnome-terminal-server check, cannot bite here).

    A plain `bash` run *inside* a pane inherits that pane's UUID, so it would
    otherwise be recorded as a second pane with the same identity and could
    overwrite the real pane's conda env. Its parent is the outer bash, not
    Warp, so this rejects it -- which is also why nested shells are simply not
    captured, exactly as for gnome-terminal.
    """
    pid = rec["pid"]
    uuid = rec.get("warp_uuid")
    if not uuid:
        return False
    return (
        _proc_environ(pid).get("WARP_TERMINAL_SESSION_UUID") == uuid
        and _parent_comm(pid) == WARP_SERVER
    )


def _is_live_tab(rec, live_screens):
    """Is this shell record still a real, running tab or pane of its own kind?

    Judging every record by gnome-terminal's rules is not merely inaccurate,
    it destroys data: read_shell_records prunes what it judges dead, so a Warp
    pane's record -- which has no GNOME_TERMINAL_SCREEN and whose parent is
    not gnome-terminal-server -- would be deleted on the next save while the
    pane was still open, taking its conda env with it. So the kind decides
    which checks apply, and an unrecognised kind is left alone rather than
    validated by rules that cannot fit it.

    The PID liveness check is common to both, and is what makes PID reuse
    safe: a stale file naming a dead PID that the kernel has since handed to
    an unrelated process would otherwise be taken at face value.
    """
    pid = rec.get("pid")
    if not isinstance(pid, int) or not os.path.isdir(f"/proc/{pid}"):
        return False
    kind = record_kind(rec)
    if kind == "warp":
        return _is_live_warp_pane(rec)
    if kind == "gnome":
        return _is_live_gnome_tab(rec, live_screens)
    return False


def read_shell_records(prune=False, kind=None):
    """Load and validate the per-tab files written by bash-hook.sh.

    With prune=True, files for tabs that are gone are deleted. Each record is
    validated against its own terminal's rules first (see _is_live_tab), so
    pruning never deletes a live record just because the caller asked for a
    different kind.

    kind filters what is returned -- "gnome", "warp", or None for everything.
    The filter is applied after validation for that reason.
    """
    live_screens, _ = dbus_tree()
    records = []
    for path in glob.glob(os.path.join(paths.SHELLS_DIR, "*.json")):
        try:
            with open(path) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            if prune:
                _unlink(path)
            continue
        if not _is_live_tab(rec, live_screens):
            if prune:
                _unlink(path)
            continue
        if kind is None or record_kind(rec) == kind:
            records.append(rec)
    return records


def read_warp_shells(prune=False):
    """Live Warp pane records from the shell hook, newest write last.

    Sorted by the hook's own timestamp so that if two records somehow claim
    the same pane UUID, the later one wins when they are folded into a
    uuid -> conda map (see warp._conda_by_uuid). Nested shells are already
    rejected upstream, so this is a tie-break for the unexpected rather than
    the normal path.
    """
    records = read_shell_records(prune=prune, kind="warp")
    records.sort(key=lambda r: (r.get("ts") or 0, r.get("pid") or 0))
    return records


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def collect_terminals(prune=True):
    """Collect gnome-terminal tabs, grouped into windows.

    Tab order is by PID, which approximates creation order.

    The tab-to-window mapping comes from AT-SPI (see atspi_window_tabs); an
    earlier version of this docstring said it was "simply not available",
    which was true of gnome-terminal's D-Bus API but not of the machine as a
    whole. When AT-SPI can't be reached the grouping falls back to an even
    split, which is exact only for equal-sized windows -- see the plan's
    limitations.
    """
    # Warp panes write into the same directory and must not be counted as
    # gnome-terminal tabs: their number would inflate the tab count, and a
    # desktop with Warp open but no gnome-terminal would look like a
    # gnome-terminal session whose windows had all vanished.
    records = read_shell_records(prune=prune, kind="gnome")
    records.sort(key=lambda r: r["pid"])

    tabs = [
        {
            "pid": r["pid"],
            "screen": r.get("screen"),
            "cwd": r.get("cwd"),
            "conda": r.get("conda") or None,
        }
        for r in records
    ]

    _, windows = dbus_tree()
    window_count = max(1, len(windows)) if tabs else len(windows)

    # Preferred source for the tab-to-window mapping; None means AT-SPI
    # wasn't reachable and _group falls back to an even split.
    window_slots = atspi_window_tabs() if tabs else None
    if window_slots:
        window_count = len(window_slots)

    return {
        "window_count": window_count,
        "windows": _group(tabs, window_count, window_slots=window_slots),
        "hook_installed": os.path.isdir(paths.SHELLS_DIR)
        and bool(glob.glob(os.path.join(paths.SHELLS_DIR, "*.json"))),
    }


def atspi_window_tabs():
    """Tab titles per terminal window, via AT-SPI, or None if unavailable.

    The real tab-to-window mapping is not on gnome-terminal's D-Bus API:
    screens and windows are flat, unrelated objects, the window objects carry
    only org.gtk.Actions (whose `active-tab` gives an index, not a count),
    there is no org.gtk.Menus to enumerate the tab list, and VTE doesn't set
    WINDOWID here. So `_group` used to split tabs evenly and get the shape
    wrong whenever the windows weren't equal -- 5/6/7 tabs came back as
    6/6/6.

    The accessibility tree does have it. gnome-terminal-server exposes one
    `frame` per window, each containing one `page tab` per tab, named with
    that tab's title. Verified against a live desktop: five frames reporting
    1/5/5/4/5 tabs, totalling exactly the 20 tabs the shell hook had
    recorded. It also works with `toolkit-accessibility` set to false, which
    is this machine's setting, and costs ~0.05s.

    Returns a list (one entry per window) of lists of tab titles, or None if
    AT-SPI can't be reached -- callers must degrade rather than fail, since
    this is an optional refinement and the a11y bus is not guaranteed.

    Runs in a subprocess, which is not defensiveness for its own sake:
    Atspi.init() *aborts the process* when it can't reach the accessibility
    bus -- "dbind-ERROR ... Is at-spi-bus-launcher running?" followed by a
    core dump, not an exception. try/except cannot catch that, exactly as it
    could not catch Gtk.init()'s abort on a dead display (see
    layout._screen). Caught here by running `save` with the bus address
    pointed at a nonexistent socket, which dumped core. In-process, that
    would mean every periodic save dying before it wrote anything, on any
    machine whose a11y bus isn't up. A child process also bounds a hang,
    which is the other way a call into an unresponsive peer can go wrong.
    """
    probe = r"""
import json, re, sys
import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi
Atspi.init()
desktop = Atspi.get_desktop(0)
app = None
for i in range(desktop.get_child_count()):
    c = desktop.get_child_at_index(i)
    if c is not None and "gnome-terminal-server" in (c.get_name() or ""):
        app = c
        break
if app is None:
    print("null"); sys.exit(0)

# "(env) user@host:~/dir$" and the same without a conda prefix.
WITH_ENV = re.compile(r"^\(([^)]*)\)\s+\S+@\S+:(\S+?)[$#]\s*$")
NO_ENV = re.compile(r"^\S+@\S+:(\S+?)[$#]\s*$")

def tabs_under(node, depth=0, maxdepth=4):
    if node is None:
        return []
    if node.get_role_name() == "page tab":
        return [node]
    if depth >= maxdepth:
        return []
    out = []
    for i in range(node.get_child_count()):
        out.extend(tabs_under(node.get_child_at_index(i), depth + 1, maxdepth))
    return out

def terminal_under(node):
    stack = [node]
    while stack:
        n = stack.pop(0)
        if n.get_role_name() == "terminal":
            return n
        for i in range(n.get_child_count()):
            stack.append(n.get_child_at_index(i))
    return None

def prompt_of(tab):
    term = terminal_under(tab)
    if term is None:
        return None, None
    try:
        n = Atspi.Text.get_character_count(term)
        # The tail: offset 0 is the top of the scrollback, not the prompt.
        raw = Atspi.Text.get_text(term, max(0, n - 600), n)
    except Exception:
        return None, None
    for line in reversed([l for l in raw.splitlines() if l.strip()]):
        line = line.strip()
        m = WITH_ENV.match(line)
        if m:
            return (m.group(1) or None), m.group(2)
        m = NO_ENV.match(line)
        if m:
            return None, m.group(1)
    return None, None

windows = []
for i in range(app.get_child_count()):
    fr = app.get_child_at_index(i)
    if fr is None or fr.get_role_name() != "frame":
        continue
    slots = []
    for tab in tabs_under(fr):
        conda, cwd = prompt_of(tab)
        slots.append({"title": tab.get_name() or "", "conda": conda, "cwd": cwd})
    if slots:
        windows.append(slots)
print(json.dumps(windows or None))
"""
    try:
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        parsed = json.loads(out.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    if not all(isinstance(w, list) for w in parsed):
        return None
    if not all(isinstance(slot, dict) for w in parsed for slot in w):
        return None
    return parsed


def _abbreviated(cwd):
    """`~`-abbreviated cwd, matching how gnome-terminal renders it in a title."""
    home = os.path.expanduser("~")
    if cwd == home:
        return "~"
    if cwd.startswith(home + "/"):
        return "~" + cwd[len(home):]
    return cwd


def _norm_env(value):
    """Normalise a conda env name for comparison ("" and None are the same)."""
    return value or None


def _group(tabs, window_count, window_slots=None):
    """Spread tabs across the known window count.

    `window_slots` is AT-SPI's per-window view (see atspi_window_tabs): one
    list per window, each entry a dict with the tab's `conda` and `cwd` as
    read from its on-screen prompt. That fixes both halves of the problem --
    the *shape* (how many tabs per window) and the *contents* (which tab).

    Matching is two passes so the strongest evidence wins globally rather
    than per-slot: first every slot that can be filled by a tab agreeing on
    both cwd and conda env, then, for what's left, agreement on cwd alone.
    Anything still unmatched fills the remaining slots in order.

    An earlier version matched only on the window's tab *titles*, which
    encode the cwd. That got the shape right but the contents wrong whenever
    tabs shared a directory: with every title reading "user@host: ~", nothing
    discriminated a torch209 tab from a base one, so slots were filled in PID
    order and three of five windows came back with the wrong envs. The conda
    env is in the prompt even when it isn't in the title.

    Without AT-SPI this falls back to the old even split, which is right only
    when the windows happen to hold equal numbers of tabs.
    """
    if not tabs:
        return []

    if window_slots:
        remaining = list(tabs)
        groups = [[None] * len(slots) for slots in window_slots]

        def claim(predicate):
            for gi, slots in enumerate(window_slots):
                for si, slot in enumerate(slots):
                    if groups[gi][si] is not None:
                        continue
                    match = next(
                        (t for t in remaining if predicate(t, slot)), None
                    )
                    if match is not None:
                        remaining.remove(match)
                        groups[gi][si] = match

        claim(lambda t, s: (
            s.get("cwd") and t.get("cwd")
            and s["cwd"] == _abbreviated(t["cwd"])
            and _norm_env(s.get("conda")) == _norm_env(t.get("conda"))
        ))
        claim(lambda t, s: (
            s.get("cwd") and t.get("cwd")
            and s["cwd"] == _abbreviated(t["cwd"])
        ))
        claim(lambda t, s: True)

        out = []
        for picked in groups:
            real = [t for t in picked if t is not None]
            if real:
                out.append({"index": len(out), "tabs": real})
        # Anything still unassigned (more shells than page tabs) goes last
        # rather than being dropped.
        if remaining:
            if out:
                out[-1]["tabs"].extend(remaining)
            else:
                out.append({"index": 0, "tabs": remaining})
        if out:
            return out

    if window_count <= 1:
        return [{"index": 0, "tabs": tabs}]

    per = -(-len(tabs) // window_count)  # ceiling division
    groups = []
    for i in range(window_count):
        chunk = tabs[i * per : (i + 1) * per]
        if chunk:
            groups.append({"index": len(groups), "tabs": chunk})
    return groups


def _stat_source(path):
    """(path, mtime, size) for a source file, used to skip a re-scan when
    nothing has changed. The dominant cost in `save` is not the write -- it
    is re-scanning Chrome's session files, which run to hundreds of KB.
    """
    try:
        st = os.stat(path)
        return {"path": path, "mtime": st.st_mtime, "size": st.st_size}
    except OSError:
        return {"path": path, "mtime": None, "size": None}


def collect_vscode(previous=None):
    """VSCode's own record of open windows, read directly.

    windowsState.openedWindows is VSCode's authoritative account of what is
    open -- no X11 title parsing or state.vscdb reverse-engineering needed.
    lastActiveWindow is folded in too: when only one window is open it can be
    the sole place that window is recorded (unverified against a real
    single-window VSCode -- closing one of two live windows to check felt too
    disruptive to force just to confirm this, so the union is deliberately
    defensive rather than assumed unnecessary).
    """
    source = _stat_source(VSCODE_STORAGE)
    if previous and previous.get("source") == source:
        return dict(previous)

    try:
        with open(VSCODE_STORAGE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"windows": [], "source": source}

    windows_state = data.get("windowsState", {})
    folders = []
    for w in windows_state.get("openedWindows", []):
        folder = w.get("folder")
        if folder:
            folders.append(folder)

    last = windows_state.get("lastActiveWindow", {}).get("folder")
    if last and last not in folders:
        folders.append(last)

    return {
        "windows": [{"folder": f, "path": _uri_to_path(f)} for f in folders],
        "source": source,
    }


def _uri_to_path(uri):
    """Decode a file:// URI to a plain filesystem path for `code <path>`."""
    from urllib.parse import unquote, urlparse

    return unquote(urlparse(uri).path)


def _chrome_profiles():
    """Profile directory names under the Chrome config dir that look real."""
    if not os.path.isdir(CHROME_CONFIG_DIR):
        return []
    names = os.listdir(CHROME_CONFIG_DIR)
    return sorted(
        n
        for n in names
        if (n == "Default" or n.startswith("Profile "))
        and os.path.isdir(os.path.join(CHROME_CONFIG_DIR, n, "Sessions"))
    )


def _snss_urls(path, limit=CHROME_URL_LIMIT):
    """Best-effort URL extraction from a Chrome SNSS session file.

    SNSS is Chromium's internal binary session-command format, and it encodes
    far more than the URLs worth restoring: referrers, iframe requests,
    favicon fetches, duplicate entries for the same tab across navigation
    events. There is no version-stable way to tell "this is the tab's
    top-level URL" apart from that noise using plain string scanning -- doing
    so properly would mean replicating Chromium's internal pickle layout,
    which changes across releases. So this stays genuinely best-effort: a
    tolerant scan, deduped and order-preserved. Good enough for the
    human-readable record and the --chrome-urls restore fallback; the primary
    restore path is Chrome's own "continue where you left off".

    Deliberately reads the newest Session_* file, not Tabs_* -- Tabs_* stores
    each tab's full back/forward history and runs 5-10x larger for the same
    browsing, almost all of it pages already navigated away from.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return []

    text = raw.decode("latin-1")  # 1:1 byte mapping; never raises
    # Restricted to the RFC 3986 URL charset, not "anything printable" -- the
    # first cut used a negated class (exclude quotes/whitespace/backslash),
    # and on the raw pickle bytes almost everything else decodes to *some*
    # latin-1 character, so it happily glued each URL to the binary field
    # data that follows it in the pickle stream. Caught by inspecting actual
    # output against this file, not by any test in isolation.
    found = re.findall(
        r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]{3,300}", text
    )

    seen = set()
    out = []
    for u in found:
        u = u.rstrip(",.;:)")
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= limit:
            break
    return out


def _session_sort_key(path):
    """Order Session_<n> files by their embedded counter, not mtime.

    Chrome's own filename suffix is a monotonically increasing counter (it
    tracks the file's creation time directly -- confirmed against real
    profile data, where the numeric order matched wall-clock order exactly).
    Filesystem mtime is not a safe substitute: two files written within one
    mtime tick sort arbitrarily under max(key=os.path.getmtime), which a test
    creating two session files back-to-back caught for real.
    """
    name = os.path.basename(path)
    try:
        return int(name.split("_", 1)[1])
    except (IndexError, ValueError):
        return -1


def collect_chrome(previous=None):
    """Chrome tab URLs, one entry per profile that has an active session."""
    profiles = []
    prev_by_profile = {
        p.get("profile"): p for p in (previous or {}).get("profiles", [])
    }

    for name in _chrome_profiles():
        session_dir = os.path.join(CHROME_CONFIG_DIR, name, "Sessions")
        candidates = glob.glob(os.path.join(session_dir, "Session_*"))
        if not candidates:
            continue
        newest = max(candidates, key=_session_sort_key)

        source = _stat_source(newest)
        prev = prev_by_profile.get(name)
        if prev and prev.get("source") == source:
            profiles.append(dict(prev))
            continue

        profiles.append(
            {"profile": name, "source": source, "urls": _snss_urls(newest)}
        )

    return {"profiles": profiles}


def collect_firefox():
    """Whether Firefox is running, and which profile it would resume.

    Firefox restores its own tabs on relaunch, so nothing here needs to parse
    its session store -- just record enough to know a relaunch is expected.
    Untested against a real Firefox session: it is an unused snap on this
    machine (see the plan and README).
    """
    rc, _ = _run(["pgrep", "-x", "firefox"])
    running = rc == 0

    profile = None
    cfg = configparser.ConfigParser()
    if cfg.read(FIREFOX_PROFILES_INI):
        for section in cfg.sections():
            if cfg.has_option(section, "Path") and (
                cfg.getboolean(section, "Default", fallback=False)
                or profile is None
            ):
                profile = cfg.get(section, "Path")

    return {"running": running, "profile": profile}
