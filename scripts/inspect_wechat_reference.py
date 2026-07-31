"""Inspect and resolve one-level WeChat references through original messages.

The default mode is read-only. ``--resolve-all`` uses WeChat's native locate
and return actions. Preview text and cache matches are not sources of truth.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from channel.wechat_desktop.uia_client import (
    MESSAGE_LIST_ID,
    WechatUiaClient,
    _bounds,
    _runtime_id,
    _text,
)


REFERENCE_CLASS = "chatbubblereferitemview"


def _reference_kind(name: str) -> str:
    value = _text(name).casefold()
    exact = {
        "图片": "image",
        "image": "image",
        "文件": "file",
        "file": "file",
        "视频": "video",
        "video": "video",
        "语音": "voice",
        "voice": "voice",
        "链接": "link",
        "link": "link",
        "位置": "location",
        "location": "location",
    }
    if value in exact:
        return exact[value]
    if re.search(
        r"\.(?:docx?|pdf|xlsx?|pptx?|txt|md|csv|zip|rar|7z)$",
        value,
        re.IGNORECASE,
    ):
        return "file"
    if re.search(r"https?://|www\.", value, re.IGNORECASE):
        return "link"
    if value:
        return "text"
    return "unknown"


def _parse_reference_text(value: str) -> Optional[dict]:
    """Parse WeChat's flattened '<message>引用 <sender> 的消息: <quote>' label."""
    match = re.match(
        r"^(?P<current>.*?)引用\s+(?P<sender>.+?)\s+的消息\s*[:：]\s*(?P<quoted>.*)$",
        _text(value),
        re.DOTALL,
    )
    if not match:
        return None
    return {
        "current_message": match.group("current").strip(),
        "quoted_sender": match.group("sender").strip(),
        "quoted_preview": match.group("quoted").strip(),
    }


def _children(control) -> list:
    try:
        return list(control.GetChildren())
    except Exception:
        return []


def _point(value) -> Optional[tuple[int, int]]:
    if value is None:
        return None
    try:
        return int(value.x), int(value.y)
    except Exception:
        pass
    try:
        return int(value[0]), int(value[1])
    except Exception:
        return None


def _safe_bool(control, attribute: str) -> Optional[bool]:
    try:
        return bool(getattr(control, attribute))
    except Exception:
        return None


def _patterns(control) -> dict:
    invoke = None
    legacy = None
    selection = None
    clickable = None
    try:
        invoke = control.GetInvokePattern()
    except Exception:
        pass
    try:
        legacy = control.GetLegacyIAccessiblePattern()
    except Exception:
        pass
    try:
        selection = control.GetSelectionItemPattern()
    except Exception:
        pass
    try:
        clickable = _point(control.GetClickablePoint())
    except Exception:
        pass
    return {
        "invoke": invoke is not None,
        "selection_item": selection is not None,
        "clickable_point": clickable,
        "legacy_role": getattr(legacy, "Role", None) if legacy else None,
        "legacy_state": getattr(legacy, "State", None) if legacy else None,
        "legacy_default_action": _text(getattr(legacy, "DefaultAction", ""))
        if legacy
        else "",
    }


def _node(control, index: int, parent_index: Optional[int], depth: int) -> dict:
    bounds = _bounds(control)
    result = {
        "index": index,
        "parent_index": parent_index,
        "depth": depth,
        "control_type": _text(control.ControlTypeName),
        "class_name": _text(control.ClassName),
        "automation_id": _text(control.AutomationId),
        "runtime_id": _runtime_id(control),
        "name": _text(control.Name),
        "bounds": bounds,
        "width": bounds[2] - bounds[0] if bounds else 0,
        "height": bounds[3] - bounds[1] if bounds else 0,
        "enabled": _safe_bool(control, "IsEnabled"),
        "offscreen": _safe_bool(control, "IsOffscreen"),
        "has_keyboard_focus": _safe_bool(control, "HasKeyboardFocus"),
    }
    result.update(_patterns(control))
    return result


