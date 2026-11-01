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
  const captured = p.pages_captured != null ? p.pages_captured : p.index || 0;
  const done = p.pages_done || 0;
  const pending = p.queue_pending || 0;
  const mb = p.queue_mb != null ? p.queue_mb : 0;
  const nextIdx = p.next_append_index != null ? p.next_append_index : p.index;
  $("progressNum").textContent =
    `截图 ${captured} · 下一index ${nextIdx} · 已译 ${done} · 队列 ${pending} · 占用 ${mb}MB`;
  if (p.last_orig != null) $("orig").textContent = p.last_orig;
  if (p.last_translated != null) $("trans").textContent = p.last_translated;
  const running = !!p.running;
  $("stop").disabled = !running;
  if ($("startCapture")) $("startCapture").disabled = !!p.capturing;
  if ($("startProcess")) $("startProcess").disabled = !!p.working;
  // 无总页数, 用"动态条"表示运行中
  $("progressFill").style.width = running ? "100%" : (done ? "100%" : "0");
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

$("startCapture").addEventListener("click", async () => {
  try {
    // 续采前提示: 只追加, 电子书位置靠人工
    const preview = await api(`/session/progress`);
    // progress 绑定的是上次 book; 先用当前表单 book 打一次 capture 前确认
    const book = cfg().book;
    const info = await api(`/session/capture-info?book=${encodeURIComponent(book)}`);
    if (info.existing_count > 0) {
      const ok = confirm(
        `「${book}」已有 ${info.existing_count} 页截图。\n` +
          `将从 index=${info.next_append_index} 追加写入 (不覆盖旧文件)。\n\n` +
          `请确认受控浏览器里电子书已手动翻到衔接页。\n继续？`
      );
      if (!ok) return;
    }
    await api("/session/capture", "POST", cfg());
    startPolling();
  } catch (e) {
    alert("采集启动失败: " + e.message);
  }
});

$("startProcess").addEventListener("click", async () => {
  try {
    await api("/session/process", "POST", cfg());
    startPolling();
  } catch (e) {
    alert("处理启动失败: " + e.message);
  }
});

$("stop").addEventListener("click", async () => {
  try { paintProgress(await api("/session/stop", "POST", {})); } catch (e) { alert(e.message); }
});

async function downloadTxt(book, layout, filename) {
  const url = `/novel/export?book=${encodeURIComponent(book)}&format=txt&layout=${layout}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`导出失败 (${res.status})`);
  const text = await res.text();
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

$("export").addEventListener("click", async () => {
  const book = $("book").value.trim() || "untitled";
  try {
    // 原文 / 译文分成两个 txt, 便于分别阅读或后处理
    await downloadTxt(book, "orig", `${book}.原文.txt`);
    await downloadTxt(book, "translated", `${book}.译文.txt`);
  } catch (e) {
    alert(e.message || String(e));
  }
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
