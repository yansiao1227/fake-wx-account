"""合并转发聊天记录的合成 XML；schema 来源见 db/chat_records.py。"""

from xml.sax.saxutils import escape

import pytest

from .test_db_reader import add_message, checkpoints, make_reader
from .test_db_reference_lookup import add_question, question_event, update
from channel.wechat_desktop.references import event_chat_record


TITLE = "南街和九月底的聊天记录"


def text_item(content="完整的合并消息正文", sender="九月底", *, datatype=1):
    return (f'<dataitem datatype="{datatype}"><sourcename>{escape(sender)}</sourcename>'
            '<sourcetime>2026-09-30 12:00:00</sourcetime>'
            f'<datadesc>{escape(content)}</datadesc></dataitem>')


def record_info(items):
    return f'<recordinfo><title>{TITLE}</title><datalist>{items}</datalist></recordinfo>'


def app_record(items=None, *, inner=None, element=False):
    inner = record_info(text_item() if items is None else items) if inner is None else inner
    payload = inner if element else f'<![CDATA[{inner}]]>'
    return (f'<msg><appmsg><type>19</type><title>{TITLE}</title>'
            f'<des>仅供预览的摘要</des><recorditem>{payload}</recorditem></appmsg></msg>')


def direct_event(reader, talker, content, *, message_type=(19 << 32) | 49):
    add_message(reader.caches["message/message_0.db"], talker, 1, content=content,
                message_type=message_type)
    reader.refresh()
    return reader.poll_batch(checkpoints(reader), {}).records[0].event


@pytest.mark.parametrize("element", [False, True])
def test_direct_chat_record_reads_structured_body_and_preserves_card_title(tmp_path, element):
    reader, talker = make_reader(tmp_path)
    event = direct_event(reader, talker, app_record(element=element))
    assert event.content_type == "chat_record"
    assert event.content == TITLE
    assert event.chat_record["title"] == TITLE
    assert event.chat_record["status"] == "success"
    assert "完整的合并消息正文" in event.chat_record["content"]
    assert event.chat_record["entries"][0]["sender_name"] == "九月底"
    assert event.chat_record["entries"][0]["content"] == "完整的合并消息正文"
    assert event.chat_record["truncated"] is False


def test_quoted_chat_record_inline_xml_is_resolved_without_title_becoming_body(tmp_path):
    reader, talker = make_reader(tmp_path)
    add_question(reader.caches["message/message_0.db"], talker,
                 native_type=49, content=app_record(), sender=talker)
    reference = question_event(reader).reference
    assert reference["content_type"] == "chat_record"
    assert reference["content"] == TITLE
    assert reference["chat_record"]["status"] == "success"
    assert reference["resolved"] is True and reference["degraded"] is False


def test_quoted_chat_record_title_is_completed_by_exact_original_and_validated(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content=app_record(), message_type=49, created=10, server_id=55)
    add_question(cache, talker, native_type=49, content=TITLE, sender=talker)
    event = question_event(reader)
    reference = event.reference
    assert reference["content_type"] == "chat_record"
    assert reference["content"] == TITLE and reference["preview_content"] == TITLE
    assert reference["chat_record"]["status"] == "success"
    assert reference["resolved"] is True and reference["source_local_id"] == 1
    assert reader.validate_native_event(event) == (True, "")
    update(cache, talker, message_content=app_record(text_item("正文已经变化")))
    valid, reason = reader.validate_native_event(event)
    assert valid is False and reason == "reference_source_changed"


@pytest.mark.parametrize("inner,status", [("", "unavailable"), ("标题预览", "unavailable"),
                                           ("<recordinfo><datalist>", "invalid"),
                                           ("<recordinfo><title>只有标题</title></recordinfo>", "unavailable")])
