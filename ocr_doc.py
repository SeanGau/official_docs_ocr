#!/usr/bin/env python3
"""公文 PDF -> Markdown（含 YAML frontmatter metadata）

OpenAI 與 Claude 會把掃描 PDF 每頁轉成點陣圖，再交給視覺模型做 OCR；
Gemini 直接讀取 PDF。三家都會重建版面、抽 metadata，輸出成 .md 檔
（開頭是 YAML frontmatter）。

用 `--provider` 可指定雲端模型；只設定 `GEMINI_API_KEY` 時會自動選 Gemini。
三家的 prompt、schema 與輸出格式完全相同。

輸出的正文會逐頁插入 `<!-- page: N/總頁數 -->` 標記，方便後續 RAG
切 chunk 時保留頁碼來源。

用法:
    export GEMINI_API_KEY=...             # 只設定這把 key 即可，或寫進 .env
    python3 ocr_doc.py                    # input/ 底下所有 pdf -> output/
    python3 ocr_doc.py a.pdf b.pdf        # 只處理指定檔案
    python3 ocr_doc.py -i in/ -o out/     # 指定輸入／輸出目錄
    python3 ocr_doc.py -p claude          # 明確指定 Claude
    python3 ocr_doc.py -m gemini-3.8-flash  # 換模型
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

# 輸入 PDF 與輸出 Markdown 各自有固定資料夾，不再散在當前目錄。
DEFAULT_INDIR = "input"
DEFAULT_OUTDIR = "output"

DEFAULT_PROVIDER = "openai"


def load_dotenv() -> None:
    """把 .env 的 KEY=VALUE 塞進 os.environ（已存在的環境變數優先）。

    非互動 shell 通常讀不到 ~/.bashrc（Ubuntu 預設在開頭就 return），
    所以金鑰放 .env 比較可靠。
    """
    for d in (Path(__file__).resolve().parent, Path.cwd()):
        env = d / ".env"
        if not env.is_file():
            continue
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip("'\"")
            os.environ.setdefault(key, val)

def collect_pdfs(pdfs: list[str], indir: str) -> list[Path]:
    """指定檔案優先，否則掃 indir/ 底下的 .pdf。地端版與 bench 共用。"""
    paths = [Path(p) for p in pdfs] if pdfs else sorted(Path(indir).glob("*.pdf"))
    return [p for p in paths if p.suffix.lower() == ".pdf" and p.is_file()]


# RAG 切 chunk 時用來還原「這段話出自第幾頁」。用 HTML 註解，
# 算是 Markdown 的隱形內容，不會污染顯示出來的正文。
PAGE_MARKER = "<!-- page: {n}/{total} -->"

# 每份文件的所有頁面一次送出，讓模型能跨頁理解（例如函文 + 附件表格）。
# 若單份文件頁數很多，可調小分批。
MAX_PAGES_PER_REQUEST = 20

SYSTEM_PROMPT = """\
你是台灣公文數位化專家，負責把掃描的公文影像轉成結構化資料。

工作要求：
1. OCR 要逐字精確，使用繁體中文。專有名詞（機關名稱、人名、地名）務必正確。
2. 保留公文原有的層次結構：主旨、說明（一、二、三…）、辦法、附件等。
   說明底下的子項（(一)(二)、1.2.3.）要保留階層，用 Markdown 巢狀清單表示。
3. 表格（如申請表、經費表）用 Markdown 表格重建，欄位對齊原件。
4. 印章、簽名、浮水印等非文字內容，用 `<!-- 印章：OO部OO司 -->` 這類 HTML 註解標註，
   不要當成正文。手寫批註也用註解標註並註明「手寫」。
5. 完全無法辨識的字用 `〇` 代替，不要臆測。
6. 民國紀年一律保留原文，另在 metadata 中同時提供西元 ISO 格式。

metadata 欄位若原件沒有，填空字串或空陣列，不要編造。
"""

USER_PROMPT = """\
以上是同一份公文的全部頁面（依序）。請完成：

1. 抽出核心 metadata。
2. 把全文轉成 Markdown（不要包含 YAML frontmatter，那由程式產生），
   並且**逐頁分開輸出**：pages 陣列一頁一筆，頁碼對應上面標示的頁次，
   缺頁或空白頁也要有一筆（markdown 填空字串）。
   第 1 頁以 `# {主旨}` 開頭，接著依序呈現受文者、主旨、說明、正副本等區塊。
   若有附件，附件所在的那一頁以 `## 附件一：xxx` 起頭。
   內容跨頁時（例如表格或段落被切斷），照原件切在頁面邊界，
   不要把後頁的內容併進前頁，也不要為了通順而重排順序；
   跨頁的表格在下一頁重新寫一次表頭即可。
