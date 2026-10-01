import { convert, listModels, MAX_PAGES_PER_REQUEST, PROVIDERS } from "./ocr.js";

const $ = (id) => document.getElementById(id);
const STORAGE_KEY = "official-doc-ocr";

// 只記住供應商與模型這類偏好；金鑰只放在 `keys`（分頁記憶體），重新整理就消失。
const saved = (() => {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) ?? {};
  } catch {
    return {};
  }
})();
const settings = {
  provider: Object.hasOwn(PROVIDERS, saved.provider) ? saved.provider : "openai",
  models: saved.models ?? {},
};
const keys = {};

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  } catch {
    // 瀏覽器封鎖儲存或空間不足：偏好只是方便，不記住也能用。
  }
}

let file = null;
let busy = false;

// 模型清單依「供應商 + 金鑰」快取在記憶體；換 key 才重新查。
const CUSTOM = "__custom__";
const modelLists = {};
let listSeq = 0;

function currentModel() {
  return settings.models[settings.provider] || PROVIDERS[settings.provider].defaultModel;
}

function setModel(model) {
  if (model && model !== PROVIDERS[settings.provider].defaultModel) {
    settings.models[settings.provider] = model;
  } else {
    delete settings.models[settings.provider];
  }
  persist();
}

/** 下拉選單＝API 回的模型；目前選的若不在清單內也保留，最後是「其他」自行輸入。 */
function renderModels() {
  const current = currentModel();
  const list = modelLists[settings.provider];
  // 清單只對查詢時用的那把 key 有效；key 改了就先不顯示舊清單。
  const ids = list && list.key === keys[settings.provider] ? list.ids : [];
  const options = ids.map((id) => new Option(id, id));
  if (!ids.includes(current)) {
    options.unshift(new Option(ids.length ? `${current}（不在清單中）` : current, current));
  }
  options.push(new Option("其他（自行輸入）…", CUSTOM));
  $("model").replaceChildren(...options);
  $("model").value = current;
  $("custommodel").hidden = true;
}

function modelNote(text, isError = false) {
  $("modelnote").textContent = text;
  $("modelnote").classList.toggle("err", isError);
}

async function loadModels() {
  // 每次呼叫都讓進行中的查詢作廢，避免舊結果蓋掉目前供應商／金鑰的狀態。
  const seq = ++listSeq;
  const provider = settings.provider;
  const spec = PROVIDERS[provider];
  const key = keys[provider];
  renderModels();
  if (!key) {
    modelNote(`填入 API key 後（按 Enter 或離開欄位）會用它查詢 ${spec.label} 的模型清單。`);
    return;
  }
  const cached = modelLists[provider];
  if (cached?.key === key) {
    modelNote(cached.note);
    return;
  }
  modelNote(`正在向 ${spec.label} 查詢可用的模型…`);
  try {
    const ids = await listModels(provider, key);
    if (seq !== listSeq) return; // 查詢期間換了供應商或金鑰，結果作廢。
    const note = ids.length
      ? `已從 ${spec.label} 載入 ${ids.length} 個候選模型（${spec.listFilter}）。`
      : `${spec.label} 沒有回傳符合條件的模型，可選「其他」自行輸入。`;
    modelLists[provider] = { key, ids, note };
    renderModels();
    modelNote(note);
  } catch (e) {
    if (seq !== listSeq) return;
    const reason = e.message.replace(/[。.]\s*$/, "");
    modelNote(`無法取得模型清單：${reason}。仍可選「其他」自行輸入模型名稱。`, true);
  }
}

function showProvider() {
  const spec = PROVIDERS[settings.provider];
  $("provider").value = settings.provider;
  $("key").value = keys[settings.provider] ?? "";
  $("key").placeholder = `${spec.label} API key（${spec.keyPlaceholder}）`;
  $("keyurl").href = spec.keyUrl;
  $("keyurl").textContent = `取得 ${spec.label} API key`;
  loadModels();
}

$("provider").append(...Object.entries(PROVIDERS).map(([name, spec]) => new Option(spec.label, name)));
$("hint").textContent = `或點一下選擇檔案（單份上限 ${MAX_PAGES_PER_REQUEST} 頁）`;
showProvider();

