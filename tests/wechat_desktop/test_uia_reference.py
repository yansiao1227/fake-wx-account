"""引用原消息定位及其附件的集成回归。"""

from contextlib import contextmanager
from channel.wechat_desktop.models import UiaChatMessage, UiaReferencedMessage
from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia.driver import WechatUiaDriver
from .helpers import FakeClient, FakeHook, GeometryControl


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
