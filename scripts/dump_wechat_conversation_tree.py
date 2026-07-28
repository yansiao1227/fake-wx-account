"""Dump the visible WeChat conversation-list UIA tree as JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from channel.wechat_desktop.uia_client import (
    SESSION_PREFIX,
    WechatUiaClient,
    _bounds,
    _runtime_id,
    _text,
)


def _children(control) -> list:
    try:
        return list(control.GetChildren())
    except Exception:
        return []


def _find_conversation_list_root(root, max_scan_nodes: int = 4000):
    """Return the lowest UIA node containing all visible session rows."""
    controls = [root]
    parent_indexes = [None]
    queue = [0]

    while queue and len(controls) < max(1, int(max_scan_nodes)):
        parent_index = queue.pop(0)
        for child in _children(controls[parent_index]):
            if len(controls) >= max(1, int(max_scan_nodes)):
                break
            controls.append(child)
            parent_indexes.append(parent_index)
            queue.append(len(controls) - 1)

    session_indexes = [
        index
        for index, control in enumerate(controls)
        if _text(control.AutomationId).startswith(SESSION_PREFIX)
    ]
    if not session_indexes:
        raise RuntimeError("No visible WeChat conversation rows were found")

    def ancestors(index: int) -> list[int]:
        result = []
        while index is not None:
            result.append(index)
            index = parent_indexes[index]
        return result

    common = set(ancestors(session_indexes[0]))
    for index in session_indexes[1:]:
        common.intersection_update(ancestors(index))

    # For a single visible row, include its immediate container as useful context.
    start = parent_indexes[session_indexes[0]] or session_indexes[0]
    while start not in common:
        start = parent_indexes[start]
    return controls[start], len(session_indexes)


def _safe_automation_id(value: str, include_content: bool) -> str:
    if include_content or not value.startswith(SESSION_PREFIX):
        return value
    return f"{SESSION_PREFIX}<redacted>"


def _dump_control_tree(control, include_content: bool, max_nodes: int) -> dict:
    queue = [(control, 0, None)]
    nodes = []
    while queue and len(nodes) < max(1, int(max_nodes)):
        item, depth, parent_index = queue.pop(0)
        children = _children(item)
        name = _text(item.Name)
        automation_id = _text(item.AutomationId)
        index = len(nodes)
        node = {
            "index": index,
            "parent_index": parent_index,
            "depth": depth,
            "control_type": _text(item.ControlTypeName),
            "class_name": _text(item.ClassName),
            "automation_id": _safe_automation_id(automation_id, include_content),
            "runtime_id": _runtime_id(item),
            "bounds": _bounds(item),
            "child_count": len(children),
        }
        if include_content:
            node["name"] = name
        else:
            node["has_name"] = bool(name)
            node["name_length"] = len(name)
        nodes.append(node)
        queue.extend((child, depth + 1, index) for child in children)

    return {
        "root_class": _text(control.ClassName),
        "node_count": len(nodes),
        "truncated": bool(queue),
        "content_included": include_content,
        "nodes": nodes,
    }


def dump_conversation_tree(
    client: WechatUiaClient,
    include_content: bool = False,
    max_nodes: int = 2000,
    max_scan_nodes: int = 4000,
) -> dict:
    with client.operation_lock, client._uia_root() as root:
        list_root, session_count = _find_conversation_list_root(
            root, max_scan_nodes
        )
        result = _dump_control_tree(list_root, include_content, max_nodes)
        result["visible_session_count"] = session_count
        return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--include-content",
        action="store_true",
        help="include conversation names and message previews (sensitive)",
    )
    parser.add_argument("--max-nodes", type=int, default=2000)
    parser.add_argument("--max-scan-nodes", type=int, default=4000)
    parser.add_argument(
        "--output",
        type=Path,
        help="write JSON to this path instead of stdout",
    )
    args = parser.parse_args()

    client = WechatUiaClient({})
    try:
        client.focus_window()
        tree = dump_conversation_tree(
            client,
            include_content=args.include_content,
            max_nodes=args.max_nodes,
            max_scan_nodes=args.max_scan_nodes,
        )
        payload = json.dumps(tree, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload + "\n", encoding="utf-8")
            print(args.output.resolve())
        else:
            print(payload)
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
