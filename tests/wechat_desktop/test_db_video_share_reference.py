"""视频分享的原生 URL 和精确原消息证据，不访问微信或网络。"""

import sqlite3

import pytest

from channel.wechat_desktop.db.errors import DatabaseReadError
from .test_db_reader import add_message, checkpoints, make_reader, table
from .test_db_reference_lookup import add_question, question_event, update


VIDEO_URL = "https://example.invalid/video?type=video&source=synthetic"


def video_share(url=VIDEO_URL):
    return ("<msg><appmsg><type>4</type><title>synthetic video</title>"
            f"<url><![CDATA[{url}]]></url><des>synthetic description</des>"
            "<sourcedisplayname>synthetic platform</sourcedisplayname></appmsg></msg>")


@pytest.mark.parametrize("native_type", [49, (4 << 32) | 49])
def test_standalone_video_share_preserves_url_without_claiming_media_read(tmp_path, native_type):
    reader, talker = make_reader(tmp_path)
    add_message(reader.caches["message/message_0.db"], talker, 1,
                message_type=native_type, content=video_share())
    reader.refresh()
    event = reader.poll_batch(checkpoints(reader), {}).records[0].event

    assert event.content_type == "share_card" and event.content == "synthetic video"
    assert event.share_card == {
        "title": "synthetic video", "url": VIDEO_URL,
        "description": "synthetic description", "platform": "synthetic platform",
    }
    assert not event.attachment_status
    assert reader.validate_native_event(event) == (True, "")


@pytest.mark.parametrize("origin_shard", [0, 1])
def test_video_share_url_is_recovered_from_exact_original(tmp_path, origin_shard):
    reader, talker = make_reader(tmp_path, shards=2)
    original = reader.caches[f"message/message_{origin_shard}.db"]
    add_message(original, talker, 1, content=video_share(), message_type=49, created=10, server_id=55)
    add_question(reader.caches["message/message_0.db"], talker, native_type=49, sender=talker)
    event = question_event(reader)

    assert event.reference["content_type"] == "share_card"
    assert event.reference["url"] == VIDEO_URL
    assert event.reference["source_stream_id"].startswith(f"message/message_{origin_shard}.db:")
    assert event.reference["source_account_id"] == reader.account_id
    assert event.reference["source_local_id"] == 1
    assert event.reference["resolved"] is False and event.reference["degraded"] is True
    assert not event.reference.get("fetched_content") and not event.reference.get("browser_content")
    assert reader.validate_native_event(event) == (True, "")

    update(original, talker, message_content=video_share("https://example.invalid/replaced"))
    reader.refresh()
    assert reader.validate_native_event(event) == (False, "reference_source_changed")


def test_duplicate_native_video_server_id_does_not_supply_an_arbitrary_url(tmp_path):
    reader, talker = make_reader(tmp_path, shards=2)
    for index in range(2):
        add_message(reader.caches[f"message/message_{index}.db"], talker, 1,
                    content=video_share(f"https://example.invalid/video/{index}"),
                    message_type=49, created=10, server_id=55)
    add_question(reader.caches["message/message_0.db"], talker, native_type=49)
    event = question_event(reader)

    assert not event.reference.get("url")
    assert "source_native_message_id" not in event.reference


def test_video_share_lookup_does_not_cross_conversations(tmp_path):
    reader, talker = make_reader(tmp_path)
    other = "other-synthetic-user"
    contacts = reader.caches["contact/contact.db"]
    with sqlite3.connect(contacts.path) as connection:
        connection.execute("INSERT INTO contact VALUES (?,?,?,?)", (other, "Synthetic", "", ""))
    contacts.changed = True
    cache = reader.caches["message/message_0.db"]
    with sqlite3.connect(cache.path) as connection:
        schema = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (table(talker),)).fetchone()[0]
        connection.execute(schema.replace(table(talker), table(other)))
    add_message(cache, other, 1, content=video_share(), message_type=49, created=10, server_id=55)
    add_question(cache, talker, native_type=49)

    event = question_event(reader)

    assert not event.reference.get("url") and VIDEO_URL not in str(event.history)
    assert "source_native_message_id" not in event.reference


def test_video_share_enrichment_rejects_other_account_event(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content=video_share(), message_type=49, created=10, server_id=55)
    add_question(cache, talker, native_type=49)
    event = question_event(reader)
    event.account_id = "other-synthetic-account"

    with pytest.raises(DatabaseReadError):
        reader.enrich_reference(event)
