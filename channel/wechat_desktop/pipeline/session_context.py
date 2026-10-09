"""微信自动回复的旧 Agent 上下文裁剪，不修改会话审计记录。"""

from __future__ import annotations

from copy import deepcopy
import os
from typing import Any, Mapping


USER_SOURCE = "wechat_desktop_user_v1"
IMAGE_SOURCE = "wechat_desktop_image_artifact_v1"
_IMAGE_PREFIX = "[本地图片产物] "
_INPUT_PREFIX = "[本地输入附件] "
_TRUNCATED = "…[已截断]"
_LEGACY_HEADINGS = (
    "[候选会话上下文，需按关联度筛选]\n",
    "[被引用的内容]\n",
    "[需要回复的新消息]\n",
    "[需要回复的引用消息]\n",
)
_LEGACY_REPLY_BOUNDARY = "\n[回复要求]\n像本人聊天一样直接回复正文，尽量自然、简短、口语化。"
_INTERNAL_USER_HINTS = frozenset({
    "工具已成功执行并返回结果。请基于这些信息向用户做出回复，不要重复调用相同的工具。",
    "请向用户说明刚才工具执行的结果或回答用户的问题。",
})


def is_wechat_auto_reply(context: Any) -> bool:
    return bool(
        context
        and context.get("channel_type") == "wechat_desktop"
        and context.get("wechat_desktop_auto_reply")
        and not context.get("is_scheduled_task")
        and not str(context.get("session_id") or "").startswith("scheduler_")
    )


