"""Diagnostic smoke test for the WeChat 4.x UIA backend."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from channel.wechat_desktop.shell_hook import WindowsShellHook
from channel.wechat_desktop.uia_client import (
    MESSAGE_LIST_ID,
    WechatUiaClient,
    _bounds,
    _runtime_id,
    _text,
)


SIMULATED_MENTION_MARKER = "[有人@我]"


def _geometry_summary(client: WechatUiaClient) -> list[dict]:
    """Return bubble geometry and UIA text for selector diagnosis."""
    with client.operation_lock, client._uia_root() as root:
        message_list = next(
            (
                control
                for control in client._walk(root)
                if _text(control.AutomationId) == MESSAGE_LIST_ID
            ),
            None,
        )
        if message_list is None:
            return []
        rows = []
        for item in message_list.GetChildren():
            class_name = _text(item.ClassName)
            if "Chat" not in class_name or "ItemView" not in class_name:
                continue
            item_text = _text(item.Name)
            try:
                clickable_point = item.GetClickablePoint()
            except Exception:
                clickable_point = None
            try:
                legacy = item.GetLegacyIAccessiblePattern()
            except Exception:
                legacy = None
            descendants = []
            for control in client._walk(item, 50):
                value = _text(control.Name)
                descendants.append(
                    {
                        "type": _text(control.ControlTypeName),
                        "class": _text(control.ClassName),
                        "bounds": _bounds(control),
                        "name": value,
                        "matches_item": bool(value and value == item_text),
                    }
                )
            row = {
                "class": class_name,
                "bounds": _bounds(item),
                "clickable_point": clickable_point,
                "legacy_role": legacy.Role if legacy else None,
                "legacy_state": legacy.State if legacy else None,
                "legacy_has_default_action": bool(legacy and legacy.DefaultAction),
                "name": item_text,
                "descendants": descendants,
                "uia_properties": {
                    "aria": _text(item.AriaProperties),
                    "help": _text(item.HelpText),
                    "item_status": _text(item.ItemStatus),
                    "provider": _text(item.ProviderDescription),
                    "legacy_description": _text(legacy.Description) if legacy else "",
                    "legacy_value": _text(legacy.Value) if legacy else "",
                },
            }
            rows.append(row)
        return rows


def _dump_current_chat_tree(
    client: WechatUiaClient,
    max_nodes: int = 500,
) -> dict:
    """Capture the current ChatDetailView hierarchy as structured JSON."""
    with client.operation_lock, client._uia_root() as root:
        chat_root = next(
            (
                control
                for control in client._walk(root)
                if _text(control.ClassName) == "mmui::ChatDetailView"
            ),
            None,
        )
        if chat_root is None:
            raise RuntimeError("WeChat chat detail UI tree is unavailable")
        return _dump_control_tree(chat_root, max_nodes)


def _dump_control_tree(control, max_nodes: int = 1000) -> dict:
    queue = [(control, 0, None)]
    nodes = []
    while queue and len(nodes) < max(1, int(max_nodes)):
        item, depth, parent_index = queue.pop(0)
        name = _text(item.Name)
        try:
            children = list(item.GetChildren())
        except Exception:
            children = []
        index = len(nodes)
        node = {
            "index": index,
            "parent_index": parent_index,
            "depth": depth,
            "control_type": _text(item.ControlTypeName),
            "class_name": _text(item.ClassName),
            "automation_id": _text(item.AutomationId),
            "runtime_id": _runtime_id(item),
            "bounds": _bounds(item),
            "child_count": len(children),
        }
        node["name"] = name
        nodes.append(node)
        queue.extend((child, depth + 1, index) for child in children)
    return {
        "root_class": _text(control.ClassName),
        "node_count": len(nodes),
        "truncated": bool(queue),
        "content_included": True,
        "nodes": nodes,
    }


def _open_and_dump_history(
    client: WechatUiaClient,
    open_member_filter: bool = False,
    select_member: str = "",
) -> dict:
    import random
    import time
    import uiautomation as auto
    import win32api
    import win32con
    import win32gui
    import win32process

    # Capture the owner PID before opening history: the history dialog is a
    # second visible WeChat top-level window and intentionally trips the
    # single-main-window guard afterwards.
    owner_hwnd = client.get_owner_window_handle()
    pid = client.get_owner_window_process_id()
    with client.operation_lock, client._uia_root() as root:
        button = next(
            (
                control
                for control in client._walk(root)
                if _text(control.Name) == "聊天记录"
                and _text(control.ControlTypeName) == "ButtonControl"
            ),
            None,
        )
        if button is None:
            raise RuntimeError("WeChat chat history button is unavailable")
        old_cursor = win32api.GetCursorPos()
        try:
            button.Click()
        finally:
            win32api.SetCursorPos(old_cursor)
        time.sleep(random.uniform(1.0, 1.6))

    windows = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
        if int(window_pid) == int(pid):
            windows.append(hwnd)
        return True

    win32gui.EnumWindows(collect, None)
    if open_member_filter:
        history_hwnd = next(
            (
                hwnd
                for hwnd in windows
                if "聊天记录" in win32gui.GetWindowText(hwnd)
            ),
            None,
        )
        if history_hwnd is None:
            raise RuntimeError("WeChat chat history window is unavailable")
        with auto.UIAutomationInitializerInThread():
            history_root = auto.ControlFromHandle(history_hwnd)
            member_button = next(
                (
                    control
                    for control in client._walk(history_root)
                    if _text(control.Name) == "群成员"
                ),
                None,
            )
            if member_button is None:
                raise RuntimeError("WeChat history member filter is unavailable")
            old_cursor = win32api.GetCursorPos()
            try:
                member_button.Click()
            finally:
                win32api.SetCursorPos(old_cursor)
        time.sleep(random.uniform(0.8, 1.3))
        windows.clear()
        win32gui.EnumWindows(collect, None)
        if select_member:
            selected = False
            with auto.UIAutomationInitializerInThread():
                for hwnd in windows:
                    popup_root = auto.ControlFromHandle(hwnd)
                    candidate = next(
                        (
                            control
                            for control in client._walk(popup_root, 500)
                            if _text(control.ClassName) == "mmui::XTableCell"
                            and _text(control.Name) == _text(select_member)
                        ),
                        None,
                    )
                    if candidate is None:
                        continue
                    old_cursor = win32api.GetCursorPos()
                    try:
                        candidate.Click()
                    finally:
                        win32api.SetCursorPos(old_cursor)
                    selected = True
                    break
            if not selected:
                raise RuntimeError("Owner is absent from the history member filter")
            time.sleep(random.uniform(0.8, 1.3))
            windows.clear()
            win32gui.EnumWindows(collect, None)
    result = {"process_id": pid, "window_count": len(windows), "windows": []}
    try:
        with auto.UIAutomationInitializerInThread():
            for hwnd in windows:
                root = auto.ControlFromHandle(hwnd)
                item = _dump_control_tree(root, 1500)
                item["window_handle"] = int(hwnd)
                item["window_class"] = win32gui.GetClassName(hwnd)
                item["window_bounds"] = list(win32gui.GetWindowRect(hwnd))
                result["windows"].append(item)
    finally:
        closed_history = False
        for hwnd in windows:
            if hwnd == owner_hwnd or not win32gui.IsWindow(hwnd):
                continue
            if "聊天记录" in win32gui.GetWindowText(hwnd):
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                closed_history = True
        if not closed_history:
            win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0)
            win32api.keybd_event(
                win32con.VK_ESCAPE, 0, win32con.KEYEVENTF_KEYUP, 0
            )
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversation", help="read the latest visible bubbles")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--self-display-name", default="")
    parser.add_argument("--send-to", default="")
    parser.add_argument("--text", default="")
    parser.add_argument(
        "--standard-targets",
        action="store_true",
        help="read the smoke diagnostics for 颜料盒 and 测试群",
    )
    parser.add_argument(
        "--send-smoke",
        action="store_true",
        help="send one timestamped smoke message to each standard target",
    )
    parser.add_argument(
        "--geometry-summary",
        action="store_true",
        help="include bubble text, classes, and bounds for selector diagnosis",
    )
    parser.add_argument(
        "--dump-tree",
        action="store_true",
        help="write the current ChatDetailView UI tree to tmp as JSON",
    )
    parser.add_argument(
        "--dump-history-tree",
        action="store_true",
        help="open chat history, dump its UIA windows to tmp, then close it",
    )
    parser.add_argument(
        "--history-member-filter",
        action="store_true",
        help="open the group-member filter before dumping chat history UIA",
    )
    parser.add_argument(
        "--select-owner-member",
        action="store_true",
        help="select the current account in the opened history member filter",
    )
    parser.add_argument(
        "--resolve-history-direction",
        action="store_true",
        help="use the group history member filter to resolve message directions",
    )
    args = parser.parse_args()

    client = WechatUiaClient(
        {
            "self_display_name": args.self_display_name,
            # Smoke tests favor stable, human-paced transitions over speed.
            "uia_focus_settle_ms_min": 600,
            "uia_focus_settle_ms_max": 1000,
            "uia_selection_settle_ms_min": 700,
            "uia_selection_settle_ms_max": 1100,
        }
    )
    hook = WindowsShellHook(client.allowed_process_ids)
    report = {"backend": "uia", "screenshot": False, "ocr": False}
    try:
        client.focus_window()
        report["window_handle"] = client.get_owner_window_handle()
        report["process_id"] = client.get_owner_window_process_id()
        report["node_count"] = client.probe_tree()
        owner = client.get_owner_info()
        report["owner"] = {
            "available": bool(owner.nick_name),
            "source": owner.source,
        }
        conversations = client.get_visible_conversations()
        conversations_by_title = {}
        for conversation in conversations:
            conversations_by_title.setdefault(
                conversation.conversation_title, []
            ).append(conversation)
        report["visible_conversations"] = len(conversations)
        report["unread_conversations"] = sum(
            item.not_read_number > 0 for item in conversations
        )
        report["mentioned_conversations"] = sum(
            item.mentions_self is True for item in conversations
        )
        report["shell_hook_active"] = hook.start()
        report["shell_hook_error"] = hook.error
        if args.conversation:
            messages = client.get_chat_history(args.conversation, args.limit)
            report["history"] = {
                "count": len(messages),
                "limit": min(max(args.limit, 1), 5),
                "directions": [item.direction for item in messages],
                "types": [item.message_type for item in messages],
                "content": [item.content for item in messages],
            }
        if args.standard_targets or args.send_smoke:
            report["standard_targets"] = {}
            for target, expected_type in (("颜料盒", "private"), ("测试群", "group")):
                located = client.locate_conversation(target)
                if not located:
                    raise RuntimeError(f"Conversation is not visible: {target}")
                # Deliberately pass the same target again.  get_chat_history()
                # must reuse the populated active pane instead of toggling it
                # blank with a second session-row click.
                messages = client.get_chat_history(target, args.limit)
                header = client.get_title()
                if (
                    args.resolve_history_direction
                    and expected_type == "group"
                    and any(message.direction == "unknown" for message in messages)
                ):
                    messages = client.resolve_group_message_directions(
                        target, messages, owner.nick_name
                    )
                item = {
                    "located": located,
                    "expected_type": expected_type,
                    "header_type": header.header_type,
                    "history_count": len(messages),
                    "directions": [message.direction for message in messages],
                    "types": [message.message_type for message in messages],
                    "content": [message.content for message in messages],
                    "direction_available": bool(messages)
                    and all(
                        message.direction in {"incoming", "outgoing"}
                        for message in messages
                    ),
                }
                matching_rows = conversations_by_title.get(target, [])
                row_marker = any(row.mentions_self is True for row in matching_rows)
                bubble_marker = any(
                    SIMULATED_MENTION_MARKER.casefold() in message.content.casefold()
                    for message in messages
                )
                if expected_type == "group":
                    if args.resolve_history_direction:
                        item["direction_resolution"] = dict(
                            client.last_direction_resolution
                        )
                    item["mention_marker"] = {
                        "matched": row_marker or bubble_marker,
                        "conversation_row": row_marker,
                        "recent_bubble": bubble_marker,
                        "rule": "contains",
                    }
                if args.geometry_summary:
                    item["geometry"] = _geometry_summary(client)
                report["standard_targets"][target] = item
            report["standard_summary"] = {
                "targets_located": all(
                    item["located"]
                    and item["header_type"] == item["expected_type"]
                    for item in report["standard_targets"].values()
                ),
                "group_marker_matched": report["standard_targets"]["测试群"]
                ["mention_marker"]["matched"],
                "direction_available": all(
                    item["direction_available"]
                    for item in report["standard_targets"].values()
                ),
            }
            if args.send_smoke:
                stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
                report["standard_send"] = {}
                for target in ("颜料盒", "测试群"):
                    report["standard_send"][target] = client.send_message(
                        target,
                        f"[CowAgent UIA smoke] {stamp}",
                    )
        if args.send_to or args.text:
            if not args.send_to or not args.text:
                raise ValueError("--send-to and --text must be supplied together")
            report["send"] = client.send_message(args.send_to, args.text)
        if args.dump_tree:
            tree = _dump_current_chat_tree(client)
            output_dir = ROOT / "tmp"
            output_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
            output_path = output_dir / f"wechat-chat-uia-tree-{stamp}.json"
            output_path.write_text(
                json.dumps(tree, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            report["tree"] = {
                "path": str(output_path),
                "node_count": tree["node_count"],
                "truncated": tree["truncated"],
                "content_included": tree["content_included"],
            }
        if args.dump_history_tree:
            history_tree = _open_and_dump_history(
                client,
                args.history_member_filter or args.select_owner_member,
                owner.nick_name if args.select_owner_member else "",
            )
            output_dir = ROOT / "tmp"
            output_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
            output_path = output_dir / f"wechat-history-uia-tree-{stamp}.json"
            output_path.write_text(
                json.dumps(history_tree, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            report["history_tree"] = {
                "path": str(output_path),
                "window_count": history_tree["window_count"],
                "node_count": sum(
                    item["node_count"] for item in history_tree["windows"]
                ),
                "content_included": True,
            }
    except Exception as exc:
        report["error"] = str(exc)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    finally:
        hook.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("[WARN] The report contains visible chat text; review before sharing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
