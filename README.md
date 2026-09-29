# 公文 PDF → Markdown

把掃描的台灣公文 PDF 轉成帶 YAML frontmatter 的 Markdown，正文逐頁標記頁碼，
供後續 RAG 建索引使用。

兩套實作，輸出格式一致：

| | `ocr_doc.py`（雲端） | `ocr_doc_local.py`（地端） |
| --- | --- | --- |
| 模型 | OpenAI GPT-5 或 Claude Opus 5，用 `-p` 選 | NVIDIA Nemotron-Parse 2.0（固定） |
| 文件外傳 | 會 | 不會 |
| 送件方式 | 整份文件一次送出，可跨頁理解 | 逐頁辨識再串接 |
| metadata | 18 個欄位，與正文同一次結構化輸出 | 從第 1 頁 OCR 文字的固定標籤抽取 |
| 輸出檔名 | `<檔名>.md` | `<檔名>.local.md` |

另有 [`web/`](web/)：雲端版的 JavaScript 移植，包成網頁服務（拖 PDF 進瀏覽器、
看進度、下載 `.md`）。辨識邏輯與 `ocr_doc.py` 的 OpenAI 路徑一致，輸出格式相同；
目前只接 OpenAI，沒有 `-p claude` 的對應選項。

地端版另有桌面程式 [`ocr_app.py`](#地端版-windows-桌面程式)：視窗操作，可打包成 Windows exe，
模型內附或首次開啟時下載一次，之後完全離線。

## 安裝

Python 3.10+，使用 [uv](https://docs.astral.sh/uv/) 管理相依套件。依要執行的版本安裝：

```bash
uv sync --extra cloud                  # ocr_doc.py
uv sync --extra local                  # ocr_doc_local.py
uv sync --extra local --extra bench    # bench_ocr.py
```

下方的 `uv run --extra ...` 也會自動同步需要的環境，不必先手動執行 `uv sync`。

金鑰放專案根目錄的 `.env`，一行一個 `KEY=VALUE`。非互動 shell 讀不到 `~/.bashrc`，
所以走 `.env` 比較可靠；已存在的環境變數優先。

```
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
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
uv run --extra cloud ocr_doc.py                       # input/ 全部 pdf -> output/
uv run --extra cloud ocr_doc.py input/a.pdf           # 只處理指定檔案
uv run --extra cloud ocr_doc.py -i in/ -o out/
uv run --extra cloud ocr_doc.py -p claude             # 改用 Claude（預設 openai）
uv run --extra cloud ocr_doc.py -m gpt-5-mini         # 換模型
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

地端版固定使用 [`nvidia/NVIDIA-Nemotron-Parse-2.0`](https://huggingface.co/nvidia/NVIDIA-Nemotron-Parse-2.0)。
其官方架構是 C-RADIO ViT-H vision encoder 加 mBART decoder，不含 Qwen base；
程式也不提供切換 OCR 模型或第二個 metadata 模型的路徑。

地端版直接在目前的 Python process 載入 Hugging Face Transformers，不需要 vLLM、
OpenAI 相容服務、API key 或另一個 terminal：

```bash
uv run --extra local ocr_doc_local.py
uv run --extra local ocr_doc_local.py input/a.pdf
uv run --extra local ocr_doc_local.py --model /path/to/NVIDIA-Nemotron-Parse-2.0
uv run --extra local ocr_doc_local.py --local-files-only
uv run --extra local ocr_doc_local.py --no-meta
```

第一次執行會從 Hugging Face 下載模型，之後使用本機 cache。預設 `--device auto` 會優先
使用 `cuda:0`；沒有 CUDA 時會退回 CPU（可明確指定 `--device cpu`，但會慢很多）。
`--model` 可接受 Hugging Face model ID 或本機模型目錄；搭配 `--local-files-only` 可保證
執行時不連網。

模型輸入使用官方建議的 `1664×2048` 上限與控制 token；Transformers 產生的 bbox/class
包裝會在寫檔前移除，只保留 reading order 中的 Markdown。metadata 不再呼叫語言模型，
而是從首頁的「受文者」、「發文日期」、「發文字號」等固定標籤確定性抽取。

### 地端版 Windows 桌面程式

`ocr_app.py` 是地端版的視窗介面（Tk），與 `ocr_doc_local.py` 共用同一套 OCR 與輸出格式。
打包後雙擊 `OfficialDocOCR.exe`：選 PDF 或資料夾、選輸出資料夾、按「開始轉換」，
逐頁顯示進度，輸出 `<檔名>.local.md`；可在任一頁完成後停止。

```bash
uv run --extra local ocr_app.py                 # 從原始碼執行
uv run --extra local ocr_app.py input/a.pdf     # 開啟時先加入檔案或資料夾
```

**開啟時的離線檢查**：模型版本固定在 `model_manifest.json`（repo、commit、每個檔案大小）。
程式開啟時只比對本機檔案，不連網，依序找：exe 內附的 `_internal\hf_hub` →
`%LOCALAPPDATA%\OfficialDocParser\hf_hub` → 使用者既有的 Hugging Face cache。

- 找到完整的一份：顯示「離線資源完整，不需要網路」，之後全程離線（`HF_HUB_OFFLINE=1`）。
- 都不完整：顯示缺幾個檔、需要下載多少，按「下載模型」後在子程序下載到
  `%LOCALAPPDATA%\OfficialDocParser\hf_hub`，顯示進度、速度與估計剩餘時間，可取消；
  完成的檔案會保留，下次只下載缺少的部分。

程式紀錄（含錯誤的完整 traceback）寫在 `%LOCALAPPDATA%\OfficialDocParser\app.log`。

#### 打包成 exe

在 Windows 上（需要 [uv](https://docs.astral.sh/uv/)）用 PyInstaller 打包，執行時不需要 Python：

```powershell
powershell -ExecutionPolicy Bypass -File windows\build.ps1            # 內附模型，完全離線
powershell -ExecutionPolicy Bypass -File windows\build.ps1 -NoModel   # 不附模型，首次開啟時下載
```

- venv、暫存與成品都放在 `%USERPROFILE%\official_doc_parser_build`（可用 `-BuildRoot` 改），
  不動 repo 內給 Linux/WSL 用的 `.venv`；repo 放在 `\\wsl.localhost\...` 也能直接建置。
- 成品是 `dist\OfficialDocOCR\` 整個資料夾（內附模型約 6.5 GB，`-NoModel` 約 3.1 GB），
  要整包複製或壓縮；單拿 `OfficialDocOCR.exe` 無法執行。
- 內附版的模型在建置時由 `python model_store.py stage` 依 manifest 下載，整理成不含 symlink
  的 Hugging Face cache 放進 `_internal\hf_hub`，搬到別台電腦也能用。
- 成品根目錄有 `NOTICE.txt` 與 `NVIDIA-Open-Model-License.pdf`（C-RADIOv2-H 的 NVIDIA Open
  Model License §3.1 要求），Nemotron 的 `LICENSE` 在模型 snapshot 裡。複製時一起帶走。
- Windows 的 torch 由 `pyproject.toml` 指定從 PyTorch 的 CUDA 13.0 index 安裝：
  需要 Turing（RTX 20 系列）以後的 NVIDIA GPU 與 R580 以上的驅動；沒有 GPU 就退回 CPU，
  但一頁要十幾分鐘、記憶體約 6.5 GB。

要換模型版本：改 `model_store.py` 的 `PINNED`，執行 `uv run --extra local python model_store.py manifest`
重新產生 `model_manifest.json`，再重新建置。

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

用同一批 PDF 跑 Nemotron-Parse 2.0，跟 `output/` 裡雲端版的產出逐項比對：

```bash
uv run --extra local --extra bench bench_ocr.py
```

指標：相似度、關鍵欄位（發文字號／發文日期／主旨）逐字命中、簡體字洩漏率、
重複迴圈佔比、篇幅比與秒/頁。完整輸出留在 `bench_out/`。

雲端版的產出只是**參照**不是 ground truth，它本身也可能有錯；相似度低不必然代表
地端模型錯，但差距很大時通常是。
