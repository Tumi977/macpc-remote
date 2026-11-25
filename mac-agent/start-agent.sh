#!/bin/bash
# Start macpc-agent: finds Python 3.10+, creates the venv, installs dependencies, generates a token on first run.
#
#   ./start-agent.sh                       # normal mode
#   ./start-agent.sh --bypass-permissions  # skip application approval prompts (the block-list still applies)
#   ./start-agent.sh --backend fake        # demo without touching the desktop
#
# Run it from a Terminal window on the Mac (not over SSH): macOS gives the Screen Recording and Accessibility
# permissions to the app that started the Agent (Terminal, iTerm, ...), and an SSH session has neither.
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"

# 1. Find Python 3.10+
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 "$HOME/.local/bin/python3" /usr/local/bin/python3 /opt/homebrew/bin/python3; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        PY="$(command -v "$cand")"; break
    fi
done
if [ -z "$PY" ]; then
    echo "Python 3.10+ not found. Install it from https://www.python.org/downloads/macos/ (or: brew install python / uv python install 3.12)." >&2
    exit 1
fi

# 2. venv + dependencies (reinstalled only when requirements.txt changes). uv is used when available (fast);
#    otherwise pip. Prebuilt wheels are preferred: the newest releases of some dependencies (e.g. cryptography,
#    pulled in by the MCP SDK) ship no wheel for older macOS / Intel, and building them needs a Rust toolchain.
UV="$(command -v uv || true)"; [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
if [ ! -x .venv/bin/python ]; then
    echo "Creating virtual environment .venv with $PY ..."
    if [ -n "$UV" ]; then "$UV" venv --quiet --python "$PY" .venv; else "$PY" -m venv .venv; fi
fi
HASH="$(shasum -a 256 requirements.txt | cut -d' ' -f1)"
if [ ! -f .venv/.requirements.sha256 ] || [ "$(cat .venv/.requirements.sha256)" != "$HASH" ]; then
    echo "Installing dependencies ..."
    if [ -n "$UV" ]; then
        "$UV" pip install --python .venv/bin/python --no-build -r requirements.txt \
            || "$UV" pip install --python .venv/bin/python -r requirements.txt
    else
        .venv/bin/python -m pip install --quiet --upgrade pip
        .venv/bin/python -m pip install --only-binary=:all: -r requirements.txt \
            || .venv/bin/python -m pip install -r requirements.txt
    fi
    echo "$HASH" > .venv/.requirements.sha256
fi

# 3. Config: copied from the example on first run; when the token is empty the agent generates one and prints it only once
if [ ! -f config.json ]; then
    cp config.example.json config.json
    echo "Created config file $HERE/config.json (edit allowed_apps / approval_mode etc.)."
fi
chmod 600 config.json      # it stores the access token in plain text

echo "Starting macpc-agent (Ctrl+C to quit; emergency stop: move the mouse to the top-left corner of the main display) ..."
exec .venv/bin/python macpc_agent.py --config "$HERE/config.json" "$@"
