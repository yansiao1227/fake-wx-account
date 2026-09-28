"""微信桌面 uia_sending 回归测试。"""
import time
from contextlib import contextmanager
from types import SimpleNamespace
from channel.wechat_desktop.models import HeaderInfo, UiaChatMessage
from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia.controls import _encode_cf_hdrop
from channel.wechat_desktop.uia.driver import WechatUiaDriver
from .helpers import FakeClient, FakeHook, GeometryControl, row


def test_cf_hdrop_payload_is_wide_dropfiles_data():
    paths = [r"D:\images\one.jpg", r"D:\images\two.png"]

    payload = _encode_cf_hdrop(paths)

    assert isinstance(payload, bytes)
    assert len(payload) > 20
    assert int.from_bytes(payload[0:4], "little") == 20
    assert int.from_bytes(payload[16:20], "little") == 1
    assert payload[20:].decode("utf-16le") == "\0".join(paths) + "\0\0"


def test_file_clipboard_passes_bytes_to_win32clipboard(monkeypatch):
    import win32clipboard

    captured = []
    monkeypatch.setattr(win32clipboard, "OpenClipboard", lambda: None)
    monkeypatch.setattr(win32clipboard, "CloseClipboard", lambda: None)
    monkeypatch.setattr(win32clipboard, "EmptyClipboard", lambda: None)
    monkeypatch.setattr(
        win32clipboard,
        "IsClipboardFormatAvailable",
        lambda _fmt: False,
    )
    monkeypatch.setattr(
        win32clipboard,
        "SetClipboardData",
        lambda fmt, value: captured.append((fmt, value)),
    )

    with WechatUiaClient({})._clipboard(files=[r"D:\images\one.jpg"]):
        pass

    assert len(captured) == 1
    assert isinstance(captured[0][1], bytes)


def test_send_button_uses_real_click_and_restores_cursor(monkeypatch):
    import win32api

    calls = []
    monkeypatch.setattr(win32api, "GetCursorPos", lambda: (123, 456))
    monkeypatch.setattr(
        win32api,
        "SetCursorPos",
        lambda position: calls.append(("restore", position)),
    )

    class Button:
        def Click(self, **kwargs):
            calls.append(("click", kwargs))

        def GetInvokePattern(self):
            raise AssertionError("WeChat 4.1.9.30 InvokePattern must not be used")

    WechatUiaClient({})._click_send_button(Button())

    assert calls == [
        ("click", {"simulateMove": False, "waitTime": 0.1}),
        ("restore", (123, 456)),
    ]


def test_startup_foreground_check_does_not_use_uia_tree(monkeypatch):
    import win32gui
    import win32process

    client = WechatUiaClient({})
    calls = []
    foreground = {"hwnd": 200}

    monkeypatch.setattr(client, "_window", lambda: (100, 42))
    monkeypatch.setattr(
        client,
        "probe_tree",
        lambda: (_ for _ in ()).throw(AssertionError("UIA tree must not be read")),
    )
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: foreground["hwnd"])
    monkeypatch.setattr(
        win32process,
        "GetWindowThreadProcessId",
        lambda hwnd: (1, 42 if hwnd == 100 else 99),
    )
    monkeypatch.setattr(
        win32gui,
        "ShowWindow",
        lambda hwnd, command: calls.append(("restore", hwnd, command)),
    )

    def set_foreground(hwnd):
        calls.append(("foreground", hwnd))
        foreground["hwnd"] = hwnd

    monkeypatch.setattr(win32gui, "SetForegroundWindow", set_foreground)

    assert client.ensure_foreground_window() is True
    assert [item[0] for item in calls] == ["restore", "foreground"]


def test_startup_foreground_check_accepts_any_wechat_process_window(monkeypatch):
    import win32gui
    import win32process

    client = WechatUiaClient({})
    monkeypatch.setattr(client, "_window", lambda: (100, 42))
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 101)
    monkeypatch.setattr(
        win32process,
        "GetWindowThreadProcessId",
        lambda _hwnd: (1, 42),
    )
    monkeypatch.setattr(
        win32gui,
        "ShowWindow",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("foreground WeChat must not be restored")
        ),
    )

    assert client.ensure_foreground_window() is False


def test_input_text_normalization_handles_uia_line_endings_and_spaces():
    normalize = WechatUiaClient._normalize_input_text

    assert normalize("第一行\r\n第二行\u00a0文本") == normalize(
        "第一行\n第二行 文本"
    )
    assert normalize("e\u0301") == normalize("é")


