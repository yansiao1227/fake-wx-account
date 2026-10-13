"""另存为操作限定本次窗口，使用合成 UIA/Win32 状态，不操作真实桌面。"""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
from channel.wechat_desktop.uia.client import WechatUiaClient
from .helpers import GeometryControl


@pytest.fixture
def save_dialog(monkeypatch, tmp_path):
    import win32gui
    import win32process
    state = SimpleNamespace(foreground=100, windows={100, 900}, pids={100: 42, 200: 42, 900: 99},
        classes={100: "WechatMain", 200: "#32770", 900: "#32770"}, runtime=(42, 200),
        source=True, checks=0, actions=[], path="", on_menu=None, on_set=None, on_save=None,
        preexisting=False, hide_dialog=False)
    client = WechatUiaClient({})
    bubble = GeometryControl((10, 10, 100, 100), "文件\nsynthetic.pdf\n1K")
    bubble.GetRuntimeId = lambda: (42, 10)
    menu = SimpleNamespace(ProcessId=42)
    edit = GeometryControl((1, 1, 100, 30), "", "Edit")
    edit.AutomationId, edit.ControlTypeName, edit.ProcessId = "1001", "EditControl", 42
    save = GeometryControl((1, 40, 50, 70), "保存", "Button")
    save.AutomationId, save.ControlTypeName, save.ProcessId = "1", "ButtonControl", 42
    cancel = GeometryControl((60, 40, 100, 70), "取消", "Button")
    cancel.AutomationId, cancel.ControlTypeName, cancel.ProcessId = "2", "ButtonControl", 42
    dialog = GeometryControl((0, 0, 200, 200), "另存为", "#32770", [edit, save, cancel])
    dialog.ProcessId = 42
    dialog.GetRuntimeId = lambda: state.runtime
    cancel.GetInvokePattern = lambda: SimpleNamespace(Invoke=lambda: state.actions.append("cancel"))

    def set_value(value):
        state.actions.append("set")
        state.path = value
        if state.on_set:
            state.on_set()
    edit.GetValuePattern = lambda: SimpleNamespace(SetValue=set_value)

    def click(item):
        if item is menu:
            state.actions.append("menu")
            state.windows.add(200)
            state.foreground = 200
            if state.on_menu:
                state.on_menu()
        elif item is save:
            state.actions.append("save")
            if state.on_save:
                state.on_save()
            else:
                Path(state.path).write_bytes(b"synthetic saved file")
                state.windows.remove(200)
                state.foreground = 100
        else:
            pytest.fail("unexpected click")

    def validate():
        state.checks += 1
        return state.source

    @contextmanager
    def uia_thread():
        yield

    monkeypatch.setitem(sys.modules, "uiautomation", SimpleNamespace(
        ControlFromHandle=lambda hwnd: dialog, UIAutomationInitializerInThread=uia_thread))
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: hwnd in state.windows)
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda hwnd: hwnd in state.windows and not (hwnd == 200 and state.hide_dialog))
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: state.foreground)
    monkeypatch.setattr(win32gui, "GetClassName", lambda hwnd: state.classes[hwnd])
    monkeypatch.setattr(win32gui, "EnumWindows", lambda callback, arg: [callback(hwnd, arg) for hwnd in sorted(state.windows)])
    monkeypatch.setattr(win32process, "GetWindowThreadProcessId", lambda hwnd: (1, state.pids[hwnd]))
    monkeypatch.setattr(client, "get_owner_window_handle", lambda: 100)
    monkeypatch.setattr(client, "get_owner_window_process_id", lambda: 42)
    monkeypatch.setattr(client, "_right_click_point", lambda point: state.actions.append("right"))
    monkeypatch.setattr(client, "_paced_wait", lambda *args: None)
    monkeypatch.setattr(client, "_find_desktop_control", lambda *args: menu)
    monkeypatch.setattr(client, "_click_and_restore", click)
    monkeypatch.setattr(client, "_recover_foreground_after_dependency_failure", lambda *args: pytest.fail("不允许抢回用户焦点"))

    def run(**kwargs):
        return client._save_control_file_as(bubble, bubble.Name, tmp_path, 1,
            validate_target=validate, **kwargs)
    return client, state, run


def test_save_dialog_uses_new_same_process_dialog_and_hook_once(save_dialog):
    client, state, run = save_dialog
    hooks = []
    result = run(on_file_materialized=lambda path: hooks.append(path) or True)
    assert Path(result).read_bytes() == b"synthetic saved file"
    assert state.actions == ["right", "menu", "set", "save"]
    assert hooks == [result] and state.checks >= 8


@pytest.mark.parametrize("hidden", [False, True])
def test_save_dialog_rejects_preexisting_dialog(save_dialog, hidden):
    client, state, run = save_dialog
    state.windows.add(200)
    state.hide_dialog = hidden
    state.on_menu = lambda: setattr(state, "hide_dialog", False)
    assert run() == ""
    assert state.actions == ["right", "menu"]


def test_save_dialog_rejects_external_application_foreground(save_dialog):
    client, state, run = save_dialog
    state.on_menu = lambda: setattr(state, "foreground", 900)
    assert run() == ""
    assert state.actions == ["right", "menu"]


@pytest.mark.parametrize("phase", ["before", "menu", "set"])
def test_save_dialog_stops_when_source_invalidates(save_dialog, phase):
    client, state, run = save_dialog
    if phase == "before":
        state.source = False
    else:
        setattr(state, "on_" + phase, lambda: setattr(state, "source", False))
    with pytest.raises(RuntimeError, match="attachment_target_changed"):
        run()
    assert "save" not in state.actions and "cancel" not in state.actions


@pytest.mark.parametrize("change", ["pid", "class", "runtime", "foreground"])
def test_save_dialog_does_not_save_or_cancel_changed_window(save_dialog, change):
    client, state, run = save_dialog
    def alter():
        if change == "pid": state.pids[200] = 99
        elif change == "class": state.classes[200] = "WpsDialog"
        elif change == "runtime": state.runtime = (42, 201)
        else: state.foreground = 900
    state.on_set = alter
    with pytest.raises(RuntimeError, match="attachment_save_dialog_changed"):
        run()
    assert state.actions == ["right", "menu", "set"]


def test_save_dialog_rebinds_expected_path_change_before_full_source_validation(save_dialog):
    client, state, run = save_dialog
    def save():
        Path(state.path).write_bytes(b"saved file with expected packed path change")
        state.source = False
        state.windows.remove(200)
        state.foreground = 100
    state.on_save = save
    hooks = []
    def rebind(path):
        assert Path(path).is_file() and not state.source
        hooks.append(path)
        state.source = True
        return True
    result = run(on_file_materialized=rebind)
    assert hooks == [result]


def test_save_dialog_rejects_source_change_after_materialization(save_dialog):
    client, state, run = save_dialog
    def reject(path):
        state.source = False
        return True
    with pytest.raises(RuntimeError, match="attachment_target_changed"):
        run(on_file_materialized=reject)
    assert state.actions == ["right", "menu", "set", "save"]


def test_save_dialog_does_not_rebind_output_while_external_window_is_foreground(save_dialog):
    client, state, run = save_dialog
    def save():
        Path(state.path).write_bytes(b"saved file")
        state.windows.remove(200)
        state.foreground = 900
    state.on_save = save
    with pytest.raises(RuntimeError, match="attachment_save_dialog_changed"):
        run(on_file_materialized=lambda path: pytest.fail("外部窗口在前台不得重签"))
    assert "cancel" not in state.actions
