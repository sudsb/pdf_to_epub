"""stringmanage 单元测试：PaddleOCR 系模型（ULQ4/ULQ8）带坐标输出转换 + 页码清理。

Run: uv run python -m unittest test_stringmanage
"""
import unittest

from stringmanage import (
    clean_and_structure_text,
    clean_bbox_text,
    convert_bbox_text,
    detect_headings,
    strip_page_numbers,
    strip_think_blocks,
    _normalize_punctuation_extended,
    _normalize_ellipsis,
)


class TestConvertBboxText(unittest.TestCase):
    def test_bbox_title_to_h2(self):
        text = "title [337, 99, 611, 123]工人夜校招生广告"
        self.assertEqual(
            convert_bbox_text(text), "<h2>工人夜校招生广告</h2>"
        )

    def test_bbox_text_to_p(self):
        text = "text [21, 152, 327, 170]列位工人来听我们说几句白话："
        self.assertEqual(
            convert_bbox_text(text), "<p>列位工人来听我们说几句白话：</p>"
        )

    def test_bbox_page_number_skipped(self):
        text = "page_number [78, 904, 94, 918]2"
        self.assertEqual(convert_bbox_text(text), "")

    def test_bbox_unknown_label_kept_as_p(self):
        text = "note [10, 10, 50, 20]一条注释"
        self.assertEqual(convert_bbox_text(text), "<p>一条注释</p>")

    def test_bbox_second_line_detected(self):
        # 回归：detection 必须匹配到多行文本中非首行的 bbox 行
        text = "某模型的前言说明\n\ntitle [1, 2, 3, 4]夜学日记"
        self.assertEqual(
            convert_bbox_text(text),
            "某模型的前言说明\n\n<h2>夜学日记</h2>",
        )

    def test_bbox_non_bbox_text_untouched(self):
        text = "普通文本输出，没有任何坐标格式\n第二行"
        self.assertEqual(convert_bbox_text(text), text)

    def test_bbox_empty_content_skipped(self):
        text = "title [1, 2, 3, 4]   \ntext [1, 2, 3, 4]正文"
        self.assertEqual(convert_bbox_text(text), "<p>正文</p>")

    def test_bbox_mixed_lines(self):
        text = (
            "title [337, 99, 611, 123]工人夜校招生广告\n"
            "text [21, 152, 327, 170]列位工人来听我们说几句白话：\n"
            "page_number [78, 904, 94, 918]2\n"
            "无坐标的散行保留"
        )
        self.assertEqual(
            convert_bbox_text(text),
            "<h2>工人夜校招生广告</h2>\n"
            "<p>列位工人来听我们说几句白话：</p>\n"
            "无坐标的散行保留",
        )

    def test_bbox_special_chars_escaped(self):
        text = 'text [1, 2, 3, 4]a < b & c "d"'
        self.assertEqual(
            convert_bbox_text(text), "<p>a &lt; b &amp; c \"d\"</p>"
        )


class TestCleanBboxText(unittest.TestCase):
    """clean_bbox_text：纯文本版 bbox 清理（/api/reocr 用，ULQ4/ULQ8 输出）。"""

    def test_strip_prefix_keeps_content(self):
        text = "text [21, 152, 327, 170]列位工人来听我们说几句白话："
        self.assertEqual(clean_bbox_text(text), "列位工人来听我们说几句白话：")

    def test_title_label_kept_as_text(self):
        text = "title [337, 99, 611, 123]工人夜校招生广告"
        self.assertEqual(clean_bbox_text(text), "工人夜校招生广告")

    def test_skip_labels_dropped(self):
        text = (
            "text [1, 2, 3, 4]正文内容\n"
            "page_number [78, 904, 94, 918]2\n"
            "figure [10, 10, 50, 20]图注\n"
            "image [10, 10, 50, 20]图片"
        )
        self.assertEqual(clean_bbox_text(text), "正文内容")

    def test_non_bbox_lines_kept(self):
        text = "某模型的前言说明\n\ntitle [1, 2, 3, 4]夜学日记"
        self.assertEqual(clean_bbox_text(text), "某模型的前言说明\n\n夜学日记")

    def test_no_bbox_match_unchanged(self):
        text = "普通文本输出，没有任何坐标格式\n第二行"
        self.assertEqual(clean_bbox_text(text), text)

    def test_empty_content_line_dropped(self):
        text = "title [1, 2, 3, 4]   \ntext [1, 2, 3, 4]正文"
        self.assertEqual(clean_bbox_text(text), "正文")

    def test_empty_text(self):
        self.assertEqual(clean_bbox_text(""), "")


