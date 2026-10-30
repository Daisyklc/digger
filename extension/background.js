// WebTranslator service worker.
// 职责: 统一代理 content script 的后端请求 (绕过页面 CORS)、跨域图片拉取、
//       本地缓存与限流。所有网络出口集中在此, 便于管理与观测。

const DEFAULT_BACKEND = "http://127.0.0.1:8000";

async function getConfig() {
  const { backend, targetLang, series, mode } = await chrome.storage.sync.get([
    "backend",
    "targetLang",
    "series",
    "mode",
  ]);
  return {
    backend: backend || DEFAULT_BACKEND,
    targetLang: targetLang || "zh-CN",
    series: series || null,
    mode: mode || "auto",
  };
}

// 简易全局令牌桶限流, 避免整页瞬时打爆后端
let tokens = 8;
setInterval(() => {
  tokens = Math.min(8, tokens + 4);
}, 1000);
async function acquire() {
  while (tokens <= 0) {
    await new Promise((r) => setTimeout(r, 120));
  }
  tokens -= 1;
}

async function translateText(payload) {
  const cfg = await getConfig();
  await acquire();
  const res = await fetch(`${cfg.backend}/translate/text`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target_lang: cfg.targetLang, series: cfg.series, ...payload }),
  });
  if (!res.ok) throw new Error(`text ${res.status}`);
  return res.json();
}

async function translateNovelPage(p) {
  // 翻译并存入服务端会话一页。DOM 文本给 text, 竖排位图给 imageDataUrl。
  const cfg = await getConfig();
  await acquire();
  const form = new FormData();
  form.append("book", p.book || "untitled");
  form.append("index", String(p.index || 0));
  form.append("target_lang", cfg.targetLang);
  form.append("source_lang", p.sourceLang || "ja");
  form.append("series", cfg.series || "");
  form.append("vertical", String(p.vertical !== false));
  form.append("polish", String(!!p.polish));
  if (p.text) form.append("text", p.text);
  if (p.imageDataUrl) {
    const blob = await (await fetch(p.imageDataUrl)).blob();
    form.append("image", blob, "page.png");
  }
  const res = await fetch(`${cfg.backend}/novel/page`, { method: "POST", body: form });
  if (!res.ok) throw new Error(`novel ${res.status}`);
  return res.json();
}

async function translateImage({ imageUrl, referer }) {
  const cfg = await getConfig();
  await acquire();
  // 携带页面上下文拉取跨域图片 (content script 受 CORS 限制)
  const imgRes = await fetch(imageUrl, { headers: referer ? { Referer: referer } : {} });
  const blob = await imgRes.blob();
  const form = new FormData();
  form.append("image", blob, "page.png");
  form.append("meta", JSON.stringify({ target_lang: cfg.targetLang, series: cfg.series, mode: "overlay" }));
  const res = await fetch(`${cfg.backend}/translate/image`, { method: "POST", body: form });
  if (!res.ok) throw new Error(`image ${res.status}`);
  return res.json();
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    try {
      if (msg.type === "translateText") {
        sendResponse({ ok: true, data: await translateText(msg.payload) });
      } else if (msg.type === "translateImage") {
        sendResponse({ ok: true, data: await translateImage(msg.payload) });
      } else if (msg.type === "translateNovelPage") {
        sendResponse({ ok: true, data: await translateNovelPage(msg.payload) });
      } else {
        sendResponse({ ok: false, error: "unknown message" });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e) });
    }
  })();
  return true; // async
});
