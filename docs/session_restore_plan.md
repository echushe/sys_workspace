# `sessionctl` — save & restore the desktop session across reboots

## Context

After every Ubuntu restart you manually rebuild your whole working setup: terminal
tabs and their conda envs, VSCode workspaces, and browser tabs. Nothing on this
machine currently does that automatically — `~/.config/autostart/` is **empty**, so
no application is relaunched at login at all.

Probing the live system showed the problem is smaller than it looks, and splits in three:

| Layer | Current state | What's actually needed |
|---|---|---|
| **The disk holding your work** | Mounted on demand by udisks, **not** at boot — `~/Projects` dangles until you open it in Files | Mount it first, before anything else |
| **Relaunching apps at login** | Nothing autostarts | The core gap — one restore command |
| **VSCode window set** | *Already tracked* by VSCode in `windowsState.openedWindows`, and it self-restores on relaunch | Just relaunch `code`; snapshot as a safety net |
| **Chrome tabs** | `restore_on_startup` is **unset** → opens a blank New Tab page | One-time pref flip to "continue where you left off" |
| **Terminal tabs + conda envs** | No native restore of any kind | Real work: custom capture + replay |
| **Which workspace each window is on** | Lost entirely on reboot | Record per-window desktop index; replay via Wnck |

So most of this is orchestration, and only the terminal layer needs genuinely new
mechanism. Outcome: after a reboot you run one command, `sessionctl restore`, and
your terminals, VSCode workspaces, and browser tabs come back.

### Verified environment facts

- Ubuntu 22.04.5, GNOME Shell 42.9, **X11** (`XDG_SESSION_TYPE=x11`) — window
  enumeration and placement work. On Wayland they would not.
- **libwnck3 is importable from the system Python** via
  `gi.require_version('Wnck','3.0')` — verified working. It supplies window class,
  title, PID, workspace index, maximized state, geometry, *and* `move_to_workspace()`.
  This means **nothing needs to be installed and no `sudo` is required**;
  `wmctrl`/`xdotool` are not needed.
- **`org.gnome.mutter dynamic-workspaces` is `true`** — workspaces are created and
  destroyed on demand. This is the main constraint on layout restore (see
  "Workspace layout", below).
- Current layout: 5 workspaces (4 occupied + 1 trailing empty). WS0 = Chrome +
  4× Nautilus + WeChat, WS1 = both VSCode windows, WS2 = 2× Awesun, WS3 = terminal.
- **`~/Projects` and `~/extra_disk_2t` are symlinks, not mounts** — and both point
  into the *same* partition:
  - `~/Projects`      → `/media/shengcn/d8d577a6-…/projects/`
  - `~/extra_disk_2t` → `/media/shengcn/d8d577a6-…/dataset/`

  That partition is `/dev/nvme0n1p6`, ext4, 259 G,
  UUID `d8d577a6-bf6a-42cc-a513-c02318d27956`. **Mounting that one device satisfies
  both symlinks.**
- **It is not in `/etc/fstab`** (which lists only `/`, `/boot/efi`, swap). It is mounted
  by **udisks2** as a transient systemd unit, and `udisksctl info` reports
  `HintAuto: false` — so it is *not* mounted at boot. It appears only when something
  (usually Nautilus) opens it. **This is the root cause of the reboot pain:** until that
  disk is mounted, `~/Projects/...` does not resolve, so VSCode folders, terminal cwds
  and this repo are all missing.
- `/media/shengcn/files` (ntfs3) and `/media/shengcn/OS` (ntfs3) are mounted the same
  transient way — optional additions to the mount list.
- Currently open: 1 gnome-terminal window (6 bash tabs, all in `~/Downloads`),
  2 VSCode windows, 1 Chrome window, 4 Nautilus, WeChat, Awesun.
- conda at `~/miniconda3`, envs: `base`, `torch209`, `torch212`.
- `/usr/bin/python3` is 3.10.12 with `sqlite3` — **must be used explicitly**, because
  bare `python3` resolves to `~/miniconda3/envs/torch209/bin/python3` and the tool
  must not depend on whichever conda env happens to be active.
- Firefox is a **snap** (profile `~/snap/firefox/common/.mozilla/firefox/doe8v6so.default`)
  and is not running / effectively unused.

### Decisions taken

- **Stage 0 mounts storage before anything else restores**, and aborts the whole run
  if a required path cannot be made to resolve.
- Restore is a **manual command**, not an autostart entry.
- **Relaunched** by the tool: **terminals + conda**, **VSCode workspaces**,
  **browser tabs**. Not relaunched: Nautilus, WeChat, Awesun.
- **Workspace layout is captured for _every_ window** — including ones the tool does
  not launch — and replayed onto whatever windows exist at restore time. Window
  geometry and maximized state ride along, since Wnck exposes them for free.
- Snapshots are **periodic (~30 s)**, so a crash or power loss
  doesn't lose the session.
- A **`.bashrc` hook is acceptable** — required for conda capture (see below).

---

## Why the conda capture needs a shell hook

`conda activate` mutates the *running* shell's `PATH` and variables. It does not
re-exec, so `/proc/<pid>/environ` — which is frozen at exec time — never shows it.
Confirmed on the live shells: no `CONDA_*` variables are visible externally.

The only reliable capture point is inside the shell itself. A `PROMPT_COMMAND` hook
writes the tab's state to a small file whenever it changes.

Usefully, `$GNOME_TERMINAL_SCREEN` **is** in `/proc/<pid>/environ` (verified — one
unique D-Bus screen path per tab), and it is absent from VSCode's integrated
terminals. Gating the hook on that variable cleanly restricts capture to real
gnome-terminal tabs and excludes VSCode/Claude Code shells for free.

---

## Architecture

