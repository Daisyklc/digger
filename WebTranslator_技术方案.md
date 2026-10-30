# WebTranslator 网页翻译器技术方案设计文档

> 模块定位：面向**网页端电子书 / 在线漫画（Manga / Webtoon）**的沉浸式翻译器。
> 负责：页面内容识别（纯文本 DOM / 图片漫画） → 术语一致的上下文翻译 → **原位回填渲染**（文本替换 / 漫画气泡贴字）。
> 形态：**浏览器扩展（前端采集 + 回填渲染） + 翻译后端服务（OCR / LLM / 术语库）**。

---

## 1. 设计目标与约束

| 维度 | 要求 |
| --- | --- |
| 使用场景 | 网页端连载小说 / 电子书阅读器、在线漫画站（图片型分镜与气泡文字） |
| 翻译对象 | ① **文本型**：DOM 中的可见文本节点；② **漫画型**：图片中烧录的气泡文字 |
| 渲染方式 | **原位回填**：文本就地替换并保留排版；漫画擦除原文后在气泡内重排译文 |
| LLM | DeepSeek API（兼容 OpenAI SDK，模型 `deepseek-chat`），与现有模块保持一致 |
| 一致性 | 长篇连载的**人名 / 地名 / 专有名词**跨章节统一，由术语库（Glossary）强约束 |
| 性能 | 分段批量 + 可见区域优先 + 结果缓存，避免整页同步阻塞；首屏译文秒级可见 |
| 鲁棒性 | 网络 / OCR / LLM 全链路异常兜底，单段失败不拖垮整页；原文可一键还原 |
| 隐私 | 可配置「仅本地缓存」；不采集与翻译无关的页面数据 |
| 可观测性 | 后端 `logging` + 前端浮层进度；关键阶段有醒目提示 |

**非目标（本期不做）**：PDF/EPUB 离线文件解析、语音/视频字幕、付费墙绕过、整站镜像。

---

## 2. 总体架构

```
┌─────────────────────────── 浏览器扩展（Front-End） ───────────────────────────┐
│                                                                              │
│  popup UI ─ 配置：目标语言 / 模式(ebook|comic) / 引擎 / 术语库 / 隐私开关     │
│      │                                                                       │
│      ▼                                                                       │
│  content script                                                              │
│   ├─ ① 模式识别      ── 判定当前页是「文本型」还是「漫画型」                  │
│   ├─ ② 内容采集                                                              │
│   │     ├─ 文本型: TreeWalker 抽取可见文本节点 + 可见区优先                   │
│   │     └─ 漫画型: 采集分镜 <img>/<canvas> 位图 + 坐标                        │
│   ├─ ④ 原位回填                                                              │
│   │     ├─ 文本型: 替换文本节点, 保留父级样式; data-wt-orig 存原文            │
│   │     └─ 漫画型: 译文图叠加在原图之上(absolute overlay), 支持原/译切换      │
│   └─ 缓存键: hash(url + 段落文本 / 图片指纹)                                  │
│      │  (background service worker 统一代理请求 + 本地缓存 + 限流)           │
└──────┼───────────────────────────────────────────────────────────────────────┘
       │ HTTPS (JSON / multipart)
       ▼
┌─────────────────────────── 翻译后端服务（Back-End, FastAPI） ──────────────────┐
│                                                                              │
│  POST /translate/text    批量文本翻译(上下文 + 术语库)                        │
│  POST /translate/image   漫画: OCR → 翻译 → 擦字重排(typeset) → 返回译图/图层 │
│  GET  /glossary /POST    术语库读写(按作品 series 维度)                       │
│                                                                              │
│  ③ 核心流水线:                                                               │
│     TextTranslator ── 分段/去重/上下文窗口 → DeepSeek → 术语回填 → 缓存       │
│     MangaPipeline  ── OCR(气泡检测+识别) → 译文 → Inpaint擦字 → 竖排/自动字号  │
│     GlossaryStore  ── 专有名词抽取 + 人工校准 + 跨章节一致                     │
│     Cache          ── (url|图片指纹, 目标语) → 译文, 命中即返回               │
└──────────────────────────────────────────────────────────────────────────────┘
```

