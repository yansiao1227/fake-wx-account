"""微信桌面 prompts 回归测试。"""
from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.config import DEFAULT_CONFIG
from channel.wechat_desktop.pipeline.prompts import (
    _format_agent_notice,
    _format_failure_notice,
    _is_network_reply_error,
    _is_user_visible_tool_notice,
    _link_reading_instruction,
    _link_reading_urls,
    _preflight_tool_notice_data,
    _render_event_context_lines,
    _tool_notice_subject,
)
import pytest


def test_network_reply_error_detection():
    assert _is_network_reply_error(
        "Agent error: Connection error: SSL: UNEXPECTED_EOF_WHILE_READING"
    )
    assert _is_network_reply_error("request timed out")
    assert not _is_network_reply_error("invalid tool arguments")


def test_tool_notice_subject_recognizes_skill_reads_and_regular_tools():
    assert _tool_notice_subject(
        {
            "tool_name": "read",
            "arguments": {"path": r"C:\cow\skills\web-search\SKILL.md"},
        }
    ) == ("skill", "web-search")
    assert _tool_notice_subject(
        {
            "tool_name": "read",
            "arguments": '{"location":"skills/vision/SKILL.md"}',
        }
    ) == ("skill", "vision")
    assert _tool_notice_subject(
        {
            "tool_name": "read",
            "arguments": {
                "path": r"C:\cow\skills\@user_087fbff2\govwriting\SKILL.md"
            },
        }
    ) == ("skill", "govwriting")
    assert _tool_notice_subject(
        {"tool_name": "web_search", "arguments": {"query": "天气"}}
    ) == ("tool", "web_search")


def test_tool_notice_templates_always_output_the_concrete_tool_name():
    assert all(
        "{tool_name}" in template
        for template in DEFAULT_CONFIG["agent_tool_notice_templates"]
    )
    assert (
        _format_agent_notice(
            ["我准备调用 `{tool_name}` tool"], "tool", "web_search"
        )
        == "我准备调用 `web_search` tool"
    )
    assert (
        _format_agent_notice(["我正在使用工具，请稍等"], "tool", "vision")
        == "我正在使用工具，请稍等 当前工具：`vision`。"
    )
    assert (
        _format_agent_notice(["调用 `{name}` skill"], "skill", "pdf-reader")
        == "调用 `pdf-reader` skill"
    )


def test_failure_notice_has_fun_fallback_when_templates_are_empty():
    assert "小齿轮" in _format_failure_notice([])
    assert _format_failure_notice(["机器人暂时打了个喷嚏 🤖"]) == (
        "机器人暂时打了个喷嚏 🤖"
    )


def test_user_visible_tool_notice_skips_internal_tools_but_keeps_skills():
    assert _is_user_visible_tool_notice({"tool_name": "bash"}) is False
    assert _is_user_visible_tool_notice({"tool_name": "read"}) is False
    assert _is_user_visible_tool_notice({"tool_name": "ls"}) is False
    assert _is_user_visible_tool_notice({"tool_name": "web_search"}) is True
    assert _is_user_visible_tool_notice({"tool_name": "vision"}) is True
    assert _is_user_visible_tool_notice(
        {
            "tool_name": "read",
            "arguments": {"path": r"C:\cow\skills\docx\SKILL.md"},
        }
    ) is True
    assert _is_user_visible_tool_notice(
        {
            "tool_name": "read",
            "arguments": {
                "path": r"C:\cow\skills\@user_087fbff2\govwriting\SKILL.md"
            },
        }
    ) is True
    assert _is_user_visible_tool_notice({"tool_name": "browser"}) is True
    assert _is_user_visible_tool_notice({"tool_name": "web_fetch"}) is True
    assert _is_user_visible_tool_notice(
        {"tool_name": "web_search"}, silent_tools=["web_search"]
    ) is False


def test_preflight_notice_predicts_attachment_tools_before_llm_turn():
    docx_event = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "file", r"C:\tmp\LDAP.docx"
    )
    image_reference_event = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "inspect",
        reference={"content_type": "image", "file_path": r"C:\tmp\quoted.png"},
    )
    unresolved_image_reference = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "inspect",
        reference={"content_type": "image"},
    )
    unresolved_file_reference = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "inspect",
        reference={"content_type": "file"},
    )
    pdf_reference = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "inspect",
        reference={"content_type": "file", "file_path": r"C:\tmp\quoted.pdf"},
    )
    share_reference = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "讲了什么",
        reference={"content_type": "share_card", "content": "分享标题"},
    )
    standalone_share = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "share_card", "分享标题"
    )
    text_event = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "hello"
    )

    assert _tool_notice_subject(_preflight_tool_notice_data(docx_event)) == (
        "skill",
        "docx",
    )
    assert _tool_notice_subject(
        _preflight_tool_notice_data(image_reference_event)
    ) == ("tool", "vision")
    assert _preflight_tool_notice_data(unresolved_image_reference) is None
    assert _preflight_tool_notice_data(unresolved_file_reference) is None
    assert _tool_notice_subject(
        _preflight_tool_notice_data(pdf_reference)
    ) == ("skill", "pdf-reader")
    assert _preflight_tool_notice_data(share_reference) is None
    assert _preflight_tool_notice_data(standalone_share) is None
    assert _preflight_tool_notice_data(text_event) is None


def _link_event(content="看看这个", **kwargs):
    return WechatDesktopEvent(
        "message", "synthetic-session", "Alice", "alice", "Alice", "text", content,
        **kwargs,
    )


