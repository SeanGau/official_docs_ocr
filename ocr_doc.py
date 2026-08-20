#!/usr/bin/env python3
"""公文 PDF -> Markdown（含 YAML frontmatter metadata）

用 PyMuPDF 把掃描 PDF 每頁轉成點陣圖，交給 Claude Vision 做 OCR + 版面重建 +
metadata 抽取，輸出成 .md 檔（開頭是 YAML frontmatter）。

輸出的正文會逐頁插入 `<!-- page: N/總頁數 -->` 標記，方便後續 RAG
切 chunk 時保留頁碼來源。

用法:
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 ocr_doc.py                    # input/ 底下所有 pdf -> output/
    python3 ocr_doc.py a.pdf b.pdf        # 只處理指定檔案
    python3 ocr_doc.py -i in/ -o out/     # 指定輸入／輸出目錄
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import anthropic
import pymupdf
import yaml

MODEL = "claude-opus-5"

# 輸入 PDF 與輸出 Markdown 各自有固定資料夾，不再散在當前目錄。
DEFAULT_INDIR = "input"
DEFAULT_OUTDIR = "output"


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


# Opus 5 高解析度視覺上限：長邊 2576px。超過會被縮放，等於白花 token。
MAX_EDGE = 2576
# A4 在 220 DPI 下約 1819x2573，剛好貼齊 MAX_EDGE，把高解析度額度用滿。
RENDER_DPI = 220

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


def render_pages(pdf_path: Path) -> list[bytes]:
    """把 PDF 每頁 render 成 PNG bytes，長邊不超過 MAX_EDGE。"""
    images = []
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=RENDER_DPI)
            if max(pix.width, pix.height) > MAX_EDGE:
                scale = MAX_EDGE / max(pix.width, pix.height)
                pix = page.get_pixmap(dpi=int(RENDER_DPI * scale))
            images.append(pix.tobytes("png"))
    return images


def build_content(images: list[bytes]) -> list[dict]:
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


def extract(client: anthropic.Anthropic, images: list[bytes]) -> dict:
    """呼叫 Claude 做 OCR + 抽 metadata，回傳 {'metadata': ..., 'markdown': ...}。"""
    # 影像多、輸出長，一律 streaming 避免 HTTP timeout。
    with client.messages.stream(
        model=MODEL,
        max_tokens=32000,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": SCHEMA},
        },
        messages=[{"role": "user", "content": build_content(images)}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError(f"模型拒絕處理：{message.stop_details}")
    if message.stop_reason == "max_tokens":
        raise RuntimeError("輸出被 max_tokens 截斷，請調高 max_tokens 或分批處理頁面")

    text = next(b.text for b in message.content if b.type == "text")
    return json.loads(text)


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


def process(client: anthropic.Anthropic, pdf: Path, outdir: Path) -> Path:
    images = render_pages(pdf)
    if len(images) > MAX_PAGES_PER_REQUEST:
        raise RuntimeError(
            f"{pdf.name} 有 {len(images)} 頁，超過單次上限 {MAX_PAGES_PER_REQUEST}"
        )
    print(f"  已 render {len(images)} 頁，送出辨識…", flush=True)

    result = extract(client, images)
    got = len(result["pages"])
    if got != len(images):
        # 頁碼標記是給 RAG 溯源用的，對不上就等於引用錯頁，寧可讓它爆掉。
        raise RuntimeError(f"模型回了 {got} 頁，與輸入的 {len(images)} 頁不符")

    out = outdir / (pdf.stem + ".md")
    out.write_text(to_markdown_file(result, pdf, len(images)), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="公文 PDF OCR 轉 Markdown")
    ap.add_argument("pdfs", nargs="*", help=f"PDF 檔案（預設：{DEFAULT_INDIR}/ 底下所有 .pdf）")
    ap.add_argument("-i", "--indir", default=DEFAULT_INDIR,
                    help=f"輸入目錄（預設：{DEFAULT_INDIR}/）")
    ap.add_argument("-o", "--outdir", default=DEFAULT_OUTDIR,
                    help=f"輸出目錄（預設：{DEFAULT_OUTDIR}/）")
    args = ap.parse_args()

    paths = collect_pdfs(args.pdfs, args.indir)
    if not paths:
        print(f"找不到 PDF 檔案（{args.indir}/）", file=sys.stderr)
        return 1

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    load_dotenv()
    client = anthropic.Anthropic()

    failed = 0
    total_secs = 0.0
    for pdf in paths:
        print(f"處理 {pdf.name}")
        t0 = time.time()
        try:
            out = process(client, pdf, outdir)
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
