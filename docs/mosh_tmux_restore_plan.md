# Plan: Restore mosh + tmux Terminal Tabs

**Date:** 2026-11-13
**Status:** proposed, nothing implemented. All facts below were measured on 2026-11-13, with the repository at
`ff2922f`.
**Scope:** `sessionctl_lib/collect.py`, `snapshot.py`, `restore.py`, `layout.py`, `cli.py` (`show`), `bash-hook.sh`,
their tests, the README and [`session_restore_plan.md`](session_restore_plan.md). Warp, VSCode and Chrome handling do
not change in phase 1. Nothing on the server changes unless you run the optional clean-up in step 8.

The server is called `shengcn-server` below, and its address is written as `<server>`. Neither the address nor
anything secret belongs in this repository.

## Goal

1. A gnome-terminal tab that was running `mosh <server> -- tmux new -A -s <name>` before a reboot comes back running
   the same command. It reattaches to the same tmux session, with its processes, scrollback and working directories
   as they were.
2. Orphaned mosh-servers on the server stop accumulating with every laptop reboot.
3. Terminal windows are placed by their tmux session names, which are unique. Titles and prompts are not.

## Where things stand

| | Laptop | Server |
| --- | --- | --- |
| System | Ubuntu 22.04.5, X11, `Linger=no` | up since 2026-11-02 14:19 (UTC+8) |
| tmux, mosh | 3.2a, 1.3.2 | 3.2a, 1.3.2; tmux status bar on |
| Terminals | gnome-terminal 3.44.0 (VTE 0.68), Warp 0.2026.11.11 | — |
| mosh processes | 11 `mosh-client`, one per mosh tab | 100 `mosh-server` |

- **Which tabs run mosh.** 11 of the 18 gnome-terminal tabs run `mosh <user>@<server> -- tmux new -A -s <name>`.
  Window 0 holds `mnt_0`, `mnt_4` and `mnt_1` plus 6 local tabs. Window 1 holds `daily_0` to `daily_7`. Window 2
  holds one local tab. No Warp pane runs mosh, ssh or tmux: Warp's own database records none of those commands.
- **What a restore does now.** `latest.json` from 16:37 records all 18 tabs as `~` with conda env `base`. A restore
  opens 18 local shells, and the 11 mosh commands have to be typed again. The hook writes a tab's state only at a
  prompt, and a tab running mosh never gets back to one. So the record is the local shell's state from before mosh
  started, with no sign that mosh is running.
- **AT-SPI reads through mosh.** The prompt parser in `collect.atspi_window_tabs` reads the *server's* prompt
  inside tmux and takes it for the tab's local directory and env. For example, it reports `torch209` in
  `~/Projects/finance/stock_mnt` for a tab recorded locally as `~` with `base`. This does no harm today only because
  every local record is the same.
- **Orphans on the server.** By start date, the 100 mosh-servers are: Nov 2 (16), Nov 3 (1), Nov 4 (1), Nov 7 (24),
  Nov 9 (26), Nov 11 (17) and Nov 13 (15). Some tmux sessions have 7 clients attached. In `daily_0` the newest client
  was created at 12:39 server time, which is 15:39 on the laptop and matches its live tab. The other six date from
  earlier boots. `MOSH_SERVER_NETWORK_TMOUT` is unset, so an orphan waits for its client forever and keeps one of the
  roughly 1,000 UDP ports from 60000 to 61000.

## Facts the design rests on

