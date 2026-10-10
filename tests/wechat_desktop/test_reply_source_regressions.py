"""失败/进度通知与聚合回复均复核原生来源；不操作真实微信。"""

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bridge.reply import Reply, ReplyType
from channel.wechat_desktop.contracts import SendResult, SendStatus
from channel.wechat_desktop.models import ReplyTargetValidation, WechatDesktopMessage
from channel.wechat_desktop.pipeline.agent_reply import AgentReplyCoordinator
from .test_delivery_regressions import _reply_setup, store
from .test_db_reader import add_message, table


def _invalidate(cache, talker, change, local_id=1):
    if change == "generation":
        cache.status = replace(cache.status, generation="replacement-generation")
    else:
        with sqlite3.connect(cache.path) as connection:
            if change == "delete":
                connection.execute(f'DELETE FROM "{table(talker)}" WHERE local_id=?', (local_id,))
            else:
                connection.execute(f'UPDATE "{table(talker)}" SET message_content=? WHERE local_id=?',
                                   ("changed input", local_id))
    cache.changed = True


def _capture_submission(channel):
    sent, stages = [], []
    channel._deliver = lambda *args, **kwargs: sent.append((args, kwargs)) or SendResult(SendStatus.SENT)
    channel._mark_lifecycle = lambda ids, stage, **kwargs: stages.append(stage)
    channel.config.update(agent_failure_notice_templates=["合成失败提示"],
                          agent_tool_notice_templates=["正在调用 {tool_name}"],
                          agent_tool_notice_enabled=True)
    return sent, stages


def _invoke(channel, context, output):
    if output == "failure":
        return channel._send_agent_failure_notice(context=context)
    if output == "tool":
        return AgentReplyCoordinator(channel).send_tool_notice(context, {"tool_name": "web_search"})
    channel._send_reply_impl(Reply(output, "合成回复"), context)


