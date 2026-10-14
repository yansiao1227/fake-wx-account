"""图片查看器延迟就绪、窗口复用和来源失效的有界等待。"""

from types import SimpleNamespace

import pytest

from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia import image_viewer


def _viewer_fixture(monkeypatch, *, ready_after=1, config=None):
    import win32gui
    import win32process

    client = WechatUiaClient(config or {})
    state = SimpleNamespace(
        windows={
            100: (42, "微信", "Qt51514QWindowIcon"),
            200: (42, "图片和视频", "Qt51514QWindowIcon"),
        },
        uia_aids={100: "wechat_main", 200: "PreviewWindow_bc200_0_0"},
        visible={100, 200},
        foreground=200,
        now=0.0,
        enum_calls=0,
        uia_calls=[],
        uia_identity_calls=[],
        find_calls=[],
        find_results=[],
        sleeps=[],
        valid=True,
    )

    def enum_windows(callback, param):
        state.enum_calls += 1
        for hwnd in sorted(state.windows):
            callback(hwnd, param)

    def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds

    def ready(hwnd):
        state.uia_calls.append(hwnd)
        return len(state.uia_calls) >= ready_after

    def uia_identity(hwnd):
        state.uia_identity_calls.append(hwnd)
        return "mmui::XView", state.uia_aids[hwnd]

    monkeypatch.setattr(win32gui, "EnumWindows", enum_windows)
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: hwnd in state.windows)
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda hwnd: hwnd in state.visible)
    monkeypatch.setattr(win32gui, "IsIconic", lambda hwnd: False)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: state.foreground)
    monkeypatch.setattr(win32gui, "GetWindowText", lambda hwnd: state.windows[hwnd][1])
    monkeypatch.setattr(win32gui, "GetClassName", lambda hwnd: state.windows[hwnd][2])
    monkeypatch.setattr(win32process, "GetWindowThreadProcessId", lambda hwnd: (1, state.windows[hwnd][0]))
    monkeypatch.setattr(client, "_has_image_viewer_close_control", ready)
    monkeypatch.setattr(client._image_viewer, "_image_viewer_uia_identity", uia_identity)
    monkeypatch.setattr(image_viewer.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(image_viewer.time, "sleep", sleep)
    find_opened = client._find_opened_image_viewer

    def find(main_hwnd, visible_before):
        state.find_calls.append((main_hwnd, set(visible_before), state.now))
        result = find_opened(main_hwnd, visible_before)
        state.find_results.append(result)
        return result

    monkeypatch.setattr(client, "_find_opened_image_viewer", find)

    def require_source():
        if not state.valid:
            raise RuntimeError("source_changed")

    return client._image_viewer, client, state, require_source


def test_wait_rechecks_native_candidate_until_its_close_button_is_ready(monkeypatch):
    viewer, _, state, require_source = _viewer_fixture(monkeypatch, ready_after=2)
    observation = {}

    assert viewer._wait_opened_image_viewer(
        100, {100}, validate_context=require_source, observation=observation,
        strict_viewer_identity=True,
    ) == 200

    assert state.find_results == [0, 200]
    assert state.uia_calls == [200, 200]
    assert all(call[:2] == (100, {100}) for call in state.find_calls)
    assert observation["attempts"] == 2
    assert observation["native_identity"] == (200, 42, "图片和视频", "Qt51514QWindowIcon")
    assert observation["uia_identity"] == ("mmui::XView", "PreviewWindow_bc200_0_0")
    assert observation["verified_identity"] == observation["native_identity"]


def test_wait_retains_native_observation_without_marking_unready_window_verified(monkeypatch):
    viewer, _, state, require_source = _viewer_fixture(monkeypatch, ready_after=99)
    observation = {}

    assert viewer._wait_opened_image_viewer(
        100, {100}, validate_context=require_source, observation=observation,
    ) == 0

    assert len(state.find_calls) == 4
    assert state.find_results == [0, 0, 0, 0]
    assert observation["attempts"] == 4
    assert observation["native_identity"] == (200, 42, "图片和视频", "Qt51514QWindowIcon")
    assert observation["uia_identity"] == ("mmui::XView", "PreviewWindow_bc200_0_0")
    assert not observation.get("verified_identity")
    assert state.now <= 3.0


def test_wait_accepts_reused_hwnd_when_it_was_hidden_before_click(monkeypatch):
    viewer, client, state, _ = _viewer_fixture(monkeypatch)
    # 微信关闭查看器后保留 HWND，点击前的可见窗口快照不应包含它。
    state.visible = {100}
    visible_before = client._visible_top_level_window_handles()
    assert 200 in state.windows and 200 not in visible_before
    state.visible.add(200)

    assert viewer._wait_opened_image_viewer(100, visible_before) == 200
    assert state.find_results == [200]


def test_wait_rejects_hwnd_that_was_already_visible_before_click(monkeypatch):
    viewer, client, state, _ = _viewer_fixture(monkeypatch)
    visible_before = client._visible_top_level_window_handles()
    observation = {}

    assert viewer._wait_opened_image_viewer(
        100, visible_before, observation=observation,
    ) == 0

    assert state.uia_calls == []
    assert state.uia_identity_calls == []
    assert len(state.find_calls) <= 4
    assert not observation.get("native_identity")
    assert not observation.get("verified_identity")


def test_wait_aborts_invalid_source_before_any_native_or_uia_read(monkeypatch):
    viewer, _, state, require_source = _viewer_fixture(monkeypatch)
    state.valid = False

    with pytest.raises(RuntimeError, match="source_changed"):
        viewer._wait_opened_image_viewer(100, {100}, validate_context=require_source)

    assert state.enum_calls == 0
    assert state.find_calls == []
    assert state.uia_calls == []
    assert state.uia_identity_calls == []
    assert state.sleeps == []


def test_wait_treats_explicit_false_source_validation_as_invalid(monkeypatch):
    viewer, _, state, _ = _viewer_fixture(monkeypatch)

    with pytest.raises(RuntimeError, match="attachment_target_changed"):
        viewer._wait_opened_image_viewer(100, {100}, validate_context=lambda: False)

    assert state.enum_calls == 0
    assert state.find_calls == []
    assert state.uia_calls == []
    assert state.uia_identity_calls == []


def test_wait_aborts_source_invalidated_by_first_probe_without_another_uia_call(monkeypatch):
    viewer, client, state, require_source = _viewer_fixture(monkeypatch)

    def invalidate(hwnd):
        state.uia_calls.append(hwnd)
        state.valid = False
        return False

    monkeypatch.setattr(client, "_has_image_viewer_close_control", invalidate)
    with pytest.raises(RuntimeError, match="source_changed"):
        viewer._wait_opened_image_viewer(100, {100}, validate_context=require_source)

    assert len(state.find_calls) == 1
    assert state.uia_calls == [200]
    assert state.sleeps == []


def test_wait_rejects_native_identity_change_between_readiness_probes(monkeypatch):
    viewer, _, state, require_source = _viewer_fixture(monkeypatch, ready_after=2)
    observation = {}

    def reuse_hwnd(seconds):
        state.now += seconds
        state.windows[200] = (42, "图片和视频", "Qt61514QWindowIcon")

    monkeypatch.setattr(image_viewer.time, "sleep", reuse_hwnd)
    with pytest.raises(RuntimeError, match="image_viewer_identity_changed"):
        viewer._wait_opened_image_viewer(
            100, {100}, validate_context=require_source, observation=observation,
        )

    assert state.find_results == [0]
    assert state.uia_calls == [200]
    assert not observation.get("verified_identity")


@pytest.mark.parametrize("strict", [False, True])
def test_wait_rejects_uia_identity_change_with_unchanged_native_identity(monkeypatch, strict):
    viewer, _, state, require_source = _viewer_fixture(monkeypatch, ready_after=2)
    observation = {}
    original_native = state.windows[200]

    def reuse_uia_identity(seconds):
        state.now += seconds
        state.uia_aids[200] = "PreviewWindow_reused_0_0"

    monkeypatch.setattr(image_viewer.time, "sleep", reuse_uia_identity)
    with pytest.raises(RuntimeError, match="image_viewer_identity_changed"):
        viewer._wait_opened_image_viewer(
            100, {100}, validate_context=require_source, observation=observation,
            strict_viewer_identity=strict,
        )

    assert state.windows[200] == original_native
    assert state.find_results == [0]
    assert state.uia_calls == [200]
    assert observation["uia_identity"] == ("mmui::XView", "PreviewWindow_bc200_0_0")
    assert not observation.get("verified_identity")


@pytest.mark.parametrize(
    "strict, native_class, uia_aid, expected",
    [
        (False, "Qt51514QWindowIcon", "PreviewWindow_bc200_0_0", 200),
        (True, "Qt51514QWindowIcon", "PreviewWindow_bc200_0_0", 200),
        (False, "OtherNativeWindow", "PreviewWindow_bc200_0_0", 200),
        (True, "OtherNativeWindow", "PreviewWindow_bc200_0_0", 0),
        (False, "Qt51514QWindowIcon", "OtherWindow_200", 200),
        (True, "Qt51514QWindowIcon", "OtherWindow_200", 0),
        (True, "PreviewWindow_200", "PreviewWindow_bc200_0_0", 0),
        (True, "Qt51514QWindowIcon_suffix", "PreviewWindow_bc200_0_0", 0),
        (True, "Qt51514QWindowIcon", "prefix_PreviewWindow_bc200_0_0", 0),
    ],
)
def test_record_wait_requires_qt_native_class_and_preview_uia_aid(
    monkeypatch, strict, native_class, uia_aid, expected,
):
    viewer, _, state, _ = _viewer_fixture(monkeypatch)
    state.windows[200] = (42, "图片和视频", native_class)
    state.uia_aids[200] = uia_aid
    observation = {}

    assert viewer._wait_opened_image_viewer(
        100, {100}, observation=observation, strict_viewer_identity=strict,
    ) == expected
    assert bool(observation.get("verified_identity")) is bool(expected)
    if expected:
        assert observation["native_identity"] == (200, 42, "图片和视频", native_class)
        assert observation["uia_identity"] == ("mmui::XView", uia_aid)


def test_wait_does_not_accept_ready_viewer_without_exact_foreground_hwnd(monkeypatch):
    viewer, _, state, _ = _viewer_fixture(monkeypatch)
    state.foreground = 100
    observation = {}

    assert viewer._wait_opened_image_viewer(100, {100}, observation=observation) == 0
    assert not observation.get("verified_identity")


def test_wait_clamps_excessive_timeout_to_three_seconds(monkeypatch):
    viewer, client, state, _ = _viewer_fixture(
        monkeypatch, ready_after=99,
        config={"uia_image_viewer_open_timeout_seconds": 999},
    )
    find = client._find_opened_image_viewer

    def slow_probe(main_hwnd, visible_before):
        result = find(main_hwnd, visible_before)
        state.now += 1.2
        return result

    monkeypatch.setattr(client, "_find_opened_image_viewer", slow_probe)

    assert viewer._wait_opened_image_viewer(100, {100}) == 0
    assert 1 <= len(state.find_calls) <= 4
    assert all(start < 3.0 for _, _, start in state.find_calls)
    assert state.now <= 3.0


@pytest.mark.parametrize(
    "hwnd, identity",
    [
        (100, (42, "微信", "Qt51514QWindowIcon")),
        (200, (99, "图片和视频", "Qt51514QWindowIcon")),
        (200, (42, "聊天记录", "Qt51514QWindowIcon")),
    ],
)
def test_native_identity_rejects_main_foreign_pid_and_wrong_title(monkeypatch, hwnd, identity):
    viewer, _, state, _ = _viewer_fixture(monkeypatch)
    state.windows[hwnd] = identity

    assert not viewer._native_image_viewer_identity(hwnd, 100)


def test_native_identity_contains_handle_pid_title_and_class(monkeypatch):
    viewer, _, _, _ = _viewer_fixture(monkeypatch)

    assert viewer._native_image_viewer_identity(200, 100) == (
        200, 42, "图片和视频", "Qt51514QWindowIcon",
    )


@pytest.mark.parametrize("window_state", ["hidden", "minimized"])
def test_native_identity_rejects_hidden_or_minimized_viewer(monkeypatch, window_state):
    import win32gui

    viewer, _, state, _ = _viewer_fixture(monkeypatch)
    if window_state == "hidden":
        state.visible.remove(200)
    else:
        monkeypatch.setattr(win32gui, "IsIconic", lambda hwnd: hwnd == 200)

    assert not viewer._native_image_viewer_identity(200, 100)