Source lives at `~/Projects/sys_workspace/`, on its own. This has now gone both
ways, and the reasoning is worth keeping rather than overwriting. The original
plan called for a standalone repo, on the grounds that a desktop-session tool
has nothing to do with a stock-trading codebase. That was superseded when
`sys_workspace/` was created inside `stock_03_claude` and everything after it
(Stage 0, terminal capture, the VSCode/Chrome collectors) was built and
committed there directly. It has since moved out again, which a dependency
audit showed costs nothing: the tool imports only the standard library, `gi`
and `yaml`; nothing in that repo ever referenced it; and there was no shared
packaging, test discovery or CI to bind them -- the single edge between them
was this document's own path, which moved with it.

The move does not disturb the running installation, because nothing deployed
points at the source tree: `~/.local/bin/sessionctl` is a symlink to the
deployed copy under `~/.local/share/sessionctl/lib/`, while the unit files and
`bash-hook.sh` are real copies, all of them on the always-mounted home
partition. Re-running `setup` from the new location is what re-syncs them.

Note the new path is itself on the late-mounting data partition
(`~/Projects` is a symlink into it), so the source is unreadable until that
disk is mounted. That is exactly the bootstrap problem the deployment layout
was built to survive, and it survives it here: `sessionctl mount` runs from
the deployed copy, not from this tree.

```
~/Projects/sys_workspace/           # source, standalone
  sessionctl                        #   thin CLI shim, #!/usr/bin/python3
  sessionctl_lib/
    __init__.py
    paths.py                        #   XDG paths, snapshot dir handling
    mounts.py                       #   Stage 0: ensure storage is mounted
    collect.py                      #   terminals / vscode / browsers
    layout.py                       #   workspace + geometry capture/replay (Wnck)
    restore.py                      #   replay logic
    setup.py                        #   symlink deployment, systemd units, Chrome prefs
    cli.py                          #   argparse, subcommands
  bash-hook.sh
  systemd/  sessionctl-save.{service,timer}
  README.md
  docs/     session_restore_plan.md   # this document
  tests/

# state and config, both XDG-standard and independent of source location:
~/.local/state/sessionctl/shells/<pid>.json          # live per-tab state
~/.local/state/sessionctl/snapshots/<ts>.json        # rotated, keep 20
~/.local/state/sessionctl/latest.json
~/.local/state/sessionctl/heartbeat.json
~/.config/sessionctl/config.yaml
~/.config/systemd/user/sessionctl-*.{service,timer}  # COPIED by `sessionctl setup`
~/.local/share/sessionctl/lib/                       # DEPLOYED copy of sessionctl_lib/ + entry script
~/.local/bin/sessionctl                              # symlink to the DEPLOYED copy, not the repo
```

**Nothing that runs at login is symlinked into the repo any more.** `setup`
(`sessionctl_lib/setup.py`) deploys the systemd unit files, the whole
`sessionctl_lib/` package, the entry script and `bash-hook.sh` as real copies under
`~/.config/systemd/user/` and `~/.local/share/sessionctl/`; the single remaining
symlink, `~/.local/bin/sessionctl`, points at the *deployed* entry script, so both
of its ends sit on the always-mounted home partition and it cannot dangle. This was
arrived at the hard way, across four separate rounds of live testing, one bug layer
deeper each time.

Earlier revisions of this plan argued the opposite for `bash-hook.sh` specifically —
that a symlink was right because a copy "would silently drift out of sync on the next
repo edit", and that this one was safe regardless since the `~/.bashrc` line guards
with `[ -f ... ]` and so degrades gracefully. Both halves proved wrong in practice:
drift is a development inconvenience solved by re-running `setup`, whereas the
"graceful" degradation meant a terminal opened before the partition mounted loaded no
hook at all and captured nothing for its entire life, silently — see the fourth
finding below.

First: `systemd --user` loads its unit files at login, which on this machine happens
~76 seconds *before* the data partition holding a symlink's target actually gets
mounted (confirmed via `journalctl -b 0`; that particular 76s mount was a manual
action, not automatic — the disk genuinely never auto-mounts, see "Stage 0" below). A
dangling symlink there makes systemd fail to load the unit and mark it not-found for
the rest of the session, permanently — nothing re-triggers a `daemon-reload` once the
disk later shows up, so the timer never fires again until the next reboot. Fixed by
copying the three unit files (`_ensure_copy`) into `~/.config/systemd/user/`, keeping
every path systemd needs to *load* the unit definitions on the always-mounted root
filesystem.

Second, on the next reboot, one layer deeper: with the unit files now loading
correctly, `ExecStart=%h/.local/bin/sessionctl` was still a symlink into the data
partition. The timer fired on schedule three separate times before the disk was
mounted, and each time systemd's EXEC step failed outright (`status=203/EXEC`,
"Failed to locate executable") *before* `save`'s own Stage 0 degradation logic — which
handles an unmounted disk correctly — ever got a chance to run, because the process
never started. An earlier version of this plan reasoned that resolving the symlink
lazily at exec time would be late enough to avoid this; that reasoning was wrong, and
this reboot proved it directly. At the time this was fixed with a generated shell
wrapper that checked reachability before exec'ing into the real entry point — that
wrapper has since been removed; see the third bug below for why.

**Third, found not by a reboot but by running `sessionctl mount` directly against an
unmounted disk: the tool could never bootstrap the one disk it exists to bootstrap.**
Even with the wrapper fix above, `~/.local/bin/sessionctl` (via the wrapper) still
pointed at `sys_workspace/sessionctl` inside this repo, and the repo itself lives on
the data partition. `sessionctl mount`'s whole purpose is to mount that partition, but
its own code — `sessionctl_lib/mounts.py` — was unreachable until the partition was
*already* mounted by some other means (Nautilus, a manual `udisksctl`). The wrapper
made this failure legible (`sessionctl: .../sessionctl not reachable (data partition
not mounted?)`) instead of a raw `status=203/EXEC`, but could never have fixed it —
checking reachability and *being* reachable are different problems, and this one
predates even the wrapper: the original dangling symlink had the identical bootstrap
gap, just with a cruder error.

