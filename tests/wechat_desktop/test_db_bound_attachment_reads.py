"""数据库物化只能使用已绑定气泡的文件路径，不能跨账号猜同名文件。"""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.models import HeaderInfo, UiaChatMessage, UiaReferencedMessage
from channel.wechat_desktop.uia.client import WechatUiaClient
from .helpers import GeometryControl, FakeClient
from .test_uia_materializer import materializer_for, native_event


def bound_file_client(monkeypatch, *, timeout=0, save_as=False):
    client = WechatUiaClient({"uia_file_download_timeout_seconds": timeout,
                              "uia_file_save_as_enabled": save_as})
    state = SimpleNamespace(hwnd=123, pid=42, title="Synthetic", copies=0, focus=0)
    control = GeometryControl((10, 10, 100, 100), "文件\nsynthetic.pdf\n1K", "mmui::ChatFileItemView")
    control.GetRuntimeId = lambda: (42, 10)
    message = UiaChatMessage("Synthetic", control.Name, "file", "incoming", "42.10", (10, 10, 100, 100))

    @contextmanager
    def fake_root():
        yield object()

    def focus():
        state.focus += 1

    def copy_paths(_control):
        state.copies += 1
        return []

    monkeypatch.setattr(client, "get_owner_window_handle", lambda: state.hwnd)
    monkeypatch.setattr(client, "get_owner_window_process_id", lambda: state.pid)
    monkeypatch.setattr(client, "get_title", lambda: HeaderInfo(state.title, "private"))
    monkeypatch.setattr(client, "focus_window", focus)
    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_find_message_control", lambda root, message: control)
    monkeypatch.setattr(client, "_copy_control_file_paths", copy_paths)
    monkeypatch.setattr(client, "_download_button", lambda _control: None)
    monkeypatch.setattr(client, "_find_cached_message_file", lambda _content: pytest.fail("strict不得按文件名搜索"))
    monkeypatch.setattr(client, "_wechat_data_roots", lambda: pytest.fail("strict不得枚举其他账号目录"))
    return client, message, control, state


def test_strict_file_never_uses_same_named_cache_from_another_account(monkeypatch, tmp_path):
    wrong = tmp_path / "other-account" / "synthetic.pdf"
    wrong.parent.mkdir()
    wrong.write_bytes(b"other account attachment")
    client, message, control, state = bound_file_client(monkeypatch)
    searched = []
    monkeypatch.setattr(client, "_find_cached_message_file", lambda content: searched.append(content) or str(wrong))

    assert client.fetch_message_file(message, tmp_path / "out", allow_filename_cache=False) == ""
    assert searched == [] and state.copies == 1


def test_strict_file_accepts_path_copied_from_exact_bound_bubble(monkeypatch, tmp_path):
    source = tmp_path / "selected-account" / "synthetic.pdf"
    source.parent.mkdir()
    source.write_bytes(b"selected bubble data")
    client, message, control, state = bound_file_client(monkeypatch)
    monkeypatch.setattr(client, "_copy_control_file_paths", lambda item: [str(source)] if item is control else [])

    result = client.fetch_message_file(message, tmp_path / "out", allow_filename_cache=False)

    assert Path(result).read_bytes() == b"selected bubble data"
    assert Path(result).parent == (tmp_path / "out").resolve()


def test_strict_download_rereads_bound_bubble_instead_of_searching_cache(monkeypatch, tmp_path):
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"downloaded bubble data")
    client, message, control, state = bound_file_client(monkeypatch, timeout=2)
    clock, downloads = [100.0], []
    monkeypatch.setattr("channel.wechat_desktop.uia.attachments.time.time", lambda: clock[0])
    monkeypatch.setattr("channel.wechat_desktop.uia.attachments.time.sleep", lambda value: clock.__setitem__(0, clock[0] + value))

    def copy_paths(item):
        assert item is control
        state.copies += 1
        return [] if state.copies == 1 else [str(source)]

    monkeypatch.setattr(client, "_copy_control_file_paths", copy_paths)
    button = object()
    monkeypatch.setattr(client, "_download_button", lambda _control: button)
    monkeypatch.setattr(client, "_click_and_restore", lambda item: downloads.append(item))

    result = client.fetch_message_file(message, tmp_path / "out", allow_filename_cache=False)

    assert Path(result).read_bytes() == b"downloaded bubble data"
    assert state.copies == 2 and downloads == [button]


