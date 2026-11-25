# MacPC Remote

Let **Claude Code running on a remote Linux server** operate **your Mac** through continuous screenshots, keyboard/mouse input, file reads/writes and binary file transfer.

- **Mac side**: a resident agent (`macpc-agent`) that exposes screenshot, click, typing, key-press, file and transfer tools over MCP (Streamable HTTP).
- **Linux side**: a Claude Code plugin `macpc` (MCP config + Skill) that connects to the agent through an **SSH reverse tunnel** (or Tailscale).

This is the macOS sibling of [WinPC Remote](https://github.com/Tumi977/winpc-remote): same tools, same security model, same Linux-side workflow. The two plugins can be installed side by side (different server names, environment variables and ports: `macpc` / `MACPC_TOKEN` / 8766 vs `winpc` / `WINPC_TOKEN` / 8765).

> **This is a third-party community project and is not affiliated with Anthropic or Apple.**

> ## ⚠️ Read this first
> Installing this means **letting a model that can be influenced by what is on your screen operate your keyboard and mouse, as you, on your desktop.**
> The project provides several layers of protection (token, app authorization, sensitive-path blocking, emergency stop, audit log), but it is **not a sandbox**.
> On macOS the agent also needs the **Screen Recording** and **Accessibility** permissions, which macOS grants to the app that starts it (normally Terminal): anything else you run in that Terminal gets them too.
> Please read [Security model and known risks](#6-security-model-and-known-risks) in full. **Strongly recommended:** a dedicated macOS user account or a VM.

---

## Table of contents

1. [How it works](#1-how-it-works)
2. [Quick start](#2-quick-start)
3. [Mac setup](#3-mac-setup)
4. [Networking](#4-networking)
5. [Linux setup](#5-linux-setup)
6. [Security model and known risks](#6-security-model-and-known-risks)
7. [Tools and usage](#7-tools-and-usage)
8. [Testing](#8-testing)
9. [Troubleshooting](#9-troubleshooting)
10. [Upgrading, rotating the token, uninstalling](#10-upgrading-rotating-the-token-uninstalling)
11. [Project layout and license](#11-project-layout-and-license)

---

## 1. How it works

```
┌──────────────── Linux server ────────────────┐          ┌────── Mac (logged in, screen unlocked) ─────────────┐
│  Claude Code                                 │          │  macpc-agent (Python, started from Terminal)        │
│   ├─ plugin macpc                            │          │   ├─ MCP server (Streamable HTTP) 127.0.0.1:8766    │
│   │   ├─ .mcp.json → http://127.0.0.1:8766   │   SSH    │   ├─ Bearer-token check + Host/Origin check         │
│   │   └─ skills/mac-control/SKILL.md         │◄═reverse═│   ├─ App authorization (prompt/allowlist/blocklist) │
│   └─ model: reads screenshots, decides       │  tunnel  │   ├─ Screenshots: CGDisplayCreateImage → JPEG       │
│  127.0.0.1:8766 ← tunnel exit (server only)  │   (-R)   │   ├─ Input: CGEvent (Unicode-safe, bypasses IMEs)   │
└──────────────────────────────────────────────┘          │   ├─ Emergency stop: mouse to top-left → pause      │
                                                          │   └─ Audit log: logs/actions.jsonl                  │
                                                          └─────────────────────────────────────────────────────┘
```

The connection is **initiated by the Mac**: `ssh -R` maps the agent port to `127.0.0.1` on the server, so the Mac works behind NAT and the agent is never exposed to any network.

How it maps macOS onto the Windows design:

| Concern | Windows version | Mac version |
|---|---|---|
| Screenshot | mss | `CGDisplayCreateImage` (full Retina resolution) |
| Coordinates | physical pixels (DPI-aware process) | **points**; screenshot pixels are converted with the Retina factor |
| Input | `SendInput` | `CGEventPost` (mouse, keys, scroll); text via `CGEventKeyboardSetUnicodeString` |
| Input method (Pinyin etc.) | Unicode input bypasses the IME | the agent switches to an ASCII layout (e.g. ABC) while typing and switches back |
| "Window under the mouse" | `WindowFromPoint` | Accessibility hit test (`AXUIElementCopyElementAtPosition`); the Dock and status items are handled |
| App identity | process name (`notepad.exe`) | **bundle id** (`com.apple.textedit`), also matching the English / localized name and executable name |
| Approval dialog | topmost tkinter window in a subprocess | **non-activating** floating `NSPanel` in a subprocess (does not take focus from the target app); `osascript` fallback |
| Paste | Win32 clipboard, text only restored | `NSPasteboard`, **every item and type** backed up and restored; marked transient for clipboard managers |
| Elevation / UIPI | elevated windows refused | not applicable; system password / Touch ID prompts are on the block-list |
| Lock screen | black-image heuristic | `CGSessionCopyCurrentDictionary` + black-image heuristic |

### Prerequisites

| | Requirement |
|---|---|
| Mac | macOS 11+ (tested on 12.7, Intel), a **logged-in GUI session** (unusable while locked), Python 3.10+ (python.org, Homebrew or `uv`), the **Screen Recording** and **Accessibility** permissions for the app that starts the agent |
| Linux | A server that can run Claude Code, with an OpenSSH server |
| Other | The Mac can reach the server over SSH |

---

## 2. Quick start

```text
1. Mac:          clone the repo, open Terminal, run mac-agent/start-agent.sh   → note the token printed on first start
2. Mac:          grant Terminal "Screen Recording" + "Accessibility", restart the agent         (3.2)
3. Mac:          fill in mac-remote.config.json, run mac-agent/mac-remote.sh on                 (3.4, 4)
4. Server:       put the token into ~/.claude/settings.json (env), install the plugin           (5)
5. Claude Code:  /mcp shows macpc as connected → "use macpc's get_status to check my Mac"
```

No Mac at hand? `--backend fake` lets you try the Linux side ([Testing](#8-testing)).

---

## 3. Mac setup

### 3.1 Get the code and start the agent

```bash
git clone <this repository> ~/macpc-remote
cd ~/macpc-remote/mac-agent
./start-agent.sh
```

`start-agent.sh` finds Python 3.10+, creates `.venv`, installs the dependencies (with `uv` when available, otherwise `pip`; prebuilt wheels are preferred, because the newest releases of some dependencies have no wheel for older macOS / Intel Macs), copies `config.example.json` to `config.json` (mode 600) and starts the agent. On first start it prints the **access token** once (it is also saved in `config.json`).

**Start it from a Terminal window on the Mac, not over SSH.** An SSH session has no access to the window server's contents and cannot be given the permissions below.

### 3.2 macOS permissions (required)

macOS attributes the permissions of a command-line program to the app that started it, so you grant them to **Terminal** (or iTerm, …):

1. **System Settings → Privacy & Security → Accessibility** (macOS 12: System Preferences → Security & Privacy → Privacy): enable **Terminal**. Without it every mouse/keyboard event is silently dropped; the agent detects this and returns a clear error.
2. **… → Screen Recording**: enable **Terminal**, then **quit and restart the agent** (this permission only takes effect in a new process). Without it macOS hides every other app's windows: screenshots show only the wallpaper and the menu bar.

The agent asks macOS to show both prompts at start-up, prints which permission is missing, and `get_status` reports `permissions` and `permission_holder_app`.

> Granting Terminal these permissions applies to **everything** you run in Terminal. If that is too broad, use a separate terminal app only for the agent (for example iTerm just for this), or a dedicated macOS user.

### 3.3 Configuration (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `8766` | Listening address. Keep loopback with the SSH tunnel |
| `token` | generated | Bearer token; the `MACPC_TOKEN` environment variable overrides it |
| `allowed_hosts` | `[]` | Extra `Host` values (e.g. `100.x.y.z:*` with Tailscale) |
| `approval_mode` | `prompt` | `prompt` (dialog on the Mac), `allowlist` (refuse anything not listed), `all` (no dialogs; not recommended) |
| `approval_timeout_s` | 60 | No click within this time = deny |
| `allowed_apps` | `["com.apple.TextEdit"]` | Apps that need no approval. Bundle id, app name (`TextEdit`, `TextEdit.app`) or executable name |
| `blocked_apps` | password managers, Keychain Access, Passwords, `SecurityAgent`, `coreautha` (Touch ID), `loginwindow` | Never operated, in any mode |
| `max_long_edge` / `jpeg_quality` | 1280 / 70 | Screenshot size and quality |
| `settle_ms` | 600 | Wait before the screenshot attached to an action |
| `file_tools_enabled` / `file_roots` | `true` / Desktop, Documents | File tools; reads inside the roots need no approval |
| `file_blocked_paths` / `file_blocked_write_ext` | `[]` / `.command .sh .app .pkg .dmg .scpt .workflow .webloc …` | Extra blocked path substrings / extensions that cannot be written |
| `transfer_max_bytes` | 512 MiB | Per-file limit for `get_file` / `put_file` |
| `type_delay_ms` / `type_newline_delay_ms` | 5 / 60 | Pacing of `type_text` |
| `paste_restore_delay_ms` | 600 | `method="paste"`: wait before restoring the clipboard |
| `switch_input_source` | `true` | Switch to an ASCII layout while typing so an input method (Pinyin, Japanese, …) cannot intercept the keys |

To find an app's bundle id: `osascript -e 'id of app "TextEdit"'`, or look at the "Foreground app: …" line of any tool result.

### 3.4 One-command start/stop (agent + tunnel)

```bash
cp mac-remote.config.example.json mac-remote.config.json    # then edit it
./mac-remote.sh on       # opens two Terminal windows: agent + tunnel
./mac-remote.sh bypass   # same, but without approval dialogs (block-lists still apply)
./mac-remote.sh off      # stops both
```

`bin/mac-remote-on.command` (and `-bypass`, `-off`) do the same on a double-click in Finder.

`mac-remote.config.json` either names the server:

```json
{ "server": "your.server.example.com", "user": "macpc-tunnel", "key_file": "~/.ssh/macpc_tunnel_ed25519", "ssh_port": 22, "port": 8766 }
```

or reuses a `Host` entry from `~/.ssh/config`:

```json
{ "ssh_host": "myserver", "port": 8766 }
```

### 3.5 Keep the Mac usable remotely

- The agent only works while you are logged in and the screen is **unlocked**. Turn off automatic screen lock / display sleep for unattended use (System Settings → Lock Screen / Battery), or run it in a dedicated user account.
- A sleeping display produces black screenshots; `get_status` reports `screen_locked` / `screen_maybe_asleep`.

---

## 4. Networking

### Option A (recommended): SSH reverse tunnel

`start-tunnel.sh` runs `ssh -N -R 127.0.0.1:8766:127.0.0.1:8766` with `ServerAliveInterval=30`, `ExitOnForwardFailure=yes`, `StrictHostKeyChecking=accept-new`, and reconnects 5 seconds after a drop:

```bash
./start-tunnel.sh -s your.server.example.com -u macpc-tunnel -k ~/.ssh/macpc_tunnel_ed25519   # or: -H <ssh_config alias>
```

**Use a dedicated, restricted key.** The tunnel only needs to listen on `127.0.0.1:8766` on the server:

```bash
# Mac
ssh-keygen -t ed25519 -f ~/.ssh/macpc_tunnel_ed25519 -C macpc-tunnel
# Server
sudo useradd -m -s /usr/sbin/nologin macpc-tunnel
sudo install -d -m 700 -o macpc-tunnel -g macpc-tunnel /home/macpc-tunnel/.ssh
```

One line in `/home/macpc-tunnel/.ssh/authorized_keys` (mode 600, owned by `macpc-tunnel`):

```text
restrict,port-forwarding,permitlisten="127.0.0.1:8766",permitopen="127.0.0.1:1",command="/bin/echo tunnel-only" ssh-ed25519 AAAA… macpc-tunnel
```

`restrict` removes every capability, `port-forwarding` re-enables forwarding, `permitlisten` limits `-R` to this one port, `permitopen` neuters `-L`, and the forced `command` means a leaked key gives no shell. Keep `GatewayPorts no` on the server.

Verify on the server (**expect 401**: tunnel up, agent running, token enforced):

```bash
curl -i http://127.0.0.1:8766/mcp
```

> Other users on the server can also reach `127.0.0.1:8766`; that is why the token is mandatory.

### Option B: Tailscale

Set `"host": "100.x.y.z"` (the Mac's Tailscale IP) in `config.json` and `MACPC_URL=http://100.x.y.z:8766/mcp` on Linux. The agent is then reachable from the whole tailnet (still token-protected); the tunnel is the tighter option.

---

## 5. Linux setup

### 5.1 Place the token

In `~/.claude/settings.json` (merge into the existing `env` object), then `chmod 600 ~/.claude/settings.json` and restart Claude Code:

```json
{ "env": { "MACPC_TOKEN": "the token printed on the Mac", "MCP_TOOL_TIMEOUT": "120000" } }
```

Keep the token in user-level files only; never in a project `.mcp.json` that gets committed.

### 5.2 Install the plugin

```bash
claude plugin marketplace add /path/to/macpc-remote
claude plugin install macpc@macpc-remote
```

The plugin's `.mcp.json` reads `MACPC_TOKEN` / `MACPC_URL` (default `http://127.0.0.1:8766/mcp`) from the environment, so the token is never written to a file. It also ships the Skill `mac-control` and the transfer script `scripts/macpc_cp.py`.

Without the plugin:

```bash
claude mcp add --scope user --transport http macpc http://127.0.0.1:8766/mcp --header 'Authorization: Bearer ${MACPC_TOKEN}'
```

(**single quotes**, so the literal `${MACPC_TOKEN}` is stored instead of the token itself), and copy `claude-plugin/macpc/skills/mac-control/` to `~/.claude/skills/`.

### 5.3 Verify

```bash
claude mcp list          # plugin:macpc:macpc … ✔ Connected
```

Then ask Claude Code: *"Use macpc's get_status to check my Mac, then take a screenshot and tell me what is on the screen."*

---

## 6. Security model and known risks

### 6.1 Layers of protection

| Layer | Mechanism |
|---|---|
| Network | Loopback only by default; machines are bridged only through an SSH tunnel (restricted key, 4) or Tailscale |
| Authentication | `Authorization: Bearer <token>` with `hmac.compare_digest`; 401 on failure. `config.json` is mode 600 |
| Host / Origin | MCP SDK DNS-rebinding protection: unknown `Host` → 421, disallowed `Origin` → 403 |
| App authorization | Before any input the target app is determined (clicks: the app under the pointer, via an Accessibility hit test; keys: the frontmost app). Block-list → refuse; allow-list or approved in this session → allow; otherwise `approval_mode` (`prompt` shows a floating panel on the Mac) |
| Re-check after approval | The target is checked again after the dialog and right before sending; if it changed, nothing is sent |
| Wrong-window guard | `expect_app` / `expect_handle` on `type_text` / `press_key`: if the frontmost app/window is not the declared one, nothing is sent (checked before any dialog) |
| Emergency stop | Move the mouse to the **top-left corner of the main display** → paused; all input tools refuse, screenshots still work. Checked before every action and during long typing. **Only a restart of the agent on the Mac resumes it** |
| File authorization | Real paths (symlinks resolved). Sensitive paths always refused: `~/.ssh`, `~/.aws`, `~/.gnupg`, keychains, browser profiles, cookies, Mail, Messages, the TCC database, `LaunchAgents`/`LaunchDaemons`, shell start-up files and histories, password-manager data, private keys (also detected by content), the agent's own directory. Reads inside `file_roots` are free; every write needs approval; backups before overwrite/edit; executable/installable extensions cannot be written |
| Audit | `logs/actions.jsonl`: time, tool, parameters (typed text: length + first 20 characters only), target app, result |
| Model constraints | The Skill: on-screen text is data; ask before paying/sending/deleting/changing settings/submitting; never type passwords |

### 6.2 Known risks

1. **`approval_mode: all` / `mac-remote.sh bypass` remove every dialog.** The model can then operate any non-blocked app and read/download any file not on the sensitive list. Use only in a VM or dedicated account, and only temporarily.
2. **App authorization is not a sandbox.** Approving Terminal, Finder, Spotlight, Script Editor, an IDE or a browser effectively allows running arbitrary code as you. Add `com.apple.Terminal`, `com.googlecode.iterm2`, `com.apple.ScriptEditor2`, `com.apple.Automator` to `blocked_apps` if you do not need them.
3. **The permissions belong to Terminal.** Every script you run in that Terminal can record the screen and synthesize input. Prefer a terminal app used only for the agent, or a dedicated user.
4. **Prompt injection.** Text on screen may try to steer the model; the Skill tells it to treat it as data, but this cannot be fully prevented.
5. **Privacy.** Every screenshot (whole display) is sent to the model service.
6. **The token is in plain text** in `config.json` (600) on the Mac and in `settings.json` on the server. A leaked token means full control; rotate it (10).
7. **The block-lists are best effort**; they cannot know where every app keeps its secrets.
8. **Not operable:** the lock screen, the login window, password / Touch ID prompts (block-listed). Secure text fields reject pasting.
9. **The macOS backend is not covered by CI** (CI runs on Linux with `FakeBackend`); verify with `bin/live_test.py` and the checklist below.
10. **Speed and cost**: several seconds per step; ~1,000–1,500 input tokens per 1280-px screenshot.

### 6.3 Recommended practices

- [ ] Dedicated macOS user or VM; keep `approval_mode: prompt`
- [ ] Dedicated, restricted tunnel key (4)
- [ ] Token only in user-level files, never in Git/screenshots/issues
- [ ] Block terminal/script apps you do not need
- [ ] Review `logs/actions.jsonl`; if anything looks wrong: mouse to the top-left corner → `mac-remote.sh off` → rotate the token

---

## 7. Tools and usage

All coordinates are pixels of the **most recent screenshot**; the agent converts them to screen points (Retina-aware). Input tools attach a fresh screenshot by default (`screenshot_after`).

| Tool | Purpose |
|---|---|
| `screenshot(monitor?)` | Capture a display; text includes sizes, display list, frontmost app/window |
| `zoom(x1,y1,x2,y2)` | Crop at native (Retina) resolution to read small text |
| `click(x,y,button,clicks,modifiers)` | Left/right/middle, double/triple click, modifiers like `cmd`, `shift`, `cmd+shift` |
| `move_mouse`, `drag`, `scroll` | Hover, drag, scroll (`amount` notches of 3 lines) |
| `type_text(text, press_enter, method, expect_app, expect_handle)` | `keys` (default) or `paste` (one Cmd+V, clipboard restored) |
| `press_key(keys, repeat, expect_app, expect_handle)` | `cmd+s`, `cmd+space`, `cmd+tab`, `alt+left`, `cmd+shift+4`…; `win`/`super` = `cmd` |
| `wait(seconds)` | Wait then screenshot |
| `list_windows`, `focus_window(handle)` | On-screen windows (handle = CGWindowID); raise + activate |
| `get_status` | Version, paused, lock state, permissions, approvals, displays |
| `read_file`, `list_files`, `write_file`, `edit_file` | Text files with encoding/newline preservation and backups |
| `stat_file`, `get_file`, `put_file` | Binary, chunked, sha256-verified, resumable transfer |

### File transfer: `macpc_cp.py`

```bash
export MACPC_TOKEN=…     # or rely on settings.json when run by Claude Code
python3 claude-plugin/macpc/scripts/macpc_cp.py put ./report.pdf '~/Desktop/'
python3 claude-plugin/macpc/scripts/macpc_cp.py get '~/Desktop/photo.heic' ./downloads/
python3 claude-plugin/macpc/scripts/macpc_cp.py stat '~/Desktop/photo.heic' --sha256
```

Quote `~` so the Linux shell does not expand it; the agent expands it on the Mac. Content never passes through the model context; uploads are atomic and can be resumed with `--resume`.

---

## 8. Testing

### Automated tests (Linux, no Mac needed)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

They cover coordinate conversion (incl. Retina), key parsing, the authorization policy, re-checks and `expect_app`, pause, audit redaction, file policy, transfer, the HTTP layer (401/421, no `structuredContent`), keep-alive, and the macOS backend's event generation against a fake Quartz layer (Unicode typing, modifier flags, click states, scrolling, the emergency-stop corner, input-source switching, clipboard backup/restore, dialog-subprocess isolation).

### Try the Linux side without a Mac

```bash
.venv/bin/python mac-agent/macpc_agent.py --backend fake --config /tmp/fake-config.json
```

### Live smoke test (real Mac)

On the server, with the agent and tunnel running and both permissions granted:

```bash
MACPC_TOKEN=… .venv/bin/python mac-agent/bin/live_test.py
```

It opens TextEdit through Spotlight, types Chinese/English/emoji (keys and paste), exercises every desktop tool inside that window, and quits TextEdit without saving. It refuses to start if TextEdit already has windows open.

### Manual acceptance checklist

1. With Pinyin (or another input method) active, `type_text` of mixed Chinese/English/emoji is exact, and the input method is restored afterwards.
2. On a Retina display, clicks on small targets (menu bar items, buttons) land exactly.
3. The first operation on Chrome shows the approval panel at the top of the screen without taking focus; "Deny" stops Claude, "Allow" is remembered for the session.
4. A block-listed app (Keychain Access) is refused.
5. Moving the mouse to the top-left corner pauses the agent; all input is refused until restart.
6. Disconnect the network; the tunnel reconnects by itself.
7. Lock the screen: `screenshot` / `get_status` report it.
8. End-to-end: "Open TextEdit, write three lines of meeting notes, save to the Desktop as test.txt".

---

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| **401** | Tokens differ: compare `MACPC_TOKEN` in the server's `settings.json` with `token` in the Mac's `config.json`; restart Claude Code |
| **421** | `Host` not allowed: add it to `allowed_hosts` (Tailscale / custom domain) |
| Connection refused | Agent or tunnel not running; `curl -i http://127.0.0.1:8766/mcp` on the server should give 401 |
| `remote port forwarding failed` | Port 8766 still held on the server by a stale connection (wait or use another `-R` port), or `permitlisten` does not match |
| Screenshots show only the wallpaper and menu bar | Screen Recording not granted to the app that started the agent, or the agent was not restarted after granting |
| "Accessibility permission is missing" | Enable that app under Privacy & Security → Accessibility (no restart needed). If it is enabled but still refused, remove and re-add it (the entry can go stale after the app updates) |
| A Pinyin candidate window appears while typing | `switch_input_source` is off, or no ASCII-capable layout (ABC / U.S.) is enabled; add "ABC" in Keyboard → Input Sources |
| Clicks are offset | Take a fresh screenshot first (coordinates must come from the latest one, never from `zoom`) |
| Black screenshot / "screen is locked" | Unlock the Mac; disable display sleep / auto-lock for unattended use |
| `start-agent.sh` fails building `cryptography` (or another package) | No prebuilt wheel for your macOS/CPU; the script already prefers wheels. Install `uv` (`curl -LsSf https://astral.sh/uv/install.sh \| sh`) and rerun, or install Rust |
| "Paused by the user" | The emergency stop fired; restart the agent on the Mac |
| Approval panel did not appear | It runs as a separate process; if it cannot start, an AppleScript dialog is shown instead. Check the agent's Terminal window for errors |

---

## 10. Upgrading, rotating the token, uninstalling

**Upgrade**: `git pull` on the Mac, then `./mac-remote.sh off && ./mac-remote.sh on`; on Linux `claude plugin marketplace update macpc-remote && claude plugin update macpc@macpc-remote`. Check `agent_version` in `get_status`.

**Rotate the token**: `./mac-remote.sh off`, set `"token": ""` in `config.json`, start again and note the new token; update `MACPC_TOKEN` on the server and restart Claude Code.

**Uninstall**: `claude plugin uninstall macpc@macpc-remote && claude plugin marketplace remove macpc-remote`; on the Mac `./mac-remote.sh off`, delete the folder, and remove Terminal from Screen Recording / Accessibility if nothing else needs them.

---

## 11. Project layout and license

```
mac-agent/       macpc_agent.py (MCP server, authorization, auth, audit) · backends.py (macOS / Fake backends)
                 fileops.py (file read/write and path policy) · start-agent.sh · start-tunnel.sh · mac-remote.sh · bin/
claude-plugin/   Claude Code plugin macpc (.mcp.json + Skill + scripts/macpc_cp.py)
.claude-plugin/  marketplace.json
tests/           unit + end-to-end tests (FakeBackend / fake Quartz; run on Linux)
```

Changes: [CHANGELOG.md](CHANGELOG.md). Security reports: [SECURITY.md](SECURITY.md). License: MIT ([LICENSE](LICENSE)).