Fixed by giving up on symlink-into-repo for the executable code specifically: `setup`
now deploys the whole `sessionctl_lib/` package plus the entry script to
`~/.local/share/sessionctl/lib/` (`_ensure_package_copy`, mirroring every `.py` file,
removing stale deployed modules whose repo source is gone, re-syncing only when
content actually changed), and `~/.local/bin/sessionctl` symlinks to *that* deployed
copy rather than into the repo. One line in the entry script had to change to make
this correct: it located `sessionctl_lib/` via `os.path.abspath(dirname(__file__))`,
which does not resolve symlinks — running it through `~/.local/bin/sessionctl` was
therefore looking for `sessionctl_lib/` next to the symlink itself, not next to the
deployed copy the symlink points at. Switched to `os.path.realpath`, which does
resolve the symlink chain, confirmed by building a throwaway package+symlink
structure and running the resulting entry script as a real subprocess.

`_ensure_copy` and `_ensure_package_copy` both re-write only when source content has
changed, so re-running `setup` after an edit still propagates it — the drift risk a
copy usually carries, accepted here because it is outweighed by the bugs a
symlink-into-repo reintroduces for anything systemd or a cold bootstrap depends on.
All three fixes were re-verified live: the timer fires unattended on its own schedule
regardless of unit-file or exec-path deployment, and `sessionctl mount` now succeeds
via the deployed path with the disk unmounted, with zero references to the repo's
path anywhere under `~/.local/share/sessionctl/lib/` (confirmed with `grep -r`).
There is still no separate `install.sh`; `setup` is that installer.

### Stage 0 — ensure storage is mounted (runs before everything else)

Every other stage depends on this one. `~/Projects` is a symlink into a partition that
is **not mounted at boot**, so a restore that runs too early would open VSCode on
non-existent folders and drop every terminal into `$HOME` instead of its recorded cwd —
worse than not restoring at all, because it looks like it worked.

**Required-mounts list**, in config, seeded with the one that matters:

```yaml
mounts:
  - uuid: d8d577a6-bf6a-42cc-a513-c02318d27956   # ext4, 259G
    mountpoint: /media/shengcn/d8d577a6-bf6a-42cc-a513-c02318d27956
    verify: [~/Projects, ~/extra_disk_2t]        # symlinks that must resolve
```

Both requested paths resolve to this single device, so one mount covers both. The
mountpoint must stay exactly as-is — it is baked into every recorded path, and the
partition has no filesystem LABEL, which is why udisks uses the UUID as the directory
name.

**Procedure**, per entry:

1. Already mounted (`os.path.ismount`)? Skip.
2. Otherwise `udisksctl mount -b /dev/disk/by-uuid/<uuid>`. No `sudo` needed for the
   console user, though PolicyKit may raise a password prompt for an internal drive —
   which is precisely why this happens up front, interactively, rather than mid-restore.
3. Confirm each `verify` path resolves (`os.path.realpath` + `exists`).
4. **Any failure aborts the entire restore** with the failing path named. Never restore
   into dangling symlinks.

**Also guards `save`:** a snapshot taken while a required mount is missing is marked
`degraded: true` and is not stored at all. Otherwise the 2-minute timer would
happily overwrite a good snapshot with an empty one during the window after boot and
before you mount the disk. (This section originally said such a snapshot is written
but "does not become `latest.json`". Refusing to store it outright is strictly safer
and is what the code does: a degraded snapshot written every 30 seconds would fill the
20-entry ring and evict the good snapshot regardless of which one `latest` pointed at.)

**A third `save` guard, for a case where nothing is broken at all.** After a reboot the
timer captures the freshly booted desktop *accurately* and promotes it to `latest.json`
— measured at 47 seconds from login — so the session you meant to restore is gone
before you type `sessionctl restore`. Neither guard above applies: the capture is
correct, the desktop is visible, and only the *identity* of the session is wrong. Every
snapshot therefore records the kernel's boot id (`/proc/sys/kernel/random/boot_id`), and
`save` refuses to overwrite a `latest.json` belonging to an earlier boot until that
session has been restored (which marks the boot) or explicitly abandoned via
`sessionctl adopt`. Snapshots written before this field existed are never blocked, so
upgrading cannot freeze saving. Note this interacts with Stage 0: while the disk is
unmounted saves already refuse as degraded, so the exposure window opens the moment
`sessionctl mount` succeeds and closes when `restore` runs — previously a 30-second race
the user had to win by hand.

**A second `save` guard, from the same failure mode one layer in.** An unmounted disk
is not the only way to capture an empty session: the `~/.bashrc` hook can fail to load
(see the deployment section above), and then terminal windows are open while zero tabs
are reported. Those two facts come from independent sources — `window_count` from
gnome-terminal's own D-Bus tree, the tabs from the hook's state files — so windows
without tabs is *provably* a broken capture rather than a closed-down desktop, and
`save` refuses to store it. This actually happened: a save wrote `0 tabs` over a real
session's `latest.json`, and the restore that followed brought back nothing. The guard
is gated on the `~/.bashrc` line actually being installed, so a machine that never
installed the hook is not blocked from saving forever.

**Recommended one-time fix (optional, needs `sudo` once).** Adding the partition to
`/etc/fstab` makes it mount at boot and reduces Stage 0 to a no-op check:

```
UUID=d8d577a6-bf6a-42cc-a513-c02318d27956  /media/shengcn/d8d577a6-bf6a-42cc-a513-c02318d27956  ext4  defaults,nofail,x-systemd.device-timeout=5s  0  2
```

`nofail` + `device-timeout` matter: without them a missing or renamed disk can stall
boot. Keeping the identical mountpoint keeps both symlinks and every recorded path
valid. This is the real fix; Stage 0 then remains as the safety check for the case
where it did not happen.

