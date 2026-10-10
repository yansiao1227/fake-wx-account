"""合成 DB/UIA 序列验证附件操作；没有真实微信控件、聊天或网络访问。"""

import sqlite3
from dataclasses import replace

import pytest

from channel.wechat_desktop.hybrid import WechatDatabaseBackend
from channel.wechat_desktop.models import ConversationInfo, UiaChatMessage, UiaReferencedMessage
from channel.wechat_desktop.storage.store import WechatDesktopStore
from channel.wechat_desktop.uia.gateway import WechatUiaGateway
from .test_db_backend import Client
from .test_db_reader import add_message, make_reader, table


class AttachmentClient(Client):
    def __init__(self, path):
        super().__init__()
        self.path = str(path)
        self.hwnd = 123
        self.bottom = True
        self.messages = []
        self.downloads = []
        self.focus_count = 0

    def get_owner_window_handle(self):
        return self.hwnd

    def ensure_foreground_window(self):
        self.focus_count += 1
        return True

    def focus_window(self):
        self.focus_count += 1

    def locate_conversation(self, title, runtime_id="", row_index=-1):
        return title == "Synthetic" and runtime_id == "row-1"

    def get_chat_scroll_position_passive(self):
        return self.bottom

    def get_send_bubble_snapshot(self, title, limit=20):
        assert title == "Synthetic"
        return self.messages[-limit:]

    def get_chat_history(self, *args, **kwargs):
        pytest.fail("附件唯一映射不允许使用旧接收/OCR历史路径")

    def fetch_message_file(self, message, *, allow_filename_cache=True, validate_target=None):
        assert allow_filename_cache is False
        if validate_target is not None:
            validate_target()
        self.downloads.append(("file", message.runtime_id))
        return self.path

    def fetch_referenced_message_image(self, message, *, strict=False, validate_target=None):
        assert strict is True
        if validate_target is not None:
            validate_target()
        self.downloads.append(("reference_image", message.runtime_id))
        return self.path

    def resolve_message_reference(self, message, *, allow_filename_cache=True, validate_target=None):
        assert allow_filename_cache is False
        if validate_target is not None:
            validate_target()
        self.downloads.append(("reference", message.runtime_id))
        if message.reference.message_type == "share_card":
            return replace(message, reference=replace(message.reference, resolved=True, degraded=False,
                                                     browser_content="synthetic page", browser_status="success"))
        return replace(message, reference=replace(message.reference, file_path=self.path,
                                                 resolved=True, degraded=False))


@pytest.fixture
def bridge(tmp_path):
    reader, talker = make_reader(tmp_path)
    store = WechatDesktopStore(str(tmp_path / "ledger.sqlite3"))
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(b"synthetic attachment")
    client = AttachmentClient(path)
    gateway = WechatUiaGateway({}, client=client)
    instance = WechatDatabaseBackend({}, db_reader=reader, store=store, uia_gateway=gateway)
    instance.observe_events()
    yield instance, reader, store, client, gateway, talker
    instance.close()
    store._get_connection().close()


def observe_target(bridge, body, *, message_type=49, reference=None):
    instance, reader, store, client, gateway, talker = bridge
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="synthetic anchor", created=1000, sort_seq=1)
    add_message(cache, talker, 2, content=body, message_type=message_type, created=1001, sort_seq=2)
    add_message(cache, talker, 3, content="synthetic tail", created=1002, sort_seq=3)
    status, events = instance.observe_events()
    store.receive_source_batch(status["source_batch"])
    instance.acknowledge_events([status["source_batch"].batch_id])
    event = next(event for event in events if event.source_local_id == 2)
    client.messages = [
        UiaChatMessage("Synthetic", "synthetic anchor", direction="incoming", runtime_id="bubble-1"),
        UiaChatMessage("Synthetic", "文件\nsynthetic.pdf\n1K" if event.content_type == "file" else event.content,
                       event.content_type, "incoming", runtime_id="bubble-2", reference=reference),
        UiaChatMessage("Synthetic", "synthetic tail", direction="incoming", runtime_id="bubble-3"),
    ]
    return event


def reference_xml(kind, preview):
    return ("<msg><appmsg><type>57</type><title>synthetic question</title><refermsg>"
            f"<type>{kind}</type><displayname>Synthetic</displayname><svrid>synthetic-reference-id</svrid>"
            f"<content>{preview}</content></refermsg></appmsg></msg>")


