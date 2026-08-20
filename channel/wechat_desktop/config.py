"""wechat_desktop 通道配置。

本通道的**全部配置默认值与本机常用项**都写在本文件。根目录 ``config.json`` /
``config-template.json`` **不必**再写 ``wechat_desktop`` 段；若 JSON 里出现该段，
仅作为可选覆盖，会浅合并到本文件默认值之上。

全局/Agent 配置（模型、workspace 等）与密钥（``~/.cow/.env``）不放在这里。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from config import conf

DEFAULT_CONFIG: dict[str, Any] = {
    # 后端与 UI 操作节奏。后端名称是迁移扩展点，当前实现为 Windows UIA。
    "desktop_backend": "uia",
    "uia_focus_settle_ms": 350,
    "uia_recovery_attempts": 3,
    "uia_recovery_settle_ms": 500,
    "uia_selection_settle_ms": 150,
    "uia_hook_settle_ms_min": 300,
    "uia_hook_settle_ms_max": 800,
    "uia_focus_settle_ms_min": 350,
    "uia_focus_settle_ms_max": 700,
    "uia_selection_settle_ms_min": 250,
    "uia_selection_settle_ms_max": 500,
    "uia_paste_settle_ms_min": 150,
    "uia_paste_settle_ms_max": 300,
    "uia_pre_send_settle_ms_min": 100,
    "uia_pre_send_settle_ms_max": 250,
    # 全局两次发送间隔（毫秒，随机区间）
    "uia_send_interval_ms_min": 1000,
    "uia_send_interval_ms_max": 2000,
    # 每条气泡粘贴上限。更长的回复会按句号/换行切开后连续发送。
    "uia_text_chunk_chars": 2000,
    "uia_conversation_cooldown_seconds": 5,
    # 已发送消息回声抑制：UIA runtime ID 长期保留，纯文本匹配仅作短时兜底。
    "outgoing_echo_suppression_seconds": 1800,
    "outgoing_echo_text_suppression_seconds": 120,
    "uia_owner_lookup_timeout_seconds": 2.0,
    "uia_owner_failure_cache_seconds": 60.0,

    # 消息观察与会话历史解析。
    "uia_group_sender_ocr_enabled": True,
    "uia_group_sender_ocr_body_min_score": 0.6,
    "uia_group_sender_ocr_name_min_score": 0.75,
    "uia_group_sender_ocr_text_similarity": 0.78,
    "uia_group_sender_ocr_name_gap_px": 48,
    "shell_hook_reconcile_seconds": 15,
    "shell_hook_reconcile_enabled": False,
    "shell_hook_debounce_ms": 250,
    "reply_monitor_interval_seconds": 1.0,
    "active_conversation_burst_limit": 5,

    # 当前会话聊天记录窗口读取。仅通过 UIA 读取，不写入本地会话历史库。
    "wechat_history_read_enabled": True,
    "wechat_history_max_messages": 50,
    "wechat_history_open_timeout_seconds": 4.0,
    "wechat_history_total_timeout_seconds": 8.0,
    "wechat_history_scroll_settle_ms_min": 250,
    "wechat_history_scroll_settle_ms_max": 500,
    "wechat_history_max_scrolls": 12,
    "wechat_history_no_progress_limit": 2,
    "wechat_history_close_timeout_seconds": 2.0,

    # 自动回复准入策略。shadow_mode=True 时只观察，不向微信发送内容。
    "auto_reply_private_all": True,
    "auto_reply_groups_all": True,
    "auto_reply_blacklist": [],
    "conversation_history_retention_days": 90,

    # 可选诊断能力：会话扫描细粒度日志写入 run.log，不打印到控制台。
    "diagnostic_logging": True,

    # Agent 回复周期与工具调用进度通知。
    "reply_cycle_timeout_seconds": 180,
    "agent_tool_notice_enabled": True,
    "agent_tool_notice_once_per_reply": True,
    "agent_preflight_notice_enabled": True,
    "agent_skill_notice_templates": [
        "这题得请 `{name}` skill 出场了，我去搬个救兵，稍等一下 🧰",
        "我先翻开 `{name}` skill 的小抄，马上回来 📖",
        "正在召唤 `{name}` skill，答案已经在路上了 ✨",
    ],
    "agent_tool_notice_templates": [
        "我准备调用 `{tool_name}` tool 查一查，稍等我操作一下 🔧",
        "轮到 `{tool_name}` tool 上场了，我去后台忙活一下 🛠️",
        "先让 `{tool_name}` tool 跑一趟，别走开，马上带结果回来 🚀",
    ],
    "share_browser_notice_templates": [
        "我钻进微信内置浏览器翻翻这张卡片，马上把重点捞回来 🧭",
        "微信内置浏览器的小窗已经推开，我去页面里巡一圈，稍等片刻 👀",
        "卡片先别跑，我让微信内置浏览器现场开箱，看看里面藏了什么 📦",
    ],
    "share_content_fetch_notice_templates": [
        "内置浏览器没读到正文，我改用 `web_fetch` 去拆这张卡片 🕵️",
        "链接拿到了，`web_fetch` 正在把页面内容搬回来 🌐",
        "卡片已经翻面，我去网上把正文捞回来，稍等片刻 🚚",
    ],
    "agent_failure_notice_enabled": True,
    "agent_failure_notice_templates": [
        "刚才脑内小齿轮打了个滑，我这次没能答上来 😵‍💫 请再戳我一下，我重新来过。",
        "答案在路上迷了个路，这一轮先投降 🧭 你可以再发一次，我会重新出发。",
        "我刚和服务器猜拳输了，回复没拿回来 🤖 再问我一次吧。",
    ],
    "auto_reply_contacts": [],
    "auto_reply_groups": ["小小地下联络站"],
    "group_reply_mode": "at_only",
    "group_command_prefixes": ["/cow"],
    "self_display_name": "",

    # 图片、文件和引用附件的提取策略。
    # Agent 生成的图片（Seedream / send 工具）需开启，否则只会发前置文本。
    "auto_send_images": True,
    "analyze_incoming_images": False,
    "resolve_message_references": True,
    # 引用分享卡片优先直接读取微信内置浏览器正文；读不到时才复制链接并走网络解析。
    # 各步的随机等待（毫秒）。过小容易点空，机器慢或页面未加载时再加大。
    "uia_share_browser_direct_read_enabled": True,
    "uia_share_browser_direct_read_settle_ms_min": 300,
    "uia_share_browser_direct_read_settle_ms_max": 600,
    "uia_share_browser_direct_read_min_chars": 20,
    # 微信 WebView 没有 Playwright 的 DOMContentLoaded 事件可等，因此参考 browser
    # tool 的“短暂等待初始渲染”策略，轮询 UIA Document，正文连续稳定后才读取。
    "uia_share_browser_load_timeout_seconds": 6,
    "uia_share_browser_load_poll_ms": 250,
    "uia_share_browser_load_min_wait_ms": 800,
    "uia_share_browser_content_stable_polls": 2,
    "uia_share_browser_direct_read_ready_chars": 80,
    "uia_share_browser_direct_read_max_chars": 50000,
    "uia_share_browser_open_settle_ms_min": 600,
    "uia_share_browser_open_settle_ms_max": 1200,
    "uia_share_browser_menu_settle_ms_min": 250,
    "uia_share_browser_menu_settle_ms_max": 500,
    "uia_share_browser_clipboard_settle_ms_min": 200,
    "uia_share_browser_clipboard_settle_ms_max": 450,
    "uia_share_browser_close_settle_ms_min": 250,
    "uia_share_browser_close_settle_ms_max": 500,
    "uia_share_browser_close_timeout_seconds": 2,
    # 回复期间微信 UI 重建/滚动后，同一消息可能短暂消失再出现；按其 UIA runtime
    # 身份抑制重复入队。新气泡会获得新的 runtime id，不影响用户重复追问。
    "uia_recent_target_suppression_seconds": 300,
    # 直接读取内置浏览器失败后，回退到 web_fetch 拉取网页正文。
    "uia_image_viewer_enabled": True,
    "uia_image_viewer_before_close_ms_min": 300,
    "uia_image_viewer_before_close_ms_max": 500,
    "uia_file_download_enabled": True,
    "uia_file_save_as_enabled": True,
    "uia_file_download_timeout_seconds": 10,

    # 私聊聚合、发送限流、数据保留与首次启动行为。
    "private_message_aggregation_min_ms": 500,
    "private_message_aggregation_max_ms": 1200,
    "private_message_aggregation_max_wait_ms": 4000,
    "max_send_per_minute": 5,
    "max_send_per_hour": 60,
    "retention_days": 7,
    "bootstrap_existing_messages": False,
    "process_startup_unread_messages": True,

    # 每日热点广播（通道专属）：到点后在子线程准备百度热搜首条，再入全局回复 FIFO。
    "daily_hot_broadcast_enabled": True,
    "daily_hot_broadcast_time": "18:00",
    "daily_hot_broadcast_tab": "livelihood",
    "daily_hot_broadcast_message_prefix": "📰 今日热点",
    
    # False：正式发送；True：只观察不发
    "shadow_mode": False,
}


def load_wechat_desktop_config(
    raw: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """合并通道默认配置与可选的用户覆盖。

    ``raw`` 通常来自 ``conf().get("wechat_desktop")``；缺省或为空时只用本文件默认值。
    测试可直接传入字典。
    """
    if raw is None:
        configured = conf().get("wechat_desktop", {})
    else:
        configured = raw
    merged = dict(DEFAULT_CONFIG)
    if isinstance(configured, Mapping):
        merged.update(dict(configured))
    return merged