def _subtree(control, max_nodes: int = 150) -> tuple[list[dict], list]:
    queue = [(control, 0, None)]
    nodes: list[dict] = []
    controls = []
    while queue and len(nodes) < max(1, int(max_nodes)):
        item, depth, parent_index = queue.pop(0)
        index = len(nodes)
        nodes.append(_node(item, index, parent_index, depth))
        controls.append(item)
        queue.extend((child, depth + 1, index) for child in _children(item))
    return nodes, controls


def _click_score(node: dict, reference_bounds) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []
    if node["invoke"]:
        score += 8
        reasons.append("InvokePattern")
    if node["clickable_point"]:
        score += 6
        reasons.append("clickable point")
    if node["legacy_default_action"]:
        score += 4
        reasons.append("legacy default action")
    bounds = node["bounds"]
    if bounds and reference_bounds:
        width = max(1, reference_bounds[2] - reference_bounds[0])
        center_x = (bounds[0] + bounds[2]) / 2
        relative_x = (center_x - reference_bounds[0]) / width
        if relative_x <= 0.45:
            score += 3
            reasons.append("left-side region")
        reference_area = max(
            1,
            (reference_bounds[2] - reference_bounds[0])
            * (reference_bounds[3] - reference_bounds[1]),
        )
        area = max(0, node["width"] * node["height"])
        if 0 < area < reference_area * 0.6:
            score += 2
            reasons.append("smaller child region")
    if node["depth"] > 0:
        score += 1
        reasons.append("descendant")
    return score, reasons


def _reference_candidates(message_list, max_nodes: int) -> list[dict]:
    candidates = []
    seen = set()
    controls_to_scan = _children(message_list)
    if not controls_to_scan:
        controls_to_scan = list(WechatUiaClient._walk(message_list, max_nodes))
    for control in controls_to_scan[:max_nodes]:
        class_match = REFERENCE_CLASS in _text(control.ClassName).casefold()
        parsed = _parse_reference_text(_text(control.Name))
        if not class_match and parsed is None:
            continue
        identity = (_runtime_id(control), _bounds(control))
        if identity in seen:
            continue
        seen.add(identity)
        nodes, controls = _subtree(control)
        reference_bounds = _bounds(control)
        scored = []
        for node in nodes:
            score, reasons = _click_score(node, reference_bounds)
            scored.append((score, -node["index"], node["index"], reasons))
        best = max(scored) if scored else (0, 0, 0, [])
        recommended_index = best[2]
        for score, _, node_index, reasons in scored:
            nodes[node_index]["click_score"] = score
            nodes[node_index]["click_reasons"] = reasons
            nodes[node_index]["recommended"] = node_index == recommended_index
        name = _text(control.Name)
        quoted_preview = parsed["quoted_preview"] if parsed else name
        candidate = {
                "candidate_index": len(candidates),
                "detection": (
                    "reference_class" if class_match else "flattened_accessible_text"
                ),
                "name": name,
                "current_message": parsed["current_message"] if parsed else "",
                "quoted_sender": parsed["quoted_sender"] if parsed else "",
                "quoted_preview": quoted_preview,
                "reference_kind": _reference_kind(quoted_preview),
                "class_name": _text(control.ClassName),
                "runtime_id": _runtime_id(control),
                "bounds": reference_bounds,
                "recommended_node_index": recommended_index,
                "nodes": nodes,
                "_controls": controls,
            }
        candidates.append(candidate)
    return candidates


def _message_snapshot(message_list) -> list[dict]:
    rows = []
    for index, item in enumerate(_children(message_list)):
        class_name = _text(item.ClassName)
        if "Chat" not in class_name or "ItemView" not in class_name:
            continue
        rows.append(
            {
                "visible_index": index,
                "class_name": class_name,
                "runtime_id": _runtime_id(item),
                "bounds": _bounds(item),
                "name": _text(item.Name),
                "has_keyboard_focus": _safe_bool(item, "HasKeyboardFocus"),
            }
        )
    return rows