def text_of(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"] for block in content
            if isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    return ""


def _visible_user(message: Mapping[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    return bool(text_of(message)) and not (
        isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )
    )


def _valid_input_paths(paths: list[str]) -> list[str]:
    result = []
    for path in paths:
        if not isinstance(path, str) or not os.path.isabs(path):
            continue
        if os.path.isfile(path) and path not in result:
            result.append(path)
    return result[-2:]


def user_message(user_text: str, input_paths: list[str] | None = None, *, is_reference: bool = False) -> dict:
    """结构标记放在 text 块中，随现有 SQLite content JSON 一同保存。"""
    block = {"type": "text", "text": user_text, "source": USER_SOURCE}
    paths = _valid_input_paths(input_paths or [])
    if paths:
        block["input_artifact_paths"] = paths
    if is_reference:
        block["is_reference"] = True
    return {
        "role": "user",
        "content": [block],
    }


def _original_user(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "text"
        and block.get("source") == USER_SOURCE for block in content
    ):
        # 已知原文中的标题、换行和提示词样例都属于用户内容，不能再次解析。
        return text_of(message)
    text = text_of(message)
    if not text.startswith(_LEGACY_HEADINGS):
        return ""
    if text.count(_LEGACY_REPLY_BOUNDARY) != 1:
        return ""
    before_requirements, _ = text.split(_LEGACY_REPLY_BOUNDARY, 1)
    for heading in ("[需要回复的新消息]\n", "[需要回复的引用消息]\n"):
        if before_requirements.startswith(heading):
            return before_requirements[len(heading):]
        # 仅迁移识别到完整固定结构的旧微信包装；未知格式不注入。
        if before_requirements.startswith(_LEGACY_HEADINGS[:2]):
            boundary = "\n" + heading
            if before_requirements.count(boundary) == 1:
                return before_requirements.split(boundary, 1)[1]
    return ""


def is_wechat_session_user(message: Mapping[str, Any]) -> bool:
    return bool(_original_user(message))


def is_internal_user_hint(message: Mapping[str, Any]) -> bool:
    if message.get("role") != "user" or text_of(message) not in _INTERNAL_USER_HINTS:
        return False
    content = message.get("content")
    return not (
        isinstance(content, list) and any(
            isinstance(block, dict) and block.get("source") == USER_SOURCE
            for block in content
        )
    )


def model_messages(messages: list[dict]) -> list[dict]:
    """API 仅接收协议字段，内部 text 元数据留在 Agent 与持久化记录。"""
    has_marker = any(
        isinstance(block, dict) and block.get("type") == "text"
        and block.get("source") in (USER_SOURCE, IMAGE_SOURCE)
        for message in messages for block in (
            message.get("content") if isinstance(message.get("content"), list) else []
        )
    )
    if not has_marker:
        return messages
    result = deepcopy(messages)
    for message in result:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (isinstance(block, dict) and block.get("type") == "text"
                    and block.get("source") in (USER_SOURCE, IMAGE_SOURCE)):
                for key in ("source", "input_artifact_paths", "is_reference"):
                    block.pop(key, None)
    return result


def _valid_image_paths(paths: list[str]) -> list[str]:
    from agent.tools.utils.image_artifacts import build_file_to_send

    result = []
    for path in paths:
        payload = build_file_to_send(path)
        if payload and payload["path"] not in result:
            result.append(payload["path"])
    return result[-2:]


def _turn_image_paths(messages: list[dict]) -> list[str]:
    from agent.tools.utils.image_artifacts import extract_image_artifacts_from_tool_result

    names = {}
    paths = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                names[block.get("id")] = str(block.get("name") or "")
            elif block.get("type") == "tool_result" and not block.get("is_error"):
                for item in extract_image_artifacts_from_tool_result(
                    names.get(block.get("tool_use_id"), ""), block.get("content")
                ):
                    paths.append(item["path"])
            elif block.get("type") == "text":
                # 固定产物指针在重启的 text-only 恢复后仍能识别。
                for line in str(block.get("text") or "").splitlines():
                    if line.startswith(_IMAGE_PREFIX):
                        paths.append(line[len(_IMAGE_PREFIX):])
    return _valid_image_paths(paths)


def preserve_image_pointers(messages: list[dict], files_to_send: list[dict]) -> list[dict]:
    """只持久化已验证的本地图片指针，保留原工具审计而不复制工具正文。"""
    paths = _valid_image_paths([
        str(item.get("path") or "") for item in files_to_send
        if isinstance(item, dict) and item.get("file_type") == "image"
    ])
    if not paths:
        return messages
    result = deepcopy(messages)
    final = next((msg for msg in reversed(result) if msg.get("role") == "assistant"), None)
    if final is None:
        final = {"role": "assistant", "content": []}
        result.append(final)
    if isinstance(final.get("content"), str):
        final["content"] = [{"type": "text", "text": final["content"]}]
    if not isinstance(final.get("content"), list):
        final["content"] = []
    final["content"].append({
        "type": "text", "text": "\n".join(_IMAGE_PREFIX + path for path in paths),
        "source": IMAGE_SOURCE,
    })
    return result


def compact_session_messages(
    messages: list[dict], *, max_turns: int, max_chars: int, is_reference: bool = False
) -> list[dict]:
    """执行前仅保留少量原消息和最终回复；引用轮次完全隔离旧消息。"""
    if is_reference or max_turns <= 0 or max_chars <= 0:
        return []
    turns = []
    current = None
    for message in messages:
        if _visible_user(message) and not is_internal_user_hint(message):
            if current is not None:
                turns.append(current)
            blocks = message.get("content")
            input_paths = []
            was_reference = False
            if isinstance(blocks, list):
                for block in blocks:
                    if isinstance(block, dict) and block.get("source") == USER_SOURCE:
                        stored_paths = block.get("input_artifact_paths")
                        if isinstance(stored_paths, list):
                            input_paths.extend(stored_paths)
                        was_reference = was_reference or block.get("is_reference") is True
            original_user = _original_user(message)
            if original_user and not was_reference:
                legacy = text_of(message)
                marked_user = isinstance(blocks, list) and any(
                    isinstance(block, dict) and block.get("source") == USER_SOURCE for block in blocks
                )
                was_reference = (
                    not marked_user
                    and legacy.startswith(_LEGACY_HEADINGS)
                    and "[需要回复的引用消息]\n" in legacy.split(_LEGACY_REPLY_BOUNDARY, 1)[0]
                )
            current = {
                "user": original_user, "messages": [], "reply": "",
                "input_paths": _valid_input_paths(input_paths),
                "is_reference": was_reference,
            }
        elif current is not None:
            current["messages"].append(message)
            if message.get("role") == "assistant" and text_of(message):
                # 有 tool_use 的助手文本属于中间过程，不作为最终回复。
                content = message.get("content")
                if not message.get("tool_calls") and not (
                    isinstance(content, list) and any(
                        isinstance(block, dict) and block.get("type") == "tool_use"
                        for block in content
                    )
                ):
                    current["reply"] = text_of(message)
    if current is not None:
        turns.append(current)
    # 引用轮次建立执行输入边界。重启恢复后也不重新拼入该边界之前的旧话题。
    for index in range(len(turns) - 1, -1, -1):
        if turns[index]["is_reference"]:
            turns = turns[index:]
            break
    # 中间助手说明之后若仍有工具结果或中断，不将该说明误当最终回复。
    turns = [
        turn for turn in turns if turn["user"] and turn["reply"] and turn["messages"]
        and turn["messages"][-1].get("role") == "assistant"
        and text_of(turn["messages"][-1]) == turn["reply"]
    ][-max_turns:]
    retained = []
    remaining = max_chars
    for turn in reversed(turns):
        paths = _turn_image_paths(turn["messages"])
        reply_lines = [
            line for line in turn["reply"].splitlines()
            if not line.startswith((_IMAGE_PREFIX, _INPUT_PREFIX))
        ]
        reply = "\n".join(reply_lines)
        pointer_lines = [_IMAGE_PREFIX + path for path in paths]
        pointer_lines.extend(_INPUT_PREFIX + path for path in turn["input_paths"] if path not in paths)
        while pointer_lines and len("\n".join(pointer_lines)) + 3 >= remaining:
            pointer_lines.pop(0)
        pointer = "\n".join(pointer_lines)
        reserve = len(pointer) + (1 if pointer else 0)
        available = remaining - reserve
        if available < 2:
            break
        user = turn["user"]
        if len(user) + len(reply) > available:
            user = _truncate(user, min(len(user), max(1, available // 2)))
            reply = _truncate(reply, available - len(user))
            if not user or not reply:
                continue
        if pointer:
            reply = reply + "\n" + pointer
        retained.append([
            user_message(user, turn["input_paths"], is_reference=turn["is_reference"]),
            {"role": "assistant", "content": [{"type": "text", "text": reply}]},
        ])
        remaining -= len(user) + len(reply)
        if remaining < 2:
            break
    return [msg for pair in reversed(retained) for msg in pair]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATED):
        return ""
    return text[:limit - len(_TRUNCATED)] + _TRUNCATED


def prepare_agent_session(agent: Any, context: Any) -> bool:
    if not is_wechat_auto_reply(context):
        return False
    from channel.wechat_desktop.config import DEFAULT_CONFIG

    max_turns = max(0, int(context.get(
        "wechat_desktop_session_max_turns", DEFAULT_CONFIG["reply_session_max_turns"]
    )))
    max_chars = max(0, int(context.get(
        "wechat_desktop_session_max_chars", DEFAULT_CONFIG["reply_session_max_chars"]
    )))
    with agent.messages_lock:
        agent.messages = compact_session_messages(
            agent.messages, max_turns=max_turns, max_chars=max_chars,
            is_reference=bool(context.get("wechat_desktop_is_reference")),
        )
    return True


def current_run_messages(messages: list[dict], query: str, fallback: list[dict]) -> list[dict]:
    """用本轮真实 user 边界取审计片段，避免 executor 裁剪导致计数偏移。"""
    for index in range(len(messages) - 1, -1, -1):
        if _visible_user(messages[index]) and text_of(messages[index]) == query:
            return deepcopy(messages[index:])
    return deepcopy(fallback)


def replace_run_user(messages: list[dict], original: dict) -> list[dict]:
    result = deepcopy(messages)
    for index, message in enumerate(result):
        if _visible_user(message):
            result[index] = deepcopy(original)
            break
    return result
