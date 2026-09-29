#!/usr/bin/env node
/**
 * 公文 PDF -> Markdown 的網頁服務。
 *
 * 用法：
 *   npm install
 *   GEMINI_API_KEY=... node server.mjs        # 也支援 OPENAI_API_KEY
 *   瀏覽器開 http://localhost:8787
 *
 * 路由：
 *   GET  /                 單頁 UI
 *   GET  /api/config       前端要用的供應商、預設模型與頁數上限
 *   POST /api/convert?name=x.pdf&provider=gemini
 *        body 直接是 PDF 原始 bytes（不走 multipart，省一個依賴），
 *        回應是 SSE：status / progress / done / error。
 */

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import OpenAI from "openai";
import { GoogleGenAI } from "@google/genai";
import { convert, DEFAULT_MODELS, MAX_PAGES_PER_REQUEST } from "./ocr.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC = path.join(HERE, "public");

// 上傳大小上限。20 頁的掃描公文大概幾 MB，50MB 已經很寬鬆，
// 主要是避免有人一直灌 body 把記憶體吃光。
const MAX_UPLOAD = 50 * 1024 * 1024;

const PROVIDERS = Object.freeze({
  openai: {
    label: "OpenAI",
    apiKeyEnv: "OPENAI_API_KEY",
    modelEnv: "OPENAI_MODEL",
    defaultModel: DEFAULT_MODELS.openai,
    makeClient: (apiKey) => new OpenAI({ apiKey, timeout: 1800_000 }),
  },
  gemini: {
    label: "Gemini",
    apiKeyEnv: "GEMINI_API_KEY",
    modelEnv: "GEMINI_MODEL",
    defaultModel: DEFAULT_MODELS.gemini,
    makeClient: (apiKey) => new GoogleGenAI({ apiKey }),
  },
});

function chooseDefaultProvider() {
  const requested = process.env.OCR_PROVIDER?.trim().toLowerCase();
  if (requested) {
    if (!Object.hasOwn(PROVIDERS, requested)) {
      throw new Error(`OCR_PROVIDER 不支援「${requested}」，可用值：${Object.keys(PROVIDERS).join(", ")}`);
    }
    return requested;
  }
  // 只放 Gemini key 時自動選 Gemini；其餘情況維持既有的 OpenAI 預設。
  if (!process.env.OPENAI_API_KEY && process.env.GEMINI_API_KEY) return "gemini";
  return "openai";
}

function providerModel(name) {
  const provider = PROVIDERS[name];
  return process.env[provider.modelEnv] || provider.defaultModel;
}

/**
 * 把 .env 的 KEY=VALUE 塞進 process.env（已存在的環境變數優先）。
 *
 * 行為對齊 ocr_doc.py 的 load_dotenv：先找 web/.env，再找專案根目錄的 .env。
 * 不用 process.loadEnvFile，因為它會覆蓋既有的環境變數。
 */
function loadDotenv() {
  for (const dir of [HERE, path.join(HERE, "..")]) {
    const file = path.join(dir, ".env");
    if (!fs.existsSync(file)) continue;
    for (const raw of fs.readFileSync(file, "utf8").split("\n")) {
      const line = raw.trim();
      if (!line || line.startsWith("#") || !line.includes("=")) continue;
      const idx = line.indexOf("=");
      const key = line.slice(0, idx).trim();
      const val = line.slice(idx + 1).trim().replace(/^['"]|['"]$/g, "");
      if (!(key in process.env)) process.env[key] = val;
    }
  }
}

/** 讀完整個 request body，超過上限就中止。 */
function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(Object.assign(new Error("檔案太大"), { statusCode: 413 }));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

function sendJson(res, status, obj) {
  const body = Buffer.from(JSON.stringify(obj), "utf8");
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "content-length": body.length });
  res.end(body);
}

