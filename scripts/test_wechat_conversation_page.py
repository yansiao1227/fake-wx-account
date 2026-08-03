"""只读测试微信当前会话页的 UIA 消息与 RapidOCR 群成员解析效果。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from channel.wechat_desktop.uia_client import WechatUiaClient


def _rapidocr_available() -> bool:
    return importlib.util.find_spec("rapidocr") is not None


def _message_payload(message) -> dict:
    """把解析结果转换为适合人工检查的 JSON 字段。"""

    payload = asdict(message)
    payload["bounds"] = list(message.bounds) if message.bounds else None
    return payload


def _build_summary(messages: list, conversation_type: str) -> dict:
    """汇总群消息 OCR 姓名覆盖情况及消息解析分布。"""

    text_messages = [
        message
        for message in messages
        if message.message_type == "text"
    ]
    incoming_text = [
        message for message in text_messages if message.direction == "incoming"
    ]
    ocr_candidates = incoming_text if conversation_type == "group" else []
    named = [message for message in ocr_candidates if message.sender_name]
    resolved_directions = [
        message
        for message in text_messages
        if message.direction in {"incoming", "outgoing"}
    ]
    return {
        "message_count": len(messages),
        "message_types": dict(Counter(message.message_type for message in messages)),
        "directions": dict(Counter(message.direction for message in messages)),
        "reference_count": sum(message.reference is not None for message in messages),
        "ocr_direction_applicable_count": len(text_messages),
        "ocr_direction_resolved_count": len(resolved_directions),
        "ocr_direction_unresolved_count": len(text_messages)
        - len(resolved_directions),
        "ocr_direction_resolution_rate": (
            round(len(resolved_directions) / len(text_messages), 4)
            if text_messages
            else None
        ),
        "incoming_text_count": len(incoming_text),
        "ocr_sender_applicable_count": len(ocr_candidates),
        "ocr_sender_resolved_count": len(named),
        "ocr_sender_unresolved_count": len(ocr_candidates) - len(named),
        "ocr_sender_resolution_rate": (
            round(len(named) / len(ocr_candidates), 4) if ocr_candidates else None
        ),
    }


def parse_conversation_page(
    client: WechatUiaClient,
    conversation: str = "",
    limit: int = 0,
) -> dict:
    """解析当前页或指定会话，并返回不执行发送操作的诊断报告。"""

    client.focus_window()
    requested_conversation = str(conversation or "").strip()
    messages = client.get_chat_history(
        requested_conversation or None,
        limit=max(0, int(limit)),
        ensure_conversation=bool(requested_conversation),
    )
    header = client.get_title()
    return {
        "captured_at": datetime.now().astimezone().isoformat(),
        "runtime": {
            "python_executable": sys.executable,
            "rapidocr_available": _rapidocr_available(),
        },
        "conversation": {
            "title": header.title,
            "type": header.header_type,
            "member_count": header.chat_number,
        },
        "ocr": {
            "configured": bool(
                client.config.get("uia_group_sender_ocr_enabled", True)
            ),
            "available": _rapidocr_available(),
            "provider": "RapidAI/RapidOCR",
            "applies_to_group_incoming_text": header.header_type == "group",
        },
        "summary": _build_summary(messages, header.header_type),
        "messages": [
            {"index": index, **_message_payload(message)}
            for index, message in enumerate(messages)
        ],
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="解析微信当前会话页并输出 UIA/RapidOCR 诊断 JSON。"
    )
    parser.add_argument(
        "--conversation",
        default="",
        help="可选；先打开指定的可见会话。省略时解析当前聊天页。",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="最多输出最近多少条消息；0 表示当前 UI 可见的全部消息。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="可选的 JSON 保存路径；无论是否设置都会输出到终端。",
    )
    parser.add_argument("--self-display-name", default="")
    args = parser.parse_args()

    if not _rapidocr_available():
        report = {
            "captured_at": datetime.now().astimezone().isoformat(),
            "runtime": {
                "python_executable": sys.executable,
                "rapidocr_available": False,
            },
            "error": (
                "当前 Python 环境缺少 rapidocr；请先激活 cowagent-wechat，"
                "再执行 python -m pip install -r requirements-windows.txt"
            ),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    client = WechatUiaClient(
        {
            "self_display_name": args.self_display_name,
            "uia_group_sender_ocr_enabled": True,
        }
    )
    try:
        report = parse_conversation_page(client, args.conversation, args.limit)
        exit_code = 0
    except Exception as exc:
        report = {
            "captured_at": datetime.now().astimezone().isoformat(),
            "error": str(exc),
        }
        exit_code = 1

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        output_path = args.output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
        print(f"[INFO] 报告已保存到: {output_path}", file=sys.stderr)
    print("[WARN] 报告包含当前可见聊天内容，分享前请检查。", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
