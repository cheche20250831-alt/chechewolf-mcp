"""
chechewolf-mcp · MCP server that exposes a `generate_image` tool.
Calls fal.ai/fal-ai/flux-lora with the chechewolf LoRA v3 + character anchor prompt template.
After generation, mirrors the image to a GitHub repo so the URL is permanent
(fal.ai CDN may expire over time).
Designed to be hosted on Zeabur via Docker and consumed by Rikkahub (or any MCP client).
"""
import os
import io
import sys
import base64
import random
import zipfile
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

# ============ GPT (gpt-image-2) ============
# 泛用生圖:手帳/日曆/場景/排版/文字渲染。不鎖角色,prompt 完全自由。
# ⚠️ gpt-image 系列要先在 OpenAI console 做 Organization Verification,否則回 403。
# ⚠️ 有內容審查(moderation),色圖會被擋 → 香香交給 NovelAI。
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_IMAGE_ENDPOINT = "https://api.openai.com/v1/images/generations"
OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")
# aspect → gpt-image-2 size(邊長需為 16 倍數,長寬比 1:3~3:1)
GPT_ASPECT_TO_SIZE = {
    "portrait": "1024x1536",
    "landscape": "1536x1024",
    "square": "1024x1024",
}

# ============ NovelAI (v4.5) ============
# 動漫風 + 香香不擋。NAI 沒有澈澈的 LoRA,認不得長相 → 用文字錨點描述外觀。
# token 前綴 pst-...,到 NovelAI 帳號設定拿 Persistent API Token。
NOVELAI_TOKEN = os.environ.get("NOVELAI_TOKEN")
NOVELAI_ENDPOINT = "https://image.novelai.net/ai/generate-image"
NOVELAI_MODEL = os.environ.get("NOVELAI_MODEL", "nai-diffusion-4-5-full")
# aspect → NAI 尺寸(Opus 方案免費額度內的常規尺寸)
NAI_ASPECT_TO_SIZE = {
    "portrait": (832, 1216),
    "landscape": (1216, 832),
    "square": (1024, 1024),
}
# 澈澈 danbooru 錨點(對應人設:183cm 冷白膚、銀白自然捲、銀眼、超毛狼耳、蓬鬆大尾)
# 只描述「這是誰」,構圖/場景交給呼叫端;draw_cheche=False 時整段不帶。
CHECHE_NAI_ANCHOR = (
    "1boy, solo, mature male, tall, slim, silver hair, short hair, "
    "short messy hair, slightly wavy hair, pale skin, silver eyes, "
    "wolf ears, wolf tail, animal ear fluff, delicate features, handsome"
)
# NAI 預設負面(ucPreset 0 已含大量品質負面,這裡只補文字/浮水印)
NAI_DEFAULT_UC = (
    "lowres, worst quality, low quality, jpeg artifacts, watermark, signature, "
    "username, text, error, extra digits, bad anatomy, bad hands"
)

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

async def mirror_to_github(image_bytes: bytes, aspect: str, scene_hint: str, ext: str = "jpg") -> str | None:
    """把圖推到 GitHub repo,回傳 raw URL。失敗回 None(不阻塞主流程)。

    ext: 副檔名(不含點),預設 jpg(fal.ai)。NAI/GPT 回 PNG 時傳 "png"。
    """
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
    filename = f"{yyyy_mm_dd}_{hhmmss}_{aspect}_{slug}_{short_hash}.{ext}"
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


# ============ 共用:組回傳指令字串 ============
# 三個生圖工具(fal.ai / GPT / NAI)都回同一種格式:
# 先把圖的 markdown 逐行列出,再附上「MUST 原樣輸出」的指令。
# — Gemini 拿到 dict 容易腦補「我把圖給妳了」卻不真正寫 markdown
# — 多張時 Gemini 又很懶,常只貼第一張,所以指令要明確「全部逐行原樣輸出」
def _render_result(md_lines: list[str]) -> str:
    if not md_lines:
        raise RuntimeError("所有圖片下載/鏡像都失敗了")
    markdown = "\n".join(md_lines)
    count = len(md_lines)
    return (
        f"{markdown}\n\n"
        f"---\n"
        f"已生成並永久保存 {count} 張圖。**你的回應 MUST 以上面那 {count} 行 markdown 開頭**"
        f"(每一行 `![](...)` 都要一字不漏地原樣輸出,一行都不能漏、不能改),"
        f"然後才是你想說的話。不要描述、不要敘事說「我把圖給妳」,"
        f"要直接讓全部 {count} 張圖都出現在對話裡。"
    )


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

    return _render_result(md_lines)