def _find_message_list(root):
    return next(
        (
            control
            for control in WechatUiaClient._walk(root)
            if _text(control.AutomationId) == MESSAGE_LIST_ID
        ),
        None,
    )


def _capture_reference(candidate: dict, message_list_bounds, target: Path) -> str:
    from PIL import ImageGrab

    bounds = candidate.get("bounds")
    if not bounds or not message_list_bounds:
        return ""
    left = max(bounds[0], message_list_bounds[0])
    top = max(bounds[1], message_list_bounds[1])
    right = min(bounds[2], message_list_bounds[2])
    bottom = min(bounds[3], message_list_bounds[3])
    if right <= left or bottom <= top:
        return ""
    target.parent.mkdir(parents=True, exist_ok=True)
    ImageGrab.grab(
        bbox=(left, top, right, bottom), all_screens=True
    ).save(target, format="PNG")
    return str(target.resolve())


def _mouse_click(
    point: tuple[int, int], double: bool, right: bool = False
) -> None:
    import win32api
    import win32con

    cursor = win32api.GetCursorPos()
    try:
        win32api.SetCursorPos(point)
        down = win32con.MOUSEEVENTF_RIGHTDOWN if right else win32con.MOUSEEVENTF_LEFTDOWN
        up = win32con.MOUSEEVENTF_RIGHTUP if right else win32con.MOUSEEVENTF_LEFTUP
        count = 2 if double else 1
        for index in range(count):
            win32api.mouse_event(down, 0, 0, 0, 0)
            win32api.mouse_event(up, 0, 0, 0, 0)
            if index + 1 < count:
                time.sleep(0.08)
    finally:
        win32api.SetCursorPos(cursor)


def _scan_cursor_handles(bounds, step: int = 20) -> dict:
    """Map cursor-shape changes without clicking the reference row."""
    import win32api
    import win32con
    import win32gui

    if not bounds:
        return {}
    original = win32api.GetCursorPos()
    handles: dict[int, list[list[int]]] = {}
    step = max(8, min(int(step), 80))
    try:
        for y in range(bounds[1] + 5, bounds[3] - 4, step):
            for x in range(bounds[0] + 5, bounds[2] - 4, step):
                win32api.SetCursorPos((x, y))
                time.sleep(0.012)
                try:
                    handle = int(win32gui.GetCursorInfo()[1] or 0)
                except Exception:
                    handle = 0
                handles.setdefault(handle, []).append([x, y])
    finally:
        win32api.SetCursorPos(original)
    try:
        hand = int(win32gui.LoadCursor(0, win32con.IDC_HAND) or 0)
    except Exception:
        hand = 0
    return {
        "hand_cursor": hand,
        "handles": [
            {
                "handle": handle,
                "is_hand": bool(hand and handle == hand),
                "point_count": len(points),
                "points": points,
            }
            for handle, points in handles.items()
        ],
    }


def _click(
    control,
    method: str,
    screen_point: Optional[tuple[int, int]] = None,
) -> str:
    if method in {"point-click", "point-double", "point-right"}:
        if screen_point is None:
            raise RuntimeError("--point X,Y is required for a point click")
        _mouse_click(
            screen_point,
            double=method == "point-double",
            right=method == "point-right",
        )
        return method
    if method in {"auto", "invoke"}:
        try:
            control.GetInvokePattern().Invoke()
            return "invoke"
        except Exception:
            if method == "invoke":
                raise
    WechatUiaClient._click_and_restore(control)
    return "click"


def _parse_screen_point(value: Optional[str]) -> Optional[tuple[int, int]]:
    if not value:
        return None
    try:
        separator = "," if "," in value else ":"
        x, y = value.split(separator, 1)
        return int(x.strip()), int(y.strip())
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "point must use X,Y or X:Y screen coordinates"
        ) from exc


