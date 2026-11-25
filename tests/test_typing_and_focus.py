"""Input reliability and focus safety, carried over from the lessons of the Windows version, plus MacBackend internals.

1) After an approval dialog the target window is re-checked; input never goes to a window that was not approved.
2) expect_app / expect_handle refuse to send when the frontmost app/window is not the declared one.
3) MacBackend event generation, verified against a fake Quartz layer (no Mac needed): Unicode typing, Return/Tab,
   emoji as one UTF-16 string, key combos with modifier flags, click states, scrolling, the emergency-stop corner,
   input-source switching, clipboard backup/restore, and the isolated approval-dialog subprocess.
"""
import ast
import io as _io
import json
import sys as _sys
import textwrap as _tw
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import backends
from backends import FailSafeTriggered, FakeBackend, MacBackend, key_sequence, resolve_key
from macpc_agent import Config, MacPCAgent


def txt(r):
    return r.content[0].text


# ----------------------------- 1) re-check the window after authorization ----------------------------- #
def make(tmp_path, **cfg):
    fb = FakeBackend()
    agent = MacPCAgent(Config(log_dir=str(tmp_path / "logs"), settle_ms=0, **cfg), fb)
    return agent, fb


def steal_focus_to(fb, process, title="VS Code"):
    def go():
        fb.foreground_process, fb.foreground_title = process, title
        fb.point_process = None
    fb.during_confirm = go


async def test_focus_changed_during_confirm_aborts_type_text(tmp_path):
    """While the dialog was up the frontmost app changed (the fallback dialog activates itself; Spotlight closes on any
    outside click). The keys must not go to the app that is frontmost now."""
    agent, fb = make(tmp_path)
    fb.foreground_process, fb.foreground_title = "com.apple.spotlight", "Spotlight"
    steal_focus_to(fb, "com.microsoft.vscode")
    r = await agent.type_text("\u8bb0\u4e8b\u672c", screenshot_after=False)
    assert r.is_error and "target window changed during authorization" in txt(r) and "nothing was sent" in txt(r)
    assert not [a for a in fb.actions if a["op"] == "type_text"], "input must never go to an unapproved window"
    assert "com.apple.spotlight" in agent.session_approved       # approved, so a later retry shows no dialog


async def test_retry_after_focus_is_restored_does_not_prompt_again(tmp_path):
    agent, fb = make(tmp_path)
    fb.foreground_process = "com.apple.spotlight"
    steal_focus_to(fb, "com.microsoft.vscode")
    assert (await agent.type_text("x", screenshot_after=False)).is_error
    fb.during_confirm = None
    fb.foreground_process, fb.foreground_title = "com.apple.spotlight", "Spotlight"
    n = len(fb.confirm_calls)
    r = await agent.type_text("abc", screenshot_after=False)
    assert not r.is_error and len(fb.confirm_calls) == n
    assert fb.actions[-1] == {"op": "type_text", "text": "abc"}


async def test_focus_changed_during_confirm_aborts_press_key_and_click(tmp_path):
    agent, fb = make(tmp_path)
    fb.foreground_process = fb.point_process = "com.apple.spotlight"
    steal_focus_to(fb, "com.microsoft.vscode")
    assert "during authorization" in txt(await agent.press_key("enter", screenshot_after=False))
    assert not fb.actions
    agent2, fb2 = make(tmp_path / "b")
    await agent2.screenshot()
    fb2.point_process = "com.apple.spotlight"
    fb2.during_confirm = lambda: setattr(fb2, "point_process", "com.microsoft.vscode")
    r = await agent2.click(10, 10, screenshot_after=False)
    assert r.is_error and "during authorization" in txt(r) and not [a for a in fb2.actions if a["op"] == "click"]


async def test_window_swapped_after_approval_is_still_caught(tmp_path):
    agent, fb = make(tmp_path, allowed_apps=["com.apple.textedit", "com.microsoft.vscode"])
    calls = {"n": 0}
    real = fb.foreground_window

    def swapping():
        calls["n"] += 1
        if calls["n"] >= 2:                         # call 1 decides, call 2 is the re-check before sending
            fb.foreground_process = "com.google.chrome"
        return real()
    fb.foreground_window = swapping
    r = await agent.type_text("secret?", screenshot_after=False)
    assert r.is_error and not fb.actions