$("provider").onchange = () => {
  settings.provider = $("provider").value;
  persist();
  showProvider();
};
$("model").onchange = () => {
  if ($("model").value === CUSTOM) {
    $("custommodel").value = "";
    $("custommodel").hidden = false;
    $("custommodel").focus();
  } else {
    setModel($("model").value);
  }
};
$("custommodel").onchange = () => {
  const model = $("custommodel").value.trim();
  if (model) setModel(model);
  renderModels();
};
$("key").oninput = () => {
  keys[settings.provider] = $("key").value.trim();
  listSeq++; // 讓進行中的查詢作廢；舊 key 的清單也不再顯示。
  renderModels();
  modelNote("輸入完成後（按 Enter 或離開欄位）會重新查詢模型清單。");
};
// 輸入完（離開欄位或按 Enter）才查清單，避免每打一個字就打一次 API。
$("key").onchange = () => loadModels();

function pick(f) {
  if (!f) return;
  if (!/\.pdf$/i.test(f.name)) { showError("只接受 .pdf 檔"); return; }
  file = f;
  const name = document.createElement("strong");
  name.textContent = f.name;
  const size = document.createElement("span");
  size.textContent = `${(f.size / 1024 / 1024).toFixed(1)} MB — 換一個檔案就再拖一次`;
  $("drop").replaceChildren(name, size);
  $("go").disabled = busy;
}

$("drop").onclick = () => $("file").click();
$("file").onchange = (e) => pick(e.target.files[0]);
["dragenter", "dragover"].forEach((ev) => $("drop").addEventListener(ev, (e) => {
  e.preventDefault(); $("drop").classList.add("over");
}));
["dragleave", "drop"].forEach((ev) => $("drop").addEventListener(ev, (e) => {
  e.preventDefault(); $("drop").classList.remove("over");
}));
$("drop").addEventListener("drop", (e) => pick(e.dataTransfer.files[0]));

function showError(msg) {
  $("status").classList.add("on");
  const err = document.createElement("span");
  err.className = "err";
  err.textContent = msg;
  $("msg").replaceChildren(err);
  $("meta").textContent = "";
  document.querySelector(".bar").classList.add("off");
}

$("go").onclick = async () => {
  if (!file || busy) return;
  busy = true;
  $("go").disabled = true;
  $("result").classList.remove("on");
  $("status").classList.add("on");
  document.querySelector(".bar").classList.remove("off");
  $("msg").textContent = "準備中…";
  $("meta").textContent = "";

  const t0 = Date.now();
  let chars = 0;
  const renderMeta = () => {
    const secs = ((Date.now() - t0) / 1000).toFixed(0);
    $("meta").textContent = chars ? `已輸出 ${chars} 字 — ${secs} 秒` : `${secs} 秒`;
  };
  const tick = setInterval(renderMeta, 1000);

  try {
    const spec = PROVIDERS[settings.provider];
    const done = await convert({
      file,
      provider: settings.provider,
      model: currentModel(),
      apiKey: $("key").value.trim(),
      onEvent: (ev) => {
        if (ev.type === "status") $("msg").textContent = ev.msg;
        else if (ev.type === "progress") { chars = ev.chars; renderMeta(); }
      },
    });

    const secs = ((Date.now() - t0) / 1000).toFixed(1);
    $("status").classList.remove("on");
    $("result").classList.add("on");
    $("outname").textContent = `${done.filename}（${secs} 秒）`;
    $("out").textContent = done.markdown;
    $("download").onclick = () => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([done.markdown], { type: "text/markdown" }));
      a.download = done.filename;
      a.click();
      URL.revokeObjectURL(a.href);
    };
    $("copy").onclick = async () => {
      await navigator.clipboard.writeText(done.markdown);
      $("copy").textContent = "已複製";
      setTimeout(() => ($("copy").textContent = "複製"), 1500);
    };
  } catch (e) {
    console.error(e);
    showError(e.message);
  } finally {
    clearInterval(tick);
    busy = false;
    $("go").disabled = !file;
  }
};
