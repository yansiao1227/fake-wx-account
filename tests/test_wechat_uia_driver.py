import threading
import time
import logging
import queue
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from bridge.context import ContextType
from channel.wechat_desktop.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.group_sender_ocr import (
    OcrTextLine,
    RapidOcrGroupSenderResolver,
)
from channel.wechat_desktop.models import (
    ConversationInfo,
    HeaderInfo,
    OwnerInfo,
    UiaChatMessage,
    UiaReferencedMessage,
    WechatDesktopEvent,
)
from channel.wechat_desktop.shell_hook import WindowsShellHook
from channel.wechat_desktop.uia_client import WechatUiaClient, parse_session_accessible_name
from channel.wechat_desktop.uia_driver import (
    WechatUiaDriver,
    _UiaPriorityCoordinator,
)
from channel.wechat_desktop.wechat_desktop_message import WechatDesktopMessage
from channel.wechat_desktop.wechat_desktop_channel import (
    ATTACHMENT_REFERENCE_REQUIRED_REPLY,
    DEFAULT_CONFIG,
    WechatDesktopChannel,
    _format_agent_notice,
    _is_network_reply_error,
    _preflight_tool_notice_data,
    _render_event_context_lines,
    _reply_requirements,
    _strip_group_bot_mentions,
    _tool_notice_subject,
)
from common.log import logger


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


class GeometryControl:
    def __init__(
        self,
        bounds,
        name="",
        class_name="",
        children=None,
        automation_id="",
    ):
        self.BoundingRectangle = SimpleNamespace(
            left=bounds[0], top=bounds[1], right=bounds[2], bottom=bounds[3]
        )
        self.Name = name
        self.ClassName = class_name
        self.AutomationId = automation_id
        self.ControlTypeName = "CustomControl"
        self._children = list(children or [])

    def GetChildren(self):
        return list(self._children)


class ClickableGeometryControl(GeometryControl):
    def __init__(self, *args, on_click=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.click_count = 0
        self.on_click = on_click

    def Click(self):
        self.click_count += 1
        if self.on_click:
            self.on_click()


def test_owner_discovery_retries_after_transient_empty_result(monkeypatch):
    client = WechatUiaClient({})
    root = GeometryControl((0, 0, 1000, 800))
    popup_results = iter([("", ""), ("颜料盒bot", "wxid_bot")])
    popup_calls = []

    @contextmanager
    def fake_root():
        yield root

    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_walk", lambda _root: iter(()))

    def read_popup(_root):
        popup_calls.append(True)
        return next(popup_results)

    monkeypatch.setattr(client, "_read_owner_profile_popup", read_popup)

    assert client.get_owner_info() == OwnerInfo("", source="unknown")
    assert client.get_owner_info() == OwnerInfo(
        "颜料盒bot", wx_id="wxid_bot", source="uia"
    )
    assert client.get_owner_info().nick_name == "颜料盒bot"
    assert len(popup_calls) == 2


def test_image_bubble_is_captured_to_tmp(monkeypatch, tmp_path):
    from PIL import Image, ImageGrab

    captures = []

    def grab(*, bbox, all_screens):
        captures.append((bbox, all_screens))
        return Image.new("RGB", (bbox[2] - bbox[0], bbox[3] - bbox[1]), "red")

    monkeypatch.setattr(ImageGrab, "grab", grab)
    message = UiaChatMessage(
        "Alice",
        "[图片]",
        message_type="image",
        runtime_id="image-1",
        bounds=(-50, 100, 150, 260),
    )

    image_path = WechatUiaClient({}).fetch_message_image(message, tmp_path)

    assert captures == [((-50, 100, 150, 260), True)]
    assert Path(image_path).parent == tmp_path
    assert Path(image_path).suffix == ".png"
    assert Image.open(image_path).size == (200, 160)


def test_image_viewer_close_control_is_found_by_exact_uia_identity():
    close = GeometryControl((900, 0, 950, 50), "关闭", "mmui::XButton")
    close.ControlTypeName = "ButtonControl"
    wrong_class = GeometryControl((850, 0, 900, 50), "关闭", "QWidget")
    wrong_class.ControlTypeName = "ButtonControl"
    root = GeometryControl(
        (0, 0, 1000, 800),
        "Weixin",
        "mmui::XView",
        children=[wrong_class, close],
    )
    root.ControlTypeName = "WindowControl"

    assert WechatUiaClient._find_image_viewer_close_control(root) is close


def test_image_viewer_uia_close_clicks_when_invoke_is_a_noop(monkeypatch):
    import sys
    import win32api
    import win32gui

    alive = {200: True}
    calls = []

    class InvokePattern:
        def Invoke(self):
            calls.append("invoke")

    class CloseButton(GeometryControl):
        def GetInvokePattern(self):
            return InvokePattern()

        def Click(self, **kwargs):
            calls.append(("click", kwargs))
            alive[200] = False

    close = CloseButton((900, 0, 950, 50), "关闭", "mmui::XButton")
    close.ControlTypeName = "ButtonControl"
    root = GeometryControl(
        (0, 0, 1000, 800),
        "Weixin",
        "mmui::XView",
        children=[close],
    )

    @contextmanager
    def uia_thread():
        yield

    fake_auto = SimpleNamespace(
        UIAutomationInitializerInThread=uia_thread,
        ControlFromHandle=lambda hwnd: root,
    )
    ticks = iter([0.0, 1.0])
    monkeypatch.setitem(sys.modules, "uiautomation", fake_auto)
    monkeypatch.setattr(win32api, "GetCursorPos", lambda: (10, 20))
    monkeypatch.setattr(
        win32api,
        "SetCursorPos",
        lambda point: calls.append(("restore", point)),
    )
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: alive.get(hwnd, False))
    monkeypatch.setattr(
        win32gui, "IsWindowVisible", lambda hwnd: alive.get(hwnd, False)
    )
    monkeypatch.setattr(
        "channel.wechat_desktop.uia_client.time.monotonic",
        lambda: next(ticks),
    )

    assert WechatUiaClient({})._try_close_image_viewer_with_uia(200) is True
    assert calls == [
        "invoke",
        ("click", {"simulateMove": False, "waitTime": 0.1}),
        ("restore", (10, 20)),
    ]


def test_image_viewer_uia_close_accepts_hidden_reusable_hwnd(monkeypatch):
    import sys
    import win32api
    import win32gui

    visible = {200: True}
    calls = []

    class InvokePattern:
        def Invoke(self):
            calls.append("invoke")
            visible[200] = False

    class CloseButton(GeometryControl):
        def GetInvokePattern(self):
            return InvokePattern()

        def Click(self, **_kwargs):
            raise AssertionError("a hidden reusable viewer must not be clicked twice")

    close = CloseButton((900, 0, 950, 50), "关闭", "mmui::XButton")
    close.ControlTypeName = "ButtonControl"
    root = GeometryControl(
        (0, 0, 1000, 800),
        "Weixin",
        "mmui::XView",
        children=[close],
    )

    @contextmanager
    def uia_thread():
        yield

    monkeypatch.setitem(
        sys.modules,
        "uiautomation",
        SimpleNamespace(
            UIAutomationInitializerInThread=uia_thread,
            ControlFromHandle=lambda _hwnd: root,
        ),
    )
    monkeypatch.setattr(win32api, "GetCursorPos", lambda: (10, 20))
    monkeypatch.setattr(
        win32api,
        "SetCursorPos",
        lambda point: calls.append(("restore", point)),
    )
    monkeypatch.setattr(win32gui, "IsWindow", lambda _hwnd: True)
    monkeypatch.setattr(
        win32gui, "IsWindowVisible", lambda hwnd: visible.get(hwnd, False)
    )

    assert WechatUiaClient({})._try_close_image_viewer_with_uia(200) is True
    assert calls == ["invoke", ("restore", (10, 20))]


def test_restore_after_viewer_targets_visible_non_minimized_main_window(
    monkeypatch,
):
    import win32con
    import win32gui

    foreground = {"hwnd": 200}
    main = {"visible": False, "iconic": True}
    calls = []

    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: hwnd in {100, 200})
    monkeypatch.setattr(
        win32gui,
        "IsWindowVisible",
        lambda hwnd: main["visible"] if hwnd == 100 else True,
    )
    monkeypatch.setattr(
        win32gui, "IsIconic", lambda hwnd: main["iconic"] if hwnd == 100 else False
    )
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: foreground["hwnd"])

    def show_window(hwnd, command):
        calls.append(("show", hwnd, command))
        main.update(visible=True, iconic=False)

    def set_foreground(hwnd):
        calls.append(("foreground", hwnd))
        foreground["hwnd"] = hwnd

    monkeypatch.setattr(win32gui, "ShowWindow", show_window)
    monkeypatch.setattr(
        win32gui,
        "BringWindowToTop",
        lambda hwnd: calls.append(("top", hwnd)),
    )
    monkeypatch.setattr(win32gui, "SetForegroundWindow", set_foreground)

    assert WechatUiaClient._restore_main_window_after_viewer(100) is True
    assert calls == [
        ("show", 100, win32con.SW_RESTORE),
        ("top", 100),
        ("foreground", 100),
    ]


def test_image_viewer_close_rejects_unverified_window(monkeypatch):
    import win32gui

    client = WechatUiaClient({})
    posted = []
    monkeypatch.setattr(client, "_is_image_viewer_window", lambda *_args: False)
    monkeypatch.setattr(
        client,
        "_try_close_image_viewer_with_uia",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("UIA must not touch an unverified window")
        ),
    )
    monkeypatch.setattr(
        win32gui,
        "PostMessage",
        lambda *args: posted.append(args),
    )

    assert client._close_image_viewer(200, 100) is False
    assert posted == []


def test_image_viewer_native_identity_rejects_main_and_other_process(monkeypatch):
    import win32gui
    import win32process

    client = WechatUiaClient({})
    alive = {100, 200, 300}
    process_ids = {100: 42, 200: 42, 300: 99}
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: hwnd in alive)
    monkeypatch.setattr(
        win32gui,
        "GetWindowText",
        lambda hwnd: "图片和视频" if hwnd in {200, 300} else "微信",
    )
    monkeypatch.setattr(
        win32process,
        "GetWindowThreadProcessId",
        lambda hwnd: (1, process_ids[hwnd]),
    )

    assert client._is_image_viewer_window(100, 100) is False
    assert client._is_image_viewer_window(300, 100) is False
    assert client._is_image_viewer_window(200, 100) is True


def test_dependency_failure_only_reactivates_when_wechat_left_foreground(
    monkeypatch,
):
    client = WechatUiaClient({})
    calls = []
    monkeypatch.setattr(client, "get_owner_window_handle", lambda: 100)
    monkeypatch.setattr(
        client,
        "ensure_foreground_window",
        lambda: calls.append("activate") or True,
    )
    monkeypatch.setattr(client, "_window_process_is_foreground", lambda _hwnd: True)

    assert client._recover_foreground_after_dependency_failure("viewer") is False
    assert calls == []

    monkeypatch.setattr(client, "_window_process_is_foreground", lambda _hwnd: False)
    assert client._recover_foreground_after_dependency_failure("viewer") is True
    assert calls == ["activate"]


def test_native_dialog_cancel_refuses_main_window_and_uses_exact_cancel_control(
    monkeypatch,
):
    import sys
    import win32gui
    import win32process

    client = WechatUiaClient({})
    calls = []

    class InvokePattern:
        def Invoke(self):
            calls.append("cancel")

    cancel = GeometryControl((0, 0, 20, 20), "取消", "Button")
    cancel.AutomationId = "2"
    cancel.ControlTypeName = "ButtonControl"
    cancel.GetInvokePattern = lambda: InvokePattern()
    root = GeometryControl((0, 0, 200, 200), "另存为", "#32770", [cancel])

    @contextmanager
    def uia_thread():
        yield

    monkeypatch.setitem(
        sys.modules,
        "uiautomation",
        SimpleNamespace(
            UIAutomationInitializerInThread=uia_thread,
            ControlFromHandle=lambda hwnd: root,
        ),
    )
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: hwnd in {100, 200})
    monkeypatch.setattr(
        win32gui,
        "GetClassName",
        lambda hwnd: "#32770" if hwnd == 200 else "WeChatMainWndForPC",
    )
    monkeypatch.setattr(
        win32process,
        "GetWindowThreadProcessId",
        lambda _hwnd: (1, 42),
    )

    assert client._try_cancel_verified_native_dialog(100, 100) is False
    assert calls == []
    assert client._try_cancel_verified_native_dialog(200, 100) is True
    assert calls == ["cancel"]


