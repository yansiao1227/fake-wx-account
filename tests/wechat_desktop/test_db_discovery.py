"""生产目录枚举与 Reader 的合成集成回归，不访问真实微信账号。"""

import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from channel.wechat_desktop.db.cache import SnapshotStatus
from channel.wechat_desktop.db.discovery import AccountBinding, DatabaseCatalog
from channel.wechat_desktop.db.reader import WechatDatabaseReader


def _binding(account_dir):
    return AccountBinding("synthetic-account", account_dir, account_dir / "db_storage",
                          42, "synthetic-version", "synthetic-owner")


def test_catalog_includes_sender_resource_and_excludes_unrelated_databases(tmp_path):
    binding = _binding(tmp_path)
    supported = ["contact/contact.db", "session/session.db", "message/message_0.db",
                 "message/message_12.db", "message/message_resource.db"]
    excluded = ["message/message_foo.db", "media/media.db", "sns/sns.db",
                "migrate/message/message_0.db", "migrate/message/message_resource.db"]
    for relative in supported + excluded:
        path = binding.db_storage / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    catalog = DatabaseCatalog({})

    assert {path.relative_to(binding.db_storage).as_posix()
            for path in catalog.list_databases(binding)} == set(supported)


class _PlainSnapshotCache:
    def __init__(self, source, key, destination, *, retry_attempts):
        self.source = Path(source)
        self.status = SnapshotStatus(healthy=True, stale=False,
                                     generation="synthetic-generation", changed=True)

    def refresh(self):
        status = self.status
        self.status = replace(self.status, changed=False)
        return status

    @contextmanager
    def read(self):
        connection = sqlite3.connect(self.source.as_uri() + "?mode=ro", uri=True)
        try:
            yield connection
        finally:
            connection.close()


@pytest.mark.parametrize("group", [False, True])
def test_catalog_reader_uses_resource_sender_map_without_shard_name2id(tmp_path, monkeypatch, group):
    binding = _binding(tmp_path / "synthetic-account")
    contact_db = binding.db_storage / "contact/contact.db"
    resource_db = binding.db_storage / "message/message_resource.db"
    message_db = binding.db_storage / "message/message_0.db"
    for path in (contact_db, resource_db, message_db):
        path.parent.mkdir(parents=True, exist_ok=True)
    talker = "synthetic@chatroom" if group else "synthetic-user"
    incoming_sender = "synthetic-member" if group else talker
    with sqlite3.connect(contact_db) as connection:
        connection.execute("CREATE TABLE contact(username TEXT,nick_name TEXT)")
        connection.executemany("INSERT INTO contact VALUES (?,?)", [
            (talker, "Conversation"), (incoming_sender, "Sender"),
            (binding.wxid, "Owner")])
    with sqlite3.connect(resource_db) as connection:
        connection.execute("CREATE TABLE SenderName2Id(user_name TEXT)")
        connection.executemany("INSERT INTO SenderName2Id(rowid,user_name) VALUES (?,?)", [
            (2, binding.wxid), (3, incoming_sender)])
    table = "Msg_" + hashlib.md5(talker.encode()).hexdigest()
    with sqlite3.connect(message_db) as connection:
        # 不创建 Name2Id 或 is_sender，方向只能通过 resource 中的发送者映射识别。
        connection.execute(f'CREATE TABLE "{table}" (local_id INTEGER PRIMARY KEY,local_type INTEGER,'
                           'real_sender_id INTEGER,create_time INTEGER,message_content TEXT)')
        connection.executemany(f'INSERT INTO "{table}" VALUES (?,?,?,?,?)', [
            (1, 1, 3, 1000, "synthetic incoming"), (2, 1, 2, 1001, "synthetic outgoing")])

    requested_keys = []

    class SyntheticKeyProvider:
        def get_key(self, relative, source):
            requested_keys.append(relative)
            return b"synthetic-test-key"

    config = {"db_cache_dir": str(tmp_path / "cache")}
    catalog = DatabaseCatalog(config)
    # 仅替换登录认证；数据库枚举和 Reader 缓存构建仍使用生产路径。
    monkeypatch.setattr(catalog, "resolve", lambda: binding)
    reader = WechatDatabaseReader(config, catalog=catalog, key_provider=SyntheticKeyProvider(),
                                  cache_factory=_PlainSnapshotCache)
    try:
        reader.refresh()
        highwaters = reader.get_highwaters()
        checkpoints = {stream_id: {"cursor": 0, "generation": high["generation"]}
                       for stream_id, high in highwaters.items()}
        batch = reader.poll_batch(checkpoints, {})

        assert batch.records[0].event is not None
        assert batch.records[0].event.sender_id == incoming_sender
        assert batch.records[0].event.sender_name == "Sender"
        assert batch.records[0].event.direction == "incoming"
        assert batch.records[0].event.is_group is group
        assert batch.records[1].event is None
        assert batch.records[1].filter_reason == "outgoing_message"
        assert "message/message_resource.db" in requested_keys
        history = reader.read_history(talker)
        assert [(message.sender_name, message.direction) for message in history.messages] == [
            ("Sender", "incoming"), ("Owner", "outgoing")]
    finally:
        reader.close()
