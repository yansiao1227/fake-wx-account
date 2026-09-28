"""微信桌面 pipeline 回归测试。"""

import threading
import time
import queue
from dataclasses import replace
from types import SimpleNamespace
from .helpers import PipelineStoreStub
from channel.wechat_desktop.pipeline.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.models import (
    HeaderInfo,
    UiaChatMessage,
    UiaReferencedMessage,
    WechatDesktopEvent,
)
from channel.wechat_desktop.uia.driver import WechatUiaDriver
from channel.wechat_desktop.pipeline.channel import WechatDesktopChannel
from channel.wechat_desktop.pipeline.prompts import ATTACHMENT_REFERENCE_REQUIRED_REPLY
from .helpers import FakeClient, FakeHook, FakeTimer, _bare_wechat_channel, row
from channel.wechat_desktop.pipeline.lifecycle import LifecycleRecorder


def test_private_aggregation_uses_sliding_window():
    FakeTimer.instances = []
    channel = _bare_wechat_channel()
    channel.config = {
        "private_message_aggregation_min_ms": 500,
        "private_message_aggregation_max_ms": 1200,
        "private_message_aggregation_max_wait_ms": 4000,
    }
    channel._pending_private_lock = threading.RLock()
    channel._pending_private_batches = {}
    channel._private_batch_timer_factory = FakeTimer
    channel._stop_event = threading.Event()
    channel._trace = lambda *_args, **_kwargs: None
    released = []
    channel._submit_materialization = lambda events: released.append(events)
    first = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "first"
    )
    second = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "second"
    )

    channel._defer_private_event(first)
    first_timer = FakeTimer.instances[-1]
    channel._defer_private_event(second)
    second_timer = FakeTimer.instances[-1]

    first_timer.fire()
    assert released == []
    second_timer.fire()

    assert released == [[first, second]]
    assert first_timer.cancelled is True
    assert 0.5 <= second_timer.interval <= 1.2
    assert channel._pending_private_batches == {}


def test_private_aggregation_is_isolated_by_conversation_and_window():
    FakeTimer.instances = []
    channel = _bare_wechat_channel()
    channel.config = {
        "private_message_aggregation_min_ms": 500,
        "private_message_aggregation_max_ms": 1200,
        "private_message_aggregation_max_wait_ms": 4000,
    }
    channel._pending_private_lock = threading.RLock()
    channel._pending_private_batches = {}
    channel._private_batch_timer_factory = FakeTimer
    channel._stop_event = threading.Event()
    channel._trace = lambda *_args, **_kwargs: None
    released = []
    channel._submit_materialization = lambda events: released.append(events)
    alice = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "alice-1"
    )
    bob = WechatDesktopEvent(
        "message", "b", "Bob", "b", "Bob", "text", "bob-1"
    )
    later = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "alice-2"
    )

    channel._defer_private_event(alice)
    alice_timer = FakeTimer.instances[-1]
    channel._defer_private_event(bob)
    bob_timer = FakeTimer.instances[-1]
    alice_timer.fire()
    bob_timer.fire()
    channel._defer_private_event(later)
    later_timer = FakeTimer.instances[-1]
    later_timer.fire()

    assert released == [[alice], [bob], [later]]


def test_private_aggregation_renews_without_exceeding_hard_deadline(monkeypatch):
    FakeTimer.instances = []
    channel = _bare_wechat_channel()
    channel.config = {
        "private_message_aggregation_min_ms": 1200,
        "private_message_aggregation_max_ms": 1200,
        "private_message_aggregation_max_wait_ms": 4000,
    }
    channel._pending_private_lock = threading.RLock()
    channel._pending_private_batches = {}
    channel._private_batch_timer_factory = FakeTimer
    channel._stop_event = threading.Event()
    channel._trace = lambda *_args, **_kwargs: None
    released = []
    channel._submit_materialization = lambda events: released.append(events)
    timestamps = iter((100.0, 103.7))
    monkeypatch.setattr(time, "monotonic", lambda: next(timestamps))
    first = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "first"
    )
    second = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "second"
    )

    channel._defer_private_event(first)
    first_timer = FakeTimer.instances[-1]
    channel._defer_private_event(second)
    deadline_timer = FakeTimer.instances[-1]

    assert first_timer.cancelled is True
    assert abs(deadline_timer.interval - 0.3) < 1e-9
    deadline_timer.fire()
    assert released == [[first, second]]


