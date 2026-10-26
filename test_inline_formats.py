# -*- coding: utf-8 -*-
"""新增行内格式（2026-10-21）与「清理段落内空白」的回归护栏。

覆盖的不变量：
  F1 rulemanage.INLINE_FORMAT_CLASSES 是行内格式类的单一事实来源：
     新增的波浪线/双下划线/上划线/着重号/多色突显/拼音注音都在册；
     前端（ui/app.js、ui/epubedit.js）的 INLINE_CLASSES 与之完全一致。
  F2 sanitize_html 放行新类；拼音注音的 data-rt 被规范化后保留（非法内容丢弃）。
  F3 富文本解析 → DOCX 导出：波浪→w:u wave、双下划线→w:u double、
     着重号→w:em dot、多色突显→w:shd 填色、拼音注音→正文+括号注音。
  F4 EPUB 样式表（htmlmanage）含新类的 CSS 规则。
  F5 智能清理「去除段落内空白」：删除水平空白、保留换行、
     英文单词之间保留单个空格（可关）。
  F6 stringmanage.strip_inline_spaces 的边界行为（幂等、不动换行）。
  F7 静态护栏：前端菜单/设置项已接入（避免只改后端漏改前端）。
"""
import io
import os
import re
import unittest
import zipfile

import correctmanage
import htmlmanage
import rulemanage
import stringmanage

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_JS = os.path.join(ROOT, "ui", "app.js")
EPUBEDIT_JS = os.path.join(ROOT, "ui", "epubedit.js")

NEW_CLASSES = (
    "ptoe-wavy",
    "ptoe-double",
    "ptoe-overline",
    "ptoe-emphasis",
    "ptoe-highlight-green",
    "ptoe-highlight-blue",
    "ptoe-highlight-pink",
    "ptoe-highlight-orange",
    "ptoe-highlight-purple",
    "ptoe-ruby",
)


