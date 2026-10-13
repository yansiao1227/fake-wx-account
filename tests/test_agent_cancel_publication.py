"""取消 token 的发布原子性；通过屏障控制线程顺序，不调用外部服务。"""

import threading

import pytest

from agent.protocol.cancel import CancelEvent, CancelTokenRegistry


def test_registry_uses_cancel_event_and_preserves_request_lifecycle():
    registry = CancelTokenRegistry()
    event = registry.register("request", "session")

    assert isinstance(event, CancelEvent)
    assert isinstance(event, threading.Event)
    assert registry.register("request", "session") is event
    assert registry.get_event("request") is event
    assert registry.has_active("session")
    assert not event.is_set()
    assert not event.wait(0)

    assert registry.cancel_request("request")
    assert event.is_set()
    assert event.wait(0)

    registry.unregister("request")
    registry.unregister("request")
    assert registry.get_event("request") is None
    assert not registry.has_active("session")
    assert not registry.cancel_request("request")


def test_registry_empty_request_returns_independent_publishable_events():
    registry = CancelTokenRegistry()
    first = registry.register("", "session")
    second = registry.register("", "session")
    published = []

    assert isinstance(first, CancelEvent)
    assert isinstance(second, CancelEvent)
    assert first is not second
    first.set()
    assert not first.publish_if_active(lambda: published.append("first"))
    assert second.publish_if_active(lambda: published.append("second"))
    assert published == ["second"]
    assert registry.get_event("") is None
    assert not registry.cancel_request("")
    assert not registry.has_active("session")


def test_registry_session_cancel_rejects_only_matching_publications():
    registry = CancelTokenRegistry()
    first = registry.register("first", "session")
    second = registry.register("second", "session")
    other = registry.register("other", "other-session")
    published = []

    assert registry.cancel_session("session") == 2
    assert not first.publish_if_active(lambda: published.append("first"))
    assert not second.publish_if_active(lambda: published.append("second"))
    assert other.publish_if_active(lambda: published.append("other"))
    assert published == ["other"]
    assert registry.cancel_session("missing") == 0
    assert registry.cancel_session("") == 0


def test_clear_restores_publication_and_event_interface():
    event = CancelEvent()
    published = []
    event.set()
    assert not event.publish_if_active(lambda: published.append("cancelled"))

    event.clear()
    assert not event.is_set()
    assert not event.wait(0)
    assert event.publish_if_active(lambda: published.append("active"))
    assert published == ["active"]


def test_callback_can_reenter_set_and_clear():
    event = CancelEvent()
    finished = threading.Event()
    results = []

    def publish():
        event.set()
        assert event.wait(0)
        event.clear()
        assert not event.is_set()

    def commit():
        results.append(event.publish_if_active(publish))
        finished.set()

    worker = threading.Thread(target=commit, daemon=True)
    worker.start()
    assert finished.wait(2)
    worker.join(2)
    assert not worker.is_alive()
    assert results == [True]
    assert not event.is_set()


def test_callback_exception_releases_publication_lock():
    event = CancelEvent()
    cancelled = threading.Event()

    def publish():
        raise ValueError("发布失败")

    with pytest.raises(ValueError, match="发布失败"):
        event.publish_if_active(publish)

    def cancel():
        event.set()
        cancelled.set()

    worker = threading.Thread(target=cancel, daemon=True)
    worker.start()
    assert cancelled.wait(2)
    worker.join(2)
    assert not worker.is_alive()
    assert event.is_set()


def test_concurrent_cancel_finishes_after_started_publication():
    event = CancelEvent()
    publishing = threading.Event()
    cancel_attempted = threading.Event()
    release_publication = threading.Event()
    cancellation_finished = threading.Event()
    results = []
    order = []

    def publish():
        publishing.set()
        assert release_publication.wait(2)
        assert not cancellation_finished.is_set()
        order.append("published")

    def commit():
        results.append(event.publish_if_active(publish))

    def cancel():
        assert publishing.wait(2)
        cancel_attempted.set()
        event.set()
        order.append("cancelled")
        cancellation_finished.set()

    publisher = threading.Thread(target=commit, daemon=True)
    canceller = threading.Thread(target=cancel, daemon=True)
    publisher.start()
    canceller.start()
    try:
        assert publishing.wait(2)
        assert cancel_attempted.wait(2)
        assert not event.is_set()
        assert not cancellation_finished.is_set()
    finally:
        release_publication.set()
        publisher.join(2)
        canceller.join(2)

    assert not publisher.is_alive()
    assert not canceller.is_alive()
    assert results == [True]
    assert order == ["published", "cancelled"]
    assert event.is_set()


@pytest.mark.parametrize("cancel_method", ["request", "session"])
def test_registry_releases_its_lock_before_cancel_waits_on_publication(cancel_method):
    registry = CancelTokenRegistry()
    event = registry.register("request", "session")
    publishing = threading.Event()
    cancellation_started = threading.Event()
    release_publication = threading.Event()
    finished = threading.Event()
    original_set = event.set
    results = []

    # 在实际 set 等待发布锁之前建立屏障，从而确定注册表锁已经释放。
    def mark_set():
        cancellation_started.set()
        original_set()

    event.set = mark_set

    def publish():
        publishing.set()
        assert release_publication.wait(2)
        # 外部取消线程正在等待发布锁；这些调用必须能取得注册表锁。
        assert registry.get_event("request") is event
        assert registry.cancel_request("request")
        assert registry.cancel_session("session") == 1

    def commit():
        results.append(event.publish_if_active(publish))

    def cancel():
        assert publishing.wait(2)
        if cancel_method == "request":
            assert registry.cancel_request("request")
        else:
            assert registry.cancel_session("session") == 1
        finished.set()

    publisher = threading.Thread(target=commit, daemon=True)
    canceller = threading.Thread(target=cancel, daemon=True)
    publisher.start()
    canceller.start()
    try:
        assert publishing.wait(2)
        assert cancellation_started.wait(2)
    finally:
        release_publication.set()
        publisher.join(2)
        canceller.join(2)

    assert not publisher.is_alive()
    assert not canceller.is_alive()
    assert finished.is_set()
    assert results == [True]
    assert event.is_set()
