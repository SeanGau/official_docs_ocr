#!/usr/bin/env python3
"""公文 PDF -> Markdown（地端版，直接使用 Hugging Face Transformers）

地端版固定使用 NVIDIA Nemotron-Parse 2.0，不接受其他 OCR 模型：
  * 官方模型架構是 C-RADIO ViT-H vision encoder + mBART decoder，不含 Qwen base。
  * 逐頁辨識後串接，並保留與雲端版相同的頁碼標記。
  * metadata 直接從 OCR 文字的固定欄位抽取，不再載入第二個語言模型。

模型會由 Hugging Face 首次下載並留在 cache，不需要另外啟動服務：
    uv run --extra local ocr_doc_local.py
    uv run --extra local ocr_doc_local.py a.pdf -o out/
    uv run --extra local ocr_doc_local.py --model /path/to/model
    uv run --extra local ocr_doc_local.py --no-meta
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
import warnings
from collections.abc import Callable
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

import pymupdf
import torch
import yaml
from PIL import Image
from transformers import AutoModel, AutoProcessor, AutoTokenizer, GenerationConfig

from ocr_doc import DEFAULT_INDIR, DEFAULT_OUTDIR, PAGE_MARKER, collect_pdfs

# Hugging Face 模型 ID；--model 也接受已下載的本機目錄。
DEFAULT_MODEL = "nvidia/NVIDIA-Nemotron-Parse-2.0"

# 官方建議輸入範圍為 1024x1280 至 1664x2048；保留長寬比並限制上界。
RENDER_DPI = 150
MAX_WIDTH = 1664
MAX_HEIGHT = 2048

# 官方建議 prompt：輸出 bbox、語意類別與 Markdown，不辨識圖片區塊內文字。
NEMOTRON_PROMPT = (
    "</s><s><predict_bbox><predict_classes>"
    "<output_markdown><predict_no_text_in_pic>"
)

# metadata 只從 OCR 文字中位置固定的欄位擷取，不呼叫第二個模型。
META_FIELDS = (
    "發文機關",
    "受文者",
    "發文日期",
    "發文字號",
    "速別",
    "密等及解密條件",
    "附件",
    "主旨",
)

# 文別不是獨立欄位，而是印在首行機關全銜後面（「○○○　函」）。位置固定，
# 用 regex 抓；交給 4B 級小模型判斷實測三份全錯（''、「說明」、「N/A」）。
DOC_TYPES = ("開會通知單", "書函", "公告", "移文單", "報告", "函", "令", "簽")

# 主旨位置固定：「主旨：」起、「說明：」或空行止。
SUBJECT_RE = re.compile(
    r"(?:^|\n)\s*(?:[#>*+\-]+\s*)*主旨[：:]\s*"
    r"(.+?)(?=\n\s*(?:[#>*+\-]+\s*)*說明[：:]|\n\s*\n|$)",
    re.S,
)
NEMOTRON_BLOCK_RE = re.compile(
    r"<x_(?:\d+(?:\.\d+)?)><y_(?:\d+(?:\.\d+)?)>"
    r"(.*?)"
    r"<x_(?:\d+(?:\.\d+)?)><y_(?:\d+(?:\.\d+)?)>"
    r"<class_([^>]+)>",
    re.S,
)


def render_pages(
    pdf_path: Path,
    dpi: int,
    max_width: int,
    max_height: int,
) -> list[bytes]:
    """把 PDF 每頁 render 成 PNG bytes，符合 Nemotron 的輸入尺寸上限。"""
    images = []
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            scale = min(
                dpi / 72,
                max_width / page.rect.width,
                max_height / page.rect.height,
            )
            pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            images.append(pix.tobytes("png"))
    return images


PAGE_MAX_TOKENS = 8192
BLANK_PIXEL_THRESHOLD = 245
BLANK_MAX_INK_RATIO = 0.0005


def _is_blank_page(image: Image.Image) -> bool:
    """判斷頁面是否只有白底或極少量 render 雜點。"""
    with image.convert("L") as grayscale:
        histogram = grayscale.histogram()
        ink_pixels = sum(histogram[:BLANK_PIXEL_THRESHOLD])
        return ink_pixels <= grayscale.width * grayscale.height * BLANK_MAX_INK_RATIO




class _MessageFilter(logging.Filter):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def filter(self, record: logging.LogRecord) -> bool:
        return self.text not in record.getMessage()


@contextmanager
def _quiet_nemotron_startup():
    """只隱藏固定上游版本在載入 Nemotron 時產生的已知無害訊息。"""
    logger_filters = [
        (
            logging.getLogger("huggingface_hub.utils._http"),
            _MessageFilter("You are sending unauthenticated requests to the HF Hub"),
        ),
        (
            logging.getLogger("timm.models._builder"),
            _MessageFilter("No pretrained configuration specified for"),
        ),
        (
            logging.getLogger("transformers.configuration_utils"),
            _MessageFilter("`torch_dtype` is deprecated!"),
        ),
    ]
    for logger, message_filter in logger_filters:
        logger.addFilter(message_filter)
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"`torch\.jit\.interface` is deprecated\..*",
                category=FutureWarning,
                module=r"torch\.jit\._script",
            )
            warnings.filterwarnings(
                "ignore",
                message=r"Importing from timm\.models\.registry is deprecated.*",
                category=FutureWarning,
                module=r"timm\.models\.registry",
            )
            warnings.filterwarnings(
                "ignore",
                message=r"Importing from timm\.models\.layers is deprecated.*",
                category=FutureWarning,
                module=r"timm\.models\.layers(?:\.__init__)?",
            )
            yield
    finally:
        for logger, message_filter in logger_filters:
            logger.removeFilter(message_filter)


class NemotronRuntime:
    """只載入一次模型，逐頁直接執行 Transformers generation。"""

    def __init__(
        self,
        model_path: str = DEFAULT_MODEL,
        device: str = "auto",
        local_files_only: bool = False,
    ) -> None:
        if device == "auto":
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
        if device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("找不到可用的 CUDA GPU；可用 --device cpu 強制使用 CPU")

        self.device = torch.device(device)
        dtype = torch.bfloat16 if self.device.type == "cuda" else torch.float32
        load_args = {
            "trust_remote_code": True,
            "local_files_only": local_files_only,
        }
        with _quiet_nemotron_startup():
            self.model = AutoModel.from_pretrained(
                model_path,
                dtype=dtype,
                **load_args,
            ).to(self.device).eval()
            self.tokenizer = AutoTokenizer.from_pretrained(model_path, **load_args)
            self.processor = AutoProcessor.from_pretrained(model_path, **load_args)
            self.generation_config = GenerationConfig.from_pretrained(model_path, **load_args)

    def parse_page(self, png: bytes, max_tokens: int) -> str:
        """解析一頁；視覺上為空白時回傳空字串，不執行模型。"""
        with Image.open(BytesIO(png)) as source:
            image = source.convert("RGB")
        if _is_blank_page(image):
            return ""
        inputs = self.processor(
            images=[image],
            text=NEMOTRON_PROMPT,
            return_tensors="pt",
            add_special_tokens=False,
        ).to(self.device)

        generation_config = GenerationConfig.from_dict(self.generation_config.to_dict())
        generation_config.max_new_tokens = max_tokens
        generation_config.do_sample = False
        generation_config.num_beams = 1
        generation_config.repetition_penalty = 1.1
        # Nemotron 官方 remote code 仍呼叫 Transformers 5.6.1 的舊 mask helper。
        # 只在 generation 範圍忽略這一則已知相容性警告，其他警告照常顯示。
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"The attention mask API under .* is deprecated .*",
                category=FutureWarning,
                module=r"transformers\.modeling_attn_mask_utils",
            )
            with torch.inference_mode():
                output_ids = self.model.generate(
                    **inputs,
                    generation_config=generation_config,
                )

        eos_ids = generation_config.eos_token_id
        eos_ids = {eos_ids} if isinstance(eos_ids, int) else set(eos_ids or ())
        if output_ids.shape[-1] >= max_tokens and output_ids[0, -1].item() not in eos_ids:
            raise RuntimeError(f"輸出被 max_tokens 截斷（目前 {max_tokens}）")
        content = self.processor.batch_decode(output_ids, skip_special_tokens=True)[0].strip()
        if not content:
            raise RuntimeError("Nemotron-Parse 2.0 回傳空內容")
        return nemotron_to_markdown(content)


def parse_page(runtime: NemotronRuntime, png: bytes, max_tokens: int = PAGE_MAX_TOKENS) -> str:
    """以 Direct Transformers 解析單頁；空白頁回傳空字串。"""
    return runtime.parse_page(png, max_tokens)




def nemotron_to_markdown(text: str) -> str:
    """移除 Nemotron 的 bbox/class 包裝，保留 reading order 與 Markdown。"""
    parts = []
    for match in NEMOTRON_BLOCK_RE.finditer(text):
        content, cls = match.groups()
        content = (
            content.replace("<tbc>", "")
            .replace(r"\<|unk|\>", "")
            .replace(r"\unknown", "")
            .strip()
        )
        if content and cls != "Picture":
            parts.append(content)
    if not parts:
        raise RuntimeError("Nemotron-Parse 2.0 輸出不含可辨識的版面區塊")
    return "\n\n".join(parts)




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


def extract_meta(page1_md: str) -> dict:
    """從第 1 頁 OCR 文字的固定標籤抽 metadata。"""
    meta: dict[str, str | list[str]] = {field: "" for field in META_FIELDS}
    meta["附件"] = []

    for field in META_FIELDS[1:-2]:
        value = field_value(page1_md, field)
        if value:
            meta[field] = value

    attachment = field_value(page1_md, "附件")
    if attachment and attachment not in {"無", "無附件"}:
        meta["附件"] = [
            item.strip()
            for item in re.split(r"[、；;]", attachment)
            if item.strip()
        ]

    meta["發文機關"] = issuing_agency(page1_md)
    meta["文別"] = doc_type(page1_md)
    meta["主旨"] = subject(page1_md)
    return meta


def field_value(text: str, field: str) -> str:
    match = re.search(
        rf"(?:^|\n)\s*(?:[#>*+\-]+\s*)*{re.escape(field)}[：:][ \t　]*([^\n]*)",
        text,
    )
    return " ".join(match.group(1).strip(" *_`").split()) if match else ""


def issuing_agency(page1_md: str) -> str:
    """從首頁前幾行的「機關全銜 + 文別」取出發文機關。"""
    for raw_line in page1_md.splitlines()[:8]:
        line = re.sub(r"^\s*[#>*+\-]+\s*", "", raw_line).strip(" *_`　")
        for document_type in DOC_TYPES:
            if line.endswith(document_type) and len(line) > len(document_type):
                return line[: -len(document_type)].rstrip(" 　")
    return field_value(page1_md, "發文機關")


def subject(page1_md: str) -> str:
    """從第 1 頁抓主旨全文。"""
    match = SUBJECT_RE.search(page1_md)
    return " ".join(match.group(1).strip(" *_`").split()) if match else ""


def doc_type(page1_md: str) -> str:
    """從第 1 頁前幾行的行尾抓文別。"""
    for raw_line in page1_md.splitlines()[:8]:
        line = re.sub(r"^\s*[#>*+\-]+\s*", "", raw_line).strip(" *_`　")
        for document_type in DOC_TYPES:
            if line.endswith(document_type) and len(line) > len(document_type):
                return document_type
    return ""


def ocr_pdf(
    runtime: NemotronRuntime,
    pdf: Path,
    dpi: int,
    max_width: int,
    max_height: int,
    max_tokens: int,
    on_page: Callable[[int, int], None] | None = None,
) -> list[str]:
    """逐頁以 Nemotron-Parse 2.0 OCR，回傳每頁 Markdown。

    on_page(第幾頁, 總頁數) 在每頁辨識前呼叫；GUI 用它更新進度，也可在裡面丟例外中止。
    """
    images = render_pages(pdf, dpi, max_width, max_height)
    total = len(images)
    print(f"  已 render {total} 頁，逐頁辨識…", flush=True)
    parts = []
    for i, png in enumerate(images, 1):
        print(f"    第 {i}/{total} 頁…", flush=True)
        if on_page:
            on_page(i, total)
        part = parse_page(runtime, png, max_tokens)
        parts.append(part)
        if not part:
            print("      空白頁，保留頁數並繼續處理", flush=True)
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
    ap = argparse.ArgumentParser(description="公文 PDF OCR 轉 Markdown（地端模型）")
    ap.add_argument("pdfs", nargs="*", help=f"PDF 檔案（預設：{DEFAULT_INDIR}/ 底下所有 .pdf）")
    ap.add_argument("-i", "--indir", default=DEFAULT_INDIR,
                    help=f"輸入目錄（預設：{DEFAULT_INDIR}/）")
    ap.add_argument("-o", "--outdir", default=DEFAULT_OUTDIR,
                    help=f"輸出目錄（預設：{DEFAULT_OUTDIR}/）")
    ap.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Hugging Face 模型 ID 或本機目錄（預設 {DEFAULT_MODEL}）",
    )
    ap.add_argument(
        "--device",
        default="auto",
        help="Transformers 裝置（預設 auto，自動使用 cuda:0 或 cpu）",
    )
    ap.add_argument(
        "--local-files-only",
        action="store_true",
        help="只使用 Hugging Face cache 或 --model 指定的本機檔案",
    )
    ap.add_argument("--no-meta", action="store_true", help="只輸出內文，不抽 metadata")
    ap.add_argument(
        "--max-tokens", type=int, default=PAGE_MAX_TOKENS,
        help=f"每頁輸出上限（預設 {PAGE_MAX_TOKENS}）",
    )
    ap.add_argument("--dpi", type=int, default=RENDER_DPI, help=f"render DPI（預設 {RENDER_DPI}）")
    ap.add_argument("--max-width", type=int, default=MAX_WIDTH,
                    help=f"影像寬度上限（預設 {MAX_WIDTH}）")
    ap.add_argument("--max-height", type=int, default=MAX_HEIGHT,
                    help=f"影像高度上限（預設 {MAX_HEIGHT}）")
    args = ap.parse_args()

    paths = collect_pdfs(args.pdfs, args.indir)
    if not paths:
        print(f"找不到 PDF 檔案（{args.indir}/）", file=sys.stderr)
        return 1

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"載入 OCR 模型 {args.model}…", flush=True)
    try:
        runtime = NemotronRuntime(args.model, args.device, args.local_files_only)
    except Exception as e:
        print(f"Nemotron 模型載入失敗：{e}", file=sys.stderr)
        return 1
    print(f"Direct Transformers，裝置 {runtime.device}，OCR 模型 {args.model}"
          + ("，不抽 metadata" if args.no_meta else "，metadata 使用固定欄位抽取"))

    total_secs = 0.0
    failed = 0
    for pdf in paths:
        print(f"處理 {pdf.name}")
        started = time.time()
        try:
            parts = ocr_pdf(
                runtime,
                pdf,
                args.dpi,
                args.max_width,
                args.max_height,
                args.max_tokens,
            )
            meta = {} if args.no_meta or not parts else extract_meta(parts[0])
            elapsed = time.time() - started
            total_secs += elapsed
            print(f"  -> {write_doc(pdf, outdir, parts, meta)}"
                  f"（{elapsed:.1f} 秒，{elapsed / max(len(parts), 1):.1f} 秒/頁）")
        except Exception as e:
            failed += 1
            print(f"  失敗：{e}（{time.time() - started:.1f} 秒）", file=sys.stderr)

    print(f"\n完成 {len(paths) - failed}/{len(paths)} 份，共 {total_secs:.1f} 秒")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
