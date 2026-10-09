"""组合层的账号与会话绑定：比较数据库身份与公开桌面观察结果。"""

from __future__ import annotations

import threading

from channel.wechat_desktop.contracts import ConversationTarget, TargetResolution, TargetStatus
from channel.wechat_desktop.conversation import conversation_titles_match
from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.send_control import SendNotSubmitted


class DatabaseUiaTargetBinder:
    def __init__(self, source, gateway_provider):
        self._source = source
        self._gateway_provider = gateway_provider
        self._lock = threading.RLock()
        self._bindings = {}
        self._session_epoch = source.session_epoch
        self._identity_verification = ""

    def _sync_session_epoch(self):
        if self._session_epoch != self._source.session_epoch:
            self._bindings.clear()
            self._identity_verification = ""
            self._session_epoch = self._source.session_epoch

    @property
    def identity_verification(self):
        return self._identity_verification if self._session_epoch == self._source.session_epoch else ""

    @staticmethod
    def _verify_account(reader, gateway):
        observed_pid, info = gateway.inspect_account()
        expected_pid = getattr(reader.binding, "pid", None)
        if not expected_pid or observed_pid != expected_pid:
            raise DatabaseReadError("account_process_mismatch", "微信窗口进程与数据库绑定不一致")
        expected_wxid = getattr(reader.binding, "wxid", "")
        observed_wxid = getattr(info, "wx_id", "")
        owner_contact = reader.get_contact_by_username(expected_wxid) or {}
        allowed_ids = {value for value in (expected_wxid, owner_contact.get("alias", "")) if value}
        if observed_wxid and allowed_ids and observed_wxid not in allowed_ids:
            raise DatabaseReadError("account_identity_mismatch", "微信窗口账号与数据库绑定不一致")

    @staticmethod
    def _database_target(reader, conversation):
        key = str(conversation or "").strip()
        contact = reader.get_contact_by_conversation_id(key)
        if contact:
            return contact, None
        if key.startswith("db-session:"):
            return None, TargetResolution(TargetStatus.STALE, reason="数据库会话身份已失效或属于其他账号")
        matches = reader.match_contacts(key)
        if len(matches) > 1:
            return None, TargetResolution(TargetStatus.AMBIGUOUS, reason="多个数据库联系人具有相同显示名")
        if not matches:
            return None, TargetResolution(TargetStatus.NOT_FOUND, reason="数据库中没有该会话")
        return matches[0], None

    def _require_epoch(self, session_epoch):
        if session_epoch is not None and session_epoch != self._source.session_epoch:
            raise DatabaseReadError("account_binding_changed", "发送期间数据库账号绑定已变化")

    def resolve_target(self, conversation, *, session_epoch=None):
        try:
            # 无效数据库身份在创建桌面组件前拒绝。
            with self._source.reader_session() as reader:
                self._require_epoch(session_epoch)
                reader.refresh()
                contact, failure = self._database_target(reader, conversation)
                if failure:
                    return failure
            gateway = self._gateway_provider()
            # 锁顺序固定为 UI 租约 -> 绑定 -> 来源，发送段复核也沿用此顺序。
            with gateway.operation(), self._lock, self._source.reader_session() as reader:
                self._require_epoch(session_epoch)
                self._sync_session_epoch()
                reader.refresh()
                contact, failure = self._database_target(reader, conversation)
                if failure:
                    return failure
                self._verify_account(reader, gateway)
                duplicates = [item for item in reader.match_contacts(contact["display_name"])
                              if conversation_titles_match(item["display_name"], contact["display_name"])]
                if len(duplicates) != 1:
                    return TargetResolution(TargetStatus.AMBIGUOUS, reason="数据库显示名无法唯一绑定微信界面")
                matches = [row for row in gateway.list_conversations()
                           if conversation_titles_match(row.conversation_title, contact["display_name"])]
                if len(matches) > 1:
                    return TargetResolution(TargetStatus.AMBIGUOUS, reason="微信界面存在多个同名会话")
                if not matches:
                    return TargetResolution(TargetStatus.NOT_FOUND, reason="目标会话当前未出现在微信会话列表")
                row = matches[0]
                if not row.runtime_id:
                    return TargetResolution(TargetStatus.STALE, reason="目标会话缺少稳定 RuntimeId")
                cid = contact["conversation_id"]
                binding = (row.runtime_id, row.conversation_title, reader.account_id, reader.binding.pid)
                previous = self._bindings.get(cid)
                if previous and previous != binding:
                    return TargetResolution(TargetStatus.STALE, reason="目标会话绑定已变化，需要重启后重新绑定")
                gateway.bind_target(cid, row)
                self._bindings[cid] = binding
                self._identity_verification = "display_name"
                return TargetResolution(TargetStatus.RESOLVED,
                                        ConversationTarget(cid, contact["display_name"], contact["is_group"]),
                                        "display_name_verified; native_identity_unavailable")
        except Exception as exc:
            return TargetResolution(TargetStatus.STALE, reason=getattr(exc, "code", "target_resolution_failed"))

    def require_target(self, conversation, *, session_epoch=None, authorized_target=None):
        """每个发送 UI 段开始前重验；失败明确表示本段尚未提交。"""
        result = self.resolve_target(conversation, session_epoch=session_epoch)
        if result.status != TargetStatus.RESOLVED or result.target is None:
            raise SendNotSubmitted(result.reason)
        if authorized_target is not None and result.target != authorized_target:
            raise SendNotSubmitted("authorized_target_changed")
        return result.target

    def current_conversation(self):
        gateway = self._gateway_provider()
        with gateway.operation(), self._lock, self._source.reader_session() as reader:
            self._sync_session_epoch()
            reader.refresh()
            self._verify_account(reader, gateway)
            header = gateway.read_current_title()
            matches = [item for item in reader.match_contacts(header.title)
                       if conversation_titles_match(item["display_name"], header.title)]
            if len(matches) != 1:
                raise DatabaseReadError("conversation_ambiguous" if matches else "conversation_not_found",
                                        "当前会话标题无法唯一对应数据库联系人")
            return matches[0]["conversation_id"], self._source.session_epoch
