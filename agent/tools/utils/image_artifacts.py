"""Detect local image artifacts produced by tools (e.g. Seedream generate.js).

Used by the agent stream loop so generated images are queued for delivery
without requiring the model to remember an extra ``send`` tool call.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# Paths that look like intentional image outputs (Seedream / image-generation).
_OUTPUT_HINTS = (
    "seedream-images",
    "seedream_images",
    f"images{os.sep}",
    f"{os.sep}images{os.sep}",
    f"tmp{os.sep}",
)

# local_path fields in JSON / prose
_LOCAL_PATH_RE = re.compile(
    r'(?P<key>"?local_path"?\s*[:=]\s*)?"(?P<path>(?:[A-Za-z]:)?[^"\n]+\.(?:jpg|jpeg|png|gif|webp|bmp))"',
    re.IGNORECASE,
)
_BARE_PATH_RE = re.compile(
    r'(?P<path>(?:[A-Za-z]:[\\/]|/|\\\\)[^\s"\'<>]+\.(?:jpg|jpeg|png|gif|webp|bmp))',
    re.IGNORECASE,
)


def _is_image_path(path: str) -> bool:
    if not path or not isinstance(path, str):
        return False
    p = path.strip().strip('"').strip("'")
    if p.lower().startswith(("http://", "https://", "data:")):
        return False
    ext = Path(p).suffix.lower()
    return ext in _IMAGE_EXTS


def _looks_like_generated_output(path: str) -> bool:
    """Prefer generated/tmp outputs over arbitrary scanned images on disk."""
    lower = path.replace("/", os.sep).replace("\\", os.sep).lower()
    if "seedream" in lower:
        return True
    for hint in _OUTPUT_HINTS:
        if hint.lower() in lower:
            return True
    # Accept any existing image path under a tmp directory segment.
    parts = lower.split(os.sep)
    return "tmp" in parts


def _mime_for(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(ext, "image/jpeg")


def build_file_to_send(path: str, message: str = "") -> Optional[Dict[str, Any]]:
    """Build a file_to_send payload if ``path`` is a readable local image."""
    if not _is_image_path(path):
        return None
    absolute = os.path.abspath(os.path.expanduser(path.strip().strip('"').strip("'")))
    if not os.path.isfile(absolute):
        return None
    if not os.access(absolute, os.R_OK):
        return None
    name = os.path.basename(absolute)
    try:
        size = os.path.getsize(absolute)
    except OSError:
        return None
    return {
        "type": "file_to_send",
        "file_type": "image",
        "path": absolute,
        "file_name": name,
        "mime_type": _mime_for(absolute),
        "size": size,
        "size_formatted": _format_size(size),
        "message": message or f"发送图片 {name}",
        "auto_detected": True,
    }


def _format_size(num: int) -> str:
    n = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(n)}{unit}"
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{num}B"


def _walk_json_for_paths(obj: Any, out: List[str]) -> None:
    if isinstance(obj, dict):
        for key, val in obj.items():
            key_l = str(key).lower()
            if key_l in (
                "local_path",
                "path",
                "file_path",
                "filepath",
                "image_path",
                "url",
            ) and isinstance(val, str) and _is_image_path(val):
                # Remote URLs are not local artifacts.
                if not val.lower().startswith(("http://", "https://")):
                    out.append(val)
            else:
                _walk_json_for_paths(val, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_json_for_paths(item, out)


def _try_parse_json_blobs(text: str) -> List[Any]:
    """Extract one or more JSON values from tool stdout."""
    blobs: List[Any] = []
    if not text or not text.strip():
        return blobs
    stripped = text.strip()
    # Whole stdout is JSON
    try:
        blobs.append(json.loads(stripped))
        return blobs
    except Exception:
        pass
    # Find balanced {...} blocks that look like result objects
    starts = [m.start() for m in re.finditer(r"\{", stripped)]
    for start in starts[-8:]:  # only scan last few candidates
        chunk = stripped[start:]
        try:
            obj, _ = json.JSONDecoder().raw_decode(chunk)
            blobs.append(obj)
        except Exception:
            continue
    return blobs


def _paths_from_seedream_result(obj: Any) -> List[str]:
    """Prefer Seedream skill success shape: {success, images:[{local_path}]}."""
    if not isinstance(obj, dict):
        return []
    images = obj.get("images")
    if not isinstance(images, list):
        # image-generation skill: {images:[{url: local path}]}
        return []
    paths: List[str] = []
    for item in images:
        if not isinstance(item, dict):
            continue
        # Seedream: local_path; legacy image-generation: url as local path
        for key in ("local_path", "path", "url"):
            val = item.get(key)
            if isinstance(val, str) and _is_image_path(val):
                if item.get("download_success") is False:
                    continue
                paths.append(val)
                break
    # Only treat as intentional output when success flag is true or paths found
    if paths and (obj.get("success") is True or obj.get("success") is None):
        return paths
    return paths


def extract_local_image_paths(text: str) -> List[str]:
    """Pull candidate local image paths from free-form tool output text."""
    if not text:
        return []
    found: List[str] = []
    seen: Set[str] = set()

    def add(p: str) -> None:
        p = p.strip().rstrip(".,;)]}")
        if not _is_image_path(p):
            return
        # Prefer generated outputs when path does not exist yet check is later
        key = os.path.normcase(os.path.abspath(os.path.expanduser(p))) if os.path.isabs(p) else p
        if key in seen:
            return
        seen.add(key)
        found.append(p)

    seedream_shaped = False
    for blob in _try_parse_json_blobs(text):
        # Seedream / image-generation shaped results: only trust images[]
        # entries (respect download_success=false) — do not re-scan free-form
        # path regexes over the same JSON (they would reintroduce failed paths).
        if isinstance(blob, dict) and isinstance(blob.get("images"), list):
            seedream_shaped = True
            for p in _paths_from_seedream_result(blob):
                add(p)
            continue
        nested: List[str] = []
        _walk_json_for_paths(blob, nested)
        for p in nested:
            if _looks_like_generated_output(p) or _is_image_path(p):
                add(p)

    if not seedream_shaped:
        for match in _LOCAL_PATH_RE.finditer(text):
            add(match.group("path"))
        for match in _BARE_PATH_RE.finditer(text):
            p = match.group("path")
            if _looks_like_generated_output(p):
                add(p)

    return found


def extract_image_artifacts_from_tool_result(
    tool_name: str,
    result: Any,
    *,
    status: str = "success",
) -> List[Dict[str, Any]]:
    """Return file_to_send dicts for local images produced by a tool call.

    Currently focuses on ``bash`` / image-generation skill outputs. Explicit
    ``send`` tool results are already ``file_to_send`` and are left alone.
    """
    if status != "success":
        return []
    name = (tool_name or "").lower()

    # send tool already emits file_to_send
    if name == "send":
        return []

    candidates: List[str] = []

    if isinstance(result, dict):
        if result.get("type") == "file_to_send" and result.get("file_type") == "image":
            return []  # already handled by caller
        # bash shape
        output = result.get("output")
        if isinstance(output, str):
            candidates.extend(extract_local_image_paths(output))
        # direct path fields
        for key in ("local_path", "path", "image_path"):
            val = result.get(key)
            if isinstance(val, str):
                candidates.append(val)
        if "images" in result:
            candidates.extend(_paths_from_seedream_result(result))
    elif isinstance(result, str):
        candidates.extend(extract_local_image_paths(result))

    # Only auto-send from tools that commonly produce image files
    allow_any_path = name in ("bash", "terminal") or "image" in name or "seedream" in name
    artifacts: List[Dict[str, Any]] = []
    seen_abs: Set[str] = set()
    for path in candidates:
        if not allow_any_path and not _looks_like_generated_output(path):
            continue
        # Require generated-looking path for bash to avoid shipping random repo pngs
        if name in ("bash", "terminal") and not _looks_like_generated_output(path):
            continue
        payload = build_file_to_send(path)
        if not payload:
            continue
        abs_path = payload["path"]
        if abs_path in seen_abs:
            continue
        seen_abs.add(abs_path)
        artifacts.append(payload)
    return artifacts