def test_standalone_file_is_cached_without_replacing_native_identity(bridge):
    event = observe_target(bridge, "<msg><appmsg><type>6</type><title>synthetic.pdf</title></appmsg></msg>")
    identity = (event.event_id, event.account_id, event.source_stream_id, event.source_message_id,
                event.source_local_id, event.native_timestamp, event.conversation_id, event.fingerprint())
    event, count = bridge[0].materialize_event(event)
    assert event.attachment_status == "materialized" and event.content == bridge[3].path
    assert count == 1 and bridge[3].downloads == [("file", "bubble-2")]
    assert identity == (event.event_id, event.account_id, event.source_stream_id, event.source_message_id,
                        event.source_local_id, event.native_timestamp, event.conversation_id, event.fingerprint())


def test_attachment_already_foreground_does_not_require_activation(bridge):
    event = observe_target(bridge, "<msg><appmsg><type>6</type><title>synthetic.pdf</title></appmsg></msg>")
    bridge[3].ensure_foreground_window = lambda: False

    result, count = bridge[0].materialize_event(event)

    assert result.attachment_status == "materialized"
    assert count == 1 and bridge[3].downloads == [("file", "bubble-2")]


def test_attachment_foreground_activation_failure_does_not_download(bridge):
    event = observe_target(bridge, "<msg><appmsg><type>6</type><title>synthetic.pdf</title></appmsg></msg>")

    def failed_activation():
        raise RuntimeError("synthetic activation failure")

    bridge[3].ensure_foreground_window = failed_activation
    result, count = bridge[0].materialize_event(event)

    assert result.attachment_status == "attachment_identity_unavailable"
    assert count == 0 and bridge[3].downloads == []


@pytest.mark.parametrize("kind,raw,preview,expected_type,operation", [
    (3, "&lt;img/&gt;", "图片", "image", "reference_image"),
    (49, "&lt;msg&gt;&lt;appmsg&gt;&lt;type&gt;6&lt;/type&gt;&lt;title&gt;synthetic.pdf&lt;/title&gt;"
         "&lt;/appmsg&gt;&lt;/msg&gt;", "synthetic.pdf", "file", "reference"),
    (49, "&lt;msg&gt;&lt;appmsg&gt;&lt;type&gt;5&lt;/type&gt;&lt;title&gt;synthetic article&lt;/title&gt;"
         "&lt;/appmsg&gt;&lt;/msg&gt;",
     "synthetic article", "share_card", "reference"),
])
def test_media_reference_uses_verified_current_bubble(bridge, kind, raw, preview, expected_type, operation):
    reference = UiaReferencedMessage("Synthetic", preview, message_type="text", degraded=True)
    event = observe_target(bridge, reference_xml(kind, raw), reference=reference)
    assert event.reference["content_type"] == expected_type
    native_reference_id = event.reference["source_message_id"]
    event, count = bridge[0].materialize_event(event)
    assert count == 1 and event.reference["resolved"] and not event.reference["degraded"]
    assert event.reference["source_message_id"] == native_reference_id
    assert bridge[3].downloads == [(operation, "bubble-2")]


@pytest.mark.parametrize("bottom", [None, False])
def test_unverified_bottom_refuses_download(bridge, bottom):
    event = observe_target(bridge, "<msg><appmsg><type>6</type><title>synthetic.pdf</title></appmsg></msg>")
    bridge[3].bottom = bottom
    event, count = bridge[0].materialize_event(event)
    assert count == 0 and event.attachment_status == "attachment_identity_unavailable"
    assert bridge[3].downloads == []


def test_repeated_identical_sequence_is_not_guessed(bridge):
    instance, reader, store, client, gateway, talker = bridge
    event = observe_target(bridge, "<msg><appmsg><type>6</type><title>synthetic.pdf</title></appmsg></msg>")
    cache = reader.caches["message/message_0.db"]
    for local_id, content, kind in [(4, "synthetic anchor", 1), (5, "<msg><appmsg><type>6</type>"
                                      "<title>synthetic.pdf</title></appmsg></msg>", 49),
                                  (6, "synthetic tail", 1)]:
        add_message(cache, talker, local_id, content=content, message_type=kind,
                    created=1000 + local_id, sort_seq=local_id)
    event, count = instance.materialize_event(event)
    assert count == 0 and client.downloads == []


def test_target_outside_visible_suffix_refuses_download(bridge):
    event = observe_target(bridge, "<msg><appmsg><type>6</type><title>synthetic.pdf</title></appmsg></msg>")
    bridge[3].messages = bridge[3].messages[-1:]
    assert bridge[0].materialize_event(event)[1] == 0
    assert bridge[3].downloads == []