**为什么选「扩展 + 后端」而非纯反代网站**：在线漫画/阅读器大量依赖 JS 动态加载、懒加载与鉴权会话，纯服务端抓取易触发反爬与登录墙；扩展运行在用户真实会话内，能稳定拿到已渲染 DOM 与图片，回填也最自然。后端只做「重活」（OCR / LLM / 术语库），便于复用与缓存。

---

## 3. 目录与文件布局

```
WebTranslator/
├── WebTranslator_技术方案.md         # 本设计文档
├── requirements-web-translator.txt   # 后端依赖
├── backend/                          # 翻译后端服务 (Python / FastAPI)
│   ├── __init__.py
│   ├── config.py                     # 配置: Key / 模型 / 语言 / 路径 (环境变量优先)
│   ├── server.py                     # FastAPI 入口 + 路由
│   ├── schemas.py                    # 请求/响应 Pydantic 模型
│   ├── text_translator.py            # 文本型: 分段 + 上下文 + 术语 + DeepSeek
│   ├── manga_pipeline.py             # 漫画型: OCR → 翻译 → 擦字 → 重排
│   ├── ocr.py                        # 气泡检测 + 文字识别 (可插拔后端)
│   ├── typeset.py                    # 擦字(inpaint) + 译文重排渲染
│   ├── glossary.py                   # 术语库: 抽取 / 存储 / 强制一致
│   └── cache.py                      # 译文缓存 (本地 KV, 可换 Redis)
└── extension/                        # 浏览器扩展 (MV3)
    ├── manifest.json
    ├── background.js                 # service worker: 请求代理 + 缓存 + 限流
    ├── content.js                    # 采集 + 回填 + 模式识别
    ├── overlay.css                   # 浮层 / 漫画图层样式
    ├── popup.html
    └── popup.js                      # 设置面板逻辑
```

> 术语库默认落在 `backend` 同级的 `data/glossary/{series}.json`，路径可配置；连载作品以 `series` 作为命名空间，保证跨章一致。

---

## 4. 配置项设计

| 配置 | 来源（优先级从高到低） | 默认值 |
| --- | --- | --- |
| DeepSeek Key | 环境变量 `DEEPSEEK_API_KEY` | 无（缺失则 LLM 接口报错） |
| DeepSeek BaseURL | 常量 | `https://api.deepseek.com` |
| 模型名 | 环境变量 `WT_LLM_MODEL` → 常量 | `deepseek-chat` |
| 目标语言 | 请求参数 → 环境变量 `WT_TARGET_LANG` | `zh-CN` |
| 源语言 | 请求参数（`auto` 由 LLM 判定） | `auto` |
| OCR 后端 | 环境变量 `WT_OCR_BACKEND` | `manga-ocr`（可选 `paddle` / `vision-llm`） |
| 术语库目录 | 环境变量 `WT_GLOSSARY_DIR` | `./data/glossary` |
| 缓存目录 | 环境变量 `WT_CACHE_DIR` | `./data/cache` |
| 批量段数上限 | 常量 | 40 段/请求 |
| 并发/限流 | 常量 | 全局 QPS + 单 IP 令牌桶 |
| 是否润色 | 请求参数 → 环境变量 `WT_POLISH` | `true`（小说场景默认开） |
| 润色模型/API | 环境变量 `WT_POLISH_MODEL` → `WT_LLM_MODEL` | 同翻译模型（可指向更强模型/外部 API） |
| 小说会话目录 | 环境变量 `WT_NOVEL_DIR` | `./data/novel` |
| 跨页上下文长度 | 常量 | 保留上一页译文末尾 ≤500 字作上下文 |

启动时**前置校验**：缺 `DEEPSEEK_API_KEY` 时文本/漫画翻译接口返回明确 `503 + 配置缺失说明`（健康检查 `/healthz` 暴露就绪状态），而非启动即崩，便于扩展侧优雅降级。

---

## 5. 各模块详细设计

### 5.1 模式识别（content script）

**职责**：自动判定当前页属于「文本型」还是「漫画型」，也允许 popup 手动覆盖。

**启发式规则**：
- 统计正文区可见**文本字符密度**与**大图数量**：
  - 正文可见文字多、图片少/小 → **文本型（ebook）**。
  - 连续纵向大图（高 > 阈值、成列堆叠，典型 Webtoon/扫图）且文字稀疏 → **漫画型（comic）**。
