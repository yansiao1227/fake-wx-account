"""微信 4.x 只读消息查询；字段识别参考固定版本 wechatauto（见本目录说明）。

来源身份由账号、数据库分片、消息表和 local_id 组成；数据库观察不访问 UIA。
只读取缓存的已验证快照，不修改微信库或实时游标。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from xml.etree import ElementTree

from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.db.types import SourceBatch, SourceCheckpoint, SourceRecord
from channel.wechat_desktop.models import (
    ReplyTaskMetadata, WechatDesktopEvent, WechatHistoryMessage, WechatHistoryReadResult,
)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _text(value, *, strict=False) -> str:
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
                frame = data[offset:]
                frame_size = zstandard.frame_content_size(frame)
                # max_output_size 只约束未声明长度的帧；声明长度的帧必须先检查。
                if frame_size not in (zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR) and frame_size > 2_000_000:
                    raise ValueError("compressed content exceeds limit")
                data = zstandard.ZstdDecompressor().decompress(frame, max_output_size=2_000_000)
            except ImportError as exc:
                raise DatabaseReadError("dependency_missing", "数据库文本读取需要 zstandard") from exc
            except Exception as exc:
                raise DatabaseReadError("content_decode_failed", "数据库压缩正文解码失败") from exc
        try:
            return data.decode("utf-8").strip("\x00")
        except UnicodeDecodeError as exc:
            if strict:
                raise DatabaseReadError("content_decode_failed", "数据库正文 UTF-8 解码失败") from exc
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
        self._database_indexes = {}
        self._index_dirty = set()
        self._session_unread = {}
        self._startup_unread_ids = set()
        self._initialized = False
        self._scan_offset = 0
        self._backlog = {}
        self._index_revision = 0
        self._highwaters = {}
        self._backlog_counts = {}
        self._idle_poll_signature = None
        self._error_code = ""
        self._last_success_at = 0.0
        self._closed_caches = set()
        self._cleanup_error_code = ""
        self._cleanup_pending = 0

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
            "db_cleanup_error_code": self._cleanup_error_code,
            "db_cleanup_pending": self._cleanup_pending,
            "account_binding": {"account_id": self.account_id, "pid": self.binding.pid,
                                "version": self.binding.version, "verification": "database_key_hmac"},
            "db_read_conversations": len(self._contacts),
        }

    def refresh(self):
        with self._lock:
            try:
                pending = set(self._index_dirty)
                changed = set(pending)
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
                            changed.add(relative)
                for relative, cache in self.caches.items():
                    status = cache.refresh()
                    if getattr(status, "error_code", "") == "page_hmac_failed" and self._key_provider is not None:
                        key = self._key_provider.get_key(relative, cache.source)
                        if key != cache.key:
                            cache.key = key
                            status = cache.refresh()
                    previous_status = self._statuses.get(relative)
                    self._statuses[relative] = status
                    if getattr(status, "stale", False) or not getattr(status, "healthy", True):
                        raise DatabaseReadError(getattr(status, "error_code", "snapshot_stale") or "snapshot_stale",
                                                "数据库快照刷新失败，暂停消息交付")
                    if (not self._initialized or relative not in self._database_indexes or
                            bool(getattr(status, "changed", True)) or
                            getattr(previous_status, "generation", None) != getattr(status, "generation", "")):
                        changed.add(relative)
                        self._index_dirty.add(relative)
                if changed:
                    self._load_indexes(changed, force_full=pending)
                    self._index_dirty.difference_update(changed)
                self._initialized = True
                self._error_code = ""
                self._last_success_at = time.time()
            except Exception as exc:
                self._error_code = getattr(exc, "code", "database_read_failed")
                raise

    @staticmethod
    def _page_tables(conn):
        """一次建立已验证快照的页归属；索引页必须归到它的消息/映射表。"""
        try:
            result = {}
            for page, table in conn.execute(
                    "SELECT d.pageno,coalesce(m.tbl_name,d.name) FROM dbstat AS d "
                    "LEFT JOIN sqlite_master AS m ON m.name=d.name"):
                result.setdefault(int(page), set()).add(str(table).lower())
            return result
        except sqlite3.OperationalError:
            # 某些 SQLite 运行时没有 dbstat，仍可安全按变化分片刷新。
            return None

    def _index_table(self, conn, lowered, table, columns):
        data = {"contacts": {}, "names": set(), "mapping": None, "unread": {}}
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
                data["contacts"][user] = {"conversation_id": self.conversation_id(user), "username": user,
                                          "nick_name": nick, "remark": remark, "alias": alias,
                                          "display_name": remark or nick or user,
                                          "is_group": user.endswith("@chatroom")}
                data["names"].add(user)
        elif lowered == "sessiontable":
            user_col = _column(columns, "username", "user_name")
            if user_col:
                data["names"].update(_text(row[0]) for row in conn.execute(
                    f"SELECT {_quote(user_col)} FROM {_quote(table)}") if row[0])
                count_col = _column(columns, "unread_count")
                first_col = _column(columns, "unread_first_msg_srv_id")
                if count_col and first_col:
                    for row in conn.execute(f"SELECT {_quote(user_col)},{_quote(count_col)},"
                                            f"{_quote(first_col)} FROM {_quote(table)} WHERE {_quote(count_col)}>0"):
                        data["unread"][_text(row[0])] = (_integer(row[1]), _integer(row[2]))
        elif lowered in {"name2id", "sendername2id"}:
            user_col = _column(columns, "user_name", "username", "name")
            id_col = _column(columns, "id", "user_id")
            if user_col:
                id_expr = _quote(id_col) if id_col else "rowid"
                data["mapping"] = {int(row[0]): _text(row[1]) for row in conn.execute(
                    f"SELECT {id_expr},{_quote(user_col)} FROM {_quote(table)}") if row[1]}
                data["names"].update(data["mapping"].values())
        return data

    def _load_indexes(self, changed=None, *, force_full=()):
        indexes = dict(self._database_indexes)
        invalid_streams = set()
        metadata_changed = False
        for relative in sorted(self.caches if changed is None else changed):
            status = self._statuses[relative]
            previous = indexes.get(relative)
            generation = getattr(status, "generation", "")
            with self.caches[relative].read() as conn:
                schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
                schema_changed = previous is None or schema_version != previous["schema_version"]
                generation_changed = previous is None or generation != previous["generation"]
                if schema_changed or generation_changed:
                    metadata_changed = True
                    tables = _tables(conn)
                    columns = {lowered: _columns(conn, table) for lowered, table in tables.items()}
                    data = {}
                    pages = self._page_tables(conn)
                    affected = set(tables)
                else:
                    tables, columns = previous["tables"], previous["columns"]
                    data, pages = dict(previous["data"]), previous["pages"]
                    changed_pages = getattr(status, "changed_pages", None)
                    if changed_pages is None or relative in force_full:
                        affected = set(tables)
                        pages = self._page_tables(conn)
                    elif pages is None:
                        affected = set(tables)
                    elif 1 in changed_pages or any(page not in pages for page in changed_pages):
                        # 页分配/释放会改动页1；未知页、页复用必须重建归属并安全回退。
                        affected = set(tables)
                        pages = self._page_tables(conn)
                    else:
                        affected = set().union(*(pages[page] for page in changed_pages)) if changed_pages else set()
                for lowered in affected:
                    if lowered not in tables:
                        continue
                    if lowered in {"contact", "sessiontable", "name2id", "sendername2id"}:
                        metadata_changed = True
                        data[lowered] = self._index_table(conn, lowered, tables[lowered], columns[lowered])
                    if lowered.startswith("msg_"):
                        invalid_streams.add(relative + ":" + tables[lowered])
                indexes[relative] = {"schema_version": schema_version, "generation": generation,
                                     "tables": tables, "columns": columns, "data": data, "pages": pages}
        if not metadata_changed:
            # 普通消息页更新只改变行数据；复用全账号联系人、映射和流对象。
            for stream_id in invalid_streams:
                self._highwaters.pop(stream_id, None)
                self._backlog_counts.pop(stream_id, None)
            self._database_indexes = indexes
            if invalid_streams:
                self._index_revision += 1
                self._idle_poll_signature = None
            return
        contacts, names, sender_maps, session_unread = {}, set(), {}, {}
        for relative, index in indexes.items():
            for lowered, data in index["data"].items():
                contacts.update(data["contacts"])
                names.update(data["names"])
                session_unread.update(data["unread"])
                if data["mapping"] is not None:
                    sender_maps.setdefault(relative, {}).update(data["mapping"])
                    if lowered == "sendername2id":
                        sender_maps.setdefault("*", {}).update(data["mapping"])
        for user in names:
            contacts.setdefault(user, {"conversation_id": self.conversation_id(user), "username": user,
                                       "nick_name": "", "remark": "", "alias": "",
                                       "display_name": user, "is_group": user.endswith("@chatroom")})
        md5_index = {hashlib.md5(user.encode()).hexdigest(): user for user in names}
        streams = {}
        for relative, index in indexes.items():
            if not Path(relative).name.lower().startswith("message_"):
                continue
            for lowered, table in index["tables"].items():
                if not lowered.startswith("msg_"):
                    continue
                columns = index["columns"][lowered]
                if not _column(columns, "local_id"):
                    raise DatabaseReadError("unsupported_message_schema", "消息表缺少 local_id")
                if not _column(columns, "local_type", "type") or not _column(columns, "create_time"):
                    raise DatabaseReadError("unsupported_message_schema", "消息表缺少类型或时间字段")
                stream_id = relative + ":" + table
                talker = md5_index.get(table[4:].lower(), "")
                existing = self._streams.get(stream_id)
                streams[stream_id] = (existing if existing is not None and existing.talker == talker and
                                      existing.columns == columns and existing.generation == index["generation"] else
                                      MessageStream(stream_id, relative, table, talker, columns, index["generation"]))
        if not streams:
            raise DatabaseReadError("message_database_unavailable", "未找到可识别的微信消息表")
        invalid_streams.update(set(self._streams) ^ set(streams))
        for stream_id in invalid_streams:
            self._highwaters.pop(stream_id, None)
            self._backlog_counts.pop(stream_id, None)
        self._database_indexes = indexes
        self._contacts, self._sender_maps, self._streams = contacts, sender_maps, streams
        self._session_unread = session_unread
        self._conversation_lookup = {item["conversation_id"]: user for user, item in contacts.items()}
        self._index_revision += 1
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
            pending = {}
            for stream_id, stream in self._streams.items():
                if stream_id not in self._highwaters:
                    pending.setdefault(stream.database, []).append(stream)
            for database, streams in pending.items():
                with self.caches[database].read() as conn:
                    for stream in streams:
                        cursor = conn.execute(f"SELECT coalesce(max(local_id),0) FROM {_quote(stream.table)}").fetchone()[0]
                        self._highwaters[stream.stream_id] = {"cursor": int(cursor), "generation": stream.generation}
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
    def _app_node(content):
        if not content.lstrip().startswith("<"):
            return None
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError:
            return None
        return root if root.tag == "appmsg" else root.find(".//appmsg")

    @staticmethod
    def _share_metadata(app):
        # 只记录原生分享字段；不联网、不获取媒体，也不声称取得了页面正文。
        return {"title": app.findtext("title") or "", "url": app.findtext("url") or "",
                "description": app.findtext("des") or "",
                "platform": app.findtext("appname") or app.findtext("sourcedisplayname") or ""}

    def _parse_reference(self, quoted):
        content = quoted.findtext("content") or ""
        kind = self._message_kind({"local_type": quoted.findtext("type")})
        app = self._app_node(content) if kind == "app_message" else None
        metadata = {}
        if app is not None:
            kind = {57: "text", 5: "share_card", 6: "file"}.get(
                _integer(app.findtext("type")), "app_message")
            content = app.findtext("title") or ("" if kind == "text" else f"[{kind}]")
            if kind == "share_card":
                metadata = self._share_metadata(app)
        elif kind in {"image", "voice", "video", "sticker", "location"}:
            content = {"image": "[图片]", "voice": "[语音]", "video": "[视频]",
                       "sticker": "[动画表情]", "location": "[位置]"}[kind]
        resolved = kind == "text" and bool(content.strip())
        return {"sender_name": quoted.findtext("displayname") or "",
                "sender_id": quoted.findtext("chatusr") or quoted.findtext("fromusr") or "",
                "content": content, "content_type": kind,
                "source_message_id": quoted.findtext("svrid") or "", "depth": 1,
                "native_type": _integer(quoted.findtext("type")) & 0xFFFFFFFF,
                "resolved": resolved, "degraded": not resolved, "strategy": "database", **metadata}

    @staticmethod
    def _reference_server_id(reference):
        """SQLite 的整数参数必须可精确表示，不能 CAST 非数字或溢出的引用 ID。"""
        value = str(reference.get("source_message_id") or "").strip()
        if not value or not value.isascii() or not value.isdecimal() or len(value) > 19:
            return None
        number = int(value)
        return number if 0 < number <= 0x7FFFFFFFFFFFFFFF else None

    def _lookup_reference_row(self, talker, server_id, connections, lookup_cache):
        key = (talker, server_id)
        if key not in lookup_cache:
            matches = []
            for stream in self._streams.values():
                column = _column(stream.columns, "server_id")
                if stream.talker != talker or not column:
                    continue
                conn = connections[stream.database]
                conn.row_factory = sqlite3.Row
                # 唯一性必须覆盖整个原生会话；相同内容的跨分片重复 ID 也不猜。
                rows = conn.execute(
                    f"SELECT /* reference_lookup */ {self._select(stream)} FROM {_quote(stream.table)} "
                    f"WHERE typeof({_quote(column)})='integer' AND {_quote(column)}=? LIMIT 2",
                    (server_id,)).fetchall()
                matches.extend((stream, dict(row)) for row in rows)
                if len(matches) > 1:
                    break
            lookup_cache[key] = matches[0] if len(matches) == 1 else None
        return lookup_cache[key]

    def _reference_origin(self, event, connections, lookup_cache):
        reference = event.reference
        server_id = self._reference_server_id(reference)
        current_stream = self._streams.get(event.source_stream_id)
        if (server_id is None or current_stream is None or event.account_id != self.account_id or
                event.conversation_id != self.conversation_id(current_stream.talker)):
            return None
        candidate = self._lookup_reference_row(current_stream.talker, server_id, connections, lookup_cache)
        if candidate is None:
            return None
        stream, row = candidate
        if (stream.stream_id == event.source_stream_id and
                _integer(row["local_id"]) >= _integer(event.source_local_id)):
            return None
        if event.native_timestamp is None or _integer(row["create_time"]) > event.native_timestamp:
            return None
        quoted_kind = self._message_kind({"local_type": reference.get("native_type")})
        if quoted_kind != "unsupported" and quoted_kind != self._message_kind(row):
            return None
        origin_key = ("parsed_origin", stream.stream_id, _integer(row["local_id"]))
        if origin_key not in lookup_cache:
            origin = self._parse(stream, row, include_filtered=True).event
            if origin is not None and origin.content_type == "text":
                raw = _text(row["message_content"]) or _text(row["compress_content"])
                app = self._app_node(raw) if self._message_kind(row) == "app_message" else None
                if not raw.strip() or app is not None and not (app.findtext("title") or "").strip():
                    origin = None
            lookup_cache[origin_key] = origin
        origin = lookup_cache[origin_key]
        if (origin is None or origin.direction not in {"incoming", "outgoing"} or
                origin.sender_id.startswith("db-sender:")):
            return None
        quoted_sender = reference.get("sender_id", "")
        # 部分群引用的 chatusr 是群 ID；它只能证明会话，不能当成群成员 ID。
        if quoted_sender and quoted_sender != current_stream.talker and quoted_sender != origin.sender_id:
            return None
        if not event.is_group and quoted_sender == current_stream.talker and quoted_sender != origin.sender_id:
            return None
        quoted_type = reference.get("source_content_type") or reference.get("content_type", "")
        if quoted_type not in {"", "unsupported", "app_message"} and quoted_type != origin.content_type:
            return None
        return stream, origin

    def _enrich_reference(self, event, connections, lookup_cache):
        """补全一层原生引用，不递归解析原消息的引用，不访问界面或网络。"""
        if not event.reference or event.reference.get("source_native_message_id"):
            return event
        candidate = self._reference_origin(event, connections, lookup_cache)
        if candidate is None:
            return event  # 找不到或证据有歧义时，仍保留 refermsg 自带的内容。
        stream, origin = candidate
        reference = event.reference
        reference.setdefault("preview_content", reference.get("content", ""))
        kind = origin.content_type
        reference["content_type"] = kind
        if kind == "text":
            reference["content"] = origin.content
            reference.update(resolved=bool(origin.content.strip()), degraded=not bool(origin.content.strip()))
        elif (kind == "file" and not origin.content.lstrip().startswith("<") and
              reference.get("content") in {None, "", "[file]", "[app_message]"}):
            reference["content"] = origin.content
        elif kind == "share_card":
            for key, value in origin.share_card.items():
                if value and not reference.get(key):
                    reference[key] = value
            if reference.get("content") in {None, "", "[share_card]", "[app_message]"}:
                reference["content"] = origin.share_card.get("title") or reference.get("content", "")
        if not reference.get("sender_name"):
            reference["sender_name"] = origin.sender_name
        reference.update(
            source_account_id=self.account_id, source_stream_id=origin.source_stream_id,
            source_local_id=origin.source_local_id, source_native_message_id=origin.source_message_id,
            source_generation=stream.generation, native_timestamp=origin.native_timestamp,
            content_signature=origin.content_signature, source_sender_id=origin.sender_id,
            source_direction=origin.direction, source_content_type=origin.content_type,
        )
        event.history = [{**reference, "is_reference": True}]
        return event

    def enrich_reference(self, event):
        """FIFO 物化前按需补文；查询不推进实时游标，也不替换已有原生证据。"""
        with self._lock, ExitStack() as stack:
            valid, reason = self.validate_native_event(event)
            if not valid:
                raise DatabaseReadError(reason, "引用消息来源已变化，暂停处理")
            stream = self._streams[event.source_stream_id]
            databases = {item.database for item in self._streams.values() if item.talker == stream.talker}
            connections = {database: stack.enter_context(self.caches[database].read())
                           for database in sorted(databases)}
            return self._enrich_reference(event, connections, {})

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

    @staticmethod
    def _row_order(stream, row):
        # 原生时间界定历史边界；并列时间用原生序号及完整来源主键确定顺序。
        return (_integer(row["create_time"]), _integer(row["sort_seq"]),
                stream.stream_id, int(row["local_id"]))

    @staticmethod
    def _history_order_sql(stream):
        timestamp = _quote(_column(stream.columns, "create_time"))
        sequence = _column(stream.columns, "sort_seq")
        order = [f"{timestamp} DESC"]
        if sequence:
            order.append(f"{_quote(sequence)} DESC")
        return ",".join([*order, "local_id DESC"])

    def _rows(self, stream, *, after=None, limit=200, descending=False, connection=None):
        if connection is None:
            with self.caches[stream.database].read() as conn:
                return self._rows(stream, after=after, limit=limit, descending=descending, connection=conn)
        predicate = " WHERE local_id > ?" if after is not None else ""
        order = self._history_order_sql(stream) if descending else "local_id ASC"
        parameters = (after, limit) if after is not None else (limit,)
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(
            f"SELECT {self._select(stream)} FROM {_quote(stream.table)}{predicate} ORDER BY {order} LIMIT ?",
            parameters)]

    def _reply_context_rows(self, stream, events, connection, scan_limit, *, batch_local_ids=()):
        """只执行可索引定位且无需 SQLite 排序的有界查询。

        时间索引可直接定位较早的跨分片历史；没有时间索引时沿 local_id
        索引读取最近的有限行。时间过滤放在内存，避免 LIMIT 前扫描大量
        不满足时间条件的行。没有可用索引时省略该流的候选上下文。
        """
        timestamp = _quote(_column(stream.columns, "create_time"))
        own_events = [event for _, event in events if event.source_stream_id == stream.stream_id]
        first_own_event = min(own_events, key=lambda event: event.source_local_id) if own_events else None
        upper_local_id = (first_own_event.source_local_id - 1 if first_own_event
                          else self._highwaters[stream.stream_id]["cursor"])
        upper_timestamp = events[0][0][0]
        excluded = tuple(sorted(batch_local_ids)[:500])
        if excluded:
            # 只有单列原生主键才能保证每个排除项最多跳过一行；参数量也有界。
            # 每流索引最多访问 scan_limit + 500 行，排除项已在同批读过。
            primary_key = [str(row[1]).lower() for row in connection.execute(
                f"PRAGMA table_info({_quote(stream.table)})") if row[5]]
            if primary_key != ["local_id"]:
                excluded = ()
        exclusion = (" AND local_id NOT IN (" + ",".join("?" for _ in excluded) + ")" if excluded else "")
        queries = [
            (f"WHERE {timestamp}>=0 AND {timestamp}<=?{exclusion} ORDER BY {timestamp} DESC LIMIT ?",
             (upper_timestamp, *excluded, scan_limit)),
            ("WHERE local_id>0 AND local_id<=? ORDER BY local_id DESC LIMIT ?",
             (upper_local_id, scan_limit)),
        ]
        connection.row_factory = sqlite3.Row
        for predicate, parameters in queries:
            sql = f"SELECT {self._select(stream)} FROM {_quote(stream.table)} {predicate}"
            plans = [str(row[3]).upper() for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)]
            # LIMIT 不能限制无索引范围查询或排序所扫描的行，先确认执行计划。
            if (not any(plan.startswith("SEARCH ") for plan in plans) or
                    any("SCAN " in plan or "TEMP B-TREE" in plan for plan in plans)):
                continue
            return [dict(row) for row in connection.execute(
                sql.replace("SELECT ", "SELECT /* reply_context */ ", 1), parameters)]
        return []

    def _attach_reply_context(self, records, rows, connections):
        """同批复用已读行，额外索引读取同时受单流和全批预算限制。

        所有会话/分片共享额外读取预算，预分配 SQL LIMIT；过滤行和边界外
        行同样占额，耗尽后不寻找更旧消息。本批已读行继续复用，不重复计额。
        完整排序只在内存进行，没有合适索引时不退回 SQLite 全表扫描。
        """
        from channel.wechat_desktop.config import DEFAULT_CONFIG
        limit = max(0, min(50, int(self.config.get(
            "reply_context_max_messages", DEFAULT_CONFIG["reply_context_max_messages"]))))
        stream_scan_limit = max(1, min(10000, int(self.config.get(
            "db_reply_context_max_rows_per_stream", DEFAULT_CONFIG["db_reply_context_max_rows_per_stream"]))))
        scan_budget = max(0, int(self.config.get(
            "reply_context_scan_max_rows", DEFAULT_CONFIG["reply_context_scan_max_rows"])))
        targets = {}
        batch_rows = {}
        reference_lookup_cache = {}
        for record in records:
            batch_rows.setdefault(record.stream_id, {})[record.local_id] = rows[record.source_message_id]
            event = record.event
            if event is None or record.receipt_phase not in {"live", "startup_unread"}:
                continue
            if event.reference:
                self._enrich_reference(event, connections, reference_lookup_cache)
                event.history = [{**event.reference, "is_reference": True}]
            elif limit:
                stream = self._streams[record.stream_id]
                targets.setdefault(stream.talker, []).append(
                    (self._row_order(stream, rows[record.source_message_id]), event))
        for events in targets.values():
            events.sort(key=lambda pair: pair[0], reverse=True)
        streams_by_talker = {talker: [] for talker in targets}
        for stream in self._streams.values():
            events = targets.get(stream.talker)
            if not events or self._highwaters[stream.stream_id]["cursor"] <= 0:
                continue  # 空来源复用已缓存高水位，不为上下文重新打开查询。
            if all(event.source_stream_id == stream.stream_id and event.source_local_id <= 1
                   for _, event in events):
                continue  # 首个原生消息没有同来源前序正主键。
            streams_by_talker[stream.talker].append(stream)
        remaining_streams = sum(len(streams) for streams in streams_by_talker.values())
        for talker, events in targets.items():
            candidates = {event.source_message_id: [] for _, event in events}
            for stream in streams_by_talker[talker]:
                # 提前保留 SQL 查询额度，所有 LIMIT 的总和不超过批次预算；
                # 不因某 target 缺少可用消息而给它重新分配扫描额度。
                scan_limit = min(stream_scan_limit, (scan_budget + remaining_streams - 1) // remaining_streams)
                remaining_streams -= 1
                scan_budget -= scan_limit
                candidates_by_id = dict(batch_rows.get(stream.stream_id, {}))
                if scan_limit:
                    candidates_by_id.update({int(row["local_id"]): row for row in self._reply_context_rows(
                        stream, events, connections[stream.database], scan_limit,
                        batch_local_ids=candidates_by_id)})
                ordered_rows = sorted(candidates_by_id.values(),
                                      key=lambda row: self._row_order(stream, row), reverse=True)
                windows = {event.source_message_id: [] for _, event in events}
                for row in ordered_rows:
                    order = self._row_order(stream, row)
                    eligible = [event for boundary, event in events
                                if len(windows[event.source_message_id]) < limit and 0 <= order[0] and order < boundary
                                and (stream.stream_id != event.source_stream_id or
                                     int(row["local_id"]) < event.source_local_id)]
                    if not eligible:
                        continue
                    record = self._parse(stream, row, include_filtered=True)
                    previous = record.event
                    if previous is None or previous.direction == "system":
                        continue
                    item = {
                            "sender_name": previous.sender_name if previous.is_group else "",
                            "content": previous.content if previous.content_type == "text" else f"[{previous.content_type}]",
                            "content_type": previous.content_type, "direction": previous.direction,
                            "_message_stable_id": previous.source_message_id,
                            "source_message_id": previous.source_message_id,
                            "source_stream_id": previous.source_stream_id,
                            "source_local_id": previous.source_local_id,
                            "account_id": previous.account_id, "native_timestamp": previous.native_timestamp,
                            "content_signature": previous.content_signature,
                            "source": "wechat_database",
                    }
                    evidence = replace(previous, content="", history=[], reference={}, share_card={},
                                       task=ReplyTaskMetadata())
                    for target in eligible:
                        windows[target.source_message_id].append((order, item, evidence))
                    if all(len(window) >= limit for window in windows.values()):
                        break
                for source_id, window in windows.items():
                    candidates[source_id].extend(window)
            for _, event in events:
                ordered = sorted(candidates[event.source_message_id], key=lambda pair: pair[0], reverse=True)
                selected = list(reversed(ordered[:limit]))
                event.history = [dict(item) for _, item, _ in selected]
                event.task.context_source_events = [evidence for _, _, evidence in selected]

    def _parse(self, stream, row, phase="live", *, include_filtered=False):
        local_id = int(row["local_id"])
        source_id = hashlib.sha256(f"{self.account_id}\0{stream.stream_id}\0{local_id}".encode()).hexdigest()
        try:
            return self._parse_message(stream, row, local_id, source_id, phase, include_filtered=include_filtered)
        except DatabaseReadError as exc:
            if exc.code != "content_decode_failed":
                raise
            # 正文损坏仅属于当前行；依赖/账号/快照错误仍必须使来源暂停。
            return SourceRecord(stream.stream_id, local_id, source_id,
                                filter_reason="content_decode_failed", receipt_phase=phase)

    @staticmethod
    def _native_payload_signature(row, content, sender, direction):
        """原生内容证据与 UI 物化结果分离；只把摘要保存到事件。

        显示名来自可变联系人目录，不参与校验。引用/XML/分享 URL 仍在完整
        原生正文中，不能用提取后的同名标题替代该证据。
        """
        def digest(value):
            if value is None:
                return None
            data = (b"bytes\0" + bytes(value) if isinstance(value, (bytes, bytearray, memoryview))
                    else b"text\0" + str(value).encode("utf-8"))
            return hashlib.sha256(data).hexdigest()

        payload = ["wechat-native-payload-v1", digest(content), _integer(row["local_type"]),
                   _integer(row["real_sender_id"], -1), sender, direction,
                   None if row["is_sender"] is None else _integer(row["is_sender"]),
                   _integer(row["create_time"]), _integer(row["server_id"]), _integer(row["sort_seq"]),
                   digest(row["source"]), digest(row["packed_info_data"])]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _parse_message(self, stream, row, local_id, source_id, phase, *, include_filtered=False):
        if not stream.talker:
            return SourceRecord(stream.stream_id, local_id, source_id, filter_reason="conversation_unresolved", receipt_phase=phase)
        kind = self._message_kind(row)
        if kind == "system" and not include_filtered:
            return SourceRecord(stream.stream_id, local_id, source_id, filter_reason="system_message", receipt_phase=phase)
        sender_num = _integer(row["real_sender_id"], -1)
        sender = self._sender_username(stream, sender_num)
        is_group = stream.talker.endswith("@chatroom")
        try:
            content = _text(row["message_content"], strict=True)
        except DatabaseReadError as exc:
            # 某些快照的主字段是二进制占位，真实正文保存在压缩字段。
            if exc.code != "content_decode_failed" or not row["compress_content"]:
                raise
            content = _text(row["compress_content"], strict=True)
            if not content:
                raise exc
        if not content:
            content = _text(row["compress_content"], strict=True)
        native_content = content
        # Older message snapshots prefix a group text body with "wxid:\n".
        if is_group and ":\n" in content and not content.lstrip().startswith("<"):
            prefix, body = content.split(":\n", 1)
            if sender and prefix == sender:
                content = body
            elif not sender and (prefix.startswith("wxid_") or (
                    prefix in self._contacts and not self._contacts[prefix]["is_group"])):
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
        content_signature = self._native_payload_signature(row, native_content, sender, direction)
        reference, share_card = {}, {}
        if kind == "app_message":
            app = self._app_node(content)
            if app is not None:
                subtype = _integer(app.findtext("type"))
                content = app.findtext("title") or content
                kind = {57: "text", 5: "share_card", 6: "file"}.get(subtype, "app_message")
                if kind == "share_card":
                    share_card = self._share_metadata(app)
                quoted = app.find("refermsg")
                if quoted is not None:
                    reference = self._parse_reference(quoted)
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
            source_type="group" if is_group else "private", reference=reference, share_card=share_card,
            message_stable_id=source_id, target_key=source_id, event_id="db-message:" + source_id,
            content_signature=content_signature,
            account_id=self.account_id, source_stream_id=stream.stream_id, source_message_id=source_id,
            source_local_id=local_id, native_timestamp=_integer(row["create_time"]), receipt_phase=phase,
        )
        return SourceRecord(stream.stream_id, local_id, source_id, event=event, receipt_phase=phase)

    def poll_batch(self, checkpoints, boot_highwaters):
        """游标按 local_id 分页，sort_seq 仅用于历史排序，避免并列序号漏消息。"""
        with self._lock, ExitStack() as stack:
            signature = (self._index_revision, tuple(sorted(
                (stream_id, _integer(checkpoint.get("cursor")), checkpoint.get("generation", ""))
                for stream_id, checkpoint in checkpoints.items())))
            if signature == self._idle_poll_signature:
                return None
            records, advances, context_rows = [], [], {}
            streams = sorted(self._streams.values(), key=lambda item: item.stream_id)
            if not streams:
                return None
            # 所有消息行与上下文查询共用这些已验证快照；缓存刷新须等待批次完成。
            connections = {database: stack.enter_context(self.caches[database].read())
                           for database in sorted({stream.database for stream in streams})}
            offset = self._scan_offset % len(streams)
            streams = streams[offset:] + streams[:offset]
            self._scan_offset = (offset + 1) % len(streams)
            budget = max(1, int(self.config.get("db_batch_size", 200)))
            backlog = {}
            highwaters = self.get_highwaters()
            for stream in streams:
                checkpoint = checkpoints.get(stream.stream_id)
                if checkpoint is None:
                    continue  # 后端必须先把新流基线写入账本，不能先交付历史。
                previous = _integer(checkpoint.get("cursor"))
                if checkpoint.get("generation") and checkpoint["generation"] != stream.generation:
                    raise DatabaseReadError("source_generation_changed", "来源消息库已替换，需要人工确认新基线")
                maximum = highwaters[stream.stream_id]["cursor"]
                if maximum < previous:
                    raise DatabaseReadError("source_cursor_regressed", "来源消息序号回退，暂停消息交付")
                if maximum == previous:
                    backlog[stream.stream_id] = 0
                    continue
                counts = self._backlog_counts.setdefault(stream.stream_id, {})
                if previous not in counts:
                    counts[previous] = int(connections[stream.database].execute(
                        f"SELECT count(*) FROM {_quote(stream.table)} WHERE local_id>?", (previous,)).fetchone()[0])
                backlog[stream.stream_id] = counts[previous]
                if budget <= 0:
                    continue
                rows = self._rows(stream, after=previous, limit=budget, connection=connections[stream.database])
                if not rows:
                    continue
                high = boot_highwaters.get(stream.stream_id, 0)
                if isinstance(high, dict):
                    high = high.get("cursor", 0)
                for row in rows:
                    phase = ("startup_unread" if (stream.stream_id, int(row["local_id"])) in self._startup_unread_ids else
                             "offline_backfill" if int(row["local_id"]) <= int(high) else "live")
                    record = self._parse(stream, row, phase)
                    records.append(record)
                    context_rows[record.source_message_id] = row
                advances.append(SourceCheckpoint(stream.stream_id, int(rows[-1]["local_id"]), previous,
                                                 _integer(checkpoint.get("baseline_high_water")), stream.generation))
                # local_id可能有空洞，必须按实际读取行数扣除，不能用游标差值。
                self._backlog_counts[stream.stream_id] = {
                    previous: counts[previous], int(rows[-1]["local_id"]): counts[previous] - len(rows)}
                budget -= len(rows)
            self._attach_reply_context(records, context_rows, connections)
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
            current = self._parse(stream, dict(row), include_filtered=True).event
            if (current is None or current.direction == "system" or
                    current.direction == "outgoing" and event.direction != "outgoing"):
                return False, "source_message_filtered"
            if current.source_message_id != event.source_message_id or current.conversation_id != event.conversation_id:
                return False, "source_message_identity_mismatch"
            if (current.sender_id != event.sender_id or current.direction != event.direction or
                    current.source_type != event.source_type or current.is_group != event.is_group or
                    current.content_type != event.content_type or
                    event.native_timestamp is not None and current.native_timestamp != event.native_timestamp):
                return False, "source_message_changed"
            if event.content_signature:
                if current.content_signature != event.content_signature:
                    return False, "source_message_changed"
            else:
                # 兼容没有签名的旧合成事件；不把物化状态或 UIA 路径当作原生证据。
                native_reference = ("source_message_id", "sender_id", "content_type", "content", "url")
                if (current.content != event.content or
                        any(current.reference.get(key, "") != event.reference.get(key, "")
                            for key in native_reference) or current.share_card != event.share_card):
                    return False, "source_message_changed"
            if event.reference.get("source_native_message_id"):
                with ExitStack() as stack:
                    databases = {item.database for item in self._streams.values() if item.talker == stream.talker}
                    connections = {database: stack.enter_context(self.caches[database].read())
                                   for database in sorted(databases)}
                    reference = event.reference
                    origin_stream = self._streams.get(reference.get("source_stream_id"))
                    if (reference.get("source_account_id") != self.account_id or origin_stream is None or
                            origin_stream.talker != stream.talker):
                        return False, "reference_source_identity_mismatch"
                    if reference.get("source_generation") != origin_stream.generation:
                        return False, "reference_source_generation_changed"
                    candidate = self._reference_origin(event, connections, {})
                    if candidate is None:
                        return False, "reference_source_unavailable"
                    _, origin = candidate
                    if (origin.source_message_id != reference.get("source_native_message_id") or
                            origin.source_stream_id != reference.get("source_stream_id") or
                            origin.source_local_id != reference.get("source_local_id")):
                        return False, "reference_source_identity_mismatch"
                    if (origin.content_signature != reference.get("content_signature") or
                            origin.native_timestamp != reference.get("native_timestamp") or
                            origin.sender_id != reference.get("source_sender_id") or
                            origin.direction != reference.get("source_direction") or
                            origin.content_type != reference.get("source_content_type")):
                        return False, "reference_source_changed"
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
            rows.sort(key=lambda pair: self._row_order(*pair), reverse=True)
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
                    content=(event.content if event else "[正文解码失败]" if record.filter_reason == "content_decode_failed"
                             else (_text(row["message_content"]) or _text(row["compress_content"]))),
                    timestamp=str(timestamp), stable_id=record.source_message_id, source="wechat_database",
                    message_id=record.source_message_id, source_message_id=record.source_message_id,
                    native_timestamp=timestamp, degraded=event is None and record.filter_reason not in {"outgoing_message", "system_message"},
                ))
            return WechatHistoryReadResult(contact["display_name"], "group" if contact["is_group"] else "private",
                                           messages, limit, len(messages), has_more=len(rows) > limit,
                                           source="wechat_database", history_window_opened=False,
                                           conversation_id=contact["conversation_id"], account_id=self.account_id)

    def close(self):
        """各分片独立清理，失败项可重试，对外异常不携带明文路径。"""
        with self._lock:
            self._initialized = False
            failed = 0
            for relative, cache in self.caches.items():
                if relative in self._closed_caches:
                    continue
                try:
                    close = getattr(cache, "close", None)
                    if callable(close):
                        close()
                except Exception:
                    failed += 1
                else:
                    self._closed_caches.add(relative)
            self._cleanup_pending = failed
            self._cleanup_error_code = "snapshot_cleanup_failed" if failed else ""
            if failed:
                raise DatabaseReadError("snapshot_cleanup_failed", "数据库快照清理失败") from None
