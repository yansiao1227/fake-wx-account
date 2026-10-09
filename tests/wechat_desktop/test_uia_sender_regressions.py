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
        message = GeometryControl(bounds, state["value"], "mmui::ChatTextItemView")
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
                        lambda messages, bounds, **kwargs:
                        client._group_sender_ocr.assign_senders(messages, lines, bounds, **kwargs))
    return client, state


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
    # 仅第一次提交前曾可读，拒绝和清空后始终不可读；仍必须记住
    # 此操作读到过错误草稿，不能退回“始终不可读”的兼容提交。
    state["unavailable_pattern_reads"] = set(range(1, 100)) - {2}

    with track_send_attempt() as attempt:
        with pytest.raises(SendNotSubmitted):
            client._paste_and_send("正确文本")

    assert attempt.submitted is False
    assert state["submitted"] == []


@pytest.mark.parametrize("send_button", [True, False])
def test_pattern_recovery_with_correct_draft_checks_post_send_clear(monkeypatch, send_button):
    client, state = input_client(monkeypatch, ["正确文本"], send_button=send_button)
    state["unavailable_pattern_reads"] = {1}

    client._paste_and_send("正确文本")

    assert state["submitted"] == ["正确文本"]
    assert state["post_submit_value_reads"] > 0


@pytest.mark.parametrize("send_button", [True, False])
def test_always_unreadable_input_keeps_initial_send_compatibility(monkeypatch, send_button):
    client, state = input_client(monkeypatch, ["正确文本"], send_button=send_button)
    state["unavailable_pattern_reads"] = "all"

    client._paste_and_send("正确文本")

    assert state["submitted"] == ["正确文本"]


@pytest.mark.parametrize("send_button", [True, False])
def test_previously_readable_pattern_loss_stays_rejected_across_retries(monkeypatch, send_button):
    client, state = input_client(monkeypatch, ["正确文本"] * 3, send_button=send_button)

    def lose_pattern(minimum, maximum):
        if minimum == "uia_paste_settle_ms_min":
            state["unavailable_pattern_reads"] = "all"

    monkeypatch.setattr(client, "_paced_wait", lose_pattern)
    with track_send_attempt() as attempt:
        with pytest.raises(SendNotSubmitted):
            client._paste_and_send("正确文本")

    assert attempt.submitted is False
    assert state["submitted"] == []


@pytest.mark.parametrize("failure", ["unavailable", "changed", "unreadable_after_focus", "unfocused"])
def test_enter_fallback_requires_readable_matching_focused_draft(monkeypatch, fast_sender_clock, failure):
    client, state = input_client(monkeypatch, [])
    state["value"] = "正确文本"
    control = state["input_control"]
    if failure == "unavailable":
        state["unavailable_pattern_reads"] = "all"
    elif failure == "changed":
        monkeypatch.setattr(control, "SetFocus", lambda: state.update(value="错误草稿"))
    elif failure == "unreadable_after_focus":
        state["unavailable_pattern_reads"] = {2}
    else:
        monkeypatch.setattr(control, "HasKeyboardFocus", False)

    with track_send_attempt() as attempt:
        assert client._send_existing_input_with_enter("正确文本") is False

    assert attempt.submitted is False
    assert state["submitted"] == []


def test_enter_fallback_sends_readable_matching_focused_draft(monkeypatch):
    client, state = input_client(monkeypatch, [])
    state["value"] = "正确文本"

    with track_send_attempt() as attempt:
        assert client._send_existing_input_with_enter("正确文本") is True

    assert attempt.submitted is True
    assert state["submitted"] == ["正确文本"]


