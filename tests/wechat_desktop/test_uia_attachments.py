"""微信桌面 uia_attachments 回归测试。"""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from bridge.context import ContextType
from channel.wechat_desktop.uia.group_sender_ocr import OcrTextLine, RapidOcrGroupSenderResolver
from channel.wechat_desktop.models import (
    HeaderInfo,
    UiaChatMessage,
    WechatDesktopEvent,
    WechatDesktopMessage,
)
from channel.wechat_desktop.uia.operations import resolve_local_media_path
from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia.driver import WechatUiaDriver
from channel.wechat_desktop.pipeline.prompts import (
    _render_event_context_lines,
    _reply_requirements,
    _strip_group_bot_mentions,
)
from .helpers import (
    ClickableGeometryControl,
    FakeClient,
    FakeHook,
    GeometryControl,
    _selection_tree,
    incoming,
    row,
)


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


def test_image_capture_sequence_closes_viewer_and_restores_main(monkeypatch, tmp_path):
    import win32api
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
        win32api,
        "keybd_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
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

    def paced_wait(minimum_key, maximum_key):
        if minimum_key == "uia_image_viewer_before_close_ms_min":
            calls.append(
                (
                    "before_close_wait",
                    client.config[minimum_key],
                    client.config[maximum_key],
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
    assert calls == [("before_close_wait", 420, 420), "close"]


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


def test_cached_file_match_allows_one_decimal_size_rounding(tmp_path, monkeypatch):
    month = tmp_path / "wxid_example" / "msg" / "file" / "2026-08"
    month.mkdir(parents=True)
    filename = "【（应届）多模态算法工程师_北京 40-70K】林芮瑭 26年应届生(1)(1).pdf"
    cached = month / filename
    cached.write_bytes(b"x" * 1_284_386)
    client = WechatUiaClient({})
    monkeypatch.setattr(client, "_wechat_data_roots", lambda: [tmp_path])

    matched = client._find_cached_message_file(
        "文件\n【（应届）多模态算法工程师_北京 40-70K】林芮瑭 26年应届生(1).pdf\n"
        "1.2M\n微信电脑版"
    )

    assert matched == str(cached)
    assert WechatUiaClient._cached_file_size_limit("1.2M", 1_258_291) >= 26_095


def test_cached_file_match_strips_repeated_duplicate_suffix(tmp_path, monkeypatch):
    month = tmp_path / "wxid_example" / "msg" / "file" / "2026-08"
    month.mkdir(parents=True)
    cached = month / "岗位JD(1)(1).pdf"
    cached.write_bytes(b"x" * 4096)
    client = WechatUiaClient({})
    monkeypatch.setattr(client, "_wechat_data_roots", lambda: [tmp_path])

    matched = client._find_cached_message_file("文件\n岗位JD.pdf\n4.0K\n微信电脑版")

    assert matched == str(cached)


def test_cached_file_name_key_normalizes_fullwidth_and_duplicate_suffix():
    left = WechatUiaClient._cached_file_name_key(
        "【（应届）岗位.pdf"
    )
    right = WechatUiaClient._cached_file_name_key(
        "【(应届)岗位(1)(1).pdf"
    )

    assert left == right
    assert left[1] == ".pdf"


def test_xwechat_configured_file_roots_read_custom_documents(tmp_path, monkeypatch):
    appdata = tmp_path / "appdata"
    documents = tmp_path / "Wechat" / "Documents"
    files_root = documents / "xwechat_files"
    files_root.mkdir(parents=True)
    config_dir = appdata / "Tencent" / "xwechat" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "account.ini").write_text(str(documents), encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))

    roots = WechatUiaClient._xwechat_configured_file_roots()

    assert files_root.resolve() in [path.resolve() for path in roots]


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


def test_resolve_local_media_path_handles_file_urls(tmp_path: Path):
    img = tmp_path / "seedream_1.jpg"
    img.write_bytes(b"fake-image")
    expected = img.resolve()

    assert resolve_local_media_path(str(img)) == expected
    assert resolve_local_media_path(f"file://{img}") == expected
    assert resolve_local_media_path(img.as_uri()) == expected
    assert resolve_local_media_path(f"file://{img}").is_file()