def test_strict_file_can_use_native_save_as_of_the_same_bubble(monkeypatch, tmp_path):
    client, message, control, state = bound_file_client(monkeypatch, save_as=True)
    saved = tmp_path / "saved-from-bubble.pdf"
    calls = []

    def save_as(item, content, target_root, timeout):
        calls.append(item)
        saved.write_bytes(b"saved from selected bubble")
        return str(saved)

    monkeypatch.setattr(client, "_save_control_file_as", save_as)
    assert client.fetch_message_file(message, tmp_path / "out", allow_filename_cache=False) == str(saved)
    assert calls == [control]


@pytest.mark.parametrize("change", ["title", "window", "process"])
def test_strict_download_wait_rejects_changed_context(monkeypatch, tmp_path, change):
    client, message, control, state = bound_file_client(monkeypatch, timeout=2)
    clock = [100.0]
    monkeypatch.setattr("channel.wechat_desktop.uia.attachments.time.time", lambda: clock[0])

    def sleep(value):
        clock[0] += value
        if change == "title":
            state.title = "Other synthetic conversation"
        elif change == "window":
            state.hwnd += 1
        else:
            state.pid += 1

    monkeypatch.setattr("channel.wechat_desktop.uia.attachments.time.sleep", sleep)
    monkeypatch.setattr(client, "_download_button", lambda _control: object())
    monkeypatch.setattr(client, "_click_and_restore", lambda _button: None)

    with pytest.raises(RuntimeError, match="attachment_target_changed"):
        client.fetch_message_file(message, tmp_path / "out", allow_filename_cache=False)
    assert state.copies == 1


@pytest.mark.parametrize("runtime", ["", "42.999"])
def test_strict_file_rejects_missing_runtime_or_content_fallback(monkeypatch, tmp_path, runtime):
    client, message, control, state = bound_file_client(monkeypatch, save_as=True)
    message = replace(message, runtime_id=runtime)
    monkeypatch.setattr(client, "_save_control_file_as", lambda *_args: pytest.fail("不能另存为错误气泡"))
    assert client.fetch_message_file(message, tmp_path / "out", allow_filename_cache=False) == ""
    assert state.copies == 0


def test_strict_target_callback_rejects_before_focus(monkeypatch, tmp_path):
    client, message, control, state = bound_file_client(monkeypatch)
    with pytest.raises(RuntimeError, match="attachment_target_changed"):
        client.fetch_message_file(message, tmp_path / "out", allow_filename_cache=False,
                                  validate_target=lambda: False)
    assert state.focus == 0 and state.copies == 0


def test_diagnostic_cache_option_remains_explicitly_available(monkeypatch, tmp_path):
    cached = tmp_path / "diagnostic-file.pdf"
    cached.write_bytes(b"diagnostic cache data")
    client, message, control, state = bound_file_client(monkeypatch)
    monkeypatch.setattr(client, "_find_cached_message_file", lambda _content: str(cached))
    result = client.fetch_message_file(message, tmp_path / "out")
    assert Path(result).read_bytes() == b"diagnostic cache data"
    assert state.copies == 0 and state.focus == 0


