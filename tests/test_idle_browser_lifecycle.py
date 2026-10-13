import threading

import pytest

from agent.tools.browser import browser_service as module
from agent.tools.browser import browser_tool as tool_module
from agent.tools.browser.browser_service import BrowserService
from agent.tools.browser.browser_tool import BrowserTool


class ManualTimer:
    instances = []

    def __init__(self, interval, function, args=()):
        self.interval = interval
        self.function = function
        self.args = args
        self.cancelled = False
        self.started = False
        self.daemon = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # Simulate cancel() racing with a callback that has already begun.
        self.function(*self.args)


@pytest.fixture
def service(monkeypatch):
    ManualTimer.instances = []
    monkeypatch.setattr(module.threading, "Timer", ManualTimer)
    service = BrowserService({"cdp_endpoint": "http://127.0.0.1:9222"})
    service._launch_browser = lambda: None
    service.shutdowns = []

    def shutdown():
        service._cancel_idle_timer()
        service.shutdowns.append(True)

    service._shutdown_browser = shutdown
    yield service
    service.close()


def test_idle_countdown_starts_after_actual_operation_and_reuses_thread(service):
    started = threading.Event()
    finish = threading.Event()
    outputs = []

    def operation():
        started.set()
        assert finish.wait(2)
        return "first"

    caller = threading.Thread(target=lambda: outputs.append(service._submit(operation)))
    caller.start()
    assert started.wait(2)
    original_thread = service._thread
    assert service._pending_operations == 1
    assert service._idle_timer is None
    service._on_idle_timeout(service._idle_generation)
    assert service._alive is True
    finish.set()
    caller.join(2)
    assert outputs == ["first"]
    first_timer = service._idle_timer
    assert first_timer.started is True and first_timer.interval == 300
    assert service._submit(lambda: "second") == "second"
    assert service._thread is original_thread
    assert first_timer.cancelled is True
    assert service._idle_timer is not first_timer
    first_timer.fire()
    assert service._alive is True
    assert service.shutdowns == []
    service._idle_timer.fire()
    assert service._alive is False
    assert service.shutdowns == [True]


def test_queued_operations_prevent_idle_close_until_all_complete(service):
    first_started = threading.Event()
    first_finish = threading.Event()
    second_started = threading.Event()
    second_finish = threading.Event()

    def first():
        first_started.set()
        assert first_finish.wait(2)

    def second():
        second_started.set()
        assert second_finish.wait(2)

    a = threading.Thread(target=lambda: service._submit(first))
    b = threading.Thread(target=lambda: service._submit(second))
    a.start()
    assert first_started.wait(2)
    second_leased = threading.Event()
    original_acquire = service._acquire_activity

    def acquire():
        original_acquire()
        second_leased.set()

    service._acquire_activity = acquire
    b.start()
    assert second_leased.wait(2)
    assert service._pending_operations == 2
    first_finish.set()
    assert second_started.wait(2)
    a.join(2)
    assert service._pending_operations == 1
    assert service._idle_timer is None
    service._on_idle_timeout(service._idle_generation)
    assert service._alive is True
    second_finish.set()
    b.join(2)
    assert service._pending_operations == 0
    assert service._idle_timer.started is True


def test_failed_operation_also_starts_idle_countdown(service):
    def failure():
        raise ValueError("synthetic failure")

    with pytest.raises(ValueError, match="synthetic failure"):
        service._submit(failure)
    assert service._pending_operations == 0
    assert service._idle_timer.started is True


def test_start_failure_releases_unsubmitted_lease(service):
    def start_failure():
        raise RuntimeError("synthetic startup failure")

    service._start_thread = start_failure
    with pytest.raises(RuntimeError, match="startup failure"):
        service._submit(lambda: None)
    assert service._pending_operations == 0
    assert service._idle_timer is None


def test_caller_timeout_retains_lease_until_actual_task_completion(service, monkeypatch):
    class TimeoutEvent:
        def wait(self, timeout):
            assert timeout == 120
            return False

        def set(self):
            pass

    service._alive = True
    service._start_thread = lambda: None
    monkeypatch.setattr(module.threading, "Event", TimeoutEvent)
    with pytest.raises(TimeoutError, match="operation timed out"):
        service._submit(lambda: None)
    assert service._pending_operations == 1
    assert service._idle_timer is None
    _fn, _args, _kwargs, slot = service._task_queue.get_nowait()
    service._complete_result_slot(slot)
    assert service._pending_operations == 0
    assert service._idle_timer.started is True
    # Completing a drained task twice cannot release the same lease twice.
    service._complete_result_slot(slot)
    assert service._pending_operations == 0


def test_drained_queue_releases_each_task_lease(service):
    service._alive = True
    slots = []
    for _ in range(2):
        service._acquire_activity()
        slot = {"event": threading.Event(), "activity_lease": True}
        slots.append(slot)
        service._task_queue.put((lambda: None, (), {}, slot))
    service._task_queue.put(None)
    service._drain_queue(RuntimeError("synthetic shutdown"))
    assert service._pending_operations == 0
    assert all(slot["event"].is_set() and "error" in slot for slot in slots)


def test_join_timeout_retains_old_thread_and_prevents_mixed_generation(service):
    class StuckThread:
        def __init__(self):
            self.joins = []

        def is_alive(self):
            return True

        def join(self, timeout):
            self.joins.append(timeout)

    old_thread = StuckThread()
    service._thread = old_thread
    service._alive = True
    old_queue = service._task_queue
    service.close()
    assert service._thread is old_thread
    assert old_thread.joins == [10]
    with pytest.raises(RuntimeError, match="still shutting down"):
        service._start_thread()
    assert service._task_queue is old_queue
    assert service._thread is old_thread
    assert old_thread.joins == [10, 5]
    service._thread = None


