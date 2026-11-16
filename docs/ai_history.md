# AI History

Jobs done by Claude Code and Codex on sessionctl between 2026-08-24 and 2026-11-16, oldest first.
Each entry covers one job, or one day of closely related jobs, in roughly 100–250 words.

sessionctl did not start in this repository. From 2026-08-24 to 2026-09-21 it lived in
`~/Projects/finance/stock_03_claude`, as `sys_workspace/` with its plan in
`docs/sys_workspace/`. On 2026-09-21 it was removed from there (`fd536a1`) and committed
here as `9ecd99b`. The code, tests, systemd units and bash hook are byte-identical to
what was removed. Only the README, the plan and `.gitignore` were edited. **Commit
hashes in entries before 2026-09-21 refer to `stock_03_claude`'s history, not this
repository's.**

Sources:

- the 27 commits in `stock_03_claude` that touch sessionctl and carry a Claude
  `Co-Authored-By` trailer: 26 by Claude Opus 5 and one, `4bc6e53`, by Claude Sonnet 5;
- the one such commit in this repository, `583e540`, by Claude Opus 5.5;
- `stock_03_claude/docs/ai_history.md`, which drew on saved session logs and is the only
  source for the 2026-09-15 entry;
- the log of the session that wrote this file (`7e46bda2`, 2026-11-13 to 2026-11-14),
  which is the source for every entry from 2026-11-13 on.

No earlier Claude Code session logs for sessionctl are kept on this laptop, so the other
entries are rebuilt from commit messages. Commits with neither a trailer nor a log are
listed at the end rather than credited. Dates are each commit's own local date: AEST
(UTC+10) except `07c1759` and `583e540`, which carry UTC+8.

## 2026-08-24 — The plan

Wrote the session-restore plan, `docs/sys_workspace/session_restore_plan.md` in
`stock_03_claude`, now [`session_restore_plan.md`](session_restore_plan.md) here. The
target was to bring back terminal tabs with their working directory and conda env, VSCode
windows, Chrome tabs and each window's workspace after a reboot. `adde12b` renamed the
plan directory and added an empty `sys_workspace/` for the code. `3fce0bf` added write
avoidance, since the save timer runs all day. Chrome and VSCode files are re-scanned only
when their mtime or size changes. The digest covers only what a restore would use, so
window titles, PIDs and timestamps are left out. An unchanged digest writes nothing but a
small heartbeat file. As a side effect, the 20-entry snapshot ring holds 20 distinct
desktop states instead of the last 40 minutes of idling. `ba50446` removed a second,
conflicting numbered list, so "do step 4" had only one meaning. `6c65cf7` recorded that the
code lives in `sys_workspace/` and not in a separate repository, as the user had asked.

## 2026-08-24 — Steps 1–4: mounting, terminal capture, VSCode and Chrome, restore

- **Stage 0, `40bf0d8`.** `~/Projects` and `~/extra_disk_2t` point into a partition that
  is not in `/etc/fstab` and does not mount at boot. `sessionctl detect` derives the
  mount list, and `sessionctl mount` mounts it with `udisksctl`. Mounts are checked by
  UUID, so an empty mountpoint directory does not pass as mounted. The mount branches are
  stubbed in tests, because the partition holds the running session itself.
- **Terminal capture, `e6ec329`.** Added `bash-hook.sh`, since `conda activate` never
  reaches `/proc/<pid>/environ`. Two bugs were found by real tests. `/proc/<pid>/comm`
  truncates `gnome-terminal-server` to 15 bytes. Snapshots taken within the same second
  overwrote each other.
- **VSCode and Chrome, `c250569`.** VSCode is read from `storage.json` and Chrome from its
  newest `Session_*` file. Two more bugs: a URL regex that ran on into binary data, and a
  file choice that depended on mtime ties.
- **Restore, `b26c009`.** gnome-terminal honours only one trailing `-- COMMAND` per
  launch, which silently dropped every tab after the first, so each tab uses `--command=`.
  The first real restore was run only after the user agreed (`d22e30e`).

The test count went from 12 to 84.

## 2026-08-24 — Steps 5–6: window layout and systemd units