@pytest.mark.parametrize("failure", ["unreadable", "changed"])
def test_declined_real_enter_fallback_preserves_original_uncertain_click(monkeypatch, fast_sender_clock, failure):
    client, state = input_client(monkeypatch, ["正确文本"])
    state["clear_input_on_submit"] = False
    state["expose_bubble_on_submit"] = False
    if failure == "unreadable":
        state["unavailable_after_submit"] = True
    else:
        def change_on_fallback_focus():
            if state["submitted"]:
                state["value"] = "错误草稿"
        monkeypatch.setattr(state["input_control"], "SetFocus", change_on_fallback_focus)
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
    """保留真实 get_chat_history 与 OCR 方向解析，仅替换控件和截图结果。"""
    controls = []
    lines = []
    for index, (text, direction, runtime) in enumerate(entries):
        horizontal = {"incoming": (40, 220), "outgoing": (680, 860), "unknown": (420, 480)}
        left, right = horizontal[direction]
        bounds = (left, index * 60 + 10, right, index * 60 + 50)
        item = GeometryControl(bounds, text, "mmui::ChatTextItemView")
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
                        lambda messages, bounds, **kwargs:
                        client._group_sender_ocr.assign_senders(messages, lines, bounds, **kwargs))
    return client, message_list, lines


@pytest.mark.parametrize("runtime", [(), (42, 1)])
def test_existing_identical_outgoing_bubble_is_unverified(monkeypatch, fast_sender_clock, runtime):
    client, _, _ = history_client(monkeypatch, [("重复文本", "outgoing", runtime)])
    before = client.get_chat_history(limit=5)

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
    after = client.get_chat_history(limit=5)

    assert client._verify_send("Synthetic", after[:1], text="重复文本")["verified"] is False
    assert client._verify_send("Synthetic", after[1:], text="重复文本")["verified"] is True


def test_no_id_identical_outgoing_append_is_verified(monkeypatch, fast_sender_clock):
    client, _, _ = history_client(monkeypatch, [("重复文本", "outgoing", ()),
                                              ("重复文本", "outgoing", ())])
    before = client.get_chat_history(limit=5)[:1]

    assert client._verify_send("Synthetic", before, text="重复文本")["verified"] is True


def test_anonymous_old_bubble_gaining_id_is_unverified(monkeypatch, fast_sender_clock):
    client, _, _ = history_client(monkeypatch, [("旧文本", "outgoing", (42, 2))])
    before = [replace(client.get_chat_history(limit=5)[0], runtime_id="")]

    assert client._verify_send("Synthetic", before, text="旧文本")["verified"] is False


def test_anonymous_old_bubble_resolving_direction_is_unverified(monkeypatch, fast_sender_clock):
    client, _, _ = history_client(monkeypatch, [("旧文本", "outgoing", ())])
    before = [replace(client.get_chat_history(limit=5)[0], direction="unknown")]

    assert client._verify_send("Synthetic", before, text="旧文本")["verified"] is False


@pytest.mark.parametrize("runtime", [(), (42, 2)])
def test_old_unknown_direction_and_identical_new_incoming_do_not_prove_submission(monkeypatch, fast_sender_clock, runtime):
    client, _, _ = history_client(monkeypatch, [("匹配文本", "outgoing", runtime),
                                              ("匹配文本", "incoming", ())])
    before = [replace(client.get_chat_history(limit=5)[0], runtime_id="", direction="unknown")]

    assert client._verify_send("Synthetic", before, text="匹配文本")["verified"] is False


def test_anonymous_old_reference_metadata_improvement_is_unverified(monkeypatch, fast_sender_clock):
    client, _, _ = history_client(monkeypatch, [("回复文本 引用 合成成员 的消息：旧消息", "outgoing", ())])
    read_history = client.get_chat_history
    before = read_history(limit=5)
    assert before[0].reference is not None

    def enrich_reference(**kwargs):
        return [replace(item, reference=replace(item.reference, resolved=True,
                                               degraded=False, strategy="resolved"))
                for item in read_history(**kwargs)]

    monkeypatch.setattr(client, "get_chat_history", enrich_reference)

    assert client._verify_send("Synthetic", before, text="回复文本")["verified"] is False


