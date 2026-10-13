"""用线程屏障验证 Jev 发布、追加指令与取消之间的提交顺序。"""

import copy
import threading

import pytest

from agent.decision.runtime import CompletionDecision, RoutingDecision
from agent.protocol.agent_stream import AgentStreamExecutor
from agent.protocol.cancel import AgentCancelledError, CancelEvent, CancelTokenRegistry
from agent.protocol.steer import SteerInbox


BASE_PROMPT = "基础上下文"
OLD_HINT = "旧目标的技能建议"
TIMEOUT = 3


class LocalDecision:
    """只返回本地决策，不创建模型、工具或后台网络任务。"""

    completion_wait_seconds = 0

    def __init__(self):
        self.route = RoutingDecision("request-v1", "tools", ("lookup",), (), .95, OLD_HINT)
        self.completion = CompletionDecision("continue", .98)
        self.resolve_calls = []
        self.closed = False
        self.invalidated = False

    def resolve(self, wait_seconds=0):
        self.resolve_calls.append("route")
        return self.route

    def resolve_completion(self, wait_seconds=0):
        self.resolve_calls.append("completion")
        return self.completion

    def close(self):
        self.closed = True

    def invalidate(self):
        self.invalidated = True
        self.closed = True


class ThreadCall:
    """保存线程异常，使用有界等待让死锁表现为测试失败。"""

    def __init__(self, operation, name="decision-publisher"):
        self.value = None
        self.error = None
        self.done = threading.Event()

        def run():
            try:
                self.value = operation()
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, name=name, daemon=True)
        self.thread.start()

    def finish(self, allow_cancel=False):
        self.thread.join(TIMEOUT)
        assert not self.thread.is_alive(), "线程未在截止时间内退出，可能存在死锁"
        if self.error is not None:
            if allow_cancel and isinstance(self.error, AgentCancelledError):
                return None
            raise self.error
        return self.value


class ObservedLock:
    """通知更新线程已开始取锁；不使用 sleep 推测线程调度。"""

    def __init__(self, lock, attempted):
        self.lock = lock
        self.attempted = attempted

    def acquire(self, *args, **kwargs):
        if threading.current_thread().name == "decision-update":
            self.attempted.set()
        return self.lock.acquire(*args, **kwargs)

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


def make_executor(cancel=None, inbox=None, on_event=None):
    decision = LocalDecision()
    events = []

    def record(event):
        events.append(event)
        if on_event is not None:
            on_event(event)

    instance = AgentStreamExecutor(
        agent=object(), model=object(), system_prompt=BASE_PROMPT, tools=[],
        decision_run=decision, cancel_event=cancel, steer_inbox=inbox,
        on_event=record,
    )
    instance._decision_completion_active = True
    instance._user_goal = "旧目标"
    instance.messages.append({
        "role": "assistant", "content": [{"type": "text", "text": "尚未发布的旧草稿"}],
    })
    return instance, decision, events


def publish(instance, stage):
    if stage == "route":
        return instance._apply_decision_route()
    return instance._completion_needs_continue()


def update(cancel, inbox, change):
    if change == "cancel":
        cancel.set()
        return True
    return inbox.submit("新的目标").accepted


def assert_unpublished(instance, events, original_messages):
    assert events == []
    assert instance.system_prompt == BASE_PROMPT
    assert instance._decision_corrections == 0
    assert instance.messages == original_messages


@pytest.mark.parametrize("stage", ["route", "completion"])
@pytest.mark.parametrize("change", ["cancel", "steer"])
def test_update_completed_before_guard_rejects_old_decision(monkeypatch, stage, change):
    cancel, inbox = CancelEvent(), SteerInbox()
    instance, decision, events = make_executor(cancel, inbox)
    original_messages = copy.deepcopy(instance.messages)
    before_guard, resume = threading.Event(), threading.Event()
    owner, method = ((cancel, "publish_if_active") if change == "cancel"
                     else (inbox, "publish_if_empty"))
    original = getattr(owner, method)
    resumed = []

    def paused_guard(callback):
        before_guard.set()
        resumed.append(resume.wait(TIMEOUT))
        return original(callback)

    monkeypatch.setattr(owner, method, paused_guard)
    publisher = ThreadCall(lambda: publish(instance, stage))
    try:
        assert before_guard.wait(TIMEOUT), "执行器未进入决策发布边界"
        assert update(cancel, inbox, change)
    finally:
        resume.set()
    publisher.finish(allow_cancel=change == "cancel")
    assert resumed == [True]
    assert decision.resolve_calls == [stage]
    assert_unpublished(instance, events, original_messages)


