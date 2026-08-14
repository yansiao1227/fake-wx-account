"""Pure extraction adapter for the installed knowledge-acquisition skill."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import requests

from .subtitles import enrich_payload_with_subtitles


class KnowledgeAcquisitionExtractor:
    """Run the skill extractor, then fill in video captions when the skill skips them."""

    def __init__(self, config: dict[str, Any]):
        self._config = config
        self._repo_root = _find_repo_root(Path(__file__).resolve())
        self._http = requests.Session()
        self._http.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com",
            }
        )

    def _skill_dir(self) -> Path:
        configured = str(
            self._config.get("knowledge_acquisition_skill_path") or ""
        ).strip()
        candidates: list[Path] = []
        if configured:
            path = Path(configured).expanduser()
            candidates.append(path if path.is_absolute() else self._repo_root / path)
        candidates.append(self._repo_root / "skills" / "knowledge-acquisition")
        candidates.extend(
            sorted(
                (self._repo_root / "skills").glob(
                    "*/knowledge-acquisition"
                )
            )
        )
        for candidate in candidates:
            if (
                (candidate / "SKILL.md").is_file()
                and (candidate / "lib" / "dynamic-content-extractor.js").is_file()
            ):
                return candidate.resolve()
        raise FileNotFoundError("knowledge-acquisition skill is not installed")

    def extract(self, url: str) -> str:
        payload: dict[str, Any] = {}
        skill_error = ""
        try:
            payload = self._invoke_skill(url)
        except Exception as exc:
            skill_error = str(exc)

        if bool(self._config.get("knowledge_acquisition_fetch_subtitles", True)):
            payload = enrich_payload_with_subtitles(
                url,
                payload,
                enabled=True,
                timeout=self._subtitle_timeout_seconds(),
                session=self._http,
            )

        if not _payload_has_content(payload):
            raise RuntimeError(skill_error or "knowledge extraction failed")

        limit = max(
            1000,
            int(
                self._config.get(
                    "knowledge_acquisition_max_content_chars", 50000
                )
            ),
        )
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        return rendered[:limit]

    def _invoke_skill(self, url: str) -> dict[str, Any]:
        node_setting = str(
            self._config.get("knowledge_acquisition_node_executable") or "node"
        ).strip()
        node = shutil.which(node_setting) or node_setting
        bridge = Path(__file__).with_name("bridge.js")
        timeout = max(
            1.0,
            float(
                self._config.get(
                    "knowledge_acquisition_timeout_seconds", 45
                )
            ),
        )
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0
        )
        completed = subprocess.run(
            [node, str(bridge), str(self._skill_dir())],
            input=json.dumps({"url": url}, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            creationflags=creationflags,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(detail[-1000:] or "knowledge extraction failed")
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise RuntimeError("knowledge extraction returned invalid data")
        return payload

    def _subtitle_timeout_seconds(self) -> float:
        budget = float(
            self._config.get("knowledge_acquisition_timeout_seconds", 45)
        )
        return max(3.0, min(12.0, budget))


def _payload_has_content(payload: dict[str, Any]) -> bool:
    if not payload:
        return False
    for key in (
        "title",
        "description",
        "content",
        "text",
        "subtitle_text",
        "subtitles",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and value:
            return True
    return False


def _find_repo_root(start: Path) -> Path:
    for candidate in start.parents:
        if (candidate / "skills").is_dir() and (candidate / "channel").is_dir():
            return candidate
    return start.parents[3]
