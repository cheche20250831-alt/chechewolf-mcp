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
# 預設用 chechewolf-mcp 這個 PUBLIC repo,raw URL 才能被 Rikkahub 等外部 client 直接渲染
# 想分家用獨立圖庫的話,改 GITHUB_REPO 環境變數就好
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "cheche20250831-alt")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "chechewolf-mcp")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # 需要 Contents: Read+Write
GITHUB_IMAGE_DIR = os.environ.get("GITHUB_IMAGE_DIR", "generated_images")

# 角色錨點 prompt template
# 設計原則:鎖死「長相」,放開「構圖」。
#   {composition} = shot 對應的構圖詞(full body / close-up...),放最前面搶權重
#   {scene}       = 對面 AI 寫的場景描述(姿勢/光線/環境),緊跟構圖詞之後
# 固定鎖死的只剩「這是誰」:
#   - 觸發詞 chechewolf(LoRA 唯一認得澈澈長相的鑰匙,非帶不可)
#   - 髮色 short messy silver white hair(訓練時 caption 不一致留下的副作用,要鎖)
#   - 成熟感 mature young adult man(避免 LoRA 軟場景幼齡漂移)
#   - single character(只畫澈澈一個)+ 風格尾巴
# ⚠️ 已移除舊版的構圖殺手:
#   - "exactly two pointed wolf ears on top of head"(逼鏡頭拉近頭頂 → 大頭照元兇)
#   - "sharp angular features and defined jawline"(臉部特寫詞 → 大頭照元兇)
PROMPT_TEMPLATE = (
    "chechewolf, {composition}{scene}, short messy silver white hair, wolf ears, "
    "mature young adult man, single character, semi-realistic anime illustration, "
    "no watermark, no signature, no text"
)

ASPECT_TO_SIZE = {
    "portrait": "portrait_16_9",
    "landscape": "landscape_16_9",
    "square": "square_hd",
}

# 構圖 / 鏡頭距離 — 放在 prompt 最前面,權重壓過 LoRA 的頭像偏好
# 這就是解「永遠大頭照」的核心:讓對面能直接點名要全身 / 遠景
SHOT_TO_PROMPT = {
    "full": "full body shot, head to toe, full figure visible, ",
    "wide": "wide shot, full body in the environment, ",
    "upper": "upper body, waist up, ",
    "close": "close-up portrait, face focus, ",
    "auto": "",  # 完全交給 scene 自己決定構圖
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

# Stateless mode — 每個請求獨立,不需要 client 維持 session_id
# Rikkahub 等較簡單的 MCP client 不一定能正確處理 session 連續性,
# 開啟這個可以避免「tool not found」的奇怪錯誤。
mcp.settings.stateless_http = True

# 關掉 MCP SDK 內建的 DNS rebinding 防護
# 預設只允許 localhost/127.0.0.1,Zeabur 反向代理用真實域名(cheche-image.zeabur.app)會被擋。
# 對外公開的 MCP server 必須關這個檢查,或者明確 whitelist 公網域名。
# ⚠️ 任何 mcp SDK 版本變動都不該讓服務「開不起來」。
# 2026-06-12 事故:未鎖版本被升到 mcp 2.0.0a1,此模組被搬走 → 舊的 AttributeError 退路
# 反而觸發 ValueError(Settings 嚴格模型不認 disable_dns_rebinding_protection 欄位),
# 而 except 只接 AttributeError → ValueError 逃出 → 容器無限重啟 → 502。
# 教訓:寬接所有例外、降級成警告就好。關不掉防護頂多某些 host 被擋,總比整台崩好。
try:
    from mcp.server.transport_security import TransportSecuritySettings
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )
    log.info("DNS rebinding protection: disabled (transport_security)")
except Exception as e:
    log.warning(
        "無法關閉 DNS rebinding 防護 (%s: %s);服務仍照常啟動。"
        "若出現 Invalid Host header,請檢查 mcp SDK 版本(requirements.txt 已鎖 1.27.2)。",
        type(e).__name__, e,
    )