def test_missing_or_invalid_record_body_is_explicitly_unresolved(tmp_path, inner, status):
    reader, talker = make_reader(tmp_path)
    event = direct_event(reader, talker, app_record(inner=inner))
    assert event.content_type == "chat_record" and event.content == TITLE
    assert event.chat_record["status"] == status
    assert event.chat_record["content"] == "" and event.chat_record["entries"] == []


def test_plain_quoted_title_is_unavailable_even_without_original(tmp_path):
    reader, talker = make_reader(tmp_path)
    add_question(reader.caches["message/message_0.db"], talker,
                 native_type=49, content=TITLE, sender=talker)
    reference = question_event(reader).reference
    assert reference["content_type"] == "chat_record"
    assert reference["resolved"] is False and reference["degraded"] is True
    assert reference["chat_record"]["status"] == "unavailable"
    assert reference["chat_record"]["content"] == ""


def test_text_type57_with_chat_record_shaped_title_remains_one_layer_text(tmp_path):
    reader, talker = make_reader(tmp_path)
    nested = (f'<msg><appmsg><type>57</type><title>{TITLE}</title><refermsg>'
              f'<type>49</type><content>{escape(app_record())}</content></refermsg></appmsg></msg>')
    add_question(reader.caches["message/message_0.db"], talker,
                 native_type=49, content=nested, sender=talker)
    reference = question_event(reader).reference
    assert reference["content_type"] == "text" and reference["content"] == TITLE
    assert reference["native_app_type"] == 57
    assert "chat_record" not in reference


@pytest.mark.parametrize("subtype", [5, 6, 57])
def test_native_app_subtype_is_retained_when_reference_only_has_card_title(tmp_path, subtype):
    reader, talker = make_reader(tmp_path)
    add_question(reader.caches["message/message_0.db"], talker,
                 native_type=(subtype << 32) | 49, content=TITLE)
    event = question_event(reader)

    assert event.reference["native_app_type"] == subtype
    assert event.reference["content_type"] == "app_message"
    assert event_chat_record(event) is None


@pytest.mark.parametrize("subtype", [5, 6])
def test_share_or_file_title_with_chat_record_suffix_remains_its_original_type(tmp_path, subtype):
    reader, talker = make_reader(tmp_path)
    app = f'<msg><appmsg><type>{subtype}</type><title>{TITLE}</title></appmsg></msg>'
    add_question(reader.caches["message/message_0.db"], talker, native_type=49, content=app)
    event = question_event(reader)

    assert event.reference["content_type"] == {5: "share_card", 6: "file"}[subtype]
    assert event_chat_record(event) is None


def test_record_card_cannot_add_another_reference_layer_or_reuse_body(tmp_path):
    reader, talker = make_reader(tmp_path)
    stray = '<refermsg><type>49</type><content>' + escape(app_record(inner="")) + '</content></refermsg>'
    value = app_record().replace('</appmsg>', stray + '</appmsg>')
    event = direct_event(reader, talker, value)
    assert event.chat_record["status"] == "success"
    assert event.reference == {}


@pytest.mark.parametrize("datatype,kind", [(2, "image"), (3, "voice"), (4, "video"),
                                           (5, "share_card"), (8, "file"), (999, "unsupported")])
def test_media_fields_are_markers_and_never_download_or_file_content(tmp_path, datatype, kind):
    reader, talker = make_reader(tmp_path)
    media = (f'<dataitem datatype="{datatype}"><sourcename>媒体发送者</sourcename>'
             '<datadesc>不可当正文的媒体预览</datadesc><datatitle>不可当正文的文件名.jpg</datatitle>'
             '<link>https://synthetic.invalid/secret</link><cdndatakey>fake-secret</cdndatakey>'
             '<datasourcepath>C:\\synthetic\\secret.jpg</datasourcepath></dataitem>')
    result = direct_event(reader, talker, app_record(text_item() + media)).chat_record
    assert result["status"] == "partial"
    entry = result["entries"][1]
    assert entry["content_type"] == kind and entry["resolved"] is False
    assert "内容未读取" in entry["content"]
    for hidden in ("不可当正文", "synthetic.invalid", "fake-secret", "secret.jpg"):
        assert hidden not in str(result)


