"""微信桌面 share_browser 回归测试。"""
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from channel.wechat_desktop.models import UiaChatMessage, UiaReferencedMessage, WechatDesktopEvent
from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia.gateway import WechatUiaGateway, _UiaPriorityCoordinator
from channel.wechat_desktop.uia.materializer import WechatUiaMaterializer
from channel.wechat_desktop.pipeline.prompts import _render_event_context_lines
from .helpers import FakeClient, GeometryControl
from .test_uia_materializer import materialize_native_target


def test_share_card_requires_link_marker_and_matching_title():
    content = (
        "[链接]我Chovy，你做薯片给我做好了呀\n"
        "UP主：印度老吃家\n播放：255.3万\n哔哩哔哩"
    )

    assert WechatUiaClient._message_type("mmui::ChatTextItemView", content) == (
        "share_card"
    )
    assert WechatUiaClient._share_card_matches_preview(
        content, "我Chovy，你做薯片给我做好了呀"
    )
    assert not WechatUiaClient._share_card_matches_preview(
        content, "另一个普通文本标题"
    )
    assert WechatUiaClient._message_type(
        "mmui::ChatTextItemView", "普通文本\n哔哩哔哩"
    ) == "text"


def test_located_original_rejects_duplicate_preview_matches():
    first = GeometryControl(
        (100, 200, 500, 260), "重复原文", "mmui::ChatTextItemView"
    )
    second = GeometryControl(
        (100, 300, 500, 360), "重复原文", "mmui::ChatTextItemView"
    )
    message_list = GeometryControl(
        (0, 0, 700, 700), children=[first, second]
    )

    assert (
        WechatUiaClient._pick_located_original_control(
            message_list, "text", "重复原文"
        )
        is None
    )


def _materialize_share_reference(monkeypatch, reference, *, initial_reference=None):
    def forbid_fetch(*_args, **_kwargs):
        raise AssertionError("UIA materialization must leave URL reading to Agent tools")

    monkeypatch.setattr("agent.tools.web_fetch.web_fetch.WebFetch.execute", forbid_fetch)
    client = FakeClient()

    def resolve_reference(message, *, allow_filename_cache=True, validate_target=None):
        assert allow_filename_cache is False
        return replace(message, reference=reference)

    client.resolve_message_reference = resolve_reference
    driver = WechatUiaMaterializer({}, gateway=WechatUiaGateway({}, client=client))
    event = WechatDesktopEvent(
        "message",
        "conversation",
        "Alice",
        "alice",
        "Alice",
        "text",
        "阅读这篇文章",
        reference=initial_reference or {"source_message_id": "synthetic-reference"},
        account_id="synthetic-account",
        source_message_id="message_0/Msg_synthetic/1",
    )
    target = UiaChatMessage(
        "Alice", event.content, runtime_id="synthetic-runtime", reference=reference,
    )
    result, count = driver.materialize_event(
        event, target_message=target, validate=lambda: True,
    )
    assert count == 1
    return result


def test_share_reference_preserves_url_without_network_fetch(monkeypatch):
    reference = UiaReferencedMessage(
        "Bob", "合成分享标题", "share_card", url="https://example.com/article",
        resolved=True, platform="合成平台",
    )

    result = _materialize_share_reference(monkeypatch, reference)

    assert result.reference["url"] == "https://example.com/article"
    assert result.reference["platform"] == "合成平台"
    assert result.reference["source_message_id"] == "synthetic-reference"
    assert result.reference["fetch_status"] == "link_available"
    assert result.reference["fetched_content"] == ""
    assert result.reference["fetch_source"] == ""
    assert result.reference["resolved"] is False
    assert result.reference["degraded"] is True


def test_share_reference_preserves_wechat_browser_content_without_network_fetch(monkeypatch):
    reference = UiaReferencedMessage(
        "Bob", "合成分享标题", "share_card",
        browser_content="这是合成的微信内置浏览器页面正文。",
        browser_status="success",
    )

    result = _materialize_share_reference(monkeypatch, reference)

    assert result.reference["browser_content"] == reference.browser_content
    assert result.reference["browser_status"] == "success"
    assert result.reference["fetch_status"] == "direct_browser"
    assert result.reference["fetch_source"] == "direct_browser"
    assert result.reference["fetched_content"] == ""
    assert result.reference["resolved"] is True
    assert result.reference["degraded"] is False
    assert result.attachment_status == "materialized"