def _capture_bounds(bounds, target: Path) -> str:
    from PIL import ImageGrab

    if not bounds:
        return ""
    target.parent.mkdir(parents=True, exist_ok=True)
    ImageGrab.grab(bbox=tuple(bounds), all_screens=True).save(target, format="PNG")
    return str(target.resolve())


def _foreground_window() -> dict:
    import win32gui
    import win32process

    hwnd = int(win32gui.GetForegroundWindow() or 0)
    if not hwnd:
        return {"hwnd": 0}
    _, process_id = win32process.GetWindowThreadProcessId(hwnd)
    try:
        bounds = tuple(int(value) for value in win32gui.GetWindowRect(hwnd))
    except Exception:
        bounds = None
    return {
        "hwnd": hwnd,
        "process_id": int(process_id),
        "title": win32gui.GetWindowText(hwnd),
        "class_name": win32gui.GetClassName(hwnd),
        "bounds": bounds,
    }


def _press_escape() -> None:
    import win32api
    import win32con

    win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0)
    win32api.keybd_event(
        win32con.VK_ESCAPE, 0, win32con.KEYEVENTF_KEYUP, 0
    )


def _press_end() -> None:
    import win32api
    import win32con

    win32api.keybd_event(win32con.VK_END, 0, 0, 0)
    win32api.keybd_event(
        win32con.VK_END, 0, win32con.KEYEVENTF_KEYUP, 0
    )


def _snapshot_changed(before: list[dict], after: list[dict]) -> bool:
    fields = lambda items: [
        (item["runtime_id"], item["bounds"], item["name"]) for item in items
    ]
    return fields(before) != fields(after)


def _find_desktop_control(name: str, class_name: str):
    import uiautomation as automation

    stack = [automation.GetRootControl()]
    while stack:
        control = stack.pop()
        if (
            _text(control.Name) == name
            and _text(control.ClassName) == class_name
        ):
            return control
        stack.extend(_children(control))
    return None


def _pick_original_message(
    messages: list[dict], message_list_bounds, kind: str, preview: str
) -> Optional[dict]:
    """Pick the row WeChat scrolls nearest the viewport centre."""
    if not messages or not message_list_bounds:
        return None
    centre_y = (message_list_bounds[1] + message_list_bounds[3]) / 2
    preview_folded = _text(preview).casefold()
    expected_classes = {
        "text": ("ChatTextItemView",),
        "file": ("ChatBubbleItemView",),
        "image": ("ChatBubbleReferItemView", "ChatImageItemView"),
    }
    best = None
    best_score = float("-inf")
    for message in messages:
        bounds = message.get("bounds")
        if not bounds:
            continue
        name = _text(message.get("name"))
        class_name = _text(message.get("class_name"))
        row_centre = (bounds[1] + bounds[3]) / 2
        score = -abs(row_centre - centre_y)
        if any(value in class_name for value in expected_classes.get(kind, ())):
            score += 240
        if preview_folded and preview_folded in name.casefold():
            score += 320
        if kind == "file" and name.startswith("文件\n"):
            score += 200
        if score > best_score:
            best_score = score
            best = message
    return best


def _return_to_reference(settle_seconds: float) -> bool:
    button = _find_desktop_control("回到引用位置", "mmui::UnreadBarView")
    if button is None:
        return False
    button.Click()
    time.sleep(max(0.2, min(float(settle_seconds), 5.0)))
    return True


def _capture_original_image(
    original: dict, artifacts_dir: Path, candidate_index: int, settle_seconds: float
) -> dict:
    bounds = original.get("bounds")
    if not bounds:
        return {"opened": False, "reason": "original image bounds unavailable"}
    before = _foreground_window()
    width = bounds[2] - bounds[0]
    point = (int(bounds[0] + width * 0.2), int((bounds[1] + bounds[3]) / 2))
    _mouse_click(point, double=False)
    time.sleep(max(0.2, min(float(settle_seconds), 5.0)))
    after = _foreground_window()
    opened = after.get("hwnd") != before.get("hwnd")
    local_path = ""
    if opened:
        artifact = artifacts_dir / f"reference-{candidate_index}-image-viewer.png"
        local_path = _capture_bounds(after.get("bounds"), artifact)
        _press_escape()
        time.sleep(0.4)
    return {
        "opened": opened,
        "point": point,
        "window": after,
        "local_path": local_path,
    }


