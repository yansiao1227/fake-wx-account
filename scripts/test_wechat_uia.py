"""Redacted smoke test for the WeChat 4.x UIA backend."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from channel.wechat_desktop.shell_hook import WindowsShellHook
from channel.wechat_desktop.uia_client import WechatUiaClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversation", help="read the latest visible bubbles")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--self-display-name", default="")
    parser.add_argument("--include-content", action="store_true")
    parser.add_argument("--send-to", default="")
    parser.add_argument("--text", default="")
    args = parser.parse_args()

    client = WechatUiaClient({"self_display_name": args.self_display_name})
    hook = WindowsShellHook(client.allowed_process_ids)
    report = {"backend": "uia", "screenshot": False, "ocr": False}
    try:
        report["window_handle"] = client.get_owner_window_handle()
        report["process_id"] = client.get_owner_window_process_id()
        report["node_count"] = client.probe_tree()
        owner = client.get_owner_info()
        report["owner"] = {
            "available": bool(owner.nick_name),
            "source": owner.source,
            "confidence": owner.confidence,
        }
        conversations = client.get_visible_conversations()
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
            }
            if args.include_content:
                report["history"]["content"] = [item.content for item in messages]
        if args.send_to or args.text:
            if not args.send_to or not args.text:
                raise ValueError("--send-to and --text must be supplied together")
            report["send"] = client.send_message(args.send_to, args.text)
    except Exception as exc:
        report["error"] = str(exc)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    finally:
        hook.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.include_content:
        print("[WARN] The report contains visible chat text; review before sharing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

