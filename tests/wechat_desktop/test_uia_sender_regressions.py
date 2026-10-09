"""真实 UIA 发送方法配合合成控件验证；不访问微信或系统输入。"""

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.models import UiaChatMessage
from channel.wechat_desktop.send_control import SendNotSubmitted, track_send_attempt
from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia.group_sender_ocr import OcrTextLine
from .helpers import GeometryControl


@pytest.fixture
def fast_sender_clock(monkeypatch):
    tick = [0.0]

    def now():
        tick[0] += 0.25
        return tick[0]

    monkeypatch.setattr("channel.wechat_desktop.uia.message_sender.time",
                        SimpleNamespace(time=now, sleep=lambda _: None))


def input_client(monkeypatch, paste_values, *, send_button=True):
    import win32api
    import win32con

    state = {"value": "旧草稿", "selected": False, "pastes": 0, "clears": 0,
             "submitted": [], "remaining": iter(paste_values), "pattern_reads": 0,
             "post_submit_value_reads": 0}
    message_list = GeometryControl((0, 100, 900, 800), automation_id="chat_message_list")
    lines = []

    class Input(GeometryControl):
        HasKeyboardFocus = True

        def SetFocus(self):
            pass

        def GetValuePattern(self):
            state["pattern_reads"] += 1
            if state["submitted"]:
                state["post_submit_value_reads"] += 1
            unavailable = state.get("unavailable_pattern_reads", set())
            if (unavailable == "all" or state["pattern_reads"] in unavailable
                    or (state["submitted"] and state.get("unavailable_after_submit"))):
                return None
            return SimpleNamespace(Value=state["value"])

    def submit():
        state["submitted"].append(state["value"])
        index = len(state["submitted"])
        bounds = (680, index * 60 + 110, 860, index * 60 + 150)
        body = GeometryControl(bounds, state["value"], "mmui::ChatTextBubble")
        message = GeometryControl(
            (0, bounds[1], 900, bounds[3]), state["value"],
            "mmui::ChatTextItemView", children=[body],
        )
        message.GetRuntimeId = lambda: (42, index)
        if state.get("expose_bubble_on_submit", True):
            message_list._children.append(message)
            lines.append(OcrTextLine(state["value"], 0.99, bounds))
        if state.get("clear_input_on_submit", True):
            state["value"] = ""

    class Button(GeometryControl):
        def Click(self, **kwargs):
            submit()

    control = Input((0, 0, 100, 50), automation_id="chat_input_field")
    state["input_control"] = control
    controls = [control, message_list,
                GeometryControl((0, 0, 200, 20), "Synthetic",
                                automation_id="current_chat_name_label")]
    if send_button:
        controls.append(Button((100, 0, 150, 50), name="发送",
                               class_name="mmui::XOutlineButton"))

    def key_event(key, _scan, flags, _extra):
        if flags:
            return
        if key == ord("A"):
            state["selected"] = True
        elif key == ord("V"):
            state["pastes"] += 1
            if not state.get("paste_succeeds", True):
                return
            value = next(state["remaining"], "错误草稿")
            state["value"] = value if state["selected"] else state["value"] + value
            state["selected"] = False
        elif key == win32con.VK_BACK:
            state["clears"] += 1
            state["value"] = "" if state["selected"] else state["value"][:-1]
            state["selected"] = False
        elif key == win32con.VK_RETURN:
            submit()

    monkeypatch.setattr(win32api, "keybd_event", key_event)
    monkeypatch.setattr(win32api, "GetCursorPos", lambda: (0, 0))
    monkeypatch.setattr(win32api, "SetCursorPos", lambda _: None)
    client = WechatUiaClient({"uia_paste_attempts": 3})
    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "_uia_root", lambda: nullcontext(controls))
    monkeypatch.setattr(client, "_walk", lambda root: iter(root))
    monkeypatch.setattr(client, "_paced_wait", lambda *_: None)
    monkeypatch.setattr(client, "_wait_for_input_value",
                        lambda item, predicate, _: predicate(client._input_value(item)))
    monkeypatch.setattr(client._group_sender_ocr, "enrich",
                        lambda *args, **kwargs: pytest.fail("发送不得触发 OCR"))
    monkeypatch.setattr(client._group_sender_ocr, "_get_engine",
                        lambda: pytest.fail("发送不得初始化 OCR 模型"))
    return client, state


