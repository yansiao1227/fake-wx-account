"""Use RapidAI/RapidOCR as a conservative group-sender fallback.

UIA remains the source of message identity, body text and bounds, but not group
sender names. OCR is the primary sender-name source: it must first match
recognized body text to the UIA message and then find one unique,
high-confidence label immediately above it.
"""

from __future__ import annotations

import hashlib
import re
import threading
import unicodedata
from dataclasses import dataclass, replace
from difflib import SequenceMatcher

from channel.wechat_desktop.models import (
    DEFAULT_SELF_SENDER_NAME,
    UNKNOWN_SENDER_NAME,
    UiaChatMessage,
)
from common.log import logger


@dataclass(frozen=True)
class OcrTextLine:
    text: str
    score: float
    bounds: tuple[int, int, int, int]

    @property
    def width(self) -> int:
        return max(0, self.bounds[2] - self.bounds[0])

    @property
    def height(self) -> int:
        return max(0, self.bounds[3] - self.bounds[1])


class RapidOcrGroupSenderResolver:
    """Lazy RapidOCR engine plus geometry-aware sender matching."""

    _IGNORED_NAMES = {
        "头像",
        "群头像",
        "已读",
        "未读",
        "发送失败",
        "重发",
        "撤回",
        "更多",
    }

    def __init__(self, config: dict | None = None):
        self.config = dict(config or {})
        self._lock = threading.RLock()
        self._engine = None
        self._engine_unavailable = False
        self._cached_digest = ""
        self._cached_lines: list[OcrTextLine] = []

    @staticmethod
    def _sender_is_unknown(value: str) -> bool:
        return not str(value or "").strip() or value == UNKNOWN_SENDER_NAME

    def _self_sender_name(self) -> str:
        return (
            str(self.config.get("self_display_name") or "").strip()
            or DEFAULT_SELF_SENDER_NAME
        )

    @staticmethod
    def _normalized(value: str) -> str:
        value = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return re.sub(r"[^\w\u4e00-\u9fff]+", "", value)

    @classmethod
    def _text_similarity(cls, left: str, right: str) -> float:
        left_value = cls._normalized(left)
        right_value = cls._normalized(right)
        if not left_value or not right_value:
            return 0.0
        if left_value == right_value:
            return 1.0
        if left_value in right_value or right_value in left_value:
            shorter = min(len(left_value), len(right_value))
            longer = max(len(left_value), len(right_value))
            return max(0.82, shorter / max(1, longer))
        return SequenceMatcher(None, left_value, right_value).ratio()

    @staticmethod
    def _line_center(line: OcrTextLine) -> tuple[float, float]:
        left, top, right, bottom = line.bounds
        return (left + right) / 2.0, (top + bottom) / 2.0

    @classmethod
    def _valid_name(cls, line: OcrTextLine, min_score: float) -> bool:
        value = str(line.text or "").strip()
        if (
            line.score < min_score
            or not value
            or value in cls._IGNORED_NAMES
            or len(value) > 40
            or "\n" in value
            or value.startswith("@")
        ):
            return False
        if re.fullmatch(r"\d{1,2}:\d{2}", value):
            return False
        if re.fullmatch(r"(?:今天|昨天|星期.|周.)?\s*\d{1,2}:\d{2}", value):
            return False
        return True

    def _match_body(
        self,
        message: UiaChatMessage,
        lines: list[OcrTextLine],
        used_line_ids: set[int] | None = None,
    ) -> OcrTextLine | None:
        if (
            message.message_type != "text"
            or not message.bounds
            or not str(message.content or "").strip()
        ):
            return None

        left, top, right, bottom = message.bounds
        body_min_score = max(
            0.0,
            min(
                float(self.config.get("uia_group_sender_ocr_body_min_score", 0.6)),
                1.0,
            ),
        )
        similarity_min = max(
            0.0,
            min(
                float(
                    self.config.get(
                        "uia_group_sender_ocr_text_similarity", 0.78
                    )
                ),
                1.0,
            ),
        )
        geometric_candidates = []
        text_candidates = []
        for line in lines:
            if used_line_ids is not None and id(line) in used_line_ids:
                continue
            if line.score < body_min_score:
                continue
            similarity = self._text_similarity(message.content, line.text)
            if similarity < similarity_min:
                continue
            center_x, center_y = self._line_center(line)
            candidate = (similarity * line.score, line)
            text_candidates.append(candidate)
            if left <= center_x <= right and top <= center_y <= bottom:
                geometric_candidates.append(candidate)
        # UIA and screenshots may use different coordinate scales under Windows
        # DPI scaling. Prefer the exact geometry match, but fall back to the OCR
        # body text instead of discarding an otherwise unambiguous recognition.
        candidates = geometric_candidates or text_candidates
        if not candidates:
            return None
        candidates.sort(key=lambda value: value[0], reverse=True)
        if (
            not geometric_candidates
            and len(candidates) > 1
            and candidates[0][1].text != candidates[1][1].text
            and candidates[0][0] - candidates[1][0] < 0.08
        ):
            return None
        return candidates[0][1]

    @staticmethod
    def _direction_from_body(
        body: OcrTextLine,
        pane_bounds: tuple[int, int, int, int] | None,
    ) -> str:
        if not pane_bounds:
            return "unknown"
        left, _, right, _ = pane_bounds
        width = max(1, right - left)
        center = (left + right) / 2.0
        threshold = max(12.0, width * 0.06)
        body_center, _ = RapidOcrGroupSenderResolver._line_center(body)
        if body_center < center - threshold:
            return "incoming"
        if body_center > center + threshold:
            return "outgoing"
        return "unknown"

    def _sender_above_body(
        self,
        body: OcrTextLine,
        lines: list[OcrTextLine],
    ) -> str:

        name_min_score = max(
            0.0,
            min(
                float(self.config.get("uia_group_sender_ocr_name_min_score", 0.75)),
                1.0,
            ),
        )
        max_gap = max(
            12,
            min(
                int(self.config.get("uia_group_sender_ocr_name_gap_px", 48)),
                120,
            ),
        )
        horizontal_tolerance = max(24, min(120, body.width // 2))
        name_candidates = []
        for line in lines:
            if line is body or not self._valid_name(line, name_min_score):
                continue
            gap = body.bounds[1] - line.bounds[3]
            if gap < -3 or gap > max_gap:
                continue
            if abs(line.bounds[0] - body.bounds[0]) > horizontal_tolerance:
                continue
            if line.height > max(28, int(body.height * 1.1)):
                continue
            # Prefer the closest label, then higher OCR confidence.
            rank = (max_gap - max(0, gap)) / max_gap + line.score
            name_candidates.append((rank, line))
        if not name_candidates:
            return ""

        name_candidates.sort(key=lambda value: value[0], reverse=True)
        best_rank, best = name_candidates[0]
        if (
            len(name_candidates) > 1
            and name_candidates[1][1].text != best.text
            and best_rank - name_candidates[1][0] < 0.12
        ):
            return ""
        return best.text.strip()

    def _match_sender(
        self,
        message: UiaChatMessage,
        lines: list[OcrTextLine],
    ) -> str:
        """Compatibility helper used by focused sender-matching tests."""

        if (
            not self._sender_is_unknown(message.sender_name)
            or message.direction == "outgoing"
        ):
            return ""
        body = self._match_body(message, lines)
        return self._sender_above_body(body, lines) if body is not None else ""

    def assign_senders(
        self,
        messages: list[UiaChatMessage],
        lines: list[OcrTextLine],
        pane_bounds: tuple[int, int, int, int] | None = None,
        resolve_sender_names: bool = True,
    ) -> list[UiaChatMessage]:
        """Fill message directions and group sender names from one OCR layout."""

        resolved = []
        used_line_ids: set[int] = set()
        for message in messages:
            body = self._match_body(message, lines, used_line_ids)
            if body is None:
                sender = message.sender_name
                if message.direction == "outgoing":
                    sender = self._self_sender_name()
                elif self._sender_is_unknown(sender):
                    sender = UNKNOWN_SENDER_NAME
                resolved.append(
                    replace(message, sender_name=sender)
                    if sender != message.sender_name
                    else message
                )
                continue
            used_line_ids.add(id(body))
            direction = message.direction
            if direction == "unknown":
                direction = self._direction_from_body(body, pane_bounds)
            sender = message.sender_name
            if direction == "outgoing":
                sender = self._self_sender_name()
            elif resolve_sender_names and self._sender_is_unknown(sender):
                sender = self._sender_above_body(body, lines)
            if self._sender_is_unknown(sender):
                sender = UNKNOWN_SENDER_NAME
            if sender != message.sender_name or direction != message.direction:
                message = replace(
                    message,
                    sender_name=sender,
                    direction=direction,
                )
            resolved.append(message)
        return resolved

    def _get_engine(self):
        with self._lock:
            if self._engine is not None:
                return self._engine
            if self._engine_unavailable:
                return None
            try:
                from rapidocr import RapidOCR

                self._engine = RapidOCR()
            except Exception as exc:
                self._engine_unavailable = True
                logger.warning(
                    "[WechatDesktop] RapidOCR unavailable; group sender OCR disabled: %s",
                    exc,
                )
                return None
            return self._engine

    @staticmethod
    def _output_lines(result, offset: tuple[int, int]) -> list[OcrTextLine]:
        boxes = getattr(result, "boxes", None)
        texts = getattr(result, "txts", None)
        scores = getattr(result, "scores", None)
        if boxes is None or texts is None or scores is None:
            return []
        offset_x, offset_y = offset
        lines = []
        for box, text, score in zip(boxes, texts, scores):
            points = list(box)
            if not points:
                continue
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
            lines.append(
                OcrTextLine(
                    text=str(text or "").strip(),
                    score=float(score),
                    bounds=(
                        int(min(xs)) + offset_x,
                        int(min(ys)) + offset_y,
                        int(max(xs)) + offset_x,
                        int(max(ys)) + offset_y,
                    ),
                )
            )
        return lines

    def enrich(
        self,
        messages: list[UiaChatMessage],
        list_bounds: tuple[int, int, int, int] | None,
        resolve_sender_names: bool = True,
    ) -> list[UiaChatMessage]:
        """Capture the visible chat pane once and OCR only missing group senders."""
        if (
            not bool(self.config.get("uia_group_sender_ocr_enabled", True))
            or not list_bounds
            or not any(
                message.message_type == "text"
                and (
                    message.direction == "unknown"
                    or self._sender_is_unknown(message.sender_name)
                )
                for message in messages
            )
        ):
            return messages
        engine = self._get_engine()
        if engine is None:
            return messages
        try:
            import numpy as np
            from PIL import ImageGrab

            image = ImageGrab.grab(bbox=tuple(list_bounds), all_screens=True)
            digest = hashlib.sha256(image.tobytes()).hexdigest()
            with self._lock:
                if digest == self._cached_digest:
                    lines = list(self._cached_lines)
                else:
                    output = engine(np.asarray(image))
                    lines = self._output_lines(
                        output,
                        (int(list_bounds[0]), int(list_bounds[1])),
                    )
                    self._cached_digest = digest
                    self._cached_lines = list(lines)
            capture_bounds = (
                int(list_bounds[0]),
                int(list_bounds[1]),
                int(list_bounds[0]) + int(image.width),
                int(list_bounds[1]) + int(image.height),
            )
            enriched = self.assign_senders(
                messages,
                lines,
                capture_bounds,
                resolve_sender_names=resolve_sender_names,
            )
            sender_matched = sum(
                self._sender_is_unknown(before.sender_name)
                and not self._sender_is_unknown(after.sender_name)
                for before, after in zip(messages, enriched)
            )
            direction_matched = sum(
                before.direction == "unknown"
                and after.direction in {"incoming", "outgoing"}
                for before, after in zip(messages, enriched)
            )
            if sender_matched or direction_matched:
                logger.info(
                    "[WechatDesktop] RapidOCR resolved %s direction(s) and "
                    "%s sender name(s)",
                    direction_matched,
                    sender_matched,
                )
            return enriched
        except Exception as exc:
            logger.warning(
                "[WechatDesktop] RapidOCR group sender recognition failed: %s",
                exc,
            )
            return messages
