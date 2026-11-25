"""Backend abstraction: MacBackend (real) / FakeBackend (for tests).

This module must be importable on Linux: every macOS-specific import (pyobjc) happens inside the
constructor/methods of MacBackend, and the platform check is in the constructor as well.

Coordinates: everything the backend takes and returns is in macOS global display coordinates, in
**points** (origin at the top-left of the main display, y grows downwards; the same space that
CGDisplayBounds and CGEvent use). On a Retina display one point is 2x2 pixels, so `grab()` returns an
image larger than the monitor's point size; the agent core handles that ratio.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import functools
import os
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass

from PIL import Image, ImageDraw


# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Monitor:
    left: int           # points, global display coordinates
    top: int
    width: int
    height: int
    display_id: int = 0  # CGDirectDisplayID (0 for the fake backend)


@dataclass
class WindowInfo:
    handle: int          # CGWindowID; 0 when the app has no (known) window, e.g. a click on the Dock or the menu bar
    title: str
    process: str         # canonical app id, lowercase: bundle id ("com.apple.textedit") or executable name; "" = unknown
    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0
    app_name: str = ""   # display name, e.g. "TextEdit" (may be localized)
    aliases: tuple[str, ...] = ()   # further lowercase names of the same app (localized name, bundle name, executable name)
    pid: int = 0

    def names(self) -> set[str]:
        """Every lowercase name the allow/block-lists and expect_app may use for this app."""
        return {n for n in (self.process, *self.aliases) if n}

    @property
    def label(self) -> str:
        """Human-readable: 'TextEdit (com.apple.textedit)'."""
        if self.app_name and self.app_name.lower() != self.process:
            return f"{self.app_name} ({self.process or '?'})"
        return self.process or self.app_name or "unknown app"


class FailSafeTriggered(Exception):
    """The user moved the mouse to the top-left corner of the main display (emergency stop)."""


class BackendError(Exception):
    """A backend error that can be shown directly to the model (e.g. a missing macOS permission)."""


# --------------------------------------------------------------------------- #
# Abstraction
# --------------------------------------------------------------------------- #
class Backend(ABC):
    name = "abstract"

    @abstractmethod
    def monitors(self) -> list[Monitor]:
        """All displays (order matches the tool parameter monitor=1..n; the main display first), in points."""

    @abstractmethod
    def grab(self, monitor: Monitor) -> Image.Image:
        """Capture the given display at full pixel resolution and return an RGB image (without the mouse pointer).
        On Retina displays the image is larger than monitor.width x monitor.height."""

    @abstractmethod
    def cursor_pos(self) -> tuple[int, int]: ...

    @abstractmethod
    def window_at_point(self, x: int, y: int) -> WindowInfo | None:
        """The app (and its window) that would receive a click at this point."""

    @abstractmethod
    def foreground_window(self) -> WindowInfo | None:
        """The frontmost app and its focused window (where key presses go)."""

    @abstractmethod
    def list_windows(self) -> list[WindowInfo]: ...

    @abstractmethod
    def focus_window(self, handle: int) -> bool: ...

    @abstractmethod
    def click(self, x: int, y: int, button: str, clicks: int, modifiers: list[str]) -> None: ...

    @abstractmethod
    def move_mouse(self, x: int, y: int) -> None: ...

    @abstractmethod
    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str) -> None: ...

    @abstractmethod
    def scroll(self, x: int, y: int, direction: str, amount: int) -> None: ...

    @abstractmethod
    def type_text(self, text: str) -> None: ...

    @abstractmethod
    def press_keys(self, keys: list[str], repeat: int) -> None:
        """Press the key combination `keys` (normalized names, e.g. ["cmd","shift","z"])."""

    @abstractmethod
    def confirm(self, title: str, message: str, timeout_s: float) -> bool:
        """Show a topmost allow/deny dialog on this machine; timeout or deny -> False."""

    def paste_text(self, text: str) -> str:
        """Paste text through the clipboard (one Cmd+V). Returns a note (may be empty)."""
        raise BackendError("This backend does not support paste input.")

    def permissions(self) -> dict:
        """macOS privacy permissions of this process: {"screen_recording": bool|None, "accessibility": bool|None}."""
        return {"screen_recording": None, "accessibility": None}

    def screen_locked(self) -> bool | None:
        """True when the login window / lock screen is up; None = unknown."""
        return None

    def host_app(self) -> str:
        """The app macOS holds responsible for this process (the one that needs the permissions), e.g. 'Terminal'."""
        return "unknown"


# --------------------------------------------------------------------------- #
# FakeBackend (for tests)
# --------------------------------------------------------------------------- #
_FAKE_APP_NAMES = {"com.apple.textedit": "TextEdit", "com.google.chrome": "Google Chrome",
                   "com.apple.terminal": "Terminal", "com.apple.finder": "Finder"}


class FakeBackend(Backend):
    """In-memory canvas that records all actions.

    actions: the action log, e.g. {"op": "click", "x": 1280, "y": 720, ...}
    foreground_process / point_process: app ids of the foreground window / the window under the coordinates
    confirm_result: the dialog return value; confirm_calls: the log of questions asked
    failsafe_armed: when True, the next input action raises FailSafeTriggered
    black: when True, screenshots are completely black (simulates a sleeping display)
    locked: what screen_locked() returns
    pixel_scale: 2 simulates a Retina display (grab() returns twice the point size)
    """

    name = "fake"

    def __init__(self, monitors: list[Monitor] | None = None, pixel_scale: int = 1) -> None:
        self._monitors = monitors or [Monitor(0, 0, 2560, 1440)]
        self.pixel_scale = pixel_scale
        self.actions: list[dict] = []
        self.foreground_process = "com.apple.textedit"
        self.foreground_title = "Untitled"
        self.point_process: str | None = None  # None -> same as the foreground
        self.windows: list[WindowInfo] = [
            self._mk(1001, "Untitled", "com.apple.textedit", 100, 100, 800, 600),
            self._mk(1002, "Google Chrome", "com.google.chrome", 200, 150, 1200, 800),
        ]
        self.confirm_result = True
        self.during_confirm = None        # optional callback: runs during confirm
        self.confirm_calls: list[tuple[str, str, float]] = []
        self.failsafe_armed = False
        self.black = False
        self.locked: bool | None = False
        self.perms = {"screen_recording": True, "accessibility": True}
        self._cursor = (10, 10)

    @staticmethod
    def _mk(handle, title, process, left=0, top=0, width=0, height=0) -> WindowInfo:
        process = process.lower()           # like MacBackend: the canonical id is always lowercase
        name = _FAKE_APP_NAMES.get(process, process.rsplit(".", 1)[-1])
        return WindowInfo(handle, title, process, left, top, width, height, app_name=name,
                          aliases=(name.lower(),))

    # -- queries --
    def monitors(self) -> list[Monitor]:
        return list(self._monitors)

    def grab(self, monitor: Monitor) -> Image.Image:
        w, h = monitor.width * self.pixel_scale, monitor.height * self.pixel_scale
        if self.black:
            return Image.new("RGB", (w, h), (0, 0, 0))
        idx = self._monitors.index(monitor)
        palette = [(40, 90, 160), (160, 90, 40), (60, 140, 80)]
        img = Image.new("RGB", (w, h), palette[idx % len(palette)])
        d = ImageDraw.Draw(img)
        d.rectangle([w // 4, h // 4, w * 3 // 4, h * 3 // 4], fill=(230, 230, 230))
        d.text((w // 4 + 20, h // 4 + 20), f"fake monitor {idx + 1}", fill=(0, 0, 0))
        return img

    def cursor_pos(self) -> tuple[int, int]:
        return self._cursor

    def _win(self, process: str | None, title: str) -> WindowInfo | None:
        if not process:
            return None
        return self._mk(sum(map(ord, process)), title, process)

    def window_at_point(self, x: int, y: int) -> WindowInfo | None:
        return self._win(self.point_process or self.foreground_process, self.foreground_title)

    def foreground_window(self) -> WindowInfo | None:
        return self._win(self.foreground_process, self.foreground_title)

    def list_windows(self) -> list[WindowInfo]:
        return list(self.windows)

    def permissions(self) -> dict:
        return dict(self.perms)

    def screen_locked(self) -> bool | None:
        return self.locked

    def host_app(self) -> str:
        return "FakeTerminal"

    # -- actions --
    def _act(self, **rec) -> None:
        if self.failsafe_armed:
            self.failsafe_armed = False
            raise FailSafeTriggered()
        self.actions.append(rec)
        if "x" in rec and "y" in rec:
            self._cursor = (rec["x"], rec["y"])

    def focus_window(self, handle: int) -> bool:
        for w in self.windows:
            if w.handle == handle:
                self.foreground_process, self.foreground_title = w.process, w.title
                self.actions.append({"op": "focus_window", "handle": handle})
                return True
        return False

    def click(self, x, y, button, clicks, modifiers):
        self._act(op="click", x=x, y=y, button=button, clicks=clicks, modifiers=list(modifiers))

    def move_mouse(self, x, y):
        self._act(op="move_mouse", x=x, y=y)

    def drag(self, x1, y1, x2, y2, button):
        self._act(op="drag", x1=x1, y1=y1, x2=x2, y2=y2, button=button)

    def scroll(self, x, y, direction, amount):
        self._act(op="scroll", x=x, y=y, direction=direction, amount=amount)

    def type_text(self, text):
        self._act(op="type_text", text=text)

    def press_keys(self, keys, repeat):
        self._act(op="press_keys", keys=list(keys), repeat=repeat)

    def paste_text(self, text):
        self._act(op="paste_text", text=text)
        return ""

    def confirm(self, title, message, timeout_s):
        self.confirm_calls.append((title, message, timeout_s))
        if self.during_confirm:
            self.during_confirm()      # simulate the dialog changing the foreground window / the window under the mouse
        return self.confirm_result


# --------------------------------------------------------------------------- #
# Key codes (macOS virtual key codes, ANSI layout) and modifier flags
# --------------------------------------------------------------------------- #
KEYCODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
    "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
    "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34,
    "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46,
    ".": 47, "`": 50,
    "enter": 36, "tab": 48, "space": 49, "backspace": 51, "escape": 53, "forwarddelete": 117,
    "cmd": 55, "shift": 56, "capslock": 57, "alt": 58, "ctrl": 59, "fn": 63,
    "home": 115, "pageup": 116, "end": 119, "pagedown": 121, "help": 114,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101,
    "f10": 109, "f11": 103, "f12": 111, "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64,
    "f18": 79, "f19": 80, "f20": 90,
}
# "delete" on a Mac keyboard is backspace; the forward delete key ("del" on PC keyboards) is forwarddelete.
KEYCODES["delete"] = KEYCODES["forwarddelete"]
# Characters that need Shift on an ANSI keyboard -> the unshifted key
SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9",
           ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "<": ",",
           ">": ".", "?": "/", "~": "`"}
FLAG = {"shift": 0x20000, "ctrl": 0x40000, "alt": 0x80000, "cmd": 0x100000, "fn": 0x800000}
MODIFIER_KEYS = set(FLAG)


def resolve_key(name: str) -> tuple[int, bool]:
    """Key name -> (virtual key code, needs shift). Raises BackendError for unknown keys."""
    if name in KEYCODES:
        return KEYCODES[name], False
    if len(name) == 1:
        if name in SHIFTED:
            return KEYCODES[SHIFTED[name]], True
        if name.isalpha() and name.lower() in KEYCODES:
            return KEYCODES[name.lower()], name.isupper()
    raise BackendError(f"Unsupported key: {name!r}")


def key_sequence(keys: list[str]) -> list[tuple[int, int]]:
    """Normalized key names -> [(key code, modifier flag bit or 0)] in press order (release is the reverse).
    A character that needs Shift gets a Shift press inserted before it (unless Shift is already held)."""
    seq: list[tuple[int, int]] = []
    held_shift = "shift" in keys
    for k in keys:
        code, need_shift = resolve_key(k)
        if need_shift and not held_shift:
            seq.append((KEYCODES["shift"], FLAG["shift"]))
            held_shift = True
        seq.append((code, FLAG.get(k, 0)))
    return seq


def utf16_units(ch: str) -> int:
    return len(ch.encode("utf-16-le")) // 2


_MOUSE_EVENTS = {   # button -> (down, up, dragged, CGMouseButton)
    "left": (1, 2, 6, 0),
    "right": (3, 4, 7, 1),
    "middle": (25, 26, 27, 2),
}
_EV_MOUSE_MOVED = 5
_RETURN, _TAB = 36, 48
_DIALOG_YES, _DIALOG_NO = "MACPC_CONFIRM:YES", "MACPC_CONFIRM:NO"


def _with_pool(fn):
    """Run a backend method inside an autorelease pool: the methods are called from worker threads,
    which have no pool of their own, so Cocoa objects created there would otherwise pile up."""
    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        with self._objc.autorelease_pool():
            return fn(self, *a, **k)
    return wrapper


# --------------------------------------------------------------------------- #
# MacBackend (real)
# --------------------------------------------------------------------------- #
class MacBackend(Backend):
    name = "macos"

    def __init__(self, type_chunk_chars: int = 1, type_delay_ms: float = 5,
                 type_newline_delay_ms: float = 60, paste_restore_delay_ms: float = 600,
                 switch_input_source: bool = True) -> None:
        self.type_chunk_chars = max(1, int(type_chunk_chars))
        self.type_delay_s = max(0.0, float(type_delay_ms)) / 1000
        self.type_newline_delay_s = max(0.0, float(type_newline_delay_ms)) / 1000
        self.paste_restore_delay_s = max(0.0, float(paste_restore_delay_ms)) / 1000
        self.switch_input_source = bool(switch_input_source)
        if sys.platform != "darwin":
            raise RuntimeError("MacBackend can only be used on macOS (use --backend fake for testing)")
        import AppKit
        import ApplicationServices as AS
        import objc
        import Quartz
        self.Q, self.AS, self.AppKit, self._objc = Quartz, AS, AppKit, objc
        self._app_cache: dict[int, tuple[float, tuple[str, str, tuple[str, ...]]]] = {}
        self._ax_get_window = self._load_ax_get_window()
        self._tis = _InputSources()
        self._host_app = _find_host_app()
        self._ax_checked = (-1e9, False)

    # -- permissions / session --
    def permissions(self) -> dict:
        sr = None
        try:
            sr = bool(self.Q.CGPreflightScreenCaptureAccess())
        except Exception:      # noqa: BLE001  (macOS < 10.15)
            pass
        return {"screen_recording": sr, "accessibility": bool(self.AS.AXIsProcessTrusted())}

    def request_permissions(self) -> None:
        """Ask macOS to show its permission prompts (only shown when not granted yet; a no-op otherwise)."""
        try:
            if not self.Q.CGPreflightScreenCaptureAccess():
                self.Q.CGRequestScreenCaptureAccess()
        except Exception:      # noqa: BLE001
            pass
        try:
            self.AS.AXIsProcessTrustedWithOptions({self.AS.kAXTrustedCheckOptionPrompt: True})
        except Exception:      # noqa: BLE001
            pass

    def screen_locked(self) -> bool | None:
        d = self.Q.CGSessionCopyCurrentDictionary()
        if d is None:
            return None
        return bool(d.get("CGSSessionScreenIsLocked", False)) or not d.get("kCGSSessionOnConsoleKey", True)

    def host_app(self) -> str:
        return self._host_app

    def _need_accessibility(self) -> None:
        if not self.AS.AXIsProcessTrusted():
            raise BackendError(
                f"macOS Accessibility permission is missing, so mouse/keyboard events would be silently dropped. "
                f"Ask the user to enable '{self._host_app}' under System Settings > Privacy & Security > Accessibility "
                "and then restart the Agent (on macOS 12 a running process does not see the change).")

    # -- screenshots --
    @_with_pool
    def monitors(self) -> list[Monitor]:
        Q = self.Q
        err, ids, n = Q.CGGetActiveDisplayList(32, None, None)
        if err:
            raise BackendError(f"CGGetActiveDisplayList failed ({err})")
        main = Q.CGMainDisplayID()
        mons = []
        for d in sorted(ids[:n], key=lambda d: d != main):
            if Q.CGDisplayMirrorsDisplay(d):      # mirrored copies show the same content
                continue
            b = Q.CGDisplayBounds(d)
            mons.append(Monitor(int(b.origin.x), int(b.origin.y), int(b.size.width), int(b.size.height), int(d)))
        return mons

    @_with_pool
    def grab(self, monitor: Monitor) -> Image.Image:
        Q = self.Q
        img = Q.CGDisplayCreateImage(monitor.display_id)
        if img is None:
            raise BackendError("Screen capture failed (the display may be asleep or disconnected).")
        w, h = Q.CGImageGetWidth(img), Q.CGImageGetHeight(img)
        bpr = Q.CGImageGetBytesPerRow(img)
        info = Q.CGImageGetBitmapInfo(img)
        alpha, order = info & 0x1F, info & 0x7000
        if Q.CGImageGetBitsPerPixel(img) == 32 and Q.CGImageGetBitsPerComponent(img) == 8:
            first = alpha in (2, 4, 6)          # PremultipliedFirst / First / NoneSkipFirst
            little = order == 0x2000            # kCGBitmapByteOrder32Little
            raw_mode = {(True, True): "BGRX", (True, False): "XRGB",
                        (False, True): "XBGR", (False, False): "RGBX"}[(first, little)]
            data = Q.CGDataProviderCopyData(Q.CGImageGetDataProvider(img))
            return Image.frombuffer("RGB", (w, h), bytes(data), "raw", raw_mode, bpr, 1).copy()
        # Other formats (e.g. 10-bit or HDR displays): draw into a known 8-bit BGRX context first
        space = Q.CGColorSpaceCreateWithName(Q.kCGColorSpaceSRGB)
        ctx = Q.CGBitmapContextCreate(None, w, h, 8, w * 4, space, 6 | 0x2000)   # NoneSkipFirst | 32Little
        Q.CGContextDrawImage(ctx, Q.CGRectMake(0, 0, w, h), img)
        out = Q.CGBitmapContextCreateImage(ctx)
        data = Q.CGDataProviderCopyData(Q.CGImageGetDataProvider(out))
        return Image.frombuffer("RGB", (w, h), bytes(data), "raw", "BGRX", Q.CGImageGetBytesPerRow(out), 1).copy()

    @_with_pool
    def cursor_pos(self) -> tuple[int, int]:
        loc = self.Q.CGEventGetLocation(self.Q.CGEventCreate(None))
        return int(round(loc.x)), int(round(loc.y))

    # -- apps / windows --
    def _app_identity(self, pid: int, owner_name: str = "") -> tuple[str, str, tuple[str, ...]]:
        """pid -> (canonical lowercase id, display name, aliases). Cached briefly (pids are rarely reused)."""
        now = time.monotonic()
        hit = self._app_cache.get(pid)
        if hit and now - hit[0] < 30:
            return hit[1]
        app = self.AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if pid > 0 else None
        bid = name = exe = bundle = ""
        if app is not None:
            bid = str(app.bundleIdentifier() or "")
            name = str(app.localizedName() or "")
            if app.executableURL() is not None:
                exe = str(app.executableURL().lastPathComponent() or "")
            if app.bundleURL() is not None:
                bundle = str(app.bundleURL().lastPathComponent() or "")
                if bundle.lower().endswith(".app"):
                    bundle = bundle[:-4]
        name = name or owner_name or exe
        canonical = (bid or exe or name).lower()
        aliases = tuple(dict.fromkeys(a.lower() for a in (name, bundle, exe, owner_name) if a and a.lower() != canonical))
        ident = (canonical, name, aliases)
        self._app_cache[pid] = (now, ident)
        return ident

    def _cg_list(self, option=None, relative_to=0) -> list:
        Q = self.Q
        if option is None:
            option = Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements
        return list(Q.CGWindowListCopyWindowInfo(option, relative_to) or [])

    def _cg_window(self, wid: int):
        if not wid:
            return None
        lst = self._cg_list(self.Q.kCGWindowListOptionIncludingWindow, wid)
        return lst[0] if lst else None

    def _info_from_cg(self, w) -> WindowInfo:
        pid = int(w.get("kCGWindowOwnerPID", 0))
        canonical, name, aliases = self._app_identity(pid, str(w.get("kCGWindowOwnerName") or ""))
        b = w.get("kCGWindowBounds") or {}
        return WindowInfo(int(w.get("kCGWindowNumber", 0)), str(w.get("kCGWindowName") or ""), canonical,
                          int(b.get("X", 0)), int(b.get("Y", 0)), int(b.get("Width", 0)), int(b.get("Height", 0)),
                          app_name=name, aliases=aliases, pid=pid)

    def _info(self, pid: int, wid: int, title_hint: str = "") -> WindowInfo:
        w = self._cg_window(wid)
        if w is not None and int(w.get("kCGWindowOwnerPID", -1)) == pid:
            info = self._info_from_cg(w)
            if not info.title and title_hint:
                info.title = title_hint
            return info
        canonical, name, aliases = self._app_identity(pid)
        return WindowInfo(0, title_hint, canonical, app_name=name, aliases=aliases, pid=pid)

    # Accessibility helpers (all return None/0 on any failure; AX needs the Accessibility permission)
    def _load_ax_get_window(self):
        """The private but long-standing _AXUIElementGetWindow maps an AX window element to its CGWindowID."""
        try:
            fns: dict = {}
            bundle = self.AppKit.NSBundle.bundleWithPath_(
                "/System/Library/Frameworks/ApplicationServices.framework/Frameworks/HIServices.framework")
            self._objc.loadBundleFunctions(bundle, fns, [("_AXUIElementGetWindow", b"i^{__AXUIElement=}o^I")])
            return fns.get("_AXUIElementGetWindow")
        except Exception:      # noqa: BLE001
            return None

    def _ax_ok(self) -> bool:
        """Only call AX APIs when trusted: an untrusted process that calls them makes macOS pop up its own
        "... would like to control this computer" prompt, naming whatever binary happens to run."""
        now = time.monotonic()
        if now - self._ax_checked[0] > 2:
            self._ax_checked = (now, bool(self.AS.AXIsProcessTrusted()))
        return self._ax_checked[1]

    def _ax_attr(self, el, attr):
        if el is None or not self._ax_ok():
            return None
        try:
            err, val = self.AS.AXUIElementCopyAttributeValue(el, attr, None)
        except Exception:      # noqa: BLE001
            return None
        return val if err == 0 else None

    def _ax_pid(self, el) -> int:
        try:
            err, pid = self.AS.AXUIElementGetPid(el, None)
            return int(pid) if err == 0 else 0
        except Exception:      # noqa: BLE001
            return 0

    def _ax_window_id(self, axwin) -> int:
        if axwin is None or self._ax_get_window is None:
            return 0
        try:
            err, wid = self._ax_get_window(axwin, None)
            return int(wid) if err == 0 else 0
        except Exception:      # noqa: BLE001
            return 0

    def _system_wide(self):
        sw = self.AS.AXUIElementCreateSystemWide()
        try:
            self.AS.AXUIElementSetMessagingTimeout(sw, 1.0)   # a hung app must not hang the Agent
        except Exception:      # noqa: BLE001
            pass
        return sw

    def _front_pid(self) -> int:
        app = self._ax_attr(self._system_wide(), self.AS.kAXFocusedApplicationAttribute) if self._ax_ok() else None
        pid = self._ax_pid(app) if app is not None else 0
        if pid:
            return pid
        try:    # without Accessibility: ask LaunchServices (NSWorkspace can be stale in a process without a run loop)
            out = subprocess.run(["/bin/sh", "-c", "lsappinfo info -only pid $(lsappinfo front)"],
                                 capture_output=True, text=True, timeout=3).stdout
            return int(out.strip().rsplit("=", 1)[-1])
        except Exception:      # noqa: BLE001
            fa = self.AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            return int(fa.processIdentifier()) if fa is not None else 0

    @_with_pool
    def foreground_window(self) -> WindowInfo | None:
        pid = self._front_pid()
        if not pid:
            return None
        if not self._ax_ok():
            return self._info(pid, self._front_cg_window(pid))
        axapp = self.AS.AXUIElementCreateApplication(pid)
        try:
            self.AS.AXUIElementSetMessagingTimeout(axapp, 1.0)
        except Exception:      # noqa: BLE001
            pass
        axwin = self._ax_attr(axapp, self.AS.kAXFocusedWindowAttribute)
        wid = self._ax_window_id(axwin)
        title = str(self._ax_attr(axwin, self.AS.kAXTitleAttribute) or "") if axwin is not None else ""
        if not wid and axwin is None:
            wid = self._front_cg_window(pid)
        return self._info(pid, wid, title)

    def _front_cg_window(self, pid: int) -> int:
        """The app's frontmost normal window (used when AX cannot tell the focused one)."""
        for w in self._cg_list():
            if int(w.get("kCGWindowOwnerPID", -1)) == pid and w.get("kCGWindowLayer") == 0:
                return int(w["kCGWindowNumber"])
        return 0

    @_with_pool
    def window_at_point(self, x: int, y: int) -> WindowInfo | None:
        if not self._ax_ok():
            return self._window_at_point_cg(x, y)
        try:
            err, el = self.AS.AXUIElementCopyElementAtPosition(self._system_wide(), float(x), float(y), None)
        except Exception:      # noqa: BLE001
            err, el = -1, None
        if err == 0 and el is not None:
            pid = self._ax_pid(el)
            if pid:
                axwin = self._ax_attr(el, self.AS.kAXWindowAttribute)
                return self._info(pid, self._ax_window_id(axwin))
        return self._window_at_point_cg(x, y)

    def _window_at_point_cg(self, x: int, y: int) -> WindowInfo | None:
        """Fallback without Accessibility: the topmost on-screen window that contains the point."""
        mons = [(m.left, m.top, m.width, m.height) for m in self.monitors()]
        for w in self._cg_list():
            b = w.get("kCGWindowBounds") or {}
            bx, by, bw, bh = b.get("X", 0), b.get("Y", 0), b.get("Width", 0), b.get("Height", 0)
            if not (bx <= x < bx + bw and by <= y < by + bh) or not w.get("kCGWindowAlpha", 1):
                continue
            owner = w.get("kCGWindowOwnerName") or ""
            if owner == "Window Server":
                if w.get("kCGWindowLayer") == 24:          # the menu bar: menus belong to the frontmost app
                    return self.foreground_window()
                continue
            if w.get("kCGWindowLayer") == 20 and (int(bx), int(by), int(bw), int(bh)) in mons:
                continue                                   # the Dock's transparent full-screen window
            return self._info_from_cg(w)
        return None

    @_with_pool
    def list_windows(self) -> list[WindowInfo]:
        out = []
        for w in self._cg_list():
            if w.get("kCGWindowLayer") != 0 or not w.get("kCGWindowAlpha", 1):
                continue
            b = w.get("kCGWindowBounds") or {}
            if b.get("Width", 0) < 40 or b.get("Height", 0) < 40:
                continue
            info = self._info_from_cg(w)
            if info.pid == os.getpid():
                continue
            out.append(info)
        return out

    @_with_pool
    def focus_window(self, handle: int) -> bool:
        AS = self.AS
        w = self._cg_window(handle)
        if w is None:
            return False
        pid = int(w.get("kCGWindowOwnerPID", 0))
        app = self.AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None:
            return False
        self._need_accessibility()
        axapp = AS.AXUIElementCreateApplication(pid)
        for axw in self._ax_attr(axapp, AS.kAXWindowsAttribute) or []:
            if self._ax_window_id(axw) == handle:
                if self._ax_attr(axw, AS.kAXMinimizedAttribute):
                    AS.AXUIElementSetAttributeValue(axw, AS.kAXMinimizedAttribute, False)
                AS.AXUIElementPerformAction(axw, AS.kAXRaiseAction)
                AS.AXUIElementSetAttributeValue(axw, AS.kAXMainAttribute, True)
                break
        app.activateWithOptions_(self.AppKit.NSApplicationActivateIgnoringOtherApps)
        for _ in range(10):
            time.sleep(0.1)
            fg = self.foreground_window()
            if fg is not None and fg.handle == handle:
                return True
        return False

    # -- input --
    def _failsafe_check(self) -> None:
        """Mouse in the top-left corner of the main display -> emergency stop (same semantics as pyautogui FAILSAFE)."""
        loc = self.Q.CGEventGetLocation(self.Q.CGEventCreate(None))
        if 0 <= loc.x < 1 and 0 <= loc.y < 1:
            raise FailSafeTriggered()

    def _post(self, ev) -> None:
        self.Q.CGEventPost(self.Q.kCGHIDEventTap, ev)

    def _mouse(self, typ: int, x: float, y: float, button: int, click_state: int = 1, flags: int = 0) -> None:
        Q = self.Q
        ev = Q.CGEventCreateMouseEvent(None, typ, Q.CGPointMake(x, y), button)
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, click_state)
        Q.CGEventSetFlags(ev, flags)
        self._post(ev)

    def _move(self, x: int, y: int) -> None:
        if (x, y) == (0, 0):
            x, y = 1, 1       # never park the pointer on the emergency-stop corner ourselves
        self._mouse(_EV_MOUSE_MOVED, x, y, 0)
        time.sleep(0.03)

    @_with_pool
    def click(self, x, y, button, clicks, modifiers):
        self._need_accessibility()
        self._failsafe_check()
        down, up, _drag, num = _MOUSE_EVENTS[button]
        flags = 0
        for m in modifiers:
            flags |= FLAG[m]
        self._move(x, y)
        for i in range(1, clicks + 1):         # click state 1, 2, 3 -> single/double/triple click
            self._mouse(down, x, y, num, i, flags)
            time.sleep(0.02)
            self._mouse(up, x, y, num, i, flags)
            time.sleep(0.05)

    @_with_pool
    def move_mouse(self, x, y):
        self._need_accessibility()
        self._failsafe_check()
        self._move(x, y)

    @_with_pool
    def drag(self, x1, y1, x2, y2, button):
        self._need_accessibility()
        self._failsafe_check()
        down, up, dragged, num = _MOUSE_EVENTS[button]
        self._move(x1, y1)
        self._mouse(down, x1, y1, num)
        last = (x1, y1)
        try:
            time.sleep(0.05)
            steps = 20
            for i in range(1, steps + 1):
                last = (x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
                self._mouse(dragged, *last, num)
                time.sleep(0.015)
            time.sleep(0.1)
        finally:
            self._mouse(up, *last, num)

    @_with_pool
    def scroll(self, x, y, direction, amount):
        self._need_accessibility()
        self._failsafe_check()
        Q = self.Q
        self._move(x, y)
        lines = 3                 # one "notch" = 3 lines, like a Windows wheel notch
        dy = lines if direction == "up" else -lines if direction == "down" else 0
        dx = lines if direction == "left" else -lines if direction == "right" else 0
        for _ in range(amount):
            ev = Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitLine, 2, dy, dx)
            self._post(ev)
            time.sleep(0.02)

    def _key(self, code: int, down: bool, flags: int) -> None:
        ev = self.Q.CGEventCreateKeyboardEvent(None, code, down)
        self.Q.CGEventSetFlags(ev, flags)
        self._post(ev)

    def _unicode(self, ch: str) -> None:
        Q = self.Q
        for down in (True, False):
            ev = Q.CGEventCreateKeyboardEvent(None, 0, down)
            Q.CGEventSetFlags(ev, 0)
            Q.CGEventKeyboardSetUnicodeString(ev, utf16_units(ch), ch)
            self._post(ev)

    @contextmanager
    def _ascii_input(self):
        """Switch to an ASCII keyboard layout (e.g. ABC) while sending, so that an input method such as Pinyin
        does not intercept the synthetic key events; the original input source is restored afterwards."""
        token = None
        if self.switch_input_source:
            try:
                token = self._tis.select_ascii()
                if token is not None:
                    time.sleep(0.05)
            except Exception:      # noqa: BLE001  best effort
                token = None
        try:
            yield
        finally:
            if token is not None:
                time.sleep(0.05)
                try:
                    self._tis.restore(token)
                except Exception:      # noqa: BLE001
                    pass

    @_with_pool
    def type_text(self, text: str) -> None:
        self._need_accessibility()
        self._failsafe_check()
        chars = list(text.replace("\r\n", "\n").replace("\r", "\n"))
        with self._ascii_input():
            for i in range(0, len(chars), self.type_chunk_chars):
                chunk = chars[i:i + self.type_chunk_chars]
                for ch in chunk:
                    if ch == "\n":
                        self._key(_RETURN, True, 0)
                        self._key(_RETURN, False, 0)
                    elif ch == "\t":
                        self._key(_TAB, True, 0)
                        self._key(_TAB, False, 0)
                    else:
                        self._unicode(ch)
                pause = self.type_delay_s
                if "\n" in chunk:
                    pause = max(pause, self.type_newline_delay_s)
                if pause:
                    time.sleep(pause)
                if i % 20 == 0:
                    self._failsafe_check()     # the emergency stop also works in the middle of long text

    @_with_pool
    def press_keys(self, keys, repeat):
        self._need_accessibility()
        self._failsafe_check()
        seq = key_sequence(list(keys))
        # Plain character keys (no Cmd/Ctrl/Alt) would be swallowed by an active input method: switch to ASCII for them.
        plain = not any(k in ("cmd", "ctrl", "alt") for k in keys) and any(
            len(k) == 1 or k == "space" for k in keys)
        with (self._ascii_input() if plain else _nullcontext()):
            for _ in range(repeat):
                flags = 0
                for code, bit in seq:
                    flags |= bit
                    self._key(code, True, flags)
                    time.sleep(0.01)
                for code, bit in reversed(seq):
                    flags &= ~bit
                    self._key(code, False, flags)
                    time.sleep(0.01)
                time.sleep(0.03)

    # -- clipboard paste --
    @_with_pool
    def paste_text(self, text: str) -> str:
        """Back up the whole pasteboard (every item, every type) -> write the text -> Cmd+V -> wait -> restore."""
        self._need_accessibility()
        self._failsafe_check()
        if not text:
            return ""
        AK = self.AppKit
        pb = AK.NSPasteboard.generalPasteboard()
        saved: list[list[tuple[str, object]]] = []
        for item in pb.pasteboardItems() or []:
            entries = []
            for t in item.types() or []:
                data = item.dataForType_(t)
                if data is not None:
                    entries.append((t, data))
            saved.append(entries)
        pb.clearContents()
        item = AK.NSPasteboardItem.alloc().init()
        item.setString_forType_(text, AK.NSPasteboardTypeString)
        # Ask clipboard managers not to record this text (http://nspasteboard.org)
        item.setString_forType_("", "org.nspasteboard.TransientType")
        item.setString_forType_("", "org.nspasteboard.AutoGeneratedType")
        if not pb.writeObjects_([item]):
            raise BackendError("Failed to write to the clipboard.")
        ours = pb.changeCount()
        try:
            self._key(KEYCODES["cmd"], True, FLAG["cmd"])
            self._key(KEYCODES["v"], True, FLAG["cmd"])
            self._key(KEYCODES["v"], False, FLAG["cmd"])
            self._key(KEYCODES["cmd"], False, 0)
            time.sleep(self.paste_restore_delay_s)
        finally:
            if pb.changeCount() == ours:          # restore only if nobody else changed the clipboard meanwhile
                pb.clearContents()
                items = []
                for entries in saved:
                    it = AK.NSPasteboardItem.alloc().init()
                    for t, data in entries:
                        it.setData_forType_(data, t)
                    items.append(it)
                if items:
                    pb.writeObjects_(items)
        return ""

    # -- dialog --
    dialog_slack_s = 15.0       # extra grace the subprocess gets beyond the countdown (slow start, busy system)

    def _confirm_panel(self, title: str, message: str, timeout_s: float) -> bool | None:
        """Show the approval dialog in a **separate subprocess** (a non-activating floating panel at the top of the screen:
        it does not take focus away from the target app). Returns None = subprocess unavailable; True/False = the choice."""
        import json
        payload = json.dumps({"title": title, "message": message, "timeout_s": timeout_s}, ensure_ascii=False)
        try:
            r = subprocess.run(self._dialog_command(), input=payload, capture_output=True, text=True,
                               encoding="utf-8", timeout=timeout_s + self.dialog_slack_s)
        except subprocess.TimeoutExpired:
            return False
        except Exception:      # noqa: BLE001  the subprocess could not be started
            return None
        for line in reversed((r.stdout or "").splitlines()):
            if line.strip() in (_DIALOG_YES, _DIALOG_NO):
                return line.strip() == _DIALOG_YES
        return None

    @staticmethod
    def _dialog_command() -> list[str]:
        return [sys.executable, os.path.abspath(__file__), "--confirm-dialog"]

    def confirm(self, title: str, message: str, timeout_s: float) -> bool:
        try:
            answer = self._confirm_panel(title, message, timeout_s)
            if answer is not None:
                return answer
        except Exception:      # noqa: BLE001
            pass
        return confirm_osascript(title, message, timeout_s)