def _resolve_by_native_locator(
    candidate: dict,
    root,
    artifacts_dir: Path,
    settle_seconds: float,
) -> dict:
    bounds = candidate.get("bounds")
    if not bounds:
        return {
            "strategy": "wechat_locate_original",
            "resolved": False,
            "reason": "reference bounds unavailable",
        }
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    quote_point = (
        int(bounds[0] + width * 0.25),
        int(bounds[1] + height * 0.72),
    )
    _mouse_click(quote_point, double=False, right=True)
    time.sleep(0.35)
    menu_item = _find_desktop_control("定位到原文位置", "mmui::XMenuView")
    if menu_item is None:
        _press_escape()
        return {
            "strategy": "wechat_locate_original",
            "resolved": False,
            "reason": "native locate-original menu item unavailable",
            "quote_point": quote_point,
        }
    # InvokePattern is advertised by WeChat but does not navigate; Click does.
    menu_item.Click()
    time.sleep(max(0.2, min(float(settle_seconds), 5.0)))
    message_list = _find_message_list(root)
    messages = _message_snapshot(message_list) if message_list is not None else []
    message_bounds = _bounds(message_list) if message_list is not None else None
    original = _pick_original_message(
        messages,
        message_bounds,
        candidate["reference_kind"],
        candidate["quoted_preview"],
    )
    capture_path = ""
    if original and original.get("bounds"):
        artifact = artifacts_dir / (
            f"reference-{candidate['candidate_index']}-"
            f"{candidate['reference_kind']}-original.png"
        )
        capture_path = _capture_bounds(original["bounds"], artifact)
    image_capture = None
    if original and candidate["reference_kind"] == "image":
        image_capture = _capture_original_image(
            original,
            artifacts_dir,
            candidate["candidate_index"],
            settle_seconds,
        )
    restored = _return_to_reference(settle_seconds)
    result = {
        "strategy": "wechat_locate_original",
        "resolved": original is not None,
        "quote_point": quote_point,
        "original_message": original,
        "original_capture": capture_path,
        "restored": restored,
        "messages_after_locate": messages,
    }
    if image_capture:
        result["image_capture"] = image_capture
        if image_capture.get("local_path"):
            result["local_path"] = image_capture["local_path"]
    return result


def _resolve_by_activation(
    candidate: dict,
    message_list,
    root,
    artifacts_dir: Path,
    settle_seconds: float,
) -> dict:
    bounds = candidate.get("bounds")
    if not bounds:
        return {
            "strategy": "activate_reference",
            "resolved": False,
            "reason": "reference bounds unavailable",
        }
    before_messages = _message_snapshot(message_list)
    before_window = _foreground_window()
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    attempts = []
    for side, x_ratio in (("left", 0.2), ("right", 0.8)):
        point = (
            int(bounds[0] + width * x_ratio),
            int(bounds[1] + height * 0.72),
        )
        _mouse_click(point, double=False)
        time.sleep(max(0.2, min(float(settle_seconds), 5.0)))
        after_window = _foreground_window()
        message_list_after = _find_message_list(root)
        after_messages = (
            _message_snapshot(message_list_after)
            if message_list_after is not None
            else []
        )
        foreground_changed = (
            after_window.get("hwnd") != before_window.get("hwnd")
            or after_window.get("class_name") != before_window.get("class_name")
        )
        viewport_changed = _snapshot_changed(before_messages, after_messages)
        attempt = {
            "side": side,
            "point": point,
            "foreground_changed": foreground_changed,
            "viewport_changed": viewport_changed,
            "foreground_after": after_window,
        }
        attempts.append(attempt)
        if foreground_changed:
            artifact = artifacts_dir / (
                f"reference-{candidate['candidate_index']}-"
                f"{candidate['reference_kind']}.png"
            )
            local_path = _capture_bounds(after_window.get("bounds"), artifact)
            same_process = (
                after_window.get("process_id") == before_window.get("process_id")
            )
            title = str(after_window.get("title") or "")
            if same_process:
                _press_escape()
                time.sleep(0.5)
            return {
                "strategy": "activate_reference",
                "resolved": bool(local_path),
                "outcome": "opened_foreground_window",
                "window_title": title,
                "local_path": local_path,
                "attempts": attempts,
                "restored": same_process,
            }
        if viewport_changed:
            _press_end()
            time.sleep(0.5)
            return {
                "strategy": "activate_reference",
                "resolved": True,
                "outcome": "navigated_chat_history",
                "messages_after": after_messages,
                "attempts": attempts,
                "restored": True,
            }
    return {
        "strategy": "activate_reference",
        "resolved": False,
        "outcome": "no_observable_change",
        "attempts": attempts,
    }


