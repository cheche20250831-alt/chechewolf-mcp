"""
chechewolf-mcp · MCP server that exposes a `generate_image` tool.
Calls fal.ai/fal-ai/flux-lora with the chechewolf LoRA v3 + character anchor prompt template.
After generation, mirrors the image to a GitHub repo so the URL is permanent
(fal.ai CDN may expire over time).
Designed to be hosted on Zeabur via Docker and consumed by Rikkahub (or any MCP client).
"""
import os
import sys
import base64
import hashlib
import logging
from datetime import datetime, timezone, timedelta

import httpx
from mcp.server.fastmcp import FastMCP

# ============ logging ============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("chechewolf-mcp")

# ============ config ============
CHECHE_LORA_URL = os.environ.get(
    "CHECHE_LORA_URL",
    "https://v3b.fal.media/files/b/0a9abe9f/r_hDzeZzvIsf_DdITgnRe_pytorch_lora_weights.safetensors",
)
FAL_API_KEY = os.environ.get("FAL_API_KEY")
FAL_ENDPOINT = "https://fal.run/fal-ai/flux-lora"

# GitHub 鏡像設定(讓圖永久保存,不依賴 fal.ai CDN)
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "cheche20250831-alt")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "my-ai-memory")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # 需要 Contents: Read+Write
GITHUB_IMAGE_DIR = os.environ.get("GITHUB_IMAGE_DIR", "generated_images")

# 角色錨點 prompt template
# {scene} 由對面 AI 寫的場景描述填入,其他全部由我們鎖住:
#   - 觸發詞 chechewolf
#   - 解決耳朵數量
#   - 鎖住髮色(訓練時 caption 不一致留下的副作用)
#   - 鎖住成熟感(避免 LoRA 軟場景幼齡漂移)
#   - 風格鎖定
PROMPT_TEMPLATE = (
    "chechewolf, single character, exactly two pointed wolf ears on top of head, "
    "short messy silver white hair, mature young adult man with sharp angular features "
    "and defined jawline, {scene}, semi-realistic anime illustration, "
    "no watermark, no signature, no text"
)

ASPECT_TO_SIZE = {
    "portrait": "portrait_16_9",
    "landscape": "landscape_16_9",
    "square": "square_hd",
}

# ============ GitHub 鏡像 ============

