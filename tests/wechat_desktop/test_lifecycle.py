"""生命周期诊断记录的独立测试。"""

import logging
from channel.wechat_desktop.models import WechatDesktopEvent
from common.log import logger
from channel.wechat_desktop.pipeline.lifecycle import LifecycleRecorder


def test_lifecycle_preserves_first_timestamp_and_returns_detached_snapshot():
    ticks = iter([10.0, 11.0, 12.0, 13.0])
    recorder = LifecycleRecorder(clock=lambda: next(ticks))
    event = WechatDesktopEvent("message", "a", "Alice", "a", "Alice", "text", "hello")
    recorder.start(event)
    recorder.start(event)
    recorder.mark([event.event_id], "queued", attachment_resolve_count=1)
    recorder.mark([event.event_id], "queued", attachment_resolve_count=2)
    snapshot = recorder.snapshot(event.event_id)
    assert snapshot["detected"] == 10.0
    assert snapshot["queued"] == 12.0
    assert snapshot["attachment_resolve_count"] == 3
    snapshot["send_result"] = "tampered"
    assert recorder.snapshot(event.event_id)["send_result"] == "-"


def test_concurrent_finish_logs_once_and_late_marks_do_not_revive_record(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    recorder = LifecycleRecorder()
    event = WechatDesktopEvent("message", "a", "Alice", "a", "Alice", "text", "hello")
    recorder.start(event)
    emitted = []
    monkeypatch.setattr(logger, "info", lambda *args: emitted.append(args))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: recorder.finish([event.event_id], "completed"), range(12)))
    recorder.mark([event.event_id], "send_verified")
    assert len(emitted) == 1
    assert recorder.snapshot(event.event_id) == {}


def test_lifecycle_summary_is_one_info_line_per_source_message():
    recorder = LifecycleRecorder()
    recorder.advance_scan()
    recorder.advance_scan()
    recorder.advance_scan()
    recorder.advance_scan()
    event = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "hello"
    )
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Capture()
    logger.addHandler(handler)
    try:
        recorder.start(event)
        recorder.mark(
            [event.event_id],
            "materialized",
            batch_id="batch-1",
            attachment_resolve_count=1,
        )
        recorder.mark([event.event_id], "queued")
        recorder.mark([event.event_id], "agent_started")
        recorder.mark([event.event_id], "agent_done")
        recorder.finish([event.event_id], "completed")
    finally:
        logger.removeHandler(handler)

    lifecycle_lines = [
        line for line in records if line.startswith("[WechatDesktop][lifecycle]")
    ]
    assert len(lifecycle_lines) == 1
    assert "event_id=" + event.event_id in lifecycle_lines[0]
    assert "attachment_resolve_count=1" in lifecycle_lines[0]
