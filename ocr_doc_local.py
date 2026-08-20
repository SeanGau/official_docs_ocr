#!/usr/bin/env python3
"""公文 PDF -> Markdown（地端版，走 LM Studio 的 OpenAI 相容 API）

與 ocr_doc.py 的差別：
  * 模型跑在本機（LM Studio），不需要 API 金鑰、不外傳文件。
  * 8GB 級距的視覺模型 context 有限，所以「逐頁」辨識再串起來，
    不像雲端版一次把整份文件送出。
  * metadata 分兩段抽：先讓 OCR 模型把第 1 頁轉成文字，再把「文字」交給
    文字模型抽欄位。olmocr 這類 OCR 專用模型不會 instruction following，
    直接叫它吐 JSON 一定失敗；而純文字階段不必看影像，小模型也做得準。

輸出的正文會逐頁插入 `<!-- page: N/總頁數 -->` 標記（與雲端版同格式），
方便後續 RAG 切 chunk 時保留頁碼來源。

用法:
    # LM Studio 先載入一個 vision 模型並啟動 server
    python3 ocr_doc_local.py                    # input/ 底下所有 pdf -> output/
    python3 ocr_doc_local.py a.pdf -o out/
    python3 ocr_doc_local.py --base-url http://172.17.224.1:1234/v1
    python3 ocr_doc_local.py --model qwen2.5-vl-7b-instruct --dpi 180
    python3 ocr_doc_local.py --meta-model qwen3.5-9b      # 指定抽 metadata 的文字模型
    python3 ocr_doc_local.py --no-meta                    # 只要內文，不抽 metadata
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import openai
import pymupdf
import yaml

# .env 讀取、輸入蒐集、頁碼標記格式都跟雲端版共用，不另外複製一份。
from ocr_doc import DEFAULT_INDIR, DEFAULT_OUTDIR, PAGE_MARKER, collect_pdfs, load_dotenv

# WSL 連 Windows 上的 LM Studio 要走 gateway，不是 127.0.0.1，
# 所以端點做成可用 .env 的 LM_STUDIO_BASE_URL 覆寫。
DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"

# 地端小模型的影像 token 很貴又容易失焦，解析度抓「字看得清楚」就好，
# 不像雲端版把高解析度額度用滿。150 DPI 的 A4 約 1240x1754。
RENDER_DPI = 150
MAX_EDGE = 1600

SYSTEM_PROMPT = """\
你是台灣公文數位化專家，負責把掃描的公文影像轉成 Markdown。

要求：
1. 逐字精確 OCR，輸出繁體中文。機關名稱、人名、地名務必正確。
2. 保留公文層次：主旨、說明（一、二、三…）、辦法等，
   子項 (一)(二)、1.2.3. 用 Markdown 巢狀清單表示。
3. 表格用 Markdown 表格重建。
4. 印章、簽名、浮水印用 `<!-- 印章：OO部 -->` 這類 HTML 註解標註，不要當正文。
   手寫批註同樣用註解並註明「手寫」。
5. 完全無法辨識的字用 `〇`，不要臆測。
6. 民國紀年保留原文，不要換算。
7. 只輸出公文內容，不要加任何說明、前言或程式碼圍籬。
"""

PAGE_PROMPT = "這是公文的第 {n} 頁（共 {total} 頁）。請把這一頁的內容轉成 Markdown。"

META_SYSTEM = """\
你從台灣公文的 OCR 文字中抽取欄位。
只根據文字中確實出現的內容填寫，找不到的欄位填空字串，絕不推測或編造。
"""

META_PROMPT = """\
以下是公文第 1 頁的 OCR 文字：

{text}

