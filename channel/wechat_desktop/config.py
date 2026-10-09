"""wechat_desktop 通道配置。

本通道的**全部业务配置**只写在本文件 ``DEFAULT_CONFIG``。包括 UIA 节拍、白名单、
``shadow_mode``、限流、通知模板、引用/附件策略等。

根目录 ``config.json`` / ``config-template.json`` / ``config.py`` 只放跨通道通用项
（模型、Agent、``channel_type``、Web 控制台等），**不要**再写 ``wechat_desktop`` 段。
全局/Agent 配置与密钥（``~/.cow/.env``）也不放在这里。
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, Mapping, Optional

from common.log import logger
from config import conf

DEFAULT_CONFIG: dict[str, Any] = {
    # 后端与 UI 操作节奏。db_uia 从加密库读取，发送仍使用 Windows UIA。
    "desktop_backend": "uia",
    # 数据库读取配置唯一来源。空路径自动定位，空账号仅在可唯一确定时绑定。
    "db_data_dir": "",
    "db_account": "",
    "db_poll_interval_seconds": 1.0,
    # 空值使用 Agent 工作区的 wechat_desktop_db，按账号隔离。
    "db_cache_dir": "",
    "db_batch_size": 200,
    "db_snapshot_retry_attempts": 3,
    "db_key_scan_timeout_seconds": 30.0,
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
    "uia_file_menu_settle_ms_min": 250,
    "uia_file_menu_settle_ms_max": 450,
    "uia_file_save_dialog_settle_ms_min": 400,
    "uia_file_save_dialog_settle_ms_max": 700,
    "uia_image_viewer_settle_ms_min": 500,
    "uia_image_viewer_settle_ms_max": 900,
    "uia_file_selection_settle_ms_min": 100,
    "uia_file_selection_settle_ms_max": 200,
    "uia_file_clipboard_settle_ms_min": 200,
    "uia_file_clipboard_settle_ms_max": 400,
    "uia_image_viewer_close_settle_ms_min": 200,
    "uia_image_viewer_close_settle_ms_max": 400,
    "uia_key_event_settle_ms_min": 20,
    "uia_key_event_settle_ms_max": 40,
    "uia_input_focus_settle_ms_min": 50,
    "uia_input_focus_settle_ms_max": 100,
    "uia_paste_retry_ms_min": 150,
    "uia_paste_retry_ms_max": 300,
    "uia_reference_return_settle_ms_min": 300,
    "uia_reference_return_settle_ms_max": 600,
    "uia_reference_menu_settle_ms_min": 250,
    "uia_reference_menu_settle_ms_max": 450,
    "uia_reference_locate_settle_ms_min": 500,
    "uia_reference_locate_settle_ms_max": 900,
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
    # 会话读取失败后主动重试；点击可能已清除未读标记，不能只等下一次闪烁。
    "uia_scan_retry_seconds": 1.0,
    "event_receipt_capacity": 100,
    # 待处理容量（不含正在执行的任务）；超过容量拒绝新任务并记录原因。
    "reply_queue_capacity": 100,
    "materialize_queue_capacity": 100,
    "reply_queue_max_wait_seconds": 300,
    "worker_join_timeout_seconds": 2.0,

    # 聊天记录只读查询。UIA 读取当前会话，db_uia 也支持稳定会话 ID。
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

    # 模型 API 有限次指数退避重试。相对上游 CowAgent 外层新增，只放本通道配置。
    "model_api_max_retries": 3,
    "model_api_retry_base_seconds": 2.0,
    "model_api_retry_max_seconds": 10.0,
    "model_api_retry_jitter_seconds": 0.5,

    # Agent 回复周期与工具调用进度通知。
    "reply_cycle_timeout_seconds": 180,
    "agent_tool_notice_enabled": True,
    "agent_tool_notice_once_per_reply": True,
    # 仅在 Agent 真正开始调用工具时通知；不要在 LLM 决策前提前猜工具。
    "agent_preflight_notice_enabled": False,
    # 用户看不懂的底层/内部工具不发微信进度通知。读取 SKILL.md 仍按 skill 通知。
    "agent_tool_notice_silent_tools": [
        "bash",
        "ls",
        "read",
        "write",
        "edit",
        "send",
        "env_config",
        "memory_search",
        "memory_get",
        "evolution_undo",
        "wechat_desktop",
        "wechat_history",
        "grep",
        "find",
    ],
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
    # 自动回复群白名单。
    "auto_reply_groups": ["小小地下联络站", "JY生活问候群", "22~25级实验室科研天才们", "816吃喝玩乐群"],
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

    # False：正式发送；True：只观察不发
    "shadow_mode": False,
}


def load_wechat_desktop_config(
    raw: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """返回通道业务配置。

    运行时只读本文件 ``DEFAULT_CONFIG``，不再从根 ``config.json`` 合并
    ``wechat_desktop`` 段。``raw`` 仅供测试注入覆盖。
    """
    merged = deepcopy(DEFAULT_CONFIG)
    if raw is None:
        leftover = conf().get("wechat_desktop")
        if leftover:
            logger.warning(
                "[WechatDesktop] 已忽略 config.json 中的 wechat_desktop 段；"
                "请把业务配置写到 channel/wechat_desktop/config.py"
            )
        return validate_config(merged)
    if isinstance(raw, Mapping):
        merged.update(deepcopy(dict(raw)))
    return validate_config(merged)


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """在启动前报告配置错误，业务层无需维护另一套默认值。"""
    for key, default in DEFAULT_CONFIG.items():
        value = config[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean")
        elif isinstance(default, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{key} must be a finite non-negative number")
            if isinstance(default, int) and not isinstance(value, int):
                raise ValueError(f"{key} must be an integer")
        elif isinstance(default, list):
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"{key} must be a list of strings")
    for key in ("reply_queue_capacity", "materialize_queue_capacity", "event_receipt_capacity", "reply_queue_max_wait_seconds", "worker_join_timeout_seconds", "reply_cycle_timeout_seconds", "db_poll_interval_seconds", "db_batch_size", "db_snapshot_retry_attempts", "db_key_scan_timeout_seconds"):
        if config[key] <= 0:
            raise ValueError(f"{key} must be positive")
    if config["desktop_backend"] not in {"uia", "db_uia"}:
        raise ValueError("desktop_backend must be uia or db_uia")
    for key in ("db_data_dir", "db_account", "db_cache_dir"):
        if not isinstance(config[key], str):
            raise ValueError(f"{key} must be a string")
    for key in config:
        if key.endswith("_min") and key[:-4] + "_max" in config:
            if config[key] > config[key[:-4] + "_max"]:
                raise ValueError(f"{key} exceeds its maximum")
    if not 100 <= config["uia_text_chunk_chars"] <= 4000:
        raise ValueError("uia_text_chunk_chars must be between 100 and 4000")
    if config["group_reply_mode"] not in {"all", "at_only", "prefix", "at_or_prefix"}:
        raise ValueError("invalid group_reply_mode")
    return config