class TestStripThinkBlocks(unittest.TestCase):
    """strip_think_blocks：剥离模型输出的 <thinking> 思考块。"""

    def test_tag_block_removed(self):
        text = "<thinking>让我先分析一下这张图片。</thinking>正文内容"
        self.assertEqual(strip_think_blocks(text), "正文内容")

    def test_multiple_blocks_removed(self):
        text = "<thinking>第一段思考</thinking>正文一<thinking>第二段思考</thinking>正文二"
        self.assertEqual(strip_think_blocks(text), "正文一正文二")

    def test_no_tag_untouched(self):
        text = "普通文本没有思考块"
        self.assertEqual(strip_think_blocks(text), text)

    def test_multiline_block_removed(self):
        text = "<thinking>\n第一行思考\n第二行思考\n</thinking>正文"
        self.assertEqual(strip_think_blocks(text), "正文")

    def test_empty_text(self):
        self.assertEqual(strip_think_blocks(""), "")


class TestStripPageNumbers(unittest.TestCase):
    def test_trailing_digit_removed(self):
        text = "第一行正文内容\n第二行正文\n2"
        self.assertEqual(strip_page_numbers(text), "第一行正文内容\n第二行正文\n")

    def test_leading_digit_removed(self):
        text = "3\n正文开始"
        self.assertEqual(strip_page_numbers(text), "\n正文开始")

    def test_both_ends_removed(self):
        text = "2\n正文\n12"
        self.assertEqual(strip_page_numbers(text), "\n正文\n")

    def test_middle_digit_kept(self):
        text = "第一段\n2\n第二段"
        self.assertEqual(strip_page_numbers(text), text)

    def test_year_kept(self):
        text = "正文末尾\n1918"
        self.assertEqual(strip_page_numbers(text), text)

    def test_variants(self):
        for num, rest in [
            ("第 3 页", "正文"),
            ("第3页", "正文"),
            ("- 4 -", "正文"),
            ("— 5 —", "正文"),
            ("6 / 12", "正文"),
        ]:
            with self.subTest(num=num):
                self.assertEqual(strip_page_numbers(num + "\n" + rest), "\n" + rest)

    def test_page_number_only_page(self):
        self.assertEqual(strip_page_numbers("2"), "")

    def test_empty_text(self):
        self.assertEqual(strip_page_numbers(""), "")
        self.assertEqual(strip_page_numbers("\n\n"), "\n\n")


class TestCleanAndStructureIntegration(unittest.TestCase):
    def test_bbox_page_flow(self):
        pages = [
            {
                "page": 1,
                "text": (
                    "title [337, 99, 611, 123]工人夜校招生广告\n"
                    "text [21, 152, 327, 170]列位工人来听我们说几句白话：\n"
                    "page_number [78, 904, 94, 918]2"
                ),
            }
        ]
        structured = clean_and_structure_text(pages)
        self.assertIn("<h2>工人夜校招生广告</h2>", structured["body"])
        self.assertIn("<p>列位工人来听我们说几句白话：</p>", structured["body"])
        self.assertNotIn("page_number", structured["body"])
        self.assertNotIn("2", structured["body"].split("\n")[-1])

    def test_plain_page_page_number_flow(self):
        pages = [
            {
                "page": 1,
                "text": (
                    "这是正文第一段内容，写得很长，超过三十个字符，"
                    "因此不会被标题启发式识别成标题行。\n"
                    "这是正文第二段内容，同样足够长，不会被误判。\n"
                    "2"
                ),
            }
        ]
        structured = clean_and_structure_text(pages)
        self.assertNotIn("\n2", structured["body"])
        self.assertIn("这是正文第一段内容", structured["body"])

    def test_page_order_preserved(self):
        pages = [
            {
                "page": 2,
                "text": "这是第二页的正文内容，句子足够长，超过三十个字符，不会被标题启发式识别。",
            },
            {
                "page": 1,
                "text": "这是第一页的正文内容，句子足够长，超过三十个字符，不会被标题启发式识别。",
            },
        ]
        structured = clean_and_structure_text(pages)
        self.assertEqual(
            structured["body"],
            "这是第一页的正文内容，句子足够长，超过三十个字符，不会被标题启发式识别。\n"
            "这是第二页的正文内容，句子足够长，超过三十个字符，不会被标题启发式识别。",
        )


