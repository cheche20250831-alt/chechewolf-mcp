> **2026-10-17：此服務已搬離 Zeabur，改為 Cloudflare Worker 架設（私有程式碼，需 Bearer token）。本倉庫的 Python 版保留作歷史與參考，generated_* 目錄仍持續作為鏡像儲存。**

# chechewolf-mcp

MCP server that exposes **one** image-generation tool, `generate_image_gpt`,
backed by OpenAI **gpt-image-2**.

| Tool | Engine | Best at | Lewd? |
|---|---|---|---|
| `generate_image_gpt` | OpenAI **gpt-image-2** | journals, calendars, scenes, layout, text rendering, 澈澈 solo / 澈澈+璃 duo (text anchor via `draw_cheche=True`) | ❌ moderated |

Output is mirrored to a GitHub repo for permanent URLs.

Designed to be hosted on Zeabur via Docker and consumed by Rikkahub / Operit or
any MCP-compatible client.

> **History.** 2026-05-20 launched with fal.ai Flux + a trained chechewolf LoRA;
> 2026-07-14 added gpt-image-2 and NovelAI V4.5 (three engines);
> 2026-10-09 consolidated to gpt-image-2 only — the LoRA produced near-identical
> compositions and NovelAI's anime face never matched. The fal / NAI tools were
> deleted outright; see git history before commit `74963df`'s successor if you
> ever need them back.

## What it does

`generate_image_gpt` takes a free-form prompt and:

1. Optionally prepends 澈澈's appearance anchor (`draw_cheche=True`) so the
   character stays consistent without any LoRA
2. Calls `POST /v1/images/generations` with `gpt-image-2`, `moderation=low`
3. **Mirrors the generated PNG to a GitHub repo** (permanent storage; gpt-image
   returns base64 only, no hosted URL)
4. Returns an instruction string embedding the image markdown, which the
   calling AI must echo verbatim

Images are saved to `generated_images/YYYY-MM/YYYY-MM-DD_HHMMSS_aspect_slug_hash.png`
in the configured repo.

## Setup

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...      # needs org verification for gpt-image models
export GITHUB_TOKEN=ghp_...       # Contents: Read+Write on the mirror repo
python server.py
```

Transport defaults to `streamable-http` on `/mcp`. For local Claude Desktop
testing, set `MCP_TRANSPORT=stdio`.

## Deployment (Zeabur)

1. Push this repo to GitHub — Zeabur auto-redeploys on push.
2. Zeabur auto-detects the `Dockerfile` and builds.
3. Set `OPENAI_API_KEY`, `GITHUB_TOKEN` and `MCP_TOKEN` in the service's "環境變數" tab.
   **After adding or changing a variable, manually Restart the service** —
   a redeploy alone does not pick up new env vars.
4. The MCP endpoint is served at `/mcp` on the service's Zeabur domain.
   **Every request must carry `Authorization: Bearer <MCP_TOKEN>`; anything else gets 401**
   (added 2026-10-16 after the endpoint was abused while unauthenticated).
   If `MCP_TOKEN` is unset the server starts but rejects everything — by design.

## Integration

### Option A: Rikkahub / Operit
- Add new MCP server (streamable HTTP), URL `https://<your-zeabur-domain>/mcp`,
  and add a custom header `Authorization: Bearer <MCP_TOKEN>`.

### Option B: MetaMCP
- "添加服务器" → type `STREAMABLE_HTTP`, URL as above, same `Authorization` header.

## Tool spec

```
generate_image_gpt(prompt, aspect="square", quality="high", num_images=1,
                   draw_cheche=False, draw_li=False, li_form="human")
  gpt-image-2. Freeform prompt. draw_cheche=True prepends a natural-language
  appearance anchor for 澈澈 (silver-white short messy hair, silver eyes, wolf
  ears + tail, pale skin, tall slim mature man); put the art style in the prompt.
  draw_li=True adds 璃 (forces draw_cheche=True — she is never drawn alone):
  the only fixed trait is deep brown eyes, hair/outfit come from the prompt;
  li_form="cat" draws her as a fluffy long-haired black cat in Cheche's arms.
  aspect → 1024x1536 / 1536x1024 / 1024x1024. Moderated (no NSFW).
```

## Notes

- Inference takes ~20-60s per image at `quality=high`. Tool calls block for that duration.
- Explicit content and "school uniform + romantic couple" prompts are rejected
  by OpenAI with 400 — don't retry, just tell the user.
- `requirements.txt` pins `mcp[cli]==1.27.2` on purpose (2026-06-12 incident:
  an unpinned alpha broke imports and the container crash-looped).