def test_private_aggregation_starts_only_while_robot_is_idle():
    FakeTimer.instances = []
    channel = _bare_wechat_channel()
    channel.config = {}
    channel._pending_private_lock = threading.RLock()
    channel._pending_private_batches = {}
    channel._private_batch_timer_factory = FakeTimer
    channel._stop_event = threading.Event()
    channel._trace = lambda *_args, **_kwargs: None
    channel._reply_queue = SimpleNamespace(
        status=lambda: {"queue_active_event": "active", "queue_depth": 0}
    )
    channel._materialize_queue = queue.Queue()
    channel._materialization_active = threading.Event()
    released = []
    channel._submit_materialization = lambda events: released.append(events)
    event = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "busy"
    )

    channel._defer_private_event(event)

    assert released == [[event]]
    assert FakeTimer.instances == []
    assert channel._pending_private_batches == {}


def test_control_events_bypass_private_aggregation():
    channel = _bare_wechat_channel()
    routed = []
    channel._dispatch_control_event = lambda event: routed.append(("control", event))
    channel._defer_private_event = lambda event: routed.append(("private", event))
    channel._submit_materialization = lambda events: routed.append(("group", events))
    cancel = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "/cancel"
    )
    steer = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "/steer 改查测试"
    )

    channel._route_reply_event(cancel)
    channel._route_reply_event(steer)

    assert routed == [("control", cancel), ("control", steer)]


def test_standalone_image_and_share_card_are_observed_while_file_is_materialized():
    channel = _bare_wechat_channel()
    routed = []
    processed = []
    finished = []
    channel._store = PipelineStoreStub(
        mark_event_processed=lambda event_id, *args: processed.append(event_id)
    )
    channel._trace = lambda *_args, **_kwargs: None
    channel._finish_lifecycle = (
        lambda event_ids, terminal: finished.append((event_ids, terminal))
    )
    channel._defer_private_event = lambda event: routed.append(("private", event))
    channel._submit_materialization = lambda events: routed.append(
        ("materialize", events)
    )
    image = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "image", "image.png"
    )
    file_event = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "file", "report.pdf"
    )
    share = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "share_card", "[链接]标题"
    )
    text = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "说明"
    )

    channel._route_reply_event(image)
    channel._route_reply_event(share)
    channel._route_reply_event(file_event)
    channel._route_reply_event(text)

    assert routed == [
        ("materialize", [file_event]),
        ("private", text),
    ]
    assert processed == [image.event_id, share.event_id]
    assert finished == [
        ([image.event_id], "observed"),
        ([share.event_id], "observed"),
    ]


def test_materialized_batch_keeps_all_source_event_ids():
    channel = _bare_wechat_channel()
    channel._driver = SimpleNamespace(
        materialize_event=lambda event: (event, int(event.content_type == "image"))
    )
    channel._preserve_event_evidence = lambda _event: None
    channel._lifecycle = LifecycleRecorder()
    channel._lifecycle.advance_scan()
    first = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "image", "C:/tmp/image.png"
    )
    second = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "这是什么"
    )
    channel._start_lifecycle(first)
    channel._start_lifecycle(second)

    batch = channel._materialize_batch([first, second])

    assert batch is second
    assert batch.task.source_event_ids == [first.event_id, second.event_id]
    assert batch.task.batch_id
    assert batch.history == []
    assert channel._lifecycle.snapshot(first.event_id)["attachment_resolve_count"] == 1
    reply_queue = WechatReplyQueue()
    assert reply_queue.enqueue(batch)
    queued = reply_queue.get()
    assert queued.source_event_ids == [first.event_id, second.event_id]
    assert queued.batch_id == batch.task.batch_id


