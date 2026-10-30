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

  // ---- 入口: 等待 popup 指令或自动运行 ----------------------------------
  function run(modeOverride) {
    const mode = modeOverride && modeOverride !== "auto" ? modeOverride : detectMode();
    if (mode === "comic") translateComic();
    else translateTextIncremental();
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "wt-run") run(msg.mode);
  });

  // 默认不自动翻译, 由用户在 popup 触发; 如需自动可取消下一行注释
  // run("auto");
})();
