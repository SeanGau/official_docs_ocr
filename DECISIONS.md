# 設計決策記錄

這份文件整理這個專案從第一行程式碼到現在的每個抉擇：當時面對什麼問題、為什麼選這條路、
付出什麼代價、後來被實測推翻了沒有。內容從 workspace 的 session log 逐則回顧整理，
標「實測」的都有實際跑過的結果，標「判斷」的是當下的推理。

時間軸：2026-08-17 起，共七個工作階段。

---

## 一、起點：怎麼把掃描公文變成文字

### 1. 用 VLM，不用傳統 OCR

**選擇**：Claude Opus 5 vision 直接看影像產 Markdown，不接 PaddleOCR 這類專用辨識器。

**理由**：需求不是「認字」而是「認字＋認版面」。傳統 OCR 給的是文字加座標框，
沒有語意——它不知道哪塊是主旨、哪塊是說明第 (三) 項、哪行是手寫批註、哪個是騎縫章。
要的階層式 Markdown 加 metadata，用傳統 OCR 得自己寫一堆規則拼，公文格式一變就爛掉。

**已知代價（判斷）**：兩者的**失敗模式不同**，這點對公文很致命。

| | 傳統 OCR 認錯 | VLM 認錯 |
| --- | --- | --- |
| 產出 | 亂碼、怪字 | 一個看起來很合理的字號／日期／句子 |
| 可發現性 | 一眼看得出來 | 沒有原件在旁邊完全看不出來 |

公文的字號、日期、金額錯一位就是實質錯誤，安靜的錯比亂碼危險。這個判斷後來在地端
被實測直接命中（見 §3.4）。

### 2. metadata 走 json_schema，不用 regex 解析

**選擇**：用 `output_config.format` 的 `json_schema` 強制模型回 `{metadata, markdown}`，
metadata 和正文**同一次呼叫**產出。

**理由**：省一次呼叫，且模型在看得到全文的狀態下抽欄位比事後 regex 準。欄位依台灣公文
格式設計 18 個（發文機關／受文者／發文日期民國原文＋西元 ISO 兩份／發文字號／速別／
密等／附件／主旨／正本／副本／承辦人／聯絡方式／檔號／保存年限／文別，另加關鍵字、摘要
供日後檢索）。缺欄位留空不編造。

**後來**：這條在雲端成立，在地端完全不成立——見 §4.1。

### 3. 220 DPI，長邊壓在 2576px

A4 在 220 DPI 下約 1819×2573，剛好貼齊 Opus 5 高解析度影像的上限，額度用滿又不會被
降採樣。代價是 input token 不便宜（每頁最高約 4784 tokens），要批次跑可以調 `RENDER_DPI`
或把 `effort` 從 `high` 降到 `medium`。

### 4. 整份文件一次送出

**選擇**：所有頁面在同一個 request 裡送，不逐頁送。

**理由**：公文常是「函文＋附件」，附件的表格要靠函文才知道在講什麼；跨頁的表格也需要
前後文才接得起來。

**實測佐證**：第三份公文的「正本」名單裡，台南市四個單位在原件真的重複列了兩次，模型
如實保留沒有自作主張去重——這是「不亂修正原件」的行為驗證。另外第三份的檔名字號
（`開字第1050102001號`）和公文本體（`開字第202601020001號`）根本不一致，模型抓的是
本體的正確值。

### 5. 印章、無法辨識字、紀年的三條約定

- 印章、簽名、手寫批註寫成 `<!-- 印章：OO部 -->` 註解，**不當正文**——它們不是文件內容，
  混進正文會污染後續檢索。
- 完全無法辨識的字用 `〇`，不臆測。呼應 §1.1 的失敗模式考量：寧可留洞也不要編字。
- 民國紀年保留原文（公文引用要能對得上），另外在 metadata 給 `發文日期_西元` 的 ISO 格式
  供程式排序。

### 6. 超過 20 頁直接失敗，不靜默分批

`MAX_PAGES_PER_REQUEST = 20`，超過報錯。理由是靜默截斷會產出「看起來完整但少了幾頁」
的檔案，比直接失敗糟得多。同樣的原則後來延伸到頁碼檢查（§5.2）。

### 7. 金鑰走 `.env`，不動使用者的 dotfile

