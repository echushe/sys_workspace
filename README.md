# sessionctl

Saves the desktop working state and restores it after a reboot: terminal tabs
with their conda envs, VSCode workspaces, browser tabs, and which GNOME
workspace each window sat on.

Design and rationale: [`docs/session_restore_plan.md`](docs/session_restore_plan.md).

## Status

Step 6 of 7 from the plan's implementation order.

| Stage | State |
|---|---|
| Stage 0 — mounts | **done**; extended to scan `$HOME` for dangling symlinks and mount what they point at, verified live against five real disks |
| Terminal capture (bash hook + `save`) | **done, hook installed** |
| VSCode + Chrome collectors | **done** |
| `restore` | **done, verified live end to end** |
| Workspace layout (`place`) | **done, verified live end to end; one further bug found by a real restore and fixed** |
| systemd units, `setup` | **done, `setup` run for real and verified live across four separate reboots** |
| Warp terminal (capture + restore) | **done, verified live against the running Warp** -- capture, signatures and matching exercised end to end against four real windows; the reboot cycle itself is not yet verified |

`setup` was run for real (not just `--dry-run`): the save timer is enabled,
and Chrome's `restore_on_startup` got set once Chrome happened to be closed.
Five separate rounds of real testing each surfaced a genuine
bug in the same family -- the timer silently never fired at all, then (after
the first fix) the timer fired correctly but every save attempted before the
disk was mounted failed outright, then (after the second fix) a structural
limitation surfaced outside the reboot cycle entirely: `sessionctl`'s own
code lived on the very partition it existed to mount, so `sessionctl mount`
could never bootstrap a cold unmounted disk no matter how the executable was
reached. The fourth was the most damaging: `restore` came back with no
terminals at all, because the shutdown snapshot had died on a dead X display,
the periodic timer had not run at all that session, and a terminal opened
seven seconds before the disk mounted never loaded the `~/.bashrc` hook -- so
a later save wrote an empty terminal set straight over `latest.json`. All
fixed: the timer gap traced to `OnBootSec=` being relative to *machine boot*
even in a per-user manager, so a login more than two minutes after boot left
it with no anchor at all (`OnStartupSec` added), and `save` now refuses to
overwrite a good snapshot when terminal windows are open but nothing was
captured.

**The fifth round was caused by the fourth round's own fix**, and is the
sharpest lesson here: making the logout snapshot stop crashing turned a
broken unit into a destructive one. `sessionctl-logout.service` ran `save`
at shutdown, after every shell had exited and X had gone, so it recorded an
empty session and wrote it over `latest.json` -- three seconds after a
periodic save had captured the real one. Restoring afterwards brought back
nothing. That unit is now removed (and actively torn down from older
installs), and `save` refuses outright to overwrite a snapshot when it
cannot see the desktop at all.

**The sixth round was not a malfunction at all**, which is why no existing
guard caught it. After a reboot the save timer captures the freshly booted
desktop *accurately* and promotes it to `latest.json` -- measured at 47
seconds from login to overwrite -- so by the time you run `restore`, the
session you wanted has already been replaced by the blank slate you were
about to restore into. Nothing was broken: there really was one terminal
with one tab. The only thing wrong was *which session* the snapshot
described. Snapshots are now keyed to the kernel's boot id, and the last
snapshot of each boot is copied to `previous_boot.json` the moment the new
boot's first save would replace it; `restore` replays that copy. (The first
fix for this paused saving until you restored, and that pause never expired
-- one reboot without a restore stopped saving for 26 days. See the last
finding under Tests.)

**The seventh finding overturned a documented impossibility rather than
fixing a fault.** Three windows of 5, 6 and 7 tabs came back as three windows
of 6, because the tab-to-window mapping was recorded as an even split -- the
plan having concluded it was unavailable. Every fact behind that conclusion
was true and re-verified; the conclusion wasn't, because "not on
gnome-terminal's D-Bus API" is not "not on this machine". The accessibility
tree exposes one frame per window and one page tab per tab, and a real save
now records `[1, 5, 5, 4, 5]` where it used to write `[4, 4, 4, 4, 4]`. Full
account under Tests, below.

**Warp terminal is captured and restored too, and it inverted the usual
difficulty.** Every other terminal here had to be reconstructed from partial
views; Warp keeps a relational record of its own session in
`~/.local/state/warp-terminal/warp.sqlite` -- `windows` -> `tabs` ->
`pane_nodes` -> `terminal_panes`, with the split tree alongside. The
tab-to-window mapping this project fought AT-SPI for is a foreign key there,
and `terminal_panes.cwd` tracks the *current* directory, not the launch one
(proven by a natural experiment already in the data: a pane whose
`WARP_INITIAL_WORKING_DIR` was `/home/shengcn` had the project directory it
had since moved to recorded in its row). Because Warp's own
`general.restore_session` defaults to true and rebuilds windows, tabs, panes,
splits and cwds from that same database, restore is the Chrome pattern rather
than the gnome-terminal one: launch the app and let it restore itself, then
place its windows on the GNOME workspaces it has no concept of. Full account
under [Warp terminal](#warp-terminal), below.

One assumption did not survive checking, and it is the kind that fails
silently: Warp does **not** source `~/.bashrc`. Its bash bootstrap sources
*login* files -- `/etc/profile`, then the first of `~/.bash_profile`,
`~/.bash_login`, `~/.profile` -- which is the exact opposite of
gnome-terminal here, whose profile is `login-shell=false`. The hook reaches
Warp panes only because this machine's `~/.profile` carries the
distribution's stock "include .bashrc" block and no `~/.bash_profile` exists
to shadow it. `setup` now checks that chain and says so, because creating a
`~/.bash_profile` for unrelated reasons would break conda capture in Warp
while leaving gnome-terminal working perfectly.

`restore` has been run for real (not just `--dry-run`) against a live
snapshot: it opened a new gnome-terminal window with the correct cwd and
conda env (confirmed by reading that new shell's own captured state
afterward), opened two new VSCode windows matching the recorded folders, and
relaunched Chrome. Because both VSCode folders were already open at the time,
this produced genuine duplicate windows -- an accepted, expected outcome of
`code -n` on an already-open folder, not a bug, and left in place rather than
cleaned up automatically.

`restore` now also runs the layout placement pass after launching everything
(unless `--dry-run`, or `--only` excludes `layout`), using the same
snapshot's recorded workspace/geometry -- this is what actually puts
newly-launched windows back where they were. `place` and the placement half
of `restore` were both exercised live: `layout.place()`'s core mechanics with
two real gnome-terminal windows deliberately moved to another workspace and
maximized, then restored with pixel-perfect geometry; `sessionctl place`
itself (the CLI command, not just the underlying function) with a real
snapshot file. What has *not* been separately re-verified live is `restore`
calling placement automatically at the end of its own run -- that specific
wiring is new, was checked via `--dry-run` output only, and reuses the
already-verified `layout.place()` underneath.

`~/.local/share/sessionctl/bash-hook.sh` is a **real copy**, deployed by
`setup` onto the always-mounted home partition. It was a symlink into this
repo for most of the project's life (it started as a plain `cp`, then became
a symlink so edits took effect without a redeploy), and it was the last
symlink into the repo left in the login path. A fourth reboot showed why
that could not stay: a terminal opened *before* the data partition mounts
sources a `~/.bashrc` whose `[ -f ... ]` guard fails, so the hook never
loads for that shell and it captures nothing for its entire life --
silently, since a guarded `source` that finds nothing is indistinguishable
from one that had nothing to do. Re-run `setup` after editing
`bash-hook.sh` to redeploy it. The `~/.bashrc` line is installed and only
applies to terminal tabs opened after installation -- tabs already open when
it was installed are not retroactively captured.

## Why Stage 0 exists

Several first-level entries in `$HOME` are symlinks into disks that are **not in
`/etc/fstab`** and that udisks reports as `HintAuto: false` — so they are not
mounted at boot. They appear only when something opens them, normally Nautilus.

Until then those links do not resolve. A restore that ran at that moment would
open VSCode on non-existent folders and drop every terminal into `$HOME` while
appearing to succeed. Stage 0 mounts first, verifies the paths actually resolve,
and refuses to continue if a *required* one does not.

The disk layout this section was originally written against has since changed,
and the old text is worth correcting rather than quietly deleting: it said
`~/Projects` and `~/extra_disk_2t` were symlinks into one partition
(`/dev/nvme0n1p6`, UUID `d8d577a6-…`). Today `~/Projects` is a real directory on
the root filesystem and that partition does not exist at all. What actually
dangles after a boot are five `~/extra_disk_*` links, spread across five
separate disks — which is what the `$HOME` scan below is for. Note the
consequence for `detect`: with `~/Projects` on root and `~/extra_disk_2t`
dangling, it finds **nothing** on a cold boot, so before this scan existed
Stage 0 had nothing to do and silently passed.

### Scanning `$HOME`