1. **The state is kept by tmux on the server, not by mosh.** A new mosh client cannot reattach to an old
   mosh-server. The documented way back is to run a multiplexer on the server
   ([mosh.org FAQ](https://mosh.org/), [mosh-server(1)](https://man.archlinux.org/man/mosh-server.1.en.txt)).
   `tmux new -A -s NAME` attaches if the session exists and creates it otherwise, so it is safe to replay any number
   of times. Plain ssh with tmux would come back the same way after a reboot. What mosh adds is surviving sleep and
   network changes without dropping the connection.
2. **The command can be read back from `/proc`.** The `mosh` wrapper ends with
   `exec {$client} ("$client", "-# @cmdline |", $ip, $port)` (line 456 of `/usr/bin/mosh`). So `mosh-client`'s
   `argv[1]` is the original command line, joined with spaces. Its parent is the tab's shell, and it leads the
   terminal's foreground process group: field 8 (`tpgid`) of `/proc/<shell>/stat` equals `mosh-client`'s pgid.
   Joining with spaces loses quoting, so an argument containing spaces, such as `--ssh="ssh -p 2222"`, cannot be
   split back reliably.
3. **Each mosh tab can be identified on screen.** tmux's default status bar starts with `[<session>] `, and AT-SPI
   exposes it. A probe that read only that pattern found exactly the 11 session names, in the right windows.
4. **Orphans can be cleared without losing the session.** This was tested locally, with a mosh-server bound to
   127.0.0.1 and a scratch tmux socket:
   - Killing `mosh-client` left the mosh-server running with its tmux client still attached. That is the state the
     server is in now.
   - `tmux detach-client -s x` made that mosh-server exit within one second. Session `x` and its processes survived.

   `tmux new -A -D -s NAME` does the same detach when a new client attaches.
5. **The command has to run at the first prompt, not during `~/.bashrc`.** In bash 5.1.16, `history -s` while
   `~/.bashrc` runs left the shell without its loaded history. The same call at the first prompt landed last, after
   the loaded history, which is what Up-arrow needs.
6. **mosh has to run inside the interactive shell.** Restore launches each tab as `bash -c '… exec bash -i'`. The
   interactive shell's parent is then gnome-terminal-server, so `_is_live_gnome_tab` accepts its record. If mosh ran
   under `bash -c` before the interactive shell started, the tab would have no hook record. It would then drop out of
   the next save, and out of the next restore.
7. **sshd limits simultaneous logins.** The server has no `MaxStartups` setting, so OpenSSH's default `10:30:100`
   applies: once 10 logins are still being authenticated, further ones start being refused at random. Restoring 11
   mosh tabs at once means 11 ssh logins at once.

## Design

### Capture (`collect.py`)

- **Detection.** For each live gnome-terminal tab record, read the shell's `tpgid`. If a foreground job is running,
  look for a child of the shell in that group whose `comm` is `mosh-client` and whose `argv[1]` starts with `-# `.
  Read only its `cmdline`, never its `environ`: the mosh session key is passed through the environment.
- **Parsing.** `parse_mosh(argv)` returns `{"kind": "mosh", "args": [...], "tmux_session": NAME}`. It accepts only
  this shape:
  - mosh options with no spaces in them, then `[user@]host`, then `--`;
  - then either `tmux new` / `new-session` with `-s NAME`, optionally with `-A`, `-D` or `-d`;
  - or `tmux attach` / `attach-session` / `a` with `-t NAME`.

  Anything else is recorded as `{"kind": "mosh", "raw": "...", "replay": false}`. `show` displays it, and restore
  never replays it.
- **The tab record** gains a `resume` field, which is absent for local tabs.
- **ssh** could use the same mechanism for `ssh -t <server> tmux …`. It is left out until it is needed: there are no
  ssh tabs today, and ssh port forwards (`-D`, `-L`, `-N`) must never be replayed.

### Snapshot (`snapshot.py`, `cli.py`)

- **Digest.** Each tab becomes `[cwd, conda, resume command or None]`, so starting or stopping mosh in a tab counts
  as a real change.
- **`show`** prints a mosh tab as `mosh <host> → tmux <name>`.
- **Old snapshots** have no `resume` field and restore exactly as they do now.

### Restore (`restore.py`, `bash-hook.sh`)

- **Tab command.** `_tab_script` becomes
  `SESSIONCTL_CONDA=<env> SESSIONCTL_RESUME=<shlex.join(args)> SESSIONCTL_RESUME_DELAY=<n> exec bash -i`.
- **The hook.** While `~/.bashrc` runs, the hook moves `SESSIONCTL_RESUME` into a shell variable and unsets the
  environment variable, so child processes do not inherit it. In `PROMPT_COMMAND`, *after* `_sessionctl_save` has
  written the state file (fact 6), a one-shot function:
  - waits the delay;
  - adds the command to history with `history -s` (fact 5);
  - runs it, then clears the variable.

  When mosh exits, whether from a detach, a network failure or an unreachable server, the tab is at its local prompt,
  and Up-arrow brings the command back.
- **Orphans.** When replaying, `-D` is added to `tmux new -A` (decision 1). This clears the previous boot's orphan
  for every session that is restored.
- **Stagger.** The delay is the tab's index in seconds (decision 3), which keeps the logins under sshd's limit
  (fact 7).
- **No network yet.** If the network isn't up yet, mosh fails and the tab is left at a local prompt. Up-arrow and
  Enter retry. There is deliberately no retry loop, so a dead server cannot keep a tab busy.

### Grouping and placement (`collect.py`, `layout.py`)

- **Status bar.** `prompt_of` also returns the session name read from the tmux status bar
  (`^\[([^\]]+)\] \d+:`) as `slot["tmux"]`. The probe reads only that one pattern, so it never collects what is on
  screen. When a tab shows a tmux name, its prompt-derived directory and env are ignored, because they are the
  server's.
- **Grouping.** `_group` gets a new first pass: a tab whose `resume.tmux_session` equals a slot's `tmux` claims that
  slot.
- **Placement.** `_terminal_signature` adds the sorted tmux names. Capture takes them from the `resume` records, and
  the live side reads them from the status bars. Agreement on tmux names ranks above agreement on tab count and
  directories.
- **Timing.** A restored window shows its status bars a few seconds after launch, which should fall inside
  `SETTLE_GRACE` (20 s). This must be checked, because of the stagger (V5).

## Steps

1. Add `parse_mosh` and foreground detection to `collect.py`. Unit tests use the real argv shapes from this machine,
   with a placeholder for the address.
2. Wire `resume` into `collect_terminals`, `digest` and `show`, with tests.
3. Add `SESSIONCTL_RESUME`, the delay and the `-D` rewrite to `restore._tab_script`. Test argv construction and
   quoting, including the two unquoting passes `--command=` already goes through.
4. Add the one-shot to `bash-hook.sh`. Test it the way the hook is already tested: real bash through `--rcfile` in a
   pty, with a stub `mosh` script that records its arguments and exits. Check the history order, the state file
   written before the command runs, and the variable not being inherited.
5. Read the tmux status bar in the AT-SPI probe, then add the `_group` pass and the signature field. Tests use
   recorded slot shapes.
6. Docs: the README's terminal section and limitations, limitation 1 in `session_restore_plan.md`, and an entry in
   `ai_history.md`.
7. Deploy with `sessionctl setup`. This also brings the installed copy up to date with `583e540`; it is currently
   behind the repository.
8. Optional, run by you on the server: the one-off orphan clean-up below.
9. Run verification V1–V5, then V6 with a real reboot.

## Verification

- **V1.** `sessionctl save --dry-run` shows `resume` on exactly 11 tabs: `mnt_0`, `mnt_1`, `mnt_4` and
  `daily_0`–`daily_7`. The 7 local tabs have none.
- **V2.** `sessionctl restore --dry-run --only terminals` prints 3 windows with 18 tabs. The 11 mosh commands are
  right, with `-D` added and delays from 0 upwards.
- **V3.** A real restore of one throwaway tab against a throwaway session (`sessionctl_probe` on the server, killed
  afterwards):
  - it attaches;
  - its state file exists, and `save --dry-run` shows the tab with `resume`;
  - after a detach (prefix, then `d`), the tab is at a local prompt and Up-arrow gives the mosh command.
- **V4.** After V3 has been repeated twice, `sessionctl_probe` has one client, and the server's mosh-server count is
  back where it started.
- **V5.** Shuffle windows 0 and 1, then run `sessionctl place` from the current snapshot: both return to their
  places, matched by tmux names.
- **V6.** After a real reboot, `sessionctl restore` brings back all 11 tabs, attached to the same sessions in the same
  windows. The server's mosh-server count does not grow by 11.
- **The full test suite passes**, including the credential check.

## Warp (phase 2, only if mosh is used in Warp)

No Warp pane runs mosh, ssh or tmux today. Warp's own restore brings back windows, tabs, panes and recent blocks, but
it does not re-run commands ([session restoration](https://docs.warp.dev/terminal/sessions/session-restoration)).
Warp also has no mosh support, and its tmux-based SSH mode has been removed
([SSH](https://docs.warp.dev/terminal/warpify/ssh/)).

The route would be the same one-shot in the hook. Warp's restore cannot pass environment variables, so the commands
would be looked up by pane UUID in a file that `sessionctl restore` writes. Two experiments come first:

- **W1.** Quit and reopen Warp, and check whether pane UUIDs survive: `terminal_panes.uuid`, and
  `WARP_TERMINAL_SESSION_UUID` in the shells. The existing data cannot show this, because Warp started at 15:31,
  before the first snapshot that recorded its panes.
- **W2.** Check whether running a full-screen program from the first prompt breaks Warp's shell integration. Warp
  moves `PROMPT_COMMAND` into its own precmd chain.

If W1 fails, the fallback is [Tab Configs](https://docs.warp.dev/terminal/windows/tab-configs/). They live in
`~/.local/share/warp-terminal/tab_configs/`, can run commands per pane, and open with
`warp://tab_config/<name>`, which is registered on this machine through `dev.warp.Warp.desktop`. But they open new
tabs next to the ones Warp restores, so sessionctl would have to take over Warp's restore entirely. That is not
planned.

## Not covered

- **Local tmux.** It ends at a reboot, and also at logout because `Linger=no`, so it would need tmux-resurrect to
  survive. Inside it the hook's liveness check rejects the shell, because its parent is tmux. sessionctl already
  restores local tabs.
- **Server reboots.** The tmux sessions end with the server, and replaying then creates empty sessions. tmux-resurrect
  on the server would be a separate plan.
- **ssh tabs.** See *Capture*.

## Points to be aware of

- **Restore runs commands automatically.** Only the shape `parse_mosh` accepts is ever replayed. The parser is the
  safety boundary, so it gets the most tests.
- **`-D` affects other devices.** It detaches a session from every other client, so a tmux session that is also open
  on another device gets detached there.
- **Snapshots hold the server address.** They live in `~/.local/state/sessionctl`, outside the repository. Tests and
  docs use placeholders.
- **The mosh session key is never read.** It is in `mosh-client`'s environment, which sessionctl does not read.

## Decisions

1. Add `-D` when replaying? **Recommended: yes.** It clears the orphans of exactly the sessions being restored, and
   needs no change on the server.
2. Set `MOSH_SERVER_NETWORK_TMOUT`? **Recommended: not now.** `-D` already handles restored sessions. A timeout would
   also end connections while the laptop sleeps for longer than the timeout. If set, use a long one, such as
   `mosh --server='env MOSH_SERVER_NETWORK_TMOUT=2592000 mosh-server' …`.
3. Stagger reconnects by 1 s per tab? **Recommended: yes.**
4. Cover ssh tabs too? **Recommended: not now.**
5. Clean up the existing orphans (step 8)? **Recommended: yes, once, run by you.**

**Step 8, run on the server.** For each session, this keeps the newest client and detaches the rest. The newest is
the live laptop tab, because live tabs were attached after the latest reboot. A detached orphan's mosh-server then
exits (fact 4). It also detaches older clients on any other device. Run it first with `echo` in front of `tmux
detach-client` to see what it would do:

```sh
for s in $(tmux list-sessions -F '#S'); do
  tmux list-clients -t "$s" -F '#{client_created} #{client_name}' | sort -n | head -n -1 |
    while read -r _ c; do tmux detach-client -t "$c"; done
done
```
