# Plan: Restore mosh + tmux Terminal Tabs

**Date:** 2026-11-13; revised 2026-11-16.
**Status:** phase 1 implemented in the working tree; automated verification complete.
Live remote reconnect, window placement and reboot verification remain pending.
**Scope:** GNOME Terminal capture, snapshots, command display, restore, shell hook and placement.
Warp is phase 2 and remains an investigation. SSH and local tmux are outside this change.

## Goal

Restore GNOME Terminal tabs that ran `mosh <server> -- tmux new -A -s <session>`.
The laptop starts a new mosh connection to the tmux session still running on the server.
Processes survive a laptop reboot because they run on the server. A server reboot ends them.

Tests and documentation use `example.test` or `<server>`. Actual connection addresses belong in
runtime snapshots under the user's state directory, outside the repository. Credentials never belong here.

## Where things stood on November 13

The laptop had 18 GNOME Terminal tabs, including 11 mosh clients. The server had 100 mosh-server
processes and several tmux sessions with multiple clients from previous laptop boots. Both machines
had tmux 3.2a and mosh 1.3.2. Warp had no mosh tabs.

Sessionctl saved only the local directory and conda environment. All 11 mosh commands had to be
entered again after restoring. The local hook stops seeing prompts while mosh runs, and AT-SPI
reads the remote prompt as if it were local. However, it can also read tmux's default status bar,
which identifies the remote session and can distinguish otherwise identical terminal windows.

These are historical measurements, not current session counts.

## Revision decisions

1. **Validate twice.** Validate at collection and again when building restore commands. Snapshots can be
   edited, and a `resume` field must never become an arbitrary command execution facility.
2. **Use a small grammar.** Accept a hostname or IPv4 address, an optional username, `-- tmux`, and
   explicit new/attach commands with a simple session name. Reject mosh options, quoted arguments,
   shell commands, IPv6 literals, detached creation and window/pane target syntax. Broader support
   needs separate tests because mosh's process title loses argument boundaries.
3. **Omit rejected command text.** Store only `{"kind":"mosh","replay":false}` for unsupported mosh
   commands. Raw text may contain credentials or an arbitrary command. Show a manual reconnect notice.
4. **Make detachment explicit.** Ordinary restore preserves clients on other devices. `restore --detach-mosh`
   adds `-D` to the normalized tmux new-session command, detaching ALL other clients of that session.
   This clears stale mosh-server clients but also disconnects live clients on other devices.
5. **Pace connections.** Use delays of 0, 1, 2, … seconds across reconnectable tabs in all windows.
   This reduces simultaneous authentication; it does not guarantee staying below sshd's MaxStartups
   limit when authentication takes longer than the interval. Failure returns to a local prompt for retry.
6. **Preserve pending reconnects.** Write the state file before waiting or connecting. Include a validated
   pending command during startup so a periodic save cannot erase the reconnect intent during the delay.
7. **Wait for reliable placement.** Require the full set of tmux session names and tab count for matching
   remote windows. If status bars are unavailable, leave them unplaced and allow `sessionctl place` later.
8. **Keep server maintenance separate.** Never infer that an older client is orphaned merely from its age.
   No automatic server-wide cleanup, timeout changes or remote process termination.

## Design

### Capture and validation (`remote.py`, `collect.py`)

Read the terminal shell's foreground process group from `/proc/<pid>/stat`, parsing after the last
`)` because process names can contain spaces and parentheses. Inspect only direct children in that group.
A child named `mosh-client` is parsed from its cmdline; its environment is never read. Process exits and
permission failures degrade to no detection without failing the save.

Mosh 1.3.2 puts the original invocation into argv[1] as `-# <space-joined arguments> |`.
Only this exact shape is accepted. The allowed command forms are:

```text
mosh [user@]host -- tmux new [-A] [-D] -s NAME
mosh [user@]host -- tmux new-session [-A] [-D] -s NAME
mosh [user@]host -- tmux attach [-d] -t NAME
mosh [user@]host -- tmux attach-session [-d] -t NAME
mosh [user@]host -- tmux a [-d] -t NAME
```

Flags precede the target and cannot repeat. NAME contains letters, digits, underscores and hyphens,
starts with a letter, digit or underscore, and has at most 128 characters. Hostnames and usernames
use a similarly restricted character set. All accepted forms normalize to
`mosh [user@]host -- tmux new-session -A -s NAME`. Original detach flags are not silently carried over;
only `--detach-mosh` requests detachment on restore.

The tab gains `resume = {kind, args, tmux_session}`. Local tabs have no `resume`. An unsupported foreground
mosh client gets the nonreplayable marker. A hook's pending command is validated with the same grammar.
SSH port forwards and other processes are never replayed.

### Snapshot and display (`snapshot.py`, `cli.py`)

The restore digest includes `resume`: starting, stopping or switching a mosh session changes the snapshot.
`show` prints `mosh <host> → tmux <session>` for supported commands and a manual reconnect notice otherwise.
Old snapshots have no reconnect metadata and keep their previous behavior. The first save after upgrade may
write a new digest even for an otherwise unchanged local session because the digest schema now includes resume.