**問題**：金鑰寫在 `~/.bashrc` 第 138 行，但第 6 行有「non-interactive 就 `return`」的保護，
非互動 shell 永遠讀不到；`export` 也不會跨 Bash 呼叫保留。

**選項**：(a) 把 export 搬到 guard 之前，動使用者 dotfile；(b) script 自己讀 `.env`。

**選擇 (b)**，寫了純標準函式庫的 `load_dotenv()`，不加新依賴，已存在的環境變數優先。
動別人的 dotfile 是副作用大又難回溯的事，`.env` 是本來就該有的路。

**教訓（實際踩到）**：後來用 `echo >> .env` 加變數時，因為原檔最後一行沒有換行符，
新內容直接黏到 `LM_STUDIO_API_KEY` 的值後面，把金鑰弄壞。修回來之後，專案規則加上
「不讀 `.env`」，一律走 `load_dotenv()`。

---

## 二、RAG 前的體檢（AnythingLLM）

還沒動手改程式，先盤點下游會遇到的問題，按影響程度排：

1. **embedding model 一定要換**。AnythingLLM 預設的 all-MiniLM-L6-v2 是英文模型，
   繁中公文的語意檢索會差很多，這件事不做後面所有調校都是白工。
   （Anthropic 沒有 embedding API，這塊一定得配別家。）
2. **YAML frontmatter 不會變成可過濾欄位**。AnythingLLM 把 `.md` 當純文字切塊，
   frontmatter 只是第一個 chunk 裡的文字。後果是「出處」和「內容」被切散：問
   「開字第202601020001號在講什麼」，字號在 chunk 1、說明在 chunk 2，可能只命中一個。
3. **正本名單會嚴重污染檢索（實測）**。第三份公文 **47% 的字數是那 90 個受文機關名稱**，
   而且 frontmatter 和本文各出現一次（886 + 701 字 / 全文 3402 字）。這些 chunk 語意上
   幾乎一樣，任何跟機關有關的查詢都會把它們撈出來當雜訊。建議超過一定數量就不進正文，
   frontmatter 改存 `正本_數量: 90` 加幾個代表性機關。
4. **chunk 設定**：預設 1000 / overlap 20 對中文偏小、重疊太少。公文的「說明 一、二、三」
   各自語意完整但常互相引用（「旨揭研會…」指回主旨），建議 1200–1500 / overlap 150–200。
5. **檔名就是 citation 標題**，而第三份的檔名字號是錯的，引用出來會誤導。

這五點裡，第 2 點直接催生了後來的頁碼標記設計（§5）；第 1、4 點屬於 AnythingLLM 的設定，
留給使用者；第 3、5 點記錄下來但尚未實作。

---

## 三、地端版：從「能不能」到「哪個模型能」

### 1. 另寫一支 script，不在雲端版加 flag

`ocr_doc.py` 和 `ocr_doc_local.py` 兩支獨立檔案，只共用 `load_dotenv()` / `collect_pdfs()` /
`join_pages()`。理由是兩者的送件策略、解析度、metadata 欄位數、失敗處理全都不同，
硬塞成一支會變成滿是 `if local:` 的分支。輸出格式則刻意維持一致，下游不必分辨是哪套產的。

### 2. 硬體是真正的約束條件

RTX 4060 Laptop **8GB VRAM** / 16GB RAM / WSL2。這個數字決定了後面每一個選擇：

- **逐頁送，不整份送**：8GB 級距的模型 context 塞不下多頁影像。代價是失去跨頁理解，
  這是硬體逼出來的妥協，不是設計偏好。
- **150 DPI / 長邊 1600**（雲端是 220 / 2576）：影像 token 直接吃 KV cache。
- **metadata 一開始只留 5 欄**，且只從第 1 頁抽。
- **不能同時跑三個模型評測**：LM Studio 會被迫來回換載入，兩邊的秒/頁都會失真，
  所以改成排隊。

另外 WSL 連 Windows 上的 LM Studio 要走 gateway IP（`172.17.224.1`）不是 `127.0.0.1`，
還得在 LM Studio 開 Serve on Local Network。

### 3. 先建量測，再選模型

**選擇**：寫 `bench_ocr.py`，拿雲端輸出當參照逐項比對，而不是憑感覺挑模型。

指標：相似度、關鍵欄位（發文字號／發文日期／主旨）逐字命中、簡體字洩漏率、重複迴圈佔比、
篇幅比、秒/頁、支不支援 json_schema。

