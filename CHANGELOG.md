# Changelog

This project follows semantic versioning (0.x: interfaces may still change).

### 0.1.1

- Fix: the approval panel's window level was reset to "floating" (3) by `setFloatingPanel_`, so menus, Spotlight and other panels could cover it. It is now set afterwards (screen-saver level, verified on a real Mac); regression test added.
- Skill: commands in Terminal are typed into a new window (`cmd+n`) pinned with `expect_handle`, because activating Terminal usually lands in the Agent's or the tunnel's window (found in live testing; `expect_app` alone cannot tell Terminal windows apart).
- Permission hints now say that the Agent must be restarted after granting Accessibility too (macOS 12).
- New `bin/functional_test.py`: an automated functional test round on a real Mac (53 checks).

### 0.1.0

First release: the macOS port of WinPC Remote 0.4.0.

- Same MCP tools and parameters as WinPC Remote (`screenshot`, `zoom`, `click`, `move_mouse`, `drag`, `scroll`, `type_text`, `press_key`, `wait`, `list_windows`, `focus_window`, `get_status`, the file tools and the chunked transfer tools), the same security model (token, Host/Origin checks, app approval with re-check, `expect_app` / `expect_handle`, emergency stop, sensitive-path blocking, audit log) and the same Linux-side workflow (`macpc` plugin, `macpc_cp.py`).
- macOS backend: Quartz screenshots at Retina resolution with point-based coordinates; CGEvent input; text typed as Unicode key events with the input method (Pinyin etc.) temporarily switched to an ASCII layout; Accessibility hit-testing for "which app is under the pointer"; apps identified by bundle id with English/localized/executable-name aliases; a non-activating floating approval panel in a separate process (AppleScript fallback); paste that backs up and restores every pasteboard item and type.
- macOS permissions are detected and explained (`get_status.permissions`, hints in screenshots, an actionable error instead of silently dropped input).
- Defaults chosen to coexist with WinPC Remote on one server: port 8766, `MACPC_TOKEN` / `MACPC_URL`.
- `start-agent.sh` uses `uv` when available and prefers prebuilt wheels (the newest `cryptography` has no wheel for macOS 12 on Intel).
