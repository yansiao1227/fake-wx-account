"""合并记录的 DB 正文与独立 UIA 图片证据不得混为逐项已读正文。"""

from copy import deepcopy
from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest

from channel.wechat_desktop.models import UiaReferencedMessage
from channel.wechat_desktop.pipeline.agent_reply import AgentReplyCoordinator
from channel.wechat_desktop.pipeline.prompts import _render_event_context
from channel.wechat_desktop.references import event_chat_record
from .test_chat_record_pipeline import BODY, record_event
from .test_db_chat_records import TITLE, app_record, text_item
from .test_db_reference_lookup import update
from .test_db_uia_attachment_bridge import bridge, observe_target, reference_xml


def observe_record(bridge):
    body = app_record(text_item("由数据库取得的真实正文", sender="记录作者")
                      + text_item("", sender="图片作者", datatype=2))
    event = observe_target(
        bridge, reference_xml(49, escape(body)),
        reference=UiaReferencedMessage("Synthetic", "[聊天记录]" + TITLE,
                                       message_type="text", degraded=True))
    assert event_chat_record(event)["status"] == "partial"
    return event


def test_verified_media_read_keeps_db_entries_and_passes_exact_record_source_id(bridge, tmp_path):
    instance, _, _, client, _, _ = bridge
    event = observe_record(bridge)
    db_record = deepcopy(event_chat_record(event))
    source_identity = (event.event_id, event.source_message_id, event.conversation_id)
    original_image = tmp_path / "original.png"
    original_image.write_bytes(b"synthetic image evidence")
    page = tmp_path / "page.png"
    page.write_bytes(b"synthetic page evidence")
    calls = []

    def fetch(message, payload, *, tmp_root, validate_target):
        focus_count = client.focus_count
        validate_target()
        validate_target()
        assert client.focus_count == focus_count, "辅助窗读取期间来源复核不得重新激活主窗"
        assert message.runtime_id == "bubble-2"
        assert message.reference.message_type == "chat_record"
        assert payload["source_message_id"] == event.reference["source_message_id"]
        assert payload["entries"] == db_record["entries"]
        calls.append((tmp_root, payload["source_message_id"]))
        return {"status": "partial", "evidence_paths": [str(page)],
                "image_evidence_paths": [str(original_image)], "page_count": 1,
                "image_evidence": [{"path": str(original_image), "runtime_id": "record-picture-7"}],
                "truncated": True, "media_status": "evidence_only",
                "native_xml": "must not enter Agent context"}

    client.fetch_referenced_chat_record = fetch
    result, count = instance.materialize_event(event)
    record = event_chat_record(result)

    assert count == 1 and len(calls) == 1
    assert result.attachment_status == "materialized"
    assert (result.event_id, result.source_message_id, result.conversation_id) == source_identity
    assert record["content"] == db_record["content"] and record["status"] == "partial"
    assert record["entries"] == db_record["entries"]
    image_entry = next(entry for entry in record["entries"] if entry["content_type"] == "image")
    assert not image_entry["resolved"] and "file_path" not in image_entry
    evidence = record["media_evidence"]
    assert evidence["image_evidence_paths"] == [str(original_image)]
    assert "native_xml" not in evidence


@pytest.mark.parametrize("case", ["disabled", "resolved_image", "no_image", "missing_body", "direct"])
def test_records_without_eligible_unread_reference_image_do_not_touch_uia(bridge, case):
    instance, _, _, client, _, _ = bridge
    event = (observe_target(bridge, app_record(text_item("由数据库取得的真实正文")
                                              + text_item("", datatype=2)))
             if case == "direct" else observe_record(bridge))
    record = event_chat_record(event)
    if case == "disabled":
        instance.config["chat_record_uia_media_enabled"] = False
    elif case == "resolved_image":
        next(entry for entry in record["entries"] if entry["content_type"] == "image")["resolved"] = True
    elif case == "no_image":
        next(entry for entry in record["entries"] if entry["content_type"] == "image")["content_type"] = "voice"
    elif case == "missing_body":
        record.update(status="unavailable", content="", entries=[])
    client.fetch_referenced_chat_record = lambda *args, **kwargs: pytest.fail("不满足图片补读条件不能打开记录")

    result, count = instance.materialize_event(event)

    assert count == 0 and client.focus_count == 0
    assert "media_evidence" not in event_chat_record(result)
    assert result.attachment_status == ("unavailable" if case == "missing_body" else "materialized")