def test_real_sender_reads_before_and_after_through_non_ocr_snapshot(monkeypatch, fast_sender_clock):
    client, state = input_client(monkeypatch, ["合成回复"])
    monkeypatch.setattr(client, "locate_conversation", lambda *args: True)
    monkeypatch.setattr(client, "_clipboard", lambda **kwargs: nullcontext())
    monkeypatch.setattr(client, "get_chat_history",
                        lambda **kwargs: pytest.fail("发送不得调用接收历史/OCR入口"))
    snapshots = []
    read_snapshot = client.get_send_bubble_snapshot

    def read(**kwargs):
        result = read_snapshot(**kwargs)
        snapshots.append((kwargs, len(result)))
        return result

    monkeypatch.setattr(client, "get_send_bubble_snapshot", read)
    result = client.send_message("Synthetic", "合成回复", expedited=True)

    assert result["verified"] is True
    assert state["submitted"] == ["合成回复"]
    assert snapshots == [({"conversation": "Synthetic", "limit": 5}, 0),
                         ({"conversation": "Synthetic", "limit": 5}, 1)]


@pytest.mark.parametrize("send_button", [True, False])
def test_delayed_submission_never_replays_matching_uncleared_draft(monkeypatch, send_button):
    """五秒后才出现气泡的已提交草稿，不能因三秒验证超时再按 Enter。"""
    import win32api

    client, state = input_client(monkeypatch, ["延迟发送文本"], send_button=send_button)
    state["clear_input_on_submit"] = False
    state["expose_bubble_on_submit"] = False
    clock = [0.0]
    queued = []

    def advance(duration):
        clock[0] += duration

    monkeypatch.setattr(
        "channel.wechat_desktop.uia.message_sender.time",
        SimpleNamespace(time=lambda: clock[0], sleep=advance),
    )
    # 保留真实输入等待和发送验证，用虚拟时间覆盖 1.5 秒输入等待及 3 秒气泡等待。
    monkeypatch.setattr(client, "_wait_for_input_value", client._send_controller._wait_for_input_value)
    monkeypatch.setattr(client, "locate_conversation", lambda *args: True)
    monkeypatch.setattr(client, "_clipboard", lambda **kwargs: nullcontext())

    def queue_new_submissions(operation, *args, **kwargs):
        before = len(state["submitted"])
        operation(*args, **kwargs)
        for index in range(before, len(state["submitted"])):
            queued.append((clock[0] + 5.0, state["submitted"][index], str(index + 1)))

    keyboard_event = win32api.keybd_event
    monkeypatch.setattr(
        win32api, "keybd_event",
        lambda *args: queue_new_submissions(keyboard_event, *args),
    )
    if send_button:
        with client._uia_root() as root:
            button = next(item for item in root if item.Name == "发送")
        click = button.Click
        monkeypatch.setattr(button, "Click", lambda **kwargs: queue_new_submissions(click, **kwargs))

    def delayed_history(**kwargs):
        messages = [UiaChatMessage("Synthetic", text, runtime_id=runtime_id, direction="outgoing")
                    for ready_at, text, runtime_id in queued if clock[0] >= ready_at]
        if messages:
            state["value"] = ""
        return messages

    monkeypatch.setattr(client, "get_send_bubble_snapshot", delayed_history)

    result = client.send_message("Synthetic", "延迟发送文本", expedited=True)
    # 所有已排队的 UI 提交都完成后，仍必须只有一个实际出站气泡。
    clock[0] = max(ready_at for ready_at, _, _ in queued)
    assert len(delayed_history()) == 1
    assert state["submitted"] == ["延迟发送文本"]
    assert result["status"] == "uncertain"
    assert result["submitted_chunks"] == 1
    assert result["verified_chunks"] == 0
    assert result["retryable"] is False


