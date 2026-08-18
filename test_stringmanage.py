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


if __name__ == "__main__":
    unittest.main()
