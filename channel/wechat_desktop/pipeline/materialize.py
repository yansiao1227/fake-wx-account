"""微信桌面通道的附件物化、引用判断和回复入队。"""

from __future__ import annotations

import os
import queue
import time
import re
import shutil
import unicodedata
import uuid
from pathlib import Path

from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.config import DEFAULT_CONFIG
from channel.wechat_desktop.contracts import EVENT_TERMINALS
from channel.wechat_desktop.references import reference_requires_uia
from common.log import logger


class WechatDesktopMaterializeMixin:
    """物化线程：解析附件、折叠批次，并把稳定事件写入回复 FIFO。"""

    def _submit_materialization(self, events: list[WechatDesktopEvent]):
        """原子地提交一个消息批次到附件物化线程。"""
        if not events:
            return False
        with self._materialize_submit_lock:
            if self._stop_event.is_set():
                accepted = False
            else:
                try:
                    self._materialize_queue.put_nowait(events)
                    accepted = True
                except queue.Full:
                    accepted = False
        if not accepted:
            event_ids = [event.event_id for event in events]
            terminal = "stopped" if self._stop_event.is_set() else "full"
            for event_id in event_ids:
                self._best_effort("mark_event_processed", self._store.mark_event_processed, event_id,
                                  "rejected" if terminal == "full" else "stopped", "materialize_queue_" + terminal)
            self._finish_lifecycle(event_ids, terminal)
            self._service.update_status(materialize_last_rejection=terminal)
        return accepted

    def _clear_pending_materializations(self) -> list[str]:
        """清空尚未开始的物化批次，供停止和网络故障收尾使用。"""
        event_ids = []
        with self._materialize_submit_lock:
            while True:
                try:
                    events = self._materialize_queue.get_nowait()
                except queue.Empty:
                    break
                event_ids.extend(event.event_id for event in events)
                self._materialize_queue.task_done()
        return list(dict.fromkeys(event_ids))

    @staticmethod
    def _attachment_marker(event: WechatDesktopEvent) -> str:
        """将已落盘附件表示成可放入 Agent 上下文的稳定文本标记。"""
        if event.content_type == "file":
            return f"[文件: {event.content}]"
        if event.content_type == "image":
            return f"[图片: {event.content}]"
        return str(event.content or "")

    @staticmethod
    def _requires_attachment_reference(event: WechatDesktopEvent) -> bool:
        """判断用户是否在未引用具体附件的情况下要求读取“这张图/文件”。

        这种请求无法可靠确定目标，不能猜测最近附件；消费者会直接提示用户使用
        微信引用功能后重试。生成图片之类的创作意图不属于此情况。
        """
        if event.content_type != "text":
            return False
        # 只要有明确引用，Agent 就已经有了唯一目标。被引用的文字本身可能在讨论
        # 图片或文件；若把其措辞当成“未引用附件请求”，会错误地用提示语替换回复。
        if event.reference:
            return False
        text = unicodedata.normalize("NFKC", str(event.content or "")).strip()
        if not text or re.search(r"(?:生成|创建|制作|画一张|做一张)", text):
            return False
        attachment = re.search(
            r"(?:图(?:片|像)?|照片|截图|文件|文档|附件|表格|PDF)",
            text,
            re.IGNORECASE,
        )
        question = re.search(
            r"(?:是什么|是啥|什么内容|讲了?什么|写了?什么|说了?什么|"
            r"内容是|分析|总结|概括|解读|看一下|看看|读一下|读取)",
            text,
            re.IGNORECASE,
        )
        return bool(attachment and question)

    def _materialize_batch(
        self, events: list[WechatDesktopEvent]
    ) -> WechatDesktopEvent:
        """解析批次附件，并把多条消息折叠为一个可回复事件。

        最后一条消息是回复目标，前面的同批消息并入它的历史。只有显式引用的附件
        才会继续出现在普通文本上下文中，防止 Agent 把缓存中的旧图片误当成目标。
        返回事件上的 ``_source_event_ids`` 和 ``_batch_id`` 用于生命周期追踪。
        """
        resolved_events = []
        for event in events:
            event, resolve_count = self._driver.materialize_event(event)
            if not self._source_invalid_for_context(event):
                self._preserve_event_evidence(event)
            resolved_events.append(event)
            self._mark_lifecycle(
                [event.event_id],
                "materialized",
                attachment_resolve_count=resolve_count,
            )

        target = resolved_events[-1]
        batch_id = str(
            events[-1].task.batch_id or uuid.uuid4().hex
        )
        source_event_ids = [event.event_id for event in resolved_events]
        invalid_ids = {
            identity
            for event in resolved_events if self._source_invalid_for_context(event)
            for identity in (event.message_stable_id, event.source_message_id)
            if identity
        }
        # 已撤回或变化的同批消息不能以旧历史或聚合正文绕过来源校验。
        # 身份和生命周期仍保留在 source_event_ids，不改变接收账本。
        history = [
            item for item in target.history
            if not any(str(item.get(key) or "") in invalid_ids
                       for key in ("_message_stable_id", "source_message_id"))
            and (not target.reference or item.get("is_reference"))
        ]
        existing = {
            (str(item.get("content") or ""), str(item.get("content_type") or ""))
            for item in history
        }
        for event in resolved_events[:-1]:
            if target.reference or self._source_invalid_for_context(event):
                continue
            content = self._attachment_marker(event)
            key = (content, event.content_type)
            replaced_history_item = False
            if event.message_stable_id:
                for item in history:
                    if (
                        str(item.get("_message_stable_id") or "")
                        == event.message_stable_id
                    ):
                        item["content"] = content
                        item["content_type"] = event.content_type
                        replaced_history_item = True
                        existing.add(key)
                        break
            if replaced_history_item:
                continue
            if key not in existing:
                history.append(
                    {
                        "sender_name": event.sender_name,
                        "content": content,
                        "content_type": event.content_type,
                        "_message_stable_id": event.message_stable_id,
                        "source_message_id": event.source_message_id,
                    }
                )
                existing.add(key)
        reference_type = str(target.reference.get("content_type") or "").lower()
        if (
            target.content_type == "text"
            and reference_type not in {"file", "image"}
        ):
            # 缓存附件不能作为隐式上下文。文字消息必须明确引用目标附件后，
            # Agent 才能看到该附件。
            history = [
                item
                for item in history
                if str(item.get("content_type") or "") not in {"file", "image"}
            ]
        target.history = history
        target.task.source_event_ids = source_event_ids
        target.task.batch_id = batch_id
        # 队列等待期间前序来源也可能被撤回。保存轻量原生证据供投递时复核，
        # 不复制 task 或历史，避免循环引用与重复保存整个聚合上下文。
        target.task.context_source_events = [
            WechatDesktopEvent(**{**event.to_dict(), "history": []})
            for event in resolved_events[:-1]
            if event.source_message_id and not target.reference
            and not self._source_invalid_for_context(event)
        ]
        target.task.cache_only = bool(resolved_events) and all((event.content_type == 'file' and (not event.reference) for event in resolved_events))
        target.task.file_cache_results = {
            event.event_id: bool(
                event.attachment_status == "materialized" and os.path.isfile(event.content)
            )
            for event in resolved_events
            if event.content_type == "file" and not event.reference
        }
        target.task.attachment_reference_required = self._requires_attachment_reference(target)
        self._mark_lifecycle(
            source_event_ids,
            "materialized",
            batch_id=batch_id,
        )
        return target

    @staticmethod
    def _source_invalid_for_context(event: WechatDesktopEvent) -> bool:
        return (event.attachment_status == "source_invalid"
                or str(event.reference.get("fetch_status") or "").lower() == "source_invalid")

    @staticmethod
    def _has_referenced_attachment(event: WechatDesktopEvent) -> bool:
        """需要界面操作的引用推迟到 FIFO 队首；已有链接交给 Agent。"""
        return reference_requires_uia(event.reference)

    def _prepare_deferred_materialization(
        self, events: list[WechatDesktopEvent]
    ) -> WechatDesktopEvent:
        """为引用附件创建轻量队列项，把实际解析推迟到回复 FIFO 队首。

        图片查看器和原文件定位会操作当前微信窗口；若在物化线程提前执行，可能与
        正在发送的上一条回复抢占窗口，因此这里只保存原始事件列表。
        """
        target = events[-1]
        source_event_ids = [event.event_id for event in events]
        batch_id = uuid.uuid4().hex
        target.task.source_event_ids = source_event_ids
        target.task.batch_id = batch_id
        target.task.deferred_materialization_events = list(events)
        target.task.attachment_reference_required = False
        return target

    def _consume_materialization_queue(self):
        """后台解析附件，并把结果转交回复 FIFO 或仅作为文件缓存结束。"""
        while not self._stop_event.is_set():
            try:
                events = self._materialize_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            event_ids = [event.event_id for event in events]
            materialization_active = getattr(
                self, "_materialization_active", None
            )
            if materialization_active is not None:
                materialization_active.set()
            try:
                config = getattr(self, "config", None) or DEFAULT_CONFIG
                max_wait = config.get(
                    "reply_queue_max_wait_seconds",
                    DEFAULT_CONFIG["reply_queue_max_wait_seconds"],
                )
                if time.monotonic() - events[-1].task.created_at >= max_wait:
                    for event_id in event_ids:
                        self._best_effort("mark_event_processed", self._store.mark_event_processed, event_id, "expired", "materialization_wait_expired")
                    self._finish_lifecycle(event_ids, "expired")
                    continue
                if self._has_referenced_attachment(events[-1]):
                    materialized = self._prepare_deferred_materialization(events)
                else:
                    materialized = self._materialize_batch(events)
                if self._stop_event.is_set():
                    for event_id in event_ids:
                        self._store.mark_event_processed(event_id, "stopped")
                    self._finish_lifecycle(event_ids, "stopped")
                elif bool(materialized.task.cache_only):
                    for event_id in event_ids:
                        cached = materialized.task.file_cache_results.get(event_id, False)
                        state = "cached" if cached else "observed"
                        self._store.mark_event_processed(
                            event_id, state, "" if cached else "attachment_unavailable"
                        )
                        self._finish_lifecycle([event_id], state)
                    self._trace(
                        "09-file-cache",
                        "conversation=%s messages=%s cached=%s",
                        materialized.conversation_name,
                        len(event_ids),
                        sum(materialized.task.file_cache_results.values()),
                    )
                else:
                    self._enqueue_reply_event(materialized)
            except Exception:
                logger.exception("[WechatDesktop] message materialization failed")
                for event_id in event_ids:
                    self._best_effort("mark_event_processed", self._store.mark_event_processed, event_id, "failed", "materialization_failed")
                self._best_effort("finish_lifecycle", self._finish_lifecycle, event_ids, "failed")
            finally:
                if materialization_active is not None:
                    materialization_active.clear()
                self._materialize_queue.task_done()

    def _enqueue_reply_event(self, event: WechatDesktopEvent):
        """把稳定事件写入全局回复 FIFO，并标记其生命周期进入排队阶段。"""
        source_event_ids = list(
            event.task.source_event_ids or [event.event_id]
        )
        if any(
            self._store.event_state(event_id).get("state") in EVENT_TERMINALS
            for event_id in source_event_ids
        ):
            self._finish_lifecycle(source_event_ids, "duplicate")
            return False
        self._store.set_event_state(source_event_ids, "queued")
        enqueue_result = self._reply_queue.enqueue(event)
        if not enqueue_result:
            if enqueue_result.action == "duplicate":
                return False  # 已有活跃任务，不改写其状态。
            for event_id in source_event_ids:
                self._store.mark_event_processed(event_id,
                                                 "rejected" if enqueue_result.action == "full" else "stopped",
                                                 "reply_queue_" + enqueue_result.action)
            self._finish_lifecycle(source_event_ids, enqueue_result.action)
            self._service.update_status(queue_last_rejection=enqueue_result.action, **self._reply_queue.status())
            return False
        self._mark_lifecycle(
            source_event_ids,
            "queued",
            batch_id=str(event.task.batch_id or event.event_id),
        )
        self._trace(
            "09-queued",
            "id=%s conversation=%s action=%s queue_depth=%s",
            event.event_id[:10],
            event.conversation_name,
            enqueue_result.action,
            self._reply_queue.status()["queue_depth"],
        )
        self._service.update_status(**self._reply_queue.status())
        return True

    def _preserve_event_evidence(self, event: WechatDesktopEvent):
        """把临时截图/附件复制到 Agent 工作区，避免微信缓存清理后路径失效。"""
        source = str(event.evidence_path or "")
        if not source or not os.path.isfile(source):
            return
        evidence_dir = self._store.evidence_dir
        evidence_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(source).suffix or ".png"
        target = evidence_dir / f"{event.event_id}{suffix}"
        if target.resolve().parent != evidence_dir.resolve():
            raise ValueError("invalid evidence event identifier")
        try:
            if Path(source).resolve() == target.resolve():
                return
            shutil.copy2(source, target)
            try:
                self._store.set_event_evidence(event.event_id, source, str(target))
            except Exception:
                # 复制成功但登记失败，不能留下数据库无法追踪的副本。
                target.unlink(missing_ok=True)
                raise
            old_source = event.evidence_path
            event.evidence_path = str(target)
            if event.content_type == "image" and event.content == old_source:
                event.content = str(target)
            if event.reference.get("file_path") == old_source:
                event.reference["file_path"] = str(target)
        except OSError as exc:
            logger.warning(f"[WechatDesktop] failed to preserve evidence: {exc}")
