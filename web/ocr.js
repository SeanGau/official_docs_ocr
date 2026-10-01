/**
 * 公文 PDF -> Markdown（含 YAML frontmatter metadata），全部在瀏覽器裡執行。
 *
 * 這是 ../ocr_doc.py 雲端版的 JavaScript 移植：用 pdf.js 讀取掃描 PDF，
 * 由瀏覽器拿使用者自己的 API key 直接呼叫 OpenAI、Claude 或 Gemini，
 * 做 OCR + 版面重建 + metadata 抽取，組成 .md 文字。沒有任何中介伺服器。
 *
 * prompt、schema、頁碼標記、各種上限都與 Python 版一致，產出的 .md 可以互換。
 */

import * as pdfjs from "./vendor/pdfjs/pdf.min.mjs";
import YAML from "./vendor/yaml/index.js";

const PDFJS = new URL("./vendor/pdfjs/", import.meta.url);
pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdf.worker.min.mjs", PDFJS).href;

// RAG 切 chunk 時用來還原「這段話出自第幾頁」。用 HTML 註解，
// 算是 Markdown 的隱形內容，不會污染顯示出來的正文。
const pageMarker = (n, total) => `<!-- page: ${n}/${total} -->`;

// 每份文件的所有頁面一次送出，讓模型能跨頁理解（例如函文 + 附件表格）。
export const MAX_PAGES_PER_REQUEST = 20;

const MAX_OUTPUT_TOKENS = 32000;

const SYSTEM_PROMPT = `你是台灣公文數位化專家，負責把掃描的公文影像轉成結構化資料。

工作要求：
1. OCR 要逐字精確，使用繁體中文。專有名詞（機關名稱、人名、地名）務必正確。
2. 保留公文原有的層次結構：主旨、說明（一、二、三…）、辦法、附件等。
   說明底下的子項（(一)(二)、1.2.3.）要保留階層，用 Markdown 巢狀清單表示。
3. 表格（如申請表、經費表）用 Markdown 表格重建，欄位對齊原件。
4. 印章、簽名、浮水印等非文字內容，用 \`<!-- 印章：OO部OO司 -->\` 這類 HTML 註解標註，
   不要當成正文。手寫批註也用註解標註並註明「手寫」。
5. 完全無法辨識的字用 \`〇\` 代替，不要臆測。
6. 民國紀年一律保留原文，另在 metadata 中同時提供西元 ISO 格式。

metadata 欄位若原件沒有，填空字串或空陣列，不要編造。
`;

const USER_PROMPT = `以上是同一份公文的全部頁面（依序）。請完成：

1. 抽出核心 metadata。
2. 把全文轉成 Markdown（不要包含 YAML frontmatter，那由程式產生），
   並且**逐頁分開輸出**：pages 陣列一頁一筆，頁碼對應上面標示的頁次，
   缺頁或空白頁也要有一筆（markdown 填空字串）。
   第 1 頁以 \`# {主旨}\` 開頭，接著依序呈現受文者、主旨、說明、正副本等區塊。
   若有附件，附件所在的那一頁以 \`## 附件一：xxx\` 起頭。
   內容跨頁時（例如表格或段落被切斷），照原件切在頁面邊界，
   不要把後頁的內容併進前頁，也不要為了通順而重排順序；
   跨頁的表格在下一頁重新寫一次表頭即可。
`;

// metadata 欄位順序＝輸出 frontmatter 的順序，也是 schema 的 required 清單。
const META_FIELDS = [
  "發文機關",
  "受文者",
  "發文日期",
  "發文日期_西元",
  "發文字號",
  "速別",
  "密等及解密條件",
  "附件",
  "主旨",
  "正本",
  "副本",
  "承辦人",
  "聯絡方式",
  "檔號",
  "保存年限",
  "文別",
  "關鍵字",
  "摘要",
];