@pytest.mark.parametrize("send_button", [True, False])
def test_visible_submission_with_stale_input_is_verified_without_replay(monkeypatch, fast_sender_clock, send_button):
    client, state = input_client(monkeypatch, ["正确文本"], send_button=send_button)
    state["clear_input_on_submit"] = False
    monkeypatch.setattr(client, "locate_conversation", lambda *args: True)
    monkeypatch.setattr(client, "_clipboard", lambda **kwargs: nullcontext())

    result = client.send_message("Synthetic", "正确文本", expedited=True)

    assert state["submitted"] == ["正确文本"]
    assert result["success"] is True
    assert result["verified"] is True
    assert result["submitted_chunks"] == 1
    assert result["verified_chunks"] == 1


@pytest.mark.parametrize("send_button", [True, False])
def test_submit_call_error_preserves_uncertain_result_without_replay(monkeypatch, send_button):
    import win32api
    import win32con

    client, state = input_client(monkeypatch, ["正确文本"], send_button=send_button)
    monkeypatch.setattr(client, "locate_conversation", lambda *args: True)
    monkeypatch.setattr(client, "_clipboard", lambda **kwargs: nullcontext())
    if send_button:
        with client._uia_root() as root:
            button = next(item for item in root if item.Name == "发送")
        click = button.Click

        def click_then_raise(**kwargs):
            click(**kwargs)
            raise RuntimeError("UIA click result is unavailable")

        monkeypatch.setattr(button, "Click", click_then_raise)
    else:
        keyboard_event = win32api.keybd_event

        def enter_then_raise(key, scan, flags, extra):
            keyboard_event(key, scan, flags, extra)
            if key == win32con.VK_RETURN and not flags:
                raise RuntimeError("keyboard submission result is unavailable")

        monkeypatch.setattr(win32api, "keybd_event", enter_then_raise)

    result = client.send_message("Synthetic", "正确文本", expedited=True)

    assert state["submitted"] == ["正确文本"]
    assert result["status"] == "uncertain"
    assert result["submitted_chunks"] == 1
    assert result["verified_chunks"] == 0
    assert result["retryable"] is False


@pytest.mark.parametrize("send_button", [True, False])
@pytest.mark.parametrize("incorrect", ["旧草稿", "完整文本的截断片段", "错误内容"])
def test_readable_mismatched_draft_never_submits(monkeypatch, incorrect, send_button):
    client, state = input_client(monkeypatch, [incorrect] * 3, send_button=send_button)

    with track_send_attempt() as attempt:
        with pytest.raises(SendNotSubmitted):
            client._paste_and_send("完整文本")

    assert attempt.submitted is False
    assert state["submitted"] == []
    assert state["pastes"] == 3
    assert state["clears"] == 3
    assert state["value"] == ""


@pytest.mark.parametrize("send_button", [True, False])
def test_mismatched_paste_is_cleared_before_successful_retry(monkeypatch, send_button):
    client, state = input_client(monkeypatch, ["错误草稿", "正确文本"], send_button=send_button)

    with track_send_attempt() as attempt:
        client._paste_and_send("正确文本")

    assert attempt.submitted is True
    assert state["submitted"] == ["正确文本"]
    assert state["pastes"] == 2
    assert state["clears"] == 1


def test_normalized_text_remains_sendable(monkeypatch):
    client, state = input_client(monkeypatch, ["第一行\r\n第二行\u00a0文本"])

    client._paste_and_send("第一行\n第二行 文本")

    assert state["submitted"] == ["第一行\r\n第二行\u00a0文本"]


@pytest.mark.parametrize("send_button", [True, False])
def test_pattern_recovery_with_wrong_draft_never_submits(monkeypatch, send_button):
    client, state = input_client(monkeypatch, ["错误草稿"] * 3, send_button=send_button)
    state["unavailable_pattern_reads"] = {1}

    with track_send_attempt() as attempt:
        with pytest.raises(SendNotSubmitted):
            client._paste_and_send("正确文本")

    assert attempt.submitted is False
    assert state["submitted"] == []


@pytest.mark.parametrize("send_button", [True, False])
def test_wrong_pattern_recovery_cannot_downgrade_later_unreadable_retry(monkeypatch, send_button):
    client, state = input_client(monkeypatch, ["错误草稿"] * 3, send_button=send_button)
    # 仅读到过一次错误草稿，后续重试均不可读，不能跳过正文核验。
    state["unavailable_pattern_reads"] = set(range(1, 100)) - {2}

    with track_send_attempt() as attempt:
        with pytest.raises(SendNotSubmitted):
            client._paste_and_send("正确文本")

    assert attempt.submitted is False
    assert state["submitted"] == []