def test_paste_sends_nonempty_text_even_when_uia_value_differs(monkeypatch):
    import win32api

    state = {"value": "", "paste_count": 0, "click_count": 0}

    class Input(GeometryControl):
        def SetFocus(self):
            pass

        def GetValuePattern(self):
            return SimpleNamespace(Value=state["value"])

    class Button(GeometryControl):
        def Click(self, **_kwargs):
            state["click_count"] += 1
            state["value"] = ""

    input_control = Input((0, 0, 100, 50), automation_id="chat_input_field")
    send_button = Button(
        (100, 0, 150, 50), name="发送", class_name="mmui::XOutlineButton"
    )
    client = WechatUiaClient({})

    @contextmanager
    def fake_root():
        yield object()

    def keybd_event(key, _scan, flags, _extra):
        if key == ord("V") and flags == 0:
            state["paste_count"] += 1
            state["value"] = "UIA 返回的可见片段"

    monkeypatch.setattr(win32api, "keybd_event", keybd_event)
    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_walk", lambda _root: iter((input_control, send_button)))
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)

    client._paste_and_send("完整的原始文本")

    assert state == {"value": "", "paste_count": 1, "click_count": 1}


def test_paste_retries_once_when_input_remains_empty(monkeypatch):
    import win32api

    state = {"value": "", "paste_count": 0, "click_count": 0}

    class Input(GeometryControl):
        def SetFocus(self):
            pass

        def GetValuePattern(self):
            return SimpleNamespace(Value=state["value"])

    class Button(GeometryControl):
        def Click(self, **_kwargs):
            state["click_count"] += 1
            state["value"] = ""

    input_control = Input((0, 0, 100, 50), automation_id="chat_input_field")
    send_button = Button(
        (100, 0, 150, 50), name="发送", class_name="mmui::XOutlineButton"
    )
    client = WechatUiaClient({})

    @contextmanager
    def fake_root():
        yield object()

    def keybd_event(key, _scan, flags, _extra):
        if key == ord("V") and flags == 0:
            state["paste_count"] += 1
            if state["paste_count"] == 2:
                state["value"] = "第二次粘贴成功"

    monkeypatch.setattr(win32api, "keybd_event", keybd_event)
    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_walk", lambda _root: iter((input_control, send_button)))
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)
    monkeypatch.setattr(
        client,
        "_wait_for_input_value",
        lambda control, predicate, _timeout: predicate(client._input_value(control)),
    )

    client._paste_and_send("第二次粘贴成功")

    assert state == {"value": "", "paste_count": 2, "click_count": 1}


def test_long_message_split_prefers_readable_boundaries():
    text = ("A" * 280) + "。" + ("B" * 280) + "\n\n" + ("C" * 280)

    chunks = WechatUiaClient._split_message_text(text, 500)

    assert len(chunks) == 2
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_paste_retry_reacquires_window_and_input(monkeypatch):
    import win32api

    state = {"value": "", "paste_count": 0, "focus_count": 0, "root_count": 0}

    class Input(GeometryControl):
        HasKeyboardFocus = True

        def SetFocus(self):
            pass

        def GetValuePattern(self):
            return SimpleNamespace(Value=state["value"])

    class Button(GeometryControl):
        def Click(self, **_kwargs):
            state["value"] = ""

    input_control = Input((0, 0, 100, 50), automation_id="chat_input_field")
    send_button = Button(
        (100, 0, 150, 50), name="发送", class_name="mmui::XOutlineButton"
    )
    client = WechatUiaClient({"uia_paste_attempts": 3})

    @contextmanager
    def fake_root():
        state["root_count"] += 1
        yield object()

    def keybd_event(key, _scan, flags, _extra):
        if key == ord("V") and flags == 0:
            state["paste_count"] += 1
            if state["paste_count"] == 2:
                state["value"] = "retry succeeded"

    def focus_window():
        state["focus_count"] += 1

    monkeypatch.setattr(win32api, "keybd_event", keybd_event)
    monkeypatch.setattr(client, "focus_window", focus_window)
    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_walk", lambda _root: iter((input_control, send_button)))
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)
    monkeypatch.setattr(
        client,
        "_wait_for_input_value",
        lambda control, predicate, _timeout: predicate(client._input_value(control)),
    )

    client._paste_and_send("retry succeeded")

    assert state["paste_count"] == 2
    assert state["focus_count"] == 2
    assert state["root_count"] == 2