def test_image_viewer_close_prefers_uia_without_global_keyboard(monkeypatch):
    import win32gui

    client = WechatUiaClient({})
    alive = {100: True, 200: True}
    calls = []
    monkeypatch.setattr(client, "_is_image_viewer_window", lambda *_args: True)

    def close_with_uia(hwnd):
        calls.append(("uia", hwnd))
        alive[hwnd] = False
        return True

    monkeypatch.setattr(client, "_try_close_image_viewer_with_uia", close_with_uia)
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: alive.get(hwnd, False))
    monkeypatch.setattr(
        win32gui,
        "PostMessage",
        lambda *args: calls.append(("wm_close", *args)),
    )
    monkeypatch.setattr(
        client,
        "_press_escape_key",
        lambda: (_ for _ in ()).throw(
            AssertionError("image viewer must never use global Escape")
        ),
    )

    assert client._close_image_viewer(200, 100) is True
    assert calls == [("uia", 200)]


def test_image_viewer_close_never_falls_back_to_native_window_close(monkeypatch):
    import win32gui

    client = WechatUiaClient({})
    alive = {100: True, 200: True}
    posted = []
    monkeypatch.setattr(client, "_is_image_viewer_window", lambda *_args: True)
    monkeypatch.setattr(client, "_try_close_image_viewer_with_uia", lambda *_args: False)
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: alive.get(hwnd, False))

    monkeypatch.setattr(
        win32gui,
        "PostMessage",
        lambda *args: posted.append(args),
    )

    assert client._close_image_viewer(200, 100) is False
    assert alive == {100: True, 200: True}
    assert posted == []


def test_opened_image_viewer_requires_new_window_and_exact_uia_identity(monkeypatch):
    import win32gui

    client = WechatUiaClient({})
    visible = {100, 200, 300}
    in_enum_callback = False

    def enum_windows(callback, param):
        nonlocal in_enum_callback
        for hwnd in sorted(visible):
            in_enum_callback = True
            try:
                callback(hwnd, param)
            finally:
                in_enum_callback = False

    monkeypatch.setattr(win32gui, "EnumWindows", enum_windows)
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda hwnd: hwnd in visible)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 200)
    monkeypatch.setattr(
        client,
        "_is_image_viewer_window",
        lambda hwnd, main_hwnd: hwnd in {200, 300} and main_hwnd == 100,
    )
    monkeypatch.setattr(
        client,
        "_has_image_viewer_close_control",
        lambda hwnd: (
            (_ for _ in ()).throw(
                AssertionError("viewer UIA must run after EnumWindows returns")
            )
            if in_enum_callback
            else hwnd == 200
        ),
    )

    assert client._find_opened_image_viewer(100, {100, 300}) == 200


def test_opened_image_viewer_does_not_accept_same_process_popup(monkeypatch):
    import win32gui

    client = WechatUiaClient({})

    def enum_windows(callback, param):
        callback(100, param)
        callback(200, param)

    monkeypatch.setattr(win32gui, "EnumWindows", enum_windows)
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda _hwnd: True)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 200)
    monkeypatch.setattr(
        client,
        "_is_image_viewer_window",
        lambda _hwnd, _main_hwnd: False,
    )
    monkeypatch.setattr(
        client,
        "_has_image_viewer_close_control",
        lambda _hwnd: (_ for _ in ()).throw(
            AssertionError("non-viewer popup must not be inspected or closed")
        ),
    )

    assert client._find_opened_image_viewer(100, {100}) == 0


def test_image_capture_sequence_closes_viewer_and_restores_main(monkeypatch, tmp_path):
    import win32gui
    from PIL import Image, ImageGrab

    client = WechatUiaClient({"uia_image_viewer_enabled": True})
    message = UiaChatMessage(
        "Alice",
        "图片",
        message_type="image",
        runtime_id="image-1",
        bounds=(100, 100, 300, 260),
    )
    control = GeometryControl(
        (100, 100, 300, 260),
        "图片",
        "mmui::ChatImageItemView",
    )
    calls = []

    @contextmanager
    def fake_root():
        yield object()

    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_find_message_control", lambda *_args: control)
    monkeypatch.setattr(client, "get_owner_window_handle", lambda: 100)
    monkeypatch.setattr(
        client,
        "_visible_top_level_window_handles",
        lambda: {100},
    )
    monkeypatch.setattr(
        client,
        "_left_click_point",
        lambda _point: calls.append("click_image"),
    )
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)
    monkeypatch.setattr(
        client,
        "_find_opened_image_viewer",
        lambda main_hwnd, before: calls.append(
            ("verify_viewer", main_hwnd, before)
        )
        or 200,
    )
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda hwnd: (0, 0, 800, 600))
    monkeypatch.setattr(
        ImageGrab,
        "grab",
        lambda *, bbox, all_screens: calls.append(("grab", bbox, all_screens))
        or Image.new("RGB", (800, 600), "red"),
    )
    monkeypatch.setattr(
        client,
        "_close_image_viewer",
        lambda viewer_hwnd, main_hwnd: calls.append(
            ("close_viewer", viewer_hwnd, main_hwnd)
        )
        or True,
    )
    monkeypatch.setattr(
        client,
        "_restore_main_window_after_viewer",
        lambda main_hwnd: calls.append(("restore_main", main_hwnd)) or True,
    )
    monkeypatch.setattr(
        client,
        "_press_escape_key",
        lambda: (_ for _ in ()).throw(
            AssertionError("viewer flow must never send global Escape")
        ),
    )
    monkeypatch.setattr(
        win32gui,
        "PostMessage",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("viewer flow must never send WM_CLOSE")
        ),
    )

    image_path = client.fetch_message_image(message, tmp_path)

    assert Path(image_path).is_file()
    assert calls == [
        "click_image",
        ("verify_viewer", 100, {100}),
        ("grab", (0, 0, 800, 600), True),
        ("close_viewer", 200, 100),
        ("restore_main", 100),
    ]


def test_image_viewer_waits_for_configured_stability_before_close(
    monkeypatch, tmp_path
):
    import win32gui
    from PIL import Image, ImageGrab

    client = WechatUiaClient(
        {
            "uia_image_viewer_before_close_ms_min": 420,
            "uia_image_viewer_before_close_ms_max": 420,
        }
    )
    target = tmp_path / "viewer.png"
    calls = []
    monkeypatch.setattr(client, "get_owner_window_handle", lambda: 100)
    monkeypatch.setattr(client, "_visible_top_level_window_handles", lambda: {100})
    monkeypatch.setattr(client, "_left_click_point", lambda _point: None)

    def paced_wait(minimum_key, maximum_key, default_minimum, default_maximum):
        if minimum_key == "uia_image_viewer_before_close_ms_min":
            calls.append(
                (
                    "before_close_wait",
                    client.config[minimum_key],
                    client.config[maximum_key],
                    default_minimum,
                    default_maximum,
                )
            )

    monkeypatch.setattr(client, "_paced_wait", paced_wait)
    monkeypatch.setattr(client, "_find_opened_image_viewer", lambda *_args: 200)
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda _hwnd: (0, 0, 800, 600))
    monkeypatch.setattr(
        ImageGrab,
        "grab",
        lambda **_kwargs: Image.new("RGB", (800, 600), "red"),
    )
    monkeypatch.setattr(
        client,
        "_close_image_viewer",
        lambda *_args: calls.append("close") or True,
    )
    monkeypatch.setattr(client, "_restore_main_window_after_viewer", lambda _hwnd: True)

    assert client._capture_image_viewer_from_point((100, 200), target) == str(target)
    assert calls == [("before_close_wait", 420, 420, 300, 500), "close"]


def test_image_viewer_capture_is_not_successful_until_viewer_closes(
    monkeypatch, tmp_path
):
    import win32gui
    from PIL import Image, ImageGrab

    client = WechatUiaClient({})
    target = tmp_path / "viewer.png"
    calls = []
    monkeypatch.setattr(client, "get_owner_window_handle", lambda: 100)
    monkeypatch.setattr(client, "_visible_top_level_window_handles", lambda: {100})
    monkeypatch.setattr(client, "_left_click_point", lambda point: calls.append(point))
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)
    monkeypatch.setattr(client, "_find_opened_image_viewer", lambda *_args: 200)
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda _hwnd: (0, 0, 800, 600))
    monkeypatch.setattr(
        ImageGrab,
        "grab",
        lambda **_kwargs: Image.new("RGB", (800, 600), "red"),
    )
    monkeypatch.setattr(client, "_close_image_viewer", lambda *_args: False)
    monkeypatch.setattr(
        client,
        "_restore_main_window_after_viewer",
        lambda _hwnd: (_ for _ in ()).throw(
            AssertionError("main restore must wait for viewer close")
        ),
    )

    result = client._capture_image_viewer_from_point((100, 200), target)

    assert result == ""
    assert target.is_file()
    assert calls == [(100, 200)]


def test_image_activation_prefers_image_anchor_over_full_message_row():
    image = GeometryControl(
        (600, 120, 820, 280),
        "图片",
        "mmui::ChatImageItemView",
    )
    row = GeometryControl(
        (100, 100, 900, 300),
        "图片",
        "mmui::ChatItemView",
        children=[image],
    )

    bounds = WechatUiaClient._image_activation_bounds(
        row, (600, 120, 820, 280)
    )
    point = ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)

    assert bounds == (600, 120, 820, 280)
    assert 600 <= point[0] <= 820
    assert 120 <= point[1] <= 280


def test_file_message_type_prefers_uia_control_class_over_suffix():
    classify = WechatUiaClient._message_type

    assert classify("mmui::ChatFileItemView", "季度报告.xlsx 12 KB") == "file"
    assert classify("mmui::ChatBubbleItemView", "文件\n季度报告.xlsx\n12 KB\n微信电脑版") == "file"
    assert classify("mmui::ChatTextItemView", "请阅读 report.pdf") == "text"
    assert classify("mmui::ChatTextItemView", "please send the file") == "text"


def test_referenced_image_card_is_classified_from_wechat_uia_shape():
    classify = WechatUiaClient._message_type

    assert classify("mmui::ChatBubbleReferItemView", "图片") == "image"
    assert classify("mmui::ChatBubbleReferItemView", "Image") == "image"
    assert classify("mmui::ChatTextItemView", "图片") == "text"


def test_flattened_reference_label_separates_current_message_and_one_preview():
    parsed = WechatUiaClient._parse_reference_label(
        "测试引用引用 Alice 的消息 : Bob 引用 Carol 的消息 : 完整原文"
    )

    assert parsed == {
        "current": "测试引用",
        "sender": "Alice",
        "preview": "Bob 引用 Carol 的消息 : 完整原文",
    }


def test_reference_history_contains_only_the_single_original_message():
    reference = UiaReferencedMessage(
        "Alice",
        "完整原文",
        resolved=True,
        strategy="wechat_locate_original",
    )
    messages = [
        UiaChatMessage("Alice", "older", direction="incoming"),
        UiaChatMessage(
            "Alice",
            "当前问题",
            direction="incoming",
            reference=reference,
        ),
        UiaChatMessage("Alice", "newer", direction="incoming"),
    ]
    driver = WechatUiaDriver({}, client=FakeClient(), shell_hook=FakeHook())

    history = driver._history_snapshot(messages, 1, False)

    assert history == [
        {
            "sender_name": "",
            "content": "完整原文",
            "content_type": "text",
            "is_reference": True,
            "resolved": True,
            "degraded": False,
            "strategy": "wechat_locate_original",
        }
    ]


def test_reference_attachment_is_added_to_event_without_changing_current_type():
    reference = UiaReferencedMessage(
        "Alice",
        "图片",
        message_type="image",
        file_path="C:/tmp/reference.png",
        resolved=True,
        strategy="wechat_locate_original",
    )
    message = UiaChatMessage(
        "Alice",
        "这是什么",
        direction="incoming",
        reference=reference,
    )
    driver = WechatUiaDriver({}, client=FakeClient(), shell_hook=FakeHook())

    event = driver._event(
        "session", "Alice", message, [], False, False, 0, "target"
    )

    assert event.content_type == "text"
    assert event.content == "这是什么"
    assert event.evidence_path == "C:/tmp/reference.png"
    assert event.reference["depth"] == 1
    assert event.reference["file_path"] == "C:/tmp/reference.png"


def test_reference_context_renderer_uses_reference_and_ignores_history():
    event = WechatDesktopEvent(
        "message",
        "session",
        "Alice",
        "alice",
        "Alice",
        "text",
        "当前问题",
        history=[
            {
                "sender_name": "Bob",
                "content": "与引用无关的会话历史",
                "content_type": "text",
            }
        ],
        reference={
            "sender_name": "Alice",
            "content": "完整原文",
            "content_type": "text",
            "depth": 1,
        },
    )

    heading, lines = _render_event_context_lines(event)

    assert heading == "[被引用的内容]"
    assert lines == ["Alice: 完整原文"]
    assert "当前问题" not in "\n".join(lines)
    assert "与引用无关的会话历史" not in "\n".join(lines)


