"""Live smoke test: calls all desktop tools of the real Mac Agent from the server through the tunnel.

Only operates a TextEdit document it created itself, never saves it, and quits TextEdit at the end ("Delete"/"Don't Save").
All mouse actions stay inside that document window. It aborts before typing anything if TextEdit is already running
with windows open (it would otherwise risk touching your documents).
Usage (on the Linux server):
    MACPC_TOKEN=... python live_test.py [url]
"""
import asyncio
import os
import re
import sys

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8766/mcp"
TOKEN = os.environ["MACPC_TOKEN"]
TE = "com.apple.textedit"
results: list[tuple[str, bool, str]] = []


class Abort(Exception):
    pass


def text_of(r) -> str:
    return " ".join(c.text for c in r.content if c.type == "text")


def has_image(r) -> bool:
    return any(c.type == "image" and c.mime_type == "image/jpeg" for c in r.content)


def foreground(r) -> str:
    m = re.search(r"Foreground app: (.*?), window '", text_of(r))
    return m.group(1).lower() if m else ""


async def step(name, coro, check=lambda r: True):
    try:
        r = await coro
        ok = (not r.is_error) and check(r)
        results.append((name, ok, text_of(r)[:110].replace("\n", " ")))
        return r
    except Exception as e:  # noqa: BLE001
        results.append((name, False, f"exception: {e!r}"[:110]))
        return None


def textedit_window(r):
    """The topmost TextEdit window from list_windows: (handle, screenshot x, y, w, h)."""
    for line in text_of(r).splitlines():
        if TE in line.lower():
            h = re.search(r"handle=(\d+)", line)
            g = re.search(r"screenshot coords ≈\((-?\d+),(-?\d+)\) (\d+)x(\d+)", line)
            if h and g:
                return (int(h.group(1)), *map(int, g.groups()))
    return None


def require(cond, msg):
    if not cond:
        results.append(("safety check", False, msg))
        raise Abort(msg)


