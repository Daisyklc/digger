// WebTranslator content script.
// 职责: 模式识别 (文本型 / 漫画型) -> 内容采集 -> 请求后端 -> 原位回填。
// 文本型: 替换文本节点并保留排版; 漫画型: 译文图层叠加在原图之上。

(function () {
  "use strict";

  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "CODE", "PRE", "NOSCRIPT", "TEXTAREA"]);
  const MIN_LEN = 2;

  // ---- 模式识别 ----------------------------------------------------------
  function detectMode() {
    const imgs = Array.from(document.images).filter((i) => i.naturalHeight > 400);
    const textLen = document.body.innerText.replace(/\s+/g, "").length;
    // 连续大图多、正文文字稀疏 -> 漫画型
    if (imgs.length >= 3 && textLen < 400) return "comic";
    return "ebook";
  }

  // ---- 文本型: 采集 ------------------------------------------------------
  function collectTextNodes() {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const p = node.parentElement;
        if (!p || SKIP_TAGS.has(p.tagName)) return NodeFilter.FILTER_REJECT;
        if (p.isContentEditable) return NodeFilter.FILTER_REJECT;
        if (p.closest("[data-wt-done]")) return NodeFilter.FILTER_REJECT;
        const t = node.textContent.trim();
        if (t.length < MIN_LEN || /^[\d\s\p{P}]+$/u.test(t)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const items = [];
    let node;
    while ((node = walker.nextNode())) {
      const id = "wt-" + items.length;
      node.parentElement.setAttribute("data-wt-id", id);
      node.parentElement.setAttribute("data-wt-orig", node.textContent);
      items.push({ id, text: node.textContent.trim(), node });
    }
    return items;
  }

  function applyText(items, segments) {
    const byId = Object.fromEntries(segments.map((s) => [s.id, s]));
    for (const it of items) {
      const tr = byId[it.id];
      if (!tr || tr.error) continue;
      it.node.textContent = it.node.textContent.replace(it.text, tr.text);
      it.node.parentElement.setAttribute("data-wt-done", "1");
    }
  }

  // 可见区优先: 先翻视口内, 滚动时增量
  function translateTextIncremental() {
    const items = collectTextNodes();
    if (!items.length) return;
    const io = new IntersectionObserver((entries, obs) => {
      const visible = entries.filter((e) => e.isIntersecting).map((e) => e.target);
      const batch = items.filter((it) => visible.includes(it.node.parentElement));
      if (batch.length) {
        obs && batch.forEach((b) => obs.unobserve(b.node.parentElement));
        sendText(batch, items);
      }
    });
    items.forEach((it) => io.observe(it.node.parentElement));
  }

  function sendText(batch, allItems) {
    chrome.runtime.sendMessage(
      { type: "translateText", payload: { segments: batch.map((b) => ({ id: b.id, text: b.text })) } },
      (resp) => {
        if (resp && resp.ok) applyText(allItems, resp.data.segments);
        else banner("翻译服务不可用, 已保留原文");
      }
    );
  }

  // ---- 漫画型: 采集 + 叠加 ----------------------------------------------
  function translateComic() {
    const imgs = Array.from(document.images).filter((i) => i.naturalHeight > 400);
    imgs.forEach((img) => {
      chrome.runtime.sendMessage(
        { type: "translateImage", payload: { imageUrl: img.currentSrc || img.src, referer: location.href } },
        (resp) => {
          if (resp && resp.ok) overlayLayers(img, resp.data.layers || []);
          else banner("漫画翻译失败, 已保留原图");
        }
      );
    });
  }

  function overlayLayers(img, layers) {
    const rect = img.getBoundingClientRect();
    const scaleX = rect.width / img.naturalWidth;
    const scaleY = rect.height / img.naturalHeight;
    const box = document.createElement("div");
    box.className = "wt-overlay";
    box.style.position = "absolute";
    box.style.left = `${rect.left + window.scrollX}px`;
    box.style.top = `${rect.top + window.scrollY}px`;
    box.style.width = `${rect.width}px`;
    box.style.height = `${rect.height}px`;
    for (const l of layers) {
      const [x, y, w, h] = l.bbox;
      const el = document.createElement("div");
      el.className = "wt-bubble";
      el.textContent = l.text;
      el.title = l.orig || "";
      Object.assign(el.style, {
        left: `${x * scaleX}px`,
        top: `${y * scaleY}px`,
        width: `${w * scaleX}px`,
        height: `${h * scaleY}px`,
        fontSize: `${Math.max(10, l.font_px * scaleY)}px`,
      });
      box.appendChild(el);
    }
    document.body.appendChild(box);
  }

  // ---- 顶部提示浮条 ------------------------------------------------------
  function banner(text) {
    let el = document.getElementById("wt-banner");
    if (!el) {
      el = document.createElement("div");
      el.id = "wt-banner";
      document.body.appendChild(el);
    }
    el.textContent = text;
    el.style.display = "block";
    setTimeout(() => (el.style.display = "none"), 4000);
  }

  // ---- 小说模式: 适配器 + 两种阅读模式 ----------------------------------
  function currentAdapter() {
    const regs = window.__WT_ADAPTERS || {};
    return regs.kindle || Object.values(regs)[0] || null;
  }

  function sendNovelPage(args) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: "translateNovelPage", payload: args }, (resp) => {
        resolve(resp && resp.ok ? resp.data : null);
      });
    });
  }

  // 侧栏显示当前页译文 (手动实时模式)
  function showPanel(text) {
    let panel = document.getElementById("wt-novel-panel");
    if (!panel) {
      panel = document.createElement("div");
      panel.id = "wt-novel-panel";
      document.body.appendChild(panel);
    }
    panel.textContent = text;
    panel.style.display = "block";
  }

  // 手动翻页 · 实时翻译: 翻译当前页, 并轮询页面指纹, 用户翻页后自动重译
  async function runNovelRealtime(opts) {
    const adapter = currentAdapter();
    if (!adapter) return banner("当前站点暂无小说适配器");
    window.__wtStop = false;
    let lastFp = null;
    let pageIndex = opts.startIndex || 0;

    async function translateCurrent() {
      if (window.__wtStop) return;
      const cap = adapter.capturePage();
      if (!cap || cap.kind === "empty" || cap.fingerprint === lastFp) return;
      lastFp = cap.fingerprint;
      showPanel("翻译中…");
      const data = await sendNovelPage({
        book: opts.book,
        index: pageIndex++,
        text: cap.text,
        imageDataUrl: cap.imageDataUrl,
        vertical: adapter.vertical,
        polish: opts.polish,
      });
      if (data) showPanel(data.polished || data.translated);
      else banner("翻译失败, 已保留原文");
    }

    await translateCurrent();
    clearInterval(window.__wtNovelPoll);
    window.__wtNovelPoll = setInterval(translateCurrent, 1200);
    banner("实时翻译已开启, 翻页即自动翻译");
  }

  // 自动翻页 · 保存翻译: 连续自动翻页, 逐页翻译落盘, 到书末停止
  async function runNovelAuto(opts) {
    const adapter = currentAdapter();
    if (!adapter) return banner("当前站点暂无小说适配器");
    window.__wtStop = false;
    banner("开始自动翻页保存…");
    let prevFp = null;
    const maxPages = opts.maxPages || 2000;
    let done = 0;

    for (let i = opts.startIndex || 0; i < maxPages; i++) {
      if (window.__wtStop) { banner("已手动停止"); break; }
      const cap = adapter.capturePage();
      if (!cap || cap.kind === "empty") { banner("无可读内容, 停止"); break; }
      if (cap.fingerprint === prevFp) { banner(`已到书末, 共保存 ${done} 页`); break; }
      prevFp = cap.fingerprint;
      const data = await sendNovelPage({
        book: opts.book,
        index: i,
        text: cap.text,
        imageDataUrl: cap.imageDataUrl,
        vertical: adapter.vertical,
        polish: opts.polish,
      });
      if (data) done += 1;
      else banner(`第 ${i} 页失败, 继续`);
      adapter.nextPage(opts.direction);
      await new Promise((r) => setTimeout(r, opts.interval || 1500));
    }
    banner(`自动翻页完成, 可在弹窗导出 (${done} 页)`);
  }

  // ---- 入口: 等待 popup 指令或自动运行 ----------------------------------
  function run(modeOverride) {
    const mode = modeOverride && modeOverride !== "auto" ? modeOverride : detectMode();
    if (mode === "comic") translateComic();
    else translateTextIncremental();
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg) return;
    if (msg.type === "wt-run") {
      if (msg.mode === "novel") {
        const opts = {
          book: msg.book,
          polish: msg.polish,
          direction: msg.direction,
          interval: msg.interval,
          maxPages: msg.maxPages,
          startIndex: msg.startIndex,
        };
        if (msg.reading === "auto") runNovelAuto(opts);
        else runNovelRealtime(opts);
      } else {
        run(msg.mode);
      }
    } else if (msg.type === "wt-stop") {
      window.__wtStop = true;
      clearInterval(window.__wtNovelPoll);
      banner("已停止");
    }
  });

  // 默认不自动翻译, 由用户在 popup 触发; 如需自动可取消下一行注释
  // run("auto");
})();
