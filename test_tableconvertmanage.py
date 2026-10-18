# -*- coding: utf-8 -*-
"""文字表格 → 程序原生表格 转换器的测试。

覆盖设计文档规格 A（Markdown 管道表）、B（ASCII 画框表）、C（空格/制表符对齐列，
仅手动）与 D（既存 HTML 表格跳过），以及幂等与 OCR 常见噪声。
"""

import unittest

from tableconvertmanage import (
    convert_lines,
    convert_text_tables,
    table_html_to_pipe_text,
)


BASIC_IN = "| A | B |\n| --- | --- |\n| C | D |"
BASIC_OUT = (
    "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
    "<tbody><tr><td>C</td><td>D</td></tr></tbody></table>"
)

# 用户真实例子的简化版：表题行 8 个单元、分隔行 7 格、数据行 5 格。
CAP_IN = (
    "1951年各省区市高级农业生产合作社组织情况 | | 社数 | 户数 | 每社平均户数 | "
    "占总农户的% | 耕地面积（亩） | 占总耕地面积% |\n"
    "|---|---|---|---|---|---|---|\n"
    "| 全国总计〔1〕30 | 30。00 | 0。00003 | 1152 | 0。00007 |"
)


class TestMdPipeTable(unittest.TestCase):
    def test_md_basic(self):
        self.assertEqual(convert_text_tables(BASIC_IN), BASIC_OUT)

    def test_no_leading_trailing_pipes(self):
        self.assertEqual(convert_text_tables("A | B\n--- | ---\nC | D"), BASIC_OUT)

    def test_align_classes(self):
        out = convert_text_tables("| A | B | C |\n| :- | -: | :-: |\n| 1 | 2 | 3 |")
        self.assertIn('<th class="ptoe-align-left">A</th>', out)
        self.assertIn('<th class="ptoe-align-right">B</th>', out)
        self.assertIn('<th class="ptoe-align-center">C</th>', out)
        self.assertIn('<td class="ptoe-align-left">1</td>', out)

    def test_escaped_pipe_in_cell(self):
        out = convert_text_tables("| A \\| B | C |\n| --- | --- |\n| x | y |")
        self.assertIn("<th>A | B</th>", out)

    def test_fullwidth_pipe(self):
        out = convert_text_tables("｜ A ｜ B ｜\n｜ --- ｜ --- ｜\n｜ C ｜ D ｜")
        self.assertEqual(out, BASIC_OUT)

    def test_caption_via_separator_count(self):
        out = convert_text_tables(CAP_IN)
        lines = out.split("\n")
        self.assertEqual(
            lines[0], "<p>1951年各省区市高级农业生产合作社组织情况</p>"
        )
        # 真实空列（省市名列表头）必须保留，不能被当噪声删掉
        self.assertIn("<th></th>", lines[1])
        # 数据行 5 格 < 7 格，补空单元格
        self.assertTrue(
            lines[1].endswith(
                "<td>0。00007</td><td></td><td></td></tr></tbody></table>"
            ),
            lines[1],
        )

    def test_no_caption_when_counts_match(self):
        out = convert_text_tables("社数 | 户数\n--- | ---\n1 | 2")
        self.assertNotIn("<p>", out)
        self.assertIn("<th>社数</th>", out)

    def test_overflow_merged_into_last_cell(self):
        out = convert_text_tables("| h1 | h2 |\n| --- | --- |\n| a | b | c |")
        self.assertIn("<td>a</td><td>bc</td>", out)

    def test_no_separator_unchanged(self):
        src = "A | B\nC | D"
        self.assertEqual(convert_text_tables(src), src)

    def test_manual_no_separator_first_row_header(self):
        out = convert_text_tables("| A | B |\n| C | D |", require_separator=False)
        self.assertIn("<th>A</th><th>B</th>", out)
        self.assertIn("<td>C</td><td>D</td>", out)

    def test_prose_with_pipe_unchanged(self):
        src = "他说 | 然后走了"
        self.assertEqual(convert_text_tables(src), src)

    def test_existing_table_untouched(self):
        src = "<table><tbody><tr><td>a</td></tr></tbody></table>"
        self.assertEqual(convert_text_tables(src), src)

    def test_idempotent(self):
        self.assertEqual(convert_text_tables(BASIC_OUT), BASIC_OUT)
        once = convert_text_tables(BASIC_IN)
        self.assertEqual(convert_text_tables(once), once)

    def test_blank_line_between_header_and_separator(self):
        out = convert_text_tables("| A | B |\n\n| --- | --- |\n| C | D |")
        self.assertEqual(out, BASIC_OUT)

    def test_table_at_page_start_and_end(self):
        self.assertEqual(
            convert_text_tables(BASIC_IN + "\n正文一"), BASIC_OUT + "\n正文一"
        )
        self.assertEqual(
            convert_text_tables("正文一\n" + BASIC_IN), "正文一\n" + BASIC_OUT
        )

    def test_surrounding_prose_not_swallowed(self):
        out = convert_text_tables("前言\n" + BASIC_IN + "\n后记")
        self.assertEqual(out, "前言\n" + BASIC_OUT + "\n后记")

    def test_two_adjacent_tables_not_merged(self):
        out = convert_text_tables(BASIC_IN + "\n说明\n" + BASIC_IN)
        self.assertEqual(out.count("<table"), 2)
        self.assertEqual(out, BASIC_OUT + "\n说明\n" + BASIC_OUT)

    def test_cell_html_escaped(self):
        out = convert_text_tables("| a<b>&c | d |\n| --- | --- |\n| 1 | 2 |")
        self.assertIn("<th>a&lt;b&gt;&amp;c</th>", out)

    def test_escaped_pipe_at_line_edges(self):
        out = convert_text_tables("| \\|a | b |\n| --- | --- |\n| 1 | 2 |")
        self.assertIn("<th>|a</th>", out)


