#!/bin/bash
# Install the short commands mac-remote-on / mac-remote-bypass / mac-remote-off (like win-remote-on on Windows).
# They go into ~/.local/bin (created if needed); add it to PATH if your shell does not have it yet.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="${1:-$HOME/.local/bin}"
mkdir -p "$BIN"
for mode in on bypass off; do
    printf '#!/bin/bash\nexec "%s/mac-remote.sh" %s\n' "$HERE" "$mode" > "$BIN/mac-remote-$mode"
    chmod +x "$BIN/mac-remote-$mode"
done
echo "Installed mac-remote-on, mac-remote-bypass, mac-remote-off into $BIN"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "Note: $BIN is not on your PATH; add this line to ~/.zshrc:  export PATH=\"$BIN:\$PATH\"" ;;
esac
