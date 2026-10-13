"""严格定位合并转发卡片，收集合并记录窗口的有界可见证据。

截图只能作为后续 Vision 的证据，不能把截图路径或 UIA 图片标签当作媒体正文。
不按数据库条目数量与 UIA 控件顺序配对，未知原生类型继续保持未读。
"""

from __future__ import annotations

import hashlib
import math
import time
from pathlib import Path
from contextlib import contextmanager

from channel.wechat_desktop.config import DEFAULT_CONFIG
from channel.wechat_desktop.uia.controls import HISTORY_WINDOW_CLASS, _bounds, _runtime_id, _text


class WechatChatRecordReader:
    def __init__(self, client):
        self.client = client

    def _setting(self, key, minimum, maximum):
        return max(minimum, min(maximum, int(self.client.config.get(key, DEFAULT_CONFIG[key]))))

    @staticmethod
    def _native_identity(hwnd):
        import win32gui
        import win32process

        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return (int(hwnd), int(pid), _text(win32gui.GetWindowText(hwnd)),
                _text(win32gui.GetClassName(hwnd)))

    def _new_windows(self, main_identity, visible_before, require, light_require=None):
        """仅观察本次点击新变为可见的同进程窗口，不猜普通历史窗口。"""
        import uiautomation as auto

        windows = []
        for hwnd in sorted(self.client._visible_top_level_window_handles() - visible_before):
            require()
            identity = self._native_identity(hwnd)
            if (identity is None or identity[0] == main_identity[0] or
                    identity[1] != main_identity[1] or identity[3] == HISTORY_WINDOW_CLASS):
                continue
            if len(windows) >= 5:
                break
            with auto.UIAutomationInitializerInThread():
                root = auto.ControlFromHandle(hwnd)
                require()
                controls = [root]
                for node in self.client._walk(root, self._setting("chat_record_uia_max_nodes", 20, 2000)):
                    (light_require or require)()
                    controls.append(node)
                summary = {"hwnd": identity[0], "title": identity[2][:200],
                    "class_name": identity[3], "automation_id": _text(root.AutomationId),
                    "uia_class_name": _text(root.ClassName),
                    "control_summary": [{"class_name": _text(node.ClassName),
                        "automation_id": _text(node.AutomationId),
                        "control_type": _text(node.ControlTypeName)} for node in controls[:80]]}
                texts = []
                remaining = self._setting("chat_record_uia_max_chars", 100, 30000)
                for node in controls:
                    (light_require or require)()
                    value = _text(node.Name)
                    if value and remaining:
                        texts.append(value[:remaining])
                        remaining -= len(texts[-1])
                windows.append((identity, summary, texts))
            require()
        return windows

    @staticmethod
    def _matches_native_record(summary, record):
        """实测 RecordDetailWindow 的 AutomationId 直接绑定原消息服务端 ID。"""
        source_id = _text(record.get("source_message_id"))
        if (not source_id.isascii() or not source_id.isdecimal() or len(source_id) > 20 or
                not 0 < int(source_id) < 2 ** 64):
            return False
        return bool(summary.get("uia_class_name") == "mmui::RecordDetailWindow"
            and summary.get("automation_id") == source_id + ":"
            and _text(summary.get("title")) == _text(record.get("title")))

    @staticmethod
    def _matches_record(texts, record):
        title = _text(record.get("title"))
        if not title or not any(title in value for value in texts):
            return False
        anchors = [_text(entry.get("content")) for entry in record.get("entries", [])
                   if entry.get("content_type") == "text" and entry.get("resolved")]
        anchors = [anchor for anchor in anchors if len(anchor) >= 4]
        # 标题是卡片元数据，必须再用数据库中实际文本证明此窗口属于原记录。
        return bool(anchors and any(anchor in value for anchor in anchors for value in texts))

    def _window_root(self, identity, main_identity, require, source_id="", *, require_foreground=True):
        import uiautomation as auto
        import win32gui

        require()
        if self._native_identity(identity[0]) != identity:
            raise RuntimeError("chat_record_window_changed")
        if self._native_identity(main_identity[0]) != main_identity:
            raise RuntimeError("attachment_target_changed")
        if require_foreground and int(win32gui.GetForegroundWindow() or 0) != identity[0]:
            raise RuntimeError("chat_record_window_not_foreground")
        root = auto.ControlFromHandle(identity[0])
        if source_id and (_text(root.ClassName) != "mmui::RecordDetailWindow"
                or _text(root.AutomationId) != source_id + ":"):
            raise RuntimeError("chat_record_window_changed")
        return root

    def _visible_page(self, root, window_bounds, require=None):
        max_nodes = self._setting("chat_record_uia_max_nodes", 20, 2000)
        controls = [root]
        for node in self.client._walk(root, max_nodes):
            if require is not None:
                require()
            controls.append(node)
        texts, scrollables, record_lists = [], [], []
        for node in controls:
            if require is not None:
                require()
            bounds = _bounds(node)
            visible = bool(bounds and bounds[2] > bounds[0] and bounds[3] > bounds[1]
                and bounds[2] > window_bounds[0] and bounds[0] < window_bounds[2]
                and bounds[3] > window_bounds[1] and bounds[1] < window_bounds[3])
            if not visible:
                continue
            name = _text(node.Name)
            if name and name not in texts:
                texts.append(name)
            if _text(node.ClassName) == "mmui::RecyclerListView":
                record_lists.append(node)
            elif _text(node.ControlTypeName) == "ListControl":
                scrollables.append(node)
        # 列表有唯一可见候选才能滚动；不会把任意容器当作记录列表。
        if record_lists:
            scrollable = record_lists[0] if len(record_lists) == 1 else None
        else:
            scrollable = scrollables[0] if len(scrollables) == 1 else None
        return texts, scrollable

    def _visible_image_items(self, root, window_bounds, light_require):
        items = []
        for control in self.client._walk(root, self._setting("chat_record_uia_max_nodes", 20, 2000)):
            light_require()
            if (_text(control.ClassName) != "mmui::ChatBubbleReferItemView"
                    or "图片" not in _text(control.Name)):
                continue
            runtime = _runtime_id(control)
            bounds = _bounds(control)
            if (not runtime or not bounds or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]
                    or bounds[2] <= window_bounds[0] or bounds[0] >= window_bounds[2]
                    or bounds[3] <= window_bounds[1] or bounds[1] >= window_bounds[3]):
                continue
            items.append((runtime, _text(control.Name), control))
        return items

    @staticmethod
    def _viewport_bounds(scrollable, window_bounds):
        bounds = _bounds(scrollable) if scrollable is not None else None
        if not bounds:
            return window_bounds
        clipped = (max(bounds[0], window_bounds[0]), max(bounds[1], window_bounds[1]),
                   min(bounds[2], window_bounds[2]), min(bounds[3], window_bounds[3]))
        return clipped if clipped[2] > clipped[0] and clipped[3] > clipped[1] else window_bounds

    def _image_visual_key(self, control, page_image, window_bounds, viewport_bounds):
        """图片区域参与身份，防止 Recycler 复用同名 RuntimeId；不读取未知媒体像素。"""
        if page_image is None:
            return ()
        image_bounds = self.client._image_activation_bounds(control, None)
        if not image_bounds:
            return ()
        left, top = max(image_bounds[0], viewport_bounds[0]), max(image_bounds[1], viewport_bounds[1])
        right, bottom = min(image_bounds[2], viewport_bounds[2]), min(image_bounds[3], viewport_bounds[3])
        if right <= left or bottom <= top:
            return ()
        # Recycler 的图片行有时包含整段列表宽度；右侧滑块位置不属于图片身份。
        # 是否完整仍按原始可见边界判断，裁去栏位不会将完整图片误认作部分图片。
        complete = (left, top, right, bottom) == tuple(image_bounds)
        gutter = self._setting("chat_record_uia_thumbnail_right_gutter_pixels", 0, 24)
        hash_right = min(right, viewport_bounds[2] - gutter)
        if hash_right <= left:
            return ()
        crop = page_image.crop((left - window_bounds[0], top - window_bounds[1],
                               hash_right - window_bounds[0], bottom - window_bounds[1]))
        digest = hashlib.sha256(crop.convert("RGB").tobytes()).hexdigest()
        return (hash_right - left, bottom - top, complete, digest)

    @contextmanager
    def _park_record_cursor(self, identity, main_identity, source_id, budget, require_source):
        """让截图避开图片上的光标高亮；仅来源和本次窗口有效且仍为自己的位置时恢复。"""
        import win32api
        import win32gui

        original = win32api.GetCursorPos()
        parked = None

        def require_native(require):
            require()
            if (self._native_identity(identity[0]) != identity
                    or self._native_identity(main_identity[0]) != main_identity
                    or int(win32gui.GetForegroundWindow() or 0) != identity[0]):
                raise RuntimeError("chat_record_window_not_foreground")

        def park():
            nonlocal parked
            require_native(budget)
            left, top, right, bottom = win32gui.GetWindowRect(identity[0])
            point = (int(left + min(60, max(1, (right - left) // 4))), int(top + 10))
            if win32api.GetCursorPos() != point:
                win32api.SetCursorPos(point)
                parked = point
                require_native(budget)
                self.client._paced_wait("uia_chat_record_scroll_settle_ms_min", "uia_chat_record_scroll_settle_ms_max")
                require_native(budget)
            return point

        self._window_root(identity, main_identity, budget, source_id)
        try:
            yield park
        finally:
            if (parked is not None and win32api.GetCursorPos() == parked
                    and int(win32gui.GetForegroundWindow() or 0) == identity[0]):
                # 阅读超时仍可恢复本次光标；来源变化、用户移动或另一前台窗口则不再操作。
                self._window_root(identity, main_identity, require_source, source_id)
                win32api.SetCursorPos(original)
                require_source()

    @staticmethod
    def _scroll_state(control):
        state = {"vertical_percent": None, "view_size": None, "at_bottom": False}
        if control is None:
            return state
        try:
            pattern = control.GetScrollPattern()
            if pattern is None:
                return state
            percent = float(pattern.VerticalScrollPercent)
            if math.isfinite(percent) and 0 <= percent <= 100:
                state["vertical_percent"] = round(percent, 3)
                state["at_bottom"] = percent >= 99.99
        except Exception:
            return state
        try:
            size = float(pattern.VerticalViewSize)
            if math.isfinite(size) and 0 < size <= 100:
                state["view_size"] = round(size, 3)
        except Exception:
            pass
        return state

    def _page_fingerprint(self, root, window_bounds, viewport_bounds, image, texts,
                          scroll_state, light_require):
        rows = []
        for control in self.client._walk(root, self._setting("chat_record_uia_max_nodes", 20, 2000)):
            light_require()
            class_name = _text(control.ClassName)
            if (_text(control.ControlTypeName) != "ListItemControl"
                    and not class_name.startswith("mmui::ChatBubble")):
                continue
            bounds = _bounds(control)
            if (not bounds or bounds[2] <= viewport_bounds[0] or bounds[0] >= viewport_bounds[2]
                    or bounds[3] <= viewport_bounds[1] or bounds[1] >= viewport_bounds[3]):
                continue
            name = _text(control.Name)
            relative = tuple(int(value - viewport_bounds[index % 2]) for index, value in enumerate(bounds))
            visual = (self._image_visual_key(control, image, window_bounds, viewport_bounds)
                if class_name == "mmui::ChatBubbleReferItemView" and "图片" in name else ())
            rows.append((class_name, name, relative, visual))
        # 未知 37 动画、系统时钟和光标不参与进展；图片像素只取明确图片条目区域。
        payload = (tuple(rows) if rows else tuple(texts), scroll_state["vertical_percent"])
        return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def _evidence_digest(path):
        digest = hashlib.sha256()
        try:
            with Path(path).open("rb") as source:
                for chunk in iter(lambda: source.read(65536), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return ""

    def _capture_page_images(self, identity, main_identity, source_id, root, bounds,
                             target, seen, result, budget, light_budget, require_source, *,
                             page_image=None, window_bounds=None, content_seen=None, diagnostics=None,
                             image_asset_cache=None):
        max_images = self._setting("chat_record_uia_max_images", 1, 12)
        diagnostics = diagnostics if diagnostics is not None else {}
        diagnostics.update(image_candidates=0, image_attempts=0, image_captured=0,
                           image_seen_skips=0, image_content_duplicates=0, runtime_reuses=0,
                           image_partial_deferred=0, image_partial_forced=0, image_visibility=[],
                           image_asset_cache_hits=0, image_asset_keys=[])
        for runtime, name, control in self._visible_image_items(root, bounds, light_budget):
            diagnostics["image_candidates"] += 1
            visual = self._image_visual_key(control, page_image, window_bounds or bounds, bounds)
            image_bounds = self.client._image_activation_bounds(control, None)
            if visual and image_bounds:
                diagnostics["image_visibility"].append({"height": image_bounds[3] - image_bounds[1],
                    "visible_height": visual[1], "width": image_bounds[2] - image_bounds[0],
                    "visible_width": visual[0], "complete": visual[2]})
                if not visual[2]:
                    fits = (image_bounds[3] - image_bounds[1] <= bounds[3] - bounds[1]
                        and image_bounds[2] - image_bounds[0] <= bounds[2] - bounds[0])
                    first_top = diagnostics.get("index", 0) == 0 and image_bounds[1] < bounds[1]
                    last_page = diagnostics.get("at_bottom") or diagnostics.get("repeated")
                    if fits and not first_top and not last_page:
                        diagnostics["image_partial_deferred"] += 1
                        continue
                    diagnostics["image_partial_forced"] += 1
            image_key = (runtime, name, visual)
            # 完整缩略图在重叠页即使重建 RuntimeId，也有精确视觉证据可跳过。
            full_visual_seen = bool(visual and visual[2] and any(
                key[1] == name and key[2] == visual for key in seen))
            if image_key in seen or full_visual_seen:
                diagnostics["image_seen_skips"] += 1
                seen.add(image_key)
                continue
            if any(key[0] == runtime for key in seen):
                diagnostics["runtime_reuses"] += 1
            if len(result["image_evidence_paths"]) >= max_images:
                result["truncated"] = True
                diagnostics["image_budget_reached"] = True
                return

            def record_context():
                # 阅读超时仍可关闭并恢复本次已验证查看器；来源失效则停止所有清理。
                return self._window_root(identity, main_identity, require_source, source_id,
                    require_foreground=False)

            def before_click():
                self._window_root(identity, main_identity, budget, source_id)
                if (_runtime_id(control) != runtime or _text(control.Name) != name
                        or _text(control.ClassName) != "mmui::ChatBubbleReferItemView"):
                    raise RuntimeError("chat_record_image_identity_changed")

            before_click()
            if not image_bounds:
                continue
            left = max(image_bounds[0], bounds[0])
            top = max(image_bounds[1], bounds[1])
            right = min(image_bounds[2], bounds[2])
            bottom = min(image_bounds[3], bounds[3])
            if right - left < 4 or bottom - top < 20:
                continue
            point = (int(left + (right - left) * .25), int(top + (bottom - top) * .65))
            destination = target / f"wechat-record-image-{time.time_ns()}.png"
            metadata = {}
            diagnostics["image_attempts"] += 1
            path = self.client._capture_image_viewer_from_point(point, destination,
                validate_before_click=before_click, validate_context=record_context,
                return_hwnd=identity[0], validate_return_window=record_context,
                prefer_original=True, capture_metadata=metadata,
                **({"image_asset_cache": image_asset_cache} if image_asset_cache is not None else {}))
            if metadata.get("asset_key_hash"):
                diagnostics["image_asset_keys"].append(metadata["asset_key_hash"])
            diagnostics["image_asset_cache_hits"] += bool(metadata.get("asset_cache_hit"))
            if not path and metadata.get("failure_reason"):
                diagnostics["viewer_failure"] = {key: metadata[key] for key in
                    ("failure_reason", "viewer_left_open", "return_window_restored", "detection_attempts")
                    if key in metadata}
                if not metadata.get("return_window_restored"):
                    self._window_root(identity, main_identity, require_source, source_id, require_foreground=False)
                    raise RuntimeError("chat_record_" + metadata["failure_reason"])
            self._window_root(identity, main_identity, require_source, source_id)
            if path:
                seen.add(image_key)
                # 部分缩略图后来完整出现可重新采集，实际原图相同则只发布一次证据。
                digest = self._evidence_digest(path) if content_seen is not None else ""
                if digest and digest in content_seen:
                    diagnostics["image_content_duplicates"] += 1
                else:
                    if digest and content_seen is not None:
                        content_seen.add(digest)
                    result["image_evidence_paths"].append(path)
                    result["image_evidence"].append({"path": path, **metadata})
                    diagnostics["image_captured"] += 1
            light_budget()

    def _scroll(self, control, require):
        if control is None:
            return False
        try:
            pattern = control.GetScrollPattern()
            if pattern is not None and getattr(pattern, "VerticallyScrollable", False):
                import uiautomation as auto

                require()
                pattern.Scroll(auto.ScrollAmount.NoAmount, auto.ScrollAmount.SmallIncrement, waitTime=.1)
                require()
                return True
        except Exception:
            require()
        try:
            require()
            control.WheelDown(wheelTimes=self._setting("chat_record_uia_scroll_wheel_times", 1, 2),
                              interval=.03, waitTime=.1)
            require()
            return True
        except Exception:
            require()
            return False

    def _close_window(self, identity, main_identity, require, source_id=""):
        """只点归属验证过的本次辅助窗内关闭按钮；无全局 Esc/WM_CLOSE。"""
        import uiautomation as auto

        with auto.UIAutomationInitializerInThread():
            root = self._window_root(identity, main_identity, require, source_id)
            buttons = []
            for node in self.client._walk(root, 150):
                if self._native_identity(identity[0]) != identity:
                    raise RuntimeError("chat_record_window_changed")
                if (_text(node.Name).casefold() in {"关闭", "close"}
                        and _text(node.ControlTypeName) == "ButtonControl"
                        and _text(node.ClassName) == "mmui::XButton"):
                    buttons.append(node)
            if len(buttons) != 1:
                return False
            button_runtime = _runtime_id(buttons[0])
            if not button_runtime:
                return False
            require()
            self._window_root(identity, main_identity, require, source_id)
            if (_runtime_id(buttons[0]) != button_runtime or
                    _text(buttons[0].Name).casefold() not in {"关闭", "close"} or
                    _text(buttons[0].ClassName) != "mmui::XButton"):
                return False
            clicked = bool(self.client._click_control_point(buttons[0]))
            require()
        if not clicked:
            return False
        # 窗口可能保留句柄但隐藏；无须二次点击旧控件。
        deadline = time.monotonic() + .5
        while self._native_identity(identity[0]) is not None and time.monotonic() < deadline:
            require()
            time.sleep(.03)
        require()
        return self._native_identity(identity[0]) is None

    def _restore_main(self, identity, require):
        """每次恢复原生主窗动作前后都复核来源；不使用无回调的重试 helper。"""
        import win32con
        import win32gui

        require()
        if self._native_identity(identity[0]) != identity:
            return False
        try:
            require()
            win32gui.ShowWindow(identity[0], win32con.SW_RESTORE)
            require()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                require()
                if int(win32gui.GetForegroundWindow() or 0) == identity[0]:
                    return True
                require()
                win32gui.BringWindowToTop(identity[0])
                require()
                win32gui.SetForegroundWindow(identity[0])
                require()
                time.sleep(.03)
            require()
            return int(win32gui.GetForegroundWindow() or 0) == identity[0]
        except Exception:
            require()
            return False

    def fetch_referenced_chat_record(self, message, record, *, tmp_root,
            validate_target, validate_source=None, diagnostics_only=False):
        # 读取、关闭辅助窗及恢复主窗共用同一锁，不能在清理前让发送线程切换会话。
        with self.client.operation_lock:
            return self._fetch_referenced_chat_record(message, record, tmp_root=tmp_root,
                validate_target=validate_target, validate_source=validate_source,
                diagnostics_only=diagnostics_only)

    def _fetch_referenced_chat_record(self, message, record, *, tmp_root,
            validate_target, validate_source=None, diagnostics_only=False):
        result = {"status": "unavailable", "strategy": "uia_chat_record_unavailable",
            "windows": [], "visible_text": "", "evidence_paths": [], "page_count": 0,
            "image_evidence_paths": [], "image_evidence": [],
            "truncated": False, "media_status": "unread",
            "read_diagnostics": {"stop_reason": "", "scroll_count": 0, "pages": [],
                "expected_image_entries": sum(entry.get("content_type") == "image"
                    for entry in record.get("entries", []))}}
        if message.reference is None or message.reference.message_type != "chat_record":
            result["strategy"] = "uia_chat_record_type_mismatch"
            return result
        if validate_target is None:
            result["strategy"] = "uia_chat_record_validation_required"
            return result
        invalidated = False
        cleanup_allowed = True
        main_hwnd = self.client.get_owner_window_handle()
        main_identity = self._native_identity(main_hwnd)
        if main_identity is None:
            result["strategy"] = "uia_chat_record_main_window_unavailable"
            return result
        deadline = time.monotonic() + self._setting("chat_record_uia_timeout_seconds", 1, 180)

        def require():
            nonlocal invalidated
            if invalidated:
                raise RuntimeError("attachment_target_changed")
            try:
                if validate_source is not None and validate_source() is False:
                    raise RuntimeError("attachment_source_changed")
                if validate_target() is False or self._native_identity(main_hwnd) != main_identity:
                    raise RuntimeError("attachment_target_changed")
            except Exception:
                invalidated = True
                raise

        def budget():
            require()
            if time.monotonic() >= deadline:
                raise RuntimeError("uia_chat_record_timeout")

        def light_budget():
            # 节点读取没有 UI 动作，不为每个节点重复解密/刷新整个数据库。
            nonlocal invalidated
            if invalidated:
                raise RuntimeError("attachment_target_changed")
            if self._native_identity(main_hwnd) != main_identity:
                invalidated = True
                raise RuntimeError("attachment_target_changed")
            if time.monotonic() >= deadline:
                raise RuntimeError("uia_chat_record_timeout")

        verified_identity = None
        source_id = _text(record.get("source_message_id"))
        try:
            with self.client._reference_resolver.located_original_reference(message,
                    validate_target=budget, cleanup_allowed=lambda: cleanup_allowed) as (control, original, located_require):
                budget()
                if _runtime_id(control) != original.runtime_id or _text(control.Name) != original.content:
                    raise RuntimeError("uia_reference_original_runtime_changed")
                before = self.client._visible_top_level_window_handles()
                located_require()
                # 点击后可能有辅助窗覆盖主窗，在完成归属验证和关闭前禁止返回栏动作。
                cleanup_allowed = False
                self.client._click_control_point(control)
                located_require()
                self.client._paced_wait("uia_chat_record_open_settle_ms_min", "uia_chat_record_open_settle_ms_max")
                budget()
                windows = self._new_windows(main_identity, before, budget, light_budget)
                result["windows"] = [summary for _, summary, _ in windows]
                matches = [(identity, texts) for identity, summary, texts in windows
                    if (self._matches_native_record(summary, record) if source_id else self._matches_record(texts, record))]
                cleanup_allowed = not bool(windows)
                if len(matches) != 1:
                    result["strategy"] = "uia_chat_record_window_ambiguous" if windows else "uia_chat_record_window_unavailable"
                    return result
                verified_identity, _ = matches[0]
                result["strategy"] = "uia_chat_record_visible_evidence"
                if diagnostics_only:
                    result["status"] = "observed"
                    return result
                import uiautomation as auto
                import win32gui
                from PIL import ImageGrab

                target = Path(tmp_root).expanduser().resolve()
                target.mkdir(parents=True, exist_ok=True)
                max_pages = self._setting("chat_record_uia_max_pages", 1, 20)
                max_scrolls = self._setting("chat_record_uia_max_scrolls", 0, 30)
                max_chars = self._setting("chat_record_uia_max_chars", 100, 30000)
                text_pages, fingerprints = [], set()
                no_progress = scrolls = 0
                seen_images, seen_image_content = set(), set()
                # 缓存只覆盖这一次已验证的原记录读取，禁止跨请求或跨来源复用。
                image_asset_cache = {}
                diagnostics = result["read_diagnostics"]
                with auto.UIAutomationInitializerInThread(), self._park_record_cursor(
                        verified_identity, main_identity, source_id, budget, require) as park_cursor:
                    for index in range(max_pages + max_scrolls):
                        light_budget()
                        root = self._window_root(verified_identity, main_identity, light_budget, source_id)
                        bounds = tuple(int(value) for value in win32gui.GetWindowRect(verified_identity[0]))
                        texts, scrollable = self._visible_page(root, bounds, light_budget)
                        park_cursor()
                        budget()
                        text = "\n".join(texts)
                        image = ImageGrab.grab(bbox=bounds, all_screens=True)
                        budget()
                        viewport_bounds = self._viewport_bounds(scrollable, bounds)
                        scroll_state = self._scroll_state(scrollable)
                        fingerprint = self._page_fingerprint(root, bounds, viewport_bounds,
                            image, texts, scroll_state, light_budget)
                        repeated = fingerprint in fingerprints
                        page_diagnostic = {"index": index, "signature": fingerprint[:12],
                            "repeated": repeated, **scroll_state}
                        diagnostics["pages"].append(page_diagnostic)
                        if not repeated:
                            fingerprints.add(fingerprint)
                            available = max_chars - sum(len(page) for page in text_pages)
                            text_pages.append(text[:max(0, available)])
                            path = target / f"wechat-record-{time.time_ns()}-{index}.png"
                            image.save(path, format="PNG")
                            result["evidence_paths"].append(str(path))
                            result["page_count"] += 1
                            result["visible_text"] = "\n\n".join(text_pages)[:max_chars]
                            if len(text) > available:
                                result["truncated"] = True
                                diagnostics["stop_reason"] = "char_budget"
                                break
                        self._capture_page_images(verified_identity, main_identity, source_id,
                            root, viewport_bounds, target, seen_images, result, budget, light_budget, require,
                            page_image=image, window_bounds=bounds, content_seen=seen_image_content,
                            diagnostics=page_diagnostic, image_asset_cache=image_asset_cache)
                        no_progress = no_progress + 1 if repeated and not page_diagnostic.get("image_captured") else 0
                        if page_diagnostic.get("image_budget_reached"):
                            diagnostics["stop_reason"] = "image_budget"
                            break
                        if scroll_state["at_bottom"]:
                            diagnostics["stop_reason"] = "scroll_bottom"
                            break
                        if no_progress >= 2:
                            diagnostics["stop_reason"] = "no_progress"
                            break
                        if result["page_count"] >= max_pages or scrolls >= max_scrolls:
                            result["truncated"] = True
                            diagnostics["stop_reason"] = "page_budget" if result["page_count"] >= max_pages else "scroll_budget"
                            break
                        light_budget()
                        self._window_root(verified_identity, main_identity, light_budget, source_id)
                        if not self._scroll(scrollable, lambda: self._window_root(
                                verified_identity, main_identity, budget, source_id)):
                            result["truncated"] = scrollable is None
                            diagnostics["stop_reason"] = "scroll_unavailable"
                            break
                        scrolls += 1
                        diagnostics["scroll_count"] = scrolls
                        light_budget()
                        self.client._paced_wait("uia_chat_record_scroll_settle_ms_min", "uia_chat_record_scroll_settle_ms_max")
                        light_budget()
                result["visible_text"] = "\n\n".join(text_pages)[:max_chars]
                diagnostics["unique_image_evidence"] = len(result["image_evidence_paths"])
                # 数量仅提示覆盖不足，不据此把图片证据逐项绑定数据库。
                if diagnostics["unique_image_evidence"] < diagnostics["expected_image_entries"]:
                    result["truncated"] = True
                result["status"] = "partial" if result["evidence_paths"] or result["image_evidence_paths"] else "unavailable"
                result["media_status"] = "evidence_only" if result["evidence_paths"] or result["image_evidence_paths"] else "unread"
        except Exception as exc:
            if invalidated:
                raise
            result["strategy"] = str(exc) if str(exc).startswith(("uia_", "chat_record_")) else "uia_chat_record_failed"
            result["read_diagnostics"]["stop_reason"] = result["strategy"]
            result["truncated"] = bool(result["evidence_paths"])
            if result["evidence_paths"]:
                result["status"], result["media_status"] = "partial", "evidence_only"
        finally:
            result["read_diagnostics"]["unique_image_evidence"] = len(result["image_evidence_paths"])
            if verified_identity is not None and not invalidated:
                require()
                try:
                    closed = self._close_window(verified_identity, main_identity, require, source_id)
                except Exception:
                    if invalidated:
                        raise
                    closed = False
                if closed:
                    require()
                    restored = self._restore_main(main_identity, require)
                    require()
                    # 上面的定位上下文已退出；此时才可安全点主窗口的返回引用栏。
                    if restored:
                        self.client._reference_resolver.return_to_reference_in_main(validate_target=require)
                        require()
                        cleanup_allowed = True
                    else:
                        result["main_restore_failed"] = True
                else:
                    result["window_left_open"] = True
        return result
