"""Warp terminal: read its session state, and hand restore back to Warp.

Warp is the odd one out among the things sessionctl captures, and in a good
way. gnome-terminal had to be reverse-engineered from three partial views --
a D-Bus tree that lists windows and screens as unrelated flat objects, an
accessibility tree for the tab-to-window mapping, and a shell hook for the
conda env. Warp keeps a real relational record of its own session in
``~/.local/state/warp-terminal/warp.sqlite``: ``windows`` -> ``tabs`` ->
``pane_nodes`` -> ``terminal_panes``, with the split tree in
``pane_branches``/``pane_leaves``. Window-to-tab-to-pane is a foreign key, not
a heuristic, so the mapping this project fought AT-SPI for comes for free and
is exact.

Two facts about that database, both checked against the live one rather than
assumed:

* ``terminal_panes.cwd`` tracks the *current* directory, not the one the pane
  started in. Proven by a natural experiment already present in the data:
  pane ``cc5b370b...`` had ``WARP_INITIAL_WORKING_DIR=/home/shengcn`` in its
  shell's environ while the database recorded
  ``/home/shengcn/Projects/finance/stock_03_claude``, which is where that
  shell had since been cd'd to. So the cwd here is trustworthy on its own and
  does not need the shell hook to correct it.
* The cwd is stored in *logical* form -- the path with ``~/Projects`` still a
  symlink -- while ``/proc/<pid>/cwd`` resolves it to the physical
  ``/media/shengcn/...`` path. Neither is wrong; they are the same directory.
  Restore never has to care, because Warp restores its own panes, but anything
  comparing the two must not expect them to be string-equal.

``WARP_TERMINAL_SESSION_UUID``, which Warp exports into every pane's shell, is
the hex of ``terminal_panes.uuid``. Verified exactly: all nine live shells'
UUIDs matched all nine rows, one to one. That is the join key for the one
thing Warp does *not* record -- the conda env, which only the shell itself can
observe (see bash-hook.sh) -- so conda arrives per pane with no guessing.

Restore is deliberately thin. Warp's own ``general.restore_session`` setting
defaults to true and rebuilds windows, tabs, panes, splits and cwds from this
same database on startup, so the entire restore is "launch Warp, if it isn't
already running". This is the Chrome pattern the project already uses: let the
app restore itself, record its state for `show`/`list`, and keep sessionctl's
job to the part the app cannot do -- which for Warp is the GNOME workspace
each window sat on, since Warp stores its own x/y/size but has no concept of a
workspace.

What is NOT here, having looked for it: there is no way to launch a Warp pane
with a command or an environment. ``warp-terminal --help`` exposes no
window/tab/session flags at all (its subcommands are agent, cloud, MCP and
auth management), the binary contains only two ``warp://`` URI routes --
``warp://cli`` and ``warp://linear``, neither of which opens a pane at a path
-- and this version has no launch-configuration file support. So the conda env
is captured but cannot be replayed into a restored Warp pane; see the README's
limitations.
"""

import os
import sqlite3

from . import collect

STATE_DIR = os.path.expanduser("~/.local/state/warp-terminal")
DB_PATH = os.path.join(STATE_DIR, "warp.sqlite")

# Warp's own settings file. TOML, hand-editable, and hot-reloaded by Warp --
# unlike Chrome's Preferences, which Chrome rewrites from memory on exit.
SETTINGS_PATH = os.path.expanduser("~/.config/warp-terminal/settings.toml")

WARP_APP = "warp-terminal"

# The X11 class every Warp window carries, read off the live windows rather
# than guessed (it matches StartupWMClass in dev.warp.Warp.desktop).
WARP_CLASS = "dev.warp.Warp"

# Warp elides the front of a long path in the window title, so the title is a
# suffix of the abbreviated cwd rather than equal to it: a pane in
# ~/Projects/finance/stock_03_claude showed as "..s/finance/stock_03_claude".
TITLE_ELLIPSIS = ".."

# A read that waits on Warp's own writer for longer than this is not worth
# blocking a 30-second timer on; a save that skips Warp once loses nothing.
DB_TIMEOUT = 2.0


def _sources():
    """(path, mtime, size) for the database *and* its write-ahead log.

    The WAL is the load-bearing half. Warp runs in WAL mode (confirmed:
    ``pragma journal_mode`` returns ``wal`` on the live database), so a
    committed change lands in ``warp.sqlite-wal`` and may leave
    ``warp.sqlite``'s own mtime untouched until a checkpoint. Statting only
    the main file would make the skip below conclude "nothing changed" while
    every recent pane move sat unread in the log.
    """
    return [
        collect._stat_source(DB_PATH),
        collect._stat_source(DB_PATH + "-wal"),
    ]


