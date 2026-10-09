"""原生载荷复核与附件物化结果隔离，仅使用合成消息。"""

import sqlite3
from xml.sax.saxutils import escape

import pytest

from .test_db_reader import add_message, checkpoints, make_reader, table


def quote(*, native_id="123", body="quoted content", sender="synthetic-user", native_type=1):
    return ("<msg><appmsg><type>57</type><title>same question</title><refermsg>"
            f"<type>{native_type}</type><svrid>{native_id}</svrid><chatusr>{sender}</chatusr>"
            f"<content>{body}</content></refermsg></appmsg></msg>")


def first_event(reader):
    reader.refresh()
    return reader.poll_batch(checkpoints(reader), {}).records[0].event


def update(cache, talker, **fields):
    with sqlite3.connect(cache.path) as conn:
        assignments = ",".join(f'"{name}"=?' for name in fields)
        conn.execute(f'UPDATE "{table(talker)}" SET {assignments} WHERE local_id=1', tuple(fields.values()))
    cache.changed = True


@pytest.mark.parametrize("changed", [
    quote(native_id="124"), quote(body="changed quote"),
    quote(sender="different-native-sender"), quote(native_type=3),
])
def test_same_title_quote_payload_changes_invalidate_native_event(tmp_path, changed):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content=quote(), message_type=49)
    event = first_event(reader)
    fingerprint = event.fingerprint()
    assert event.content == "same question" and len(event.content_signature) == 64
    assert reader.validate_native_event(event) == (True, "")
    update(cache, talker, message_content=changed)
    assert reader.validate_native_event(event) == (False, "source_message_changed")
    assert event.fingerprint() == fingerprint


def test_same_title_share_url_change_is_rejected(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    payload = ("<msg><appmsg><type>5</type><title>same title</title>"
               "<url>https://synthetic.invalid/first</url></appmsg></msg>")
    add_message(cache, talker, 1, content=payload, message_type=49)
    event = first_event(reader)
    update(cache, talker, message_content=payload.replace("/first", "/second"))
    assert reader.validate_native_event(event) == (False, "source_message_changed")


def test_quoted_share_same_title_changed_url_cannot_reuse_native_evidence(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    share = ("<msg><appmsg><type>5</type><title>same title</title>"
             "<url>https://synthetic.invalid/first</url></appmsg></msg>")
    payload = quote(body=escape(share), native_type=49)
    add_message(cache, talker, 1, content=payload, message_type=49)
    event = first_event(reader)
    assert event.reference["url"] == "https://synthetic.invalid/first"
    update(cache, talker, message_content=payload.replace("/first", "/second"))
    assert reader.validate_native_event(event) == (False, "source_message_changed")


def test_materialized_file_path_does_not_replace_native_payload_evidence(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    payload = "<msg><appmsg><type>6</type><title>synthetic.pdf</title></appmsg></msg>"
    add_message(cache, talker, 1, content=payload, message_type=49)
    event = first_event(reader)
    signature, fingerprint = event.content_signature, event.fingerprint()
    event.content = str(tmp_path / "materialized-file.pdf")
    event.attachment_status = "materialized"
    assert reader.validate_native_event(event) == (True, "")
    assert event.content_signature == signature and event.fingerprint() == fingerprint
    update(cache, talker, message_content=payload.replace("synthetic.pdf", "different.pdf"))
    assert reader.validate_native_event(event) == (False, "source_message_changed")


def test_materialized_reference_metadata_is_not_native_signature_input(tmp_path):
    reader, talker = make_reader(tmp_path)
    add_message(reader.caches["message/message_0.db"], talker, 1,
                content=quote(native_type=3, body="&lt;img /&gt;"), message_type=49)
    event = first_event(reader)
    event.reference.update(content="UIA preview", file_path=str(tmp_path / "materialized.png"),
                           resolved=True, degraded=False, strategy="uia", sender_name="New Display")
    assert reader.validate_native_event(event) == (True, "")


def test_contact_and_sender_display_rename_do_not_invalidate_native_message(tmp_path):
    reader, talker = make_reader(tmp_path, group=True)
    add_message(reader.caches["message/message_0.db"], talker, 1)
    event = first_event(reader)
    contact = reader.caches["contact/contact.db"]
    with sqlite3.connect(contact.path) as conn:
        conn.execute("UPDATE contact SET remark='Renamed' WHERE username IN (?,?)", (talker, "synthetic-sender"))
    contact.changed = True
    assert reader.validate_native_event(event) == (True, "")
    assert event.conversation_name == "Synthetic" and event.sender_name == "Sender"


@pytest.mark.parametrize("fields", [
    {"create_time": 1001}, {"sort_seq": 101}, {"server_id": 1000}, {"local_type": 3},
    {"source": '<msgsource><atuserlist>synthetic-owner</atuserlist></msgsource>'},
    {"packed_info_data": b"synthetic changed native metadata"},
])
def test_native_time_type_and_source_metadata_changes_are_rejected(tmp_path, fields):
    reader, talker = make_reader(tmp_path, group=True)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1)
    event = first_event(reader)
    update(cache, talker, **fields)
    assert reader.validate_native_event(event) == (False, "source_message_changed")


def test_native_sender_mapping_change_is_rejected_while_display_rename_is_not(tmp_path):
    reader, talker = make_reader(tmp_path, group=True)
    add_message(reader.caches["message/message_0.db"], talker, 1)
    event = first_event(reader)
    resource = reader.caches["message/message_resource.db"]
    with sqlite3.connect(resource.path) as conn:
        conn.execute("UPDATE SenderName2Id SET user_name='other-native-sender' WHERE rowid=3")
    resource.changed = True
    assert reader.validate_native_event(event) == (False, "source_message_changed")


def test_message_becoming_outgoing_cannot_validate_for_reply(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1)
    event = first_event(reader)
    update(cache, talker, real_sender_id=2)
    assert reader.validate_native_event(event) == (False, "source_message_filtered")


def test_legacy_event_without_signature_compares_native_reference_fields(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content=quote(), message_type=49)
    event = first_event(reader)
    event.content_signature = ""
    assert reader.validate_native_event(event) == (True, "")
    event.reference.update(resolved=False, degraded=True, strategy="synthetic-ui")
    assert reader.validate_native_event(event) == (True, "")
    update(cache, talker, message_content=quote(native_id="changed"))
    assert reader.validate_native_event(event) == (False, "source_message_changed")


def test_native_signature_and_identity_survive_event_serialization(tmp_path):
    from channel.wechat_desktop.models import WechatDesktopEvent
    reader, talker = make_reader(tmp_path)
    add_message(reader.caches["message/message_0.db"], talker, 1, content=quote(), message_type=49)
    event = first_event(reader)
    document = event.to_dict()
    assert len(document["content_signature"]) == 64 and "same question" not in document["content_signature"]
    restored = WechatDesktopEvent(**document)
    assert reader.validate_native_event(restored) == (True, "")
    assert restored.fingerprint() == event.fingerprint()


def test_equivalent_compression_encoding_keeps_decoded_native_signature(tmp_path):
    import zstandard
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    body = quote().encode("utf-8")
    add_message(cache, talker, 1, content=zstandard.ZstdCompressor(level=1).compress(body), message_type=49)
    event = first_event(reader)
    update(cache, talker, message_content=zstandard.ZstdCompressor(level=9).compress(body))
    assert reader.validate_native_event(event) == (True, "")