def test_same_file_titles_are_rejected_even_with_distinct_context(bridge):
    instance, reader, store, client, gateway, talker = bridge
    body = "<msg><appmsg><type>6</type><title>synthetic.pdf</title></appmsg></msg>"
    event = observe_target(bridge, body)
    add_message(reader.caches["message/message_0.db"], talker, 4, content=body,
                message_type=49, created=1003, sort_seq=4)
    client.messages.append(replace(client.messages[1], runtime_id="bubble-4"))
    assert instance.materialize_event(event)[1] == 0
    assert client.downloads == []


@pytest.mark.parametrize("change", ["window", "account", "row", "messages", "database"])
def test_target_change_after_alignment_is_rejected_before_download(bridge, monkeypatch, change):
    instance, reader, store, client, gateway, talker = bridge
    event = observe_target(bridge, "<msg><appmsg><type>6</type><title>synthetic.pdf</title></appmsg></msg>")
    original = gateway.materialize_event

    def change_before_materialize(*args, **kwargs):
        if change == "window":
            client.hwnd += 1
        elif change == "account":
            client.wxid = "synthetic-other-account"
        elif change == "row":
            client.rows = [replace(client.rows[0], runtime_id="replacement-row")]
        elif change == "messages":
            client.messages[0] = replace(client.messages[0], content="changed anchor")
        else:
            add_message(reader.caches["message/message_0.db"], talker, 4, content="changed database tail",
                        created=1004, sort_seq=4)
        return original(*args, **kwargs)

    monkeypatch.setattr(gateway, "materialize_event", change_before_materialize)
    assert instance.materialize_event(event)[1] == 0
    assert client.downloads == []


def test_duplicate_database_names_refuse_before_foreground(bridge):
    instance, reader, store, client, gateway, talker = bridge
    event = observe_target(bridge, "<msg><appmsg><type>6</type><title>synthetic.pdf</title></appmsg></msg>")
    cache = reader.caches["contact/contact.db"]
    with sqlite3.connect(cache.path) as conn:
        conn.execute("INSERT INTO contact VALUES ('synthetic-duplicate','Synthetic','Synthetic','')")
    cache.changed = True
    assert instance.materialize_event(event)[1] == 0
    assert client.focus_count == 0 and client.downloads == []


@pytest.mark.parametrize("kind", [1, 3, 49])
def test_plain_text_standalone_image_and_share_do_not_initialize_uia(tmp_path, kind, monkeypatch):
    reader, talker = make_reader(tmp_path)
    store = WechatDesktopStore(str(tmp_path / "ledger.sqlite3"))
    instance = WechatDatabaseBackend({}, db_reader=reader, store=store)
    monkeypatch.setattr(instance, "_actions", lambda: pytest.fail("无待下载附件不得初始化UIA"))
    instance.observe_events()
    body = "<msg><appmsg><type>5</type><title>synthetic article</title></appmsg></msg>" if kind == 49 else "synthetic"
    add_message(reader.caches["message/message_0.db"], talker, 1, content=body, message_type=kind)
    event = instance.observe_events()[1][0]
    assert instance.materialize_event(event)[1] == 0
    assert not instance.uia_initialized
    instance.close()
    store._get_connection().close()


def test_unknown_app_reference_uses_verified_uia_original_as_fallback(bridge):
    instance, _, _, client, _, _ = bridge
    preview = "synthetic attachment preview"
    event = observe_target(bridge, reference_xml(49, preview), reference=UiaReferencedMessage(
        "Synthetic", preview, message_type="text", resolved=False, degraded=True))
    native_reference_id = event.reference["source_message_id"]

    def resolve_original(message, *, allow_filename_cache=True, validate_target=None):
        assert allow_filename_cache is False
        if validate_target is not None:
            validate_target()
        client.downloads.append(("reference", message.runtime_id))
        return replace(message, reference=replace(
            message.reference, message_type="file", file_path=client.path,
            resolved=True, degraded=False, strategy="wechat_locate_original"))

    client.resolve_message_reference = resolve_original
    result, count = instance.materialize_event(event)

    assert count == 1 and result.reference["content_type"] == "file"
    assert result.reference["file_path"] == client.path and result.reference["resolved"]
    assert result.reference["source_message_id"] == native_reference_id
    assert client.downloads == [("reference", "bubble-2")]
    assert instance.validate_reply_target(result).valid