// 結構化輸出 schema。所有欄位皆為 required 且不允許額外屬性
// （structured outputs 的硬性要求），缺值用空字串／空陣列表示。
const SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["metadata", "pages"],
  properties: {
    metadata: {
      type: "object",
      additionalProperties: false,
      required: META_FIELDS,
      properties: {
        發文機關: { type: "string", description: "發文的機關全銜" },
        受文者: { type: "string" },
        發文日期: { type: "string", description: "原件民國紀年，如「中華民國110年8月31日」" },
        發文日期_西元: { type: "string", description: "ISO 格式 YYYY-MM-DD" },
        發文字號: { type: "string", description: "如「開字第1100831001號」" },
        速別: { type: "string", description: "普通件／速件／最速件" },
        密等及解密條件: { type: "string" },
        附件: { type: "array", items: { type: "string" }, description: "附件名稱清單" },
        主旨: { type: "string", description: "主旨欄全文（不含「主旨：」三字）" },
        正本: { type: "array", items: { type: "string" } },
        副本: { type: "array", items: { type: "string" } },
        承辦人: { type: "string" },
        聯絡方式: { type: "string", description: "電話、傳真、email 等" },
        檔號: { type: "string" },
        保存年限: { type: "string" },
        文別: { type: "string", description: "如「函」「書函」「令」「公告」" },
        關鍵字: {
          type: "array",
          items: { type: "string" },
          description: "3-8 個便於檢索的主題關鍵字",
        },
        摘要: { type: "string", description: "一到兩句話的內容摘要" },
      },
    },
    pages: {
      type: "array",
      description: "逐頁的 Markdown，順序與輸入頁面相同",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["頁碼", "markdown"],
        properties: {
          頁碼: { type: "integer", description: "頁次，從 1 起算" },
          markdown: { type: "string", description: "該頁的 Markdown，不含 YAML frontmatter" },
        },
      },
    },
  },
};

/** Blob -> base64（不含 data: 前綴）。FileReader 不必把整份檔案攤成 JS 字串再編碼。 */
function toBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.slice(reader.result.indexOf(",") + 1));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

/** 回傳 pdf.js 的 loading task；用完要 destroy() 它才會釋放 worker。 */
function openPdf(bytes) {
  return pdfjs.getDocument({
    // getDocument 會把 buffer 轉移給 worker，原陣列會被清空，所以給一份副本。
    data: bytes.slice(),
    cMapUrl: new URL("cmaps/", PDFJS).href,
    iccUrl: new URL("iccs/", PDFJS).href,
    standardFontDataUrl: new URL("standard_fonts/", PDFJS).href,
    wasmUrl: new URL("wasm/", PDFJS).href,
  });
}

/**
 * 把 PDF 每頁 render 成 PNG Blob，長邊不超過 maxEdge。
 * onPage(n) 在每頁完成後呼叫，用來推進度。
 */
async function renderPages(doc, { dpi, maxEdge }, onPage) {
  const images = [];
  const canvas = document.createElement("canvas");
  for (let n = 1; n <= doc.numPages; n++) {
    const page = await doc.getPage(n);
    try {
      let scale = dpi / 72;
      // 先量原尺寸，長邊超標就等比例縮，避免 render 兩次大圖。
      const natural = page.getViewport({ scale });
      const longEdge = Math.max(natural.width, natural.height);
      if (longEdge > maxEdge) scale *= maxEdge / longEdge;
      const viewport = page.getViewport({ scale });
      canvas.width = Math.round(viewport.width);
      canvas.height = Math.round(viewport.height);
      await page.render({ canvas, viewport }).promise;
      images.push(
        await new Promise((resolve, reject) =>
          canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error(`第 ${n} 頁轉 PNG 失敗`))), "image/png"),
        ),
      );
    } finally {
      page.cleanup();
    }
    onPage(n);
  }
  return images;
}

/**
 * 逐一產生 SSE 的 data（已 JSON.parse），遇到 [DONE] 結束。
 * 三家的事件型別都寫在 data 裡，用不到 `event:` 行。
 */
async function* readSse(res) {
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  let data = [];
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buf += value;
    const lines = buf.split("\n");
    buf = lines.pop();
    for (const raw of lines) {
      const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
      if (line === "") {
        if (data.length) {
          const text = data.join("\n");
          if (text === "[DONE]") return;
          yield JSON.parse(text);
        }
        data = [];
      } else if (line.startsWith("data:")) {
        data.push(line.slice(line.startsWith("data: ") ? 6 : 5));
      }
    }
  }
}

/**
 * POST 一個 streaming 請求；HTTP 錯誤時把 API 回的錯誤訊息帶出來。
 */
