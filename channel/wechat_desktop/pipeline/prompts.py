"""微信桌面通道的提示词、通知文案和纯函数。

这些函数不持有通道状态，便于测试和在扫描、物化、回复各阶段复用。
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

from channel.wechat_desktop.models import WechatDesktopEvent

ATTACHMENT_REFERENCE_REQUIRED_REPLY = (
    "为了确保我读取的是正确的图片或文件，请在微信中引用对应的图片或文件消息后再提问。"
)
DEFAULT_BOT_MENTION_ALIASES = ("颜料盒bot",)


def _normalize_auto_reply_text(value) -> str:
    """去掉“建议回复：”等代写包装，只保留要发给微信用户的正文。"""
    text = str(value or "").strip()
    if not text:
        return ""
    wrapper = re.compile(
        r"^(?:(?:你)?可以|建议|推荐)?\s*"
        r"(?:这样\s*)?回(?:复)?(?:对方)?(?:内容)?\s*[:：]\s*",
        re.IGNORECASE,
    )
    text = wrapper.sub("", text, count=1).strip()
    text = re.sub(r"^(?:回复内容|回复)\s*[:：]\s*", "", text, count=1).strip()
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE).strip()
    if text.startswith("**") and text.endswith("**") and len(text) > 4:
        text = text[2:-2].strip()
    quote_pairs = [("“", "”"), ('"', '"'), ("「", "」")]
    for left, right in quote_pairs:
        if text.startswith(left) and text.endswith(right) and len(text) > 2:
            text = text[len(left):-len(right)].strip()
            break
    return text


def _strip_group_bot_mentions(value: str, aliases) -> str:
    """从群聊提示词移除机器人的 @，保留对其他成员的显式 @。"""
    text = str(value or "")
    normalized_aliases = list(
        dict.fromkeys(
            str(alias or "").strip().lstrip("@").casefold()
            for alias in aliases or []
            if str(alias or "").strip().lstrip("@")
        )
    )
    for alias in normalized_aliases:
        pattern = re.compile(
            rf"@{re.escape(alias)}(?=$|[\s\u2005\u00a0,，。.!！?？:：])",
            re.IGNORECASE,
        )
        text = pattern.sub("", text)
    # 微信会在 @ 后插入特殊空格；删除名字后顺手收敛横向空白，但不合并换行。
    text = re.sub(r"[\t \u2005\u00a0]{2,}", " ", text)
    text = re.sub(r"^[\t \u2005\u00a0]+", "", text, flags=re.MULTILINE)
    return text.strip()


def _render_event_context_lines(event: WechatDesktopEvent) -> tuple[str, list[str]]:
    """将事件快照渲染成提示词片段，不在这里重新读取微信 UI。

    引用消息只返回被引用的一层内容；普通消息返回候选历史，后续由提示词要求
    Agent 按相关性筛选。这样可以保证排队期间 UI 变化不会改变回复依据。
    """
    if event.reference:
        reference = event.reference
        speaker = str(reference.get("sender_name") or "原消息发送者")
        reference_type = str(reference.get("content_type") or "text").lower()
        reference_path = str(reference.get("file_path") or "").strip()
        reference_content = str(reference.get("content") or "").strip()
        if reference_type == "share_card":
            url = str(reference.get("url") or "").strip()
            platform = str(reference.get("platform") or "").strip()
            browser_content = str(
                reference.get("browser_content") or ""
            ).strip()
            fetched = str(reference.get("fetched_content") or "").strip()
            fetch_status = str(reference.get("fetch_status") or "").strip()
            parts = [f"[第三方分享卡片] {reference_content or '标题不可用'}"]
            if platform:
                parts.append(f"平台：{platform}")
            if url:
                parts.append(f"链接：{url}")
            if browser_content:
                parts.append(f"微信内置浏览器页面正文：\n{browser_content}")
            elif fetched:
                parts.append(f"WebFetch 网页结果：\n{fetched}")
            elif fetch_status:
                parts.append("WebFetch 网页结果不可用。")
            if not browser_content and not fetched:
                parts.append("页面正文不可用；不要根据标题猜测页面内容。")
            reference_content = "\n".join(parts)
        elif reference_type == "image" and reference_path:
            reference_content = f"[图片: {reference_path}]"
        elif reference_type == "file" and reference_path:
            reference_content = f"[文件: {reference_path}]"
        elif not reference_content:
            labels = {"image": "图片", "file": "文件"}
            reference_content = f"[{labels.get(reference_type, '原消息')}内容不可用]"
        return "[被引用的内容]", [f"{speaker}: {reference_content}"]

    history_lines = []
    for item in event.history:
        if event.is_group:
            speaker = str(
                item.get("sender_name") or item.get("sender") or "群成员"
            )
        else:
            speaker = "历史消息"
        history_lines.append(
            f"{speaker}: {item.get('content') or '[非文字消息]'}"
        )
    return "[候选会话上下文，需按关联度筛选]", history_lines


def _reply_requirements(event: WechatDesktopEvent) -> str:
    """生成引用消息与普通消息各自的回复约束。"""
    style = (
        "像本人聊天一样直接回复正文，尽量自然、简短、口语化。"
        "不要出现“可以回复”“可以回”“建议回复”“回复如下”等"
        "提示语，也不要用引号、Markdown 加粗或解释你正在代写。"
    )
    group_rule = (
        "群聊中，只有待回复消息明确使用“@成员”时，才认为发送者在询问、"
        "指向或要求该成员回应；没有 @ 时，不要仅因正文出现成员姓名就作此推断。"
        if event.is_group
        else ""
    )
    if event.reference:
        return (
            style
            + "当前是引用消息，只根据“被引用的内容”和“需要回复的引用消息”作答；"
            "不要使用、补充或推断引用之外的会话上下文。"
            + group_rule
        )
    return (
        style
        + group_rule
        + "先判断候选会话上下文与待回复消息的语义关联度，"
        "只保留并使用关联度高、能帮助理解当前意图的内容；"
        "关联度低、已结束或属于其他话题的内容直接忽略；"
        "如果候选上下文整体都无法可靠支撑当前消息的意图，就不要为了让回复显得完整而强行串联、补全或猜测。"
        "此时只基于当前消息中明确的信息作答；如果当前消息缺少必要信息，再用一句话请求对方补充，"
        "不要把低关联上下文中的内容改写成用户的真实意思，也不要把猜测写成确认式结论。"
        "不要逐条回复上下文，也不要在答复中复述筛选过程。"
        "若候选上下文包含本地文件或图片，只在待回复消息确实要求处理该附件时"
        "调用相应工具读取；否则不要擅自分析附件。"
    )


def _tool_notice_subject(data: dict) -> tuple[str, str]:
    """返回通知类型和展示名，并把读取 SKILL.md 识别为技能调用。"""
    tool_name = str(data.get("tool_name") or "tool").strip() or "tool"
    arguments = data.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            arguments = {}
    if isinstance(arguments, dict):
        for key in ("path", "file_path", "location"):
            value = str(arguments.get(key) or "").replace("\\", "/").rstrip("/")
            match = re.search(r"(?:^|/)skills/([^/]+)/SKILL\.md$", value, re.I)
            if match:
                return "skill", match.group(1)
    return "tool", tool_name


def _format_agent_notice(templates: list[str], kind: str, name: str) -> str:
    """渲染工具/技能通知；旧模板没有占位符时自动补上工具名。"""
    fallback = f"我准备调用 `{name}` {kind}，稍等一下 🛠️"
    if not templates:
        return fallback
    try:
        notice = random.choice(templates).format(
            name=name,
            tool_name=name,
            kind=kind,
        )
    except (KeyError, ValueError):
        return fallback
    # 旧的自定义工具模板可能没有占位符；工具通知仍必须写出正在执行的具体工具名。
    if kind == "tool" and name.casefold() not in notice.casefold():
        notice = f"{notice.rstrip()} 当前工具：`{name}`。"
    return notice


def _format_failure_notice(templates) -> str:
    """挑选一条不暴露内部异常细节的轻松失败提示。"""
    fallback = "刚才脑内小齿轮打了个滑 😵‍💫 请再戳我一下，我重新来过。"
    candidates = [str(item).strip() for item in templates or [] if str(item).strip()]
    return random.choice(candidates) if candidates else fallback


def _preflight_tool_notice_data(event: WechatDesktopEvent) -> dict | None:
    """根据附件类型预判首个工具，使耗时读取开始前就能发送进度通知。"""
    content_type = str(event.content_type or "").lower()
    path = str(event.content or "")
    if event.reference:
        reference_type = str(event.reference.get("content_type") or "").lower()
        reference_path = str(event.reference.get("file_path") or "")
        if reference_type == "share_card":
            return {
                "tool_name": "微信内置浏览器",
                "notice_template_key": "share_browser_notice_templates",
            }
        if reference_type in {"image", "file"}:
            if not reference_path:
                return None
            content_type, path = reference_type, reference_path
    if content_type == "share_card":
        return {
            "tool_name": "微信内置浏览器",
            "notice_template_key": "share_browser_notice_templates",
        }
    if content_type == "image":
        return {"tool_name": "vision", "arguments": {"path": path}}
    if content_type != "file":
        return None
    suffix = Path(path).suffix.lower()
    skill_names = {
        ".doc": "docx",
        ".docx": "docx",
        ".pdf": "pdf-reader",
        ".xls": "xlsx",
        ".xlsx": "xlsx",
    }
    skill_name = skill_names.get(suffix)
    if skill_name:
        return {
            "tool_name": "read",
            "arguments": {"path": f"skills/{skill_name}/SKILL.md"},
        }
    return {"tool_name": "file-reader", "arguments": {"path": path}}


def _is_network_reply_error(value) -> bool:
    """判断 Agent 错误是否属于应终止当前排队任务的网络故障。"""
    text = str(value or "").casefold()
    return any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "connection error",
            "connection reset",
            "connection aborted",
            "sslerror",
            "ssl:",
            "unexpected_eof",
            "max retries exceeded",
            "network is unreachable",
        )
    )