def test_quoted_file_propagates_strict_policy_and_bound_target_callback(monkeypatch, tmp_path):
    client, file_message, control, state = bound_file_client(monkeypatch)
    quote_control = GeometryControl((10, 100, 300, 200),
        "synthetic question引用 Synthetic 的消息 : synthetic.pdf", "mmui::ChatTextItemView")
    quote_control.GetRuntimeId = lambda: (42, 11)
    message_list = GeometryControl((0, 0, 700, 700), children=[control, quote_control], automation_id="chat_message_list")
    root = GeometryControl((0, 0, 700, 700), children=[message_list])
    quoted = UiaChatMessage("Synthetic", "synthetic question", "text", "incoming", "42.11", (10, 100, 300, 200),
                           reference=UiaReferencedMessage("Synthetic", "synthetic.pdf", "file"))

    @contextmanager
    def fake_root():
        yield root

    calls, validations = [], []
    expected_callback = lambda: validations.append("validate")
    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_find_message_control", lambda root, message:
                        WechatUiaClient._find_message_control(client, root, message))
    monkeypatch.setattr(client, "_right_click_point", lambda _point: None)
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)
    monkeypatch.setattr(client, "_find_desktop_control", lambda *_args: object())
    monkeypatch.setattr(client, "_click_control_point", lambda _control: True)
    monkeypatch.setattr(client, "_pick_located_original_control", lambda *_args: control)
    monkeypatch.setattr(client, "_return_to_reference", lambda: True)
    path = tmp_path / "selected-reference.pdf"
    path.write_bytes(b"bound reference file")

    def fetch_file(original, *, allow_filename_cache=True, validate_target=None):
        calls.append((original.runtime_id, allow_filename_cache, validate_target))
        assert allow_filename_cache is False and callable(validate_target)
        validate_target()
        return str(path)

    monkeypatch.setattr(client, "fetch_message_file", fetch_file)
    result = client.resolve_message_reference(quoted, allow_filename_cache=False, validate_target=expected_callback)
    assert result.reference.file_path == str(path)
    assert len(calls) == 1 and len(validations) >= 3


def test_native_materializer_does_not_retry_unsupported_strict_api(tmp_path):
    client = FakeClient()
    calls = []

    def old_diagnostic_api(message):
        calls.append(message)
        return "unbound-file.pdf"

    client.fetch_message_file = old_diagnostic_api
    with pytest.raises(TypeError):
        materializer_for(client).materialize_event(
            native_event("file", "synthetic.pdf"),
            target_message=UiaChatMessage("Synthetic", "synthetic.pdf", "file", runtime_id="42.1"),
        )
    assert calls == []


def test_verified_materializer_cache_isolated_by_native_source_and_account(tmp_path):
    first, second = tmp_path / "first.pdf", tmp_path / "second.pdf"
    first.write_bytes(b"first native file")
    second.write_bytes(b"second native file")
    client = FakeClient()
    materializer = materializer_for(client)
    target = UiaChatMessage("Synthetic", "synthetic.pdf", "file", runtime_id="42.1")
    a = replace(native_event("file", "synthetic.pdf"), conversation_id="db-session:account-a")
    # 即使调用方错误复用会话标签，缓存仍须由原生账号隔离。
    b = replace(a, account_id="account-b")
    c = replace(a, source_message_id="other-native-source-id")
    client.file_paths["synthetic.pdf"] = str(first)
    assert materializer.materialize_event(a, target_message=target)[0].content == str(first)
    client.file_paths["synthetic.pdf"] = str(second)
    assert materializer.materialize_event(b, target_message=target)[0].content == str(second)
    assert materializer.materialize_event(c, target_message=target)[0].content == str(second)
    assert client.file_cache_policies == [False, False, False]


def bound_image(monkeypatch, *, reference=False):
    client, message, control, state = bound_file_client(monkeypatch)
    control.ClassName = "mmui::ChatTextItemView" if reference else "mmui::ChatImageItemView"
    control.Name = "synthetic question引用 Synthetic 的消息 : 图片" if reference else "图片"
    # 旧快照位置刻意与当前控件不同；strict必须使用当前精确控件的矩形。
    message = replace(message, content="synthetic question" if reference else "图片",
                      message_type="text" if reference else "image", bounds=(400, 400, 700, 700),
                      reference=UiaReferencedMessage("Synthetic", "图片", "image") if reference else None)
    return client, message, control, state


def fetch_bound_image(client, message, tmp_path, *, reference, **kwargs):
    fetch = client.fetch_referenced_message_image if reference else client.fetch_message_image
    return fetch(message, tmp_path, strict=True, **kwargs)