def test_share_reference_without_url_is_unavailable_without_network_fetch(monkeypatch):
    reference = UiaReferencedMessage(
        "Bob", "合成分享标题", "share_card", resolved=True,
        fetched_content="旧的预取正文", fetch_status="success", browser_status="success",
    )

    result = _materialize_share_reference(monkeypatch, reference)

    assert result.reference["fetch_status"] == "link_unavailable"
    assert result.reference["browser_status"] == "unavailable"
    assert result.reference["fetched_content"] == ""
    assert result.reference["resolved"] is False
    assert result.reference["degraded"] is True
    assert result.attachment_status == "unavailable"


def test_share_browser_flow_clicks_quote_right_clicks_more_and_closes(monkeypatch):
    client = WechatUiaClient({})
    reference_node = GeometryControl(
        (900, 540, 1620, 670),
        "这个视频讲了啥引用 颜料盒 的消息 : 分享标题",
        "mmui::ChatTextItemView",
    )
    root = GeometryControl((0, 0, 1800, 1000), children=[reference_node])
    more = GeometryControl((1500, 50, 1540, 90), "更多", "mmui::XButton")
    close = GeometryControl((1560, 50, 1600, 90), "关闭", "mmui::XButton")
    more.ControlTypeName = "ButtonControl"
    close.ControlTypeName = "ButtonControl"
    calls = []

    @contextmanager
    def fake_root():
        yield root

    monkeypatch.setattr(client, "get_owner_window_handle", lambda: 100)
    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_find_message_control", lambda *_args: reference_node)
    monkeypatch.setattr(client, "_visible_top_level_window_handles", lambda: {100})
    monkeypatch.setattr(client, "_left_click_point", lambda point: calls.append(("open", point)))
    monkeypatch.setattr(client, "_find_opened_share_browser", lambda *_args: 200)
    monkeypatch.setattr(
        client,
        "_verified_share_browser_controls",
        lambda *_args: (more, close),
    )
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)
    monkeypatch.setattr(client, "_right_click_point", lambda point: calls.append(("more", point)))
    monkeypatch.setattr(client, "_click_copy_link_menu", lambda hwnd: calls.append(("copy", hwnd)) or True)

    def clipboard(action):
        action()
        return "https://www.bilibili.com/video/BV1test"

    monkeypatch.setattr(client, "_clipboard_unicode_after", clipboard)
    monkeypatch.setattr(client, "_close_share_browser", lambda *args: calls.append(("close", args)) or True)
    message = UiaChatMessage(
        "Alice",
        "这个视频讲了啥",
        bounds=(900, 540, 1620, 670),
        reference=UiaReferencedMessage(
            "颜料盒", "分享标题", message_type="share_card"
        ),
    )

    assert client.fetch_referenced_share_url(message) == (
        "https://www.bilibili.com/video/BV1test"
    )
    assert calls == [
        ("open", (1080, 638)),
        ("more", (1520, 70)),
        ("copy", 200),
        ("close", (200, 100)),
    ]


def test_share_browser_prefers_right_embedded_webview_panel(monkeypatch):
    import uiautomation as auto

    main = GeometryControl((0, 0, 1800, 1000))
    side_document = GeometryControl((1000, 100, 1800, 1000), "文章正文")
    side_document.ControlTypeName = "DocumentControl"
    side_panel = GeometryControl(
        (1000, 0, 1800, 1000), children=[side_document]
    )
    side_panel.ClassName = "Chrome_WidgetWin_0"
    side_panel.NativeWindowHandle = 200
    left_document = GeometryControl((0, 100, 700, 1000), "不是侧边分享页")
    left_document.ControlTypeName = "DocumentControl"
    left_panel = GeometryControl((0, 0, 700, 1000), children=[left_document])
    left_panel.ClassName = "Chrome_WidgetWin_0"
    left_panel.NativeWindowHandle = 201
    main._children = [left_panel, side_panel]
    client = WechatUiaClient({})

    monkeypatch.setattr(auto, "ControlFromHandle", lambda _hwnd: main)

    assert client._find_embedded_share_browser(100) == 200