def test_referenced_attachment_remains_available_to_agent_context():
    channel = _bare_wechat_channel()
    channel._driver = SimpleNamespace(
        materialize_event=lambda event: (event, 0)
    )
    channel._preserve_event_evidence = lambda _event: None
    channel._lifecycle = LifecycleRecorder()
    channel._lifecycle.advance_scan()
    event = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "这是什么图？",
        history=[
            {
                "content": "[图片: C:/tmp/image.png]",
                "content_type": "image",
            }
        ],
        reference={
            "content_type": "image",
            "file_path": "C:/tmp/image.png",
        },
    )
    channel._start_lifecycle(event)

    materialized = channel._materialize_batch([event])

    assert materialized.history == event.history
    assert materialized.task.attachment_reference_required is False


def test_standalone_file_batch_is_cache_only():
    channel = _bare_wechat_channel()
    channel._driver = SimpleNamespace(
        materialize_event=lambda event: (event, 1)
    )
    channel._preserve_event_evidence = lambda _event: None
    channel._lifecycle = LifecycleRecorder()
    channel._lifecycle.advance_scan()
    file_event = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "file",
        "C:/tmp/report.pdf",
    )
    channel._start_lifecycle(file_event)

    materialized = channel._materialize_batch([file_event])

    assert materialized.task.cache_only is True
    assert materialized.task.attachment_reference_required is False


def test_non_reference_attachment_question_requires_quote():
    question = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "这个文件讲了什么？",
    )
    referenced = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "这个文件讲了什么？",
        reference={"content_type": "file", "file_path": "C:/tmp/a.pdf"},
    )
    referenced_text = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "这个文件讲了什么？",
        reference={
            "content_type": "text",
            "sender_name": "Bob",
            "content": "请查看这个文件的处理规则",
        },
    )
    generation = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "帮我生成一张图片",
    )
    image_question = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "这是什么图？",
    )

    assert WechatDesktopChannel.__closure__[0].cell_contents._requires_attachment_reference(
        question
    )
    assert WechatDesktopChannel.__closure__[0].cell_contents._requires_attachment_reference(
        image_question
    )
    assert not WechatDesktopChannel.__closure__[0].cell_contents._requires_attachment_reference(
        referenced
    )
    assert not WechatDesktopChannel.__closure__[0].cell_contents._requires_attachment_reference(
        referenced_text
    )
    assert not WechatDesktopChannel.__closure__[0].cell_contents._requires_attachment_reference(
        generation
    )


def test_materialization_worker_defers_referenced_attachment_until_fifo():
    channel = _bare_wechat_channel()
    channel._stop_event = threading.Event()
    channel._materialize_queue = queue.Queue()
    enqueued = []
    channel._prepare_deferred_materialization = (
        WechatDesktopChannel.__closure__[0].cell_contents._prepare_deferred_materialization.__get__(
            channel
        )
    )
    channel._has_referenced_attachment = (
        WechatDesktopChannel.__closure__[0].cell_contents._has_referenced_attachment
    )
    channel._materialize_batch = lambda _events: (_ for _ in ()).throw(
        AssertionError("referenced attachment must wait for its FIFO turn")
    )
    def enqueue_and_stop(event):
        enqueued.append(event)
        channel._stop_event.set()

    channel._enqueue_reply_event = enqueue_and_stop
    event = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "这是什么图",
        reference={"content_type": "image"},
    )
    channel._materialize_queue.put([event])

    channel._consume_materialization_queue()

    assert enqueued == [event]
    assert event.task.deferred_materialization_events == [event]