Built `layout.py` (`e0a4736`). It records each window's workspace, geometry, maximized and
sticky state through Wnck, and `sessionctl place` puts windows back. Testing the write
side found two bugs no read-only test could reach. Wnck's actions do nothing until the
GLib main loop is pumped. `set_geometry` straight after `unmaximize` lands tens of pixels
short. An idea to tag terminals with `--title` at launch was dropped, because the prompt
overwrites the title on the first command. Then came step 6 (`c37af0a`): the save timer
and service, a logout snapshot service, and `sessionctl setup` / `uninstall`. Setup
symlinks the command and units into place and sets Chrome's `restore_on_startup`. It
refuses to edit Chrome's preferences while Chrome is running, because Chrome would
overwrite the edit on exit. Every symlink step refuses to replace a file that is not
already a symlink. Enabling the timer and a real `setup` run were held for the user's
confirmation. 130 tests.

## 2026-08-24 — First reboots: the tool could not reach its own disk

Each real reboot that evening found a failure one layer deeper than the last.

- **`8676658`.** The timer never fired. `systemd --user` loads unit files at login, but
  the symlinks pointed into the data partition, which was not mounted yet. systemd
  marked the units not found for the rest of the session. The units are now copied.
- **`ef96f3c`.** Corrected a guess in the README. The agent had blamed the mount 76
  seconds after login on VSCode, but the user had mounted the disk by hand.
- **`ea2b2bd`.** `ExecStart` went through a symlink into the same partition, so every
  timer run before the mount failed with `status=203/EXEC`. A generated shell wrapper
  now checks the target exists before running it.
- **`4bc6e53`, Claude Sonnet 5.** The wrapper only made the failure readable. The mount
  code lived on the disk it existed to mount. `setup` now copies the whole package to
  `~/.local/share/sessionctl/lib/` and links the command there. The entry script switched
  from `abspath` to `realpath` so it finds that copy. Checked live: `sessionctl mount`
  worked with the disk unmounted.

146 tests.

## 2026-08-26 — Empty restores after reboots

- **`5c0ea06`.** A restored terminal came back small. Every terminal title is identical
  once a prompt is drawn, so the record was matched to the terminal `restore` was run
  from. `restore` now notes the windows that existed before launching and prefers new
  ones.
- **`35f7c59`.** `restore` brought back no terminals, from three faults together.
  `Gtk.init()` killed the logout snapshot instead of raising an error; it now uses
  `init_check`. The hook was still a symlink into the unmounted disk; it is now a copy.
  An empty capture overwrote a real one; `save` now refuses when terminal windows are open
  but no tabs were captured.
- **`8b11e7a`.** The timer fired zero times in 75 minutes. For a user timer, `OnBootSec=`
  counts from machine boot, and the login came 2.5 minutes after boot. `OnStartupSec=`
  was added, and the inert `Persistent=yes` removed.
- **`66b11d0`.** At the user's request, the timer now saves every 30 s, with
  `AccuracySec=1s` so the interval holds.
- **`b0a13ee`.** The `init_check` fix let the logout snapshot succeed. It ran after every
  shell had exited, so it wrote an empty session three seconds after a good periodic
  save. The unit was deleted and is torn down on older installs. `save` also refuses when
  it cannot see any windows but the previous snapshot had some.

166 tests.

## 2026-08-27 — The boot guard and the real tab-to-window mapping

- **`787b8ed`.** After a reboot, the timer captured the fresh one-tab desktop 47 seconds
  after login and replaced the session waiting to be restored. Snapshots now record the
  kernel boot id. `save` would not overwrite an earlier boot's session until `restore` or
  a new `sessionctl adopt` ran; `save --force` overrode it. This guard is the one replaced
  on 2026-10-11.
- **`ae5ea97`.** Windows of 5, 6 and 7 tabs came back as three windows of 6, because the
  plan had decided the real mapping was unavailable and split tabs evenly. The
  accessibility tree does have it: one frame per window, one page tab per tab. The probe
  runs in a subprocess, because `Atspi.init()` kills the process when the accessibility
  bus is down. The service gained `TimeoutStartSec=60`.