@pytest.mark.parametrize("reference", [False, True])
@pytest.mark.parametrize("invalid", ["missing_runtime", "missing_control", "different_runtime"])
def test_strict_image_rejects_missing_control_identity_and_old_coordinates(monkeypatch, tmp_path, reference, invalid):
    client, message, control, state = bound_image(monkeypatch, reference=reference)
    if invalid == "missing_runtime":
        message = replace(message, runtime_id="")
    elif invalid == "missing_control":
        monkeypatch.setattr(client, "_find_message_control", lambda *_args: None)
    else:
        message = replace(message, runtime_id="42.999")
    monkeypatch.setattr(client, "_capture_image_viewer_from_point", lambda *_args, **kwargs: pytest.fail("不能点击旧图片坐标"))
    from PIL import ImageGrab
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: pytest.fail("不能截取旧图片坐标"))
    assert fetch_bound_image(client, message, tmp_path, reference=reference) == ""


@pytest.mark.parametrize("reference", [False, True])
def test_strict_image_uses_bounds_of_current_exact_control(monkeypatch, tmp_path, reference):
    client, message, control, state = bound_image(monkeypatch, reference=reference)
    calls = []

    def capture(point, target, *, validate_before_click=None, validate_context=None):
        assert validate_before_click is not None and validate_context is not None
        validate_before_click()
        validate_context()
        calls.append(point)
        target.write_bytes(b"owned viewer image")
        return str(target)

    monkeypatch.setattr(client, "_capture_image_viewer_from_point", capture)
    result = fetch_bound_image(client, message, tmp_path, reference=reference)
    assert Path(result).read_bytes() == b"owned viewer image"
    assert calls == [(32, 78) if reference else (55, 55)]


@pytest.mark.parametrize("reference", [False, True])
@pytest.mark.parametrize("change", ["title", "window", "runtime"])
def test_strict_image_revalidates_context_and_control_before_activation(monkeypatch, tmp_path, reference, change):
    client, message, control, state = bound_image(monkeypatch, reference=reference)
    activations = []

    def capture(point, target, *, validate_before_click=None, validate_context=None):
        if change == "title":
            state.title = "Different synthetic conversation"
        elif change == "window":
            state.hwnd += 1
        else:
            control.GetRuntimeId = lambda: (42, 999)
        validate_before_click()
        activations.append(point)
        return "must-not-return.png"

    monkeypatch.setattr(client, "_capture_image_viewer_from_point", capture)
    assert fetch_bound_image(client, message, tmp_path, reference=reference) == ""
    assert activations == []


def test_strict_viewer_context_change_closes_only_its_viewer_without_restoring_old_focus(monkeypatch, tmp_path):
    import win32gui
    from PIL import Image, ImageGrab

    client, message, control, state = bound_image(monkeypatch, reference=True)
    calls = []
    monkeypatch.setattr(client, "_visible_top_level_window_handles", lambda: {123})
    monkeypatch.setattr(client, "_left_click_point", lambda point: calls.append(("activate", point)))
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)
    monkeypatch.setattr(client, "_find_opened_image_viewer", lambda *_args: 200)
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda _hwnd: (0, 0, 50, 50))

    def capture(**kwargs):
        state.title = "Different synthetic conversation"
        return Image.new("RGB", (50, 50), "red")

    monkeypatch.setattr(ImageGrab, "grab", capture)
    monkeypatch.setattr(client, "_close_image_viewer", lambda viewer, main: calls.append(("close", viewer, main)) or True)
    monkeypatch.setattr(client, "_restore_main_window_after_viewer", lambda *_args: pytest.fail("会话变化不得恢复旧前台"))
    assert fetch_bound_image(client, message, tmp_path, reference=True) == ""
    assert calls[-1] == ("close", 200, 123)
    assert not list(tmp_path.glob("*.png"))


@pytest.mark.parametrize("reference", [False, True])
def test_native_images_do_not_fallback_to_unsupported_diagnostic_api(tmp_path, reference):
    client = FakeClient()
    calls = []

    def old_diagnostic_api(message):
        calls.append(message)
        return "unbound-image.png"

    if reference:
        client.fetch_referenced_message_image = old_diagnostic_api
        event = native_event(reference={"content_type": "image"})
        target = UiaChatMessage("Synthetic", "question", reference=UiaReferencedMessage("Synthetic", "图片", "image"))
    else:
        client.fetch_message_image = old_diagnostic_api
        event = native_event("image", "图片")
        target = UiaChatMessage("Synthetic", "图片", "image")
    with pytest.raises(TypeError):
        materializer_for(client).materialize_event(event, target_message=target)
    assert calls == []


