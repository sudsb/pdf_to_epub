"""bench_ocr.py 纯函数单测（不加载 PaddleOCR / cv2，保持快速）。"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

import numpy as np

import bench_ocr as B


class TestNormalize(unittest.TestCase):
    def test_strip_whitespace(self):
        self.assertEqual(B.normalize_text("你 好\n世\t界　啊"), "你好世界啊")

    def test_keep_punct_by_default(self):
        self.assertEqual(B.normalize_text("你好，世界。"), "你好，世界。")

    def test_drop_punct(self):
        self.assertEqual(B.normalize_text("你好，世界。", drop_punct=True), "你好世界")

    def test_empty(self):
        self.assertEqual(B.normalize_text(None), "")
        self.assertEqual(B.normalize_text("   "), "")


class TestEditDistance(unittest.TestCase):
    def test_equal(self):
        self.assertEqual(B.edit_distance("毛泽东", "毛泽东"), 0)

    def test_empty(self):
        self.assertEqual(B.edit_distance("", "abc"), 3)
        self.assertEqual(B.edit_distance("abc", ""), 3)
        self.assertEqual(B.edit_distance("", ""), 0)

    def test_substitution(self):
        self.assertEqual(B.edit_distance("abc", "abd"), 1)

    def test_insert_delete(self):
        self.assertEqual(B.edit_distance("毛泽东选集", "毛泽东"), 2)
        self.assertEqual(B.edit_distance("毛泽东", "毛泽东选集"), 2)

    def test_mixed_cjk(self):
        # 标准 Levenshtein 示例
        self.assertEqual(B.edit_distance("kitten", "sitting"), 3)


class TestCer(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(B.cer("你好世界", "你好世界"), 0.0)

    def test_one_error_in_four(self):
        self.assertAlmostEqual(B.cer("你好世界", "你好世"), 0.25)

    def test_whitespace_ignored(self):
        self.assertEqual(B.cer("你好 世界", "你好世界"), 0.0)

    def test_empty_ref(self):
        self.assertEqual(B.cer("", ""), 0.0)
        self.assertEqual(B.cer("", "啊"), 1.0)


class TestInkBbox(unittest.TestCase):
    def test_empty_page(self):
        img = np.full((100, 100), 255, dtype=np.uint8)
        self.assertIsNone(B.ink_bbox(img))
        self.assertEqual(B.ink_ratio(img), 0.0)

    def test_margin_crop(self):
        # 常见书页：内容居中、四周留白 -> 应裁到内容附近
        img = np.full((400, 400), 255, dtype=np.uint8)
        img[60:340, 60:340] = 0
        box = B.ink_bbox(img, pad_ratio=0.0)
        self.assertIsNotNone(box)
        x1, y1, x2, y2 = box
        self.assertLessEqual(x1, 66)
        self.assertLessEqual(y1, 66)
        self.assertGreaterEqual(x2, 335)
        self.assertGreaterEqual(y2, 335)
        self.assertGreater(x1, 20)  # 不应裁到页边

    def test_too_aggressive_crop_rejected(self):
        # 墨迹只占很小一角 -> 过于激进 -> 返回 None，退回整页
        img = np.full((400, 400), 255, dtype=np.uint8)
        img[180:220, 190:230] = 0
        self.assertIsNone(B.ink_bbox(img))

    def test_noop_crop_rejected(self):
        # 墨迹几乎铺满整页 -> 裁剪无收益 -> 返回 None
        img = np.full((200, 200), 255, dtype=np.uint8)
        img[1:199, 1:199] = 0
        self.assertIsNone(B.ink_bbox(img))

    def test_ink_ratio(self):
        img = np.full((100, 100), 255, dtype=np.uint8)
        img[0:10, :] = 0  # 10%
        self.assertAlmostEqual(B.ink_ratio(img), 0.1, places=6)


class TestMatrix(unittest.TestCase):
    def test_resolution_preset_truncates(self):
        m = B.build_matrix("resolution", native_dpi=200)
        dpis = [c["dpi"] for c in m]
        self.assertEqual(dpis, [200, 150, 120, 100])

    def test_det_preset_cross(self):
        m = B.build_matrix("det", native_dpi=300)
        self.assertTrue(all(c["det"] in (None, 1600, 1280, 960) for c in m))
        self.assertTrue(all(not c["crop"] for c in m))

    def test_all_preset_has_crop_variants(self):
        m = B.build_matrix("all", native_dpi=100, dpi_list=[100])
        self.assertEqual(len(m), 4)  # 1 dpi × 2 det × 2 crop

    def test_custom_dpi_list(self):
        m = B.build_matrix("resolution", native_dpi=300, dpi_list=[300, 150])
        self.assertEqual([c["dpi"] for c in m], [300, 150])


class TestSources(unittest.TestCase):
    def test_find_source_matches_absolute_path(self):
        with tempfile.TemporaryDirectory() as td:
            src = {"dir": os.path.join(td, "book"), "dpi": 200, "pages": 3}
            abs_path = os.path.abspath(src["dir"])
            self.assertIs(B.find_source([src], abs_path), src)
            self.assertIs(B.find_source([src], src["dir"]), src)
            self.assertIsNone(B.find_source([src], os.path.join(td, "other")))

    def test_discover_and_pick(self):
        with tempfile.TemporaryDirectory() as td:
            data = os.path.join(td, "data")
            good = os.path.join(data, "bookA")
            os.makedirs(good)
            with open(os.path.join(good, ".ptoe_split.json"), "w", encoding="utf-8") as fh:
                json.dump({"dpi": 200, "pages": 5}, fh)
            for i in (1, 2, 3, 4, 5):
                with open(os.path.join(good, f"{i}.png"), "wb") as fh:
                    fh.write(b"\x89PNG")
            bad = os.path.join(data, "no_meta")
            os.makedirs(bad)
            with open(os.path.join(bad, "1.png"), "wb") as fh:
                fh.write(b"\x89PNG")

            found = B.discover_sources(data)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["dpi"], 200)
            self.assertEqual(found[0]["pages"], 5)

            picked = B.pick_pages(good, 3)
            self.assertEqual(len(picked), 3)
            names = [os.path.basename(p) for p in picked]
            self.assertEqual(names, sorted(names, key=lambda s: int(s.split(".")[0])))


class TestTimedProxy(unittest.TestCase):
    def test_times_and_forwards(self):
        sink: dict = {}

        def fn(x):
            time.sleep(0.01)
            return x * 2

        p = B.TimedProxy(fn, sink, "det")
        self.assertEqual(p(3), 6)
        self.assertGreater(sink["det"], 0.005)
        self.assertEqual(p.__name__, "fn")

    def test_rebind_moves_timing(self):
        sink1: dict = {}
        sink2: dict = {}
        p = B.TimedProxy(lambda: None, sink1, "rec")
        p.rebind(sink2)
        p()
        self.assertNotIn("rec", sink1)
        self.assertIn("rec", sink2)

    def test_instrument_only_wraps_once(self):
        class Model:
            def __call__(self):
                return 1

        class Pipe:
            text_det_model = Model()
            text_rec_model = None

        pred = type("P", (), {"paddlex_pipeline": Pipe()})()
        s1: dict = {}
        s2: dict = {}
        B.instrument(pred, s1)
        B.instrument(pred, s2)
        self.assertIsInstance(pred.paddlex_pipeline.text_det_model, B.TimedProxy)
        pred.paddlex_pipeline.text_det_model()
        self.assertNotIn("det", s1)
        self.assertIn("det", s2)

    def test_instrument_without_pipeline(self):
        B.instrument(object(), {})  # 不应抛异常

    def test_instrument_unwraps_parallel_wrapper(self):
        class Model:
            def __call__(self):
                return 1

        class Real:
            text_det_model = Model()
            text_rec_model = Model()

        class Wrapper:
            def __init__(self, inner):
                self._pipeline = inner

        pred = type("P", (), {"paddlex_pipeline": Wrapper(Real())})()
        sink: dict = {}
        B.instrument(pred, sink)
        real = pred.paddlex_pipeline._pipeline
        self.assertIsInstance(real.text_det_model, B.TimedProxy)
        real.text_det_model()
        real.text_rec_model()
        self.assertIn("det", sink)
        self.assertIn("rec", sink)


class TestLoadGray(unittest.TestCase):
    def setUp(self):
        try:
            import cv2
        except Exception:
            self.skipTest("cv2 不可用（跳过读图测试）")
        self.cv2 = cv2
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_non_ascii_path(self):
        # 回归：Windows 下 cv2.imread 打不开中文路径
        arr = np.zeros((4, 6), dtype=np.uint8)
        arr[1:3, 2:5] = 200
        path = os.path.join(self.tmp, "中文目录", "第1页.png")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ok, buf = self.cv2.imencode(".png", arr)
        self.assertTrue(ok)
        buf.tofile(path)

        got = B.load_gray(path)
        self.assertEqual(got.shape, (4, 6))
        self.assertEqual(int(got[2, 3]), 200)

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            B.load_gray(os.path.join(self.tmp, "不存在.png"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