### `bash-hook.sh` — per-tab state (the only `.bashrc` change)

Appended to `~/.bashrc` as a single guarded `source` line.

- No-ops immediately unless `$GNOME_TERMINAL_SCREEN` is set.
- Adds a `PROMPT_COMMAND` entry that compares `"$PWD:$CONDA_DEFAULT_ENV"` against a
  cached value and **only writes on change** — so steady-state prompt cost is a string
  compare, no I/O.
- Writes `~/.local/state/sessionctl/shells/$$.json`:
  `{pid, screen, cwd, conda, ts}`.
- `trap ... EXIT` removes the file when the tab closes.
- **Restore side of the same hook**: if `$SESSIONCTL_CONDA` is set, run
  `conda activate "$SESSIONCTL_CONDA"` then `unset` it. This is how a replayed tab
  re-enters its env (see `restore.py`, below) — keeping it in the hook avoids fragile quoting in the
  `gnome-terminal` command line.

### `collect.py`

**Terminals.** Read `shells/*.json`; drop entries whose PID is dead or whose parent
is no longer `gnome-terminal-server`. Cross-check the surviving `screen` paths against
the live D-Bus tree (`busctl --user tree org.gnome.Terminal`, which lists
`/org/gnome/Terminal/screen/<uuid>` per tab and `/org/gnome/Terminal/window/<n>`) to
drop stale tabs and get a stable ordering. Group the surviving tabs into windows using
the accessibility tree (`collect.atspi_window_tabs`), which is the only interface on
this machine that actually knows which tab belongs to which window — see limitation 2.
That probe runs in a **subprocess**: `Atspi.init()` aborts the process outright when the
accessibility bus is unreachable, the same way `Gtk.init()` does on a dead display, so
it cannot be contained with `try`/`except`.

**VSCode.** Read `~/.config/Code/User/globalStorage/storage.json` →
`windowsState.openedWindows[].folder`. This is VSCode's own authoritative record of
open windows and gives **full folder URIs** directly — verified to exactly match the
two live windows. No X11 title parsing, no `state.vscdb` needed.

**Chrome.** Record which profiles have a running Chrome. Best-effort extract the tab
URL list from the newest `~/.config/google-chrome/<Profile>/Sessions/Session_*` with a
tolerant scan (the SNSS container is binary, but URLs are recoverable as embedded
strings — verified working). This is for the human-readable record and the
`--chrome-urls` fallback; it is *not* the primary restore path.

**Firefox.** If a firefox process is running, record the snap profile. Rely on its own
session restore.

Output: one JSON snapshot + refresh `latest.json`; prune to the newest 20 — but only
when the state actually changed. See **Write avoidance** under "Scheduling", above.

### Scheduling — systemd user units

- `sessionctl-save.timer`: `OnBootSec=30s`, `OnStartupSec=30s`, `OnUnitActiveSec=30s`,
  `AccuracySec=1s`,
  driving a `Type=oneshot` `sessionctl save`. `OnStartupSec` was added after a session
  in which this timer fired **zero times in 75 minutes**. An earlier note in this plan
  assumed `OnBootSec`/`OnStartupSec` both count from manager start for user units;
  `systemd.timer(5)` says otherwise — `OnBootSec=` is relative to machine boot, and the
  remap to `OnStartupSec=` applies only to the system manager inside a container. So on
  a login more than two minutes after boot (18:17:16 boot, 18:19:44 manager start, in
  the failing case) `OnBootSec=2min` is already past and `OnUnitActiveSec=` has no
  previous activation to anchor to. `OnStartupSec=` is relative to the manager's own
  start and so always lies in the future when the timer loads. `Persistent=yes` was
  dropped as inert: it "only has an effect on timers configured with `OnCalendar=`".
  `AccuracySec=1s` is required rather than cosmetic at a sub-minute tick: it defaults to
  **1min**, and systemd places each elapse at a randomized point inside that window to
  coalesce wake-ups, so a 30 s interval left at the default would be smeared across 60 s.
  Measured after the change: firings 30.8-31.0 s apart, each run ~105 ms, writing nothing
  when the digest is unchanged.
- **`sessionctl-logout.service` was removed.** It ran `save` from `ExecStop` to take a
  final exact snapshot at logout, and earlier revisions of this plan described that as
  making a clean logout "exact". It is the opposite: `ExecStop` runs after every shell
  has exited — their `EXIT` traps having already deleted the per-tab state files — and
  after X is gone, so it captures an empty session and promotes it to `latest.json`.
  Observed with three seconds between two journal lines: a periodic save recording
  `6 tabs, 5 windows placed`, then the logout save writing `0 tabs, 0 windows placed`,
  after which a restore had nothing to replay. It was harmless only while it was
  crashing on the dead display; fixing that crash (limitation 3) is what made it
  destructive. At a 30 s tick it was never worth more than 30 seconds of freshness.
  `setup` disables and deletes it from older installs (`setup.OBSOLETE_UNITS`) —
  dropping it from the shipped set alone would leave an enabled unit running forever.

**Write avoidance.** The timer runs every 30 seconds for as long as the machine is up,
so the steady state must cost close to nothing. Three layers, cheapest first:

1. **Gate expensive collection on mtime.** The dominant cost of `save` is *not* the
   write — it is scanning Chrome's session files for URLs, which are currently 1.7 MB
   (`Tabs_*`) and 522 KB (`Session_*`). Record each source file's `(path, mtime, size)`
   in the snapshot; on the next run, skip the scan entirely and reuse the previous
   result when those are unchanged. Same gate for VSCode's `storage.json`. Terminal
   state costs nothing already — the shells wrote it themselves — and a Wnck
   enumeration of ~12 windows is negligible.

