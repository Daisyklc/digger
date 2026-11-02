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
  const conc = parseInt($("concurrency").value || "2", 10);
  return {
    book: $("book").value.trim() || "untitled",
    target_lang: $("targetLang").value,
    source_lang: $("sourceLang").value,
    direction: $("direction").value,
    interval: parseFloat($("interval").value || "1.5"),
    polish: $("polish").checked,
    concurrency: Math.max(1, Math.min(4, Number.isFinite(conc) ? conc : 2)),
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

async function refreshBooks() {
  const sel = $("bookSelect");
  const current = $("book").value.trim();
  try {
    const d = await api("/session/books");
    const books = d.books || [];
    sel.innerHTML = "";
    const opt0 = document.createElement("option");
    opt0.value = "";
    opt0.textContent = books.length ? "— 选择已有采集子目录 —" : "— 暂无采集子目录 —";
    sel.appendChild(opt0);
    for (const b of books) {
      const opt = document.createElement("option");
      opt.value = b.book;
      opt.textContent =
        `${b.book}  (截图 ${b.existing_count} · OCR ${b.ocr_done} · 译 ${b.tr_done})`;
      if (b.book === current) opt.selected = true;
      sel.appendChild(opt);
    }
    if (current && ![...sel.options].some((o) => o.value === current)) {
      // 新书名尚未落盘, 保持输入框即可
    }
    updateBookStats();
  } catch (e) {
    sel.innerHTML = `<option value="">加载失败: ${e.message}</option>`;
  }
}

async function updateBookStats() {
  const book = $("book").value.trim();
  const el = $("bookStats");
  if (!book) {
    el.textContent = "选择或输入书籍文件夹";
    return;
  }
  try {
    const info = await api(`/session/capture-info?book=${encodeURIComponent(book)}`);
    el.textContent =
      `「${book}」截图 ${info.existing_count} · 下一 index ${info.next_append_index}` +
      ` · 待 OCR ${info.ocr_pending || 0} · 已 OCR ${info.ocr_done || 0}` +
      ` · 待译 ${info.tr_pending || 0} · 已译 ${info.tr_done || 0}`;
  } catch {
    el.textContent = `「${book}」`;
  }
}

function paintProgress(p) {
  $("progressMsg").textContent = p.message || "";
  const captured = p.pages_captured != null ? p.pages_captured : p.index || 0;
  const done = p.pages_done || 0;
  const pending = p.queue_pending || 0;
  const mb = p.queue_mb != null ? p.queue_mb : 0;
  const nextIdx = p.next_append_index != null ? p.next_append_index : p.index;
  const ocrP = p.ocr_pending != null ? p.ocr_pending : 0;
  const ocrD = p.ocr_done != null ? p.ocr_done : 0;
  const trP = p.tr_pending != null ? p.tr_pending : 0;
  const trD = p.tr_done != null ? p.tr_done : 0;
  $("progressNum").textContent =
    `截图 ${captured} · 下一 ${nextIdx} · OCR ${ocrD}/${ocrD + ocrP}` +
    ` · 译 ${trD}/${trD + trP} · 队列 ${pending} · ${mb}MB` +
    (p.concurrency ? ` · 并行 ${p.concurrency}` : "");
  if (p.last_orig != null) $("orig").textContent = p.last_orig;
  if (p.last_translated != null) $("trans").textContent = p.last_translated;
  const running = !!p.running;
  $("stop").disabled = !running;
  if ($("startCapture")) $("startCapture").disabled = !!p.capturing;
  if ($("startOcr")) $("startOcr").disabled = !!p.ocr_running;
  if ($("startTranslate")) $("startTranslate").disabled = !!p.translate_running;
  if ($("startAuto")) {
    $("startAuto").disabled = !!(p.capturing && p.ocr_running && p.translate_running);
  }
  $("progressFill").style.width = running ? "100%" : (done || ocrD || trD ? "100%" : "0");
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

$("refreshBooks").addEventListener("click", () => refreshBooks());

$("bookSelect").addEventListener("change", () => {
  const v = $("bookSelect").value;
  if (v) {
    $("book").value = v;
    updateBookStats();
  }
});

$("book").addEventListener("change", () => {
  updateBookStats();
  const v = $("book").value.trim();
  const sel = $("bookSelect");
  if ([...sel.options].some((o) => o.value === v)) sel.value = v;
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
    refreshBooks();
  } catch (e) {
    alert("采集启动失败: " + e.message);
  }
});

$("startOcr").addEventListener("click", async () => {
  try {
    await api("/session/ocr", "POST", cfg());
    startPolling();
  } catch (e) {
    alert("OCR 启动失败: " + e.message);
  }
});

$("startTranslate").addEventListener("click", async () => {
  try {
    await api("/session/translate", "POST", cfg());
    startPolling();
  } catch (e) {
    alert("翻译启动失败: " + e.message);
  }
});

$("startAuto").addEventListener("click", async () => {
  try {
    const book = cfg().book;
    const info = await api(`/session/capture-info?book=${encodeURIComponent(book)}`);
    if (info.existing_count > 0) {
      const ok = confirm(
        `全自动运行「${book}」\n` +
          `已有 ${info.existing_count} 页, 采集将从 index=${info.next_append_index} 追加。\n` +
          `同时启动 OCR + 翻译 (并行 ${cfg().concurrency})。\n\n继续？`
      );
      if (!ok) return;
    }
    await api("/session/auto", "POST", cfg());
    startPolling();
    refreshBooks();
  } catch (e) {
    alert("全自动启动失败: " + e.message);
  }
});

$("stop").addEventListener("click", async () => {
  try {
    paintProgress(await api("/session/stop", "POST", { book: cfg().book }));
  } catch (e) {
    alert(e.message);
  }
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
    await downloadTxt(book, "orig", `${book}.原文.txt`);
    await downloadTxt(book, "translated", `${book}.译文.txt`);
  } catch (e) {
    alert(e.message || String(e));
  }
});

// --- 轮询 ---
let pollTimer = null;
async function pollProgress() {
  try {
    const book = cfg().book;
    paintProgress(await api(`/session/progress?book=${encodeURIComponent(book)}`));
  } catch {}
}
function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const book = cfg().book;
    const p = await api(`/session/progress?book=${encodeURIComponent(book)}`).catch(() => null);
    if (p) paintProgress(p);
    if (p && !p.running) {
      clearInterval(pollTimer);
      pollTimer = null;
      refreshBooks();
      updateBookStats();
    }
  }, 1000);
}

health();
refreshBrowser();
refreshBooks();
pollProgress();
setInterval(refreshBrowser, 5000);
