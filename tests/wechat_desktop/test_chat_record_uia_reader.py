"""合并记录 UIA 原气泡与辅助窗的归属、预算和失效边界。"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.models import UiaChatMessage, UiaReferencedMessage
from channel.wechat_desktop.uia.chat_record_reader import WechatChatRecordReader
from channel.wechat_desktop.uia.client import WechatUiaClient
from .helpers import GeometryControl
from .test_uia_reference import _strict_reference_client


def record():
    return {"title": "Synthetic 的聊天记录", "entries": [
        {"content_type": "text", "content": "可独立验证的真正正文", "resolved": True},
        {"content_type": "image", "content": "[图片，内容未读取]", "resolved": False}]}


def quoted():
    return UiaChatMessage("Synthetic", "问题", "text", "incoming", "42.12",
        (10, 300, 500, 400), reference=UiaReferencedMessage(
            "Synthetic", "[聊天记录]Synthetic 的聊天记录", "chat_record"))


def test_record_window_requires_title_and_actual_body_anchor():
    assert not WechatChatRecordReader._matches_record(["聊天记录"], record())
    assert not WechatChatRecordReader._matches_record([record()["title"]], record())
    assert not WechatChatRecordReader._matches_record(
        [record()["title"], "[图片，内容未读取]"], record())
    assert WechatChatRecordReader._matches_record(
        [record()["title"], "可独立验证的真正正文"], record())


def test_native_record_window_server_id_is_a_strong_anchor_even_for_first_image_page():
    value = record() | {"source_message_id": "1782205151168095427"}
    summary = {"title": value["title"], "uia_class_name": "mmui::RecordDetailWindow",
               "automation_id": "1782205151168095427:"}
    assert WechatChatRecordReader._matches_native_record(summary, value)
    assert not WechatChatRecordReader._matches_native_record(
        summary | {"automation_id": "1782205151168095428:"}, value)
    assert not WechatChatRecordReader._matches_native_record(
        summary | {"uia_class_name": "mmui::SearchMsgUniqueChatWindow"}, value)
    assert not WechatChatRecordReader._matches_native_record(
        summary | {"title": "另一个记录"}, value)
    assert not WechatChatRecordReader._matches_native_record(summary, record())


def test_same_title_window_reuse_with_another_record_server_id_is_rejected(monkeypatch):
    import uiautomation as auto
    import win32gui

    reader = WechatUiaClient({})._chat_record_reader
    main = (100, 42, "微信", "mmui::MainWindow")
    window = (200, 42, record()["title"], "Qt51514QWindowIcon")
    root = SimpleNamespace(ClassName="mmui::RecordDetailWindow", AutomationId="1782205151168095428:")
    monkeypatch.setattr(reader, "_native_identity", lambda hwnd: main if hwnd == 100 else window)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 200)
    monkeypatch.setattr(auto, "ControlFromHandle", lambda hwnd: root)
    with pytest.raises(RuntimeError, match="chat_record_window_changed"):
        reader._window_root(window, main, lambda: None, "1782205151168095427")


def test_real_card_name_extracts_only_record_title_and_refines_reference_type():
    name = "聊天记录南街和九月底的聊天记录南街: [图片]\n南街: [图片]"
    assert WechatUiaClient._chat_record_card_title(name) == "南街和九月底的聊天记录"
    assert WechatUiaClient._message_type("mmui::ChatBubbleItemView", name) == "chat_record"
    assert WechatUiaClient._reference_message_type("[聊天记录]南街和九月底的聊天记录") == "chat_record"
    assert WechatUiaClient._message_type("mmui::ChatTextItemView", name) == "text"
    assert not WechatUiaClient._chat_record_card_title("聊天记录南街和九月底的聊天记录无归属尾巴")
    assert not WechatUiaClient._chat_record_card_title("问题引用 Synthetic 的消息 : [聊天记录]南街和九月底的聊天记录")


def _reader(monkeypatch, windows):
    client = WechatUiaClient({})
    reader = client._chat_record_reader
    state = SimpleNamespace(valid=True, actions=[], closed=False)
    main = (100, 42, "微信", "mmui::MainWindow")
    original = GeometryControl((30, 30, 400, 100),
        "[聊天记录]Synthetic 的聊天记录", "mmui::ChatBubbleItemView")
    original.GetRuntimeId = lambda: (42, 11)
    message = UiaChatMessage("Synthetic", original.Name, "chat_record", "unknown", "42.11", (30, 30, 400, 100))

    @contextmanager
    def located(_message, *, validate_target, cleanup_allowed):
        validate_target()
        yield original, message, validate_target
        if cleanup_allowed():
            state.actions.append("return")

    monkeypatch.setattr(client, "get_owner_window_handle", lambda: 100)
    monkeypatch.setattr(reader, "_native_identity", lambda hwnd: main if hwnd == 100 else windows[0][0])
    monkeypatch.setattr(client._reference_resolver, "located_original_reference", located)
    monkeypatch.setattr(client, "_visible_top_level_window_handles", lambda: {100})
    monkeypatch.setattr(client, "_click_control_point", lambda control: state.actions.append("open") or True)
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)
    monkeypatch.setattr(reader, "_new_windows", lambda *args: windows)
    monkeypatch.setattr(reader, "_close_window", lambda *args: state.actions.append("close") or True)
    monkeypatch.setattr(reader, "_restore_main", lambda *args: state.actions.append("restore") or True)
    monkeypatch.setattr(client._reference_resolver, "return_to_reference_in_main",
                        lambda **kwargs: state.actions.append("return") or True)
    return reader, state


def test_diagnostics_observes_verified_window_and_closes_only_this_window(monkeypatch, tmp_path):
    windows = [((200, 42, "聊天记录", "mmui::SyntheticRecordWindow"),
        {"hwnd": 200, "title": "聊天记录", "class_name": "mmui::SyntheticRecordWindow"},
        [record()["title"], "可独立验证的真正正文"])]
    reader, state = _reader(monkeypatch, windows)
    result = reader.fetch_referenced_chat_record(quoted(), record(), tmp_root=tmp_path,
        validate_target=lambda: state.valid, diagnostics_only=True)
    assert result["status"] == "observed"
    assert result["evidence_paths"] == [] and result["media_status"] == "unread"
    assert state.actions == ["open", "close", "restore", "return"]


def test_unknown_auxiliary_window_is_not_closed_and_underlying_return_is_not_clicked(monkeypatch, tmp_path):
    windows = [((200, 42, "无关辅助窗", "mmui::SyntheticRecordWindow"),
                {"hwnd": 200, "title": "无关辅助窗"}, ["可独立验证的真正正文"])]
    reader, state = _reader(monkeypatch, windows)
    result = reader.fetch_referenced_chat_record(quoted(), record(), tmp_root=tmp_path,
        validate_target=lambda: state.valid, diagnostics_only=True)
    assert result["status"] == "unavailable"
    assert result["strategy"] == "uia_chat_record_window_ambiguous"
    assert state.actions == ["open"]


def test_provided_native_id_never_falls_back_to_matching_title_and_body(monkeypatch, tmp_path):
    value = record() | {"source_message_id": "1782205151168095427"}
    windows = [((200, 42, value["title"], "Qt51514QWindowIcon"),
        {"hwnd": 200, "title": value["title"], "uia_class_name": "mmui::RecordDetailWindow",
         "automation_id": "1782205151168095428:"}, [value["title"], "可独立验证的真正正文"])]
    reader, state = _reader(monkeypatch, windows)
    result = reader.fetch_referenced_chat_record(quoted(), value, tmp_root=tmp_path,
        validate_target=lambda: state.valid, diagnostics_only=True)
    assert result["status"] == "unavailable" and result["strategy"] == "uia_chat_record_window_ambiguous"
    assert state.actions == ["open"]


def test_record_target_change_after_open_skips_all_remaining_actions(monkeypatch, tmp_path):
    reader, state = _reader(monkeypatch, [])

    def click(_control):
        state.actions.append("open")
        state.valid = False
        return True

    monkeypatch.setattr(reader.client, "_click_control_point", click)
    with pytest.raises(RuntimeError, match="attachment_target_changed"):
        reader.fetch_referenced_chat_record(quoted(), record(), tmp_root=tmp_path,
            validate_target=lambda: state.valid, diagnostics_only=True)
    assert state.actions == ["open"]


def test_record_source_change_before_open_does_not_act(monkeypatch, tmp_path):
    reader, state = _reader(monkeypatch, [])
    with pytest.raises(RuntimeError, match="attachment_source_changed"):
        reader.fetch_referenced_chat_record(quoted(), record(), tmp_root=tmp_path,
            validate_target=lambda: state.valid, validate_source=lambda: False)
    assert state.actions == []


def test_unknown_datatype_is_never_treated_as_an_image_or_body_anchor():
    value = {"title": "Synthetic 的聊天记录", "entries": [
        {"content_type": "unsupported", "native_type": 37,
         "content": "不应认作表情的文本", "resolved": False}]}
    assert not WechatChatRecordReader._matches_record(
        [value["title"], "不应认作表情的文本"], value)


def test_located_context_reacquires_quote_runtime_and_restores_after_reader(monkeypatch):
    client, message, quote_control, original, state = _strict_reference_client(monkeypatch)
    _isolate_locator(monkeypatch, client, state)
    with client._reference_resolver.located_original_reference(message,
            validate_target=lambda: state.valid) as (control, snapshot, require):
        assert control is original and snapshot.runtime_id == "42.11"
        require()
        state.actions.append("read")
    assert (120, 386) in state.actions
    assert state.actions.index("read") < state.actions.index("return")


def test_located_context_runtime_change_never_uses_stale_bounds(monkeypatch):
    client, message, quote_control, original, state = _strict_reference_client(monkeypatch)
    _isolate_locator(monkeypatch, client, state)
    quote_control.GetRuntimeId = lambda: (42, 999)
    with pytest.raises(RuntimeError, match="uia_reference_runtime_changed"):
        with client._reference_resolver.located_original_reference(message,
                validate_target=lambda: state.valid):
            pytest.fail("身份不符不得发布原气泡")
    assert "right_click" not in state.actions and "return" not in state.actions


def test_located_context_invalidated_while_reading_does_not_restore(monkeypatch):
    client, message, quote_control, original, state = _strict_reference_client(monkeypatch)
    _isolate_locator(monkeypatch, client, state)
    with pytest.raises(RuntimeError, match="attachment_target_changed"):
        with client._reference_resolver.located_original_reference(message,
                validate_target=lambda: state.valid) as (_, _, require):
            state.actions.append("read")
            state.valid = False
            require()
    assert "return" not in state.actions and "end" not in state.actions


def _isolate_locator(monkeypatch, client, state):
    monkeypatch.setattr(client, "_window", lambda: (100, 42))
    monkeypatch.setattr(client, "_visible_top_level_window_handles", lambda: {100})
    monkeypatch.setattr(client._reference_resolver, "_menu_owned_by_main", lambda *args: True)
    monkeypatch.setattr(client._reference_resolver, "return_to_reference_in_main",
        lambda **kwargs: state.actions.append("return") or True)


def test_visible_page_uses_actual_visible_geometry_and_never_pairs_entries(monkeypatch):
    reader = WechatUiaClient({})._chat_record_reader
    hidden = GeometryControl((0, 1000, 200, 1100), "未显示内容")
    visible = GeometryControl((10, 30, 200, 100), "实际可见正文")
    root = GeometryControl((0, 0, 400, 500), children=[hidden, visible])
    texts, scrollable = reader._visible_page(root, (0, 0, 400, 500))
    assert "实际可见正文" in texts and "未显示内容" not in texts
    assert scrollable is None


def test_record_recycler_list_is_scroll_target_not_its_list_items_or_viewport():
    reader = WechatUiaClient({})._chat_record_reader
    items = [GeometryControl((10, 30 + i * 100, 300, 100 + i * 100),
        "[图片]", "mmui::ChatBubbleReferItemView") for i in range(3)]
    for item in items:
        item.ControlTypeName = "ListItemControl"
    viewport = GeometryControl((0, 0, 350, 450), "", "qt_scrollarea_viewport")
    viewport.ControlTypeName = "CustomControl"
    record_list = GeometryControl((0, 0, 350, 450), "", "mmui::RecyclerListView", items)
    record_list.ControlTypeName = "ListControl"
    root = GeometryControl((0, 0, 400, 500), children=[record_list, viewport])
    _, scrollable = reader._visible_page(root, (0, 0, 400, 500))
    assert scrollable is record_list


def test_two_actual_record_lists_are_ambiguous_and_do_not_scroll():
    reader = WechatUiaClient({})._chat_record_reader
    lists = [GeometryControl((0, 0, 350, 450), "", "mmui::RecyclerListView") for _ in range(2)]
    for control in lists:
        control.ControlTypeName = "ListControl"
    root = GeometryControl((0, 0, 400, 500), children=lists)
    assert reader._visible_page(root, (0, 0, 400, 500))[1] is None


def _record_image_items():
    images = []
    for index in range(2):
        item = GeometryControl((10, 30 + index * 100, 300, 100 + index * 100),
            f"Synthetic 11:3{index} 图片", "mmui::ChatBubbleReferItemView")
        item.GetRuntimeId = lambda value=index: (42, value + 20)
        images.append(item)
    unknown = GeometryControl((10, 250, 300, 320), "未知原生37", "mmui::ChatBubbleReferItemView")
    unknown.GetRuntimeId = lambda: (42, 37)
    text = GeometryControl((10, 330, 300, 400), "这是提及图片的文字", "mmui::ChatTextItemView")
    text.GetRuntimeId = lambda: (42, 38)
    return GeometryControl((0, 0, 400, 500), children=[*images, unknown, text])


def test_record_full_images_dedupe_image_identity_and_do_not_zip_or_read_unknown_items(monkeypatch, tmp_path):
    client = WechatUiaClient({})
    reader = client._chat_record_reader
    state = SimpleNamespace(viewer=False, paths=[])
    identity, main = (200, 42, "记录", "QtWindow"), (100, 42, "微信", "mmui::MainWindow")
    root = _record_image_items()

    def window(*args, require_foreground=True):
        if state.viewer:
            assert require_foreground is False, "viewer活跃时记录前台校验必须关闭"
        return root

    def capture(point, destination, **kwargs):
        kwargs["validate_before_click"]()
        assert kwargs["return_hwnd"] == 200 and kwargs["prefer_original"] is True
        state.viewer = True
        kwargs["validate_context"]()
        kwargs["validate_return_window"]()
        state.viewer = False
        kwargs["capture_metadata"].update(capture_kind="clipboard_original", width=800, height=4000)
        destination.write_bytes(b"original pixels")
        state.paths.append(str(destination))
        return str(destination)

    monkeypatch.setattr(reader, "_window_root", window)
    monkeypatch.setattr(client, "_capture_image_viewer_from_point", capture)
    result = {"image_evidence_paths": [], "image_evidence": [], "truncated": False}
    seen = set()
    for _ in range(2):
        reader._capture_page_images(identity, main, "123", root, (0, 0, 400, 500),
            tmp_path, seen, result, lambda: None, lambda: None, lambda: None)
    assert len(state.paths) == len(result["image_evidence_paths"]) == 2
    assert {key[0] for key in seen} == {"42.20", "42.21"}
    assert len(seen) == 2
    assert all(item["capture_kind"] == "clipboard_original" for item in result["image_evidence"])
    assert all(set(item) == {"path", "capture_kind", "width", "height"} for item in result["image_evidence"])


def test_record_original_image_budget_is_bounded(monkeypatch, tmp_path):
    client = WechatUiaClient({"chat_record_uia_max_images": 1})
    reader = client._chat_record_reader
    root = _record_image_items()
    monkeypatch.setattr(reader, "_window_root", lambda *args, **kwargs: root)
    monkeypatch.setattr(client, "_capture_image_viewer_from_point", lambda point, path, **kwargs: str(path))
    result = {"image_evidence_paths": [], "image_evidence": [], "truncated": False}
    reader._capture_page_images((200, 42, "记录", "Qt"), (100, 42, "微信", "mmui::MainWindow"),
        "123", root, (0, 0, 400, 500), tmp_path, set(), result,
        lambda: None, lambda: None, lambda: None)
    assert len(result["image_evidence_paths"]) == 1 and result["truncated"] is True


def _one_recycled_image(bounds=(10, 30, 110, 130)):
    image = GeometryControl(bounds, "Synthetic 11:30 图片", "mmui::ChatBubbleReferItemView")
    image.GetRuntimeId = lambda: (42, 20)
    return image, GeometryControl((0, 0, 400, 500), children=[image])


def _paint_image_row(image, color):
    from PIL import Image, ImageDraw

    page = Image.new("RGB", (400, 500), "white")
    from channel.wechat_desktop.uia.controls import _bounds
    ImageDraw.Draw(page).rectangle(_bounds(image), fill=color)
    return page


def test_recycler_reused_runtime_and_name_with_different_image_is_not_skipped(monkeypatch, tmp_path):
    client = WechatUiaClient({})
    reader = client._chat_record_reader
    image, root = _one_recycled_image()
    monkeypatch.setattr(reader, "_window_root", lambda *args, **kwargs: root)
    state = SimpleNamespace(attempts=0)

    def capture(point, path, **kwargs):
        state.attempts += 1
        path.write_bytes(f"original-{state.attempts}".encode())
        return str(path)

    monkeypatch.setattr(client, "_capture_image_viewer_from_point", capture)
    result = {"image_evidence_paths": [], "image_evidence": [], "truncated": False}
    seen, content_seen, diagnostic = set(), set(), {}
    for color in ("red", "blue"):
        reader._capture_page_images((200, 42, "记录", "Qt"), (100, 42, "微信", "mmui::MainWindow"),
            "123", root, (0, 0, 400, 500), tmp_path, seen, result,
            lambda: None, lambda: None, lambda: None, page_image=_paint_image_row(image, color),
            window_bounds=(0, 0, 400, 500), content_seen=content_seen, diagnostics=diagnostic)
    assert len(result["image_evidence_paths"]) == state.attempts == 2
    assert diagnostic["runtime_reuses"] == 1


@pytest.mark.parametrize("recycled_runtime", [False, True])
def test_same_image_in_overlapping_page_with_moved_bounds_is_skipped(monkeypatch, tmp_path, recycled_runtime):
    client = WechatUiaClient({})
    reader = client._chat_record_reader
    image, root = _one_recycled_image()
    monkeypatch.setattr(reader, "_window_root", lambda *args, **kwargs: root)
    state = SimpleNamespace(attempts=0)

    def capture(point, path, **kwargs):
        state.attempts += 1
        path.write_bytes(b"same original")
        return str(path)

    monkeypatch.setattr(client, "_capture_image_viewer_from_point", capture)
    result = {"image_evidence_paths": [], "image_evidence": [], "truncated": False}
    seen = set()
    for top in (30, 60):
        image.BoundingRectangle.top, image.BoundingRectangle.bottom = top, top + 100
        if top == 60 and recycled_runtime:
            image.GetRuntimeId = lambda: (42, 22)
        reader._capture_page_images((200, 42, "记录", "Qt"), (100, 42, "微信", "mmui::MainWindow"),
            "123", root, (0, 0, 400, 500), tmp_path, seen, result,
            lambda: None, lambda: None, lambda: None, page_image=_paint_image_row(image, "blue"),
            window_bounds=(0, 0, 400, 500), content_seen=set())
    assert state.attempts == len(result["image_evidence_paths"]) == 1


def test_partial_image_reappearing_in_full_dedupes_actual_original_evidence(monkeypatch, tmp_path):
    client = WechatUiaClient({})
    reader = client._chat_record_reader
    image, root = _one_recycled_image()
    monkeypatch.setattr(reader, "_window_root", lambda *args, **kwargs: root)
    state = SimpleNamespace(attempts=0)

    def capture(point, path, **kwargs):
        state.attempts += 1
        path.write_bytes(b"same native original")
        return str(path)

    monkeypatch.setattr(client, "_capture_image_viewer_from_point", capture)
    result = {"image_evidence_paths": [], "image_evidence": [], "truncated": False}
    seen, content_seen, diagnostic = set(), set(), {}
    for viewport in ((0, 60, 400, 500), (0, 0, 400, 500)):
        reader._capture_page_images((200, 42, "记录", "Qt"), (100, 42, "微信", "mmui::MainWindow"),
            "123", root, viewport, tmp_path, seen, result,
            lambda: None, lambda: None, lambda: None, page_image=_paint_image_row(image, "blue"),
            window_bounds=(0, 0, 400, 500), content_seen=content_seen, diagnostics=diagnostic)
    assert state.attempts == 2 and len(result["image_evidence_paths"]) == 1
    assert diagnostic["image_content_duplicates"] == 1


def test_bottom_partial_image_is_deferred_until_fully_visible(monkeypatch, tmp_path):
    client = WechatUiaClient({})
    reader = client._chat_record_reader
    image, root = _one_recycled_image((10, 430, 110, 550))
    monkeypatch.setattr(reader, "_window_root", lambda *args, **kwargs: root)
    attempts = []
    monkeypatch.setattr(client, "_capture_image_viewer_from_point",
        lambda point, path, **kwargs: attempts.append(point) or str(path))
    result = {"image_evidence_paths": [], "image_evidence": [], "truncated": False}
    seen = set()
    first = {"index": 0}
    reader._capture_page_images((200, 42, "记录", "Qt"), (100, 42, "微信", "mmui::MainWindow"),
        "123", root, (0, 0, 400, 500), tmp_path, seen, result,
        lambda: None, lambda: None, lambda: None, page_image=_paint_image_row(image, "blue"),
        window_bounds=(0, 0, 400, 500), diagnostics=first)
    assert attempts == [] and first["image_partial_deferred"] == 1
    image.BoundingRectangle.top, image.BoundingRectangle.bottom = 330, 450
    reader._capture_page_images((200, 42, "记录", "Qt"), (100, 42, "微信", "mmui::MainWindow"),
        "123", root, (0, 0, 400, 500), tmp_path, seen, result,
        lambda: None, lambda: None, lambda: None, page_image=_paint_image_row(image, "blue"),
        window_bounds=(0, 0, 400, 500), diagnostics={"index": 1})
    assert len(attempts) == len(result["image_evidence_paths"]) == 1


def test_captured_image_clipped_at_top_of_later_page_is_not_reopened(monkeypatch, tmp_path):
    client = WechatUiaClient({})
    reader = client._chat_record_reader
    image, root = _one_recycled_image()
    monkeypatch.setattr(reader, "_window_root", lambda *args, **kwargs: root)
    attempts = []
    monkeypatch.setattr(client, "_capture_image_viewer_from_point",
        lambda point, path, **kwargs: attempts.append(point) or str(path))
    result = {"image_evidence_paths": [], "image_evidence": [], "truncated": False}
    seen = set()
    for index, top in enumerate((30, -30)):
        image.BoundingRectangle.top, image.BoundingRectangle.bottom = top, top + 100
        reader._capture_page_images((200, 42, "记录", "Qt"), (100, 42, "微信", "mmui::MainWindow"),
            "123", root, (0, 0, 400, 500), tmp_path, seen, result,
            lambda: None, lambda: None, lambda: None, page_image=_paint_image_row(image, "blue"),
            window_bounds=(0, 0, 400, 500), diagnostics={"index": index})
    assert len(attempts) == 1


@pytest.mark.parametrize("bounds,diagnostic", [
    ((10, 30, 110, 700), {"index": 1}),
    ((10, 430, 110, 550), {"index": 2, "repeated": True})])
def test_oversized_or_final_page_partial_image_has_bounded_fallback(monkeypatch, tmp_path, bounds, diagnostic):
    client = WechatUiaClient({})
    reader = client._chat_record_reader
    image, root = _one_recycled_image(bounds)
    monkeypatch.setattr(reader, "_window_root", lambda *args, **kwargs: root)
    attempts = []
    monkeypatch.setattr(client, "_capture_image_viewer_from_point",
        lambda point, path, **kwargs: attempts.append(point) or str(path))
    result = {"image_evidence_paths": [], "image_evidence": [], "truncated": False}
    reader._capture_page_images((200, 42, "记录", "Qt"), (100, 42, "微信", "mmui::MainWindow"),
        "123", root, (0, 0, 400, 500), tmp_path, set(), result,
        lambda: None, lambda: None, lambda: None, page_image=_paint_image_row(image, "blue"),
        window_bounds=(0, 0, 400, 500), diagnostics=diagnostic)
    assert len(attempts) == diagnostic["image_partial_forced"] == 1


def test_full_image_key_keeps_original_pixels_instead_of_downsampled_similarity():
    reader = WechatUiaClient({})._chat_record_reader
    image, root = _one_recycled_image()
    first = _paint_image_row(image, "blue")
    different = first.copy()
    different.putpixel((20, 40), (0, 0, 254))
    assert reader._image_visual_key(image, first, (0, 0, 400, 500), (0, 0, 400, 500)) != (
        reader._image_visual_key(image, different, (0, 0, 400, 500), (0, 0, 400, 500)))


def test_page_signature_ignores_clock_and_unknown_animation_pixels_but_tracks_images():
    from PIL import ImageDraw

    reader = WechatUiaClient({})._chat_record_reader
    image, root = _one_recycled_image()
    unknown = GeometryControl((10, 200, 110, 300), "未知原生37", "mmui::ChatBubbleReferItemView")
    root._children.append(unknown)
    page = _paint_image_row(image, "blue")
    changed = page.copy()
    draw = ImageDraw.Draw(changed)
    draw.rectangle((10, 200, 110, 300), fill="red")
    draw.rectangle((250, 0, 350, 20), fill="red")
    state = {"vertical_percent": None}

    def signature(value):
        return reader._page_fingerprint(root, (0, 0, 400, 500), (0, 0, 400, 500),
            value, ["静止记录正文"], state, lambda: None)

    assert signature(page) == signature(changed)
    draw.rectangle((10, 30, 110, 130), fill="red")
    assert signature(page) != signature(changed)


def test_record_image_candidates_and_clicks_use_list_viewport(monkeypatch, tmp_path):
    client = WechatUiaClient({})
    reader = client._chat_record_reader
    image, root = _one_recycled_image((10, 10, 110, 110))
    hidden = GeometryControl((10, 0, 110, 40), "Synthetic 图片", "mmui::ChatBubbleReferItemView")
    hidden.GetRuntimeId = lambda: (42, 21)
    root._children.append(hidden)
    monkeypatch.setattr(reader, "_window_root", lambda *args, **kwargs: root)
    points = []
    monkeypatch.setattr(client, "_capture_image_viewer_from_point",
        lambda point, path, **kwargs: points.append(point) or str(path))
    result = {"image_evidence_paths": [], "image_evidence": [], "truncated": False}
    reader._capture_page_images((200, 42, "记录", "Qt"), (100, 42, "微信", "mmui::MainWindow"),
        "123", root, (0, 50, 400, 500), tmp_path, set(), result,
        lambda: None, lambda: None, lambda: None)
    assert len(points) == 1 and 50 <= points[0][1] <= 110


def test_record_scroll_pattern_uses_small_increment():
    import uiautomation as auto

    reader = WechatUiaClient({})._chat_record_reader
    calls = []
    pattern = SimpleNamespace(VerticallyScrollable=True,
        Scroll=lambda horizontal, vertical, **kwargs: calls.append((horizontal, vertical)))
    control = SimpleNamespace(GetScrollPattern=lambda: pattern,
        WheelDown=lambda **kwargs: pytest.fail("有效 ScrollPattern 不该使用滚轮"))
    assert reader._scroll(control, lambda: None)
    assert calls == [(auto.ScrollAmount.NoAmount, auto.ScrollAmount.SmallIncrement)]


def test_record_scroll_fallback_defaults_to_two_wheel_steps():
    reader = WechatUiaClient({})._chat_record_reader
    calls = []
    control = SimpleNamespace(GetScrollPattern=lambda: None,
        WheelDown=lambda **kwargs: calls.append(kwargs))
    assert reader._scroll(control, lambda: None)
    assert calls[0]["wheelTimes"] == 2


def test_record_scroll_bottom_percent_survives_unsupported_view_size():
    control = SimpleNamespace(GetScrollPattern=lambda: SimpleNamespace(VerticalScrollPercent=100))
    assert WechatChatRecordReader._scroll_state(control) == {
        "vertical_percent": 100, "view_size": None, "at_bottom": True}


def _capture_reader(monkeypatch):
    import uiautomation as auto
    import win32api
    import win32gui
    from PIL import ImageGrab

    windows = [((200, 42, "聊天记录", "mmui::SyntheticRecordWindow"),
        {"hwnd": 200, "title": "聊天记录"}, [record()["title"], "可独立验证的真正正文"])]
    reader, state = _reader(monkeypatch, windows)

    @contextmanager
    def initializer():
        yield

    class Image:
        def tobytes(self):
            return b"same visible record image"

        def save(self, path, **kwargs):
            path.write_bytes(b"synthetic evidence")

    monkeypatch.setattr(auto, "UIAutomationInitializerInThread", initializer)
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda hwnd: (0, 0, 300, 400))
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 200)
    state.cursor = (300, 300)
    monkeypatch.setattr(win32api, "GetCursorPos", lambda: state.cursor)
    monkeypatch.setattr(win32api, "SetCursorPos", lambda point: setattr(state, "cursor", point))
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: Image())
    monkeypatch.setattr(reader, "_window_root", lambda *args: GeometryControl((0, 0, 300, 400)))
    monkeypatch.setattr(reader, "_visible_page", lambda *args: (["实际可见正文"], object()))
    monkeypatch.setattr(reader, "_capture_page_images", lambda *args, **kwargs: None)
    monkeypatch.setattr(reader, "_scroll", lambda *args: state.actions.append("scroll") or True)
    return reader, state


def test_repeated_page_stops_after_two_attempts_without_progress(monkeypatch, tmp_path):
    reader, state = _capture_reader(monkeypatch)
    result = reader.fetch_referenced_chat_record(quoted(), record(), tmp_root=tmp_path,
        validate_target=lambda: state.valid)
    assert result["status"] == "partial" and result["media_status"] == "evidence_only"
    assert result["page_count"] == 1 and len(result["evidence_paths"]) == 1
    assert result["visible_text"] == "实际可见正文"
    assert state.actions.count("scroll") == 2
    assert state.actions[-3:] == ["close", "restore", "return"]


def test_page_budget_marks_truncation_without_an_extra_scroll(monkeypatch, tmp_path):
    reader, state = _capture_reader(monkeypatch)
    reader.client.config["chat_record_uia_max_pages"] = 1
    result = reader.fetch_referenced_chat_record(quoted(), record(), tmp_root=tmp_path,
        validate_target=lambda: state.valid)
    assert result["page_count"] == 1 and result["truncated"] is True
    assert "scroll" not in state.actions


@pytest.mark.parametrize("user_moves", [False, True])
def test_record_cursor_parks_on_title_and_only_restores_its_own_location(monkeypatch, user_moves):
    reader, state = _capture_reader(monkeypatch)
    original = state.cursor
    with reader._park_record_cursor(reader._native_identity(200), reader._native_identity(100),
            "", lambda: None, lambda: None) as park:
        park()
        assert state.cursor == (60, 10)
        if user_moves:
            state.cursor = (305, 307)
    assert state.cursor == ((305, 307) if user_moves else original)


def test_record_cursor_source_failure_skips_restoring_old_location(monkeypatch):
    reader, state = _capture_reader(monkeypatch)

    def require_source():
        if not state.valid:
            raise RuntimeError("attachment_target_changed")

    def window(identity, main, require, *args):
        require()
        return GeometryControl((0, 0, 300, 400))

    monkeypatch.setattr(reader, "_window_root", window)
    with pytest.raises(RuntimeError, match="attachment_target_changed"):
        with reader._park_record_cursor(reader._native_identity(200), reader._native_identity(100),
                "", lambda: None, require_source) as park:
            park()
            state.valid = False
    assert state.cursor == (60, 10)


@pytest.mark.parametrize("elapsed,expected_strategy", [(70, "uia_chat_record_visible_evidence"),
                                                      (185, "uia_chat_record_timeout")])
def test_record_time_budget_allows_seven_slow_images_but_is_capped_at_three_minutes(monkeypatch, tmp_path,
                                                                           elapsed, expected_strategy):
    import channel.wechat_desktop.uia.chat_record_reader as reader_module

    reader, state = _capture_reader(monkeypatch)
    reader.client.config.update(chat_record_uia_max_pages=1, chat_record_uia_timeout_seconds=999)
    state.now = 0.
    monkeypatch.setattr(reader_module.time, "monotonic", lambda: state.now)

    def capture(*args, **kwargs):
        state.now = elapsed
        args[-2]()

    monkeypatch.setattr(reader, "_capture_page_images", capture)
    result = reader.fetch_referenced_chat_record(quoted(), record(), tmp_root=tmp_path,
        validate_target=lambda: state.valid)
    assert result["strategy"] == expected_strategy
    assert "unique_image_evidence" in result["read_diagnostics"]


def test_main_restore_failure_never_clicks_return_on_another_foreground_app(monkeypatch, tmp_path):
    windows = [((200, 42, "聊天记录", "mmui::SyntheticRecordWindow"),
        {"hwnd": 200, "title": "聊天记录"}, [record()["title"], "可独立验证的真正正文"])]
    reader, state = _reader(monkeypatch, windows)
    monkeypatch.setattr(reader, "_restore_main", lambda *args: False)
    result = reader.fetch_referenced_chat_record(quoted(), record(), tmp_root=tmp_path,
        validate_target=lambda: state.valid, diagnostics_only=True)
    assert result["main_restore_failed"] is True
    assert state.actions == ["open", "close"]


def test_scroll_source_change_during_pattern_failure_never_wheels_an_old_control():
    state = SimpleNamespace(valid=True, actions=[])

    def scroll(*args, **kwargs):
        state.actions.append("scroll_pattern")
        state.valid = False
        raise RuntimeError("模式在滚动中失败")

    control = SimpleNamespace(GetScrollPattern=lambda: SimpleNamespace(
        VerticallyScrollable=True, Scroll=scroll),
        WheelDown=lambda **kwargs: state.actions.append("wheel"))

    def require():
        if not state.valid:
            raise RuntimeError("attachment_target_changed")

    with pytest.raises(RuntimeError, match="attachment_target_changed"):
        WechatUiaClient({})._chat_record_reader._scroll(control, require)
    assert state.actions == ["scroll_pattern"]


def test_main_restore_source_change_after_show_skips_all_further_actions(monkeypatch):
    import win32gui

    reader = WechatUiaClient({})._chat_record_reader
    identity = (100, 42, "微信", "mmui::MainWindow")
    state = SimpleNamespace(valid=True, actions=[])
    monkeypatch.setattr(reader, "_native_identity", lambda hwnd: identity)

    def show(*args):
        state.actions.append("show")
        state.valid = False

    monkeypatch.setattr(win32gui, "ShowWindow", show)
    monkeypatch.setattr(win32gui, "BringWindowToTop", lambda *args: state.actions.append("top"))
    monkeypatch.setattr(win32gui, "SetForegroundWindow", lambda *args: state.actions.append("foreground"))

    def require():
        if not state.valid:
            raise RuntimeError("attachment_target_changed")

    with pytest.raises(RuntimeError, match="attachment_target_changed"):
        reader._restore_main(identity, require)
    assert state.actions == ["show"]