**關鍵動作：指標自己要先被驗證**。拿三份參照檔自己比自己（應該 100%）跑一次，當場抓到
兩個 bug：

- **複讀率公式錯**：`sum(counts) × n` 會把重疊的 n-gram 重複計算，容易灌到 100%。
  改成算「被重複區段覆蓋的字元位置數」。
- **簡體洩漏誤判**：opencc 的 `s2t` 會把 `台→臺`、`栗→慄`、`群→羣` 這些**合法繁體異體字**
  也改掉，導致參照檔本身被誤判 1–2%。要排除。

校準後三份參照檔在簡體洩漏與複讀率都是乾淨的 0%，人工注入的錯誤都能按比例抓到。

**一個刻意的界線**：雲端輸出是**參照不是 ground truth**，它本身也可能有錯。相似度低不
必然代表地端模型錯，但差距很大時通常是。

### 4. 模型實測，四個結論

| 模型 | 結果 |
| --- | --- |
| `allenai/olmocr-2-7b` | **可用**。69s/頁，繁體正確，字號／日期／主旨全對。但**不吃 `json_schema`** |
| `google/gemma-4-12b-qat` | 幻覺 + reasoning 開銷，不可用 |
| `qwen3.5-9b-…` | **沒有 vision encoder**，餵影像產出全是空的（80~140 bytes） |
| abliterated（uncensored/aggressive）版本 | 對 OCR 是負面的 |

幾件值得記下來的事：

- **「卡住」的真相不是 VRAM 爆掉**。gemma-4-12b-qat 三次呼叫的 `content` 全是空字串、
  `completion_tokens` 每次都正好等於 max_tokens——它是 **reasoning 模型**，token 全被
  thinking 吃掉（原始回應證實有 `reasoning_content`、`reasoning_tokens: 197`）。配上
  11.6 tok/s，max_tokens=4096 等於每次呼叫要跑 370 秒才回一個空字串。這個發現後來
  直接變成 §4.4 的修正。
- **§1.1 的預言命中**。gemma 把 `財團法人開放文化基金會` 讀成 `財團法人台灣文化基金會`，
  而且 thinking 裡還寫「the text is clearly legible」。不是認不出來，是**自信地編了一個
  合理的機關名**。
- **評測改用正文 regex 抓關鍵欄位，不靠模型的 JSON**。因為 olmOCR 不支援 JSON，用 JSON
  評分會把它不公平地打成 0 分——那是在比錯的東西。關鍵欄位本來就直接印在公文正文裡，
  改成從 OCR 正文抓，兩個模型才站在同一個基準上。已用參照檔驗證欄位抽取 3/3 全中。
- **abliterated 微調的理由**：它們靠削弱模型的拒絕方向來解除限制，副作用是指令遵循變差、
  幻覺變多，正好命中公文 OCR 最危險的失效模式。使用者指定要測就照測，結果照實報，
  不擅自換掉。

---

## 四、地端 metadata：三次改法

### 1. 放棄「同一個模型同時做 OCR 和抽欄位」

olmOCR-2-7B 是 OCR 專用模型（Qwen2-VL fine-tune），只被訓練成「影像 → 逐字文字」，
沒有 instruction-following。原本把第 1 頁**影像**加 `json_schema` 丟給它要 metadata，
它會無視 schema 一路生到 4096 token 上限才失敗，白燒好幾分鐘。

**改法：兩段式，兩個模型**

1. **Stage 1** olmOCR 逐頁影像 → Markdown
2. **Stage 2** 把第 1 頁的**文字**（不再送影像）交給文字模型 + `json_schema` → metadata

**額外收穫**：因為 stage 2 是純文字，小模型不會像看影像那樣幻覺，欄位反而從 5 個補回 8 個
（多了受文者、速別、密等、附件陣列）。順帶把 `META_MAX_TOKENS` 從無限制改成有上限，
讓不吃 schema 的模型**快速失敗**而不是燒幾分鐘。

metadata 也改成在 OCR 之後才抽——抽失敗只會少 frontmatter，不會浪費已經辨識好的內文。

### 2. 全部 OCR 完，再統一抽 metadata