def test_send_message_splits_and_verifies_each_chunk(monkeypatch):
    client = WechatUiaClient({"uia_text_chunk_chars": 100})
    pasted = []
    verified = []

    @contextmanager
    def fake_clipboard(unicode_text=None, files=None):
        assert files is None
        pasted.append(unicode_text)
        yield

    monkeypatch.setattr(client, "_wait_for_send_slot", lambda _who: None)
    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "locate_conversation", lambda *_args: True)
    monkeypatch.setattr(
        client, "get_title", lambda: HeaderInfo("target", "private", 1)
    )
    monkeypatch.setattr(client, "get_chat_history", lambda **_kwargs: [])
    monkeypatch.setattr(client, "_clipboard", fake_clipboard)
    monkeypatch.setattr(client, "_paste_and_send", lambda expected_text: None)

    def verify(_who, _before, text="", expected_type=""):
        assert not expected_type
        verified.append(text)
        return {"success": True, "verified": True}

    monkeypatch.setattr(client, "_verify_send", verify)

    result = client.send_message("target", "X" * 240)

    assert [len(chunk) for chunk in pasted] == [100, 100, 40]
    assert verified == pasted
    assert result == {
        "success": True,
        "verified": True,
        "chunks": 3,
        "submitted_chunks": 3,
        "verified_chunks": 3,
        "chunk_results": [{"success": True, "verified": True}] * 3,
        "message": "",
    }


def test_send_message_retries_uncleared_input_with_enter_after_verification(
    monkeypatch,
):
    client = WechatUiaClient({})
    enter_retries = []
    verification_results = iter(
        [
            {
                "success": True,
                "verified": False,
                "message": "not visible yet",
            },
            {"success": True, "verified": True, "runtime_id": "sent-1"},
        ]
    )

    @contextmanager
    def fake_clipboard(unicode_text=None, files=None):
        assert unicode_text == "working"
        assert files is None
        yield

    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "locate_conversation", lambda *_args: True)
    monkeypatch.setattr(
        client, "get_title", lambda: HeaderInfo("Alice", "private", 1)
    )
    monkeypatch.setattr(client, "get_chat_history", lambda **_kwargs: [])
    monkeypatch.setattr(client, "_clipboard", fake_clipboard)
    monkeypatch.setattr(
        client,
        "_paste_and_send",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("WeChat Send button did not clear the reply input")
        ),
    )
    monkeypatch.setattr(
        client,
        "_verify_send",
        lambda *_args, **_kwargs: next(verification_results),
    )
    monkeypatch.setattr(
        client,
        "_send_existing_input_with_enter",
        lambda text: enter_retries.append(text) or True,
    )

    result = client.send_message("Alice", "working", expedited=True)

    assert enter_retries == ["working"]
    assert result["success"] is True
    assert result["verified"] is True


def test_expedited_send_skips_pacing_and_does_not_delay_next_reply(monkeypatch):
    client = WechatUiaClient({"uia_text_chunk_chars": 500})
    waits = []

    @contextmanager
    def fake_clipboard(unicode_text=None, files=None):
        assert unicode_text == "working"
        assert files is None
        yield

    monkeypatch.setattr(client, "_wait_for_send_slot", lambda who: waits.append(who))
    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "locate_conversation", lambda *_args: True)
    monkeypatch.setattr(
        client, "get_title", lambda: HeaderInfo("Alice", "private", 1)
    )
    monkeypatch.setattr(client, "get_chat_history", lambda **_kwargs: [])
    monkeypatch.setattr(client, "_clipboard", fake_clipboard)
    monkeypatch.setattr(client, "_paste_and_send", lambda expected_text: None)
    monkeypatch.setattr(
        client,
        "_verify_send",
        lambda *_args, **_kwargs: {"success": True, "verified": True},
    )

    client.send_message("Alice", "working", expedited=True)

    assert waits == []
    assert client._last_send_at == 0.0
    assert client._last_conversation_send == {}


def test_send_pacing_waits_outside_the_uia_section(monkeypatch):
    """Humanized pacing must never hold the UIA lease that blocks scanning."""
    client = WechatUiaClient({"uia_text_chunk_chars": 100})
    timeline = []
    section_depth = 0

    @contextmanager
    def tracking_section():
        nonlocal section_depth
        section_depth += 1
        timeline.append(("section_enter", section_depth))
        try:
            yield
        finally:
            section_depth -= 1
            timeline.append(("section_exit", section_depth))

    client.uia_section = tracking_section

    def wait_for_slot(_who):
        timeline.append(("pacing_wait", section_depth))

    @contextmanager
    def fake_clipboard(unicode_text=None, files=None):
        yield

    monkeypatch.setattr(client, "_wait_for_send_slot", wait_for_slot)
    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "locate_conversation", lambda *_args: True)
    monkeypatch.setattr(
        client, "get_title", lambda: HeaderInfo("target", "private", 1)
    )
    monkeypatch.setattr(client, "get_chat_history", lambda **_kwargs: [])
    monkeypatch.setattr(client, "_clipboard", fake_clipboard)
    monkeypatch.setattr(client, "_paste_and_send", lambda expected_text: None)
    monkeypatch.setattr(
        client,
        "_verify_send",
        lambda *_args, **_kwargs: {"success": True, "verified": True},
    )

    result = client.send_message("target", "X" * 240)

    assert result["success"] is True
    pacing_entries = [entry for entry in timeline if entry[0] == "pacing_wait"]
    section_enters = [entry for entry in timeline if entry[0] == "section_enter"]
    # Three chunks: each pacing wait happens with no UIA section held, and
    # each chunk's UI work runs inside exactly one section.
    assert len(pacing_entries) == 3
    assert all(depth == 0 for _, depth in pacing_entries)
    assert len(section_enters) == 3
    assert all(depth == 1 for _, depth in section_enters)


