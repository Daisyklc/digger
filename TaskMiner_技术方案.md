# TaskMiner 技术方案设计文档

> 模块定位：Kaggle 竞赛自动化 Agent 的「数据准备 / 赛题挖掘」模块。
> 负责：环境初始化 → 网页抓取（绕登录） → 数据集下载解压 → 调用 DeepSeek 生成客观赛题总结 `analysis.md`。

---

## 1. 设计目标与约束

| 维度 | 要求 |
| --- | --- |
| 运行平台 | macOS（路径以 `/Volumes/...` 形式挂载外接硬盘） |
| 存储约束 | **所有大体积数据强制落在外接硬盘**，根路径可配置，默认 `/Volumes/MySSD` |
| LLM | DeepSeek API，兼容 OpenAI SDK，模型 `deepseek-chat` |
| 鲁棒性 | 网络请求 / 子进程执行必须完整异常处理 |
| 可观测性 | 关键步骤有 `logging` + 醒目控制台输出，终端可见进度 |
| 客观性 | LLM 总结**只做客观陈述**，禁止算法建议 / 论文推荐 / 主观解题思路 |

---

## 2. 总体架构

```
┌──────────────────────────────────────────────────────────────┐
│                          TaskMiner                            │
│                                                                │
│  __init__(url, base_drive=...)                                 │
│        │                                                       │
│        ▼                                                       │
│  ① setup_workspace(url)        ── 解析简称 + 建目录             │
│        │                                                       │
│        ▼                                                       │
│  ② fetch_web_content(url)      ── Cookie 绕登录 + 抓 3 页面     │
│        │            └─(403/401)─▶ raise LoginRequiredError      │
│        ▼                                                       │
│  ③ download_dataset(name,path) ── kaggle CLI + 解压清理        │
│        │                                                       │
│        ▼                                                       │
│  ④ generate_analysis_md(text)  ── DeepSeek 严格模板 → md       │
│        │                                                       │
│        ▼                                                       │
│  run()  ── 编排全流程的入口                                    │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. 目录与文件布局

外接硬盘根路径（可配置，默认 `/Volumes/MySSD`）下：

```
/Volumes/MySSD/
└── Kaggle_Agent/
    ├── config/
    │   └── cookies.txt              # 人工维护的登录 Cookie（Netscape 或 raw 格式）
    └── {competition_name}/          # 例如 titanic/
        ├── data/                    # 数据集解压后的落地目录
        │   ├── train.csv
        │   └── ...
        └── analysis.md              # LLM 生成的客观赛题总结
```

> 说明：`config/cookies.txt` 放在 `Kaggle_Agent/config/` 下，全局共享，避免每个比赛重复配置。

---

## 4. 配置项设计

| 配置 | 来源（优先级从高到低） | 默认值 |
| --- | --- | --- |
| 外接硬盘根 `base_drive` | 构造参数 → 环境变量 `KAGGLE_AGENT_DRIVE` | `/Volumes/MySSD` |
| Agent 根目录 | 派生 `{base_drive}/Kaggle_Agent` | — |
| Cookie 文件 | `{agent_root}/config/cookies.txt` | — |
| DeepSeek Key | 环境变量 `DEEPSEEK_API_KEY` | 无（缺失则报错） |
| DeepSeek BaseURL | 常量 | `https://api.deepseek.com` |
| 模型名 | 常量 | `deepseek-chat` |

启动时**前置校验**：外接硬盘是否已挂载（`os.path.ismount` / 路径存在性），未挂载直接报错，避免误写到本地盘。

---

## 5. 各方法详细设计

### 5.1 `setup_workspace(url) -> str`

**职责**：解析比赛简称、创建工作区目录结构。

**URL 解析逻辑**：
- 用 `urllib.parse.urlparse` 拆出 path。
- 兼容多种形式：
  - `https://www.kaggle.com/competitions/titanic`
  - `https://www.kaggle.com/c/titanic/overview`
  - 结尾带 `/`、带 query string。