def test_reply_requirements_separate_reference_and_regular_context_rules():
    reference_event = WechatDesktopEvent(
        "message",
        "session",
        "Alice",
        "alice",
        "Alice",
        "text",
        "当前问题",
        reference={"content": "完整原文", "content_type": "text"},
    )
    regular_event = replace(reference_event, reference={})

    reference_requirements = _reply_requirements(reference_event)
    regular_requirements = _reply_requirements(regular_event)

    assert "只根据“被引用的内容”和“需要回复的引用消息”作答" in reference_requirements
    assert "引用之外的会话上下文" in reference_requirements
    assert "语义关联度" not in reference_requirements
    assert "只保留并使用关联度高" in regular_requirements
    assert "关联度低、已结束或属于其他话题的内容直接忽略" in regular_requirements


def test_group_prompt_rule_requires_explicit_member_mention():
    event = WechatDesktopEvent(
        "message",
        "group",
        "项目群",
        "alice",
        "Alice",
        "text",
        "李四怎么看",
        is_group=True,
    )

    requirements = _reply_requirements(event)

    assert "只有待回复消息明确使用“@成员”时" in requirements
    assert "没有 @ 时，不要仅因正文出现成员姓名就作此推断" in requirements


def test_group_bot_mention_is_removed_without_touching_other_members():
    content = "@颜料盒bot\u2005请看一下，@李四 也确认下；颜料盒bot 是正文"

    cleaned = _strip_group_bot_mentions(content, ["颜料盒bot"])

    assert cleaned == "请看一下，@李四 也确认下；颜料盒bot 是正文"


def test_regular_context_renderer_marks_history_as_filterable_candidates():
    event = WechatDesktopEvent(
        "message",
        "session",
        "Alice",
        "alice",
        "Alice",
        "text",
        "继续聊部署",
        history=[{"content": "昨天讨论部署"}, {"content": "午饭吃什么"}],
    )

    heading, lines = _render_event_context_lines(event)

    assert heading == "[候选会话上下文，需按关联度筛选]"
    assert lines == ["历史消息: 昨天讨论部署", "历史消息: 午饭吃什么"]


def test_dispatch_reference_prompt_excludes_unrelated_history():
    channel = _bare_wechat_channel()
    channel.config = {}
    channel._trace = lambda *_args, **_kwargs: None
    captured = {}

    def compose_context(ctype, content, **kwargs):
        captured.update(ctype=ctype, content=content, kwargs=kwargs)
        return None

    channel._compose_context = compose_context
    event = WechatDesktopEvent(
        "message",
        "session",
        "Alice",
        "alice",
        "Alice",
        "text",
        "这句话是什么意思？",
        history=[{"content": "无关的早餐话题"}],
        reference={
            "sender_name": "Bob",
            "content": "今晚发布",
            "content_type": "text",
        },
    )

    assert channel._dispatch_message(event) is False

    prompt = captured["content"]
    assert "[被引用的内容]" in prompt
    assert "Bob: 今晚发布" in prompt
    assert "[需要回复的引用消息]" in prompt
    assert "这句话是什么意思？" in prompt
    assert "无关的早餐话题" not in prompt
    assert "[候选会话上下文" not in prompt


def test_dispatch_regular_prompt_requests_context_relevance_filtering():
    channel = _bare_wechat_channel()
    channel.config = {}
    channel._trace = lambda *_args, **_kwargs: None
    captured = {}

    def compose_context(ctype, content, **kwargs):
        captured.update(ctype=ctype, content=content, kwargs=kwargs)
        return None

    channel._compose_context = compose_context
    event = WechatDesktopEvent(
        "message",
        "session",
        "Alice",
        "alice",
        "Alice",
        "text",
        "部署时间定了吗？",
        history=[{"content": "今晚发布"}, {"content": "中午吃面"}],
    )

    assert channel._dispatch_message(event) is False

    prompt = captured["content"]
    assert "[候选会话上下文，需按关联度筛选]" in prompt
    assert "今晚发布" in prompt
    assert "中午吃面" in prompt
    assert "只保留并使用关联度高" in prompt
    assert "关联度低、已结束或属于其他话题的内容直接忽略" in prompt


def test_dispatch_group_prompt_removes_bot_mention_before_agent():
    channel = _bare_wechat_channel()
    channel.config = {"self_display_name": ""}
    channel._trace = lambda *_args, **_kwargs: None
    captured = {}

    def compose_context(ctype, content, **kwargs):
        captured.update(ctype=ctype, content=content, kwargs=kwargs)
        return None

    channel._compose_context = compose_context
    event = WechatDesktopEvent(
        "message",
        "group",
        "项目群",
        "alice",
        "Alice",
        "text",
        "@颜料盒bot\u2005李四怎么看？ @王五 也说一下",
        is_group=True,
        is_at=True,
    )

    assert channel._dispatch_message(event) is False

    prompt = captured["content"]
    assert "@颜料盒bot" not in prompt
    assert "李四怎么看？" in prompt
    assert "@王五 也说一下" in prompt
    assert "只有待回复消息明确使用“@成员”时" in prompt


def test_referenced_image_clicks_quote_region_without_locating_original(monkeypatch):
    client = WechatUiaClient({})
    calls = []
    root_active = False
    reference_node = GeometryControl(
        (792, 483, 1632, 611),
        "这张图引用 颜料盒 的消息 : 图片",
        "mmui::ChatTextItemView",
    )
    root = GeometryControl((0, 0, 1800, 1000), children=[reference_node])
    quoted = UiaChatMessage(
        "Alice",
        "这张图",
        message_type="text",
        runtime_id="42.198746.4.-2147467003",
        bounds=(792, 483, 1632, 611),
        reference=UiaReferencedMessage(
            sender_name="颜料盒",
            content="图片",
            message_type="image",
        ),
    )

    @contextmanager
    def fake_root():
        nonlocal root_active
        root_active = True
        try:
            yield root
        finally:
            root_active = False

    def capture(point, _target):
        assert root_active is False
        calls.append(point)
        return "C:/tmp/quoted-viewer.png"

    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(
        client,
        "_capture_image_viewer_from_point",
        capture,
    )
    monkeypatch.setattr(
        client,
        "resolve_message_reference",
        lambda _message: (_ for _ in ()).throw(
            AssertionError("image references must not locate the original")
        ),
    )

    image_path = client.fetch_referenced_message_image(quoted)

    assert image_path == "C:/tmp/quoted-viewer.png"
    assert calls == [(1002, 580)]


def test_missing_locate_original_menu_never_sends_global_escape(monkeypatch):
    client = WechatUiaClient({})
    quoted = UiaChatMessage(
        "Alice",
        "看看这个",
        message_type="text",
        bounds=(100, 500, 500, 650),
        reference=UiaReferencedMessage(
            sender_name="Bob",
            content="report.pdf",
            message_type="file",
        ),
    )

    @contextmanager
    def fake_root():
        yield object()

    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_right_click_point", lambda _point: None)
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)
    monkeypatch.setattr(client, "_find_desktop_control", lambda *_args: None)
    monkeypatch.setattr(
        client,
        "_press_escape_key",
        lambda: (_ for _ in ()).throw(
            AssertionError("missing reference menu must not send global Escape")
        ),
    )

    assert client.resolve_message_reference(quoted) is quoted


def test_reply_target_image_requests_viewer_quality_capture():
    client = FakeClient()
    calls = []

    def fetch_image(message, prefer_viewer=True):
        calls.append(prefer_viewer)
        return "C:/tmp/viewer.png"

    client.fetch_message_image = fetch_image
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())
    message = UiaChatMessage(
        "Alice",
        "图片",
        message_type="image",
        bounds=(100, 100, 300, 260),
    )

    resolved, resolve_count = driver._resolve_reply_target_message("a", message)

    assert calls == [True]
    assert resolved.file_path == "C:/tmp/viewer.png"
    assert resolve_count == 1


def test_reply_target_skips_verified_outgoing_without_using_direction():
    for observed_direction in ("incoming", "outgoing", "unknown"):
        client = WechatUiaClient({"outgoing_echo_suppression_seconds": 300})
        client.remember_outgoing_message(
            "Alice", "机器人刚发的回复", "bot-runtime"
        )
        driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())
        messages = [
            UiaChatMessage(
                "",
                "对方的新消息",
                direction="unknown",
                runtime_id="user-runtime",
            ),
            UiaChatMessage(
                "",
                "机器人刚发的回复",
                direction=observed_direction,
                runtime_id="bot-runtime",
            ),
        ]

        index, target = driver._select_reply_target(
            messages, False, "", "session", "Alice"
        )

        assert index == 0
        assert target.content == "对方的新消息"


def test_private_scan_excludes_verified_outgoing_before_history_and_queue():
    client = WechatUiaClient({"outgoing_echo_suppression_seconds": 300})
    client.remember_outgoing_message(
        "Alice", "这题得请 pdf-reader skill 出场了", "notice-runtime"
    )
    client.remember_outgoing_message(
        "Alice", "机器人最终回复", "reply-runtime"
    )
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())
    incoming = UiaChatMessage(
        "Alice", "用户新消息", runtime_id="incoming-runtime"
    )
    notice = UiaChatMessage(
        "", "这题得请 pdf-reader skill 出场了", runtime_id="notice-runtime"
    )
    reply = UiaChatMessage(
        "", "机器人最终回复", runtime_id="reply-runtime"
    )

    filtered = driver._exclude_private_outgoing_messages(
        [incoming, notice, reply], "Alice", False
    )

    assert filtered == [incoming]
    assert driver._exclude_private_outgoing_messages(
        [incoming, notice, reply], "Alice", True
    ) == [incoming, notice, reply]


def test_event_fingerprint_does_not_depend_on_observed_direction():
    common = dict(
        kind="message",
        conversation_id="session",
        conversation_name="Alice",
        sender_id="alice",
        sender_name="Alice",
        content_type="text",
        content="hello",
    )
    incoming_event = WechatDesktopEvent(**common, direction="incoming")
    unknown_event = WechatDesktopEvent(**common, direction="unknown")

    assert incoming_event.fingerprint() == unknown_event.fingerprint()


def test_file_card_metadata_parses_wechat_binary_units():
    filename, size = WechatUiaClient._file_card_metadata(
        "文件\n季度报告.xlsx\n684.2K\n微信电脑版"
    )

    assert filename == "季度报告.xlsx"
    assert size == 700620


def test_cached_file_match_uses_displayed_size_and_accepts_duplicate_suffix(
    tmp_path, monkeypatch
):
    month = tmp_path / "wxid_example" / "msg" / "file" / "2026-07"
    month.mkdir(parents=True)
    older = month / "报告.pdf"
    current = month / "报告(1).pdf"
    older.write_bytes(b"x" * 712215)
    current.write_bytes(b"x" * 700636)
    client = WechatUiaClient({})
    monkeypatch.setattr(client, "_wechat_data_roots", lambda: [tmp_path])

    matched = client._find_cached_message_file("文件\n报告.pdf\n684.2K\n微信电脑版")

    assert matched == str(current)


def test_store_file_in_project_tmp_preserves_bytes_and_deduplicates(tmp_path):
    source = tmp_path / "source" / "报告.txt"
    source.parent.mkdir()
    source.write_bytes(b"wechat attachment")
    target_root = tmp_path / "tmp" / "wechat_files"

    first = WechatUiaClient._store_file_in_tmp(str(source), target_root)
    second = WechatUiaClient._store_file_in_tmp(str(source), target_root)

    assert first == second
    assert Path(first).parent == target_root
    assert Path(first).read_bytes() == b"wechat attachment"
    assert Path(first).name.endswith("_报告.txt")


def test_downloaded_file_event_becomes_file_context(tmp_path):
    attachment = tmp_path / "attachment.txt"
    attachment.write_text("hello", encoding="utf-8")
    event = WechatDesktopEvent(
        "message",
        "session",
        "Alice",
        "alice",
        "Alice",
        "file",
        str(attachment),
    )

    message = WechatDesktopMessage(event)

    assert message.ctype == ContextType.FILE
    assert message.content == str(attachment)


def test_message_sender_uses_ocr_direction_for_self_and_private_peer():
    owner = "当前账号"
    client = WechatUiaClient({"self_display_name": owner})
    header = HeaderInfo("颜料盒", "private", 1)
    assert client._message_sender(
        header, None, "对方消息", "incoming"
    ) == "颜料盒"
    assert client._message_sender(
        header, None, "自己的消息", "outgoing"
    ) == owner
    assert client._message_sender(
        header, None, "方向未知", "unknown"
    ) == "unknown"


def test_group_message_sender_is_deferred_to_rapidocr():
    member = GeometryControl((130, 100, 220, 120), "群成员甲", "Text")
    bubble = GeometryControl((120, 130, 420, 190), "群消息", "ChatTextBubble")
    row = GeometryControl(
        (100, 90, 900, 210), "群消息", "mmui::ChatTextItemView", [member, bubble]
    )

    assert (
        WechatUiaClient({})._message_sender(
            HeaderInfo("测试群", "group", 3), row, "群消息", "incoming"
        )
        == "unknown"
    )


