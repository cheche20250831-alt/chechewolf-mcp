# chechewolf-mcp

MCP server that exposes **three** image-generation tools, each backed by a
different engine so they cover different jobs:

| Tool | Engine | Best at | Lewd? |
|---|---|---|---|
| `generate_image` | fal.ai Flux + **chechewolf LoRA v3** | 澈澈's face, most accurate likeness | ✅ safety off |
| `generate_image_gpt` | OpenAI **gpt-image-2** | journals, calendars, scenes, layout, text rendering — general purpose | ❌ moderated |
| `generate_image_nai` | **NovelAI Diffusion V4.5** | anime style + NSFW (character anchor is text-based) | ✅ |

All three mirror output to a GitHub repo for permanent URLs.

Designed to be hosted on Zeabur via Docker and consumed by Rikkahub or any
MCP-compatible client.

## What it does

The `generate_image` tool takes a scene description and:

1. Wraps it with a character-anchoring prompt template (locks hair color, ear
   count, jawline, illustration style — fixes known LoRA quirks)
2. Calls `fal-ai/flux-lora` with the trained chechewolf LoRA at scale 0.95
3. **Mirrors the generated image to a GitHub repo** (permanent storage; fal.ai
   CDN URLs may expire over time)
4. Returns both URLs plus a ready-to-embed markdown snippet pointing at the
   permanent GitHub URL

Images are saved to `generated_images/YYYY-MM/YYYY-MM-DD_HHMMSS_aspect_slug_hash.jpg`
in the configured repo.

## Setup

```bash
pip install -r requirements.txt
export FAL_API_KEY=fal_...        # generate_image
export OPENAI_API_KEY=sk-...      # generate_image_gpt (needs org verification for gpt-image models)
export NOVELAI_TOKEN=pst-...      # generate_image_nai (NovelAI subscription, persistent token)
python server.py
```

Each tool is independently gated on its key: if a key is missing, only that tool
errors with a clear message — the others keep working.

By default it runs in `sse` transport mode. For local Claude Desktop testing,
set `MCP_TRANSPORT=stdio`.

## Deployment (Zeabur)

1. Push this repo to GitHub.
2. In Zeabur dashboard, create a new service in your project, connect the GitHub repo.
3. Zeabur auto-detects the `Dockerfile` and builds.
4. Set environment variable `FAL_API_KEY` in the service's "環境變數" tab.
5. Once deployed, Zeabur assigns a public URL like `chechewolf-mcp.zeabur.app`.
6. The SSE endpoint will be at that URL (typically `/sse` path — check Zeabur logs).

## Integration

### Option A: Paste SSE URL directly into Rikkahub
- Open Rikkahub MCP settings.
- Add new MCP server, paste the Zeabur SSE URL.
- Toggle on when needed.

### Option B: Add to MetaMCP
- In your MetaMCP UI, "添加服务器" → type `STREAMABLE_HTTP`.
- URL: your Zeabur deployment.
- Rikkahub then connects to MetaMCP and gets this tool alongside other MCPs.

## Tool spec

```
generate_image(scene, aspect="portrait", shot="full", num_images=2)
  fal.ai + LoRA. 澈澈's face, most accurate. Do NOT describe appearance
  (auto-added); use `shot` for framing (full/wide/upper/close/auto).

generate_image_gpt(prompt, aspect="square", quality="high", num_images=1)
  gpt-image-2. Freeform prompt, NO character lock. For journals/calendars/
  scenes/text. Moderated (no NSFW). aspect → 1024x1536 / 1536x1024 / 1024x1024.

generate_image_nai(scene, aspect="portrait", draw_cheche=True, num_images=1)
  NovelAI V4.5. Anime style, NSFW-friendly. draw_cheche=True prepends a
  danbooru text anchor for 澈澈; False = freeform. Use danbooru-style tags.

All three return an instruction string embedding the image markdown.
```

## Notes

- The LoRA URL is hardcoded as the default but can be overridden via the
  `CHECHE_LORA_URL` environment variable if a newer model is trained.
- Inference takes ~15-30s per image. Tool calls will block for that duration.
- Cost: ~$0.025/image via fal.ai Flux Dev.