- 命中已知站点适配（可选 site-profile 配置）直接采用对应模式。
- 兜底：默认文本型；用户可在 popup 一键切换并记忆该域名偏好。

### 5.2 内容采集（content script）

**文本型**：
- 用 `document.createTreeWalker(..., NodeFilter.SHOW_TEXT)` 遍历文本节点。
- 过滤：跳过 `script/style/code/pre` 下节点、纯空白、纯数字/符号、`contenteditable`、已标记 `data-wt-done`。
- **可见区优先**：用 `IntersectionObserver` 先翻视口内段落，滚动时增量翻译后续内容，首屏秒级可见。
- 将相邻短文本节点合并为「段」，为 LLM 提供完整句子，减少割裂。每段带稳定 `id`（节点路径指纹）。

**漫画型**：
- 收集分镜 `<img>`（含懒加载，监听 `load`）与必要的 `<canvas>`。
- 跨域图片经 `background` 代理拉取位图（绕过 content script 的 CORS 限制），携带页面 `referer/cookie` 以通过图床鉴权。
- 记录每张图在页面中的 `boundingClientRect`，用于叠加译文图层对齐。
- 图片指纹（感知哈希）作为缓存键，重复图片直接命中。

### 5.3 文本翻译 `text_translator.py`

**职责**：批量、上下文感知、术语一致的文本翻译。

**接口**：
```python
def translate_batch(
    segments: list[Segment],   # [{id, text}]
    target_lang: str,
    source_lang: str = "auto",
    series: str | None = None, # 术语库命名空间
    context: list[str] | None = None,  # 前文若干段, 维持语气/指代
) -> list[TranslatedSegment]:  # [{id, text, used_terms}]
```

**流程**：
1. **去重 + 缓存**：对每段算 `hash(text+target_lang+series)`，命中缓存直接复用。
2. **术语注入**：从 `GlossaryStore` 取该 `series` 命中术语，作为 system 约束（「以下专有名词必须按给定译法」）。
3. **分段打包**：以带编号的结构（JSON 数组）一次送 ≤40 段，要求模型**逐段对齐返回**，严禁合并/漏段。
4. **上下文窗口**：附带前 N 段原/译摘要，保证指代、人称、语气连续。
5. **调用 DeepSeek**：`temperature` 低（0.2），`response_format` 要求结构化；失败重试（指数退避）。
6. **术语回填校验**：对返回译文做术语一致性检查，发现违例用术语表强制替换。
7. **写缓存 + 返回**。

**System Prompt（要点）**：
> 你是专业的本地化译者。将给定段落翻译为目标语言，**保持原文的分段与顺序，逐段一一对应返回**，不得合并、增删或改变编号。保留原文的标点风格与语气；遇到提供的术语表，必须采用其指定译法。只返回译文，不要解释。

### 5.4 漫画流水线 `manga_pipeline.py` / `ocr.py` / `typeset.py`

**职责**：对单张漫画图完成「识别气泡文字 → 翻译 → 擦除原文 → 重排译文」。

**① OCR（`ocr.py`，可插拔）**：
- 后端可选：`manga-ocr`（日漫竖排强）、`PaddleOCR`（多语通用）、`vision-llm`（直接让多模态模型给出文本+框）。
- 统一输出：`[{bbox, text, direction(竖/横), confidence}]`。
- 气泡/文本块检测：优先用检测模型给出区域，再逐块识别；低置信块标记待人工。

**② 翻译**：复用 `text_translator.translate_batch`，把同一张图/同一话的所有气泡作为一个批次，共享上下文与术语，保证同页风格统一。

**③ 擦字 Inpaint（`typeset.py`）**：
- 在 `bbox` 区域做背景修复（inpainting）擦掉原文：纯色气泡用背景色填充；复杂背景用 OpenCV `inpaint` 或轻量扩散修复。
- 保留气泡边缘，避免糊到画面。

**④ 译文重排**：
- 根据 `bbox` 尺寸**自动选择字号**（二分拟合，使译文恰好填充气泡且不溢出）。
- 方向自适应：日文竖排气泡→译文可横排居中；长译文自动换行 / 缩字号 / 必要时省略加 tooltip。
- 字体可配置（中文建议思源黑体/宋体），描边提升可读性。
- 输出：**整张译图** 或 **透明文字图层 + 坐标**（扩展侧叠加，支持原/译一键切换、hover 看原文）。

