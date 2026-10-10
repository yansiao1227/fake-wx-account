"""发送服务以可信会话身份执行黑名单门禁；只使用临时数据库和发送替身。"""

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.contracts import (
    ConversationTarget,
    SendResult,
    SendStatus,
    TargetResolution,
    TargetStatus,
)
from channel.wechat_desktop.hybrid import WechatDatabaseBackend
from channel.wechat_desktop.models import HeaderInfo
from channel.wechat_desktop.pipeline.delivery import DeliveryBlocked, DeliveryService
from channel.wechat_desktop.pipeline.policy import WechatDesktopPolicy
from channel.wechat_desktop import send_control
from channel.wechat_desktop.storage.store import WechatDesktopStore
from .test_db_backend import Gateway
from .test_db_reader import make_reader


STABLE_ID = "db-session:synthetic-account:stable-id"


@pytest.fixture
def store(tmp_path):
    ledger = WechatDesktopStore(str(tmp_path / "delivery-authorization.sqlite3"))
    yield ledger
    ledger._get_connection().close()


def service_for(store, backend, **overrides):
    config = {
        "shadow_mode": False,
        "auto_send_images": True,
        "max_send_per_minute": 10,
        "max_send_per_hour": 100,
        **overrides,
    }
    return DeliveryService(
        config, WechatDesktopPolicy(config, store), backend,
        lambda: False, lambda: False, lambda token: True, store,
    )


def backend_for(resolution):
    resolved = []
    submitted = []

    def resolve(conversation):
        resolved.append(conversation)
        if isinstance(resolution, Exception):
            raise resolution
        return resolution

    def send(method, conversation, content, **kwargs):
        submitted.append((method, conversation, content, kwargs))
        return SendResult(SendStatus.SENT, submitted_chunks=1, verified_chunks=1)

    backend = SimpleNamespace(
        resolve_send_target=resolve,
        send_text=lambda *args, **kwargs: send("text", *args, **kwargs),
        send_image=lambda *args, **kwargs: send("image", *args, **kwargs),
        send_interim_text=lambda *args, **kwargs: send("interim", *args, **kwargs),
    )
    return backend, resolved, submitted


def quota_count(store):
    return store._get_connection().execute("SELECT COUNT(*) FROM rate_events").fetchone()[0]


@pytest.mark.parametrize("is_group", [False, True])
def test_resolved_current_name_and_kind_override_stale_call_metadata(store, is_group):
    target = ConversationTarget(STABLE_ID, "当前被屏蔽会话", is_group)
    backend, resolved, submitted = backend_for(TargetResolution(TargetStatus.RESOLVED, target))
    key = "auto_reply_group_blacklist" if is_group else "auto_reply_private_blacklist"
    service = service_for(store, backend, **{key: [target.display_name]})

    with pytest.raises(DeliveryBlocked):
        service.send(STABLE_ID, "不能发送", policy_target="旧会话名称", is_group=not is_group)

    assert resolved == [STABLE_ID]
    assert submitted == []
    assert quota_count(store) == 0


@pytest.mark.parametrize(
    "resolution",
    [
        TargetResolution(TargetStatus.NOT_FOUND),
        TargetResolution(TargetStatus.AMBIGUOUS),
        TargetResolution(TargetStatus.STALE),
        TargetResolution(TargetStatus.RESOLVED),
        None,
        SimpleNamespace(),
        SimpleNamespace(status=TargetStatus.RESOLVED),
        RuntimeError("可信会话解析失败"),
    ],
    ids=["missing", "ambiguous", "stale", "missing-target", "invalid-result",
         "missing-status", "missing-target-field", "exception"],
)
def test_failed_resolution_never_submits_or_reserves_quota(store, resolution):
    backend, resolved, submitted = backend_for(resolution)
    service = service_for(store, backend)

    with pytest.raises(DeliveryBlocked):
        service.send(STABLE_ID, "不能发送", policy_target="旧会话名称")

    assert resolved == [STABLE_ID]
    assert submitted == []
    assert quota_count(store) == 0


@pytest.mark.parametrize(
    "target",
    [
        ConversationTarget("", "正常名称", False),
        ConversationTarget("  ", "正常名称", False),
        ConversationTarget(None, "正常名称", False),
        ConversationTarget(123, "正常名称", False),
        ConversationTarget(STABLE_ID, "", False),
        ConversationTarget(STABLE_ID, "  ", False),
        ConversationTarget(STABLE_ID, None, False),
        ConversationTarget(STABLE_ID, 123, False),
        ConversationTarget(STABLE_ID, "正常名称", None),
        ConversationTarget(STABLE_ID, "正常名称", "group"),
        ConversationTarget(STABLE_ID, "正常名称", 1),
        SimpleNamespace(),
    ],
    ids=["empty-id", "blank-id", "null-id", "number-id", "empty-name", "blank-name",
         "null-name", "number-name", "unknown-kind", "string-kind", "number-kind",
         "missing-identity-fields"],
)
@pytest.mark.parametrize("preauthorized", [False, True])
def test_untrusted_identity_fields_never_authorize_send(store, target, preauthorized):
    backend, _, submitted = backend_for(TargetResolution(TargetStatus.RESOLVED, target))
    service = service_for(store, backend)
    kwargs = {"authorized_target": target} if preauthorized else {}

    with pytest.raises(DeliveryBlocked):
        service.send(STABLE_ID, "不能发送", policy_target="正常名称", **kwargs)

    assert submitted == []
    assert quota_count(store) == 0


