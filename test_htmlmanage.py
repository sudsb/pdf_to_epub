"""test_htmlmanage.py — unittest suite for htmlmanage module.

Covers:
- transform_note_labels: 加粗注释标签转换（注　　释：+ 顶格 class）
- CSSManager.generate_stylesheet: 包含 p.ptoe-note-label 规则
- HTMLConverter.convert_document: 集成测试（输出 XHTML 含 注　　释：且对应块带 ptoe-note-label，CSS 含新规则）
"""

import os
import tempfile
import unittest
from pathlib import Path

import htmlmanage


class TestTransformNoteLabels(unittest.TestCase):
    """transform_note_labels 单测"""

    def test_basic_replacement(self):
        """基本替换：<strong>注释</strong> -> <strong>注　　释：</strong>（加粗保留）"""
        html = '<p><strong>注释</strong>这是内容</p>'
        result = htmlmanage.transform_note_labels(html)
        self.assertIn('注释\uFF1A', result)
        # 转换后的「注释：」仍保留加粗
        self.assertIn('<strong>注释\uFF1A</strong>', result)
        self.assertNotIn('<strong>注释</strong>', result)

    def test_bold_preserved_inside_comment_block(self):
        """注释块（ptoe-note）内加粗「注释」转换后仍保留加粗 + 注入顶格 class"""
        html = '<p class="ptoe-note"><strong>注释</strong>内容</p>'
        result = htmlmanage.transform_note_labels(html)
        self.assertIn('<strong>注释\uFF1A</strong>', result)
        self.assertIn('ptoe-note', result)
        self.assertIn('ptoe-note-label', result)

    def test_b_tag_replacement(self):
        """<b> 标签也应被替换"""
        html = '<p><b>注释</b>这是内容</p>'
        result = htmlmanage.transform_note_labels(html)
        self.assertIn('注释\uFF1A', result)
        self.assertNotIn('<b>注释</b>', result)

    def test_colon_inside_tag(self):
        """标签内带冒号：<strong>注释：</strong> -> 注　　释： (去重)"""
        html = '<p><strong>注释：</strong>这是内容</p>'
        result = htmlmanage.transform_note_labels(html)
        self.assertIn('注释\uFF1A', result)
        # 不应有双冒号
        self.assertNotIn('\uFF1A\uFF1A', result)

    def test_colon_outside_tag(self):
        """标签外紧跟冒号：<strong>注释</strong>： -> 注　　释： (去重)"""
        html = '<p><strong>注释</strong>：这是内容</p>'
        result = htmlmanage.transform_note_labels(html)
        self.assertIn('注释\uFF1A', result)
        self.assertNotIn('\uFF1A\uFF1A', result)

    def test_tag_with_whitespace(self):
        """标签内有空白：<strong> 注释 </strong> -> 注　　释："""
        html = '<p><strong> 注释 </strong>这是内容</p>'
        result = htmlmanage.transform_note_labels(html)
        self.assertIn('注释\uFF1A', result)

    def test_multiple_matches_same_block(self):
        """同一块内多处匹配只加一次 class"""
        html = '<p><strong>注释</strong>第一处<strong>注释</strong>第二处</p>'
        result = htmlmanage.transform_note_labels(html)
        # 应该有两个替换
        self.assertEqual(result.count('注释\uFF1A'), 2)
        # class 只加一次
        self.assertEqual(result.count('ptoe-note-label'), 1)

    def test_class_appended_to_existing(self):
        """class 追加到已有 class 的块"""
        html = '<p class="ptoe-note"><strong>注释</strong>内容</p>'
        result = htmlmanage.transform_note_labels(html)
        self.assertIn('class="ptoe-note ptoe-note-label"', result)
        # 或 class="ptoe-note-label ptoe-note" 顺序不重要
        self.assertIn('ptoe-note-label', result)
        self.assertIn('ptoe-note', result)

    def test_non_bold_not_replaced(self):
        """非加粗的「注释」不替换"""
        html = '<p>注释这是内容</p>'
        result = htmlmanage.transform_note_labels(html)
        self.assertNotIn('注释\uFF1A', result)
        self.assertIn('注释', result)

    def test_nested_tags_not_crash(self):
        """嵌套标签如 <strong><em>注释</em></strong> 不崩溃（可不替换但不报错）"""
        html = '<p><strong><em>注释</em></strong>内容</p>'
        result = htmlmanage.transform_note_labels(html)
        # 不崩溃即可，具体是否替换视实现而定
        self.assertIsInstance(result, str)

    def test_empty_html(self):
        """空 HTML 返回原样"""
        self.assertEqual(htmlmanage.transform_note_labels(''), '')
        # None 输入返回 None（函数开头有判断）
        self.assertIsNone(htmlmanage.transform_note_labels(None))

    def test_no_match_returns_original(self):
        """无匹配时返回原 HTML"""
        html = '<p>普通段落</p>'
        result = htmlmanage.transform_note_labels(html)
        self.assertEqual(result, html)


