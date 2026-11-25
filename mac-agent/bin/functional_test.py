"""Functional test round on a real Mac: realistic tasks end to end, each verified (not just "no error").

Requires the Agent in bypass mode (./mac-remote.sh bypass) and both macOS permissions. Everything is written to a
scratch folder ~/macpc-test (created and removed over SSH; the home folder itself is not a protected location), so
no macOS privacy prompt is triggered. The only windows touched are ones this script opens itself.

    MACPC_TOKEN=... MACPC_SSH=user@mac python functional_test.py [--keep] [--shots DIR]

Tests:
  T1 TextEdit via Spotlight: new plain-text document, keys + paste input with the input method active,
     save through the save sheet's "Go to folder" into ~/macpc-test, read back and compare byte-exact
  T2 file tools: create/refuse-overwrite/edit/append/read/list, then 10 things that must be refused
  T3 transfer with macpc_cp.py: upload, interrupted upload + resume, download, sha256 on both sides
  T4 Chrome: new window, address bar, page load, scroll, read, close only that window
  T5 guards: block-list (Keychain Access), expect_app mismatch, bad input validation
  T6 robustness: connection reuse after idle, tunnel killed and re-established
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[2]
CP = ROOT / "claude-plugin/macpc/scripts/macpc_cp.py"
WORK = "~/macpc-test"
results: list[tuple[str, str, bool, str]] = []
_shot_no = [0]          # one counter for the whole run, so images of later sessions do not overwrite earlier ones


class Abort(Exception):
    pass


def ssh(cmd: str, timeout=30) -> str:
    host = os.environ.get("MACPC_SSH")
    if not host:
        raise Abort("MACPC_SSH is not set")
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", host, cmd], capture_output=True, text=True, timeout=timeout)
    return (r.stdout + r.stderr).strip()


class Mac:
    def __init__(self, client, shots: Path | None):
        self.c, self.shots = client, shots

    async def __call__(self, tool: str, **args):
        r = await self.c.call_tool(tool, args)
        text = " ".join(p.text for p in r.content if p.type == "text")
        img = next((p.data for p in r.content if p.type == "image"), None)
        if img and self.shots:
            _shot_no[0] += 1
            (self.shots / f"{_shot_no[0]:02d}-{tool}.jpg").write_bytes(base64.b64decode(img))
        return r.is_error, text

    async def fg(self) -> tuple[str, str, int]:
        _, t = await self("screenshot")
        m = re.search(r"Foreground app: (.*?), window '(.*?)' \(handle=(\d+)\)", t)
        return (m.group(1).lower(), m.group(2), int(m.group(3))) if m else ("", "", 0)

    async def wait_front(self, app_id: str, timeout: float = 15) -> tuple[str, str, int]:
        """Poll until app_id is frontmost (a cold app start can take several seconds on an older Mac)."""
        deadline = time.time() + timeout
        while True:
            fg = await self.fg()
            if app_id in fg[0] or time.time() > deadline:
                return fg
            await asyncio.sleep(1)


def check(test: str, name: str, ok: bool, info: str = ""):
    results.append((test, name, bool(ok), info.replace("\n", " ")[:120]))
    return ok


def need(test: str, name: str, ok: bool, info: str = ""):
    if not check(test, name, ok, info):
        raise Abort(f"{test} {name}: {info}")


async def t1_textedit(m: Mac):
    T = "T1"
    TE = "com.apple.textedit"
    _, wl = await m("list_windows")
    need(T, "TextEdit has no open windows (safety)", TE not in wl.lower(), "close your TextEdit documents first")
    await m("press_key", keys="cmd+space", screenshot_after=False)
    await asyncio.sleep(1)
    app, *_ = await m.fg()
    need(T, "Spotlight opened", "spotlight" in app, app)
    err, t = await m("type_text", text="TextEdit", press_enter=True, expect_app="com.apple.Spotlight", screenshot_after=False)
    need(T, "typed app name into Spotlight", not err, t)
    app, *_ = await m.wait_front(TE)
    need(T, "TextEdit launched and frontmost", TE in app, app)
    await m("press_key", keys="cmd+n", expect_app=TE, screenshot_after=False)
    await asyncio.sleep(1)
    app, title, handle = await m.fg()
    need(T, "new TextEdit document frontmost", TE in app and handle, f"{app} '{title}'")
    await m("press_key", keys="cmd+shift+t", expect_app=TE, expect_handle=handle, screenshot_after=False)
    keys_part = "\u7b2c\u4e8c\u8f6e\u6d4b\u8bd5 round 2\n\u952e\u76d8\u8f93\u5165\uff1a\u4f60\u597d\uff0c\u4e16\u754c\uff01ABC xyz 123 \U0001f642\n"
    paste_part = "\u7c98\u8d34\u8f93\u5165\uff1a\u591a\u884c\u6587\u672c\n\u7b26\u53f7 ~!@#$%^&*()_+{}|:\"<>?\n"
    err, t = await m("type_text", text=keys_part, expect_app=TE, expect_handle=handle, screenshot_after=False)
    check(T, "type_text (keys, Pinyin active)", not err, t)
    err, t = await m("type_text", text=paste_part, method="paste", expect_app=TE, expect_handle=handle, screenshot_after=False)
    check(T, "type_text (paste)", not err, t)
    # Save sheet: name first, then Go to folder (cmd+shift+g) ~/macpc-test, then Return
    await m("press_key", keys="cmd+s", expect_app=TE, screenshot_after=False)
    await asyncio.sleep(1.2)
    await m("press_key", keys="cmd+a", expect_app=TE, screenshot_after=False)
    await m("type_text", text="round2-notes.txt", expect_app=TE, screenshot_after=False)
    await m("press_key", keys="cmd+shift+g", expect_app=TE, screenshot_after=False)
    await asyncio.sleep(1)
    await m("type_text", text=WORK + "/", press_enter=True, expect_app=TE, screenshot_after=False)
    await asyncio.sleep(1)
    await m("zoom", x1=100, y1=30, x2=900, y2=450)
    await m("press_key", keys="enter", expect_app=TE, screenshot_after=False)
    await asyncio.sleep(1.5)
    err, t = await m("read_file", path=f"{WORK}/round2-notes.txt")
    body = t.split("\n---\n", 1)[-1] if not err else t
    check(T, "saved file reads back byte-exact", not err and body == keys_part + paste_part, body[:100])
    check(T, "saved as UTF-8 plain text", "encoding utf-8" in t, t[:100])
    _, title, _ = await m.fg()
    check(T, "window title is the file name", "round2-notes" in title, title)
    await m("press_key", keys="cmd+q", expect_app=TE, screenshot_after=False)
    await asyncio.sleep(1.5)
    app, *_ = await m.fg()
    check(T, "TextEdit quit (document was saved, no prompt)", TE not in app, app)


async def t2_files(m: Mac):
    T = "T2"
    f = f"{WORK}/ft.md"
    ok = lambda r: not r[0]       # noqa: E731
    check(T, "write_file create", ok(await m("write_file", path=f, content="# \u6587\u4ef6\u5de5\u5177\nA\nB\n")))
    err, t = await m("write_file", path=f, content="x")
    check(T, "create on existing file refused", err and "already exists" in t, t)
    err, t = await m("edit_file", path=f, old_string="B", new_string="B\uff08\u5df2\u6539\uff09")
    check(T, "edit_file (+ backup)", not err and "backed up" in t, t)
    check(T, "append", ok(await m("write_file", path=f, content="C \U0001f642\n", mode="append")))
    err, t = await m("read_file", path=f)
    check(T, "read_file content exact", t.endswith("\n---\n# \u6587\u4ef6\u5de5\u5177\nA\nB\uff08\u5df2\u6539\uff09\nC \U0001f642\n"), t[-60:])
    err, t = await m("list_files", path=WORK, pattern="*.md")
    check(T, "list_files with pattern", not err and "ft.md" in t and "round2" not in t, t)
    check(T, "fake private key written (name looks harmless)",
          ok(await m("write_file", path=f"{WORK}/backup-notes", content="-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END OPENSSH PRIVATE KEY-----\n")))
    refused = {
        "read private key by content": ("read_file", {"path": f"{WORK}/backup-notes"}),
        "download private key by content": ("get_file", {"path": f"{WORK}/backup-notes"}),
        "read via symlink into ~/.ssh": ("read_file", {"path": f"{WORK}/link-to-ssh"}),
        "read ~/.ssh/config": ("read_file", {"path": "~/.ssh/config"}),
        "read ~/.zshrc": ("read_file", {"path": "~/.zshrc"}),
        "stat login keychain": ("stat_file", {"path": "~/Library/Keychains/login.keychain-db"}),
        "read the Agent's own config (token)": ("read_file", {"path": "~/macpc-remote/mac-agent/config.json"}),
        "write .command": ("write_file", {"path": f"{WORK}/evil.command", "content": "open -a Calculator"}),
        "write into LaunchAgents": ("write_file", {"path": "~/Library/LaunchAgents/com.macpc.evil.plist", "content": "<plist/>"}),
        "list Messages database folder": ("list_files", {"path": "~/Library/Messages"}),
    }
    for name, (tool, args) in refused.items():
        err, t = await m(tool, **args)
        check(T, f"refused: {name}", err, t)
    left = ssh(f"ls {WORK}/evil.command ~/Library/LaunchAgents/com.macpc.evil.plist 2>&1 | grep -vc 'No such file'")
    check(T, "nothing was created by refused writes", left.strip() == "0", left)


def t3_transfer(token: str, scratch: Path):
    T = "T3"
    env = {**os.environ, "MACPC_TOKEN": token}
    blob = scratch / "blob3.bin"
    blob.write_bytes(os.urandom(3 * 1024 * 1024 + 12345))
    want = hashlib.sha256(blob.read_bytes()).hexdigest()

    def cp(*a, timeout=120):
        r = subprocess.run([sys.executable, str(CP), "-q", *a], capture_output=True, text=True, env=env, timeout=timeout)
        return r.returncode, (r.stdout or r.stderr).strip()
    rc, out = cp("put", str(blob), f"{WORK}/")
    check(T, "upload into a folder (name kept)", rc == 0 and want in out, out)
    try:
        subprocess.run([sys.executable, str(CP), "-q", "--chunk-kib", "128", "put", str(blob), f"{WORK}/resume.bin"],
                       env=env, timeout=2.5, capture_output=True)
    except subprocess.TimeoutExpired:
        pass
    rc, out = cp("stat", f"{WORK}/resume.bin")
    part = json.loads(out).get("partial_upload_size", 0) if rc == 0 else 0
    check(T, "interrupted upload left a resumable part", 0 < part < blob.stat().st_size, out)
    rc, out = cp("--chunk-kib", "128", "put", "--resume", str(blob), f"{WORK}/resume.bin")
    check(T, "resume completes with the right sha256", rc == 0 and want in out, out)
    back = scratch / "back3.bin"
    back.unlink(missing_ok=True)
    rc, out = cp("get", f"{WORK}/resume.bin", str(back))
    check(T, "download sha256 matches the original", rc == 0 and hashlib.sha256(back.read_bytes()).hexdigest() == want, out)
    rc, out = cp("stat", f"{WORK}/blob3.bin", "--sha256")
    check(T, "Mac-side sha256 matches", rc == 0 and want in out, out)
    rc, out = cp("put", str(blob), f"{WORK}/blob3.bin")
    check(T, "upload over an existing file refused without --overwrite", rc != 0 and "already exists" in out, out)


async def t4_chrome(m: Mac):
    T = "T4"
    CH = "com.google.chrome"
    await m("press_key", keys="cmd+space", screenshot_after=False)
    await asyncio.sleep(1)
    await m("type_text", text="Google Chrome", press_enter=True, expect_app="com.apple.Spotlight", screenshot_after=False)
    app, *_ = await m.wait_front(CH)
    need(T, "Chrome frontmost", CH in app, app)
    await m("press_key", keys="cmd+n", expect_app=CH, screenshot_after=False)
    await asyncio.sleep(1.5)
    app, title, handle = await m.fg()
    need(T, "new Chrome window", CH in app and handle, f"{app} '{title}' {handle}")
    await m("press_key", keys="cmd+l", expect_app=CH, expect_handle=handle, screenshot_after=False)
    await m("type_text", text="https://example.com", press_enter=True, expect_app=CH, expect_handle=handle, screenshot_after=False)
    title = ""
    for _ in range(10):
        await asyncio.sleep(1)
        _, title, h = await m.fg()
        if "Example Domain" in title:
            break
    check(T, "page loaded (title 'Example Domain')", "Example Domain" in title, title)
    err, t = await m("scroll", x=640, y=400, direction="down", amount=5)
    check(T, "scroll inside the page", not err, t)
    err, t = await m("zoom", x1=300, y1=150, x2=980, y2=650)
    check(T, "zoom on page content", not err, t)
    await m("press_key", keys="cmd+w", expect_app=CH, expect_handle=handle, screenshot_after=False)
    await asyncio.sleep(1)
    _, wl = await m("list_windows")
    check(T, "only our window was closed", f"handle={handle} " not in wl, "")


async def t5_guards(m: Mac):
    T = "T5"
    ssh("open -a 'Keychain Access'")
    await asyncio.sleep(3)
    _, wl = await m("screenshot")
    _, wl = await m("list_windows")
    line = next((l for l in wl.splitlines() if "com.apple.keychainaccess" in l), "")
    g = re.search(r"screenshot coords ≈\((-?\d+),(-?\d+)\) (\d+)x(\d+)", line)
    if check(T, "Keychain Access window found", bool(g), line):
        x, y, w, h = map(int, g.groups())
        cx, cy = x + w // 2, y + h * 2 // 3
        for tool, args in (("click", {}), ("scroll", {"direction": "down", "amount": 1}), ("move_mouse", {})):
            err, t = await m(tool, x=cx, y=cy, screenshot_after=False, **args)
            check(T, f"block-list refuses {tool} (bypass mode)", err and "block-list" in t, t)
    ssh("pkill -x 'Keychain Access'")
    await asyncio.sleep(1)
    app, *_ = await m.fg()
    err, t = await m("type_text", text="should never appear", expect_app="com.apple.TextEdit", screenshot_after=False)
    check(T, "expect_app mismatch refuses typing", err and "nothing was sent" in t, f"fg={app}: {t}")
    err, t = await m("press_key", keys="hyper+x", screenshot_after=False)
    check(T, "unknown key rejected", err and "Unsupported key" in t, t)
    err, t = await m("click", x=99999, y=5, screenshot_after=False)
    check(T, "out-of-screenshot coordinates rejected", err and "outside" in t, t)
    err, t = await m("get_status")
    check(T, "not paused after all of this", '"paused": false' in t, "")


async def t6_robustness(url: str, token: str):
    T = "T6"
    async with mk_client(url, token) as c:
        m = Mac(c, None)
        await m("get_status")
        await asyncio.sleep(7)                           # longer than uvicorn's default 5 s keep-alive
        err, t = await m("get_status")
        check(T, "connection reused after 7 s idle", not err, t[:60])
    t0 = time.time()
    ssh("pkill -f 'ssh -N -R 127.0.0.1:8766'")
    back = False
    for _ in range(40):
        await asyncio.sleep(1)
        try:
            async with mk_client(url, token) as c:
                err, _ = await Mac(c, None)("get_status")
                back = not err
                if back:
                    break
        except Exception:      # noqa: BLE001  tunnel still down
            pass
    check(T, "tunnel killed, re-established, Agent reachable again", back, f"after {time.time() - t0:.0f} s")


def mk_client(url, token):
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=180, trust_env=False)
    return Client(streamable_http_client(url, http_client=http))


async def main(args):
    token = os.environ["MACPC_TOKEN"]
    scratch = Path(args.shots) if args.shots else None
    if scratch:
        scratch.mkdir(parents=True, exist_ok=True)
    ssh(f"rm -rf {WORK} && mkdir -p {WORK} && ln -s ~/.ssh/config {WORK}/link-to-ssh")
    async with mk_client(args.url, token) as c:
        m = Mac(c, scratch)
        _, st = await m("get_status")
        need("pre", "Agent in bypass mode", '"approval_mode": "all"' in st, "run ./mac-remote.sh bypass first")
        need("pre", "permissions granted", '"screen_recording": true' in st and '"accessibility": true' in st, "")
        for fn in (t1_textedit, t2_files):
            try:
                await fn(m)
            except Abort as e:
                print("aborted:", e)
    t3_transfer(token, scratch or Path("/tmp"))
    async with mk_client(args.url, token) as c:
        m = Mac(c, scratch)
        for fn in (t4_chrome, t5_guards):
            try:
                await fn(m)
            except Abort as e:
                print("aborted:", e)
    await t6_robustness(args.url, token)
    if not args.keep:
        ssh(f"rm -rf {WORK}")


def report() -> int:
    for test, name, ok, info in results:
        print(f"{'PASS' if ok else 'FAIL'}  {test}  {name:<58} {'' if ok else info}")
    failed = [r for r in results if not r[2]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8766/mcp")
    ap.add_argument("--keep", action="store_true", help="keep ~/macpc-test afterwards")
    ap.add_argument("--shots", help="save every returned image into this folder")
    a = ap.parse_args()
    try:
        asyncio.run(main(a))
    except BaseException as e:  # noqa: BLE001
        print("run stopped:", type(e).__name__, e)
    sys.exit(report())
