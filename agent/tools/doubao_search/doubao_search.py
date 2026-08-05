"""Volcengine Doubao Search (豆包搜索 / SearchInfinity) as a standalone tool.

API: POST https://open.feedcoopapi.com/search_api/web_search
Docs: https://www.volcengine.com/docs/87772/2272953
Harness guide: https://www.volcengine.com/docs/82379/2301412

Degradation chain (preferred order):
  doubao_search  →  baidu_ai_search  →  web_search

This tool is intentionally separate from ``web_search`` / ``baidu_ai_search``
(not mixed into multi-source fan-out). When Doubao is unavailable, exhausted,
or fails, it automatically falls back to ``baidu_ai_search`` (which may further
degrade to ``web_search``).

Credentials (priority):
  1. tools.doubao_search.api_key
  2. env WEB_SEARCH_API_KEY  (official skill env name)
  3. env DOUBAO_SEARCH_API_KEY
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import requests

from agent.tools.base_tool import BaseTool, ToolResult
from agent.tools.search_quota import (
    CAP_DOUBAO,
    SearchCapabilityState,
    get_search_capability_state,
    looks_like_quota_error,
)
from common.log import logger
from config import conf


DEFAULT_TIMEOUT = 30
DEFAULT_COUNT = 10
DOUBAO_SEARCH_URL = "https://open.feedcoopapi.com/search_api/web_search"
DOUBAO_TRAFFIC_TAG = "skill_web_search_common"
CITATION_POLICY = (
    "When search results inform the answer, cite the supporting result URLs as "
    "clickable source links. Do not present search-derived factual claims without citations."
)

_QUOTA_CODES = {
    "10406",
    "10407",
    "10408",
    "10412",
    "700429",
    "100018",
    "FlowLimitExceeded",
    "FunctionUnavailable",
}
_FRESHNESS_MAP = {
    "oneday": "OneDay",
    "oneweek": "OneWeek",
    "onemonth": "OneMonth",
    "oneyear": "OneYear",
}


def _tools_doubao_search_conf() -> dict:
    tools_cfg = conf().get("tools") or {}
    if not isinstance(tools_cfg, dict):
        return {}
    block = tools_cfg.get("doubao_search") or {}
    return block if isinstance(block, dict) else {}


def _get_api_key() -> str:
    cfg = _tools_doubao_search_conf()
    key = (cfg.get("api_key") or "").strip()
    if key:
        return key
    for env_name in ("WEB_SEARCH_API_KEY", "DOUBAO_SEARCH_API_KEY"):
        key = os.environ.get(env_name, "").strip()
        if key:
            return key
    return ""


def _map_time_range(freshness: str) -> Optional[str]:
    raw = (freshness or "").strip()
    if not raw or raw.lower() in ("nolimit", "none", "all"):
        return None
    mapped = _FRESHNESS_MAP.get(raw.lower())
    if mapped:
        return mapped
    if ".." in raw:
        return raw
    return None


class DoubaoSearch(BaseTool):
    """Standalone 豆包搜索 tool (Volcengine SearchInfinity)."""

    name: str = "doubao_search"
    description: str = (
        "Primary real-time web search via Volcengine Doubao Search (豆包搜索). "
        "Search cascade: doubao_search → baidu_ai_search → web_search. Prefer this "
        "tool first for factual questions, news, prices, policies, verification, "
        "and anything that needs current online sources. On quota exhaustion or "
        "failure it automatically falls back to baidu_ai_search (then web_search). "
        "Returns web titles, URLs, snippets/summaries. When using these results, "
        "cite the supporting result URLs as clickable source links."
    )

    params: dict = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search keywords (recommend 1-100 characters)",
            },
            "count": {
                "type": "integer",
                "description": f"Number of results (1-50, default {DEFAULT_COUNT})",
            },
            "freshness": {
                "type": "string",
                "description": (
                    "Time range filter: 'noLimit' (default), 'oneDay', 'oneWeek', "
                    "'oneMonth', 'oneYear', or date range 'YYYY-MM-DD..YYYY-MM-DD'"
                ),
            },
            "auth_level": {
                "type": "integer",
                "description": "0 = all sources (default), 1 = authoritative only",
            },
            "query_rewrite": {
                "type": "boolean",
                "description": "Enable Doubao server-side query rewrite (default false)",
            },
            "summary": {
                "type": "boolean",
                "description": "Include longer page summaries when available (default true)",
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
        """Offer when Doubao key exists or the degradation chain can still serve."""
        if _get_api_key():
            return True
        try:
            from agent.tools.baidu_ai_search.baidu_ai_search import BaiduAiSearch

            return BaiduAiSearch.is_available()
        except Exception:
            return False

    def _fallback_enabled(self) -> bool:
        cfg = _tools_doubao_search_conf()
        if "fallback_to_baidu_ai_search" in cfg:
            return bool(cfg.get("fallback_to_baidu_ai_search"))
        if "fallback_to_baidu_ai_search" in self.config:
            return bool(self.config.get("fallback_to_baidu_ai_search"))
        return True

    def _is_exhausted(self) -> bool:
        try:
            return self._get_capability_state().is_exhausted(CAP_DOUBAO)
        except Exception as exc:
            logger.debug("[DoubaoSearch] exhaustion check failed: %s", exc)
            return False

    def _mark_exhausted(self, reason: str) -> None:
        try:
            self._get_capability_state().mark_exhausted(CAP_DOUBAO, reason)
        except Exception as exc:
            logger.warning("[DoubaoSearch] failed to mark exhausted: %s", exc)

    @staticmethod
    def _attach_citation_policy(result: ToolResult) -> ToolResult:
        if result.status == "success" and isinstance(result.result, dict):
            result.result.setdefault("citationPolicy", CITATION_POLICY)
        return result

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        query = (args.get("query") or "").strip()
        if not query:
            return self._attach_citation_policy(
                ToolResult.fail("Error: 'query' parameter is required")
            )

        count = args.get("count", DEFAULT_COUNT)
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = DEFAULT_COUNT
        count = max(1, min(count, 50))
        freshness = args.get("freshness", "noLimit")

        api_key = _get_api_key()
        if not api_key:
            logger.info("[DoubaoSearch] no API key; falling back to baidu_ai_search")
            return self._fallback_to_baidu_ai_search(
                query,
                count=count,
                freshness=freshness,
                reason="WEB_SEARCH_API_KEY / tools.doubao_search.api_key not configured",
            )

        if self._is_exhausted():
            reason = "daily Doubao search exhaustion flag set"
            logger.info("[DoubaoSearch] %s; falling back to baidu_ai_search", reason)
            return self._fallback_to_baidu_ai_search(
                query, count=count, freshness=freshness, reason=reason
            )

        try:
            result = self._call_doubao(api_key, query, count, freshness, args)
        except requests.Timeout:
            return self._fallback_to_baidu_ai_search(
                query,
                count=count,
                freshness=freshness,
                reason=f"Doubao search timed out after {DEFAULT_TIMEOUT}s",
            )
        except requests.RequestException as exc:
            logger.warning("[DoubaoSearch] request failed: %s", exc)
            return self._fallback_to_baidu_ai_search(
                query,
                count=count,
                freshness=freshness,
                reason=f"Doubao search request failed: {exc}",
            )
        except Exception as exc:
            logger.error("[DoubaoSearch] unexpected error: %s", exc, exc_info=True)
            return self._fallback_to_baidu_ai_search(
                query,
                count=count,
                freshness=freshness,
                reason=f"Doubao search unexpected error: {exc}",
            )

        if result.status == "success":
            return self._attach_citation_policy(result)

        err_text = str(result.result or "")
        # Quota / billing → mark day + degrade.
        if looks_like_quota_error(0, err_text) or any(
            code in err_text for code in _QUOTA_CODES
        ):
            logger.info(
                "[DoubaoSearch] remote quota/billing signal; falling back to baidu_ai_search: %s",
                err_text[:200],
            )
            self._mark_exhausted(err_text)
            return self._fallback_to_baidu_ai_search(
                query, count=count, freshness=freshness, reason=err_text
            )

        if self._fallback_enabled():
            logger.warning(
                "[DoubaoSearch] primary call failed; falling back to baidu_ai_search: %s",
                err_text[:200],
            )
            return self._fallback_to_baidu_ai_search(
                query, count=count, freshness=freshness, reason=err_text
            )
        return self._attach_citation_policy(result)

    def _call_doubao(
        self,
        api_key: str,
        query: str,
        count: int,
        freshness: str,
        args: Dict[str, Any],
    ) -> ToolResult:
        auth_level = args.get("auth_level")
        if auth_level is None:
            auth_level = _tools_doubao_search_conf().get("auth_level", 0)
        try:
            auth_level_int = int(auth_level)
        except (TypeError, ValueError):
            auth_level_int = 0

        if "query_rewrite" in args:
            query_rewrite = bool(args.get("query_rewrite"))
        else:
            query_rewrite = bool(_tools_doubao_search_conf().get("query_rewrite", False))

        if "summary" in args:
            want_summary = bool(args.get("summary"))
        else:
            want_summary = True

        trimmed_query = query if len(query) <= 100 else query[:100]
        payload: Dict[str, Any] = {
            "Query": trimmed_query,
            "SearchType": "web",
            "Count": count,
            "NeedSummary": True,
        }
        time_range = _map_time_range(str(freshness or ""))
        if time_range:
            payload["TimeRange"] = time_range
        if auth_level_int > 0:
            payload["Filter"] = {"AuthInfoLevel": auth_level_int}
        if query_rewrite:
            payload["QueryControl"] = {"QueryRewrite": True}

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Traffic-Tag": DOUBAO_TRAFFIC_TAG,
        }

        logger.info(
            "[DoubaoSearch] query=%r count=%s freshness=%r auth_level=%s rewrite=%s",
            trimmed_query if len(trimmed_query) <= 60 else (trimmed_query[:57] + "..."),
            count,
            freshness,
            auth_level_int,
            query_rewrite,
        )

        resp = requests.post(
            DOUBAO_SEARCH_URL,
            headers=headers,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=DEFAULT_TIMEOUT,
        )

        body_preview = (resp.text or "")[:300]
        if resp.status_code == 401:
            return ToolResult.fail(
                "Error: Invalid Doubao search API key "
                "(check WEB_SEARCH_API_KEY / search-infinity or Agent Plan apiKey)."
            )
        if looks_like_quota_error(resp.status_code, body_preview):
            reason = f"Doubao search quota HTTP {resp.status_code}: {body_preview}"
            self._mark_exhausted(reason)
            return ToolResult.fail(f"Error: {reason}")
        if resp.status_code != 200:
            return ToolResult.fail(
                f"Error: Doubao search API returned HTTP {resp.status_code}: {body_preview}"
            )

        try:
            data = resp.json()
        except Exception:
            return ToolResult.fail(
                f"Error: Doubao search returned non-JSON response: {body_preview}"
            )

        error = (
            (data.get("ResponseMetadata") or {}).get("Error")
            if isinstance(data, dict)
            else None
        )
        if isinstance(error, dict) and (error.get("Code") or error.get("Message")):
            code = str(error.get("Code") or "")
            message = str(error.get("Message") or "")
            err = f"Error: Doubao search returned {code}: {message}"
            if code in _QUOTA_CODES or looks_like_quota_error(
                200, f"{code} {message}", code=code
            ):
                self._mark_exhausted(err)
            return ToolResult.fail(err)

        result_block = (data.get("Result") or {}) if isinstance(data, dict) else {}
        web_results = result_block.get("WebResults") or []
        results: List[Dict[str, Any]] = []
        for item in web_results:
            if not isinstance(item, dict):
                continue
            title = str(item.get("Title") or "").strip()
            url = str(item.get("Url") or "").strip()
            if not title or not url:
                continue
            snippet = str(item.get("Snippet") or item.get("Summary") or "").strip()
            entry: Dict[str, Any] = {
                "title": title,
                "url": url,
                "snippet": snippet[:500] if snippet else "",
                "siteName": str(item.get("SiteName") or "").strip(),
                "datePublished": str(
                    item.get("PublishTime")
                    or item.get("publish_time")
                    or item.get("DatePublished")
                    or ""
                ).strip(),
            }
            summary_text = str(item.get("Summary") or "").strip()
            if want_summary and summary_text:
                entry["summary"] = summary_text[:1000]
            elif summary_text and not entry["snippet"]:
                entry["snippet"] = summary_text[:500]
            results.append(entry)

        if not results:
            return ToolResult.fail("Error: Doubao search returned no results.")

        return ToolResult.success(
            {
                "query": query,
                "backend": "doubao",
                "total": int(result_block.get("ResultCount") or len(results)),
                "count": len(results),
                "results": results,
            }
        )

    def _fallback_to_baidu_ai_search(
        self,
        query: str,
        *,
        count: int,
        freshness: str,
        reason: str,
    ) -> ToolResult:
        if not self._fallback_enabled():
            return self._attach_citation_policy(
                ToolResult.fail(
                    f"Error: doubao_search unavailable ({reason}) and "
                    "fallback_to_baidu_ai_search is disabled"
                )
            )

        try:
            from agent.tools.baidu_ai_search.baidu_ai_search import BaiduAiSearch
        except Exception as exc:
            return self._attach_citation_policy(
                ToolResult.fail(
                    f"Error: doubao_search unavailable ({reason}); "
                    f"baidu_ai_search import failed: {exc}"
                )
            )

        if not BaiduAiSearch.is_available():
            return self._attach_citation_policy(
                ToolResult.fail(
                    f"Error: doubao_search unavailable ({reason}); "
                    "baidu_ai_search / web_search also unavailable"
                )
            )

        # baidu_ai_search accepts count 1-20; web_search accepts up to 50 via its own cascade.
        ai_count = max(1, min(int(count or DEFAULT_COUNT), 20))
        ai_tool = BaiduAiSearch(
            {"usage_store_path": self._usage_store_path}
            if self._usage_store_path
            else {}
        )
        ai_result = ai_tool.execute(
            {
                "query": query,
                "count": ai_count,
                "freshness": freshness or "noLimit",
            }
        )

        if ai_result.status != "success":
            return self._attach_citation_policy(
                ToolResult.fail(
                    f"Error: doubao_search unavailable ({reason}); "
                    f"baidu_ai_search/web_search also failed: {ai_result.result}"
                )
            )

        payload = ai_result.result if isinstance(ai_result.result, dict) else {}
        output = dict(payload)
        output["fallback_from"] = "doubao_search"
        output["fallback_reason"] = reason
        # Keep citation policy visible on the degraded result.
        output.setdefault("citationPolicy", CITATION_POLICY)
        logger.info(
            "[DoubaoSearch] fell back to backend=%s reason=%s",
            output.get("backend"),
            reason[:160],
        )
        return ToolResult.success(output)