def test_attachment_reference_prompt_is_sent_without_agent():
    channel = _bare_wechat_channel()
    event = WechatDesktopEvent(
        "message",
        "uia-session:a",
        "Alice",
        "a",
        "Alice",
        "text",
        "这是什么图？",
    )
    event.task.attachment_reference_required = True
    reply_queue = WechatReplyQueue()
    assert reply_queue.enqueue(event)
    item = reply_queue.get()
    sent = []
    lifecycle = []
    audits = []
    history = []
    channel._service = SimpleNamespace(status=lambda: {"paused": False})
    channel._policy = SimpleNamespace(allows_send=lambda *_args, **_kwargs: True, reserve_send=lambda _units=1: True)
    channel._reply_queue = reply_queue
    channel._driver = SimpleNamespace(
        validate_reply_target=lambda _event: SimpleNamespace(
            valid=True,
            reason="",
            replacement_event=None,
        ),
        send_text=lambda target, text: (
            sent.append((target, text))
            or {"success": True, "verified": True}
        ),
    )
    channel._store = PipelineStoreStub(
        audit=lambda *args, **kwargs: audits.append((args, kwargs)),
        append_conversation_history=lambda **kwargs: history.append(kwargs),
    )
    channel._mark_lifecycle = (
        lambda event_ids, stage, **kwargs: lifecycle.append(
            (event_ids, stage, kwargs)
        )
    )
    channel.config = {"self_display_name": "Bot"}

    terminal = channel._send_attachment_reference_prompt(item)

    assert terminal == "completed"
    assert sent == [
        ("uia-session:a", ATTACHMENT_REFERENCE_REQUIRED_REPLY)
    ]
    assert [stage for _, stage, _ in lifecycle] == [
        "send_started",
        "send_verified",
    ]
    assert history[0]["content"] == ATTACHMENT_REFERENCE_REQUIRED_REPLY
    assert audits


def test_reply_consumer_bypasses_agent_for_reference_prompt():
    channel = _bare_wechat_channel()
    channel._stop_event = threading.Event()
    channel._reply_queue = WechatReplyQueue()
    event = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "这是什么图？"
    )
    event.task.attachment_reference_required = True
    assert channel._reply_queue.enqueue(event)
    original_finish = channel._reply_queue.finish

    def finish(item, terminal):
        original_finish(item, terminal)
        channel._stop_event.set()

    channel._reply_queue.finish = finish
    stages = []
    processed = []
    channel._service = SimpleNamespace(
        update_status=lambda **_kwargs: None
    )
    channel._store = PipelineStoreStub(
        mark_event_processed=lambda event_id, *args: processed.append(event_id),
        audit=lambda *_args, **_kwargs: None,
    )
    channel._driver = SimpleNamespace(
        end_reply_cycle=lambda: (_ for _ in ()).throw(
            AssertionError("no Agent reply cycle should be active")
        )
    )
    channel._mark_lifecycle = (
        lambda _event_ids, stage, **_kwargs: stages.append(stage)
    )
    channel._finish_lifecycle = lambda *_args, **_kwargs: None
    channel._send_attachment_reference_prompt = lambda _item: "completed"
    channel._dispatch_message = lambda *_args: (_ for _ in ()).throw(
        AssertionError("reference prompt must bypass Agent")
    )

    channel._consume_reply_queue()

    assert processed == [event.event_id]
    assert "agent_started" not in stages
    assert "agent_done" not in stages


def test_reply_consumer_dispatches_enqueue_time_context_snapshot():
    channel = _bare_wechat_channel()
    channel._stop_event = threading.Event()
    channel._reply_queue = WechatReplyQueue()
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
    assert channel._reply_queue.enqueue(event)
    event.history = [{"content": "消费时错误找回的上文"}]
    original_finish = channel._reply_queue.finish

    def finish(item, terminal):
        original_finish(item, terminal)
        channel._stop_event.set()

    channel._reply_queue.finish = finish
    dispatched_history = []
    channel._service = SimpleNamespace(update_status=lambda **_kwargs: None)
    channel._store = PipelineStoreStub(
        mark_event_processed=lambda _event_id: None,
        audit=lambda *_args, **_kwargs: None,
    )
    channel._driver = SimpleNamespace(end_reply_cycle=lambda: None)
    channel._mark_lifecycle = lambda *_args, **_kwargs: None
    channel._finish_lifecycle = lambda *_args, **_kwargs: None

    def dispatch(dispatched_event, _token):
        dispatched_history.extend(dispatched_event.history)
        return False

    channel._dispatch_message = dispatch

    channel._consume_reply_queue()

    assert dispatched_history == [
        {"content": "入队时的上文", "content_type": "text"}
    ]


