# 公文 PDF → Markdown 網頁服務

`../ocr_doc.py` 的 JavaScript 版，包成一個單頁網站：把 PDF 拖進瀏覽器，
看著進度跑完，下載 `.md`。

OpenAI 與 Gemini 共用同一套 prompt、18 欄 metadata schema、頁碼標記、20 頁上限
與頁數檢查，產出的 `.md` 格式相同。OpenAI 會把每頁 render 成 PNG；Gemini 使用
原生 PDF 文件理解，避免多頁 PNG 膨脹。CLI 掃資料夾批次跑，這裡則一次一份、
結果直接下載，不寫進 `output/`。

`ocr_doc.py` 支援相同的 OpenAI、Gemini，另外也支援 Claude。

## 安裝

需要 Node.js 20 以上（開發時用 24.15）。

```bash
cd web
npm install
```

四個依賴：`mupdf`（WASM 版 MuPDF，PDF 讀取與 OpenAI 路徑的 render）、
`openai`、`@google/genai`、`yaml`。瀏覽器只負責上傳，金鑰不會送到前端。

## 啟動

金鑰讀取順序：環境變數 → `web/.env` → 專案根目錄 `.env`（已存在的環境變數優先，
與 `ocr_doc.py` 的 `load_dotenv` 一致）。

```bash
GEMINI_API_KEY=... npm start         # 只設這一把 key 即可，不需要 OpenAI key
OPENAI_API_KEY=sk-... npm start      # 也可只使用 OpenAI
```

打開 http://localhost:8787 。

環境變數：

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `GEMINI_API_KEY` | 選 Gemini 時必填 | 只設定這把 key 時會自動選 Gemini |
| `OPENAI_API_KEY` | 選 OpenAI 時必填 | 不使用 OpenAI 就不需要設定 |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Gemini 的預設模型 |
| `OPENAI_MODEL` | `gpt-5` | OpenAI 的預設模型 |
| `OCR_PROVIDER` | 自動 | 可指定 `gemini` 或 `openai`；未指定時，只有 Gemini key 就選 Gemini，其他情況維持 OpenAI |
| `PORT` | `8787` | |

頁面可切換供應商與臨時覆寫模型。選到沒有對應 key 的供應商時，轉檔會顯示缺少哪個環境變數。

## API

也可以不透過頁面直接打：

```bash
curl -N -X POST "http://localhost:8787/api/convert?name=doc.pdf&provider=gemini" \
     --data-binary @input/doc.pdf
```

- `POST /api/convert?name=<檔名>[&provider=openai|gemini][&model=<模型>]`
  body 直接是 PDF 原始 bytes（不走 multipart），回應是 SSE。省略 `provider` 時使用
  `/api/config` 回傳的預設供應商：

  | 事件 | 內容 |
  | --- | --- |
  | `status` | `{msg}` 目前階段 |
  | `progress` | `{chars}` 模型已輸出的字元數 |
  | `done` | `{filename, markdown}` |
  | `error` | `{msg}` |

- `GET /api/config` → `{provider, model, providers, maxPages, maxUploadMB}`

上限：單份 20 頁（超過直接失敗，不靜默分批）、上傳 50MB。
模型回傳的頁數與輸入頁數不符會失敗，不會產出頁碼對不上的檔案。

## 為什麼是 SSE

單份公文用 high effort 推理跑好幾分鐘，一個沉默的 POST 看起來像當掉。
伺服器把文件讀取完成、模型輸出字數即時推給前端，並每 15 秒送一次心跳
避免中介 proxy 掐斷閒置連線；Node 的 `requestTimeout` 也關掉了
（預設 5 分鐘會在辨識中途砍掉連線）。