async def main():
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120, trust_env=False)
    async with Client(streamable_http_client(URL, http_client=http)) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        want = {"screenshot", "zoom", "click", "move_mouse", "drag", "scroll", "type_text", "press_key",
                "wait", "list_windows", "focus_window", "get_status"}
        results.append(("list_tools", want <= tools, f"missing: {sorted(want - tools)}" if want - tools else "all 12 desktop tools present"))

        st = await step("get_status", c.call_tool("get_status", {}), lambda r: "agent_version" in text_of(r))
        require(st and '"screen_recording": true' in text_of(st) and '"accessibility": true' in text_of(st),
                "Screen Recording / Accessibility permission missing (see get_status); grant them and restart the Agent first.")
        pre = await step("list_windows (pre-check)", c.call_tool("list_windows", {}))
        require(pre is not None and TE not in text_of(pre).lower(),
                "TextEdit already has windows open. This test types into a new TextEdit document and quits TextEdit without saving; "
                "save and close your TextEdit documents first, then run it again.")
        await step("screenshot", c.call_tool("screenshot", {}), has_image)

        # Spotlight: cmd+space, type the app name (the Agent bypasses an active input method), Return
        await step("press_key cmd+space", c.call_tool("press_key", {"keys": "cmd+space", "screenshot_after": False}))
        await asyncio.sleep(1.0)
        s = await step("screenshot (Spotlight)", c.call_tool("screenshot", {}), has_image)
        require(s and "spotlight" in foreground(s), "Spotlight did not open; aborted: " + (foreground(s) if s else ""))
        await step("type_text 'TextEdit' + Return", c.call_tool("type_text", {"text": "TextEdit", "press_enter": True,
                                                                            "expect_app": "com.apple.Spotlight",
                                                                            "screenshot_after": False}))
        await asyncio.sleep(3)
        await step("press_key cmd+n (new document)", c.call_tool("press_key", {"keys": "cmd+n", "expect_app": TE,
                                                                              "screenshot_after": False}))
        await asyncio.sleep(1.5)
        s = await step("screenshot (TextEdit)", c.call_tool("screenshot", {}), has_image)
        require(s and TE in foreground(s), "TextEdit did not become the frontmost app; aborted: " + (foreground(s) if s else ""))

        await step("type_text Chinese + English + emoji (keys)", c.call_tool(
            "type_text", {"text": "MacPC live test \u4f60\u597d\U0001f642\n\u7b2c\u4e8c\u884c line 2\n",
                          "expect_app": TE, "screenshot_after": False}))
        await step("type_text multi-line (paste)", c.call_tool(
            "type_text", {"text": "pasted line A \u4e2d\u6587\npasted line B ~!@#$%^&*()_+{}|:\"<>?\n",
                          "method": "paste", "expect_app": TE, "screenshot_after": True}), has_image)
        await step("press_key cmd+a", c.call_tool("press_key", {"keys": "cmd+a", "expect_app": TE, "screenshot_after": False}))
        await step("press_key right (deselect)", c.call_tool("press_key", {"keys": "right", "expect_app": TE,
                                                                          "screenshot_after": False}))

        wl = await step("list_windows (find TextEdit)", c.call_tool("list_windows", {}))
        win = textedit_window(wl) if wl else None
        require(win, "No TextEdit window found in list_windows")
        handle, wx, wy, ww, wh = win
        cx, cy = wx + ww // 2, wy + wh // 2
        require(ww >= 40 and wh >= 40 and cx > 5 and cy > 5, f"The TextEdit window is too small or off screen: {win}")

        await step("zoom (document area)", c.call_tool("zoom", {"x1": wx, "y1": wy, "x2": wx + ww, "y2": wy + min(wh, 200)}), has_image)
        await step("move_mouse", c.call_tool("move_mouse", {"x": cx, "y": cy, "screenshot_after": False}))
        await step("click", c.call_tool("click", {"x": cx, "y": cy, "screenshot_after": False}))
        await step("scroll", c.call_tool("scroll", {"x": cx, "y": cy, "direction": "down", "amount": 3,
                                                    "screenshot_after": False}))
        await step("drag (select text)", c.call_tool("drag", {"x1": wx + 20, "y1": wy + 40, "x2": wx + ww // 2, "y2": wy + 40,
                                                        "screenshot_after": False}))
        await step("wait(1s)", c.call_tool("wait", {"seconds": 1}), has_image)
        await step("focus_window", c.call_tool("focus_window", {"handle": handle}))

        # Cleanup: quit TextEdit only if it is still frontmost; answer the save sheet with cmd+backspace ("Delete" / "Don't Save")
        s = await step("TextEdit still frontmost", c.call_tool("screenshot", {}), lambda r: TE in foreground(r))
        require(s and TE in foreground(s), "TextEdit is no longer frontmost; cmd+q not sent, please quit TextEdit manually")
        await step("press_key cmd+q", c.call_tool("press_key", {"keys": "cmd+q", "expect_app": TE, "screenshot_after": False}))
        await asyncio.sleep(1)
        s = await step("screenshot (save sheet)", c.call_tool("screenshot", {}), has_image)
        if s and TE in foreground(s):
            await step("press_key cmd+backspace (don't save)", c.call_tool(
                "press_key", {"keys": "cmd+backspace", "expect_app": TE, "screenshot_after": False}))
            await asyncio.sleep(1.5)
        await step("screenshot (wrap-up)", c.call_tool("screenshot", {}), lambda r: TE not in foreground(r))


def report():
    width = max(len(n) for n, *_ in results)
    for n, ok, info in results:
        print(f"{'PASS' if ok else 'FAIL'}  {n.ljust(width)}  {info}")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f", failed: {failed}" if failed else ""))
    return 1 if failed else 0


def find_abort(e: BaseException):
    """The MCP client's TaskGroup wraps Abort in an exception group, so search recursively."""
    if isinstance(e, Abort):
        return e
    for sub in getattr(e, "exceptions", ()):
        found = find_abort(sub)
        if found:
            return found
    return None


try:
    asyncio.run(main())
except BaseException as e:  # noqa: BLE001
    abort = find_abort(e)
    if abort is None:
        raise
    print(f"Aborted: {abort}")
sys.exit(report())
