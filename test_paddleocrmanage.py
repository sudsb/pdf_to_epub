# -*- coding: utf-8 -*-
"""paddleocrmanage 单元测试（2026-08）。

不依赖真实 paddleocr/paddle：通过注入假预测器工厂与假模块验证
batch_infer 契约（与 llamamanage.batch_infer 同形状）、回调、GPU 回退与缺库报错。
"""

import os
import sys
import tempfile
import types
import unittest


def _make_fake_module_paddle(cuda: bool, count: int):
    """构造假 paddle 模块：is_compiled_with_cuda / device_count 可控。"""
    mod = types.ModuleType("paddle")
    mod.is_compiled_with_cuda = lambda: cuda
    dev = types.SimpleNamespace(cuda=types.SimpleNamespace(device_count=lambda: count))
    mod.device = dev
    return mod


class _FakeRes:
    def __init__(self, texts):
        self.res = {"rec_texts": texts}


class _FakePredictor:
    """记录 predict 调用并返回固定文本。"""

    def __init__(self, texts=None):
        self.calls = []
        self.texts = texts or ["第一行", "第二行"]

    def predict(self, path):
        self.calls.append(path)
        return [_FakeRes(self.texts)]


import paddleocrmanage


class TestBatchInferContract(unittest.TestCase):
    def setUp(self):
        self._old_factory = paddleocrmanage._predictor_factory
        self._old_available = paddleocrmanage.available
        paddleocrmanage.reset_predictor()
        self.tmp = tempfile.TemporaryDirectory()
        self.imgs = []
        for i in (2, 1):  # 故意乱序
            p = os.path.join(self.tmp.name, f"{i}.png")
            with open(p, "wb") as f:
                f.write(b"png")
            self.imgs.append(p)

    def tearDown(self):
        paddleocrmanage.reset_predictor()
        paddleocrmanage._predictor_factory = self._old_factory
        paddleocrmanage.available = self._old_available
        self.tmp.cleanup()

    def test_shape_order_and_callbacks(self):
        pred = _FakePredictor()
        paddleocrmanage.available = lambda: True
        paddleocrmanage._predictor_factory = lambda: pred
        progress = []
        got = []

        def on_result(entry):
            self.assertIsInstance(entry, dict)
            got.append(entry)

        res = paddleocrmanage.batch_infer(
            self.imgs,
            prompts=["x", "x"],
            model_key="HY",
            max_workers=3,
            thinking=False,
            timeout=600,
            on_progress=lambda d, t: progress.append((d, t)),
            on_result=on_result,
        )
        # 形状：img/page/result/error 四键；按页码升序
        self.assertEqual([r["page"] for r in res], [1, 2])
        for r in res:
            self.assertIn("img", r)
            self.assertIn("result", r)
            self.assertIn("error", r)
            self.assertIsNone(r["error"])
            self.assertEqual(r["result"], "第一行\n第二行")
        # on_result 收到完整 dict（含 img）
        self.assertEqual(len(got), 2)
        self.assertEqual({g["img"] for g in got}, set(self.imgs))
        # on_progress 逐页推进到 total
        self.assertEqual(progress, [(1, 2), (2, 2)])
        # 预测器按输入顺序被调用（顺序推理；结果才按页码排序）
        self.assertEqual(pred.calls, self.imgs)

    def test_missing_file_error_entry(self):
        pred = _FakePredictor()
        paddleocrmanage.available = lambda: True
        paddleocrmanage._predictor_factory = lambda: pred
        bad = os.path.join(self.tmp.name, "nope.png")
        res = paddleocrmanage.batch_infer([bad])
        self.assertEqual(len(res), 1)
        self.assertIsNone(res[0]["result"])
        self.assertIn("页面图片不存在", res[0]["error"])

    def test_missing_lib_zh_error_no_raise(self):
        paddleocrmanage.available = lambda: False
        called = []

        def boom():
            raise AssertionError("缺库时不应构建预测器")

        paddleocrmanage._predictor_factory = boom
        res = paddleocrmanage.batch_infer(
            self.imgs,
            on_progress=lambda d, t: called.append((d, t)),
            on_result=lambda e: called.append(e),
        )
        self.assertEqual(len(res), 2)
        for r in res:
            self.assertIsNone(r["result"])
            self.assertEqual(r["error"], "未安装 paddleocr：请先安装（pip install paddleocr paddlepaddle）")
        # 缺库路径同样触发回调（断点续传落盘依赖）
        self.assertEqual(len(called), 4)


class TestGpuFallback(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for name in ("paddleocr", "paddle"):
            if name in sys.modules:
                self._saved[name] = sys.modules[name]
                del sys.modules[name]
        paddleocrmanage.reset_predictor()

    def tearDown(self):
        for name, mod in self._saved.items():
            sys.modules[name] = mod
        for name in ("paddleocr", "paddle"):
            if name not in self._saved:
                sys.modules.pop(name, None)
        paddleocrmanage.reset_predictor()

    def _install_fake_ocr(self):
        seen = {}

        class FakePaddleOCR:
            def __init__(self, **kwargs):
                seen.update(kwargs)

        sys.modules["paddleocr"] = types.SimpleNamespace(PaddleOCR=FakePaddleOCR)
        return seen

    def test_gpu_detected(self):
        seen = self._install_fake_ocr()
        sys.modules["paddle"] = _make_fake_module_paddle(True, 1)
        paddleocrmanage._default_factory()
        self.assertEqual(seen.get("device"), "gpu:0")

    def test_cpu_fallback_on_exception(self):
        seen = self._install_fake_ocr()

        class Boom:
            @staticmethod
            def is_compiled_with_cuda():
                raise RuntimeError("boom")

        sys.modules["paddle"] = types.SimpleNamespace(device=Boom)
        paddleocrmanage._default_factory()
        self.assertEqual(seen.get("device"), "cpu")

    def test_cpu_when_no_cuda(self):
        seen = self._install_fake_ocr()
        sys.modules["paddle"] = _make_fake_module_paddle(False, 0)
        paddleocrmanage._default_factory()
        self.assertEqual(seen.get("device"), "cpu")


if __name__ == "__main__":
    unittest.main()
