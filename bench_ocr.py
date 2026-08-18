#!/usr/bin/env python3
"""地端 OCR 模型對比評測

拿同一批公文 PDF 餵給多個地端模型，跟 ocr_doc.py（雲端 Opus）產出的 .md
逐項比對，輸出量化結果。

注意：雲端版的輸出只是「參照」不是 ground truth，它本身也可能有錯。
所以相似度低不必然代表地端模型錯，但差距很大時通常是地端模型的問題。

用法:
    export LM_STUDIO_API_KEY=xxx
    python3 bench_ocr.py --base-url http://172.17.224.1:1234/v1 \
        --models gemma-4-e4b-uncensored-hauhaucs-aggressive \
                 qwen3.5-9b-uncensored-hauhaucs-aggressive
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
from pathlib import Path

import openai
import opencc
import yaml

import ocr_doc_local as L

S2T = opencc.OpenCC("s2t")

# opencc 的 s2t 會把這些「合法繁體異體字」也一起改寫（台→臺、栗→慄、群→羣），
# 不排除的話參照檔自己就會被誤判成 1~2% 簡體。
VARIANT_EXEMPT = set("台栗群裡裏峰註ందా")

# 拿來對照的關鍵欄位：錯一個字就是實質錯誤的那幾項。
# 直接從 OCR 出來的正文抓，不靠模型的 JSON 輸出——像 olmOCR 這種
# 專用 OCR 模型根本不吃 json_schema，用 JSON 評分等於在比錯的東西。
KEY_FIELDS = ["發文字號", "發文日期", "主旨"]

FIELD_RE = {
    "發文字號": re.compile(r"發文字號[：:\s]*([^\n]+)"),
    "發文日期": re.compile(r"發文日期[：:\s]*([^\n]+)"),
    # 主旨可能跨行，抓到「說明」或空行為止。
    "主旨": re.compile(r"主旨[：:\s]*(.+?)(?=\n\s*\n|\n\s*說明|$)", re.S),
}


def extract_fields(body: str) -> dict:
    """從 OCR 正文抓關鍵欄位。"""
    out = {}
    for name, rx in FIELD_RE.items():
        m = rx.search(body)
        if m:
            out[name] = m.group(1).strip()
    return out


def split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    _, _, rest = text.partition("---\n")
    front, sep, body = rest.partition("\n---")
    if not sep:
        return {}, text
    return yaml.safe_load(front) or {}, body.lstrip("\n")


def normalize(text: str) -> str:
    """只留下可比對的實體內容：CJK、英數。

    Markdown 語法、HTML 註解（印章標註）、空白、標點都拿掉，
    免得「排版風格不同」被誤判成「認錯字」。
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    return "".join(re.findall(r"[一-鿿0-9A-Za-z]", text))


def simplified_ratio(text: str) -> float:
    """簡體字佔比：s2t 轉換後會變的字就是簡體字。"""
    cjk = re.findall(r"[一-鿿]", text)
    if not cjk:
        return 0.0
    bad = sum(1 for c in cjk if c not in VARIANT_EXEMPT and S2T.convert(c) != c)
    return bad / len(cjk)


def repetition_ratio(text: str, n: int = 24) -> float:
    """重複迴圈偵測：出現 3 次以上的 n-gram 覆蓋了多少內容。

    小模型在密集頁面上常卡進複讀，這是最需要抓出來的失效模式。
    """
    s = normalize(text)
    if len(s) < n * 3:
        return 0.0
    pos: dict[str, list[int]] = {}
    for i in range(len(s) - n + 1):
        pos.setdefault(s[i : i + n], []).append(i)
    # 算「被重複區段實際覆蓋的字元位置」，重疊的 n-gram 才不會被重複計數。
    covered: set[int] = set()
    for g, idxs in pos.items():
        if len(idxs) >= 3:
            for i in idxs:
                covered.update(range(i, i + n))
    return len(covered) / len(s)


def field_score(got: dict, ref: dict) -> tuple[int, list[str]]:
    """關鍵欄位精確命中數（正規化後逐字相同才算），並回報對不上的欄位。"""
    hits, misses = 0, []
    for f in KEY_FIELDS:
        g, r = str(got.get(f, "")).strip(), str(ref.get(f, "")).strip()
        if not r:
            continue
        gn, rn = normalize(g), normalize(r)
        if gn and gn == rn:
            hits += 1
        else:
            misses.append(f"{f}: 得到 {g[:40]!r} / 參照 {r[:40]!r}")
    return hits, misses


def run_model(client, model: str, pdf: Path, dpi: int, max_edge: int,
              max_tokens: int) -> dict:
    images = L.render_pages(pdf, dpi, max_edge)
    t0 = time.time()

    # 只記錄「這個模型支不支援 json_schema」，分數不靠它。
    meta = {}
    meta_err = ""
    try:
        meta = json.loads(
            L.ask(client, model, images[0], L.META_PROMPT, L.META_SCHEMA, L.META_MAX_TOKENS)
        )
    except Exception as e:
        meta_err = f"{type(e).__name__}: {e}"[:120]

    parts, page_errs = [], []
    for i, png in enumerate(images, 1):
        try:
            prompt = L.PAGE_PROMPT.format(n=i, total=len(images))
            parts.append(L.strip_fence(L.ask(client, model, png, prompt, max_tokens=max_tokens)))
        except Exception as e:
            page_errs.append(f"p{i}: {type(e).__name__}")
    body = L.join_pages(parts)

    return {
        "meta": meta,
        "meta_err": meta_err,
        "body": body,
        "pages": len(images),
        "page_errs": page_errs,
        "secs": time.time() - t0,
    }


