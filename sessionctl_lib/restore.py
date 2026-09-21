"""Replay a snapshot: relaunch terminals, VSCode windows, and Chrome.

Building the command lines is kept separate from running them (terminal_commands
/ vscode_commands / chrome_commands vs. launch) so the whole plan can be printed
and reviewed under --dry-run without side effects, and so the argv-construction
logic is unit-testable without ever touching a real process.

Firefox is deliberately not launched here. The plan's capture side only records
whether it was running and which profile it would resume -- Firefox restores its
own tabs on relaunch, and since it's an unused, untested snap on this machine
(see the plan's limitations), auto-launching it is not something to add without
ever having verified its restore behaviour.
"""

import os
import shlex
import subprocess

TERMINAL_APP = "gnome-terminal"
VSCODE_APP = "code"
CHROME_APP = "google-chrome"
WARP_APP = "warp-terminal"


def _tab_script(tab):
    """The command a restored tab runs: re-enter its conda env, then an
    interactive shell. Empty/whitespace conda names are treated as "none" --
    collect_terminals stores "" as null already, but a snapshot loaded from
    disk is just JSON and this stays defensive against a blank string too.
    """
    conda = (tab.get("conda") or "").strip()
    if conda:
        return f"SESSIONCTL_CONDA={shlex.quote(conda)} exec bash -i"
    return "exec bash -i"


def terminal_commands(terminals):
    """One gnome-terminal argv per recorded window.

    gnome-terminal only honours ONE trailing "-- COMMAND" for the whole
    invocation -- confirmed live: a second "--tab ... -- bash -c ..." block
    does not run as its own command, it is silently swallowed as extra
    positional arguments to the first tab's command, and every tab after the
    first is simply never opened. --command=STRING is the one flag that
    genuinely works per-tab here, despite gnome-terminal printing a
    deprecation notice suggesting "--" instead -- which does not actually
    support this. There is no non-deprecated way to do this.
    """
    commands = []
    for window in terminals.get("windows", []):
        tabs = window.get("tabs", [])
        if not tabs:
            continue
        argv = [TERMINAL_APP]
        for i, tab in enumerate(tabs):
            argv.append("--window" if i == 0 else "--tab")
            cwd = tab.get("cwd")
            if cwd:
                argv.append(f"--working-directory={cwd}")
            argv.append("--command=" + "bash -c " + shlex.quote(_tab_script(tab)))
        commands.append(argv)
    return commands


def vscode_commands(vscode):
    """`code <path>` for the first window, `code -n <path>` for the rest.

    Without -n, a second `code` invocation folds into the first instance's
    existing window rather than opening a separate one (confirmed via
    `code --help`: -n/--new-window "Force to open a new window").
    """
    commands = []
    for i, w in enumerate(vscode.get("windows", [])):
        path = w.get("path")
        if not path or not os.path.isdir(path):
            continue
        argv = [VSCODE_APP]
        if i > 0:
            argv.append("-n")
        argv.append(path)
        commands.append(argv)
    return commands


def chrome_commands(chrome, use_recorded_urls=False):
    """One google-chrome argv per profile that has recorded session data.

    By default this just relaunches Chrome per profile and relies on its own
    "continue where you left off" (set by `sessionctl setup`, step 6). With
    use_recorded_urls, the profile's captured URL list is passed explicitly
    instead -- the fallback for when that preference was never set or the
    session file was lost. --chrome-urls is deliberately best-effort, as
    documented on the collector side: SNSS is a binary format with no
    version-stable way to separate a tab's real URL from referrer/iframe
    noise, so this list may include more than what was actually open as tabs.
    """
    commands = []
    for p in chrome.get("profiles", []):
        profile = p.get("profile")
        urls = p.get("urls", [])
        if use_recorded_urls and not urls:
            continue
        argv = [CHROME_APP]
        if profile and profile != "Default":
            argv.append(f"--profile-directory={profile}")
        if use_recorded_urls:
            argv += urls
        commands.append(argv)
    return commands


def warp_commands(warp_state, running=False, force=False):
    """At most one argv: launch Warp so it can restore its own session.

    Warp is the one terminal here that needs no reconstruction. Its
    ``general.restore_session`` setting -- boolean, documented in Warp's own
    settings schema as "Whether to restore the previous session when Warp
    starts up", and **default true** -- rebuilds every window, tab, pane,
    split and cwd from the same ``warp.sqlite`` the collector reads. So the
    whole restore is "start Warp", and everything sessionctl recorded is there
    to place those windows on the right GNOME workspace afterwards, which is
    the part Warp itself has no concept of.

    `running` is whether Warp is running *now*, which the caller supplies
    (cli passes warp.running()). It deliberately does not come from the
    snapshot: the snapshot records whether Warp was running when the session
    was *saved*, which after a reboot is almost always true and would mean
    Warp never got launched at all -- the one case this whole function exists
    for. Keeping the live lookup in the caller also keeps this module free of
    process inspection, so every argv it builds stays unit-testable and
    --dry-run stays side-effect-free.

    Nothing is launched when Warp is already running. There is no flag or URI
    to ask a running Warp for a pane at a path (checked: ``--help`` exposes no
    window/tab options, and the binary carries only ``warp://cli`` and
    ``warp://linear``), so a second invocation cannot add the recorded panes
    to a live instance -- it would at best raise another window sessionctl
    then has to place, at worst nothing at all. `force` overrides that for the
    case where you have quit Warp yourself between `save` and `restore`; it
    exists because "is it running" is a race, not a certainty.

    Returns [] when the snapshot recorded no Warp session at all, so a desktop
    that never used Warp never launches it.
    """
    state = warp_state or {}
    if not state.get("windows"):
        return []
    if running and not force:
        return []
    return [[WARP_APP]]


def launch(argv):
    """Launch one command, detached, without waiting for it to exit.

    All three target apps (gnome-terminal, code, google-chrome) hand off to a
    long-running instance and return quickly on their own, but launch()
    doesn't wait regardless -- a restore should never be able to hang on an
    app that, this once, takes longer to start. stdout/stderr are discarded;
    gnome-terminal's own deprecation notice for --command is expected noise,
    not something a restore run needs to surface.
    """
    subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