@pytest.mark.parametrize("method", ["text", "image", "interim"])
def test_allowed_resolution_reaches_backend_with_authorization_and_stable_id(store, method):
    target = ConversationTarget(STABLE_ID, "允许的新名称", True)
    backend, resolved, submitted = backend_for(TargetResolution(TargetStatus.RESOLVED, target))
    service = service_for(store, backend, auto_reply_private_blacklist=[target.display_name])

    result = service.send(
        STABLE_ID, "合成发送内容", policy_target="旧名称", is_group=False,
        content_type="image" if method == "image" else "text", interim=method == "interim",
        source_event_ids=["synthetic-event"],
    )

    assert result.status == SendStatus.SENT
    assert resolved == [STABLE_ID]
    assert submitted == [(method, STABLE_ID, "合成发送内容", {"authorized_target": target})]
    assert quota_count(store) == 1


@pytest.mark.parametrize("is_group", [False, True])
def test_existing_authorization_cannot_be_bypassed_with_forged_name_or_kind(store, is_group):
    target = ConversationTarget(STABLE_ID, "被屏蔽会话", is_group)
    backend, resolved, submitted = backend_for(RuntimeError("已有授权无需再次解析"))
    key = "auto_reply_group_blacklist" if is_group else "auto_reply_private_blacklist"
    service = service_for(store, backend, **{key: [target.display_name]})

    with pytest.raises(DeliveryBlocked):
        service.send(STABLE_ID, "不能发送", policy_target="伪造允许名称", is_group=not is_group,
                     authorized_target=target)

    assert resolved == []
    assert submitted == []
    assert quota_count(store) == 0


@pytest.mark.parametrize("is_group", [False, True])
def test_blacklist_change_during_send_wait_cancels_before_submission(store, monkeypatch, is_group):
    target = ConversationTarget(STABLE_ID, "等待中的会话", is_group)
    backend, _, submitted = backend_for(TargetResolution(TargetStatus.RESOLVED, target))
    service = service_for(store, backend)
    waiting = []
    key = "auto_reply_group_blacklist" if is_group else "auto_reply_private_blacklist"

    def blacklist_while_waiting(delay):
        waiting.append(delay)
        service.config[key] = [target.display_name]

    def delayed_send(conversation, content, **kwargs):
        send_control.wait_send_delay(1)
        submitted.append(("text", conversation, content, kwargs))
        return SendResult(SendStatus.SENT, submitted_chunks=1, verified_chunks=1)

    backend.send_text = delayed_send
    monkeypatch.setattr(send_control.time, "sleep", blacklist_while_waiting)

    with pytest.raises(DeliveryBlocked):
        service.send(STABLE_ID, "不能发送", policy_target="旧名称", is_group=not is_group)

    assert waiting
    assert submitted == []
    # 已进入执行阶段才变更名单，已有额度保留，但尚未提交任何气泡。
    assert quota_count(store) == 1
    statuses = store._get_connection().execute("SELECT status FROM deliveries").fetchall()
    assert [row[0] for row in statuses] == ["not_sent"]


def test_database_group_blacklist_blocks_stale_private_metadata_before_any_uia(tmp_path, store, monkeypatch):
    reader, talker = make_reader(tmp_path, group=True)
    backend = WechatDatabaseBackend({}, db_reader=reader, store=store)
    cid = reader.conversation_id(talker)
    service = service_for(store, backend, auto_reply_group_blacklist=["Synthetic"])

    def reject_uia():
        pytest.fail("被屏蔽数据库会话不得初始化 UIA")

    monkeypatch.setattr(backend, "_actions", reject_uia)
    monkeypatch.setattr(backend._binder, "_gateway_provider", reject_uia)
    try:
        with pytest.raises(DeliveryBlocked):
            service.send(cid, "不能发送", policy_target="旧群名", is_group=False)
        assert not backend.uia_initialized
        assert quota_count(store) == 0
    finally:
        reader.close()


def test_database_rejects_name_change_after_existing_authorization(tmp_path, store):
    reader, talker = make_reader(tmp_path, group=True)
    gateway = Gateway()
    gateway.client.header_type = "group"
    backend = WechatDatabaseBackend({}, db_reader=reader, store=store, uia_gateway=gateway)
    cid = reader.conversation_id(talker)
    authorized = backend.resolve_send_target(cid).target
    service = service_for(store, backend, auto_reply_group_blacklist=["改名后被屏蔽"])
    cache = reader.caches["contact/contact.db"]
    with sqlite3.connect(cache.path) as connection:
        connection.execute("UPDATE contact SET remark=? WHERE username=?", ("改名后被屏蔽", talker))
    cache.changed = True
    gateway.client.rows = [replace(gateway.client.rows[0], conversation_title="改名后被屏蔽")]
    gateway.client.get_title = lambda: HeaderInfo("改名后被屏蔽", "group")

    try:
        result = service.send(cid, "不能发送", policy_target="Synthetic", is_group=False,
                              authorized_target=authorized)
        assert result.status == SendStatus.NOT_SENT
        assert result.message == "authorized_target_changed"
        assert gateway.client.sent == []
    finally:
        reader.close()
