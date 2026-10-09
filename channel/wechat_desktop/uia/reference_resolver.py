"""引用解析：定位被引用的原消息，验证身份并恢复当前消息位置。"""

from __future__ import annotations
from dataclasses import replace
from common.log import logger
from channel.wechat_desktop.models import UiaChatMessage, UiaReferencedMessage
from channel.wechat_desktop.uia.controls import MESSAGE_LIST_ID, _text, _bounds, _runtime_id


class WechatReferenceResolver:
    def __init__(self, client):
        self.client = client

    def _pick_located_original_control(
        self, message_list, message_type: str, preview: str
    ):
        client = self.client
        list_bounds = _bounds(message_list)
        if not list_bounds:
            return None
        center_y = (list_bounds[1] + list_bounds[3]) / 2
        preview_folded = _text(preview).casefold()
        expected_classes = {
            "text": ("chattextitemview",),
            "file": ("chatbubbleitemview", "chatfileitemview"),
            "image": ("chatbubblereferitemview", "chatimageitemview"),
        }
        ranked = []
        preview_matches = []
        for control in message_list.GetChildren():
            class_name = _text(control.ClassName).casefold()
            if "chat" not in class_name or "itemview" not in class_name:
                continue
            bounds = _bounds(control)
            if not bounds:
                continue
            name = _text(control.Name)
            # The current quoted bubble may remain in the viewport and contains
            # the same preview text. It is never a valid original candidate.
            if client._parse_reference_label(name) is not None:
                continue
            if (message_type in {"file", "image", "share_card"}
                    and client._message_type(_text(control.ClassName), name) != message_type):
                continue
            row_center = (bounds[1] + bounds[3]) / 2
            score = -abs(row_center - center_y)
            if any(
                expected in class_name
                for expected in expected_classes.get(message_type, ())
            ):
                score += 240
            if preview_folded and preview_folded in name.casefold():
                score += 320
            if message_type == "file" and client._file_card_metadata(name)[0]:
                score += 200
            ranked.append((score, control))
            normalized_name = client._normalized_reference_text(name)
            normalized_preview = client._normalized_reference_text(preview)
            if normalized_preview and normalized_preview in normalized_name:
                preview_matches.append(control)
        # The locate bar normally centers the original, but a viewport can
        # contain repeated text. Never resolve an ambiguous quote by proximity.
        if len(preview_matches) == 1:
            return preview_matches[0]
        if len(preview_matches) > 1:
            return None
        return max(ranked, key=lambda item: item[0])[1] if ranked and not preview else None

    def _return_to_reference(self, *, validate_target=None) -> bool:
        client = self.client
        def require():
            if validate_target is not None and validate_target() is False:
                raise RuntimeError("attachment_target_changed")

        require()
        button = client._find_desktop_control(
            "回到引用位置", "mmui::UnreadBarView"
        )
        if button is None:
            return False
        require()
        if not client._click_control_point(button):
            require()
            return False
        require()
        client._paced_wait(
            "uia_reference_return_settle_ms_min",
            "uia_reference_return_settle_ms_max",
        )
        require()
        return True

    def resolve_message_reference(
        self, message: UiaChatMessage, *, allow_filename_cache: bool = True, validate_target=None,
    ) -> UiaChatMessage:
        """在当前账号、窗口和消息身份持续有效时定位一层引用。"""
        client = self.client
        reference = message.reference
        if reference is None or reference.message_type == "image":
            return message
        strict = not allow_filename_cache
        invalidated = False

        def require():
            nonlocal invalidated
            if invalidated:
                raise RuntimeError("attachment_target_changed")
            try:
                if validate_target is not None and validate_target() is False:
                    raise RuntimeError("attachment_target_changed")
            except Exception:
                invalidated = True
                raise

        def wait(prefix):
            require()
            client._paced_wait(prefix + "_ms_min", prefix + "_ms_max")
            require()

        def unavailable(strategy):
            return replace(message, reference=replace(
                reference, resolved=False, degraded=True, strategy=strategy))

        if strict and not message.runtime_id:
            return unavailable("uia_reference_runtime_unavailable")
        if strict and not client._normalized_reference_text(reference.content):
            return unavailable("uia_reference_preview_unavailable")
        if not strict and not message.bounds:
            return message
        located = restored = message_list_verified = False
        resolved_message = message
        original = None
        try:
            require()
            client.focus_window()
            require()
            with client.operation_lock, client._uia_root() as root:
                require()
                bounds = message.bounds
                if strict:
                    current = client._find_message_control(root, message)
                    if current is None or _runtime_id(current) != message.runtime_id:
                        return unavailable("uia_reference_runtime_changed")
                    bounds = _bounds(current)
                if not bounds:
                    return unavailable("uia_reference_bounds_unavailable") if strict else message
                left, top, right, bottom = bounds
                if right <= left or bottom <= top:
                    return unavailable("uia_reference_bounds_unavailable") if strict else message
                point = (int(left + (right - left) * 0.25), int(top + (bottom - top) * 0.72))
                require()
                client._right_click_point(point)
                require()
                wait("uia_reference_menu_settle")
                menu_item = client._find_desktop_control("定位到原文位置", "mmui::XMenuView")
                require()
                if menu_item is None:
                    return unavailable("uia_reference_menu_unavailable") if strict else message
                require()
                located = bool(client._click_control_point(menu_item))
                require()
                if not located:
                    return unavailable("uia_reference_locate_failed") if strict else message
                wait("uia_reference_locate_settle")
                message_list = next((control for control in client._walk(root)
                                     if _text(control.AutomationId) == MESSAGE_LIST_ID), None)
                require()
                if message_list is None:
                    return unavailable("uia_reference_list_unavailable") if strict else message
                message_list_verified = True
                control = client._pick_located_original_control(
                    message_list, reference.message_type, reference.content)
                require()
                if control is not None:
                    content, class_name = _text(control.Name), _text(control.ClassName)
                    original_type = client._message_type(class_name, content)
                    can_refine = reference.message_type in {"unknown", "app_message", "unsupported"} or (
                        reference.message_type == "text" and not reference.resolved)
                    if not can_refine and original_type != reference.message_type:
                        resolved_message = unavailable("uia_reference_type_mismatch")
                    elif original_type == "share_card" and not client._share_card_matches_preview(content, reference.content):
                        resolved_message = unavailable("uia_reference_preview_mismatch")
                    else:
                        original = UiaChatMessage(
                            reference.sender_name, content, original_type, "unknown",
                            _runtime_id(control), _bounds(control))
                else:
                    resolved_message = unavailable("uia_reference_original_ambiguous")
            if original is not None:
                require()
                file_path = share_url = browser_content = ""
                if original.message_type == "image":
                    file_path = (client.fetch_message_image(original, strict=True, validate_target=require)
                                 if strict else client.fetch_message_image(original))
                elif original.message_type == "file":
                    file_path = (client.fetch_message_file(original, allow_filename_cache=False,
                                                           validate_target=require)
                                 if strict else client.fetch_message_file(original))
                require()
                title, platform = client._share_card_metadata(original.content)
                if original.message_type == "share_card" and original.bounds:
                    require()
                    browser_content, share_url = (client.fetch_share_message_page(
                        original, strict=True, validate_target=require) if strict else
                        client.fetch_share_page_from_point(client._share_card_activation_point(original.bounds)))
                    require()
                available = (bool(file_path) if original.message_type in {"file", "image"} else
                             bool(browser_content or share_url) if original.message_type == "share_card" else
                             bool(original.content))
                resolved_reference = UiaReferencedMessage(
                    reference.sender_name, title if original.message_type == "share_card" else original.content,
                    original.message_type, file_path=file_path, resolved=available, degraded=not available,
                    strategy=("wechat_share_browser_direct_read" if browser_content else
                              "wechat_share_browser_copy_link" if share_url else "wechat_locate_original"),
                    original_content=original.content, url=share_url, platform=platform,
                    browser_content=browser_content, browser_status="success" if browser_content else "unavailable")
                resolved_message = replace(message, reference=resolved_reference)
        except Exception as exc:
            if strict and invalidated:
                raise
            logger.warning("[WechatDesktop] reference resolution failed (%s)", type(exc).__name__)
            resolved_message = unavailable("uia_reference_resolution_failed")
        finally:
            # 失效后不能再点返回栏、向新窗口按 End 或恢复旧窗口。
            if located and not invalidated:
                try:
                    require()
                    restored = (self._return_to_reference(validate_target=require) if strict else
                                client._return_to_reference())
                    require()
                    if not restored and message_list_verified:
                        require()
                        client._press_end_key()
                        require()
                        wait("uia_reference_return_settle")
                        restored = True
                except Exception as exc:
                    if strict and invalidated:
                        raise
                    logger.warning("[WechatDesktop] reference return failed (%s)", type(exc).__name__)
        if (resolved_message.reference is not None
                and resolved_message.reference.message_type == "share_card"):
            if strict and resolved_message.reference.strategy in {
                    "uia_reference_type_mismatch", "uia_reference_preview_mismatch",
                    "uia_reference_original_ambiguous"}:
                return resolved_message
            if resolved_message.reference.browser_content or resolved_message.reference.url:
                return resolved_message
            if invalidated or not restored:
                return replace(resolved_message, reference=replace(
                    resolved_message.reference, resolved=False, degraded=True,
                    strategy="wechat_locate_original_return_failed"))
            require()
            browser_content, url = (client.fetch_referenced_share_page(
                resolved_message, strict=True, validate_target=require) if strict else
                client.fetch_referenced_share_page(resolved_message))
            require()
            return replace(resolved_message, reference=replace(
                resolved_message.reference, url=url, browser_content=browser_content,
                browser_status="success" if browser_content else "unavailable",
                resolved=bool(browser_content or url), degraded=not bool(browser_content or url),
                strategy="wechat_share_browser_direct_read" if browser_content else "wechat_share_browser_copy_link"))
        return resolved_message