class TestIndentedPipeRows(unittest.TestCase):
    """终审 F1：管道行/分隔行带前导空白（OCR 缩进）时列切分不得错位。"""

    def test_leading_whitespace_row_keeps_columns(self):
        out = convert_text_tables("| A | B |\n| --- | --- |\n   | C | D |")
        self.assertIn("<td>C</td><td>D</td>", out)
        self.assertNotIn("<td>CD</td>", out)

    def test_indented_separator_recognized(self):
        out = convert_text_tables("\t| A | B |\n\t| --- | --- |\n\t| C | D |")
        self.assertIn("<th>A</th><th>B</th>", out)
        self.assertIn("<td>C</td><td>D</td>", out)

    def test_indented_chinese_rows(self):
        src = "| 姓名 | 年龄 |\n| --- | --- |\n | 张三 | 20 |\n | 李四 | 30 |"
        out = convert_text_tables(src)
        self.assertIn("<td>张三</td><td>20</td>", out)
        self.assertIn("<td>李四</td><td>30</td>", out)


class TestBoxTable(unittest.TestCase):
    def test_unicode_box(self):
        src = "┌───┬───┐\n│ A │ B │\n├───┼───┤\n│ C │ D │\n└───┴───┘"
        out = convert_text_tables(src)
        self.assertIn("<th>A</th><th>B</th>", out)
        self.assertIn("<td>C</td><td>D</td>", out)

    def test_ascii_plus(self):
        src = "+---+---+\n| A | B |\n+---+---+\n| C | D |\n+---+---+"
        out = convert_text_tables(src)
        self.assertIn("<th>A</th><th>B</th>", out)

    def test_box_not_mistaken_for_prose(self):
        self.assertEqual(convert_text_tables("普通一行──没有竖边框"), "普通一行──没有竖边框")


class TestSpaceAligned(unittest.TestCase):
    SRC = "省份    数值    数值\n河北    1       2\n山西    3       4"

    def test_off_by_default(self):
        self.assertEqual(convert_text_tables(self.SRC), self.SRC)

    def test_on_when_enabled(self):
        out = convert_text_tables(self.SRC, allow_space_aligned=True)
        self.assertIn("<th>省份</th><th>数值</th><th>数值</th>", out)
        self.assertIn("<td>河北</td><td>1</td><td>2</td>", out)

    def test_single_space_not_a_table(self):
        src = "一行 只有 单空格"
        self.assertEqual(convert_text_tables(src, allow_space_aligned=True), src)

    def test_tab_separated(self):
        out = convert_text_tables("A\tB\n1\t2", allow_space_aligned=True)
        self.assertIn("<th>A</th><th>B</th>", out)

    def test_single_line_not_a_table(self):
        src = "省份    数值"
        self.assertEqual(convert_text_tables(src, allow_space_aligned=True), src)