async function handleConvert(getClient, defaultProvider, req, res, url) {
  const name = url.searchParams.get("name") || "document.pdf";
  if (!name.toLowerCase().endsWith(".pdf")) {
    sendJson(res, 400, { error: "只接受 .pdf 檔" });
    return;
  }
  const providerName = (url.searchParams.get("provider") || defaultProvider).toLowerCase();
  if (!Object.hasOwn(PROVIDERS, providerName)) {
    sendJson(res, 400, { error: `不支援的模型供應商：${providerName}` });
    return;
  }


  let buf;
  try {
    buf = await readBody(req, MAX_UPLOAD);
  } catch (e) {
    sendJson(res, e.statusCode ?? 400, { error: e.message });
    return;
  }
  // PDF magic number，擋掉改副檔名的假檔，省下一次白花的 API 呼叫。
  if (buf.length < 5 || buf.subarray(0, 5).toString("latin1") !== "%PDF-") {
    sendJson(res, 400, { error: "這不是一個 PDF 檔（檔頭不是 %PDF-）" });
    return;
  }

  res.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-cache, no-transform",
    connection: "keep-alive",
    "x-accel-buffering": "no",
  });
  const send = (obj) => res.write(`data: ${JSON.stringify(obj)}\n\n`);
  // 心跳：長工作中間沒有事件時，讓中介的 proxy 知道連線還活著。
  const beat = setInterval(() => res.write(":\n\n"), 15000);

  const model = url.searchParams.get("model") || providerModel(providerName);
  send({ type: "status", msg: `讀入 ${name}，正在分析 PDF…` });
  try {
    const out = await convert(getClient(providerName), providerName, model, buf, name, send);
    send({ type: "done", ...out });
  } catch (e) {
    console.error(`[convert] ${name}: ${e.stack ?? e.message}`);
    send({ type: "error", msg: e.message });
  } finally {
    clearInterval(beat);
    res.end();
  }
}

function serveStatic(res, urlPath) {
  const rel = urlPath === "/" ? "index.html" : urlPath.replace(/^\/+/, "");
  const file = path.join(PUBLIC, rel);
  // 擋掉 ../ 之類的路徑穿越。
  if (!file.startsWith(PUBLIC + path.sep) || !fs.existsSync(file)) {
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    res.end("Not Found");
    return;
  }
  const types = { ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8" };
  res.writeHead(200, { "content-type": types[path.extname(file)] ?? "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
}

function main() {
  loadDotenv();
  const defaultProvider = chooseDefaultProvider();
  const selected = PROVIDERS[defaultProvider];
  if (!process.env[selected.apiKeyEnv]) {
    console.warn(
      `警告：沒有讀到 ${selected.apiKeyEnv}，${selected.label} 轉檔會失敗。` +
      "請設環境變數或寫進 .env。",
    );
  }
  // 每家 client 都延後建立：缺金鑰時讓錯誤在轉檔時回報前端，而不是讓服務起不來。
  // 多頁文件可能跑好幾分鐘，OpenAI client 使用 30 分鐘 timeout；
  // Gemini 的單次 request timeout 則設在 extractGemini。
  const clients = new Map();
  const getClient = (name) => {
    const provider = PROVIDERS[name];
    const apiKey = process.env[provider.apiKeyEnv];
    if (!apiKey) {
      throw new Error(`沒有設定 ${provider.apiKeyEnv}，請設環境變數或寫進 .env 後重啟服務`);
    }
    if (!clients.has(name)) clients.set(name, provider.makeClient(apiKey));
    return clients.get(name);
  };

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, `http://${req.headers.host ?? "localhost"}`);
    if (req.method === "POST" && url.pathname === "/api/convert") {
      handleConvert(getClient, defaultProvider, req, res, url);
    } else if (req.method === "GET" && url.pathname === "/api/config") {
      sendJson(res, 200, {
        provider: defaultProvider,
        model: providerModel(defaultProvider),
        providers: Object.fromEntries(
          Object.entries(PROVIDERS).map(([name, provider]) => [
            name,
            {
              label: provider.label,
              model: providerModel(name),
              available: Boolean(process.env[provider.apiKeyEnv]),
            },
          ]),
        ),
        maxPages: MAX_PAGES_PER_REQUEST,
        maxUploadMB: MAX_UPLOAD / 1024 / 1024,
      });
    } else if (req.method === "GET") {
      serveStatic(res, url.pathname);
    } else {
      res.writeHead(405, { "content-type": "text/plain; charset=utf-8" });
      res.end("Method Not Allowed");
    }
  });

  // Node 預設 requestTimeout 是 5 分鐘，長文件會在辨識中途被砍掉連線。
  server.requestTimeout = 0;
  server.headersTimeout = 0;
  server.keepAliveTimeout = 0;

  const port = Number(process.env.PORT) || 8787;
  server.listen(port, () => {
    console.log(`公文轉檔服務啟動：http://localhost:${port}`);
  });
}

main();
