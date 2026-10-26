# -*- coding: utf-8 -*-
"""转换 ETA（预计完成时间）回归护栏（2026-10-21）。

覆盖的不变量：
  E1 format_duration / format_clock 的展示格式。
  E2 estimate_stage_eta：样本不足不给结论、done==total 为 0、线性外推。
  E3 EtaTracker：速率 EMA、阶段切换重置、整体 ETA ≥ 阶段 ETA、
     手动矫正（correct）阶段不给整体 ETA。
  E4 mian._emit_progress 返回 ETA 信息并带进进度标记（GUI 用）。
  E5 guimanage 解析进度标记时保留 ETA 字段；GUI 前端有 ETA 渲染函数。
"""
import io
import os
import re
import unittest

import mian
import progressmanage

ROOT = os.path.dirname(os.path.abspath(__file__))


def read(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return f.read()


class FormatTest(unittest.TestCase):
    """E1：展示格式。"""

    def test_duration(self):
        self.assertEqual(progressmanage.format_duration(0), "0秒")
        self.assertEqual(progressmanage.format_duration(45), "45秒")
        self.assertEqual(progressmanage.format_duration(192), "3分12秒")
        self.assertEqual(progressmanage.format_duration(3720), "1小时02分")

    def test_duration_invalid(self):
        self.assertEqual(progressmanage.format_duration(None), "-")
        self.assertEqual(progressmanage.format_duration(-5), "-")

    def test_clock(self):
        import time

        self.assertEqual(progressmanage.format_clock(0), "-")
        self.assertRegex(progressmanage.format_clock(time.time()), r"^\d{2}:\d{2}$")


class StageEtaTest(unittest.TestCase):
    """E2：阶段内 ETA。"""

    def test_insufficient_samples(self):
        self.assertIsNone(progressmanage.estimate_stage_eta(0, 10, 5.0))
        self.assertIsNone(progressmanage.estimate_stage_eta(1, 10, 5.0))

    def test_done_equals_total(self):
        self.assertEqual(progressmanage.estimate_stage_eta(10, 10, 5.0), 0.0)

    def test_linear_extrapolation(self):
        # 10 项用了 20 秒 → 剩余 10 项约 20 秒
        got = progressmanage.estimate_stage_eta(10, 20, 20.0)
        self.assertAlmostEqual(20.0, got, places=1)

    def test_rate_overrides(self):
        got = progressmanage.estimate_stage_eta(5, 10, 100.0, rate=0.5)
        self.assertAlmostEqual(10.0, got, places=1)

    def test_no_total(self):
        self.assertIsNone(progressmanage.estimate_stage_eta(1, 0, 5.0))


class TrackerTest(unittest.TestCase):
    """E3：跟踪器。"""

    def test_update_returns_eta_fields(self):
        t = progressmanage.EtaTracker()
        info = t.update("ocr", done=2, total=10, now=100.0)
        self.assertIn("eta_sec", info)
        self.assertIn("eta_total_sec", info)
        self.assertIn("eta_at", info)
        self.assertIn("elapsed_sec", info)

    def test_rate_is_smoothed(self):
        t = progressmanage.EtaTracker()
        t.update("ocr", done=1, total=10, now=0.0)
        info = t.update("ocr", done=2, total=10, now=2.0)
        self.assertGreater(info["rate"], 0)

    def test_overall_eta_not_smaller_than_stage_eta(self):
        t = progressmanage.EtaTracker()
        t.update("ocr", done=1, total=10, now=0.0)
        info = t.update("ocr", done=3, total=10, now=6.0)
        self.assertIsNotNone(info["eta_sec"])
        self.assertIsNotNone(info["eta_total_sec"])
        self.assertGreaterEqual(info["eta_total_sec"], info["eta_sec"])

    def test_stage_switch_resets_rate(self):
        t = progressmanage.EtaTracker()
        t.update("ocr", done=5, total=10, now=0.0)
        info = t.update("render", done=1, total=2, now=100.0)
        self.assertEqual(0.0, info["rate"])

    def test_manual_stage_has_no_overall_eta(self):
        t = progressmanage.EtaTracker()
        info = t.update("correct", done=1, total=3, now=10.0)
        self.assertIsNone(info["eta_total_sec"])

    def test_finished_stage_eta_zero(self):
        t = progressmanage.EtaTracker()
        info = t.update("done", done=1, total=1, now=10.0)
        self.assertEqual(0.0, info["eta_total_sec"])


class EmitProgressTest(unittest.TestCase):
    """E4：mian 的进度上报带 ETA。"""

    def test_emit_progress_returns_info(self):
        info = mian._emit_progress("ocr", "测试", done=2, total=10)
        self.assertIsInstance(info, dict)
        self.assertIn("eta_sec", info)

    def test_marker_payload_contains_eta(self):
        import json

        old = mian._PROGRESS_MARKER
        buf = []

        class _FakeOut:
            def write(self, s):
                buf.append(s)

            def flush(self):
                pass

        old_stdout = mian.sys.stdout
        mian.sys.stdout = _FakeOut()
        old_env = os.environ.get(mian._UI_PROGRESS_ENV)
        os.environ[mian._UI_PROGRESS_ENV] = "1"
        try:
            mian._emit_progress("ocr", "测试", done=3, total=10)
        finally:
            mian.sys.stdout = old_stdout
            if old_env is None:
                os.environ.pop(mian._UI_PROGRESS_ENV, None)
            else:
                os.environ[mian._UI_PROGRESS_ENV] = old_env
        self.assertTrue(buf, "没有输出进度标记")
        line = buf[0]
        self.assertTrue(line.startswith(old))
        payload = json.loads(line[len(old):].strip())
        self.assertEqual("ocr", payload["stage"])
        self.assertIn("elapsed_sec", payload)


class GuiStaticTest(unittest.TestCase):
    """E5：GUI 侧保留 ETA 字段并渲染。"""

    def test_guimanage_keeps_eta_fields(self):
        src = read(os.path.join(ROOT, "guimanage.py"))
        for key in ("eta_sec", "eta_total_sec", "eta_at"):
            self.assertIn('"%s"' % key, src)

    def test_gui_has_eta_renderer(self):
        src = read(os.path.join(ROOT, "guimanage.py"))
        self.assertIn("function fmtEtaText", src)
        self.assertIn("fmtEtaText(pg)", src)


if __name__ == "__main__":
    unittest.main()