async function postStream(label, url, headers, body) {
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "text/event-stream", ...headers },
      body: JSON.stringify(body),
    });
  } catch (e) {
    // 錯誤回應若沒帶 CORS 標頭，瀏覽器只給 "Failed to fetch"，分不出是斷線、被擋或請求被拒。
    throw new Error(`連不到 ${label} API：${e.message}。請確認網路連線與 API key`);
  }
  if (!res.ok) {
    const text = await res.text();
    let msg = text;
    try {
      const j = JSON.parse(text);
      const err = (Array.isArray(j) ? j[0] : j)?.error;
      msg = err?.message ?? text;
    } catch {
      // 不是 JSON 就直接顯示原文。
    }
    throw new Error(`${label} API 回應 ${res.status}：${msg || res.statusText}`);
  }
  return res;
}

async function buildContentOpenAI(images) {
  const content = [];
  for (const [idx, png] of images.entries()) {
    content.push({ type: "input_text", text: `--- 第 ${idx + 1} 頁 ---` });
    content.push({
      type: "input_image",
      // 公文字小，低解析度會直接認不出來，一律用 high。
      detail: "high",
      image_url: `data:image/png;base64,${await toBase64(png)}`,
    });
  }
  content.push({ type: "input_text", text: USER_PROMPT });
  return content;
}

/**
 * 呼叫 OpenAI Responses API 做 OCR + 抽 metadata，回傳 {metadata, pages}。
 * onProgress(chars) 會在模型輸出過程中被呼叫，用來更新進度。
 */
async function extractOpenAI(apiKey, model, images, onProgress) {
  // 影像多、輸出長，一律 streaming，邊收邊回報進度。
  const res = await postStream(
    "OpenAI",
    "https://api.openai.com/v1/responses",
    { authorization: `Bearer ${apiKey}` },
    {
      model,
      max_output_tokens: MAX_OUTPUT_TOKENS,
      instructions: SYSTEM_PROMPT,
      reasoning: { effort: "high" },
      text: {
        format: {
          type: "json_schema",
          name: "official_doc",
          schema: SCHEMA,
          strict: true,
        },
      },
      input: [{ role: "user", content: await buildContentOpenAI(images) }],
      stream: true,
    },
  );

  let chars = 0;
  let response = null;
  for await (const data of readSse(res)) {
    if (data.type === "response.output_text.delta") {
      chars += data.delta.length;
      onProgress(chars);
    } else if (["response.completed", "response.incomplete", "response.failed"].includes(data.type)) {
      response = data.response;
    } else if (data.type === "error") {
      throw new Error(`OpenAI 回應失敗：${data.message ?? data.code ?? "未知錯誤"}`);
    }
  }
  if (!response) {
    throw new Error("OpenAI 連線中斷，沒有收到完整回應");
  }
  if (response.status === "failed") {
    throw new Error(`OpenAI 回應失敗：${response.error?.message ?? "未知錯誤"}`);
  }

  let text = "";
  for (const item of response.output ?? []) {
    for (const part of item.content ?? []) {
      if (part.type === "refusal") {
        throw new Error(`模型拒絕處理：${part.refusal}`);
      }
      if (part.type === "output_text") text += part.text;
    }
  }
  if (response.status === "incomplete") {
    const reason = response.incomplete_details?.reason ?? response.status;
    if (reason === "max_output_tokens") {
      throw new Error("輸出被 max_output_tokens 截斷，請調高上限或分批處理頁面");
    }
    throw new Error(`回應不完整：${reason}`);
  }
  return parseResult("OpenAI", text);
}

async function buildContentClaude(images) {
  const content = [];
  for (const [idx, png] of images.entries()) {
    content.push({ type: "text", text: `--- 第 ${idx + 1} 頁 ---` });
    content.push({
      type: "image",
      source: { type: "base64", media_type: "image/png", data: await toBase64(png) },
    });
  }
  content.push({ type: "text", text: USER_PROMPT });
  return content;
}