class TestCSSManagerStylesheet(unittest.TestCase):
    """CSSManager.generate_stylesheet 包含 p.ptoe-note-label 规则"""

    def test_note_label_rule_exists(self):
        cssm = htmlmanage.CSSManager()
        css = cssm.generate_stylesheet()
        self.assertIn('p.ptoe-note-label', css)
        self.assertIn('text-indent: 0', css)
        # 注释说明
        self.assertIn('注释标签顶格', css)

    def test_h1_rule_exists(self):
        """测试 h1 规则：红色 RGB(255,0,0) + 分割线 + 居中紧凑间距"""
        cssm = htmlmanage.CSSManager()
        css = cssm.generate_stylesheet()
        self.assertIn('h1 {', css)
        self.assertIn('color: #FF0000', css)
        self.assertIn('border-bottom: 1px solid #999', css)
        self.assertIn('padding-bottom: 0.35em', css)
        # 注释说明
        self.assertIn('标题红色 + 标题与正文分割线', css)
        # 2026-08-23 用户反馈：部分阅读器标题不居中、与正文间距过大
        self.assertIn('h1, h2 {', css)
        self.assertIn('text-align: center', css)
        self.assertIn('margin: 0.6em 0 0.35em', css)
        self.assertIn('margin: 0.4em 0', css)
        # 2026-08-23 修复：h3-h6 也需居中（CSS 与内联保持一致）
        self.assertIn('h3, h4, h5, h6 {', css)

    def test_note_font_size_rule_covers_all_elements(self):
        """注释字号：对任意承载 ptoe-note 的元素生效 + 嵌套不复合（2026-09-16）。

        症状：导出 EPUB 后「注释格式文本大小不一」——注释类也会落在标题块
        （h1.ptoe-note：规则「标题1 + 注释」叠加，或先设标题再设注释），
        原先选择器只写 p/span，h1 上的注释不匹配 → 保持标题字号（UA 2em），
        与其它注释 0.85em 差一倍；嵌套注释还会 0.85em 逐层缩小（0.72em）。
        编辑器用 .editable .ptoe-note{font-size:12px} 匹配任何元素，故界面看不出。
        """
        cssm = htmlmanage.CSSManager()
        css = cssm.generate_stylesheet()
        # 通用规则（不再限定 p/span）
        self.assertIn(".ptoe-note {", css)
        self.assertNotIn("p.ptoe-note, span.ptoe-note {", css)
        self.assertIn("font-size: 0.85em", css)
        # 嵌套保护
        self.assertIn(".ptoe-note .ptoe-note {", css)
        self.assertIn("font-size: 1em", css)

    def test_default_text_indent(self):
        """测试正文/注释默认顶格（2026-08-23 用户要求：不再全局缩进）"""
        cssm = htmlmanage.CSSManager()
        css = cssm.generate_stylesheet()
        # 全局 p{text-indent:2em} 已移除——正文与注释默认顶格
        self.assertNotIn('p {\n          text-indent: 2em', css)
        # 安全防护：目录和封面明确不缩进
        self.assertIn('nav.toc p, .cover p {', css)
        self.assertIn('text-indent: 0', css)
        # 注释说明
        self.assertIn('正文/注释默认顶格', css)

    def test_format_classes_exist(self):
        """测试手动段落格式类 .ptoe-flush 和 p.ptoe-indent"""
        cssm = htmlmanage.CSSManager()
        css = cssm.generate_stylesheet()
        self.assertIn('.ptoe-flush {', css)
        self.assertIn('text-indent: 0', css)
        self.assertIn('p.ptoe-indent {', css)
        self.assertIn('text-indent: 2em', css)
        # 注释说明
        self.assertIn('顶格/缩进为手动段落格式', css)


