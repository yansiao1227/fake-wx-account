"""Baidu Qianfan Intelligent Search (智能搜索生成) tool.

API: POST https://qianfan.baidubce.com/v2/ai_search/chat/completions
Docs: https://cloud.baidu.com/doc/qianfan-api/s/Hmbu8m06u

Unlike ``web_search`` (raw multi-source snippets), this tool asks Qianfan to
search the open web and return an LLM-summarized answer with citation markers
and reference links. No query rewrite is applied — the user query is sent as-is.

Like ``doubao_search``, successful results are returned as normal tool output
for cowagent to compose the user-facing reply (no direct-final-answer
short-circuit).

Routing policy
  1. Prefer this tool while the local daily free-tier quota remains and the
     daily exhaustion flag is clear.
  2. When the local quota is exhausted, or the remote API reports quota /
     billing / rate-limit exhaustion, mark the day exhausted and automatically
     fall back to ``web_search`` so the agent still gets usable search results.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from agent.tools.base_tool import BaseTool, ToolResult
from agent.tools.search_quota import (
    CAP_BAIDU_AI_SEARCH,
    SearchCapabilityState,
    get_search_capability_state,
    looks_like_quota_error,
)
from common.log import logger
from config import conf


DEFAULT_TIMEOUT = 60
DEFAULT_MODEL = "ernie-4.5-turbo-32k"
DEFAULT_DAILY_LIMIT = 100
DEFAULT_SEARCH_SOURCE = "baidu_search_v2"
DEFAULT_TOP_K = 10


def _tools_baidu_ai_search_conf() -> dict:
    tools_cfg = conf().get("tools") or {}
    if not isinstance(tools_cfg, dict):
        return {}
    block = tools_cfg.get("baidu_ai_search") or {}
    return block if isinstance(block, dict) else {}


def _get_qianfan_api_key() -> str:
    """Resolve Qianfan key: conf fallback after env / ~/.cow/.env load."""
    key = (conf().get("qianfan_api_key") or "").strip()
    if key:
        return key
    return os.environ.get("QIANFAN_API_KEY", "").strip()


def _api_base() -> str:
    return (conf().get("qianfan_api_base") or "https://qianfan.baidubce.com/v2").rstrip(
        "/"
    )


class BaiduAiSearch(BaseTool):
    """Intelligent web search with LLM summary (Baidu Qianfan AI Search)."""

    name: str = "baidu_ai_search"
    description: str = (
        "Real-time web Q&A via Baidu Qianfan Intelligent Search. Returns a "
        "summarized answer with citation markers plus source references for you "
        "to compose the user-facing reply (same agent-summary flow as "
        "doubao_search). Search cascade: doubao_search → baidu_ai_search → "
        "web_search. Prefer doubao_search first when available; use this when "
        "Doubao is unavailable or you need a model-summarized search draft. "
        "Do not rewrite the query before calling. When the daily intelligent-"
        "search free quota is exhausted, this tool automatically falls back to "
        "web_search. When using the answer, cite the reference URLs as "
        "clickable source links."
    )

    params: dict = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language question or search query",
            },
            "count": {
                "type": "integer",
                "description": (
                    "Max web references to retrieve for summarization "
                    f"(1-20, default: {DEFAULT_TOP_K})"
                ),
            },
            "freshness": {
                "type": "string",
                "description": (
                    "Time range filter. Options: "
                    "'noLimit' (default), 'oneDay', 'oneWeek', 'oneMonth', 'oneYear'"
                ),
            },
        },
        "required": ["query"],
    }

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._usage_store_path = self.config.get("usage_store_path")
        self._capability_state: Optional[SearchCapabilityState] = None

    def _get_capability_state(self) -> SearchCapabilityState:
        if self._capability_state is None:
            self._capability_state = get_search_capability_state(self._usage_store_path)
        return self._capability_state

    @staticmethod
    def is_available() -> bool:
        """Offer the tool when Qianfan key exists or web_search can still serve."""
        if _get_qianfan_api_key():
            return True
        try:
            from agent.tools.web_search.web_search import WebSearch

            return WebSearch.is_available()
        except Exception:
            return False

    def _daily_limit(self) -> Optional[int]:
        cfg = _tools_baidu_ai_search_conf()
        raw = cfg.get("daily_limit", DEFAULT_DAILY_LIMIT)
        if raw is None:
            return None
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return DEFAULT_DAILY_LIMIT

    def _fallback_enabled(self) -> bool:
        cfg = _tools_baidu_ai_search_conf()
        if "fallback_to_web_search" not in cfg:
            return True
        return bool(cfg.get("fallback_to_web_search"))

    def _model(self) -> str:
        cfg = _tools_baidu_ai_search_conf()
        model = (cfg.get("model") or self.config.get("model") or DEFAULT_MODEL).strip()
        return model or DEFAULT_MODEL

    def _search_source(self) -> str:
        cfg = _tools_baidu_ai_search_conf()
        source = (
            cfg.get("search_source") or self.config.get("search_source") or DEFAULT_SEARCH_SOURCE
        )
        source = str(source).strip()
        if source not in ("baidu_search_v1", "baidu_search_v2"):
            return DEFAULT_SEARCH_SOURCE
        return source

    def _mark_exhausted(self, reason: str) -> None:
        try:
            self._get_capability_state().mark_exhausted(CAP_BAIDU_AI_SEARCH, reason)
        except Exception as exc:
            logger.warning("[BaiduAiSearch] failed to mark exhausted: %s", exc)

    def _is_exhausted(self) -> bool:
        try:
            return self._get_capability_state().is_exhausted(CAP_BAIDU_AI_SEARCH)
        except Exception as exc:
            logger.debug("[BaiduAiSearch] exhaustion check failed: %s", exc)
            return False

    def _reserve_quota(self) -> Tuple[bool, int, Optional[int]]:
        limit = self._daily_limit()
        try:
            reserved, count = self._get_capability_state().reserve(CAP_BAIDU_AI_SEARCH, limit)
            return reserved, count, limit
        except sqlite3.Error as exc:
            logger.error("[BaiduAiSearch] failed to persist usage: %s", exc)
            # Fail closed so a broken counter cannot exceed free quotas.
            return limit is None, 0, limit

    @staticmethod
    def _map_freshness(freshness: str) -> Optional[str]:
        return {
            "oneDay": "day",
            "oneWeek": "week",
            "oneMonth": "month",
            "oneYear": "year",
        }.get((freshness or "").strip())

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        query = (args.get("query") or "").strip()
        if not query:
            return ToolResult.fail("Error: 'query' parameter is required")

        count = args.get("count", DEFAULT_TOP_K)
        if not isinstance(count, int) or count < 1 or count > 20:
            count = DEFAULT_TOP_K
        freshness = args.get("freshness", "noLimit")

        api_key = _get_qianfan_api_key()
        if not api_key:
            logger.info("[BaiduAiSearch] no QIANFAN_API_KEY; falling back to web_search")
            return self._fallback_to_web_search(
                query,
                count=count,
                freshness=freshness,
                reason="QIANFAN_API_KEY not configured",
            )

        if self._is_exhausted():
            reason = "daily intelligent-search exhaustion flag set"
            logger.info("[BaiduAiSearch] %s; falling back to web_search", reason)
            return self._fallback_to_web_search(
                query, count=count, freshness=freshness, reason=reason
            )

        reserved, usage_count, quota_limit = self._reserve_quota()
        if not reserved:
            reason = (
                f"daily intelligent-search quota reached "
                f"({usage_count}/{quota_limit})"
            )
            logger.info("[BaiduAiSearch] %s; falling back to web_search", reason)
            self._mark_exhausted(reason)
            return self._fallback_to_web_search(
                query, count=count, freshness=freshness, reason=reason
            )

        logger.info(
            "[BaiduAiSearch] query=%r model=%s usage=%s%s top_k=%s",
            query if len(query) <= 60 else (query[:57] + "..."),
            self._model(),
            usage_count,
            f"/{quota_limit}" if quota_limit is not None else "",
            count,
        )

        try:
            result = self._call_baidu_ai_search(api_key, query, count, freshness)
        except requests.Timeout:
            return self._fallback_to_web_search(
                query,
                count=count,
                freshness=freshness,
                reason=f"baidu_ai_search timed out after {DEFAULT_TIMEOUT}s",
            )
        except requests.RequestException as exc:
            logger.warning("[BaiduAiSearch] request failed: %s", exc)
            return self._fallback_to_web_search(
                query,
                count=count,
                freshness=freshness,
                reason=f"baidu_ai_search request failed: {exc}",
            )
        except Exception as exc:
            logger.error("[BaiduAiSearch] unexpected error: %s", exc, exc_info=True)
            return self._fallback_to_web_search(
                query,
                count=count,
                freshness=freshness,
                reason=f"baidu_ai_search unexpected error: {exc}",
            )

        if result.status == "success":
            return result

        # Quota / billing exhaustion from remote → mark day + degrade.
        err_text = str(result.result or "")
        if looks_like_quota_error(0, err_text) or "quota" in err_text.lower():
            logger.info(
                "[BaiduAiSearch] remote quota/billing signal; falling back to web_search: %s",
                err_text[:200],
            )
            self._mark_exhausted(err_text)
            return self._fallback_to_web_search(
                query,
                count=count,
                freshness=freshness,
                reason=err_text,
            )

        # Other hard failures: still try web_search when fallback is on, so the
        # agent is not stuck without search.
        if self._fallback_enabled():
            logger.warning(
                "[BaiduAiSearch] primary call failed; falling back to web_search: %s",
                err_text[:200],
            )
            return self._fallback_to_web_search(
                query,
                count=count,
                freshness=freshness,
                reason=err_text,
            )
        return result

    def _call_baidu_ai_search(
        self, api_key: str, query: str, count: int, freshness: str
    ) -> ToolResult:
        url = f"{_api_base()}/ai_search/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Appbuilder-From": "cow",
        }

        cfg = _tools_baidu_ai_search_conf()
        payload: Dict[str, Any] = {
            "messages": [{"role": "user", "content": query}],
            "stream": False,
            "model": self._model(),
            "search_source": self._search_source(),
            "resource_type_filter": [{"type": "web", "top_k": count}],
            # Always search for tool invocations; pure LLM answers defeat the purpose.
            "search_mode": str(cfg.get("search_mode") or "required"),
            "enable_corner_markers": bool(
                cfg.get("enable_corner_markers", True)
            ),
            "enable_deep_search": bool(cfg.get("enable_deep_search", False)),
        }

        recency = self._map_freshness(freshness)
        if recency:
            payload["search_recency_filter"] = recency

        if cfg.get("instruction"):
            payload["instruction"] = str(cfg.get("instruction"))[:4000]
        if cfg.get("enable_reasoning") is not None:
            payload["enable_reasoning"] = bool(cfg.get("enable_reasoning"))
        if cfg.get("temperature") is not None:
            try:
                payload["temperature"] = float(cfg.get("temperature"))
            except (TypeError, ValueError):
                pass

        resp = requests.post(
            url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT
        )

        body_preview = (resp.text or "")[:300]
        if resp.status_code == 401:
            return ToolResult.fail("Error: Invalid Qianfan API key for baidu_ai_search.")
        if looks_like_quota_error(resp.status_code, body_preview):
            return ToolResult.fail(
                f"Error: baidu_ai_search quota/rate-limit HTTP {resp.status_code}: {body_preview}"
            )
        if resp.status_code != 200:
            return ToolResult.fail(
                f"Error: baidu_ai_search HTTP {resp.status_code}: {body_preview}"
            )

        try:
            data = resp.json()
        except ValueError:
            return ToolResult.fail("Error: baidu_ai_search returned non-JSON body")

        if not isinstance(data, dict):
            return ToolResult.fail("Error: baidu_ai_search returned unexpected payload")

        # Business-level errors surface as code/message even on HTTP 200.
        code = data.get("code")
        if code not in (None, 0, "0", ""):
            msg = data.get("message") or ""
            err = f"Error: baidu_ai_search business error code={code}: {msg}"
            if looks_like_quota_error(200, f"{code} {msg}", code=code):
                return ToolResult.fail(err)
            return ToolResult.fail(err)

        answer, reasoning = self._extract_answer(data)
        references = self._extract_references(data)
        if not answer and not references:
            return ToolResult.fail("Error: baidu_ai_search returned empty answer and references")

        output: Dict[str, Any] = {
            "query": query,
            "backend": "baidu_ai_search",
            "model": self._model(),
            "answer": answer,
            "references": references,
            "total": len(references),
            "count": len(references),
            # Alias so agents used to web_search.results still see sources.
            "results": [
                {
                    "title": r.get("title") or "",
                    "url": r.get("url") or "",
                    "snippet": r.get("snippet") or r.get("content") or "",
                    "siteName": r.get("siteName") or "",
                    "datePublished": r.get("datePublished") or "",
                }
                for r in references
                if r.get("url") or r.get("title")
            ],
            "citationPolicy": (
                "When using the intelligent-search answer, cite the supporting "
                "reference URLs as clickable source links. Do not present "
                "search-derived factual claims without citations."
            ),
        }
        if reasoning:
            output["reasoning"] = reasoning
        if data.get("usage"):
            output["usage"] = data.get("usage")
        request_id = (
            data.get("request_Id")
            or data.get("request_id")
            or data.get("requestId")
            or ""
        )
        if request_id:
            output["request_id"] = request_id
        if data.get("is_safe") is not None:
            output["is_safe"] = data.get("is_safe")
        if data.get("followup_queries"):
            output["followup_queries"] = data.get("followup_queries")

        # Like doubao_search: leave composition to cowagent (no direct_final_answer).
        return ToolResult.success(output)

    @staticmethod
    def _extract_answer(data: Dict[str, Any]) -> Tuple[str, str]:
        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices:
            # Some deployments may put answer at top level.
            content = str(data.get("answer") or data.get("content") or "").strip()
            return content, ""

        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        delta = first.get("delta") if isinstance(first.get("delta"), dict) else {}
        content = str(
            message.get("content")
            or delta.get("content")
            or first.get("content")
            or ""
        ).strip()
        reasoning = str(
            message.get("reasoning_content")
            or delta.get("reasoning_content")
            or ""
        ).strip()
        return content, reasoning

    @staticmethod
    def _extract_references(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        refs = data.get("references") or []
        if not isinstance(refs, list):
            return []
        results: List[Dict[str, Any]] = []
        for item in refs:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            content = str(item.get("content") or item.get("snippet") or "").strip()
            site = str(
                item.get("web_anchor") or item.get("website") or item.get("siteName") or ""
            ).strip()
            if not site and url:
                site = urlparse(url).hostname or ""
            if not (title or content or url):
                continue
            results.append(
                {
                    "id": item.get("id"),
                    "title": title,
                    "url": url,
                    "content": content[:1200],
                    "snippet": content[:300],
                    "siteName": site,
                    "datePublished": str(item.get("date") or "").strip(),
                    "type": str(item.get("type") or "web"),
                }
            )
        return results

    def _fallback_to_web_search(
        self,
        query: str,
        *,
        count: int,
        freshness: str,
        reason: str,
    ) -> ToolResult:
        if not self._fallback_enabled():
            return ToolResult.fail(
                f"Error: baidu_ai_search unavailable ({reason}) and fallback_to_web_search is disabled"
            )

        try:
            from agent.tools.web_search.web_search import WebSearch
        except Exception as exc:
            return ToolResult.fail(
                f"Error: baidu_ai_search unavailable ({reason}); web_search import failed: {exc}"
            )

        if not WebSearch.is_available():
            return ToolResult.fail(
                f"Error: baidu_ai_search unavailable ({reason}); no web_search provider configured"
            )

        # Map baidu_ai_search count (1-20) into web_search range.
        web_count = max(1, min(int(count or DEFAULT_TOP_K), 50))
        web_tool = WebSearch(
            {"usage_store_path": self._usage_store_path}
            if self._usage_store_path
            else {}
        )
        web_result = web_tool.execute(
            {
                "query": query,
                "count": web_count,
                "freshness": freshness or "noLimit",
            }
        )

        if web_result.status != "success":
            return ToolResult.fail(
                f"Error: baidu_ai_search unavailable ({reason}); web_search also failed: "
                f"{web_result.result}"
            )

        payload = web_result.result if isinstance(web_result.result, dict) else {}
        # Preserve original web_search fields; annotate fallback metadata.
        output = dict(payload)
        output["fallback_from"] = "baidu_ai_search"
        output["fallback_reason"] = reason
        output.setdefault("backend", "web_search")
        # Keep an explicit answer-less signal so the agent knows it must summarize.
        output.setdefault(
            "note",
            "Intelligent search unavailable; raw web_search results returned for you to summarize.",
        )
        return ToolResult.success(output)
