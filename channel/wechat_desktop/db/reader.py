"""微信 4.x 只读消息查询；字段识别参考固定版本 wechatauto（见本目录说明）。

来源身份由账号、数据库分片、消息表和 local_id 组成；数据库观察不访问 UIA。
只读取缓存的已验证快照，不修改微信库或实时游标。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.db.types import SourceBatch, SourceCheckpoint, SourceRecord
from channel.wechat_desktop.models import (
    WechatDesktopEvent, WechatHistoryMessage, WechatHistoryReadResult,
)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip("\x00")
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        # WCDB may prepend a small container header to a standard zstd frame.
        offset = data.find(b"\x28\xb5\x2f\xfd", 0, 16)
        if offset >= 0:
            try:
                import zstandard
                data = zstandard.ZstdDecompressor().decompress(data[offset:], max_output_size=2_000_000)
            except ImportError as exc:
                raise DatabaseReadError("dependency_missing", "数据库文本读取需要 zstandard") from exc
            except Exception as exc:
                raise DatabaseReadError("content_decode_failed", "数据库压缩正文解码失败") from exc
        try:
            return data.decode("utf-8").strip("\x00")
        except UnicodeDecodeError:
            return ""
    return str(value)


def _integer(value, default=0) -> int:
    try:
        return int(value or default)
    except (ValueError, TypeError, OverflowError):
        return default


def _tables(conn) -> dict[str, str]:
    return {str(row[0]).lower(): str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}


def _columns(conn, table) -> dict[str, str]:
    return {str(row[1]).lower(): str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quote(table)})")}


def _column(columns, *candidates):
    return next((columns[name.lower()] for name in candidates if name.lower() in columns), None)


@dataclass(frozen=True)
class MessageStream:
    stream_id: str
    database: str
    table: str
    talker: str
    columns: dict
    generation: str


class WechatDatabaseReader:
    """按实际 SQLite schema 识别联系人、发送者映射和分片消息流。"""

    def __init__(self, config, *, catalog=None, key_provider=None, cache_factory=None,
                 binding=None, caches=None):
        self.config = config
        self._lock = threading.RLock()
        self.catalog = catalog
        if binding is None:
            if self.catalog is None:
                from channel.wechat_desktop.db.discovery import DatabaseCatalog
                self.catalog = DatabaseCatalog(config)
            binding = self.catalog.resolve()
        self.binding = binding
        self.account_id = binding.account_id
        self.owner_wxid = getattr(binding, "wxid", "") or self.account_id
        self._key_provider = None
        self._cache_factory = None
        self._cache_dir = None
        if caches is None:
            from channel.wechat_desktop.db.cache import EncryptedDatabaseCache
            from channel.wechat_desktop.db.discovery import cache_directory
            from channel.wechat_desktop.db.keys import KeyProvider
            base = cache_directory(config)
            cache_dir = base / self.account_id
            if key_provider is None:
                key_provider = KeyProvider(binding, cache_dir / "keys", timeout_seconds=config.get("db_key_scan_timeout_seconds", 30.0),
                                           candidates=getattr(self.catalog, "key_candidates", None))
            factory = cache_factory or EncryptedDatabaseCache
            self._key_provider, self._cache_factory, self._cache_dir = key_provider, factory, cache_dir / "snapshots"
            caches = {}
            for source in self.catalog.list_databases(binding):
                source = Path(source)
                if not self._supported_database(source.name):
                    continue
                relative = source.relative_to(binding.db_storage).as_posix()
                key = key_provider.get_key(relative, source)
                caches[relative] = factory(source, key, self._cache_dir / relative,
                                          retry_attempts=config.get("db_snapshot_retry_attempts", 3))
        self.caches = dict(caches)
        self._statuses = {}
        self._contacts = {}
        self._conversation_lookup = {}
        self._sender_maps = {}
        self._streams = {}
        self._session_unread = {}
        self._startup_unread_ids = set()
        self._initialized = False
        self._scan_offset = 0
        self._backlog = {}
        self._index_revision = 0
        self._highwaters = None
        self._idle_poll_signature = None
        self._error_code = ""
        self._last_success_at = 0.0

    @staticmethod
    def _supported_database(name):
        lowered = name.lower()
        return lowered in {"contact.db", "session.db", "message_resource.db"} or (
            lowered.startswith("message_") and lowered.endswith(".db") and
            lowered.removeprefix("message_").removesuffix(".db").isdigit()
        )

    def conversation_id(self, talker: str) -> str:
        digest = hashlib.sha256((self.account_id + "\0" + talker).encode()).hexdigest()
        return "db-session:" + digest

    def status(self):
        stale = bool(self._error_code) or any(getattr(item, "stale", False) for item in self._statuses.values())
        return {
            "db_read_healthy": self._initialized and not stale,
            "db_read_stale": stale, "db_read_last_success_at": self._last_success_at,
            "db_read_account_id": self.account_id, "db_read_backlog": dict(self._backlog),
            "db_read_error_code": self._error_code,
            "account_binding": {"account_id": self.account_id, "pid": self.binding.pid,
                                "version": self.binding.version, "verification": "database_key_hmac"},
            "db_read_conversations": len(self._contacts),
        }

    def refresh(self):
        with self._lock:
            try:
                changed = not self._initialized
                if self.catalog is not None:
                    binding = self.catalog.resolve()
                    if binding.account_id != self.account_id or binding.pid != self.binding.pid:
                        raise DatabaseReadError("account_binding_changed", "微信登录账号或进程已变化，暂停读取")
                if self.catalog is not None and self._cache_factory is not None:
                    for source in self.catalog.list_databases(self.binding):
                        source = Path(source)
                        if not self._supported_database(source.name):
                            continue
                        relative = source.relative_to(self.binding.db_storage).as_posix()
                        if relative not in self.caches:
                            key = self._key_provider.get_key(relative, source)
                            self.caches[relative] = self._cache_factory(
                                source, key, self._cache_dir / relative,
                                retry_attempts=self.config.get("db_snapshot_retry_attempts", 3))
                            changed = True
                for relative, cache in self.caches.items():
                    status = cache.refresh()
                    if getattr(status, "error_code", "") == "page_hmac_failed" and self._key_provider is not None:
                        key = self._key_provider.get_key(relative, cache.source)
                        if key != cache.key:
                            cache.key = key
                            status = cache.refresh()
                    self._statuses[relative] = status
                    if getattr(status, "stale", False) or not getattr(status, "healthy", True):
                        raise DatabaseReadError(getattr(status, "error_code", "snapshot_stale") or "snapshot_stale",
                                                "数据库快照刷新失败，暂停消息交付")
                    changed |= bool(getattr(status, "changed", True))
                if changed:
                    self._load_indexes()
                self._initialized = True
                self._error_code = ""
                self._last_success_at = time.time()
            except Exception as exc:
                self._error_code = getattr(exc, "code", "database_read_failed")
                raise

    def _load_indexes(self):
        contacts, names, sender_maps, session_unread = {}, set(), {}, {}
        for relative, cache in self.caches.items():
            with cache.read() as conn:
                tables = _tables(conn)
                for lowered, table in tables.items():
                    columns = _columns(conn, table)
                    if lowered == "contact":
                        username = _column(columns, "username", "user_name")
                        if username is None:
                            raise DatabaseReadError("unsupported_contact_schema", "联系人表缺少 username")
                        expressions = [_quote(username)]
                        for field in ("nick_name", "remark", "alias"):
                            column = _column(columns, field)
                            expressions.append(_quote(column) if column else "''")
                        for row in conn.execute(f"SELECT {','.join(expressions)} FROM {_quote(table)}"):
                            user = _text(row[0])
                            if not user:
                                continue
                            nick, remark, alias = map(_text, row[1:])
                            contacts[user] = {"conversation_id": self.conversation_id(user), "username": user,
                                              "nick_name": nick, "remark": remark, "alias": alias,
                                              "display_name": remark or nick or user,
                                              "is_group": user.endswith("@chatroom")}
                            names.add(user)
                    elif lowered == "sessiontable":
                        user_col = _column(columns, "username", "user_name")
                        if user_col:
                            names.update(_text(row[0]) for row in conn.execute(
                                f"SELECT {_quote(user_col)} FROM {_quote(table)}") if row[0])
                            count_col = _column(columns, "unread_count")
                            first_col = _column(columns, "unread_first_msg_srv_id")
                            if count_col and first_col:
                                for row in conn.execute(f"SELECT {_quote(user_col)},{_quote(count_col)},"
                                                        f"{_quote(first_col)} FROM {_quote(table)} WHERE {_quote(count_col)}>0"):
                                    session_unread[_text(row[0])] = (_integer(row[1]), _integer(row[2]))
                    elif lowered in {"name2id", "sendername2id"}:
                        user_col = _column(columns, "user_name", "username", "name")
                        id_col = _column(columns, "id", "user_id")
                        if user_col:
                            id_expr = _quote(id_col) if id_col else "rowid"
                            mapping = {int(row[0]): _text(row[1]) for row in conn.execute(
                                f"SELECT {id_expr},{_quote(user_col)} FROM {_quote(table)}") if row[1]}
                            names.update(mapping.values())
                            # Name2Id is local to its message shard; SenderName2Id is the global resource map.
                            sender_maps.setdefault(relative, {}).update(mapping)
                            if lowered == "sendername2id":
                                sender_maps.setdefault("*", {}).update(mapping)
        for user in names:
            contacts.setdefault(user, {"conversation_id": self.conversation_id(user), "username": user,
                                       "nick_name": "", "remark": "", "alias": "",
                                       "display_name": user, "is_group": user.endswith("@chatroom")})
        md5_index = {hashlib.md5(user.encode()).hexdigest(): user for user in names}
        streams = {}
        for relative, cache in self.caches.items():
            if not Path(relative).name.lower().startswith("message_"):
                continue
            with cache.read() as conn:
                for table in _tables(conn).values():
                    if not table.lower().startswith("msg_"):
                        continue
                    columns = _columns(conn, table)
                    if not _column(columns, "local_id"):
                        raise DatabaseReadError("unsupported_message_schema", "消息表缺少 local_id")
                    if not _column(columns, "local_type", "type") or not _column(columns, "create_time"):
                        raise DatabaseReadError("unsupported_message_schema", "消息表缺少类型或时间字段")
                    stream_id = relative + ":" + table
                    generation = getattr(self._statuses[relative], "generation", "")
                    streams[stream_id] = MessageStream(stream_id, relative, table,
                                                       md5_index.get(table[4:].lower(), ""), columns, generation)
        if not streams:
            raise DatabaseReadError("message_database_unavailable", "未找到可识别的微信消息表")
        self._contacts, self._sender_maps, self._streams = contacts, sender_maps, streams
        self._session_unread = session_unread
        self._conversation_lookup = {item["conversation_id"]: user for user, item in contacts.items()}
        self._index_revision += 1
        self._highwaters = None
        self._idle_poll_signature = None

    def search_contacts(self, query="", limit=20):
        self.refresh()
        with self._lock:
            needle = str(query or "").casefold()
            matches = [dict(item) for item in self._contacts.values() if not needle or any(
                needle in str(item[field]).casefold() for field in ("username", "nick_name", "remark", "alias"))]
            return sorted(matches, key=lambda item: (item["display_name"], item["username"]))[:max(1, min(50, int(limit)))]

    def get_contact_by_conversation_id(self, conversation_id):
        with self._lock:
            user = self._conversation_lookup.get(conversation_id)
            return dict(self._contacts[user]) if user else None

    def get_contact_by_username(self, username):
        with self._lock:
            contact = self._contacts.get(username)
            return dict(contact) if contact else None

    def _sender_username(self, stream, sender_num):
        # 4.1.9.30 real_sender_id 引用消息分片的 Name2Id。仅在分片没有该映射表
        # 的其他已识别 schema 中采用资源 SenderName2Id，不能混用两个 ID 命名空间。
        mapping = self._sender_maps.get(stream.database)
        if mapping is None:
            mapping = self._sender_maps.get("*", {})
        return mapping.get(sender_num, "")

    def exact_contacts(self, name):
        with self._lock:
            return [dict(item) for item in self._contacts.values() if item["display_name"] == name]

    def match_contacts(self, name):
        """发送目标完整匹配，不受面向工具的搜索分页上限影响。"""
        from channel.wechat_desktop.conversation import conversation_titles_match
        with self._lock:
            return [dict(item) for item in self._contacts.values() if item["username"] == name or
                    conversation_titles_match(item["display_name"], name)]

    def get_highwaters(self):
        with self._lock:
            if self._highwaters is None:
                result = {}
                for stream_id, stream in self._streams.items():
                    with self.caches[stream.database].read() as conn:
                        cursor = conn.execute(f"SELECT coalesce(max(local_id),0) FROM {_quote(stream.table)}").fetchone()[0]
                    result[stream_id] = {"cursor": int(cursor), "generation": stream.generation}
                self._highwaters = result
            return {stream: dict(high) for stream, high in self._highwaters.items()}

    def startup_unread_boundaries(self, highwaters):
        """仅使用确切的普通消息 server_id；pat 边界不代表普通未读消息。

        first server_id 与 sort_seq 均唯一，且所有候选入站消息数恰好匹配 session
        unread_count 时，才把固定启动快照中的这些消息标记为可回复未读。
        """
        with self._lock:
            boundaries, verified = {}, set()
            for talker, (unread_count, first_server_id) in self._session_unread.items():
                if not (0 < unread_count <= 10000 and first_server_id > 0):
                    continue
                streams = [stream for stream in self._streams.values() if stream.talker == talker and
                           _column(stream.columns, "server_id") and _column(stream.columns, "sort_seq")]
                first = []
                for stream in streams:
                    with self.caches[stream.database].read() as conn:
                        rows = conn.execute(f"SELECT local_id,sort_seq FROM {_quote(stream.table)} WHERE server_id=? "
                                            "AND local_id<=? LIMIT 2", (first_server_id, highwaters[stream.stream_id]["cursor"])).fetchall()
                        first.extend((stream, int(row[0]), _integer(row[1])) for row in rows)
                if len(first) != 1 or first[0][2] <= 0:
                    continue
                first_seq = first[0][2]
                equal_count = 0
                candidates = []
                for stream in streams:
                    with self.caches[stream.database].read() as conn:
                        equal_count += int(conn.execute(f"SELECT count(*) FROM {_quote(stream.table)} WHERE sort_seq=? "
                                                       "AND local_id<=?", (first_seq, highwaters[stream.stream_id]["cursor"])).fetchone()[0])
                        conn.row_factory = sqlite3.Row
                        rows = conn.execute(f"SELECT {self._select(stream)} FROM {_quote(stream.table)} WHERE sort_seq>=? "
                                            "AND local_id<=? ORDER BY local_id LIMIT 10001",
                                            (first_seq, highwaters[stream.stream_id]["cursor"])).fetchall()
                        candidates.extend((stream, dict(row)) for row in rows)
                    if len(candidates) > 10000:
                        break
                if equal_count != 1 or len(candidates) > 10000:
                    continue
                incoming = [(stream, row) for stream, row in candidates if self._parse(stream, row).event is not None]
                if len(incoming) != unread_count or not any(
                    stream.stream_id == first[0][0].stream_id and int(row["local_id"]) == first[0][1]
                    for stream, row in incoming):
                    continue
                for stream, row in incoming:
                    local_id = int(row["local_id"])
                    verified.add((stream.stream_id, local_id))
                    boundaries[stream.stream_id] = min(boundaries.get(stream.stream_id, local_id), local_id)
            self._startup_unread_ids = verified
            return boundaries

    @staticmethod
    def _message_kind(row):
        return {1: "text", 3: "image", 34: "voice", 43: "video", 47: "sticker", 48: "location",
                49: "app_message", 50: "voip", 10000: "system", 11000: "sticker"}.get(
                    _integer(row["local_type"]) & 0xFFFFFFFF, "unsupported")

    @staticmethod
    def _select(stream):
        names = {
            "local_id": ("local_id",), "local_type": ("local_type", "type"),
            "real_sender_id": ("real_sender_id", "sender_id"), "create_time": ("create_time",),
            "message_content": ("message_content", "content"), "compress_content": ("compress_content",),
            "source": ("source", "msg_source"), "packed_info_data": ("packed_info_data", "packed_info"),
            "server_id": ("server_id",), "sort_seq": ("sort_seq",), "is_sender": ("is_sender", "issender"),
        }
        return ",".join(((_quote(column) if column else "NULL") + " AS " + _quote(alias))
                        for alias, candidates in names.items()
                        for column in [_column(stream.columns, *candidates)])

    def _rows(self, stream, *, after=None, limit=200, descending=False):
        predicate = " WHERE local_id > ?" if after is not None else ""
        order = "local_id ASC" if not descending else (
            "sort_seq DESC, local_id DESC" if _column(stream.columns, "sort_seq") else "local_id DESC")
        parameters = (after, limit) if after is not None else (limit,)
        with self.caches[stream.database].read() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(
                f"SELECT {self._select(stream)} FROM {_quote(stream.table)}{predicate} ORDER BY {order} LIMIT ?",
                parameters)]

    def _parse(self, stream, row, phase="live", *, include_filtered=False):
        local_id = int(row["local_id"])
        source_id = hashlib.sha256(f"{self.account_id}\0{stream.stream_id}\0{local_id}".encode()).hexdigest()
        if not stream.talker:
            return SourceRecord(stream.stream_id, local_id, source_id, filter_reason="conversation_unresolved", receipt_phase=phase)
        kind = self._message_kind(row)
        if kind == "system" and not include_filtered:
            return SourceRecord(stream.stream_id, local_id, source_id, filter_reason="system_message", receipt_phase=phase)
        sender_num = _integer(row["real_sender_id"], -1)
        sender = self._sender_username(stream, sender_num)
        is_group = stream.talker.endswith("@chatroom")
        content = _text(row["message_content"])
        if not content:
            content = _text(row["compress_content"])
        # Older message snapshots prefix a group text body with "wxid:\n".
        if is_group and ":\n" in content and not content.lstrip().startswith("<"):
            prefix, body = content.split(":\n", 1)
            if prefix in self._contacts or prefix.startswith("wxid_"):
                sender, content = prefix, body
        if kind == "system":
            direction = "system"
        elif row["is_sender"] is not None:
            direction = "outgoing" if _integer(row["is_sender"]) else "incoming"
            if direction == "incoming" and not sender and not is_group:
                sender = stream.talker
        elif sender and sender == self.owner_wxid:
            direction = "outgoing"
        elif sender:
            direction = "incoming"
        else:
            return SourceRecord(stream.stream_id, local_id, source_id, filter_reason="sender_direction_unresolved", receipt_phase=phase)
        if direction == "outgoing" and not include_filtered:
            return SourceRecord(stream.stream_id, local_id, source_id, filter_reason="outgoing_message", receipt_phase=phase)
        reference = {}
        root = None
        if content.lstrip().startswith("<"):
            try:
                root = ElementTree.fromstring(content)
            except ElementTree.ParseError:
                root = None
        if kind == "app_message" and root is not None:
            app = root.find(".//appmsg")
            if app is None and root.tag == "appmsg":
                app = root
            if app is not None:
                subtype = _integer(app.findtext("type"))
                content = app.findtext("title") or content
                kind = {57: "text", 5: "share_card", 6: "file"}.get(subtype, "app_message")
                quoted = app.find("refermsg")
                if quoted is not None:
                    reference = {"sender_name": quoted.findtext("displayname") or "",
                                 "sender_id": quoted.findtext("chatusr") or quoted.findtext("fromusr") or "",
                                 "content": quoted.findtext("content") or "",
                                 "content_type": "text" if _integer(quoted.findtext("type")) == 1 else "unsupported",
                                 "source_message_id": quoted.findtext("svrid") or "", "depth": 1,
                                 "resolved": True, "strategy": "database"}
        at_users = set()
        for raw in (row["source"], row["packed_info_data"]):
            # msgsource may be wrapped in a binary protobuf field. Inspect only its XML island.
            if isinstance(raw, (bytes, bytearray, memoryview)):
                binary = bytes(raw)
                begin, finish = binary.find(b"<msgsource"), binary.find(b"</msgsource>")
                if begin >= 0 and finish >= begin:
                    raw = binary[begin:finish + len(b"</msgsource>")]
            text = _text(raw)
            start, end = text.find("<msgsource"), text.find("</msgsource>")
            if start >= 0 and end >= start:
                try:
                    at = ElementTree.fromstring(text[start:end + len("</msgsource>")]).findtext(".//atuserlist") or ""
                    at_users.update(user.strip() for user in at.split(",") if user.strip())
                except ElementTree.ParseError:
                    pass
        contact = self._contacts[stream.talker]
        sender_name = self._contacts.get(sender, {}).get("display_name", sender or "unknown")
        event = WechatDesktopEvent(
            kind="message", conversation_id=contact["conversation_id"], conversation_name=contact["display_name"],
            sender_id=sender or f"db-sender:{sender_num}", sender_name=sender_name, content_type=kind,
            content=content or f"[{kind}]", direction=direction, is_group=is_group,
            is_at=is_group and (self.owner_wxid in at_users or "notify@all" in at_users),
            source_type="group" if is_group else "private", reference=reference,
            message_stable_id=source_id, target_key=source_id, event_id="db-message:" + source_id,
            account_id=self.account_id, source_stream_id=stream.stream_id, source_message_id=source_id,
            source_local_id=local_id, native_timestamp=_integer(row["create_time"]), receipt_phase=phase,
        )
        return SourceRecord(stream.stream_id, local_id, source_id, event=event, receipt_phase=phase)

    def poll_batch(self, checkpoints, boot_highwaters):
        """游标按 local_id 分页，sort_seq 仅用于历史排序，避免并列序号漏消息。"""
        with self._lock:
            signature = (self._index_revision, tuple(sorted(
                (stream_id, _integer(checkpoint.get("cursor")), checkpoint.get("generation", ""))
                for stream_id, checkpoint in checkpoints.items())))
            if signature == self._idle_poll_signature:
                return None
            records, advances = [], []
            streams = sorted(self._streams.values(), key=lambda item: item.stream_id)
            if not streams:
                return None
            offset = self._scan_offset % len(streams)
            streams = streams[offset:] + streams[:offset]
            self._scan_offset = (offset + 1) % len(streams)
            budget = max(1, int(self.config.get("db_batch_size", 200)))
            backlog = {}
            for stream in streams:
                checkpoint = checkpoints.get(stream.stream_id)
                if checkpoint is None:
                    continue  # 后端必须先把新流基线写入账本，不能先交付历史。
                previous = _integer(checkpoint.get("cursor"))
                if checkpoint.get("generation") and checkpoint["generation"] != stream.generation:
                    raise DatabaseReadError("source_generation_changed", "来源消息库已替换，需要人工确认新基线")
                with self.caches[stream.database].read() as conn:
                    maximum = int(conn.execute(f"SELECT coalesce(max(local_id),0) FROM {_quote(stream.table)}").fetchone()[0])
                    backlog[stream.stream_id] = int(conn.execute(
                        f"SELECT count(*) FROM {_quote(stream.table)} WHERE local_id>?", (previous,)).fetchone()[0])
                if maximum < previous:
                    raise DatabaseReadError("source_cursor_regressed", "来源消息序号回退，暂停消息交付")
                if budget <= 0:
                    continue
                rows = self._rows(stream, after=previous, limit=budget)
                if not rows:
                    continue
                high = boot_highwaters.get(stream.stream_id, 0)
                if isinstance(high, dict):
                    high = high.get("cursor", 0)
                for row in rows:
                    phase = ("startup_unread" if (stream.stream_id, int(row["local_id"])) in self._startup_unread_ids else
                             "offline_backfill" if int(row["local_id"]) <= int(high) else "live")
                    records.append(self._parse(stream, row, phase))
                advances.append(SourceCheckpoint(stream.stream_id, int(rows[-1]["local_id"]), previous,
                                                 _integer(checkpoint.get("baseline_high_water")), stream.generation))
                budget -= len(rows)
            self._backlog = backlog
            self._idle_poll_signature = signature if not records else None
            return SourceBatch(self.account_id, uuid.uuid4().hex, tuple(records), tuple(advances)) if records else None

    def validate_native_event(self, event, checkpoint=None):
        """复核原生行，撤回、删除或替换的消息不能继续沿用旧回复任务。"""
        self.refresh()
        with self._lock:
            if event.account_id != self.account_id or event.source_local_id is None:
                return False, "source_account_mismatch"
            stream = self._streams.get(event.source_stream_id)
            if stream is None:
                return False, "source_stream_unavailable"
            if checkpoint and checkpoint.get("generation") and checkpoint["generation"] != stream.generation:
                return False, "source_generation_changed"
            with self.caches[stream.database].read() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(f"SELECT {self._select(stream)} FROM {_quote(stream.table)} WHERE local_id=? LIMIT 1",
                                   (event.source_local_id,)).fetchone()
            if row is None:
                return False, "source_message_deleted"
            current = self._parse(stream, dict(row)).event
            if current is None:
                return False, "source_message_filtered"
            if current.source_message_id != event.source_message_id or current.conversation_id != event.conversation_id:
                return False, "source_message_identity_mismatch"
            if current.content != event.content or current.content_type != event.content_type:
                return False, "source_message_changed"
            return True, ""

    def read_history(self, native_talker, limit=20):
        self.refresh()
        with self._lock:
            contact = self._contacts.get(native_talker)
            if contact is None:
                raise DatabaseReadError("conversation_not_found", "数据库中没有该会话")
            limit = max(1, min(50, int(limit)))
            rows = [(stream, row) for stream in self._streams.values() if stream.talker == native_talker
                    for row in self._rows(stream, limit=limit + 1, descending=True)]
            rows.sort(key=lambda pair: (_integer(pair[1]["sort_seq"], _integer(pair[1]["create_time"])),
                                        pair[0].stream_id, int(pair[1]["local_id"])), reverse=True)
            messages = []
            for stream, row in reversed(rows[:limit]):
                record = self._parse(stream, row, include_filtered=True)
                event = record.event
                sender_num = _integer(row["real_sender_id"])
                sender = self._sender_username(stream, sender_num)
                timestamp = _integer(row["create_time"])
                messages.append(WechatHistoryMessage(
                    sender_name=event.sender_name if event else self._contacts.get(sender, {}).get("display_name", sender or ("自己" if record.filter_reason == "outgoing_message" else "unknown")),
                    direction=event.direction if event else ("outgoing" if record.filter_reason == "outgoing_message" else "system" if record.filter_reason == "system_message" else "unknown"),
                    content_type=event.content_type if event else self._message_kind(row),
                    content=event.content if event else (_text(row["message_content"]) or _text(row["compress_content"])),
                    timestamp=str(timestamp), stable_id=record.source_message_id, source="wechat_database",
                    message_id=record.source_message_id, source_message_id=record.source_message_id,
                    native_timestamp=timestamp, degraded=event is None and record.filter_reason not in {"outgoing_message", "system_message"},
                ))
            return WechatHistoryReadResult(contact["display_name"], "group" if contact["is_group"] else "private",
                                           messages, limit, len(messages), has_more=len(rows) > limit,
                                           source="wechat_database", history_window_opened=False,
                                           conversation_id=contact["conversation_id"], account_id=self.account_id)

    def close(self):
        for cache in self.caches.values():
            close = getattr(cache, "close", None)
            if callable(close):
                close()