def bound_share(monkeypatch, *, reference=True):
    import win32gui
    import win32process

    client, message, control, state = bound_file_client(monkeypatch)
    state.browser_pid = 84
    control.ClassName = "mmui::ChatTextItemView" if reference else "mmui::ChatBubbleItemView"
    control.Name = "question引用 Synthetic 的消息 : synthetic article" if reference else "[链接]synthetic article"
    message = replace(message, content="question" if reference else "[链接]synthetic article",
                      message_type="text" if reference else "share_card", bounds=(400, 400, 700, 700),
                      reference=UiaReferencedMessage("Synthetic", "synthetic article", "share_card") if reference else None)
    calls = []
    monkeypatch.setattr(client, "_visible_top_level_window_handles", lambda: {123})
    monkeypatch.setattr(client, "_left_click_point", lambda point: calls.append(("activate", point)))
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)
    monkeypatch.setattr(client, "_find_opened_share_browser", lambda *_args: 200)
    monkeypatch.setattr(client, "_is_verified_share_browser_window", lambda browser, main: browser == 200 and main == 123)
    monkeypatch.setattr(win32process, "GetWindowThreadProcessId", lambda _hwnd: (1, state.browser_pid))
    monkeypatch.setattr(win32gui, "GetClassName", lambda _hwnd: "Chrome_WidgetWin_0")

    def read_body(browser, *, validate_context=None):
        assert browser == 200 and validate_context is not None
        validate_context()
        return "synthetic share body"

    def close_browser(browser, main, **kwargs):
        calls.append(("close", browser, main, kwargs))
        return True

    monkeypatch.setattr(client, "_read_share_browser_content", read_body)
    monkeypatch.setattr(client, "_close_share_browser", close_browser)
    return client, message, control, state, calls


def fetch_bound_share(client, message, *, reference, **kwargs):
    fetch = client.fetch_referenced_share_page if reference else client.fetch_share_message_page
    return fetch(message, strict=True, **kwargs)


@pytest.mark.parametrize("reference", [False, True])
@pytest.mark.parametrize("invalid", ["missing_runtime", "missing_control", "different_runtime"])
def test_strict_share_refuses_old_coordinates_without_exact_control(monkeypatch, reference, invalid):
    client, message, control, state, calls = bound_share(monkeypatch, reference=reference)
    if invalid == "missing_runtime":
        message = replace(message, runtime_id="")
    elif invalid == "missing_control":
        monkeypatch.setattr(client, "_find_message_control", lambda *_args: None)
    else:
        message = replace(message, runtime_id="42.999")
    assert fetch_bound_share(client, message, reference=reference) == ("", "")
    assert calls == []


@pytest.mark.parametrize("reference", [False, True])
def test_strict_share_retains_successful_browser_read_from_actual_control(monkeypatch, reference):
    client, message, control, state, calls = bound_share(monkeypatch, reference=reference)
    assert fetch_bound_share(client, message, reference=reference) == ("synthetic share body", "")
    expected = (client._reference_image_activation_point((10, 10, 100, 100)) if reference else
                client._share_card_activation_point((10, 10, 100, 100)))
    assert calls[0] == ("activate", expected)
    assert calls[-1] == ("close", 200, 123, {"restore_main": True,
                                           "expected_identity": (84, "Chrome_WidgetWin_0")})


@pytest.mark.parametrize("reference", [False, True])
@pytest.mark.parametrize("change", ["title", "window", "runtime"])
def test_strict_share_rechecks_context_and_runtime_before_click(monkeypatch, reference, change):
    client, message, control, state, calls = bound_share(monkeypatch, reference=reference)

    def visible_before():
        if change == "title":
            state.title = "Different synthetic conversation"
        elif change == "window":
            state.hwnd += 1
        else:
            control.GetRuntimeId = lambda: (42, 999)
        return {123}

    monkeypatch.setattr(client, "_visible_top_level_window_handles", visible_before)
    monkeypatch.setattr(client, "_recover_foreground_after_dependency_failure", lambda *_args: pytest.fail("点击前校验失败不得恢复旧前台"))
    assert fetch_bound_share(client, message, reference=reference) == ("", "")
    assert calls == []


