# sessionctl -- per-tab state capture, sourced from ~/.bashrc.
#
# `conda activate` mutates the running shell and never re-execs, so the active
# env never reaches /proc/<pid>/environ. The only place that can observe it is
# the shell itself, which is why this hook exists.
#
# Source this at the END of ~/.bashrc, after conda's init block -- the restore
# path at the bottom needs `conda activate` to be defined.

# Only real gnome-terminal tabs have this. VSCode's integrated terminals do not,
# which is exactly the filter we want: their cwd is the workspace folder and
# restoring them is VSCode's job, not ours.
[[ -n "$GNOME_TERMINAL_SCREEN" ]] || return 0

# Guard against a double source (e.g. .bashrc re-sourced by hand).
[[ -n "$_SESSIONCTL_HOOKED" ]] && return 0
_SESSIONCTL_HOOKED=1

_sessionctl_dir="${XDG_STATE_HOME:-$HOME/.local/state}/sessionctl/shells"
_sessionctl_file="$_sessionctl_dir/$$.json"
_sessionctl_last=

# Runs before every prompt, so it must not fork. Everything below is a bash
# builtin: the parameter expansions, printf, and the redirect. In the steady
# state this is one string compare and nothing else.
_sessionctl_save() {
    local key="$PWD:$CONDA_DEFAULT_ENV"
    [[ $key == "$_sessionctl_last" ]] && return 0

    local cwd=$PWD conda=$CONDA_DEFAULT_ENV screen=$GNOME_TERMINAL_SCREEN
    cwd=${cwd//\\/\\\\};       cwd=${cwd//\"/\\\"}
    conda=${conda//\\/\\\\};   conda=${conda//\"/\\\"}
    screen=${screen//\\/\\\\}; screen=${screen//\"/\\\"}

    [[ -d $_sessionctl_dir ]] || mkdir -p "$_sessionctl_dir" 2>/dev/null || return 0

    printf '{"pid":%d,"screen":"%s","cwd":"%s","conda":"%s","ts":%d}\n' \
        "$$" "$screen" "$cwd" "$conda" "${EPOCHSECONDS:-0}" \
        > "$_sessionctl_file" 2>/dev/null

    _sessionctl_last=$key
}

_sessionctl_cleanup() {
    rm -f "$_sessionctl_file" 2>/dev/null
}
trap _sessionctl_cleanup EXIT

PROMPT_COMMAND="_sessionctl_save${PROMPT_COMMAND:+; $PROMPT_COMMAND}"

# Restore path: `sessionctl restore` launches tabs as
#   bash -c 'SESSIONCTL_CONDA=<env> exec bash -i'
# so the env name arrives here rather than in the gnome-terminal command line,
# where quoting it would be fragile.
if [[ -n "$SESSIONCTL_CONDA" ]]; then
    conda activate "$SESSIONCTL_CONDA" 2>/dev/null
    unset SESSIONCTL_CONDA
fi