2. **Hash only the restore-relevant state.** Build a canonical digest over just the
   fields a restore actually consumes: terminal window grouping and per-tab
   cwd + conda, the VSCode folder list, the Chrome URL list, and per window
   `wm_class` / workspace / geometry / maximized / sticky.

   Deliberately **excluded from the digest**: timestamps, PIDs, and **window titles**.
   A title changes every time you switch browser tab or VSCode file, so letting it feed
   the change signal would defeat the whole mechanism. Titles are still *stored* —
   they are matching hints during placement — but a slightly stale title is harmless
   because matching is class-first, title-second.

3. **Unchanged digest → write nothing at all.** No snapshot rewrite, no ring rotation,
   no touch of `latest.json`. Update only a small `heartbeat` file holding
   `{digest, last_verified_ts}`, so `sessionctl list` can still report "last confirmed
   live at …". An idle machine then does one stat-and-hash pass every 30 seconds and
   performs zero writes.

**Consequence for the ring buffer.** Because snapshots are appended only on real
change, the 20-snapshot history spans 20 *distinct* desktop states rather than the last
40 minutes of idling. That makes rotation genuinely useful — you can roll back to how
things looked before you rearranged everything, instead of finding twenty identical
copies of the last idle state.

**Not doing delta/partial writes.** A snapshot is tens of KB; rewriting it in full on a
real change is cheaper and far simpler than maintaining a journal and replaying deltas.
The saving comes from *not writing at all* in the common case, not from writing less
per write.

### `restore.py` — `sessionctl restore`

**Terminals.** One `gnome-terminal` invocation per recorded window, tabs in order:

```
gnome-terminal \
  --window --working-directory=/path/a -- bash -c 'SESSIONCTL_CONDA=torch209 exec bash -i' \
  --tab    --working-directory=/path/b -- bash -c 'exec bash -i'
```

The `VAR=x exec bash -i` prefix places `SESSIONCTL_CONDA` in the exec'd shell's
environment; that shell reads `.bashrc`, the hook sees the variable, activates the env
and clears it. Reliable, and avoids depending on `gnome-terminal` forwarding
environment to its D-Bus server.

**VSCode.** `code <folder>` for the first, `code -n <folder>` for each subsequent one
(without `-n` a second invocation folds into the existing window). VSCode then restores
its own editor tabs per workspace.

**Chrome.** Launch `google-chrome`; with the pref set it restores its own tabs.
`--chrome-urls` instead opens the recorded URL list explicitly, for when the pref path
fails or the session file was lost.

Flags: `--dry-run` (print the exact commands, run nothing), `--snapshot <id>`,
`--only terminals,vscode,chrome`.

### `sessionctl setup`

Idempotent installer step: append the `.bashrc` source line, install + `--now` enable
the systemd units, and set Chrome's `restore_on_startup`.

**Important ordering detail:** Chrome rewrites `Preferences` from memory when it exits,
so editing that file while Chrome is running silently loses the change. `setup` must
detect a running Chrome and, rather than editing under it, tell you to set
*Settings → On startup → Continue where you left off* in the UI (or to close Chrome and
re-run). Recommend doing it in the UI once — it's a single click and can't race.

### CLI surface

`sessionctl save | restore | place | mount | adopt | list | show <id> | setup | uninstall`

`mount` runs Stage 0 alone. `restore --skip-mounts` bypasses it for the case where
you have already mounted things yourself.

`adopt` declares the currently-visible desktop to be the session of record after a
reboot, releasing the boot guard described under Stage 0 without restoring anything.

`place` re-applies only the workspace/geometry pass from a snapshot, without
relaunching anything — useful for testing and for fixing a layout you disturbed.

`uninstall` reverses everything: removes the `.bashrc` line, disables the units,
optionally deletes state.

### Workspace layout — capture and replay

Handled by `layout.py`, driven entirely through Wnck (no external binaries).

**Capture** (part of every periodic snapshot). For each window record:
`wm_class`, `title`, `pid`, `workspace` index, `geometry` (x/y/w/h), `maximized`,
and a `sticky` flag. A workspace index of `0xFFFFFFFF` means *visible on all
workspaces* (your GNOME Shell overlay window reports this) and is stored as
`sticky: true` rather than as a number.

**The dynamic-workspaces constraint.** Because `dynamic-workspaces=true`, GNOME keeps
exactly one trailing empty workspace and destroys empty ones in between. At restore,
workspace 3 simply *does not exist* until workspaces 0–2 are occupied, so a naive
"move this window to WS3" silently fails or lands it on the wrong desktop. Two
mitigations, applied together:

1. **Restore in ascending workspace order.** Placing windows on WS0, then WS1, then
   WS2… causes GNOME to append the next empty workspace each time, so the target always
   exists by the time it is needed. Your current layout (0,1,2,3) is gap-free, so this
   alone is sufficient today.
2. **Gap compaction.** If a snapshot has gaps (e.g. windows on 0 and 3, nothing on
   1–2), the higher indices are compacted down to consecutive ones, preserving relative
   order and grouping. Without this the gap is unreachable.

   `--static-workspaces` is an opt-in escape hatch that instead sets
   `dynamic-workspaces=false` / `num-workspaces=<n>`, making indices exact and gaps
   legal. It changes GNOME's behaviour system-wide, so it is off by default.

**Matching windows back to records.** After a reboot every window has a new ID, so
records are re-bound by `layout.match_windows()`: class-first, title-second, every
recorded and every live window consumed at most once. Candidates are pooled by exact
`wm_class`, then narrowed by title similarity when more than one candidate shares a
class; whatever is left over is reported as unmatched and never force-placed onto a
guess.