class _nullcontext:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def confirm_osascript(title: str, message: str, timeout_s: float) -> bool:
    """Fallback dialog: AppleScript `display dialog` (takes focus while it is shown). Any failure counts as denial."""
    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    script = (f"display dialog {q(message)} with title {q(title)} buttons {{\"Deny\", \"Allow\"}} "
              f"default button \"Deny\" cancel button \"Deny\" with icon caution giving up after {int(timeout_s)}")
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout_s + 15)
    except Exception:      # noqa: BLE001
        return False
    return r.returncode == 0 and "button returned:Allow" in r.stdout and "gave up:true" not in r.stdout


def _find_host_app() -> str:
    """Walk up the parent processes to the first one inside an .app bundle (macOS attributes the Screen Recording /
    Accessibility permissions of a command-line process to that app, e.g. Terminal or iTerm)."""
    pid = os.getpid()
    for _ in range(12):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=3).stdout.strip()
        except Exception:      # noqa: BLE001
            break
        if not out:
            break
        ppid_s, _, comm = out.partition(" ")
        comm = comm.strip()
        if ".app/" in comm:
            return os.path.basename(comm.split(".app/")[0])
        try:
            pid = int(ppid_s)
        except ValueError:
            break
        if pid <= 1:
            break
    return os.path.basename(os.path.realpath(sys.executable))


