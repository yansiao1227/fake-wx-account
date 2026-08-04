"""Web Search tool. Supports six backends with a unified response format:
  - tavily  (https://api.tavily.com/search)
  - bocha   (https://open.bochaai.com)
  - zhipu   (https://docs.bigmodel.cn/cn/guide/tools/web-search)
  - qianfan / baidu (https://cloud.baidu.com/doc/qianfan/s/2mh4su4uy)
  - linkai  (https://link-ai.tech, fallback)
  - ddgs    (keyless metasearch fallback)

Default search pipeline (strategy 'auto')
  1. Call Baidu Qianfan ``/v2/tools/query_rewrite`` with ``QIANFAN_API_KEY``
     to produce an optimized ``altered_query`` (and optional decomposition).
  2. Fan-out the rewritten query to the multi-source trio in parallel:
     baidu/qianfan, tavily, ddgs (whichever are configured/available).
  3. Deduplicate by URL, score by relevance to the original + rewritten
     query, and return the top-N results for the model to summarize.

Provider selection
  - strategy 'auto' (default): multi-source fan-out over baidu/tavily/ddgs.
    If none of the trio is available, fall back through the remaining
    providers in PROVIDER_ORDER.
  - strategy 'fixed': use the configured provider only (still rewrites the
    query first when a Qianfan key is present). If its credential is
    missing at call time, silently fall back to auto multi-source.

Credentials
  - tavily  : tools.web_search.tavily_api_key -> env TAVILY_API_KEY
  - bocha   : tools.web_search.bocha_api_key  ->  env BOCHA_API_KEY
  - zhipu   : conf.zhipu_ai_api_key            ->  env ZHIPUAI_API_KEY
  - qianfan : conf.qianfan_api_key             ->  env QIANFAN_API_KEY
              (also used for query_rewrite)
  - linkai  : conf.linkai_api_key              ->  env LINKAI_API_KEY
  - ddgs    : no credential required; requires the ``ddgs`` Python package

Query rewrite docs: https://cloud.baidu.com/doc/qianfan-api/s/tmokwnznj
"""

import json
import os
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

try:
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException
except ImportError:  # Optional dependency; API-key providers still work.
    DDGS = None

    class DDGSException(Exception):
        pass

from agent.tools.base_tool import BaseTool, ToolResult
from common.log import logger
from config import conf, get_data_root


DEFAULT_TIMEOUT = 30
REWRITE_TIMEOUT = 15
CITATION_POLICY = (
    "When search results inform the answer, cite the supporting result URLs as "
    "clickable source links. Do not present search-derived factual claims without citations."
)

# Full catalog of backends (used for fixed strategy / legacy single-provider).
PROVIDER_ORDER = ("qianfan", "tavily", "bocha", "zhipu", "linkai", "ddgs")

# Default multi-source fan-out trio (baidu search is exposed as provider id
# ``qianfan`` for credential and quota continuity).
MULTI_SOURCE_PROVIDERS = ("qianfan", "tavily", "ddgs")

PROVIDER_LABELS = {
    "tavily": "Tavily",
    "bocha":   "Bocha",
    "zhipu":   "Zhipu",
    "qianfan": "Baidu Qianfan",
    "linkai":  "LinkAI",
    "ddgs": "DDGS",
}

_DEFAULT_PROVIDER_QUOTAS = {
    "qianfan": ("day", 50),
    "tavily": ("month", 1000),
}

# Lightweight CJK / Latin tokeniser for relevance scoring.
_TOKEN_RE = re.compile(
    r"[A-Za-z0-9_]+|[\u4e00-\u9fff]",
    re.UNICODE,
)