**返回结构**：
```json
{
  "image_id": "phash:...",
  "mode": "overlay",
  "layers": [
    {"bbox": [x,y,w,h], "text": "译文", "font_px": 22, "orig": "原文"}
  ],
  "rendered_png": "<可选: base64 整张译图>"
}
```

### 5.5 术语库 `glossary.py`

**职责**：保证长篇连载的人名/地名/招式等专有名词**跨章节一致**。

**数据结构**（按 `series` 分文件）：
```json
{
  "series": "some-novel",
  "target_lang": "zh-CN",
  "terms": [
    {"src": "ユキ", "dst": "由纪", "type": "person", "locked": true, "note": "女主"}
  ]
}
```

**能力**：
- **抽取建议**：翻译过程中让 LLM 标注疑似专有名词，产出候选术语供人工确认（`locked` 后不再变动）。
- **强制一致**：翻译时注入已锁定术语；返回后做校验替换，杜绝同一名词多种译法。
- **读写接口**：`GET/POST /glossary?series=...`，popup 侧可编辑。

### 5.6 缓存 `cache.py`

- 键：`sha1(payload + target_lang + series + engine_version)`。
- 文本：段级缓存，复翻/回看命中率高。
- 漫画：图片感知哈希命中，整图译结果复用。
- 存储：默认本地文件 KV（`WT_CACHE_DIR`），接口预留以便切换 Redis。
- 失效：携带 `engine_version`，升级提示词/模型时自动绕过旧缓存。

### 5.7 原位回填（content script）

**文本型**：
- 替换文本节点的 `textContent` 为译文；原文存于最近元素的 `data-wt-orig`。
- 不改父级标签与样式，保留排版；超长译文不破坏布局（继承原节点换行规则）。
- 提供「显示原文 / 双语对照 / 仅译文」三态，一键切回。

**漫画型**：
- 在原图之上绝对定位叠加译文图层（或整张译图），`boundingClientRect` 实时对齐，监听 resize/scroll 重算。
- hover 气泡显示原文；顶部开关一键在原图/译图间切换。

---

## 6. 接口契约（后端 REST）

### 6.1 `POST /translate/text`
```jsonc
// 请求
{
  "target_lang": "zh-CN",
  "source_lang": "auto",
  "series": "some-novel",
  "context": ["前文段落..."],
  "segments": [{"id": "p1", "text": "原文..."}]
}
// 响应
{
  "segments": [{"id": "p1", "text": "译文...", "used_terms": ["由纪"]}],
  "cached": 3
}
```

### 6.2 `POST /translate/image`（multipart：图片 + json 元数据）
```jsonc
// 元数据
{ "target_lang": "zh-CN", "series": "some-manga", "mode": "overlay" }
// 响应: 见 5.4 返回结构
```

### 6.3 术语库
- `GET /glossary?series=some-novel` → 术语列表
- `POST /glossary` → 新增/更新/锁定术语

### 6.4 `GET /healthz`
- 返回 LLM/OCR 就绪状态与缺失配置项，供扩展优雅降级。

---

## 7. 异常与提示策略

| 场景 | 处理 |
| --- | --- |
| `DEEPSEEK_API_KEY` 缺失 | `/healthz` 标记未就绪；翻译接口 `503` + 配置说明，扩展弹提示 |
| LLM 限流 / 网络抖动 | 指数退避重试（4s/8s/16s/32s）；最终失败该批返回原文 + `error` 标记，不拖垮整页 |
| LLM 漏段 / 编号错位 | 后端校验段数与 id，缺失段**单独补翻**，仍失败则回退原文 |
| OCR 低置信 | 标记 `needs_review`，扩展侧可提示；不强行贴错字 |
| 跨域图片拉取失败 | `background` 代理重试；失败仅该图跳过并提示 |
| 译文溢出气泡 | 自动缩字号 / 换行 / 省略 + hover 原文 |
| 缓存损坏 | 跳过该键按未命中处理，记录 warning |
| 扩展与后端版本不符 | 响应带 `engine_version`，不匹配提示升级 |

**醒目提示**：后端关键错误用红色 ANSI（与现有模块风格一致）；扩展侧用顶部浮条提示「翻译服务不可用 / 已降级为原文」。