# --------------------------------------------------------------------------- #
# Input sources (Carbon Text Input Sources via ctypes; pyobjc has no wrapper)
# --------------------------------------------------------------------------- #
class _InputSources:
    def __init__(self) -> None:
        self.ok = False
        try:
            carbon = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
            cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
            vp = ctypes.c_void_p
            for fn in ("TISCopyCurrentKeyboardInputSource", "TISCopyCurrentASCIICapableKeyboardInputSource"):
                getattr(carbon, fn).restype, getattr(carbon, fn).argtypes = vp, []
            carbon.TISSelectInputSource.restype, carbon.TISSelectInputSource.argtypes = ctypes.c_int32, [vp]
            carbon.TISGetInputSourceProperty.restype, carbon.TISGetInputSourceProperty.argtypes = vp, [vp, vp]
            cf.CFRelease.argtypes, cf.CFRelease.restype = [vp], None
            cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
            cf.CFStringGetCString.restype = ctypes.c_bool
            self.carbon, self.cf = carbon, cf
            self.k_id = vp.in_dll(carbon, "kTISPropertyInputSourceID")
            self.ok = True
        except Exception:      # noqa: BLE001
            pass

    def _id(self, src) -> str:
        ref = self.carbon.TISGetInputSourceProperty(src, self.k_id)
        buf = ctypes.create_string_buffer(512)
        if ref and self.cf.CFStringGetCString(ref, buf, 512, 0x08000100):    # kCFStringEncodingUTF8
            return buf.value.decode("utf-8")
        return ""

    def select_ascii(self):
        """Select the ASCII-capable layout if another source is active. Returns a restore token or None."""
        if not self.ok:
            return None
        cur = self.carbon.TISCopyCurrentKeyboardInputSource()
        asc = self.carbon.TISCopyCurrentASCIICapableKeyboardInputSource()
        try:
            if not cur or not asc or self._id(cur) == self._id(asc):
                if cur:
                    self.cf.CFRelease(cur)
                return None
            self.carbon.TISSelectInputSource(asc)
            return cur              # keep the reference until restore()
        finally:
            if asc:
                self.cf.CFRelease(asc)

    def restore(self, token) -> None:
        try:
            self.carbon.TISSelectInputSource(token)
        finally:
            self.cf.CFRelease(token)

    def current_id(self) -> str:
        if not self.ok:
            return ""
        cur = self.carbon.TISCopyCurrentKeyboardInputSource()
        try:
            return self._id(cur) if cur else ""
        finally:
            if cur:
                self.cf.CFRelease(cur)