### Restore and shell hook (`restore.py`, `bash-hook.sh`)

`_tab_script` validates snapshot arguments again before passing a canonical command through
`SESSIONCTL_RESUME`, alongside `SESSIONCTL_RESUME_DELAY` and the existing conda restore variable.
Malformed metadata produces an ordinary interactive shell.

The hook consumes and unsets reconnect environment variables before invoking children. It records its state
before the first prompt action, waits the delay, adds the command to Bash history, and runs a Bash argv array.
There is no `eval` or command substitution. When mosh returns, it clears pending metadata and updates the
state file. The original interactive shell stays the terminal server's child, satisfying capture liveness checks.
Subsequent prompts do not reconnect again. Up-arrow can retry a failed connection.

The one-second delay counter advances only for supported mosh tabs and continues across windows.
`--detach-mosh` affects only those commands. Normal restore requires no server configuration changes.

### Grouping and placement (`collect.py`, `layout.py`)

Read the last 600 characters of the accessible terminal text, as before. Recognize tmux's default status
bar prefix `[NAME] 0:`; emit only the name as `slot.tmux`. If it is present, suppress remote prompt-derived
cwd and conda. Match records by session name before local cwd/env matching.

Record sorted tmux names in terminal window signatures. During placement, require those names and tab count
to agree. This applies after the settle grace too, so slow logins cannot cause a guess against another window.
Existing timeouts still bound placement; after connecting, run `sessionctl place` again if necessary.

Status bars customized away from the default pattern, duplicate names on different hosts, and inaccessible
AT-SPI make identity ambiguous. Those cases need better terminal-to-screen identity before claiming exact placement.

## Implementation steps

1. Add shared parsing, validation and foreground discovery in `remote.py`.
2. Add resume metadata to collection, snapshot digests and `show`.
3. Add validated restore assignments, global reconnect delays and `--detach-mosh`.
4. Add the first-prompt reconnect action, pending metadata and history support to the shell hook.
5. Add status-bar grouping and remote window signatures, with conservative placement.
6. Add parser, process-race, snapshot, restore and real Bash startup tests; update documentation and AI history.
7. Deploy using `sessionctl setup`. The tool deliberately runs from an installed copy rather than this checkout.
8. Verify real connections with a dedicated scratch session, then placement and a user-initiated reboot.

## Verification

Automated checks cover:

- Accepted aliases, canonicalization and opt-in detachment.
- Rejection of shell syntax, options, trailing commands, malformed snapshots and detached creation.
- Foreground/direct-parent checks, process names containing parentheses, process exits and missing `/proc` files.
- Collection of pending metadata, digest changes and grouping by session name.
- Missing/wrong tmux signatures remaining unplaced even outside the settling period.
- Restore argument quoting and stagger numbering across windows.
- A real interactive Bash in a pseudo-terminal with a stub mosh executable: history order, state written before
  execution, reconnect variables absent from child environments, exact argv, one execution, cleared pending state,
  and state-file cleanup when the shell exits.
- Full unit suite, credential check, `bash -n` and `git diff --check`.

Live checks, pending:

1. Capture currently running mosh tabs with pruning disabled; compare counts and names with the desktop.
2. Inspect a restore dry run. Do not replay an entire desktop merely to test command generation.
3. Connect a throwaway tab to a dedicated scratch tmux session; detach and verify return to a local prompt.
4. Repeat with `--detach-mosh` only on that scratch session and confirm the previous mosh-server exits.
5. Verify placement with the remote tabs connected. Test a delayed/unavailable network: unconfirmed windows
   should remain unplaced, and manual retry followed by `place` should work.
6. After a user-initiated laptop reboot, restore and compare remote sessions, windows and server counts.

## Warp (phase 2)

Warp's native restore reopens panes but does not replay their commands. Implementing this for Warp requires:

- W1: prove pane UUIDs survive a quit/reopen cycle before using a UUID-indexed reconnect file.
- W2: prove a first-prompt full-screen reconnect is compatible with Warp's shell integration.

Neither experiment is covered by the GNOME phase. The hook mechanism exists but there is no Warp capture/replay
wiring. Do not claim Warp support from the GNOME tests.

## Deployment and results

Implemented and deployed on November 16 with `sessionctl setup`. All 355 tests pass, including 14 new tests.
The installed modules and hook were compared with the checkout. A read-only desktop capture found 3 windows
with 19 tabs, and no running mosh clients, so it could not confirm a real reconnect command against live tabs.
Live remote reconnect, window movement and reboot checks remain pending. No server cleanup was performed.
The November 13 observation that the installed copy lagged the repository is historical.

## References

- [Mosh FAQ](https://mosh.org/): persistent terminal sessions belong in a remote multiplexer.
- [mosh-server manual](https://man.archlinux.org/man/mosh-server.1.en.txt): timeout behavior.
- Installed mosh 1.3.2 wrapper, `/usr/bin/mosh`: synthetic process-title construction.
- [Main session restore plan](session_restore_plan.md).
