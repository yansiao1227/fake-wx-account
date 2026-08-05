---
name: image-generation
description: Generate or edit images from text prompts. Use when the user asks to create, draw, design, or edit an image, illustration, photo, icon, poster, or any visual content.
metadata:
  cowagent:
    requires:
      anyEnv:
        - OPENAI_API_KEY
        - GEMINI_API_KEY
        - ARK_API_KEY
        - DASHSCOPE_API_KEY
        - MINIMAX_API_KEY
        - LINKAI_API_KEY
---

# Image Generation

Generate and edit images using AI models. The script automatically picks a backend based on which API keys are configured — **you don't need to specify a model unless the user explicitly names one**.

Supported models (passed via `model` only when the user asks for a specific one):

- **OpenAI** — `gpt-image-2`, `gpt-image-1`
- **Gemini Nano Banana** — `nano-banana-2`, `nano-banana-pro`, `nano-banana`
- **Seedream (Volcengine Ark / Agent Plan)** — `seedream-5.0-lite` (Agent Plan), `seedream-4.5` (standard Ark only)
- **Qwen (DashScope)** — `qwen-image-2.0`, `qwen-image-2.0-pro`
- **MiniMax** — `image-01`

## Usage

Run `scripts/generate.py` with a JSON argument. The path is relative to this skill's `base_dir`.

```bash
python <base_dir>/scripts/generate.py '<json_args>'
```

**Set bash timeout to at least 600 seconds**, as image generation can take 30–200s per provider, and the script may try multiple providers sequentially.

### Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `prompt` | string | yes | — | Image description |
| `image_url` | string / list | no | null | Input image(s) for editing: local file path or URL. Multi-image fusion is supported (pass a list) |
| `quality` | string | no | auto | `low` / `medium` / `high` (only some backends honour this) |
| `size` | string | no | auto | `512` / `1K` / `2K` / `3K` / `4K`, or pixel value (`1024x1024`) |
| `aspect_ratio` | string | no | null | `1:1` / `3:2` / `2:3` / `16:9` / `9:16` / `21:9` (some backends also support extreme ratios like `1:4` / `8:1`) |

**Higher `quality` and larger `size` cost more and run slower.** In normal cases, when the user does not explicitly specify, `low` or `medium` is sufficient. Only use `high` when the user asks for it.

### Example — generate

```bash
python <base_dir>/scripts/generate.py '{"prompt": "A corgi astronaut floating in space"}'
```

With aspect ratio:

```bash
python <base_dir>/scripts/generate.py '{"prompt": "Isometric miniature city of Shanghai at sunset", "size": "2K", "aspect_ratio": "16:9"}'
```

### Important: Editing vs Generating

When the user asks to **edit, modify, or improve an existing image**, pass the original image via `image_url`. Prefer **local file paths** directly — the script handles file reading internally. Without `image_url`, the script generates a brand-new image instead of editing.

### Example — edit (image-to-image)

```bash
python <base_dir>/scripts/generate.py '{"prompt": "Add a Santa hat to the dog", "image_url": "/path/to/dog.png"}'
```

Multi-image fusion — pass a list:

```bash
python <base_dir>/scripts/generate.py '{"prompt": "Combine these characters into a group photo", "image_url": ["/path/a.png", "/path/b.png"]}'
```

### Output

Prints JSON to stdout:

```json
{
  "model": "doubao-seedream-5-0-260128",
  "images": [
    {"url": "/path/to/output.png"}
  ]
}
```

After success, display the image to the user. You can either embed it in markdown (`![description](/path/to/output.png)`) or use the `send` tool.

On error:

```json
{
  "error": "error message"
}
```

### Setup

The script needs **at least one** of these API keys (set via `env_config` or `config.json`):

`OPENAI_API_KEY` / `GEMINI_API_KEY` / `ARK_API_KEY` / `DASHSCOPE_API_KEY` / `MINIMAX_API_KEY` / `LINKAI_API_KEY`

Each also has an optional `*_API_BASE` for custom endpoints. The script automatically picks the first configured backend and falls back to the next if it fails — no need to specify a model.

**Volcengine Agent Plan (企业版订阅)**：

- Key: `ARK_API_KEY`（也兼容 `AGENT_PLAN_API_KEY`）
- Base: `ARK_API_BASE=https://ark.cn-beijing.volces.com/api/plan/v3`
- 全路径：`POST {ARK_API_BASE}/images/generations`
- Plan 侧当前兼容模型：`doubao-seedream-5-0-lite`（技能别名 `seedream-5.0-lite`）
- 不要用标准 Ark 的带日期 Model ID（如 `doubao-seedream-5-0-260128`），Plan 会返回 `UnsupportedModel`

### Error Handling

If the script returns an error after trying all configured backends, **do NOT retry with the same parameters** — the failure is almost always a configuration issue (wrong API key, unsupported API base). Tell the user to fix it via `env_config`, then retry.

### Notes

- HTTP timeout is 300s — high-resolution generation can take over 200s.
- Omit `quality` / `size` to let the model pick automatically (`auto`).
- Input images for editing are auto-compressed to ≤ 4MB / longest edge ≤ 4096px.