`sessionctl mount` — and **only** that command — scans the first level of
`$HOME` for symlinks that dangle and tries to mount whatever each one points at.
`restore` deliberately does not: its Stage 0 stays limited to the configured
set, so the one command that opens windows keeps exactly the mount behaviour it
had before, and mounting five disks stays something you ask for explicitly
rather than a side effect of restoring a session. The periodic `save` timer
never scans either. The mapping runs backwards from
the link: udisks always mounts at `/media/<user>/<label-or-uuid>`, so the final
path component of a dangling target is exactly what identifies the device —
looked up in `/dev/disk/by-label`, then `/dev/disk/by-uuid`. Verified against
all five real disks: every target basename matched a `by-label` entry, and a
real mount landed at precisely the predicted path.

Two deliberate limits:

- **Only links pointing straight into `/media/<user>/`.** A link to `/mnt/foo`
  cannot be fixed this way — udisks would mount the disk at `/media/<user>/foo`
  and the link would still dangle — so attempting it would report success while
  changing nothing.
- **Symlinks only.** An empty directory sitting at a mountpoint is
  indistinguishable from an ordinary empty directory, and carries no label to
  work back to a device from.

These mounts are **best-effort and never fatal** (`mounts.ensure_optional`), in
deliberate contrast to the configured set (`mounts.ensure_all`, which still
raises on the first failure and still blocks a restore). The distinction is the
point: a disk named by the session's own recorded paths is required, while a
disk found by scanning is opportunistic, and an external drive that is simply
unplugged is an ordinary state rather than a reason to fail the whole command.
Failures print as `skipped:` and the pass continues to the next entry — checked
directly with a fabricated absent-UUID entry ordered *before* a real one, which
is what proves the loop carries on rather than merely swallowing the last error.

`--no-scan-home` turns the scan off, leaving `mount` with exactly its old
behaviour.

Mounting needed no PolicyKit prompt on this machine despite every one of these
disks reporting `HintSystem: true` — tested non-interactively with stdin closed,
on ext4 and on NTFS, both of which mounted clean. That is a property of this
machine's polkit rules, not a guarantee; the note under "Optional one-time fix"
about `udisksctl` and internal drives still stands for machines where it isn't
true, and a prompt that cannot be answered would surface as an ordinary
`skipped:` line rather than a hang, since the mount simply fails.

## Usage

```sh
./sessionctl detect          # work out the mount config from the paths themselves
./sessionctl detect --write  # save it to ~/.config/sessionctl/config.yaml
./sessionctl mount           # Stage 0: ensure storage is mounted
./sessionctl mount --dry-run # show what would be mounted, mount nothing
./sessionctl mount --no-scan-home   # configured set only; ignore $HOME's dangling links

./sessionctl hook            # show the ~/.bashrc line this needs
./sessionctl hook --install  # append it (not done automatically -- see Status)

./sessionctl save            # snapshot terminal tabs, but only if changed
./sessionctl save --dry-run  # print what would be saved, store nothing
./sessionctl list            # list stored snapshots, newest first, and what
                             #   `restore` would replay
./sessionctl show [id]       # show a snapshot (default: latest;
                             #   `previous` = the one from before the last reboot)

./sessionctl restore --dry-run              # print the exact commands, launch nothing
./sessionctl restore                        # relaunch the session from before the last reboot
./sessionctl restore --snapshot ID          # restore a specific snapshot (or `latest`)
./sessionctl restore --only vscode,chrome   # restore a subset
                             #   (terminals, warp, vscode, chrome, layout)
./sessionctl restore --warp-force           # launch Warp even if it's running
./sessionctl restore --chrome-urls          # open Chrome's recorded URLs explicitly
./sessionctl restore --skip-mounts          # skip Stage 0 (you've already mounted)

./sessionctl place                          # re-apply layout from latest, launch nothing new
./sessionctl place --snapshot ID            # use a specific snapshot (or `previous`)
./sessionctl place --timeout 30             # wait longer for windows to appear
./sessionctl place --static-workspaces      # use raw indices (only if you've set
                                             # dynamic-workspaces=false yourself)

./sessionctl setup --dry-run                # preview every step below, do nothing
./sessionctl setup                          # symlink ~/.local/bin/sessionctl, enable the
                                             # systemd save timer, set Chrome's
                                             # restore_on_startup (skipped if Chrome is
                                             # running -- see below)
./sessionctl setup --install-hook           # also append the ~/.bashrc line in the same run
./sessionctl uninstall --dry-run            # explain what would be reversed
./sessionctl uninstall --yes                # actually reverse it (refuses without --yes)
./sessionctl uninstall --yes --purge        # also delete snapshots and config
```