---

## 8. 依赖清单（`requirements-web-translator.txt`）

```
fastapi>=0.110
uvicorn>=0.29
pydantic>=2.6
requests>=2.31
openai>=1.30          # DeepSeek 兼容 SDK
pillow>=10.0          # 图片处理 / 重排渲染
opencv-python>=4.9    # 擦字 inpaint
numpy>=1.26
manga-ocr>=0.1        # 漫画 OCR (可选, 按 WT_OCR_BACKEND 启用)
```

- 系统级：环境变量 `DEEPSEEK_API_KEY`；漫画模式需中文字体文件（渲染译文）。
- 扩展侧无需打包依赖，纯 MV3（manifest/content/background/popup）。

---

## 9. 日志规范

- 后端用标准 `logging`，格式 `[%(asctime)s] %(levelname)s %(name)s: %(message)s`，级别 `INFO`；关键阶段 banner、错误红色、成功绿色（与 `TaskMiner` 一致）。
- 扩展侧关键事件打 `console.debug`（可开关），用户可见进度走顶部浮条。

---

## 10. 典型使用流程

**电子书 / 连载小说**：
1. 打开阅读页 → 扩展识别为文本型 → 可见段落即时翻译并原位替换。
2. 滚动时增量翻译后续段；人名等走 `series` 术语库保持一致。
3. 需要时一键「双语对照 / 还原原文」。

**在线漫画**：
1. 打开漫画页 → 扩展识别为漫画型 → 采集分镜图发后端。
2. 后端 OCR → 翻译 → 擦字重排 → 返回译图/图层 → 扩展叠加显示。
3. hover 看原文，一键在原图/译图切换；同话术语一致。

---

## 11. 里程碑拆解（按可交付切片，非时间估计）

| 切片 | 内容 | 依赖 |
| --- | --- | --- |
| M1 文本 MVP | 扩展采集回填 + `/translate/text` + 缓存 | DeepSeek Key |
| M2 术语一致 | Glossary 抽取/锁定/强制一致 + popup 编辑 | M1 |
| M3 漫画 MVP | 图片采集代理 + OCR + 翻译 + 简单贴字(overlay) | M1, OCR 后端 |
| M4 漫画精修 | Inpaint 擦字 + 自动字号/竖排 + 原/译切换 | M3, OpenCV/字体 |
| M5 体验 | 可见区优先 / 双语对照 / 站点适配 / 降级提示 | M1-M4 |
| M6 小说专项 | 两种阅读模式 + 润色两阶段 + Kindle 适配 + 导出 | M1-M2 |

---

## 12. 小说阅读专项设计（Kindle 日文小说）

> 面向「在 Kindle 网页版（Kindle Cloud Reader, `read.amazon.co.jp` / `read.amazon.com`）阅读日文小说」的强化场景。
> 相比通用文本型，额外解决：**竖排（縦書き）识别、跨页语气连贯、译文通顺润色、整本批量保存**。

### 12.1 两种阅读模式

| 维度 | 手动翻页 · 实时翻译（realtime） | 自动翻页 · 保存翻译（auto-save） |
| --- | --- | --- |
| 触发 | 用户自己翻页，扩展即时翻译当前页 | 扩展自动连续翻页，逐页翻译 |
| 延迟 | 低，边读边译，原位/对照显示 | 后台批量，不要求即时 |
| 翻页 | 用户操作 | 程序模拟翻页（方向可配置，日文竖排默认右→左） |
| 输出 | 当前页就地渲染 | 累积落盘，支持整本导出（txt / markdown / epub） |
| 润色 | 可开（略增延迟） | 默认开（追求成书质量） |
| 适用 | 边读边看、交互阅读 | 一次翻完整本，之后离线慢慢看 |

两模式共用同一套「采集 → 翻译 → 润色」内核，差异仅在**驱动方式（谁翻页）**与**输出去向（屏幕 vs 落盘）**。

### 12.2 Kindle 内容提取（DOM / Canvas 双路）

Kindle Cloud Reader 把正文渲染在**同源 iframe** 内，日文竖排小说常以 **canvas/位图** 渲染（防复制 + 竖排排版），也有部分书为可选中 DOM 文本。因此采用双路自适应：