def test_media_only_record_does_not_claim_readable_body(tmp_path):
    reader, talker = make_reader(tmp_path)
    add_question(reader.caches["message/message_0.db"], talker,
                 native_type=49, content=app_record(text_item("预览", datatype=2)))
    reference = question_event(reader).reference
    assert reference["chat_record"]["status"] == "unavailable"
    assert reference["resolved"] is False
    assert reference["chat_record"]["entries"][0]["resolved"] is False


def nested_item(items, *, encoded=False):
    inner = record_info(items)
    return '<dataitem datatype="17"><recordxml>' + (escape(inner) if encoded else inner) + '</recordxml></dataitem>'


@pytest.mark.parametrize("encoded", [False, True])
def test_nested_chat_records_share_global_message_budget(tmp_path, encoded):
    reader, talker = make_reader(tmp_path)
    reader.config["chat_record_max_messages"] = 3
    items = nested_item(text_item("嵌套一") + text_item("嵌套二"), encoded=encoded) + text_item("超额的外层")
    result = direct_event(reader, talker, app_record(items)).chat_record
    assert result["status"] == "partial" and result["truncated"] is True
    assert [entry["content"] for entry in result["entries"]] == ["嵌套一", "嵌套二"]
    assert all(entry["depth"] == 1 for entry in result["entries"])
    assert "超额的外层" not in result["content"]


def test_nested_depth_is_bounded_and_cannot_promote_nested_title(tmp_path):
    reader, talker = make_reader(tmp_path)
    reader.config["chat_record_max_depth"] = 1
    items = text_item("已读取") + nested_item(nested_item(text_item("过深的正文")))
    result = direct_event(reader, talker, app_record(items)).chat_record
    assert result["status"] == "partial" and result["truncated"] is True
    assert len(result["entries"]) == 1 and "过深的正文" not in result["content"]
    assert TITLE not in result["content"]


def test_character_budget_is_shared_between_entries(tmp_path):
    reader, talker = make_reader(tmp_path)
    reader.config["chat_record_max_chars"] = 80
    result = direct_event(reader, talker, app_record(text_item("甲" * 100) + text_item("不得再输出"))).chat_record
    assert result["status"] == "partial" and result["truncated"] is True
    assert len(result["title"]) + len(result["content"]) <= 80
    assert len(result["entries"]) == 1 and "不得再输出" not in result["content"]


def test_raw_xml_budget_is_shared_by_outer_and_cdata_parse(tmp_path):
    reader, talker = make_reader(tmp_path)
    value = app_record()
    reader.config["chat_record_max_xml_bytes"] = len(value.encode("utf-8"))
    result = direct_event(reader, talker, value).chat_record
    assert result["status"] == "unavailable" and result["truncated"] is True
    assert result["content"] == "" and result["entries"] == []


def test_raw_xml_budget_rejects_before_elementtree_parsing(tmp_path, monkeypatch):
    from channel.wechat_desktop.db import chat_records
    reader, talker = make_reader(tmp_path)
    reader.config["chat_record_max_xml_bytes"] = 10
    monkeypatch.setattr(chat_records.ElementTree, "fromstring", lambda *_: pytest.fail("parsed oversized XML"))
    result = direct_event(reader, talker, app_record()).chat_record
    assert result["status"] == "unavailable" and result["truncated"] is True


def test_xml_entities_are_rejected_before_expansion(tmp_path):
    reader, talker = make_reader(tmp_path)
    inner = '<!DOCTYPE recordinfo [<!ENTITY expanded "synthetic">]><recordinfo><datalist/></recordinfo>'
    result = direct_event(reader, talker, app_record(inner=inner)).chat_record
    assert result["status"] == "invalid" and result["content"] == ""