請抽出欄位，以 JSON 回覆。
"""

# 欄位是從「OCR 出來的文字」抽的，不是看影像猜的，所以可以多抽幾個；
# 但仍只取公文首頁一定有的項目，避免要模型跨頁推論。
META_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["發文機關", "受文者", "發文日期", "發文字號", "速別",
                 "密等及解密條件", "附件", "主旨"],
    "properties": {
        "發文機關": {"type": "string", "description": "發文的機關全銜"},
        "受文者": {"type": "string"},
        "發文日期": {"type": "string", "description": "原件民國紀年，如「中華民國110年8月31日」"},
        "發文字號": {"type": "string", "description": "如「開字第1100831001號」"},
        "速別": {"type": "string", "description": "普通件／速件／最速件"},
        "密等及解密條件": {"type": "string"},
        "附件": {"type": "array", "items": {"type": "string"}},
        "主旨": {"type": "string", "description": "主旨欄全文，不含「主旨：」三字"},
    },
}

# 文別不是獨立欄位，而是印在首行機關全銜後面（「○○○　函」）。位置固定，
# 用 regex 抓；交給 4B 級小模型判斷實測三份全錯（''、「說明」、「N/A」）。
DOC_TYPES = ("開會通知單", "書函", "公告", "移文單", "報告", "函", "令", "簽")

# 主旨同樣位置固定：「主旨：」起、「說明：」或空行止。實測 gemma-4-e4b-it 會在
# 後面黏上自評（「(註：原文結尾為…)」），qwen3.5-9b 則有時把整段說明吞進來，
# 兩個模型都髒過，所以能用 regex 抓就不要問模型。
SUBJECT_RE = re.compile(r"主旨[：:]\s*(.+?)(?=\n\s*\n|\n?\s*說明[：:]|$)", re.S)


def render_pages(pdf_path: Path, dpi: int, max_edge: int) -> list[bytes]:
    """把 PDF 每頁 render 成 PNG bytes，長邊不超過 max_edge。"""
    images = []
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            if max(pix.width, pix.height) > max_edge:
                scale = max_edge / max(pix.width, pix.height)
                pix = page.get_pixmap(dpi=int(dpi * scale))
            images.append(pix.tobytes("png"))
    return images


def image_part(png: bytes) -> dict:
    b64 = base64.standard_b64encode(png).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}


# 欄位雖然短，但 meta model 多半是 reasoning 模型，額度要含 thinking 的量。
# 實測 gemma-4-e4b-it 約 800、qwen3.5-9b 要到 6000 才把 8 個欄位抽完；
# 給太少不會報「答錯」，而是 content 空掉、metadata 整份抽不到。
META_MAX_TOKENS = 8192
PAGE_MAX_TOKENS = 4096


def ask(
    client,
    model: str,
    png: bytes | None,
    prompt: str,
    schema: dict | None = None,
    max_tokens: int = PAGE_MAX_TOKENS,
    system: str = SYSTEM_PROMPT,
) -> str:
    kwargs = {}
    if schema is not None:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "metadata", "strict": True, "schema": schema},
        }
    content_parts = [{"type": "text", "text": prompt}]
    if png is not None:
        content_parts.insert(0, image_part(png))
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0,  # OCR 不需要創意，壓掉隨機性
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": content_parts},
        ],
        **kwargs,
    )
    choice = resp.choices[0]
    content = (choice.message.content or "").strip()
    reasoning = (getattr(choice.message, "reasoning_content", None) or "").strip()

    # 截斷要最先判掉。thinking 被切一半時，下面那段 fallback 會把殘缺的 JSON
    # 當成答案回傳，錯誤最後才在 json.loads 爆成看不懂的 JSONDecodeError。
    if choice.finish_reason == "length":
        used = getattr(resp.usage.completion_tokens_details, "reasoning_tokens", 0)
        detail = f"，其中 thinking 用掉 {used}" if used else ""
        raise RuntimeError(f"輸出被 max_tokens 截斷（目前 {max_tokens}{detail}）")

    # 有些 reasoning 模型（實測 qwen3.5 的社群 build）會把 schema 限制過的 JSON
    # 整段吐在 thinking 頻道，content 反而是空的；那份 JSON 本身是對的，直接用。
    if not content and reasoning and schema is not None:
        return reasoning

    # 非 structured 的情況下 content 空掉，多半是 token 全花在 thinking 上，
    # 靜默回空字串會很難查，直接報出來。
    if not content and reasoning:
        used = getattr(resp.usage.completion_tokens_details, "reasoning_tokens", "?")
        raise RuntimeError(
            f"模型把 {used} 個 token 全花在 thinking 上，content 是空的；"
            f"請調高 --max-tokens（目前 {max_tokens}）"
        )
    return content


def strip_fence(text: str) -> str:
    """小模型常自作主張包上 ```markdown 圍籬，拆掉。"""
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def resolve_model(client, wanted: str | None) -> str:
    """沒指定 --model 就用 LM Studio 目前載入的第一個模型。"""
    if wanted:
        return wanted
    models = [m.id for m in client.models.list().data]
    if not models:
        raise RuntimeError("LM Studio 沒有載入任何模型")
    return models[0]


def to_markdown_file(meta: dict, body: str, source: Path, pages: int) -> str:
    meta = dict(meta)
    meta["來源檔案"] = source.name
    meta["頁數"] = pages
    front = yaml.safe_dump(
        meta,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1000,
    )
    return f"---\n{front}---\n\n{body.strip()}\n"


def extract_meta(client, meta_model: str, page1_md: str) -> dict:
    """從第 1 頁的 OCR 文字抽 metadata（純文字，不再看影像）。"""
    raw = ask(
        client,
        meta_model,
        None,
        META_PROMPT.format(text=page1_md),
        META_SCHEMA,
        META_MAX_TOKENS,
        system=META_SYSTEM,
    )
    meta = json.loads(raw)
    meta["文別"] = doc_type(page1_md)
    meta["主旨"] = subject(page1_md) or meta.get("主旨", "")
    return meta


def subject(page1_md: str) -> str:
    """從第 1 頁抓主旨全文；抓不到回空字串，由呼叫端沿用模型抽的值。"""
    m = SUBJECT_RE.search(page1_md)
    return " ".join(m.group(1).split()) if m else ""


def doc_type(page1_md: str) -> str:
    """從第 1 頁前幾行的行尾抓文別。"""
    for line in page1_md.splitlines()[:6]:
        line = line.strip().rstrip("　 ")
        for t in DOC_TYPES:
            if line.endswith(t) and len(line) > len(t):
                return t
    return ""


def ocr_pdf(client, model: str, pdf: Path, dpi: int, max_edge: int,
            max_tokens: int) -> list[str]:
    """逐頁 OCR，回傳每頁的 Markdown。"""
    images = render_pages(pdf, dpi, max_edge)
    total = len(images)
    print(f"  已 render {total} 頁，逐頁辨識…", flush=True)
    parts = []
    for i, png in enumerate(images, 1):
        print(f"    第 {i}/{total} 頁…", flush=True)
        parts.append(
            strip_fence(ask(client, model, png, PAGE_PROMPT.format(n=i, total=total),
                            max_tokens=max_tokens))
        )
    return parts


def join_pages(parts: list[str]) -> str:
    """把逐頁 Markdown 串成正文，每頁前面加頁碼標記。

    不插 `---` 分隔線：公文的段落、表格常常跨頁，多一條線反而會讓
    RAG 的 chunker 在句子中間硬切。
    """
    total = len(parts)
    return "\n\n".join(
        f"{PAGE_MARKER.format(n=i, total=total)}\n\n{md.strip()}"
        for i, md in enumerate(parts, 1)
        if md.strip()
    )


def write_doc(pdf: Path, outdir: Path, parts: list[str], meta: dict) -> Path:
    body = join_pages(parts)
    out = outdir / (pdf.stem + ".local.md")
    out.write_text(to_markdown_file(meta, body, pdf, len(parts)), encoding="utf-8")
    return out


def main() -> int:
    load_dotenv()  # 要在 add_argument 之前，default 才吃得到 .env 的值
    ap = argparse.ArgumentParser(description="公文 PDF OCR 轉 Markdown（地端模型）")
    ap.add_argument("pdfs", nargs="*", help=f"PDF 檔案（預設：{DEFAULT_INDIR}/ 底下所有 .pdf）")
    ap.add_argument("-i", "--indir", default=DEFAULT_INDIR,
                    help=f"輸入目錄（預設：{DEFAULT_INDIR}/）")
    ap.add_argument("-o", "--outdir", default=DEFAULT_OUTDIR,
                    help=f"輸出目錄（預設：{DEFAULT_OUTDIR}/）")
    ap.add_argument(
        "--base-url",
        default=os.environ.get("LM_STUDIO_BASE_URL", DEFAULT_BASE_URL),
        help="OpenAI 相容端點（或設 .env 的 LM_STUDIO_BASE_URL）",
    )
    ap.add_argument("--model", help="OCR 模型名稱（預設：用 LM Studio 已載入的第一個）")
    ap.add_argument(
        "--meta-model",
        default=os.environ.get("LM_STUDIO_META_MODEL"),
        help="抽 metadata 的文字模型（預設同 --model；OCR 專用模型請另外指定一個文字模型）",
    )
    ap.add_argument("--no-meta", action="store_true", help="只輸出內文，不抽 metadata")
    ap.add_argument(
        "--api-key",
        default=os.environ.get("LM_STUDIO_API_KEY", "local"),
        help="LM Studio API token（或設環境變數 LM_STUDIO_API_KEY；未開驗證時免填）",
    )
    ap.add_argument(
        "--max-tokens", type=int, default=PAGE_MAX_TOKENS,
        help=f"每頁輸出上限（預設 {PAGE_MAX_TOKENS}；reasoning 模型要留 thinking 的量）",
    )
    ap.add_argument("--dpi", type=int, default=RENDER_DPI, help=f"render DPI（預設 {RENDER_DPI}）")
    ap.add_argument("--max-edge", type=int, default=MAX_EDGE, help=f"影像長邊上限（預設 {MAX_EDGE}）")
    args = ap.parse_args()

    paths = collect_pdfs(args.pdfs, args.indir)
    if not paths:
        print(f"找不到 PDF 檔案（{args.indir}/）", file=sys.stderr)
        return 1

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    # LM Studio 未開驗證時 token 隨便填即可，但 SDK 硬性要求非空字串。
    client = openai.OpenAI(base_url=args.base_url, api_key=args.api_key, timeout=600)
    try:
        model = resolve_model(client, args.model)
    except Exception as e:
        print(f"連不到 {args.base_url}：{e}", file=sys.stderr)
        return 1
    meta_model = None if args.no_meta else (args.meta_model or model)
    print(f"端點 {args.base_url}，OCR 模型 {model}"
          + (f"，metadata 模型 {meta_model}" if meta_model else "，不抽 metadata"))

    # 先把所有文件 OCR 完，再統一抽 metadata。兩個模型交錯呼叫會讓 LM Studio
    # 一直換載入，實測會把模型跑到 crash。
    # 每份文件的時間分兩段累計（OCR、metadata），最後一起報。
    done: list[tuple[Path, list[str], float]] = []
    failed = 0
    for pdf in paths:
        print(f"處理 {pdf.name}")
        t0 = time.time()
        try:
            parts = ocr_pdf(client, model, pdf, args.dpi, args.max_edge,
                            args.max_tokens)
            secs = time.time() - t0
            done.append((pdf, parts, secs))
            print(f"  OCR {secs:.1f} 秒（{secs / max(len(parts), 1):.1f} 秒/頁）")
        except Exception as e:
            failed += 1
            print(f"  失敗：{e}（{time.time() - t0:.1f} 秒）", file=sys.stderr)

    total_secs = 0.0
    for pdf, parts, ocr_secs in done:
        meta = {}
        t0 = time.time()
        if meta_model and parts:
            print(f"抽 metadata {pdf.name}（{meta_model}）…", flush=True)
            try:
                meta = extract_meta(client, meta_model, parts[0])
            except (json.JSONDecodeError, openai.APIError, RuntimeError) as e:
                # 抽不到 metadata 不該讓已經辨識好的內文一起白費。
                print(f"  metadata 抽取失敗（{e}），改為只輸出內文", file=sys.stderr)
        meta_secs = time.time() - t0
        total_secs += ocr_secs + meta_secs
        print(f"  -> {write_doc(pdf, outdir, parts, meta)}"
              f"（共 {ocr_secs + meta_secs:.1f} 秒＝OCR {ocr_secs:.1f}"
              f" + metadata {meta_secs:.1f}）")

    print(f"\n完成 {len(paths) - failed}/{len(paths)} 份，共 {total_secs:.1f} 秒")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