def test_link_targets_include_current_text_and_direct_quote_preserving_query():
    first = "https://example.invalid/post?a=1&xsec_token=synthetic%2Fvalue%3D&mode=share"
    second = "https://example.invalid/file.pdf?signature=synthetic+value=="
    event = _link_event(
        f"看看{first}。另一个是 [{second}]({second})",
        reference={"content_type": "text", "content": f"引用链接：{first}"},
        history=[{"content": "https://example.invalid/unrelated-history"}],
    )

    assert _link_reading_urls(event) == [first, second]
    instruction = _link_reading_instruction(event)
    assert first in instruction and second in instruction
    assert "unrelated-history" not in instruction


@pytest.mark.parametrize("reference", [
    {"content_type": "text", "content": "链接：https://example.invalid/post"},
    {"content_type": "text", "text": "https://example.invalid/post"},
    {"content_type": "share_card", "content": "合成卡片", "url": "https://example.invalid/post"},
    {"content_type": "share_card", "content": "[第三方分享卡片] 合成卡片\n链接：https://example.invalid/post"},
])
def test_direct_reference_links_use_common_reader(reference):
    event = _link_event(reference=reference)

    assert _link_reading_urls(event) == ["https://example.invalid/post"]
    instruction = _link_reading_instruction(event)
    assert "skills/analyze-url/SKILL.md" in instruction
    assert "navigate" in instruction and "snapshot" in instruction and "get_text" in instruction


@pytest.mark.parametrize("content,expected", [
    ("[文章](https://example.invalid/post?a=1&a=2&token=synthetic%2Bbase64==)",
     "https://example.invalid/post?a=1&a=2&token=synthetic%2Bbase64=="),
    ("（https://example.invalid/post?token=synthetic=）。",
     "https://example.invalid/post?token=synthetic="),
    ("看看 https://example.invalid/article_(part)?key=synthetic==。",
     "https://example.invalid/article_(part)?key=synthetic=="),
])
def test_link_query_is_kept_while_prose_and_markdown_wrappers_are_removed(content, expected):
    assert _link_reading_urls(_link_event(content)) == [expected]


@pytest.mark.parametrize("body", [
    {"browser_content": "合成页面正文", "fetch_status": "direct_browser"},
    {"fetched_content": "合成页面正文", "fetch_status": "success"},
])
def test_read_reference_body_excludes_same_url_but_keeps_other_current_link(body):
    quote_url = "https://example.invalid/read?token=synthetic"
    other_url = "https://example.invalid/new"
    event = _link_event(
        f"结合 {quote_url} 和 {other_url} 看看",
        reference={"content_type": "share_card", "url": quote_url, **body},
    )

    assert _link_reading_urls(event) == [other_url]
    _heading, lines = _render_event_context_lines(event)
    assert "合成页面正文" in "\n".join(lines)


@pytest.mark.parametrize("fetch_status", ["error", "pending_tool", "link_available"])
def test_failed_or_pending_share_body_still_requires_tools(fetch_status):
    event = _link_event(reference={
        "content_type": "share_card", "content": "合成卡片",
        "url": "https://example.invalid/post", "fetch_status": fetch_status,
        "fetched_content": "synthetic request error; not page body",
    })

    assert _link_reading_urls(event) == ["https://example.invalid/post"]
    _heading, lines = _render_event_context_lines(event)
    rendered = "\n".join(lines)
    assert "synthetic request error" not in rendered
    assert "页面正文尚未读取" in rendered
    instruction = _link_reading_instruction(event)
    assert "技能不可用时，直接使用现有 browser 或 web_fetch" in instruction


def test_source_invalid_reference_does_not_schedule_any_links():
    event = _link_event("https://example.invalid/current", reference={
        "content_type": "share_card", "url": "https://example.invalid/quote",
        "fetch_status": "source_invalid", "browser_content": "旧的正文",
    })

    assert _link_reading_instruction(event) == ""
    _heading, lines = _render_event_context_lines(event)
    assert "来源校验失败" in "\n".join(lines)
    assert "旧的正文" not in "\n".join(lines)


@pytest.mark.parametrize("content,reference", [
    ("普通提问", {"content_type": "text", "content": "普通引用"}),
    ("普通提问", {"content_type": "share_card", "content": "标题 https://example.invalid/title"}),
    (r"C:\uia\https://example.invalid/local", None),
    ('<msg><appmsg><url>https://example.invalid/raw</url></appmsg></msg>', None),
    ("普通提问", {"content_type": "text", "content": '<msg><url>https://example.invalid/raw</url></msg>'}),
])
def test_non_links_xml_and_card_titles_do_not_schedule_browser(content, reference):
    event = _link_event(content, reference=reference, history=[
        {"content": "https://example.invalid/history"},
    ])

    assert _link_reading_instruction(event) == ""


def test_local_attachment_path_is_not_a_text_link_target():
    event = _link_event("https://example.invalid/not-user-text")
    event.content_type = "file"

    assert _link_reading_urls(event) == []


def test_existing_page_result_still_requires_article_content_check():
    event = _link_event(reference={
        "content_type": "share_card", "url": "https://example.invalid/post",
        "browser_content": "synthetic page text", "browser_status": "success",
    })

    assert _link_reading_urls(event) == []
    instruction = _link_reading_instruction(event)
    assert "[网页正文核验]" in instruction
    assert "登录、验证码、访问错误" in instruction
    assert "继续读取一次" in instruction
    assert "实际正文可以直接复用" in instruction


def test_explicit_failed_browser_body_is_not_reused_and_url_is_scheduled():
    event = _link_event(reference={
        "content_type": "share_card", "url": "https://example.invalid/post",
        "browser_content": "synthetic error text", "browser_status": "error",
        "fetch_status": "link_available",
    })

    assert _link_reading_urls(event) == ["https://example.invalid/post"]
    assert "synthetic error text" not in "\n".join(_render_event_context_lines(event)[1])
    assert "[链接读取要求]" in _link_reading_instruction(event)
