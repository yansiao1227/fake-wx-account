---
name: pdf-reader
description: Read, extract, summarize, and analyze PDF documents. Use when the user uploads or references a PDF and asks to read it, summarize it, extract text/tables, find key points, translate content, answer questions about the PDF, or create notes from it.
---

# PDF Reader

Use this skill whenever the task involves understanding or extracting information from a PDF document.

## Requirements

- PDF parsing requires `pypdf` in the Python environment running CowAgent.
- Use the existing `read` or `web_fetch` tool for extraction. Do not install packages from inside a conversation with an ambiguous `python` command.
- If `pypdf` is unavailable, report the missing runtime dependency instead of claiming to have analyzed the PDF.

## Workflow

1. Locate the PDF file or URL provided by the user.
2. If it is a URL, use `web_fetch` first when possible.
3. If it is a local file, use `read` first; it can parse PDF text directly.
4. For scanned/image-only PDFs or pages with poor text extraction, use `vision` on page images if available, or explain that OCR is needed.
5. For long PDFs, process in chunks:
   - identify title, author/source, and date if available
   - extract the table of contents or headings
   - summarize section by section
   - then produce the requested output
6. Preserve page numbers or section names when citing evidence if the extracted text includes them.

## Common Tasks

### Summarize a PDF

Return:

- one-sentence gist
- 5-10 key points
- important conclusions
- notable data, dates, names, or claims
- any caveats or unclear parts

### Answer Questions About a PDF

Base answers only on the PDF content unless the user asks for outside knowledge. If the answer is not in the document, say so directly.

### Extract Content

For text extraction, keep the original structure where useful. For tables, output Markdown tables when the structure is clear; otherwise explain formatting limitations.

### Translate or Rewrite PDF Content

Keep meaning faithful. For long documents, translate section by section and ask before continuing if the output would be very long.

## Notes

- Do not invent page numbers, citations, or claims.
- If the PDF is too large or extraction is truncated, read it in ranges or chunks, or ask which sections matter most.
- If the user shares an article or report PDF with lasting value, also store a structured note in the knowledge wiki according to the knowledge-wiki rules.
