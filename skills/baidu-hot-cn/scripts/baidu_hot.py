#!/usr/bin/env python
"""Fetch real Baidu trending lists through the Qianfan API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import requests


API_URL = "https://qianfan.baidubce.com/v2/tools/baidu_trending"
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


def fetch_trending(tab: str, limit: int) -> dict[str, Any]:
    api_key = os.getenv("QIANFAN_API_KEY", "").strip()
    if not api_key:
        return {
            "ok": False,
            "error": "未配置 QIANFAN_API_KEY",
            "items": [],
        }

    try:
        response = requests.get(
            API_URL,
            params={"tab": tab},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=(5, 20),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
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


def format_text(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"百度热搜获取失败：{result.get('error', '未知错误')}"
    lines = [f"百度热搜（{result['tab']}）"]
    for item in result["items"]:
        score = f" · 热度 {item['hot_score']}" if item["hot_score"] else ""
        change = f" · {item['change']}" if item["change"] else ""
        lines.append(f"{item['rank']}. {item['word']}{score}{change}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="获取真实百度热搜榜")
    parser.add_argument(
        "tab",
        nargs="?",
        default="livelihood",
        choices=sorted(VALID_TABS),
        help="榜单类型，默认 livelihood",
    )
    parser.add_argument("--limit", type=int, default=10, help="返回条数，默认 10")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    result = fetch_trending(args.tab, max(1, min(args.limit, 50)))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_text(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
