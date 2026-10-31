// WebTranslator 本地仪表盘前端。
// 只和本地后端(同源)通信; 去打开/控制目标站的是后端的 Playwright 浏览器, 不是这个页面。

const $ = (id) => document.getElementById(id);
async function api(path, method = "GET", body) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const res = await fetch(path, opt);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.status);
  return res.json();
}

function cfg() {
  return {
    book: $("book").value.trim() || "untitled",
    target_lang: $("targetLang").value,
    source_lang: $("sourceLang").value,
    direction: $("direction").value,
    interval: parseFloat($("interval").value || "1.5"),
    polish: $("polish").checked,
  };
}

async function health() {
  const el = $("health");
  try {
    const d = await api("/healthz");
    if (d.llm_ready) {
      el.textContent = `就绪 · OCR ${d.ocr_backend}`;
      el.className = "pill ok";
    } else {
      el.textContent = `未就绪: 缺 ${d.missing.join(", ")}`;
      el.className = "pill bad";
    }
  } catch {
    el.textContent = "后端离线";
    el.className = "pill bad";
  }
}

async function refreshBrowser() {
  try {
    const s = await api("/browser/status");
    $("browserStatus").textContent = s.running
      ? `已连接: ${s.title || ""} — ${s.url}`
      : "浏览器未启动";
  } catch {
    $("browserStatus").textContent = "浏览器未启动";
  }
}

function paintProgress(p) {
  $("progressMsg").textContent = p.message || "";
  $("progressNum").textContent = `已翻 ${p.pages_done || 0} 页`;
  if (p.last_orig != null) $("orig").textContent = p.last_orig;
  if (p.last_translated != null) $("trans").textContent = p.last_translated;
  const running = !!p.running;
  $("stop").disabled = !running;
  $("startAuto").disabled = running;
  // 无总页数, 用"动态条"表示运行中
  $("progressFill").style.width = running ? "100%" : (p.pages_done ? "100%" : "0");
  $("progressFill").style.background = running ? "#2d7ff9" : "#2faa5b";
}

// --- 事件 ---
$("launch").addEventListener("click", async () => {
  $("browserStatus").textContent = "启动中…";
  try {
    await api("/browser/launch", "POST", { url: $("url").value.trim() || undefined });
  } catch (e) {
    $("browserStatus").textContent = "启动失败: " + e.message;
  }
  refreshBrowser();
});

$("goto").addEventListener("click", async () => {
  const url = $("url").value.trim();
  if (!url) return;
  try { await api("/browser/goto", "POST", { url }); } catch (e) { alert(e.message); }
  refreshBrowser();
});

$("translateOne").addEventListener("click", async () => {
  try {
    const out = await api("/session/translate-current", "POST", cfg());
    $("orig").textContent = out.orig;
    $("trans").textContent = out.translated;
    $("progressMsg").textContent = `已翻译第 ${out.index} 页 (${out.kind})`;
  } catch (e) {
    alert("翻译失败: " + e.message);
  }
  pollProgress();
});

$("startAuto").addEventListener("click", async () => {
  try {
    await api("/session/auto", "POST", cfg());
    startPolling();
  } catch (e) {
    alert("启动失败: " + e.message);
  }
});

$("stop").addEventListener("click", async () => {
  try { paintProgress(await api("/session/stop", "POST", {})); } catch (e) { alert(e.message); }
});

$("export").addEventListener("click", async () => {
  const book = $("book").value.trim() || "untitled";
  const url = `/novel/export?book=${encodeURIComponent(book)}&format=md&layout=bilingual`;
  const res = await fetch(url);
  const text = await res.text();
  const blob = new Blob([text], { type: "text/markdown" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${book}.md`;
  a.click();
});

// --- 轮询 ---
let pollTimer = null;
async function pollProgress() {
  try { paintProgress(await api("/session/progress")); } catch {}
}
function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const p = await api("/session/progress").catch(() => null);
    if (p) paintProgress(p);
    if (p && !p.running) { clearInterval(pollTimer); pollTimer = null; }
  }, 1000);
}

health();
refreshBrowser();
pollProgress();
setInterval(refreshBrowser, 5000);
