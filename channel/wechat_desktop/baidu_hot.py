"""Baidu trending helpers for wechat_desktop daily hot broadcast.

Flow:
1. Pull top-1 item from Qianfan ``baidu_trending``.
2. Pull detail snippets for that word via Qianfan Baidu search
   (``ai_search/web_search`` + ``baidu_search_v2``).
3. Summarize with a fun personal commentary via the project's chat model
   (OpenAI-compatible ``custom_api_*`` preferred; Qianfan as fallback).
4. Format a group message that always ends with the original hot link.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import requests

from common.log import logger
from common.utils import expand_path
from config import conf

TRENDING_API_URL = "https://qianfan.baidubce.com/v2/tools/baidu_trending"
SEARCH_API_URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"
VALID_TABS = {
    "livelihood",
    "finance",
    "sports",
    "new_entertainment",
    "internation_news",
    "challenge",
    "movie",
    "teleplay",
    "novel",
}

_COW_ENV_LOADED = False

SUMMARY_SYSTEM_PROMPT = (
    "你是一个爱看热闹、说话有趣的群聊播报员。"
    "根据热搜词和检索资料写群聊短讯：先概括事实，再补上你自己轻松的评论和思考。"
    "只输出正文，不要标题、不要分点、不要链接、不要自我介绍。"
)

SUMMARY_USER_PROMPT = """请根据下面材料写一段今日热点群聊文案。

要求：
1. 前半段用 2-4 句概括事件来龙去脉与关键事实，务必依据资料，禁止编造。
2. 后半段用 1-2 句加上你自己的趣味评论或思考（可吐槽、共鸣、反问），语气轻松，不油腻、不引战。
3. 全文中文，约 120-220 字；不要标题、不要列表、不要输出任何链接或“链接：”。
4. 不要出现“根据资料”“作为 AI”等元话语。

热搜词：{word}