async def test_unchanged_window_still_works_and_prompts_once(tmp_path):
    agent, fb = make(tmp_path)
    fb.foreground_process = "com.google.chrome"
    for _ in range(2):
        assert not (await agent.type_text("ok", screenshot_after=False)).is_error
    assert len(fb.confirm_calls) == 1
    assert [a["text"] for a in fb.actions] == ["ok", "ok"]


async def test_blocklist_and_deny_paths(tmp_path):
    agent, fb = make(tmp_path)
    fb.foreground_process = "com.1password.1password"
    assert "block-list" in txt(await agent.type_text("x", screenshot_after=False))
    fb.foreground_process, fb.confirm_result = "com.google.chrome", False
    assert "denied" in txt(await agent.type_text("x", screenshot_after=False))
    assert not fb.actions


# ----------------------------- paste mode ----------------------------- #
async def test_method_paste_routes_to_paste_text_and_audit_records_method(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    r = await agent.type_text("\u591a\u884c\n\u6587\u672c 🙂", method="paste", screenshot_after=False)
    assert not r.is_error and "Pasted" in txt(r)
    assert fb.actions[-1] == {"op": "paste_text", "text": "\u591a\u884c\n\u6587\u672c 🙂"}
    entry = json.loads((tmp_path / "logs" / "actions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["params"]["method"] == "paste" and entry["params"]["text_len"] == 7


async def test_default_method_is_keys_and_invalid_method_rejected(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    await agent.type_text("abc", screenshot_after=False)
    assert fb.actions[-1]["op"] == "type_text"
    assert "keys or paste" in txt(await agent.type_text("abc", method="clipboard", screenshot_after=False))


async def test_paste_limit_is_larger_than_keys_limit(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    assert (await agent.type_text("x" * 10_001, screenshot_after=False)).is_error
    assert not (await agent.type_text("x" * 10_001, method="paste", screenshot_after=False)).is_error
    assert (await agent.type_text("x" * 100_001, method="paste", screenshot_after=False)).is_error


async def test_paste_obeys_authorization_focus_recheck_and_pause(tmp_path):
    agent, fb = make(tmp_path)
    fb.foreground_process = "com.apple.spotlight"
    steal_focus_to(fb, "com.microsoft.vscode")
    r = await agent.type_text("x", method="paste", screenshot_after=False)
    assert r.is_error and "during authorization" in txt(r) and not fb.actions
    agent.paused = True
    assert "Paused" in txt(await agent.type_text("x", method="paste", screenshot_after=False))


# ----------------------------- 2) expect_app / expect_handle ----------------------------- #
async def test_expect_app_mismatch_refuses_type_text_and_press_key(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    fb.foreground_process, fb.foreground_title = "com.microsoft.vscode", "VS Code"
    r = await agent.type_text("rm -rf ~/x", expect_app="Terminal", screenshot_after=False)
    assert r.is_error and "not the expected 'Terminal'" in txt(r) and "nothing was sent" in txt(r)
    r = await agent.press_key("cmd+a", expect_app="com.apple.TextEdit", screenshot_after=False)
    assert r.is_error and "com.microsoft.vscode" in txt(r)
    assert not fb.actions


async def test_expect_app_accepts_bundle_id_name_or_app_file_name(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    fb.foreground_process = "com.apple.terminal"
    for spec in ("Terminal", "terminal.app", " com.apple.Terminal ", "TERMINAL"):
        assert not (await agent.type_text("x", expect_app=spec, screenshot_after=False)).is_error, spec
    assert not (await agent.press_key("enter", expect_app="terminal", screenshot_after=False)).is_error
    assert len(fb.actions) == 5


async def test_expect_app_is_checked_before_any_prompt_and_unknown_foreground_is_refused(tmp_path):
    agent, fb = make(tmp_path)                    # prompt mode
    fb.foreground_process = "com.microsoft.vscode"
    r = await agent.type_text("x", expect_app="TextEdit", screenshot_after=False)
    assert r.is_error and not fb.confirm_calls, "the approval dialog must not be shown first when the window is wrong"
    fb.foreground_process = None
    assert (await agent.type_text("x", expect_app="TextEdit", screenshot_after=False)).is_error
    assert not fb.actions


async def test_expect_handle_distinguishes_two_windows_of_the_same_app(tmp_path):
    """The Agent window, the tunnel window and the user's own shell are all Terminal windows."""
    agent, fb = make(tmp_path, approval_mode="all")
    fb.foreground_process = "com.apple.terminal"
    mine = fb.foreground_window().handle
    assert not (await agent.type_text("x", expect_app="Terminal", expect_handle=mine, screenshot_after=False)).is_error
    r = await agent.type_text("x", expect_app="Terminal", expect_handle=mine + 1, screenshot_after=False)
    assert r.is_error and f"handle={mine + 1}" in txt(r) and "nothing was sent" in txt(r)
    assert (await agent.press_key("enter", expect_handle=mine + 1, screenshot_after=False)).is_error
    assert (await agent.type_text("x", expect_handle=0, screenshot_after=False)).is_error      # 0 is not None
    assert len(fb.actions) == 1


# ----------------------------- 3) MacBackend against a fake Quartz layer ----------------------------- #
class FakeQuartz:
    kCGHIDEventTap = 0
    kCGMouseEventClickState = 1
    kCGScrollEventUnitLine = 1

    def __init__(self):
        self.posted = []
        self.cursor = (500.0, 400.0)

    # event construction
    def CGEventCreateKeyboardEvent(self, src, code, down):
        return {"kind": "key", "code": code, "down": bool(down), "flags": None}

    def CGEventCreateMouseEvent(self, src, typ, pt, button):
        return {"kind": "mouse", "type": typ, "pt": pt, "button": button, "flags": None}

    def CGEventCreateScrollWheelEvent(self, src, unit, count, dy, dx):
        return {"kind": "scroll", "unit": unit, "dy": dy, "dx": dx}

    def CGPointMake(self, x, y):
        return (x, y)

    def CGEventSetFlags(self, ev, flags):
        ev["flags"] = flags

    def CGEventSetIntegerValueField(self, ev, field, value):
        ev[f"field{field}"] = value

    def CGEventKeyboardSetUnicodeString(self, ev, n, s):
        ev["unicode"], ev["units"] = s, n

    def CGEventPost(self, tap, ev):
        self.posted.append(ev)
        if ev["kind"] == "mouse":
            self.cursor = ev["pt"]

    def CGEventCreate(self, src):
        return "current"

    def CGEventGetLocation(self, ev):
        return SimpleNamespace(x=self.cursor[0], y=self.cursor[1])


class FakeTIS:
    def __init__(self):
        self.log = []

    def select_ascii(self):
        self.log.append("ascii")
        return "pinyin"

    def restore(self, token):
        self.log.append(f"restore:{token}")


class _Pool:
    @contextmanager
    def autorelease_pool(self):
        yield


@pytest.fixture
def mac(monkeypatch):
    """A MacBackend without macOS: __init__ is bypassed and every framework is replaced by a fake."""
    sleeps = []
    monkeypatch.setattr(backends.time, "sleep", lambda s: sleeps.append(s))
    b = object.__new__(MacBackend)
    b.Q, b._objc, b._tis = FakeQuartz(), _Pool(), FakeTIS()
    b.AS = SimpleNamespace(AXIsProcessTrusted=lambda: True)
    b.type_chunk_chars, b.type_delay_s, b.type_newline_delay_s = 1, 0.005, 0.06
    b.paste_restore_delay_s, b.switch_input_source, b._host_app = 0, True, "Terminal"
    b.sleeps = sleeps
    return b


def keys_of(posted):
    return [(e["code"], e["down"], e["flags"]) for e in posted if e["kind"] == "key"]


def test_type_text_sends_each_character_as_a_unicode_key_pair(mac):
    mac.type_text("a\u4e2d🙂\n\tb")
    uni = [e for e in mac.Q.posted if e.get("unicode")]
    assert [(e["unicode"], e["units"], e["down"]) for e in uni] == [
        ("a", 1, True), ("a", 1, False), ("\u4e2d", 1, True), ("\u4e2d", 1, False),
        ("🙂", 2, True), ("🙂", 2, False),          # the emoji's surrogate pair travels as one string
        ("b", 1, True), ("b", 1, False)]
    real_keys = [(e["code"], e["down"]) for e in mac.Q.posted if e["kind"] == "key" and not e.get("unicode")]
    assert real_keys == [(36, True), (36, False), (48, True), (48, False)]     # Return and Tab are real keys
    # ASCII goes out with its real key code (web inputs dropped characters when everything was key code 0);
    # other characters keep key code 0 and rely on the attached Unicode string
    assert [(e["unicode"], e["code"]) for e in uni if e["down"]] == [("a", 0), ("\u4e2d", 0), ("🙂", 0), ("b", 11)]


def test_ascii_characters_use_real_key_codes_and_shift():
    assert backends.char_keycode("s") == (1, 0)
    assert backends.char_keycode("S") == (1, 0x20000)
    assert backends.char_keycode("+") == (24, 0x20000)
    assert backends.char_keycode(" ") == (49, 0)
    assert backends.char_keycode("/") == (44, 0)
    assert backends.char_keycode("\u4e2d") == (0, 0) and backends.char_keycode("é") == (0, 0)


def test_type_text_pacing_newline_delay_and_crlf(mac):
    mac.type_text("ab\r\ncd")
    pacing = [s for s in mac.sleeps if s in (0.005, 0.06)]
    assert pacing == [0.005, 0.005, 0.06, 0.005, 0.005]


def test_type_text_switches_input_source_and_restores_it(mac):
    mac.type_text("x")
    assert mac._tis.log == ["ascii", "restore:pinyin"]
    mac._tis.log.clear()
    mac.switch_input_source = False
    mac.type_text("x")
    assert mac._tis.log == []


def test_input_source_is_restored_even_if_sending_fails(mac):
    def boom(ev, *a):
        raise RuntimeError("post failed")
    mac.Q.CGEventKeyboardSetUnicodeString = boom
    with pytest.raises(RuntimeError):
        mac.type_text("x")
    assert mac._tis.log == ["ascii", "restore:pinyin"]


def test_press_keys_combo_sets_modifier_flags_and_releases_in_reverse(mac):
    mac.press_keys(["cmd", "shift", "z"], 1)
    assert keys_of(mac.Q.posted) == [
        (55, True, 0x100000), (56, True, 0x120000), (6, True, 0x120000),
        (6, False, 0x120000), (56, False, 0x100000), (55, False, 0)]
    assert mac._tis.log == [], "shortcuts with cmd are not touched by an input method; no switch needed"


def test_press_keys_plain_character_switches_input_source(mac):
    mac.press_keys(["j"], 2)
    assert keys_of(mac.Q.posted) == [(38, True, 0), (38, False, 0)] * 2
    assert mac._tis.log == ["ascii", "restore:pinyin"]


def test_shifted_symbols_and_named_keys():
    assert key_sequence(["+"]) == [(56, 0x20000), (24, 0)]
    assert key_sequence(["shift", "+"]) == [(56, 0x20000), (24, 0)]          # no second shift
    assert key_sequence(["cmd", "space"]) == [(55, 0x100000), (49, 0)]
    assert resolve_key("backspace") == (51, False) and resolve_key("forwarddelete") == (117, False)
    assert resolve_key("delete") == (117, False)       # PC/xdotool naming: Delete = forward delete; backwards is backspace
    with pytest.raises(backends.BackendError):
        resolve_key("printscreen")


def test_click_uses_click_state_for_double_click_and_modifier_flags(mac):
    mac.click(100, 200, "left", 2, ["cmd"])
    downs_ups = [(e["type"], e["field1"], e["flags"]) for e in mac.Q.posted if e["type"] in (1, 2)]
    assert downs_ups == [(1, 1, 0x100000), (2, 1, 0x100000), (1, 2, 0x100000), (2, 2, 0x100000)]
    assert mac.Q.posted[0]["type"] == 5 and mac.Q.posted[0]["pt"] == (100, 200)     # moved there first


def test_right_and_middle_buttons(mac):
    mac.click(1, 1, "right", 1, [])
    mac.click(1, 1, "middle", 1, [])
    types = [e["type"] for e in mac.Q.posted if e["type"] != 5]
    assert types == [3, 4, 25, 26]


def test_scroll_directions(mac):
    mac.scroll(10, 10, "down", 2)
    mac.scroll(10, 10, "left", 1)
    sc = [(e["dy"], e["dx"]) for e in mac.Q.posted if e["kind"] == "scroll"]
    assert sc == [(-3, 0), (-3, 0), (0, 3)]


def test_drag_presses_moves_and_always_releases(mac):
    mac.drag(0, 0, 100, 50, "left")
    types = [e["type"] for e in mac.Q.posted]
    assert types[1] == 1 and types[-1] == 2 and types.count(6) == 20
    assert mac.Q.posted[-1]["pt"] == (100, 50)


def test_failsafe_corner_raises_and_agent_never_parks_on_it(mac):
    mac.Q.cursor = (0.0, 0.0)
    with pytest.raises(FailSafeTriggered):
        mac.click(5, 5, "left", 1, [])
    assert mac.Q.posted == []
    mac.Q.cursor = (300.0, 300.0)
    mac.move_mouse(0, 0)
    assert mac.Q.posted[-1]["pt"] == (1, 1)          # the next action must not trip the stop by itself


def test_failsafe_also_interrupts_long_typing(mac):
    def post(tap, ev):
        mac.Q.posted.append(ev)
        if len(mac.Q.posted) == 40:
            mac.Q.cursor = (0.0, 0.0)               # the user flings the mouse to the corner mid-way
    mac.Q.CGEventPost = post
    with pytest.raises(FailSafeTriggered):
        mac.type_text("x" * 200)
    assert len([e for e in mac.Q.posted if e.get("unicode")]) < 100
    assert mac._tis.log[-1] == "restore:pinyin"


def test_missing_accessibility_gives_actionable_error(mac):
    mac.AS = SimpleNamespace(AXIsProcessTrusted=lambda: False)
    with pytest.raises(backends.BackendError) as e:
        mac.type_text("x")
    assert "Accessibility" in str(e.value) and "Terminal" in str(e.value)
    assert mac.Q.posted == []


# ---- clipboard paste with a fake NSPasteboard ----
class FakeItem:
    def __init__(self, data=None):
        self.data = dict(data or {})

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def types(self):
        return list(self.data)

    def dataForType_(self, t):
        return self.data.get(t)

    def setData_forType_(self, d, t):
        self.data[t] = d

    def setString_forType_(self, s, t):
        self.data[t] = s


class FakePasteboard:
    def __init__(self, items):
        self.items, self.count, self.log = [FakeItem(i) for i in items], 1, []

    def pasteboardItems(self):
        return list(self.items)

    def clearContents(self):
        self.items, self.count = [], self.count + 1
        self.log.append("clear")

    def writeObjects_(self, items):
        self.items, self.count = list(items), self.count + 1
        self.log.append(f"write:{[sorted(i.data) for i in items]}")
        return True

    def changeCount(self):
        return self.count


def paste_mac(mac, items):
    pb = FakePasteboard(items)
    mac.AppKit = SimpleNamespace(NSPasteboard=SimpleNamespace(generalPasteboard=lambda: pb),
                                 NSPasteboardItem=FakeItem, NSPasteboardTypeString="public.utf8-plain-text")
    return pb


def test_paste_backs_up_every_type_pastes_with_cmd_v_and_restores(mac):
    pb = paste_mac(mac, [{"public.utf8-plain-text": "orig", "public.png": b"PNG"}, {"public.file-url": "file:///x"}])
    assert mac.paste_text("\u884c1\n\u884c2") == ""
    assert keys_of(mac.Q.posted) == [(55, True, 0x100000), (9, True, 0x100000), (9, False, 0x100000), (55, False, 0)]
    assert [i.data for i in pb.items] == [{"public.utf8-plain-text": "orig", "public.png": b"PNG"},
                                          {"public.file-url": "file:///x"}], "every item and type is restored"
    assert "org.nspasteboard.TransientType" in pb.log[1], "clipboard managers are told not to keep the pasted text"


def test_paste_restores_clipboard_even_if_sending_fails(mac):
    pb = paste_mac(mac, [{"public.utf8-plain-text": "orig"}])

    def boom(tap, ev):
        raise RuntimeError("post failed")
    mac.Q.CGEventPost = boom
    with pytest.raises(RuntimeError):
        mac.paste_text("new")
    assert [i.data for i in pb.items] == [{"public.utf8-plain-text": "orig"}]


def test_paste_does_not_clobber_a_clipboard_changed_meanwhile(mac):
    pb = paste_mac(mac, [{"public.utf8-plain-text": "orig"}])
    real_post = mac.Q.CGEventPost

    def post(tap, ev):
        real_post(tap, ev)
        if ev["code"] == 55 and not ev["down"]:
            pb.items, pb.count = [FakeItem({"public.utf8-plain-text": "user copied this"})], pb.count + 1
    mac.Q.CGEventPost = post
    mac.paste_text("new")
    assert [i.data for i in pb.items] == [{"public.utf8-plain-text": "user copied this"}]


def test_paste_empty_text_does_nothing(mac):
    pb = paste_mac(mac, [{"public.utf8-plain-text": "keep"}])
    assert mac.paste_text("") == "" and pb.log == [] and mac.Q.posted == []


# ---- approval dialog subprocess: GUI code runs in a separate process so it can never take the Agent down ----
def test_gui_frameworks_are_only_imported_inside_functions():
    """AppKit / tkinter at module top level would make the module unimportable on Linux and would initialise GUI state in
    the Agent process; the NSApplication of the dialog may only be created in the subprocess entry run_confirm_dialog."""
    tree = ast.parse(open(backends.__file__, encoding="utf-8").read())
    top = [ast.dump(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any(m in d for d in top for m in ("AppKit", "Quartz", "objc", "tkinter", "ApplicationServices"))
    nsapp_users = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and "NSApplication" in ast.unparse(fn) and "sharedApplication" in ast.unparse(fn):
            nsapp_users.add(fn.name)
    assert nsapp_users == {"run_confirm_dialog"}, nsapp_users


def fake_dialog(monkeypatch, code: str, slack=0.5):
    b = object.__new__(MacBackend)
    b.dialog_slack_s = slack
    f = _tw.dedent(code)
    monkeypatch.setattr(MacBackend, "_dialog_command", staticmethod(lambda: [_sys.executable, "-c", f]))
    return b


def test_dialog_subprocess_yes_and_no_and_payload_roundtrip(monkeypatch):
    yes = fake_dialog(monkeypatch, """
        import json, sys
        a = json.loads(sys.stdin.read())
        assert a["title"] == "\u6807\u9898 ⚠" and a["message"] == "\u884c1\\n\u884c2" and a["timeout_s"] == 5
        print("noise before"); print("MACPC_CONFIRM:YES")
    """)
    assert yes._confirm_panel("\u6807\u9898 ⚠", "\u884c1\n\u884c2", 5) is True
    no = fake_dialog(monkeypatch, 'print("MACPC_CONFIRM:NO")')
    assert no._confirm_panel("t", "m", 5) is False


@pytest.mark.parametrize("code", ["import sys; sys.exit(3)", 'print("garbage")', "import os; os._exit(134)", "pass"])
def test_dialog_subprocess_failures_return_none_so_caller_falls_back(monkeypatch, code):
    assert fake_dialog(monkeypatch, code)._confirm_panel("t", "m", 2) is None


def test_dialog_subprocess_hang_is_killed_and_counts_as_deny(monkeypatch):
    import time as _t
    b = fake_dialog(monkeypatch, "import time; time.sleep(60)", slack=0.3)
    t0 = _t.monotonic()
    assert b._confirm_panel("t", "m", 0.3) is False
    assert _t.monotonic() - t0 < 10


def test_dialog_cannot_start_returns_none(monkeypatch):
    b = object.__new__(MacBackend)
    b.dialog_slack_s = 1
    monkeypatch.setattr(MacBackend, "_dialog_command", staticmethod(lambda: ["/no/such/python"]))
    assert b._confirm_panel("t", "m", 1) is None


def test_confirm_falls_back_to_osascript_and_never_crashes(monkeypatch):
    b = fake_dialog(monkeypatch, "import os; os._exit(134)")
    seen = []
    monkeypatch.setattr(backends, "confirm_osascript", lambda t, m, s: seen.append(t) or False)
    assert b.confirm("t", "m", 2) is False and seen == ["t"]


def test_osascript_fallback_parses_allow_deny_and_timeout(monkeypatch):
    outputs = iter([(0, "button returned:Allow, gave up:false\n"), (1, ""), (0, "button returned:, gave up:true\n")])

    def run(cmd, **kw):
        assert cmd[0] == "osascript" and 'giving up after 7' in cmd[2] and '\\"quoted\\"' in cmd[2]
        rc, out = next(outputs)
        return SimpleNamespace(returncode=rc, stdout=out)
    monkeypatch.setattr(backends.subprocess, "run", run)
    assert backends.confirm_osascript("T", 'say "quoted"', 7) is True
    assert backends.confirm_osascript("T", 'say "quoted"', 7) is False     # Deny = cancel button -> exit code 1
    assert backends.confirm_osascript("T", 'say "quoted"', 7) is False     # gave up (timeout)


class _FakeStdin:
    def __init__(self, data: bytes):
        self.buffer = _io.BytesIO(data)


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_dialog_entry_decodes_utf8_stdin_with_or_without_bom(monkeypatch, capsys, bom):
    payload = json.dumps({"title": "MacPC Remote - \u5e94\u7528\u6388\u6743", "message": "\u52a8\u4f5c\uff1a\u8f93\u5165",
                          "timeout_s": 5}, ensure_ascii=False).encode("utf-8")
    seen = {}
    monkeypatch.setattr(backends, "run_confirm_dialog", lambda t, m, s: seen.update(t=t, m=m, s=s) or True)
    monkeypatch.setattr(backends.sys, "stdin", _FakeStdin(bom + payload))
    assert backends._dialog_main() == 0
    assert seen == {"t": "MacPC Remote - \u5e94\u7528\u6388\u6743", "m": "\u52a8\u4f5c\uff1a\u8f93\u5165", "s": 5.0}
    assert "MACPC_CONFIRM:YES" in capsys.readouterr().out


def test_mac_backend_refuses_other_platforms(monkeypatch):
    monkeypatch.setattr(backends.sys, "platform", "linux")
    with pytest.raises(RuntimeError):
        MacBackend()


def test_config_defaults():
    c = Config()
    assert (c.type_chunk_chars, c.type_delay_ms, c.switch_input_source) == (1, 30, True)


def test_dialog_level_is_set_after_floating_panel():
    """setFloatingPanel_(True) resets the window level to NSFloatingWindowLevel (3); set before it, the screen-saver
    level was lost and menus / Spotlight could cover the approval panel (found in live testing)."""
    src = open(backends.__file__, encoding="utf-8").read()
    body = src[src.index("def run_confirm_dialog"):]
    assert body.index("panel.setFloatingPanel_(True)") < body.index("panel.setLevel_(AppKit.NSScreenSaverWindowLevel)")