**問題（實測）**：原本每份文件都在 olmocr ↔ meta model 之間換載入，3 份 = 6 次 swap，
實測把 LM Studio 跑掛（`model has crashed` + `Model reloaded`，3 份只成功 1 份）。

**改法**：所有文件先全部 OCR 完，再一次抽所有 metadata，模型只換一次。重跑 3/3 通過。

這是 8GB VRAM 逼出來的流程設計，不是為了效能。

### 3. 位置固定的欄位改用 regex，不問模型

**文別**：交給 gemma-4-e4b 三份全錯（`''`、`說明`、`N/A`）。公文格式固定——首行是
「機關全銜　函」——改用 regex 抓首行行尾，三份全對。

**主旨**：後來的 meta model 比較（§4.4）發現兩個模型都會污染它：

- gemma 會黏上自評尾巴：`…請查照。請查照。 (註：原文結尾為『請查照』，已完整呈現…)`
- qwen 有時把整段說明吞進來——而且**同一份文件重跑結果不同**，temperature=0 也不保證穩定

主旨在原文位置固定（`主旨：` 起、`說明：` 或空行止），改用 regex 抓，模型輸出只當 fallback。
三份實測逐字正確。

**原則**：**位置固定的欄位就不要問模型**。模型只該做它無可取代的事（讀懂版面、
處理不定形的內容）。

### 4. meta model 比較：gemma vs qwen

設定上刻意讓 OCR **只跑一次**（olmocr），把同一份第 1 頁文字分別餵兩個 meta model，
比到的才是純粹的 metadata 抽取能力，不被 OCR 差異污染。ground truth 用 OCR 文字本身
（比的是「忠實抽取」，不是 OCR 對錯）。

| | gemma-4-e4b-it | qwen3.5-9b |
| --- | --- | --- |
| 512 tokens（當時的預設） | 全失敗 | 全失敗 |
| 2048 | 3/3 過 | 0/3 |
| 4096 | — | 1/3 |
| 8192 | — | 3/3 |
| 欄位正確（8 欄 × 3 份 = 24 格） | 21 全對 + 3 個主旨夾雜雜訊 | 24 全對 |
| thinking token | ~800 | 3800–6100 |
| 秒/份 | 34 | 260 |

**這輪還抓到一個評分陷阱**：qwen 在 4096 那份「成功」其實是從 thinking 撈到的草稿 JSON，
欄位名根本不符 schema（機關名稱/地址/電話），不能算數。

### 5. 由此產生的三個程式修正（commit `a6d4b6a`）

1. **`META_MAX_TOKENS` 512 → 8192**。兩個 meta model 都是 reasoning 型，512 全燒在
   thinking 上，metadata 三份全滅。給太少不會報「答錯」，而是 content 空掉、整份抽不到。
2. **`ask()` 把「被截斷」判斷提到 reasoning fallback 之前**。原本順序反了：thinking 被切
   一半時，fallback 會把殘缺的 JSON 當答案回傳，錯誤要到 `json.loads` 才爆成看不懂的
   `JSONDecodeError`。改成最先判截斷，並附上 thinking 用掉多少 token。
   實測：故意給 128 token → `輸出被 max_tokens 截斷（目前 128，其中 thinking 用掉 125）`。
3. **主旨改 regex**（§4.3）。

另有一個保留下來的 workaround：某些 build（qwen3.5 社群版）會把 schema 限制過的 JSON
整段吐在 `reasoning_content`、`content` 留空。那份 JSON 本身是對的，所以 `ask()` 加了
fallback 直接用它。

---

## 五、為 RAG 保留頁碼

需求：下一步要做 RAG，引用時得知道某段話出自第幾頁。

### 1. 用 HTML 註解標記，刻意不加分隔線

正文每頁前面插入 `<!-- page: N/總頁數 -->`。切 chunk 時往回找最近一個標記就知道頁碼。

三個刻意的選擇：

- **用 HTML 註解**：Markdown 算隱形內容，不會污染顯示出來的正文，
  而 `bench_ocr.py` 的 `normalize()` 本來就會清掉註解，所以標記不影響相似度計分。
- **刻意不加 `---` 分隔線**：公文的段落和表格常常跨頁，多一條線反而會讓 chunker
  在句子中間硬切。
- **兩版格式完全相同**：下游不必分辨是哪套產的。

