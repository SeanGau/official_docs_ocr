# 公文 PDF → Markdown

把掃描的台灣公文 PDF 轉成帶 YAML frontmatter 的 Markdown，正文逐頁標記頁碼，
供後續 RAG 建索引使用。

兩套實作，輸出格式一致：

| | `ocr_doc.py`（雲端） | `ocr_doc_local.py`（地端） |
| --- | --- | --- |
| 模型 | OpenAI GPT-5 或 Claude Opus 5，用 `-p` 選 | LM Studio 上的視覺模型 |
| 文件外傳 | 會 | 不會 |
| 送件方式 | 整份文件一次送出，可跨頁理解 | 逐頁辨識再串接（小模型 context 有限）|
| metadata | 18 個欄位，與正文同一次結構化輸出 | 8 個欄位，先 OCR 第 1 頁再用文字模型抽（文別另用 regex 從首行抓）|
| 輸出檔名 | `<檔名>.md` | `<檔名>.local.md` |

另有 [`web/`](web/)：雲端版的 JavaScript 移植，包成網頁服務（拖 PDF 進瀏覽器、
看進度、下載 `.md`）。辨識邏輯與 `ocr_doc.py` 的 OpenAI 路徑一致，輸出格式相同；
目前只接 OpenAI，沒有 `-p claude` 的對應選項。

## 安裝

Python 3.10+，套件：

```bash
pip install anthropic openai pymupdf pyyaml opencc-python-reimplemented
```

（`opencc` 只有 `bench_ocr.py` 用到。）

金鑰放專案根目錄的 `.env`，一行一個 `KEY=VALUE`。非互動 shell 讀不到 `~/.bashrc`，
所以走 `.env` 比較可靠；已存在的環境變數優先。

```
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
LM_STUDIO_BASE_URL=http://172.17.224.1:1234/v1
LM_STUDIO_META_MODEL=...
```

雲端版只會用到你要跑的那家的金鑰（SDK 是延後 import 的，另一家沒裝也不影響）。

## 資料夾

```
input/    要處理的 PDF
output/   產出的 Markdown
bench_out/  各模型的原始輸出（評測用）
```

`input/`、`output/` 是預設值，可用 `-i` / `-o` 覆寫。

## 用法

### 雲端版

```bash
python3 ocr_doc.py                       # input/ 全部 pdf -> output/
python3 ocr_doc.py input/a.pdf           # 只處理指定檔案
python3 ocr_doc.py -i in/ -o out/
python3 ocr_doc.py -p claude             # 改用 Claude（預設 openai）
python3 ocr_doc.py -m gpt-5-mini         # 換模型
```

OpenAI 與 Claude 兩家並存，`-p` / `--provider` 選一家。prompt、metadata schema、
輸出格式完全相同，差別只有 SDK、預設模型與影像解析度上限：

| | `-p openai`（預設） | `-p claude` |
| --- | --- | --- |
| 金鑰 | `OPENAI_API_KEY` | `ANTHROPIC_API_KEY` |
| 預設模型 | `gpt-5` | `claude-opus-5` |
| 覆寫模型的環境變數 | `OPENAI_MODEL` | `ANTHROPIC_MODEL` |
| render | 175 DPI，長邊 ≤ 2048 | 220 DPI，長邊 ≤ 2576 |

解析度差異是各家視覺輸入上限不同，兩邊都是「把額度用滿又不會被降採樣」的值。
模型名稱優先序：`-m` > 環境變數 > 內建預設值。

兩家的輸出檔名都是 `<檔名>.md`，要並排比較就用 `-o` 分開放。

單份上限 20 頁（`MAX_PAGES_PER_REQUEST`）。模型回傳的頁數與輸入頁數不符會直接失敗，
不會產出頁碼對不上的檔案。

### 地端版

先在 LM Studio 載入一個 vision 模型並啟動 server：

```bash
python3 ocr_doc_local.py
python3 ocr_doc_local.py --base-url http://172.17.224.1:1234/v1
python3 ocr_doc_local.py --model qwen2.5-vl-7b-instruct --dpi 180
python3 ocr_doc_local.py --model "allenai/olmocr-2-7b" --meta-model "gemma-4-e4b-it"
python3 ocr_doc_local.py --no-meta                 # 只要內文
```

WSL 連 Windows 上的 LM Studio 要走 gateway IP，不是 `127.0.0.1`。

olmOCR 這類 OCR 專用模型不會 instruction following，直接叫它吐 JSON 一定失敗，
所以 metadata 分兩段抽：先 OCR 出文字，再把文字交給 `--meta-model` 的文字模型。
所有文件會先全部 OCR 完才統一抽 metadata——兩個模型交錯呼叫會讓 LM Studio
一直換載入，實測會跑到 crash。

## 輸出格式

```markdown
---
發文機關: 財團法人開放文化基金會
受文者: 花蓮縣教育處
發文日期: 中華民國112年11月29日
發文日期_西元: '2023-11-29'
發文字號: 開字第11211290001號
主旨: 檢送辦理「g0v Summit 2024台灣零時政府雙年會」海報乙份…
關鍵字: [...]
摘要: ...
來源檔案: 開字第 11211290001 號_....pdf
頁數: 1
---

<!-- page: 1/1 -->

# 主旨…
```

### 頁碼標記

正文每一頁前面都有 `<!-- page: N/總頁數 -->`。切 chunk 時往回找最近一個標記就知道
這段話出自第幾頁，可以回填成引用來源。

- 用 HTML 註解：Markdown 算隱形內容，不會污染顯示出來的正文。
- 兩版格式相同，下游不必分辨是哪套產的。
- **刻意不加 `---` 分隔線**：公文的段落和表格常常跨頁，多一條線反而會讓 chunker
  在句子中間硬切。
- 空白頁不會產生標記，所以標記數可能少於 frontmatter 的 `頁數`。

### 其他約定

- 印章、簽名、手寫批註寫成 `<!-- 印章：OO部 -->` 這類註解，不當正文。
- 完全無法辨識的字用 `〇`，不臆測。
- 民國紀年保留原文；雲端版另外在 metadata 給 `發文日期_西元` 的 ISO 格式。

## 評測地端模型

拿多個地端模型跑同一批 PDF，跟 `output/` 裡雲端版的產出逐項比對：

```bash
python3 bench_ocr.py --base-url http://172.17.224.1:1234/v1 \
    --models model-a model-b
```

指標：相似度、關鍵欄位（發文字號／發文日期／主旨）逐字命中、簡體字洩漏率、
重複迴圈佔比、篇幅比、秒/頁、支不支援 json_schema。各模型的完整輸出留在 `bench_out/`。

雲端版的產出只是**參照**不是 ground truth，它本身也可能有錯；相似度低不必然代表
地端模型錯，但差距很大時通常是。