- **`e28cb17`.** Tab counts were right but conda envs landed in the wrong windows. The env
  is read from each tab's last prompt line, and matching uses two passes so exact matches
  win.
- **`bdfddfb`.** Terminal windows came back on each other's workspaces. Each window now
  carries a signature of its tab count and tab directories.

204 tests.

## 2026-08-28 — Placement waits for workspaces to exist

Every window came back on workspace 0. After a reboot only workspace 0 and one empty
workspace exist, and with dynamic workspaces, workspace N appears only once 0 to N−1 are
in use. Terminals appear in under a second and VSCode takes several. So the first pass
asked for workspaces 2–5 before they could exist, skipped them without saying so, and
reported "placed 8 of 8". `place()` now retries a record whose workspace does not exist
yet (`8d90a6f`). The timeout rose from 20 s to 45 s. A 15 s idle exit stops waiting for
windows that will never appear, such as two Awesun windows in the snapshot. That cut a
real restore from 45.3 s to 19.4 s. Verified by moving every window to workspace 0 and
replaying the pre-reboot snapshot. 211 tests.

## 2026-09-09 — Multiple monitors

- **`3002294`.** Two five-tab terminal windows swapped workspaces. A tab that is running
  a command has no prompt, so the window signature lost its distinctive directory. The
  signature now uses the directories the hook recorded. For a short settle period,
  matching also waits instead of breaking ties by list order. A second bug: windows on a
  second monitor have no workspace, and capture recorded that as sticky. Restoring
  pinned them to every workspace. Capture now asks Wnck directly. A window recorded on a
  monitor that is no longer attached is moved back onto the screen.
- **`e109098`.** With four monitors, VSCode windows landed on each other's displays.
  VSCode titles lead with the open file, so title similarity preferred the wrong window
  (0.696 against 0.639). Windows are now matched by the folder part of the title. The
  settle period rose from 8 s to 20 s.

Left as found: eight windows that the pinning bug had made sticky stay sticky in later
snapshots. 239 tests.

## 2026-09-15 — Deployment and mounting broken home links

This entry comes from `stock_03_claude/docs/ai_history.md`, which was based on session
logs. The user committed the work there as `07c1759`, without a trailer. sessionctl was
deployed for real: the package went to `~/.local/share/sessionctl/`, the 30 s timer was
enabled, and Chrome was set to continue where it left off. At the user's request, the
`~/.bashrc` hook line was installed and `~/.local/bin` was added to `PATH`. Then a scan was
added for dangling symlinks at the top level of `$HOME`. Each target under `/media/<user>/`
is mapped back to its disk by label or UUID and mounted, and a missing external disk is
skipped instead of failing. After the user clarified, the scan runs only in `sessionctl
mount`, not in `restore` or the timer. To verify, all five disks were unmounted, and
`sessionctl mount` brought them all back. 246 tests.

## 2026-10-11 — Pin the pre-reboot session instead of pausing saves

The first commit by Claude in this repository (`583e540`, Claude Opus 5.5). It reached this
checkout on 2026-11-13 through a `git pull`. The 2026-08-27 boot guard paused every save
until `restore` or `adopt` ran, and nothing else ever lifted it. One reboot without a
restore on 2026-09-15 stopped saving for 26 days: 72,777 refused saves across three
boots. The restore on 2026-10-11 then replayed a one-tab session from 15 September, and
nothing opened after 27 September had been captured. The fix:

- `save` always runs. The first save of a new boot copies the old `latest.json` to
  `previous_boot.json`, and `restore` uses that copy by default
  (`snapshot.restore_target`).
- `adopt` and `save --force` were removed, since they existed only to lift the guard.
- The staleness warning now checks whether saving ran until the previous boot shut down
  (`journalctl -b -1`), instead of warning after every shutdown longer than an hour.
- `restore` and `list` say which snapshot they mean. `--snapshot` accepts `latest` and
  `previous`.

7 files, +610/−339. The suite went from 316 to 326 tests.

## 2026-11-13 — Workspace overview and this file

