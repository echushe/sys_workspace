"""Conservative mosh/tmux replay metadata. Never read mosh environments or keys."""
import os
import re
from pathlib import Path

# Only arguments which survive mosh's lossy space-joined process title unchanged.
HOST = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
SESSION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,127}\Z")
UNSUPPORTED = {"kind": "mosh", "replay": False}


def validate_args(args):
    """Return normalized argv, or None. Used at capture AND replay boundaries.

    Deliberately excludes mosh options, remote shell commands, tmux command
    arguments, targets like session:window, detached creation and shell syntax.
    """
    if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        return None
    if len(args) < 7 or args[0] != "mosh" or not HOST.fullmatch(args[1]):
        return None
    if args[2:4] != ["--", "tmux"]:
        return None
    verb = args[4]
    if verb in ("new", "new-session"):
        target, flags = "-s", {"-A", "-D"}
    elif verb in ("attach", "attach-session", "a"):
        target, flags = "-t", {"-d"}
    else:
        return None
    tail = args[5:]
    if len(tail) < 2 or tail[-2] != target or not SESSION.fullmatch(tail[-1]):
        return None
    opts = tail[:-2]
    if len(opts) != len(set(opts)) or any(x not in flags for x in opts):
        return None
    # new without -A is not idempotent. Normalize both forms to new -A.
    return ["mosh", args[1], "--", "tmux", "new-session", "-A", "-s", tail[-1]]


def parse_mosh(argv):
    """Parse mosh-client's synthetic title, without retaining rejected text."""
    if (not isinstance(argv, list) or len(argv) != 4
            or not all(isinstance(x, str) for x in argv)
            or os.path.basename(argv[0]) != "mosh-client"
            or not argv[1].startswith("-# ") or not argv[1].endswith(" |")):
        return dict(UNSUPPORTED)
    raw = argv[1][3:-2]
    args = validate_args(["mosh"] + raw.split(" "))
    if args is None:
        return dict(UNSUPPORTED)
    return {"kind": "mosh", "args": args, "tmux_session": args[-1]}


def replay_args(resume, detach=False):
    if not isinstance(resume, dict) or resume.get("kind") != "mosh" or resume.get("replay") is False:
        return None
    args = validate_args(resume.get("args"))
    if args is None or resume.get("tmux_session") != args[-1]:
        return None
    if detach:
        args.insert(6, "-D")
    return args


def _stat(pid, proc):
    # comm may contain spaces and parentheses; fields after its final ')' start at 3.
    fields = (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[1]), int(fields[2]), int(fields[5])  # ppid, pgrp, tpgid


def foreground_mosh(pid, proc=Path("/proc")):
    """Only a direct foreground child; tolerate races with exiting processes."""
    try:
        _, shell_group, foreground = _stat(pid, proc)
        if foreground <= 0 or foreground == shell_group:
            return None
        children = (proc / str(pid) / "task" / str(pid) / "children").read_text().split()
        for child in children:
            try:
                parent, group, _ = _stat(child, proc)
                if parent != pid or group != foreground:
                    continue
                if (proc / child / "comm").read_text().strip() != "mosh-client":
                    continue
                raw = (proc / child / "cmdline").read_bytes()
                argv = raw.rstrip(b"\0").decode("utf-8", "strict").split("\0")
                return parse_mosh(argv)
            except (OSError, ValueError, IndexError):
                continue
    except (OSError, ValueError, IndexError):
        pass
    return None