@pytest.mark.parametrize("send_button", [True, False])
def test_pattern_recovery_with_correct_draft_checks_post_send_clear(monkeypatch, fast_sender_clock, send_button):
    client, state = input_client(monkeypatch, ["正确文本"], send_button=send_button)
    # 初始检查及第一次等待均不可读；必须继续等待恢复，然后再次检查提交前正文。
    state["unavailable_pattern_reads"] = {1, 2}
    monkeypatch.setattr(client, "_wait_for_input_value", client._send_controller._wait_for_input_value)

    with track_send_attempt() as attempt:
        client._paste_and_send("正确文本")

    assert attempt.submitted is True
    assert state["submitted"] == ["正确文本"]
    assert state["pastes"] == 1
    assert state["post_submit_value_reads"] > 0


@pytest.mark.parametrize("send_button", [True, False])
def test_unreadable_failed_paste_never_submits_stale_draft(monkeypatch, send_button):
    """Ctrl+V 无效且 ValuePattern 不可用时，不能把旧草稿当成回复提交。"""
    client, state = input_client(monkeypatch, [], send_button=send_button)
    state["value"] = "旧敏感草稿"
    state["paste_succeeds"] = False
    state["unavailable_pattern_reads"] = "all"
    error = None

    with track_send_attempt() as attempt:
        try:
            client._paste_and_send("正确文本")
        except SendNotSubmitted as exc:
            error = exc

    assert state["submitted"] == []
    assert attempt.submitted is False
    assert error is not None


@pytest.mark.parametrize("send_button", [True, False])
def test_always_unreadable_input_rejects_even_successful_paste(monkeypatch, send_button):
    client, state = input_client(monkeypatch, ["正确文本"] * 3, send_button=send_button)
    state["unavailable_pattern_reads"] = "all"

    with track_send_attempt() as attempt:
        with pytest.raises(SendNotSubmitted):
            client._paste_and_send("正确文本")

    assert attempt.submitted is False
    assert state["submitted"] == []


@pytest.mark.parametrize("send_button", [True, False])
def test_previously_readable_pattern_loss_stays_rejected_across_retries(monkeypatch, fast_sender_clock, send_button):
    client, state = input_client(monkeypatch, ["正确文本"] * 3, send_button=send_button)
    monkeypatch.setattr(client, "_wait_for_input_value", client._send_controller._wait_for_input_value)

    def lose_pattern(minimum, maximum):
        last_settle = "uia_pre_send_settle_ms_min" if send_button else "uia_paste_settle_ms_min"
        if minimum == last_settle:
            state["unavailable_pattern_reads"] = "all"

    monkeypatch.setattr(client, "_paced_wait", lose_pattern)
    with track_send_attempt() as attempt:
        with pytest.raises(SendNotSubmitted):
            client._paste_and_send("正确文本")

    assert attempt.submitted is False
    assert state["submitted"] == []


@pytest.mark.parametrize("failure", ["unavailable", "changed", "unreadable_after_focus", "unfocused"])
def test_first_enter_submission_requires_readable_matching_focused_draft(monkeypatch, fast_sender_clock, failure):
    client, state = input_client(monkeypatch, ["正确文本"] * 3, send_button=False)
    control = state["input_control"]
    if failure == "unavailable":
        state["unavailable_pattern_reads"] = "all"
    elif failure == "unfocused":
        monkeypatch.setattr(control, "HasKeyboardFocus", False)
    else:
        def change_after_paste(minimum, maximum):
            if minimum == "uia_paste_settle_ms_min":
                if failure == "changed":
                    state["value"] = "错误草稿"
                else:
                    state["unavailable_pattern_reads"] = "all"

        monkeypatch.setattr(client, "_paced_wait", change_after_paste)

    with track_send_attempt() as attempt:
        with pytest.raises(SendNotSubmitted):
            client._paste_and_send("正确文本")

    assert attempt.submitted is False
    assert state["submitted"] == []


def test_first_enter_submission_sends_readable_matching_focused_draft(monkeypatch):
    client, state = input_client(monkeypatch, ["正确文本"], send_button=False)

    with track_send_attempt() as attempt:
        client._paste_and_send("正确文本")

    assert attempt.submitted is True
    assert state["submitted"] == ["正确文本"]


