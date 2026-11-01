# WebTranslator 私有模型部署方案（Mac Mini · 待部署）

> **状态：设想阶段，尚未执行。** 本文记录与用户讨论确定的私有模型部署思路，供**后续在 Mac 上实际部署时**按第 6 节 Runbook 执行。
> 关联：主方案见 `WebTranslator_技术方案.md`（内容路由与隐私见其第 14 节）。

---

## 1. 部署拓扑

- **Mac Mini**（Apple Silicon，16GB，常驻不关机）＝专职**私有 LLM 服务器**；偶尔也用来看视频等。
- **Windows PC**＝跑 WebTranslator 本体（受控浏览器 Playwright + OCR + FastAPI + 本地 Web UI + 留存）。
- **网络**：Windows 经局域网访问 Mac 的 OpenAI 兼容端点（Ollama）。
- **分工**：Mac 只出翻译 / 润色；OCR、浏览器、留存都在 Windows，职责干净。
- **隐私主线**：R18 / 敏感内容 → **只走 Mac 私有模型**，绝不发公有 DeepSeek；普通内容可走公有以省本地算力。

```
┌──────────── Windows PC (内容侧) ────────────┐        ┌──── Mac Mini 16G (模型侧, 常驻) ────┐
│ WebTranslator 本体                          │        │  Ollama (OpenAI 兼容)              │
│  受控浏览器(Playwright) · OCR · 留存 · WebUI │        │   :11434/v1                        │
│  Provider 路由:                             │        │   - Sakura-7B / Qwen2.5-7B (常驻)  │
│    public  → DeepSeek(公有, 普通内容)        │──LAN──▶│   - (可选) Sakura-14B (自动卸载)   │
│    private → Mac (R18/敏感, 不出本网)        │        │                                    │
└─────────────────────────────────────────────┘        └────────────────────────────────────┘
                                                        ↑ 偶尔还用来看视频 → 需留内存余量
```

---

## 2. 模型选型（16G，日文小说→中文，含 R18）

| 档位 | 模型 | 量化 | 常驻占用 | 看视频时 | 建议 |
| --- | --- | --- | --- | --- | --- |
| **日常主力** | **Sakura-7B (Qwen2.5)** 或 Qwen2.5-7B-Instruct | Q5_K_M / Q6_K | ~6-7G | ✅ 剩 5-6G，随便看 | **常驻 24/7，秒回** |
| 备选通用 | Qwen3-8B | Q5_K_M | ~6G | ✅ | 通用更新版 |
| 质量档 | Sakura-14B / Qwen2.5-14B | Q4_K_M | ~10-11G | ⚠️ 剩 2-3G 吃紧 | **不常驻**，自动卸载，挑不看视频时挂机整本 |
| 不选 | Qwen2.5-32B / Qwen3-30B | — | ~18G+ | ❌ | 16G 放不下 |

**要点**：
- **SakuraLLM**＝基于 Qwen 专门微调的日文轻小说 / galgame / 漫画→中文模型，**无审查**，最契合 R18 小说场景，是本场景首选。
- 专职服务器有余量，7-8B 直接上 **Q5_K_M / Q6_K**（比 Q4 质量更好，内存只多 1-2G，等于白捡）。
- 14B 质量更高但要避免与视频抢内存，用"空闲自动卸载"。

---

## 3. 内存与常驻策略（因偶尔看视频）

- 16G ≈ 系统 3-4G ＋ 模型 ＋ 视频余量；专职服务器可给模型 ~11-12G，但看视频要留几 G。
- **7-8B**：可 `OLLAMA_KEEP_ALIVE=-1` 永久常驻，看视频也从容。
- **14B**：**不要**常驻（用默认空闲卸载）——翻译时加载、看视频时已卸载，内存还给视频。
- 唯一冲突：边看视频边挂机翻整本 → 那种时候退回 7B。整本翻译是后台挂机、对速度不敏感，14B 慢一点可接受。

---

## 4. Mac 侧 Ollama 配置（部署时执行）

安装（任一）：官网安装包，或 `brew install ollama`。