def _connect():
    """Open Warp's database read-only, or raise sqlite3.Error.

    ``mode=ro`` and not ``immutable=1``: immutable promises the file cannot
    change and makes SQLite ignore the WAL entirely, which would silently
    serve state from the last checkpoint instead of the live session. ro is
    also what keeps this safe to run against a database Warp has open -- it
    takes read locks only and cannot write, checkpoint or recover.
    """
    return sqlite3.connect(
        f"file:{DB_PATH}?mode=ro", uri=True, timeout=DB_TIMEOUT
    )


def running():
    """Is Warp running? Used to decide whether restore needs to launch it."""
    rc, _ = collect._run(["pgrep", "-x", "warp"])
    return rc == 0


def _conda_by_uuid(shells):
    """uuid -> conda env, from the shell hook's Warp records.

    The database has no column for this and cannot have one: ``conda
    activate`` mutates the running shell and never re-execs, so the env name
    exists only inside the shell. bash-hook.sh reports it keyed by the pane
    UUID Warp puts in that shell's environment, which is the same UUID the
    database stores as a blob -- so this join is exact rather than positional.
    """
    out = {}
    for rec in shells or []:
        uuid = rec.get("warp_uuid")
        if uuid and rec.get("conda"):
            out[uuid.lower()] = rec["conda"]
    return out


def _read_db(conda_by_uuid):
    """The window/tab/pane tree, or [] if the database can't be read.

    Ordering is by primary key throughout, which is Warp's own insertion
    order and so approximates the order tabs and panes were created in. It is
    deliberately NOT relied on to line these rows up with X11 windows -- see
    layout._warp_signatures for why that pairing is done by content instead.
    """
    conn = _connect()
    try:
        windows = conn.execute(
            "SELECT id, active_tab_index, window_width, window_height,"
            "       origin_x, origin_y, fullscreen_state"
            "  FROM windows ORDER BY id"
        ).fetchall()

        tabs_by_window = {}
        for tab_id, window_id, custom_title in conn.execute(
            "SELECT id, window_id, custom_title FROM tabs ORDER BY id"
        ):
            tabs_by_window.setdefault(window_id, []).append(
                {"id": tab_id, "custom_title": custom_title}
            )

        # Panes are joined to their tab through pane_nodes, which carries
        # tab_id on every node (not just roots), so leaves can be selected
        # directly without walking the tree. pane_leaves.is_focused is what
        # decides the window title; terminal_panes.is_active is Warp's own
        # per-pane flag and is kept as-is rather than reinterpreted.
        panes_by_tab = {}
        for (
            node_id, tab_id, cwd, is_active, is_focused, uuid_hex, flex,
        ) in conn.execute(
            "SELECT n.id, n.tab_id, p.cwd, p.is_active, l.is_focused,"
            "       lower(hex(p.uuid)), n.flex"
            "  FROM pane_nodes n"
            "  JOIN pane_leaves  l ON l.pane_node_id = n.id"
            "  JOIN terminal_panes p ON p.id = n.id"
            " WHERE n.is_leaf = 1"
            " ORDER BY n.tab_id, n.id"
        ):
            panes_by_tab.setdefault(tab_id, []).append({
                "id": node_id,
                "uuid": uuid_hex,
                "cwd": cwd,
                "conda": conda_by_uuid.get((uuid_hex or "").lower()),
                "active": bool(is_active),
                "focused": bool(is_focused),
                "flex": flex,
            })

        # Split orientation, for the record only -- Warp rebuilds its own
        # splits, so nothing downstream replays this.
        horizontal_by_tab = {}
        for tab_id, horizontal in conn.execute(
            "SELECT n.tab_id, b.horizontal"
            "  FROM pane_branches b JOIN pane_nodes n ON n.id = b.pane_node_id"
        ):
            horizontal_by_tab[tab_id] = bool(horizontal)
    finally:
        conn.close()

    out = []
    for (
        win_id, active_tab_index, width, height, x, y, fullscreen,
    ) in windows:
        tabs = []
        for tab in tabs_by_window.get(win_id, []):
            panes = panes_by_tab.get(tab["id"], [])
            tabs.append({
                "id": tab["id"],
                "custom_title": tab["custom_title"],
                "split_horizontal": horizontal_by_tab.get(tab["id"]),
                "panes": panes,
            })
        out.append({
            "id": win_id,
            "active_tab_index": active_tab_index,
            "fullscreen_state": fullscreen,
            "geometry": _geometry(x, y, width, height),
            "tabs": tabs,
        })
    return out


