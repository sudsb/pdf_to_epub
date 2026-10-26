"""logmanage 测试：任务日志（logs/<prefix>-<时间戳>.log）。

用途：界面只显示「一条进度条 + 一条消息」，转换子进程的完整输出写入
独立的本地日志文件（用户可随时打开查看）。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import logmanage


class TestTaskLogPath(unittest.TestCase):
    def setUp(self):
        """用临时目录做 logs/，避免测试在仓库里留下日志文件。"""
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        patcher = mock.patch.object(logmanage, "logs_dir", return_value=Path(self._td.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_path_under_logs_dir(self):
        """任务日志放在 logs/ 目录下，文件名带前缀与时间戳。"""
        p = logmanage.task_log_path("convert")
        self.assertTrue(str(p).endswith(".log"))
        self.assertIn("convert", p.name)
        self.assertEqual(p.parent, Path(logmanage.logs_dir()))

    def test_prefix_sanitized(self):
        """非法文件名字符被替换为下划线。"""
        p = logmanage.task_log_path("bad/name:here")
        self.assertNotIn("/", p.name)
        self.assertNotIn(":", p.name)
        self.assertTrue(p.name.startswith("bad_name_here"))

    def test_same_prefix_gets_unique_names(self):
        """同一前缀多次调用 → 不同文件名（不覆盖已有日志）。"""
        a = logmanage.task_log_path("convert")
        b = logmanage.task_log_path("convert")
        self.assertNotEqual(a, b)


class TestTaskLog(unittest.TestCase):
    def test_write_and_close(self):
        """写入按行追加，close 后内容落盘。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.log"
            log = logmanage.TaskLog(p)
            log.write("第一行")
            log.write("第二行\n")
            log.close()
            content = p.read_text(encoding="utf-8")
            self.assertIn("第一行", content)
            self.assertIn("第二行", content)

    def test_context_manager(self):
        """支持 with 语句（退出时自动关闭）。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.log"
            with logmanage.TaskLog(p) as log:
                log.write("hello")
            self.assertIn("hello", p.read_text(encoding="utf-8"))

    def test_write_failure_is_silent(self):
        """写入失败（目录不可写）不抛异常——不能因日志拖垮主流程。"""
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "sub" / "x.log"
            log = logmanage.TaskLog(bad)
            with mock.patch("builtins.open", side_effect=OSError("boom")):
                log.write("x")  # 不抛异常
            log.close()


if __name__ == "__main__":
    unittest.main()
