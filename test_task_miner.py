"""TaskMiner 最小可跑单元测试。

全部 mock 网络 / subprocess / LLM, 用临时目录充当外接硬盘, 无需真实 SSD 与 API Key。
运行: python3 -m unittest test_task_miner -v
"""

import os
import tempfile
import unittest
import zipfile
from unittest import mock

from task_miner import TaskMiner, LoginRequiredError


class TaskMinerTestBase(unittest.TestCase):
    """公共 setUp: 用临时目录作为可配置的外接硬盘根。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base_drive = self._tmp.name
        self.url = "https://www.kaggle.com/competitions/titanic"
        self.miner = TaskMiner(self.url, base_drive=self.base_drive)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestParseCompetitionName(unittest.TestCase):
    """URL 解析纯函数, 无需实例。"""

    def test_various_url_forms(self) -> None:
        cases = {
            "https://www.kaggle.com/competitions/titanic": "titanic",
            "https://www.kaggle.com/competitions/titanic/overview": "titanic",
            "https://www.kaggle.com/c/titanic/data": "titanic",
            "https://www.kaggle.com/competitions/house-prices/": "house-prices",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(TaskMiner._parse_competition_name(url), expected)

    def test_invalid_url_raises(self) -> None:
        with self.assertRaises(ValueError):
            TaskMiner._parse_competition_name("https://www.kaggle.com")


class TestInit(unittest.TestCase):
    def test_unmounted_drive_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            TaskMiner(
                "https://www.kaggle.com/competitions/titanic",
                base_drive="/no/such/drive/path",
            )


class TestSetupWorkspace(TaskMinerTestBase):
    def test_creates_dirs_and_sets_attrs(self) -> None:
        name = self.miner.setup_workspace(self.url)
        self.assertEqual(name, "titanic")
        self.assertTrue(os.path.isdir(self.miner.data_dir))
        self.assertTrue(os.path.isdir(self.miner.config_dir))
        self.assertTrue(self.miner.project_root.endswith("Kaggle_Agent/titanic"))


class TestLoadCookieHeader(TaskMinerTestBase):
    def _write_cookie(self, content: str) -> None:
        os.makedirs(self.miner.config_dir, exist_ok=True)
        with open(self.miner.cookie_file, "w", encoding="utf-8") as f:
            f.write(content)

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(self.miner._load_cookie_header(), "")

    def test_raw_format(self) -> None:
        self._write_cookie("CSRF=abc; SESSION=xyz")
        header = self.miner._load_cookie_header()
        self.assertIn("CSRF=abc", header)
        self.assertIn("SESSION=xyz", header)

    def test_netscape_format(self) -> None:
        line = "\t".join([
            ".kaggle.com", "TRUE", "/", "TRUE", "0", "SESSION", "netscape_val",
        ])
        self._write_cookie("# Netscape HTTP Cookie File\n" + line)
        header = self.miner._load_cookie_header()
        self.assertEqual(header, "SESSION=netscape_val")


class TestFetchWebContent(TaskMinerTestBase):
    @staticmethod
    def _fake_response(status_code: int, text: str = ""):
        resp = mock.Mock()
        resp.status_code = status_code
        resp.text = text
        return resp

    def test_success_returns_text(self) -> None:
        html = "<html><body><h1>Titanic</h1><script>x=1</script><p>Predict survival</p></body></html>"
        with mock.patch(
            "task_miner.requests.get",
            return_value=self._fake_response(200, html),
        ):
            content = self.miner.fetch_web_content(self.url)
        self.assertIn("Titanic", content)
        self.assertIn("Predict survival", content)
        self.assertNotIn("x=1", content)  # script 被剥离

    def test_403_raises_login_required(self) -> None:
        with mock.patch(
            "task_miner.requests.get",
            return_value=self._fake_response(403),
        ):
            with self.assertRaises(LoginRequiredError):
                self.miner.fetch_web_content(self.url)


class TestDownloadDataset(TaskMinerTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.miner.setup_workspace(self.url)

    def _make_zip(self, save_path: str) -> str:
        zip_path = os.path.join(save_path, "titanic.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("train.csv", "PassengerId,Survived\n1,0\n")
        return zip_path

    def test_success_unzip_and_cleanup(self) -> None:
        save_path = self.miner.data_dir
        zip_path = self._make_zip(save_path)

        fake_result = mock.Mock()
        fake_result.returncode = 0
        fake_result.stdout = "Downloading titanic.zip"
        fake_result.stderr = ""

        with mock.patch("task_miner.subprocess.run", return_value=fake_result):
            self.miner.download_dataset("titanic", save_path)

        self.assertFalse(os.path.exists(zip_path))  # 原压缩包被删除
        self.assertTrue(os.path.exists(os.path.join(self.miner.data_dir, "train.csv")))

    def test_nonzero_returncode_raises(self) -> None:
        fake_result = mock.Mock()
        fake_result.returncode = 1
        fake_result.stdout = ""
        fake_result.stderr = "403 - You must accept competition rules"

        with mock.patch("task_miner.subprocess.run", return_value=fake_result):
            with self.assertRaises(RuntimeError):
                self.miner.download_dataset("titanic", self.miner.data_dir)

    def test_kaggle_not_installed_raises(self) -> None:
        with mock.patch("task_miner.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(RuntimeError):
                self.miner.download_dataset("titanic", self.miner.data_dir)


class TestGenerateAnalysisMd(TaskMinerTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.miner.setup_workspace(self.url)

    def test_writes_llm_content(self) -> None:
        fake_md = "# 🏆 赛题名称: Titanic\n## 1. 核心目标\n- **任务类型**: 二分类\n"
        with mock.patch.object(self.miner, "_call_deepseek", return_value=fake_md):
            md_path = self.miner.generate_analysis_md("scraped text", self.miner.project_root)
        self.assertTrue(os.path.exists(md_path))
        with open(md_path, encoding="utf-8") as f:
            self.assertIn("Titanic", f.read())

    def test_fallback_when_llm_fails(self) -> None:
        with mock.patch.object(self.miner, "_call_deepseek", return_value=None):
            md_path = self.miner.generate_analysis_md("scraped text", self.miner.project_root)
        with open(md_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("本地资源路径索引", content)
        self.assertIn(self.miner.data_dir, content)


class TestRunIntegration(TaskMinerTestBase):
    """全流程集成测试: 串联 setup -> fetch -> download -> generate, 全部 mock 外部依赖。"""

    @staticmethod
    def _fake_response(status_code: int, text: str = ""):
        resp = mock.Mock()
        resp.status_code = status_code
        resp.text = text
        return resp

    def test_full_pipeline_success(self) -> None:
        html = "<html><body><h1>Titanic</h1><p>Predict survival, metric is accuracy.</p></body></html>"

        # 预置 Cookie 文件, 走 raw 分支
        os.makedirs(self.miner.config_dir, exist_ok=True)
        with open(self.miner.cookie_file, "w", encoding="utf-8") as f:
            f.write("SESSION=integration_val")

        # kaggle CLI: 假装下载出一个 zip, 由 subprocess mock 的 side_effect 落盘
        def fake_subprocess_run(cmd, **kwargs):
            save_path = cmd[cmd.index("-p") + 1]
            os.makedirs(save_path, exist_ok=True)
            zip_path = os.path.join(save_path, "titanic.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("train.csv", "PassengerId,Survived\n1,0\n")
            result = mock.Mock()
            result.returncode = 0
            result.stdout = "Downloading titanic.zip"
            result.stderr = ""
            return result

        fake_md = (
            "# 🏆 赛题名称: Titanic\n"
            "## 1. 核心目标\n- **任务类型**: 二分类\n"
        )

        with mock.patch(
            "task_miner.requests.get",
            return_value=self._fake_response(200, html),
        ), mock.patch(
            "task_miner.subprocess.run",
            side_effect=fake_subprocess_run,
        ), mock.patch.object(
            TaskMiner, "_call_deepseek", return_value=fake_md,
        ) as mock_llm:
            md_path = self.miner.run()

        # 1) 返回并生成了 analysis.md
        self.assertIsNotNone(md_path)
        self.assertTrue(os.path.exists(md_path))
        with open(md_path, encoding="utf-8") as f:
            self.assertIn("Titanic", f.read())

        # 2) 数据集已解压且压缩包被清理
        self.assertTrue(os.path.exists(os.path.join(self.miner.data_dir, "train.csv")))
        self.assertFalse(os.path.exists(os.path.join(self.miner.data_dir, "titanic.zip")))

        # 3) LLM 拿到的是真实抓取文本
        called_text = mock_llm.call_args.args[0]
        self.assertIn("Predict survival", called_text)

    def test_pipeline_aborts_on_login_required(self) -> None:
        """抓取阶段 403, run() 应捕获 LoginRequiredError 并返回 None, 不再继续下载。"""
        with mock.patch(
            "task_miner.requests.get",
            return_value=self._fake_response(403),
        ), mock.patch("task_miner.subprocess.run") as mock_run:
            result = self.miner.run()

        self.assertIsNone(result)
        mock_run.assert_not_called()  # 未进入下载阶段
        self.assertFalse(
            os.path.exists(os.path.join(self.miner.project_root, "analysis.md"))
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
