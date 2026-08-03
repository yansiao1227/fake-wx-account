"""Shared retry policy for model API calls."""

from __future__ import annotations

import random
from dataclasses import dataclass

from config import conf


DEFAULT_MODEL_API_FAILURE_MESSAGES = (
    "刚才脑内小齿轮打了个滑，我这次没能答上来 😵‍💫 请再戳我一下，我重新来过。",
    "答案在路上迷了个路，这一轮先投降 🧭 你可以再发一次，我会重新出发。",
    "我刚和服务器猜拳输了，回复没拿回来 🤖 再问我一次吧。",
)


class ModelApiRetriesExhausted(RuntimeError):
    """Raised after a model API failure reaches its retry terminal state."""

    def __init__(self, original_error, retries: int):
        self.original_error = str(original_error or "model API request failed")
        self.retries = max(0, int(retries))
        super().__init__(self.original_error)


@dataclass(frozen=True)
class ModelApiRetryPolicy:
    """Bounded exponential-backoff settings.

    ``max_retries`` counts retries after the initial request, so the default
    value of three permits at most four total API attempts.
    """

    max_retries: int = 3
    base_seconds: float = 2.0
    max_seconds: float = 10.0
    jitter_seconds: float = 0.5


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _bounded_float(value, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(float(value), maximum))
    except (TypeError, ValueError):
        return default


def get_model_api_retry_policy() -> ModelApiRetryPolicy:
    """Load retry settings dynamically so configuration reloads take effect."""
    config = conf()
    base_seconds = _bounded_float(
        config.get("model_api_retry_base_seconds", 2.0),
        2.0,
        0.0,
        60.0,
    )
    max_seconds = _bounded_float(
        config.get("model_api_retry_max_seconds", 10.0),
        10.0,
        base_seconds,
        300.0,
    )
    return ModelApiRetryPolicy(
        max_retries=_bounded_int(
            config.get("model_api_max_retries", 3),
            3,
            0,
            10,
        ),
        base_seconds=base_seconds,
        max_seconds=max_seconds,
        jitter_seconds=_bounded_float(
            config.get("model_api_retry_jitter_seconds", 0.5),
            0.5,
            0.0,
            10.0,
        ),
    )


def is_retryable_model_api_error(error) -> bool:
    """Return whether another identical API attempt can reasonably recover."""
    text = str(error or "").casefold()
    if not text:
        return False

    # Credentials, billing and policy failures require user/config changes.
    permanent_markers = (
        "invalid api key",
        "incorrect api key",
        "authentication failed",
        "authentication_error",
        "unauthorized",
        "permission denied",
        "permission_denied",
        "insufficient quota",
        "insufficient_quota",
        "billing",
        "content policy",
        "content_policy",
        "model not found",
        "does not exist",
    )
    if any(marker in text for marker in permanent_markers):
        return False

    transient_markers = (
        "timeout",
        "timed out",
        "connection",
        "network",
        "rate limit",
        "overloaded",
        "unavailable",
        "server busy",
        "temporarily",
        "try again",
        "retry",
        "protocol_mismatch",
        "invalid_request",
        "invalid request",
        "请求不合法",
        "status: 408",
        "status: 409",
        "status: 425",
        "status: 429",
        "status: 500",
        "status: 502",
        "status: 503",
        "status: 504",
        "status: 512",
        "status: 520",
        "status: 522",
        "status: 524",
    )
    return any(marker in text for marker in transient_markers)


def model_api_retry_delay(
    retry_count: int,
    error,
    policy: ModelApiRetryPolicy | None = None,
) -> float:
    """Calculate bounded exponential backoff plus a small random jitter."""
    policy = policy or get_model_api_retry_policy()
    delay = min(
        policy.max_seconds,
        policy.base_seconds * (2 ** max(0, int(retry_count))),
    )
    text = str(error or "").casefold()
    if "429" in text or "rate limit" in text:
        delay = min(policy.max_seconds, max(5.0, delay))
    if policy.jitter_seconds:
        delay = min(
            policy.max_seconds,
            delay + random.uniform(0.0, policy.jitter_seconds),
        )
    return delay


def model_api_failure_message() -> str:
    """Return a user-safe final message without leaking provider diagnostics."""
    configured = conf().get("model_api_failure_messages", [])
    candidates = [
        str(item).strip()
        for item in configured or []
        if str(item).strip()
    ]
    return random.choice(candidates or list(DEFAULT_MODEL_API_FAILURE_MESSAGES))