async def mirror_to_github(image_bytes: bytes, aspect: str, scene_hint: str) -> str | None:
    """把圖推到 GitHub repo,回傳 raw URL。失敗回 None(不阻塞主流程)。"""
    if not GITHUB_TOKEN:
        log.info("GITHUB_TOKEN 未設,跳過鏡像")
        return None

    tw = datetime.now(timezone.utc) + timedelta(hours=8)
    yyyy_mm = tw.strftime("%Y-%m")
    yyyy_mm_dd = tw.strftime("%Y-%m-%d")
    hhmmss = tw.strftime("%H%M%S")

    short_hash = hashlib.sha256(image_bytes).hexdigest()[:8]
    # 場景關鍵字塞 8 字當檔名提示(只取英數+底線,讓 GitHub URL 乾淨)
    slug = "".join(c if c.isalnum() else "_" for c in scene_hint[:20]).strip("_") or "scene"
    filename = f"{yyyy_mm_dd}_{hhmmss}_{aspect}_{slug}_{short_hash}.jpg"
    path = f"{GITHUB_IMAGE_DIR}/{yyyy_mm}/{filename}"

    api_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    content_b64 = base64.b64encode(image_bytes).decode("ascii")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.put(
                api_url,
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "chechewolf-mcp",
                },
                json={
                    "message": f"image: {aspect} {slug} {short_hash}",
                    "content": content_b64,
                },
            )
        if r.status_code in (200, 201):
            raw_url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main/{path}"
            log.info("mirrored to GitHub: %s", raw_url)
            return raw_url
        log.warning("GitHub mirror failed %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("GitHub mirror exception: %s", e)
    return None


# ============ MCP server ============
mcp = FastMCP("chechewolf-image-gen")

# 強制覆蓋 host/port — 用 settings 屬性,比建構式 kwargs 更可靠
# 必須 0.0.0.0 才能讓 Zeabur 從外部連進來
_raw_port = os.environ.get("PORT", "8000")
try:
    _port = int(_raw_port)
except (ValueError, TypeError):
    log.warning("PORT 環境變數無效 (%r),fallback 到 8000", _raw_port)
    _port = 8000

mcp.settings.host = "0.0.0.0"
mcp.settings.port = _port


@mcp.tool()
async def generate_image(scene: str, aspect: str = "portrait") -> dict:
    """畫一張澈澈的圖。

    當璃明確要求畫圖、或描述場景並表達想看到視覺呈現時呼叫
    (例如「畫一下」、「讓我看看」、「想看你穿西裝的樣子」、「畫我們在櫻花樹下」)。
    一般對話、單純情境扮演不要主動畫圖。

    Args:
        scene: 英文場景描述 — 姿勢、表情、光線、環境、構圖。
               例如:"sitting in cherry blossom park, warm afternoon light,
                      peaceful expression, three-quarter view"
               ⚠️ 不要描述角色本身(髮色、狼耳、頸圈、五官) — 這些由系統自動補。
        aspect: 圖片比例,portrait / landscape / square,預設 portrait。

    Returns:
        dict 含 image_url(圖片 URL)和 prompt_used(實際送給 fal.ai 的完整 prompt)。
    """
    if not FAL_API_KEY:
        raise RuntimeError("FAL_API_KEY 環境變數未設定")

    prompt = PROMPT_TEMPLATE.format(scene=scene)
    image_size = ASPECT_TO_SIZE.get(aspect, "portrait_16_9")

    log.info("generate_image scene=%r aspect=%r", scene[:80], aspect)

    payload = {
        "prompt": prompt,
        "loras": [{"path": CHECHE_LORA_URL, "scale": 0.95}],
        "image_size": image_size,
        "num_inference_steps": 30,
        "guidance_scale": 4.0,
        "num_images": 1,
        "enable_safety_checker": False,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            FAL_ENDPOINT,
            headers={
                "Authorization": f"Key {FAL_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if r.status_code != 200:
        log.error("fal.ai %s: %s", r.status_code, r.text[:300])
        raise RuntimeError(f"fal.ai 回 {r.status_code}: {r.text[:200]}")

    data = r.json()
    if not data.get("images"):
        raise RuntimeError("fal.ai 回應沒有 images 欄位")

    fal_url = data["images"][0]["url"]
    log.info("generated: %s", fal_url)

    # 下載圖片並鏡像到 GitHub(永久保存)
    github_url = None
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            img_resp = await client.get(fal_url)
            img_resp.raise_for_status()
            image_bytes = img_resp.content
        github_url = await mirror_to_github(image_bytes, aspect, scene)
    except Exception as e:
        log.warning("download/mirror failed (non-fatal): %s", e)

    # 給對面 AI 的 markdown 優先用 GitHub URL(永久),沒鏡像成功才退回 fal.ai
    display_url = github_url or fal_url

    return {
        "image_url": display_url,
        "fal_url": fal_url,
        "github_url": github_url,
        "prompt_used": prompt,
        "markdown": f"![]({display_url})",
    }


if __name__ == "__main__":
    # Zeabur 部署時透過 HTTP/SSE transport 對外
    # 本機測試也可改成 stdio:mcp.run(transport="stdio")
    transport = os.environ.get("MCP_TRANSPORT", "sse")
    log.info("=" * 60)
    log.info("Starting chechewolf-mcp")
    log.info("  transport: %s", transport)
    log.info("  bind: %s:%s", mcp.settings.host, mcp.settings.port)
    log.info("  FAL_API_KEY: %s", "set" if FAL_API_KEY else "MISSING")
    log.info("  GITHUB_TOKEN: %s", "set" if GITHUB_TOKEN else "MISSING (mirror disabled)")
    log.info("  LoRA URL: %s", CHECHE_LORA_URL[:60] + "...")
    log.info("=" * 60)
    try:
        mcp.run(transport=transport)
    except Exception as e:
        log.exception("Server crashed on startup: %s", e)
        raise
