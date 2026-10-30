// WebTranslator Kindle Cloud Reader 适配器。
// 仅在 read.amazon.* 生效, 向 window.__WT_ADAPTERS 注册 kindle 适配器。
// 负责: 定位正文文档(同源 iframe) -> 采集当前页(DOM文本/Canvas截图) -> 翻页 -> 内容指纹。

(function () {
  "use strict";
  if (!/(^|\.)read\.amazon\./.test(location.hostname)) return;

  // 递归收集所有可访问(同源)的 document (Kindle 正文在嵌套 iframe 内)
  function sameOriginDocs(root, acc) {
    acc = acc || [];
    root = root || document;
    acc.push(root);
    const frames = root.querySelectorAll("iframe, frame");
    for (const f of frames) {
      try {
        if (f.contentDocument) sameOriginDocs(f.contentDocument, acc);
      } catch (_) {
        /* 跨域 iframe 不可读, 跳过 */
      }
    }
    return acc;
  }

  // 在所有同源文档中选出最像正文的那个(文本最多 / 含大 canvas)
  function readerDoc() {
    let best = document;
    let bestScore = -1;
    for (const d of sameOriginDocs()) {
      if (!d.body) continue;
      const textLen = (d.body.innerText || "").replace(/\s+/g, "").length;
      const canvases = d.querySelectorAll("canvas").length;
      const score = textLen + canvases * 500;
      if (score > bestScore) {
        bestScore = score;
        best = d;
      }
    }
    return best;
  }

  function hash(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    return String(h);
  }

  function mergeCanvases(canvases) {
    // 简化: 取面积最大的一块。生产可按 bbox 坐标拼接整页。
    let big = canvases[0];
    for (const c of canvases) {
      if (c.width * c.height > big.width * big.height) big = c;
    }
    try {
      return big.toDataURL("image/png");
    } catch (_) {
      return ""; // 被 taint 的 canvas 无法导出
    }
  }

  // 采集当前页: 优先 DOM 文本; 否则截 canvas(竖排日文小说常见)
  function capturePage() {
    const doc = readerDoc();
    const text = ((doc.body && doc.body.innerText) || "").trim();
    if (text.replace(/\s+/g, "").length > 20) {
      return { kind: "text", text, fingerprint: hash(text) };
    }
    const canvases = Array.from(doc.querySelectorAll("canvas")).filter(
      (c) => c.width > 100 && c.height > 100
    );
    if (canvases.length) {
      const url = mergeCanvases(canvases);
      if (url) return { kind: "image", imageDataUrl: url, fingerprint: hash(url.slice(-2000)) };
    }
    return { kind: "empty", fingerprint: "" };
  }

  // 翻页: 竖排日文 RTL, 下一页通常是 ArrowLeft; 方向可配置
  function nextPage(direction) {
    const doc = readerDoc();
    const key = direction === "ltr" ? "ArrowRight" : "ArrowLeft";
    const opts = { key, code: key, keyCode: key === "ArrowLeft" ? 37 : 39, bubbles: true, cancelable: true };
    for (const target of [doc, doc.body, document]) {
      if (!target) continue;
      target.dispatchEvent(new KeyboardEvent("keydown", opts));
      target.dispatchEvent(new KeyboardEvent("keyup", opts));
    }
    // TODO: 部分版本需点击翻页热区; 可按 site-profile 补充点击实现
  }

  (window.__WT_ADAPTERS = window.__WT_ADAPTERS || {}).kindle = {
    name: "kindle",
    vertical: true,
    capturePage,
    nextPage,
    hash,
  };
})();