def test_rapidocr_output_uses_official_boxes_txts_scores_api():
    output = SimpleNamespace(
        boxes=[[[1, 2], [11, 2], [11, 8], [1, 8]]],
        txts=("成员甲",),
        scores=(0.98,),
    )

    lines = RapidOcrGroupSenderResolver._output_lines(output, (100, 200))

    assert lines == [OcrTextLine("成员甲", 0.98, (101, 202, 111, 208))]


def test_rapidocr_preload_loads_engine_once(monkeypatch):
    created = {"n": 0}
    called = {"n": 0}

    class FakeEngine:
        def __call__(self, image):
            called["n"] += 1
            return SimpleNamespace(boxes=[], txts=(), scores=())

    def fake_rapidocr():
        created["n"] += 1
        return FakeEngine()

    import types
    import sys

    fake_mod = types.ModuleType("rapidocr")
    fake_mod.RapidOCR = fake_rapidocr
    monkeypatch.setitem(sys.modules, "rapidocr", fake_mod)

    resolver = RapidOcrGroupSenderResolver({"uia_group_sender_ocr_enabled": True})
    assert resolver.preload() is True
    assert resolver.preload() is True
    assert created["n"] == 1
    assert called["n"] >= 1
    assert resolver._get_engine() is resolver._engine


def test_rapidocr_preload_skipped_when_disabled():
    resolver = RapidOcrGroupSenderResolver({"uia_group_sender_ocr_enabled": False})
    assert resolver.preload() is False
    assert resolver._engine is None


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


def test_locate_conversation_rejects_click_that_leaves_previous_chat(monkeypatch):
    """A failed session switch must not look successful just because a message
    list is still visible from the previous private chat.
    """
    previous_message = GeometryControl(
        (400, 200, 900, 260), "私聊旧消息", "mmui::ChatTextItemView"
    )
    row = ClickableGeometryControl(
        (0, 100, 300, 160),
        "小小地下联络站",
        "mmui::ChatSessionCell",
        automation_id="session_item_小小地下联络站",
        # Click is a no-op: detail pane stays on 颜料盒.
    )
    # Active chat is private 颜料盒 while the target row is the group.
    root, _, _ = _selection_tree("颜料盒", [previous_message], row)
    client = WechatUiaClient({})

    @contextmanager
    def fake_root():
        yield root

    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_paced_wait", lambda *args, **kwargs: None)

    assert client.locate_conversation("小小地下联络站") is False
    assert row.click_count == 2


def test_locate_conversation_accepts_group_title_with_member_count(monkeypatch):
    """Detail headers often show '群名(n)' while session_item_/config use bare names."""
    message = GeometryControl(
        (400, 200, 900, 260), "群消息", "mmui::ChatTextItemView"
    )

    def activate():
        header.Name = "小小地下联络站(9)"
        message_list._children[:] = [message]

    row = ClickableGeometryControl(
        (0, 100, 300, 160),
        "小小地下联络站",
        "mmui::ChatSessionCell",
        automation_id="session_item_小小地下联络站",
        on_click=activate,
    )
    root, header, message_list = _selection_tree("颜料盒", [], row)
    client = WechatUiaClient({})

    @contextmanager
    def fake_root():
        yield root

    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_paced_wait", lambda *args, **kwargs: None)

    assert client.locate_conversation("小小地下联络站") is True
    assert row.click_count == 1


def test_get_title_strips_member_count_suffix(monkeypatch):
    header = GeometryControl(
        (400, 100, 700, 130),
        "小小地下联络站(9)",
        "Text",
        automation_id="current_chat_name_label",
    )
    root = GeometryControl((0, 0, 1000, 800), children=[header])
    client = WechatUiaClient({})

    @contextmanager
    def fake_root():
        yield root

    monkeypatch.setattr(client, "_uia_root", fake_root)
    info = client.get_title()
    assert info.title == "小小地下联络站"
    assert info.chat_number == 9
    assert info.header_type == "group"