`save` also records Warp's own session -- every window, tab, pane, split and
per-pane cwd, read straight out of Warp's database, plus each pane's conda env
from the shell hook. See [Warp terminal](#warp-terminal).

`save` captures every window's workspace, geometry, maximized state and
sticky flag via Wnck -- not just windows sessionctl launches. `place` re-binds
those recorded windows to whatever is currently live and puts them back,
without launching anything; `restore` calls the same placement pass after it
finishes launching. Matching is class-first, title-second (see
`layout.match_windows`): candidates are pooled by exact `wm_class`, then
narrowed by title similarity when more than one candidate shares a class.
Terminal windows deliberately get no special title-marker treatment despite
gnome-terminal supporting `--title` at launch -- tested live and found this
system's default `PS1` embeds a title-setting escape sequence that fires on
the very first shell prompt, silently overwriting any launch-time title
before a placement pass could ever observe it. So terminals fall back to
class-plus-position pairing, which is the same existing limitation on
multi-window terminal grouping documented in the plan, not a new one.

One signal does outrank title similarity: **whether the window did not exist
before this restore launched things.** `restore` snapshots the live window
XIDs before launching anything (`layout.live_xids()`) and passes that
baseline to `place()`, which flags every window absent from it as `new`. A
window created specifically to satisfy these records is far stronger
evidence than a title, which can match by coincidence -- or, for terminals,
matches everything equally. `place` on its own launches nothing, has no
baseline, and matches purely on class and title exactly as before.

Placement always proceeds in ascending workspace order, with gaps in the
recorded indices compacted to consecutive ones first. `dynamic-workspaces`
being enabled means workspace N does not exist until 0..N-1 are occupied
(confirmed live: moving a window onto the trailing empty workspace grows the
count by one), so placing out of order or into a gap can target a workspace
that isn't there yet.

`setup` does four things, each idempotent and each individually safe to
re-run: deploys the whole `sessionctl_lib/` package, the entry script and
`bash-hook.sh` as **real copies** under `~/.local/share/sessionctl/`, with
`~/.local/bin/sessionctl` symlinked to the deployed entry script (both ends
of that symlink are on the always-mounted home partition, so it can't
dangle; `_ensure_symlink` also refuses outright to touch anything at that
path that isn't already a symlink, so it can never silently clobber an
unrelated file); copies the three unit files below into
`~/.config/systemd/user/` and `enable --now`s the save timer (also disabling
and deleting the obsolete logout unit from older installs);
optionally appends the `~/.bashrc` hook line (`--install-hook` -- off by
default, since editing a live dotfile is held for explicit confirmation the
same way `sessionctl hook --install` already is); and sets Chrome's
`restore_on_startup` to "continue where you left off", **refusing outright
while Chrome is running** -- Chrome rewrites `Preferences` from its in-memory
state when it exits, so an edit made while it's open would be silently
discarded the next time it closes. Verified against a real, running Chrome:
`setup --dry-run` correctly detected this and refused, printing the manual
Settings path instead.

Three systemd user units, copied from `systemd/` in this repo (copied, not
symlinked -- see the reboot findings under Tests):

- `sessionctl-save.timer` — `OnBootSec=30s`, **`OnStartupSec=30s`**,
  `OnUnitActiveSec=30s`, `AccuracySec=1s`, driving `sessionctl-save.service`
  (`Type=oneshot`, `ExecStart=%h/.local/bin/sessionctl save`). `AccuracySec`
  is required rather than decorative at this interval: it defaults to **1min**
  and systemd places each elapse at a randomized point inside that window to
  coalesce wake-ups, so a 30s tick left at the default would be scattered
  across 60s and would not actually be 30 seconds. Measured after the change:
  firings 30.8-31.0s apart, each run ~105ms and writing nothing when the
  digest is unchanged. `OnStartupSec` is the one that
  matters for a *user* timer and was missing originally: per
  `systemd.timer(5)`, `OnBootSec=` is relative to machine boot for a per-user
  manager (the remap to `OnStartupSec=` applies only to the system manager
  inside a container), so it only anchors the timer if you log in within two
  minutes of boot. `OnStartupSec=` is relative to the service manager's own
  start and is documented as "primarily useful ... in the per-user service
  manager". `Persistent=yes` was dropped: it "only has an effect on timers
  configured with `OnCalendar=`", so on a purely monotonic timer it was inert
  and implied a catch-up guarantee that never existed.
There **was** a third, `sessionctl-logout.service`, which ran `save` from
`ExecStop` to take a final exact snapshot at logout. It has been removed, and
`setup` actively disables and deletes it from any older install
(`setup.OBSOLETE_UNITS`) -- leaving it in place would keep an enabled unit
running with no source file to explain it. It could never do what it claimed:
by the time `ExecStop` runs, every shell has exited and its `EXIT` trap has
already deleted the per-tab state file, and X is gone too, so it captured an
empty session and wrote it over `latest.json`. Caught with three seconds
between two journal lines -- a periodic save reporting `6 tabs, 5 windows
placed`, then the logout save writing `0 tabs, 0 windows placed`. It was
harmless only while it was crashing on the dead display; fixing that crash is
what turned it into the thing that destroyed a session. At a 30s tick it was
never buying more than 30 seconds of freshness anyway.

Both remaining units pass `systemd-analyze --user verify` (checked directly against the
files, not just eyeballed). The timer's `save` runs need a working X11
`DISPLAY` for layout capture -- confirmed present in
`systemctl --user show-environment` on this machine (`DISPLAY=:1`,
`XAUTHORITY=...`), so no extra environment wiring was needed here, but
`_capture_layout()`'s existing degrade-to-empty-rather-than-crash fallback
(added in step 5, before this was even checked) is what makes this safe on a
machine where it isn't true.

`uninstall` reverses `setup`: disables and unlinks the units, removes the
`~/.local/bin/sessionctl` symlink and the `~/.bashrc` hook line, and (only
with `--purge`) deletes snapshots and config. It refuses to run at all
without `--yes`, to rule out an accidental invocation. It deliberately does
**not** revert Chrome's `restore_on_startup` -- there's nothing unsafe about
leaving that on, and the plan doesn't call for undoing it.

`save` refuses to write while a required mount is missing or unresolved (a
degraded session), so the periodic timer cannot grind a good snapshot away
during the window between boot and the disk being mounted.

Write-avoidance has two layers. `save` hashes only the state a restore would
actually consume -- each tab's cwd and conda env, VSCode folder paths, Chrome
URLs, which mounts are present -- and skips the *write* entirely when that has
not changed; only a `heartbeat.json` timestamp moves. Underneath that,
`collect_vscode`/`collect_chrome` record each source file's `(path, mtime,
size)` and skip the *scan* itself when the source is unchanged from the
previous snapshot -- this is the layer that matters, since scanning Chrome's
session file is the dominant cost of a save (hundreds of KB of binary
session-command data), not the write.

VSCode's window list comes straight from
`~/.config/Code/User/globalStorage/storage.json`, which VSCode maintains as
its own authoritative record -- no X11 title parsing needed. Chrome's URLs are
extracted from the newest `Session_*` file (not `Tabs_*`, which stores full
back/forward history and runs several times larger for the same browsing) via
a tolerant scan restricted to the RFC 3986 URL character set; there is no
version-stable way to distinguish "this is the tab's current URL" from
referrer/iframe/favicon noise in that binary format without replicating
Chromium's internal pickle layout, so this stays a best-effort record for
`show`/`list` and the `--chrome-urls` restore fallback -- the primary restore
path is Chrome's own "continue where you left off", which `sessionctl setup`
(step 6, not yet built) will set as a preference; until then, Chrome's own
restore stays off and `--chrome-urls` is the only way `restore` actually
reopens tabs.

`restore` runs Stage 0 first, always (`--skip-mounts` bypasses it), then loads
a snapshot and builds one command per terminal window, VSCode window, and
Chrome profile. `--dry-run` prints the exact argv for each without running
anything. Live-launched processes are detached (`subprocess.Popen`,
`start_new_session=True`) so `restore` itself never blocks waiting on them.

gnome-terminal only honours **one** trailing `-- COMMAND` per invocation --
confirmed by testing directly: a second `--tab ... -- bash -c ...` block does
not run as its own command, it is silently swallowed as inert positional
arguments to the first tab's command, and every tab after the first simply
never opens. `--command=STRING` is the one option that works correctly
per-tab here, despite gnome-terminal printing a deprecation notice suggesting
`--` instead -- which does not actually support what it's suggested for. Each
tab's `--command=` value carries `SESSIONCTL_CONDA=<env> exec bash -i` (or
just `exec bash -i`), which the installed `bash-hook.sh` picks up and turns
into a real `conda activate`.

`detect` follows each path a restore depends on, finds the mount containing it,
and reads back that device's UUID. Paths on the root filesystem are skipped —
root is always mounted. Several paths on one device collapse into one entry, so
`~/Projects` and `~/extra_disk_2t` produce a single mount.

With no config file, commands fall back to live detection, so the tool works
before `detect --write` has ever run.

## Warp terminal

Warp is captured and restored alongside gnome-terminal, and the split of
labour is different because Warp's own record is better than anything
sessionctl could reconstruct.

**What Warp gives us.** `~/.local/state/warp-terminal/warp.sqlite` is a real
relational session record: `windows` (with `active_tab_index`, geometry and
fullscreen state) -> `tabs` -> `pane_nodes` -> `terminal_panes` (with `cwd`
and the shell), plus `pane_branches`/`pane_leaves` for the split tree and
which pane is focused. So the tab-to-window mapping is a foreign key rather
than a heuristic -- the thing that cost this project an accessibility-tree
detour for gnome-terminal is free here, and exact. Two facts about that
database were checked rather than assumed:

- `terminal_panes.cwd` is the **current** directory, not the launch one. A
  natural experiment was already sitting in the data: a pane whose shell had
  `WARP_INITIAL_WORKING_DIR=/home/shengcn` had
  `/home/shengcn/Projects/finance/stock_03_claude` in its row, which is where
  that shell had since been `cd`'d to.
- The cwd is stored in **logical** form (`~/Projects/...`, symlink intact)
  while `/proc/<pid>/cwd` resolves to the physical `/media/shengcn/...` path.
  Same directory, different strings; nothing may compare them for equality.

It is read `mode=ro` and never `immutable=1`. Immutable would make SQLite
ignore the write-ahead log and quietly serve state from the last checkpoint;
read-only takes read locks only and cannot write, checkpoint or recover, which
is what makes it safe against a database Warp has open. Both `warp.sqlite`
**and** `warp.sqlite-wal` are stat'd for the write-avoidance skip, because
Warp runs in WAL mode (confirmed: `pragma journal_mode` returns `wal`) and a
committed change can sit in the log with the main file's mtime untouched.

**What Warp doesn't give us: the conda env.** `shell_launch_data` records only
the executable (`/bin/bash`), and no database column could hold more --
`conda activate` mutates the running shell and never re-execs, which is the
same reason `bash-hook.sh` exists at all. So the hook now reports Warp panes
too, and the join is exact rather than positional:
`WARP_TERMINAL_SESSION_UUID`, which Warp exports into every pane's shell, is
the hex of `terminal_panes.uuid`. Verified one-to-one -- all nine live pane
shells matched all nine rows.

**Warp does not source `~/.bashrc`.** Its bootstrap sources login files:
`/etc/profile` (or `/etc/bash.bashrc`), then the first of `~/.bash_profile`,
`~/.bash_login`, `~/.profile`. That is the opposite of gnome-terminal here,
whose default profile is `login-shell=false` and so reads `~/.bashrc` and
never `~/.profile`. The hook reaches Warp panes over a two-hop chain --
Warp -> `~/.profile` -> `~/.bashrc` -- which holds only because this machine's
`~/.profile` has the distribution's stock `if [ -n "$BASH_VERSION" ]; then
. "$HOME/.bashrc"` block and nothing shadows it. Creating a `~/.bash_profile`
later is enough to break it, silently and only in Warp, so `setup` checks the
chain and reports it. One useful side effect of Warp being a login shell:
`~/.local/bin` *is* on `PATH` in a Warp pane, so the bare `sessionctl` command
works there even though it does not in a new gnome-terminal tab (see the note
at the end of `setup`'s section).

Warp's bootstrap also constrains how the hook may install itself. It re-homes
the user's `PROMPT_COMMAND` into its own bash-preexec chain, but only after
this test, lifted verbatim from the shipped binary:

```sh
if [[ ! $PROMPT_COMMAND =~ "PROMPT_COMMAND=" ]]; then
```

It inspects the *value*, so the hook's `PROMPT_COMMAND="_sessionctl_save; ..."`
is honoured -- confirmed by replaying Warp's own bootstrap logic around the
real hook file, which reported it captured and wrote a pane's state file. A
future edit that put the literal `PROMPT_COMMAND=` into the value would
silently disable the hook in Warp only.

**Restore is Warp's job, not sessionctl's.** `general.restore_session` is
documented in Warp's own settings schema (`warp-terminal
dump-settings-schema`) as "Whether to restore the previous session when Warp
starts up", boolean, **default true**, and it rebuilds windows, tabs, panes,
splits and cwds from the same database. So `restore` launches `warp-terminal`
if Warp isn't already running and stops there -- the Chrome pattern. Whether
Warp is running is read *now*, never from the snapshot: the snapshot records
whether Warp was up when the session was **saved**, which after a reboot is
almost always true and would skip the one launch that matters.

Nothing is launched when Warp is already running, because there is nothing
useful to launch: `warp-terminal --help` exposes no window, tab or session
options at all (its subcommands are agent, cloud, MCP and auth management),
the binary carries only two `warp://` routes -- `warp://cli` and
`warp://linear` -- and this version has no launch-configuration file support.
`--warp-force` overrides the check for the case where Warp was quit between
`save` and `restore`.

