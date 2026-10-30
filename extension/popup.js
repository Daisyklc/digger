// WebTranslator popup: 配置读写 + 触发本页翻译。

const fields = ["mode", "targetLang", "series", "backend"];

async function load() {
  const saved = await chrome.storage.sync.get(fields);
  for (const f of fields) {
    const el = document.getElementById(f);
    if (saved[f] != null) el.value = saved[f];
  }
  checkHealth(saved.backend || "http://127.0.0.1:8000");
}

async function save() {
  const payload = {};
  for (const f of fields) payload[f] = document.getElementById(f).value.trim();
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

document.getElementById("run").addEventListener("click", async () => {
  const cfg = await save();
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  chrome.tabs.sendMessage(tab.id, { type: "wt-run", mode: cfg.mode });
  window.close();
});

for (const f of fields) {
  document.getElementById(f).addEventListener("change", save);
}

load();