def test_exact_origin_chat_record_never_crosses_duplicate_svrid(tmp_path):
    reader, talker = make_reader(tmp_path, shards=2)
    first = reader.caches["message/message_0.db"]
    second = reader.caches["message/message_1.db"]
    for cache in (first, second):
        add_message(cache, talker, 1, content=app_record(), message_type=49, created=10, server_id=55)
    add_question(first, talker, local_id=2, native_type=49, content=TITLE)
    reference = question_event(reader).reference
    assert reference["chat_record"]["status"] == "unavailable" and reference["resolved"] is False
    assert "source_native_message_id" not in reference


@pytest.mark.parametrize("oversized", [False, True])
def test_unknown_low49_failed_xml_never_reaches_display_content_and_keeps_native_signature(tmp_path, oversized):
    reader, talker = make_reader(tmp_path)
    raw = app_record() if oversized else '<msg><appmsg><type>19</type><title>损坏的应用消息'
    if oversized:
        reader.config["chat_record_max_xml_bytes"] = 10
    event = direct_event(reader, talker, raw, message_type=49)
    assert event.content == "[应用消息正文不可用]"
    assert event.content_type == "app_message" and event.reference == {}
    assert event.attachment_status == "native_xml_unavailable"
    assert reader.validate_native_event(event) == (True, "")
    update(reader.caches["message/message_0.db"], talker, message_content=raw + "不同原文")
    assert reader.validate_native_event(event) == (False, "source_message_changed")


def test_outer_type57_xml_budget_failure_is_explicit_and_does_not_leak_reference_xml(tmp_path):
    from .test_db_reference_lookup import quote
    reader, talker = make_reader(tmp_path)
    reader.config["chat_record_max_xml_bytes"] = 10
    raw = quote(native_type=49, content=app_record())
    event = direct_event(reader, talker, raw, message_type=(57 << 32) | 49)
    assert event.content == "[应用消息正文不可用]" and event.reference == {}
    assert event.attachment_status == "native_xml_unavailable"
    assert reader.validate_native_event(event) == (True, "")


@pytest.mark.parametrize("invalid", [False, True])
def test_nested_reference_xml_failure_clears_raw_but_preserves_outer_question(tmp_path, invalid):
    from .test_db_reference_lookup import quote
    reader, talker = make_reader(tmp_path)
    inner = '<msg><appmsg><type>19</type><title>损坏的应用消息' if invalid else app_record()
    raw = quote(native_type=49, content=inner)
    if not invalid:
        reader.config["chat_record_max_xml_bytes"] = len(raw.encode("utf-8"))
    event = direct_event(reader, talker, raw, message_type=(57 << 32) | 49)
    assert event.content == "synthetic question"
    assert event.attachment_status == ""
    assert event.reference["content"] == "[应用消息正文不可用]"
    assert event.reference["native_xml_status"] == ("invalid" if invalid else "unavailable")
    assert event.reference["resolved"] is False and event.reference["degraded"] is True
    assert inner not in str(event.to_dict())
    assert reader.validate_native_event(event) == (True, "")


@pytest.mark.parametrize("subtype,kind,title", [(19, "chat_record", TITLE),
                                              (5, "share_card", "完整的分享卡片"),
                                              (6, "file", "完整的文件名.pdf"),
                                              (57, "text", "完整的文字")])
def test_exact_usable_original_clears_active_xml_failure_and_replaces_placeholder(tmp_path, subtype, kind, title):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    original = (app_record() if subtype == 19 else
                f'<msg><appmsg><type>{subtype}</type><title>{title}</title>'
                '<url>https://synthetic.invalid/article</url></appmsg></msg>')
    add_message(cache, talker, 1, content=original, message_type=49, created=10, server_id=55)
    add_question(cache, talker, native_type=49, content='<msg><appmsg><type>19</type>', sender=talker)
    event = question_event(reader)
    assert event.reference["content_type"] == kind and event.reference["content"] == title
    assert "native_xml_status" not in event.reference
    assert event.reference["source_local_id"] == 1
    assert reader.validate_native_event(event) == (True, "")