class TestDetectHeadings(unittest.TestCase):
    def test_short_line_becomes_h1(self):
        text = "工人夜校招生广告\n列位工人来听我们说几句白话："
        self.assertEqual(
            detect_headings(text),
            "<h1>工人夜校招生广告</h1>\n<p>列位工人来听我们说几句白话：</p>",
        )

    def test_colon_line_not_heading(self):
        # 以冒号结尾的行是引出行/前言，不是标题；单独出现时整页无命中 → 原样返回
        self.assertEqual(detect_headings("编者按："), "编者按：")

    def test_no_heading_page_unchanged(self):
        text = "这是一段非常长的句子，总长度已经远远超过三十个字符的上限，所以它应该是正文而不是标题。"
        self.assertEqual(detect_headings(text), text)  # 无命中 → 原样返回（逐字节不变）

    def test_html_lines_skipped(self):
        text = "<h2>工人夜校招生广告</h2>\n散行文本"
        self.assertEqual(
            detect_headings(text),
            "<h2>工人夜校招生广告</h2>\n<h1>散行文本</h1>",
        )

    def test_digits_only_not_heading(self):
        self.assertEqual(detect_headings("1918"), "1918")  # 无文字 → 不判标题 → 原样

    def test_clean_flow_converts_heading_pages(self):
        pages = [
            {
                "page": 1,
                "text": "工人夜校招生广告\n列位工人来听我们说几句白话：\n2",
            }
        ]
        structured = clean_and_structure_text(pages)
        self.assertIn("<h1>工人夜校招生广告</h1>", structured["body"])
        self.assertIn("<p>列位工人来听我们说几句白话：</p>", structured["body"])


