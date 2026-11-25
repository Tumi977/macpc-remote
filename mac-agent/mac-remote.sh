#!/bin/bash
# Start/stop MacPC Remote with one command (Agent + SSH reverse tunnel, each in its own Terminal window).
#
#   mac-remote.sh on       # normal mode: the first operation on an unapproved app shows a confirmation dialog
#   mac-remote.sh bypass   # skip application approval prompts (the block-list and sensitive-path blocking still apply)
#   mac-remote.sh off      # stop the Agent and the tunnel
#
# The server connection details are read from mac-remote.config.json in the same directory (it is in .gitignore).
# First use: copy mac-remote.config.example.json to mac-remote.config.json and fill it in. Either give
# "server"/"user"(/"key_file"/"ssh_port"), or "ssh_host": the name of a Host entry in ~/.ssh/config.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-}"
case "$MODE" in on|bypass|off) ;; *) echo "usage: $0 on|bypass|off" >&2; exit 2 ;; esac
CFG="$HERE/mac-remote.config.json"

jget() {   # read a top-level field of the JSON config (JavaScript for Automation ships with every macOS)
    osascript -l JavaScript -e 'function run(a){var s=$.NSString.stringWithContentsOfFileEncodingError(a[0],4,null);var v=JSON.parse(s.js)[a[1]];return (v===undefined||v===null)?"":String(v)}' "$CFG" "$1"
}

PORT=8766
if [ -f "$CFG" ]; then
    p="$(jget port)"; [ -n "$p" ] && PORT="$p"
elif [ "$MODE" != off ]; then
    echo "Cannot find $CFG. Copy mac-remote.config.example.json to mac-remote.config.json and fill it in." >&2
    exit 1
fi

stop_all() {
    local n=0
    # the reconnect loops first (otherwise the tunnel would come back after 5 s), then the agent and ssh
    for pat in "start-tunnel.sh" "start-agent.sh" "macpc_agent.py" "ssh -N -R 127.0.0.1:${PORT}:"; do
        for pid in $(pgrep -f -- "$pat" || true); do
            [ "$pid" = "$$" ] && continue
            kill "$pid" 2>/dev/null && n=$((n + 1))
        done
    done
    echo "$n"
}

n="$(stop_all)"
if [ "$MODE" = off ]; then
    echo "Stopped MacPC Remote ($n process(es) ended)."
    exit 0
fi
[ "$n" -gt 0 ] && { echo "Stopped the old Agent/tunnel ($n process(es))."; sleep 1; }

alias_="$(jget ssh_host)"
if [ -n "$alias_" ]; then
    TUNNEL_ARGS="-H '$alias_'"
else
    server="$(jget server)"; user="$(jget user)"
    if [ -z "$server" ] || [ -z "$user" ]; then echo "mac-remote.config.json needs ssh_host, or server and user" >&2; exit 1; fi
    TUNNEL_ARGS="-s '$server' -u '$user'"
    k="$(jget key_file)"; [ -n "$k" ] && TUNNEL_ARGS="$TUNNEL_ARGS -k '$k'"
    sp="$(jget ssh_port)"; [ -n "$sp" ] && TUNNEL_ARGS="$TUNNEL_ARGS -p '$sp'"
fi
AGENT_ARGS=""
[ "$MODE" = bypass ] && AGENT_ARGS="--bypass-permissions"

term() {   # open a new Terminal window running the given command
    osascript -e "tell application \"Terminal\" to do script \"$1\"" >/dev/null
}
term "cd '$HERE' && ./start-agent.sh $AGENT_ARGS"
term "cd '$HERE' && ./start-tunnel.sh $TUNNEL_ARGS -P $PORT"

label="normal (unapproved apps show a confirmation dialog)"
[ "$MODE" = bypass ] && label="bypass (approval prompts skipped)"
echo "Started the Agent and the tunnel in two Terminal windows, mode: $label. Keep them open."
echo "To stop: $HERE/mac-remote.sh off"