检索资料：
{materials}
"""


def _ensure_cow_env_loaded() -> None:
    """Load ``~/.cow/.env`` once so channel code can read secrets without Agent."""
    global _COW_ENV_LOADED
    if _COW_ENV_LOADED:
        return
    _COW_ENV_LOADED = True
    env_file = expand_path("~/.cow/.env")
    if not os.path.isfile(env_file):
        return
    try:
        from dotenv import load_dotenv

        # Do not override keys already present in the process environment.
        load_dotenv(env_file, override=False)
    except ImportError:
        logger.warning(
            "[WechatDesktop][BaiduHot] python-dotenv not installed; "
            "cannot load ~/.cow/.env"
        )
    except Exception as exc:
        logger.warning(
            "[WechatDesktop][BaiduHot] failed to load ~/.cow/.env: %s", exc
        )


def resolve_qianfan_api_key() -> str:
    """Resolve Qianfan key: ``~/.cow/.env`` / env first, then config fallback.

    Production secrets live in ``~/.cow/.env`` as ``QIANFAN_API_KEY`` (this
    workstation: ``C:\\Users\\<user>\\.cow\\.env``). ``config.json`` may keep an
    empty placeholder only.
    """
    _ensure_cow_env_loaded()
    key = str(os.environ.get("QIANFAN_API_KEY", "") or "").strip()
    if key:
        return key
    return str(conf().get("qianfan_api_key") or "").strip()


def fetch_trending(
    tab: str = "livelihood",
    limit: int = 1,
    *,
    api_key: Optional[str] = None,
    timeout: tuple[float, float] = (5.0, 20.0),
) -> dict[str, Any]:
    """Fetch Baidu trending list via Qianfan ``baidu_trending`` API."""
    tab = str(tab or "livelihood").strip() or "livelihood"
    if tab not in VALID_TABS:
        return {
            "ok": False,
            "error": f"unsupported baidu hot tab: {tab}",
            "items": [],
        }

    key = (api_key if api_key is not None else resolve_qianfan_api_key()).strip()
    if not key:
        return {
            "ok": False,
            "error": "未配置 QIANFAN_API_KEY（请写入 ~/.cow/.env）",
            "items": [],
        }

    limit = max(1, min(int(limit or 1), 50))
    try:
        response = requests.get(
            TRENDING_API_URL,
            params={"tab": tab},
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        logger.warning("[WechatDesktop][BaiduHot] request failed: %s", exc)
        return {"ok": False, "error": f"百度热搜请求失败：{exc}", "items": []}
    except ValueError:
        return {"ok": False, "error": "百度热搜返回了无效 JSON", "items": []}

    if str(payload.get("code", "")) != "0":
        return {
            "ok": False,
            "error": payload.get("message") or "百度热搜 API 返回错误",
            "request_id": payload.get("requestId"),
            "items": [],
        }

    items = []
    for item in payload.get("data", [])[:limit]:
        items.append(
            {
                "rank": int(item.get("index", len(items))) + 1,
                "word": item.get("word") or item.get("query") or "",
                "query": item.get("query") or "",
                "hot_score": item.get("hotScore") or "",
                "change": item.get("hotChange") or "",
                "tag": item.get("hotTag") or "",
                "description": item.get("desc") or "",
                "url": item.get("url") or "",
            }
        )
    return {"ok": True, "tab": tab, "count": len(items), "items": items}


def _trim_search_query(query: str, max_weight: int = 72) -> str:
    """Match agent web_search Qianfan limit: CJK ~2 weight, ASCII ~1."""
    weighted_length = 0
    chars: list[str] = []
    for char in str(query or ""):
        weight = 1 if ord(char) < 128 else 2
        if weighted_length + weight > max_weight:
            break
        chars.append(char)
        weighted_length += weight
    return "".join(chars).strip()


def fetch_hot_detail(
    word: str,
    *,
    count: int = 5,
    api_key: Optional[str] = None,
    timeout: tuple[float, float] = (5.0, 25.0),
) -> dict[str, Any]:
    """Fetch detail snippets for a hot word via Qianfan Baidu search."""
    query = _trim_search_query(word)
    if not query:
        return {"ok": False, "error": "热搜词为空，无法检索详情", "references": []}

    key = (api_key if api_key is not None else resolve_qianfan_api_key()).strip()
    if not key:
        return {
            "ok": False,
            "error": "未配置 QIANFAN_API_KEY（请写入 ~/.cow/.env）",
            "references": [],
        }

    count = max(1, min(int(count or 5), 10))
    payload = {
        "messages": [{"role": "user", "content": query}],
        "edition": "standard",
        "search_source": "baidu_search_v2",
        "resource_type_filter": [{"type": "web", "top_k": count}],
        "search_recency_filter": "week",
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Appbuilder-From": "cow",
    }
    try:
        response = requests.post(
            SEARCH_API_URL,
            headers=headers,
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.warning("[WechatDesktop][BaiduHot] detail search failed: %s", exc)
        return {"ok": False, "error": f"热点详情检索失败：{exc}", "references": []}
    except ValueError:
        return {"ok": False, "error": "热点详情返回了无效 JSON", "references": []}

    if isinstance(data, dict) and data.get("code"):
        return {
            "ok": False,
            "error": f"热点详情 API 错误：{data.get('code')}: {data.get('message') or ''}",
            "references": [],
        }

    references: list[dict[str, str]] = []
    for ref in data.get("references") or []:
        if not isinstance(ref, dict):
            continue
        title = str(ref.get("title") or "").strip()
        content = str(
            ref.get("content") or ref.get("snippet") or ""
        ).strip()
        url = str(ref.get("url") or "").strip()
        date = str(ref.get("date") or "").strip()
        site = str(ref.get("website") or ref.get("web_anchor") or "").strip()
        if not (title or content):
            continue
        references.append(
            {
                "title": title,
                "content": content[:800],
                "url": url,
                "date": date,
                "site": site,
            }
        )

    if not references:
        return {
            "ok": False,
            "error": "热点详情检索结果为空",
            "references": [],
            "query": query,
        }
    return {
        "ok": True,
        "error": "",
        "query": query,
        "count": len(references),
        "references": references,
    }


def _build_materials(item: dict[str, Any], references: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    desc = str(item.get("description") or "").strip()
    if desc:
        chunks.append(f"[榜单简介] {desc}")
    for idx, ref in enumerate(references[:5], start=1):
        title = str(ref.get("title") or "").strip()
        content = str(ref.get("content") or "").strip()
        site = str(ref.get("site") or "").strip()
        date = str(ref.get("date") or "").strip()
        meta = " / ".join(part for part in (site, date) if part)
        head = f"[{idx}] {title}" if title else f"[{idx}]"
        if meta:
            head = f"{head}（{meta}）"
        body = content[:420] if content else ""
        chunks.append(f"{head}\n{body}".strip())
    return "\n\n".join(chunks).strip()


def resolve_chat_endpoint() -> dict[str, str]:
    """Resolve OpenAI-compatible chat endpoint for fun summarization."""
    cfg = conf()
    base = str(cfg.get("custom_api_base") or "").strip().rstrip("/")
    key = str(cfg.get("custom_api_key") or "").strip()
    model = str(cfg.get("model") or "").strip()
    if base and key:
        return {"api_base": base, "api_key": key, "model": model or "gpt-4o-mini"}

    qianfan_key = resolve_qianfan_api_key()
    if qianfan_key:
        q_base = str(
            cfg.get("qianfan_api_base") or "https://qianfan.baidubce.com/v2"
        ).strip().rstrip("/")
        q_model = str(cfg.get("qianfan_model") or "ernie-4.0-8k").strip()
        return {"api_base": q_base, "api_key": qianfan_key, "model": q_model}
    return {}


def _extract_chat_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(str(block.get("text") or ""))
                    elif isinstance(block, str):
                        parts.append(block)
                return "".join(parts).strip()
    # Some providers return top-level content.
    content = payload.get("content")
    if isinstance(content, str):
        return content.strip()
    return ""


def summarize_hot_with_commentary(
    word: str,
    materials: str,
    *,
    timeout: tuple[float, float] = (5.0, 45.0),
) -> dict[str, Any]:
    """Ask the chat model for a fun factual summary + personal commentary."""
    word = str(word or "").strip()
    materials = str(materials or "").strip()
    if not word:
        return {"ok": False, "error": "热搜词为空", "text": ""}
    if not materials:
        return {"ok": False, "error": "缺少检索资料，无法概括", "text": ""}

    endpoint = resolve_chat_endpoint()
    if not endpoint:
        return {
            "ok": False,
            "error": "未配置可用的对话模型（custom_api_* 或 QIANFAN_API_KEY）",
            "text": "",
        }

    url = f"{endpoint['api_base']}/chat/completions"
    body = {
        "model": endpoint["model"],
        "temperature": 0.7,
        "max_tokens": 450,
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": SUMMARY_USER_PROMPT.format(
                    word=word,
                    materials=materials[:4500],
                ),
            },
        ],
    }
    headers = {
        "Authorization": f"Bearer {endpoint['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(url, headers=headers, json=body, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        logger.warning("[WechatDesktop][BaiduHot] summary LLM failed: %s", exc)
        return {"ok": False, "error": f"热点概括失败：{exc}", "text": ""}
    except ValueError:
        return {"ok": False, "error": "热点概括返回了无效 JSON", "text": ""}

    if isinstance(payload, dict) and payload.get("error"):
        err = payload.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or str(err)
        else:
            msg = str(err)
        return {"ok": False, "error": f"热点概括 API 错误：{msg}", "text": ""}

    text = _extract_chat_content(payload if isinstance(payload, dict) else {})
    text = _sanitize_summary_text(text)
    if not text:
        return {"ok": False, "error": "热点概括结果为空", "text": ""}
    return {"ok": True, "error": "", "text": text}


def _sanitize_summary_text(text: str) -> str:
    """Drop accidental link lines the model may still emit."""
    lines = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        lower = line.lower()
        if lower.startswith("链接") or lower.startswith("http://") or lower.startswith(
            "https://"
        ):
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    # Collapse excessive blank lines.
    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")
    return cleaned.strip()


def fallback_summary_from_materials(
    word: str,
    references: list[dict[str, Any]],
    *,
    description: str = "",
) -> str:
    """Rule-based fallback when the chat model is unavailable."""
    pieces: list[str] = []
    desc = str(description or "").strip()
    if desc:
        pieces.append(desc[:180])
    for ref in references[:2]:
        content = str(ref.get("content") or "").strip()
        title = str(ref.get("title") or "").strip()
        snippet = content or title
        if snippet:
            pieces.append(snippet[:160])
        if sum(len(p) for p in pieces) >= 220:
            break
    body = "；".join(p for p in pieces if p).strip("； ")
    if not body:
        body = f"「{word}」正在热搜榜上趴着刷存在感，细节还在路上。"
    comment = (
        f"我的碎碎念：热点这东西，越吵越值得多看一眼来龙去脉——"
        f"「{word}」今天就先记一笔，别只看标题就站队。"
    )
    return f"{body}\n{comment}".strip()


def format_top_item_message(
    item: dict[str, Any],
    *,
    prefix: str = "📰 今日热点",
    body: str = "",
) -> str:
    """Format the daily hot broadcast text.

    Layout::

        📰 今日热点
        1. 标题
        热度 xxx · 趋势
        <概括 + 趣味评论>
        链接：...
    """
    if not item:
        return ""
    lines: list[str] = []
    title = str(prefix or "").strip()
    if title:
        lines.append(title)

    rank = item.get("rank") or 1
    word = str(item.get("word") or item.get("query") or "").strip()
    if word:
        lines.append(f"{rank}. {word}")

    meta_parts = []
    hot_score = str(item.get("hot_score") or "").strip()
    change = str(item.get("change") or "").strip()
    if hot_score:
        meta_parts.append(f"热度 {hot_score}")
    if change:
        meta_parts.append(change)
    if meta_parts:
        lines.append(" · ".join(meta_parts))

    body_text = str(body or "").strip()
    if not body_text:
        # Backward-compatible: use list description when no composed body.
        body_text = str(item.get("description") or "").strip()
    if body_text:
        lines.append("")
        lines.append(body_text)

    url = str(item.get("url") or "").strip()
    if url:
        lines.append("")
        lines.append(f"链接：{url}")

    return "\n".join(lines).strip()


def build_daily_hot_message(
    tab: str = "livelihood",
    *,
    prefix: str = "📰 今日热点",
    api_key: Optional[str] = None,
    detail_count: int = 5,
) -> dict[str, Any]:
    """Fetch top hot, enrich with Baidu search detail, summarize, keep link."""
    result = fetch_trending(tab=tab, limit=1, api_key=api_key)
    if not result.get("ok"):
        return {
            "ok": False,
            "error": result.get("error") or "百度热搜获取失败",
            "message": "",
            "item": None,
            "tab": tab,
            "summary_source": "",
        }
    items = list(result.get("items") or [])
    if not items:
        return {
            "ok": False,
            "error": "百度热搜今日榜单为空",
            "message": "",
            "item": None,
            "tab": result.get("tab") or tab,
            "summary_source": "",
        }
    item = items[0]
    word = str(item.get("word") or item.get("query") or "").strip()
    if not word:
        return {
            "ok": False,
            "error": "百度热搜首条词条为空",
            "message": "",
            "item": item,
            "tab": result.get("tab") or tab,
            "summary_source": "",
        }

    detail = fetch_hot_detail(word, count=detail_count, api_key=api_key)
    references = list(detail.get("references") or []) if detail.get("ok") else []
    if not detail.get("ok"):
        logger.warning(
            "[WechatDesktop][BaiduHot] detail fetch failed word=%s error=%s",
            word,
            detail.get("error"),
        )

    materials = _build_materials(item, references)
    summary_source = ""
    body = ""
    if materials:
        summary = summarize_hot_with_commentary(word, materials)
        if summary.get("ok") and summary.get("text"):
            body = str(summary["text"]).strip()
            summary_source = "llm"
        else:
            logger.warning(
                "[WechatDesktop][BaiduHot] summary failed word=%s error=%s",
                word,
                summary.get("error"),
            )

    if not body:
        body = fallback_summary_from_materials(
            word,
            references,
            description=str(item.get("description") or ""),
        )
        summary_source = "fallback"

    message = format_top_item_message(item, prefix=prefix, body=body)
    if not message:
        return {
            "ok": False,
            "error": "百度热搜首条文案为空",
            "message": "",
            "item": item,
            "tab": result.get("tab") or tab,
            "summary_source": summary_source,
            "detail_ok": bool(detail.get("ok")),
            "detail_error": detail.get("error") or "",
        }
    return {
        "ok": True,
        "error": "",
        "message": message,
        "item": item,
        "tab": result.get("tab") or tab,
        "summary_source": summary_source,
        "detail_ok": bool(detail.get("ok")),
        "detail_error": detail.get("error") or "",
        "detail_count": len(references),
    }