**There is one tier above title similarity: whether the window existed before this
restore launched anything.** An earlier version of this plan said the opposite — that
matching "applies uniformly", with no separate tier for windows the tool itself just
launched — and a real reboot-and-restore proved that wrong. Running `sessionctl
restore` from a terminal means a same-class window is already open when placement
runs, and every gnome-terminal title is byte-identical once a shell has drawn a
prompt, so the similarity tie resolved to whichever came first in the live pool: the
older, pre-existing window. The recorded workspace and maximized state landed on the
terminal already in use while the freshly restored one was left at gnome-terminal's
default size — and it reported success, because the leftover live window is computed
but discarded at the call site. `restore` now takes a baseline of live window XIDs
before launching (`layout.live_xids()`) and passes it to `place()`, which flags
anything absent from it as `new` and ranks that above title similarity; a window
created specifically to satisfy these records is stronger evidence than a title,
which for terminals matches everything equally. Title similarity still orders windows
*within* the newly launched set, so two restored VSCode folders still land correctly,
and `sessionctl place` — which launches nothing and has no baseline — matches on
class and title exactly as before.

An earlier version of this plan proposed giving terminal windows an explicit `--title`
at launch for exact tier-1 matching. Tested live and dropped: this system's default
`PS1` embeds a `\e]0;...\a` title-setting escape sequence that fires on the very first
shell prompt, silently overwriting any launch-time title before a placement pass could
ever observe it — confirmed by launching a real interactive terminal with `--title` set
and watching it revert within moments. So terminal windows fall back to class-plus-title
matching same as everything else, which for terminals in practice means position within
the class pool, since every gnome-terminal window's title looks the same once a shell
has actually run. This is the same limitation already noted below for multi-window tab
grouping, not a new one.

**Placement.** For each bound window: `move_to_workspace()`, then geometry via
`set_geometry()`, then re-maximize if it was maximized (order matters — setting
geometry on a maximized window is a no-op, and geometry set immediately after
`unmaximize()` lands short unless the main loop gets a moment to catch up first — see
below). Windows flagged `sticky` get `pin()`.

**The GLib main loop, not documented until this was actually built.** Wnck's action
methods (`move_to_workspace`, `maximize`, `unmaximize`, `set_geometry`, `pin`) send
asynchronous X11 requests. Tested live: calling one and immediately re-querying the
window's state shows no change at all, every time, until `Gtk.main_iteration()` is
actually given a chance to run. Without that, every placement action would silently
no-op — this would have shipped as a completely non-functional feature had it not been
caught by testing the write side directly rather than trusting the read side (which
needs no pumping, since it reflects state already cached by `force_update()`).

**Timing.** Apps do not map their windows instantly. The placement pass polls Wnck for
up to a configurable timeout (default 45 s), placing each window as it appears rather
than sleeping a fixed amount, and reports anything that never showed up. It also gives
up early once nothing has been placed for 15 s, since a snapshot almost always names
windows that are not running and waiting out the full timeout for those makes restore
feel broken.

A window whose **target workspace does not exist yet** is deferred and retried, not
skipped. This is the normal case immediately after a reboot: everything starts on
workspace 0, so only workspace 0 and one trailing empty workspace exist, and
dynamic-workspaces means workspace N appears only once 0..N-1 are occupied. Terminals
map in under a second while VSCode takes several, so the first poll asks for workspaces
that cannot exist until VSCode has claimed workspace 1. `_place_one` originally did
nothing at all in that case while `place()` counted the record as placed, so every
terminal stayed on workspace 0 and the run reported success — the ascending-order
strategy above was correct, but it only works if each step is allowed to wait for the
one below it.
`sessionctl place` re-runs *only* this pass against the latest snapshot, so a layout
can be re-applied without relaunching anything.

---

## Known limitations (stated up front)

1. **Running commands are not resumed.** A reboot kills every process; only cwd and
   conda env are restorable. Nothing can change this short of `tmux`/`mosh`-style
   persistent sessions, which a reboot also ends.
2. **Tab→window grouping is exact when AT-SPI is reachable, best-effort otherwise.**
   This limitation was originally stated as absolute: GNOME Terminal 42 exposes windows
   and screens as *flat, unrelated* D-Bus objects, VTE does not set `WINDOWID` here, the
   window objects carry only `org.gtk.Actions` (whose `active-tab` is an index, not a
   count) and no `org.gtk.Menus` to enumerate tabs — all verified, and all true. The
   conclusion drawn from it was not. The **accessibility tree** has the mapping: one
   `frame` per window, one `page tab` per tab, each named with that tab's title. Checked
   against a live desktop with five windows: 1/5/5/4/5 page tabs, totalling exactly the
   20 tabs the shell hook had recorded, in ~0.05 s and with `toolkit-accessibility` set
   to false. Tabs are assigned to the window whose page-tab title matches their
   `~`-abbreviated cwd, so tabs in distinctive directories land in the right window and
   indistinguishable ones fill the remaining slots — with the conda env read from each
   tab's on-screen prompt as the stronger key, since the title carries only the
   directory and every tab in `~` looks alike. AT-SPI exposes the terminal widget's
   text, whose last prompt line yields env and cwd together; matching runs in two
   passes (exact cwd+env, then cwd alone, then fill) so the strongest evidence wins
   globally rather than per-slot. Without AT-SPI it falls back to the
   old even split, which is right only for equal-sized windows — three windows of 5, 6
   and 7 tabs came back as 6/6/6 before this.
3. **Snapshot staleness up to ~30 s** on an unclean shutdown. A clean logout was
   originally described here as exact; that was not true in practice until fixed. The
   logout unit's `ExecStop` runs when X is already gone, and `Gtk.init()` aborts the
   process rather than raising, so the final save died outright (`cannot open display`,
   `status=1/FAILURE`) and captured nothing — the layout-capture fallback that exists
   for exactly this case never got to run. Fixed via `Gtk.init_check()` — after which
   the logout snapshot succeeded, and promptly destroyed a session by writing its empty
   capture over a good `latest.json`. The unit is now gone entirely (see Scheduling),
   and `save` refuses to store a snapshot whose layout capture came back empty when the
   previous one had windows. So staleness on a clean logout is now also bounded by the
   ~30 s tick, not by a final snapshot.
