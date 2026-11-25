# Security policy

MacPC Remote lets a remote model control a real Mac, so we take security issues seriously.

## How to report a vulnerability

**Please do not disclose vulnerability details in a public Issue.** Prefer GitHub's *Security -> Report a vulnerability* (private vulnerability reporting), and include the affected version (`agent_version` from `get_status`), reproduction steps and the impact.

Do not attach real tokens, private keys or private screen content; reproduce with fake data.

## Scope

In scope: bypassing token authentication or the Host / Origin checks; bypassing application or file authorization (block-lists, sensitive paths, `file_roots`, extension blocking, symlinks, resource forks, etc.); reading or modifying the token, private keys, keychains or the Agent's own directory through the file or transfer tools; lifting the emergency stop remotely; the audit log leaking content that should not be recorded.

Known design limitations (not vulnerabilities; see "Security model and known risks" in the README): `approval_mode: all` / `bypass`; app authorization is not a sandbox; the Screen Recording / Accessibility permissions belong to the app that starts the Agent (e.g. Terminal) and therefore to everything run in it; prompt injection through screen content; other programs of the same macOS user can read the plain-text token; the sensitive-path list cannot cover every app's secrets.

## Supported versions

Only the latest release is fixed.