Unlike Chrome's, Warp's preference is **reported, not written**. `setup` edits
Chrome's `Preferences` because Chrome ships that setting off and restore
depends on turning it on; Warp's equivalent already defaults to true and is
absent from `settings.toml` entirely on this machine, so there is nothing to
write. Writing anyway would mean hand-editing a live TOML file (Python 3.10
has no `tomllib`, and `/usr/bin/python3` is deliberately what this runs under)
to set a value already in force. So `setup` only speaks up when the setting
has actually been turned off, and then says where to turn it back on.

**Placing Warp windows is the part only sessionctl can do**, since Warp stores
its own x/y/size but has no concept of a GNOME workspace. Warp windows have
exactly gnome-terminal's matching problem -- one `wm_class`
(`dev.warp.Warp`), one shared PID for every window, identical maximized
geometry, and repeated titles (two of the four live windows were both
`~/Projects/ZERO`) -- so they now carry the same `{tabs, cwds}` signature
gnome-terminal windows do, which means `match_windows` needed no Warp-specific
case. The difference is where it comes from: Warp's is read from its database
and is exact whatever the panes are running, while gnome-terminal's is read
off prompts and is blind to any tab that isn't sitting at one.

That signature is paired to X11 windows **by content, never by position**, and
that distinction is not cosmetic. Warp's window ids do not line up with XID
order: sorted by XID the live windows read `stock_03_claude, ZERO, ~, ZERO`,
while database ids 1-4 read `stock_03_claude, ZERO, ZERO, ~`. Pairing them
index-for-index -- the way terminal signatures pair AT-SPI frames with
XID-sorted windows -- would have handed two windows each other's signatures,
silently. Instead each database window is matched to the live window whose
title it predicts (Warp titles a window after the focused pane of its active
tab, or that tab's custom title), exact titles first and then Warp's elided
form, since Warp cuts the front off a long path mid-segment:
`..s/finance/stock_03_claude`. Anything left unpaired gets no signature rather
than a guessed one, and the whole map is abandoned if the two views disagree
on how many windows there are -- the same degrade-never-guess policy the
terminal path uses.

Verified live against the real four-window session: all four paired correctly
including the elided title, the three-tab `~` window got `{tabs: 3}` and so is
now positively identified rather than relying on title similarity, and
`match_windows` bound all four recorded rows to the right live windows. Under
`strict` it correctly deferred the two genuinely identical `~/Projects/ZERO`
windows instead of coin-flipping them, which is the documented settle-window
behaviour.

### Re-run `setup` after upgrading

`bash-hook.sh` is deployed as a real copy, so the Warp half of capture does not
take effect until `sessionctl setup` redeploys it -- and, as with
gnome-terminal, only panes opened *after* that pick it up. Panes already open
keep reporting nothing, so their conda envs read as null until they are
reopened. Their cwds are unaffected, since those come from Warp's database
rather than the hook.

## Optional one-time fix

Adding the partition to `/etc/fstab` makes it mount at boot and reduces
`sessionctl mount` to a no-op check. Needs `sudo` once:

```
UUID=d66e0f19-00d6-4d8c-9ccb-8205772d85a4  /media/shengcn/Samsung2T  ext4  defaults,nofail,x-systemd.device-timeout=5s  0  2
```

That is the one ext4 disk of the five; the other four are NTFS and need
`ntfs-3g` as the fstype with `uid=`/`gid=` options, so they are a different
line rather than the same one repeated. Current UUIDs are whatever
`sessionctl mount --dry-run` prints — it names the device for each disk it
would mount.

`nofail` and `device-timeout` matter — without them a missing or renamed disk
can stall boot. Keep each mountpoint identical to the path the `~/extra_disk_*`
symlink already points at, or the link keeps dangling and every recorded path
on that disk breaks.

This also avoids the one rough edge of `udisksctl`: these are internal drives
(`HintSystem: true`), so PolicyKit may ask for a password when mounting them on
a machine whose rules require it — see the note under "Scanning `$HOME`".

## Tests

```sh
/usr/bin/python3 -m unittest discover -s tests -v
```

326 tests. Mount branches are stubbed rather than run against real hardware --
see the section above. Terminal capture, VSCode and Chrome were all exercised
against real state (a real gnome-terminal tab via `--rcfile`, matching how
`restore` invokes shells; the real `storage.json`; the real Chrome session
file), which is how three real bugs got caught -- none of which a mock-only
test suite would have surfaced:

- `/proc/<pid>/comm` truncates to 15 bytes, so `gnome-terminal-server`'s
  parent-process check was comparing against a name that can never match.
- The first Chrome URL regex used a negated character class (exclude
  quotes/whitespace/backslash) against the raw latin-1-decoded session file.
  Since nearly every byte decodes to *some* printable latin-1 character, it
  glued each URL onto the binary pickle data that follows it, rather than
  stopping at the URL's actual end. Fixed by restricting to the RFC 3986 URL
  character set instead of a negated "anything but" class.
- Session file selection used `max(files, key=os.path.getmtime)`, which
  breaks ties arbitrarily when two files share an mtime -- caught by a test
  that created two session files back to back within one mtime tick. Fixed
  by sorting on Chrome's own filename counter (`Session_<n>`, confirmed to be
  monotonic and directly time-ordered against real profile data) instead of
  filesystem mtime.

`restore.py`'s command-building was verified the same way. gnome-terminal's
multi-`--` limitation (above) was found by writing a marker file per tab and
watching only the first one appear -- no test in isolation would have
surfaced it, since the bug is in an assumption about the tool being invoked,
not in any of sessionctl's own code. `--command=`, a `--working-directory`
containing a space, and the real `SESSIONCTL_CONDA` hook activating a real
conda env were all then confirmed together in one real (throwaway) terminal
window. `sessionctl restore --dry-run` was run against a real captured
snapshot end to end, and then a real, non-dry-run `sessionctl restore` was
also run (with explicit confirmation first, since it opens real windows) --
see Status, above, for what that actually did.

Warp's tests build a **real sqlite database** using Warp's own DDL for the
columns being read, rather than mocking query results: the multi-table join
from `windows` down to `terminal_panes` is the part worth testing, and a mock
of `conn.execute` would only assert that the test agrees with itself. A rename
in Warp's schema should break these tests, because it would break the
collector. Covered there and not reachable live: an unreadable or locked
database degrading to an empty record instead of taking down the whole save, a
window Warp has not sized yet reading as unknown geometry rather than a window
at the origin, the source-skip still re-joining conda (a pane that only ran
`conda activate` leaves both files' mtime and size untouched, so returning the
cached record verbatim would hold a stale env forever), and the read-only
connection genuinely refusing a write.

Two Warp findings came out of writing those tests rather than out of running
the code, and both are recorded above because they are the sort that fail
silently: the database's window ids not lining up with XID order, and Warp
sourcing login files instead of `~/.bashrc`.

One Warp hazard is a **data-loss** guard rather than a correctness one, and it
has its own test. Warp panes and gnome-terminal tabs write into the same
`shells/` directory, and `read_shell_records` deletes what it judges dead --
so judging a Warp pane's record by gnome-terminal's rules (no
`GNOME_TERMINAL_SCREEN`, parent not `gnome-terminal-server`) would have
deleted a live pane's record, and its conda env with it, on the very next
save. Validation is therefore per-kind and happens *before* the caller's kind
filter is applied. Records written before the hook learned about Warp carry no
`kind` at all and read as gnome-terminal, so a snapshot taken across the
upgrade stays readable instead of quietly losing its terminals.

`layout.py` needed live testing to find bugs no unit test could reach at all:
Wnck's action methods (`move_to_workspace`, `maximize`, `set_geometry`, `pin`)
are asynchronous X11 requests that silently no-op unless the GLib main loop
is actively pumped -- confirmed by calling one and immediately re-querying
the window's state, every time, with zero change. Without that pump, every
placement action would have shipped completely non-functional. A second,
narrower bug needed the same discovery method: `unmaximize()` immediately
followed by `set_geometry()` landed tens of pixels short on width/height,
because the request was computed against the window's still-transitioning
unmaximize state -- fixed by pumping between the two calls specifically.
Both fixes were then verified with a real disturb-then-restore round trip
(two real gnome-terminal windows moved to another workspace and maximized,
then put back via `place()` with pixel-perfect geometry), and again through
`sessionctl place` itself at the CLI level using a real snapshot file. The
dynamic-workspace growth mechanism -- moving a window onto the trailing
empty workspace grows the count by one, confirmed live -- is what makes the
ascending-placement-order strategy actually work.

