"""微信桌面 queue 回归测试。"""
from channel.wechat_desktop.pipeline.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.models import WechatDesktopEvent


def test_reply_queue_is_global_fifo_and_rejects_duplicates():
    reply_queue = WechatReplyQueue()
    second = WechatDesktopEvent(
        "message", "b", "Bob", "b", "Bob", "text", "second", observed_at=2
    )
    first = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "first", observed_at=1
    )
    assert reply_queue.enqueue_many([second, first, first]) == 2
    first_item = reply_queue.get()
    assert first_item.event.content == "first"
    reply_queue.finish(first_item, "completed")
    second_item = reply_queue.get()
    assert second_item.event.content == "second"
    reply_queue.finish(second_item, "completed")
    assert reply_queue.status()["queue_completed"] == 2
    assert "queue_approval" not in reply_queue.status()


def test_reply_queue_appends_pending_target_from_same_conversation():
    reply_queue = WechatReplyQueue()
    old = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "old"
    )
    new = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "new"
    )

    assert reply_queue.enqueue(old).action == "added"
    assert reply_queue.enqueue(new).action == "added"
    first = reply_queue.get()
    assert first.event is old
    reply_queue.finish(first, "completed")
    assert reply_queue.get().event is new


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


def test_reply_queue_private_followup_does_not_expire_active():
    reply_queue = WechatReplyQueue()
    old = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "old"
    )
    new = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "new"
    )
    reply_queue.enqueue(old)
    active = reply_queue.get()

    result = reply_queue.enqueue(new)

    assert result.action == "added"
    assert active.expired is False
    assert active.terminal == ""
    assert reply_queue.is_relevant(active.token, "a") is True
    assert reply_queue.is_relevant(active.token, "b") is True
    reply_queue.finish(active, "completed")
    assert reply_queue.get().event.content == "new"
    assert reply_queue.status()["queue_superseded"] == 0


def test_reply_queue_preserves_multiple_private_followups():
    reply_queue = WechatReplyQueue()
    first = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "first"
    )
    second = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "second"
    )
    third = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "third"
    )
    reply_queue.enqueue(first)
    active = reply_queue.get()

    assert reply_queue.enqueue(second).action == "added"
    assert reply_queue.enqueue(third).action == "added"
    reply_queue.finish(active, "completed")
    second_item = reply_queue.get()
    assert second_item.event is second
    reply_queue.finish(second_item, "completed")
    assert reply_queue.get().event is third


def test_reply_queue_appends_group_mentions_without_canceling_active():
    reply_queue = WechatReplyQueue()
    first = WechatDesktopEvent(
        "message", "g", "项目群", "a", "成员A", "text", "@小牛 first", is_group=True
    )
    second = WechatDesktopEvent(
        "message", "g", "项目群", "b", "成员B", "text", "@小牛 second", is_group=True
    )
    third = WechatDesktopEvent(
        "message", "g", "项目群", "c", "成员C", "text", "@小牛 third", is_group=True
    )
    reply_queue.enqueue(first)
    active = reply_queue.get()

    assert reply_queue.enqueue(second).action == "added"
    assert reply_queue.enqueue(third).action == "added"
    assert active.expired is False
    assert active.done.is_set() is False
    reply_queue.finish(active, "completed")

    next_item = reply_queue.get()
    assert next_item.event.content == "@小牛 second"
    reply_queue.finish(next_item, "completed")
    assert reply_queue.get().event.content == "@小牛 third"


def test_reply_queue_ignores_legacy_burst_limit_and_keeps_global_fifo():
    reply_queue = WechatReplyQueue(conversation_burst_limit=2)
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