*Claude Opus 5.5, session `7e46bda2`.* The user asked for an overview of the workspace.
The agent read the code, the README and the plan. It ran the suite, 316 tests at the
time, and checked that the installed copy and bash hook matched the repo and that the save
timer was enabled and active. The user then pulled `583e540`, and the agent wrote this
file at their request. It followed the format of `stock_03_claude/docs/ai_history.md`,
which already covered sessionctl's first month. Entries were rebuilt from the 27 Claude
commits in `stock_03_claude`, the one here, and that file's 2026-09-15 entry. Each was
checked against the commit it cites. The agent also confirmed that `9ecd99b` moved the
code over unchanged.

Found while writing: the installed copy in `~/.local/share/sessionctl/lib/` is now behind
the repo. `cli.py`, `paths.py` and `snapshot.py` differ, so the installed `sessionctl` still
runs the boot guard that `583e540` removes, until `sessionctl setup` is run again. At the
user's request, the file was committed the same day, together with a link from the
README.

## 2026-11-13 — Credential check of the whole repository

*Claude Opus 5.5, same session.* The user set a rule: no login credentials (keys, tokens,
passwords) in this repository. They asked for a check. No secret scanner was installed
(gitleaks, trufflehog, detect-secrets), so the agent wrote one that matches known key and
token formats and flags long random-looking strings. It scanned:

- all 48 files on disk, including ignored ones;
- all 44 distinct file versions in the 4 commits, on every branch and in the reflog;
- every commit message;
- `.git/config` and the hooks folder.

There were no unreachable objects. Nothing matched a credential pattern. The only
random-looking strings were paths that contain the data partition's UUID. 50 lines used
words such as "login" or "password", and all were prose about login shells or PolicyKit
prompts. The agent noted what is public without being a credential: the partition UUID,
the username in paths, and commit author details. It also found that `master` had been
fast-forwarded to `ff2922f` and pushed from VS Code at 16:07, outside the session. The
rule was saved to this project's Claude memory.

## 2026-11-13 — Commit hooks that block credentials

Added `githooks/pre-commit` and `githooks/commit-msg`, which both run
`githooks/credential_check.py`. The first scans the lines a commit adds and the names of
files it adds, such as `id_ed25519`, `*.pem` and `.env`. The second scans the commit
message. Matches are printed masked, so a real secret is not echoed. A reviewed line can
carry an allow marker, and `--no-verify` skips both hooks.

`tests/test_credential_check.py` adds 15 tests, and builds its samples from fragments so
the file passes its own check. Removing one rule made three of those tests fail. One of
the tests fails if any tracked file matches. Run over the whole history, the check found
nothing. Its first version did flag the example credentials in its own comments, which
were rewritten.

In a throwaway clone, the hooks blocked a file holding a token, a file named `id_ed25519`
and an AWS key in a commit message. They let through a line with the allow marker and the
real change. They were turned on here with `git config core.hooksPath githooks`, and the
README gained a "Commit hooks" section. 341 tests.

Committed on 2026-11-14 as `e194e42` on a new branch, `credential-hooks-and-mosh-plan`. An
empty commit with a fake token in its message was then refused, which showed the hooks run
in this checkout.

## 2026-11-13 — Can mosh + tmux tabs be restored?

The user asked whether gnome-terminal or Warp tabs could be restored with mosh + tmux.
11 of the 18 gnome-terminal tabs already ran `mosh <server> -- tmux new -A -s <name>`, for
sessions `mnt_0`, `mnt_1`, `mnt_4` and `daily_0`–`daily_7`. `latest.json` recorded all 18
tabs as `~` with env `base`, so a restore would have brought back local shells. The
accessibility probe also read the server's prompt as the tab's own.

- **Status bar.** tmux's status bar, read through the same probe, named each tab's session
  exactly.
- **Server.** A read-only look found 100 mosh-servers dating from Nov 2 to Nov 13, and
  sessions with up to 7 clients attached. With `MOSH_SERVER_NETWORK_TMOUT` unset, every
  laptop reboot had left its mosh-servers behind.
- **Orphan test.** In a local test, killing a mosh client left its server running and
  attached to tmux. Detaching that tmux client made the server exit within a second, and
  the session survived.
