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


class _FakeDictPredictor:
    """模拟真实 PaddleOCR 3.x：predict() 返回顶层 dict 列表（rec_texts 为顶层键）。"""

    def __init__(self, texts=None):
        self.calls = []
        self.texts = texts or ["顶层甲", "顶层乙"]

    def predict(self, path):
        self.calls.append(path)
        return [{"rec_texts": self.texts}]


class _FakePredictor:
    """记录 predict 调用并返回固定文本。"""

    def __init__(self, texts=None):
        self.calls = []
        self.texts = texts or ["第一行", "第二行"]

    def predict(self, path):
        self.calls.append(path)
        return [_FakeRes(self.texts)]


class _FakeNumpyArray:
    """模拟 numpy 数组：只提供 tolist()（鸭子类型处理，测试不真装 numpy）。"""

    def __init__(self, data):
        self._data = data

    def tolist(self):
        return self._data


# 带坐标的识别结果：同一段落被切成两行（第一行底部 120，第二行顶部 120 → gap=0）
_BOX_RES = {
    "rec_texts": ["今天天气很好适合", "出门散步"],
    "rec_scores": [0.99, 0.98],
    "rec_boxes": [[10, 100, 110, 120], [10, 120, 110, 140]],
}


class _FakeBoxPredictor:
    """返回带坐标结果（rec_texts + rec_scores + rec_boxes），供合并逻辑测试。"""

    def __init__(self, res=None):
        self.calls = []
        self.res = res or _BOX_RES

    def predict(self, path):
        self.calls.append(path)
        return [self.res]


def _line(text, box):
    """构造一条行字典（box=None 表示该行无坐标）。"""
    return {"text": text, "score": 1.0, "box": box}


def _res(texts, boxes=None, scores=None):
    """构造一条 PaddleOCR 结果（3.x 顶层 dict 形状）。"""
    d = {"rec_texts": list(texts)}
    if scores is not None:
        d["rec_scores"] = list(scores)
    if boxes is not None:
        d["rec_boxes"] = boxes
    return d


class _MergeConfigMixin:
    """把 configmanage.get_config 换成内存假配置：不弹 tkinter、不写用户 config.json。

    value 为 None 时返回空 dict（模拟旧配置里没有该键）。
    """

    def _patch_config(self, value):
        import configmanage

        cfg = {} if value is None else {"paddle_merge_lines": value}
        self._saved_get_config = configmanage.get_config
        configmanage.get_config = lambda **kwargs: dict(cfg)

    def _restore_config(self):
        import configmanage

        configmanage.get_config = self._saved_get_config


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

    def test_top_level_dict_result_shape(self):
        """PaddleOCR 3.x predict() 返回顶层 dict（rec_texts 为顶层键）。

        2026-09 曾只兼容 .res 包装形状，导致真实引擎 result 恒空、
        EPUB 无正文（旧测试用 _FakeRes 掩盖了该 bug）。
        """
        pred = _FakeDictPredictor()
        paddleocrmanage.available = lambda: True
        paddleocrmanage._predictor_factory = lambda: pred
        res = paddleocrmanage.batch_infer(self.imgs)
        self.assertEqual(len(res), 2)
        for r in res:
            self.assertIsNone(r["error"])
            self.assertEqual(r["result"], "顶层甲\n顶层乙")
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