def _geometry(x, y, width, height):
    """Warp's own recorded window geometry, or None when it has none.

    Kept for `show` and for diagnosing a restore, not for placement: the
    layout pass uses Wnck's live geometry like it does for every other
    window, and Warp applies this itself when it restores.
    """
    if None in (x, y, width, height):
        return None
    return {"x": int(x), "y": int(y), "w": int(width), "h": int(height)}


def active_cwd(window):
    """The cwd Warp is showing in this window's title bar, abbreviated.

    Warp titles a window after the focused pane of its active tab, with ``~``
    substituted for $HOME -- so this is the one field that can be compared
    against a live X11 window title. Falls back through the active tab's
    first pane and then the window's first pane, because a window mid-restore
    can have an ``active_tab_index`` pointing past a tab list that is still
    filling in.
    """
    tabs = window.get("tabs") or []
    if not tabs:
        return None
    index = window.get("active_tab_index") or 0
    tab = tabs[index] if 0 <= index < len(tabs) else tabs[0]
    panes = tab.get("panes") or []
    if not panes:
        panes = next((t["panes"] for t in tabs if t.get("panes")), [])
    if not panes:
        return None
    pane = next((p for p in panes if p.get("focused")), panes[0])
    cwd = pane.get("cwd")
    return collect._abbreviated(cwd) if cwd else None


def signature(window):
    """A Warp window's identity in the same shape layout.py already uses.

    Deliberately the exact ``{"tabs": n, "cwds": [...]}`` dict that
    _terminal_signature produces for gnome-terminal, so _signature_score and
    match_windows work on Warp windows unchanged. The difference is only in
    how it is obtained: gnome-terminal's comes from prompts read off an
    accessibility tree and is blind to any tab that isn't sitting at a
    prompt, while this is read from Warp's own record and is exact whatever
    the panes are running.

    Pane directories rather than tab directories, because a split tab holds
    several and all of them are part of what makes the window recognisable.
    """
    cwds = set()
    for tab in window.get("tabs") or []:
        for pane in tab.get("panes") or []:
            if pane.get("cwd"):
                cwds.add(collect._abbreviated(pane["cwd"]))
    return {"tabs": len(window.get("tabs") or []), "cwds": sorted(cwds)}


def collect_warp(previous=None, shells=None):
    """Warp's session state, or an empty record when Warp isn't installed.

    Follows collect_vscode/collect_chrome: the source files' (path, mtime,
    size) are recorded and the whole scan is skipped when they are unchanged,
    which matters here for the same reason it does for Chrome -- this is a
    1.2 MB database with a 2 MB write-ahead log being read by a timer every
    30 seconds.

    The skip has one wrinkle those two don't: conda envs come from the shell
    hook, not from the database, so a pane that only changed its conda env
    leaves both files' stats untouched. The cached record is therefore
    re-joined against the current hook data rather than returned as-is.
    """
    if not os.path.exists(DB_PATH):
        return {"running": False, "windows": [], "sources": [], "installed": False}

    sources = _sources()
    conda_by_uuid = _conda_by_uuid(shells)

    if previous and previous.get("sources") == sources:
        cached = dict(previous)
        cached["running"] = running()
        cached["windows"] = _rejoin_conda(cached.get("windows", []), conda_by_uuid)
        return cached

    try:
        windows = _read_db(conda_by_uuid)
    except sqlite3.Error as e:
        # A locked or half-written database is a bad moment, not a broken
        # install: report nothing for Warp this round and let the next save
        # pick it up. Never raise -- one unreadable collector must not take
        # down a save that still has terminals, VSCode, Chrome and layout to
        # record.
        return {
            "running": running(),
            "windows": [],
            "sources": sources,
            "installed": True,
            "error": str(e),
        }

    return {
        "running": running(),
        "windows": windows,
        "sources": sources,
        "installed": True,
    }


def _rejoin_conda(windows, conda_by_uuid):
    """Re-apply hook-sourced conda envs to a cached window list."""
    out = []
    for window in windows:
        tabs = []
        for tab in window.get("tabs") or []:
            panes = [
                dict(p, conda=conda_by_uuid.get((p.get("uuid") or "").lower()))
                for p in tab.get("panes") or []
            ]
            tabs.append(dict(tab, panes=panes))
        out.append(dict(window, tabs=tabs))
    return out