def _normalized_reference(candidate: dict) -> dict:
    resolution = candidate.get("resolution", {})
    original = resolution.get("original_message") or {}
    kind = candidate["reference_kind"]
    referenced = {
        "sender": candidate["quoted_sender"],
        "type": kind,
        "resolved": bool(resolution.get("resolved")),
        "depth": 1,
        "strategy": resolution.get("strategy", ""),
    }
    referenced["preview"] = candidate["quoted_preview"]
    if kind == "text" and original.get("name"):
        referenced["content"] = original["name"]
    if original:
        referenced["original_message"] = original
    if resolution.get("local_path"):
        referenced["local_path"] = resolution["local_path"]
    if resolution.get("outcome"):
        referenced["outcome"] = resolution["outcome"]
    if resolution.get("original_capture"):
        referenced["original_capture"] = resolution["original_capture"]
    return {
        "current_message": candidate["current_message"],
        "referenced_message": referenced,
    }


def inspect(client: WechatUiaClient, args) -> dict:
    client.focus_window()
    with client.operation_lock, client._uia_root() as root:
        message_list = _find_message_list(root)
        if message_list is None:
            raise RuntimeError("WeChat message list is unavailable")
        before = _message_snapshot(message_list)
        foreground_before = _foreground_window()
        candidates = _reference_candidates(message_list, args.max_nodes)
        for candidate in candidates:
            candidate["max_reference_depth"] = 1
            candidate["resolution"] = {
                "strategy": "wechat_locate_original",
                "resolved": False,
                "fallback_preview": candidate["quoted_preview"],
            }
        report = {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "read_only": not (args.click or args.resolve_all),
            "message_list_bounds": _bounds(message_list),
            "message_count_before": len(before),
            "messages_before": before,
            "foreground_before": foreground_before,
            "reference_count": len(candidates),
            "references": candidates,
        }
        if args.scan_cursors:
            for candidate in candidates:
                candidate["cursor_scan"] = _scan_cursor_handles(
                    candidate.get("bounds"), args.cursor_step
                )
        if args.resolve_all:
            artifacts_dir = args.artifacts_dir or ROOT / "tmp" / "wechat_references"
            for candidate in candidates:
                candidate["resolution"] = _resolve_by_native_locator(
                        candidate,
                        root,
                        artifacts_dir,
                        args.settle_seconds,
                    )
            report["model_inputs"] = [
                _normalized_reference(candidate) for candidate in candidates
            ]
        if args.capture and candidates:
            report["reference_capture"] = _capture_reference(
                candidates[args.candidate], _bounds(message_list), args.capture
            )
        if args.click:
            if not candidates:
                raise RuntimeError("No visible ChatBubbleReferItemView was found")
            if not 0 <= args.candidate < len(candidates):
                raise RuntimeError(
                    f"candidate index {args.candidate} is outside 0..{len(candidates) - 1}"
                )
            candidate = candidates[args.candidate]
            node_index = (
                args.node
                if args.node is not None
                else candidate["recommended_node_index"]
            )
            controls = candidate["_controls"]
            if not 0 <= node_index < len(controls):
                raise RuntimeError(
                    f"node index {node_index} is outside 0..{len(controls) - 1}"
                )
            report["click_test"] = {
                "candidate_index": args.candidate,
                "node_index": node_index,
                "target": candidate["nodes"][node_index],
                "screen_point": args.point,
                "method": _click(
                    controls[node_index], args.method, args.point
                ),
            }
            time.sleep(max(0.1, min(float(args.settle_seconds), 5.0)))
            foreground_after = _foreground_window()
            report["foreground_after"] = foreground_after
            report["foreground_changed"] = (
                foreground_after.get("hwnd") != foreground_before.get("hwnd")
                or foreground_after.get("class_name")
                != foreground_before.get("class_name")
            )
            if args.capture_after:
                report["after_capture"] = _capture_bounds(
                    _bounds(root), args.capture_after
                )
            message_list_after = _find_message_list(root)
            after = (
                _message_snapshot(message_list_after)
                if message_list_after is not None
                else []
            )
            report["message_count_after"] = len(after)
            report["messages_after"] = after
            report["viewport_changed"] = [
                (item["runtime_id"], item["bounds"], item["name"])
                for item in before
            ] != [
                (item["runtime_id"], item["bounds"], item["name"])
                for item in after
            ]
            if report["foreground_changed"]:
                report["activation_outcome"] = "opened_foreground_window"
            elif report["viewport_changed"]:
                report["activation_outcome"] = "navigated_chat_history"
            else:
                report["activation_outcome"] = "no_observable_change"
            if args.close_after and report["foreground_changed"]:
                _press_escape()
                time.sleep(0.5)
                report["foreground_restored"] = _foreground_window()
        for candidate in candidates:
            candidate.pop("_controls", None)
        return report


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--click", action="store_true", help="test one reference hit target")
    parser.add_argument("--candidate", type=int, default=0, help="reference candidate index")
    parser.add_argument("--node", type=int, help="subtree node index; default uses recommendation")
    parser.add_argument(
        "--method",
        choices=(
            "auto",
            "invoke",
            "click",
            "point-click",
            "point-double",
            "point-right",
        ),
        default="auto",
    )
    parser.add_argument(
        "--point",
        type=_parse_screen_point,
        help="absolute screen coordinate X,Y for a point click",
    )
    parser.add_argument("--settle-seconds", type=float, default=1.0)
    parser.add_argument("--max-nodes", type=int, default=4000)
    parser.add_argument(
        "--scan-cursors",
        action="store_true",
        help="scan reference rows for hand-cursor hit regions without clicking",
    )
    parser.add_argument("--cursor-step", type=int, default=20)
    parser.add_argument(
        "--resolve-all",
        action="store_true",
        help="resolve every visible reference with at most one level of lookup",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        help="directory for artifacts produced by --resolve-all",
    )
    parser.add_argument(
        "--capture",
        type=Path,
        help="save the visible part of the selected reference card as PNG",
    )
    parser.add_argument(
        "--capture-after",
        type=Path,
        help="capture the WeChat window after a click test",
    )
    parser.add_argument(
        "--close-after",
        action="store_true",
        help="press Esc after a click opens a different foreground window",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or (
        ROOT
        / "tmp"
        / f"wechat-reference-{datetime.now():%Y%m%d-%H%M%S}.json"
    )
    try:
        report = inspect(WechatUiaClient({}), args)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(output.resolve())
        print(
            json.dumps(
                {
                    "reference_count": report["reference_count"],
                    "read_only": report["read_only"],
                    "viewport_changed": report.get("viewport_changed"),
                    "activation_outcome": report.get("activation_outcome"),
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