class TestExtractLines(unittest.TestCase):
    """坐标/置信度抽取：多种 rec_boxes 形状与缺失字段的默认值。"""

    def test_extract_lines_box_shapes(self):
        """xyxy 一维 / 4×2 四点多边形 / 折行成 2×4 的四点 / numpy 风格 tolist() 都能摊成 xyxy。"""
        poly2x4 = _FakeNumpyArray([[10, 100, 110, 100], [10, 120, 110, 120]])  # 折行成 2 行
        poly4x2 = _FakeNumpyArray([[10, 100], [110, 100], [110, 120], [10, 120]])  # 4 点×2
        res = _res(
            ["甲", "乙", "丙", "丁"],
            boxes=[[10, 100, 110, 120], poly2x4, poly4x2, _FakeNumpyArray([5, 6, 105, 116])],
            scores=[0.9, 0.8, 0.7, 0.6],
        )
        lines = paddleocrmanage.extract_lines_from_res(res)
        self.assertEqual([ln["text"] for ln in lines], ["甲", "乙", "丙", "丁"])
        for ln in lines[:3]:  # 前三种形状都应是同一个外接矩形
            self.assertEqual(ln["box"], (10.0, 100.0, 110.0, 120.0), f"box 摊平失败：{ln}")
        self.assertEqual(lines[3]["box"], (5.0, 6.0, 105.0, 116.0))
        self.assertEqual([ln["score"] for ln in lines], [0.9, 0.8, 0.7, 0.6])

    def test_extract_lines_missing_scores_and_boxes(self):
        """rec_scores/rec_boxes 缺失 → score 1.0、box None，文本照常保留。"""
        lines = paddleocrmanage.extract_lines_from_res(_res(["甲", "乙"]))
        self.assertEqual([ln["score"] for ln in lines], [1.0, 1.0])
        self.assertEqual([ln["box"] for ln in lines], [None, None])
        self.assertEqual([ln["text"] for ln in lines], ["甲", "乙"])

    def test_extract_lines_unknown_box_shape_keeps_text(self):
        """坐标形状不认识（元素数不对/None/脏数据）→ box=None，文本绝不丢；空白行丢弃。"""
        res = _res(["甲", "乙", "   ", "丙"], boxes=[[1, 2, 3], None, [4, 5, 6, 7, 8], "脏数据"])
        lines = paddleocrmanage.extract_lines_from_res(res)
        self.assertEqual([ln["text"] for ln in lines], ["甲", "乙", "丙"])
        self.assertEqual([ln["box"] for ln in lines], [None, None, None])

    def test_extract_lines_res_wrapper_shape(self):
        """旧版 .res 包装形状同样能抽到坐标。"""

        class _Wrapped:
            def __init__(self, data):
                self.res = data

        lines = paddleocrmanage.extract_lines_from_res(_Wrapped(_res(["甲"], boxes=[[1, 2, 3, 4]])))
        self.assertEqual(lines[0]["box"], (1.0, 2.0, 3.0, 4.0))

    def test_extract_lines_not_a_dict_result(self):
        """结果形状完全不认识（既非 dict 也无 .res）→ 空列表，不抛异常。"""
        self.assertEqual(paddleocrmanage.extract_lines_from_res(object()), [])
        self.assertEqual(paddleocrmanage.extract_lines_from_res(None), [])


class TestGroupRowsOrder(unittest.TestCase):
    """行归组排序：Y 升序、行内 X 升序、无坐标时保持原序。"""

    def test_group_rows_sorts_by_y_then_x(self):
        """乱序输入 → 按中心 Y 聚行后，行间 Y 升序、同一行内 X 升序。"""
        lines = [
            _line("丁", (10, 140, 110, 160)),
            _line("乙", (130, 100, 230, 120)),
            _line("甲", (10, 100, 110, 120)),
            _line("丙", (10, 120, 110, 140)),
        ]
        rows = paddleocrmanage._group_rows(lines, 20.0)
        self.assertEqual([ln["text"] for row in rows for ln in row], ["甲", "乙", "丙", "丁"])
        self.assertEqual(len(rows), 3, "甲乙同一行，丙/丁各自成行")

    def test_row_column_split_not_glued(self):
        """同一视觉行内相距很远的两个框（左右两栏）被切成独立视觉行，不粘成一行。"""
        lines = [
            _line("右栏上", (500, 100, 600, 120)),
            _line("左栏上", (10, 100, 110, 120)),
        ]
        rows = paddleocrmanage._group_rows(lines, 20.0)
        self.assertEqual([len(r) for r in rows], [1, 1], "水平间隙 390 > 2.0×行高，应切成两行")
        self.assertEqual(paddleocrmanage._lines_to_paragraphs(lines), ["左栏上", "右栏上"])

    def test_all_boxes_none_keeps_input_order(self):
        """全部无坐标 → 保持输入原顺序，每行独立成段（等价旧的逐行行为）。"""
        lines = [_line("丙", None), _line("甲", None), _line("乙", None)]
        self.assertEqual(paddleocrmanage._lines_to_paragraphs(lines), ["丙", "甲", "乙"])

    def test_empty_lines(self):
        self.assertEqual(paddleocrmanage._lines_to_paragraphs([]), [])


