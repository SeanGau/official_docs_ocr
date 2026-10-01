# 公文 PDF → Markdown 網頁版（GitHub Pages）

`../ocr_doc.py` 雲端版的 JavaScript 移植，整個是純靜態網頁（HTML + JavaScript + CSS），
沒有伺服器端程式。使用者在頁面填入自己的 OpenAI、Claude 或 Gemini API key，
把 PDF 拖進瀏覽器，看著進度跑完，下載 `.md`。

三家共用同一套 prompt、18 欄 metadata schema、頁碼標記、20 頁上限與頁數檢查，
產出的 `.md` 與 `ocr_doc.py` 格式相同：

| 供應商 | 預設模型 | 送件方式 |
| --- | --- | --- |
| OpenAI | `gpt-5` | 每頁 render 成 PNG（長邊 ≤ 2048px，175 DPI） |
| Claude | `claude-opus-5` | 每頁 render 成 PNG（長邊 ≤ 2576px，220 DPI） |
| Gemini | `gemini-3.8-flash` | 原生讀取整份 PDF，不 render |

填入 API key 後（按 Enter 或離開欄位），頁面會用這把 key 呼叫供應商的模型清單 API
（只送 key，不送 PDF），模型欄改成下拉選單，列出候選模型：

| 供應商 | 清單來源 | 篩選 |
| --- | --- | --- |
| OpenAI | `GET /v1/models` | 清單沒有能力欄位，只能看名稱：GPT-5 以後與 o 系列推理模型，排除語音、即時、影像生成、搜尋、codex、chat 等專用型號 |
| Claude | `GET /v1/models` | 依 API 回報的能力：影像輸入、結構化輸出、effort high、adaptive thinking 四項都明確支援才列入 |
| Gemini | `GET /v1beta/models` | `gemini-*`、支援 `generateContent`、未標示不支援 thinking，排除 tts／image／embedding／audio／live |

只有 Claude 是依能力精確篩選；OpenAI、Gemini 的清單無法證明與本工具的請求相容，
選到不相容的模型時，轉檔會由 API 回錯。查不到清單（金鑰錯、網路、CORS）時會顯示原因，
仍可選「其他（自行輸入）」填任意模型名稱。
清單只在記憶體裡，依供應商與金鑰快取。一次轉一份，結果直接下載，不寫進 `output/`。

## 金鑰與隱私

- 金鑰只留在分頁記憶體，不寫進任何儲存空間，重新整理後要重填。
  只有供應商與模型的選擇會記在 `localStorage`（不含機密）。
- PDF 與金鑰由瀏覽器直接送往 `api.openai.com`、`api.anthropic.com` 或
  `generativelanguage.googleapis.com`。沒有自建後端，但文件與金鑰仍會送到該供應商。
- 頁面用 `<meta>` CSP 限制：只執行本站腳本，連線只放行上面三個 API 網域與本站資源。
  GitHub Pages 無法設定 HTTP 標頭，`<meta>` CSP 的防護範圍比標頭版小（例如不支援
  `frame-ancestors`）。第三方程式庫全部放在 `vendor/`，不從 CDN 載入。
- 金鑰在瀏覽器裡，任何能在這個頁面執行腳本的東西（例如瀏覽器擴充功能）都讀得到。
  建議為這個工具另開一把 key，並在供應商後台設定用量上限。
- 被 API 拒絕的請求若回應沒帶 CORS 標頭（實測 OpenAI 的 401 就沒有），瀏覽器只能顯示
  「Failed to fetch」，與斷線、被擴充功能攔截無法區分；Claude 與 Gemini 的 401/400
  實測可讀到 API 回傳的錯誤訊息。

## 部署到 GitHub Pages

`.github/workflows/pages.yml` 會在 `main` 分支的 `web/` 有變動時，把 `web/` 原封不動部署
（沒有建置步驟）。第一次使用前到 repo 的 **Settings → Pages → Build and deployment → Source**
選「GitHub Actions」，之後 push 或在 Actions 頁手動執行 workflow 即可。

## 本機執行

ES module 與 pdf.js worker 不能從 `file://` 載入，需要任一靜態伺服器：

```bash
python3 -m http.server 8787 -d web
```

打開 http://localhost:8787 。

## 檔案

| 檔案 | 內容 |
| --- | --- |
| `index.html`、`style.css` | 頁面 |
| `app.js` | 介面：供應商／模型／金鑰設定、拖放、進度、下載 |
| `ocr.js` | 讀 PDF、render、呼叫三家 API（streaming）、組 Markdown |
| `vendor/pdfjs/` | [pdf.js](https://github.com/mozilla/pdf.js) `pdfjs-dist@6.3.289`（Apache-2.0）：`build/` 的 `pdf.min.mjs`、`pdf.worker.min.mjs`，以及 `cmaps/`、`standard_fonts/`、`iccs/`、`wasm/`（不含 `quickjs-eval`） |
| `vendor/yaml/` | [yaml](https://github.com/eemeli/yaml) `yaml@2.9.1`（ISC）的 `browser/` 目錄 |

更新 vendor：`npm pack pdfjs-dist@<版本> yaml@<版本>`，解開後照上表複製對應檔案與 LICENSE。

## 為什麼用 streaming

單份公文用 high effort 推理跑好幾分鐘。三家 API 都以 SSE 串流回應，頁面即時顯示
模型已輸出的字數，看得出還在跑；瀏覽器的 `fetch` 本身沒有逾時，長時間辨識不會被切斷。