class TestCitationItalicConfig(unittest.TestCase):
    """2026-09-22 修复：EPUB 导出 CSS 遵循 citationItalicEnabled 设置。

    generate_stylesheet 默认路径懒读 configmanage.get_config(show_dialogs=False)；
    测试以 monkeypatch 替换 configmanage.get_config（与 test_correctmanage
    _patch_cfg 同法），addCleanup 恢复，避免影响其他套件。
    """

    def _patch_cfg(self, cfg):
        """替换 configmanage.get_config 返回 cfg；记录调用 kwargs 供断言。"""
        import configmanage

        calls = []

        def fake_get_config(*a, **k):
            calls.append(k)
            return cfg

        orig = configmanage.get_config
        configmanage.get_config = fake_get_config
        self.addCleanup(lambda: setattr(configmanage, "get_config", orig))
        return calls

    @staticmethod
    def _citation_rule(css):
        """截取 .ptoe-citation { ... } 规则体（含选择器），用于块内断言。"""
        start = css.index('.ptoe-citation {')
        end = css.index('}', start)
        return css[start:end + 1]

    def test_disabled_emits_normal(self):
        """citationItalicEnabled=False → .ptoe-citation 规则体为 font-style: normal，
        且规则体内绝不出现 font-style: italic。"""
        calls = self._patch_cfg({"citationItalicEnabled": False})
        css = htmlmanage.CSSManager().generate_stylesheet()
        rule = self._citation_rule(css)
        self.assertIn('font-style: normal', rule)
        self.assertNotIn('font-style: italic', rule)
        # font-family（宋体）不受影响
        self.assertIn('font-family: "宋体", SimSun, serif;', rule)
        # 必须以 show_dialogs=False 调用，杜绝 tkinter 对话框
        self.assertTrue(calls, "get_config 未被调用")
        self.assertTrue(all(k.get("show_dialogs") is False for k in calls),
                        f"get_config 须 show_dialogs=False，实得 {calls}")

    def test_enabled_emits_italic(self):
        """citationItalicEnabled=True → 规则体含 font-style: italic（与历史输出一致）。"""
        self._patch_cfg({"citationItalicEnabled": True})
        css = htmlmanage.CSSManager().generate_stylesheet()
        rule = self._citation_rule(css)
        self.assertIn('font-style: italic', rule)
        self.assertNotIn('font-style: normal', rule)

    def test_missing_key_defaults_to_italic(self):
        """配置缺键（不含 citationItalicEnabled）→ 回退默认 True → italic。"""
        self._patch_cfg({})
        css = htmlmanage.CSSManager().generate_stylesheet()
        rule = self._citation_rule(css)
        self.assertIn('font-style: italic', rule)
        self.assertNotIn('font-style: normal', rule)