def test_send_file_pacing_waits_outside_the_uia_section(monkeypatch, tmp_path):
    client = WechatUiaClient({})
    timeline = []
    section_depth = 0

    @contextmanager
    def tracking_section():
        nonlocal section_depth
        section_depth += 1
        try:
            yield
        finally:
            section_depth -= 1

    client.uia_section = tracking_section
    monkeypatch.setattr(
        client,
        "_wait_for_send_slot",
        lambda _who: timeline.append(("pacing_wait", section_depth)),
    )

    @contextmanager
    def fake_clipboard(unicode_text=None, files=None):
        yield

    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "locate_conversation", lambda *_args: True)
    monkeypatch.setattr(client, "get_chat_history", lambda **_kwargs: [])
    monkeypatch.setattr(client, "_clipboard", fake_clipboard)
    monkeypatch.setattr(
        client,
        "_paste_and_send",
        lambda: timeline.append(("paste", section_depth)),
    )
    monkeypatch.setattr(
        client,
        "_verify_send",
        lambda *_args, **_kwargs: {"success": True, "verified": True},
    )
    media = tmp_path / "photo.png"
    media.write_bytes(b"data")

    result = client.send_file("Alice", [str(media)])

    assert result["verified"] is True
    assert timeline == [("pacing_wait", 0), ("paste", 1)]


def test_unverified_send_registers_text_to_suppress_echo(monkeypatch):
    """Text sent but unverified must still register to prevent self-reply loops."""
    client = WechatUiaClient({"uia_text_chunk_chars": 500})

    @contextmanager
    def fake_clipboard(unicode_text=None, files=None):
        yield

    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "locate_conversation", lambda *_args: True)
    monkeypatch.setattr(
        client, "get_title", lambda: HeaderInfo("Alice", "private", 1)
    )
    monkeypatch.setattr(client, "get_chat_history", lambda **_kwargs: [])
    monkeypatch.setattr(client, "_clipboard", fake_clipboard)
    monkeypatch.setattr(client, "_paste_and_send", lambda expected_text: None)
    monkeypatch.setattr(
        client,
        "_verify_send",
        lambda *_args, **_kwargs: {"success": True, "verified": False},
    )
    # Do NOT monkeypatch remember_outgoing_message — we need the real cache written.

    result = client.send_message("Alice", "hello")

    assert result["verified"] is False
    # Unverified send must still populate the outgoing cache so that
    # is_known_outgoing_message can suppress the echo on the next scan.
    echo_message = UiaChatMessage("Alice", "hello", direction="unknown")
    assert client.is_known_outgoing_message("Alice", echo_message) is True


def test_outgoing_echo_uses_short_text_and_long_runtime_windows(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    client = WechatUiaClient(
        {
            "outgoing_echo_suppression_seconds": 1800,
            "outgoing_echo_text_suppression_seconds": 120,
        }
    )
    client.remember_outgoing_message("Alice", "收到", "bot-runtime")

    clock[0] += 121

    same_text_from_user = UiaChatMessage(
        "Alice", "收到", direction="unknown", runtime_id="user-runtime"
    )
    rebuilt_outgoing = UiaChatMessage(
        "", "内容可能变化", direction="unknown", runtime_id="bot-runtime"
    )
    assert client.is_known_outgoing_message("Alice", same_text_from_user) is False
    assert client.is_known_outgoing_message("Alice", rebuilt_outgoing) is True

    clock[0] += 1680
    assert client.is_known_outgoing_message("Alice", rebuilt_outgoing) is False


def test_select_reply_target_skips_outgoing_bubbles_in_private_chat(monkeypatch):
    """OCR-confirmed outgoing direction acts as backstop against self-reply loops."""
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    # Message history: two incoming, then one outgoing (OCR labeled it).
    # The outgoing bubble is not in known_outgoing_message cache (e.g., after restart).
    client.histories["Alice"] = [
        UiaChatMessage("Alice", "how are you?", direction="incoming", runtime_id="1"),
        UiaChatMessage("Alice", "please reply", direction="incoming", runtime_id="2"),
        UiaChatMessage("Me", "I'm fine", direction="outgoing", runtime_id="3"),
    ]
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())

    _, events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in events])

    # Should select "please reply" (last incoming), not "I'm fine" (outgoing).
    assert len(events) == 1
    assert events[0].content == "please reply"
