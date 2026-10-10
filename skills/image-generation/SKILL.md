---
name: image-generation
description: Generate or edit images from text prompts. Use when the user asks to create, draw, design, or edit an image, illustration, photo, icon, poster, or any visual content.
metadata:
  cowagent:
    requires:
      anyEnv:
        - SKILL_IMAGE_GENERATION_API_KEY
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
- **Seedream (Volcengine Ark)** — `seedream-5.0-lite`, `seedream-4.5`
- **Qwen (DashScope)** — `qwen-image-2.0`, `qwen-image-2.0-pro`
- **MiniMax** — `image-01`

## Usage

Run `scripts/generate.py` with a JSON argument. The path is relative to this skill's `base_dir`.

```bash
python <base_dir>/scripts/generate.py '<json_args>'
```

On this Windows project, use the required interpreter `D:\Miniconda\envs\cowagent-wechat\python.exe` rather than bare `python`:

```powershell
& 'D:\Miniconda\envs\cowagent-wechat\python.exe' '<base_dir>/scripts/generate.py' '{"prompt":"A serene koi pond at sunset, ukiyo-e style.","size":"1024x1024"}'
```

The configured image provider and model are used automatically. Do not put API keys in the command, prompt, tool output, or generated script.

**Set bash timeout to at least 600 seconds**, as image generation can take 30–200s per provider, and the script may try multiple providers sequentially.

### Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `prompt` | string | yes | — | Image description |
| `model` | string | no | configured model / auto | Explicit model override; otherwise read `SKILL_IMAGE_GENERATION_MODEL` |
| `provider` | string | no | configured provider / auto | Explicit provider override; `openai` also supports compatible image endpoints |
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

The script loads `~/.cow/.env` before resolving credentials. Store real API keys there, using `env_config` when configuring them through the Agent; do not put production keys in repository JSON or logs.

For a dedicated OpenAI-compatible image service, use:

```dotenv
SKILL_IMAGE_GENERATION_API_KEY=your-image-api-key
SKILL_IMAGE_GENERATION_API_BASE=https://mediocre-new-api.midway.run/v1
SKILL_IMAGE_GENERATION_PROVIDER=openai
SKILL_IMAGE_GENERATION_MODEL=gpt-image-2
```

These settings only affect the image skill and do not change `OPENAI_API_KEY`, `OPENAI_API_BASE`, or the chat model. The base includes `/v1`; the script appends `/images/generations` and requests one image (`n=1`). A dedicated base without its dedicated key is a configuration error; never borrow a chat key for that host. A dedicated key without a base uses the official OpenAI image base.

Non-secret defaults may also be set in `config.json` under `skills.image-generation.{provider,model,api_base}`; the matching `SKILL_IMAGE_GENERATION_*` environment variables take precedence. The `api_key` template field is an optional fallback; keep it empty when using `~/.cow/.env`.

When both dedicated fields are absent, the existing provider keys remain supported:

`OPENAI_API_KEY` / `GEMINI_API_KEY` / `ARK_API_KEY` / `DASHSCOPE_API_KEY` / `MINIMAX_API_KEY` / `LINKAI_API_KEY`

Each also has an optional `*_API_BASE` for custom endpoints. Automatic routing can try configured providers in order; a pinned provider/model uses only its matching provider, and missing credentials are a configuration error. Pinning only a provider uses that provider's default model. Unknown models require an explicit provider. Missing credentials must be corrected before invoking the skill again.

Image URLs returned by the API are downloaded immediately without forwarding the generation Bearer token. The output contains local absolute paths in `images[].url`, which the existing Agent artifact and WeChat sending pipeline consume. Empty results, failed downloads, and non-image responses are failures. Do not paste signed download URLs into progress messages.

Result URLs must use HTTP/HTTPS and resolve only to public Internet addresses; local paths, private addresses, and unsafe redirects are rejected. Local paths remain supported for user-supplied editing inputs. Generated outputs accept verified PNG, JPEG, or WebP with matching extensions. OpenAI-compatible responses are fully validated before saving, and files created by a failed batch are removed.

The custom service above has only been specified for text-to-image generation. Image editing uses `/images/edits` and depends on the service supporting that endpoint; report an edit failure rather than silently generating a new image.

Seedream uses standard Volcengine Ark (`ARK_API_BASE` defaults to `https://ark.cn-beijing.volces.com/api/v3`).

### Error Handling

If the script returns an error after trying all configured backends, **do NOT retry with the same parameters** — the failure is almost always a configuration issue (wrong API key, unsupported API base). Tell the user to fix it via `env_config`, then retry.

### Notes

- HTTP timeout is 300s — high-resolution generation can take over 200s.
- Omit `quality` / `size` to let the model pick automatically (`auto`).
- Input images for editing are auto-compressed to ≤ 4MB / longest edge ≤ 4096px.