@pytest.mark.parametrize("stage", ["route", "completion"])
@pytest.mark.parametrize("change", ["cancel", "steer"])
def test_publication_holds_shared_guard_until_callback_finishes(stage, change):
    cancel, inbox = CancelEvent(), SteerInbox()
    entered, resume, attempted = threading.Event(), threading.Event(), threading.Event()
    owner, attribute = ((cancel, "_publication_lock") if change == "cancel"
                        else (inbox, "_lock"))
    lock = ObservedLock(getattr(owner, attribute), attempted)
    setattr(owner, attribute, lock)
    timeline, resumed = [], []

    def callback(event):
        if event["type"] == "decision_" + stage:
            timeline.append("publish_enter")
            entered.set()
            resumed.append(resume.wait(TIMEOUT))
            timeline.append("publish_exit")

    instance, decision, events = make_executor(cancel, inbox, callback)
    publisher = ThreadCall(lambda: publish(instance, stage))
    actor = None
    try:
        assert entered.wait(TIMEOUT), "没有收到决策发布回调"
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        assert not acquired, "发布回调没有持有与更新共用的锁"

        def apply_update():
            result = update(cancel, inbox, change)
            timeline.append("update_complete")
            return result

        actor = ThreadCall(apply_update, name="decision-update")
        assert attempted.wait(TIMEOUT), "更新线程没有开始获取发布锁"
        assert not actor.done.is_set(), "发布临界区尚未结束，更新却已完成"
    finally:
        resume.set()
    publisher.finish()
    assert actor is not None and actor.finish() is True
    assert resumed == [True]
    assert timeline == ["publish_enter", "publish_exit", "update_complete"]
    assert [event["type"] for event in events] == ["decision_" + stage]
    assert decision.resolve_calls == [stage]


@pytest.mark.parametrize("stage", ["route", "completion"])
@pytest.mark.parametrize("change", ["cancel", "steer"])
def test_remote_resolve_does_not_hold_publication_guards(monkeypatch, stage, change):
    cancel, inbox = CancelEvent(), SteerInbox()
    instance, decision, events = make_executor(cancel, inbox)
    original_messages = copy.deepcopy(instance.messages)
    resolving, resume = threading.Event(), threading.Event()
    resumed = []
    method = "resolve" if stage == "route" else "resolve_completion"
    original = getattr(decision, method)

    def resolve(wait_seconds=0):
        resolving.set()
        resumed.append(resume.wait(TIMEOUT))
        return original(wait_seconds=wait_seconds)

    monkeypatch.setattr(decision, method, resolve)
    publisher = ThreadCall(lambda: publish(instance, stage))
    actor = None
    try:
        assert resolving.wait(TIMEOUT), "没有进入远程决策等待替身"
        actor = ThreadCall(lambda: update(cancel, inbox, change), name="decision-update")
        assert actor.finish() is True
        assert not publisher.done.is_set(), "远程等待尚未释放，发布线程却提前结束"
    finally:
        resume.set()
    publisher.finish(allow_cancel=change == "cancel")
    assert resumed == [True]
    assert_unpublished(instance, events, original_messages)


@pytest.mark.parametrize("stage", ["route", "completion"])
@pytest.mark.parametrize("change", ["steer", "cancel_request", "cancel_session", "both"])
def test_decision_callback_can_reenter_steering_and_registry_without_deadlock(stage, change):
    registry = CancelTokenRegistry()
    cancel, inbox = registry.register("request", "session"), SteerInbox()
    results = []

    def callback(event):
        if event["type"] != "decision_" + stage:
            return
        if change in {"cancel_request", "both"}:
            results.append(registry.cancel_request("request"))
        if change == "cancel_session":
            results.append(registry.cancel_session("session"))
        if change in {"steer", "both"}:
            results.append(inbox.submit("新的目标").accepted)

    instance, decision, events = make_executor(cancel, inbox, callback)
    ThreadCall(lambda: publish(instance, stage)).finish()
    assert results == ([True, True] if change == "both" else [True])
    assert cancel.is_set() is (change != "steer")
    assert inbox.has_pending() is (change in {"steer", "both"})
    assert [event["type"] for event in events] == ["decision_" + stage]


