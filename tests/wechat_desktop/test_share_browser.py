"""微信桌面 share_browser 回归测试。"""
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from channel.wechat_desktop.models import HeaderInfo, UiaChatMessage, UiaReferencedMessage, WechatDesktopEvent
from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia.driver import WechatUiaDriver, _UiaPriorityCoordinator
from channel.wechat_desktop.pipeline.prompts import _render_event_context_lines
from .helpers import FakeClient, FakeHook, GeometryControl, incoming, row


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


def test_share_reference_fetches_url_after_uia_materialization():
    calls = []
    fetcher = SimpleNamespace(
        execute=lambda args: calls.append(args)
        or SimpleNamespace(status="success", result="Title: B站视频\nContent: 页面正文")
    )
    driver = WechatUiaDriver(
        {}, client=FakeClient(), shell_hook=FakeHook(), web_fetcher=fetcher
    )
    event = WechatDesktopEvent(
        "message",
        "conversation",
        "Alice",
        "alice",
        "Alice",
        "text",
        "这个视频讲了啥",
        reference={
            "content_type": "share_card",
            "content": "B站视频",
            "url": "https://www.bilibili.com/video/BV1test",
        },
    )

    result = driver._fetch_share_reference(event)

    assert calls == [{"url": "https://www.bilibili.com/video/BV1test"}]
    assert result.reference["fetch_status"] == "success"
    assert "页面正文" in result.reference["fetched_content"]


def test_share_reference_uses_wechat_browser_content_without_network_fetch():
    fetcher = SimpleNamespace(
        execute=lambda _args: (_ for _ in ()).throw(
            AssertionError("direct WeChat browser content must bypass web_fetch")
        )
    )
    driver = WechatUiaDriver(
        {},
        client=FakeClient(),
        shell_hook=FakeHook(),
        web_fetcher=fetcher,
    )
    event = WechatDesktopEvent(
        "message",
        "conversation",
        "Alice",
        "alice",
        "Alice",
        "text",
        "总结一下",
        reference={
            "content_type": "share_card",
            "content": "分享标题",
            "browser_content": "这是微信内置浏览器直接读取到的页面正文。",
            "browser_status": "success",
        },
    )

    result = driver._fetch_share_reference(event)

    assert result.reference["fetch_status"] == "direct_browser"
    assert result.reference["fetched_content"] == ""


def test_share_reference_without_url_never_calls_web_fetch():
    fetcher = SimpleNamespace(
        execute=lambda _args: (_ for _ in ()).throw(
            AssertionError("web_fetch must not run without a copied URL")
        )
    )
    driver = WechatUiaDriver(
        {}, client=FakeClient(), shell_hook=FakeHook(), web_fetcher=fetcher
    )
    event = WechatDesktopEvent(
        "message",
        "conversation",
        "Alice",
        "alice",
        "Alice",
        "text",
        "看看",
        reference={"content_type": "share_card", "content": "标题", "url": ""},
    )

    driver._fetch_share_reference(event)

    assert event.reference["fetch_status"] == "link_unavailable"


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