"""

# 結構化輸出 schema。所有欄位皆為 required 且不允許額外屬性
# （structured outputs 的硬性要求），缺值用空字串／空陣列表示。
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["metadata", "pages"],
    "properties": {
        "metadata": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "發文機關",
                "受文者",
                "發文日期",
                "發文日期_西元",
                "發文字號",
                "速別",
                "密等及解密條件",
                "附件",
                "主旨",
                "正本",
                "副本",
                "承辦人",
                "聯絡方式",
                "檔號",
                "保存年限",
                "文別",
                "關鍵字",
                "摘要",
            ],
            "properties": {
                "發文機關": {"type": "string", "description": "發文的機關全銜"},
                "受文者": {"type": "string"},
                "發文日期": {"type": "string", "description": "原件民國紀年，如「中華民國110年8月31日」"},
                "發文日期_西元": {"type": "string", "description": "ISO 格式 YYYY-MM-DD"},
                "發文字號": {"type": "string", "description": "如「開字第1100831001號」"},
                "速別": {"type": "string", "description": "普通件／速件／最速件"},
                "密等及解密條件": {"type": "string"},
                "附件": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "附件名稱清單",
                },
                "主旨": {"type": "string", "description": "主旨欄全文（不含「主旨：」三字）"},
                "正本": {"type": "array", "items": {"type": "string"}},
                "副本": {"type": "array", "items": {"type": "string"}},
                "承辦人": {"type": "string"},
                "聯絡方式": {"type": "string", "description": "電話、傳真、email 等"},
                "檔號": {"type": "string"},
                "保存年限": {"type": "string"},
                "文別": {"type": "string", "description": "如「函」「書函」「令」「公告」"},
                "關鍵字": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "3-8 個便於檢索的主題關鍵字",
                },
                "摘要": {"type": "string", "description": "一到兩句話的內容摘要"},
            },
        },
        "pages": {
            "type": "array",
            "description": "逐頁的 Markdown，順序與輸入頁面相同",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["頁碼", "markdown"],
                "properties": {
                    "頁碼": {"type": "integer", "description": "頁次，從 1 起算"},
                    "markdown": {
                        "type": "string",
                        "description": "該頁的 Markdown，不含 YAML frontmatter",
                    },
                },
            },
        },
    },
}


def render_pages(pdf_path: Path, dpi: int, max_edge: int) -> list[bytes]:
    """把 PDF 每頁 render 成 PNG bytes，長邊不超過 max_edge。"""
    # 延後 import：地端版與其 exe 會 import 本模組取共用常數，但不安裝 AGPL 的 PyMuPDF。
    import pymupdf

    images = []
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            if max(pix.width, pix.height) > max_edge:
                scale = max_edge / max(pix.width, pix.height)
                pix = page.get_pixmap(dpi=int(dpi * scale))
            images.append(pix.tobytes("png"))
    return images


# --- OpenAI ------------------------------------------------------------------

def make_client_openai() -> Any:
    import openai

    # 多頁高解析度影像 + high effort 推理，單份跑好幾分鐘是常態，
    # SDK 預設的 timeout 不夠用。
    return openai.OpenAI(timeout=1800.0)


def build_content_openai(images: list[bytes]) -> list[dict]:
    content: list[dict] = []
    for i, png in enumerate(images, 1):
        content.append({"type": "input_text", "text": f"--- 第 {i} 頁 ---"})
        b64 = base64.standard_b64encode(png).decode()
        content.append(
            {
                "type": "input_image",
                # 公文字小，低解析度會直接認不出來，一律用 high。
                "detail": "high",
                "image_url": f"data:image/png;base64,{b64}",
            }
        )
    content.append({"type": "input_text", "text": USER_PROMPT})
    return content


def extract_openai(client: Any, model: str, images: list[bytes]) -> dict:
    """呼叫 OpenAI 做 OCR + 抽 metadata，回傳 {'metadata': ..., 'pages': [...]}。"""
    # 影像多、輸出長，一律 streaming 避免 HTTP timeout。
    with client.responses.stream(
        model=model,
        max_output_tokens=32000,
        instructions=SYSTEM_PROMPT,
        reasoning={"effort": "high"},
        text={
            "format": {
                "type": "json_schema",
                "name": "official_doc",
                "schema": SCHEMA,
                "strict": True,
            }
        },
        input=[{"role": "user", "content": build_content_openai(images)}],
    ) as stream:
        response = stream.get_final_response()

    for item in response.output:
        for part in getattr(item, "content", None) or []:
            if part.type == "refusal":
                raise RuntimeError(f"模型拒絕處理：{part.refusal}")
    if response.status == "incomplete":
        reason = getattr(response.incomplete_details, "reason", None) or response.status
        if reason == "max_output_tokens":
            raise RuntimeError("輸出被 max_output_tokens 截斷，請調高上限或分批處理頁面")
        raise RuntimeError(f"回應不完整：{reason}")

    return json.loads(response.output_text)


# --- Claude ------------------------------------------------------------------

def make_client_claude() -> Any:
    import anthropic

    return anthropic.Anthropic()


def build_content_claude(images: list[bytes]) -> list[dict]:
    content: list[dict] = []
    for i, png in enumerate(images, 1):
        content.append({"type": "text", "text": f"--- 第 {i} 頁 ---"})
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(png).decode(),
                },
            }
        )
    content.append({"type": "text", "text": USER_PROMPT})
    return content


def extract_claude(client: Any, model: str, images: list[bytes]) -> dict:
    """呼叫 Claude 做 OCR + 抽 metadata，回傳 {'metadata': ..., 'pages': [...]}。"""
    # 影像多、輸出長，一律 streaming 避免 HTTP timeout。
    with client.messages.stream(
        model=model,
        max_tokens=32000,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": SCHEMA},
        },
        messages=[{"role": "user", "content": build_content_claude(images)}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError(f"模型拒絕處理：{message.stop_details}")
    if message.stop_reason == "max_tokens":
        raise RuntimeError("輸出被 max_tokens 截斷，請調高 max_tokens 或分批處理頁面")

    text = next(b.text for b in message.content if b.type == "text")
    return json.loads(text)


# --- Gemini ------------------------------------------------------------------

def make_client_gemini() -> Any:
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("沒有設定 GEMINI_API_KEY，請設環境變數或寫進 .env")
    return genai.Client(api_key=api_key)


def extract_gemini(client: Any, model: str, pdf: bytes) -> dict:
    """呼叫 Gemini 原生讀取 PDF，回傳 {'metadata': ..., 'pages': [...]}。"""
    stream = client.interactions.create(
        model=model,
        input=[
            {
                "type": "document",
                "data": base64.standard_b64encode(pdf).decode(),
                "mime_type": "application/pdf",
            },
            {"type": "text", "text": USER_PROMPT},
        ],
        system_instruction=SYSTEM_PROMPT,
        generation_config={
            "max_output_tokens": 32000,
            "thinking_level": "high",
        },
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": SCHEMA,
        },
        stream=True,
        timeout=1800.0,
    )

    parts: list[str] = []
    for event in stream:
        event_type = getattr(event, "event_type", None)
        if event_type == "error":
            error = getattr(event, "error", None)
            detail = (
                getattr(error, "message", None)
                or getattr(error, "code", None)
                or "未知錯誤"
            )
            raise RuntimeError(f"Gemini 回應失敗：{detail}")
        delta = getattr(event, "delta", None)
        if event_type == "step.delta" and getattr(delta, "type", None) == "text":
            parts.append(delta.text)

    text = "".join(parts)
    if not text:
        raise RuntimeError("Gemini 沒有回傳內容")
    return json.loads(text)


@dataclass(frozen=True)
class Provider:
    """一家雲端模型的接法。prompt、schema、輸出格式都相同；
    render_dpi/max_edge 為 None 時代表模型直接接收 PDF。"""

    name: str
    default_model: str
    model_env: str
    # 視覺輸入的解析度上限：超過會被 API 自己縮，等於白花 token。
    # Gemini 原生接收 PDF，不需要 render，因此兩者皆為 None。
    max_edge: int | None
    render_dpi: int | None
    make_client: Callable[[], Any]
    extract: Callable[[Any, str, Any], dict]


PROVIDERS = {
    "openai": Provider(
        name="openai",
        default_model="gpt-5",
        model_env="OPENAI_MODEL",
        # OpenAI 視覺輸入會先把影像縮到 2048x2048 以內。
        max_edge=2048,
        # A4 在 175 DPI 下約 1447x2047，剛好貼齊 max_edge，把解析度額度用滿。
        render_dpi=175,
        make_client=make_client_openai,
        extract=extract_openai,
    ),
    "claude": Provider(
        name="claude",
        default_model="claude-opus-5",
        model_env="ANTHROPIC_MODEL",
        # Opus 5 高解析度視覺上限：長邊 2576px。
        max_edge=2576,
        # A4 在 220 DPI 下約 1819x2573，剛好貼齊 max_edge。
        render_dpi=220,
        make_client=make_client_claude,
        extract=extract_claude,
    ),
    "gemini": Provider(
        name="gemini",
        default_model="gemini-3.8-flash",
        model_env="GEMINI_MODEL",
        max_edge=None,
        render_dpi=None,
        make_client=make_client_gemini,
        extract=extract_gemini,
    ),
}


def join_pages(pages: list[dict], total: int) -> str:
    """把逐頁 Markdown 串成正文，每頁前面加頁碼標記。

    不插 `---` 分隔線：公文的段落、表格常常跨頁，多一條線反而會讓
    RAG 的 chunker 在句子中間硬切。
    """
    blocks = []
    for i, page in enumerate(pages, 1):
        md = (page.get("markdown") or "").strip()
        if not md:
            continue
        n = page.get("頁碼") or i
        blocks.append(f"{PAGE_MARKER.format(n=n, total=total)}\n\n{md}")
    return "\n\n".join(blocks)


def to_markdown_file(result: dict, source: Path, pages: int) -> str:
    meta = dict(result["metadata"])
    meta["來源檔案"] = source.name
    meta["頁數"] = pages

    front = yaml.safe_dump(
        meta,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1000,
    )
    body = join_pages(result["pages"], pages)
    return f"---\n{front}---\n\n{body}\n"


def process(provider: Provider, client: Any, model: str, pdf: Path, outdir: Path) -> Path:
    if provider.render_dpi is None or provider.max_edge is None:
        import pymupdf

        document = pdf.read_bytes()
        with pymupdf.open(stream=document, filetype="pdf") as doc:
            total = doc.page_count
        prepared = "讀取"
    else:
        document = render_pages(pdf, provider.render_dpi, provider.max_edge)
        total = len(document)
        prepared = "render"

    if total > MAX_PAGES_PER_REQUEST:
        raise RuntimeError(
            f"{pdf.name} 有 {total} 頁，超過單次上限 {MAX_PAGES_PER_REQUEST}"
        )
    print(f"  已{prepared} {total} 頁，送出辨識…", flush=True)

    result = provider.extract(client, model, document)
    got = len(result["pages"])
    if got != total:
        # 頁碼標記是給 RAG 溯源用的，對不上就等於引用錯頁，寧可讓它爆掉。
        raise RuntimeError(f"模型回了 {got} 頁，與輸入的 {total} 頁不符")

    out = outdir / (pdf.stem + ".md")
    out.write_text(to_markdown_file(result, pdf, total), encoding="utf-8")
    return out


def choose_default_provider() -> str:
    """只有 Gemini key 時自動選 Gemini；其餘維持既有的 OpenAI 預設。"""
    if not os.environ.get("OPENAI_API_KEY") and os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    return DEFAULT_PROVIDER


def main() -> int:
    ap = argparse.ArgumentParser(description="公文 PDF OCR 轉 Markdown")
    ap.add_argument("pdfs", nargs="*", help=f"PDF 檔案（預設：{DEFAULT_INDIR}/ 底下所有 .pdf）")
    ap.add_argument("-i", "--indir", default=DEFAULT_INDIR,
                    help=f"輸入目錄（預設：{DEFAULT_INDIR}/）")
    ap.add_argument("-o", "--outdir", default=DEFAULT_OUTDIR,
                    help=f"輸出目錄（預設：{DEFAULT_OUTDIR}/）")
    ap.add_argument("-p", "--provider", choices=sorted(PROVIDERS), default=None,
                    help="用哪家模型（預設：openai；只有 Gemini key 時自動選 gemini）")
    ap.add_argument("-m", "--model", default=None,
                    help="模型名稱（預設：各 provider 的 $..._MODEL 或內建預設值）")
    args = ap.parse_args()

    paths = collect_pdfs(args.pdfs, args.indir)
    if not paths:
        print(f"找不到 PDF 檔案（{args.indir}/）", file=sys.stderr)
        return 1

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    load_dotenv()
    provider = PROVIDERS[args.provider or choose_default_provider()]
    model = args.model or os.environ.get(provider.model_env) or provider.default_model
    client = provider.make_client()
    print(f"provider={provider.name} model={model}")

    failed = 0
    total_secs = 0.0
    for pdf in paths:
        print(f"處理 {pdf.name}")
        t0 = time.time()
        try:
            out = process(provider, client, model, pdf, outdir)
            secs = time.time() - t0
            total_secs += secs
            print(f"  -> {out}（{secs:.1f} 秒）")
        except Exception as e:
            failed += 1
            total_secs += time.time() - t0
            print(f"  失敗：{e}", file=sys.stderr)

    print(f"\n完成 {len(paths) - failed}/{len(paths)} 份，共 {total_secs:.1f} 秒")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