def test_strict_share_context_change_during_body_read_cleans_own_browser_without_focus_restore(monkeypatch):
    client, message, control, state, calls = bound_share(monkeypatch)

    def changed_body(browser, *, validate_context=None):
        state.title = "Different synthetic conversation"
        return "must not return changed page"

    monkeypatch.setattr(client, "_read_share_browser_content", changed_body)
    assert fetch_bound_share(client, message, reference=True) == ("", "")
    assert calls[-1] == ("close", 200, 123, {"restore_main": False,
                                           "expected_identity": (84, "Chrome_WidgetWin_0")})


def test_strict_share_reused_browser_handle_is_not_closed(monkeypatch):
    import win32gui

    client, message, control, state, calls = bound_share(monkeypatch)

    def changed_browser(browser, *, validate_context=None):
        state.browser_pid += 1
        return "must not return another process page"

    monkeypatch.setattr(client, "_read_share_browser_content", changed_browser)
    # 调用真实清理方法，验证重新使用的 HWND 不会收到 WM_CLOSE。
    monkeypatch.setattr(client, "_close_share_browser", lambda browser, main, **kwargs:
                        client._share_browser._close_share_browser(browser, main, **kwargs))
    monkeypatch.setattr(win32gui, "PostMessage", lambda *_args: pytest.fail("不能关闭身份变化的浏览器"))
    monkeypatch.setattr(client, "_restore_main_window_after_viewer", lambda *_args: pytest.fail("不能恢复身份变化后的旧窗口"))
    assert fetch_bound_share(client, message, reference=True) == ("", "")
    assert len(calls) == 1 and calls[0][0] == "activate"


def test_native_share_original_resolution_uses_bound_message_api(monkeypatch):
    client, quoted, control, state, calls = bound_share(monkeypatch)
    quote_control = GeometryControl((10, 100, 300, 200),
        "question引用 Synthetic 的消息 : synthetic article", "mmui::ChatTextItemView")
    quote_control.GetRuntimeId = lambda: (42, 11)
    quoted = replace(quoted, runtime_id="42.11")
    control.ClassName, control.Name = "mmui::ChatBubbleItemView", "[链接]synthetic article"
    message_list = GeometryControl((0, 0, 700, 700), children=[control, quote_control], automation_id="chat_message_list")
    root = GeometryControl((0, 0, 700, 700), children=[message_list])

    @contextmanager
    def fake_root():
        yield root

    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_find_message_control", lambda root, message:
                        WechatUiaClient._find_message_control(client, root, message))
    monkeypatch.setattr(client, "_right_click_point", lambda *_args: None)
    monkeypatch.setattr(client, "_find_desktop_control", lambda *_args: object())
    monkeypatch.setattr(client, "_click_control_point", lambda *_args: True)
    monkeypatch.setattr(client, "_pick_located_original_control", lambda *_args: control)
    monkeypatch.setattr(client, "_return_to_reference", lambda: True)
    monkeypatch.setattr(client, "fetch_share_page_from_point", lambda *_args: pytest.fail("原生引用不能只传坐标"))
    result = client.resolve_message_reference(quoted, allow_filename_cache=False, validate_target=lambda: None)
    assert result.reference.browser_content == "synthetic share body"
    assert result.reference.strategy == "wechat_share_browser_direct_read"


def test_native_share_strict_api_does_not_retry_diagnostic_signature():
    client = FakeClient()
    calls = []

    def old_api(message):
        calls.append(message)
        return message

    client.resolve_message_reference = old_api
    event = native_event(reference={"content_type": "share_card"})
    target = UiaChatMessage("Synthetic", "question", reference=UiaReferencedMessage("Synthetic", "article", "share_card"))
    with pytest.raises(TypeError):
        materializer_for(client).materialize_event(event, target_message=target)
    assert calls == []