class TestNormalizePunctuationExtended(unittest.TestCase):
    """_normalize_punctuation_extended：扩展标点/符号归一化（智能清理用）。"""

    def test_base_punctuation_cjk_context(self):
        # 基础标点在中文上下文：半角→全角
        self.assertEqual(_normalize_punctuation_extended("你好,世界"), "你好，世界")
        self.assertEqual(_normalize_punctuation_extended("他说;好吧"), "他说；好吧")
        self.assertEqual(_normalize_punctuation_extended("真的:假的"), "真的：假的")
        self.assertEqual(_normalize_punctuation_extended("什么?真的"), "什么？真的")
        self.assertEqual(_normalize_punctuation_extended("太好了!"), "太好了！")
        self.assertEqual(_normalize_punctuation_extended("见(附录)"), "见（附录）")
        self.assertEqual(_normalize_punctuation_extended("测试)内容"), "测试）内容")

    def test_base_punctuation_latin_context(self):
        # 基础标点在英文/数字上下文：全角→半角
        self.assertEqual(_normalize_punctuation_extended("Hello，world"), "Hello,world")
        self.assertEqual(_normalize_punctuation_extended("Test；case"), "Test;case")
        self.assertEqual(_normalize_punctuation_extended("A：B"), "A:B")
        self.assertEqual(_normalize_punctuation_extended("What？"), "What?")
        self.assertEqual(_normalize_punctuation_extended("Wow！"), "Wow!")
        self.assertEqual(_normalize_punctuation_extended("Func（x）"), "Func(x)")

    def test_period_cjk_context(self):
        # 句号：与汉字相邻的半角点 → 全角。
        self.assertEqual(_normalize_punctuation_extended("你好.世界"), "你好\u3002世界")
        self.assertEqual(_normalize_punctuation_extended("结束."), "结束\u3002")
        # 小数点 / 英文语境不误伤
        self.assertEqual(_normalize_punctuation_extended("1.5"), "1.5")
        self.assertEqual(_normalize_punctuation_extended("test.OK"), "test.OK")

    def test_quotes_cjk_content(self):
        # 引号内容含汉字 → 中文弯引号
        self.assertEqual(
            _normalize_punctuation_extended('他说"你好"'),
            "他说\u201c你好\u201d",
        )
        self.assertEqual(
            _normalize_punctuation_extended("它'是'什么"),
            "它\u2018是\u2019什么",
        )

    def test_quotes_latin_content_preserved(self):
        # 引号内容为纯英文 → 保持 ASCII 引号（不误伤英文引语）
        self.assertEqual(_normalize_punctuation_extended('他说 "hello"'), '他说 "hello"')
        self.assertEqual(_normalize_punctuation_extended('He said "hi"'), 'He said "hi"')
        self.assertEqual(_normalize_punctuation_extended("It 'is' good"), "It 'is' good")

    def test_extended_symbols_cjk_context(self):
        # 扩展符号在中文上下文：半角→全角
        self.assertEqual(_normalize_punctuation_extended("见[附录]"), "见【附录】")
        self.assertEqual(_normalize_punctuation_extended("等等..."), "等等……")
        self.assertEqual(_normalize_punctuation_extended("然后--继续"), "然后——继续")
        self.assertEqual(_normalize_punctuation_extended("大约~10个"), "大约～10个")

    def test_extended_symbols_latin_context(self):
        # 扩展符号在英文/数字上下文：全角→半角
        self.assertEqual(_normalize_punctuation_extended("Wait……"), "Wait...")
        self.assertEqual(_normalize_punctuation_extended("A——B"), "A--B")
        self.assertEqual(_normalize_punctuation_extended("About～10"), "About~10")

    def test_mixed_context_preserved(self):
        # 混合上下文：中文部分转全角，英文部分保持半角
        # 逗号转全角，空格保留（当前实现不删除标点后空格）
        self.assertEqual(_normalize_punctuation_extended('你好, "world"'), '你好， "world"')
        self.assertEqual(_normalize_punctuation_extended("See [附录] and [appendix]"), "See 【附录】 and [appendix]")

    def test_ellipsis_normalization_single_line(self):
        # 一行内多个省略号 → 只保留第一对标准省略号，后续标记删除，文字保留
        self.assertEqual(_normalize_punctuation_extended("啊...吧...次"), "啊……吧次")
        self.assertEqual(_normalize_punctuation_extended("啊……吧……次"), "啊……吧次")
        self.assertEqual(_normalize_punctuation_extended("测试...更多...结束"), "测试……更多结束")
        self.assertEqual(_normalize_punctuation_extended("开始...中间...结束..."), "开始……中间结束")

    def test_ellipsis_cross_lines_independent(self):
        # 跨行处理：各行独立归一化
        text = "第一行...\n第二行...\n第三行..."
        expected = "第一行……\n第二行……\n第三行……"
        self.assertEqual(_normalize_punctuation_extended(text), expected)

    def test_no_ellipsis_unchanged(self):
        self.assertEqual(_normalize_punctuation_extended("没有省略号"), "没有省略号")


class TestNormalizeEllipsis(unittest.TestCase):
    """_normalize_ellipsis：省略号长度归一化 + 多对合并为一对（文字不丢）。"""

    def test_multiple_ascii_ellipsis(self):
        # 多对省略号：保留第一对，后续省略号标记删除，两侧文字全部保留
        self.assertEqual(_normalize_ellipsis("a...b...c"), "a……bc")

    def test_multiple_unicode_ellipsis(self):
        self.assertEqual(_normalize_ellipsis("a……b……c"), "a……bc")

    def test_mixed_ellipsis(self):
        self.assertEqual(_normalize_ellipsis("test...more...end"), "test……moreend")

    def test_single_ellipsis_standardized(self):
        self.assertEqual(_normalize_ellipsis("只有……一个"), "只有……一个")
        self.assertEqual(_normalize_ellipsis("只有...一个"), "只有……一个")

    def test_long_dot_run_collapsed(self):
        # 6 个点是一个省略号序列 → 归一为 ……（长度统一）
        self.assertEqual(_normalize_ellipsis("等等......好"), "等等……好")

    def test_adjacent_ellipsis_noise_collapsed(self):
        # 纯省略号噪声（连续全角省略号点）→ 单对
        self.assertEqual(_normalize_ellipsis("………………"), "……")
        self.assertEqual(_normalize_ellipsis("......"), "……")

    def test_no_ellipsis_unchanged(self):
        self.assertEqual(_normalize_ellipsis("没有省略号"), "没有省略号")

    def test_cross_lines_independent(self):
        text = "第一行...\n第二行..."
        expected = "第一行……\n第二行……"
        self.assertEqual(_normalize_ellipsis(text), expected)


if __name__ == "__main__":
    unittest.main()
