"""数据库事件来源：账号生命周期、分页和接收回执，不操作桌面界面。"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from channel.wechat_desktop.db.errors import DatabaseReadError
from common.log import logger


class WechatDatabaseSource:
    def __init__(self, config, *, reader=None, checkpoint_store=None, checkpoint_store_provider=None):
        self.config = config
        self._reader = reader
        self._frozen_account_id = getattr(reader, "account_id", None)
        self._account_changed = False
        self._startup_unread_allowed = True
        self._store = checkpoint_store
        self._store_provider = checkpoint_store_provider
        self._lock = threading.RLock()
        self._pending_batch = None
        self._boot_highwaters = None
        self._startup_unread_bounds = {}
        self._cleanup_readers = []
        self._closed = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self._closed_cleaned = False
        self._session_epoch = 0
        self._last_status = {"db_read_healthy": False, "db_read_stale": False,
                             "db_read_error_code": "", "message_source": "wechat_database"}

    @property
    def session_epoch(self):
        """账号进程生命周期代次；组合层据此丢弃旧的桌面绑定。"""
        return self._session_epoch

    @property
    def pending_batch(self):
        return self._pending_batch

    def get_reader(self):
        with self._lock:
            if self._closed.is_set():
                raise DatabaseReadError("source_closed", "数据库事件来源已关闭")
            if self._account_changed:
                raise DatabaseReadError("account_changed", "登录账号已变化，请停止并重新绑定通道")
            if self._reader is None:
                from channel.wechat_desktop.db.reader import WechatDatabaseReader
                candidate = WechatDatabaseReader(self.config)
                if self._frozen_account_id is not None and candidate.account_id != self._frozen_account_id:
                    self._cleanup_readers.append(candidate)
                    self._cleanup_retired_readers()
                    self._account_changed = True
                    raise DatabaseReadError("account_changed", "登录账号已变化，请停止并重新绑定通道")
                self._reader = candidate
                self._frozen_account_id = candidate.account_id
            return self._reader

    def get_checkpoint_store(self):
        if self._store is None:
            if self._store_provider is None:
                raise RuntimeError("数据库事件来源尚未绑定接收账本")
            self._store = self._store_provider()
        return self._store

    @contextmanager
    def reader_session(self):
        """查询与绑定期间固定账号生命周期，避免重登录关闭正在使用的副本。"""
        with self._lock:
            yield self.get_reader()

    def search_contacts(self, query="", limit=20):
        with self.reader_session() as reader:
            return reader.search_contacts(query, limit)

    def read_chat_history(self, conversation_id, limit=20, *, session_epoch=None):
        with self.reader_session() as reader:
            if session_epoch is not None and session_epoch != self._session_epoch:
                raise DatabaseReadError("account_binding_changed", "当前会话查询期间数据库账号绑定已变化")
            reader.refresh()
            contact = reader.get_contact_by_conversation_id(str(conversation_id))
            if contact is None:
                raise DatabaseReadError("conversation_not_found", "数据库中没有该稳定会话身份")
            return reader.read_history(contact["username"], limit)

    def validate_event(self, event):
        with self.reader_session() as reader:
            if event.account_id != reader.account_id or not event.source_message_id or not event.source_stream_id:
                return False, "数据库消息身份与绑定账号不一致"
            checkpoint = self.get_checkpoint_store().get_source_checkpoint(reader.account_id, event.source_stream_id)
            return reader.validate_native_event(event, checkpoint)

    def _reset_for_relogin(self):
        old_reader = self._reader
        self._reader = None
        if old_reader is not None:
            self._cleanup_readers.append(old_reader)
        self._cleanup_retired_readers()
        self._session_epoch += 1
        self._boot_highwaters = None
        self._startup_unread_bounds = {}
        self._startup_unread_allowed = False
        # 丢失 ACK 的已提交批次由持久游标覆盖；未提交行按新高水位补历史。
        self._pending_batch = None

    def _cleanup_retired_readers(self):
        """清理失败的读取器保留到后续关闭或恢复时重试，不泄露异常细节。"""
        pending = []
        for reader in self._cleanup_readers:
            try:
                reader.close()
            except Exception:
                pending.append(reader)
        self._cleanup_readers = pending
        if pending:
            logger.warning("数据库快照清理失败：code=snapshot_cleanup_failed pending_readers=%s", len(pending))

    def status(self, **extra):
        with self._lock:
            result = dict(self._last_status)
            for transient in ("source_batch", "redelivery", "error"):
                result.pop(transient, None)
            if self._reader is not None:
                result.update(self._reader.status())
            if self._closed.is_set():
                result.update(db_read_healthy=False, db_read_error_code="source_closed")
            result.update(db_cleanup_error_code="snapshot_cleanup_failed" if self._cleanup_readers else "",
                          db_cleanup_pending=len(self._cleanup_readers))
            result.update(extra)
            self._last_status = result
            return result

    def observe_events(self):
        with self._lock:
            try:
                reader, ledger = self.get_reader(), self.get_checkpoint_store()
                reader.refresh()
                highwaters = reader.get_highwaters()
                if self._pending_batch is not None:
                    batch = self._pending_batch
                    for checkpoint in batch.checkpoints:
                        high = highwaters.get(checkpoint.stream_id)
                        if high is None or (checkpoint.generation and checkpoint.generation != high.get("generation")):
                            raise DatabaseReadError("source_generation_changed", "待接收批次的来源库已替换，暂停交付")
                    return self.status(source_batch=batch, redelivery=True), [r.event for r in batch.records if r.event]
                checkpoints = ledger.get_source_checkpoints(reader.account_id)
                if self._boot_highwaters is None:
                    unread_bounds = {}
                    if (not ledger.source_account_initialized(reader.account_id) and self._startup_unread_allowed and
                            self.config.get("process_startup_unread_messages", True)):
                        unread = getattr(reader, "startup_unread_boundaries", None)
                        if callable(unread):
                            unread_bounds = unread(highwaters)
                    # 本次启动的全部流和账号标记必须同事务完成。失败不固定
                    # 内存高水位，下次轮询或重启重新建立完整首次基线。
                    checkpoints = ledger.initialize_source_account(
                        reader.account_id, highwaters, startup_unread_bounds=unread_bounds)
                    self._boot_highwaters = dict(highwaters)
                    self._startup_unread_bounds = unread_bounds
                elif any(stream_id not in checkpoints for stream_id in highwaters):
                    checkpoints = ledger.initialize_source_account(reader.account_id, highwaters)
                batch = reader.poll_batch(checkpoints, self._boot_highwaters)
                self._pending_batch = batch
                return self.status(**({"source_batch": batch} if batch else {})), (
                    [r.event for r in batch.records if r.event] if batch else [])
            except Exception as exc:
                # SQLite 异常可能含正文，只公开稳定错误码。
                code = getattr(exc, "code", "database_read_failed")
                if code in {"login_process_changed", "account_binding_changed", "login_account_changed"}:
                    self._reset_for_relogin()
                return self.status(db_read_healthy=False, db_read_stale=True,
                                   db_read_error_code=code, error=f"数据库读取暂停：{code}"), []

    def acknowledge_events(self, event_ids):
        with self._lock:
            if self._pending_batch and self._pending_batch.batch_id in event_ids:
                self._pending_batch = None

    def wait_for_changes(self, stop_event):
        interval = max(0.1, float(self.config.get("db_poll_interval_seconds", 1.0)))
        return "stopped" if stop_event.wait(interval) or self._closed.is_set() else "database_poll"

    def close(self):
        with self._lifecycle_lock:
            self._closed.set()
            with self._lock:
                if not self._closed_cleaned:
                    self._reset_for_relogin()
                    self._closed_cleaned = True
                elif self._cleanup_readers:
                    self._cleanup_retired_readers()

    def resume(self):
        """通道明确重新启动后重建读取器，并按持久游标补历史。"""
        with self._lifecycle_lock, self._lock:
            self._cleanup_retired_readers()
            self._closed.clear()
            self._closed_cleaned = False
            self._last_status.update(db_read_healthy=False, db_read_stale=False, db_read_error_code="")