class WebSearchUsageStore:
    """Persist provider request counts and reserve quota atomically."""

    _schema_lock = threading.Lock()

    def __init__(self, path: Optional[str] = None):
        configured = path or _tools_web_search_conf().get("usage_store_path")
        self.path = Path(configured or (Path(get_data_root()) / "web_search_usage.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self):
        with self._schema_lock, self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_usage (
                    provider TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (provider, period_key)
                )
                """
            )

    @staticmethod
    def period_key(period: str, now: Optional[datetime] = None) -> str:
        current = now or datetime.now().astimezone()
        if period == "day":
            return current.strftime("%Y-%m-%d")
        if period == "month":
            return current.strftime("%Y-%m")
        return "all"

    def count(self, provider: str, period: str, now: Optional[datetime] = None) -> int:
        key = self.period_key(period, now)
        with self._connect() as db:
            row = db.execute(
                "SELECT request_count FROM provider_usage WHERE provider=? AND period_key=?",
                (provider, key),
            ).fetchone()
        return int(row["request_count"]) if row else 0

    def reserve(
        self,
        provider: str,
        period: str,
        limit: Optional[int],
        now: Optional[datetime] = None,
    ) -> tuple[bool, int]:
        """Increment before the request; return False without increment at quota."""
        key = self.period_key(period, now)
        updated_at = (now or datetime.now().astimezone()).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT request_count FROM provider_usage WHERE provider=? AND period_key=?",
                (provider, key),
            ).fetchone()
            current = int(row["request_count"]) if row else 0
            if limit is not None and current >= max(0, int(limit)):
                db.rollback()
                return False, current
            next_count = current + 1
            db.execute(
                """
                INSERT INTO provider_usage(provider, period_key, request_count, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider, period_key) DO UPDATE SET
                    request_count=excluded.request_count,
                    updated_at=excluded.updated_at
                """,
                (provider, key, next_count, updated_at),
            )
            db.commit()
            return True, next_count


def _tools_web_search_conf() -> dict:
    """Return the tools.web_search config block (dict-like)."""
    tools_cfg = conf().get("tools") or {}
    if not isinstance(tools_cfg, dict):
        return {}
    block = tools_cfg.get("web_search") or {}
    return block if isinstance(block, dict) else {}


def _get_api_key(provider: str) -> str:
    """Resolve API key for a provider, with conf -> env fallback."""
    if provider == "tavily":
        key = (_tools_web_search_conf().get("tavily_api_key") or "").strip()
        return key or os.environ.get("TAVILY_API_KEY", "").strip()
    if provider == "bocha":
        key = (_tools_web_search_conf().get("bocha_api_key") or "").strip()
        return key or os.environ.get("BOCHA_API_KEY", "").strip()
    if provider == "zhipu":
        key = (conf().get("zhipu_ai_api_key") or "").strip()
        return key or os.environ.get("ZHIPUAI_API_KEY", "").strip()
    if provider == "qianfan":
        key = (conf().get("qianfan_api_key") or "").strip()
        return key or os.environ.get("QIANFAN_API_KEY", "").strip()
    if provider == "linkai":
        key = (conf().get("linkai_api_key") or "").strip()
        return key or os.environ.get("LINKAI_API_KEY", "").strip()
    return ""


def configured_providers() -> List[str]:
    """Return usable providers in canonical order.

    Credentialed APIs are preferred. DDGS is intentionally last so it keeps
    search available without replacing a configured API backend.
    """
    return [p for p in PROVIDER_ORDER if (p == "ddgs" and DDGS is not None) or _get_api_key(p)]


def multi_source_providers() -> List[str]:
    """Providers used for the default multi-source fan-out.

    Prefer the baidu/tavily/ddgs trio. When none of them is available, fall
    back to any other configured provider so the tool remains usable.
    """
    available = set(configured_providers())
    trio = [p for p in MULTI_SOURCE_PROVIDERS if p in available]
    if trio:
        return trio
    return [p for p in PROVIDER_ORDER if p in available]


def _configured_strategy() -> str:
    return (_tools_web_search_conf().get("strategy") or "auto").strip().lower()


def _configured_provider() -> str:
    return (_tools_web_search_conf().get("provider") or "").strip().lower()


def _query_rewrite_enabled() -> bool:
    """Whether to call Qianfan query_rewrite before searching (default True)."""
    val = _tools_web_search_conf().get("query_rewrite")
    if val is None:
        return True
    return bool(val)


def _tokenize_for_relevance(text: str) -> List[str]:
    """Extract lightweight tokens for relevance scoring (Latin words + CJK chars)."""
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _normalize_result_url(url: str) -> str:
    """Normalize URL for cross-provider deduplication."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw.rstrip("/").lower()
    scheme = (parsed.scheme or "https").lower()
    netloc = (parsed.netloc or "").lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = (parsed.path or "").rstrip("/")
    return f"{scheme}://{netloc}{path}"


def _overlap_score(query_tokens: List[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    text_tokens = set(_tokenize_for_relevance(text))
    if not text_tokens:
        return 0.0
    hits = sum(1 for t in query_tokens if t in text_tokens)
    return hits / float(len(query_tokens))


def score_result_relevance(
    query: str,
    altered_query: str,
    item: Dict[str, Any],
    *,
    provider_rank: int = 0,
    provider_hits: int = 1,
) -> float:
    """Score how relevant a single result is to the search intent.

    Combines:
      - lexical overlap of original + rewritten query against title/snippet
      - provider-native score when present (e.g. Tavily)
      - multi-provider agreement boost
      - mild within-provider rank bonus (earlier results score higher)
    """
    title = str(item.get("title") or "")
    snippet = str(item.get("snippet") or item.get("summary") or "")
    queries = [q for q in (query, altered_query) if q]
    if not queries:
        base_overlap = 0.0
    else:
        overlaps = []
        for q in queries:
            tokens = _tokenize_for_relevance(q)
            title_s = _overlap_score(tokens, title)
            snip_s = _overlap_score(tokens, snippet)
            overlaps.append(0.7 * title_s + 0.3 * snip_s)
        base_overlap = max(overlaps) if overlaps else 0.0

    provider_score = 0.0
    raw_score = item.get("score")
    if isinstance(raw_score, (int, float)):
        provider_score = max(0.0, min(1.0, float(raw_score)))

    rank_bonus = max(0.0, 0.12 * (1.0 - min(provider_rank, 20) / 20.0))
    multi_boost = 0.08 * max(0, int(provider_hits) - 1)

    # Weighted blend; keep in [0, 1].
    score = (
        0.55 * base_overlap
        + 0.30 * provider_score
        + rank_bonus
        + multi_boost
    )
    return round(max(0.0, min(1.0, score)), 4)


def merge_and_rank_results(
    query: str,
    altered_query: str,
    provider_payloads: List[Tuple[str, List[Dict[str, Any]]]],
    limit: int,
) -> List[Dict[str, Any]]:
    """Deduplicate multi-provider hits by URL and return top-N by relevance."""
    by_url: Dict[str, Dict[str, Any]] = {}

    for provider, items in provider_payloads:
        for rank, raw in enumerate(items or []):
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("url") or "").strip()
            title = str(raw.get("title") or "").strip()
            if not url or not title:
                continue
            key = _normalize_result_url(url) or url
            if key not in by_url:
                entry = {
                    "title": title,
                    "url": url,
                    "snippet": str(raw.get("snippet") or "").strip(),
                    "siteName": str(raw.get("siteName") or "").strip(),
                    "datePublished": str(raw.get("datePublished") or "").strip(),
                    "sources": [provider],
                    "_best_rank": rank,
                    "_provider_score": raw.get("score"),
                }
                if raw.get("summary"):
                    entry["summary"] = raw["summary"]
                by_url[key] = entry
            else:
                entry = by_url[key]
                if provider not in entry["sources"]:
                    entry["sources"].append(provider)
                # Prefer longer snippet / title when merging duplicates.
                snip = str(raw.get("snippet") or "").strip()
                if len(snip) > len(entry.get("snippet") or ""):
                    entry["snippet"] = snip
                if raw.get("summary") and not entry.get("summary"):
                    entry["summary"] = raw["summary"]
                if not entry.get("siteName") and raw.get("siteName"):
                    entry["siteName"] = str(raw.get("siteName") or "")
                if not entry.get("datePublished") and raw.get("datePublished"):
                    entry["datePublished"] = str(raw.get("datePublished") or "")
                entry["_best_rank"] = min(entry.get("_best_rank", rank), rank)
                # Keep strongest native score when present.
                prev = entry.get("_provider_score")
                cur = raw.get("score")
                if isinstance(cur, (int, float)):
                    if not isinstance(prev, (int, float)) or float(cur) > float(prev):
                        entry["_provider_score"] = cur

    ranked: List[Dict[str, Any]] = []
    for entry in by_url.values():
        proxy = {
            "title": entry.get("title"),
            "snippet": entry.get("snippet"),
            "summary": entry.get("summary"),
            "score": entry.get("_provider_score"),
        }
        entry["score"] = score_result_relevance(
            query,
            altered_query,
            proxy,
            provider_rank=int(entry.pop("_best_rank", 0) or 0),
            provider_hits=len(entry.get("sources") or []),
        )
        entry.pop("_provider_score", None)
        ranked.append(entry)

    ranked.sort(
        key=lambda item: (
            float(item.get("score") or 0.0),
            len(item.get("sources") or []),
        ),
        reverse=True,
    )
    return ranked[: max(1, min(int(limit or 10), 50))]


class WebSearch(BaseTool):
    """Tool for searching the web across multiple providers."""

    name: str = "web_search"
    description: str = (
        "Search the web for real-time information. Returns titles, URLs, and snippets. "
        "When using these results in an answer, cite the supporting result URLs as "
        "clickable source links; do not present search-derived factual claims without citations."
    )

    params: dict = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query string"
            },
            "count": {
                "type": "integer",
                "description": "Number of results to return (1-50, default: 10)"
            },
            "freshness": {
                "type": "string",
                "description": (
                    "Time range filter. Options: "
                    "'noLimit' (default), 'oneDay', 'oneWeek', 'oneMonth', 'oneYear', "
                    "or date range like '2025-01-01..2025-02-01'"
                )
            },
            "summary": {
                "type": "boolean",
                "description": "Whether to include text summary for each result (default: false)"
            }
        },
        "required": ["query"]
    }

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._usage_store_path = self.config.get("usage_store_path")
        self._usage_store: Optional[WebSearchUsageStore] = None

    def _get_usage_store(self) -> WebSearchUsageStore:
        if self._usage_store is None:
            self._usage_store = WebSearchUsageStore(self._usage_store_path)
        return self._usage_store

    @staticmethod
    def _provider_quota(provider: str) -> tuple[str, Optional[int]]:
        default_period, default_limit = _DEFAULT_PROVIDER_QUOTAS.get(
            provider, ("all", None)
        )
        search_config = _tools_web_search_conf()
        if provider == "qianfan":
            limit = search_config.get("qianfan_daily_limit", default_limit)
        elif provider == "tavily":
            limit = search_config.get("tavily_monthly_limit", default_limit)
        else:
            limit = None
        try:
            normalized_limit = None if limit is None else max(0, int(limit))
        except (TypeError, ValueError):
            normalized_limit = default_limit
        return default_period, normalized_limit

    def _reserve_provider(self, provider: str) -> tuple[bool, int, Optional[int]]:
        period, limit = self._provider_quota(provider)
        try:
            reserved, count = self._get_usage_store().reserve(
                provider, period, limit
            )
            return reserved, count, limit
        except sqlite3.Error as exc:
            logger.error(
                "[WebSearch] failed to persist provider usage provider=%s: %s",
                provider,
                exc,
            )
            # Fail closed for metered APIs so a broken counter cannot exceed
            # free quotas. Unlimited providers such as DDGS remain available.
            return limit is None, 0, limit

    @staticmethod
    def is_available() -> bool:
        """Tool is offered to the agent when at least one provider has a key."""
        return bool(configured_providers())

    @classmethod
    def get_json_schema(cls) -> dict:
        """Return the schema without exposing deployment routing to the model."""
        schema = {
            "name": cls.name,
            "description": cls.description,
            "parameters": json.loads(json.dumps(cls.params)),  # deep copy
        }
        # Provider routing is a deployment policy. Do not expose a provider
        # selector that lets a model bypass the configured fallback order.
        return schema

    # ------------------------------------------------------------------
    # Provider resolution
    # ------------------------------------------------------------------

    def _resolve_search_providers(self, requested: Optional[str]) -> List[str]:
        """Resolve which providers to hit for this call.

        - fixed: single pinned provider when available
        - auto: multi-source trio (baidu/tavily/ddgs), else remaining backends
        """
        available = configured_providers()
        if not available:
            return []

        if _configured_strategy() == "fixed":
            pinned = _configured_provider()
            if pinned in available:
                return [pinned]
            if pinned:
                logger.warning(
                    "[WebSearch] pinned provider '%s' unavailable, falling back to auto multi-source",
                    pinned,
                )

        if requested:
            logger.info(
                "[WebSearch] ignoring provider hint=%r in auto strategy; "
                "using multi-source fan-out",
                requested,
            )

        return multi_source_providers()

    def _resolve_provider(self, requested: Optional[str]) -> Optional[str]:
        """Compatibility helper: first provider of the resolved set."""
        providers = self._resolve_search_providers(requested)
        return providers[0] if providers else None

    @staticmethod
    def _resolution_reason(requested: Optional[str], providers: List[str]) -> str:
        """Human-readable explanation for the routing decision."""
        strategy = _configured_strategy()
        if strategy == "fixed" and len(providers) == 1 and _configured_provider() == providers[0]:
            return "fixed-strategy"
        if len(providers) > 1:
            return "multi-source"
        return "auto-fallback"

    # ------------------------------------------------------------------
    # Query rewrite (Baidu Qianfan)
    # ------------------------------------------------------------------

    def _rewrite_query(self, query: str) -> Dict[str, Any]:
        """Call Qianfan query_rewrite; fall back to the original query on any failure.

        API: POST https://qianfan.baidubce.com/v2/tools/query_rewrite
        Docs: https://cloud.baidu.com/doc/qianfan-api/s/tmokwnznj
        Auth: Bearer QIANFAN_API_KEY from ~/.cow/.env / conf.
        """
        original = (query or "").strip()
        empty = {
            "query": original,
            "altered_query": original,
            "decomposition_query": [],
            "rewritten": False,
        }
        if not original or not _query_rewrite_enabled():
            return empty

        api_key = _get_api_key("qianfan")
        if not api_key:
            logger.info("[WebSearch] query_rewrite skipped: no QIANFAN_API_KEY")
            return empty

        api_base = (conf().get("qianfan_api_base") or "https://qianfan.baidubce.com/v2").rstrip("/")
        url = f"{api_base}/tools/query_rewrite"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {"query": original}

        try:
            resp = requests.post(
                url, headers=headers, json=payload, timeout=REWRITE_TIMEOUT
            )
        except requests.Timeout:
            logger.warning("[WebSearch] query_rewrite timed out; using original query")
            return empty
        except requests.RequestException as exc:
            logger.warning("[WebSearch] query_rewrite request failed: %s", exc)
            return empty

        if resp.status_code != 200:
            logger.warning(
                "[WebSearch] query_rewrite HTTP %s: %s",
                resp.status_code,
                (resp.text or "")[:200],
            )
            return empty

        try:
            data = resp.json()
        except ValueError:
            logger.warning("[WebSearch] query_rewrite returned non-JSON body")
            return empty

        if not isinstance(data, dict):
            return empty

        # Business errors surface as non-zero code even on HTTP 200.
        code = data.get("code")
        if code not in (None, 0, "0"):
            logger.warning(
                "[WebSearch] query_rewrite business error code=%s message=%s",
                code,
                data.get("message"),
            )
            return empty

        altered = str(data.get("altered_query") or "").strip() or original
        decomposition = data.get("decomposition_query") or []
        if not isinstance(decomposition, list):
            decomposition = []
        decomposition = [str(q).strip() for q in decomposition if str(q).strip()]

        logger.info(
            "[WebSearch] query_rewrite ok altered=%r decomposition=%d",
            altered if len(altered) <= 80 else (altered[:77] + "..."),
            len(decomposition),
        )
        return {
            "query": original,
            "altered_query": altered,
            "decomposition_query": decomposition,
            "rewritten": altered != original or bool(decomposition),
            "request_id": data.get("requestId") or data.get("request_id") or "",
        }

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    @staticmethod
    def _attach_citation_policy(result: ToolResult) -> ToolResult:
        """Keep the citation requirement visible after the tool call, not only in its schema."""
        if result.status == "success" and isinstance(result.result, dict):
            result.result.setdefault("citationPolicy", CITATION_POLICY)
        return result

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        query = (args.get("query") or "").strip()
        if not query:
            return ToolResult.fail("Error: 'query' parameter is required")

        count = args.get("count", 10)
        freshness = args.get("freshness", "noLimit")
        summary = args.get("summary", False)
        if not isinstance(count, int) or count < 1 or count > 50:
            count = 10

        requested = args.get("provider")
        providers = self._resolve_search_providers(requested)
        if not providers:
            return ToolResult.fail(
                "Error: No search provider configured. "
                "Configure QIANFAN_API_KEY / TAVILY_API_KEY or install ddgs."
            )

        # 1) Optimize the query via Baidu Qianfan before searching.
        rewrite = self._rewrite_query(query)
        search_query = (rewrite.get("altered_query") or query).strip() or query

        available = configured_providers()
        reason = self._resolution_reason(requested, providers)
        q_preview = search_query if len(search_query) <= 60 else (search_query[:57] + "...")
        logger.info(
            "[WebSearch] providers=%s reason=%s available=%s "
            "query=%r search_query=%r count=%s freshness=%s",
            list(providers),
            reason,
            list(available),
            query if len(query) <= 60 else (query[:57] + "..."),
            q_preview,
            count,
            freshness,
        )

        # 2) Fan-out to resolved providers (parallel for multi-source).
        provider_payloads, failures = self._fanout_search(
            providers, search_query, count, freshness, summary
        )

        # If the primary selection produced nothing (e.g. fixed provider down
        # or all multi-source legs empty), try any remaining configured
        # backends as a last resort.
        if not provider_payloads:
            remaining = [p for p in configured_providers() if p not in providers]
            if remaining:
                logger.warning(
                    "[WebSearch] primary providers failed (%s); trying remaining=%s",
                    failures,
                    remaining,
                )
                extra_payloads, extra_failures = self._fanout_search(
                    remaining, search_query, count, freshness, summary
                )
                failures.extend(extra_failures)
                provider_payloads = extra_payloads

        if not provider_payloads:
            detail = " | ".join(failures) if failures else "no results"
            return ToolResult.fail(f"Error: All search providers failed. {detail}")

        # 3) Merge + rank by relevance; keep the most related hits.
        ranked = merge_and_rank_results(
            query, search_query, provider_payloads, count
        )
        if not ranked:
            detail = " | ".join(failures) if failures else "empty result sets"
            return ToolResult.fail(
                f"Error: Search returned no usable results. {detail}"
            )

        backends = [name for name, _ in provider_payloads]
        output: Dict[str, Any] = {
            "query": query,
            "altered_query": search_query,
            "backend": "multi" if len(backends) > 1 else backends[0],
            "backends": backends,
            "total": len(ranked),
            "count": len(ranked),
            "results": ranked,
        }
        if rewrite.get("decomposition_query"):
            output["decomposition_query"] = rewrite["decomposition_query"]
        if rewrite.get("request_id"):
            output["rewrite_request_id"] = rewrite["request_id"]
        if failures:
            output["provider_warnings"] = failures

        return self._attach_citation_policy(ToolResult.success(output))

    def _fanout_search(
        self,
        providers: List[str],
        query: str,
        count: int,
        freshness: str,
        summary: bool,
    ) -> Tuple[List[Tuple[str, List[Dict[str, Any]]]], List[str]]:
        """Run one or more providers; multi-source runs in parallel."""
        if not providers:
            return [], ["no providers"]

        if len(providers) == 1:
            provider = providers[0]
            reserved, usage_count, quota_limit = self._reserve_provider(provider)
            if not reserved:
                return [], [
                    f"{PROVIDER_LABELS.get(provider, provider)}: local quota "
                    f"reached ({usage_count}/{quota_limit})"
                ]
            logger.info(
                "[WebSearch] provider=%s usage=%s%s",
                provider,
                usage_count,
                f"/{quota_limit}" if quota_limit is not None else "",
            )
            result = self._execute_provider(provider, query, count, freshness, summary)
            if result.status != "success":
                return [], [
                    f"{PROVIDER_LABELS.get(provider, provider)}: {result.result}"
                ]
            items = list((result.result or {}).get("results") or [])
            return [(provider, items)], []

        # Parallel multi-source search.
        payloads: List[Tuple[str, List[Dict[str, Any]]]] = []
        failures: List[str] = []
        runnable: List[str] = []

        for provider in providers:
            reserved, usage_count, quota_limit = self._reserve_provider(provider)
            if not reserved:
                logger.info(
                    "[WebSearch] provider=%s skipped quota=%s/%s",
                    provider,
                    usage_count,
                    quota_limit,
                )
                failures.append(
                    f"{PROVIDER_LABELS.get(provider, provider)}: local quota "
                    f"reached ({usage_count}/{quota_limit})"
                )
                continue
            logger.info(
                "[WebSearch] provider=%s usage=%s%s",
                provider,
                usage_count,
                f"/{quota_limit}" if quota_limit is not None else "",
            )
            runnable.append(provider)

        if not runnable:
            return [], failures

        def _run(provider: str) -> Tuple[str, ToolResult]:
            return provider, self._execute_provider(
                provider, query, count, freshness, summary
            )

        max_workers = min(len(runnable), 3)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_run, p): p for p in runnable}
            for fut in as_completed(futures):
                provider = futures[fut]
                try:
                    name, result = fut.result()
                except Exception as exc:  # defensive: provider wrappers already catch
                    logger.error(
                        "[WebSearch] provider=%s fan-out raised: %s",
                        provider,
                        exc,
                        exc_info=True,
                    )
                    failures.append(
                        f"{PROVIDER_LABELS.get(provider, provider)}: {exc}"
                    )
                    continue
                if result.status != "success":
                    failures.append(
                        f"{PROVIDER_LABELS.get(name, name)}: {result.result}"
                    )
                    continue
                items = list((result.result or {}).get("results") or [])
                if not items:
                    failures.append(
                        f"{PROVIDER_LABELS.get(name, name)}: empty results"
                    )
                    continue
                payloads.append((name, items))

        # Stable order for backends field: follow MULTI_SOURCE / input order.
        order = {p: i for i, p in enumerate(providers)}
        payloads.sort(key=lambda pair: order.get(pair[0], 99))
        return payloads, failures

    def _execute_provider(
        self, provider: str, query: str, count: int, freshness: str, summary: bool
    ) -> ToolResult:
        try:
            if provider == "tavily":
                return self._search_tavily(query, count, freshness, summary)
            if provider == "bocha":
                return self._search_bocha(query, count, freshness, summary)
            if provider == "zhipu":
                return self._search_zhipu(query, count, freshness)
            if provider == "qianfan":
                return self._search_qianfan(query, count, freshness)
            if provider == "linkai":
                return self._search_linkai(query, count, freshness)
            if provider == "ddgs":
                return self._search_ddgs(query, count, freshness)
            return ToolResult.fail(f"Error: Unknown provider '{provider}'")
        except requests.Timeout:
            return ToolResult.fail(
                f"Error: Search request timed out after {DEFAULT_TIMEOUT}s"
            )
        except requests.ConnectionError:
            return ToolResult.fail("Error: Failed to connect to search API")
        except DDGSException as e:
            return ToolResult.fail(f"Error: DDGS search failed - {e}")
        except Exception as e:
            logger.error(
                f"[WebSearch] Unexpected error ({provider}): {e}", exc_info=True
            )
            return ToolResult.fail(f"Error: Search failed - {str(e)}")

    # ------------------------------------------------------------------
    # Tavily
    # ------------------------------------------------------------------

    def _search_tavily(
        self, query: str, count: int, freshness: str, summary: bool
    ) -> ToolResult:
        api_key = _get_api_key("tavily")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "query": query,
            "search_depth": "basic",
            "max_results": max(1, min(int(count or 10), 20)),
            "include_answer": bool(summary),
            "include_raw_content": False,
        }
        time_range = {
            "oneDay": "day",
            "oneWeek": "week",
            "oneMonth": "month",
            "oneYear": "year",
        }.get(freshness)
        if time_range:
            payload["time_range"] = time_range
        elif isinstance(freshness, str) and ".." in freshness:
            start_date, end_date = freshness.split("..", 1)
            if start_date.strip() and end_date.strip():
                payload["start_date"] = start_date.strip()
                payload["end_date"] = end_date.strip()

        resp = requests.post(
            "https://api.tavily.com/search",
            headers=headers,
            json=payload,
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code == 401:
            return ToolResult.fail("Error: Invalid Tavily API key.")
        if resp.status_code == 429:
            return ToolResult.fail("Error: Tavily API quota or rate limit reached.")
        if resp.status_code != 200:
            return ToolResult.fail(
                f"Error: Tavily API returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()
        results = []
        for item in data.get("results") or []:
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url or not title:
                continue
            results.append(
                {
                    "title": title,
                    "url": url,
                    "snippet": str(item.get("content") or "").strip(),
                    "siteName": urlparse(url).hostname or "",
                    "datePublished": str(item.get("published_date") or ""),
                    "score": item.get("score"),
                }
            )
        if not results:
            return ToolResult.fail("Error: Tavily returned no results.")
        output = {
            "query": query,
            "backend": "tavily",
            "total": len(results),
            "count": len(results),
            "results": results,
        }
        if summary and data.get("answer"):
            output["summary"] = data["answer"]
        if data.get("usage"):
            output["usage"] = data["usage"]
        return ToolResult.success(output)

    # ------------------------------------------------------------------
    # Bocha
    # ------------------------------------------------------------------

    def _search_bocha(self, query: str, count: int, freshness: str, summary: bool) -> ToolResult:
        api_key = _get_api_key("bocha")
        url = "https://api.bochaai.com/v1/web-search"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {"query": query, "count": count, "freshness": freshness, "summary": summary}

        logger.debug(f"[WebSearch] bocha: query='{query}', count={count}")
        resp = requests.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)

        if resp.status_code == 401:
            return ToolResult.fail("Error: Invalid bocha API key.")
        if resp.status_code == 403:
            return ToolResult.fail("Error: bocha API — insufficient balance. Top up at https://open.bochaai.com")
        if resp.status_code == 429:
            return ToolResult.fail("Error: bocha API rate limit reached.")
        if resp.status_code != 200:
            return ToolResult.fail(f"Error: bocha API returned HTTP {resp.status_code}")

        data = resp.json()
        api_code = data.get("code")
        if api_code is not None and api_code != 200:
            msg = data.get("msg") or "Unknown error"
            return ToolResult.fail(f"Error: bocha API error (code={api_code}): {msg}")

        pages = (data.get("data") or {}).get("webPages", {}).get("value", []) or []
        results = []
        for p in pages:
            item = {
                "title": p.get("name", ""),
                "url": p.get("url", ""),
                "snippet": p.get("snippet", ""),
                "siteName": p.get("siteName", ""),
                "datePublished": p.get("datePublished") or p.get("dateLastCrawled", ""),
            }
            if p.get("summary"):
                item["summary"] = p["summary"]
            results.append(item)
        total = (data.get("data") or {}).get("webPages", {}).get("totalEstimatedMatches", len(results))
        return ToolResult.success({
            "query": query, "backend": "bocha",
            "total": total, "count": len(results), "results": results,
        })

    # ------------------------------------------------------------------
    # Zhipu
    # ------------------------------------------------------------------

    def _search_zhipu(self, query: str, count: int, freshness: str) -> ToolResult:
        api_key = _get_api_key("zhipu")
        api_base = (conf().get("zhipu_ai_api_base") or "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
        url = f"{api_base}/web_search"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # Zhipu Web Search expects `search_query` <= 70 chars; truncate
        # gracefully so a long agent-supplied query doesn't get rejected.
        trimmed_query = (query or "")[:70]
        engine = (_tools_web_search_conf().get("zhipu_search_engine") or "search_pro").strip().lower()
        if engine not in ("search_std", "search_pro", "search_pro_sogou", "search_pro_quark"):
            engine = "search_pro"

        payload: Dict[str, Any] = {
            "search_engine": engine,
            "search_query": trimmed_query,
            "search_intent": False,
            "count": max(1, min(int(count or 10), 50)),
            "search_recency_filter": freshness if freshness in (
                "oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit"
            ) else "noLimit",
        }
        content_size = (_tools_web_search_conf().get("zhipu_content_size") or "").strip().lower()
        if content_size in ("medium", "high"):
            payload["content_size"] = content_size

        logger.debug(f"[WebSearch] zhipu: query='{trimmed_query}', count={payload['count']}, engine={engine}")
        resp = requests.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)

        if resp.status_code == 401:
            return ToolResult.fail("Error: Invalid Zhipu API key.")
        if resp.status_code != 200:
            return ToolResult.fail(f"Error: Zhipu API returned HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        # Business-level errors (1701/1702/1703 etc.) come back as
        # {"error": {"code","message"}} even on HTTP 200.
        if isinstance(data, dict) and data.get("error"):
            err = data["error"] or {}
            return ToolResult.fail(f"Error: Zhipu returned {err.get('code')}: {err.get('message','')}")

        items = data.get("search_result") or (data.get("data") or {}).get("search_result") or []
        results = []
        for it in items:
            results.append({
                "title": it.get("title", ""),
                "url": it.get("link") or it.get("url", ""),
                "snippet": it.get("content") or it.get("snippet", ""),
                "siteName": it.get("media") or it.get("siteName", ""),
                "datePublished": it.get("publish_date") or it.get("datePublished", ""),
            })
        return ToolResult.success({
            "query": query, "backend": "zhipu",
            "total": len(results), "count": len(results), "results": results,
        })

    # ------------------------------------------------------------------
    # Qianfan (Baidu)
    # ------------------------------------------------------------------

    def _search_qianfan(self, query: str, count: int, freshness: str) -> ToolResult:
        api_key = _get_api_key("qianfan")
        api_base = (conf().get("qianfan_api_base") or "https://qianfan.baidubce.com/v2").rstrip("/")
        url = f"{api_base}/ai_search/web_search"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Appbuilder-From": "cow",
        }

        count = max(1, min(int(count or 10), 50))
        weighted_length = 0
        trimmed_chars = []
        for char in query or "":
            char_weight = 1 if ord(char) < 128 else 2
            if weighted_length + char_weight > 72:
                break
            trimmed_chars.append(char)
            weighted_length += char_weight
        trimmed_query = "".join(trimmed_chars)
        payload: Dict[str, Any] = {
            "messages": [{"role": "user", "content": trimmed_query}],
            "edition": "standard",
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": count}],
        }

        recency = {
            "oneWeek": "week",
            "oneMonth": "month",
            "oneYear": "year",
        }.get(freshness)
        if recency:
            payload["search_recency_filter"] = recency
        else:
            search_filter = self._qianfan_build_freshness_filter(freshness)
            if search_filter:
                payload["search_filter"] = search_filter

        logger.debug(f"[WebSearch] qianfan: query='{trimmed_query}', count={count}, freshness={freshness!r}")
        resp = requests.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)

        if resp.status_code == 401:
            return ToolResult.fail("Error: Invalid Qianfan API key.")
        if resp.status_code != 200:
            return ToolResult.fail(f"Error: Qianfan API returned HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        # Even on HTTP 200 Baidu surfaces business errors as {"code","message"}.
        if isinstance(data, dict) and data.get("code"):
            return ToolResult.fail(f"Error: Qianfan returned {data.get('code')}: {data.get('message','')}")

        refs = data.get("references") or []
        results = []
        for d in refs:
            results.append({
                "title": d.get("title", ""),
                "url": d.get("url", ""),
                "snippet": (d.get("content") or "")[:200],
                "siteName": d.get("web_anchor") or d.get("website") or "",
                "datePublished": d.get("date", ""),
            })
        if not results:
            return ToolResult.fail("Error: Baidu Search returned no results.")
        return ToolResult.success({
            "query": query, "backend": "qianfan",
            "total": len(results), "count": len(results), "results": results,
        })

    @staticmethod
    def _qianfan_build_freshness_filter(freshness: str) -> Optional[Dict[str, Any]]:
        if not freshness or freshness == "noLimit":
            return None
        delta_days = {"oneDay": 1, "oneWeek": 7, "oneMonth": 30, "oneYear": 365}.get(freshness)
        if not delta_days:
            return None
        from datetime import datetime, timedelta
        now = datetime.now()
        end_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        start_date = (now - timedelta(days=delta_days)).strftime("%Y-%m-%d")
        return {"range": {"page_time": {"gte": start_date, "lt": end_date}}}

    # ------------------------------------------------------------------
    # LinkAI (plugin)
    # ------------------------------------------------------------------

    def _search_linkai(self, query: str, count: int, freshness: str) -> ToolResult:
        api_key = _get_api_key("linkai")
        api_base = (conf().get("linkai_api_base") or "https://api.link-ai.tech").rstrip("/")
        url = f"{api_base}/v1/plugin/execute"

        from common.utils import get_cloud_headers
        headers = get_cloud_headers(api_key)

        payload = {"code": "web-search", "args": {"query": query, "count": count, "freshness": freshness}}
        logger.debug(f"[WebSearch] linkai: query='{query}', count={count}")
        resp = requests.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)

        if resp.status_code == 401:
            return ToolResult.fail("Error: Invalid LinkAI API key.")
        if resp.status_code != 200:
            return ToolResult.fail(f"Error: LinkAI API returned HTTP {resp.status_code}")

        data = resp.json()
        if not data.get("success"):
            msg = data.get("message") or "Unknown error"
            return ToolResult.fail(f"Error: LinkAI search failed: {msg}")

        raw = data.get("data", "")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return ToolResult.success({
                    "query": query, "backend": "linkai",
                    "total": 1, "count": 1, "results": [{"content": raw}],
                })

        if isinstance(raw, dict):
            pages = (raw.get("webPages") or {}).get("value", []) or []
            if pages:
                results = []
                for p in pages:
                    item = {
                        "title": p.get("name", ""),
                        "url": p.get("url", ""),
                        "snippet": p.get("snippet", ""),
                        "siteName": p.get("siteName", ""),
                        "datePublished": p.get("datePublished") or p.get("dateLastCrawled", ""),
                    }
                    if p.get("summary"):
                        item["summary"] = p["summary"]
                    results.append(item)
                total = (raw.get("webPages") or {}).get("totalEstimatedMatches", len(results))
                return ToolResult.success({
                    "query": query, "backend": "linkai",
                    "total": total, "count": len(results), "results": results,
                })

        return ToolResult.success({
            "query": query, "backend": "linkai",
            "total": 1, "count": 1, "results": [{"content": str(raw)}],
        })

    # ------------------------------------------------------------------
    # DDGS (keyless metasearch fallback)
    # ------------------------------------------------------------------

    def _search_ddgs(self, query: str, count: int, freshness: str) -> ToolResult:
        if DDGS is None:
            return ToolResult.fail(
                "Error: ddgs library is required for keyless search. Install with: pip install ddgs"
            )

        timelimit = {
            "oneDay": "d",
            "oneWeek": "w",
            "oneMonth": "m",
            "oneYear": "y",
        }.get(freshness)
        kwargs: Dict[str, Any] = {
            "region": (_tools_web_search_conf().get("ddgs_region") or "cn-zh").strip(),
            "safesearch": "moderate",
            "max_results": max(1, min(int(count or 10), 50)),
            # DDGS "auto" may rotate engines between calls. Bing + Brave
            # produced stable Chinese results in this deployment while DDGS
            # still owns transport, parsing, aggregation, and ranking.
            "backend": (_tools_web_search_conf().get("ddgs_backend") or "bing,brave").strip(),
        }
        if timelimit:
            kwargs["timelimit"] = timelimit

        logger.debug(
            f"[WebSearch] ddgs: query='{query}', count={kwargs['max_results']}, "
            f"freshness={freshness!r}"
        )
        try:
            rows = DDGS(timeout=DEFAULT_TIMEOUT).text(query, **kwargs) or []
        except DDGSException as exc:
            # DDGS raises for a legitimate empty result set in recent
            # versions; keep transport failures as errors.
            if "no results" not in str(exc).lower():
                raise
            logger.info("[WebSearch] DDGS returned no results query=%r", query)
            rows = []
        results = []
        for row in rows:
            title = (row.get("title") or "").strip()
            url = (row.get("href") or row.get("url") or "").strip()
            if not title or not url:
                continue
            results.append({
                "title": title,
                "url": url,
                "snippet": (row.get("body") or row.get("snippet") or "").strip(),
                "siteName": row.get("source") or (urlparse(url).hostname or ""),
                "datePublished": row.get("date") or "",
            })

        return ToolResult.success({
            "query": query,
            "backend": "ddgs",
            "total": len(results),
            "count": len(results),
            "results": results,
        })