def read(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return f.read()


class InlineClassRegistryTest(unittest.TestCase):
    """F1：类名单一事实来源 + 前端一致。"""

    def test_new_classes_registered(self):
        for cls in NEW_CLASSES:
            self.assertIn(cls, rulemanage.INLINE_FORMAT_CLASSES)

    def test_highlight_colors_cover_all_highlight_classes(self):
        for cls in rulemanage.INLINE_FORMAT_CLASSES:
            if cls.startswith("ptoe-highlight"):
                self.assertIn(cls, rulemanage.HIGHLIGHT_COLORS)

    def test_frontend_inline_classes_match_backend(self):
        app = read(APP_JS)
        edit = read(EPUBEDIT_JS)
        for cls in NEW_CLASSES:
            self.assertIn(cls, app, "ui/app.js 缺少类 %s" % cls)
            self.assertIn(cls, edit, "ui/epubedit.js 缺少类 %s" % cls)

    def test_rule_ops_registered(self):
        # 格式规则面板的操作名必须被后端接受
        for op in ("wavy", "double", "overline", "emphasis"):
            self.assertIn(op, correctmanage._VALID_FORMAT_OPS)
        for color in ("green", "blue", "pink", "orange", "purple"):
            self.assertIn("highlight_" + color, correctmanage._VALID_FORMAT_OPS)


class SanitizeTest(unittest.TestCase):
    """F2：清洗放行 + 注音属性处理。"""

    def test_new_classes_survive_sanitize(self):
        html = '<p>a<span class="ptoe-wavy">b</span><span class="ptoe-emphasis">c</span></p>'
        out = correctmanage.sanitize_html(html)
        self.assertIn("ptoe-wavy", out)
        self.assertIn("ptoe-emphasis", out)

    def test_ruby_data_rt_kept_and_normalized(self):
        out = correctmanage.sanitize_html(
            '<p><span class="ptoe-ruby" data-rt="hàn">汉</span></p>'
        )
        self.assertIn('data-rt="hàn"', out)

    def test_ruby_data_rt_sanitized(self):
        # 含控制字符/超长的注音 → 规范化（截断 32 字），不注入引号
        out = correctmanage.sanitize_html(
            '<p><span class="ptoe-ruby" data-rt="a&#10;b">汉</span></p>'
        )
        self.assertIn("data-rt=", out)
        self.assertNotIn("\n", out.split("data-rt=")[1].split('"')[0])

    def test_ruby_without_rt_keeps_class_only(self):
        out = correctmanage.sanitize_html(
            '<p><span class="ptoe-ruby" data-rt="">汉</span></p>'
        )
        self.assertIn("ptoe-ruby", out)
        self.assertNotIn("data-rt", out)

    def test_style_to_inline_class(self):
        cases = {
            "text-decoration: underline wavy": "ptoe-wavy",
            "text-decoration: underline double": "ptoe-double",
            "text-decoration: overline": "ptoe-overline",
            "text-emphasis: dot": "ptoe-emphasis",
            "text-decoration: underline": "ptoe-underline",
            "text-decoration: line-through": "ptoe-strike",
        }
        for style, expect in cases.items():
            got = None
            for pat, cls in correctmanage._STYLE_TO_INLINE_CLASS:
                if pat.search(style):
                    got = cls
                    break
            self.assertEqual(expect, got, "style=%s" % style)


class DocxExportTest(unittest.TestCase):
    """F3：DOCX 导出映射。"""

    def _docx_xml(self, html):
        blocks = correctmanage._html_to_rich_blocks(html)
        path = os.path.join(ROOT, "_test_inline.docx")
        try:
            correctmanage._build_docx(blocks, path)
            with zipfile.ZipFile(path) as zf:
                return zf.read("word/document.xml").decode("utf-8")
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_wavy_underline(self):
        xml = self._docx_xml('<p><span class="ptoe-wavy">x</span></p>')
        self.assertIn('<w:u w:val="wave"/>', xml)

    def test_double_underline(self):
        xml = self._docx_xml('<p><span class="ptoe-double">x</span></p>')
        self.assertIn('<w:u w:val="double"/>', xml)

    def test_emphasis_dot(self):
        xml = self._docx_xml('<p><span class="ptoe-emphasis">x</span></p>')
        self.assertIn('<w:em w:val="dot"/>', xml)

    def test_colored_highlight(self):
        xml = self._docx_xml('<p><span class="ptoe-highlight-green">x</span></p>')
        self.assertIn('w:fill="C8E6C9"', xml)

    def test_ruby_annotation_kept(self):
        xml = self._docx_xml(
            '<p><span class="ptoe-ruby" data-rt="hàn">汉</span></p>'
        )
        self.assertIn("(h", xml)  # 括号注音（小字 run）
        self.assertIn("hàn", xml)


class EpubCssTest(unittest.TestCase):
    """F4：EPUB 样式表。"""

    def test_css_contains_new_classes(self):
        css = htmlmanage.CSSManager().generate_stylesheet()
        for cls in ("ptoe-wavy", "ptoe-double", "ptoe-overline", "ptoe-emphasis"):
            self.assertIn(cls, css)
        for cls in ("ptoe-highlight-green", "ptoe-highlight-blue"):
            self.assertIn(cls, css)


class CleanSpacesTest(unittest.TestCase):
    """F5：智能清理「去除段落内空白」。"""

    def test_removes_spaces_but_keeps_newlines(self):
        out = correctmanage.clean_page_html(
            "<p>你 好</p>\n<p>第二 段</p>", clean_spaces=True
        )
        self.assertIn("你好", out)
        self.assertIn("第二段", out)
        self.assertIn("\n", out)

    def test_collapses_ocr_soft_newline_in_paragraph(self):
        # OCR 把一行拆成多行：浏览器把 \n 渲染成空格，只删空格会留下可见空隙
        out = correctmanage.clean_page_html(
            "<p>关于同意发表《克服教条主义，\n投入反右派斗争》一文的批语〔1〕</p>",
            clean_spaces=True,
        )
        self.assertIn("教条主义，投入反右派斗争", out)
        self.assertNotIn("主义，\n投入", out)

    def test_newline_between_english_words_becomes_one_space(self):
        out = correctmanage.clean_page_html(
            "<p>hello \n world</p>", clean_spaces=True
        )
        self.assertIn("hello world", out)
        self.assertNotIn("\n", out)

    def test_align_lines_block_keeps_newlines(self):
        out = correctmanage.clean_page_html(
            '<p class="ptoe-align-lines">甲 乙\n丙 丁</p>', clean_spaces=True
        )
        # 水平空格清除，但逐行排版块必须保留换行
        self.assertIn("甲乙\n丙丁", out)

    def test_removes_invisible_unicode_spaces(self):
        out = correctmanage.clean_page_html(
            "<p>甲\u200b乙\u00ad丙\u2028丁\u2060戊</p>", clean_spaces=True
        )
        self.assertIn("甲乙丙丁戊", out)
        for ch in ("\u200b", "\u00ad", "\u2028", "\u2060"):
            self.assertNotIn(ch, out)

    def test_table_content_is_not_rewritten(self):
        # 表格清洗涉及列宽/结构，clean_spaces 保持逐字节透传（与历史行为一致）
        out = correctmanage.clean_page_html(
            "<table><tr><td>你 好</td></tr></table>", clean_spaces=True
        )
        self.assertIn("你 好", out)

    def test_keeps_english_word_gap_by_default(self):
        out = correctmanage.clean_page_html(
            "<p>hello   world</p>", clean_spaces=True
        )
        self.assertIn("hello world", out)

    def test_can_drop_english_word_gap(self):
        out = correctmanage.clean_page_html(
            "<p>hello   world</p>",
            clean_spaces=True,
            clean_spaces_keep_word_gap=False,
        )
        self.assertIn("helloworld", out)

    def test_disabled_by_default(self):
        out = correctmanage.clean_page_html("<p>你 好</p>")
        self.assertIn("你 好", out)

    def test_stats_spaces_counter(self):
        html, stats = correctmanage.clean_page_html(
            "<p>你 好</p>", clean_spaces=True, with_stats=True
        )
        self.assertIn("你好", html)
        self.assertEqual(1, stats.get("spaces"))


class StripInlineSpacesTest(unittest.TestCase):
    """F6：stringmanage.strip_inline_spaces 边界行为。"""

    def test_keeps_newlines(self):
        self.assertEqual(
            stringmanage.strip_inline_spaces("a \n b"), "a\nb"
        )

    def test_collapses_english_gap(self):
        self.assertEqual(
            stringmanage.strip_inline_spaces("a    b"), "a b"
        )

    def test_removes_cjk_gap(self):
        self.assertEqual(stringmanage.strip_inline_spaces("中 文"), "中文")

    def test_drops_all_when_gap_disabled(self):
        self.assertEqual(
            stringmanage.strip_inline_spaces("a b", keep_word_gap=False), "ab"
        )

    def test_idempotent(self):
        once = stringmanage.strip_inline_spaces("中  文 hello  world")
        twice = stringmanage.strip_inline_spaces(once)
        self.assertEqual(once, twice)

    def test_empty(self):
        self.assertEqual(stringmanage.strip_inline_spaces(""), "")
        self.assertEqual(stringmanage.strip_inline_spaces(None), "")

    def test_can_collapse_newlines(self):
        self.assertEqual(
            stringmanage.strip_inline_spaces("中，\n投入", keep_newlines=False),
            "中，投入",
        )
        self.assertEqual(
            stringmanage.strip_inline_spaces("hello \n world", keep_newlines=False),
            "hello world",
        )

    def test_collapse_newlines_without_word_gap(self):
        self.assertEqual(
            stringmanage.strip_inline_spaces(
                "hello \n world", keep_word_gap=False, keep_newlines=False
            ),
            "helloworld",
        )

    def test_invisible_chars_removed(self):
        self.assertEqual(
            stringmanage.strip_inline_spaces("甲\u200b乙\u00ad丙"),
            "甲乙丙",
        )


class FrontendStaticTest(unittest.TestCase):
    """F7：前端接入的静态护栏。"""

    @classmethod
    def setUpClass(cls):
        cls.app = read(APP_JS)
        cls.edit = read(EPUBEDIT_JS)
        cls.cm = read(os.path.join(ROOT, "correctmanage.py"))

    def test_app_has_new_inline_classes(self):
        for cls in NEW_CLASSES:
            self.assertIn(cls, self.app)

    def test_app_labels_present(self):
        for label in ("wavy:", "double:", "overline:", "emphasis:", "ruby:"):
            self.assertIn(label, self.app)

    def test_charwrap_menu_has_new_entries(self):
        for wrap in ("wavy", "double", "overline", "emphasis", "ruby",
                     "highlight-green", "highlight-blue"):
            self.assertIn('data-wrap="%s"' % wrap, self.cm)

    def test_app_supports_ruby_attribute(self):
        self.assertIn("data-rt", self.app)

    def test_clean_spaces_settings_wired(self):
        self.assertIn("cleanSpacesChk", self.cm)
        self.assertIn("cleanSpacesChk", self.app)
        self.assertIn("clean_spaces", self.app)
        self.assertIn("clean_spaces_keep_word_gap", self.app)

    def test_config_defaults(self):
        import configmanage

        defaults = configmanage.CLEAN_SETTINGS_DEFAULTS
        self.assertIn("clean_spaces", defaults)
        self.assertIn("clean_spaces_keep_word_gap", defaults)
        self.assertFalse(defaults["clean_spaces"])
        self.assertTrue(defaults["clean_spaces_keep_word_gap"])


class RubyModalTest(unittest.TestCase):
    """F8：拼音注音走应用内弹窗（内嵌 WebView2 不保证支持 window.prompt）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = read(APP_JS)
        cls.cm = read(os.path.join(ROOT, "correctmanage.py"))

    def test_modal_markup_exists(self):
        for mark in ('id="rubyModalBg"', 'id="rubyInput"', 'id="rubyOkBtn"',
                     'id="rubyCancelBtn"', 'id="rubyCloseBtn"'):
            self.assertIn(mark, self.cm)

    def test_modal_functions_exist(self):
        for fn in ("function openRubyModal(", "function closeRubyModal(",
                   "function applyRubyModal("):
            self.assertIn(fn, self.app)

    def test_modal_is_bound(self):
        self.assertIn("bindRubyModal", self.app)
        self.assertIn("applyRubyModal", self.app)

    def test_no_window_prompt_used(self):
        # window.prompt 在 WebView2 中不可用（返回 null），必须走内置弹窗
        self.assertNotIn("window.prompt(", self.app)

    def test_context_menu_has_new_entries(self):
        for wrap in ("wavy", "double", "overline", "emphasis", "ruby",
                     "highlight-green", "highlight-blue"):
            self.assertIn('data-ctx-wrap="%s"' % wrap, self.cm)


class RuleEngineInlineFormatTest(unittest.TestCase):
    """F9：格式规则可套用新增行内格式（新操作必须被当作“行内”操作）。"""

    def _apply(self, formats, pattern="你好"):
        html = "<p>你好世界</p>"
        rules = [{
            "id": "r1",
            "name": "新格式测试",
            "mode": "first",
            "conditions": [{
                "type": "contains",
                "pattern": pattern,
                "scope": "page",
                "formats": list(formats),
            }],
        }]
        new_html, err = rulemanage.apply_rules(html, rules, all_rules=True)
        self.assertIsNone(err)
        return new_html

    def test_new_ops_are_inline_ops(self):
        for op in ("wavy", "double", "overline", "emphasis",
                   "highlight_green", "highlight_blue", "highlight_pink",
                   "highlight_orange", "highlight_purple"):
            self.assertIn(op, rulemanage._INLINE_LEAF_OPS)
            self.assertIn(op, rulemanage._INLINE_LEAF_OPS_M)

    def test_wavy(self):
        self.assertIn('class="ptoe-wavy"', self._apply(["wavy"]))

    def test_double(self):
        self.assertIn('class="ptoe-double"', self._apply(["double"]))

    def test_overline(self):
        self.assertIn('class="ptoe-overline"', self._apply(["overline"]))

    def test_emphasis(self):
        self.assertIn('class="ptoe-emphasis"', self._apply(["emphasis"]))

    def test_colored_highlight(self):
        out = self._apply(["highlight_green"])
        self.assertIn('class="ptoe-highlight-green"', out)

    def test_new_ops_in_valid_format_ops(self):
        for op in ("wavy", "double", "overline", "emphasis",
                   "highlight_green", "highlight_blue", "highlight_pink",
                   "highlight_orange", "highlight_purple"):
            self.assertIn(op, rulemanage.VALID_FORMAT_OPS)


if __name__ == "__main__":
    unittest.main()