对局域网开放 + 常驻策略（关键两条）：
```bash
# 1) 对局域网开放 (默认只听 localhost, Windows 连不上)
launchctl setenv OLLAMA_HOST "0.0.0.0:11434"
# 2) 常驻: 7B 想秒回 -> 永久常驻; 14B 则不设 (用默认自动卸载)
launchctl setenv OLLAMA_KEEP_ALIVE "-1"
# 设完重启 Ollama 服务使其生效
```

拉取通用模型：
```bash
ollama pull qwen2.5:7b          # 通用主力
# 或更高质量: ollama pull qwen2.5:14b
```

导入 Sakura（官方库没有，用 GGUF + Modelfile 导入）：
```dockerfile
# 文件名: Modelfile.sakura  (GGUF 自 HuggingFace / ModelScope 下载)
FROM ./sakura-7b-qwen2.5-v1.0-q6_k.gguf
PARAMETER temperature 0.1
PARAMETER num_ctx 4096
# Sakura 官方推荐的 JP->ZH 翻译提示模板可写入 TEMPLATE / SYSTEM
```
```bash
ollama create sakura-7b -f Modelfile.sakura
```

网络与验证：
```bash
# 防火墙放行 11434; 建议给 Mac 固定内网 IP
ollama list
curl http://localhost:11434/v1/models            # 本机验证
# Windows 侧: curl http://<macmini-ip>:11434/v1/models  (局域网验证)
```

---

## 5. WebTranslator 对接（Windows 侧）

```yaml
providers:
  public:
    base_url: https://api.deepseek.com
    model: deepseek-chat
    api_key_env: DEEPSEEK_API_KEY
  private:                                   # 指向 Mac
    base_url: http://<macmini-ip>:11434/v1
    model: sakura-7b                          # 或 qwen2.5:7b
    api_key_env: WT_PRIVATE_KEY               # 本地可空

routing:
  default: public
  series:
    some-r18-title: { route: private, sensitive: true }   # 整本锁私有
  local_prescreen:
    enabled: true
    lexicon: ./data/sensitive_lexicon.txt
```

- 敏感命中 → **OCR 与翻译/润色全链路走 private**（OCR 本就在 Windows 本地）。
- 缓存键带 provider，公有/私有译文不混。

---

## 6. 部署 Runbook（后续实际执行用 · 勾选清单）

**Mac 侧**
1. [ ] 安装 Ollama。
2. [ ] `launchctl setenv OLLAMA_HOST "0.0.0.0:11434"`；7B 加 `OLLAMA_KEEP_ALIVE "-1"`；重启 Ollama。
3. [ ] `ollama pull qwen2.5:7b`；（可选）下载 Sakura GGUF → `ollama create sakura-7b -f Modelfile.sakura`。
4. [ ] 给 Mac 固定内网 IP；防火墙放行 11434。
5. [ ] 本机验证 `curl localhost:11434/v1/models`。

**Windows 侧**
6. [ ] 局域网验证 `curl http://<mac-ip>:11434/v1/models` 通。
7. [ ] 配置 private provider（base_url=mac、model）。
8. [ ] 配 R18 路由（series `sensitive:true` + 本地敏感词表）。
9. [ ] 冒烟：一段日文走 private 翻译成功；标记为敏感的页确认**只到 mac、不发 DeepSeek**（看日志的 provider 字段，必要时抓包确认）。
10. [ ] 回归：普通内容仍走公有 DeepSeek。

---

## 7. 隐私核查（部署验收项）

- 译文留存 / 日志里记录每页所用 `provider`，一眼可查敏感页是否走了私有。
- 必要时在 Windows 侧抓包确认：敏感页的请求目的地只有 `<mac-ip>:11434`，无任何到 `api.deepseek.com` 的外发。

---

## 8. 部署时需用户提供

- Mac Mini 的局域网 IP（建议固定）。
- 最终模型选择与 Ollama 标签名（如 `sakura-7b` / `qwen2.5:7b`）。
- 若用 Sakura：具体 GGUF 版本来源（HuggingFace / ModelScope 链接）与量化档（建议 Q6_K）。
- DeepSeek Key（公有普通内容用）。
