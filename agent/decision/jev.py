"""通过 NewAPI 调用 TypeSafe；不记录请求、响应或凭据。"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import requests

from common.utils import expand_path
from config import conf
from models.custom_provider import parse_custom_bot_type, resolve_custom_credentials


DEFAULT_MODEL = "jev-1.13"
DEFAULT_TIMEOUT = 3.0
_ENDPOINT = "/typesafe/v1/systemone"
_PLACEHOLDERS = {"YOUR API KEY", "YOUR_API_KEY", "YOUR_NEWAPI_API_KEY"}


class JevError(RuntimeError):
    """只包含稳定错误类别与可选状态码，允许上层安全记录。"""

    def __init__(self, category: str, status_code: Optional[int] = None):
        # 类别不可来自服务端正文、异常消息或用户内容。
        allowed = {"configuration", "request", "timeout", "transport", "http", "response"}
        self.category = category if category in allowed else "request"
        self.status_code = status_code if isinstance(status_code, int) else None
        suffix = f" ({self.status_code})" if self.status_code is not None else ""
        super().__init__(f"Jev {self.category}{suffix}")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _key(value: Any) -> str:
    value = _text(value)
    return "" if value.upper() in _PLACEHOLDERS else value


def _endpoint(api_base: str) -> str:
    """保留网关前缀；移除 OpenAI 兼容 /v1，避免拼接重复。"""
    parsed = urlsplit(_text(api_base))
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise JevError("configuration")
    path = parsed.path.rstrip("/")
    if path.endswith(_ENDPOINT):
        pass
    elif path.endswith("/typesafe/v1"):
        path += "/systemone"
    elif path.endswith("/typesafe"):
        path += "/v1/systemone"
    else:
        if path.endswith("/v1"):
            path = path[:-3]
        path += _ENDPOINT
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _timeout(value: Any) -> float:
    if isinstance(value, bool):
        raise JevError("configuration")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise JevError("configuration") from None
    if not math.isfinite(number) or number <= 0:
        raise JevError("configuration")
    return number


def _load_env() -> None:
    """与 AgentInitializer 一致，先载入 canonical .env，再读环境。"""
    env_file = expand_path("~/.cow/.env")
    if os.path.isfile(env_file):
        from dotenv import load_dotenv

        load_dotenv(env_file, override=True)


def _provider_credentials(config: Mapping) -> Tuple[str, str]:
    """只有实际选中的 custom provider 可以作为共享凭据来源。"""
    bot_type = config.get("bot_type", "")
    is_custom, provider_id = parse_custom_bot_type(bot_type)
    if not is_custom:
        return "", ""
    # resolver 的遗留回退可能指向另一个地址；不存在的显式 id 不采用回退。
    if provider_id:
        providers = config.get("custom_providers", [])
        if not isinstance(providers, list) or not any(
            isinstance(item, Mapping) and item.get("id") == provider_id
            for item in providers
        ):
            return "", ""
    api_key, api_base, _ = resolve_custom_credentials()
    return _text(api_base), _key(api_key)


def _shared_credentials(config: Mapping) -> Tuple[str, str]:
    """base/key 成对选择，禁止从不同厂商分别凑地址与密钥。"""
    newapi_base = _text(os.environ.get("NEWAPI_API_BASE")) or _text(
        os.environ.get("NEWAPI_BASE_URL")
    )
    newapi_key = _key(os.environ.get("NEWAPI_API_KEY"))
    if newapi_base and newapi_key:
        return newapi_base, newapi_key

    custom_base = _text(os.environ.get("CUSTOM_API_BASE"))
    custom_key = _key(os.environ.get("CUSTOM_API_KEY"))
    selected_base, selected_key = _provider_credentials(config)
    # 传统单一 custom 环境变量允许覆盖同一配置；多提供商模式只接受完整环境对。
    if config.get("bot_type") == "custom":
        custom_base = custom_base or selected_base
        custom_key = custom_key or selected_key
    if custom_base and custom_key:
        return custom_base, custom_key
    return selected_base, selected_key


def _number(value: Any, lower: float, upper: float) -> bool:
    try:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and lower <= value <= upper
        )
    except (TypeError, ValueError, OverflowError):
        return False


def _probabilities(value: Any, expected: set) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == expected
        and all(_number(item, 0, 1) for item in value.values())
        and math.isclose(sum(value.values()), 1.0, abs_tol=0.01)
    )


def _validate_answers(body: Any, questions: Mapping) -> Dict[str, dict]:
    if not isinstance(body, Mapping) or not isinstance(body.get("answers"), Mapping):
        raise JevError("response")
    answers = body["answers"]
    if set(answers) != set(questions):
        raise JevError("response")
    clean = {}
    for question_id, question in questions.items():
        answer = answers[question_id]
        kind = question.get("type")
        if not isinstance(answer, Mapping) or answer.get("type") != kind:
            raise JevError("response")
        if kind == "noul":
            valid = _number(answer.get("noul"), 0, 1)
        elif kind == "choice":
            criteria = question.get("criteria")
            valid = (
                isinstance(criteria, Mapping)
                and isinstance(answer.get("choice"), str)
                and answer["choice"] in criteria
                and _number(answer.get("confidence"), 0, 1)
                and _probabilities(answer.get("probabilities"), set(criteria))
            )
        elif kind == "score":
            criteria = question.get("criteria")
            expected = {str(index) for index in range(len(criteria))} if isinstance(criteria, list) else set()
            valid = (
                bool(expected)
                and _number(answer.get("score"), 0, len(criteria) - 1)
                and _number(answer.get("confidence"), 0, 1)
                and _probabilities(answer.get("probabilities"), expected)
                and isinstance(answer.get("legend"), Mapping)
                and set(answer["legend"]) == expected
            )
        else:
            valid = False
        if not valid:
            raise JevError("response")
        # 只向协调器传递协议字段，不携带网关附加的正文/调试内容。
        fields = {
            "noul": ("type", "noul"),
            "choice": ("type", "choice", "probabilities", "confidence"),
            "score": ("type", "score", "probabilities", "confidence", "legend"),
        }[kind]
        clean[question_id] = {field: answer[field] for field in fields}
    return clean


class JevClient:
    """同步且无重试的客户端，由调用方放入并行任务。"""

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.api_base = _text(api_base)
        self.endpoint = _endpoint(self.api_base)
        self.api_key = _key(api_key)
        if not self.api_key:
            raise JevError("configuration")
        self.model = _text(model) or DEFAULT_MODEL
        self.timeout = _timeout(timeout)

    def __repr__(self) -> str:
        return "JevClient(credentials=<redacted>)"

    @classmethod
    def from_config(cls, settings: Optional[Mapping] = None) -> Optional["JevClient"]:
        """缺少凭据或配置错误时返回 None，由主循环决定是否启用旁路。"""
        try:
            _load_env()
            config = conf()
            configured = config.get("agent_decision") or {}
            options = dict(configured) if isinstance(configured, Mapping) else {}
            if settings is not None:
                if not isinstance(settings, Mapping):
                    return None
                options.update(settings)
            shared_base, shared_key = _shared_credentials(config)
            explicit_base = _text(os.environ.get("TYPESAFE_API_BASE")) or _text(options.get("api_base"))
            explicit_key = _key(os.environ.get("TYPESAFE_API_KEY")) or _key(options.get("api_key"))
            api_base = explicit_base or shared_base
            api_key = explicit_key
            if not api_key and shared_key:
                # 单独改为不同地址时，不把原提供商密钥送到另一个网关。
                if not explicit_base or (shared_base and _endpoint(explicit_base) == _endpoint(shared_base)):
                    api_key = shared_key
            if not api_base or not api_key:
                return None
            return cls(
                api_base,
                api_key,
                model=options.get("model", DEFAULT_MODEL),
                timeout=options.get("timeout_seconds", options.get("timeout", DEFAULT_TIMEOUT)),
            )
        except Exception:
            # 不传播 dotenv / resolver 原始异常，它们可能包含凭据。
            return None

    def evaluate(self, state: Any, questions: Mapping, timeout: Optional[float] = None) -> Dict[str, dict]:
        if not isinstance(questions, Mapping) or not questions or not all(
            isinstance(question_id, str)
            and isinstance(question, Mapping)
            and question.get("type") in ("choice", "score", "noul")
            for question_id, question in questions.items()
        ):
            raise JevError("request")
        request_timeout = self.timeout if timeout is None else _timeout(timeout)
        try:
            response = requests.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": self.model, "state": state, "questions": dict(questions)},
                timeout=request_timeout,
                allow_redirects=False,
            )
        except requests.Timeout:
            raise JevError("timeout") from None
        except requests.RequestException:
            raise JevError("transport") from None
        except (TypeError, ValueError):
            raise JevError("request") from None
        except Exception:
            raise JevError("transport") from None
        if not 200 <= response.status_code < 300:
            raise JevError("http", response.status_code)
        try:
            body = response.json()
        except Exception:
            raise JevError("response") from None
        try:
            return _validate_answers(body, questions)
        except JevError:
            raise
        except Exception:
            raise JevError("response") from None