**A third layout bug surfaced only after a real reboot-and-restore, and only
because it was reported: the restored terminal window came back small and
unmaximized.** The natural suspicion was the placement pass -- specifically
`maximize()` being issued straight after `set_geometry()` with no pump
between them, the exact hazard already proven real above for
`unmaximize()` -> `set_geometry()`. That suspicion was wrong, and testing it
directly on a throwaway window is what showed it: `maximize()` lands
correctly either way, pump or no pump.

The actual cause was in matching, not placement. Every gnome-terminal title
is byte-identical once a shell has drawn a prompt, so with a snapshot
holding one terminal record and *two* live terminal windows -- the one you
launched `sessionctl restore` from, plus the one restore just created --
title similarity tied at 1.0, and `max()` returns the first maximal element:
the older, pre-existing window. The record's workspace and maximized state
were applied to the terminal already in use, while the freshly restored one
was left wherever gnome-terminal put it. Confirmed from the live X11 state
after the reboot: the lower (older) XID sat on the recorded workspace 1,
maximized, while the higher (newly launched, 4-tab) XID sat unplaced on
workspace 0. Worse, this reported success -- `place()` computes the leftover
live windows but the call site discards them, so nothing ever reported that
a record had bound to a window restore did not create.

Fixed by giving `restore` a baseline of live window XIDs taken before it
launches anything, so matching can prefer windows this run actually created
(see `layout.live_xids` and the `new` flag in `match_windows`). Verified
with a live A/B on two throwaway terminal windows with identical titles,
driving the real `place()` code path: with `preexisting=None` the
pre-existing window got maximized (reproducing the bug exactly), and with
the real baseline the newly launched one did instead -- each trial
reporting the same `placed=1 unmatched=0` that had made the original
failure invisible.

**A fourth reboot then produced the worst failure so far: `restore` brought
back no terminals at all, because `latest.json` had been overwritten with an
empty session.** Three independent faults compounded, reconstructed from
`journalctl`:

1. *The final pre-reboot snapshot never happened.* `sessionctl-logout.service`'s
   `ExecStop` ran at shutdown, by which point X was already gone, and
   `Gtk.init()` **aborts the process** rather than raising when it can't open
   the display -- `cannot open display: :1`, `status=1/FAILURE`. So
   `_capture_layout()`'s try/except, written specifically to degrade to an
   empty layout rather than lose a snapshot, never got to run: the process
   was killed underneath it. Fixed by using `Gtk.init_check()`, which returns
   a boolean, and raising a catchable `NoDisplayError` -- verified by running
   `save` with `DISPLAY` unset and watching it degrade and exit 0 instead of
   dying.
2. *The periodic timer also fired zero times in that session* -- 75 minutes,
   no `sessionctl-save.service` runs at all, so nothing else captured the
   pre-reboot state either. Restarting the timer by hand did not reproduce
   it, because in a session where the service has already run once
   `OnUnitActiveSec` has a valid anchor and in that session it never did.
   The cause, found in `systemd.timer(5)` afterwards: **`OnBootSec=` is
   relative to machine boot even for a per-user manager** -- the remapping to
   `OnStartupSec=` applies only to the system manager inside a container. The
   failing session booted at 18:17:16 and started its user manager at
   18:19:44, 2.5 minutes later, so `OnBootSec=2min` was already in the past
   and `OnUnitActiveSec=` had no previous activation to count from; the
   working session had its manager start ~2 seconds after boot. Fixed by
   adding `OnStartupSec=2min`, which is relative to manager start and so
   always lies in the future when the timer loads. Confirmed registered
   (`systemctl --user show -p TimersMonotonic` now lists an `OnStartupUSec`
   anchor alongside an `OnBootUSec` one that has already elapsed), but **not
   yet confirmed against a real late login**, which needs a boot where the
   user manager starts more than two minutes after the kernel.
3. *After the reboot the hook silently didn't load.* The terminal shell
   started at 19:36:43; the data partition mounted at 19:36:50, seven seconds
   later. `bash-hook.sh` was still a symlink into the repo, so the `~/.bashrc`
   guard found nothing and that shell captured nothing for its whole life.
   Fixed by deploying it as a real copy (see Status, above) -- verified by
   launching a real interactive terminal the same way `restore` does and
   confirming a shell state file appears with the right cwd and conda, and is
   cleaned up on close.