def test_share_browser_direct_read_skips_copy_link_and_closes(monkeypatch):
    client = WechatUiaClient({})
    calls = []
    monkeypatch.setattr(client, "get_owner_window_handle", lambda: 100)
    monkeypatch.setattr(client, "_visible_top_level_window_handles", lambda: {100})
    monkeypatch.setattr(
        client, "_left_click_point", lambda point: calls.append(("open", point))
    )
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)
    monkeypatch.setattr(client, "_find_opened_share_browser", lambda *_args: 200)
    monkeypatch.setattr(
        client,
        "_read_share_browser_content",
        lambda hwnd: calls.append(("read", hwnd)) or "页面标题\n页面正文内容",
    )
    monkeypatch.setattr(
        client,
        "_verified_share_browser_controls",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("successful direct read must not open the copy-link menu")
        ),
    )
    monkeypatch.setattr(
        client,
        "_close_share_browser",
        lambda *args: calls.append(("close", args)) or True,
    )

    content, url = client.fetch_share_page_from_point((400, 500))

    assert content == "页面标题\n页面正文内容"
    assert url == ""
    assert calls == [
        ("open", (400, 500)),
        ("read", 200),
        ("close", (200, 100)),
    ]


def test_share_browser_reads_uia_document_without_clipboard(monkeypatch):
    import uiautomation as auto

    title = GeometryControl((0, 0, 800, 40), "文章标题")
    title.ControlTypeName = "TextControl"
    paragraph = GeometryControl((0, 40, 800, 200), "这是文章正文，足够长，可以直接读取。")
    paragraph.ControlTypeName = "TextControl"
    document = GeometryControl(
        (0, 0, 800, 600), "文章标题", children=[title, paragraph]
    )
    document.ControlTypeName = "DocumentControl"
    root = GeometryControl((0, 0, 800, 700), children=[document])
    client = WechatUiaClient(
        {
            "uia_share_browser_load_timeout_seconds": 0,
            "uia_share_browser_load_min_wait_ms": 0,
            "uia_share_browser_content_stable_polls": 1,
            "uia_share_browser_direct_read_ready_chars": 20,
        }
    )
    monkeypatch.setattr(auto, "ControlFromHandle", lambda _hwnd: root)
    monkeypatch.setattr(
        client,
        "_clipboard_unicode_after",
        lambda _action: (_ for _ in ()).throw(
            AssertionError("UIA document success must not touch the clipboard")
        ),
    )

    content = client._read_share_browser_content(200)

    assert "文章标题" in content
    assert "这是文章正文" in content


def test_share_browser_waits_until_uia_content_is_stable(monkeypatch):
    client = WechatUiaClient(
        {
            "uia_share_browser_load_timeout_seconds": 2,
            "uia_share_browser_load_poll_ms": 100,
            "uia_share_browser_load_min_wait_ms": 0,
            "uia_share_browser_content_stable_polls": 2,
            "uia_share_browser_direct_read_ready_chars": 20,
        }
    )
    clock = [0.0]
    probes = iter(
        [
            "",
            "页面正在加载",
            "页面标题\n这是加载完成后的完整正文内容。",
            "页面标题\n这是加载完成后的完整正文内容。",
        ]
    )
    calls = []

    def read_once(_hwnd):
        calls.append(clock[0])
        return next(probes)

    def wait(seconds):
        clock[0] += seconds
        return False

    monkeypatch.setattr(client, "_read_share_browser_uia_content_once", read_once)
    monkeypatch.setattr(client, "_stop_event", SimpleNamespace(wait=wait))
    monkeypatch.setattr(
        "channel.wechat_desktop.uia.client.time.monotonic", lambda: clock[0]
    )

    content = client._wait_for_share_browser_content(200)

    assert content == "页面标题\n这是加载完成后的完整正文内容。"
    assert len(calls) == 4


def test_share_context_contains_fetch_result():
    event = WechatDesktopEvent(
        "message",
        "conversation",
        "Alice",
        "alice",
        "Alice",
        "text",
        "这个视频讲了啥",
        reference={
            "sender_name": "颜料盒",
            "content_type": "share_card",
            "content": "分享标题",
            "platform": "哔哩哔哩",
            "url": "https://www.bilibili.com/video/BV1test",
            "fetch_status": "success",
            "fetched_content": "页面正文",
        },
    )

    heading, lines = _render_event_context_lines(event)

    assert heading == "[被引用的内容]"
    rendered = "\n".join(lines)
    assert "[第三方分享卡片] 分享标题" in rendered
    assert "页面正文" in rendered


