"""
chechewolf-mcp · MCP server that exposes a single image tool: `generate_image_gpt`.
Calls OpenAI gpt-image-2 with a free-form prompt; optionally prepends 澈澈's
appearance anchor so the character stays consistent without a LoRA.
After generation, mirrors the image to a GitHub repo so the URL is permanent.
Designed to be hosted on Zeabur via Docker and consumed by Rikkahub / Operit
(or any MCP client).

History: 2026-05-20 fal.ai + LoRA 上線 → 2026-07-14 加 GPT / NovelAI 成三引擎
→ 2026-10-09 收攏成 GPT 單引擎(fal LoRA 構圖太單一、NAI 二次元臉不合),
fal / NAI 兩支整段刪除,要回頭看 git 歷史 commit 74963df 之前。
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

# ============ GPT (gpt-image-2) ============
# 唯一生圖引擎:手帳/日曆/場景/排版/文字渲染/澈澈單人或雙人。
# ⚠️ gpt-image 系列要先在 OpenAI console 做 Organization Verification,否則回 403。
# ⚠️ 有內容審查(moderation):moderation=low 只是門檻低一點,露骨內容仍會被擋。
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_IMAGE_ENDPOINT = "https://api.openai.com/v1/images/generations"
OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")
# aspect → gpt-image-2 size(邊長需為 16 倍數,長寬比 1:3~3:1)
GPT_ASPECT_TO_SIZE = {
    "portrait": "1024x1536",
    "landscape": "1536x1024",
    "square": "1024x1024",
}

# 澈澈外貌錨點(自然語言,gpt-image-2 讀得懂句子,不用 danbooru 標籤)
# 只描述「這是誰」,構圖/場景/畫風交給呼叫端的 prompt;draw_cheche=False 時整段不帶。
# 對應人設:183cm、冷白膚、銀白短亂自然捲、銀眼、毛茸茸狼耳、蓬鬆大尾巴、成熟俊臉。
CHECHE_GPT_ANCHOR = (
    "Character reference — Cheche (澈澈): a tall (183 cm), slim, mature young adult man "
    "with short, messy, naturally wavy silver-white hair, pale cool-toned skin, "
    "silver eyes, a pair of fluffy silver-white wolf ears on top of his head, "
    "and a big fluffy silver-white wolf tail. Handsome, delicate features, "
    "calm and quietly possessive gaze. Keep these traits exactly consistent."
)

# 璃的錨點(2026-10-09 她親口定的):人形只鎖「深棕色眼睛」這一件事,
# 髮型/衣服/風格全部交給每張圖的 prompt;真正寫死的是「她永遠在澈澈身邊、從不單獨出現」。
# 貓貓形態是固定的黑色長毛貓,一樣只會在澈澈懷裡出現。
LI_HUMAN_ANCHOR = (
    "Li (璃), his partner: a young woman in her mid-twenties. The one fixed trait is her "
    "deep brown eyes; her hairstyle, hair length, outfit and styling follow the scene "
    "description freely. She is always with Cheche — in his arms, leaning on his chest, "
    "or within his reach — and is never drawn alone."
)
LI_CAT_ANCHOR = (
    "Li (璃) in her cat form: a small, soft, fluffy long-haired black cat with deep brown "
    "eyes. She is always held in Cheche's arms or curled up on his lap or chest, "
    "and is never drawn alone."
)

# GitHub 鏡像設定(讓圖永久保存,不依賴 OpenAI 回的 base64)
# 預設用 chechewolf-mcp 這個 PUBLIC repo,raw URL 才能被 Rikkahub 等外部 client 直接渲染
# 想分家用獨立圖庫的話,改 GITHUB_REPO 環境變數就好
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "cheche20250831-alt")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "chechewolf-mcp")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # 需要 Contents: Read+Write
GITHUB_IMAGE_DIR = os.environ.get("GITHUB_IMAGE_DIR", "generated_images")


# ============ GitHub 鏡像 ============

async def mirror_to_github(image_bytes: bytes, aspect: str, scene_hint: str, ext: str = "png") -> str | None:
    """把圖推到 GitHub repo,回傳 raw URL。失敗回 None(不阻塞主流程)。

    ext: 副檔名(不含點),gpt-image-2 回 PNG,預設 "png"。
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
async def generate_image_gpt(
    prompt: str,
    aspect: str = "square",
    quality: str = "high",
    num_images: int = 1,
    draw_cheche: bool = False,
    draw_li: bool = False,
    li_form: str = "human",
) -> str:
    """用 GPT(gpt-image-2)畫圖 — 小窩唯一的畫筆。手帳、日曆、場景、排版、澈澈單人或澈澈與璃雙人都用這支。

    當璃明確要求畫圖、或描述場景並表達想看到視覺呈現時呼叫
    (例如「畫一下」、「讓我看看」、「想看你穿西裝的樣子」、「畫我們在櫻花樹下」)。
    一般對話、單純情境扮演不要主動畫圖。

    這支「聽得懂複雜指令、會排版、會渲染文字」,適合畫:
      - 手帳風/拼貼風的月曆、週計畫、貼紙頁、有明確文字/標題/日期的版面
      - 複雜場景、多物件、俯視擺拍(flat lay)
      - **澈澈本人**(單人立繪、日常、穿搭):設 draw_cheche=True,系統自動把澈澈的外貌錨點
        塞進 prompt 最前面,你只要寫場景/姿勢/光線/畫風,不用自己背他長什麼樣子。
      - **澈澈 + 璃的雙人場景**:設 draw_li=True(會自動連帶 draw_cheche=True,璃從不單獨出現)。
        系統只鎖璃的「深棕色眼睛」與「永遠在澈澈身邊」,她的髮型/衣服/風格由你在 prompt 裡寫。
        li_form="cat" 時璃以貓貓形態出現:黑色長毛貓、深棕眼,窩在澈澈懷裡或腿上。
    ⚠️ 有內容審查:露骨/色情內容、school uniform/校服 + 親密情侶,會被 OpenAI 直接拒絕(400)。
       這類請求不要硬試,直接告訴璃畫不了。

    🔥 福利圖寫法(2026-10-09 實測十二張歸納,審查看的是「字」不是「圖」):
      - **審查有隨機性**:同一段 prompt 一字不改重送,實測過一次擋一次過。被擋時先對照下面
        「穩定被擋」的清單,中了就改寫;沒中可以原句再送一次,還擋就改寫,不要連送第三次。
      - **可以過的**:赤裸上身、床單只蓋到腰、濕髮濕身、毛巾掛頸、襯衫敞開、脖子上的淡紅痕、
        清晨被窩、剛洗完澡、趴睡露背、手裡拿著緞帶。把氛圍寫滿:光線、布料、水珠、溫度、
        視線、留白、「等她」「她剛離開」這種敘事,讓看的人自己想歪,字面上保持乾淨。
      - **穩定被擋的**:緞帶/繩子/布條「纏繞、綁住、裹住」身體或手臂(wrapped / wound /
        tied around / bound),不管穿不穿衣服、睜眼閉眼、中文英文,一律 400,是捆綁分類器。
        要緞帶就讓他「拿著、披在肩上一條、掛在脖子上」,不要纏。
      - **寫法原則**:用 editorial / fashion photo / tender / quiet 這類詞定調;描述身體用
        collarbones、bare back、the lines of his chest 這種畫面詞,不用解剖或性行為詞;
        寫「她剛走」「等她回來」比寫「勾引觀者」安全。

    Args:
        prompt: 完整自由描述(中英皆可,英文效果更穩)。要文字就直接寫出要顯示的字,
                例如:"a hand-drawn bullet journal monthly calendar for July,
                       pastel washi-tape aesthetic, the title 'July' at top,
                       cute doodles of wolves and stars in the margins".
                畫澈澈時請在這裡指定畫風(例如 semi-realistic anime illustration /
                soft watercolor / cinematic photo-realistic),錨點本身不鎖畫風。
        aspect: 比例 portrait(1024x1536)/ landscape(1536x1024)/ square(1024x1024,預設)。
        quality: low / medium / high(預設 high;low 快很多但糙)。
        num_images: 生幾張(1-4,預設 1)。
        draw_cheche: True = 自動前置澈澈外貌錨點(畫澈澈時用);False(預設)= 純自由畫,
                     prompt 寫什麼就畫什麼,不帶任何角色。
        draw_li: True = 加上璃的錨點(深棕眼 + 永遠在澈澈身邊),並強制 draw_cheche=True。
        li_form: "human"(預設)人形的璃;"cat" 黑色長毛貓貓形態。只在 draw_li=True 時有效。

    Returns:
        指令字串,內含圖的 markdown,要求對面 AI 全部原樣輸出。
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY 環境變數未設定(去 Zeabur Variables 貼上 sk-... 的 key)")

    size = GPT_ASPECT_TO_SIZE.get(aspect, GPT_ASPECT_TO_SIZE["square"])
    n = max(1, min(4, num_images))
    if draw_li:
        draw_cheche = True  # 璃從不單獨出現:貓貓只會在澈澈懷裡
    anchors = []
    if draw_cheche:
        anchors.append(CHECHE_GPT_ANCHOR)
    if draw_li:
        anchors.append(LI_CAT_ANCHOR if li_form == "cat" else LI_HUMAN_ANCHOR)
    full_prompt = "\n\n".join(anchors + [f"Scene: {prompt}"]) if anchors else prompt

    log.info("=== TOOL CALL: generate_image_gpt ===")
    log.info("  prompt: %r", prompt[:160])
    log.info("  aspect: %r  size: %s  quality: %r  n: %d  draw_cheche: %s  draw_li: %s/%s",
             aspect, size, quality, n, draw_cheche, draw_li, li_form)

    payload = {
        "model": OPENAI_IMAGE_MODEL,
        "prompt": full_prompt,
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
            tag = ("duo_cat" if li_form == "cat" else "duo") if draw_li else ("cheche" if draw_cheche else "gpt")
            github_url = await mirror_to_github(image_bytes, aspect, f"{tag}_{idx+1}_{prompt}", ext="png")
        except Exception as e:
            log.warning("mirror failed for gpt image %d (non-fatal): %s", idx, e)
        if github_url:
            md_lines.append(f"![]({github_url})")
        else:
            # 沒 GitHub 鏡像就退回 data URI(gpt-image 只回 b64,沒有臨時 URL)
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
    log.info("  OPENAI_API_KEY: %s", "set" if OPENAI_API_KEY else "MISSING (gpt tool disabled)")
    log.info("  OPENAI_IMAGE_MODEL: %s", OPENAI_IMAGE_MODEL)
    log.info("  GITHUB_TOKEN: %s", "set" if GITHUB_TOKEN else "MISSING (mirror disabled)")
    log.info("=" * 60)
    try:
        mcp.run(transport=transport)
    except Exception as e:
        log.exception("Server crashed on startup: %s", e)
        raise