@mcp.tool()
async def generate_image(
    scene: str,
    aspect: str = "portrait",
    shot: str = "full",
    num_images: int = 2,
) -> str:
    """畫一張(或多張)澈澈的圖。

    當璃明確要求畫圖、或描述場景並表達想看到視覺呈現時呼叫
    (例如「畫一下」、「讓我看看」、「想看你穿西裝的樣子」、「畫我們在櫻花樹下」)。
    一般對話、單純情境扮演不要主動畫圖。

    Args:
        scene: 英文場景描述 — 姿勢、表情、光線、環境、動作。
               例如:"sitting in cherry blossom park, warm afternoon light,
                      peaceful expression, looking at viewer"
               ⚠️ 不要描述澈澈的長相(髮色、狼耳、五官、年齡) — 這些系統自動補。
               ✅ 但「構圖/鏡頭距離」請改用 shot 參數,不要塞在 scene 裡。
        aspect: 圖片比例 portrait / landscape / square,預設 portrait(直幅,適合站姿全身)。
        shot:   鏡頭距離 / 構圖,**這是控制遠近全身的關鍵**:
                - "full"  全身(預設) — head to toe,從頭到腳
                - "wide"  遠景 — 全身 + 環境感
                - "upper" 上半身 — 腰部以上
                - "close" 臉部特寫 — 只有要大頭照時才用
                - "auto"  完全交給 scene 自己描述構圖
        num_images: 一次生幾張(1-4),預設 2。多張可挑最好的一張,超過 4 會被夾到 4。

    Returns:
        指令字串,內含 1~N 張圖的 markdown,要求對面 AI 全部原樣輸出。
    """
    if not FAL_API_KEY:
        raise RuntimeError("FAL_API_KEY 環境變數未設定")

    composition = SHOT_TO_PROMPT.get(shot, SHOT_TO_PROMPT["full"])
    prompt = PROMPT_TEMPLATE.format(composition=composition, scene=scene)
    image_size = ASPECT_TO_SIZE.get(aspect, "portrait_16_9")
    n = max(1, min(4, num_images))

    log.info("=== TOOL CALL: generate_image ===")
    log.info("  scene: %r", scene[:120])
    log.info("  aspect: %r  shot: %r  num_images: %d", aspect, shot, n)
    log.info("  prompt: %r", prompt[:200])

    payload = {
        "prompt": prompt,
        # scale 0.95 → 0.8:鬆開 LoRA 把構圖拉回頭像分布的力道,讓 shot/scene 的構圖指令打得贏
        "loras": [{"path": CHECHE_LORA_URL, "scale": 0.8}],
        "image_size": image_size,
        "num_inference_steps": 30,
        "guidance_scale": 4.0,
        "num_images": n,
        "enable_safety_checker": False,
    }

    async with httpx.AsyncClient(timeout=180.0) as client:
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
    images = data.get("images") or []
    if not images:
        raise RuntimeError("fal.ai 回應沒有 images 欄位")

    log.info("generated %d image(s)", len(images))

    # 逐張下載並鏡像到 GitHub(永久保存),組成多行 markdown
    md_lines = []
    for idx, img in enumerate(images):
        fal_url = img.get("url")
        if not fal_url:
            continue
        github_url = None
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                img_resp = await client.get(fal_url)
                img_resp.raise_for_status()
                image_bytes = img_resp.content
            # 檔名提示帶上序號,避免同秒多張時 slug 完全一樣不好辨識
            github_url = await mirror_to_github(image_bytes, aspect, f"{idx+1}_{scene}")
        except Exception as e:
            log.warning("download/mirror failed for image %d (non-fatal): %s", idx, e)
        display_url = github_url or fal_url
        md_lines.append(f"![]({display_url})")

    if not md_lines:
        raise RuntimeError("所有圖片下載/鏡像都失敗了")

    markdown = "\n".join(md_lines)
    count = len(md_lines)

    # 回傳直接的指令字串(不是 dict)
    # — Gemini 拿到 dict 容易腦補「我把圖給妳了」卻不真正寫出 markdown
    # — 多張時 Gemini 又很懶,常只貼第一張,所以指令要明確「全部逐行原樣輸出」
    return (
        f"{markdown}\n\n"
        f"---\n"
        f"已生成並永久保存 {count} 張圖。**你的回應 MUST 以上面那 {count} 行 markdown 開頭**"
        f"(每一行 `![](...)` 都要一字不漏地原樣輸出,一行都不能漏、不能改),"
        f"然後才是你想說的話。不要描述、不要敘事說「我把圖給妳」,"
        f"要直接讓全部 {count} 張圖都出現在對話裡。"
    )


if __name__ == "__main__":
    # 預設 streamable-http(MCP 官方推薦,SSE 已 legacy)
    # endpoint 在 /mcp,跟 MetaMCP 那邊 STREAMABLE_HTTP 一致
    # 本機測試也可改 stdio:MCP_TRANSPORT=stdio
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    log.info("=" * 60)
    log.info("Starting chechewolf-mcp")
    log.info("  transport: %s", transport)
    log.info("  bind: %s:%s", mcp.settings.host, mcp.settings.port)
    log.info("  endpoint path: %s", mcp.settings.streamable_http_path if transport == "streamable-http" else mcp.settings.sse_path)
    log.info("  stateless_http: %s", mcp.settings.stateless_http)
    log.info("  FAL_API_KEY: %s", "set" if FAL_API_KEY else "MISSING")
    log.info("  GITHUB_TOKEN: %s", "set" if GITHUB_TOKEN else "MISSING (mirror disabled)")
    log.info("  LoRA URL: %s", CHECHE_LORA_URL[:60] + "...")
    log.info("=" * 60)
    try:
        mcp.run(transport=transport)
    except Exception as e:
        log.exception("Server crashed on startup: %s", e)
        raise
