"""TaskMiner: Kaggle 竞赛自动化 Agent 的数据准备 / 赛题挖掘模块。

管线: 环境初始化 -> 网页抓取(Cookie 绕登录) -> 数据集下载解压 -> DeepSeek 生成客观赛题总结。

环境依赖:
    - 已挂载的外接硬盘 (默认 /Volumes/MySSD, 可配置)
    - 已配置 ~/.kaggle/kaggle.json (kaggle CLI)
    - 环境变量 DEEPSEEK_API_KEY

用法:
    miner = TaskMiner("https://www.kaggle.com/competitions/titanic", base_drive="/Volumes/MySSD")
    miner.run()
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import zipfile
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - 仅在缺依赖时触发
    OpenAI = None  # type: ignore


# --------------------------------------------------------------------------- #
# 日志与彩色输出
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("TaskMiner")


class _C:
    """ANSI 颜色常量。"""

    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def _banner(text: str, color: str = _C.CYAN) -> None:
    """打印阶段性分隔 banner。"""
    line = "=" * 64
    print(f"{color}{_C.BOLD}{line}\n  {text}\n{line}{_C.RESET}")


def _red_alert(lines: list[str]) -> None:
    """打印极其醒目的红色告警框。"""
    width = max(len(l) for l in lines) + 4
    top = "╔" + "═" * width + "╗"
    bottom = "╚" + "═" * width + "╝"
    print(f"\n{_C.RED}{_C.BOLD}{top}")
    for l in lines:
        print(f"║  {l.ljust(width - 4)}  ║")
    print(f"{bottom}{_C.RESET}\n")


# --------------------------------------------------------------------------- #
# 自定义异常
# --------------------------------------------------------------------------- #
class LoginRequiredError(Exception):
    """Kaggle 返回 403/401, Cookie 失效, 需要人工重新登录。"""


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
DEFAULT_DRIVE = "/Volumes/MySSD"
AGENT_DIR_NAME = "Kaggle_Agent"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"
REQUEST_TIMEOUT = 30
MAX_LLM_INPUT_CHARS = 16000  # 防止超 token, 对抓取文本做截断

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

ANALYSIS_TEMPLATE = """# 🏆 赛题名称: [比赛名称]
## 1. 核心目标
- **任务类型**: [二分类 / 多分类 / 回归 / 图像分割 / 文本生成等]
- **业务背景**: [一句话总结]
- **评价指标 (Metric)**: [准确提取官方规定的 Metric，如 AUC, RMSE 等]
## 2. 数据集概述
- **数据形式**: [表格数据 / 图像 / 文本]
- **核心特征描述**: [简述数据集中包含的核心字段含义]
- **目标变量 (Target)**: [需要预测的列名]
## 3. 本地资源路径索引
- **数据集根目录**: `{data_dir}`
- **项目工作区**: `{project_root}`
"""


# --------------------------------------------------------------------------- #
# TaskMiner
# --------------------------------------------------------------------------- #
class TaskMiner:
    """Kaggle 赛题挖掘器。"""

    def __init__(self, url: str, base_drive: Optional[str] = None) -> None:
        self.url = url.strip()
        # 配置优先级: 构造参数 > 环境变量 > 默认值
        self.base_drive = (
            base_drive
            or os.environ.get("KAGGLE_AGENT_DRIVE")
            or DEFAULT_DRIVE
        )
        self.agent_root = os.path.join(self.base_drive, AGENT_DIR_NAME)
        self.config_dir = os.path.join(self.agent_root, "config")
        self.cookie_file = os.path.join(self.config_dir, "cookies.txt")

        # 由 setup_workspace 填充
        self.competition_name: Optional[str] = None
        self.project_root: Optional[str] = None
        self.data_dir: Optional[str] = None

        self._verify_drive_mounted()

    # ------------------------------------------------------------------ #
    # 前置校验
    # ------------------------------------------------------------------ #
    def _verify_drive_mounted(self) -> None:
        """确保外接硬盘已挂载, 避免误写到本地盘。"""
        if not os.path.isdir(self.base_drive):
            raise RuntimeError(
                f"外接硬盘根路径不存在或未挂载: {self.base_drive} "
                f"(可通过构造参数 base_drive 或环境变量 KAGGLE_AGENT_DRIVE 配置)"
            )
        logger.info("外接硬盘已就绪: %s", self.base_drive)

    # ------------------------------------------------------------------ #
    # ① 环境初始化
    # ------------------------------------------------------------------ #
    def setup_workspace(self, url: str) -> str:
        """从 URL 解析比赛简称, 并创建工作区目录结构。返回比赛简称。"""
        _banner("① setup_workspace — 解析 URL & 创建工作区")
        name = self._parse_competition_name(url)
        self.competition_name = name
        self.project_root = os.path.join(self.agent_root, name)
        self.data_dir = os.path.join(self.project_root, "data")

        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.data_dir, exist_ok=True)

        logger.info("比赛简称: %s%s%s", _C.GREEN, name, _C.RESET)
        logger.info("项目工作区: %s", self.project_root)
        logger.info("数据集目录: %s", self.data_dir)
        return name

    @staticmethod
    def _parse_competition_name(url: str) -> str:
        """兼容多种 Kaggle URL 形式, 提取比赛简称。

        支持:
            https://www.kaggle.com/competitions/titanic
            https://www.kaggle.com/competitions/titanic/overview
            https://www.kaggle.com/c/titanic/data
        """
        path = urlparse(url).path
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise ValueError(f"无法从 URL 解析出比赛简称: {url!r}")

        for marker in ("competitions", "c"):
            if marker in parts:
                idx = parts.index(marker)
                if idx + 1 < len(parts):
                    return parts[idx + 1]
        # 兜底: 取最后一段
        return parts[-1]

    # ------------------------------------------------------------------ #
    # ② 网页抓取 (Cookie 绕登录)
    # ------------------------------------------------------------------ #
    def fetch_web_content(self, url: str) -> str:
        """抓取 overview / evaluation / data 三个页面的纯文本。"""
        _banner("② fetch_web_content — Cookie 绕登录 & 抓取页面")
        if not self.competition_name:
            self.setup_workspace(url)

        headers = {"User-Agent": USER_AGENT}
        cookie_header = self._load_cookie_header()
        if cookie_header:
            headers["Cookie"] = cookie_header
            logger.info("已装配 Cookie (长度 %d)", len(cookie_header))
        else:
            logger.warning("未找到有效 Cookie, 可能触发登录拦截")

        base = f"https://www.kaggle.com/competitions/{self.competition_name}"
        targets = {
            "Overview": f"{base}/overview",
            "Evaluation": f"{base}/overview/evaluation",
            "Data": f"{base}/data",
        }

        collected: list[str] = []
        for label, page_url in targets.items():
            text = self._fetch_single_page(label, page_url, headers)
            if text:
                collected.append(f"===== [{label}] =====\n{text}")

        if not collected:
            logger.warning("三个页面均未抓取到内容")
        combined = "\n\n".join(collected)
        logger.info("抓取完成, 合计文本长度: %d", len(combined))
        return combined

    def _load_cookie_header(self) -> str:
        """读取 cookies.txt, 兼容 raw 与 Netscape 两种格式, 返回 Cookie 头字符串。"""
        if not os.path.isfile(self.cookie_file):
            logger.warning("Cookie 文件不存在: %s", self.cookie_file)
            return ""
        try:
            with open(self.cookie_file, "r", encoding="utf-8") as f:
                raw = f.read().strip()
        except OSError as exc:
            logger.warning("读取 Cookie 文件失败: %s", exc)
            return ""

        if not raw:
            return ""

        # raw 形式: 直接是 "k1=v1; k2=v2"
        if ";" in raw and "\t" not in raw and "=" in raw.split("\n")[0]:
            return " ".join(line.strip() for line in raw.splitlines() if line.strip())

        # Netscape cookies.txt 形式: 按 tab 分 7 列, 取 name(第6列) 与 value(第7列)
        pairs: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) >= 7:
                pairs.append(f"{cols[5]}={cols[6]}")
        if pairs:
            return "; ".join(pairs)

        # 兜底: 当作单行 raw
        return raw.replace("\n", " ").strip()

    def _fetch_single_page(self, label: str, page_url: str, headers: dict) -> str:
        """抓取单页并返回纯文本。403/401 抛 LoginRequiredError。"""
        logger.info("抓取 [%s]: %s", label, page_url)
        try:
            resp = requests.get(page_url, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logger.error("请求 [%s] 网络异常: %s", label, exc)
            return ""

        if resp.status_code in (401, 403):
            _red_alert(
                [
                    f"LOGIN REQUIRED — Kaggle 返回 {resp.status_code}",
                    "Cookie 已失效, 请手动更新文件:",
                    self.cookie_file,
                ]
            )
            raise LoginRequiredError(
                f"页面 {page_url} 返回 {resp.status_code}, 需要更新 Cookie"
            )

        if resp.status_code != 200:
            logger.warning("[%s] 返回非 200 状态码: %s, 跳过", label, resp.status_code)
            return ""

        return self._html_to_text(resp.text)

    @staticmethod
    def _html_to_text(html: str) -> str:
        """剥离 script/style, 提取可见纯文本并压缩空白。"""
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        lines = [ln.strip() for ln in text.splitlines()]
        return "\n".join(ln for ln in lines if ln)

    # ------------------------------------------------------------------ #
    # ③ 数据集下载
    # ------------------------------------------------------------------ #
    def download_dataset(self, competition_name: str, save_path: str) -> None:
        """调用 kaggle CLI 下载数据集, 并就地解压 .zip 到 data/。"""
        _banner("③ download_dataset — kaggle CLI 下载 & 解压")
        os.makedirs(save_path, exist_ok=True)
        cmd = [
            "kaggle", "competitions", "download",
            "-c", competition_name,
            "-p", save_path,
        ]
        logger.info("执行命令: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "未找到 kaggle 命令, 请先安装: pip install kaggle 并配置 ~/.kaggle/kaggle.json"
            )
        except OSError as exc:
            raise RuntimeError(f"执行 kaggle 命令失败: {exc}") from exc

        if result.stdout:
            logger.info("kaggle stdout: %s", result.stdout.strip())
        if result.returncode != 0:
            logger.error("kaggle stderr: %s", result.stderr.strip())
            raise RuntimeError(
                f"kaggle 下载失败 (returncode={result.returncode}); "
                f"常见原因: 未接受比赛规则 / kaggle.json 未配置。"
            )

        logger.info("%s下载完成%s", _C.GREEN, _C.RESET)
        self._unzip_all(save_path)

    def _unzip_all(self, save_path: str) -> None:
        """解压 save_path 下所有 .zip 到 data_dir, 成功后删除原压缩包。"""
        zips = [
            os.path.join(save_path, f)
            for f in os.listdir(save_path)
            if f.lower().endswith(".zip")
        ]
        if not zips:
            logger.info("未发现 .zip 文件, 跳过解压")
            return

        target = self.data_dir or save_path
        for zip_path in zips:
            logger.info("解压: %s -> %s", zip_path, target)
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    zf.extractall(target)
            except zipfile.BadZipFile as exc:
                logger.error("压缩包损坏, 跳过 %s: %s", zip_path, exc)
                continue
            try:
                os.remove(zip_path)
                logger.info("已删除原压缩包: %s", zip_path)
            except OSError as exc:
                logger.warning("删除压缩包失败 %s: %s", zip_path, exc)

    # ------------------------------------------------------------------ #
    # ④ 生成 analysis.md
    # ------------------------------------------------------------------ #
    def generate_analysis_md(self, scraped_text: str, save_path: str) -> str:
        """调用 DeepSeek 生成客观赛题总结, 写入 analysis.md。返回文件路径。"""
        _banner("④ generate_analysis_md — DeepSeek 生成客观总结")
        md_path = os.path.join(save_path, "analysis.md")
        template_filled = ANALYSIS_TEMPLATE.format(
            data_dir=self.data_dir, project_root=self.project_root
        )

        content = self._call_deepseek(scraped_text, template_filled)
        if content is None:
            # 降级: 仅写真实路径索引 + 错误说明, 保证管线不中断
            logger.warning("LLM 生成失败, 写入降级版 analysis.md")
            content = (
                "# 🏆 赛题名称: (LLM 生成失败)\n\n"
                "> 注意: DeepSeek 调用失败, 以下仅为本地路径索引。\n\n"
                "## 3. 本地资源路径索引\n"
                f"- **数据集根目录**: `{self.data_dir}`\n"
                f"- **项目工作区**: `{self.project_root}`\n"
            )

        try:
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as exc:
            raise RuntimeError(f"写入 analysis.md 失败: {exc}") from exc

        logger.info("%s已生成: %s%s", _C.GREEN, md_path, _C.RESET)
        return md_path

    def _call_deepseek(self, scraped_text: str, template_filled: str) -> Optional[str]:
        """调用 DeepSeek API, 失败返回 None。"""
        if OpenAI is None:
            logger.error("未安装 openai SDK, 请 pip install openai")
            return None

        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            logger.error("环境变量 DEEPSEEK_API_KEY 未设置")
            return None

        if len(scraped_text) > MAX_LLM_INPUT_CHARS:
            logger.info("抓取文本过长, 截断至 %d 字符", MAX_LLM_INPUT_CHARS)
            scraped_text = scraped_text[:MAX_LLM_INPUT_CHARS]

        system_prompt = (
            "你是一个客观的数据科学助手。请仅根据提供的网页文本, 客观总结赛题内容。"
            "绝对不要提供任何算法建议、模型选型、论文推荐或主观解题思路, 只做客观陈述。"
            "必须严格按照用户给定的 Markdown 模板输出, 不要增删章节, "
            "不要在最外层包裹额外的代码块标记。路径字段必须使用我提供的真实本地路径, 不得改写。"
        )
        user_prompt = (
            "下面是某 Kaggle 比赛页面抓取到的纯文本内容:\n\n"
            f"<<<网页文本开始>>>\n{scraped_text}\n<<<网页文本结束>>>\n\n"
            "请严格按照以下 Markdown 模板填充并输出 (路径已是真实本地路径, 原样保留):\n\n"
            f"{template_filled}"
        )

        try:
            client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
            logger.info("正在调用 DeepSeek (%s)...", DEEPSEEK_MODEL)
            resp = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0,
                stream=False,
            )
            return resp.choices[0].message.content
        except Exception as exc:  # SDK 各类异常: 鉴权/限流/网络
            logger.error("DeepSeek 调用失败: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    # 编排入口
    # ------------------------------------------------------------------ #
    def run(self) -> Optional[str]:
        """按顺序执行完整管线。返回 analysis.md 路径 (失败返回 None)。"""
        _banner("TaskMiner 启动", _C.YELLOW)
        try:
            name = self.setup_workspace(self.url)
            scraped = self.fetch_web_content(self.url)
            self.download_dataset(name, self.data_dir)
            md_path = self.generate_analysis_md(scraped, self.project_root)
            _banner("管线全部完成 ✔", _C.GREEN)
            return md_path
        except LoginRequiredError as exc:
            logger.error("需要人工更新 Cookie: %s", exc)
        except (ValueError, RuntimeError) as exc:
            logger.error("管线中断: %s", exc)
        except Exception as exc:  # 兜底
            logger.exception("未预期异常: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# CLI 入口
# --------------------------------------------------------------------------- #
def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python task_miner.py <kaggle_competition_url> [base_drive]")
        sys.exit(1)
    url = sys.argv[1]
    base_drive = sys.argv[2] if len(sys.argv) > 2 else None
    miner = TaskMiner(url=url, base_drive=base_drive)
    miner.run()


if __name__ == "__main__":
    main()