class TestHTMLConverterIntegration(unittest.TestCase):
    """HTMLConverter.convert_document 集成测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.converter = htmlmanage.HTMLConverter(output_dir=self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_convert_document_with_note_labels(self):
        """convert_document 输出 XHTML 含 注　　释： 且对应块带 ptoe-note-label，CSS 含新规则"""
        structured = {
            'meta': {
                'title': '测试书',
                'author': '作者',
                'language': 'zh-CN',
            },
            'pages': [
                {'page': 1, 'text': '<p><strong>注释</strong>这是第一页内容</p>'},
                {'page': 2, 'text': '<p>第二页<strong>注释：</strong>内容</p>'},
            ],
        }
        result = self.converter.convert_document(structured, merge_pages=True)

        # 检查生成的内容文件
        self.assertIn('content_files', result)
        self.assertTrue(len(result['content_files']) > 0)

        # 读取第一个内容文件 (content_files 已包含 OEBPS/ 前缀)
        content_file = os.path.join(self.tmpdir, result['content_files'][0])
        self.assertTrue(os.path.exists(content_file), f"Content file not found: {content_file}")
        with open(content_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # 检查替换结果
        self.assertIn('注释\uFF1A', content)
        # 检查 class 注入
        self.assertIn('ptoe-note-label', content)

        # 检查 CSS 文件
        css_file = os.path.join(self.tmpdir, 'OEBPS', 'style.css')
        self.assertTrue(os.path.exists(css_file), f"CSS file not found: {css_file}")
        with open(css_file, 'r', encoding='utf-8') as f:
            css = f.read()
        self.assertIn('p.ptoe-note-label', css)
        self.assertIn('text-indent: 0', css)

    def test_convert_document_with_format_classes(self):
        """测试手动段落格式类 ptoe-flush 和 ptoe-indent 被保留在输出中"""
        structured = {
            'meta': {
                'title': '测试书',
                'author': '作者',
                'language': 'zh-CN',
            },
            'pages': [
                {'page': 1, 'text': '<p class="ptoe-flush">顶格段</p><p class="ptoe-indent">缩进段</p><p>普通段</p>'},
            ],
        }
        result = self.converter.convert_document(structured, merge_pages=True)

        # 检查生成的内容文件
        self.assertIn('content_files', result)
        self.assertTrue(len(result['content_files']) > 0)

        # 读取内容文件
        content_file = os.path.join(self.tmpdir, result['content_files'][0])
        self.assertTrue(os.path.exists(content_file), f"Content file not found: {content_file}")
        with open(content_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # 检查 class 被保留
        self.assertIn('class="ptoe-flush"', content)
        self.assertIn('class="ptoe-indent"', content)
        # 普通段落不应有额外 class
        self.assertIn('<p>普通段</p>', content)

        # 检查 CSS 文件包含格式类规则
        css_file = os.path.join(self.tmpdir, 'OEBPS', 'style.css')
        self.assertTrue(os.path.exists(css_file), f"CSS file not found: {css_file}")
        with open(css_file, 'r', encoding='utf-8') as f:
            css = f.read()
        self.assertIn('.ptoe-flush {', css)
        self.assertIn('text-indent: 0', css)
        self.assertIn('p.ptoe-indent {', css)
        self.assertIn('text-indent: 2em', css)


class TestHeadingCentering(unittest.TestCase):
    """标题居中硬化（2026-08-23）：所有 h1-h6 一律无条件内联 text-align:center，
    不论是否带 ptoe-align-* 类（内联优先级高于类选择器）。
    <p> 段落仍尊重 ptoe-align-*（正文对齐不受影响）。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.converter = htmlmanage.HTMLConverter(output_dir=self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_h1_gets_inline_center(self):
        """h1 无对齐类时应带内联 text-align:center"""
        out = self.converter._render_fragment("<h1>标题</h1><p>正文</p>")
        self.assertIn('<h1 id="h1" style="text-align:center">标题</h1>', out)

    def test_h2_gets_inline_center(self):
        """h2 无对齐类时应带内联 text-align:center"""
        out = self.converter._render_fragment("<h2>章节</h2><p>正文</p>")
        self.assertIn('<h2 id="h1" style="text-align:center">章节</h2>', out)

    def test_h3_h6_get_inline_center(self):
        """h3-h6 应带内联 text-align:center（2026-08-23 修复：之前只处理 h1/h2）"""
        out = self.converter._render_fragment("<h3>三级</h3><h4>四级</h4><h5>五级</h5><h6>六级</h6>")
        self.assertIn('<h3 id="h1" style="text-align:center">三级</h3>', out)
        self.assertIn('<h4 id="h2" style="text-align:center">四级</h4>', out)
        self.assertIn('<h5 id="h3" style="text-align:center">五级</h5>', out)
        self.assertIn('<h6 id="h4" style="text-align:center">六级</h6>', out)

    def test_h1_with_align_left_still_gets_inline_center(self):
        """h1 带 ptoe-align-left 时仍应加内联居中（2026-08-23 修复：之前
        豁免 ptoe-align-left 导致历史数据标题不居中）"""
        out = self.converter._render_fragment('<h1 class="ptoe-align-left">标题</h1>')
        self.assertIn('class="ptoe-align-left"', out)
        self.assertIn('style="text-align:center"', out)
        self.assertEqual(out.count('style="'), 1)

    def test_h1_with_align_right_still_gets_inline_center(self):
        """h1 带 ptoe-align-right 时仍应加内联居中"""
        out = self.converter._render_fragment('<h1 class="ptoe-align-right">标题</h1>')
        self.assertIn('class="ptoe-align-right"', out)
        self.assertIn('style="text-align:center"', out)
        self.assertEqual(out.count('style="'), 1)

    def test_h1_with_align_center_gets_inline_center(self):
        """h1 带 ptoe-align-center 时应加内联居中，无重复 style 属性"""
        out = self.converter._render_fragment('<h1 class="ptoe-align-center">标题</h1>')
        self.assertIn('class="ptoe-align-center"', out)
        self.assertIn('style="text-align:center"', out)
        self.assertEqual(out.count('style="'), 1)

    def test_h2_with_align_center_gets_inline_center(self):
        """h2 带 ptoe-align-center 时应加内联居中，无重复 style 属性"""
        out = self.converter._render_fragment('<h2 class="ptoe-align-center">章节</h2>')
        self.assertIn('class="ptoe-align-center"', out)
        self.assertIn('style="text-align:center"', out)
        self.assertEqual(out.count('style="'), 1)

    def test_h3_with_align_left_still_gets_inline_center(self):
        """h3 带 ptoe-align-left 时仍应加内联居中（抑制对所有级别失效）"""
        out = self.converter._render_fragment('<h3 class="ptoe-align-left">三级</h3>')
        self.assertIn('class="ptoe-align-left"', out)
        self.assertIn('style="text-align:center"', out)
        self.assertEqual(out.count('style="'), 1)

    def test_h1_with_align_left_has_both_class_and_style(self):
        """h1 带 ptoe-align-left → 输出同时含 class 与 style 属性（居中不被抑制）"""
        out = self.converter._render_fragment('<h1 class="ptoe-align-left">标题</h1>')
        self.assertIn('<h1 id="h1" class="ptoe-align-left" style="text-align:center">标题</h1>', out)

    def test_h1_with_indent_style_merges_center(self):
        """h1 带 data-pl（产生 style）时，居中应合并到已有 style 而非新增 style 属性"""
        out = self.converter._render_fragment('<h1 data-pl="2">标题</h1>')
        # 应只有一个 style 属性，且同时含 margin-left 和 text-align:center
        self.assertIn('style="margin-left:2em;text-align:center"', out)
        self.assertEqual(out.count('style="'), 1)

    def test_h2_with_indent_style_merges_center(self):
        """h2 带 data-spb（产生 style）时，居中应合并到已有 style"""
        out = self.converter._render_fragment('<h2 data-spb="1">章节</h2>')
        self.assertIn('style="margin-top:1.5em;text-align:center"', out)
        self.assertEqual(out.count('style="'), 1)

    def test_h3_with_indent_style_merges_center(self):
        """h3 带 data-pl（产生 style）时，居中应合并到已有 style"""
        out = self.converter._render_fragment('<h3 data-pl="1">三级</h3>')
        self.assertIn('style="margin-left:1em;text-align:center"', out)
        self.assertEqual(out.count('style="'), 1)


class TestNewInlineFormatCSS(unittest.TestCase):
    """新增 7 项行内格式 CSS 与渲染测试（2026-09）。"""

    def test_css_contains_seven_new_rules(self):
        """CSSManager.generate_stylesheet 包含全部 7 条 .ptoe-* 规则。"""
        cssm = htmlmanage.CSSManager()
        css = cssm.generate_stylesheet()
        # 检查选择器存在
        for selector in ('.ptoe-underline', '.ptoe-underdot', '.ptoe-strike', '.ptoe-charbox', '.ptoe-shade', '.ptoe-highlight', '.ptoe-sup', '.ptoe-sub'):
            self.assertIn(selector, css, f"Missing selector {selector}")
        # 检查关键属性存在（CSS 输出含空格和分号，做宽松匹配）
        self.assertIn('text-decoration: underline', css)
        self.assertIn('border-bottom: 1px dotted #333', css)
        self.assertIn('text-decoration: line-through', css)
        self.assertIn('border: 1px solid #333', css)
        self.assertIn('padding: 0 .15em', css)
        self.assertIn('border-radius: 2px', css)
        self.assertIn('background: #eef1f4', css)
        self.assertIn('background: #e0e0e0', css)
        self.assertIn('vertical-align: super', css)
        self.assertIn('font-size: .7em', css)
        self.assertIn('line-height: 1', css)
        self.assertIn('vertical-align: sub', css)

    def test_render_fragment_preserves_new_inline_spans(self):
        """_render_fragment 保留段落内的新增行内 span（ptoe-highlight 等）不被剥离。"""
        converter = htmlmanage.HTMLConverter(output_dir="/tmp")
        # 含新增行内格式的段落
        html = '<p>正文<span class="ptoe-highlight">突显文字</span>继续</p>'
        out = converter._render_fragment(html)
        self.assertIn('<span class="ptoe-highlight">突显文字</span>', out)
        # 同时验证其他 6 种 class 也能通过
        for cls in ("ptoe-underline", "ptoe-strike", "ptoe-charbox", "ptoe-shade", "ptoe-sup", "ptoe-sub"):
            html2 = f'<p>正文<span class="{cls}">测试</span>继续</p>'
            out2 = converter._render_fragment(html2)
            self.assertIn(f'<span class="{cls}">测试</span>', out2)

    def test_render_fragment_inline_align_spans_passthrough(self):
        """_render_fragment 对行内对齐 span 透传 class（ptoe-align-*）与
        规则引擎产出的 style="text-align:..."，两者都不进样式转换路径被剥。"""
        converter = htmlmanage.HTMLConverter(output_dir="/tmp")
        out = converter._render_fragment(
            '<p>前<span class="ptoe-align-center">居中</span>后</p>'
        )
        self.assertIn('<span class="ptoe-align-center">居中</span>', out)
        out2 = converter._render_fragment(
            '<p><span style="text-align:right">规则产出</span></p>'
        )
        self.assertIn('<span style="text-align:right">规则产出</span>', out2)
        # 行内注释/引用 span 同样原样透传
        out3 = converter._render_fragment(
            '<p>正文<span class="ptoe-note">注</span><span class="ptoe-citation">引</span></p>'
        )
        self.assertIn('<span class="ptoe-note">注</span>', out3)
        self.assertIn('<span class="ptoe-citation">引</span>', out3)

    def test_align_css_block_display_with_table_cell_guard(self):
        """对齐类 CSS：display:block（行内 span 渲染对齐）+ td/th 覆盖回 table-cell。"""
        cssm = htmlmanage.CSSManager()
        css = cssm.generate_stylesheet()
        for cls in (".ptoe-align-left", ".ptoe-align-center", ".ptoe-align-right"):
            self.assertIn(cls + " {", css)
            self.assertIn("display: block", css)
            self.assertIn("text-align: ", css)
        # 表格单元格保护（display:block 会破坏表格布局）
        self.assertIn("td.ptoe-align-left, td.ptoe-align-center, td.ptoe-align-right", css)
        self.assertIn("display: table-cell", css)


class TestDividerCSS(unittest.TestCase):
    """分隔线 CSS 四种线型（2026-09-27）。"""

    def test_base_and_four_variants_present(self):
        """基类 + solid/dashed/dotted/double 五条规则齐全。"""
        css = htmlmanage.CSSManager().generate_stylesheet()
        for cls in (
            ".ptoe-divider",
            ".ptoe-divider-solid",
            ".ptoe-divider-dashed",
            ".ptoe-divider-dotted",
            ".ptoe-divider-double",
        ):
            self.assertIn(cls + " {", css, cls)

    def test_base_rule_is_weak_and_centered(self):
        """基类：居中 + 弱化色 + 不换行（避免字形被折行截断）。"""
        css = htmlmanage.CSSManager().generate_stylesheet()
        body = css.split(".ptoe-divider {", 1)[1].split("}", 1)[0]
        self.assertIn("text-align: center", body)
        self.assertIn("text-indent: 0", body)
        self.assertIn("color: #999999", body)
        self.assertIn("white-space: nowrap", body)

    def test_variants_differ_from_each_other(self):
        """四种线型各有独立声明（字距/字重区分），不是四条空规则。"""
        css = htmlmanage.CSSManager().generate_stylesheet()
        bodies = {}
        for name in ("solid", "dashed", "dotted", "double"):
            bodies[name] = css.split(f".ptoe-divider-{name} {{", 1)[1].split("}", 1)[0]
        for name, body in bodies.items():
            self.assertTrue(body.strip(), f"{name} 规则为空")
        # double 用粗体字重，线型系列靠字距
        self.assertIn("font-weight: bold", bodies["double"])
        self.assertIn("letter-spacing:", bodies["dashed"])
        self.assertIn("letter-spacing:", bodies["dotted"])

    def test_divider_css_comment_has_no_literal_angle_brackets(self):
        """新增的分隔线 CSS 注释不含字面尖括号（保持注释卫生与历史约定一致）。"""
        css = htmlmanage.CSSManager().generate_stylesheet()
        comments = [chunk.split("*/", 1)[0] for chunk in css.split("/*")[1:]]
        divider_comments = [c for c in comments if "ptoe-divider" in c or "分隔线" in c]
        self.assertTrue(divider_comments, "未找到分隔线相关 CSS 注释")
        for comment in divider_comments:
            self.assertNotIn("<", comment, comment)
            self.assertNotIn(">", comment, comment)

    def test_inject_styles_wraps_css_in_cdata(self):
        """内联 CSS 由 CDATA 包裹 —— 注释里的字面尖括号因此对 XML 解析器无害。"""
        out = htmlmanage.CSSManager().inject_styles(
            "<html><head></head><body>x</body></html>"
        )
        self.assertIn("/* <![CDATA[ */", out)
        self.assertIn("/* ]]> */", out)


class TestDividerAndCharStyleOutput(unittest.TestCase):
    """分隔线与字符样式在 XHTML/EPUB 中的落地（2026-09-27）。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.converter = htmlmanage.HTMLConverter(output_dir=self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _convert(self, page_text):
        structured = {
            'meta': {'title': '测试书', 'author': '作者', 'language': 'zh-CN'},
            'pages': [{'page': 1, 'text': page_text}],
        }
        result = self.converter.convert_document(structured, merge_pages=True)
        path = os.path.join(self.tmpdir, result['content_files'][0])
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    def test_divider_keeps_base_and_suffix_class(self):
        """基类 + 后缀两个 class 都保留（按精确成员过滤会丢掉后缀）。"""
        for style in ('solid', 'dashed', 'dotted', 'double'):
            content = self._convert(
                f'<p class="ptoe-divider ptoe-divider-{style}">────────</p>'
            )
            self.assertIn(f'class="ptoe-divider ptoe-divider-{style}"', content, style)

    def test_unknown_suffix_preserved_but_still_has_base(self):
        """未知后缀类同样透传（前缀判定），CSS 缺失时按基类渲染。"""
        content = self._convert(
            '<p class="ptoe-divider ptoe-divider-wavy">X</p>'
        )
        self.assertIn('class="ptoe-divider ptoe-divider-wavy"', content)

    def test_unrelated_classes_still_stripped(self):
        """分隔线放行不放宽整个白名单：无关 class 仍被剥除。"""
        content = self._convert(
            '<p class="ptoe-divider ptoe-divider-solid evil-class">X</p>'
        )
        self.assertIn('class="ptoe-divider ptoe-divider-solid"', content)
        self.assertNotIn("evil-class", content)

    def test_char_style_span_passthrough(self):
        """span style 原样进入 XHTML（HTML 侧无需再翻译）。"""
        content = self._convert(
            '<p>混排<span style="font-size:24px;color:#c00000">红</span>尾</p>'
        )
        self.assertIn('style="font-size:24px;color:#c00000"', content)
        self.assertIn(">红<", content)

    def test_output_is_well_formed_xml(self):
        """含分隔线与字符样式的 XHTML 必须 XML 良构（CSS 注释尖括号陷阱回归）。"""
        from xml.dom import minidom
        content = self._convert(
            '<h1>章</h1>'
            '<p class="ptoe-divider ptoe-divider-solid">────────</p>'
            '<p>混排<span style="font-size:24px">红</span>尾</p>'
        )
        minidom.parseString(content)

    def test_divider_not_treated_as_heading_or_toc_entry(self):
        """分隔线不进目录、不被当作标题（它是版式元素不是文章标题）。"""
        structured = {
            'meta': {'title': '测试书', 'author': '作者', 'language': 'zh-CN'},
            'pages': [{'page': 1, 'text': (
                '<h1>真标题</h1>'
                '<p class="ptoe-divider ptoe-divider-solid">────────</p>'
            )}],
        }
        result = self.converter.convert_document(structured, merge_pages=True)
        content_path = os.path.join(self.tmpdir, result['content_files'][0])
        with open(content_path, 'r', encoding='utf-8') as f:
            content = f.read()
        # 真标题仍是 h1，分隔线仍是带 class 的 p（未被升级为标题）
        self.assertIn('<h1', content)
        self.assertIn('class="ptoe-divider ptoe-divider-solid"', content)
        # 目录文件里不含分隔线字形
        nav_path = os.path.join(self.tmpdir, result['toc_file'])
        with open(nav_path, 'r', encoding='utf-8') as f:
            nav = f.read()
        self.assertIn("真标题", nav)
        self.assertNotIn("─", nav)

    def test_block_class_html_prefix_acceptance(self):
        """_block_class_html 按前缀放行分隔线类（单元级）。"""
        for style in ('solid', 'dashed', 'dotted', 'double'):
            out = htmlmanage._block_class_html(
                f' class="ptoe-divider ptoe-divider-{style}"'
            )
            self.assertEqual(
                out, f' class="ptoe-divider ptoe-divider-{style}"', style
            )


if __name__ == '__main__':
    unittest.main()