class TestMergeRules(unittest.TestCase):
    """合并规则：正例 1 个 + 反例 4 个各自独立。"""

    def test_merge_same_paragraph(self):
        """同段落多行（gap=0、左对齐、重叠充分、上一行无句末标点、下一行非新块）→ 合并。"""
        lines = [
            _line("今天天气很好适合", (10, 100, 110, 120)),
            _line("出门散步", (10, 120, 110, 140)),
        ]
        self.assertEqual(paddleocrmanage._lines_to_paragraphs(lines), ["今天天气很好适合出门散步"])

    def test_no_merge_when_prev_ends_with_sentence_punct(self):
        """反例①：上一行以句末标点「。」结尾 → 不合并。"""
        lines = [
            _line("今天天气很好适合出门散步。", (10, 100, 110, 120)),
            _line("沿途的花也都开了", (10, 120, 110, 140)),
        ]
        self.assertEqual(
            paddleocrmanage._lines_to_paragraphs(lines),
            ["今天天气很好适合出门散步。", "沿途的花也都开了"],
        )

    def test_no_merge_when_gap_too_large(self):
        """反例②：行间距 40 ≥ 1.5 × 中位行高 30 → 不合并。"""
        lines = [
            _line("今天天气很好适合", (10, 100, 110, 120)),
            _line("出门散步", (10, 160, 110, 180)),
        ]
        self.assertEqual(
            paddleocrmanage._lines_to_paragraphs(lines), ["今天天气很好适合", "出门散步"]
        )

    def test_no_merge_when_left_boundary_misaligned(self):
        """反例③：左边界严重不对齐（390 > 0.5×行高）且水平无重叠 → 不合并。"""
        lines = [
            _line("今天天气很好适合", (10, 100, 110, 120)),
            _line("出门散步", (400, 120, 500, 140)),
        ]
        self.assertEqual(
            paddleocrmanage._lines_to_paragraphs(lines), ["今天天气很好适合", "出门散步"]
        )

    def test_no_merge_when_next_starts_with_digit(self):
        """反例④：下一行以数字开头（新块起始，如「3页…」）→ 不合并。"""
        lines = [
            _line("今天天气很好适合", (10, 100, 110, 120)),
            _line("3页之后再说", (10, 120, 110, 140)),
        ]
        self.assertEqual(
            paddleocrmanage._lines_to_paragraphs(lines), ["今天天气很好适合", "3页之后再说"]
        )

    def test_merge_uses_center_alignment(self):
        """居中排版的行靠「中心对齐」也能合并（左边界不对齐但中心差 ≤ 0.5×行高）。"""
        lines = [
            _line("短句", (100, 100, 200, 120)),
            _line("后面这句稍长一些", (80, 120, 220, 140)),
        ]
        self.assertEqual(paddleocrmanage._lines_to_paragraphs(lines), ["短句后面这句稍长一些"])


class TestJoinFragments(unittest.TestCase):
    """连接符按字符类型自适应。"""

    def test_cjk_plus_cjk_no_space(self):
        self.assertEqual(paddleocrmanage._join_fragments("今天天气", "很好"), "今天天气很好")

    def test_cjk_plus_ascii_letter_gets_space(self):
        self.assertEqual(paddleocrmanage._join_fragments("见附录", "Appendix"), "见附录 Appendix")

    def test_cjk_plus_ascii_digit_gets_space(self):
        self.assertEqual(paddleocrmanage._join_fragments("共计", "3页"), "共计 3页")

    def test_strips_and_empty_guard(self):
        self.assertEqual(paddleocrmanage._join_fragments("见图 ", " Table"), "见图 Table")
        self.assertEqual(paddleocrmanage._join_fragments("", "甲"), "甲")
        self.assertEqual(paddleocrmanage._join_fragments("甲", "  "), "甲")


