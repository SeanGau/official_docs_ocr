import { convert, MAX_PAGES_PER_REQUEST, PROVIDERS } from "./ocr.js";

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

function showProvider() {
  const spec = PROVIDERS[settings.provider];
  $("provider").value = settings.provider;
  $("model").value = settings.models[settings.provider] || spec.defaultModel;
  $("model").placeholder = spec.defaultModel;
  $("key").value = keys[settings.provider] ?? "";
  $("key").placeholder = `${spec.label} API key（${spec.keyPlaceholder}）`;
  $("keyurl").href = spec.keyUrl;
  $("keyurl").textContent = `取得 ${spec.label} API key`;
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
  const model = $("model").value.trim();
  if (model && model !== PROVIDERS[settings.provider].defaultModel) {
    settings.models[settings.provider] = model;
  } else {
    delete settings.models[settings.provider];
  }
  persist();
};
$("key").oninput = () => {
  keys[settings.provider] = $("key").value.trim();
};

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
      model: $("model").value.trim() || spec.defaultModel,
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
