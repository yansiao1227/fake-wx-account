---
name: analyze-url
description: "Analyze an HTTP or HTTPS URL, decide whether it points to a downloadable file or a web page, and route it appropriately. Use whenever the user provides a URL to open, read, inspect, summarize, or extract content from: download files into the workspace tmp directory and parse them with a suitable tool; fetch non-file web pages with web_fetch."
---

# Analyze URL

Route every URL through the following workflow.

## 1. Classify the URL

1. Accept only `http://` or `https://` URLs. Do not expose credentials embedded in a URL.
2. Inspect the decoded final URL path. Treat a clear filename extension as a file hint, not conclusive proof.
3. For an ambiguous URL, use `bash` with `curl -sSIL --max-redirs 10 '<url>'` to inspect the final response headers. Shell-escape the URL as untrusted input. If the server rejects `HEAD` or omits useful headers, make a minimal GET request that preserves response headers.
4. Follow redirects when classifying. Prefer the final response's headers over the original URL.
5. Classify the response as a file when any strong signal exists:
   - `Content-Disposition` contains `attachment` or supplies a filename;
   - `Content-Type` is a document, archive, image, audio, video, font, or generic binary type;
   - the final URL has a file extension and the response is not HTML.
6. Classify it as a web page when the final `Content-Type` is `text/html` or `application/xhtml+xml`. Treat JSON/XML API responses as web content unless they are attachments or clearly named downloads.
7. If signals conflict, trust `Content-Disposition`, then `Content-Type`, then the URL suffix. Do not download the same response twice merely to reconfirm its type.

## 2. Handle a file

1. Create or reuse the workspace-relative `tmp/` directory.
2. Derive a safe filename from `Content-Disposition` or the final URL. Remove path components and unsafe characters, and add a short unique prefix to prevent overwrites. If no name is available, use `downloaded-file` plus an extension inferred from `Content-Type` when possible.
3. Download with `bash` and `curl --fail --location` into `tmp/<safe-name>`. Shell-escape both the URL and destination as untrusted input. Apply a reasonable timeout and size limit. Never overwrite an existing file.
4. Confirm the file exists and is non-empty before parsing it.
5. Select the best available tool for the local file:
   - PDF, Word, Excel, PowerPoint, plain text, Markdown, CSV, or similar documents: call `read` first.
   - Images: call `vision` when visual understanding or OCR is needed; otherwise report metadata from `read`.
   - Archives: inspect with an appropriate archive command through `bash`; extract only when needed and keep extraction under `tmp/`.
   - Other formats: use a purpose-built available tool when one exists, otherwise call `read` for metadata and explain the limitation.
6. If the first parser fails, try one reasonable fallback suited to the detected file type. Keep the downloaded file in `tmp/` and report its path even when parsing fails.
7. Base the answer on parsed content, not the filename alone.

## 3. Handle a web page

1. Call `web_fetch` with the final URL.
2. Use the fetched title and content to answer the user's request.
3. If `web_fetch` reveals that the response is actually a downloadable file, switch to the file workflow and do not keep treating it as HTML.

## Guardrails

- Do not fetch private, loopback, link-local, cloud-metadata, or otherwise unsafe network targets.
- Do not execute downloaded programs, scripts, macros, or embedded code.
- Do not install new parsers unless the user explicitly asks. Report unsupported or encrypted files clearly.
- Preserve the source URL in the final response when it helps traceability.