class TestConvertLinesChunks(unittest.TestCase):
    """convert_lines：输出行与 convert_text_tables 一致，并回传「源行范围 → 表格 HTML」。"""

    def test_output_matches_text_api(self):
        src = "开篇\n" + BASIC_IN + "\n结尾"
        out, _chunks = convert_lines(src.split("\n"))
        self.assertEqual("\n".join(out), convert_text_tables(src))

    def test_chunk_range_covers_source_lines(self):
        lines = ["开篇", "| A | B |", "| --- | --- |", "| C | D |", "结尾"]
        out, chunks = convert_lines(lines)
        self.assertEqual(len(chunks), 1)
        start, end, html = chunks[0]
        self.assertEqual((start, end), (1, 4))  # 输入行 1..3（含表头+分隔+数据）
        self.assertEqual(out[start], html)  # chunks 索引即输出行索引（单行 HTML）
        self.assertEqual(html, BASIC_OUT)

    def test_no_table_no_chunks(self):
        out, chunks = convert_lines(["甲", "乙"])
        self.assertEqual((out, chunks), (["甲", "乙"], []))

    def test_manual_mode_space_aligned(self):
        lines = ["A  B", "1  2"]
        out, chunks = convert_lines(lines, allow_space_aligned=True)
        self.assertEqual(len(chunks), 1)
        self.assertEqual((chunks[0][0], chunks[0][1]), (0, 2))
        self.assertIn("<th>A</th><th>B</th>", out[0])

    def test_require_separator_false_takes_header(self):
        lines = ["| A | B |", "| C | D |"]
        _out, chunks = convert_lines(lines, require_separator=False)
        self.assertEqual(len(chunks), 1)
        self.assertNotEqual(convert_lines(lines)[1], chunks)  # 自动档不识别

    def test_box_table_chunk(self):
        lines = ["┌──┬──┐", "│A │B │", "├──┼──┤", "│1 │2 │", "└──┴──┘"]
        _out, chunks = convert_lines(lines)
        self.assertEqual(len(chunks), 1)
        self.assertEqual((chunks[0][0], chunks[0][1]), (0, 5))


class TestTableHtmlToPipeText(unittest.TestCase):
    """table_html_to_pipe_text：原生 <table> → Markdown 管道文字（「表格转文本」）。"""

    def test_basic_header_and_rows(self):
        html = (
            "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
            "<tbody><tr><td>C</td><td>D</td></tr></tbody></table>"
        )
        self.assertEqual(
            table_html_to_pipe_text(html), "| A | B |\n| --- | --- |\n| C | D |"
        )

    def test_roundtrip_back_to_table(self):
        html = (
            '<table><thead><tr><th>省</th>'
            '<th class="ptoe-align-right">人口</th></tr></thead>'
            "<tbody><tr><td>广东</td><td>1.2亿</td></tr></tbody></table>"
        )
        text = table_html_to_pipe_text(html)
        self.assertEqual(text, "| 省 | 人口 |\n| --- | ---: |\n| 广东 | 1.2亿 |")
        self.assertIn("<th>省</th>", convert_text_tables(text))

    def test_alignment_from_classes(self):
        html = (
            "<table><tr>"
            '<th class="ptoe-align-left">L</th>'
            '<th class="ptoe-align-center">C</th>'
            '<th class="ptoe-align-right">R</th>'
            "</tr><tr><td>1</td><td>2</td><td>3</td></tr></table>"
        )
        self.assertEqual(table_html_to_pipe_text(html).split("\n")[1], "| :--- | :---: | ---: |")

    def test_br_becomes_space_and_whitespace_collapsed(self):
        html = "<table><tr><td>a<br>b</td><td>  c   d  </td></tr></table>"
        self.assertEqual(table_html_to_pipe_text(html).split("\n")[0], "| a b | c d |")

    def test_entities_decoded(self):
        html = "<table><tr><td>&amp;</td><td>&lt;x&gt;</td></tr></table>"
        self.assertEqual(table_html_to_pipe_text(html).split("\n")[0], "| & | <x> |")

    def test_pipe_in_cell_escaped(self):
        html = (
            "<table><tr><td>a|b</td><td>c</td></tr>"
            "<tr><td>1</td><td>2</td></tr></table>"
        )
        text = table_html_to_pipe_text(html)
        self.assertEqual(text.split("\n")[0], "| a\\|b | c |")
        self.assertIn("<th>a|b</th>", convert_text_tables(text))

    def test_ragged_rows_padded(self):
        html = (
            "<table><tr><td>a</td><td>b</td></tr>"
            "<tr><td>c</td></tr></table>"
        )
        self.assertEqual(table_html_to_pipe_text(html), "| a | b |\n| --- | --- |\n| c |  |")

    def test_single_row_and_empty(self):
        self.assertEqual(
            table_html_to_pipe_text("<table><tr><td>only</td></tr></table>"),
            "| only |\n| --- |",
        )
        self.assertEqual(table_html_to_pipe_text(""), "")
        self.assertEqual(table_html_to_pipe_text("<p>无表格</p>"), "")
        self.assertEqual(table_html_to_pipe_text("<table></table>"), "")

    def test_only_first_table_used(self):
        html = (
            "<table><tr><td>t1</td></tr></table>"
            "<table><tr><td>t2</td></tr></table>"
        )
        self.assertEqual(table_html_to_pipe_text(html), "| t1 |\n| --- |")

    def test_nested_table_text_merged_into_cell(self):
        html = (
            "<table><tr><td>外<table><tr><td>内</td></tr></table></td>"
            "<td>x</td></tr></table>"
        )
        self.assertEqual(table_html_to_pipe_text(html).split("\n")[0], "| 外内 | x |")