@pytest.mark.parametrize("failure", ["unreadable", "changed", "stale_matching_draft"])
def test_uncertain_click_never_repeats_enter_submission(monkeypatch, fast_sender_clock, failure):
    client, state = input_client(monkeypatch, ["正确文本"])
    state["clear_input_on_submit"] = False
    state["expose_bubble_on_submit"] = False
    if failure == "unreadable":
        state["unavailable_after_submit"] = True
    elif failure == "changed":
        read_value = state["input_control"].GetValuePattern

        def change_after_submission():
            if state["submitted"]:
                state["value"] = "错误草稿"
            return read_value()

        monkeypatch.setattr(state["input_control"], "GetValuePattern", change_after_submission)
    monkeypatch.setattr(client, "locate_conversation", lambda *args: True)
    monkeypatch.setattr(client, "_clipboard", lambda **kwargs: nullcontext())

    result = client.send_message("Synthetic", "正确文本", expedited=True)

    assert state["submitted"] == ["正确文本"]
    assert result["status"] == "uncertain"
    assert result["submitted_chunks"] == 1
    assert result["verified_chunks"] == 0
    assert result["retryable"] is False


def test_draft_change_before_click_is_rechecked(monkeypatch):
    client, state = input_client(monkeypatch, ["正确文本"] * 3)

    def change_draft(minimum, maximum):
        if minimum == "uia_pre_send_settle_ms_min":
            state["value"] = "粘贴后变动的草稿"

    monkeypatch.setattr(client, "_paced_wait", change_draft)
    with track_send_attempt() as attempt:
        with pytest.raises(SendNotSubmitted):
            client._paste_and_send("正确文本")

    assert attempt.submitted is False
    assert state["submitted"] == []
    assert state["clears"] == 3


@pytest.mark.parametrize("second_succeeds", [False, True])
def test_real_chunk_sender_keeps_partial_submission_semantics(monkeypatch, fast_sender_clock, second_succeeds):
    first, second = "A" * 100, "B" * 20
    values = [first, second] if second_succeeds else [first, "错误草稿", "错误草稿", "错误草稿"]
    client, state = input_client(monkeypatch, values)
    client.config["uia_text_chunk_chars"] = 100
    monkeypatch.setattr(client, "locate_conversation", lambda *args: True)
    monkeypatch.setattr(client, "_clipboard", lambda **kwargs: nullcontext())

    result = client.send_message("Synthetic", first + second, expedited=True)

    assert state["submitted"] == ([first, second] if second_succeeds else [first])
    assert result["chunks"] == 2
    assert result["submitted_chunks"] == (2 if second_succeeds else 1)
    assert result["verified_chunks"] == (2 if second_succeeds else 1)
    assert result["verified"] is second_succeeds
    if not second_succeeds:
        assert result["status"] == "partial"
        assert result["retryable"] is False


def history_client(monkeypatch, entries):
    """使用真实发送快照和可信子控件；OCR 入口调用会使测试失败。"""
    controls = []
    lines = []
    for index, (text, direction, runtime) in enumerate(entries):
        horizontal = {"incoming": (40, 220), "outgoing": (680, 860), "unknown": (420, 480)}
        left, right = horizontal[direction]
        bounds = (left, index * 60 + 10, right, index * 60 + 50)
        body = GeometryControl(bounds, text, "mmui::ChatTextBubble")
        item = GeometryControl((0, bounds[1], 900, bounds[3]), text,
                               "mmui::ChatTextItemView", children=[body])
        item.GetRuntimeId = lambda value=runtime: value
        controls.append(item)
        lines.append(OcrTextLine(text, 0.99, bounds))
    header = GeometryControl((0, 0, 200, 20), "Synthetic",
                             automation_id="current_chat_name_label")
    message_list = GeometryControl((0, 0, 900, 700), children=controls,
                                   automation_id="chat_message_list")
    root = GeometryControl((0, 0, 900, 800), children=[header, message_list])
    client = WechatUiaClient({})
    monkeypatch.setattr(client, "_uia_root", lambda: nullcontext(root))
    monkeypatch.setattr(client._group_sender_ocr, "enrich",
                        lambda *args, **kwargs: pytest.fail("发送快照不得触发 OCR"))
    return client, message_list, lines