- 规则：找到 `competitions` 或 `c` 段后的下一段作为 `competition_name`；兜底取最后一个非空 path 段。
- 校验非空，否则抛 `ValueError`。

**目录创建**：
- `project_root = {agent_root}/{competition_name}`
- `data_dir = project_root/data`
- `os.makedirs(..., exist_ok=True)`
- 返回 `competition_name`，并把关键路径存到实例属性（`self.project_root`、`self.data_dir`）。

**日志**：打印解析出的简称 + 创建的路径。

---

### 5.2 `fetch_web_content(url) -> str`

**职责**：用 Cookie 绕过登录，抓取 Overview / Evaluation / Data 三个页面文本。

**Cookie 装配**：
- 读取 `{agent_root}/config/cookies.txt`。
- 支持两种格式：
  - **raw 形式**：`key1=value1; key2=value2`（整行直接当 `Cookie` 头）。
  - **Netscape cookies.txt 形式**：按 tab 分列解析（兼容浏览器插件导出）。
- 组装到 `requests` 的 headers：`Cookie` + 真实 `User-Agent`。

**抓取目标页**：基于比赛 URL 拼出：
- `.../competitions/{name}/overview`
- `.../competitions/{name}/overview/evaluation`（评价指标常在此）
- `.../competitions/{name}/data`

**请求处理**：
- `requests.get(timeout=...)`，包 `try/except requests.RequestException`。
- **状态码判定**：
  - `403` / `401` → 抛自定义 `LoginRequiredError`，并在控制台打印**红色醒目提示**（ANSI 转义 `\033[91m...\033[0m` + 边框），要求人工更新 `cookies.txt`。
  - 其他非 200 → 记录 warning，跳过该页（不致命）。
- **HTML 提取纯文本**：用 `BeautifulSoup`（`bs4`）剥离 `script/style`，取可见文本并压缩空白。
- 合并三页文本，加分隔标题，返回拼接结果。

**自定义异常**：

```python
class LoginRequiredError(Exception):
    """Cookie 失效 / 需要人工重新登录。"""
```

---

### 5.3 `download_dataset(competition_name, save_path) -> None`

**职责**：调用 Kaggle CLI 下载并解压。

**执行命令**：
```
kaggle competitions download -c {competition_name} -p {save_path}
```
- 用 `subprocess.run(..., capture_output=True, text=True, check=False)`。
- 显式检查 `returncode`，非 0 抛 `RuntimeError`，并把 stderr 打到日志（常见错误：未配置 `~/.kaggle/kaggle.json`、未接受比赛规则）。
- 包 `FileNotFoundError`（kaggle 命令不存在 → 提示 `pip install kaggle`）。

**解压逻辑**：
- 扫描 `save_path` 下所有 `.zip`。
- 逐个 `zipfile.ZipFile(...).extractall(data_dir)`。
- 解压成功后 `os.remove(zip)` 删除原压缩包。
- 注意：`-p save_path` 即 `data/` 目录，解压目标也是 `data/`（题目要求「就地解压到 data/」）。

**日志**：下载开始、CLI stdout、解压进度、清理结果。

---

### 5.4 `generate_analysis_md(scraped_text, save_path) -> str`

**职责**：调用 DeepSeek，按严格模板生成 `analysis.md`。

**Client**：
```python
from openai import OpenAI
client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"],
                base_url="https://api.deepseek.com")
```

**System Prompt（强约束）**：
> 你是一个客观的数据科学助手。请仅根据提供的网页文本，客观总结赛题内容。
> **绝对不要**提供任何算法建议、模型选型、论文推荐或主观解题思路。
> 只做客观陈述。必须严格按照用户给定的 Markdown 模板输出，不要增删章节，不要包裹额外的代码块标记。
> 路径字段必须使用我提供的真实本地路径。