def test_share_browser_notice_is_not_sent_before_materialization():
    channel = _bare_wechat_channel()
    channel._stop_event = threading.Event()
    channel._reply_queue = WechatReplyQueue()
    event = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "讲了什么",
        reference={"content_type": "share_card", "content": "分享标题"},
    )
    event.task.deferred_materialization_events = [event]
    assert channel._reply_queue.enqueue(event)
    original_finish = channel._reply_queue.finish

    def finish(item, terminal):
        original_finish(item, terminal)
        channel._stop_event.set()

    channel._reply_queue.finish = finish
    notices = []
    channel._service = SimpleNamespace(update_status=lambda **_kwargs: None)
    channel._store = PipelineStoreStub(
        mark_event_processed=lambda _event_id: None,
        audit=lambda *_args, **_kwargs: None,
    )
    channel._driver = SimpleNamespace(
        begin_reply_cycle=lambda *_args: None,
        end_reply_cycle=lambda: None,
    )
    channel._mark_lifecycle = lambda *_args, **_kwargs: None
    channel._finish_lifecycle = lambda *_args, **_kwargs: None
    channel._send_deferred_attachment_notice = (
        lambda _item: notices.append("微信内置浏览器") or False
    )
    channel._send_share_content_fetch_notice = (
        lambda _item: notices.append("网络回退") or True
    )

    def materialize(events, *, before_share_fetch=None):
        before_share_fetch()
        before_share_fetch()
        return events[-1]

    channel._materialize_batch = materialize
    channel._dispatch_message = lambda *_args: False

    channel._consume_reply_queue()

    assert notices == ["微信内置浏览器", "网络回退"]
    assert event.task.preflight_attachment_notice_sent is True


def test_reply_timeout_sends_final_failure_notice():
    channel = _bare_wechat_channel()
    channel._stop_event = threading.Event()
    channel._reply_queue = WechatReplyQueue()
    event = WechatDesktopEvent(
        "message", "uia-session:a", "Alice", "a", "Alice", "text", "hello"
    )
    assert channel._reply_queue.enqueue(event)
    original_finish = channel._reply_queue.finish

    def finish(item, terminal):
        original_finish(item, terminal)
        channel._stop_event.set()

    channel._reply_queue.finish = finish
    sent = []
    processed = []
    channel.config = {
        "reply_cycle_timeout_seconds": 0,
        "agent_failure_notice_enabled": True,
        "agent_failure_notice_templates": ["答案刚才迷路了 🧭 请再试一次。"],
        "shadow_mode": False,
    }
    channel._failure_notice_lock = threading.RLock()
    channel._service = SimpleNamespace(
        status=lambda: {"paused": False},
        update_status=lambda **_kwargs: None,
    )
    channel._policy = SimpleNamespace(
        is_blocked=lambda _target: False,
        is_allowlisted=lambda _target, _is_group: True,
        allows_send=lambda *_args, **_kwargs: True,
        reserve_send=lambda _units=1: True,
    )
    channel._driver = SimpleNamespace(
        send_interim_text=lambda target, text: (
            sent.append((target, text))
            or {"success": True, "verified": True}
        ),
        end_reply_cycle=lambda: None,
    )
    channel._store = PipelineStoreStub(
        mark_event_processed=lambda event_id, *args: processed.append(event_id),
        audit=lambda *_args, **_kwargs: None,
        append_conversation_history=lambda **_kwargs: None,
    )
    channel._dispatch_message = lambda *_args: True
    channel._mark_lifecycle = lambda *_args, **_kwargs: None
    channel._finish_lifecycle = lambda *_args, **_kwargs: None
    channel._trace = lambda *_args, **_kwargs: None

    channel._consume_reply_queue()

    assert sent == [("uia-session:a", "答案刚才迷路了 🧭 请再试一次。")]
    assert processed == [event.event_id]
    assert channel._reply_queue.status()["queue_timeout"] == 1


