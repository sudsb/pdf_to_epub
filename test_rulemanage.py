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

import unittest
from rulemanage import (
    parse_html,
    serialize_html,
    collect_text_nodes,
    apply_rules,
    VALID_FORMAT_OPS,
    parse_regex_pattern,
    Rule,
    Condition,
    eval_format_rule,
    op_group,
    ops_conflict,
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
        self.assertEqual(result.fmt_entries[0]["fmts"], ["bold"])
        self.assertEqual(len(result.fmt_entries), 1)

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
        self.assertEqual(len(result.fmt_entries), 2)
        self.assertEqual(result.fmt_entries[0]["fmts"], ["bold"])
        self.assertEqual(result.fmt_entries[1]["fmts"], ["italic"])

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
        # none 被过滤，fmt_entries 为空但仍停止
        self.assertEqual(result.fmt_entries[0]["fmts"], [])

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
            "name": "Align测试",
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
        self.assertIn('class="ptoe-align-center"', new_html)

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
        html = "<p>测试文本</p>"
        rules = [{
            "id": "r1",
            "name": "Conflict测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": "测试",
                "scope": "page",
                "formats": ["align_left", "align_center"],  # 冲突：align 组
            }],
        }]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # first-wins：只应用第一个 align_left
        self.assertIn('class="ptoe-align-left"', new_html)
        self.assertNotIn('class="ptoe-align-center"', new_html)

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
                    "formats": ["align_center"],  # 与 r1 冲突
                }],
            },
        ]
        new_html, err = apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        # 跨规则冲突：r1 先应用 align_left，r2 的 align_center 被跳过
        self.assertIn('class="ptoe-align-left"', new_html)
        self.assertNotIn('class="ptoe-align-center"', new_html)

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


if __name__ == "__main__":
    unittest.main()