**User Prompt**：包含
1. 抓取到的网页纯文本（截断到安全长度，防超 token）。
2. 真实的本地路径（`project_root`、`data_dir`），让模型填进「3. 本地资源路径索引」。
3. 待填充的 Markdown 模板（与题目一致）。

**输出模板**（强制）：
```markdown
# 🏆 赛题名称: [比赛名称]
## 1. 核心目标
- **任务类型**: [...]
- **业务背景**: [...]
- **评价指标 (Metric)**: [...]
## 2. 数据集概述
- **数据形式**: [...]
- **核心特征描述**: [...]
- **目标变量 (Target)**: [...]
## 3. 本地资源路径索引
- **数据集根目录**: `/Volumes/MySSD/Kaggle_Agent/[比赛名]/data/`
- **项目工作区**: `/Volumes/MySSD/Kaggle_Agent/[比赛名]/`
```

**调用与落盘**：
- `temperature=0`（要客观稳定）。
- 包 `try/except`，捕获 SDK 异常（鉴权失败 / 限流 / 网络）。
- 把返回内容写入 `{project_root}/analysis.md`（UTF-8）。
- 兜底：若 LLM 调用失败，仍写一份「仅含真实路径索引 + 错误说明」的降级版 md，保证管线不中断。

---

### 5.5 `run() -> None`（编排入口）

顺序执行 ①→②→③→④，集中捕获 `LoginRequiredError` 等异常并给出可读提示。每步打印阶段分隔日志。

---

## 6. 异常与提示策略

| 场景 | 处理 |
| --- | --- |
| 外接硬盘未挂载 | 启动即抛 `RuntimeError`，提示检查 SSD |
| URL 无法解析出简称 | `ValueError` |
| Cookie 文件缺失 | warning，继续尝试（可能仍被 403） |
| HTTP 403/401 | `LoginRequiredError` + 红色醒目框，提示更新 Cookie |
| 其他网络异常 | `requests.RequestException` 捕获，记录日志 |
| kaggle 命令不存在 | `FileNotFoundError` → 提示 `pip install kaggle` |
| kaggle 下载失败 | `RuntimeError` + stderr |
| zip 损坏 | `zipfile.BadZipFile` 捕获，跳过该文件 |
| DeepSeek Key 缺失 | 启动校验报错 |
| DeepSeek 调用失败 | 捕获 + 降级 md |

**红色醒目提示样例**：

```
╔══════════════════════════════════════════════════════════╗
║  ⚠️  LOGIN REQUIRED — Kaggle 返回 403/401                  ║
║  Cookie 已失效，请手动更新：                                ║
║  /Volumes/MySSD/Kaggle_Agent/config/cookies.txt           ║
╚══════════════════════════════════════════════════════════╝
```
（用 ANSI `\033[91m` 红色高亮输出。）

---

## 7. 依赖清单（`requirements.txt`）

```
requests>=2.31
beautifulsoup4>=4.12
openai>=1.30
kaggle>=1.6
```

- 系统级：已挂载的外接硬盘、已配置的 `~/.kaggle/kaggle.json`、环境变量 `DEEPSEEK_API_KEY`。

---

## 8. 日志规范

- 使用标准 `logging`，格式 `[%(asctime)s] %(levelname)s %(name)s: %(message)s`，级别 `INFO`。
- 阶段性 banner 用自定义彩色输出，错误用红色，成功用绿色。

---

## 9. 使用示例

```python
miner = TaskMiner(
    url="https://www.kaggle.com/competitions/titanic",
    base_drive="/Volumes/MySSD",   # 可配置
)
miner.run()
# 产物：/Volumes/MySSD/Kaggle_Agent/titanic/analysis.md
```

---

## 10. 后续可扩展点（非本期）

- 抓取改用官方 Kaggle API（`competitions_view`）替代网页爬取，降低封禁风险。
- `analysis.md` 增加数据文件清单自动扫描（读 `data/` 真实文件列表）。
- 支持 dataset（非 competition）类型 URL。
