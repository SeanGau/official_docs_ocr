/**
 * 公文 PDF -> Markdown（含 YAML frontmatter metadata）
 *
 * 這是 ../ocr_doc.py 的 JavaScript 移植：用 MuPDF 把掃描 PDF 每頁轉成點陣圖，
 * 交給 OpenAI 視覺模型做 OCR + 版面重建 + metadata 抽取，組成 .md 文字。
 *
 * prompt、schema、頁碼標記、各種上限都與 Python 版逐字一致，兩邊產出的 .md 可以互換。
 */

import * as mupdf from "mupdf";
import YAML from "yaml";

export const DEFAULT_MODEL = "gpt-5";

// OpenAI 視覺輸入會先把影像縮到 2048x2048 以內，長邊超過就是白花 token。
const MAX_EDGE = 2048;
// A4 在 175 DPI 下約 1447x2047，剛好貼齊 MAX_EDGE，把解析度額度用滿。
const RENDER_DPI = 175;

// RAG 切 chunk 時用來還原「這段話出自第幾頁」。用 HTML 註解，
// 算是 Markdown 的隱形內容，不會污染顯示出來的正文。
const pageMarker = (n, total) => `<!-- page: ${n}/${total} -->`;

// 每份文件的所有頁面一次送出，讓模型能跨頁理解（例如函文 + 附件表格）。
export const MAX_PAGES_PER_REQUEST = 20;

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

/** 把 PDF 每頁 render 成 PNG bytes，長邊不超過 MAX_EDGE。 */
export function renderPages(buf) {
  const images = [];
  const doc = mupdf.Document.openDocument(buf, "application/pdf");
  try {
    for (let i = 0; i < doc.countPages(); i++) {
      const page = doc.loadPage(i);
      try {
        let zoom = RENDER_DPI / 72;
        // 先量原尺寸，長邊超標就等比例縮，避免 render 兩次大圖。
        const bounds = page.getBounds();
        const w = (bounds[2] - bounds[0]) * zoom;
        const h = (bounds[3] - bounds[1]) * zoom;
        if (Math.max(w, h) > MAX_EDGE) {
          zoom *= MAX_EDGE / Math.max(w, h);
        }
        const pixmap = page.toPixmap(
          mupdf.Matrix.scale(zoom, zoom),
          mupdf.ColorSpace.DeviceRGB,
          false,
          true,
        );
        try {
          images.push(pixmap.asPNG());
        } finally {
          pixmap.destroy();
        }
      } finally {
        page.destroy();
      }
    }
  } finally {
    doc.destroy();
  }
  return images;
}

function buildContent(images) {
  const content = [];
  images.forEach((png, idx) => {
    content.push({ type: "input_text", text: `--- 第 ${idx + 1} 頁 ---` });
    const b64 = Buffer.from(png).toString("base64");
    content.push({
      type: "input_image",
      // 公文字小，低解析度會直接認不出來，一律用 high。
      detail: "high",
      image_url: `data:image/png;base64,${b64}`,
    });
  });
  content.push({ type: "input_text", text: USER_PROMPT });
  return content;
}

/**
 * 呼叫模型做 OCR + 抽 metadata，回傳 {metadata, pages}。
 * onProgress(chars) 會在模型輸出過程中被呼叫，用來推進度給前端。
 */
export async function extract(client, model, images, onProgress) {
  // 影像多、輸出長，一律 streaming 避免 HTTP timeout。
  const stream = client.responses.stream({
    model,
    max_output_tokens: 32000,
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
    input: [{ role: "user", content: buildContent(images) }],
  });

  let chars = 0;
  for await (const event of stream) {
    if (event.type === "response.output_text.delta") {
      chars += event.delta.length;
      onProgress?.(chars);
    }
  }
  const response = await stream.finalResponse();

  for (const item of response.output ?? []) {
    for (const part of item.content ?? []) {
      if (part.type === "refusal") {
        throw new Error(`模型拒絕處理：${part.refusal}`);
      }
    }
  }
  if (response.status === "incomplete") {
    const reason = response.incomplete_details?.reason ?? response.status;
    if (reason === "max_output_tokens") {
      throw new Error("輸出被 max_output_tokens 截斷，請調高上限或分批處理頁面");
    }
    throw new Error(`回應不完整：${reason}`);
  }

  return JSON.parse(response.output_text);
}

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
 * 完整流程：render -> 辨識 -> 組 Markdown。
 * onEvent({type, ...}) 用來回報進度，型別與 SSE 事件一致。
 */
export async function convert(client, model, buf, sourceName, onEvent = () => {}) {
  const images = renderPages(buf);
  if (images.length === 0) {
    throw new Error("這份 PDF 沒有任何頁面");
  }
  if (images.length > MAX_PAGES_PER_REQUEST) {
    throw new Error(`${sourceName} 有 ${images.length} 頁，超過單次上限 ${MAX_PAGES_PER_REQUEST}`);
  }
  onEvent({ type: "status", msg: `已 render ${images.length} 頁，送出辨識…`, pages: images.length });

  const result = await extract(client, model, images, (chars) => {
    onEvent({ type: "progress", chars });
  });
  if (result.pages.length !== images.length) {
    // 頁碼標記是給 RAG 溯源用的，對不上就等於引用錯頁，寧可讓它爆掉。
    throw new Error(`模型回了 ${result.pages.length} 頁，與輸入的 ${images.length} 頁不符`);
  }

  const stem = sourceName.replace(/\.pdf$/i, "");
  return {
    filename: `${stem}.md`,
    markdown: toMarkdownFile(result, sourceName, images.length),
  };
}
