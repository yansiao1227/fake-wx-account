"""引用原消息定位及其附件的集成回归。"""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
import pytest
from channel.wechat_desktop.models import UiaChatMessage, UiaReferencedMessage, WechatDesktopEvent
from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia.gateway import WechatUiaGateway
from channel.wechat_desktop.uia.materializer import WechatUiaMaterializer
from .helpers import FakeClient, GeometryControl
from .test_uia_materializer import native_event


def test_reference_history_contains_only_the_single_original_message():
    reference = UiaReferencedMessage("Alice", "完整原文", resolved=True,
                                     strategy="wechat_locate_original")
    message = UiaChatMessage("Alice", "当前问题", direction="incoming", reference=reference)
    client = FakeClient()
    client.resolve_message_reference = lambda value, **kwargs: value
    materializer = WechatUiaMaterializer({}, gateway=WechatUiaGateway({}, client=client))
    event = native_event(content="当前问题")
    result, _ = materializer.materialize_event(event, target_message=message)
    assert result.history == [{
        "sender_name": "", "content": "完整原文", "content_type": "text",
        "is_reference": True, "resolved": True, "degraded": False,
        "strategy": "wechat_locate_original",
    }]


def test_reference_attachment_is_added_to_event_without_changing_current_type(tmp_path):
    image = tmp_path / "reference.png"
    image.write_bytes(b"synthetic image")
    reference = UiaReferencedMessage("Alice", "图片", message_type="image")
    message = UiaChatMessage("Alice", "这是什么", direction="incoming", reference=reference)
    client = FakeClient()
    client.fetch_referenced_message_image = lambda value, **kwargs: str(image)
    materializer = WechatUiaMaterializer({}, gateway=WechatUiaGateway({}, client=client))
    event = native_event(content="这是什么")
    result, _ = materializer.materialize_event(event, target_message=message)
    assert result.content_type == "text"
    assert result.content == "这是什么"
    assert result.evidence_path == str(image)
    assert result.reference["depth"] == 1
    assert result.reference["file_path"] == str(image)


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
    import win32api

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
        win32api,
        "keybd_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing reference menu must not send global Escape")
        ),
    )

    assert client.resolve_message_reference(quoted) is quoted


def _strict_reference_client(monkeypatch, *, kind="file", return_available=True):
    client = WechatUiaClient({})
    preview = "synthetic.pdf" if kind == "file" else "synthetic article"
    quote_control = GeometryControl((20, 300, 420, 420),
        f"question引用 Synthetic 的消息 : {preview}", "mmui::ChatTextItemView")
    quote_control.GetRuntimeId = lambda: (42, 12)
    original = GeometryControl((20, 100, 420, 200),
        "文件\nsynthetic.pdf\n1K" if kind == "file" else "[链接]synthetic article",
        "mmui::ChatFileItemView" if kind == "file" else "mmui::ChatBubbleItemView")
    original.GetRuntimeId = lambda: (42, 11)
    message_list = GeometryControl((0, 0, 700, 700), children=[original, quote_control],
                                  automation_id="chat_message_list")
    root = GeometryControl((0, 0, 700, 700), children=[message_list])
    menu = GeometryControl((0, 0, 50, 50), "locate-menu")
    back = GeometryControl((0, 0, 50, 50), "return-button")
    state = SimpleNamespace(valid=True, switch_at="", actions=[])
    quoted = UiaChatMessage("Synthetic", "question", "text", "incoming", "42.12",
        (700, 700, 900, 900), reference=UiaReferencedMessage("Synthetic", preview, kind))

    def action(name):
        state.actions.append(name)
        if state.switch_at == name:
            state.valid = False

    @contextmanager
    def fake_root():
        yield root

    def find_menu(name, _class):
        if name == "定位到原文位置":
            action("menu_lookup")
            return menu
        action("return_lookup")
        return back if return_available else None

    def click(control):
        action("locate" if control is menu else "return")
        return True

    def fetch_file(message, *, allow_filename_cache=True, validate_target=None):
        assert allow_filename_cache is False and message.runtime_id == "42.11"
        action("fetch")
        validate_target()
        return ""

    def fetch_share(message, *, strict=False, validate_target=None):
        assert strict is True
        action("share_fetch")
        validate_target()
        return "", ""

    monkeypatch.setattr(client, "focus_window", lambda: action("focus"))
    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_right_click_point", lambda point: (action("right_click"), state.actions.append(point)))
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: action("wait"))
    monkeypatch.setattr(client, "_find_desktop_control", find_menu)
    monkeypatch.setattr(client, "_click_control_point", click)
    monkeypatch.setattr(client, "fetch_message_file", fetch_file)
    monkeypatch.setattr(client, "fetch_share_message_page", fetch_share)
    monkeypatch.setattr(client, "fetch_referenced_share_page", lambda *_args, **kwargs:
                        (action("share_fallback"), ("", ""))[1])
    monkeypatch.setattr(client, "_press_end_key", lambda: action("end"))
    return client, quoted, quote_control, original, state