4. **VSCode windows are matched by the folder in their title.** All of them share a
   single Electron PID, so PID cannot separate them, and the whole title leads with the
   open file — which changes constantly, so similarity pairs windows by whichever
   filenames happen to look alike. Measured: a recorded window scored 0.696 against a
   *different* project's window and 0.639 against its own. With multiple monitors this
   is not cosmetic: under `workspaces-only-on-primary` a non-primary window has no
   workspace at all, so geometry alone decides its display, and geometry is applied to
   whatever window the record matched. The folder segment is stable under the churn;
   `_vscode_folder` parses it, and a window still titled "Visual Studio Code" while it
   loads yields None, which strict matching treats as "wait" rather than "guess".
5. **Terminal windows are matched by what they contain, not by their titles.**
   Recorded terminal windows used to be interchangeable to `match_windows` -- same
   `wm_class`, same PID (all of them are `gnome-terminal-server`), same maximized
   geometry, same title whenever their active tabs share a directory -- so their
   recorded workspaces were applied to whichever window came first and windows came
   back on each other's desktops. Each terminal record now carries a signature (tab
   count plus the set of directories those tabs are in), obtained by pairing AT-SPI's
   frame list with Wnck's windows sorted by XID; that pairing is verified against a
   controlled marker rather than assumed, and is dropped wholesale if the two views
   disagree on the window count. The signature outranks title similarity but not the
   `new` flag, so it separates terminals from each other without reintroducing the
   bug where a record binds to the terminal `restore` was launched from.
6. **Workspace placement for apps the tool doesn't launch is best-effort.** Nautilus,
   WeChat and Awesun are matched by class and title. Multiple same-class windows with
   changing titles (your 4 Nautilus windows) may be placed in the wrong order — they
   land on the right *workspace* group, but which window is which can shuffle. Windows
   the tool *does* launch no longer suffer this: they are identified by not having
   existed before the restore began, which is exact rather than title-based (see
   "Matching windows back to records"). The limitation is now specifically about
   same-class windows that were already open and that restore did not create.
7. **Gaps in workspace numbering are compacted, not preserved**, unless you opt into
   `--static-workspaces`. A window on WS3 with WS1–2 empty comes back on WS1.
8. **X11-only.** Wnck cannot enumerate or move windows under Wayland. If you ever
   switch the login session to Wayland, workspace layout restore stops working
   entirely and terminal window counting degrades to "one window".
9. **Firefox support is untested** — it's an unused snap on this machine.
10. **Recorded window titles can lag.** Titles are excluded from the change digest, so
   a snapshot may carry the title a window had at the last *substantive* change. This
   is deliberate and safe — titles are only a secondary matching hint — but it means
   `sessionctl show` may display a stale title for a window whose content moved on.
11. **`udisksctl` may prompt for a password.** `/dev/nvme0n1p6` is an internal,
   non-removable drive, so PolicyKit can require admin auth to mount it. Stage 0 will
   then be interactive. The `/etc/fstab` entry above removes this entirely.

---

## Verification

1. Stage 0, the honest test: `udisksctl unmount -b /dev/disk/by-uuid/d8d577a6-…`,
   confirm `ls ~/Projects/` now fails, then `sessionctl mount` and confirm both
   `~/Projects` and `~/extra_disk_2t` resolve again. Then confirm
   `sessionctl restore --dry-run` *refuses to run* while unmounted.
2. `sessionctl save && sessionctl show latest` — confirm the JSON lists 6 terminal
   tabs with correct cwds, 2 VSCode folders, and Chrome URLs.
3. Conda capture: open a new tab, `conda activate torch212`, `cd ~/Projects`, then
   `sessionctl save && sessionctl show latest` — that tab must show
   `cwd=/home/shengcn/Projects`, `conda=torch212`.
4. Hook hygiene: confirm a VSCode integrated terminal writes **no** file into
   `shells/` (it has no `$GNOME_TERMINAL_SCREEN`).
5. `sessionctl restore --dry-run` — review the exact `gnome-terminal` / `code` /
   `google-chrome` command lines before anything executes.
6. Live partial test, no reboot needed: `sessionctl restore --only terminals` — a new
   window appears with the right tab count, cwds, and `(torch212)` in the prompt.
7. Timer: `systemctl --user list-timers sessionctl-save.timer`, then confirm a new file
   lands in `snapshots/` within ~30 seconds.
8. Write avoidance, the load-bearing test: note the snapshot count, leave the desktop
   untouched for 10 minutes (5 timer firings), and confirm **no new snapshot files
   appeared** and `latest.json`'s mtime is unchanged — only `heartbeat` moved. Then
   switch a browser tab and a VSCode file and confirm still no new snapshot (titles are
   excluded). Then `cd` in a terminal and confirm exactly one new snapshot appears.
9. Layout capture: `sessionctl show latest` must record WS0 = Chrome + Nautilus +
   WeChat, WS1 = 2× VSCode, WS2 = 2× Awesun, WS3 = terminal, and the GNOME overlay
   window as `sticky: true` rather than workspace 4294967295.
10. Layout replay without a reboot: drag a VSCode window to another workspace, then run
   `sessionctl place` — it must return to WS1, still maximized.
11. Dynamic-workspace edge case: close everything on WS1 and WS2 so only WS0 and a
   high-index workspace are occupied, snapshot, then `sessionctl place --dry-run` and
   confirm the compaction remap is reported rather than an unreachable index.
12. Full test: reboot, then `sessionctl restore`.

## Implementation order

This is the single authoritative build sequence -- the only numbered list in this
document. (The Architecture section above is organized by topic, not by build order,
which is why its subsections are no longer numbered: an earlier revision numbered both
lists and they disagreed with each other -- e.g. Architecture's old "§3 Scheduling"
landed well before Architecture's old "§4 restore.py", while this list always built
`restore.py` first. Fixed by keeping exactly one numbered sequence.)