@pytest.mark.parametrize("runtime", [(), (42, 1)])
def test_existing_identical_outgoing_bubble_is_unverified(monkeypatch, fast_sender_clock, runtime):
    client, _, _ = history_client(monkeypatch, [("重复文本", "outgoing", runtime)])
    before = client.get_send_bubble_snapshot(limit=5)

    assert client._verify_send("Synthetic", before, text="重复文本")["verified"] is False


@pytest.mark.parametrize("direction", ["incoming", "unknown"])
@pytest.mark.parametrize("runtime", [(), (42, 2)])
def test_matching_new_bubble_requires_outgoing_direction(monkeypatch, fast_sender_clock, direction, runtime):
    client, _, _ = history_client(monkeypatch, [("匹配文本", direction, runtime)])

    assert client._verify_send("Synthetic", [], text="匹配文本")["verified"] is False


@pytest.mark.parametrize("runtime", [(), (42, 2)])
def test_new_outgoing_text_is_verified(monkeypatch, fast_sender_clock, runtime):
    client, _, _ = history_client(monkeypatch, [("新增文本", "outgoing", runtime)])

    assert client._verify_send("Synthetic", [], text="新增文本")["verified"] is True


def test_no_id_duplicate_requires_increased_outgoing_count(monkeypatch, fast_sender_clock):
    client, _, _ = history_client(monkeypatch, [("重复文本", "outgoing", ()),
                                              ("重复文本", "incoming", ())])
    after = client.get_send_bubble_snapshot(limit=5)

    assert client._verify_send("Synthetic", after[:1], text="重复文本")["verified"] is False
    assert client._verify_send("Synthetic", after[1:], text="重复文本")["verified"] is True


def test_no_id_identical_outgoing_append_is_verified(monkeypatch, fast_sender_clock):
    client, _, _ = history_client(monkeypatch, [("重复文本", "outgoing", ()),
                                              ("重复文本", "outgoing", ())])
    before = client.get_send_bubble_snapshot(limit=5)[:1]

    assert client._verify_send("Synthetic", before, text="重复文本")["verified"] is True


def test_anonymous_old_bubble_gaining_id_is_unverified(monkeypatch, fast_sender_clock):
    client, _, _ = history_client(monkeypatch, [("旧文本", "outgoing", (42, 2))])
    before = [replace(client.get_send_bubble_snapshot(limit=5)[0], runtime_id="")]

    assert client._verify_send("Synthetic", before, text="旧文本")["verified"] is False


def test_anonymous_old_bubble_resolving_direction_is_unverified(monkeypatch, fast_sender_clock):
    client, _, _ = history_client(monkeypatch, [("旧文本", "outgoing", ())])
    before = [replace(client.get_send_bubble_snapshot(limit=5)[0], direction="unknown")]

    assert client._verify_send("Synthetic", before, text="旧文本")["verified"] is False


@pytest.mark.parametrize("runtime", [(), (42, 2)])
def test_old_unknown_direction_and_identical_new_incoming_do_not_prove_submission(monkeypatch, fast_sender_clock, runtime):
    client, _, _ = history_client(monkeypatch, [("匹配文本", "outgoing", runtime),
                                              ("匹配文本", "incoming", ())])
    before = [replace(client.get_send_bubble_snapshot(limit=5)[0], runtime_id="", direction="unknown")]

    assert client._verify_send("Synthetic", before, text="匹配文本")["verified"] is False


def test_anonymous_old_reference_metadata_improvement_is_unverified(monkeypatch, fast_sender_clock):
    client, _, _ = history_client(monkeypatch, [("回复文本 引用 合成成员 的消息：旧消息", "outgoing", ())])
    read_history = client.get_send_bubble_snapshot
    before = read_history(limit=5)
    assert before[0].reference is not None

    def enrich_reference(**kwargs):
        return [replace(item, reference=replace(item.reference, resolved=True,
                                               degraded=False, strategy="resolved"))
                for item in read_history(**kwargs)]

    monkeypatch.setattr(client, "get_send_bubble_snapshot", enrich_reference)

    assert client._verify_send("Synthetic", before, text="回复文本")["verified"] is False


