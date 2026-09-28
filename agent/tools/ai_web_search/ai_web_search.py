"""Preferred search entry.

Every call uses:

  ai_web_search  →  baidu_ai_search  →  web_search
"""

from __future__ import annotations

from typing import Any, Dict

from agent.tools.base_tool import BaseTool, ToolResult
from common.log import logger
from config import conf


DEFAULT_COUNT = 10
AI_WEB_SEARCH_ROUTE_REASON = "using baidu_ai_search"
CITATION_POLICY = (
    "When search results inform the answer, cite the supporting result URLs as "
    "clickable source links. Do not present search-derived factual claims without citations."
)


def _tools_ai_web_search_conf() -> dict:
    tools_cfg = conf().get("tools") or {}
    if not isinstance(tools_cfg, dict):
        return {}
    block = tools_cfg.get("ai_web_search")
    if not isinstance(block, dict):
        block = tools_cfg.get("doubao_search") or {}
    return block if isinstance(block, dict) else {}


class AiWebSearch(BaseTool):
    """Preferred search entry; internally uses baidu_ai_search → web_search."""

    name: str = "ai_web_search"
    description: str = (
        "Primary real-time web search entry. Always uses baidu_ai_search "
        "(which may further fall back to web_search). Prefer this tool first "
        "for factual questions, news, prices, policies, verification, and "
        "anything that needs current online sources. Returns a summarized "
        "draft and/or web titles, URLs, snippets. When using these results, "
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
                "description": "Ignored. Kept for call compatibility.",
            },
            "query_rewrite": {
                "type": "boolean",
                "description": "Ignored. Kept for call compatibility.",
            },
            "summary": {
                "type": "boolean",
                "description": "Ignored. Kept for call compatibility.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._usage_store_path = self.config.get("usage_store_path")

    @staticmethod
    def is_available() -> bool:
        """Offer when the baidu_ai_search → web_search chain can still serve."""
        try:
            from agent.tools.baidu_ai_search.baidu_ai_search import BaiduAiSearch

            return BaiduAiSearch.is_available()
        except Exception:
            return False

    def _fallback_enabled(self) -> bool:
        cfg = _tools_ai_web_search_conf()
        if "fallback_to_baidu_ai_search" in cfg:
            return bool(cfg.get("fallback_to_baidu_ai_search"))
        if "fallback_to_baidu_ai_search" in self.config:
            return bool(self.config.get("fallback_to_baidu_ai_search"))
        return True

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

        logger.info(
            "[AiWebSearch] routing to baidu_ai_search query=%r",
            query if len(query) <= 60 else (query[:57] + "..."),
        )
        return self._fallback_to_baidu_ai_search(
            query,
            count=count,
            freshness=freshness,
            reason=AI_WEB_SEARCH_ROUTE_REASON,
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
                    f"Error: ai_web_search unavailable ({reason}) and "
                    "fallback_to_baidu_ai_search is disabled"
                )
            )

        try:
            from agent.tools.baidu_ai_search.baidu_ai_search import BaiduAiSearch
        except Exception as exc:
            return self._attach_citation_policy(
                ToolResult.fail(
                    f"Error: ai_web_search unavailable ({reason}); "
                    f"baidu_ai_search import failed: {exc}"
                )
            )

        if not BaiduAiSearch.is_available():
            return self._attach_citation_policy(
                ToolResult.fail(
                    f"Error: ai_web_search unavailable ({reason}); "
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
                    f"Error: ai_web_search unavailable ({reason}); "
                    f"baidu_ai_search/web_search also failed: {ai_result.result}"
                )
            )

        payload = ai_result.result if isinstance(ai_result.result, dict) else {}
        output = dict(payload)
        output["fallback_from"] = "ai_web_search"
        output["fallback_reason"] = reason
        output.setdefault("citationPolicy", CITATION_POLICY)
        logger.info(
            "[AiWebSearch] routed to backend=%s reason=%s",
            output.get("backend"),
            reason[:160],
        )
        return ToolResult.success(output)