def test_uia_media_failure_keeps_readable_db_partial_body_and_does_not_retry(bridge):
    instance, _, _, client, _, _ = bridge
    event = observe_record(bridge)
    before = deepcopy(event_chat_record(event))
    calls = []

    def failed_fetch(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("bounded auxiliary window read failed")

    client.fetch_referenced_chat_record = failed_fetch
    result, count = instance.materialize_event(event)
    record = event_chat_record(result)

    assert count == 0 and calls == [True]
    assert result.attachment_status == "materialized"
    assert record["status"] == "partial" and record["content"] == before["content"]
    assert record["entries"] == before["entries"]
    assert record["media_evidence"]["status"] == "unavailable"
    assert record["media_evidence"]["evidence_paths"] == []


def test_changed_wechat_window_discards_media_evidence_but_keeps_valid_db_body(bridge):
    instance, _, _, client, _, _ = bridge
    event = observe_record(bridge)
    before = deepcopy(event_chat_record(event))
    calls = []

    def replaced_window(*args, **kwargs):
        calls.append(True)
        client.hwnd += 1
        return {"status": "partial", "image_evidence_paths": ["wrong-window-image.png"]}

    client.fetch_referenced_chat_record = replaced_window
    result, count = instance.materialize_event(event)
    record = event_chat_record(result)

    assert count == 0 and calls == [True]
    assert result.attachment_status == "materialized"
    assert record["content"] == before["content"] and record["entries"] == before["entries"]
    assert record["media_evidence"]["status"] == "unavailable"
    assert record["media_evidence"]["strategy"] == "attachment_target_changed"
    assert "wrong-window-image.png" not in str(record)


@pytest.mark.parametrize("moment", ["before_read", "during_read"])
def test_changed_native_quote_rejects_media_and_agent_dispatch(bridge, moment):
    instance, reader, _, client, _, talker = bridge
    event = observe_record(bridge)
    calls = []

    def mutate():
        update(reader.caches["message/message_0.db"], talker, local_id=2,
               message_content="native source changed")

    def fetch(*args, **kwargs):
        calls.append(True)
        mutate()
        return {"status": "partial", "evidence_paths": ["stale-evidence.png"]}

    client.fetch_referenced_chat_record = fetch
    if moment == "before_read":
        mutate()
    result, count = instance.materialize_event(event)
    compose_calls = []
    channel = SimpleNamespace(_compose_context=lambda *args, **kwargs: compose_calls.append(args))

    assert count == 0 and result.attachment_status == "source_invalid"
    assert result.reference["fetch_status"] == "source_invalid"
    assert calls == ([] if moment == "before_read" else [True])
    assert "media_evidence" not in event_chat_record(result)
    assert AgentReplyCoordinator(channel).dispatch(result) is False
    assert compose_calls == []


def media_event(tmp_path, *, originals=True):
    event = record_event(status="partial")
    page = tmp_path / "record-page.png"
    page.write_bytes(b"synthetic page")
    original = tmp_path / "record-original.png"
    original.write_bytes(b"synthetic original")
    event.reference["chat_record"]["media_evidence"] = {
        "status": "partial", "evidence_paths": [str(page)],
        "image_evidence_paths": [str(original)] if originals else [],
        "truncated": True,
    }
    return event, str(page), str(original)


def test_prompt_prefers_original_images_to_thumbnail_pages_and_preserves_unread_limits(tmp_path):
    event, page, original = media_event(tmp_path)

    heading, lines, history = _render_event_context(event)
    rendered = "\n".join(lines)

    assert BODY in rendered and original in rendered
    assert page not in rendered
    assert "必须调用 vision" in rendered
    assert "未逐项绑定" in rendered and "不按列表顺序猜发送者" in rendered
    assert "不能声称读完全部媒体" in rendered
    assert history == []


def test_missing_original_file_falls_back_to_page_evidence_without_claiming_media_read(tmp_path):
    event, page, original = media_event(tmp_path)
    event.reference["chat_record"]["media_evidence"]["image_evidence_paths"] = [str(tmp_path / "missing.png")]

    rendered = "\n".join(_render_event_context(event)[1])

    assert page in rendered and "missing.png" not in rendered
    assert "截图尚未分析" in rendered and "未覆盖的图片继续标明未读" in rendered


def test_prompt_caps_original_image_evidence_before_agent_reading(tmp_path):
    event, page, original = media_event(tmp_path)
    extra = tmp_path / "over-budget.png"
    extra.write_bytes(b"synthetic evidence outside the configured reading budget")
    event.reference["chat_record"]["media_evidence"]["image_evidence_paths"].append(str(extra))

    rendered = "\n".join(_render_event_context(event, {"chat_record_uia_max_images": 1})[1])

    assert original in rendered and str(extra) not in rendered and page not in rendered


def test_dispatch_marks_original_and_page_evidence_as_inputs_with_no_duplicate_paths(tmp_path):
    event, page, original = media_event(tmp_path)
    evidence = event.reference["chat_record"]["media_evidence"]
    evidence["image_evidence_paths"].append(original)
    captured = {}
    channel = SimpleNamespace(config={}, _driver=SimpleNamespace(), _trace=lambda *args, **kwargs: None,
                              _compose_context=lambda ctype, content, **kwargs:
                              captured.update(content=content, kwargs=kwargs))

    assert AgentReplyCoordinator(channel).dispatch(event) is False
    paths = captured["kwargs"]["wechat_desktop_input_artifact_paths"]
    assert paths.count(original) == 1 and paths.count(page) == 1
    assert set(paths) == {original, page}
    assert original in captured["content"] and page not in captured["content"]
    assert captured["kwargs"]["wechat_desktop_chat_record"] is True
    assert "wechat_history" in captured["kwargs"]["wechat_desktop_disabled_tools"]
