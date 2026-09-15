#!/usr/bin/env python3
"""NVIDIA Nemotron-Parse 2.0 地端 OCR 評測。

拿同一批公文 PDF 跟 ocr_doc.py（雲端版）產出的 .md 逐項比對，
輸出相似度、欄位命中率、簡體字洩漏率、複讀比例與速度。

注意：雲端版輸出只是參照，不是 ground truth。

用法:
    uv run --extra local --extra bench bench_ocr.py
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
import time
from pathlib import Path

import opencc
import yaml

import ocr_doc_local as L

S2T = opencc.OpenCC("s2t")

# opencc 的 s2t 會把這些「合法繁體異體字」也一起改寫（台→臺、栗→慄、群→羣），
# 不排除的話參照檔自己就會被誤判成 1~2% 簡體。
VARIANT_EXEMPT = set("台栗群裡裏峰註ందా")

# 直接從 OCR 正文抓關鍵欄位，不依賴額外語言模型。
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


def run_model(
    runtime: L.NemotronRuntime,
    pdf: Path,
    dpi: int,
    max_width: int,
    max_height: int,
    max_tokens: int,
) -> dict:
    images = L.render_pages(pdf, dpi, max_width, max_height)
    started = time.time()
    parts, page_errs = [], []
    for i, png in enumerate(images, 1):
        try:
            parts.append(L.parse_page(runtime, png, max_tokens))
        except Exception as e:
            page_errs.append(f"p{i}: {type(e).__name__}")
    body = L.join_pages(parts)
    return {
        "meta": L.extract_meta(parts[0]) if parts else {},
        "body": body,
        "pages": len(images),
        "page_errs": page_errs,
        "secs": time.time() - started,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Nemotron-Parse 2.0 地端 OCR 評測")
    ap.add_argument(
        "--model",
        default=L.DEFAULT_MODEL,
        help=f"Hugging Face 模型 ID 或本機目錄（預設 {L.DEFAULT_MODEL}）",
    )
    ap.add_argument(
        "--device",
        default="auto",
        help="Transformers 裝置（預設 auto，自動使用 cuda:0 或 cpu）",
    )
    ap.add_argument("--local-files-only", action="store_true")
    ap.add_argument("--max-tokens", type=int, default=L.PAGE_MAX_TOKENS)
    ap.add_argument("--dpi", type=int, default=L.RENDER_DPI)
    ap.add_argument("--max-width", type=int, default=L.MAX_WIDTH)
    ap.add_argument("--max-height", type=int, default=L.MAX_HEIGHT)
    ap.add_argument("--pdfs", nargs="*",
                    help=f"預設：{L.DEFAULT_INDIR}/ 底下有對應參照 .md 的 pdf")
    ap.add_argument("--refdir", default=L.DEFAULT_OUTDIR,
                    help=f"雲端版輸出的參照 .md 所在目錄（預設 {L.DEFAULT_OUTDIR}/）")
    ap.add_argument("-o", "--outdir", default="bench_out", help="評測原始輸出存放處")
    args = ap.parse_args()

    pairs = []
    for pdf in L.collect_pdfs(args.pdfs or [], L.DEFAULT_INDIR):
        ref = Path(args.refdir) / (pdf.stem + ".md")
        if ref.is_file():
            pairs.append((pdf, ref))
    if not pairs:
        print("找不到「PDF + 同名 .md 參照」的組合", file=sys.stderr)
        return 1

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"載入 OCR 模型 {args.model}…", flush=True)
    try:
        runtime = L.NemotronRuntime(args.model, args.device, args.local_files_only)
    except Exception as e:
        print(f"Nemotron 模型載入失敗：{e}", file=sys.stderr)
        return 1

    print(f"Direct Transformers，裝置 {runtime.device}，"
          f"{len(pairs)} 份文件，模型 {args.model}\n")
    aggregate = {
        "sim": [],
        "fields": 0,
        "field_total": 0,
        "simp": [],
        "rep": [],
        "len": [],
        "secs": 0.0,
        "pages": 0,
        "errs": [],
        "misses": [],
    }
    for pdf, refpath in pairs:
        ref_meta, ref_body = split_frontmatter(refpath.read_text(encoding="utf-8"))
        print(f"  {pdf.name[:40]}…", flush=True)
        result = run_model(
            runtime,
            pdf,
            args.dpi,
            args.max_width,
            args.max_height,
            args.max_tokens,
        )
        (outdir / f"{pdf.stem}__nemotron-parse-2.0.md").write_text(
            L.to_markdown_file(result["meta"], result["body"], pdf, result["pages"]),
            encoding="utf-8",
        )

        got_n, ref_n = normalize(result["body"]), normalize(ref_body)
        similarity = difflib.SequenceMatcher(None, got_n, ref_n).ratio() if ref_n else 0.0
        hits, misses = field_score(extract_fields(result["body"]), ref_meta)
        aggregate["sim"].append(similarity)
        aggregate["fields"] += hits
        aggregate["field_total"] += sum(
            1 for field in KEY_FIELDS if str(ref_meta.get(field, "")).strip()
        )
        aggregate["simp"].append(simplified_ratio(result["body"]))
        aggregate["rep"].append(repetition_ratio(result["body"]))
        aggregate["len"].append(len(got_n) / len(ref_n) if ref_n else 0.0)
        aggregate["secs"] += result["secs"]
        aggregate["pages"] += result["pages"]
        aggregate["errs"] += [f"{pdf.stem[:12]} {e}" for e in result["page_errs"]]
        aggregate["misses"] += [f"{pdf.stem[:12]} {m}" for m in misses]
        print(f"    相似度 {similarity:.1%}  欄位 {hits}  {result['secs']:.0f}s", flush=True)

    count = len(pairs)
    fields = f"{aggregate['fields']}/{aggregate['field_total']}"
    print("\n" + "=" * 72)
    print(f"{'模型':<34}{'相似度':>8}{'欄位':>7}{'簡體':>7}{'複讀':>7}{'篇幅':>7}{'秒/頁':>7}")
    print("-" * 72)
    print(
        f"{L.DEFAULT_MODEL[:33]:<34}"
        f"{sum(aggregate['sim']) / count:>7.1%}{fields:>7}"
        f"{sum(aggregate['simp']) / count:>6.1%}"
        f"{sum(aggregate['rep']) / count:>7.1%}"
        f"{sum(aggregate['len']) / count:>7.2f}"
        f"{aggregate['secs'] / max(1, aggregate['pages']):>7.0f}"
    )
    print("=" * 72)
    print("相似度/篇幅是相對雲端版輸出的參照值，非絕對正確率。")
    print("欄位=發文字號/發文日期/主旨逐字命中數。")
    print("簡體=簡體字洩漏率；複讀=重複迴圈佔比；篇幅=1.0 表示與參照等長。")

    if aggregate["errs"] or aggregate["misses"]:
        print(f"\n--- {L.DEFAULT_MODEL} ---")
        for error in aggregate["errs"][:8]:
            print(f"  錯誤 {error}")
        for miss in aggregate["misses"][:8]:
            print(f"  欄位 {miss}")

    print(f"\n完整輸出：{outdir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