def test_anonymous_old_file_path_completion_is_unverified(monkeypatch, fast_sender_clock):
    client, message_list, _ = history_client(monkeypatch, [("report.pdf", "outgoing", ())])
    message_list._children[0].ClassName = "mmui::ChatFileItemView"
    read_history = client.get_chat_history
    before = [replace(item, direction="outgoing") for item in read_history(limit=5)]

    def enrich_attachment(**kwargs):
        return [replace(item, direction="outgoing", file_path="D:/synthetic/report.pdf")
                for item in read_history(**kwargs)]

    monkeypatch.setattr(client, "get_chat_history", enrich_attachment)

    assert client._verify_send("Synthetic", before, expected_type="file")["verified"] is False


def test_anonymous_old_body_correction_without_new_bubble_is_unverified(monkeypatch, fast_sender_clock):
    client, message_list, lines = history_client(monkeypatch, [("识别错误文本", "outgoing", ())])
    before = client.get_chat_history(limit=5)
    message_list._children[0].Name = "正确文本"
    lines[0] = replace(lines[0], text="正确文本")

    assert client._verify_send("Synthetic", before, text="正确文本")["verified"] is False


def test_anonymous_old_body_correction_gaining_id_is_unverified(monkeypatch, fast_sender_clock):
    client, message_list, lines = history_client(monkeypatch, [("识别错误文本", "outgoing", ())])
    before = client.get_chat_history(limit=5)
    message_list._children[0].Name = "正确文本"
    message_list._children[0].GetRuntimeId = lambda: (42, 2)
    lines[0] = replace(lines[0], text="正确文本")

    assert client._verify_send("Synthetic", before, text="正确文本")["verified"] is False


@pytest.mark.parametrize("runtime", [(), (42, 2)])
def test_body_correction_with_unrelated_new_incoming_is_unverified(monkeypatch, fast_sender_clock, runtime):
    client, _, _ = history_client(monkeypatch, [("正确文本", "outgoing", runtime),
                                              ("无关新消息", "incoming", ())])
    before = [replace(client.get_chat_history(limit=5)[0], content="识别错误文本", runtime_id="")]

    assert client._verify_send("Synthetic", before, text="正确文本")["verified"] is False


def test_different_type_anonymous_baseline_prevents_full_snapshot_new_id_claim(monkeypatch, fast_sender_clock):
    entries = [(f"文本{index}", "outgoing", (42, index)) for index in range(1, 5)]
    entries.append(("新增文本", "outgoing", (42, 6)))
    client, _, _ = history_client(monkeypatch, entries)
    before = client.get_chat_history(limit=5)
    before[-1] = replace(before[-1], content="report.pdf", message_type="file", runtime_id="")

    assert client._verify_send("Synthetic", before, text="新增文本")["verified"] is False


@pytest.mark.parametrize("stable_ids", [False, True])
def test_full_snapshot_append_requires_stable_identity(monkeypatch, fast_sender_clock, stable_ids):
    entries = [(f"文本{index}", "outgoing", (42, index) if stable_ids else ())
               for index in range(1, 5)]
    entries.append(("新增文本", "outgoing", (42, 6) if stable_ids else ()))
    client, _, _ = history_client(monkeypatch, entries)
    before = client.get_chat_history(limit=5)
    before[-1] = replace(before[-1], content="之前的文本",
                         runtime_id="42.5" if stable_ids else "")

    assert client._verify_send("Synthetic", before, text="新增文本")["verified"] is stable_ids


@pytest.mark.parametrize("direction, expected", [("outgoing", True), ("incoming", False), ("unknown", False)])
def test_file_verification_requires_new_outgoing_snapshot(monkeypatch, fast_sender_clock, direction, expected):
    client = WechatUiaClient({})
    old = UiaChatMessage("成员", "report.pdf", message_type="file", direction="outgoing")
    new = replace(old, runtime_id="42.2", direction=direction)
    monkeypatch.setattr(client, "get_chat_history", lambda **_: [old, new])

    assert client._verify_send("Synthetic", [old], expected_type="file")["verified"] is expected