class TestInlineGluedRows(unittest.TestCase):
    """无换行表格：一行里用 ``||`` 把多行拼起来（2026-10 用户需求）。

    规格：`| 省 | 人口 || 广东 | 1.2亿 |` → 2 列表格（表头 省/人口 + 1 数据行）；
    拼行本身即表格结构的强证据，故无 Markdown 分隔行也成表；chunks 折算回源行区间。
    保护：单列片段（GFM 合法空单元 `| a || b |`）、不以竖线开头的正文行不拆。
    """

    def test_glued_two_rows_without_separator(self):
        out = convert_text_tables("| 省 | 人口 || 广东 | 1.2亿 |")
        self.assertEqual(out.count("<tr>"), 2)
        self.assertIn("<th>省</th><th>人口</th>", out)
        self.assertIn("<td>广东</td><td>1.2亿</td>", out)

    def test_glued_three_rows(self):
        out = convert_text_tables("| A | B || C | D || E | F |")
        self.assertEqual(out.count("<tr>"), 3)
        self.assertIn("<td>C</td><td>D</td>", out)
        self.assertIn("<td>E</td><td>F</td>", out)

    def test_glued_with_separator_inside(self):
        out = convert_text_tables("| A | B || --- | --- || C | D |")
        self.assertIn("<th>A</th><th>B</th>", out)
        self.assertIn("<td>C</td><td>D</td>", out)

    def test_glued_body_after_normal_separator_keeps_one_table(self):
        out = convert_text_tables("| A | B |\n| --- | --- |\n| c | d || e | f |")
        self.assertEqual(out.count("<table>"), 1)
        self.assertIn("<td>c</td><td>d</td>", out)
        self.assertIn("<td>e</td><td>f</td>", out)

    def test_fullwidth_glued(self):
        out = convert_text_tables("｜ A ｜ B ｜｜ C ｜ D ｜")
        self.assertIn("<td>C</td><td>D</td>", out)

    def test_real_empty_cell_not_split(self):
        # 单列片段（GFM 合法空单元）不拆：仍按普通管道行处理（自动档无分隔行 → 原样）
        self.assertEqual(convert_text_tables("| a || b |"), "| a || b |")

    def test_prose_double_pipe_unchanged(self):
        self.assertEqual(convert_text_tables("a | b || c | d"), "a | b || c | d")

    def test_chunks_map_back_to_source_line(self):
        lines = ["标题", "| 省 | 人口 || 广东 | 1.2亿 |", "尾行"]
        out, chunks = convert_lines(lines)
        self.assertEqual(len(chunks), 1)
        start, end, html = chunks[0]
        self.assertEqual((start, end), (1, 2))  # 只替换源行 1
        self.assertIn("<table>", html)
        self.assertEqual([out[0], out[2]], ["标题", "尾行"])

    def test_two_glued_lines_merge_into_one_table(self):
        out, chunks = convert_lines(["| A | B || C | D |", "| E | F || G | H |"])
        self.assertEqual(len(chunks), 1)
        self.assertEqual((chunks[0][0], chunks[0][1]), (0, 2))
        self.assertEqual(out[0].count("<tr>"), 4)  # 表头 + 3 数据行

    def test_single_pipe_line_still_not_a_table(self):
        self.assertEqual(convert_text_tables("| a | b |"), "| a | b |")


if __name__ == "__main__":
    unittest.main()
