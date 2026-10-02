"""test_rulemanage.py — unittest suite for rulemanage module.

Covers:
- HTML parsing/serialization roundtrip and escaping
- Each format op (bold, italic, heading, align, note, citation, merge, remove, no_bold)
- Three scopes (selection, paragraph, page)
- match_formats, group_formats, target (before/after/between)
- mode first/all
- Conflict first-wins
- Multiple matches in same block: note idempotent
- img preservation
- Chinese text offset correctness
"""

import re
import unittest
from rulemanage import (
    parse_html,
    serialize_html,
    collect_text_nodes,
    apply_rules,
    find_matches,
    VALID_FORMAT_OPS,
    parse_regex_pattern,
    Rule,
    Condition,
    eval_format_rule,
    op_group,
    ops_conflict,
    _is_dangerous,
    normalize_char_style,
    char_style_get,
    CHAR_FONT_STACKS,
    CHAR_STYLE_PROPS,
    ALLOWED_SPAN_CLASSES,
    INLINE_FORMAT_CLASSES,
    _is_divider_block,
    _is_caption_block,
    apply_block_format,
    ElementNode,
    TextNode,
)


class TestParseSerialize(unittest.TestCase):
    """解析/序列化往返与转义测试。"""

    def test_plain_text(self):
        html = "你好世界"
        root = parse_html(html)
        out = serialize_html(root)
        self.assertEqual(out, "<p>你好世界</p>")

    def test_html_escaping(self):
        html = "a < b & c"
        root = parse_html(html)
        out = serialize_html(root)
        self.assertIn("&", out)
        self.assertIn("<", out)

    def test_bold_italic(self):
        html = "<p><b>粗</b> <i>斜</i></p>"
        root = parse_html(html)
        out = serialize_html(root)
        self.assertIn("<strong>粗</strong>", out)
        self.assertIn("<em>斜</em>", out)

    def test_headings(self):
        html = "<h2>标题</h2><p>正文</p>"
        root = parse_html(html)
        out = serialize_html(root)
        self.assertIn("<h2>标题</h2>", out)
        self.assertIn("<p>正文</p>", out)

    def test_img_preserved(self):
        html = '<p><img src="data:image/png;base64,AAA" alt="图" class="ptoe-img-full"></p>'
        root = parse_html(html)
        out = serialize_html(root)
        self.assertIn('src="data:image/png;base64,AAA"', out)
        self.assertIn('alt="图"', out)
        self.assertIn('class="ptoe-img-full"', out)

    def test_img_dropped_without_src(self):
        html = '<p><img alt="图"></p>'
        root = parse_html(html)
        out = serialize_html(root)
        self.assertNotIn("<img", out)

    def test_span_marker_preserved(self):
        html = '<p>文本<span data-ptoe-marker="join" class="ptoe-marker">join</span>更多</p>'
        root = parse_html(html)
        out = serialize_html(root)
        self.assertIn('data-ptoe-marker="join"', out)
        self.assertIn('class="ptoe-marker"', out)

    def test_unknown_tags_stripped(self):
        html = '<p style="color:red">文本</p><script>alert(1)</script><span>span内容</span>'
        root = parse_html(html)
        out = serialize_html(root)
        self.assertNotIn("style", out)
        self.assertNotIn("<script", out)
        self.assertIn("文本", out)
        self.assertIn("span内容", out)

    def test_block_class_preserved(self):
        html = '<p class="ptoe-note ptoe-align-center">注释</p>'
        root = parse_html(html)
        out = serialize_html(root)
        self.assertIn('class="ptoe-note ptoe-align-center"', out)


class TestTextIndexing(unittest.TestCase):
    """文本索引：中文文本偏移正确性。"""

    def test_chinese_offsets(self):
        html = "<p>你好世界</p><p>测试</p>"
        root = parse_html(html)
        text, nodes = collect_text_nodes(root)
        self.assertEqual(text, "你好世界测试")
        # 验证偏移
        self.assertEqual(nodes[0].start, 0)
        self.assertEqual(nodes[0].end, 4)  # "你好世界" 4 字符
        self.assertEqual(nodes[1].start, 4)
        self.assertEqual(nodes[1].end, 6)  # "测试" 2 字符

    def test_mixed_content_offsets(self):
        html = "<p>文本<strong>加粗</strong>继续</p>"
        root = parse_html(html)
        text, nodes = collect_text_nodes(root)
        self.assertEqual(text, "文本加粗继续")
        # 文本节点应该被正确分割
        self.assertGreaterEqual(len(nodes), 3)


class TestParseRegexPattern(unittest.TestCase):
    """正则解析 /pattern/flags 语法。"""

    def test_simple_pattern(self):
        pattern, flags = parse_regex_pattern("hello")
        self.assertEqual(pattern, "hello")
        self.assertEqual(flags, "")

    def test_with_flags(self):
        pattern, flags = parse_regex_pattern("/hello/gi")
        self.assertEqual(pattern, "hello")
        self.assertEqual(flags, "gi")

    def test_complex_pattern(self):
        pattern, flags = parse_regex_pattern("/\\d+/gm")
        self.assertEqual(pattern, "\\d+")
        self.assertEqual(flags, "gm")


class TestConflictModel(unittest.TestCase):
    """冲突模型 first-wins。"""

    def test_block_tag_conflict(self):
        self.assertTrue(ops_conflict("p", "heading1"))
        self.assertTrue(ops_conflict("heading1", "heading2"))
        self.assertTrue(ops_conflict("p", "heading3"))

    def test_align_conflict(self):
        self.assertTrue(ops_conflict("align_left", "align_center"))
        self.assertTrue(ops_conflict("align_center", "align_right"))
        self.assertTrue(ops_conflict("align_left", "align_right"))

    def test_remove_conflicts_all(self):
        self.assertTrue(ops_conflict("remove", "bold"))
        self.assertTrue(ops_conflict("remove", "italic"))
        self.assertTrue(ops_conflict("remove", "p"))
        self.assertTrue(ops_conflict("remove", "align_left"))

    def test_no_conflict(self):
        self.assertFalse(ops_conflict("bold", "italic"))
        self.assertFalse(ops_conflict("bold", "note"))
        self.assertFalse(ops_conflict("italic", "note"))
        self.assertFalse(ops_conflict("bold", "citation"))

    def test_same_op_no_conflict(self):
        self.assertFalse(ops_conflict("bold", "bold"))
        self.assertFalse(ops_conflict("p", "p"))

    def test_op_group(self):
        self.assertEqual(op_group("p"), "block_tag")
        self.assertEqual(op_group("heading1"), "block_tag")
        self.assertEqual(op_group("align_left"), "align")
        self.assertEqual(op_group("merge"), "merge")
        self.assertIsNone(op_group("bold"))
        self.assertIsNone(op_group("note"))


class TestRuleEvaluation(unittest.TestCase):
    """规则求值。"""

    def test_mode_first_stops_at_first_match(self):
        rule = Rule(
            id="r1",
            name="Test",
            mode="first",
            conditions=[
                Condition("contains", "A", "page", ["bold"]),
                Condition("contains", "B", "page", ["italic"]),
            ],
        )
        result = eval_format_rule(rule, "A B C")
        # first 模式：首个匹配条件生效即停
        self.assertEqual(result.pattern_conds[0].formats, ["bold"])
        self.assertEqual(len(result.pattern_conds), 1)

    def test_mode_all_applies_all(self):
        rule = Rule(
            id="r1",
            name="Test",
            mode="all",
            conditions=[
                Condition("contains", "A", "page", ["bold"]),
                Condition("contains", "B", "page", ["italic"]),
            ],
        )
        result = eval_format_rule(rule, "A B C")
        # all 模式：全部匹配条件按序各自应用
        self.assertEqual(len(result.pattern_conds), 2)
        self.assertEqual(result.pattern_conds[0].formats, ["bold"])
        self.assertEqual(result.pattern_conds[1].formats, ["italic"])

    def test_none_filtered(self):
        rule = Rule(
            id="r1",
            name="Test",
            mode="first",
            conditions=[
                Condition("contains", "A", "page", ["none"]),
            ],
        )
        result = eval_format_rule(rule, "A B C")
        # none 单独存在：formats 全为 none → 仍是按匹配应用的条件（pattern 非空）
        self.assertEqual(len(result.pattern_conds), 1)
        self.assertEqual(result.pattern_conds[0].formats, ["none"])

    def test_target_before_after_between(self):
        rule = Rule(
            id="r1",
            name="Test",
            mode="first",
            conditions=[
                Condition("regex", "标题", "page", ["bold"], target="before"),
                Condition("regex", "正文", "page", ["italic"], target="after"),
                Condition("regex", "开始", "page", ["note"], target="between", between_end_pattern="结束"),
            ],
        )
        result = eval_format_rule(rule, "标题 正文 开始 中间 结束")
        self.assertEqual(len(result.target_conds), 3)
        self.assertEqual(result.target_conds[0].target, "before")
        self.assertEqual(result.target_conds[1].target, "after")
        self.assertEqual(result.target_conds[2].target, "between")

    def test_group_formats(self):
        rule = Rule(
            id="r1",
            name="Test",
            mode="first",
            conditions=[
                Condition("regex", "(\\d+)-(\\d+)", "page", [], group_formats=[["bold"], ["italic"]]),
            ],
        )
        result = eval_format_rule(rule, "123-456")
        self.assertEqual(len(result.group_conds), 1)
        self.assertEqual(result.group_conds[0].group_formats, [["bold"], ["italic"]])

    def test_match_formats(self):
        rule = Rule(
            id="r1",
            name="Test",
            mode="first",
            conditions=[
                Condition("regex", "测试", "page", [], match_formats=[["bold"], ["italic"]]),
            ],
        )
        result = eval_format_rule(rule, "测试 测试 测试")
        self.assertEqual(len(result.match_conds), 1)
        self.assertEqual(result.match_conds[0].match_formats, [["bold"], ["italic"]])