/** 呼叫 Claude Messages API 做 OCR + 抽 metadata，回傳 {metadata, pages}。 */
async function extractClaude(apiKey, model, images, onProgress) {
  const res = await postStream(
    "Claude",
    "https://api.anthropic.com/v1/messages",
    {
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
      // 金鑰是使用者自己填、只送往 Anthropic，正是這個 header 允許的情境。
      "anthropic-dangerous-direct-browser-access": "true",
    },
    {
      model,
      max_tokens: MAX_OUTPUT_TOKENS,
      system: SYSTEM_PROMPT,
      thinking: { type: "adaptive" },
      output_config: {
        effort: "high",
        format: { type: "json_schema", schema: SCHEMA },
      },
      messages: [{ role: "user", content: await buildContentClaude(images) }],
      stream: true,
    },
  );

  let text = "";
  let stopReason = null;
  let stopDetails = null;
  for await (const data of readSse(res)) {
    if (data.type === "content_block_delta" && data.delta?.type === "text_delta") {
      text += data.delta.text;
      onProgress(text.length);
    } else if (data.type === "message_delta") {
      stopReason = data.delta?.stop_reason ?? stopReason;
      stopDetails = data.delta?.stop_details ?? stopDetails;
    } else if (data.type === "error") {
      throw new Error(`Claude 回應失敗：${data.error?.message ?? data.error?.type ?? "未知錯誤"}`);
    }
  }
  if (stopReason === "refusal") {
    const detail = stopDetails?.explanation ?? stopDetails?.category ?? "未提供原因";
    throw new Error(`模型拒絕處理：${detail}`);
  }
  if (stopReason === "max_tokens") {
    throw new Error("輸出被 max_tokens 截斷，請調高 max_tokens 或分批處理頁面");
  }
  if (!stopReason) {
    throw new Error("Claude 連線中斷，沒有收到完整回應");
  }
  return parseResult("Claude", text);
}

/**
 * 呼叫 Gemini Interactions API 原生讀取 PDF。PDF 直接作為 document 傳入，
 * 避免 20 頁 PNG 膨脹後超過多模態 request 上限，也省掉 render。
 */
async function extractGemini(apiKey, model, pdf, onProgress) {
  const res = await postStream(
    "Gemini",
    "https://generativelanguage.googleapis.com/v1beta/interactions",
    { "x-goog-api-key": apiKey },
    {
      model,
      input: [
        { type: "document", data: await toBase64(pdf), mime_type: "application/pdf" },
        { type: "text", text: USER_PROMPT },
      ],
      system_instruction: SYSTEM_PROMPT,
      generation_config: {
        max_output_tokens: MAX_OUTPUT_TOKENS,
        thinking_level: "high",
      },
      response_format: {
        type: "text",
        mime_type: "application/json",
        schema: SCHEMA,
      },
      stream: true,
    },
  );

  let text = "";
  let status = null;
  for await (const data of readSse(res)) {
    if (data.event_type === "error") {
      throw new Error(`Gemini 回應失敗：${data.error?.message ?? data.error?.code ?? "未知錯誤"}`);
    }
    if (data.event_type === "step.delta" && data.delta?.type === "text") {
      text += data.delta.text;
      onProgress(text.length);
    } else if (data.event_type === "interaction.status_update") {
      status = data.status ?? status;
    } else if (data.event_type === "interaction.completed") {
      // 串流的 interaction 可能省略欄位；沒給 status 就以事件名稱為準。
      status = data.interaction?.status ?? "completed";
    }
  }
  if (status === "incomplete") {
    throw new Error("Gemini 回應不完整（incomplete），可能是輸出超過 max_output_tokens，請分批處理頁面");
  }
  if (["failed", "cancelled", "budget_exceeded"].includes(status)) {
    throw new Error(`Gemini 回應失敗：狀態 ${status}`);
  }
  if (!text) {
    throw new Error("Gemini 沒有回傳內容");
  }
  return parseResult("Gemini", text);
}

/** 解析模型的結構化輸出；JSON 不完整時說清楚是哪一家、可能原因。 */
function parseResult(label, text) {
  try {
    return JSON.parse(text);
  } catch (e) {
    throw new Error(`${label} 回傳的 JSON 不完整或格式錯誤（可能是連線中斷或輸出被截斷）：${e.message}`);
  }
}

/**
 * 各供應商的接法。prompt、schema、輸出格式都相同；
 * render 為 null 代表模型直接接收 PDF。
 */
export const PROVIDERS = Object.freeze({
  openai: {
    label: "OpenAI",
    defaultModel: "gpt-5",
    keyPlaceholder: "sk-…",
    keyUrl: "https://platform.openai.com/api-keys",
    // OpenAI 視覺輸入會先把影像縮到 2048x2048 以內；
    // A4 在 175 DPI 下約 1447x2047，剛好貼齊上限，把解析度額度用滿。
    render: { maxEdge: 2048, dpi: 175 },
    extract: extractOpenAI,
  },
  claude: {
    label: "Claude",
    defaultModel: "claude-opus-5",
    keyPlaceholder: "sk-ant-…",
    keyUrl: "https://platform.claude.com/settings/keys",
    // Opus 5 高解析度視覺上限：長邊 2576px；A4 在 220 DPI 下約 1819x2573。
    render: { maxEdge: 2576, dpi: 220 },
    extract: extractClaude,
  },
  gemini: {
    label: "Gemini",
    defaultModel: "gemini-3.8-flash",
    keyPlaceholder: "AIza…",
    keyUrl: "https://aistudio.google.com/apikey",
    render: null,
    extract: extractGemini,
  },
});

