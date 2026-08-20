# 公文 PDF → Markdown 網頁服務

`../ocr_doc.py` 的 JavaScript 版，包成一個單頁網站：把 PDF 拖進瀏覽器，
看著進度跑完，下載 `.md`。

辨識邏輯與 Python 版的 OpenAI 路徑完全一致（同樣的 prompt、同樣的 18 欄
metadata schema、同樣的頁碼標記、同樣的 20 頁上限與頁數檢查），兩邊產出的
`.md` 可以互換。差別只在入口：CLI 掃資料夾批次跑，這裡一次一份、結果直接下載，
不寫進 `output/`。

`ocr_doc.py` 另外支援 `-p claude`，這裡沒有——這個服務只接 OpenAI。

## 安裝

需要 Node.js 20 以上（開發時用 24.15）。

```bash
cd web
npm install
```

三個依賴：`mupdf`（WASM 版 MuPDF，PDF render，免編譯原生模組）、
`openai`、`yaml`。PDF 轉圖在伺服器端做，瀏覽器只負責上傳。

## 啟動

金鑰讀取順序：環境變數 → `web/.env` → 專案根目錄 `.env`（已存在的環境變數優先，
與 `ocr_doc.py` 的 `load_dotenv` 一致）。

```bash
OPENAI_API_KEY=sk-... npm start     # 或把金鑰放 ../.env
```

打開 http://localhost:8787 。

環境變數：

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 必填 | 沒設也能啟動，但轉檔時會在頁面上報錯 |
| `OPENAI_MODEL` | `gpt-5` | 頁面上的模型欄位可以臨時覆寫 |
| `PORT` | `8787` | |

## API

也可以不透過頁面直接打：

```bash
curl -N -X POST "http://localhost:8787/api/convert?name=doc.pdf" \
     --data-binary @input/doc.pdf
```

- `POST /api/convert?name=<檔名>[&model=<模型>]`
  body 直接是 PDF 原始 bytes（不走 multipart），回應是 SSE：

  | 事件 | 內容 |
  | --- | --- |
  | `status` | `{msg}` 目前階段 |
  | `progress` | `{chars}` 模型已輸出的字元數 |
  | `done` | `{filename, markdown}` |
  | `error` | `{msg}` |

- `GET /api/config` → `{model, maxPages, maxUploadMB}`

上限：單份 20 頁（超過直接失敗，不靜默分批）、上傳 50MB。
模型回傳的頁數與輸入頁數不符會失敗，不會產出頁碼對不上的檔案。

## 為什麼是 SSE

單份公文用 high effort 推理跑好幾分鐘，一個沉默的 POST 看起來像當掉。
伺服器把 render 完成、模型輸出字數即時推給前端，並每 15 秒送一次心跳
避免中介 proxy 掐斷閒置連線；Node 的 `requestTimeout` 也關掉了
（預設 5 分鐘會在辨識中途砍掉連線）。
