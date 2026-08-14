"""Pure extraction adapter for the installed knowledge-acquisition skill."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


class KnowledgeAcquisitionExtractor:
    """Run only the skill's platform extractor and return structured JSON."""

    def __init__(self, config: dict[str, Any]):
        self._config = config
        self._repo_root = Path(__file__).resolve().parents[2]

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
        node_setting = str(
            self._config.get("knowledge_acquisition_node_executable") or "node"
        ).strip()
        node = shutil.which(node_setting) or node_setting
        bridge = Path(__file__).with_name("knowledge_acquisition_bridge.js")
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