/**
 * 把逐頁 Markdown 串成正文，每頁前面加頁碼標記。
 *
 * 不插 `---` 分隔線：公文的段落、表格常常跨頁，多一條線反而會讓
 * RAG 的 chunker 在句子中間硬切。
 */
function joinPages(pages, total) {
  const blocks = [];
  pages.forEach((page, idx) => {
    const md = (page.markdown ?? "").trim();
    if (!md) return;
    const n = page["頁碼"] || idx + 1;
    blocks.push(`${pageMarker(n, total)}\n\n${md}`);
  });
  return blocks.join("\n\n");
}

function toMarkdownFile(result, sourceName, pages) {
  // 依 SCHEMA 順序重組，確保 frontmatter 欄位順序穩定（JSON 的 key 順序不保證）。
  const meta = {};
  for (const key of META_FIELDS) meta[key] = result.metadata[key];
  meta["來源檔案"] = sourceName;
  meta["頁數"] = pages;

  const doc = new YAML.Document(meta);
  // 「2021-08-31」在 YAML 1.2 是字串，但 PyYAML 走 1.1 會把它讀成 date 物件。
  // 強制加引號，讓 Python 端讀 frontmatter 時拿到的一樣是字串。
  const dateNode = doc.get("發文日期_西元", true);
  if (dateNode) dateNode.type = "QUOTE_SINGLE";
  // indentSeq: false → 清單項目不縮排，與 Python 版 yaml.safe_dump 的排版一致。
  const front = doc.toString({ lineWidth: 1000, indentSeq: false });
  const body = joinPages(result.pages, pages);
  return `---\n${front}---\n\n${body}\n`;
}

/**
 * 完整流程：讀 PDF -> 辨識 -> 組 Markdown，回傳 {filename, markdown}。
 * onEvent({type: "status", msg} | {type: "progress", chars}) 用來回報進度。
 */
export async function convert({ file, provider, model, apiKey, onEvent = () => {} }) {
  const spec = PROVIDERS[provider];
  if (!spec) {
    throw new Error(`不支援的模型供應商：${provider}`);
  }
  if (!apiKey) {
    throw new Error(`請先填入 ${spec.label} API key`);
  }
  const bytes = new Uint8Array(await file.arrayBuffer());

  onEvent({ type: "status", msg: `讀入 ${file.name}，正在分析 PDF…` });
  const task = openPdf(bytes);
  let total;
  let images = null;
  try {
    let doc;
    try {
      doc = await task.promise;
    } catch (e) {
      if (e?.name === "InvalidPDFException") throw new Error("這不是一個有效的 PDF 檔");
      throw e;
    }
    total = doc.numPages;
    if (total === 0) {
      throw new Error("這份 PDF 沒有任何頁面");
    }
    if (total > MAX_PAGES_PER_REQUEST) {
      throw new Error(`${file.name} 有 ${total} 頁，超過單次上限 ${MAX_PAGES_PER_REQUEST}`);
    }
    if (spec.render) {
      images = await renderPages(doc, spec.render, (n) => {
        onEvent({ type: "status", msg: `render 第 ${n}/${total} 頁…` });
      });
    }
  } finally {
    await task.destroy();
  }

  const onProgress = (chars) => onEvent({ type: "progress", chars });
  let result;
  if (images) {
    onEvent({ type: "status", msg: `已 render ${total} 頁，送出 ${spec.label} 辨識…` });
    result = await spec.extract(apiKey, model, images, onProgress);
  } else {
    onEvent({ type: "status", msg: `已讀取 ${total} 頁，送出 ${spec.label} 辨識…` });
    result = await spec.extract(apiKey, model, file, onProgress);
  }
  if (result.pages.length !== total) {
    // 頁碼標記是給 RAG 溯源用的，對不上就等於引用錯頁，寧可讓它爆掉。
    throw new Error(`模型回了 ${result.pages.length} 頁，與輸入的 ${total} 頁不符`);
  }

  const stem = file.name.replace(/\.pdf$/i, "");
  return {
    filename: `${stem}.md`,
    markdown: toMarkdownFile(result, file.name, total),
  };
}