@pytest.mark.parametrize("output", ["failure", "tool"])
@pytest.mark.parametrize("change", ["delete", "replace", "generation"])
def test_notices_revalidate_native_source_before_any_submission(tmp_path, store, output, change):
    _, talker, cache, channel, context = _reply_setup(tmp_path, store)
    sent, stages = _capture_submission(channel)
    _invalidate(cache, talker, change)

    assert _invoke(channel, context, output) is False

    assert sent == stages == []
    assert context["wechat_desktop_queue_terminal"] == "skipped"
    assert store._get_connection().execute(
        "SELECT count(*) FROM conversation_history WHERE direction='outgoing'").fetchone()[0] == 0
    audit = store._get_connection().execute(
        "SELECT action_type,result FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(audit) == ("agent_failure_notice" if output == "failure" else "agent_tool_notice", "stale_target")


@pytest.mark.parametrize("output", ["failure", "tool"])
def test_valid_source_notice_still_submits(tmp_path, store, output):
    _, _, _, channel, context = _reply_setup(tmp_path, store)
    sent, _ = _capture_submission(channel)

    assert _invoke(channel, context, output) is True

    assert len(sent) == 1
    assert store._get_connection().execute(
        "SELECT count(*) FROM conversation_history WHERE direction='outgoing'").fetchone()[0] == 1


@pytest.mark.parametrize("output", [ReplyType.TEXT, ReplyType.IMAGE, ReplyType.ERROR, "failure", "tool"])
@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("change", ["delete", "replace"])
def test_aggregate_reply_revalidates_earlier_source(tmp_path, store, output, deferred, change):
    reader, talker, cache, channel, context = _reply_setup(tmp_path, store)
    first = context["msg"].event
    add_message(cache, talker, 2)
    observation, events = channel._driver.observe_events()
    store.receive_source_batch(observation["source_batch"])
    channel._driver.acknowledge_events([observation["source_batch"].batch_id])
    last = events[0]
    channel._preserve_event_evidence = lambda event: None
    target = (channel._prepare_deferred_materialization([first, last]) if deferred
              else channel._materialize_batch([first, last]))
    context.update(msg=WechatDesktopMessage(target),
                   wechat_desktop_source_event_ids=target.task.source_event_ids)
    assert channel._driver.validate_reply_target(last).valid
    sent, stages = _capture_submission(channel)
    _invalidate(cache, talker, change, local_id=1)
    assert channel._driver.validate_reply_target(last).valid

    _invoke(channel, context, output)

    assert sent == stages == []
    assert context["wechat_desktop_queue_terminal"] == "skipped"
    assert store._get_connection().execute(
        "SELECT count(*) FROM conversation_history WHERE direction='outgoing'").fetchone()[0] == 0


def test_deferred_source_snapshots_survive_materialization_mutation(tmp_path, store):
    _, _, _, channel, context = _reply_setup(tmp_path, store)
    event = context["msg"].event
    target = channel._prepare_deferred_materialization([event])
    original = event.content
    channel._driver.materialize_event = lambda item: (item, 0)
    channel._preserve_event_evidence = lambda item: None
    event.content = "materialized content"
    channel._materialize_batch([event])
    checked = []
    channel._driver.validate_reply_target = lambda item: checked.append(item) or ReplyTargetValidation(True)
    _capture_submission(channel)

    _invoke(channel, context, ReplyType.TEXT)

    assert len(checked) == 1
    assert checked[0].content == original
    assert checked[0] is not target
    assert not checked[0].task.deferred_materialization_events


def test_tool_callback_on_withdrawn_source_finishes_queue_as_skipped(tmp_path, store, monkeypatch):
    import agent.protocol

    _, talker, cache, channel, context = _reply_setup(tmp_path, store)
    event = context["msg"].event
    channel._reply_queue.enqueue(event)
    item = channel._reply_queue.get()
    context["wechat_desktop_queue_token"] = item.token
    sent, _ = _capture_submission(channel)
    cancelled = []
    monkeypatch.setattr(agent.protocol, "get_cancel_registry",
                        lambda: SimpleNamespace(cancel_request=cancelled.append))
    callback = AgentReplyCoordinator(channel).make_event_callback(context)
    _invalidate(cache, talker, "delete")

    callback({"type": "tool_execution_start", "data": {"tool_name": "web_search", "tool_call_id": "search-1"}})

    assert item.done.is_set() and item.terminal == "skipped"
    assert cancelled == [event.event_id]
    assert sent == []
    channel._send_reply_impl(Reply(ReplyType.TEXT, "迟到回复"), context)
    channel._fail_callback(event.conversation_id, RuntimeError("合成模型异常"), context=context)
    assert item.terminal == context["wechat_desktop_queue_terminal"] == "skipped"
    assert sent == []
    channel._reply_queue.finish(item, "skipped")
    channel._on_reply_worker_finish(item, "skipped")
    assert store.event_state(event.event_id)["state"] == "skipped"
    assert store._get_connection().execute(
        "SELECT count(*) FROM conversation_history WHERE direction='outgoing'").fetchone()[0] == 0


@pytest.mark.parametrize("terminal", ["failed", "timeout"])
def test_event_only_failure_notice_preserves_skipped_worker_terminal(tmp_path, store, monkeypatch, terminal):
    _, talker, cache, channel, context = _reply_setup(tmp_path, store)
    event = context["msg"].event
    channel._reply_queue.enqueue(event)
    item = channel._reply_queue.get()
    sent, stages = _capture_submission(channel)
    channel._dispatch_message = lambda *args: True
    monkeypatch.setattr(AgentReplyCoordinator, "wait_for_reply", lambda self, item: terminal)
    _invalidate(cache, talker, "delete")

    result = channel._process_reply_item(item)

    assert result == "skipped"
    assert event.task.source_invalid
    assert sent == []
    assert "send_started" not in stages
    audit = store._get_connection().execute(
        "SELECT action_type,result FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(audit) == ("reply_queue", "skipped")


def test_deferred_preflight_revalidates_all_sources_before_materialization(tmp_path, store):
    _, talker, cache, channel, context = _reply_setup(tmp_path, store)
    first = context["msg"].event
    add_message(cache, talker, 2)
    observation, events = channel._driver.observe_events()
    store.receive_source_batch(observation["source_batch"])
    channel._driver.acknowledge_events([observation["source_batch"].batch_id])
    last = events[0]
    last.reference = {"content_type": "image", "file_path": "synthetic.png", "resolved": False}
    target = channel._prepare_deferred_materialization([first, last])
    channel._reply_queue.enqueue(target)
    item = channel._reply_queue.get()
    sent, stages = _capture_submission(channel)
    channel._materialize_batch = lambda *args, **kwargs: pytest.fail("失效来源仍被物化")
    channel._dispatch_message = lambda *args: pytest.fail("失效来源仍启动 Agent")
    _invalidate(cache, talker, "delete", local_id=1)

    result = channel._process_reply_item(item)

    assert result == "skipped"
    assert item.done.is_set() and item.terminal == "skipped"
    assert sent == []
    assert "send_started" not in stages