空白頁不會產生標記，所以標記數可能少於 frontmatter 的 `頁數`——這是正確行為，
已實測（4 頁那份第 4 頁空白，只有 3 個標記）。

### 2. 雲端版 schema 從 `markdown` 改成 `pages` 陣列

雲端版是整份一次送，模型得自己知道頁面邊界。schema 改成 `pages` 陣列（每筆 `頁碼` +
`markdown`），prompt 要求照原件頁面邊界切、跨頁表格在次頁重寫表頭。

**回傳頁數與輸入頁數不符會直接報錯**——頁碼對不上等於引用錯頁，比沒有頁碼更糟。
同 §1.6 的原則。

地端版本來就逐頁 OCR，只是把原本的 `---` 串接換成 `join_pages()` 加標記。

### 3. input/ → output/ 資料夾化

預設從 `input/` 讀、寫到 `output/`，可用 `-i` / `-o` 覆寫。共用的 `collect_pdfs()` 放在
`ocr_doc.py`，地端版和 bench 直接 import，不重複一份。

---

## 六、目前的已知差異與未做的事

**兩版的差異（刻意保留）**

| | 雲端 | 地端 |
| --- | --- | --- |
| metadata 欄位 | 18 | 8 + 文別/主旨 regex |
| 缺的欄位 | — | 正本／副本／承辦人／聯絡方式／檔號／保存年限／關鍵字／摘要／西元日期 |

理由：正本副本那類要跨頁看，關鍵字摘要要生成能力，小模型做不準所以沒放進來。
要補的話正本副本可以照文別的做法用 regex 抓。

**已知但沒動的**

- olmOCR 表格輸出的是 HTML `<table>` 不是 Markdown 表格——它不理會 system prompt，
  要轉得在程式端後處理。
- `bench_ocr.py` 裡 `L.ask(..., L.META_PROMPT, ...)` 把帶 `{text}` 佔位符的 prompt 原樣送出
  （那段只是探測模型支不支援 json_schema）。
- §2 的第 3、5 點（正本名單外移、檔名依 metadata 正規化）尚未實作。

---

## 七、地端版 Windows 桌面程式（`ocr_app.py`）

1. **模型版本鎖在 `model_manifest.json`**：Nemotron 與它以 remote code 引用的 C-RADIOv2-H
   都固定 commit，並記下每個檔案大小。開啟時只比對本機檔案大小與 `refs/main`，完全不連網；
   也避免 `trust_remote_code` 在使用者端默默抓到新版程式碼。
2. **找模型的順序**：exe 內附 → 使用者的 Hugging Face cache；缺檔時也下載到這個 cache。
   與其他 HF 工具共用、不重複下載 3.4 GB，也讓「顯示的位置」就是「下載的位置」。
   代價是 cache 的 `refs/main` 會被設成固定 commit。預設建置內附模型（完全離線）；
   `-NoModel` 版首次開啟顯示缺多少、按鈕下載。
3. **下載放子程序**：huggingface_hub 在 import 時就讀 `HF_HUB_OFFLINE`。GUI 程序在載入模型前
   設 `HF_HUB_OFFLINE=1` 並固定 cache 位置，只有下載子程序會連網；取消就是結束子程序。
   完成的檔案會保留，未完成的大檔不保證續傳。manifest 只列執行必需檔與授權檔。
4. **Tk，不用網頁或 Qt**：Python 內建、PyInstaller 直接支援，不多一層伺服器或 3xx MB 的 Qt。
5. **exe 的授權要能公開散布**：PyMuPDF 是 AGPL-3.0（或 Artifex 商業授權），與隨 PyTorch 打包的
   NVIDIA CUDA/cuDNN 授權（不得使 SDK 受要求公開原始碼的開源授權約束）放在同一個 exe 有衝突。
   地端版改用 pypdfium2（Apache-2.0／BSD-3-Clause）render。82 頁的圖尺寸完全相同、平均像素差
   < 0.5/255，但**OCR 結果會變**：50 份樣本中 23 份與 PyMuPDF 版逐字相同、27 份有差（這 27 份
   平均相似度 99.13%，最低 93.14%）；PyMuPDF 重跑 27/27 逐字相同，差異確實來自 render。
   差異是單字辨識翻轉與個別表格／純文字排版不同，方向不一（例：「選場」→「還場」較合理，
   「檔號」→「橘號」則變錯）。沒有 ground truth；唯一可驗證的欄位「發文字號 == 檔名」
   PyMuPDF 48/50、pypdfium2 49/50。這是為了授權接受的行為變更。雲端版仍用 PyMuPDF
   （延後 import，只在 `cloud` extra）；`output/` 仍是 PyMuPDF 版的結果。
   `windows/third_party_licenses.py` 依 PyInstaller 實際打包的檔案產生授權清單並附原文，遇到
   GPL/AGPL 套件或不在 NVIDIA 可散布清單的 DLL 就讓建置失敗——這只是防呆，不取代逐項審核。
   `cusolverMg`、`nvperf_host` 不在清單內、沒有 DLL 靜態連結，排除後實際 CUDA OCR 正常，spec
   直接排除。授權原文固定存在 `windows/licenses/`，建置不連網下載。本專案自有程式碼採 MIT。