class TestBatchInferMergeLines(_MergeConfigMixin, unittest.TestCase):
    """batch_infer 集成：开关开 → 合并；关/缺键/非法/读失败 → 逐行；合并异常 → 兜底逐行。"""

    def setUp(self):
        self._old_factory = paddleocrmanage._predictor_factory
        self._old_available = paddleocrmanage.available
        paddleocrmanage.reset_predictor()
        self.tmp = tempfile.TemporaryDirectory()
        self.imgs = []
        for i in (1, 2):
            p = os.path.join(self.tmp.name, f"{i}.png")
            with open(p, "wb") as f:
                f.write(b"png")
            self.imgs.append(p)

    def tearDown(self):
        self._restore_config()
        paddleocrmanage.reset_predictor()
        paddleocrmanage._predictor_factory = self._old_factory
        paddleocrmanage.available = self._old_available
        self.tmp.cleanup()

    def _run(self, value, res=None):
        self._patch_config(value)
        paddleocrmanage.available = lambda: True
        paddleocrmanage._predictor_factory = lambda: _FakeBoxPredictor(res)
        return paddleocrmanage.batch_infer(self.imgs)

    def test_merge_enabled_merges_lines(self):
        """开关开 + 结果带坐标 → 同段落两行合并成一段。"""
        res = self._run(True)
        self.assertEqual([r["page"] for r in res], [1, 2])
        for r in res:
            self.assertIsNone(r["error"])
            self.assertEqual(r["result"], "今天天气很好适合出门散步")

    def test_merge_disabled_keeps_newline_join(self):
        """开关关 → 逐字节退回旧的 "\\n".join(rec_texts)。"""
        res = self._run(False)
        for r in res:
            self.assertIsNone(r["error"])
            self.assertEqual(r["result"], "今天天气很好适合\n出门散步")

    def test_config_key_missing_defaults_true(self):
        """配置里没有该键（旧配置）→ 回退 True，走合并。"""
        for r in self._run(None):
            self.assertEqual(r["result"], "今天天气很好适合出门散步")

    def test_config_illegal_value_defaults_true(self):
        """配置值非法 → 回退 True，走合并。"""
        for r in self._run("maybe"):
            self.assertEqual(r["result"], "今天天气很好适合出门散步")

    def test_config_string_false_is_respected(self):
        """配置里存了字符串 "false"（与 mian/guimanage 同一真值表）→ 视为关闭。"""
        for r in self._run("false"):
            self.assertEqual(r["result"], "今天天气很好适合\n出门散步")

    def test_config_read_failure_defaults_true(self):
        """get_config 抛异常（headless/损坏配置）→ 回退 True，不能让 OCR 挂掉。"""
        import configmanage

        def boom(**kwargs):
            raise RuntimeError("config 读不了")

        self._saved_get_config = configmanage.get_config
        configmanage.get_config = boom
        paddleocrmanage.available = lambda: True
        paddleocrmanage._predictor_factory = lambda: _FakeBoxPredictor()
        for r in paddleocrmanage.batch_infer(self.imgs):
            self.assertIsNone(r["error"])
            self.assertEqual(r["result"], "今天天气很好适合出门散步")

    def test_merge_exception_falls_back_to_plain_lines(self):
        """合并过程抛任何异常 → 退回逐行旧行为（逐页容错，page 仍成功）。"""
        old = paddleocrmanage._lines_to_paragraphs

        def boom(lines):
            raise RuntimeError("合并炸了")

        paddleocrmanage._lines_to_paragraphs = boom
        try:
            res = self._run(True)
        finally:
            paddleocrmanage._lines_to_paragraphs = old
        for r in res:
            self.assertIsNone(r["error"])
            self.assertEqual(r["result"], "今天天气很好适合\n出门散步")

    def test_no_boxes_result_keeps_line_per_line(self):
        """结果不带坐标（抽不到行）→ 保持旧的逐行输出。"""
        res = self._run(True, res={"rec_texts": ["顶层甲", "顶层乙"]})
        for r in res:
            self.assertEqual(r["result"], "顶层甲\n顶层乙")


class TestDefaultConfigSeed(unittest.TestCase):
    """configmanage 种子键（新增顶层键由递归 merge 自动补全，不另写补全分支）。"""

    def test_default_config_has_merge_lines_true(self):
        import configmanage

        self.assertIn("paddle_merge_lines", configmanage.DEFAULT_CONFIG)
        self.assertIs(configmanage.DEFAULT_CONFIG["paddle_merge_lines"], True)

    def test_validate_and_patch_fills_missing_key(self):
        """旧配置（无该键）经 validate_and_patch_config 自动补成 True。"""
        import configmanage

        out = configmanage.validate_and_patch_config({})
        self.assertIs(out["paddle_merge_lines"], True)


if __name__ == "__main__":
    unittest.main()