def test_attachment_path_cache_avoids_second_image_capture(tmp_path):
    local_image = tmp_path / "cached.png"
    local_image.write_bytes(b"png")
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage(
            "Alice",
            "[图片]",
            message_type="image",
            runtime_id="image-1",
            bounds=(100, 100, 300, 260),
        )
    ]
    client.image_paths["[图片]"] = str(local_image)
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in events])

    _, first_count = driver.materialize_event(events[0])
    _, second_count = driver.materialize_event(events[0])

    assert first_count == 1
    assert second_count == 0
    assert client.image_fetches == ["[图片]"]


def test_materialization_resolves_only_selected_visible_attachment(tmp_path):
    old_file = tmp_path / "old.pdf"
    target_image = tmp_path / "target.png"
    old_file.write_bytes(b"pdf")
    target_image.write_bytes(b"png")
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage(
            "Alice",
            "old.pdf",
            message_type="file",
            runtime_id="file-1",
        ),
        UiaChatMessage(
            "Alice",
            "target-image",
            message_type="image",
            runtime_id="image-1",
            bounds=(100, 100, 300, 260),
        ),
    ]
    client.file_paths["old.pdf"] = str(old_file)
    client.image_paths["target-image"] = str(target_image)
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True},
        client=client,
        shell_hook=FakeHook(),
    )

    _, events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in events])
    materialized, resolve_count = driver.materialize_event(events[0])

    assert resolve_count == 1
    assert materialized.content == str(target_image)
    assert client.image_fetches == ["target-image"]
    assert client.file_fetches == []


def test_cached_file_is_reused_by_quoted_filename(tmp_path):
    local_file = tmp_path / "report.pdf"
    local_file.write_bytes(b"%PDF")
    client = FakeClient()
    client.file_paths["文件\nreport.pdf\n8 KB"] = str(local_file)
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())
    standalone = UiaChatMessage(
        "Alice",
        "文件\nreport.pdf\n8 KB",
        message_type="file",
        runtime_id="file-1",
        stable_id="stable-file-1",
    )

    cached, first_count = driver._resolve_reply_target_message(
        "conversation", standalone
    )
    quoted = UiaChatMessage(
        "Alice",
        "帮我读一下",
        message_type="text",
        runtime_id="quote-1",
        stable_id="stable-quote-1",
        reference=UiaReferencedMessage(
            sender_name="Alice",
            content="report.pdf",
            message_type="file",
        ),
    )
    resolved, second_count = driver._resolve_reply_target_message(
        "conversation", quoted
    )

    assert cached.file_path == str(local_file)
    assert first_count == 1
    assert resolved.reference.file_path == str(local_file)
    assert resolved.reference.resolved is True
    assert second_count == 0
    assert client.file_fetches == ["文件\nreport.pdf\n8 KB"]


def test_text_reference_locates_original_before_resolving():
    client = FakeClient()
    calls = []

    def resolve_reference(message):
        calls.append(message.reference.content)
        return replace(
            message,
            reference=replace(
                message.reference,
                content="这是定位后的完整原文",
                original_content="这是定位后的完整原文",
                resolved=True,
                degraded=False,
                strategy="wechat_locate_original",
            ),
        )

    client.resolve_message_reference = resolve_reference
    client.fetch_referenced_message_image = lambda _message: (
        _ for _ in ()
    ).throw(AssertionError("text reference must not open image viewer"))
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())
    quoted = UiaChatMessage(
        "Alice",
        "你怎么看",
        message_type="text",
        runtime_id="quote-text-1",
        reference=UiaReferencedMessage(
            sender_name="Bob",
            content="这是被引用的文本预览",
            message_type="text",
        ),
    )

    resolved, resolve_count = driver._resolve_reply_target_message(
        "conversation", quoted
    )

    assert calls == ["这是被引用的文本预览"]
    assert resolve_count == 1
    assert resolved.reference.content == "这是定位后的完整原文"
    assert resolved.reference.resolved is True
    assert resolved.reference.strategy == "wechat_locate_original"