def test_existing_starting_thread_waits_for_its_ready_event(service):
    class StartingThread:
        def is_alive(self):
            return True

    class ReadyEvent:
        def __init__(self):
            self.waits = []

        def wait(self, timeout):
            self.waits.append(timeout)
            return True

    ready = ReadyEvent()
    service._thread = StartingThread()
    service._ready = ready
    service._alive = True
    service._start_thread()
    assert ready.waits == [30]
    service._thread = None
    service._alive = False


@pytest.mark.parametrize("value", [0, -1, "bad", float("nan"), float("inf"), True, 1e100])
def test_invalid_or_disabled_timeout_falls_back_to_finite_default(value):
    service = BrowserService({"cdp_endpoint": "http://127.0.0.1:9222", "idle_timeout": value})
    assert service._idle_timeout == 300


def test_positive_timeout_can_retain_session_longer():
    service = BrowserService({"cdp_endpoint": "http://127.0.0.1:9222", "idle_timeout": 600})
    assert service._idle_timeout == 600


@pytest.mark.parametrize("mode", ["cdp", "system-cdp", "persistent", "fresh"])
def test_shutdown_only_releases_owned_browser_resources(mode):
    calls = []

    class Resource:
        def __init__(self, label):
            self.label = label

        def close(self):
            calls.append(self.label)

        def stop(self):
            calls.append(self.label)

    service = BrowserService({"cdp_endpoint": "http://127.0.0.1:9222"})
    service._launch_mode = mode
    service._page = Resource("external page")
    service._context = Resource("context")
    service._browser = None if mode == "persistent" else Resource("browser")
    service._chrome_launcher = Resource("owned launcher")
    service._playwright = Resource("playwright")
    service._shutdown_browser()
    assert "external page" not in calls
    if mode == "cdp":
        assert calls == ["browser", "playwright"]
    elif mode == "system-cdp":
        assert calls == ["browser", "owned launcher", "playwright"]
    elif mode == "persistent":
        assert calls == ["context", "playwright"]
    else:
        assert calls == ["context", "browser", "playwright"]


def test_partial_launch_failure_releases_started_resources(service):
    def launch_failure():
        raise RuntimeError("synthetic connection failure")

    service._launch_browser = launch_failure
    with pytest.raises(RuntimeError, match="not available"):
        service._submit(lambda: None)
    assert service.shutdowns == [True]
    assert service._pending_operations == 0
    assert service._alive is False


def test_concurrent_tool_copies_create_one_shared_service(monkeypatch):
    monkeypatch.setattr(BrowserTool, "_shared_service", None)
    constructor_started = threading.Event()
    release_constructor = threading.Event()
    all_lock_attempts = threading.Event()
    ready = threading.Barrier(6)
    created = []
    results = []
    failures = []

    class TrackedLock:
        def __init__(self):
            self.lock = threading.Lock()
            self.count_lock = threading.Lock()
            self.attempts = 0

        def __enter__(self):
            with self.count_lock:
                self.attempts += 1
                if self.attempts == 6:
                    all_lock_attempts.set()
            self.lock.acquire()

        def __exit__(self, *args):
            self.lock.release()

    monkeypatch.setattr(BrowserTool, "_shared_service_lock", TrackedLock())

    def factory(config):
        instance = object()
        created.append(instance)
        constructor_started.set()
        assert release_constructor.wait(5)
        return instance

    monkeypatch.setattr(tool_module, "BrowserService", factory)

    def acquire(tool):
        try:
            ready.wait(2)
            results.append(tool._get_service())
        except Exception as exc:
            failures.append(exc)

    tools = [BrowserTool() for _ in range(6)]
    callers = [threading.Thread(target=acquire, args=(tool,)) for tool in tools]
    for caller in callers:
        caller.start()
    try:
        assert constructor_started.wait(2)
        assert all_lock_attempts.wait(2)
    finally:
        release_constructor.set()
        for caller in callers:
            caller.join(2)
    assert failures == []
    assert all(not caller.is_alive() for caller in callers)
    assert len(created) == 1
    assert results == [created[0]] * 6
    assert all(tool._service is created[0] for tool in tools)


def test_unused_tool_close_preserves_another_shared_service(monkeypatch):
    shared = object()
    monkeypatch.setattr(BrowserTool, "_shared_service", shared)
    BrowserTool().close()
    assert BrowserTool._shared_service is shared


def test_old_tool_close_preserves_replacement_shared_service(monkeypatch):
    class Service:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    old_service = Service()
    new_service = Service()
    monkeypatch.setattr(BrowserTool, "_shared_service", new_service)
    old_tool = BrowserTool()
    old_tool._service = old_service
    old_tool.close()
    assert old_service.closed == 1
    assert new_service.closed == 0
    assert BrowserTool._shared_service is new_service
    assert old_tool._service is None


def test_cached_copy_reuses_new_shared_service_after_owner_closes(monkeypatch):
    class Service:
        def __init__(self, config):
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(BrowserTool, "_shared_service", None)
    monkeypatch.setattr(tool_module, "BrowserService", Service)
    owner = BrowserTool()
    old_service = owner._get_service()
    cached_copy = BrowserTool()
    cached_copy._service = old_service
    owner.close()
    new_service = BrowserTool()._get_service()
    assert old_service.closed is True
    assert cached_copy._get_service() is new_service
    assert new_service is BrowserTool._shared_service
    assert new_service is not old_service
