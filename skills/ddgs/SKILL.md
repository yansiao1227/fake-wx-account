---
name: ddgs
description: Search the web and find images, news, videos, or books with DDGS, or extract content from a URL. Use for DDGS-specific search categories and extraction; prefer the project's web_search tool for ordinary text search, and always cite source URLs when results inform an answer.
---

# DDGS — Web Search for Agents

This skill is based on the official DDGS skill, with project-specific routing and citation rules added.

## Project integration

- Use the project's `web_search` tool for ordinary text searches. It already uses DDGS as a keyless fallback and returns normalized titles, URLs, and snippets.
- Use the DDGS Python API or CLI directly for images, news, videos, books, or URL extraction that `web_search` does not expose.
- Whenever search or extracted web content informs an answer, cite the supporting source URLs as clickable links. Do not present search-derived factual claims without citations.
- Prefer primary and authoritative sources. Open a result when its snippet alone is insufficient to support a claim.

## Python API

```python
from ddgs import DDGS

# Text search
results = DDGS().text("python async", max_results=5)

# Images, news, videos, books
images = DDGS().images("butterfly", max_results=5)
news = DDGS().news("ai regulation", timelimit="w")
videos = DDGS().videos("rust programming")
books = DDGS().books("machine learning")

# Extract content from a URL
page = DDGS().extract("https://example.com")
page["content"]
page = DDGS().extract("https://example.com", fmt="text_plain")
page = DDGS().extract("https://example.com", fmt="content")
```

All search methods return `list[dict[str, Any]]`. Keys depend on category:

| Method | Keys |
| --- | --- |
| `text()` | `title`, `href`, `body` |
| `images()` | `title`, `image`, `thumbnail`, `url`, `height`, `width`, `source` |
| `videos()` | `title`, `content`, `description`, `duration`, `embed_url`, `images`, `publisher` |
| `news()` | `date`, `title`, `body`, `url`, `image`, `source` |
| `books()` | `title`, `author`, `publisher`, `info`, `url`, `thumbnail` |

## CLI

```bash
ddgs text -q "python async" --max_results 5
ddgs images -q "cats" --color Blue
ddgs news -q "tech" --timelimit d
ddgs videos -q "cars" --duration medium
ddgs books -q "sea wolf"
ddgs extract -u https://example.com
ddgs extract -u https://example.com -f text_plain
ddgs text -q "dogs" -o results.json
ddgs text -q "dogs" -o results.csv
ddgs version
```

The CLI auto-detects non-TTY use and emits plain text without interactive pauses. Discover options with:

```bash
ddgs --help
ddgs text --help
ddgs images --help
```

## MCP server

```bash
pip install "ddgs[mcp]"
ddgs mcp
ddgs mcp -pr socks5h://127.0.0.1:9150
```

Available MCP tools are `search_text`, `search_images`, `search_news`, `search_videos`, `search_books`, and `extract_content`.

## Quick reference

| Parameter | Values | Default |
| --- | --- | --- |
| `region` | `us-en`, `uk-en`, `ru-ru`, `de-de`, etc. | `us-en` |
| `safesearch` | `on`, `moderate`, `off` | `moderate` |
| `timelimit` | `d`, `w`, `m`, `y` | None |
| `max_results` | integer | 10 |
| `page` | integer | 1 |
| `backend` | `auto`, `all`, or engine name | `auto` |

Available backends by method:

| Method | Backends |
| --- | --- |
| `text()` | `bing`, `brave`, `duckduckgo`, `google`, `grokipedia`, `mojeek`, `startpage`, `yandex`, `yahoo`, `wikipedia` |
| `images()` | `bing`, `duckduckgo` |
| `videos()` | `duckduckgo` |
| `news()` | `bing`, `duckduckgo`, `yahoo` |
| `books()` | `annasarchive` |

## Operational tips

- Set a proxy with `DDGS(proxy="socks5h://127.0.0.1:9150")` or `DDGS_PROXY`.
- Configure SSL verification with `DDGS(verify=False)` or a CA path.
- Configure timeout with `DDGS(timeout=10)`; the library default is 5 seconds.
- Catch `DDGSException`, `RatelimitException`, and `TimeoutException`.
- Pass multiple backends as a comma-separated string, such as `backend="bing,google"`.