class TestApplyRules(unittest.TestCase):
    """apply_rules 主入口测试。"""

    def test_bold_inline(self):
        html = "<p>你好世界</p>"
        rules = [{
            "id": "r1",
            "name": "Bold测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "你好",
                "scope": "page",
                "formats": ["bold"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        self.assertIn("<strong>你好</strong>", new_html)

    def test_italic_inline(self):
        html = "<p>你好世界</p>"
        rules = [{
            "id": "r1",
            "name": "Italic测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "世界",
                "scope": "page",
                "formats": ["italic"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        self.assertIn("<em>世界</em>", new_html)

    def test_heading_block(self):
        html = "<p>标题内容</p>"
        rules = [{
            "id": "r1",
            "name": "Heading测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "标题",
                "scope": "page",
                "formats": ["heading1"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        self.assertIn("<h1>标题内容</h1>", new_html)

    def test_align_block(self):
        html = "<p>居中文本</p>"
        rules = [{
            "id": "r1",
            "name": "Align 测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "居中",
                "scope": "page",
                "formats": ["align_center"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # Align uses block-level ptoe-align-* class (text-align on inline span is CSS no-op)
        self.assertIn('ptoe-align-center', new_html)

    def test_note_block(self):
        html = "<p>注释文本</p>"
        rules = [{
            "id": "r1",
            "name": "Note测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "注释",
                "scope": "page",
                "formats": ["note"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        self.assertIn('class="ptoe-note"', new_html)

    def test_citation_block(self):
        html = "<p>引用文本</p>"
        rules = [{
            "id": "r1",
            "name": "Citation测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "引用",
                "scope": "page",
                "formats": ["citation"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        self.assertIn('class="ptoe-citation"', new_html)

    def test_citation_partial_selection_inline(self):
        """选区只覆盖段内部分字符：引用改为行内 span 包裹选中文字，
        整段不加块级类，选区外前后缀保持原样（2026-09-22 修复）。"""
        html = "<p>前缀引用内容后缀</p>"
        rules = [{
            "id": "r1",
            "name": "引用部分选区",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "",
                "scope": "selection",
                "formats": ["citation"],
            }],
        }]
        # 全文 8 字："前缀引用内容后缀" → 选中 "引用内容"（偏移 2-6）
        new_html, err = apply_rules(html, rules, all_rules=True, sel_start=2, sel_end=6)
        self.assertIsNone(err)
        self.assertEqual(
            new_html,
            '<p>前缀<span class="ptoe-citation">引用内容</span>后缀</p>',
        )
        # 无块级类（<p> 不带 class）；选区外前后缀均未被包裹
        self.assertNotIn('<p class="ptoe-citation">', new_html)
        self.assertEqual(new_html.count('<span class="ptoe-citation">'), 1)

    def test_note_partial_selection_inline(self):
        """注释同引用：选区只覆盖段内部分字符时行内 span 包裹，整段不加块级类。"""
        html = "<p>前缀注释内容后缀</p>"
        rules = [{
            "id": "r1",
            "name": "注释部分选区",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "",
                "scope": "selection",
                "formats": ["note"],
            }],
        }]
        # 选中 "注释内容"（偏移 2-6）
        new_html, err = apply_rules(html, rules, all_rules=True, sel_start=2, sel_end=6)
        self.assertIsNone(err)
        self.assertEqual(
            new_html,
            '<p>前缀<span class="ptoe-note">注释内容</span>后缀</p>',
        )
        self.assertNotIn('<p class="ptoe-note">', new_html)

    def test_citation_full_paragraph_selection_keeps_block_class(self):
        """选区等于整段文字：保持块级类路径（<p class="ptoe-citation">），不产生行内 span。"""
        html = "<p>整段引用</p>"
        rules = [{
            "id": "r1",
            "name": "引用整段选区",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "",
                "scope": "selection",
                "formats": ["citation"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True, sel_start=0, sel_end=4)
        self.assertIsNone(err)
        self.assertEqual(new_html, '<p class="ptoe-citation">整段引用</p>')
        self.assertNotIn('<span class="ptoe-citation">', new_html)

    def test_citation_multi_block_partial_range_inline(self):
        """选区从块1中部跨到块2中部：两个块都只在选中部分套行内 span，块级类都不加。"""
        html = "<p>甲块内容</p><p>乙块内容</p>"
        rules = [{
            "id": "r1",
            "name": "引用跨块部分选区",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "",
                "scope": "selection",
                "formats": ["citation"],
            }],
        }]
        # page_text = "甲块内容乙块内容"（8 字）→ 选中 [3,5) = 块1末字"容" + 块2首字"乙"
        new_html, err = apply_rules(html, rules, all_rules=True, sel_start=3, sel_end=5)
        self.assertIsNone(err)
        self.assertIn('<span class="ptoe-citation">容</span>', new_html)
        self.assertIn('<span class="ptoe-citation">乙</span>', new_html)
        self.assertNotIn('<p class="ptoe-citation">', new_html)
        # 选区外文本完整保留
        self.assertIn('<p>甲块内', new_html)
        self.assertIn('块内容</p>', new_html)

    def test_merge_blocks(self):
        html = "<p>第一段</p><p>第二段</p>"
        rules = [{
            "id": "r1",
            "name": "Merge测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "第一段",
                "scope": "page",
                "formats": ["merge"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # merge 应该将两个段落合并
        self.assertIn("第一段", new_html)
        self.assertIn("第二段", new_html)
        # 应该只有一个 <p> 标签包含两者
        self.assertEqual(new_html.count("<p>"), 1)

    def test_merge_all_selected_blocks(self):
        # 选区跨多个段落时，merge 应将全部选中块合并为一段（而非只并相邻一对）
        html = "<p>第一段</p><p>第二段</p><p>第三段</p>"
        rules = [{
            "id": "r1",
            "name": "Merge全部测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "第一段",
                "scope": "selection",
                "formats": ["merge"],
            }],
        }]
        # 整页纯文本 = 第一段第二段第三段（9 字），选区覆盖全部三段
        new_html, err = apply_rules(html, rules, all_rules=True, sel_start=0, sel_end=9)
        self.assertIsNone(err)
        self.assertEqual(new_html.count("<p>"), 1)
        for seg in ("第一段", "第二段", "第三段"):
            self.assertIn(seg, new_html)

    def test_remove_format(self):
        html = "<p><strong>粗体</strong> <em>斜体</em></p>"
        rules = [{
            "id": "r1",
            "name": "Remove测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "粗体",
                "scope": "page",
                "formats": ["remove"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # remove 应该移除 strong/em
        self.assertNotIn("<strong>", new_html)
        self.assertNotIn("<em>", new_html)

    def test_no_bold(self):
        html = "<p><strong>粗体文本</strong></p>"
        rules = [{
            "id": "r1",
            "name": "NoBold测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "粗体",
                "scope": "page",
                "formats": ["no_bold"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # no_bold 应该移除 strong 但保留文本
        self.assertNotIn("<strong>", new_html)
        self.assertIn("粗体文本", new_html)

    def test_identical_siblings_no_recursion(self):
        # 回归：结构完全相同的兄弟段落 + parent 回指曾使 dataclass 结构化
        # __eq__ 在 siblings.index() 中无限递归（RecursionError → 400）。
        # 节点相等性现为对象身份，块级 op 应正常应用。
        html = "<p>同</p><p>同</p><p>同</p>"
        rules = [{
            "id": "r1",
            "name": "排版测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "",
                "scope": "page",
                "formats": ["no_bold", "p", "remove"],
            }],
        }]
        new_html, err = apply_rules(html, rules, rule_id="r1")
        self.assertIsNone(err)
        self.assertEqual(new_html.count("<p"), 3)

    def test_selection_scope(self):
        html = "<p>选中这部分文字</p>"
        rules = [{
            "id": "r1",
            "name": "Selection测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "这部分",
                "scope": "selection",
                "formats": ["bold"],
            }],
        }]
        # selection scope 需要 sel_start/sel_end
        new_html, err = apply_rules(html, rules, all_rules=True, sel_start=2, sel_end=5)
        self.assertIsNone(err)
        # 选区内的文本应该被加粗
        self.assertIn("<strong>", new_html)

    def test_page_scope(self):
        html = "<p>第一段</p><p>第二段</p>"
        rules = [{
            "id": "r1",
            "name": "PageScope测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "段",
                "scope": "page",
                "formats": ["bold"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # page scope 应该对整页生效
        self.assertEqual(new_html.count("<strong>"), 2)  # 两个"段"都被加粗

    def test_regex_group_formats(self):
        html = "<p>123-456</p>"
        rules = [{
            "id": "r1",
            "name": "GroupFormats测试",
            "mode": "first",
            "conditions": [{
                "type": "regex",
                "pattern": "(\\d+)-(\\d+)",
                "scope": "page",
                "formats": [],
                "group_formats": [["bold"], ["italic"]],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # 第一个捕获组加粗，第二个斜体
        self.assertIn("<strong>123</strong>", new_html)
        self.assertIn("<em>456</em>", new_html)

    def test_regex_match_formats(self):
        html = "<p>测试 测试 测试</p>"
        rules = [{
            "id": "r1",
            "name": "MatchFormats测试",
            "mode": "first",
            "conditions": [{
                "type": "regex",
                "pattern": "测试",
                "scope": "page",
                "formats": [],
                "match_formats": [["bold"], ["italic"], ["note"]],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # 三次匹配分别应用不同格式
        self.assertIn("<strong>测试</strong>", new_html)
        self.assertIn("<em>测试</em>", new_html)
        self.assertIn('class="ptoe-note"', new_html)

    def test_target_before(self):
        html = "<p>标题：正文内容</p>"
        rules = [{
            "id": "r1",
            "name": "TargetBefore测试",
            "mode": "first",
            "conditions": [{
                "type": "regex",
                "pattern": "标题",
                "scope": "page",
                "formats": ["bold"],
                "target": "before",
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # before 应该对匹配之前的文本生效（这里匹配在开头，before 为空）

    def test_target_after(self):
        html = "<p>标题：正文内容</p>"
        rules = [{
            "id": "r1",
            "name": "TargetAfter测试",
            "mode": "first",
            "conditions": [{
                "type": "regex",
                "pattern": "标题",
                "scope": "page",
                "formats": ["italic"],
                "target": "after",
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # after 应该对匹配之后的文本生效
        self.assertIn("<em>", new_html)

    def test_target_between(self):
        html = "<p>开始 中间 结束</p>"
        rules = [{
            "id": "r1",
            "name": "TargetBetween测试",
            "mode": "first",
            "conditions": [{
                "type": "regex",
                "pattern": "开始",
                "scope": "page",
                "formats": ["note"],
                "target": "between",
                "between_end_pattern": "结束",
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # between 应该对开始和结束之间的文本生效
        self.assertIn('class="ptoe-note"', new_html)

    def test_conflict_first_wins(self):
        html = "<p>测试文本内容</p>"
        rules = [{
            "id": "r1",
            "name": "Conflict 测试",
            "mode": "all",
            "conditions": [
                {"type": "contains", "pattern": "测试", "scope": "page", "formats": ["align_left"]},
                {"type": "contains", "pattern": "内容", "scope": "page", "formats": ["align_right"]},
            ],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # Two contains rules targeting same block: block-level align first-wins
        # align_left applied first, align_right blocked by block_conflicts
        self.assertIn('ptoe-align-left', new_html)
        self.assertNotIn('ptoe-align-right', new_html)

    def test_cross_rule_conflict(self):
        html = "<p>测试文本</p>"
        rules = [
            {
                "id": "r1",
                "name": "Rule1",
                "mode": "first",
                "conditions": [{
                    "type": "contains",
                    "pattern": "测试",
                    "scope": "page",
                    "formats": ["align_left"],
                }],
            },
            {
                "id": "r2",
                "name": "Rule2",
                "mode": "first",
                "conditions": [{
                    "type": "contains",
                    "pattern": "文本",
                    "scope": "page",
                    "formats": ["align_center"],  # Align ops now use inline spans, can coexist
                }],
            },
        ]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # Cross-rule: both rules target same block, block-level align first-wins
        # Rule1 (align_left) applied, Rule2 (align_center) blocked by block_conflicts
        self.assertIn('ptoe-align-left', new_html)
        self.assertNotIn('ptoe-align-center', new_html)

    def test_group_align_same_block_first_wins(self):
        """用户场景回归（2026-08）：组区间块内部分覆盖（`## 注释` 中「注释」是末组、
        `## ` 前缀落入上一组区间）时，后应用组不得覆盖同块已生效的对齐。

        应用顺序=组起始偏移倒序 → 末组（align_left）先应用并记录冲突，
        前一组的 align_right 在同块被逐块过滤跳过；毛泽东/刊印等独立块仍正常居右。
        注意：可选组（如 `(...)?`）前需有必选锚定组，否则组1 贪婪吞掉全部、
        可选组不参与匹配（与用户真实正则结构一致：必选日期组强制回溯）。
        """
        html = (
            "<p>（一九五〇年）</p><p>毛泽东</p><p>根据手稿刊印。</p>"
            "<p>## 注释</p><p>〔1〕脚注。</p>"
        )
        rules = [{
            "id": "r1",
            "name": "标注",
            "mode": "first",
            "conditions": [{
                "type": "regex",
                "pattern": r"([\s\S]*?)(一九五〇[\s\S]*?)(毛\s*泽\s*东[\s\S]*刊\s*印[\s\S]*?)?(注[\s]*释)",
                "scope": "page",
                "group_formats": [[], ["align_center"], ["align_right", "p"], ["bold", "align_left", "p"]],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # 独立块正常居右
        self.assertIn('<p class="ptoe-align-right">毛泽东</p>', new_html)
        self.assertIn('<p class="ptoe-align-right">根据手稿刊印。</p>', new_html)
        # 注释块保持末组（高偏移先应用）的居左，不被前一组的居右覆盖
        self.assertIn('ptoe-align-left">## <strong>注释</strong>', new_html)
        self.assertNotIn('ptoe-align-right">##', new_html)

    def test_group_align_partial_conflict_keeps_other_blocks(self):
        """多块区间中个别块命中既有对齐冲突时，其余非冲突块仍应用对齐
        （逐块过滤 first-wins，2026-08 修复——原 any() 检查会拖垮整段）。"""
        html = "<p>甲块</p><p>中间块</p><p>## 注释</p>"
        rules = [
            {
                "id": "r1",
                "name": "左",
                "mode": "all",
                "conditions": [{
                    "type": "contains",
                    "pattern": "甲",
                    "scope": "page",
                    "formats": ["align_left"],
                }],
            },
            {
                "id": "r2",
                "name": "右",
                "mode": "first",
                "conditions": [{
                    "type": "regex",
                    "pattern": r"([\s\S]*)(注释)",
                    "scope": "page",
                    "group_formats": [["align_right"], ["align_left", "p"]],
                }],
            },
        ]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # 甲块：r1 先应用居左 → r2 组1 同块居右被过滤，保持居左
        self.assertIn('ptoe-align-left">甲块', new_html)
        # 中间块：无冲突，仍被 r2 组1 居右
        self.assertIn('ptoe-align-right">中间块', new_html)
        # 注释块（r2 组2 先应用居左）不被组1 居右覆盖
        self.assertNotIn('ptoe-align-right">##', new_html)

    def test_selection_tool_no_selection_skipped(self):
        """选区工具（contains 空 pattern + scope=selection）在无选区时不整页应用
        （2026-08 修复——此前「应用全部规则」无选区会退化为整页 h1+居中）。"""
        html = "<p>甲</p><p>乙</p>"
        rules = [{
            "id": "tool",
            "name": "中标",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "",
                "scope": "selection",
                "formats": ["align_center", "heading1"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        self.assertNotIn("<h1", new_html)
        self.assertNotIn("ptoe-align-center", new_html)
        # 有选区时正常应用（选区范围内的块级格式）
        new_html2, err2 = apply_rules(html, rules, all_rules=True, sel_start=0, sel_end=2)
        self.assertIsNone(err2)
        self.assertIn("<h1", new_html2)
        self.assertIn("ptoe-align-center", new_html2)

    def test_note_idempotent_multiple_matches(self):
        """同段多次匹配 note 应该幂等添加，不互相抵消。"""
        html = "<p>注释1 注释2</p>"
        rules = [{
            "id": "r1",
            "name": "Note多匹配",
            "mode": "all",
            "conditions": [{
                "type": "regex",
                "pattern": "注释\\d",
                "scope": "page",
                "formats": ["note"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # 两个匹配都在同一个块内，note 应该只添加一次（幂等）
        self.assertIn('class="ptoe-note"', new_html)
        # 不应该有重复的 class
        self.assertEqual(new_html.count('ptoe-note'), 1)

    def test_img_preserved_during_formatting(self):
        html = '<p>文本<img src="x.png" alt="图" class="ptoe-img-full">尾部</p>'
        rules = [{
            "id": "r1",
            "name": "Img保留测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "文本",
                "scope": "page",
                "formats": ["bold"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # img 应该被保留
        self.assertIn('src="x.png"', new_html)
        self.assertIn('class="ptoe-img-full"', new_html)
        # 2026-10-01 回归：非自闭合 <img> 曾把「尾部」吞成 img 的子节点，
        # 而 to_html 序列化空元素时丢弃 children → 尾文凭空消失。
        self.assertIn('尾部', new_html)

    def test_void_tag_does_not_swallow_following_text(self):
        """空元素不得入栈：img/br/hr 与非白名单空元素都不能吃掉后续文字。

        浏览器 innerHTML 序列化对 void 元素永不输出自闭合斜杠，故非自闭合形态
        是矫正界面格式规则的**默认**输入，而非边界情况。
        """
        rules = [{
            "id": "r1", "name": "加粗", "mode": "first",
            "conditions": [{
                "type": "contains", "pattern": "前文",
                "scope": "page", "formats": ["bold"],
            }],
        }]
        # 非自闭合 img（浏览器 innerHTML 的真实形态）
        cases: list[str] = [
            '<p>前文<img src="x.png">尾部</p>',
            '<p>前文<br>尾部</p>',
            '<p>前文<hr>尾部</p>',
            # 非白名单空元素（__skip_ 压栈后无 endtag 可出栈）
            '<p>前文<input type="hidden">尾部</p>',
            '<p>前文<source src="a.mp4">尾部</p>',
        ]
        for html in cases:
            with self.subTest(html=html):
                new_html, err = apply_rules(html, rules, all_rules=True)
                assert err is None and new_html is not None
                self.assertIn('尾部', new_html,
                              f"尾文被空元素吞掉: {html} -> {new_html}")

    def test_img_selfclosed_and_not_both_keep_tail(self):
        """自闭合与非自闭合两种形态必须产出等价结果。"""
        rules = [{
            "id": "r1", "name": "加粗", "mode": "first",
            "conditions": [{
                "type": "contains", "pattern": "前文",
                "scope": "page", "formats": ["bold"],
            }],
        }]
        a, _ = apply_rules('<p>前文<img src="x.png"/>尾部</p>', rules, all_rules=True)
        b, _ = apply_rules('<p>前文<img src="x.png">尾部</p>', rules, all_rules=True)
        assert a is not None and b is not None
        self.assertEqual(a, b)

    def test_single_rule_by_id(self):
        html = "<p>测试A 测试B</p>"
        rules = [
            {
                "id": "r1",
                "name": "RuleA",
                "mode": "first",
                "conditions": [{
                    "type": "contains",
                    "pattern": "A",
                    "scope": "page",
                    "formats": ["bold"],
                }],
            },
            {
                "id": "r2",
                "name": "RuleB",
                "mode": "first",
                "conditions": [{
                    "type": "contains",
                    "pattern": "B",
                    "scope": "page",
                    "formats": ["italic"],
                }],
            },
        ]
        # 只应用 r1
        new_html, err = apply_rules(html, rules, rule_id="r1")
        self.assertIsNone(err)
        self.assertIn("<strong>A</strong>", new_html)
        self.assertNotIn("<em>", new_html)

    def test_invalid_rule_id(self):
        html = "<p>测试</p>"
        rules = [{
            "id": "r1",
            "name": "Rule1",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "测试",
                "scope": "page",
                "formats": ["bold"],
            }],
        }]
        new_html, err = apply_rules(html, rules, rule_id="nonexistent")
        self.assertIsNotNone(err)
        self.assertIn("规则不存在", err)

    def test_chinese_text_offsets(self):
        """中文文本偏移正确性：确保多字节字符不导致偏移错位。"""
        html = "<p>你好世界测试中文</p>"
        rules = [{
            "id": "r1",
            "name": "中文偏移",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "世界",
                "scope": "page",
                "formats": ["bold"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        self.assertIn("<strong>世界</strong>", new_html)
        # 验证其他中文未被破坏
        self.assertIn("你好", new_html)
        self.assertIn("测试中文", new_html)

    # ---- 2026-08 正则引擎修复回归测试 ----
    # 修复前：同文本节点内多次匹配只应用最后一次（nodes_info 陈旧，旧节点已脱离树，
    # range_from_offsets 命中旧节点 → parent.children.index() ValueError 静默吞掉）。

    def test_contains_multiple_matches_same_node_all_applied(self):
        """同一文本节点多次命中 contains：全部匹配都要格式化（修复前只剩最后一个）。"""
        html = "<p>test test test</p>"
        rules = [{
            "id": "r1",
            "name": "BoldAll",
            "mode": "all",
            "conditions": [{
                "type": "contains",
                "pattern": "test",
                "scope": "page",
                "formats": ["bold"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        self.assertEqual(new_html.count("<strong"), 3)
        self.assertEqual(new_html, "<p><strong>test</strong> <strong>test</strong> <strong>test</strong></p>")

    def test_regex_multiple_matches_same_node_all_applied(self):
        """同一文本节点多次命中 regex：全部匹配都要格式化。"""
        html = "<p>abc abc abc</p>"
        rules = [{
            "id": "r1",
            "name": "ItalicAll",
            "mode": "all",
            "conditions": [{
                "type": "regex",
                "pattern": "abc",
                "scope": "page",
                "formats": ["italic"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        self.assertEqual(new_html.count("<em"), 3)

    def test_group_formats_multiple_matches_no_data_loss(self):
        """多匹配 group_formats：全部捕获组生效，且不丢失匹配以外的文本
        （修复前 pretty 路径整节点替换，只剩最后一个匹配、前缀文本丢失）。"""
        html = "<p>2024year 2025year 2026year</p>"
        rules = [{
            "id": "r1",
            "name": "GroupAll",
            "mode": "all",
            "conditions": [{
                "type": "regex",
                "pattern": "(\\d{4})year",
                "scope": "page",
                "formats": [],
                "group_formats": [["bold"]],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        self.assertEqual(new_html.count("<strong"), 3)
        # 三个匹配全部生效，且无任何文本丢失（修复前只剩 2026、前缀全丢）
        import re as _re
        stripped = _re.sub(r"<[^>]+>", "", new_html)
        self.assertEqual(stripped, "2024year 2025year 2026year")

    def test_selection_scope_does_not_format_outside(self):
        """scope=selection：只格式化选区内的匹配，选区外的不动
        （修复前忽略选区，全文找匹配，作用于错误对象）。"""
        html = "<p>hello world hello world</p>"
        rules = [{
            "id": "r1",
            "name": "SelBold",
            "mode": "all",
            "conditions": [{
                "type": "contains",
                "pattern": "hello",
                "scope": "selection",
                "formats": ["bold"],
            }],
        }]
        new_html, err = apply_rules(
            html, rules, all_rules=True, sel_start=0, sel_end=11
        )
        self.assertIsNone(err)
        self.assertEqual(new_html.count("<strong"), 1)
        self.assertEqual(new_html, "<p><strong>hello</strong> world hello world</p>")

    def test_selection_scope_regex_outside_excluded(self):
        """scope=selection + regex：选区文本无匹配时整条条件不生效，页面原样。"""
        html = "<p>aaa bbb aaa bbb</p>"
        rules = [{
            "id": "r1",
            "name": "SelRegex",
            "mode": "all",
            "conditions": [{
                "type": "regex",
                "pattern": "bbb",
                "scope": "selection",
                "formats": ["italic"],
            }],
        }]
        new_html, err = apply_rules(
            html, rules, all_rules=True, sel_start=0, sel_end=4
        )
        self.assertIsNone(err)
        self.assertEqual(new_html, html)

    def test_cross_condition_offsets_valid(self):
        """跨条件/跨规则偏移仍有效：第一条规则改动树后，第二条规则的匹配
        不得命中已脱离的旧节点（修复前陈旧的 nodes_info 导致部分匹配失效）。"""
        html = "<p>one two one two one two</p>"
        rules = [
            {
                "id": "r1",
                "name": "BoldOne",
                "mode": "all",
                "conditions": [{
                    "type": "contains",
                    "pattern": "one",
                    "scope": "page",
                    "formats": ["bold"],
                }],
            },
            {
                "id": "r2",
                "name": "BoldTwo",
                "mode": "all",
                "conditions": [{
                    "type": "contains",
                    "pattern": "two",
                    "scope": "page",
                    "formats": ["bold"],
                }],
            },
        ]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        self.assertEqual(new_html.count("<strong"), 6)

    def test_regex_invalid_pattern_rejected(self):
        """非法正则：应用前预检拦截，返回中文错误，页面原样（不静默吞掉）。"""
        html = "<p>test</p>"
        rules = [{
            "id": "r1",
            "name": "非法正则",
            "mode": "all",
            "conditions": [{
                "type": "regex",
                "pattern": "[unclosed",
                "scope": "page",
                "formats": ["bold"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNotNone(err)
        self.assertIn("无效", err)
        self.assertEqual(new_html, html)

    def test_dangerous_pattern_rejected(self):
        """灾难性回溯模式（嵌套量词 (a+)+）：预检拦截，返回中文风险提示，页面原样。"""
        html = "<p>test</p>"
        rules = [{
            "id": "r1",
            "name": "危险正则",
            "mode": "all",
            "conditions": [{
                "type": "regex",
                "pattern": "(a+)+",
                "scope": "page",
                "formats": ["bold"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNotNone(err)
        self.assertIn("风险", err)
        self.assertEqual(new_html, html)

    def test_dangerous_wildcard_optional_not_rejected(self):
        """通配/可选嵌套量词（如 ([\\s\\S]*?)?）不再误判为灾难性回溯，可正常编译与应用。"""
        # 用户实际规则：含通配组 + 可选捕获组
        pat = r"([\s\S]*)([\(（]\s*.+\s*年\s*.*?[日月]\s*[\）)])([\s\S]*?)(毛\s*泽\s*东[\s\S]*刊\s*印[\s\S]*?)?(注[\s]*释)([\s\S]*)"
        self.assertFalse(_is_dangerous(pat))
        # 通配量词外再套 +/* 也安全（.*+、(.)+）
        self.assertFalse(_is_dangerous(r"(.+)+"))
        self.assertFalse(_is_dangerous(r"([\s\S]+)+"))
        # 反向量词 ?（可选）不构成重划分
        self.assertFalse(_is_dangerous(r"([\s\S]*?)?"))

    def test_dangerous_still_rejects_nested_concrete(self):
        """具体原子上的嵌套量词（(a+)+、(\\d+)+）仍被拦截，性能保护不退化。"""
        self.assertTrue(_is_dangerous("(a+)+"))
        self.assertTrue(_is_dangerous("(a*)*"))
        self.assertTrue(_is_dangerous(r"(\d+)+"))
        self.assertTrue(_is_dangerous("(?:ab+)+"))

    def test_regex_wildcard_optional_groups_applied(self):
        """用户规则端到端：通配/可选捕获组各自套用独立格式，无错误、无丢文本。"""
        html = "<p>（2024年5月）这是正文。毛泽东同志题写刊印。注释：此处为注。</p>"
        pat = r"([\s\S]*)([\(（]\s*.+\s*年\s*.*?[日月]\s*[\）)])([\s\S]*?)(毛\s*泽\s*东[\s\S]*刊\s*印[\s\S]*?)?(注[\s]*释)([\s\S]*)"
        rules = [{
            "id": "r1",
            "name": "通配可选组",
            "mode": "first",
            "conditions": [{
                "type": "regex",
                "pattern": pat,
                "scope": "page",
                "formats": [],
                "group_formats": [[], ["bold"], [], ["italic"], ["note"], []],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # 日期组加粗、毛泽东刊印组斜体、注释组注释类
        self.assertIn("<strong>", new_html)
        self.assertIn("<em>", new_html)
        self.assertIn('class="ptoe-note"', new_html)
        # 匹配以外的文本不丢失
        self.assertIn("这是正文", new_html)
        self.assertIn("此处为注", new_html)

    def test_group_formats_per_block_independent(self):
        """匹配对象分别设置独立格式（块级 heading）：不同段落各自独立生效，不再被全局冲突误跳过。"""
        html = "<p>章节一 内容A。注释</p><p>章节一 内容B。注释</p>"
        rules = [{
            "id": "r1",
            "name": "每段标题",
            "mode": "all",
            "conditions": [{
                "type": "regex",
                "pattern": "(章节一)(.*?)(注释)",
                "scope": "page",
                "formats": [],
                "group_formats": [["heading1"], [], ["note"]],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # 两段“章节一”都成为 h1（修复前仅一段）
        self.assertEqual(new_html.count("<h1>"), 2)
        self.assertIn("<h1>章节一 内容A。", new_html)
        self.assertIn("<h1>章节一 内容B。", new_html)
        # 每段“注释”都加注释类（块级路径 note 为块内 class）
        self.assertEqual(new_html.count("ptoe-note"), 2)

    def test_cache_boundary_lru(self):
        """正则缓存有界：超过上限按 LRU 淘汰最早条目，不再整体清空。"""
        import rulemanage
        rulemanage._REGEX_CACHE.clear()
        try:
            for i in range(300):
                rulemanage._compile_cached(f"/pat{i}/")
            self.assertLessEqual(len(rulemanage._REGEX_CACHE), rulemanage._REGEX_CACHE_MAX)
            self.assertIsNone(rulemanage._REGEX_CACHE.get("/pat0/"))  # 最早插入 → 已淘汰
            self.assertIsNotNone(rulemanage._REGEX_CACHE.get("/pat299/"))
        finally:
            rulemanage._REGEX_CACHE.clear()

    def test_cache_thread_safety(self):
        """并发编译正则：锁保护下无异常、无丢条目（serve 为多线程 HTTP 服务器）。"""
        import rulemanage
        from concurrent.futures import ThreadPoolExecutor
        rulemanage._REGEX_CACHE.clear()
        try:
            def compile_one(i):
                return rulemanage._compile_cached(f"/thr{i % 16}/")

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(compile_one, range(96)))
            self.assertEqual(len(results), 96)
            self.assertTrue(all(r is not None for r in results))
            self.assertEqual(len(rulemanage._REGEX_CACHE), 16)  # 唯一模式各 1 条
        finally:
            rulemanage._REGEX_CACHE.clear()


class TestConditionFormatsNotSwallowed(unittest.TestCase):
    """回归（2026-09-13）：条件级 formats 与 match_formats/group_formats 叠加生效。

    症状历史：一条正则匹配到多个对象，却只有最后一个匹配对象被改格式。
    根因：eval_format_rule 用 elif 串联分类，只要条件带了 group/match 格式
    （哪怕数组里只有一行填了格式、甚至全是空行），条件级 formats 就被整条丢弃。
    """

    def test_condition_formats_kept_with_match_formats(self):
        rule = Rule(
            id="r1",
            name="T",
            mode="all",
            conditions=[
                Condition(
                    "regex", "〔\\d+〕", "page", ["underline"],
                    match_formats=[[], [], [], ["bold"]],
                )
            ],
        )
        result = eval_format_rule(rule, "〔1〕〔2〕〔3〕〔4〕")
        # 条件级 → pattern_conds（作用到全部匹配）；逐匹配 → match_conds（追加）
        self.assertEqual(len(result.pattern_conds), 1)
        self.assertEqual(result.pattern_conds[0].formats, ["underline"])
        self.assertEqual(len(result.match_conds), 1)
        self.assertEqual(result.match_conds[0].match_formats, [[], [], [], ["bold"]])

    def test_condition_formats_kept_with_empty_match_formats(self):
        rule = Rule(
            id="r1",
            name="T",
            mode="all",
            conditions=[
                Condition(
                    "regex", "〔\\d+〕", "page", ["underline"],
                    match_formats=[[], [], [], []],
                )
            ],
        )
        result = eval_format_rule(rule, "〔1〕〔2〕")
        self.assertEqual(len(result.pattern_conds), 1)

    def test_condition_formats_kept_with_group_formats(self):
        rule = Rule(
            id="r1",
            name="T",
            mode="all",
            conditions=[
                Condition(
                    "regex", "(〔\\d+〕)", "page", ["underline"],
                    group_formats=[[]],
                )
            ],
        )
        result = eval_format_rule(rule, "〔1〕〔2〕")
        self.assertEqual(len(result.pattern_conds), 1)
        self.assertEqual(len(result.group_conds), 1)

    def test_all_matches_formatted_when_match_formats_partial(self):
        """端到端：条件级「下划线」必须作用于全部 4 个匹配，末行「加粗」额外追加。"""
        html = "<p>〔1〕甲</p><p>〔2〕乙</p><p>〔3〕丙</p><p>〔4〕丁</p>"
        rules = [
            {
                "id": "r1",
                "name": "t",
                "mode": "all",
                "conditions": [
                    {
                        "type": "regex",
                        "pattern": "〔\\d+〕",
                        "scope": "page",
                        "target": "match",
                        "formats": ["underline"],
                        "match_formats": [[], [], [], ["bold"]],
                    }
                ],
            }
        ]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        self.assertEqual(new_html.count('class="ptoe-underline"'), 4)
        self.assertEqual(new_html.count("<strong>"), 1)

    def test_all_matches_formatted_when_match_formats_all_empty(self):
        """匹配行全是空行（界面里加了行但没填格式）：条件级格式仍须全部生效。"""
        html = "<p>〔1〕甲〔2〕乙〔3〕丙〔4〕丁</p>"
        rules = [
            {
                "id": "r1",
                "name": "t",
                "mode": "all",
                "conditions": [
                    {
                        "type": "regex",
                        "pattern": "〔\\d+〕",
                        "scope": "page",
                        "target": "match",
                        "formats": ["underline"],
                        "match_formats": [[], [], [], []],
                    }
                ],
            }
        ]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        self.assertEqual(new_html.count('class="ptoe-underline"'), 4)

    def test_match_formats_only_still_per_match(self):
        """未设置条件级格式时，逐匹配格式仍按索引各管各的（不改变原语义）。"""
        html = "<p>〔1〕甲</p><p>〔2〕乙</p><p>〔3〕丙</p><p>〔4〕丁</p>"
        rules = [
            {
                "id": "r1",
                "name": "t",
                "mode": "all",
                "conditions": [
                    {
                        "type": "regex",
                        "pattern": "〔\\d+〕",
                        "scope": "page",
                        "target": "match",
                        "formats": [],
                        "match_formats": [[], [], [], ["bold"]],
                    }
                ],
            }
        ]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        self.assertEqual(new_html.count("<strong>"), 1)
        self.assertIn("<strong>〔4〕</strong>", new_html)


class TestParagraphStartExclusion(unittest.TestCase):
    r"""跳过"段首标记"的正则配方（2026-09-13）。

    场景：正文里的引用标记〔1〕嵌在句中（应设上标），而"注释"区每条的〔n〕在段首
    （不该设上标）。语料实测 8052 页：句中标记 7861 处、段首 5687 处、页首 192 处。

    引擎口径：页面纯文本由各文本节点拼接，段与段的边界就是换行符 \n，
    因此"段首/页首"等价于"标记前面没有本行的非空白字符"。

    - 推荐 `(?:[^\n\s][^\n]*?)(〔\d+〕)`：分组号仍是组1，句中标记 100% 命中，
      段首/页首 0 误套（前缀用非捕获组，不占用组号）；
    - `(?<!\n)(〔\d+〕)`：只排除"紧跟换行"的段首，页首页首那 192 处仍会误套。
    """

    HTML = (
        "<p>\n一月二十日二十四时来电〔3〕及两组〔4〕简报均悉。</p>"
        "<p>\n注释</p>"
        "<p>\n〔1〕李克农，当时任外交部副部长。</p>"
        "<p>\n〔2〕金，指金日成。</p>"
    )
    # 首段本身就是标记开头（页首情形）
    HTML_HEAD = "<p>〔9〕页首条目。</p><p>\n正文引用〔3〕在此。</p>"

    def _sups(self, pattern: str, html: str | None = None) -> str:
        rules = [{
            "id": "r1", "name": "注释", "mode": "all",
            "conditions": [{
                "type": "regex", "pattern": pattern, "scope": "page", "target": "match",
                "formats": [], "group_formats": [["sup"]],
            }],
        }]
        out, err = apply_rules(html or self.HTML, rules, all_rules=True)
        self.assertIsNone(err)
        return out

    def test_non_capturing_prefix_recipe(self):
        """推荐配方：句中标记上标、段首标记（注释条目）跳过。"""
        out = self._sups(r"(?:[^\n\s][^\n]*?)(〔\d+〕)")
        for marker in ("〔3〕", "〔4〕"):
            self.assertIn(f'<span class="ptoe-sup">{marker}</span>', out)
        for marker in ("〔1〕", "〔2〕"):
            self.assertNotIn(f'<span class="ptoe-sup">{marker}</span>', out)

    def test_non_capturing_prefix_recipe_skips_page_head(self):
        """页首（页首即段首）的标记同样跳过。"""
        out = self._sups(r"(?:[^\n\s][^\n]*?)(〔\d+〕)", self.HTML_HEAD)
        self.assertIn('<span class="ptoe-sup">〔3〕</span>', out)
        self.assertNotIn('<span class="ptoe-sup">〔9〕</span>', out)

    def test_lookbehind_recipe_leaks_page_head(self):
        r"""`(?<!\n)` 只排除紧跟换行的段首：页首标记仍会被套（说明为何推荐上面的写法）。"""
        out = self._sups(r"(?<!\n)(〔\d+〕)", self.HTML_HEAD)
        self.assertIn('<span class="ptoe-sup">〔3〕</span>', out)
        self.assertIn('<span class="ptoe-sup">〔9〕</span>', out)

    def test_greedy_prefix_misses_inline_markers(self):
        r"""对照：`[\t\S]+(〔\d+〕)` 贪婪前缀会吞掉同一行的后续标记。"""
        out = self._sups(r"[\t\S]+(〔\d+〕)")
        self.assertNotIn('<span class="ptoe-sup">〔3〕</span>', out)
        self.assertIn('<span class="ptoe-sup">〔4〕</span>', out)

    def test_slash_flags_recipe_matches_paragraph_start(self):
        r"""反向需求（只处理段首条目）：`/^(〔\d+〕)/m` 靠 m 标志让 ^ 匹配每行行首。"""
        cond = Condition("regex", r"/^(〔\d+〕)/m", "page", [])
        found = find_matches(cond, "行内〔1〕引用。\n〔2〕段首条目。")
        self.assertEqual([m.group(1) for m in found], ["〔2〕"])
        # 不带 m 时 ^ 只匹配整页开头
        cond2 = Condition("regex", r"^(〔\d+〕)", "page", [])
        self.assertEqual(find_matches(cond2, "行内〔1〕引用。\n〔2〕段首条目。"), [])


class TestNewInlineFormats(unittest.TestCase):
    """新增 7 项行内格式测试（2026-09）。"""

    def test_parse_serialize_new_span_classes(self):
        """解析/序列化往返保留新增 span class（underline/strike/charbox/shade/highlight/sup/sub）。"""
        html = (
            '<p>a<span class="ptoe-underline">b</span>c</p>'
            '<p>a<span class="ptoe-strike">b</span>c</p>'
            '<p>a<span class="ptoe-charbox">b</span>c</p>'
            '<p>a<span class="ptoe-shade">b</span>c</p>'
            '<p>a<span class="ptoe-highlight">b</span>c</p>'
            '<p>a<span class="ptoe-sup">b</span>c</p>'
            '<p>a<span class="ptoe-sub">b</span>c</p>'
        )
        root = parse_html(html)
        out = serialize_html(root)
        for cls in ("ptoe-underline", "ptoe-strike", "ptoe-charbox", "ptoe-shade", "ptoe-highlight", "ptoe-sup", "ptoe-sub"):
            self.assertIn(f'class="{cls}"', out)

    def test_apply_rules_underline_wraps_matched_range(self):
        """apply_rules 含 underline op 时，匹配范围被 <span class="ptoe-underline"> 包裹。"""
        html = "<p>测试下划线文本</p>"
        rules = [{
            "id": "r1",
            "name": "Underline测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "下划线",
                "scope": "page",
                "formats": ["underline"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        self.assertIn('<span class="ptoe-underline">下划线</span>', new_html)

    def test_sup_sub_mutual_exclusion_no_nesting(self):
        """sup 后应用 sub（或反向）不产生嵌套 span，只保留最后应用的格式。"""
        html = "<p>测试上下标</p>"
        # 先应用 sup
        rules1 = [{
            "id": "r1",
            "name": "Sup测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "上下标",
                "scope": "page",
                "formats": ["sup"],
            }],
        }]
        new_html, err = apply_rules(html, rules1, all_rules=True)
        self.assertIsNone(err)
        self.assertIn('<span class="ptoe-sup">上下标</span>', new_html)
        self.assertNotIn('ptoe-sub', new_html)

        # 再在同一范围应用 sub（模拟切换）
        rules2 = [{
            "id": "r2",
            "name": "Sub测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "上下标",
                "scope": "page",
                "formats": ["sub"],
            }],
        }]
        new_html2, err2 = apply_rules(new_html, rules2, all_rules=True)
        self.assertIsNone(err2)
        # 应该只有 sub，没有嵌套
        self.assertIn('<span class="ptoe-sub">上下标</span>', new_html2)
        self.assertNotIn('ptoe-sup', new_html2)
        # 确保没有 span 内嵌 span
        self.assertEqual(new_html2.count('<span'), 1)

    def test_remove_strips_new_inline_formats(self):
        """remove op 移除 strong/em 以及所有新增行内格式 span，保留 marker span。"""
        html = (
            '<p><strong>粗</strong>'
            '<span class="ptoe-underline">线</span>'
            '<span class="ptoe-strike">删</span>'
            '<span class="ptoe-charbox">框</span>'
            '<span class="ptoe-shade">纹</span>'
            '<span class="ptoe-highlight">亮</span>'
            '<span class="ptoe-sup">上</span>'
            '<span class="ptoe-sub">下</span>'
            '<span class="ptoe-note">注</span>'
            '<span class="ptoe-citation">引</span>'
            '<span data-ptoe-marker="join" class="ptoe-marker">标记</span>'
            '</p>'
        )
        rules = [{
            "id": "r1",
            "name": "Remove测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "粗",
                "scope": "page",
                "formats": ["remove"],
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # strong/em 应被移除
        self.assertNotIn("<strong>", new_html)
        self.assertNotIn("<em>", new_html)
        # 新增 7 项行内格式 span 应被移除（内容保留）
        for cls in ("ptoe-underline", "ptoe-strike", "ptoe-charbox", "ptoe-shade", "ptoe-highlight", "ptoe-sup", "ptoe-sub"):
            self.assertNotIn(f'class="{cls}"', new_html)
        # ptoe-note 和 ptoe-citation 也应被移除
        self.assertNotIn('class="ptoe-note"', new_html)
        self.assertNotIn('class="ptoe-citation"', new_html)
        # marker span 保留
        self.assertIn('data-ptoe-marker="join"', new_html)
        self.assertIn('class="ptoe-marker"', new_html)

    def test_ops_conflict_sup_sub(self):
        """ops_conflict('sup','sub') 为 True，ops_conflict('underline','bold') 为 False。"""
        self.assertTrue(ops_conflict("sup", "sub"))
        self.assertTrue(ops_conflict("sub", "sup"))
        self.assertFalse(ops_conflict("underline", "bold"))
        self.assertFalse(ops_conflict("strike", "italic"))
        self.assertFalse(ops_conflict("charbox", "note"))
        self.assertFalse(ops_conflict("shade", "citation"))
        self.assertFalse(ops_conflict("highlight", "p"))

    def test_rule_from_dict_drops_fake_op_keeps_underline(self):
        """rule_from_dict 丢弃假 op 名（如 fake_op），保留 underline 等合法 op。"""
        from rulemanage import rule_from_dict
        rule_dict = {
            "id": "r1",
            "name": "测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "测试",
                "scope": "page",
                "formats": ["underline", "fake_op", "bold"],
            }],
        }
        rule = rule_from_dict(rule_dict)
        self.assertIn("underline", rule.conditions[0].formats)
        self.assertIn("bold", rule.conditions[0].formats)
        self.assertNotIn("fake_op", rule.conditions[0].formats)


class TestCharStyleNormalize(unittest.TestCase):
    """字符样式归一 normalize_char_style（2026-09-27）。"""

    def test_canonical_order_and_no_trailing_semicolon(self):
        """四属性按固定顺序输出，分隔符 ';' 且无尾分号。"""
        raw = "color:#c00000;font-family:黑体, SimHei, sans-serif;font-size:20px;text-align:center"
        self.assertEqual(
            normalize_char_style(raw),
            "text-align:center;font-size:20px;font-family:黑体, SimHei, sans-serif;color:#c00000",
        )

    def test_drops_unknown_and_dangerous_props(self):
        """非白名单属性（含 position/top 等危险项）整条丢弃，不影响其余声明。"""
        out = normalize_char_style(
            "position:fixed;top:99px;left:0;font-size:20px;background:url(x);z-index:9999"
        )
        self.assertEqual(out, "font-size:20px")

    def test_font_size_clamped_and_rounded(self):
        """字号四舍五入到整数像素并钳制到 8..72。"""
        self.assertEqual(normalize_char_style("font-size:5px"), "font-size:8px")
        self.assertEqual(normalize_char_style("font-size:999px"), "font-size:72px")
        self.assertEqual(normalize_char_style("font-size:20.4px"), "font-size:20px")
        # 非 px 单位不接受
        self.assertEqual(normalize_char_style("font-size:2em"), "")

    def test_font_family_quoted_form_matches_stack(self):
        """带引号/多空白的字体栈归一后命中白名单。"""
        self.assertEqual(
            normalize_char_style("font-family:\"黑体\",   SimHei, sans-serif"),
            "font-family:黑体, SimHei, sans-serif",
        )
        # 不在白名单的字体栈被拒
        self.assertEqual(normalize_char_style("font-family:Comic Sans, Papyrus"), "")

    def test_all_ten_stacks_serialize_verbatim(self):
        """10 项白名单字体栈逐项原样往返。"""
        self.assertEqual(len(CHAR_FONT_STACKS), 10)
        for stack in CHAR_FONT_STACKS:
            self.assertEqual(normalize_char_style(f"font-family:{stack}"), f"font-family:{stack}")

    def test_color_lowercases_hex_and_keeps_named(self):
        """十六进制统一小写；颜色名原样透传（与 _COLOR_VALUE_RE 同形，刻意保留）。

        3-8 位十六进制与 3-20 位颜色名均放行（与既有清洗链同形），
        超出位数或非十六进制字符才整条丢弃。
        """
        self.assertEqual(normalize_char_style("color:#C00000"), "color:#c00000")
        self.assertEqual(normalize_char_style("color:#abc"), "color:#abc")
        self.assertEqual(normalize_char_style("color:#C00000FF"), "color:#c00000ff")
        self.assertEqual(normalize_char_style("color:red"), "color:red")
        # 非法颜色整条丢弃
        self.assertEqual(normalize_char_style("color:#12"), "")
        self.assertEqual(normalize_char_style("color:#1234567890"), "")
        self.assertEqual(normalize_char_style("color:#xyzxyz"), "")
        self.assertEqual(normalize_char_style("color:rgb(1,2,3)"), "")

    def test_idempotent(self):
        """幂等：normalize(normalize(x)) == normalize(x)。"""
        raw = "position:fixed;font-size:20.6px;color:#ABC;font-family:楷体, KaiTi, serif;text-align:right"
        once = normalize_char_style(raw)
        self.assertEqual(normalize_char_style(once), once)
        self.assertEqual(
            once, "text-align:right;font-size:21px;font-family:楷体, KaiTi, serif;color:#abc"
        )

    def test_last_declaration_wins(self):
        """同一属性重复出现后者覆盖前者，输出只留一条。"""
        self.assertEqual(normalize_char_style("font-size:10px;font-size:30px"), "font-size:30px")

    def test_empty_and_garbage_inputs(self):
        """空串/无冒号/全非法 → 空串。"""
        for raw in ("", "   ", "garbage;;:;x", "font-size", ";;;", "not-a-decl:1"):
            self.assertEqual(normalize_char_style(raw), "", raw)

    def test_char_style_get(self):
        """char_style_get 取单条声明值；未知名/缺失返回 ''。"""
        cs = "text-align:center;font-size:20px;color:#c00000"
        self.assertEqual(char_style_get(cs, "font-size"), "20px")
        self.assertEqual(char_style_get(cs, "color"), "#c00000")
        self.assertEqual(char_style_get(cs, "font-family"), "")
        self.assertEqual(char_style_get(cs, "position"), "")
        self.assertEqual(char_style_get(cs, "BOGUS"), "")


class TestCharStyleSanitizationAndRoundTrip(unittest.TestCase):
    """span style 经解析/序列化/清洗的往返与白名单（2026-09-27）。"""

    def test_parse_serialize_preserves_style(self):
        """解析/序列化往返保留规范化后的 style。"""
        html = '<p>a<span style="font-size:20px;color:#C00000">b</span>c</p>'
        out = serialize_html(parse_html(html))
        self.assertIn('style="font-size:20px;color:#c00000"', out)

    def test_dangerous_props_dropped_in_pipeline(self):
        """position 等危险属性在往返中被剥除，仅留白名单声明。"""
        html = '<p>a<span style="position:fixed;font-size:14px">b</span>c</p>'
        out = serialize_html(parse_html(html))
        self.assertIn('style="font-size:14px"', out)
        self.assertNotIn("position", out)

    def test_bold_italic_style_combined(self):
        """加粗/斜体类与字符样式共存，互不覆盖。"""
        html = (
            '<p>a<span class="ptoe-underline" style="font-size:16px">b</span>c</p>'
        )
        out = serialize_html(parse_html(html))
        self.assertIn('class="ptoe-underline"', out)
        self.assertIn('style="font-size:16px"', out)

    def test_no_style_attribute_when_all_props_rejected(self):
        """全部声明非法时不输出 style 属性（不留空 style=""）。"""
        html = '<p>a<span style="position:fixed;top:0">b</span>c</p>'
        out = serialize_html(parse_html(html))
        self.assertNotIn("style=", out)

    def test_style_round_trip_idempotent_through_parser(self):
        """二次往返字节稳定（归一串可被再次解析为同串）。"""
        html = '<p>x<span style="COLOR:#ABC;Font-Size:20px">y</span>z</p>'
        once = serialize_html(parse_html(html))
        twice = serialize_html(parse_html(once))
        self.assertEqual(once, twice)

    def test_allowed_span_classes_cover_all_inline_format_classes(self):
        """类漂移守卫：8 项行内格式类必须全在 span 白名单内。"""
        for cls in INLINE_FORMAT_CLASSES:
            self.assertIn(cls, ALLOWED_SPAN_CLASSES, f"{cls} 未进 span 白名单")

    def test_unknown_span_class_still_dropped(self):
        """未知 class 仍被剥除（白名单未被放宽成全放行）。"""
        html = '<p>a<span class="ptoe-underline evil-class">b</span>c</p>'
        out = serialize_html(parse_html(html))
        self.assertIn('class="ptoe-underline"', out)
        self.assertNotIn("evil-class", out)


class TestDividerBlockProtection(unittest.TestCase):
    """分隔线段落（ptoe-divider）在规则引擎中的保护（2026-09-27）。"""

    def _html(self, suffix="solid", glyph="────────"):
        return (
            f'<p>甲段落</p><p class="ptoe-divider ptoe-divider-{suffix}">{glyph}</p>'
            "<p>丙段落</p>"
        )

    def _apply(self, html, needle, op, end_needle=None):
        """按「文本内容」定位区间：end_needle 给定时取其**末尾**偏移。

        注意：原样 HTML 的块间没有 \\n，分隔线自身的字形也参与纯文本偏移，
        故 end 必须用 end_needle 的结尾位置，不能用 start+len(needle)。
        """
        root = parse_html(html)
        text = collect_text_nodes(root)[0]
        start = text.index(needle)
        end = (
            text.index(end_needle) + len(end_needle)
            if end_needle
            else start + len(needle)
        )
        ok = apply_block_format(root, collect_text_nodes(root)[1], start, end, op)
        return ok, serialize_html(root)

    def test_is_divider_block_detects_base_and_suffix(self):
        """基类与任一后缀都判为分隔线（startswith 前缀，避免后缀漏判）。"""
        for cls in (
            "ptoe-divider",
            "ptoe-divider-solid",
            "ptoe-divider-dashed",
            "ptoe-divider-dotted",
            "ptoe-divider-double",
            "ptoe-divider-wavy",
        ):
            root = parse_html(f'<p class="{cls}">X</p>')
            el = root.children[0]
            self.assertTrue(_is_divider_block(el), cls)

    def test_is_divider_block_false_for_normal_paragraphs(self):
        """普通段落/其它 class 不误判为分隔线。"""
        for cls in ("", "ptoe-note", "ptoe-align-center", "ptoe-note ptoe-dividerless"):
            root = parse_html(f'<p class="{cls}">X</p>')
            self.assertFalse(_is_divider_block(root.children[0]), cls)
        self.assertFalse(_is_divider_block(None))

    def test_divider_classes_survive_round_trip(self):
        """解析/序列化保留基类 + 后缀两个 class。"""
        for suffix in ("solid", "dashed", "dotted", "double"):
            html = f'<p class="ptoe-divider ptoe-divider-{suffix}">X</p>'
            out = serialize_html(parse_html(html))
            self.assertIn(f'class="ptoe-divider ptoe-divider-{suffix}"', out)

    def test_block_format_refused_when_selection_starts_on_divider(self):
        """选区起点是分隔线 → 一律拒绝块级格式，HTML 字节不变。"""
        html = self._html()
        for op in ("heading1", "align_center", "note", "merge", "flush", "indent"):
            ok, out = self._apply(html, "────────", op)
            self.assertFalse(ok, op)
            self.assertEqual(out, html, op)

    def test_merge_across_divider_refused(self):
        """选区跨分隔线 → 拒绝合并（不吞线、不跨线拼正文）。"""
        html = self._html()
        ok, out = self._apply(html, "甲段落", "merge", "丙段落")
        self.assertFalse(ok)
        self.assertEqual(out, html)

    def test_merge_with_adjacent_divider_refused(self):
        """选区仅一个块、紧邻兄弟是分隔线 → 拒绝合并。"""
        html = self._html()
        ok, out = self._apply(html, "甲段落", "merge")
        self.assertFalse(ok)
        self.assertEqual(out, html)

    def test_merge_stops_before_divider(self):
        """合并在分隔线前收敛：线前两段合并，线后段落与线本身不动。"""
        html = "<p>甲</p><p>乙</p><p class=\"ptoe-divider ptoe-divider-double\">X</p>"
        ok, out = self._apply(html, "甲", "merge", "乙")
        self.assertTrue(ok)
        self.assertEqual(out, '<p>甲 乙</p><p class="ptoe-divider ptoe-divider-double">X</p>')

    def test_merge_normal_paragraphs_regression(self):
        """无分隔线时合并行为不变（回归）。"""
        ok, out = self._apply("<p>甲</p><p>乙</p>", "甲", "merge", "乙")
        self.assertTrue(ok)
        self.assertEqual(out, "<p>甲 乙</p>")

    def test_cross_block_format_excludes_middle_divider(self):
        """跨块格式化跳过区间内的分隔线，其余块照常生效。"""
        html = '<p>甲</p><p class="ptoe-divider ptoe-divider-dashed">X</p><p>丙</p>'
        ok, out = self._apply(html, "甲", "heading1", "丙")
        self.assertTrue(ok)
        self.assertIn('<h1>甲</h1>', out)
        self.assertIn('<h1>丙</h1>', out)
        self.assertIn('<p class="ptoe-divider ptoe-divider-dashed">X</p>', out)

    def test_normal_paragraph_format_regression(self):
        """普通段落仍可被格式化（回归）。"""
        ok, out = self._apply("<p>甲</p><p>乙</p>", "乙", "heading1")
        self.assertTrue(ok)
        self.assertEqual(out, "<p>甲</p><h1>乙</h1>")


# 题注段落的固定写法：一条真实链路上会出现的说明文字（含「图N」编号，
# 恰好是最容易被正则命中的形态），以及它的精确 HTML 形态。
CAPTION_TEXT = "图1示例插图"
CAPTION_HTML = '<p class="ptoe-caption">图1示例插图</p>'


class TestCaptionBlockProtection(unittest.TestCase):
    """题注段落（ptoe-caption）在规则引擎中的版式保护（2026-10 图文混合）。

    题注 = 图片块之后的独立说明段落。与 ptoe-divider 同为「用户手动放置的版式
    元素」，但对标的是配图：题注-图片是一对，拆开或搬走就失去意义。故规则引擎对
    题注采取与分隔线**完全一致**的四道保护：
      ① 选区起点落在题注 → 一律拒绝（apply_block_format 开头早退）；
      ② merge 收集遇题注即停（不跨题注把两侧正文拼起来）；
      ③ merge 退化为合并下一兄弟时，紧邻题注直接拒绝（不把题注并进邻段）；
      ④ 跨块区间剔除中间的题注（区间其余块照常生效）。
    判定口径与解析侧块级白名单**逐字一致**（2026-10-01 修正）：「精确相等 或
    前缀+连字符」。修前解析白名单只放行精确相等的 ptoe-caption，与本判定的前缀
    口径分叉（见 test_suffixed_caption_class_protected_end_to_end）。
    """

    def _apply(self, html, needle, op, end_needle=None):
        """按「文本内容」定位区间后应用块级格式，返回 (是否应用, 序列化结果)。

        与 TestDividerBlockProtection._apply 同口径：end_needle 给定时取其**末尾**
        偏移（块间无 \\n，字形与题注文本都参与纯文本偏移，不能用 start+len）。
        """
        root = parse_html(html)
        text = collect_text_nodes(root)[0]
        start = text.index(needle)
        end = (
            text.index(end_needle) + len(end_needle)
            if end_needle
            else start + len(needle)
        )
        ok = apply_block_format(root, collect_text_nodes(root)[1], start, end, op)
        return ok, serialize_html(root)

    def _html(self):
        """题注夹在两段正文中间的最小三块结构。"""
        return f"<p>甲段</p>{CAPTION_HTML}<p>乙段</p>"

    # ---------- 1. 判定函数单测 ----------

    def test_is_caption_block_detects_base_and_suffix(self):
        """基类与「前缀+连字符」后缀都判为题注。

        这里**直构 ElementNode** 而不走 parse_html：即便解析侧白名单已改成严格
        前缀（2026-10-01 修正），直构仍是测判定函数本身口径的唯一干净途径——
        走解析器会把「判定」与「白名单」两个环节混在一起，白名单一改就分不清是
        判定退化还是白名单放行。两条链路由 test_suffixed_caption_* 与
        test_caption_lookalike_class_not_protected 各自钉死。
        """
        for cls in ("ptoe-caption", "ptoe-caption-xxx", "ptoe-caption-solid"):
            self.assertTrue(_is_caption_block(ElementNode("p", {"class": cls})), cls)
        # 基类与后缀类都走一遍真实解析路径（白名单放行 → 判定成立）
        for cls in ("ptoe-caption", "ptoe-caption-solid"):
            with self.subTest(cls=cls):
                root = parse_html(f'<p class="{cls}">{CAPTION_TEXT}</p>')
                self.assertTrue(_is_caption_block(root.children[0]), cls)
        # 多类共存时只看题注类本身
        self.assertTrue(
            _is_caption_block(ElementNode("p", {"class": "ptoe-note ptoe-caption"}))
        )

    def test_is_caption_block_false_for_lookalike_classes(self):
        """严格口径：形似前缀的无关 class 不误判（防 ptoe-captionx 类漏保护反噬）。

        与解析侧白名单同口径（2026-10-01 修正后）：判定与白名单都必须剥掉
        ptoe-captionx，两侧任一被改成宽松 startswith 都算回归。
        """
        for cls in ("ptoe-captionx", "ptoe-captionless", "ptoe-note",
                    "ptoe-citations", "ptoe-divider", "", "caption"):
            self.assertFalse(
                _is_caption_block(ElementNode("p", {"class": cls})), cls
            )
        # 无 class 键 / 空 class 串
        self.assertFalse(_is_caption_block(ElementNode("p", {})))
        # None 与非 ElementNode（文本节点）都必须 False，不得抛异常
        self.assertFalse(_is_caption_block(None))
        self.assertFalse(_is_caption_block(TextNode("题注")))

    # ---------- 2. 起点守卫（保护①） ----------

    def test_block_format_refused_when_selection_starts_on_caption(self):
        """选区起点是题注 → 一律拒绝块级格式，输出逐字节不变。"""
        html = self._html()
        for op in ("heading1", "note", "align_center", "indent", "flush", "merge"):
            with self.subTest(op=op):
                ok, out = self._apply(html, CAPTION_TEXT, op)
                self.assertFalse(ok)
                self.assertEqual(out, html)

    def test_rules_matching_caption_text_are_noop(self):
        """端到端：一条命中题注文字的规则不得改写题注（走真实 apply_rules）。

        覆盖用户实际配置的形态——规则条件 contains「图1」，formats 为标题/注释/
        对齐/缩进/合并五种块级格式。若无保护，heading1 会把题注变成 h1（进而进
        EPUB 目录），merge 会把题注并进邻段。
        """
        html = self._html()
        for op in ("heading1", "note", "align_center", "indent", "merge"):
            with self.subTest(op=op):
                rules = [{
                    "id": "r1", "name": op, "mode": "all",
                    "conditions": [{
                        "type": "contains", "pattern": "图1",
                        "scope": "page", "formats": [op],
                    }],
                }]
                out, err = apply_rules(html, rules, all_rules=True)
                self.assertIsNone(err)
                self.assertEqual(out, html)

    # ---------- 3. merge 三形态（保护②③） ----------

    def test_merge_across_caption_refused(self):
        """选区跨题注 → 拒绝合并（不吞题注、不跨题注拼正文）。

        真·负控：去掉保护后 merge 收集会拿到 [甲段, 题注, 乙段] 并合成一段，
        输出与本用例的「逐字节不变」断言不同。
        """
        html = self._html()
        ok, out = self._apply(html, "甲段", "merge", "乙段")
        self.assertFalse(ok)
        self.assertEqual(out, html)

    def test_merge_with_adjacent_caption_refused(self):
        """选区仅一个块、紧邻兄弟是题注 → 拒绝合并（题注不被并进上一段）。"""
        html = f"<p>甲段</p>{CAPTION_HTML}"
        ok, out = self._apply(html, "甲段", "merge")
        self.assertFalse(ok)
        self.assertEqual(out, html)

    def test_merge_before_caption_still_merges(self):
        """题注**之前**的两个块照常合并，题注本身不被改动（保护不过度）。"""
        html = f"<p>甲段</p><p>乙段</p>{CAPTION_HTML}"
        ok, out = self._apply(html, "甲段", "merge", "乙段")
        self.assertTrue(ok)
        self.assertEqual(out, f"<p>甲段 乙段</p>{CAPTION_HTML}")

    def test_merge_after_caption_still_merges(self):
        """题注**之后**的两个块照常合并，题注本身不被改动（保护不过度）。

        本条是回归闸门而非负控：去掉保护后输出相同。它锁住的是「起点守卫不得
        顺着兄弟链误伤后续块」——merge 收集在题注处停止后仍要让区间内其余块生效。
        """
        html = f"{CAPTION_HTML}<p>甲段</p><p>乙段</p>"
        ok, out = self._apply(html, "甲段", "merge", "乙段")
        self.assertTrue(ok)
        self.assertEqual(out, f"{CAPTION_HTML}<p>甲段 乙段</p>")

    # ---------- 4. 跨块非 merge（保护④） ----------

    def test_cross_block_format_excludes_middle_caption(self):
        """跨块格式化跳过区间内的题注，首尾块照常生效。"""
        html = f"<h2>甲</h2>{CAPTION_HTML}<h2>乙</h2>"
        ok, out = self._apply(html, "甲", "heading1", "乙")
        self.assertTrue(ok)
        self.assertIn("<h1>甲</h1>", out)
        self.assertIn("<h1>乙</h1>", out)
        self.assertIn(CAPTION_HTML, out)

    def test_regex_span_really_crosses_caption_and_leaves_it_intact(self):
        """真·负控：正则匹配区间确实跨过题注块，题注仍逐字节保留。

        ⚠️ 本用例存在的理由：第一版「区间压着题注」用例在开/关保护两种模式下
        输出完全相同 → 断言恒 PASS（假绿）。根因是 merge 收集循环在
        `cur_block is end_block` 处就 break，根本没走到题注。
        因此这里先断言匹配区间**真的跨过题注**（纯文本里题注文字落在 match 内），
        再断言题注原样——去掉保护时题注会被改写成 h1，用例必 FAIL。
        """
        html = f"<p>甲段乙段</p>{CAPTION_HTML}<p>丙段</p>"
        plain = collect_text_nodes(parse_html(html))[0]
        rules = [{
            "id": "r1", "name": "标题1", "mode": "all",
            "conditions": [{
                "type": "regex", "pattern": r"甲段乙段[\s\S]*丙段",
                "scope": "page", "formats": ["heading1"],
            }],
        }]
        out, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # 前置条件：匹配区间真的跨过题注（否则本用例退化为假绿）
        self.assertIn(CAPTION_TEXT, plain)
        self.assertLess(plain.index("甲段"), plain.index(CAPTION_TEXT))
        self.assertLess(plain.index(CAPTION_TEXT), plain.index("丙段"))
        self.assertIn("<h1>甲段乙段</h1>", out)
        self.assertIn("<h1>丙段</h1>", out)
        self.assertIn(CAPTION_HTML, out)

    # ---------- 5. 回归：前缀不误伤 + 分隔线不受影响 ----------

    def test_caption_lookalike_class_not_protected(self):
        """ptoe-captionx 不受保护：合并照常（证明保护不是「凡 ptoe-caption* 都拦」）。"""
        html = '<p>甲段</p><p class="ptoe-captionx">X</p><p>乙段</p>'
        ok, out = self._apply(html, "甲段", "merge", "乙段")
        self.assertTrue(ok)
        self.assertEqual(out, "<p>甲段 X 乙段</p>")
        # 口径分叉守卫（2026-10-01）：解析期白名单**同样**必须剥掉 ptoe-captionx。
        # 这是「严格前缀」与「宽松 startswith」唯一的可观测差异——若白名单照抄
        # 分隔线的宽松口径，题注判定(_is_caption_block，严格)与白名单就会分叉：
        # 白名单放行而判定不认（fail-open，无害但两侧口径不一致），日后任一侧
        # 改口径都极易漏掉另一侧。本断言把「两侧口径必须一致」钉死。
        self.assertNotIn(
            "ptoe-captionx", serialize_html(parse_html(html))
        )
        self.assertIn('class="ptoe-caption-s"',
                      serialize_html(parse_html('<p class="ptoe-caption-s">X</p>')))

    def test_divider_protection_unaffected(self):
        """题注保护未改变分隔线的既有行为（2026-09-27 回归）。"""
        div = '<p class="ptoe-divider ptoe-divider-dashed">X</p>'
        # 起点守卫
        ok, out = self._apply(f"<p>甲</p>{div}<p>丙</p>", "X", "heading1")
        self.assertFalse(ok)
        self.assertEqual(out, f"<p>甲</p>{div}<p>丙</p>")
        # 跨线合并拒绝
        ok, out = self._apply(f"<p>甲</p>{div}<p>丙</p>", "甲", "merge", "丙")
        self.assertFalse(ok)
        self.assertEqual(out, f"<p>甲</p>{div}<p>丙</p>")
        # 线前合并仍生效
        ok, out = self._apply(f"<p>甲</p><p>乙</p>{div}", "甲", "merge", "乙")
        self.assertTrue(ok)
        self.assertEqual(out, f"<p>甲 乙</p>{div}")

    def test_caption_and_divider_neighbours_independent(self):
        """题注与分隔线相邻：各自的保护互不干扰（题注仍受保护，分隔线仍受保护）。"""
        div = '<p class="ptoe-divider ptoe-divider-double">X</p>'
        html = f"<p>甲段</p>{div}{CAPTION_HTML}<p>乙段</p>"
        ok, out = self._apply(html, CAPTION_TEXT, "heading1")
        self.assertFalse(ok)
        self.assertEqual(out, html)
        ok, out = self._apply(html, "X", "heading1")
        self.assertFalse(ok)
        self.assertEqual(out, html)

    # ---------- 6. 目录不泄漏 ----------

    def test_caption_never_enters_toc(self):
        """题注是普通段落（不是标题），走真实 _render_fragment 不进目录。

        这是保护①的最终目的：一条命中「图1」的规则若把题注改成 h1，题注就会
        作为目录条目出现在 nav.xhtml 里。此处直接断言 toc 收集结果不含题注文字。
        """
        import tempfile

        import htmlmanage

        conv = htmlmanage.HTMLConverter(tempfile.mkdtemp(prefix="t_cap_"))
        toc: list = []
        body = conv._render_fragment(
            f"<h1>第一章</h1><p>正文。</p>{CAPTION_HTML}", toc_out=toc
        )
        self.assertIn(CAPTION_HTML, body)
        titles = [t["title"] for t in toc]
        self.assertEqual(titles, ["第一章"], toc)
        self.assertNotIn(CAPTION_TEXT, titles)

    # ---------- 7. 解析往返 ----------

    def test_caption_class_survives_round_trip(self):
        """解析 → 序列化保留 ptoe-caption（白名单漏登记会被静默剥掉，同型 bug）。"""
        out = serialize_html(parse_html(CAPTION_HTML))
        self.assertEqual(out, CAPTION_HTML)
        # 走一遍真实规则链路后 class 仍在（每应用一次规则都不该被剥掉）
        rules = [{
            "id": "r1", "name": "加粗", "mode": "all",
            "conditions": [{
                "type": "contains", "pattern": "示例插图",
                "scope": "page", "formats": ["bold"],
            }],
        }]
        out, err = apply_rules(CAPTION_HTML, rules, all_rules=True)
        self.assertIsNone(err)
        self.assertIn('class="ptoe-caption"', out)
        self.assertIn("<strong>示例插图</strong>", out)

    # ---------- 8. 带连字符后缀的题注端到端保护（2026-10-01 已修） ----------

    def test_suffixed_caption_class_protected_end_to_end(self):
        """ptoe-caption-xxx 端到端受保护（解析期白名单已改为严格前缀）。

        历史 bug：_is_caption_block 用「精确相等 或 前缀+连字符」口径，但解析侧
        块级白名单（MiniDOMParser.handle_starttag 的 BLOCK_TAGS 分支）只放行
        **精确相等**的 "ptoe-caption"，ptoe-caption-xxx 在解析期即被剥掉 →
        带后缀的题注在链路上根本不带该 class，_is_caption_block 的前缀分支成了
        端到端死代码。

        修复口径：白名单保留集合内的精确 "ptoe-caption"（基类），另加一行
        `c.startswith("ptoe-caption-")`。**刻意不用**分隔线的宽松
        startswith("ptoe-divider")：宽松口径会把 ptoe-captionx 这类无连字符的
        无关 class 误判为受保护，与 _is_caption_block 的严格口径分叉
        （见同文件 test_caption_lookalike_class_not_protected）。

        本用例即当初挂 @unittest.expectedFailure 的探针，修复后已转正。
        """
        html = '<p>甲段</p><p class="ptoe-caption-s">图1示例插图</p><p>乙段</p>'
        self.assertIn('class="ptoe-caption-s"', serialize_html(parse_html(html)))
        ok, out = self._apply(html, CAPTION_TEXT, "merge", "乙段")
        self.assertFalse(ok, "带后缀的题注应受保护（跨它合并须被拒绝）")
        self.assertEqual(out, html)


# ===========================================================================
# float 绕排图片类白名单（2026-10 图文混合）
# ===========================================================================

class TestFloatImgClassWhitelist(unittest.TestCase):
    """ptoe-img-float-left / right 经「解析 → 应用规则 → 序列化」往返仍存活。

    背景：MiniDOMParser 的 img 分支有一份独立的 class 白名单
    （handle_starttag 的 `allowed_img_classes`，2026-10-01 加 float 时补齐）。
    漏登记的后果**不是报错而是静默丢类**：矫正界面的格式规则每次应用都会重解析
    页面 HTML，没进白名单的图片类当场消失 —— 图片从绕排悄悄退回独占块，
    而规则本身报 success。本类锁死这条链路。

    白名单**恰好少一项**时本类会红（已用 %TEMP% 副本做真·负控验证，见报告）。
    """

    _LEFT = "ptoe-img-float-left"
    _RIGHT = "ptoe-img-float-right"

    def _page(self, *classes):
        """绕排图所在页：图与后文同段（CSS float 必须同段文字才绕排）。

        多个类以**多个参数**传入（别塞进一个字符串再靠 or 兜底 —— 那会把
        后面的类悄悄丢掉，写测试时真踩过：or 短路使 w50 无声消失）。
        """
        joined = " ".join(c for c in classes if c) or self._LEFT
        return (
            f'<p>绕排段落：<img class="{joined}" src="a.png" alt="插图"/>'
            f'文字继续绕排。</p><p>乙段</p>'
        )

    def test_float_classes_survive_parse_serialize(self):
        """① 解析 → 序列化往返存活（含配套的尺寸 / 垂直对齐类）。"""
        for cls in (self._LEFT, self._RIGHT, f"{self._LEFT} ptoe-img-w50",
                    f"{self._RIGHT} ptoe-img-w25 ptoe-img-vtop"):
            with self.subTest(cls=cls):
                out = serialize_html(parse_html(self._page(cls)))
                got = (_class_of(out, "img") or "").split()
                for c in cls.split():
                    self.assertIn(c, got, out)

    def test_float_classes_survive_inline_rule(self):
        """② 应用行内规则（加粗）后 float 类仍在（规则不许顺手改图片版式）。"""
        out, err = apply_rules(
            self._page(), [{
                "id": "r1", "name": "加粗", "enabled": True,
                "conditions": [{"type": "contains", "pattern": "文字",
                                "formats": ["bold"], "mode": "all"}],
                "formats": [],
            }], all_rules=True
        )
        self.assertIsNone(err)
        self.assertIn(self._LEFT, (_class_of(out, "img") or "").split())
        self.assertIn("<strong>", out)
        self.assertIn("绕排段落：", out)

    def test_float_classes_survive_block_rule(self):
        """③ 应用块级规则（居中）后 float 类仍在，段落拿到 ptoe-align-center。"""
        out, err = apply_rules(
            self._page(self._RIGHT, "ptoe-img-w50"), [{
                "id": "r2", "name": "居中", "enabled": True,
                "conditions": [{"type": "contains", "pattern": "文字",
                                "formats": ["align_center"], "mode": "all"}],
                "formats": [],
            }], all_rules=True
        )
        self.assertIsNone(err)
        got = (_class_of(out, "img") or "").split()
        self.assertIn(self._RIGHT, got)
        self.assertIn("ptoe-img-w50", got)
        self.assertIn("ptoe-align-center", out)

    def test_float_img_survives_merge_of_its_paragraph(self):
        """④ 绕排图所在段被 merge 进相邻段后，图片与 float 类都不丢。"""
        root = parse_html(self._page())
        text = collect_text_nodes(root)[0]
        start, end = text.index("绕排段落"), len(text)
        ok = apply_block_format(root, collect_text_nodes(root)[1], start, end, "merge")
        out = serialize_html(root)
        self.assertTrue(ok)
        self.assertIn(self._LEFT, (_class_of(out, "img") or "").split())
        self.assertIn("乙段", out)

    def test_img_keeps_src_and_alt_alongside_float_class(self):
        """⑤ src / alt 与 float 类共存（图片本身仍可被加载与替换）。"""
        out = serialize_html(parse_html(self._page()))
        img = _img_tag_of(out)
        self.assertIn('src="a.png"', img)
        self.assertIn('alt="插图"', img)
        self.assertIn(self._LEFT, img)

    def test_non_whitelisted_img_classes_stripped(self):
        """负控：白名单外的图片类**确实**会被剥（证明上几条不是「全放行」的假绿）。

        这里刻意不写 float 的近似名（如 ptoe-img-floatw）—— img 白名单用精确
        成员判断（不是前缀），近似名与 block 侧 ptoe-caption 的口径不同，
        混用会让人误以为两边一致。
        """
        out = serialize_html(parse_html(
            self._page("ptoe-img-float-left ptoe-img-shadow ptoe-float-left")
        ))
        got = (_class_of(out, "img") or "").split()
        self.assertIn(self._LEFT, got)
        self.assertNotIn("ptoe-img-shadow", got)
        self.assertNotIn("ptoe-float-left", got)


def _class_of(html: str, tag: str) -> str | None:
    """取首个 <tag> 的 class 属性值（无则 None）——反序列化后属性序不定，
    不能按 substrings 猜，故按标签取。"""
    m = re.search(r"<%s\b([^>]*)>" % re.escape(tag), html)
    if not m:
        return None
    c = re.search(r'\bclass\s*=\s*"([^"]*)"', m.group(1))
    return c.group(1) if c else None


def _img_tag_of(html: str) -> str:
    """取首个 <img> 开标签原文。"""
    m = re.search(r"<img\b[^>]*>", html)
    if not m:
        raise AssertionError(f"未找到 img 标签：{html!r}")
    return m.group(0)


if __name__ == "__main__":
    unittest.main()