def test_anonymous_old_file_path_completion_is_unverified(monkeypatch, fast_sender_clock):
    client, message_list, _ = history_client(monkeypatch, [("report.pdf", "outgoing", ())])
    message_list._children[0].ClassName = "mmui::ChatFileItemView"
    read_history = client.get_send_bubble_snapshot
    before = [replace(item, direction="outgoing") for item in read_history(limit=5)]

    def enrich_attachment(**kwargs):
        return [replace(item, direction="outgoing", file_path="D:/synthetic/report.pdf")
                for item in read_history(**kwargs)]

    monkeypatch.setattr(client, "get_send_bubble_snapshot", enrich_attachment)

    assert client._verify_send("Synthetic", before, expected_type="file")["verified"] is False


def test_anonymous_old_body_correction_without_new_bubble_is_unverified(monkeypatch, fast_sender_clock):
    client, message_list, lines = history_client(monkeypatch, [("识别错误文本", "outgoing", ())])
    before = client.get_send_bubble_snapshot(limit=5)
    message_list._children[0].Name = "正确文本"
    lines[0] = replace(lines[0], text="正确文本")

    assert client._verify_send("Synthetic", before, text="正确文本")["verified"] is False


def test_anonymous_old_body_correction_gaining_id_is_unverified(monkeypatch, fast_sender_clock):
    client, message_list, lines = history_client(monkeypatch, [("识别错误文本", "outgoing", ())])
    before = client.get_send_bubble_snapshot(limit=5)
    message_list._children[0].Name = "正确文本"
    message_list._children[0].GetRuntimeId = lambda: (42, 2)
    lines[0] = replace(lines[0], text="正确文本")

    assert client._verify_send("Synthetic", before, text="正确文本")["verified"] is False


@pytest.mark.parametrize("runtime", [(), (42, 2)])
def test_body_correction_with_unrelated_new_incoming_is_unverified(monkeypatch, fast_sender_clock, runtime):
    client, _, _ = history_client(monkeypatch, [("正确文本", "outgoing", runtime),
                                              ("无关新消息", "incoming", ())])
    before = [replace(client.get_send_bubble_snapshot(limit=5)[0], content="识别错误文本", runtime_id="")]

    assert client._verify_send("Synthetic", before, text="正确文本")["verified"] is False


def test_different_type_anonymous_baseline_prevents_full_snapshot_new_id_claim(monkeypatch, fast_sender_clock):
    entries = [(f"文本{index}", "outgoing", (42, index)) for index in range(1, 5)]
    entries.append(("新增文本", "outgoing", (42, 6)))
    client, _, _ = history_client(monkeypatch, entries)
    before = client.get_send_bubble_snapshot(limit=5)
    before[-1] = replace(before[-1], content="report.pdf", message_type="file", runtime_id="")

    assert client._verify_send("Synthetic", before, text="新增文本")["verified"] is False


@pytest.mark.parametrize("stable_ids", [False, True])
def test_full_snapshot_append_requires_stable_identity(monkeypatch, fast_sender_clock, stable_ids):
    entries = [(f"文本{index}", "outgoing", (42, index) if stable_ids else ())
               for index in range(1, 5)]
    entries.append(("新增文本", "outgoing", (42, 6) if stable_ids else ()))
    client, _, _ = history_client(monkeypatch, entries)
    before = client.get_send_bubble_snapshot(limit=5)
    before[-1] = replace(before[-1], content="之前的文本",
                         runtime_id="42.5" if stable_ids else "")

    assert client._verify_send("Synthetic", before, text="新增文本")["verified"] is stable_ids


@pytest.mark.parametrize("direction, expected", [("outgoing", True), ("incoming", False), ("unknown", False)])
def test_file_verification_requires_new_outgoing_snapshot(monkeypatch, fast_sender_clock, direction, expected):
    client = WechatUiaClient({})
    old = UiaChatMessage("成员", "report.pdf", message_type="file", direction="outgoing")
    new = replace(old, runtime_id="42.2", direction=direction)
    monkeypatch.setattr(client, "get_send_bubble_snapshot", lambda **_: [old, new])

    assert client._verify_send("Synthetic", [old], expected_type="file")["verified"] is expected