def test_private_sender_names_are_derived_only_from_direction():
    client = WechatUiaClient({"self_display_name": "颜料盒bot"})
    messages = [
        UiaChatMessage("错误用户名", "收到", direction="incoming"),
        UiaChatMessage("", "发出", direction="outgoing"),
        UiaChatMessage("对方", "无法判断", direction="unknown"),
    ]

    normalized = client._normalize_private_senders(
        HeaderInfo("曹博淳", "private", 1), messages
    )

    assert [message.sender_name for message in normalized] == [
        "曹博淳",
        "颜料盒bot",
        "unknown",
    ]


def test_nested_group_member_uia_metadata_is_not_used_before_ocr():
    member = GeometryControl(
        (130, 100, 220, 120),
        "群成员乙",
        "mmui::ChatSenderNameLabel",
    )
    wrapper = GeometryControl((120, 90, 430, 200), "", "Wrapper", [member])
    row_control = GeometryControl(
        (100, 80, 900, 220),
        "群消息",
        "mmui::ChatTextItemView",
        [wrapper],
    )

    assert (
        WechatUiaClient({})._message_sender(
            HeaderInfo("测试群", "group", 3),
            row_control,
            "群消息",
            "incoming",
        )
        == "unknown"
    )


def test_rapidocr_matches_group_body_then_name_above_it():
    resolver = RapidOcrGroupSenderResolver({})
    message = UiaChatMessage(
        "",
        "请确认今晚部署",
        message_type="text",
        direction="incoming",
        bounds=(100, 90, 500, 190),
    )
    lines = [
        OcrTextLine("成员甲", 0.96, (140, 100, 200, 120)),
        OcrTextLine("请确认今晚部署", 0.99, (140, 132, 300, 156)),
        OcrTextLine("12:30", 0.99, (400, 60, 450, 80)),
    ]

    resolved = resolver.assign_senders([message], lines)

    assert resolved[0].sender_name == "成员甲"


def test_rapidocr_does_not_guess_between_ambiguous_sender_labels():
    resolver = RapidOcrGroupSenderResolver({})
    message = UiaChatMessage(
        "",
        "请确认今晚部署",
        message_type="text",
        direction="incoming",
        bounds=(100, 90, 500, 190),
    )
    lines = [
        OcrTextLine("成员甲", 0.95, (140, 101, 200, 121)),
        OcrTextLine("成员乙", 0.94, (141, 100, 201, 120)),
        OcrTextLine("请确认今晚部署", 0.99, (140, 132, 300, 156)),
    ]

    resolved = resolver.assign_senders([message], lines)

    assert resolved[0].sender_name == "unknown"


def test_rapidocr_falls_back_to_text_when_uia_bounds_use_another_dpi_scale():
    resolver = RapidOcrGroupSenderResolver({})
    message = UiaChatMessage(
        "",
        "我只是觉得好笑哈哈哈哈",
        message_type="text",
        direction="unknown",
        # Deliberately model UIA physical pixels while OCR uses logical pixels.
        bounds=(690, 450, 1010, 510),
    )
    lines = [
        OcrTextLine("杨昊", 0.985, (460, 270, 497, 292)),
        OcrTextLine("我只是觉得好笑哈哈哈哈", 1.0, (475, 308, 674, 331)),
    ]

    resolved = resolver.assign_senders(
        [message], lines, pane_bounds=(375, 100, 1095, 647)
    )

    assert resolved[0].sender_name == "杨昊"
    assert resolved[0].direction == "incoming"


def test_rapidocr_uses_body_side_to_resolve_outgoing_direction():
    resolver = RapidOcrGroupSenderResolver({})
    message = UiaChatMessage(
        "",
        "这是机器人发出的消息",
        message_type="text",
        direction="unknown",
        bounds=(100, 100, 900, 180),
    )
    lines = [
        OcrTextLine("这是机器人发出的消息", 0.99, (790, 120, 970, 145)),
    ]

    resolved = resolver.assign_senders(
        [message], lines, pane_bounds=(375, 100, 1095, 647)
    )

    assert resolved[0].sender_name == "自己"
    assert resolved[0].direction == "outgoing"


def test_rapidocr_output_uses_official_boxes_txts_scores_api():
    output = SimpleNamespace(
        boxes=[[[1, 2], [11, 2], [11, 8], [1, 8]]],
        txts=("成员甲",),
        scores=(0.98,),
    )

    lines = RapidOcrGroupSenderResolver._output_lines(output, (100, 200))

    assert lines == [OcrTextLine("成员甲", 0.98, (101, 202, 111, 208))]


def test_group_sender_name_survives_flattened_followup_snapshot():
    driver = WechatUiaDriver({}, client=FakeClient(), shell_hook=FakeHook())
    first = driver._stabilize_messages(
        "group",
        [UiaChatMessage("成员甲", "同一条消息", runtime_id="runtime-1")],
    )
    second = driver._stabilize_messages(
        "group",
        [UiaChatMessage("unknown", "同一条消息", runtime_id="runtime-1")],
    )

    assert first[0].sender_name == "成员甲"
    assert second[0].sender_name == "成员甲"
    assert second[0].stable_id == first[0].stable_id


def _selection_tree(active_title, messages, row):
    header = GeometryControl(
        (400, 100, 700, 130),
        active_title,
        "Text",
        automation_id="current_chat_name_label",
    )
    message_list = GeometryControl(
        (400, 140, 900, 700),
        class_name="List",
        children=messages,
        automation_id="chat_message_list",
    )
    root = GeometryControl((0, 0, 1000, 800), children=[header, message_list, row])
    return root, header, message_list


def test_locate_conversation_does_not_click_populated_active_chat(monkeypatch):
    row = ClickableGeometryControl(
        (0, 100, 300, 160),
        "颜料盒",
        "mmui::ChatSessionCell",
        automation_id="session_item_颜料盒",
    )
    message = GeometryControl(
        (400, 200, 900, 260), "已有消息", "mmui::ChatTextItemView"
    )
    root, _, _ = _selection_tree("颜料盒", [message], row)
    client = WechatUiaClient({})

    @contextmanager
    def fake_root():
        yield root

    monkeypatch.setattr(client, "_uia_root", fake_root)

    assert client.locate_conversation("颜料盒") is True
    assert row.click_count == 0


def test_locate_conversation_clicks_when_active_chat_is_blank(monkeypatch):
    message = GeometryControl(
        (400, 200, 900, 260), "恢复后的消息", "mmui::ChatTextItemView"
    )
    row = None

    def activate():
        header.Name = "颜料盒"
        message_list._children[:] = [message]

    row = ClickableGeometryControl(
        (0, 100, 300, 160),
        "颜料盒",
        "mmui::ChatSessionCell",
        automation_id="session_item_颜料盒",
        on_click=activate,
    )
    root, header, message_list = _selection_tree("颜料盒", [], row)
    client = WechatUiaClient({})

    @contextmanager
    def fake_root():
        yield root

    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_paced_wait", lambda *args, **kwargs: None)

    assert client.locate_conversation("颜料盒") is True
    assert row.click_count == 1


class FakeClient:
    def __init__(self):
        self.operation_lock = threading.RLock()
        self.rows = []
        self.histories = {}
        self.headers = {}
        self.history_calls = []
        self.history_ensure_conversation = []
        self.focus_calls = 0
        self.owner_calls = 0
        self.seeded_outgoing = {}
        self.file_paths = {}
        self.file_fetches = []
        self.image_paths = {}
        self.image_fetches = []

    def allowed_process_ids(self):
        return {42}

    def get_owner_info(self):
        self.owner_calls += 1
        return OwnerInfo("小牛", source="config")

    def get_owner_window_process_id(self):
        return 42

    def probe_tree(self):
        return 10

    def focus_window(self):
        self.focus_calls += 1

    def get_visible_conversations(self):
        return list(self.rows)

    def locate_conversation(self, name, runtime_id="", row_index=-1):
        self.selected = name
        self.selected_key = runtime_id or name
        return self.selected_key in self.headers or name in self.headers

    def get_title(self):
        name = getattr(self, "selected", "")
        if name:
            key = getattr(self, "selected_key", "")
            return self.headers[key] if key in self.headers else self.headers[name]
        return next(iter(self.headers.values()), HeaderInfo("", "unknown", 1))

    def get_chat_history(
        self,
        name=None,
        limit=5,
        runtime_id="",
        row_index=-1,
        ensure_conversation=True,
    ):
        if name:
            self.selected = name
            self.selected_key = runtime_id or name
        self.history_calls.append(name)
        self.history_ensure_conversation.append(ensure_conversation)
        return list(self.histories.get(runtime_id or name, []))[-limit:]

    def fetch_message_file(self, message):
        self.file_fetches.append(message.content)
        return self.file_paths.get(message.content, "")

    def fetch_message_image(self, message):
        self.image_fetches.append(message.content)
        return self.image_paths.get(message.content, "")

    def seed_outgoing_texts(self, conversation, texts):
        self.seeded_outgoing[conversation] = list(texts)

    def send_message(self, who, text, runtime_id="", row_index=-1):
        return {"success": True, "verified": True}

    def send_file(self, who, files, runtime_id="", row_index=-1):
        return {"success": True, "verified": True}


class FakeHook:
    available = True
    error = ""

    def start(self):
        return True

    def close(self):
        pass


def row(
    name,
    unread=1,
    mention=False,
    signature="new",
    runtime_id="",
    row_index=0,
    preview_prefix=False,
):
    return ConversationInfo(
        name,
        not_read_number=unread,
        mentions_self=mention,
        row_signature=signature,
        automation_id=f"session_item_{name}",
        runtime_id=runtime_id,
        row_index=row_index,
        preview_sender="成员" if preview_prefix else "",
        preview_has_sender_prefix=preview_prefix,
    )


def incoming(content, runtime_id):
    return UiaChatMessage(
        "成员",
        content,
        direction="incoming",
        runtime_id=runtime_id,
    )


def outgoing(content, runtime_id):
    return UiaChatMessage(
        "小牛",
        content,
        direction="outgoing",
        runtime_id=runtime_id,
    )


def test_session_parser_redacts_preview_and_reads_markers():
    parsed = parse_session_accessible_name(
        "项目群",
        "项目群\n[3条] 项目成员: secret [有人@我]\n23:31\n已置顶\n消息免打扰",
    )
    assert parsed.conversation_title == "项目群"
    assert parsed.not_read_number == 3
    assert parsed.mentions_self is True
    assert parsed.is_top is True
    assert parsed.is_do_not_disturb is True
    assert "secret" not in parsed.row_signature
    assert parsed.preview_sender == "项目成员"
    assert parsed.preview_has_sender_prefix is True


def test_session_parser_mention_marker_can_appear_inside_preview():
    parsed = parse_session_accessible_name(
        "测试群",
        "测试群\n成员: 这是一条模拟消息 [有人@我] 后面仍有内容\n12:45",
        "session_item_测试群",
    )

    assert parsed.mentions_self is True


def test_session_preview_without_sender_prefix_is_not_marked_as_incoming_sender():
    parsed = parse_session_accessible_name(
        "项目群", "项目群\n[1条] 我刚发送的消息\n23:59"
    )
    assert parsed.preview_sender == ""
    assert parsed.preview_has_sender_prefix is False


def test_group_selects_only_latest_mention_and_uses_other_bubbles_as_history():
    client = FakeClient()
    client.rows = [row("项目群", unread=3, mention=True, preview_prefix=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [
        incoming("@小牛 第一条", "1"),
        incoming("这是后续普通消息", "2"),
        incoming("@小牛 第二条", "3"),
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True},
        client=client,
        shell_hook=FakeHook(),
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["@小牛 第二条"]
    assert events[0].is_at is True
    assert client.owner_calls >= 1
    assert [item["content"] for item in events[0].history] == [
        "@小牛 第一条",
        "这是后续普通消息",
    ]


def test_known_group_without_mention_never_reads_history():
    client = FakeClient()
    client.rows = [row("项目群", unread=2, mention=False)]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True, "auto_reply_groups": ["项目群"]},
        client=client,
        shell_hook=FakeHook(),
    )

    _, events = driver.observe_events()

    assert events == []
    assert client.history_calls == []


