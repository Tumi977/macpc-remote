# macpc plugin

Lets Claude Code on a remote Linux server see and operate your Mac through `macpc-agent` (which you run on the Mac separately): screenshots, clicks, typing, key presses, file reads/writes and binary file transfer.

This directory contains:

- `.mcp.json`: connects Claude Code to the agent. The token and URL are read from the environment variables `MACPC_TOKEN` / `MACPC_URL` (default `http://127.0.0.1:8766/mcp`); nothing is written to a file;
- `skills/mac-control/SKILL.md`: teaches the model to use the tools safely (look before acting, Mac shortcuts use `cmd`, pass `expect_app` on every key press, paste long text, ask before risky actions);
- `scripts/macpc_cp.py`: a command-line tool for transferring files between Linux and the Mac (standard library only; content never passes through the conversation; chunked, sha256-verified and resumable).

Install (from a clone of the repository):

```bash
claude plugin marketplace add /path/to/macpc-remote
claude plugin install macpc@macpc-remote
```

It can be installed next to the `winpc` plugin: the server names, tools, environment variables and the default port (8766 vs 8765) are all distinct.

For installation on the Mac, the macOS permissions, the SSH reverse tunnel, the security model and known risks, see the repository [README](../../README.md).