def test_share_context_prefers_wechat_browser_content():
    event = WechatDesktopEvent(
        "message",
        "conversation",
        "Alice",
        "alice",
        "Alice",
        "text",
        "总结一下",
        reference={
            "sender_name": "Bob",
            "content_type": "share_card",
            "content": "分享标题",
            "browser_content": "微信浏览器直接读取的正文",
            "fetch_status": "direct_browser",
        },
    )

    _heading, lines = _render_event_context_lines(event)

    rendered = "\n".join(lines)
    assert "微信内置浏览器页面正文" in rendered
    assert "微信浏览器直接读取的正文" in rendered
    assert "WebFetch 网页结果不可用" not in rendered


def test_image_reference_opens_current_quote_region_without_locating_original(
    tmp_path,
):
    image_path = tmp_path / "quoted.png"
    image_path.write_bytes(b"png")
    client = FakeClient()
    calls = []
    client.fetch_referenced_message_image = lambda message, **kwargs: calls.append(
        ("viewer", message.content)
    ) or str(image_path)
    client.resolve_message_reference = lambda _message: (_ for _ in ()).throw(
        AssertionError("image reference must not locate original")
    )
    driver = WechatUiaMaterializer({}, gateway=WechatUiaGateway({}, client=client))
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

    resolved, resolve_count = materialize_native_target(driver, quoted)

    assert calls == [("viewer", "这张图")]
    assert resolve_count == 1
    assert resolved.reference["file_path"] == str(image_path)
    assert resolved.reference["resolved"] is True
    assert resolved.reference["strategy"] == "wechat_reference_image_viewer"


def test_file_reference_does_not_reuse_same_named_prefix_cache(tmp_path):
    cached_file = tmp_path / "report(1).pdf"
    cached_file.write_bytes(b"%PDF")
    client = FakeClient()
    calls = []
    client.resolve_message_reference = lambda message, **kwargs: calls.append(message.reference.content) or message
    client.file_paths["report(1).pdf"] = str(cached_file)
    driver = WechatUiaMaterializer({}, gateway=WechatUiaGateway({}, client=client))
    original = UiaChatMessage("Alice", "report(1).pdf", message_type="file", runtime_id="file-runtime")
    materialize_native_target(driver, original, local_id=2)
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

    resolved, resolve_count = materialize_native_target(driver, quoted)

    assert resolve_count == 1
    assert calls == ["report.pdf"]
    assert resolved.reference["file_path"] == ""
    assert resolved.reference["resolved"] is False


def test_file_reference_cache_miss_locates_original(tmp_path):
    resolved_file = tmp_path / "report.pdf"
    resolved_file.write_bytes(b"%PDF")
    client = FakeClient()
    calls = []

    def resolve_reference(message, **kwargs):
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
    driver = WechatUiaMaterializer({}, gateway=WechatUiaGateway({}, client=client))
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

    resolved, resolve_count = materialize_native_target(driver, quoted)

    assert calls == ["report.pdf"]
    assert resolve_count == 1
    assert resolved.reference["file_path"] == str(resolved_file)
    assert resolved.reference["strategy"] == "wechat_locate_original"


def test_attachment_cache_re_resolves_invalid_path_and_changed_identity(tmp_path):
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"
    first_path.write_bytes(b"one")
    second_path.write_bytes(b"two")
    client = FakeClient()
    client.image_paths["same"] = str(first_path)
    driver = WechatUiaMaterializer({}, gateway=WechatUiaGateway({}, client=client))
    first = UiaChatMessage(
        "Alice",
        "same",
        message_type="image",
        runtime_id="runtime-1",
        stable_id="stable-1",
    )

    resolved, first_count = materialize_native_target(driver, first)
    first_path.unlink()
    client.image_paths["same"] = str(second_path)
    refreshed, invalid_path_count = materialize_native_target(driver, first)
    changed_identity = UiaChatMessage(
        "Alice",
        "same",
        message_type="image",
        runtime_id="runtime-2",
        stable_id="stable-2",
    )
    _, identity_count = materialize_native_target(driver, changed_identity)

    assert resolved.content == str(first_path)
    assert refreshed.content == str(second_path)
    assert (first_count, invalid_path_count, identity_count) == (1, 1, 1)
    assert client.image_fetches == ["same", "same", "same"]


def test_waiting_reply_prevents_attachment_operation_from_reentering_uia():
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
