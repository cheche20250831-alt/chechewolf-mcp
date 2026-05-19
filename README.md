# chechewolf-mcp

MCP server that exposes a single `generate_image` tool. Internally calls
[fal.ai](https://fal.ai)'s Flux Dev with the **chechewolf LoRA v3** to generate
images of 澈澈 (chechewolf character).

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
export FAL_API_KEY=fal_...
python server.py
```

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
generate_image(scene: str, aspect: str = "portrait") -> dict

  scene: English scene description (pose, lighting, environment, composition).
         Do NOT describe character appearance — those are auto-added.
         Example: "sitting in cherry blossom park, warm afternoon light"

  aspect: "portrait" | "landscape" | "square"  (default "portrait")

  Returns: { image_url, prompt_used, markdown }
```

## Notes

- The LoRA URL is hardcoded as the default but can be overridden via the
  `CHECHE_LORA_URL` environment variable if a newer model is trained.
- Inference takes ~15-30s per image. Tool calls will block for that duration.
- Cost: ~$0.025/image via fal.ai Flux Dev.
