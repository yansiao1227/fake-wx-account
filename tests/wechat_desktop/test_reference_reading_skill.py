"""引用阅读技能可发现、跨工作区可读且只引导有专门读取方式的资料。"""

from pathlib import Path

import pytest

from agent.skills.manager import SkillManager
from agent.skills.paths import get_project_skills_dir
from agent.tools.read.read import Read
from channel.wechat_desktop.models import WechatDesktopEvent
from agent.tools.pdf_ocr.pdf_ocr import PdfOcr
from channel.wechat_desktop.pipeline.prompts import _reference_reading_instruction, _reply_requirements


def _event(reference):
    return WechatDesktopEvent(
        kind="message", conversation_id="synthetic", conversation_name="测试会话",
        sender_id="synthetic-sender", sender_name="测试发送者", content_type="text",
        content="请读取被引用的资料", reference=reference,
    )


def test_runtime_discovers_enabled_reference_skill_and_absolute_location_is_readable(tmp_path):
    manager = SkillManager(
        builtin_dir=get_project_skills_dir(),
        custom_dir=str(tmp_path / "registry"),
    )
    entry = manager.get_skill("wechat-reference-reading")
    assert entry is not None
    assert manager.is_skill_enabled(entry.skill.name)
    assert entry in manager.filter_skills(skill_filter=[entry.skill.name])
    assert Path(entry.skill.file_path).is_absolute()
    prompt = manager.build_skills_prompt(skill_filter=[entry.skill.name])
    assert f"<location>{entry.skill.file_path}</location>" in prompt

    result = Read({"cwd": str(tmp_path / "unrelated-workspace")}).execute(
        {"path": entry.skill.file_path}
    )
    assert result.status == "success"
    assert result.result["content"] == entry.skill.content


@pytest.mark.parametrize("kind", ["share_card", "image", "file", "chat_record"])
def test_specific_reference_types_offer_runtime_skill(kind):
    event = _event({"content_type": kind, "content": "资料标识"})
    assert "wechat-reference-reading" in _reference_reading_instruction(event)
    assert _reference_reading_instruction(event) in _reply_requirements(event)


def test_pdf_reference_routes_to_available_ocr_interface():
    event = _event({"content_type": "file", "file_path": r"D:\synthetic\scan.PDF"})
    instruction = _reply_requirements(event)
    tool = PdfOcr()
    assert tool.name in instruction
    for name in ("path", "start_page", "max_pages"):
        assert name in tool.params["properties"]
        assert name in instruction
    assert tool.params["properties"]["start_page"]["default"] == 1
    assert tool.params["properties"]["max_pages"]["default"] == 8


def test_non_pdf_reference_does_not_require_pdf_ocr():
    event = _event({"content_type": "file", "file_path": r"D:\synthetic\document.docx"})
    assert "pdf_ocr" not in _reference_reading_instruction(event)


def test_video_share_reference_uses_same_reading_route_without_losing_scope():
    event = _event({
        "content_type": "share_card", "native_app_type": "4", "content": "测试视频标题",
        "description": "测试简介", "url": "https://example.invalid/video",
    })
    instruction = _reply_requirements(event)
    assert "wechat-reference-reading" in instruction
    assert "视频" in instruction
    assert "video_frames" in instruction
    assert "max_frames=6" in instruction
    assert "音轨未转写" in instruction


@pytest.mark.parametrize("reference", [None, {"content_type": "text", "content": "原文"}])
def test_ordinary_text_does_not_route_to_reference_skill(reference):
    event = _event(reference)
    assert _reference_reading_instruction(event) == ""
