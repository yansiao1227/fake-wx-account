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

    def _return_to_reference(self) -> bool:
        client = self.client
        button = client._find_desktop_control(
            "回到引用位置", "mmui::UnreadBarView"
        )
        if button is None:
            return False
        if not client._click_control_point(button):
            return False
        client._paced_wait(
            "uia_reference_return_settle_ms_min",
            "uia_reference_return_settle_ms_max",
            300,
            600,
        )
        return True

    def resolve_message_reference(
        self, message: UiaChatMessage
    ) -> UiaChatMessage:
        """Locate one quoted original, classify it, and materialize it safely."""
        client = self.client
        reference = message.reference
        if (
            reference is None
            or reference.message_type == "image"
            or not message.bounds
        ):
            return message
        left, top, right, bottom = message.bounds
        if right <= left or bottom <= top:
            return message
        point = (
            int(left + (right - left) * 0.25),
            int(top + (bottom - top) * 0.72),
        )
        located = False
        restored = False
        message_list_verified = False
        resolved_message = message
        original = None
        try:
            client.focus_window()
            with client.operation_lock, client._uia_root() as root:
                client._right_click_point(point)
                client._paced_wait(
                    "uia_reference_menu_settle_ms_min",
                    "uia_reference_menu_settle_ms_max",
                    250,
                    450,
                )
                menu_item = client._find_desktop_control(
                    "定位到原文位置", "mmui::XMenuView"
                )
                if menu_item is None:
                    logger.warning(
                        "[WechatDesktop] locate-original menu item unavailable; "
                        "leaving UI untouched"
                    )
                    return message
                if not client._click_control_point(menu_item):
                    return message
                located = True
                client._paced_wait(
                    "uia_reference_locate_settle_ms_min",
                    "uia_reference_locate_settle_ms_max",
                    500,
                    900,
                )
                message_list = next(
                    (
                        control
                        for control in client._walk(root)
                        if _text(control.AutomationId) == MESSAGE_LIST_ID
                    ),
                    None,
                )
                if message_list is None:
                    return message
                message_list_verified = True
                control = client._pick_located_original_control(
                    message_list, reference.message_type, reference.content
                )
                if control is None:
                    logger.warning(
                        "[WechatDesktop] located reference original is ambiguous"
                    )
                else:
                    content = _text(control.Name)
                    class_name = _text(control.ClassName)
                    original_type = client._message_type(class_name, content)
                    if original_type == "share_card" and not client._share_card_matches_preview(
                        content, reference.content
                    ):
                        logger.warning(
                            "[WechatDesktop] share-card title does not match quote preview"
                        )
                    else:
                        original = UiaChatMessage(
                            sender_name=reference.sender_name,
                            content=content,
                            message_type=original_type,
                            direction="unknown",
                            runtime_id=_runtime_id(control),
                            bounds=_bounds(control),
                        )
            if original is not None:
                # The original must remain visible for attachment extraction,
                # but nested UIA initializers are avoided by leaving the tree.
                file_path = ""
                if original.message_type == "image":
                    file_path = client.fetch_message_image(original)
                elif original.message_type == "file":
                    file_path = client.fetch_message_file(original)
                title, platform = client._share_card_metadata(original.content)
                share_url = ""
                browser_content = ""
                if original.message_type == "share_card" and original.bounds:
                    browser_content, share_url = client.fetch_share_page_from_point(
                        client._share_card_activation_point(original.bounds)
                    )
                resolved_reference = UiaReferencedMessage(
                    sender_name=reference.sender_name,
                    content=(
                        title
                        if original.message_type == "share_card"
                        else original.content
                    ),
                    message_type=original.message_type,
                    file_path=file_path,
                    resolved=(
                        original.message_type != "share_card"
                        or bool(browser_content or share_url)
                    ),
                    degraded=(
                        original.message_type in {"image", "file"}
                        and not file_path
                    ),
                    strategy=(
                        "wechat_share_browser_direct_read"
                        if browser_content
                        else "wechat_share_browser_copy_link"
                        if share_url
                        else "wechat_locate_original"
                    ),
                    original_content=original.content,
                    url=share_url,
                    platform=platform,
                    browser_content=browser_content,
                    browser_status=("success" if browser_content else "unavailable"),
                )
                resolved_message = replace(message, reference=resolved_reference)
        except Exception as exc:
            logger.warning("[WechatDesktop] failed to resolve reference: %s", exc)
        finally:
            if located:
                try:
                    restored = client._return_to_reference()
                    if not restored and message_list_verified:
                        logger.warning(
                            "[WechatDesktop] return-to-reference control unavailable; "
                            "falling back to End"
                        )
                        client._press_end_key()
                        client._paced_wait(
                            "uia_reference_return_settle_ms_min",
                            "uia_reference_return_settle_ms_max",
                            300,
                            600,
                        )
                        restored = True
                except Exception as exc:
                    logger.warning(
                        "[WechatDesktop] failed to return to reference: %s", exc
                    )
        if (
            resolved_message.reference is not None
            and resolved_message.reference.message_type == "share_card"
        ):
            if (
                resolved_message.reference.browser_content
                or resolved_message.reference.url
            ):
                return resolved_message
            if not restored:
                return replace(
                    resolved_message,
                    reference=replace(
                        resolved_message.reference,
                        degraded=True,
                        strategy="wechat_locate_original_return_failed",
                    ),
                )
            browser_content, url = client.fetch_referenced_share_page(
                resolved_message
            )
            return replace(
                resolved_message,
                reference=replace(
                    resolved_message.reference,
                    url=url,
                    browser_content=browser_content,
                    browser_status=("success" if browser_content else "unavailable"),
                    resolved=bool(browser_content or url),
                    degraded=not bool(browser_content or url),
                    strategy=(
                        "wechat_share_browser_direct_read"
                        if browser_content
                        else "wechat_share_browser_copy_link"
                    ),
                ),
            )
        return resolved_message