# --------------------------------------------------------------------------- #
# Approval dialog (runs in a separate subprocess: python backends.py --confirm-dialog, parameters read as JSON from stdin)
# A non-activating NSPanel: it floats above everything (also over full-screen apps and on every Space), and clicking its
# buttons does not activate this process, so the app the model is about to operate keeps the focus.
# --------------------------------------------------------------------------- #
def run_confirm_dialog(title: str, message: str, timeout_s: float) -> bool:
    import AppKit
    import objc
    from Foundation import NSObject

    result = {"yes": False}
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)    # no Dock icon, no menu bar

    class Handler(NSObject):
        def allow_(self, _sender):
            result["yes"] = True
            self.finish()

        def deny_(self, _sender):
            self.finish()

        def tick_(self, _timer):
            remain = int(self.deadline - time.time() + 0.999)
            if remain <= 0:
                self.finish()
                return
            self.countdown.setStringValue_(f"No choice within {remain} s counts as denial")

        @objc.python_method
        def finish(self):
            app.stop_(None)
            # stop: only takes effect after the next event; post a dummy one so that run() returns now
            ev = AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                AppKit.NSEventTypeApplicationDefined, (0, 0), 0, 0, 0, None, 0, 0, 0)
            app.postEvent_atStart_(ev, True)

    handler = Handler.alloc().init()
    handler.deadline = time.time() + timeout_s

    width, pad = 560, 18
    style = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskNonactivatingPanel | AppKit.NSWindowStyleMaskUtilityWindow
    panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        ((0, 0), (width, 200)), style, AppKit.NSBackingStoreBuffered, False)
    panel.setTitle_("⚠ " + title)
    panel.setLevel_(AppKit.NSScreenSaverWindowLevel)          # above menus, Spotlight and full-screen apps
    panel.setFloatingPanel_(True)
    panel.setHidesOnDeactivate_(False)
    panel.setBecomesKeyOnlyIfNeeded_(True)
    panel.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                 | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                                 | AppKit.NSWindowCollectionBehaviorStationary)
    content = panel.contentView()

    label = AppKit.NSTextField.wrappingLabelWithString_(message)
    label.setFont_(AppKit.NSFont.systemFontOfSize_(13))
    label.setPreferredMaxLayoutWidth_(width - 2 * pad)
    text_h = label.fittingSize().height
    countdown = AppKit.NSTextField.labelWithString_("")
    countdown.setFont_(AppKit.NSFont.systemFontOfSize_(11))
    countdown.setTextColor_(AppKit.NSColor.secondaryLabelColor())
    handler.countdown = countdown

    btn_h, cd_h = 32, 18
    total_h = pad + btn_h + 10 + cd_h + 10 + text_h + pad
    panel.setContentSize_((width, total_h))
    label.setFrame_(((pad, pad + btn_h + 10 + cd_h + 10), (width - 2 * pad, text_h)))
    countdown.setFrame_(((pad, pad + btn_h + 10), (width - 2 * pad, cd_h)))
    for lbl, sel, x, key in (("Deny", "deny:", width - pad - 2 * 130 - 12, "\x1b"), ("Allow", "allow:", width - pad - 130, "")):
        b = AppKit.NSButton.buttonWithTitle_target_action_(lbl, handler, sel)
        b.setFrame_(((x, pad), (130, btn_h)))
        if key:
            b.setKeyEquivalent_(key)
        content.addSubview_(b)
    content.addSubview_(label)
    content.addSubview_(countdown)

    screen = AppKit.NSScreen.mainScreen() or AppKit.NSScreen.screens()[0]
    vf = screen.visibleFrame()
    frame = panel.frame()
    panel.setFrameOrigin_((vf.origin.x + (vf.size.width - frame.size.width) / 2,
                           vf.origin.y + vf.size.height - frame.size.height - 40))    # centered at the top of the screen
    handler.tick_(None)
    timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.25, handler, "tick:", None, True)
    panel.orderFrontRegardless()
    AppKit.NSBeep()
    try:
        app.run()
    finally:
        timer.invalidate()
        panel.orderOut_(None)
    return result["yes"]


def _dialog_main() -> int:
    import json
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:       # noqa: BLE001
        pass
    args = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    yes = run_confirm_dialog(args["title"], args["message"], float(args["timeout_s"]))
    print(_DIALOG_YES if yes else _DIALOG_NO, flush=True)
    return 0


if __name__ == "__main__":
    if "--confirm-dialog" in sys.argv:
        sys.exit(_dialog_main())
