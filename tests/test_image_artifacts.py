"""Tests for auto-detection of generated local images from tool results."""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent.tools.utils.image_artifacts import (
    build_file_to_send,
    extract_image_artifacts_from_tool_result,
    extract_local_image_paths,
)


def test_extract_seedream_json(tmp_path: Path):
    img = tmp_path / "tmp" / "seedream-images" / "2026-08-05" / "seedream_1.jpg"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"\xff\xd8\xff" + b"\x00" * 32)

    payload = {
        "success": True,
        "images": [
            {
                "url": "https://example.com/a.jpg",
                "local_path": str(img),
                "index": 1,
                "download_success": True,
            }
        ],
        "metadata": {"image_count": 1},
    }
    text = json.dumps(payload, ensure_ascii=False)
    paths = extract_local_image_paths(text)
    assert str(img) in paths or any(Path(p) == img for p in paths)

    arts = extract_image_artifacts_from_tool_result(
        "bash",
        {"output": text, "exit_code": 0},
        status="success",
    )
    assert len(arts) == 1
    assert arts[0]["type"] == "file_to_send"
    assert arts[0]["file_type"] == "image"
    assert os.path.isfile(arts[0]["path"])
    assert arts[0].get("auto_detected") is True


def test_skip_failed_download(tmp_path: Path):
    img = tmp_path / "tmp" / "seedream-images" / "x.jpg"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"\xff\xd8\xff" + b"\x00" * 8)
    payload = {
        "success": True,
        "images": [
            {
                "url": "https://example.com/a.jpg",
                "local_path": str(img),
                "download_success": False,
            }
        ],
    }
    arts = extract_image_artifacts_from_tool_result(
        "bash",
        {"output": json.dumps(payload), "exit_code": 0},
        status="success",
    )
    assert arts == []


def test_ignore_non_generated_paths(tmp_path: Path):
    img = tmp_path / "random_photo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    arts = extract_image_artifacts_from_tool_result(
        "bash",
        {"output": f"saved to {img}", "exit_code": 0},
        status="success",
    )
    assert arts == []


def test_build_file_to_send_missing():
    assert build_file_to_send("/no/such/seedream_x.jpg") is None


def test_send_tool_not_auto_duplicated():
    arts = extract_image_artifacts_from_tool_result(
        "send",
        {"type": "file_to_send", "file_type": "image", "path": "/tmp/x.jpg"},
        status="success",
    )
    assert arts == []