def test_group_mention_followed_by_normal_message_replies_only_to_mention():
    client = FakeClient()
    client.rows = [row("项目群", unread=2, mention=True, preview_prefix=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [
        incoming("@小牛 请确认", "1"),
        incoming("后续普通消息", "2"),
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["@小牛 请确认"]
    assert [item["content"] for item in events[0].history] == ["后续普通消息"]


def test_group_marker_without_locatable_mention_fails_closed():
    client = FakeClient()
    client.rows = [row("项目群", unread=1, mention=True, preview_prefix=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [incoming("普通消息", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert events == []


def test_group_selects_first_at_message_without_using_direction():
    client = FakeClient()
    client.rows = [row("项目群", unread=0, mention=True, preview_prefix=False)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [
        incoming("@小牛 请确认", "1"),
        outgoing("已确认", "2"),
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["@小牛 请确认"]
    assert [item["content"] for item in events[0].history] == ["已确认"]


def test_same_name_conversations_use_runtime_id_instead_of_title():
    client = FakeClient()
    client.rows = [
        row("同名用户", runtime_id="runtime-1", row_index=0, signature="a"),
        row("同名用户", runtime_id="runtime-2", row_index=1, signature="b"),
    ]
    client.headers["runtime-1"] = HeaderInfo("同名用户", "private", 1)
    client.headers["runtime-2"] = HeaderInfo("同名用户", "private", 1)
    client.histories["runtime-1"] = [incoming("first account", "m1")]
    client.histories["runtime-2"] = [incoming("second account", "m2")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["first account", "second account"]
    assert len({event.conversation_id for event in events}) == 2
    assert all(event.conversation_name == "同名用户" for event in events)


def test_private_burst_selects_only_latest_and_keeps_earlier_messages_as_history():
    client = FakeClient()
    client.rows = [row("Alice", unread=3)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        incoming("one", "1"),
        incoming("two", "2"),
        incoming("three", "3"),
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["three"]
    assert [item["content"] for item in events[0].history] == ["one", "two"]
    assert all(item["sender_name"] == "" for item in events[0].history)


def test_private_file_target_is_fetched_and_event_uses_local_path(tmp_path):
    local_file = tmp_path / "tmp" / "wechat_files" / "report.pdf"
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"%PDF-test")
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage(
            "Alice",
            "report.pdf 8 KB",
            message_type="file",
            direction="incoming",
            runtime_id="file-1",
        )
    ]
    client.file_paths["report.pdf 8 KB"] = str(local_file)
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert client.file_fetches == []
    assert len(events) == 1
    assert events[0].content_type == "file"
    assert events[0].content == "report.pdf 8 KB"

    materialized, resolve_count = driver.materialize_event(events[0])

    assert client.file_fetches == ["report.pdf 8 KB"]
    assert resolve_count == 1
    assert materialized.content_type == "file"
    assert materialized.content == str(local_file)


def test_private_image_target_is_captured_and_event_uses_local_path(tmp_path):
    local_image = tmp_path / "tmp" / "wechat_images" / "photo.png"
    local_image.parent.mkdir(parents=True)
    local_image.write_bytes(b"png")
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage(
            "Alice",
            "[图片]",
            message_type="image",
            direction="incoming",
            runtime_id="image-1",
            bounds=(100, 100, 300, 260),
        )
    ]
    client.image_paths["[图片]"] = str(local_image)
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert client.image_fetches == []
    assert len(events) == 1
    assert events[0].content_type == "image"

    materialized, resolve_count = driver.materialize_event(events[0])

    assert client.image_fetches == ["[图片]"]
    assert resolve_count == 1
    assert materialized.content == str(local_image)
    assert materialized.evidence_path == str(local_image)


def test_attachment_history_keeps_previous_and_excludes_all_followups():
    messages = [incoming(f"before-{index}", str(index)) for index in range(6)]
    messages.append(
        UiaChatMessage(
            "Alice",
            "[图片]",
            message_type="image",
            direction="incoming",
            runtime_id="image-target",
            file_path="C:/tmp/wechat_images/photo.png",
        )
    )
    messages.extend(incoming(f"after-{index}", f"a{index}") for index in range(6))
    driver = WechatUiaDriver({}, client=FakeClient(), shell_hook=FakeHook())

    history = driver._history_snapshot(messages, 6, False)

    assert [item["content"] for item in history] == [
        "before-0",
        "before-1",
        "before-2",
        "before-3",
        "before-4",
        "before-5",
    ]


def test_private_text_target_does_not_resolve_preceding_visible_file(tmp_path):
    local_file = tmp_path / "tmp" / "wechat_files" / "report.pdf"
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"%PDF-test")
    client = FakeClient()
    client.rows = [row("Alice", unread=2)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage(
            "Alice",
            "文件\nreport.pdf\n8 KB\n微信电脑版",
            message_type="file",
            direction="incoming",
            runtime_id="file-1",
        ),
        incoming("读取这个文件", "text-1"),
    ]
    client.file_paths["文件\nreport.pdf\n8 KB\n微信电脑版"] = str(local_file)
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()
    materialized, resolve_count = driver.materialize_event(events[0])

    assert [event.content for event in events] == ["读取这个文件"]
    assert resolve_count == 0
    assert client.file_fetches == []
    assert materialized.history[0]["content_type"] == "file"
    assert str(local_file) not in materialized.history[0]["content"]


def test_private_message_after_outgoing_is_the_only_reply_target():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        incoming("old", "1"),
        outgoing("answered", "2"),
        incoming("new", "3"),
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["new"]
    assert [item["content"] for item in events[0].history] == ["old", "answered"]
    assert all("direction" not in item for item in events[0].history)


def test_private_latest_outgoing_means_nothing_needs_reply():
    client = FakeClient()
    client.rows = [row("Alice", unread=0)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("question", "1"), outgoing("reply", "2")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert events == []


def test_unknown_geometry_is_never_coerced_to_incoming():
    client = FakeClient()
    client.rows = [row("Alice", unread=0)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage("", "ambiguous", direction="unknown", runtime_id="1")
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert events == []


def test_private_unread_marker_classifies_only_latest_unknown_bubble():
    client = FakeClient()
    client.rows = [row("Alice", unread=2)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage("", "older", direction="unknown", runtime_id="1"),
        UiaChatMessage("", "newest", direction="unknown", runtime_id="2"),
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["newest"]
    assert [item["content"] for item in events[0].history] == ["older"]
    assert "direction" not in events[0].history[0]


def test_private_context_keeps_every_visible_message_before_reply_target():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        incoming(f"context-{index}", f"runtime-{index}")
        for index in range(12)
    ] + [incoming("reply-target", "runtime-target")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["reply-target"]
    assert [item["content"] for item in events[0].history] == [
        f"context-{index}" for index in range(12)
    ]


def test_private_row_change_without_unread_does_not_open_or_enqueue_chat():
    client = FakeClient()
    client.rows = [row("Alice", unread=0, signature="changed")]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("already-read", "runtime-1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert events == []
    assert client.history_calls == []


def test_startup_processes_private_unread_from_session_list_by_default():
    client = FakeClient()
    client.rows = [row("Alice", unread=3, signature="startup")]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        incoming("older unread", "1"),
        incoming("latest unread", "2"),
    ]
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["latest unread"]
    assert events[0].session_unread_count == 3
    assert events[0].history[-1]["content"] == "older unread"


def test_startup_does_not_open_private_session_without_unread_marker():
    client = FakeClient()
    client.rows = [row("Alice", unread=0, signature="startup")]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("already read", "1")]
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())

    _, events = driver.observe_events()

    assert events == []
    assert client.history_calls == []


def test_startup_unread_processing_can_be_disabled():
    client = FakeClient()
    client.rows = [row("Alice", unread=2, signature="startup")]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("unread", "1")]
    driver = WechatUiaDriver(
        {"process_startup_unread_messages": False},
        client=client,
        shell_hook=FakeHook(),
    )

    _, events = driver.observe_events()

    assert events == []
    assert client.history_calls == []


def test_reply_target_key_does_not_use_recyclable_runtime_id():
    first = incoming("same message", "runtime-a")
    second = incoming("same message", "runtime-b")

    assert WechatUiaDriver._target_key(first, 3) == WechatUiaDriver._target_key(
        second, 3
    )


def test_reused_runtime_id_produces_new_private_message_event():
    client = FakeClient()
    client.rows = [row("Alice", unread=1, signature="first")]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("first", "recycled-runtime")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, first_events = driver.observe_events()
    client.rows = [row("Alice", unread=1, signature="second")]
    client.histories["Alice"] = [incoming("second", "recycled-runtime")]
    _, second_events = driver.observe_events()

    assert [event.content for event in first_events] == ["first"]
    assert [event.content for event in second_events] == ["second"]
    assert first_events[0].event_id != second_events[0].event_id


def test_reply_queue_is_global_fifo_and_rejects_duplicates():
    reply_queue = WechatReplyQueue()
    second = WechatDesktopEvent(
        "message", "b", "Bob", "b", "Bob", "text", "second", observed_at=2
    )
    first = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "first", observed_at=1
    )
    assert reply_queue.enqueue_many([second, first, first]) == 2
    first_item = reply_queue.get()
    assert first_item.event.content == "first"
    reply_queue.finish(first_item, "completed")
    second_item = reply_queue.get()
    assert second_item.event.content == "second"
    reply_queue.finish(second_item, "completed")
    assert reply_queue.status()["queue_completed"] == 2
    assert "queue_approval" not in reply_queue.status()


def test_reply_queue_appends_pending_target_from_same_conversation():
    reply_queue = WechatReplyQueue()
    old = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "old"
    )
    new = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "new"
    )

    assert reply_queue.enqueue(old).action == "added"
    assert reply_queue.enqueue(new).action == "added"
    first = reply_queue.get()
    assert first.event is old
    reply_queue.finish(first, "completed")
    assert reply_queue.get().event is new


def test_reply_queue_captures_enqueue_time_context_independently():
    reply_queue = WechatReplyQueue()
    event = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "待回复消息",
        history=[{"content": "入队时的上文", "content_type": "text"}],
    )

    assert reply_queue.enqueue(event)
    event.history[0]["content"] = "入队后被修改"
    event.history.append({"content": "消费前出现的新消息"})

    item = reply_queue.get()

    assert item.context_history == [
        {"content": "入队时的上文", "content_type": "text"}
    ]
    item.restore_context()
    assert item.event.content == "待回复消息"
    assert item.event.history == [
        {"content": "入队时的上文", "content_type": "text"}
    ]


def test_reply_queue_private_followup_does_not_expire_active():
    reply_queue = WechatReplyQueue()
    old = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "old"
    )
    new = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "new"
    )
    reply_queue.enqueue(old)
    active = reply_queue.get()

    result = reply_queue.enqueue(new)

    assert result.action == "added"
    assert active.expired is False
    assert active.terminal == ""
    assert reply_queue.is_relevant(active.token, "a") is True
    assert reply_queue.is_relevant(active.token, "b") is True
    reply_queue.finish(active, "completed")
    assert reply_queue.get().event.content == "new"
    assert reply_queue.status()["queue_superseded"] == 0


def test_reply_queue_preserves_multiple_private_followups():
    reply_queue = WechatReplyQueue()
    first = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "first"
    )
    second = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "second"
    )
    third = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "third"
    )
    reply_queue.enqueue(first)
    active = reply_queue.get()

    assert reply_queue.enqueue(second).action == "added"
    assert reply_queue.enqueue(third).action == "added"
    reply_queue.finish(active, "completed")
    second_item = reply_queue.get()
    assert second_item.event is second
    reply_queue.finish(second_item, "completed")
    assert reply_queue.get().event is third


def test_reply_queue_appends_group_mentions_without_canceling_active():
    reply_queue = WechatReplyQueue()
    first = WechatDesktopEvent(
        "message", "g", "项目群", "a", "成员A", "text", "@小牛 first", is_group=True
    )
    second = WechatDesktopEvent(
        "message", "g", "项目群", "b", "成员B", "text", "@小牛 second", is_group=True
    )
    third = WechatDesktopEvent(
        "message", "g", "项目群", "c", "成员C", "text", "@小牛 third", is_group=True
    )
    reply_queue.enqueue(first)
    active = reply_queue.get()

    assert reply_queue.enqueue(second).action == "added"
    assert reply_queue.enqueue(third).action == "added"
    assert active.expired is False
    assert active.done.is_set() is False
    reply_queue.finish(active, "completed")

    next_item = reply_queue.get()
    assert next_item.event.content == "@小牛 second"
    reply_queue.finish(next_item, "completed")
    assert reply_queue.get().event.content == "@小牛 third"


def test_reply_queue_ignores_legacy_burst_limit_and_keeps_global_fifo():
    reply_queue = WechatReplyQueue(conversation_burst_limit=2)
    first = WechatDesktopEvent(
        "message", "g", "项目群", "a", "成员A", "text", "@小牛 1", is_group=True
    )
    second = WechatDesktopEvent(
        "message", "g", "项目群", "b", "成员B", "text", "@小牛 2", is_group=True
    )
    third = WechatDesktopEvent(
        "message", "g", "项目群", "c", "成员C", "text", "@小牛 3", is_group=True
    )
    other = WechatDesktopEvent(
        "message", "p", "Bob", "p", "Bob", "text", "hello"
    )
    reply_queue.enqueue(first)
    active = reply_queue.get()
    reply_queue.enqueue(other)
    reply_queue.enqueue(second)
    reply_queue.finish(active, "completed")
    active = reply_queue.get()
    assert active.event is other
    reply_queue.enqueue(third)
    reply_queue.finish(active, "completed")

    second_item = reply_queue.get()

    assert second_item.event is second
    reply_queue.finish(second_item, "completed")
    assert reply_queue.get().event is third