def pane_count(warp):
    """Total panes across every recorded Warp window."""
    return sum(
        len(tab.get("panes") or [])
        for w in (warp or {}).get("windows", [])
        for tab in w.get("tabs") or []
    )


def restore_session_enabled():
    """Warp's own session-restore setting: True, False, or None if unreadable.

    ``general.restore_session`` is documented in Warp's settings schema as
    "Whether to restore the previous session when Warp starts up", type
    boolean, **default true** -- read out of ``warp-terminal
    dump-settings-schema`` on this machine rather than taken from docs. So an
    absent key means enabled, which is why this returns True when the file
    exists and says nothing about it.
    """
    if not os.path.isfile(SETTINGS_PATH):
        # No settings file at all: Warp has never written one, so every
        # setting is at its default and restore_session is on.
        return True
    try:
        with open(SETTINGS_PATH) as f:
            text = f.read()
    except OSError:
        return None
    return _parse_restore_session(text)


def _parse_restore_session(text):
    """Find general.restore_session in settings.toml, defaulting to True.

    Hand-rolled rather than via a TOML parser: Python 3.10 is what this tool
    runs under (/usr/bin/python3, deliberately -- see the README's note) and
    tomllib only arrives in 3.11, so importing it would make Warp support
    depend on a newer interpreter than the rest of the tool needs. Only one
    key is ever read, and it is a bare boolean in a known table.
    """
    section = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != "general" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "restore_session":
            return value.strip().lower() == "true"
    return True


# Warp's bash bootstrap sources *login* files, not ~/.bashrc. Read out of the
# shipped binary rather than assumed: it sources /etc/profile (falling back to
# /etc/bash.bashrc), then the first of these that exists, and nothing else.
# gnome-terminal here is the exact opposite -- its profile is login-shell=false,
# so it reads ~/.bashrc and never ~/.profile.
LOGIN_FILES = ("~/.bash_profile", "~/.bash_login", "~/.profile")

BASHRC = os.path.expanduser("~/.bashrc")


def login_file():
    """The login file Warp will source, or None if the user has none.

    First match wins, which is bash's own rule and the order Warp's bootstrap
    implements.
    """
    for rel in LOGIN_FILES:
        path = os.path.expanduser(rel)
        if os.path.exists(path):
            return path
    return None


def hook_reaches_panes():
    """Can the ~/.bashrc hook actually load in a Warp pane? (ok, explanation)

    This is the one piece of Warp support that does not follow from the
    database, and it is worth a check of its own because its failure mode is
    the silent kind this project has been bitten by repeatedly: the hook would
    keep working perfectly in gnome-terminal while capturing nothing in Warp,
    and a pane with no conda record is indistinguishable from a pane with no
    conda env.

    The chain has two hops. Warp sources a login file; on Ubuntu ~/.profile
    then sources ~/.bashrc, which is where the hook line lives. So the hook
    reaches Warp only because ~/.profile happens to do that -- verified on this
    machine, where ~/.profile carries the distribution's stock
    `if [ -n "$BASH_VERSION" ]; then . "$HOME/.bashrc"` block and neither
    ~/.bash_profile nor ~/.bash_login exists to shadow it.

    Creating a ~/.bash_profile later is enough to break it, since bash and Warp
    both stop at the first login file they find. That is a normal thing to do
    for unrelated reasons, which is exactly why this is checked rather than
    assumed to stay true.
    """
    path = login_file()
    if path is None:
        return False, (
            "Warp sources a login file (~/.bash_profile, ~/.bash_login or "
            "~/.profile) and you have none, so ~/.bashrc -- and the sessionctl "
            "hook in it -- never loads in a Warp pane. Warp panes will be "
            "captured from Warp's own database, but without their conda envs."
        )
    try:
        with open(path) as f:
            text = f.read()
    except OSError as e:
        return False, f"could not read {path} ({e})"

    if "bashrc" not in text:
        return False, (
            f"Warp sources {path}, which does not source ~/.bashrc, so the "
            "sessionctl hook never loads in a Warp pane -- its cwd still comes "
            "from Warp's own database, but its conda env cannot be captured. "
            f"Add `[ -f ~/.bashrc ] && . ~/.bashrc` to {path} to fix it."
        )
    return True, f"reaches Warp panes via {path} -> ~/.bashrc"
