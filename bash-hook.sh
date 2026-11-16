# sessionctl -- per-tab state capture, sourced from ~/.bashrc.
#
# `conda activate` mutates the running shell and never re-execs, so the active
# env never reaches /proc/<pid>/environ. The only place that can observe it is
# the shell itself, which is why this hook exists.
#
# Source this at the END of ~/.bashrc, after conda's init block -- the restore
# path at the bottom needs `conda activate` to be defined.

# Which terminal is this shell in? Only real gnome-terminal tabs have
# GNOME_TERMINAL_SCREEN, and only Warp panes have WARP_TERMINAL_SESSION_UUID.
# VSCode's integrated terminals have neither, which is exactly the filter we
# want: their cwd is the workspace folder and restoring them is VSCode's job,
# not ours.
#
# The Warp UUID is worth recording rather than just testing: it is the hex of
# terminal_panes.uuid in Warp's own session database (verified exactly -- all
# nine live shells matched all nine rows), so it joins this shell's conda env
# straight onto the right pane. Warp records the pane's cwd itself and keeps it
# current, but it has no way to see the conda env, because `conda activate`
# mutates the running shell and never re-execs. That is the same reason this
# hook exists for gnome-terminal.
if [[ -n "$GNOME_TERMINAL_SCREEN" ]]; then
    _sessionctl_kind=gnome
elif [[ -n "$WARP_TERMINAL_SESSION_UUID" ]]; then
    _sessionctl_kind=warp
else
    return 0
fi

# Guard against a double source (e.g. .bashrc re-sourced by hand).
[[ -n "$_SESSIONCTL_HOOKED" ]] && return 0
_SESSIONCTL_HOOKED=1

# Consume restore metadata before running conda or any child process.
_sessionctl_resume=${SESSIONCTL_RESUME:-}
_sessionctl_resume_delay=${SESSIONCTL_RESUME_DELAY:-0}
unset SESSIONCTL_RESUME SESSIONCTL_RESUME_DELAY
[[ $_sessionctl_resume_delay =~ ^[0-9]{1,4}$ ]] || _sessionctl_resume_delay=0

_sessionctl_dir="${XDG_STATE_HOME:-$HOME/.local/state}/sessionctl/shells"
_sessionctl_file="$_sessionctl_dir/$$.json"
_sessionctl_last=

# Runs before every prompt, so it must not fork. Everything below is a bash
# builtin: the parameter expansions, printf, and the redirect. In the steady
# state this is one string compare and nothing else.
_sessionctl_save() {
    local key="$PWD:$CONDA_DEFAULT_ENV:$_sessionctl_resume"
    [[ $key == "$_sessionctl_last" ]] && return 0

    local cwd=$PWD conda=$CONDA_DEFAULT_ENV screen=$GNOME_TERMINAL_SCREEN
    local uuid=$WARP_TERMINAL_SESSION_UUID
    local pending=$_sessionctl_resume
    pending=${pending//\\/\\\\}; pending=${pending//\"/\\\"}
    cwd=${cwd//\\/\\\\};       cwd=${cwd//\"/\\\"}
    conda=${conda//\\/\\\\};   conda=${conda//\"/\\\"}
    screen=${screen//\\/\\\\}; screen=${screen//\"/\\\"}
    uuid=${uuid//\\/\\\\};     uuid=${uuid//\"/\\\"}

    [[ -d $_sessionctl_dir ]] || mkdir -p "$_sessionctl_dir" 2>/dev/null || return 0

    printf '{"pid":%d,"kind":"%s","screen":"%s","warp_uuid":"%s","cwd":"%s","conda":"%s","ts":%d,"resume_pending":"%s"}\n' \
        "$$" "$_sessionctl_kind" "$screen" "$uuid" "$cwd" "$conda" "${EPOCHSECONDS:-0}" "$pending" \
        > "$_sessionctl_file" 2>/dev/null

    _sessionctl_last=$key
}

_sessionctl_cleanup() {
    rm -f "$_sessionctl_file" 2>/dev/null
}
trap _sessionctl_cleanup EXIT

# Warp constrains how this line may be written, so it is not free to change.
# Warp's bash bootstrap (it sources ~/.bashrc, then installs bash-preexec over
# the top) re-homes the user's PROMPT_COMMAND into its own precmd chain, but
# only after this test, lifted verbatim from the shipped binary:
#
#     if [[ ! $PROMPT_COMMAND =~ "PROMPT_COMMAND=" ]]; then
#
# It inspects the *value*, not the assignment, so appending as below is safe --
# the value becomes "_sessionctl_save; ..." and is honoured. Writing something
# like PROMPT_COMMAND='PROMPT_COMMAND=...' would silently drop the hook in Warp
# while still working in gnome-terminal. Verified by replaying Warp's own
# bootstrap logic around this file: it reports the hook as captured, and a Warp
# pane writes its state file.
_sessionctl_reconnect() {
    [[ -n $_sessionctl_resume ]] || return 0
    local command=$_sessionctl_resume
    # Consume first, so Ctrl-C during the wait/connection cannot trigger a retry.
    _sessionctl_resume=
    local -a argv
    # Restore emits only canonical space-separated tokens. No eval or shell expansion.
    read -r -a argv <<< "$command"
    [[ $_sessionctl_resume_delay == 0 ]] || sleep "$_sessionctl_resume_delay"
    history -s "$command"
    "${argv[@]}"
    _sessionctl_save
    return 0
}
PROMPT_COMMAND="_sessionctl_save; _sessionctl_reconnect${PROMPT_COMMAND:+; $PROMPT_COMMAND}"

# Restore path: `sessionctl restore` launches tabs as
#   bash -c 'SESSIONCTL_CONDA=<env> exec bash -i'
# so the env name arrives here rather than in the gnome-terminal command line,
# where quoting it would be fragile.
if [[ -n "$SESSIONCTL_CONDA" ]]; then
    conda activate "$SESSIONCTL_CONDA" 2>/dev/null
    unset SESSIONCTL_CONDA
fi