def test_reply_queue_can_clear_global_and_active_conversation_pending_work():
    reply_queue = WechatReplyQueue()
    active_event = WechatDesktopEvent(
        "message", "g", "项目群", "a", "成员A", "text", "@小牛 1", is_group=True
    )
    local_event = WechatDesktopEvent(
        "message", "g", "项目群", "b", "成员B", "text", "@小牛 2", is_group=True
    )
    global_event = WechatDesktopEvent(
        "message", "p", "Bob", "p", "Bob", "text", "hello"
    )
    reply_queue.enqueue(active_event)
    active = reply_queue.get()
    reply_queue.enqueue(local_event)
    reply_queue.enqueue(global_event)

    discarded = reply_queue.clear_pending()

    assert set(discarded) == {local_event.event_id, global_event.event_id}
    assert reply_queue.status()["queue_depth"] == 0
    assert reply_queue.is_active(active.token) is True
    reply_queue.finish(active, "failed")
    assert reply_queue.get(timeout=0.01) is None


def test_network_reply_error_detection():
    assert _is_network_reply_error(
        "Agent error: Connection error: SSL: UNEXPECTED_EOF_WHILE_READING"
    )
    assert _is_network_reply_error("request timed out")
    assert not _is_network_reply_error("invalid tool arguments")


def test_driver_private_revalidation_keeps_started_target_relevant():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("old", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, events = driver.observe_events()
    client.histories["Alice"] = [incoming("old", "1"), incoming("new", "2")]

    validation = driver.validate_reply_target(events[0])
    valid, reason = validation

    assert valid is True
    assert reason == ""
    assert validation.replacement_event is None


def test_reply_cycle_monitors_private_conversation_without_unread_marker():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("old", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, first_events = driver.observe_events()
    discovery_focus_calls = client.focus_calls
    driver.begin_reply_cycle("Alice", first_events[0].conversation_id)
    client.rows = [row("Alice", unread=0, signature="unchanged")]
    client.histories["Alice"] = [incoming("old", "1"), incoming("new", "2")]

    _, monitored_events = driver.observe_events()

    assert [event.content for event in monitored_events] == ["new"]
    assert client.focus_calls == discovery_focus_calls
    assert client.history_ensure_conversation[-1] is False


def test_discovery_focus_failure_backs_off_without_failing_observation():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)

    def deny_foreground():
        client.focus_calls += 1
        raise OSError(5, "SetForegroundWindow", "拒绝访问。")

    client.focus_window = deny_foreground
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())

    first_observation, first_events = driver.observe_events()
    second_observation, second_events = driver.observe_events()

    assert first_observation["error"] == ""
    assert second_observation["error"] == ""
    assert first_events == []
    assert second_events == []
    assert client.focus_calls == 1
    assert client.history_calls == []


def test_reply_cycle_ignores_interim_tool_notice_but_still_finds_followup():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("old", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, first_events = driver.observe_events()
    original = first_events[0]
    driver.begin_reply_cycle("Alice", original.conversation_id)
    notice = "我准备调用 `web_search` tool 查一查，稍等一下 🔧"
    driver.register_interim_text(original.conversation_id, notice)
    client.rows = [row("Alice", unread=0, signature="unchanged")]
    client.histories["Alice"] = [
        incoming("old", "1"),
        outgoing(notice, "2"),
    ]

    _, notice_events = driver.observe_events()

    assert notice_events == []
    assert driver.validate_reply_target(original).valid is True

    client.histories["Alice"].append(incoming("new", "3"))
    _, followup_events = driver.observe_events()

    assert [event.content for event in followup_events] == ["new"]
    assert notice not in [
        item["content"] for item in followup_events[0].history
    ]


def test_tool_notice_subject_recognizes_skill_reads_and_regular_tools():
    assert _tool_notice_subject(
        {
            "tool_name": "read",
            "arguments": {"path": r"C:\cow\skills\web-search\SKILL.md"},
        }
    ) == ("skill", "web-search")
    assert _tool_notice_subject(
        {
            "tool_name": "read",
            "arguments": '{"location":"skills/vision/SKILL.md"}',
        }
    ) == ("skill", "vision")
    assert _tool_notice_subject(
        {"tool_name": "web_search", "arguments": {"query": "天气"}}
    ) == ("tool", "web_search")


def test_tool_notice_templates_always_output_the_concrete_tool_name():
    assert all(
        "{tool_name}" in template
        for template in DEFAULT_CONFIG["agent_tool_notice_templates"]
    )
    assert (
        _format_agent_notice(
            ["我准备调用 `{tool_name}` tool"], "tool", "web_search"
        )
        == "我准备调用 `web_search` tool"
    )
    assert (
        _format_agent_notice(["我正在使用工具，请稍等"], "tool", "vision")
        == "我正在使用工具，请稍等 当前工具：`vision`。"
    )
    assert (
        _format_agent_notice(["调用 `{name}` skill"], "skill", "pdf-reader")
        == "调用 `pdf-reader` skill"
    )


def test_preflight_notice_predicts_attachment_tools_before_llm_turn():
    docx_event = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "file", r"C:\tmp\LDAP.docx"
    )
    image_reference_event = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "inspect",
        reference={"content_type": "image", "file_path": r"C:\tmp\quoted.png"},
    )
    unresolved_image_reference = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "inspect",
        reference={"content_type": "image"},
    )
    unresolved_file_reference = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "inspect",
        reference={"content_type": "file"},
    )
    pdf_reference = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "inspect",
        reference={"content_type": "file", "file_path": r"C:\tmp\quoted.pdf"},
    )
    text_event = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "hello"
    )

    assert _tool_notice_subject(_preflight_tool_notice_data(docx_event)) == (
        "skill",
        "docx",
    )
    assert _tool_notice_subject(
        _preflight_tool_notice_data(image_reference_event)
    ) == ("tool", "vision")
    assert _preflight_tool_notice_data(unresolved_image_reference) is None
    assert _preflight_tool_notice_data(unresolved_file_reference) is None
    assert _tool_notice_subject(
        _preflight_tool_notice_data(pdf_reference)
    ) == ("skill", "pdf-reader")
    assert _preflight_tool_notice_data(text_event) is None