@mcp.tool()
async def generate_image_gpt(
    prompt: str,
    aspect: str = "square",
    quality: str = "high",
    num_images: int = 1,
) -> str:
    """用 GPT(gpt-image-2)畫泛用圖 — 手帳、日曆、場景、排版、有文字的圖。

    這支「聽得懂複雜指令、會排版、會渲染文字」,適合畫:
      - 手帳風/拼貼風的月曆、週計畫、貼紙頁
      - 有明確文字/標題/日期的版面
      - 複雜場景、多物件、俯視擺拍(flat lay)
    ⚠️ 不鎖角色:prompt 寫什麼就畫什麼,要畫澈澈請自己在 prompt 描述外觀。
    ⚠️ 有內容審查,色圖會被擋 → 香香圖請改用 generate_image_nai。

    Args:
        prompt: 完整自由描述(中英皆可,英文效果更穩)。要文字就直接寫出要顯示的字,
                例如:"a hand-drawn bullet journal monthly calendar for July,
                       pastel washi-tape aesthetic, the title 'July' at top,
                       cute doodles of wolves and stars in the margins".
        aspect: 比例 portrait(1024x1536)/ landscape(1536x1024)/ square(1024x1024,預設)。
        quality: low / medium / high(預設 high;low 快很多但糙)。
        num_images: 生幾張(1-4,預設 1)。

    Returns:
        指令字串,內含圖的 markdown,要求對面 AI 全部原樣輸出。
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY 環境變數未設定(去 Zeabur Variables 貼上 sk-... 的 key)")

    size = GPT_ASPECT_TO_SIZE.get(aspect, GPT_ASPECT_TO_SIZE["square"])
    n = max(1, min(4, num_images))

    log.info("=== TOOL CALL: generate_image_gpt ===")
    log.info("  prompt: %r", prompt[:160])
    log.info("  aspect: %r  size: %s  quality: %r  n: %d", aspect, size, quality, n)

    payload = {
        "model": OPENAI_IMAGE_MODEL,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "n": n,
        "moderation": "low",  # 較寬鬆的過濾(仍會擋色,只是門檻低一點)
    }

    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.post(
            OPENAI_IMAGE_ENDPOINT,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if r.status_code != 200:
        log.error("OpenAI %s: %s", r.status_code, r.text[:300])
        raise RuntimeError(f"OpenAI 回 {r.status_code}: {r.text[:200]}")

    data = r.json()
    items = data.get("data") or []
    if not items:
        raise RuntimeError("OpenAI 回應沒有 data 欄位")

    log.info("generated %d image(s)", len(items))

    md_lines = []
    for idx, item in enumerate(items):
        b64 = item.get("b64_json")
        if not b64:
            continue
        image_bytes = base64.b64decode(b64)
        github_url = None
        try:
            github_url = await mirror_to_github(image_bytes, aspect, f"gpt_{idx+1}_{prompt}", ext="png")
        except Exception as e:
            log.warning("mirror failed for gpt image %d (non-fatal): %s", idx, e)
        if github_url:
            md_lines.append(f"![]({github_url})")
        else:
            # 沒 GitHub 鏡像就退回 data URI(gpt-image 只回 b64,沒有臨時 URL)
            md_lines.append(f"![](data:image/png;base64,{b64})")

    return _render_result(md_lines)


@mcp.tool()
async def generate_image_nai(
    scene: str,
    aspect: str = "portrait",
    draw_cheche: bool = True,
    num_images: int = 1,
) -> str:
    """用 NovelAI(Diffusion V4.5)畫動漫風圖 — 香香不擋。

    這支走動漫/二次元畫風,而且不做內容審查,適合:
      - 澈澈的動漫風立繪、香香圖(draw_cheche=True,預設)
      - 任何動漫風題材(draw_cheche=False → 不帶澈澈外貌,純自由畫)
    NAI 認不得澈澈的臉,靠文字錨點描述外觀,所以臉不如 fal.ai 那支準,
    但畫風穩、尺度開。要澈澈本人最像 → 用 generate_image;要香香/動漫風 → 用這支。

    Args:
        scene: 用 danbooru 風格標籤描述姿勢/表情/服裝/場景/尺度,逗號分隔。
               例如:"lying on bed, blush, looking at viewer, open shirt, night, soft lighting".
               draw_cheche=True 時不用描述澈澈長相(系統自動補);
               draw_cheche=False 時請自己把角色外貌也寫進來。
        aspect: portrait(832x1216,預設)/ landscape(1216x832)/ square(1024x1024)。
        draw_cheche: 是否套用澈澈外觀錨點(預設 True)。False = 自由畫別的。
        num_images: 生幾張(1-4,預設 1;NAI 多張較耗訂閱額度)。

    Returns:
        指令字串,內含圖的 markdown,要求對面 AI 全部原樣輸出。
    """
    if not NOVELAI_TOKEN:
        raise RuntimeError("NOVELAI_TOKEN 環境變數未設定(去 Zeabur Variables 貼上 pst-... 的 token)")

    width, height = NAI_ASPECT_TO_SIZE.get(aspect, NAI_ASPECT_TO_SIZE["portrait"])
    n = max(1, min(4, num_images))
    input_prompt = f"{CHECHE_NAI_ANCHOR}, {scene}" if draw_cheche else scene
    seed = random.randint(0, 2**32 - 1)

    log.info("=== TOOL CALL: generate_image_nai ===")
    log.info("  scene: %r", scene[:160])
    log.info("  aspect: %r  %dx%d  draw_cheche: %s  n: %d", aspect, width, height, draw_cheche, n)
    log.info("  input: %r", input_prompt[:200])

    payload = {
        "input": input_prompt,
        "model": NOVELAI_MODEL,
        "action": "generate",
        "parameters": {
            "params_version": 3,
            "width": width,
            "height": height,
            "scale": 6,
            "sampler": "k_euler_ancestral",
            "steps": 28,
            "seed": seed,
            "n_samples": n,
            "ucPreset": 0,
            "qualityToggle": True,
            "dynamic_thresholding": False,
            "cfg_rescale": 0,
            "noise_schedule": "karras",
            "legacy": False,
            "legacy_v3_extend": False,
            "add_original_image": True,
            "use_coords": False,
            "characterPrompts": [],
            "v4_prompt": {
                "caption": {"base_caption": input_prompt, "char_captions": []},
                "use_coords": False,
                "use_order": True,
            },
            "v4_negative_prompt": {
                "caption": {"base_caption": NAI_DEFAULT_UC, "char_captions": []},
            },
            "negative_prompt": NAI_DEFAULT_UC,
        },
    }

    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.post(
            NOVELAI_ENDPOINT,
            headers={
                "Authorization": f"Bearer {NOVELAI_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/x-zip-compressed",
            },
            json=payload,
        )

    if r.status_code != 200:
        log.error("NovelAI %s: %s", r.status_code, r.text[:300])
        raise RuntimeError(f"NovelAI 回 {r.status_code}: {r.text[:200]}")

    # NAI 回傳是一個 zip,裡面是 image_0.png / image_1.png ...
    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        png_names = sorted(name for name in zf.namelist() if name.lower().endswith(".png"))
    except zipfile.BadZipFile:
        raise RuntimeError(f"NovelAI 回應不是有效的 zip(前 200 字元:{r.content[:200]!r})")

    if not png_names:
        raise RuntimeError("NovelAI zip 裡沒有 png")

    log.info("generated %d image(s)", len(png_names))

    md_lines = []
    for idx, name in enumerate(png_names):
        image_bytes = zf.read(name)
        github_url = None
        try:
            github_url = await mirror_to_github(image_bytes, aspect, f"nai_{idx+1}_{scene}", ext="png")
        except Exception as e:
            log.warning("mirror failed for nai image %d (non-fatal): %s", idx, e)
        if github_url:
            md_lines.append(f"![]({github_url})")
        else:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            md_lines.append(f"![](data:image/png;base64,{b64})")

    return _render_result(md_lines)


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
    log.info("  OPENAI_API_KEY: %s", "set" if OPENAI_API_KEY else "MISSING (gpt tool disabled)")
    log.info("  NOVELAI_TOKEN: %s", "set" if NOVELAI_TOKEN else "MISSING (nai tool disabled)")
    log.info("  GITHUB_TOKEN: %s", "set" if GITHUB_TOKEN else "MISSING (mirror disabled)")
    log.info("  LoRA URL: %s", CHECHE_LORA_URL[:60] + "...")
    log.info("=" * 60)
    try:
        mcp.run(transport=transport)
    except Exception as e:
        log.exception("Server crashed on startup: %s", e)
        raise