def test_failed_exact_original_inherits_active_xml_failure_and_keeps_signature_validation(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    original = '<msg><appmsg><type>19</type><title>仍然损坏的原消息'
    add_message(cache, talker, 1, content=original, message_type=49, created=10, server_id=55)
    add_question(cache, talker, native_type=49, content='<msg><appmsg><type>19</type>', sender=talker)
    event = question_event(reader)
    assert event.reference["content_type"] == "app_message"
    assert event.reference["content"] == "[应用消息正文不可用]"
    assert event.reference["native_xml_status"] in {"invalid", "unavailable"}
    assert event.reference["resolved"] is False and event.reference["degraded"] is True
    assert event.reference["source_local_id"] == 1
    assert reader.validate_native_event(event) == (True, "")
    update(cache, talker, message_content=original + "修改但仍然损坏")
    assert reader.validate_native_event(event) == (False, "reference_source_changed")


def test_nonxml_unknown_app_title_preserves_existing_display_path(tmp_path):
    reader, talker = make_reader(tmp_path)
    event = direct_event(reader, talker, "普通应用卡片的标题", message_type=49)
    assert event.content == "普通应用卡片的标题" and event.attachment_status == ""


@pytest.mark.parametrize("original_valid", [False, True])
def test_explicit_origin_enrichment_clears_sticky_event_failure_only_for_usable_original(tmp_path, monkeypatch, original_valid):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    original = app_record() if original_valid else '<msg><appmsg><type>19</type>'
    add_message(cache, talker, 1, content=original, message_type=49, created=10, server_id=55)
    add_question(cache, talker, native_type=49, content='<msg><appmsg><type>19</type>', sender=talker)
    enrich = reader._enrich_reference
    monkeypatch.setattr(reader, "_enrich_reference", lambda event, *_: event)
    event = question_event(reader)
    assert event.reference["native_xml_status"] == "invalid"
    event.attachment_status = "native_xml_unavailable"
    monkeypatch.setattr(reader, "_enrich_reference", enrich)
    assert reader.enrich_reference(event) is event
    if original_valid:
        assert event.attachment_status == "" and "native_xml_status" not in event.reference
        assert event.reference["chat_record"]["status"] == "success"
    else:
        assert event.attachment_status == "native_xml_unavailable"
        assert event.reference["native_xml_status"] == "unavailable"
    assert reader.validate_native_event(event) == (True, "")


def test_known_high19_xml_failure_retains_chat_record_contract(tmp_path):
    reader, talker = make_reader(tmp_path)
    reader.config["chat_record_max_xml_bytes"] = 10
    event = direct_event(reader, talker, app_record())
    assert event.content_type == "chat_record" and event.content == "[聊天记录]"
    assert event.chat_record["status"] == "unavailable" and event.chat_record["truncated"] is True
    assert event.attachment_status == ""


@pytest.mark.parametrize("raw", ['<msg><unknown>不支持的结构</unknown></msg>',
                                  '<wrapper><msg><appmsg><type>19</type><title>嵌套错位</title></appmsg></msg></wrapper>'])
def test_well_formed_xml_without_direct_appmsg_is_explicitly_unavailable(tmp_path, raw):
    reader, talker = make_reader(tmp_path)
    event = direct_event(reader, talker, raw, message_type=49)
    assert event.content == "[应用消息正文不可用]" and event.attachment_status == "native_xml_unavailable"
    add_question(reader.caches["message/message_0.db"], talker, native_type=49, content=raw)
    reference = question_event(reader).reference
    assert reference["content"] == "[应用消息正文不可用]" and reference["native_xml_status"] == "unavailable"
    assert reference["resolved"] is False