Then the compounding step: with the disk mounted, `save` no longer counted as
degraded, so it happily wrote `0 tabs` and promoted it to `latest.json`. The
snapshot even recorded the contradiction -- `window_count: 1` (read from
gnome-terminal's D-Bus tree, independent of the hook) alongside an empty tab
list. `save` now treats exactly that shape as a broken capture rather than an
empty desktop and refuses to store at all, which preserves both `latest.json`
and the 20-entry ring (declining only to update `latest` would still let a
2-minute timer fill the ring and evict the good snapshot). It is deliberately
gated on the `~/.bashrc` line actually being installed, so a machine that
never installed the hook isn't blocked from saving forever.

**The fifth reboot then failed because of the first fix in that list**, which
is the most useful thing this project has turned up. Fix 1 above made the
logout snapshot survive a dead X display. It then ran successfully -- and
because it runs at `ExecStop`, after every shell's `EXIT` trap has deleted
its state file and after X has gone, "successfully" meant capturing an empty
session and writing it over `latest.json`:

```
20:17:12.48  periodic save: unchanged (6 tabs, ..., 5 windows placed)
20:17:13.32  Stopping Final sessionctl snapshot on logout...
20:17:15.55  logout save:   saved ... (0 tabs, ..., 0 windows placed)
```

Three seconds, and the session was gone. While that unit was crashing it was
merely useless; fixing the crash is what made it harmful. The
`terminal_capture_broken` guard didn't catch it either, and correctly so: at
shutdown `gnome-terminal-server` is gone too, so `window_count` is 0, which
reads as "no terminals open" rather than "capture broken" -- the guard was
built for the opposite shape, windows with no tabs.

Two changes. The unit is **removed**, and `setup` disables and deletes it
from any older install (`setup.OBSOLETE_UNITS`) rather than merely ceasing to
ship it -- systemd keeps running what is already enabled, so dropping it from
the list alone would have left it destroying sessions indefinitely. Verified
on this machine: it was `enabled` before, and afterwards the unit file is
gone, `is-enabled` reports no such file, and the save timer still fires on
schedule. And `save` now refuses when layout capture came back empty while
the previous snapshot had windows -- it could not see the desktop, so it must
not claim to know what is on it. Deliberately keyed on layout visibility
rather than tab count, so genuinely closing every terminal is still recorded.
Verified by running `save` with `DISPLAY` unset against a populated
`latest.json`: it refuses and leaves the file untouched.

**The sixth failure was the first one where nothing malfunctioned**, and it
had been latent the whole time -- the earlier fixes just kept exposing it.
Terminals restored correctly after one reboot and failed after the next,
which was pure timing:

| Snapshot | Contents |
|---|---|
| 11:00:25, pre-reboot | 5 tabs, one in `~/Projects/finance/stock_03_claude` on `torch209` |
| 11:02:43, 47s after login | **1 tab** in `~` -- and this became `latest.json` |

Before the disk is mounted, `save` refuses as degraded, so nothing is lost.
The moment `sessionctl mount` succeeds, the 30s timer starts capturing the
freshly booted desktop and promoting it to `latest`. You then have to beat a
30-second timer to `sessionctl restore`, which is not a design -- and the
faster tick requested earlier made losing that race much more likely. The
first reboot that day was won, the second was lost.

Neither existing guard applies, correctly: the capture is accurate (one real
terminal, one real tab) and the layout is fully visible. `terminal_capture_broken`
is for windows without tabs; `desktop_not_visible` is for a desktop that
couldn't be observed. Here the observation was perfect and the *subject* was
wrong.

Fixed with the one signal that distinguishes the two sessions: the kernel's
boot id (`/proc/sys/kernel/random/boot_id`), recorded in every snapshot. If
`latest.json` carries a different boot id than the running boot, it describes
a session from before the reboot. The first version of this fix made `save`
refuse to overwrite it until `restore` or `sessionctl adopt --yes` had run.
That turned out to be its own failure, and it has been replaced: see the last
finding below.

**A seventh finding, and the first that overturned a "not possible" rather
than fixing a fault.** Three terminal windows holding 5, 6 and 7 tabs came
back as three windows of 6. The recorded data was already wrong: `_group()`
split tabs evenly by ceiling division across the window count, because the
plan had concluded the real mapping was unavailable. That conclusion rested
on true facts -- gnome-terminal's screens and windows are flat, unrelated
D-Bus objects; the window objects carry only `org.gtk.Actions`, whose
`active-tab` is an index rather than a count; there is no `org.gtk.Menus` to
enumerate tabs; VTE doesn't set `WINDOWID` -- each re-verified here. But
"not on gnome-terminal's D-Bus API" is not the same as "not on this
machine".

The accessibility tree has it: `gnome-terminal-server` exposes one `frame`
per window containing one `page tab` per tab, each named with that tab's
title. Measured on a live desktop with five windows: 1/5/5/4/5 page tabs,
totalling exactly the 20 tabs the shell hook had independently recorded, in
~0.05s, and working even with `toolkit-accessibility` set to false. Tabs are
now assigned to the window whose page-tab title matches their
`~`-abbreviated cwd, so a tab in a distinctive directory lands where it
belongs and indistinguishable ones fill the rest. A real `save` afterwards
recorded `[1, 5, 5, 4, 5]` where it would previously have written
`[4, 4, 4, 4, 4]`.

Getting the shape right exposed the second half of the same bug: the
*contents* were still wrong. Matching used the windows' tab titles, which
encode only the cwd, so with every tab sitting in `~` every title read
`user@host: ~`, nothing distinguished a `torch209` tab from a `base` one,
and slots were filled in PID order -- three of five windows came back with
the wrong envs. The env is in each tab's *prompt* even when it isn't in its
title, and AT-SPI exposes the terminal widget's text: reading the tail of
each tab's buffer and parsing the last prompt line gives its env and cwd
together. Matching then runs in two passes so the strongest evidence wins
globally rather than per-slot -- first every slot fillable by a tab agreeing
on both cwd and env, then cwd alone, then whatever is left, in order.
Verified against the live desktop: the grouped result now equals AT-SPI's
ground truth window for window (`['base']`, `['torch209']*3`,
`['base','base','torch209','torch209']`, `['torch209']*5`,
`['torch209']*6`).

**The probe runs in a subprocess, and that is not caution for its own sake.**
`Atspi.init()` *aborts the process* when the accessibility bus is
unreachable -- `dbind-ERROR ... Is at-spi-bus-launcher running?`, then a core
dump, not an exception. The first implementation wrapped it in `try`/`except`
and was verified by pointing `AT_SPI_BUS_ADDRESS` at a nonexistent socket:
the whole process dumped core, exactly as `Gtk.init()` had on a dead display
two findings earlier. In-process that would have meant every periodic save
dying before it wrote anything, on any machine whose a11y bus wasn't up.
Re-verified after the change: with the bus broken the child dies, the parent
survives, `atspi_window_tabs()` returns None and the even split takes over.
`sessionctl-save.service` also gained `TimeoutStartSec=60`, so a hung peer
now costs one failed run rather than a pile of stacked ones.

**An eighth finding, in the same place the seventh was.** With tabs, dirs and
envs all restoring correctly, the *workspaces* did not: terminal windows came
back on each other's desktops. Capture was right -- the recorded workspaces
matched the live ones exactly -- and so was placement. Matching was the
problem, for the same reason the tab grouping had been: terminal windows are
mutually indistinguishable. They share a `wm_class`, share a PID (all of them
belong to `gnome-terminal-server`), share a geometry when maximized, and
share a title whenever their active tabs sit in the same directory. Four
records saying "put a Gnome-terminal on workspace N" were applied to whichever
window came first.

What tells them apart is what they contain. Each terminal record now carries
a signature -- how many tabs, and which directories those tabs are in -- built
by pairing AT-SPI's frames with Wnck's windows sorted by XID. That pairing was
verified rather than assumed, with a controlled marker: a throwaway terminal
opened in a distinctive directory landed at the same index in both views, and
stayed matched after being moved to another workspace. If the two views ever
disagree on how many terminal windows exist, the signatures are dropped
wholesale and matching degrades to class-and-title rather than pairing things
up wrongly.

The signature ranks above title similarity and below the `new` flag, so it
distinguishes terminals from each other without reintroducing the bug where a
record binds to the terminal you launched `restore` from. Verified live on the
exact failure shape: two throwaway windows with byte-identical titles, one
holding two tabs and one three, put on different workspaces, deliberately
swapped, then placed from the capture -- each returned to its own workspace,
where title-only matching had been a coin flip.

**A ninth finding, and the one that actually explained the workspaces.**
Even with terminal windows correctly identified, they all came back on
workspace 0. Capture was right, matching was right, and placement was right
-- for workspaces that existed. `_place_one` did `ws = screen.get_workspace(n)`
and, when that returned None, silently did nothing, while `place()` appended
the record to `placed` regardless. A target that did not exist yet was
skipped in silence and never revisited.

After a reboot that is the *normal* case. Every window starts on workspace 0,
so only workspace 0 and one trailing empty workspace exist; with
dynamic-workspaces, workspace N exists only once 0..N-1 are occupied.
Terminals map in under a second while VSCode takes several, so the first poll
tried to put terminals on workspaces 2-5 that could not exist until VSCode
had claimed workspace 1. They stayed where they were, and `place` reported
"placed 8 of 8".

`_place_one` now returns whether the workspace move happened, and `place()`
re-queues records whose workspace does not exist yet so they are retried once
the cascade below them has grown the desktop far enough. Geometry and
maximized state are still applied on a deferred attempt, both being
idempotent. A pump after each successful move lets GNOME append the next
trailing workspace before the following record asks for it.

Two supporting changes fell out of the retry. The default timeout rose from
20s to 45s, because the cascade can only advance as fast as the apps below it
appear; and `place()` now gives up early once nothing has been placed for
`IDLE_GRACE` (15s), because a snapshot almost always names windows that are
not running -- two Awesun windows here -- and waiting the full timeout for
those every time made restore feel broken. Measured on the real snapshot:
19.4s to place everything correctly and report the two Awesun records
unplaced, against 45.3s before the early exit.

Verified by reproducing the post-reboot shape exactly -- every window herded
onto workspace 0, two workspaces in existence -- and then running the real
`place()` from the pre-reboot snapshot: terminals ended on 0/2/3/4/5 matched
by tab count, Chrome on 0, VSCode on 1, exactly as recorded.

**A tenth finding: a busy tab is an invisible tab.** A restore put two
five-tab terminal windows on each other's workspaces -- 1 and 4 swapped --
with everything else correct. The signature from the ninth finding was
supposed to prevent exactly this, and it had not, because of where its
directories come from. AT-SPI reads each tab's directory *off the prompt*,
and a tab only has a prompt when it is at one. In the snapshot that failed,
the tab that made its window distinctive was sitting in
`.../TensorRT-LLM/examples` running something, so its last line was not a
prompt and it contributed `""` to the signature. That window recorded
`{tabs: 5, cwds: ["", "~"]}`; the other five-tab window, all tabs in `~`,
recorded `{tabs: 5, cwds: ["~"]}`. Neither record could match its own
restored window exactly -- restored tabs are all freshly at prompts, so they
report real directories -- so both scored 1 (tab count only) against both
windows, and `max()` broke the tie by list order.

The directory was never actually unknown: the shell hook writes each tab's
cwd on every prompt, whatever that tab is doing now, and `collect_terminals`
already had it. Capture now builds the signature from the hook's record
instead of the prompts, keeping the AT-SPI frames only for the tab *count*
and the window pairing. Live-checked on the desktop that produced the bad
restore: the two five-tab windows now record `["~"]` and
`["~", "~/Projects/agents/trtllm-work/TensorRT-LLM/examples"]` -- distinct,
and exactly what their restored windows will report.

That alone would not have been enough. Terminals map within a second of each
other but their shells draw prompts a beat later, so for that beat every
same-tab-count window still looks identical, and `place()` was placing on the
first poll. `match_windows` now takes `strict`, which refuses to resolve a
tie at all and leaves the record pending, and `place()` runs strict for its
first `SETTLE_GRACE` (8s). Deferring costs a poll interval and lets the
prompts arrive; it also lets the records that *do* match unambiguously claim
their windows first, which usually leaves a single candidate for the
ambiguous ones -- no longer a tie. After the settle window a genuinely
ambiguous record is placed rather than dropped, so nothing gets stuck.

**An eleventh finding: a second monitor is not a sticky window.** The same
restore was the first one done with two monitors attached, and it exposed a
conflation in capture that had been there from the start: `"sticky": ws is
None`. With mutter's default `workspaces-only-on-primary=true`, *every*
window on a non-primary monitor has no workspace at all. Probed live on this
machine: Chrome and two VSCode windows on the second monitor all report
workspace `None`, `is_pinned()` True, and `is_sticky()` **False**, while the
genuinely sticky window -- the GNOME Shell overlay -- reports `is_sticky()`
True. So every window on the second monitor was being recorded as sticky, and
restoring a sticky record calls `pin()`, which really does stick the window
to every workspace. A window that belonged to one workspace came back
following the user everywhere -- and worse, the next capture then saw a
genuinely sticky window and recorded it as such, so the damage survived into
every later snapshot.

Capture now uses `w.is_sticky()`, and `_place_one` pins only what was really
pinned; a record with no workspace and no sticky flag gets its geometry
applied and nothing else, which puts it back on the second monitor, where
mutter takes it off the workspace list again by itself. Verified live: the
two shell overlays record sticky, the three second-monitor windows record
`workspace: None, sticky: False`.

One consequence of that split needed handling. A window recorded at x=1920
and restored with no second monitor attached is placed exactly where asked --
somewhere legal for X11 and invisible to the user, with nothing to rescue it,
since as far as the WM is concerned it is not offscreen. Previously the
spurious `pin()` at least kept it reachable. `_onscreen` now pulls the origin
back inside the screen when the recorded coordinates fall outside it, leaving
width and height alone: a window sized for a 2560x1440 monitor coming back on
a 1920x1080 one is oversized but visible and draggable, which is recoverable
in a way that x=1920 with no monitor there is not.

Worth stating plainly, because it looks like a bug and is not: restore
compacts recorded workspace indices to a gap-free sequence
(`_compact_workspace_indices`), so a snapshot with windows on 0, 1 and 3 and
nothing on 2 comes back on 0, 1 and 2. Under dynamic-workspaces an empty
middle workspace cannot be recreated at all -- GNOME keeps exactly one
trailing empty workspace -- so there is nothing to preserve. It does mean
that if the only occupants of a workspace were on the second monitor, that
index disappears from the recorded set and everything above it shifts down by
one. `--static-workspaces` opts out, for a desktop that has already been
switched to `dynamic-workspaces=false`.

**A further finding, once four monitors were attached: VSCode windows came
back on the wrong displays.** With `workspaces-only-on-primary` set, windows
on the three non-primary monitors have no workspace at all, so which display
a window lands on is decided purely by its geometry -- and geometry is
applied to whichever live window the record matched. Matching was the fault
again, and for the third time it was identity: every VSCode window shares one
Electron PID (checked: four windows, all pid 6349, identical cmdline), so PID
separates them no better than it separates gnome-terminal windows, leaving
only the title.

The title leads with the *open file*, which changes constantly, and
whole-title similarity is dominated by it. Measured on the real snapshot: the
recorded LLM window, titled `Preview PoC.zh.md - LLM - Visual Studio Code`,
scored **0.696** against the *resume* window and only **0.639** against its
own, because by restore time it was showing a different file. It claimed
resume's window, resume took whatever was left, and both were placed with
each other's geometry -- on each other's monitors.

The folder segment of the title does not move when the open file does, so
VSCode windows are now identified by it (`_vscode_folder`), ranked exactly
where the terminal tab signature is: above title similarity, below the `new`
flag. Strict matching was also widened -- it used to defer only on an exact
score tie, which floating-point title similarity essentially never produces.
It now also defers when a record *has* an identity and no candidate matches
it yet, which is the state of a VSCode window still titled "Visual Studio
Code" while it loads. `SETTLE_GRACE` rose from 8s to 20s to cover that, and
the `IDLE_GRACE` early exit no longer fires while strict matching is
deliberately waiting -- otherwise the two mechanisms would fight and the
deferred records would be abandoned.

Verified live on the four-monitor desktop: the four VSCode windows were
deliberately scattered onto the wrong monitors and the capture replayed --
all four returned to their own display, matched by folder, in 3.3s.

**The next finding was not a restore bug at all: `save` had been dead for
25 hours.** Windows came back not matching the session before shutdown,
because `latest.json` was a day old. Every run of `save` had been crashing:

```
File ".../snapshot.py", line 183, in digest
    "layout": sorted(
TypeError: '<' not supported between instances of 'int' and 'NoneType'
```

`workspace` is None for every window mutter gave no workspace -- a sticky
one, or, under `workspaces-only-on-primary`, *every* window on a non-primary
monitor. Sorting the raw tuples compares that None against an int as soon as
two windows share a `wm_class`, which Python 3 refuses. Four monitors is what
made that possible: with one monitor the only workspace-less window was the
GNOME Shell overlay, whose class is unique, so the comparison never got past
the first tuple element. Three classes on this desktop now carry a mixed
list -- `Gnome-terminal [0, None, 0, 0, 0]`, `code [0, None, 0, 0]`,
`Google-chrome [0, None]`.

Fixed with a None-safe sort key that is otherwise element for element what
tuple comparison already did, so nothing churns: checked against all 20
stored snapshots, 19 sort identically and the 20th is the one the old code
could not sort at all.

The more useful lesson is that it was invisible. The service failed loudly
into the timer's journal every 30 seconds for a day, and the first symptom
anyone saw was a restore replaying the wrong session. `restore` now states
the age of what it is about to replay when that is over an hour, and says
where to look -- the one moment the user is definitely paying attention.

Regression tests pin all seventeen bugs found this way. What unit tests cover
instead of live testing: D-Bus-unavailable fallback, multi-window tab
grouping, missing or malformed VSCode/Chrome source files, multiple Chrome
profiles, the VSCode `openedWindows`/`lastActiveWindow` dedup, every
write-avoidance branch in `snapshot.py` (including a same-second filename
collision that a first pass of the tests caught for real, and every layout
digest field), every branch of `restore.py`'s command construction --
including one that initially failed for the wrong reason: a test asserting
on a *single* pass of shell-unquoting, when the `--command=` value genuinely
needs two passes (gnome-terminal's own parser, then bash -c's) before a
conda name with an embedded quote comes back out whole, fixed in the test
once the two-stage unwrap was worked through by hand and confirmed correct,
not the code -- and `layout.match_windows`/`_compact_workspace_indices`,
both pure functions with no Wnck or X11 involved.

`setup.py`'s system-dependent facts were checked directly rather than
assumed: `systemd-analyze --user verify` against the real generated unit
files (the only complaint before `~/.local/bin/sessionctl` existed was the
missing executable -- exactly what `setup` creates), `DISPLAY`/`XAUTHORITY`
confirmed present in `systemctl --user show-environment`, and the Chrome
`Preferences` edit worked out against a scratch copy of the real file before
any code was written -- it needed `setdefault('session', {})`, not a plain
key assignment, since a fresh profile's `Preferences` has no `session` key
at all yet. `sessionctl setup` was then run for real: the timer enabled, and Chrome's preference set once Chrome happened to be closed.

**A real reboot then found a bug `--dry-run` and `systemd-analyze` could
never have caught, because it only exists across an actual login boundary.**
Symlinking the three systemd unit files was consistent with the
deploy-by-symlink convention used for `bash-hook.sh` and
`~/.local/bin/sessionctl` elsewhere in this repo, but wrong here specifically.
Confirmed via `journalctl -b 0`: on reboot the kernel comes up with the data
partition unmounted, same as always -- but `systemd --user` starts and tries
to load its unit files at the moment you log in, while the partition holding
the symlinks' targets doesn't actually get mounted until **~76 seconds
later**. During that window the symlinks are dangling, so systemd logs
`Failed to open .../sessionctl-save.timer: No such file or directory` and
marks the unit **not-found for the rest of the session** -- permanently:
systemd never retries loading a unit file that failed once, and nothing in
this chain ever runs `daemon-reload` again once the disk shows up. Net
effect: the timer never fired once, for the entire session, confirmed by its
total absence from that session's journal, even though `systemctl --user
list-timers` had shown it `active` right after `setup` ran, before the
reboot.

The 76-second mount in this particular test was a manual action (opening
Nautilus/Files), not an automatic one -- confirmed with the user, who did
it themselves, along with manually launching VSCode afterward. That matters
beyond just explaining this one test run: the mount genuinely is not
automatic (see "Why Stage 0 exists" -- `HintAuto: false`), so on a reboot
where nobody is at the keyboard to open Nautilus or a folder, the disk could
stay unmounted far longer than 76 seconds, or indefinitely. This unit-file
bug is now fixed regardless of that timing, but it's worth being clear that
the periodic timer firing does not by itself mean the data partition is
mounted -- `save`'s own Stage 0 check inside each run is what actually
protects against writing a degraded snapshot in that case, correctly
refusing every single tick until a human mounts the disk (via Nautilus, or
`sessionctl mount` themselves).

Fixed by making the three unit files the **one deliberate exception** to
symlink-over-copy: `setup` now copies them into `~/.config/systemd/user/`
instead (`_ensure_copy`, re-copying only when the repo's source content
changed, so re-running `setup` after an edit still propagates it). This puts
every path systemd needs to *load* the unit definitions on the always-mounted
root filesystem. Re-verified live after the fix: the timer fired unattended,
on its own schedule, with no manual trigger.

**The next reboot found a second bug one layer deeper, in the same family.**
`ExecStart=%h/.local/bin/sessionctl` was still a symlink into the data
partition, and this README originally reasoned that resolving it lazily at
exec time would be late enough for `save`'s own Stage 0 degradation to
already handle an unmounted disk correctly -- that reasoning was wrong, and
this reboot proved it directly. With the unit files now loading correctly,
the timer fired on schedule three separate times before the disk was
mounted, and each time systemd's EXEC step failed outright:

```
sessionctl-save.service: Failed to locate executable /home/shengcn/.local/bin/sessionctl: No such file or directory
sessionctl-save.service: Failed at step EXEC spawning ...: No such file or directory
sessionctl-save.service: Main process exited, code=exited, status=203/EXEC
```

`save`'s Stage 0 code never ran at all in any of the three failures -- the
Python process never started, because the executable systemd tried to spawn
didn't resolve. A degraded-disk *skip* and an EXEC failure look identical
from the outside (no snapshot written) but are not the same thing: one is
handled gracefully by design, the other is a hard failure with no code of
sessionctl's own involved.

Fixed at the time the same way as the first bug, with one twist:
`~/.local/bin/sessionctl` couldn't simply become a copy of the real entry
point, because that script does `sys.path.insert(0, dirname(__file__))` to
find `sessionctl_lib/` alongside it -- copying just the one file would break
its own imports, and copying the whole package looked, at the time, like it
would mean abandoning in-place repo development for a real install step. So
`setup` generated a tiny static shell wrapper that checked whether the real
entry point was reachable *before* trying to exec into it, printing a clear
message and exiting 0 if not -- the same graceful skip Stage 0 gives an
unmounted disk everywhere else, just one layer earlier. Re-verified live: the
timer fired unattended, on its own schedule, immediately after `setup`
deployed it. **This wrapper has since been superseded** -- see the third bug,
below -- but is documented here because it's the reasoning the third bug's
fix had to overturn.

**A structural bug remained even with both fixes above, found not by a
reboot but by trying to run `sessionctl mount` directly while the disk was
unmounted.** It failed with `sessionctl:
.../stock_03_claude/sys_workspace/sessionctl not reachable (data partition
not mounted?)`. The wrapper was doing exactly what it was designed to do --
but that design was itself the bug: `sessionctl mount`'s entire *purpose* is
to mount the data partition, yet its own Python code
(`sessionctl_lib/mounts.py`) lived on that same partition, symlinked into
`~/.local/bin/sessionctl`'s target. No amount of correctness in the wrapper's
reachability check could fix this -- the tool could never bootstrap the one
disk it was supposed to bootstrap, because it wasn't runnable until that disk
was already mounted by some other means. This was already true even before
the wrapper fix (the old dangling symlink had the identical problem, just
with a cruder `status=203/EXEC` error instead of a clean message) -- the
wrapper made the failure legible but never could have fixed it, because
checking reachability and *being* reachable are different problems.

Fixed by abandoning symlink-into-repo for the executable code entirely:
`setup` now deploys the whole `sessionctl_lib/` package plus the entry
script to `~/.local/share/sessionctl/lib/` (`_ensure_package_copy`, mirroring
every `.py` file and removing stale deployed modules, re-syncing only when
content actually changed), and `~/.local/bin/sessionctl` symlinks to *that*
deployed copy instead of into the repo. The entry script itself needed one
line fixed to make this correct: it located `sessionctl_lib/` via
`os.path.abspath(os.path.dirname(__file__))`, which does not resolve
symlinks, so running it through `~/.local/bin/sessionctl` was finding
nothing next to the symlink itself. Switched to `os.path.realpath`, which
does resolve the symlink chain, so the script now always finds
`sessionctl_lib/` next to whichever copy it's actually running from --
verified directly by building a throwaway package+symlink structure and
running the resulting entry script as a real subprocess. Re-verified live
end to end: `sessionctl mount` now succeeds while invoked through
`~/.local/bin/sessionctl` with the disk unmounted, with zero references to
the repo's path anywhere in the deployed copy (checked with `grep -r` over
`~/.local/share/sessionctl/lib/`); the systemd timer still fires correctly
through the new deployment shape; and a repeat `setup` run is idempotent
(one summary line, no file churn) when nothing changed.

What unit tests cover: the symlink safety logic for `bash-hook.sh` (refuses
to clobber a non-symlink, idempotent on an already-correct link, dry-run
touches nothing); the parallel copy/write safety logic shared by the unit
files and the deployed package (idempotent when content matches, re-copies
when the source changed, removes stale deployed modules whose repo source is
gone, sets the exec bit when asked); the entry script's `realpath`-based
symlink resolution, run as a real subprocess against a throwaway
package+symlink structure; and the `~/.bashrc` hook line's add/remove round
trip -- all against temp files or throwaway subprocesses, nothing in the
suite touches the real `~/.bashrc` or `~/.local`. What was found by testing
that pure unit tests structurally cannot reach at all: the two reboot-timing
bugs, each of which only exists across a real login boundary no test harness
simulates, and the package-deployment bug, found by running `sessionctl
mount` directly against a real (if artificially unmounted) disk rather than
through any reboot at all.

**The boot guard from the sixth failure then lost 26 days of session.**
The symptom was the familiar one: after a reboot on 2026-10-11, `restore`
brought back one terminal tab instead of the session from before the
reboot. The snapshot it replayed was from 2026-09-15, three boots earlier.
The save timer's journal shows why:

| Period | `save` outcome |
|---|---|
| 2026-09-15 07:18 -- 07:31 | saving normally; last snapshot 07:31 (1 tab) |
| 2026-09-15 07:35 -- 2026-10-11 10:55 | **refused 72,777 times**: "latest.json holds a session from a previous boot ... that hasn't been restored yet" |
| 2026-10-11 10:55 | `restore` replays the 07:31 snapshot; saving resumes |

The guard did exactly what it was written to do. `save` would not overwrite a
previous boot's session until `restore` or `adopt` had run, and after the
Sep 15 07:34 reboot neither did. Nothing ever released it. It held through
the Sep 15 10:50 and Sep 27 21:05 reboots too, because each new boot was
also "not restored", and it kept protecting the *oldest* session rather
than the one just left. Everything open from Sep 27 to Oct 11 was never
captured. The hook's state files for those shells were deleted by their
EXIT traps at shutdown and by the first unrefused save's prune, so it is
not recoverable. The only signals were the timer's journal and a NOTE in
`sessionctl list`, the same kind of silence the 25-hour `save` crash above
hid in. The restore-time staleness warning did fire (26 days), but only as
a warning after the fact. It also fired after any shutdown longer than an
hour, which is a good way to teach yourself to ignore it.

The fix keeps the guard's purpose and drops the pause. `save` always
proceeds. The first time a new boot's snapshot would replace `latest.json`,
`store()` copies the old one to `previous_boot.json`
(`snapshot._pin_previous_boot`). `restore` with no `--snapshot` replays
`snapshot.restore_target()`. That is `latest.json` while nothing has been
stored under this boot yet (e.g. the disk is still unmounted and saves refuse
as degraded), and the pinned copy afterwards. So the 47-second race from the
sixth failure stays closed, and rebooting any number of times without
restoring never falls more than one boot behind. `adopt` and `save --force`
existed only to release the guard, and are gone. `restore`, `list` and
`place --snapshot previous` say which snapshot they mean.

The staleness warning is now boot-aware, so it fires for the two real
failures and stays quiet otherwise:

- **From the boot that just ended:** it compares when saving last confirmed
  the snapshot (`snapshot.last_verified`) against when that boot's journal
  stops (`cli.previous_boot()`, the last entry of `journalctl -b -1`). A gap
  over an hour means saving died before shutdown. How long the machine was
  then off is irrelevant.
- **From an older boot:** it warns that nothing was saved during the boot
  that just ended. That is exactly the Oct 11 case. Run against a copy of
  the Sep 15 snapshot, it says "is from an older boot, not the one that
  ended 2026-10-11T10:29:20".
- **From this boot:** the old one-hour rule, measured from `last_verified`
  rather than `ts`. An idle desktop no longer looks stale just because it
  hasn't changed.

`last_verified` comes from the heartbeat, which now records its boot id.
The first heartbeat of a new boot moves the old one under `previous`, so the
pre-reboot session's last confirmation survives the reboot.

Verified end to end against throwaway state dirs and the real desktop:
`save` writes immediately where it used to refuse, pins the Sep 15 snapshot,
and `restore --dry-run` keeps targeting it; `--snapshot latest` replays this
boot's two tabs. A previous-boot snapshot confirmed 15s before shutdown
restores with no warning, even though its `ts` is 12 hours older; the same
snapshot last confirmed 4.5 hours before shutdown warns.

## Note on `python3`

Scripts use `#!/usr/bin/python3` explicitly, never `env python3`. Bare `python3`
here resolves to `~/miniconda3/envs/torch209/bin/python3`, and this tool must
not depend on whichever conda env happens to be active — least of all while
restoring the terminals that set those envs up.
