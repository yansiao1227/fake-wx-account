"""PR #3 发送来源、推理上下文和当前会话策略回归；只操作合成数据库。"""

import sqlite3
from dataclasses import replace

import pytest

from bridge.context import Context
from bridge.reply import Reply, ReplyType
from channel.wechat_desktop.contracts import ConversationTarget, SendResult, SendStatus, TargetResolution, TargetStatus
from channel.wechat_desktop.hybrid import WechatDatabaseBackend
from channel.wechat_desktop.models import WechatDesktopMessage
from channel.wechat_desktop.pipeline.agent_reply import AgentReplyCoordinator
from channel.wechat_desktop.pipeline.fifo_queue import ReplyQueueItem
from channel.wechat_desktop.storage.store import WechatDesktopStore
from .test_contracts import make_channel
from .test_db_backend import Gateway
from .test_db_reader import add_message, make_reader, table


@pytest.fixture
def native_reply(tmp_path):
    reader, talker = make_reader(tmp_path)
    store = WechatDesktopStore(str(tmp_path / "send-guards.sqlite3"))
    gateway = Gateway()
    backend = WechatDatabaseBackend({}, db_reader=reader, store=store, uia_gateway=gateway)
    backend.observe_events()
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="上文材料", created=1)
    add_message(cache, talker, 2, content="当前问题", created=2)
    observation, events = backend.observe_events()
    store.receive_source_batch(observation["source_batch"])
    backend.acknowledge_events([observation["source_batch"].batch_id])
    previous, event = events
    event.history = [{"content": previous.content, "source_message_id": previous.source_message_id}]
    event.task.context_source_events = [previous]
    channel = make_channel(store, backend)
    channel.config.update(auto_send_images=True)
    context = {"msg": WechatDesktopMessage(event), "receiver": event.conversation_id,
               "isgroup": False, "wechat_desktop_source_type": "private",
               "wechat_desktop_source_event_ids": [event.event_id]}
    yield channel, reader, talker, cache, previous, event, context, gateway
    store._get_connection().close()


def change_source(cache, talker, local_id, change="delete"):
    if change == "generation":
        cache.status = replace(cache.status, generation="different-generation")
    else:
        with sqlite3.connect(cache.path) as connection:
            if change == "delete":
                connection.execute(f'DELETE FROM "{table(talker)}" WHERE local_id=?', (local_id,))
            else:
                connection.execute(f'UPDATE "{table(talker)}" SET message_content=? WHERE local_id=?',
                                   ("材料已更改", local_id))
    cache.changed = True


def send_notice(channel, event, context, kind):
    item = ReplyQueueItem(event=event, source_event_ids=[event.event_id], token="")
    if kind == "tool":
        return channel._send_agent_tool_notice(context, {"tool_name": "web_search"})
    if kind == "attachment":
        # 只更改展示字段，原生签名和来源 ID 保持为读取时的快照。
        event.reference = {"content_type": "image", "file_path": "synthetic-image.png"}
        return channel._send_deferred_attachment_notice(item)
    if kind == "failure":
        return channel._send_agent_failure_notice(context=context)
    return channel._send_attachment_reference_prompt(item)


@pytest.mark.parametrize("kind", [ReplyType.TEXT, ReplyType.IMAGE, ReplyType.IMAGE_URL])
@pytest.mark.parametrize("change", ["delete", "replace", "generation"])
def test_final_reply_rejects_context_changed_during_reasoning(native_reply, kind, change):
    channel, _, talker, cache, _, _, context, gateway = native_reply
    change_source(cache, talker, 1, change)
    submissions = []
    channel._deliver = lambda *args, **kwargs: submissions.append(args) or SendResult(SendStatus.SENT)

    channel._send_reply_impl(Reply(kind, "根据上文生成的结果"), context)

    assert submissions == [] and gateway.client.ui_calls == []
    assert context["wechat_desktop_queue_terminal"] == "skipped"


@pytest.mark.parametrize("kind", ["tool", "attachment", "failure", "reference"])
@pytest.mark.parametrize("local_id", [1, 2])
def test_every_notice_revalidates_primary_and_context_sources(native_reply, kind, local_id):
    channel, _, talker, cache, _, event, context, gateway = native_reply
    change_source(cache, talker, local_id)
    submissions = []
    channel._deliver = lambda *args, **kwargs: submissions.append(args) or SendResult(SendStatus.SENT)

    result = send_notice(channel, event, context, kind)

    assert result in (False, "skipped")
    assert submissions == [] and gateway.client.ui_calls == []
    assert not event.task.failure_notice_sent


@pytest.mark.parametrize("kind", ["tool", "attachment", "failure", "reference"])
def test_valid_notice_preserves_authorized_current_target(native_reply, kind):
    channel, _, _, _, _, event, context, _ = native_reply
    submissions = []
    channel._deliver = lambda *args, **kwargs: submissions.append((args, kwargs)) or SendResult(SendStatus.SENT)

    assert send_notice(channel, event, context, kind) in (True, "completed")

    assert len(submissions) == 1
    args, kwargs = submissions[0]
    assert args[0] == event.conversation_id
    assert kwargs["policy_target"] == "Synthetic"
    assert kwargs["authorized_target"] == ConversationTarget(event.conversation_id, "Synthetic", False)
    assert kwargs["source_event_ids"] == [event.event_id]


