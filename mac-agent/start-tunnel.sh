#!/bin/bash
# SSH reverse tunnel (with automatic reconnect): maps the agent port on this Mac to 127.0.0.1 on the Linux server.
#
#   ./start-tunnel.sh -s myserver.example.com -u alice [-k ~/.ssh/key] [-p 22] [-P 8766] [-R 8766]
#   ./start-tunnel.sh -H myserver          # use a Host alias from ~/.ssh/config instead of -s/-u/-k/-p
set -uo pipefail
SERVER="" USER_="" KEY="" SSH_PORT=22 PORT=8766 REMOTE_PORT="" HOST_ALIAS=""
while getopts "s:u:k:p:P:R:H:" o; do
    case "$o" in
        s) SERVER="$OPTARG" ;; u) USER_="$OPTARG" ;; k) KEY="$OPTARG" ;; p) SSH_PORT="$OPTARG" ;;
        P) PORT="$OPTARG" ;; R) REMOTE_PORT="$OPTARG" ;; H) HOST_ALIAS="$OPTARG" ;;
        *) echo "usage: $0 -s server -u user [-k key_file] [-p ssh_port] [-P agent_port] [-R server_port] | -H ssh_config_alias" >&2; exit 2 ;;
    esac
done
REMOTE_PORT="${REMOTE_PORT:-$PORT}"
ARGS=(-N -R "127.0.0.1:${REMOTE_PORT}:127.0.0.1:${PORT}"     # bind only the server's loopback address
      -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes
      -o StrictHostKeyChecking=accept-new)
if [ -n "$HOST_ALIAS" ]; then
    TARGET="$HOST_ALIAS"
else
    if [ -z "$SERVER" ] || [ -z "$USER_" ]; then echo "need -s server and -u user (or -H alias)" >&2; exit 2; fi
    ARGS+=(-p "$SSH_PORT")
    if [ -n "$KEY" ]; then ARGS+=(-i "${KEY/#\~/$HOME}" -o IdentitiesOnly=yes); fi
    TARGET="$USER_@$SERVER"
fi
while true; do
    echo "[$(date +%H:%M:%S)] Connecting $TARGET, forwarding server 127.0.0.1:${REMOTE_PORT} -> this Mac 127.0.0.1:${PORT}"
    ssh "${ARGS[@]}" "$TARGET"
    echo "[$(date +%H:%M:%S)] Tunnel disconnected (exit code $?), reconnecting in 5 s ..."
    sleep 5
done