1. [x] `mounts.py` + `sessionctl mount` — smallest piece, highest payoff, and it is
   the thing actually blocking you after a reboot. Usable on its own before anything
   else exists. **Done** — `sys_workspace/sessionctl_lib/mounts.py`.
2. [x] `bash-hook.sh` + `sessionctl save` for terminals only. **Done** — hook
   installed in `~/.bashrc`, `sys_workspace/sessionctl_lib/{collect,snapshot}.py`.
3. [x] VSCode + Chrome collectors. **Done** — same two files, extended.
4. [x] `restore.py` with `--dry-run` first, then live. **Done, verified live end to end** (real gnome-terminal window with correct cwd/conda, real VSCode windows, Chrome relaunch) — sys_workspace/sessionctl_lib/restore.py.
5. [x] `layout.py`: capture first (pure read, verifiable via step 9), then the
   placement pass behind `sessionctl place --dry-run`, then live. **Done, verified live
   end to end** (real gnome-terminal windows disturbed then restored with pixel-perfect
   geometry; `sessionctl place` itself run at the CLI level against a real snapshot;
   `sessionctl restore` now also calls the placement pass after launching, verified via
   `--dry-run` output) — sys_workspace/sessionctl_lib/layout.py. Three real bugs found
   live that no unit test could have reached: Wnck's action methods need the GLib main
   loop pumped or they silently no-op; `set_geometry` right after `unmaximize` needs a
   pump in between or it lands short; and — found only after a real reboot-and-restore,
   reported as "the restored terminal came back small" — a single terminal record bound
   to the *pre-existing* terminal window (the one `sessionctl restore` was launched
   from) rather than the one restore had just created, because both titles are
   byte-identical once a shell has drawn a prompt and the similarity tie resolves to
   whichever comes first in the live pool. The obvious suspect there, a missing pump
   between `set_geometry` and `maximize`, was tested directly and found innocent —
   `maximize()` lands either way. Fixed by having `restore` snapshot live window XIDs
   before launching and rank not-previously-present windows above title similarity;
   verified by a live A/B on two identically-titled throwaway terminals that reproduced
   the bug with the baseline withheld and fixed it with the baseline supplied.
   `--static-workspaces` is implemented as a CLI/API flag but sessionctl never flips the
   underlying gsetting itself.
6. [x] systemd units + `setup`/`uninstall`. **Done, `setup` run for real and verified live across three rounds of testing, two of them real reboots** — sys_workspace/sessionctl_lib/setup.py, sys_workspace/systemd/. Each round found a genuine bug in the same family, one layer apart. First: the unit files were originally symlinked (matching the repo's usual deploy convention), but `systemd --user` loads them at login, ~76s before the data partition holding the symlink targets actually mounts (that particular 76s mount was a manual action, not automatic — the disk genuinely never auto-mounts, see "Why Stage 0 exists") — the units silently failed to load and the timer never fired for that whole session. Fixed by copying the unit files into ~/.config/systemd/user/ instead. Second, on the *next* reboot: with the unit files now loading correctly, ExecStart=%h/.local/bin/sessionctl was still a symlink into the same partition, so the timer fired on schedule three times before the disk was mounted and each one failed outright at the EXEC step (status=203/EXEC) before save's own Stage 0 degradation logic ever got a chance to run — a hard failure, not the graceful skip it looks like from the outside. Fixed at the time with a generated shell wrapper that checked reachability before exec'ing into the real entry point. Third, found not by a reboot but by running `sessionctl mount` directly against an unmounted disk: the wrapper fix was legible but structurally couldn't work, because `~/.local/bin/sessionctl` still pointed into the repo, and the repo lives on the very partition `mount` exists to mount — a bootstrap paradox that predated even the wrapper. Fixed by deploying the whole `sessionctl_lib/` package plus the entry script to `~/.local/share/sessionctl/lib/` (`_ensure_package_copy`) and symlinking `~/.local/bin/sessionctl` to that deployed copy instead of the repo, plus switching the entry script's `sessionctl_lib/` lookup from `os.path.abspath` to `os.path.realpath` so it resolves correctly through the symlink. All three fixes re-verified live: the timer fires unattended on its own schedule, and `sessionctl mount` now succeeds via the deployed path with the disk unmounted and zero repo-path references anywhere in the deployed copy. Fourth, and worst, on the next reboot: `restore` brought back no terminals at all, because three faults compounded — the shutdown snapshot died on a dead X display (`Gtk.init()` aborts the process instead of raising, so the layout-capture fallback never ran), the periodic timer fired zero times that whole session (75 minutes — traced afterwards to `OnBootSec=` being relative to machine boot even in a per-user manager, so a login 2.5 minutes after boot left the timer with no anchor at all; fixed by adding `OnStartupSec=2min`), and `bash-hook.sh` was still a symlink into the repo, so a terminal opened 7 seconds before the partition mounted never loaded the hook and captured nothing for its whole life. A later save then wrote that empty terminal set over `latest.json`. Fixed by `Gtk.init_check()` (catchable `NoDisplayError`), deploying `bash-hook.sh` as a real copy, and a new `save` guard that refuses to store when terminal windows are open but no tabs were captured. Fifth, caused by the first of those fixes: with the logout snapshot no longer crashing it began succeeding, and since `ExecStop` runs after every shell has exited and X has gone, succeeding meant writing an empty session over `latest.json` three seconds after a periodic save had captured the real one — the restore that followed brought back nothing. `sessionctl-logout.service` is removed (and actively disabled+deleted from older installs via `setup.OBSOLETE_UNITS`, since systemd keeps running what is already enabled), and `save` now also refuses to store when layout capture came back empty while the previous snapshot had windows — it could not see the desktop, so it must not overwrite one that could. See README.md's Tests section for the full account.
7. [ ] README documenting the limitations above. **Mostly done** —
   `README.md` covers steps 1-6.