@pytest.mark.parametrize("kind", [ReplyType.TEXT, ReplyType.IMAGE, ReplyType.IMAGE_URL,
                                  "tool", "attachment", "failure", "reference"])
@pytest.mark.parametrize("blocked_name", ["新名称", "Synthetic"])
def test_current_contact_blacklist_uses_latest_name(native_reply, kind, blocked_name):
    channel, reader, talker, _, _, event, context, gateway = native_reply
    contacts = reader.caches["contact/contact.db"]
    with sqlite3.connect(contacts.path) as connection:
        connection.execute("UPDATE contact SET remark=? WHERE username=?", ("新名称", talker))
    contacts.changed = True
    channel.config["auto_reply_private_blacklist"] = [blocked_name]
    submissions = []
    channel._deliver = lambda *args, **kwargs: submissions.append((args, kwargs)) or SendResult(SendStatus.SENT)

    if isinstance(kind, ReplyType):
        channel._send_reply_impl(Reply(kind, "结果"), context)
    else:
        send_notice(channel, event, context, kind)

    if blocked_name == "新名称":
        assert submissions == [] and gateway.client.ui_calls == []
    else:
        assert len(submissions) == 1
        args, kwargs = submissions[0]
        assert args[0] == event.conversation_id
        assert kwargs["policy_target"] == "新名称"
        assert kwargs["authorized_target"] == ConversationTarget(event.conversation_id, "新名称", False)


@pytest.mark.parametrize("kind", [ReplyType.TEXT, ReplyType.IMAGE, "tool", "failure", "reference"])
def test_current_backend_group_type_controls_policy(native_reply, kind):
    channel, _, _, _, _, event, context, _ = native_reply
    channel._driver.resolve_send_target = lambda identity: TargetResolution(
        TargetStatus.RESOLVED, ConversationTarget(event.conversation_id, "Synthetic", True))
    channel.config["auto_reply_group_blacklist"] = ["Synthetic"]
    submissions = []
    channel._deliver = lambda *args, **kwargs: submissions.append(args) or SendResult(SendStatus.SENT)

    if isinstance(kind, ReplyType):
        channel._send_reply_impl(Reply(kind, "结果"), context)
    else:
        send_notice(channel, event, context, kind)

    assert submissions == []


def dispatch_to_capture(channel, event):
    captured = []
    channel._compose_context = lambda kind, content, **kwargs: Context(kind, content, kwargs)
    channel.produce = captured.append
    assert AgentReplyCoordinator(channel).dispatch(event)
    assert len(captured) == 1
    return captured[0]


def test_dispatch_prunes_invalid_sources_and_keeps_valid_reply_sendable(native_reply):
    channel, _, talker, cache, _, event, _, _ = native_reply
    change_source(cache, talker, 1)

    context = dispatch_to_capture(channel, event)

    assert "上文材料" not in context.content
    assert event.task.context_source_events == []
    assert context["wechat_desktop_context_source_events"] == []
    submissions = []
    channel._deliver = lambda *args, **kwargs: submissions.append(args) or SendResult(SendStatus.SENT)
    channel._send_reply_impl(Reply(ReplyType.TEXT, "仅根据当前消息回复"), context)
    assert len(submissions) == 1


def test_dispatch_snapshot_cannot_be_erased_after_context_is_used(native_reply):
    channel, _, talker, cache, previous, event, _, _ = native_reply
    context = dispatch_to_capture(channel, event)
    assert "上文材料" in context.content
    assert context["wechat_desktop_context_source_events"][0] is not previous
    event.task.context_source_events.clear()
    change_source(cache, talker, 1)
    submissions = []
    channel._deliver = lambda *args, **kwargs: submissions.append(args) or SendResult(SendStatus.SENT)

    channel._send_reply_impl(Reply(ReplyType.TEXT, "结果"), context)

    assert submissions == [] and context["wechat_desktop_queue_terminal"] == "skipped"


@pytest.mark.parametrize("kind", [ReplyType.TEXT, ReplyType.IMAGE, "tool", "failure"])
def test_missing_primary_event_never_authorizes_automatic_reply_or_notice(native_reply, kind):
    channel, _, _, _, _, event, context, gateway = native_reply
    context["msg"] = None
    submissions = []
    channel._deliver = lambda *args, **kwargs: submissions.append(args) or SendResult(SendStatus.SENT)

    if isinstance(kind, ReplyType):
        channel._send_reply_impl(Reply(kind, "结果"), context)
    else:
        send_notice(channel, event, context, kind)

    assert submissions == [] and gateway.client.ui_calls == []
    assert context["wechat_desktop_queue_terminal"] == "skipped"


@pytest.mark.parametrize("kind", [ReplyType.TEXT, "failure"])
def test_target_change_after_policy_authorization_is_refused_by_backend(native_reply, kind):
    channel, reader, talker, _, _, event, context, gateway = native_reply

    def rename_before_submit():
        contacts = reader.caches["contact/contact.db"]
        with sqlite3.connect(contacts.path) as connection:
            connection.execute("UPDATE contact SET remark='Changed' WHERE username=?", (talker,))
        contacts.changed = True

    # Gateway 替身只实现文本；提示最终使用同一提交验证接口。
    gateway.send_interim_text = gateway.send_text
    gateway.before_send = rename_before_submit
    if isinstance(kind, ReplyType):
        channel._send_reply_impl(Reply(kind, "结果"), context)
    else:
        send_notice(channel, event, context, kind)

    assert gateway.client.sent == []