@pytest.mark.parametrize("stage", ["route", "completion"])
def test_cancel_and_steer_guards_use_fixed_nesting_order(monkeypatch, stage):
    cancel, inbox = CancelEvent(), SteerInbox()
    original_cancel, original_steer = cancel.publish_if_active, inbox.publish_if_empty
    timeline = []

    def cancel_guard(callback):
        def guarded():
            timeline.append("cancel_enter")
            callback()
            timeline.append("cancel_exit")
        return original_cancel(guarded)

    def steer_guard(callback):
        def guarded():
            timeline.append("steer_enter")
            callback()
            timeline.append("steer_exit")
        return original_steer(guarded)

    monkeypatch.setattr(cancel, "publish_if_active", cancel_guard)
    monkeypatch.setattr(inbox, "publish_if_empty", steer_guard)
    instance, decision, events = make_executor(
        cancel, inbox, lambda event: timeline.append("callback"))
    ThreadCall(lambda: publish(instance, stage)).finish()
    assert timeline == ["cancel_enter", "steer_enter", "callback", "steer_exit", "cancel_exit"]


@pytest.mark.parametrize("stage", ["route", "completion"])
def test_reentrant_registry_cancel_does_not_deadlock_concurrent_cancel(stage):
    registry = CancelTokenRegistry()
    cancel, inbox = registry.register("request", "session"), SteerInbox()
    entered, attempted = threading.Event(), threading.Event()
    cancel._publication_lock = ObservedLock(cancel._publication_lock, attempted)
    results = []

    def callback(event):
        if event["type"] != "decision_" + stage:
            return
        entered.set()
        results.append(attempted.wait(TIMEOUT))
        # 并行取消已阻塞在事件锁，但必须已释放 registry 的锁。
        results.append(registry.cancel_session("session"))
        results.append(inbox.submit("新的目标").accepted)

    instance, decision, events = make_executor(cancel, inbox, callback)
    publisher = ThreadCall(lambda: publish(instance, stage))
    assert entered.wait(TIMEOUT), "没有进入决策事件回调"
    actor = ThreadCall(lambda: registry.cancel_request("request"), name="decision-update")
    publisher.finish()
    assert actor.finish() is True
    assert results == [True, 1, True]
    assert cancel.is_set() and inbox.has_pending()


def test_completion_commits_state_before_callback_applies_new_steering():
    cancel, inbox = CancelEvent(), SteerInbox()
    snapshots, submitted = [], []

    def callback(event):
        if event["type"] != "decision_completion":
            return
        snapshots.append((instance._decision_corrections, instance.system_prompt,
                          copy.deepcopy(instance.messages)))
        submitted.append(inbox.submit("新的目标").accepted)
        instance._append_steering(instance._drain_steering())

    instance, decision, events = make_executor(cancel, inbox, callback)
    assert ThreadCall(instance._completion_needs_continue).finish() is True
    assert submitted == [True]
    assert len(snapshots) == 1
    corrections, prompt, messages = snapshots[0]
    assert corrections == 1
    assert "内部完成复核" in prompt
    assert messages == []
    assert instance._decision_corrections == corrections
    assert instance.system_prompt == BASE_PROMPT
    assert len(instance.messages) == 1
    assert "新的目标" in instance.messages[0]["content"][0]["text"]
    assert decision.invalidated and instance.decision_run is None
    assert [event["type"] for event in events] == ["decision_completion", "agent_steered"]


@pytest.mark.parametrize("stage", ["route", "completion"])
def test_closed_steering_inbox_rejects_old_decision(stage):
    cancel, inbox = CancelEvent(), SteerInbox()
    inbox.close()
    instance, decision, events = make_executor(cancel, inbox)
    original_messages = copy.deepcopy(instance.messages)
    assert not publish(instance, stage)
    assert_unpublished(instance, events, original_messages)


@pytest.mark.parametrize("stage", ["route", "completion"])
@pytest.mark.parametrize("initially_cancelled", [False, True])
def test_raw_event_disables_optional_decision_and_preserves_cancel_polling(stage, initially_cancelled):
    cancel = threading.Event()
    if initially_cancelled:
        cancel.set()
    instance, decision, events = make_executor(cancel, SteerInbox())
    original_messages = copy.deepcopy(instance.messages)
    assert instance.decision_run is None and decision.closed
    assert not publish(instance, stage)
    assert decision.resolve_calls == []
    assert_unpublished(instance, events, original_messages)
    cancel.set()
    with pytest.raises(AgentCancelledError):
        instance._check_cancelled()
