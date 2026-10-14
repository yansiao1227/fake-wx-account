"""聊天记录标题不能证明正文可读，数据库结果和 UIA 卡片状态分开处理。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.references import chat_record_ready, event_chat_record, reference_requires_uia
from channel.wechat_desktop.uia.client import WechatUiaClient
from .test_uia_reference import _strict_reference_client


@pytest.mark.parametrize("payload", [
    None, {}, {"status": "success", "content": "卡片标题"},
    {"status": "success", "content": "", "entries": [{"content": "消息"}]},
    {"status": "unavailable", "content": "标题", "entries": [{"content": "消息"}]},
    {"status": "invalid", "content": "标题", "entries": [{"content": "消息"}]},
])
def test_record_requires_actual_body_entries_and_explicit_read_status(payload):
    assert not chat_record_ready(payload)


@pytest.mark.parametrize("status", ["success", "partial"])
def test_complete_and_partial_record_body_are_readable(status):
    assert chat_record_ready({"status": status, "content": "甲: 合成消息",
                              "entries": [{"content": "合成消息"}]})


def test_legacy_native_app_title_cannot_bypass_record_failure_gate():
    event = SimpleNamespace(reference={"native_type": 49, "content_type": "text",
                                      "content": "甲和乙的聊天记录", "resolved": True},
                            content_type="text")
    assert event_chat_record(event) == {}
    event.reference["native_app_type"] = 57
    assert event_chat_record(event) is None
    event.reference.pop("native_app_type")
    event.reference["source_content_type"] = "text"
    assert event_chat_record(event) is None


def test_plain_text_discussing_records_is_not_a_forwarded_record():
    event = SimpleNamespace(reference={"native_type": 1, "content_type": "text",
                                      "content": "甲和乙的聊天记录", "resolved": True},
                            content_type="text")
    assert event_chat_record(event) is None


def test_known_record_does_not_fetch_title_through_uia():
    assert not reference_requires_uia({"content_type": "chat_record", "resolved": False})


def test_uia_record_card_is_distinct_from_text_and_flattened_reference():
    title = "甲和乙的聊天记录"
    assert WechatUiaClient._message_type("mmui::ChatBubbleItemView", title) == "chat_record"
    assert WechatUiaClient._message_type("mmui::ChatTextItemView", title) == "text"
    assert WechatUiaClient._message_type(
        "mmui::ChatBubbleItemView", f"看看引用 甲 的消息 : {title}"
    ) == "text"


def test_located_record_title_is_never_resolved_as_text(monkeypatch):
    client, quoted, quote_control, original, state = _strict_reference_client(monkeypatch, kind="app_message")
    title = "甲和乙的聊天记录"
    quoted = replace(quoted, reference=replace(quoted.reference, content=title))
    quote_control.Name = f"question引用 Synthetic 的消息 : {title}"
    original.Name, original.ClassName = title, "mmui::ChatBubbleItemView"

    result = client.resolve_message_reference(quoted, allow_filename_cache=False,
                                               validate_target=lambda: state.valid)

    assert result.reference.message_type == "chat_record"
    assert not result.reference.resolved and result.reference.degraded
    assert "fetch" not in state.actions