- **Warp.** Warp had no mosh, ssh or tmux use. Its own restore does not re-run commands,
  and its tmux-based SSH mode has been removed. Whether its pane IDs survive a restart
  could not be shown.

Verdict: feasible for gnome-terminal, unproven for Warp, and not worthwhile with local
tmux.

One mistake: the agent's version check `mosh-server -V` started a real mosh-server on UDP
60001 for about a minute and printed its session key into the session. The agent stopped
it at once and told the user.

## 2026-11-13 — The mosh + tmux restore plan

Wrote [`mosh_tmux_restore_plan.md`](mosh_tmux_restore_plan.md). Several things were checked
first, and each one shaped the design:

- **Command line.** mosh passes the original command to `mosh-client` as one space-joined
  `-# … |` argument, so quoting is lost and only a strict tmux new/attach shape is
  replayed.
- **History.** `history -s` while `~/.bashrc` runs lost the shell's loaded history, while
  the same call at the first prompt landed last. So the command runs once, at the first
  prompt.
- **Shell.** Running it before the interactive shell starts would leave the tab without a
  hook record, so it runs inside that shell.
- **sshd.** The server uses the default `MaxStartups 10:30:100`, so reconnects are
  staggered by one second per tab.
- **Cleanup rule.** In `daily_0`, the newest tmux client was the live tab, so the
  one-off cleanup keeps the newest client and detaches the rest.

The plan:

- records each tab's mosh command from `/proc`;
- replays it with `-D` added, which clears the previous boot's orphaned server;
- places windows by tmux session name;
- treats Warp as a phase 2 that depends on two experiments;
- lists nine steps, verification cases V1–V6 and five decisions for the user.

The server's address is written as a placeholder. The README links the plan, and
limitation 1 of `session_restore_plan.md` now points to it. Committed on 2026-11-14 as
`fefa009`. At that point nothing in it was implemented.

## 2026-11-16 — Revised and implemented GNOME mosh + tmux reconnect

Codex revised `docs/mosh_tmux_restore_plan.md` and implemented phase 1. A shared
`remote.py` validates a narrow mosh/tmux grammar at collection and again at replay.
Only direct foreground mosh-client children are detected; their environments and
session keys are never read. Unsupported commands get a nonreplayable marker, with
no raw text retained. Snapshots and `show` include reconnect metadata. The shell
hook records pending reconnects before waiting, runs the canonical argv once at
the first prompt, adds it to history and returns to the local shell when it ends.
Reconnects are paced across windows. Detaching other devices is now an explicit
`restore --detach-mosh` option rather than an automatic effect. AT-SPI reads tmux
status-bar names for grouping and placement; unconfirmed remote windows remain
unplaced. Tests include a real interactive Bash with a stub mosh command, process
races, malformed snapshots, quoting, history and pending-state capture. The full
suite passes (355 tests). An existing same-size VSCode cache test was made
independent of filesystem timestamp granularity after it failed intermittently.
Deployed with `sessionctl setup`; the installed modules and hook match the checkout.
A read-only desktop check found 3 windows with 19 tabs and no mosh clients, so
Warp wiring and live remote/reboot verification remain pending.

## Commits not attributed here

These commits have no Claude trailer, and no session log for them is kept on this laptop.
They may have been AI-assisted, but nothing records it, so they are listed and not
credited.

- **`843af0b`, 2026-09-16, in `stock_03_claude`, "fix a new sessionctl bug".** `save`
  had crashed every 30 s for 25 hours. Its sort compared a `None` workspace with an int
  once four monitors put workspace-less windows in the same class. The fix is a sort key
  that handles `None`. 258 tests.
- **`9ecd99b`, 2026-09-21, "Initial commit".** The move from `stock_03_claude` described
  at the top of this file.
- **`230a68d`, 2026-09-21, "to restore WARP windows".** Added Warp terminal capture and
  restore from Warp's own SQLite session database: `warp.py` and `tests/test_warp.py`, with
  changes to the hook, collection, layout matching, restore and setup. +1,895 lines.