def test_strict_reference_reacquires_runtime_and_current_bounds_after_focus(monkeypatch):
    client, quoted, quote_control, original, state = _strict_reference_client(monkeypatch)

    client.resolve_message_reference(quoted, allow_filename_cache=False, validate_target=lambda: state.valid)

    assert (120, 386) in state.actions
    assert (750, 844) not in state.actions
    assert "fetch" in state.actions


def test_strict_reference_runtime_change_never_clicks_old_bounds(monkeypatch):
    client, quoted, quote_control, original, state = _strict_reference_client(monkeypatch)
    quote_control.GetRuntimeId = lambda: (42, 999)

    result = client.resolve_message_reference(quoted, allow_filename_cache=False, validate_target=lambda: state.valid)

    assert "right_click" not in state.actions
    assert "fetch" not in state.actions
    assert "return" not in state.actions and "end" not in state.actions
    assert not result.reference.resolved


@pytest.mark.parametrize("stage", ["focus", "right_click", "menu_lookup", "locate", "fetch", "return_lookup", "return"])
def test_strict_reference_switch_cancels_remaining_actions_and_cleanup(monkeypatch, stage):
    client, quoted, quote_control, original, state = _strict_reference_client(monkeypatch)
    state.switch_at = stage

    with pytest.raises(RuntimeError, match="attachment_target_changed"):
        client.resolve_message_reference(quoted, allow_filename_cache=False, validate_target=lambda: state.valid)

    after_switch = state.actions[state.actions.index(stage) + 1:]
    # right-click记录的坐标不是后续动作；失效后禁止返回/End/其他附件操作。
    assert not any(isinstance(item, str) for item in after_switch)


def test_strict_reference_switch_after_end_skips_share_fallback(monkeypatch):
    client, quoted, quote_control, original, state = _strict_reference_client(
        monkeypatch, kind="share_card", return_available=False)
    state.switch_at = "end"

    with pytest.raises(RuntimeError, match="attachment_target_changed"):
        client.resolve_message_reference(quoted, allow_filename_cache=False, validate_target=lambda: state.valid)

    assert "end" in state.actions
    assert state.actions[-1] == "end"
    assert "share_fallback" not in state.actions


def test_known_file_reference_rejects_same_named_text_original(monkeypatch):
    client, quoted, quote_control, original, state = _strict_reference_client(monkeypatch)
    original.Name, original.ClassName = "synthetic.pdf", "mmui::ChatTextItemView"
    # 即使上层定位器错误返回这个同名控件，解析器仍复核类型。
    monkeypatch.setattr(client, "_pick_located_original_control", lambda *_args: original)

    result = client.resolve_message_reference(quoted, allow_filename_cache=False, validate_target=lambda: state.valid)

    assert result.reference.message_type == "file"
    assert not result.reference.resolved and result.reference.degraded
    assert result.reference.strategy == "uia_reference_type_mismatch"
    assert "fetch" not in state.actions and "share_fallback" not in state.actions


def test_original_picker_excludes_same_named_text_for_known_file(monkeypatch):
    client, quoted, quote_control, original, state = _strict_reference_client(monkeypatch)
    original.Name, original.ClassName = "synthetic.pdf", "mmui::ChatTextItemView"
    message_list = GeometryControl((0, 0, 700, 700), children=[original])

    assert client._pick_located_original_control(message_list, "file", "synthetic.pdf") is None


def test_strict_reference_without_preview_never_guesses_original_by_position(monkeypatch):
    client, quoted, quote_control, original, state = _strict_reference_client(monkeypatch)
    quoted = replace(quoted, reference=replace(quoted.reference, content="  ", message_type="app_message"))

    result = client.resolve_message_reference(quoted, allow_filename_cache=False, validate_target=lambda: state.valid)

    assert state.actions == []
    assert not result.reference.resolved and result.reference.degraded
    assert result.reference.strategy == "uia_reference_preview_unavailable"