---

## 八、`web/` 改成 GitHub Pages 純靜態網頁

1. **拿掉 Node 伺服器，瀏覽器直接打 API**：使用者自己填 key，就不需要伺服器保管金鑰，
   也就能放上 GitHub Pages。三家 API 的 CORS preflight 都放行（Anthropic 另需
   `anthropic-dangerous-direct-browser-access: true`）。實測金鑰錯誤時，Anthropic、Gemini 的
   錯誤回應帶 CORS 標頭、瀏覽器讀得到原因；OpenAI 的 401 沒帶，見第 6 點。舊的 `server.mjs`、
   `/api/*` 與 `npm` 依賴整個移除，不留兩套。
2. **不用 SDK，直接 `fetch` + 自己解析 SSE**：請求內容與 `ocr_doc.py` 經 SDK 送出的相同
   （OpenAI Responses、Claude Messages、Gemini Interactions `v1beta/interactions`），
   省掉打包步驟與幾 MB 的 SDK。補上 Claude，三家與 Python 版對齊。Gemini 的 endpoint 是
   `v1beta`，之後若改版，靜態版會直接壞，先查這裡。
3. **pdf.js 取代 MuPDF.js**：MuPDF 是 AGPL，公開部署要另外提供原始碼；pdf.js 是 Apache-2.0。
   換 render 器可能改變 OCR 結果（§七第 5 點換 pypdfium2 時就有差異），pdf.js 與 MuPDF 之間
   **沒有做過比對**。尺寸規則不變（OpenAI 175 DPI／長邊 2048，Claude 220 DPI／長邊 2576，
   Gemini 原生 PDF）。
4. **第三方程式庫放 `web/vendor/`，不走 CDN**：頁面握有使用者的 key，CDN 被換掉就能偷 key。
   搭配 `<meta>` CSP：只允許本站腳本，`connect-src` 只放行三個 API 網域。
5. **金鑰只在分頁記憶體**，不提供「記住金鑰」：需求只要求填 key 就能用，存進 `localStorage`
   會擴大外洩面。供應商與模型偏好不含機密，會記住。
6. **已知限制**：實測 OpenAI 對錯誤金鑰回的 401 不帶 CORS 標頭，瀏覽器只看得到 `Failed to fetch`，
   與斷線無法區分，錯誤訊息只能通用地提示檢查網路與金鑰。
7. **不在前端預設檔案大小上限**：各家 request 大小上限不同且會調整，寫死數字可能擋掉合法檔案；
   超過時由 API 拒絕請求（若錯誤回應沒帶 CORS 標頭，頁面只會顯示連線失敗）。

---

## 貫穿全程的幾條原則

1. **失敗要吵，不要安靜**。頁數不符報錯、超過 20 頁報錯、截斷報錯並說出 thinking 用掉多少。
   安靜的錯誤（幻覺的字號、少掉的頁、殘缺的 JSON）比明顯的失敗危險得多。
2. **位置固定的欄位用 regex，不問模型**。文別、主旨都是這樣改的。
3. **先驗證量測工具，再拿它評模型**。指標拿參照檔自比跑一次就抓到兩個公式 bug。
4. **比較要控制變因**。OCR 只跑一次餵兩個 meta model；不同時載入三個模型跑評測。
5. **參照不是 ground truth**。雲端輸出只是對照組，它自己也會錯。
6. **有數字就不用猜**。「小 VLM 比傳統 OCR 好嗎」這種問題，先建 bench 再回答。