def main() -> int:
    L.load_dotenv()  # 要在 add_argument 之前，default 才吃得到 .env 的值
    ap = argparse.ArgumentParser(description="地端 OCR 模型對比評測")
    ap.add_argument("--models", nargs="+", required=True, help="要比較的模型 id")
    ap.add_argument("--base-url", default=os.environ.get("LM_STUDIO_BASE_URL", L.DEFAULT_BASE_URL))
    ap.add_argument("--api-key", default=os.environ.get("LM_STUDIO_API_KEY", "local"))
    ap.add_argument("--max-tokens", type=int, default=L.PAGE_MAX_TOKENS)
    ap.add_argument("--dpi", type=int, default=L.RENDER_DPI)
    ap.add_argument("--max-edge", type=int, default=L.MAX_EDGE)
    ap.add_argument("--pdfs", nargs="*",
                    help=f"預設：{L.DEFAULT_INDIR}/ 底下有對應參照 .md 的 pdf")
    ap.add_argument("--refdir", default=L.DEFAULT_OUTDIR,
                    help=f"雲端版輸出的參照 .md 所在目錄（預設 {L.DEFAULT_OUTDIR}/）")
    ap.add_argument("-o", "--outdir", default="bench_out", help="各模型原始輸出存放處")
    args = ap.parse_args()

    pairs = []
    for p in L.collect_pdfs(args.pdfs or [], L.DEFAULT_INDIR):
        ref = Path(args.refdir) / (p.stem + ".md")
        if ref.is_file():
            pairs.append((p, ref))
    if not pairs:
        print("找不到「PDF + 同名 .md 參照」的組合", file=sys.stderr)
        return 1

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    client = openai.OpenAI(base_url=args.base_url, api_key=args.api_key, timeout=1800)
    try:
        available = {m.id for m in client.models.list().data}
    except Exception as e:
        print(f"連不到 {args.base_url}：{e}", file=sys.stderr)
        return 1

    for m in args.models:
        if m not in available:
            print(f"警告：LM Studio 沒有 {m!r}", file=sys.stderr)
    print(f"端點 {args.base_url}，{len(pairs)} 份文件，{len(args.models)} 個模型\n")

    rows = []
    for model in args.models:
        print(f"=== {model} ===", flush=True)
        agg = {"sim": [], "fields": 0, "field_total": 0, "simp": [], "rep": [],
               "len": [], "secs": 0.0, "pages": 0, "errs": [], "misses": [], "json_ok": True}
        for pdf, refpath in pairs:
            ref_meta, ref_body = split_frontmatter(refpath.read_text(encoding="utf-8"))
            print(f"  {pdf.name[:40]}…", flush=True)
            r = run_model(client, model, pdf, args.dpi, args.max_edge, args.max_tokens)

            (outdir / f"{pdf.stem}__{model}.md").write_text(
                L.to_markdown_file(r["meta"], r["body"], pdf, r["pages"]), encoding="utf-8"
            )

            got_n, ref_n = normalize(r["body"]), normalize(ref_body)
            sim = difflib.SequenceMatcher(None, got_n, ref_n).ratio() if ref_n else 0.0
            hits, misses = field_score(extract_fields(r["body"]), ref_meta)

            agg["sim"].append(sim)
            agg["fields"] += hits
            agg["field_total"] += sum(1 for f in KEY_FIELDS if str(ref_meta.get(f, "")).strip())
            agg["simp"].append(simplified_ratio(r["body"]))
            agg["rep"].append(repetition_ratio(r["body"]))
            agg["len"].append(len(got_n) / len(ref_n) if ref_n else 0.0)
            agg["secs"] += r["secs"]
            agg["pages"] += r["pages"]
            if r["meta_err"]:
                agg["json_ok"] = False
            agg["errs"] += [f"{pdf.stem[:12]} {e}" for e in r["page_errs"]]
            agg["misses"] += [f"{pdf.stem[:12]} {m}" for m in misses]

            print(f"    相似度 {sim:.1%}  欄位 {hits}  {r['secs']:.0f}s", flush=True)

        n = len(pairs)
        rows.append({
            "model": model,
            "sim": sum(agg["sim"]) / n,
            "fields": f"{agg['fields']}/{agg['field_total']}",
            "simp": sum(agg["simp"]) / n,
            "rep": sum(agg["rep"]) / n,
            "len": sum(agg["len"]) / n,
            "spp": agg["secs"] / max(1, agg["pages"]),
            "json_ok": agg["json_ok"],
            "errs": agg["errs"],
            "misses": agg["misses"],
        })
        print()

    print("=" * 78)
    print(f"{'模型':<34}{'相似度':>8}{'欄位':>7}{'簡體':>7}{'複讀':>7}{'篇幅':>7}{'秒/頁':>7}{'JSON':>6}")
    print("-" * 78)
    for r in rows:
        print(f"{r['model'][:33]:<34}{r['sim']:>7.1%}{r['fields']:>7}"
              f"{r['simp']:>6.1%}{r['rep']:>7.1%}{r['len']:>7.2f}{r['spp']:>7.0f}"
              f"{'是' if r['json_ok'] else '否':>6}")
    print("=" * 78)
    print("相似度/篇幅 是相對雲端 Opus 輸出的參照值，非絕對正確率。")
    print("欄位=發文字號/發文日期/主旨 逐字命中數  JSON=支不支援結構化輸出")
    print("簡體=簡體字洩漏率(越低越好)  複讀=重複迴圈佔比(越低越好)  篇幅=1.0 表示長度與參照相當")

    for r in rows:
        if r["errs"] or r["misses"]:
            print(f"\n--- {r['model']} ---")
            for e in r["errs"][:8]:
                print(f"  錯誤 {e}")
            for m in r["misses"][:8]:
                print(f"  欄位 {m}")

    print(f"\n各模型完整輸出：{outdir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
