"""微信桌面 queue 回归测试。"""
import pytest

from channel.wechat_desktop.pipeline.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.models import WechatDesktopEvent


@pytest.mark.parametrize("is_group", [False, True], ids=["private", "group"])
@pytest.mark.parametrize("already_running", [False, True], ids=["pending", "running"])
def test_followups_preserve_fifo_and_active_token(is_group, already_running):
    reply_queue = WechatReplyQueue()
    events = [
        WechatDesktopEvent(
            "message", "chat", "会话", str(index), "成员", "text", content,
            is_group=is_group, is_at=is_group,
        )
        for index, content in enumerate(("first", "second", "third"))
    ]
    assert reply_queue.enqueue(events[0]).action == "added"
    active = reply_queue.get() if already_running else None
    for event in events[1:]:
        assert reply_queue.enqueue(event).action == "added"
    if active is not None:
        assert reply_queue.is_active(active.token)
        assert not active.expired
        assert not active.done.is_set()
        assert active.terminal == ""
    for index, event in enumerate(events):
        item = active if index == 0 and active is not None else reply_queue.get()
        assert item.event is event
        reply_queue.finish(item, "completed")
    assert reply_queue.status()["queue_depth"] == 0
    assert reply_queue.status()["queue_completed"] == 3
    assert reply_queue._queue.unfinished_tasks == 0


def test_reply_queue_is_global_fifo_and_rejects_duplicates():
    reply_queue = WechatReplyQueue()
    second = WechatDesktopEvent(
        "message", "b", "Bob", "b", "Bob", "text", "second", observed_at=2
    )
    first = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "first", observed_at=1
    )
    assert reply_queue.enqueue(first)
    assert reply_queue.enqueue(second)
    assert reply_queue.enqueue(first).action == "duplicate"
    first_item = reply_queue.get()
    assert first_item.event.content == "first"
    reply_queue.finish(first_item, "completed")
    second_item = reply_queue.get()
    assert second_item.event.content == "second"
    reply_queue.finish(second_item, "completed")
    assert reply_queue.status()["queue_completed"] == 2
    assert "queue_approval" not in reply_queue.status()


def test_reply_queue_captures_enqueue_time_context_independently():
    reply_queue = WechatReplyQueue()
    event = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "待回复消息",
        history=[{"content": "入队时的上文", "content_type": "text"}],
    )

    assert reply_queue.enqueue(event)
    event.history[0]["content"] = "入队后被修改"
    event.history.append({"content": "消费前出现的新消息"})

    item = reply_queue.get()

    assert item.context_history == [
        {"content": "入队时的上文", "content_type": "text"}
    ]
    item.restore_context()
    assert item.event.content == "待回复消息"
    assert item.event.history == [
        {"content": "入队时的上文", "content_type": "text"}
    ]


def test_reply_queue_keeps_global_fifo_when_new_followups_arrive():
    reply_queue = WechatReplyQueue()
    first = WechatDesktopEvent(
        "message", "g", "项目群", "a", "成员A", "text", "@小牛 1", is_group=True
    )
    second = WechatDesktopEvent(
        "message", "g", "项目群", "b", "成员B", "text", "@小牛 2", is_group=True
    )
    third = WechatDesktopEvent(
        "message", "g", "项目群", "c", "成员C", "text", "@小牛 3", is_group=True
    )
    other = WechatDesktopEvent(
        "message", "p", "Bob", "p", "Bob", "text", "hello"
    )
    reply_queue.enqueue(first)
    active = reply_queue.get()
    reply_queue.enqueue(other)
    reply_queue.enqueue(second)
    reply_queue.finish(active, "completed")
    active = reply_queue.get()
    assert active.event is other
    reply_queue.enqueue(third)
    reply_queue.finish(active, "completed")

    second_item = reply_queue.get()

    assert second_item.event is second
    reply_queue.finish(second_item, "completed")
    assert reply_queue.get().event is third


def test_reply_queue_can_clear_global_and_active_conversation_pending_work():
    reply_queue = WechatReplyQueue()
    active_event = WechatDesktopEvent(
        "message", "g", "项目群", "a", "成员A", "text", "@小牛 1", is_group=True
    )
    local_event = WechatDesktopEvent(
        "message", "g", "项目群", "b", "成员B", "text", "@小牛 2", is_group=True
    )
    global_event = WechatDesktopEvent(
        "message", "p", "Bob", "p", "Bob", "text", "hello"
    )
    reply_queue.enqueue(active_event)
    active = reply_queue.get()
    reply_queue.enqueue(local_event)
    reply_queue.enqueue(global_event)

    discarded = reply_queue.clear_pending()

    assert set(discarded) == {local_event.event_id, global_event.event_id}
    assert reply_queue.status()["queue_depth"] == 0
    assert reply_queue.is_active(active.token) is True
    reply_queue.finish(active, "failed")
    assert reply_queue.get(timeout=0.01) is None


def test_expired_queue_token_rejects_late_result():
    reply_queue = WechatReplyQueue()
    event = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "hello"
    )
    reply_queue.enqueue(event)
    item = reply_queue.get()
    assert reply_queue.is_active(item.token)
    reply_queue.expire(item.token)
    assert not reply_queue.is_active(item.token)
    reply_queue.signal(item.token, "completed")
    assert item.terminal == ""
