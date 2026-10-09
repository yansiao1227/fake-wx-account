"""数据库接收与 UIA 操作的组合后端；跨来源协调留在通道层。"""

from __future__ import annotations

from dataclasses import replace
import threading

from channel.wechat_desktop.backend import WechatDesktopBackend
from channel.wechat_desktop.binding import DatabaseUiaTargetBinder
from channel.wechat_desktop.contracts import SendResult, SendStatus, TargetStatus
from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.db.source import WechatDatabaseSource
from channel.wechat_desktop.models import ReplyTargetValidation, WechatHistoryReadError


class WechatDatabaseBackend(WechatDesktopBackend):
    capabilities = {"background_observation": True, "database_history": True, "contact_search": True}

    def __init__(self, config, *, db_reader=None, store=None, client=None, shell_hook=None,
                 uia_gateway=None, uia_driver=None):
        self.config = config
        self._store = store
        self._client = client
        # 旧调用方从默认 UIA 后端取得公开 gateway，不访问驱动内部状态。
        if uia_driver is not None and uia_gateway is None:
            uia_gateway = uia_driver.gateway
        self._gateway = uia_gateway
        self._actions_lock = threading.RLock()
        self._closed = False
        self.source = WechatDatabaseSource(config, reader=db_reader, checkpoint_store=store,
                                           checkpoint_store_provider=self._ledger)
        self._binder = DatabaseUiaTargetBinder(self.source, self._actions)

    def _ledger(self):
        if self._store is None:
            from channel.wechat_desktop.storage.service import get_wechat_desktop_service
            self._store = get_wechat_desktop_service().store
        return self._store

    @property
    def uia_initialized(self):
        return self._gateway is not None

    def _actions(self):
        with self._actions_lock:
            if self._closed:
                raise DatabaseReadError("backend_closed", "微信组合后端已关闭")
            if self._gateway is None:
                from channel.wechat_desktop.uia.gateway import WechatUiaGateway
                self._gateway = WechatUiaGateway(self.config, client=self._client)
            return self._gateway

    def _status(self, status=None):
        return {**(self.source.status() if status is None else status),
                "mode": "db_uia", "desktop_backend": "db_uia", "automation_backend": "db_uia",
                "uia_available": self.uia_initialized and not self._closed,
                "target_identity_verification": self._binder.identity_verification}

    def observe_events(self):
        status, events = self.source.observe_events()
        for event in events:
            self._mark_attachment_capability(event)
        return self._status(status), events

    def acknowledge_events(self, event_ids):
        self.source.acknowledge_events(event_ids)

    def wait_for_changes(self, stop_event):
        return self.source.wait_for_changes(stop_event)

    def ensure_foreground(self):
        return self._actions().ensure_foreground()

    def resolve_target(self, conversation):
        return self._binder.resolve_target(conversation)

    def validate_reply_target(self, event):
        try:
            valid, reason = self.source.validate_event(event)
            if not valid:
                return ReplyTargetValidation(False, reason)
            result = self.resolve_target(event.conversation_id)
            return ReplyTargetValidation(result.status == TargetStatus.RESOLVED, result.reason)
        except Exception as exc:
            return ReplyTargetValidation(False, getattr(exc, "code", "source_validation_failed"))

    def _send(self, method, conversation, payload, *, authorized_target=None):
        epoch = self.source.session_epoch
        result = self.resolve_target(conversation)
        if result.status != TargetStatus.RESOLVED or result.target is None:
            return SendResult(SendStatus.NOT_SENT, result.reason)
        if authorized_target is not None and result.target != authorized_target:
            return SendResult(SendStatus.NOT_SENT, "authorized_target_changed")
        if epoch != self.source.session_epoch:
            return SendResult(SendStatus.NOT_SENT, "account_binding_changed")
        cid = result.target.conversation_id
        response = SendResult.from_backend(getattr(self._actions(), method)(
            cid, payload, validate=lambda: self._binder.require_target(
                cid, session_epoch=epoch, authorized_target=authorized_target)))
        return replace(response, accepted_by="db_uia", observation={**response.observation, **self._status()})

    def send_text(self, conversation, text, *, authorized_target=None):
        return self._send("send_text", conversation, text, authorized_target=authorized_target)

    def send_interim_text(self, conversation, text, *, authorized_target=None):
        return self._send("send_interim_text", conversation, text, authorized_target=authorized_target)

    def send_image(self, conversation, image_path, *, authorized_target=None):
        return self._send("send_image", conversation, image_path, authorized_target=authorized_target)

    @staticmethod
    def _mark_attachment_capability(event):
        # 原生消息尚不能与 UIA 气泡可靠关联，不能靠正文或坐标猜测附件归属。
        if event.content_type not in {"text", "share_card"}:
            event.attachment_status = "uia_identity_unavailable"
            if event.reference:
                event.reference["degraded"] = True

    def materialize_event(self, event, *, before_share_fetch=None):
        self._mark_attachment_capability(event)
        return event, 0

    def search_contacts(self, query="", limit=20):
        return {"contacts": self.source.search_contacts(query, limit), "source": "wechat_database"}

    def read_chat_history(self, conversation_id, limit=20):
        try:
            return self.source.read_chat_history(conversation_id, limit)
        except DatabaseReadError as exc:
            raise WechatHistoryReadError(exc.code, str(exc)) from exc

    def read_current_chat_history(self, limit=20):
        try:
            conversation_id, epoch = self._binder.current_conversation()
            return self.source.read_chat_history(conversation_id, limit, session_epoch=epoch)
        except DatabaseReadError as exc:
            raise WechatHistoryReadError(exc.code, str(exc)) from exc

    def close(self):
        with self._actions_lock:
            if self._closed:
                return
            self._closed = True
            if self._gateway is not None:
                self._gateway.close()
            self.source.close()

    def resume(self):
        with self._actions_lock:
            self.source.resume()
            if self._gateway is not None:
                self._gateway.resume()
            self._closed = False
