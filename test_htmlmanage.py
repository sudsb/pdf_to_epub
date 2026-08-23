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
        """基本替换：<strong>注释</strong> -> 注　　释："""
        html = '<p><strong>注释</strong>这是内容</p>'
        result = htmlmanage.transform_note_labels(html)
        self.assertIn('注释\uFF1A', result)
        self.assertNotIn('<strong>注释</strong>', result)

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


if __name__ == '__main__':
    unittest.main()