def test_reply_cycle_monitors_group_without_session_mention_marker():
    client = FakeClient()
    client.rows = [row("项目群", unread=1, mention=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [incoming("@小牛 old", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, first_events = driver.observe_events()
    driver.begin_reply_cycle("项目群", first_events[0].conversation_id)
    client.rows = [row("项目群", unread=0, mention=False, signature="unchanged")]
    client.histories["项目群"] = [
        incoming("@小牛 old", "1"),
        incoming("@小牛 new", "2"),
    ]

    _, monitored_events = driver.observe_events()

    assert [event.content for event in monitored_events] == ["@小牛 new"]


def test_reply_cycle_changes_wait_deadline_to_monitor_interval():
    class IdleHook(FakeHook):
        def wait(self, timeout):
            return False

    driver = WechatUiaDriver(
        {
            "shell_hook_reconcile_seconds": 15,
            "reply_monitor_interval_seconds": 0.25,
        },
        client=FakeClient(),
        shell_hook=IdleHook(),
    )
    driver.begin_reply_cycle("Alice", "uia-session:alice")

    started = time.monotonic()
    reason = driver.wait_for_changes(threading.Event())

    assert reason == "reply-monitor"
    assert time.monotonic() - started < 1.0


def test_global_reconcile_scan_is_disabled_by_default(monkeypatch):
    stop_event = threading.Event()

    class StopHook(FakeHook):
        def wait(self, timeout):
            stop_event.set()
            return False

    driver = WechatUiaDriver(
        {"shell_hook_reconcile_seconds": 1},
        client=FakeClient(),
        shell_hook=StopHook(),
    )
    monkeypatch.setattr(
        "channel.wechat_desktop.uia_driver.time.monotonic", lambda: 1000.0
    )

    assert driver.wait_for_changes(stop_event) == "stopped"


def test_global_reconcile_scan_can_be_enabled(monkeypatch):
    class IdleHook(FakeHook):
        def wait(self, timeout):
            return False

    ticks = iter([0.0, 2.0, 2.0])
    monkeypatch.setattr(
        "channel.wechat_desktop.uia_driver.time.monotonic", lambda: next(ticks)
    )
    driver = WechatUiaDriver(
        {
            "shell_hook_reconcile_enabled": True,
            "shell_hook_reconcile_seconds": 1,
        },
        client=FakeClient(),
        shell_hook=IdleHook(),
    )

    assert driver.wait_for_changes(threading.Event()) == "reconcile"


def test_reply_target_key_ignores_bounds_changes():
    first = UiaChatMessage(
        "Alice", "same", bounds=(10, 20, 100, 60), runtime_id="1"
    )
    moved = UiaChatMessage(
        "Alice", "same", bounds=(30, 40, 120, 80), runtime_id="1"
    )

    assert WechatUiaDriver._target_key(first, 3) == WechatUiaDriver._target_key(
        moved, 3
    )


def test_reply_target_key_ignores_visible_index_changes():
    message = UiaChatMessage("Alice", "same", runtime_id="42.1")

    assert WechatUiaDriver._target_key(message, 4) == WechatUiaDriver._target_key(
        message, 0
    )


def test_reply_target_key_distinguishes_identical_messages_by_stable_id():
    first = UiaChatMessage("Alice", "same", runtime_id="42.1", stable_id="first")
    second = UiaChatMessage("Alice", "same", runtime_id="42.2", stable_id="second")

    assert WechatUiaDriver._target_key(first, 4) != WechatUiaDriver._target_key(
        second, 4
    )


def test_message_snapshot_keeps_identity_when_runtime_id_changes():
    driver = WechatUiaDriver({}, client=FakeClient(), shell_hook=FakeHook())
    first = driver._stabilize_messages(
        "conversation", [incoming("same", "runtime-a")]
    )
    recreated = driver._stabilize_messages(
        "conversation", [incoming("same", "runtime-b")]
    )

    assert recreated[0].stable_id == first[0].stable_id


def test_message_snapshot_distinguishes_new_identical_message():
    driver = WechatUiaDriver({}, client=FakeClient(), shell_hook=FakeHook())
    first = driver._stabilize_messages(
        "conversation", [incoming("same", "runtime-a")]
    )
    next_snapshot = driver._stabilize_messages(
        "conversation",
        [incoming("same", "runtime-a"), incoming("same", "runtime-b")],
    )

    assert next_snapshot[0].stable_id == first[0].stable_id
    assert next_snapshot[1].stable_id != first[0].stable_id


def test_group_reply_monitor_does_not_reemit_target_when_visible_index_shifts():
    client = FakeClient()
    client.rows = [row("项目群", unread=1, mention=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [
        incoming("普通消息 1", "1"),
        incoming("普通消息 2", "2"),
        incoming("@小牛 你还存活吗", "target"),
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, first_events = driver.observe_events()
    driver.begin_reply_cycle("项目群", first_events[0].conversation_id)
    client.rows = [row("项目群", unread=0, mention=False, signature="shifted")]
    client.histories["项目群"] = [
        incoming("普通消息 2", "2"),
        incoming("@小牛 你还存活吗", "target"),
    ]

    _, shifted_events = driver.observe_events()

    assert shifted_events == []


def test_revalidation_accepts_same_target_after_layout_moves():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage("Alice", "same", bounds=(10, 20, 100, 60))
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, events = driver.observe_events()
    client.histories["Alice"] = [
        UiaChatMessage("Alice", "same", bounds=(30, 40, 120, 80))
    ]

    validation = driver.validate_reply_target(events[0])

    assert validation.valid is True
    assert validation.replacement_event is None


def test_group_revalidation_uses_first_visible_at_message_without_direction():
    client = FakeClient()
    client.rows = [row("项目群", unread=1, mention=True, preview_prefix=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    unknown_message = UiaChatMessage(
        "", "@小牛 请确认", direction="unknown", runtime_id="1"
    )
    client.histories["项目群"] = [unknown_message]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()
    valid, reason = driver.validate_reply_target(events[0])

    assert valid is True
    assert reason == ""


def test_group_revalidation_keeps_older_visible_at_target_valid():
    client = FakeClient()
    client.rows = [row("项目群", unread=1, mention=True, preview_prefix=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [incoming("@小牛 first", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, events = driver.observe_events()
    client.histories["项目群"] = [
        incoming("@小牛 first", "1"),
        incoming("@小牛 second", "2"),
    ]

    validation = driver.validate_reply_target(events[0])

    assert validation.valid is True
    assert validation.replacement_event is None


def test_sending_does_not_clear_a_collected_group_target():
    client = FakeClient()
    client.rows = [row("项目群", unread=1, mention=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [incoming("@小牛 first", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, events = driver.observe_events()
    conversation_id = events[0].conversation_id
    emitted_target = driver._emitted_targets[conversation_id]

    driver.send_text(conversation_id, "reply")

    assert driver._emitted_targets[conversation_id] == emitted_target


def test_expired_queue_token_rejects_late_result():
    reply_queue = WechatReplyQueue()
    event = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "hello"
    )
    reply_queue.enqueue(event)
    item = reply_queue.get()
    assert reply_queue.is_active(item.token)
    reply_queue.expire(item.token)
    assert not reply_queue.is_active(item.token)
    reply_queue.signal(item.token, "completed")
    assert item.terminal == ""


def _bare_wechat_channel():
    implementation = WechatDesktopChannel.__closure__[0].cell_contents
    return object.__new__(implementation)


class FakeTimer:
    instances = []

    def __init__(self, interval, callback, args=()):
        self.interval = interval
        self.callback = callback
        self.args = args
        self.daemon = False
        self.cancelled = False
        self.started = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback(*self.args)


def test_private_aggregation_uses_sliding_window():
    FakeTimer.instances = []
    channel = _bare_wechat_channel()
    channel.config = {"private_message_aggregation_ms": 800}
    channel._pending_private_lock = threading.RLock()
    channel._pending_private_batches = {}
    channel._private_batch_timer_factory = FakeTimer
    channel._stop_event = threading.Event()
    channel._trace = lambda *_args, **_kwargs: None
    released = []
    channel._submit_materialization = lambda events: released.append(events)
    first = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "first"
    )
    second = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "second"
    )

    channel._defer_private_event(first)
    first_timer = FakeTimer.instances[-1]
    channel._defer_private_event(second)
    second_timer = FakeTimer.instances[-1]

    first_timer.fire()
    assert released == []
    second_timer.fire()

    assert released == [[first, second]]
    assert first_timer.cancelled is True
    assert second_timer.interval == 0.8
    assert channel._pending_private_batches == {}


def test_private_aggregation_is_isolated_by_conversation_and_window():
    FakeTimer.instances = []
    channel = _bare_wechat_channel()
    channel.config = {"private_message_aggregation_ms": 800}
    channel._pending_private_lock = threading.RLock()
    channel._pending_private_batches = {}
    channel._private_batch_timer_factory = FakeTimer
    channel._stop_event = threading.Event()
    channel._trace = lambda *_args, **_kwargs: None
    released = []
    channel._submit_materialization = lambda events: released.append(events)
    alice = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "alice-1"
    )
    bob = WechatDesktopEvent(
        "message", "b", "Bob", "b", "Bob", "text", "bob-1"
    )
    later = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "alice-2"
    )

    channel._defer_private_event(alice)
    alice_timer = FakeTimer.instances[-1]
    channel._defer_private_event(bob)
    bob_timer = FakeTimer.instances[-1]
    alice_timer.fire()
    bob_timer.fire()
    channel._defer_private_event(later)
    later_timer = FakeTimer.instances[-1]
    later_timer.fire()

    assert released == [[alice], [bob], [later]]


def test_control_events_bypass_private_aggregation():
    channel = _bare_wechat_channel()
    routed = []
    channel._dispatch_control_event = lambda event: routed.append(("control", event))
    channel._defer_private_event = lambda event: routed.append(("private", event))
    channel._submit_materialization = lambda events: routed.append(("group", events))
    cancel = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "/cancel"
    )
    steer = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "/steer 改查测试"
    )

    channel._route_reply_event(cancel)
    channel._route_reply_event(steer)

    assert routed == [("control", cancel), ("control", steer)]


def test_standalone_image_is_observed_while_file_is_materialized():
    channel = _bare_wechat_channel()
    routed = []
    processed = []
    finished = []
    channel._store = SimpleNamespace(
        mark_event_processed=lambda event_id: processed.append(event_id)
    )
    channel._trace = lambda *_args, **_kwargs: None
    channel._finish_lifecycle = (
        lambda event_ids, terminal: finished.append((event_ids, terminal))
    )
    channel._defer_private_event = lambda event: routed.append(("private", event))
    channel._submit_materialization = lambda events: routed.append(
        ("materialize", events)
    )
    image = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "image", "image.png"
    )
    file_event = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "file", "report.pdf"
    )
    text = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "说明"
    )

    channel._route_reply_event(image)
    channel._route_reply_event(file_event)
    channel._route_reply_event(text)

    assert routed == [
        ("materialize", [file_event]),
        ("private", text),
    ]
    assert processed == [image.event_id]
    assert finished == [
        ([image.event_id], "observed"),
    ]


def test_materialized_batch_keeps_all_source_event_ids():
    channel = _bare_wechat_channel()
    channel._driver = SimpleNamespace(
        materialize_event=lambda event: (event, int(event.content_type == "image"))
    )
    channel._preserve_event_evidence = lambda _event: None
    channel._lifecycle_lock = threading.RLock()
    channel._lifecycles = {}
    channel._scan_count = 1
    first = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "image", "C:/tmp/image.png"
    )
    second = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "这是什么"
    )
    channel._start_lifecycle(first)
    channel._start_lifecycle(second)

    batch = channel._materialize_batch([first, second])

    assert batch is second
    assert getattr(batch, "_source_event_ids") == [first.event_id, second.event_id]
    assert getattr(batch, "_batch_id")
    assert batch.history == []
    assert channel._lifecycles[first.event_id]["attachment_resolve_count"] == 1
    reply_queue = WechatReplyQueue()
    assert reply_queue.enqueue(batch)
    queued = reply_queue.get()
    assert queued.source_event_ids == [first.event_id, second.event_id]
    assert queued.batch_id == getattr(batch, "_batch_id")


def test_referenced_attachment_remains_available_to_agent_context():
    channel = _bare_wechat_channel()
    channel._driver = SimpleNamespace(
        materialize_event=lambda event: (event, 0)
    )
    channel._preserve_event_evidence = lambda _event: None
    channel._lifecycle_lock = threading.RLock()
    channel._lifecycles = {}
    channel._scan_count = 1
    event = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "这是什么图？",
        history=[
            {
                "content": "[图片: C:/tmp/image.png]",
                "content_type": "image",
            }
        ],
        reference={
            "content_type": "image",
            "file_path": "C:/tmp/image.png",
        },
    )
    channel._start_lifecycle(event)

    materialized = channel._materialize_batch([event])

    assert materialized.history == event.history
    assert getattr(materialized, "_attachment_reference_required") is False


def test_standalone_file_batch_is_cache_only():
    channel = _bare_wechat_channel()
    channel._driver = SimpleNamespace(
        materialize_event=lambda event: (event, 1)
    )
    channel._preserve_event_evidence = lambda _event: None
    channel._lifecycle_lock = threading.RLock()
    channel._lifecycles = {}
    channel._scan_count = 1
    file_event = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "file",
        "C:/tmp/report.pdf",
    )
    channel._start_lifecycle(file_event)

    materialized = channel._materialize_batch([file_event])

    assert getattr(materialized, "_cache_only") is True
    assert getattr(materialized, "_attachment_reference_required") is False


def test_non_reference_attachment_question_requires_quote():
    question = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "这个文件讲了什么？",
    )
    referenced = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "这个文件讲了什么？",
        reference={"content_type": "file", "file_path": "C:/tmp/a.pdf"},
    )
    generation = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "帮我生成一张图片",
    )
    image_question = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "这是什么图？",
    )

    assert WechatDesktopChannel.__closure__[0].cell_contents._requires_attachment_reference(
        question
    )
    assert WechatDesktopChannel.__closure__[0].cell_contents._requires_attachment_reference(
        image_question
    )
    assert not WechatDesktopChannel.__closure__[0].cell_contents._requires_attachment_reference(
        referenced
    )
    assert not WechatDesktopChannel.__closure__[0].cell_contents._requires_attachment_reference(
        generation
    )


def test_materialization_worker_defers_referenced_attachment_until_fifo():
    channel = _bare_wechat_channel()
    channel._stop_event = threading.Event()
    channel._materialize_queue = queue.Queue()
    channel._materialize_stop = object()
    enqueued = []
    channel._prepare_deferred_materialization = (
        WechatDesktopChannel.__closure__[0].cell_contents._prepare_deferred_materialization.__get__(
            channel
        )
    )
    channel._has_referenced_attachment = (
        WechatDesktopChannel.__closure__[0].cell_contents._has_referenced_attachment
    )
    channel._materialize_batch = lambda _events: (_ for _ in ()).throw(
        AssertionError("referenced attachment must wait for its FIFO turn")
    )
    channel._enqueue_reply_event = lambda event: enqueued.append(event)
    event = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "这是什么图",
        reference={"content_type": "image"},
    )
    channel._materialize_queue.put([event])
    channel._materialize_queue.put(channel._materialize_stop)

    channel._consume_materialization_queue()

    assert enqueued == [event]
    assert getattr(event, "_deferred_materialization_events") == [event]


def test_attachment_reference_prompt_is_sent_without_agent():
    channel = _bare_wechat_channel()
    event = WechatDesktopEvent(
        "message",
        "uia-session:a",
        "Alice",
        "a",
        "Alice",
        "text",
        "这是什么图？",
    )
    setattr(event, "_attachment_reference_required", True)
    reply_queue = WechatReplyQueue()
    assert reply_queue.enqueue(event)
    item = reply_queue.get()
    sent = []
    lifecycle = []
    audits = []
    history = []
    channel._service = SimpleNamespace(status=lambda: {"paused": False})
    channel._policy = SimpleNamespace(can_auto_send=lambda *_args: True)
    channel._driver = SimpleNamespace(
        validate_reply_target=lambda _event: SimpleNamespace(
            valid=True,
            reason="",
            replacement_event=None,
        ),
        send_text=lambda target, text: (
            sent.append((target, text))
            or {"success": True, "verified": True}
        ),
    )
    channel._store = SimpleNamespace(
        audit=lambda *args, **kwargs: audits.append((args, kwargs)),
        append_conversation_history=lambda **kwargs: history.append(kwargs),
    )
    channel._mark_lifecycle = (
        lambda event_ids, stage, **kwargs: lifecycle.append(
            (event_ids, stage, kwargs)
        )
    )
    channel.config = {"self_display_name": "Bot"}

    terminal = channel._send_attachment_reference_prompt(item)

    assert terminal == "completed"
    assert sent == [
        ("uia-session:a", ATTACHMENT_REFERENCE_REQUIRED_REPLY)
    ]
    assert [stage for _, stage, _ in lifecycle] == [
        "send_started",
        "send_verified",
    ]
    assert history[0]["content"] == ATTACHMENT_REFERENCE_REQUIRED_REPLY
    assert audits


def test_reply_consumer_bypasses_agent_for_reference_prompt():
    channel = _bare_wechat_channel()
    channel._stop_event = threading.Event()
    channel._reply_queue = WechatReplyQueue()
    event = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "这是什么图？"
    )
    setattr(event, "_attachment_reference_required", True)
    assert channel._reply_queue.enqueue(event)
    original_finish = channel._reply_queue.finish

    def finish(item, terminal):
        original_finish(item, terminal)
        channel._stop_event.set()

    channel._reply_queue.finish = finish
    stages = []
    processed = []
    channel._service = SimpleNamespace(
        update_status=lambda **_kwargs: None
    )
    channel._store = SimpleNamespace(
        mark_event_processed=lambda event_id: processed.append(event_id),
        audit=lambda *_args, **_kwargs: None,
    )
    channel._driver = SimpleNamespace(
        end_reply_cycle=lambda: (_ for _ in ()).throw(
            AssertionError("no Agent reply cycle should be active")
        )
    )
    channel._mark_lifecycle = (
        lambda _event_ids, stage, **_kwargs: stages.append(stage)
    )
    channel._finish_lifecycle = lambda *_args, **_kwargs: None
    channel._send_attachment_reference_prompt = lambda _item: "completed"
    channel._dispatch_message = lambda *_args: (_ for _ in ()).throw(
        AssertionError("reference prompt must bypass Agent")
    )

    channel._consume_reply_queue()

    assert processed == [event.event_id]
    assert "agent_started" not in stages
    assert "agent_done" not in stages


def test_reply_consumer_dispatches_enqueue_time_context_snapshot():
    channel = _bare_wechat_channel()
    channel._stop_event = threading.Event()
    channel._reply_queue = WechatReplyQueue()
    event = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "待回复消息",
        history=[{"content": "入队时的上文", "content_type": "text"}],
    )
    assert channel._reply_queue.enqueue(event)
    event.history = [{"content": "消费时错误找回的上文"}]
    original_finish = channel._reply_queue.finish

    def finish(item, terminal):
        original_finish(item, terminal)
        channel._stop_event.set()

    channel._reply_queue.finish = finish
    dispatched_history = []
    channel._service = SimpleNamespace(update_status=lambda **_kwargs: None)
    channel._store = SimpleNamespace(
        mark_event_processed=lambda _event_id: None,
        audit=lambda *_args, **_kwargs: None,
    )
    channel._driver = SimpleNamespace(end_reply_cycle=lambda: None)
    channel._mark_lifecycle = lambda *_args, **_kwargs: None
    channel._finish_lifecycle = lambda *_args, **_kwargs: None

    def dispatch(dispatched_event, _token):
        dispatched_history.extend(dispatched_event.history)
        return False

    channel._dispatch_message = dispatch

    channel._consume_reply_queue()

    assert dispatched_history == [
        {"content": "入队时的上文", "content_type": "text"}
    ]


