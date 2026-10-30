// WebTranslator popup: 配置读写 + 触发翻译 (含小说两种模式与导出)。

const fields = [
  "mode", "targetLang", "series", "backend",
  "reading", "book", "direction", "interval",
];

function backendUrl() {
  return (document.getElementById("backend").value || "http://127.0.0.1:8000").trim();
}

function toggleNovel() {
  const isNovel = document.getElementById("mode").value === "novel";
  document.getElementById("novelOpts").style.display = isNovel ? "block" : "none";
  document.getElementById("export").style.display = isNovel ? "block" : "none";
  document.getElementById("stop").style.display = isNovel ? "block" : "none";
  document.getElementById("run").textContent = isNovel ? "开始" : "翻译本页";
}

async function load() {
  const saved = await chrome.storage.sync.get([...fields, "polish"]);
  for (const f of fields) {
    const el = document.getElementById(f);
    if (el && saved[f] != null) el.value = saved[f];
  }
  if (saved.polish != null) document.getElementById("polish").checked = saved.polish;
  toggleNovel();
  checkHealth(backendUrl());
}

async function save() {
  const payload = {};
  for (const f of fields) payload[f] = (document.getElementById(f).value || "").trim();
  payload.polish = document.getElementById("polish").checked;
  await chrome.storage.sync.set(payload);
  return payload;
}

async function checkHealth(backend) {
  const status = document.getElementById("status");
  try {
    const res = await fetch(`${backend}/healthz`);
    const data = await res.json();
    status.textContent = data.llm_ready
      ? `后端就绪 · OCR: ${data.ocr_backend}`
      : `后端未就绪, 缺少: ${data.missing.join(", ")}`;
  } catch {
    status.textContent = "无法连接后端, 请检查地址";
  }
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

document.getElementById("run").addEventListener("click", async () => {
  const cfg = await save();
  const tab = await activeTab();
  const msg = { type: "wt-run", mode: cfg.mode };
  if (cfg.mode === "novel") {
    Object.assign(msg, {
      reading: cfg.reading || "realtime",
      book: cfg.book || "untitled",
      polish: cfg.polish,
      direction: cfg.direction || "rtl",
      interval: parseInt(cfg.interval || "1500", 10),
    });
  }
  chrome.tabs.sendMessage(tab.id, msg);
  if (cfg.mode !== "novel") window.close();
});

document.getElementById("stop").addEventListener("click", async () => {
  const tab = await activeTab();
  chrome.tabs.sendMessage(tab.id, { type: "wt-stop" });
});

document.getElementById("export").addEventListener("click", async () => {
  const cfg = await save();
  const book = cfg.book || "untitled";
  const url = `${backendUrl()}/novel/export?book=${encodeURIComponent(book)}&format=md&layout=bilingual`;
  try {
    const res = await fetch(url);
    const text = await res.text();
    const blob = new Blob([text], { type: "text/markdown" });
    const dl = URL.createObjectURL(blob);
    await chrome.downloads.download({ url: dl, filename: `${book}.md` });
  } catch {
    document.getElementById("status").textContent = "导出失败, 请检查后端";
  }
});

document.getElementById("mode").addEventListener("change", () => { toggleNovel(); save(); });
for (const f of fields) {
  const el = document.getElementById(f);
  if (el) el.addEventListener("change", save);
}
document.getElementById("polish").addEventListener("change", save);

load();
