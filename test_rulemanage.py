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
    _is_dangerous,
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


if __name__ == "__main__":
    unittest.main()