def test_attachment_path_cache_avoids_second_image_capture(tmp_path):
    local_image = tmp_path / "cached.png"
    local_image.write_bytes(b"png")
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage(
            "Alice",
            "[图片]",
            message_type="image",
            runtime_id="image-1",
            bounds=(100, 100, 300, 260),
        )
    ]
    client.image_paths["[图片]"] = str(local_image)
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, events = driver.observe_events()

    _, first_count = driver.materialize_event(events[0])
    _, second_count = driver.materialize_event(events[0])

    assert first_count == 1
    assert second_count == 0
    assert client.image_fetches == ["[图片]"]


def test_materialization_resolves_only_selected_visible_attachment(tmp_path):
    old_file = tmp_path / "old.pdf"
    target_image = tmp_path / "target.png"
    old_file.write_bytes(b"pdf")
    target_image.write_bytes(b"png")
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage(
            "Alice",
            "old.pdf",
            message_type="file",
            runtime_id="file-1",
        ),
        UiaChatMessage(
            "Alice",
            "target-image",
            message_type="image",
            runtime_id="image-1",
            bounds=(100, 100, 300, 260),
        ),
    ]
    client.file_paths["old.pdf"] = str(old_file)
    client.image_paths["target-image"] = str(target_image)
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True},
        client=client,
        shell_hook=FakeHook(),
    )

    _, events = driver.observe_events()
    materialized, resolve_count = driver.materialize_event(events[0])

    assert resolve_count == 1
    assert materialized.content == str(target_image)
    assert client.image_fetches == ["target-image"]
    assert client.file_fetches == []


def test_cached_file_is_reused_by_quoted_filename(tmp_path):
    local_file = tmp_path / "report.pdf"
    local_file.write_bytes(b"%PDF")
    client = FakeClient()
    client.file_paths["文件\nreport.pdf\n8 KB"] = str(local_file)
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())
    standalone = UiaChatMessage(
        "Alice",
        "文件\nreport.pdf\n8 KB",
        message_type="file",
        runtime_id="file-1",
        stable_id="stable-file-1",
    )

    cached, first_count = driver._resolve_reply_target_message(
        "conversation", standalone
    )
    quoted = UiaChatMessage(
        "Alice",
        "帮我读一下",
        message_type="text",
        runtime_id="quote-1",
        stable_id="stable-quote-1",
        reference=UiaReferencedMessage(
            sender_name="Alice",
            content="report.pdf",
            message_type="file",
        ),
    )
    resolved, second_count = driver._resolve_reply_target_message(
        "conversation", quoted
    )

    assert cached.file_path == str(local_file)
    assert first_count == 1
    assert resolved.reference.file_path == str(local_file)
    assert resolved.reference.resolved is True
    assert second_count == 0
    assert client.file_fetches == ["文件\nreport.pdf\n8 KB"]


def test_text_reference_uses_uia_preview_without_locating_original():
    client = FakeClient()
    client.resolve_message_reference = lambda _message: (_ for _ in ()).throw(
        AssertionError("text reference must not locate original")
    )
    client.fetch_referenced_message_image = lambda _message: (
        _ for _ in ()
    ).throw(AssertionError("text reference must not open image viewer"))
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())
    quoted = UiaChatMessage(
        "Alice",
        "你怎么看",
        message_type="text",
        runtime_id="quote-text-1",
        reference=UiaReferencedMessage(
            sender_name="Bob",
            content="这是被引用的文本预览",
            message_type="text",
        ),
    )

    resolved, resolve_count = driver._resolve_reply_target_message(
        "conversation", quoted
    )

    assert resolve_count == 0
    assert resolved.reference.content == "这是被引用的文本预览"
    assert resolved.reference.resolved is True
    assert resolved.reference.strategy == "uia_reference_preview"


def test_image_reference_opens_current_quote_region_without_locating_original(
    tmp_path,
):
    image_path = tmp_path / "quoted.png"
    image_path.write_bytes(b"png")
    client = FakeClient()
    calls = []
    client.fetch_referenced_message_image = lambda message: calls.append(
        ("viewer", message.content)
    ) or str(image_path)
    client.resolve_message_reference = lambda _message: (_ for _ in ()).throw(
        AssertionError("image reference must not locate original")
    )
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())
    quoted = UiaChatMessage(
        "Alice",
        "这张图",
        message_type="text",
        runtime_id="quote-image-1",
        reference=UiaReferencedMessage(
            sender_name="Bob",
            content="图片",
            message_type="image",
        ),
    )

    resolved, resolve_count = driver._resolve_reply_target_message(
        "conversation", quoted
    )

    assert calls == [("viewer", "这张图")]
    assert resolve_count == 1
    assert resolved.reference.file_path == str(image_path)
    assert resolved.reference.resolved is True
    assert resolved.reference.strategy == "wechat_reference_image_viewer"


def test_file_reference_uses_prefix_cache_before_locating_original(tmp_path):
    cached_file = tmp_path / "report(1).pdf"
    cached_file.write_bytes(b"%PDF")
    client = FakeClient()
    client.resolve_message_reference = lambda _message: (_ for _ in ()).throw(
        AssertionError("prefix cache hit must not locate original")
    )
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())
    driver._remember_file_by_name(
        "conversation", "report(1).pdf", str(cached_file)
    )
    quoted = UiaChatMessage(
        "Alice",
        "帮我读一下",
        message_type="text",
        runtime_id="quote-file-prefix-1",
        reference=UiaReferencedMessage(
            sender_name="Bob",
            content="report.pdf",
            message_type="file",
        ),
    )

    resolved, resolve_count = driver._resolve_reply_target_message(
        "conversation", quoted
    )

    assert resolve_count == 0
    assert resolved.reference.file_path == str(cached_file)
    assert resolved.reference.resolved is True


def test_file_reference_cache_miss_locates_original(tmp_path):
    resolved_file = tmp_path / "report.pdf"
    resolved_file.write_bytes(b"%PDF")
    client = FakeClient()
    calls = []

    def resolve_reference(message):
        calls.append(message.reference.content)
        return replace(
            message,
            reference=replace(
                message.reference,
                file_path=str(resolved_file),
                resolved=True,
                degraded=False,
                strategy="wechat_locate_original",
            ),
        )

    client.resolve_message_reference = resolve_reference
    client.fetch_referenced_message_image = lambda _message: (
        _ for _ in ()
    ).throw(AssertionError("file reference must not open image viewer"))
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())
    quoted = UiaChatMessage(
        "Alice",
        "帮我读一下",
        message_type="text",
        runtime_id="quote-file-miss-1",
        reference=UiaReferencedMessage(
            sender_name="Bob",
            content="report.pdf",
            message_type="file",
        ),
    )

    resolved, resolve_count = driver._resolve_reply_target_message(
        "conversation", quoted
    )

    assert calls == ["report.pdf"]
    assert resolve_count == 1
    assert resolved.reference.file_path == str(resolved_file)
    assert resolved.reference.strategy == "wechat_locate_original"


def test_attachment_cache_re_resolves_invalid_path_and_changed_identity(tmp_path):
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"
    first_path.write_bytes(b"one")
    second_path.write_bytes(b"two")
    client = FakeClient()
    client.image_paths["same"] = str(first_path)
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())
    first = UiaChatMessage(
        "Alice",
        "same",
        message_type="image",
        runtime_id="runtime-1",
        stable_id="stable-1",
    )

    resolved, first_count = driver._resolve_reply_target_message("alice", first)
    first_path.unlink()
    client.image_paths["same"] = str(second_path)
    refreshed, invalid_path_count = driver._resolve_reply_target_message(
        "alice", first
    )
    changed_identity = UiaChatMessage(
        "Alice",
        "same",
        message_type="image",
        runtime_id="runtime-2",
        stable_id="stable-2",
    )
    _, identity_count = driver._resolve_reply_target_message(
        "alice", changed_identity
    )

    assert resolved.file_path == str(first_path)
    assert refreshed.file_path == str(second_path)
    assert (first_count, invalid_path_count, identity_count) == (1, 1, 1)
    assert client.image_fetches == ["same", "same", "same"]


def test_reply_priority_skips_starting_a_scan():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())
    driver._reply_ui_pending.set()

    observation, events = driver.observe_events()

    assert events == []
    assert observation["scan_yielded_to_reply"] is True
    assert client.focus_calls == 0
    assert client.owner_calls == 0
    assert client.history_calls == []


def test_waiting_reply_prevents_scanner_from_reentering_uia():
    coordinator = _UiaPriorityCoordinator()
    first_scan_started = threading.Event()
    release_first_scan = threading.Event()
    order = []

    def first_scan():
        with coordinator.lease(reply=False):
            first_scan_started.set()
            release_first_scan.wait(1)

    def reply():
        with coordinator.lease(reply=True):
            order.append("reply")

    def second_scan():
        with coordinator.lease(reply=False):
            order.append("scan")

    first = threading.Thread(target=first_scan)
    high = threading.Thread(target=reply)
    low = threading.Thread(target=second_scan)
    first.start()
    assert first_scan_started.wait(1)
    high.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with coordinator._condition:
            if coordinator._reply_waiters:
                break
        time.sleep(0.005)
    low.start()
    release_first_scan.set()
    for worker in (first, high, low):
        worker.join(1)

    assert order == ["reply", "scan"]


def test_reply_validation_does_not_deadlock_with_active_scan():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("hello", "message-1")]
    scan_inside_uia = threading.Event()
    release_scan_read = threading.Event()
    original_locate = client.locate_conversation

    def blocking_locate(*args, **kwargs):
        scan_inside_uia.set()
        assert release_scan_read.wait(1)
        return original_locate(*args, **kwargs)

    client.locate_conversation = blocking_locate
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True},
        client=client,
        shell_hook=FakeHook(),
    )
    conversation_id = driver._row_key(client.rows[0])
    event = WechatDesktopEvent(
        "message",
        conversation_id,
        "Alice",
        "Alice",
        "Alice",
        "text",
        "hello",
    )
    result = {}
    scan_thread = threading.Thread(
        target=lambda: result.setdefault("scan", driver.observe_events()),
        daemon=True,
    )
    reply_thread = threading.Thread(
        target=lambda: result.setdefault(
            "validation", driver.validate_reply_target(event)
        ),
        daemon=True,
    )

    scan_thread.start()
    assert scan_inside_uia.wait(1)
    reply_thread.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with driver._uia_priority._condition:
            if driver._uia_priority._reply_waiters:
                break
        time.sleep(0.005)
    release_scan_read.set()
    scan_thread.join(1)
    reply_thread.join(1)

    assert not scan_thread.is_alive()
    assert not reply_thread.is_alive()
    assert result["validation"].valid is True


def test_lifecycle_summary_is_one_info_line_per_source_message():
    channel = _bare_wechat_channel()
    channel._lifecycle_lock = threading.RLock()
    channel._lifecycles = {}
    channel._scan_count = 4
    event = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "hello"
    )
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Capture()
    logger.addHandler(handler)
    try:
        channel._start_lifecycle(event)
        channel._mark_lifecycle(
            [event.event_id],
            "materialized",
            batch_id="batch-1",
            attachment_resolve_count=1,
        )
        channel._mark_lifecycle([event.event_id], "queued")
        channel._mark_lifecycle([event.event_id], "agent_started")
        channel._mark_lifecycle([event.event_id], "agent_done")
        channel._finish_lifecycle([event.event_id], "completed")
    finally:
        logger.removeHandler(handler)

    lifecycle_lines = [
        line for line in records if line.startswith("[WechatDesktop][lifecycle]")
    ]
    assert len(lifecycle_lines) == 1
    assert "event_id=" + event.event_id in lifecycle_lines[0]
    assert "attachment_resolve_count=1" in lifecycle_lines[0]
    assert "superseded_by=-" in lifecycle_lines[0]


def test_shell_hook_filters_process_and_debounces_injected_signals():
    hook = WindowsShellHook(lambda: {42}, debounce_ms=250)
    hook.notify_for_tests(100, 7, created_at=1.0)
    assert hook.consume().signaled is False
    hook.notify_for_tests(100, 42, created_at=2.0)
    hook.notify_for_tests(100, 42, created_at=2.1)
    signal = hook.consume()
    assert signal.signaled is True
    assert signal.created_at == 2.0


def test_white_tree_recovery_stops_after_tree_returns(monkeypatch):
    client = WechatUiaClient({"uia_recovery_attempts": 3, "uia_recovery_settle_ms": 0})
    probes = iter([0, 0, 12])
    clicks = []
    monkeypatch.setattr(client, "probe_tree", lambda: next(probes))
    monkeypatch.setattr(client, "_click_taskbar_button", lambda: clicks.append(1) or True)
    assert client._recover_empty_tree(123) is True
    assert len(clicks) == 2