- **访问正文文档**：扩展内容脚本以 `all_frames: true` 注入，或从顶层 `iframe.contentDocument`（同源可读）递归定位渲染正文的文档节点。
- **Path A · DOM 文本**：当正文为可选中文本时，用 `TreeWalker` 抽取，走文本型翻译（无需 OCR，质量最高）。
- **Path B · Canvas 截图**：当正文为 canvas/图片时，`canvas.toDataURL()` 截取当前页位图 → 送后端**整页 OCR（竖排）**。
- **路径自适应**：按当前页文本密度自动择路；站点适配（site-profile）内置 `read.amazon.*` 的容器选择器与翻页热区。

### 12.3 日文竖排整页 OCR（`read_page`）

- 与漫画「按气泡」不同，小说需**整页按正确阅读顺序**还原：竖排为「每列自上而下、列序自右向左」。
- 后端 `ocr.read_page(image, source_lang='ja', vertical=True)`：优先多模态 `vision-llm`（对竖排与阅读顺序最稳）或 PaddleOCR 竖排模式，输出整页纯文本。
- **ルビ（振假名/furigana）**：默认剥离注音只保留汉字正文；可选保留作为生僻词读音提示。

### 12.4 翻译 + 润色两阶段

小说追求**文学通顺**而非逐字直译，采用两阶段：

1. **忠实翻译（pass-1）**：`style="novel"` 文学直译，保留原意、人称、称谓、语气，术语走 `series` 术语库保证人名一致。
2. **通顺润色（pass-2）**：把 pass-1 译文整段送润色（同模型或**更强模型 / 外部 API**，由 `WT_POLISH_MODEL` 指定），要求在**不改变原意**前提下理顺语序、统一文风、去除翻译腔。
   - 整页为单位润色（而非逐句），以获得段落级连贯。
   - **跨页上下文**：携带上一页译文末尾 ≤500 字，保证人称/语气/指代跨页连续。
   - 可关闭（`polish=false`）以降延迟。

润色 System Prompt（要点）：
> 你是资深文学编辑。下面是一段由日文小说翻译而来的中文译文。请在**完全保留原意、人物称谓与情节**的前提下，使其更自然流畅、符合中文阅读习惯，消除翻译腔与生硬语序。不要增删情节，不要加入原文没有的信息，只输出润色后的正文。

### 12.5 自动翻页控制（auto-save 模式）

- **翻页动作**：向正文文档派发键盘事件（竖排日文「下一页」通常为 `ArrowLeft`）或点击翻页热区；方向可配置（RTL/LTR）。
- **到底检测**：翻页后对比页面内容指纹，连续未变化即判定到达书末；辅以进度条 100% 判定；设置最大页数安全上限防死循环。
- **节奏控制**：每次翻页后等待渲染完成 + 令牌桶限流，避免打爆后端与触发风控。
- **断点续传**：会话以 `book_id + page_index` 记录，已翻页面跳过，可随时中断/续跑。

### 12.6 保存与导出（NovelSession）

- 服务端 `NovelSession` 以 `book_id` 为命名空间，逐页存 `{index, orig, translated, polished}` 落盘（`WT_NOVEL_DIR`）。
- 导出 `GET /novel/export`：
  - `format`：`txt` / `md` / `epub`。
  - `layout`：`translated`（纯译文）/ `bilingual`（原文译文对照）。
- 已翻页面幂等去重；重复触发不重算（命中缓存/会话）。

### 12.7 接口补充

- `POST /translate/text`：新增 `polish: bool`、`style: "novel"|"general"`。
- `POST /novel/page`：翻译并存入会话一页（文本或整页图片均可），返回译文/润色文。
- `GET  /novel/session?book=`：返回进度（已翻页数 / 总页数估计 / 最近一页）。
- `GET  /novel/export?book=&format=md&layout=bilingual`：导出整本。

---

## 13. 后续可扩展点（非本期）

- 本地离线 EPUB/PDF 解析与整书导出双语版。
- OCR + 翻译结果的**人工校对面板**（众包/个人校订回流术语库）。
- 多引擎可选（DeepSeek / 其他 LLM / 传统 MT），按成本与质量路由。
- 气泡检测与擦字换用更强的漫画专用修复模型，提升复杂背景质量。
- 视口预取与译文 WebWorker 预渲染，进一步降低首屏延迟。
