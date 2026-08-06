"""test_correctmanage.py — unittest suite for the manual-correction stage.

Covers:
- correctmanage.sanitize_html: whitelist cleaning of UI-submitted HTML
- htmlmanage.HTMLConverter._render_fragment: markup-aware rendering
  (legacy plain-text path must stay byte-identical)
- mian.pdf_to_epub(correct=True): wiring — correct_pages called between
  structuring and HTML conversion, EPUB still produced
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import htmlmanage
import mian
import pdfmanage
from correctmanage import (
    sanitize_html,
    initial_html,
    apply_markers,
    convert_text_html,
    clean_page_html,
    _browser_gone,
    _history_pages_for_init,
)


class TestSanitize(unittest.TestCase):
    def test_plain_text_escaped(self):
        # 文本内容中的 < & 被转义；未知标签 <x> 整体丢弃（白名单设计）
        self.assertEqual(
            sanitize_html("a &lt; b &amp; c"), "<p>a &lt; b &amp; c</p>"
        )
        self.assertEqual(sanitize_html("你好 & <x>"), "<p>你好 &amp; </p>")

    def test_bold_italic_headings(self):
        out = sanitize_html("<div><b>粗</b> <i>斜</i></div><h2>章标题</h2><p>正文</p>")
        self.assertEqual(
            out, "<p><strong>粗</strong> <em>斜</em></p><h2>章标题</h2><p>正文</p>"
        )

    def test_div_normalized_to_p_and_br(self):
        self.assertEqual(sanitize_html("<div>第一行<br>第二行</div>"), "<p>第一行<br/>第二行</p>")

    def test_attributes_stripped_unknown_tags_dropped(self):
        out = sanitize_html(
            '<p style="color:red" onclick="x()">文本</p>'
            '<img src="/preview/1"><script>alert(1)</script><span>span内容</span>'
        )
        self.assertNotIn("style", out)
        self.assertNotIn("<script", out)
        self.assertNotIn("<span", out)
        self.assertIn("文本", out)
        self.assertIn("span内容", out)
        # img 白名单（src + alt + ptoe-img-* class）被保留；无 src 的 img 才被丢弃
        self.assertIn('<img src="/preview/1" alt="插图"/>', out)

    def test_img_preserved_with_whitelist(self):
        # 插入的图片：src/alt/显示模式 class 保留，其余属性与 class 剥除
        out = sanitize_html(
            '<p><img src="data:image/png;base64,AAA" alt="图" class="ptoe-img-full"></p>'
        )
        self.assertEqual(
            out, '<p><img src="data:image/png;base64,AAA" alt="图" class="ptoe-img-full"/></p>'
        )
        out2 = sanitize_html('<p><img src="x.png" class="ptoe-img-fit evil" onerror="alert(1)"></p>')
        self.assertEqual(out2, '<p><img src="x.png" alt="插图" class="ptoe-img-fit"/></p>')

    def test_img_dropped_without_src(self):
        # src 缺失/为空时整张图丢弃（不留空 img）
        self.assertNotIn("<img", sanitize_html('<p><img alt="图"></p>'))
        self.assertNotIn("<img", sanitize_html('<p><img src=""></p>'))

    def test_unclosed_inline_closed_at_block_boundary(self):
        out = sanitize_html("<p><b>未闭合</p><p>下一段</p>")
        self.assertIn("<p><strong>未闭合</strong></p>", out)
        self.assertIn("<p>下一段</p>", out)

    def test_empty_and_garbage(self):
        self.assertEqual(sanitize_html(""), "")
        self.assertEqual(sanitize_html("   "), "")
        self.assertEqual(sanitize_html("<p></p>"), "")
        # 无法解析时退化为纯文本，不抛异常
        out = sanitize_html("<p>残")
        self.assertTrue(out)

    def test_script_style_content_dropped(self):
        out = sanitize_html("<p>第3页 <script>alert(1)</script><style>.x{}</style></p>")
        self.assertEqual(out, "<p>第3页 </p>")

    def test_marker_span_preserved(self):
        out = sanitize_html('<p>正文<span data-ptoe-marker="full">全文</span></p>')
        self.assertEqual(
            out, '<p>正文<span data-ptoe-marker="full">全文</span></p>'
        )
        out2 = sanitize_html('<p>a<span data-ptoe-marker="chapter:2">第二章节</span></p>')
        self.assertIn('data-ptoe-marker="chapter:2"', out2)

    def test_marker_span_extra_attrs_and_fake_values_stripped(self):
        out = sanitize_html('<p>x<span class="x" data-ptoe-marker="evil" onclick="f()">y</span></p>')
        self.assertEqual(out, "<p>xy</p>")

    def test_note_class_preserved(self):
        out = sanitize_html('<div class="ptoe-note">注释</div><p class="ptoe-note">注2</p>')
        self.assertEqual(
            out, '<p class="ptoe-note">注释</p><p class="ptoe-note">注2</p>'
        )

    def test_note_marker_preserved(self):
        out = sanitize_html('<p>a<span data-ptoe-marker="note">注释</span>b</p>')
        self.assertEqual(out, '<p>a<span data-ptoe-marker="note">注释</span>b</p>')

    def test_page_marker_preserved(self):
        out = sanitize_html('<p>a<span data-ptoe-marker="page">换页</span>b</p>')
        self.assertEqual(out, '<p>a<span data-ptoe-marker="page">换页</span>b</p>')
        # 非白名单标记值仍被剥离
        out2 = sanitize_html('<p>x<span data-ptoe-marker="evil">y</span></p>')
        self.assertEqual(out2, "<p>xy</p>")

    def test_align_classes_preserved(self):
        # 块级对齐类（居中/居左/居右）随 ptoe-note 一起保留
        out = sanitize_html('<p class="ptoe-align-center">x</p><div class="ptoe-align-right">y</div>')
        self.assertEqual(
            out, '<p class="ptoe-align-center">x</p><p class="ptoe-align-right">y</p>'
        )
        # 注释 + 对齐可并存
        out2 = sanitize_html('<p class="ptoe-note ptoe-align-center">注</p>')
        self.assertEqual(out2, '<p class="ptoe-note ptoe-align-center">注</p>')
        # 非白名单 class 仍被剥离
        out3 = sanitize_html('<p class="evil ptoe-align-left">x</p>')
        self.assertEqual(out3, '<p class="ptoe-align-left">x</p>')


class TestCleanPageHtml(unittest.TestCase):
    """clean_page_html：段落合并 / 段首符号 / 中英文标点 / 残留 HTML 标签清理。"""

    def test_merge_adjacent_paragraphs(self):
        # 前块不以句末标点结尾 → 与下一块合并；CJK 相接不需要补空格
        self.assertEqual(clean_page_html('<p>第一段</p><p>续文</p>'), '<p>第一段续文</p>')

    def test_no_merge_after_sentence_end(self):
        self.assertEqual(
            clean_page_html('<p>完了。</p><p>新段</p>'), '<p>完了。</p>\n<p>新段</p>'
        )

    def test_leading_symbols_stripped(self):
        self.assertEqual(clean_page_html('<p># 标题</p>'), '<p>标题</p>')
        self.assertEqual(
            clean_page_html('<p>完了。</p><p>* 乙</p>'), '<p>完了。</p>\n<p>乙</p>'
        )

    def test_cjk_punctuation_normalized(self):
        # 与汉字相邻的半角标点 → 全角
        self.assertEqual(clean_page_html('<p>你好,世界</p>'), '<p>你好，世界</p>')
        # 字母/数字之间的全角标点 → 半角（英文语境）
        self.assertEqual(clean_page_html('<p>ABC，DEF</p>'), '<p>ABC,DEF</p>')
        # 纯英文语境保留半角
        self.assertEqual(clean_page_html('<p>英文 test,OK</p>'), '<p>英文 test,OK</p>')

    def test_disallowed_tags_stripped(self):
        # OCR 残留的 <div> 等非白名单标签被剥掉（内容保留）
        self.assertEqual(clean_page_html('<p>a<div>b</div></p>'), '<p>ab</p>')

    def test_headings_not_merged(self):
        self.assertEqual(
            clean_page_html('<h1>标题</h1><p>正文</p>'), '<h1>标题</h1>\n<p>正文</p>'
        )

    def test_marker_and_inline_preserved(self):
        out = clean_page_html('<p>正文<span data-ptoe-marker="note">注释</span></p>')
        self.assertEqual(out, '<p>正文<span data-ptoe-marker="note">注释</span></p>')
        self.assertEqual(clean_page_html('<p><b>粗</b></p>'), '<p><strong>粗</strong></p>')

    def test_img_blocks_not_merged(self):
        # 带图片模式 class 的 <p> 有结构意图，保持独立
        out = clean_page_html(
            '<p class="ptoe-img-full"><img src="data:image/png;base64,AAA" alt="插图"/></p>'
        )
        self.assertEqual(
            out, '<p class="ptoe-img-full"><img src="data:image/png;base64,AAA" alt="插图"/></p>'
        )

    def test_idempotent(self):
        src = '<p># 标题。</p><p>后续,内容</p><p>完了。</p><p>* 新段</p>'
        once = clean_page_html(src)
        self.assertEqual(clean_page_html(once), once)

    def test_md_bold_symbols_removed(self):
        # OCR/文本残留的 Markdown 加粗符号 ** 全部清除（含句中）
        self.assertEqual(clean_page_html('<p>这是**重点**内容</p>'), '<p>这是重点内容</p>')
        self.assertEqual(clean_page_html('<p>a***b</p>'), '<p>ab</p>')
        # 与段首符号清理叠加
        self.assertEqual(clean_page_html('<p>**重点**</p>'), '<p>重点</p>')
        # 幂等
        once = clean_page_html('<p>这是**重点**内容</p>')
        self.assertEqual(clean_page_html(once), once)

    def test_no_merge_after_closing_punct(self):
        # 以闭合括号/书名号结尾视为完整段落，不与下一段合并
        self.assertEqual(
            clean_page_html('<p>见附录（一）</p><p>续文</p>'), '<p>见附录（一）</p>\n<p>续文</p>'
        )
        self.assertEqual(
            clean_page_html('<p>见《文集》</p><p>续文</p>'), '<p>见《文集》</p>\n<p>续文</p>'
        )
        self.assertEqual(
            clean_page_html('<p>见附录第3）</p><p>续文</p>'), '<p>见附录第3）</p>\n<p>续文</p>'
        )
        # 对照组：普通结尾仍合并
        self.assertEqual(clean_page_html('<p>见附录</p><p>续文</p>'), '<p>见附录续文</p>')

    def test_formatted_blocks_not_merged(self):
        # 已设行内格式/标记的段落保留原结构，不与附近段落合并
        self.assertEqual(
            clean_page_html('<p>正文</p><p><strong>加粗</strong></p><p>续文</p>'),
            '<p>正文</p>\n<p><strong>加粗</strong></p>\n<p>续文</p>',
        )
        self.assertEqual(
            clean_page_html('<p>正文</p><p><strong>粗</strong></p>'),
            '<p>正文</p>\n<p><strong>粗</strong></p>',
        )
        self.assertEqual(
            clean_page_html('<p>正文</p><p><span data-ptoe-marker="note">注</span></p>'),
            '<p>正文</p>\n<p><span data-ptoe-marker="note">注</span></p>',
        )


class TestInitialHtml(unittest.TestCase):
    def test_lines_become_divs(self):
        self.assertEqual(initial_html("a\nb\n\nc"), "<div>a</div><div>b</div><div>c</div>")

    def test_escaped_and_empty(self):
        self.assertEqual(initial_html("a<b & c"), "<div>a&lt;b &amp; c</div>")
        self.assertEqual(initial_html("   \n \n"), "")


class TestApplyMarkers(unittest.TestCase):
    def test_join_merges_across_pages(self):
        pages = [
            {"page": 1, "text": '<p>甲</p><p>乙<span data-ptoe-marker="join">段落</span></p>'},
            {"page": 2, "text": "<p>丙</p><p>丁</p>"},
        ]
        self.assertEqual(apply_markers(pages), [{"text": "<p>甲</p><p>乙丙</p><p>丁</p>"}])

    def test_chapter_marker_inserts_h2(self):
        pages = [
            {"page": 1, "text": '<p>前</p><p>后<span data-ptoe-marker="chapter:2">第二章节</span></p><p>续</p>'}
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": "<p>前</p><p>后</p><h2>第二章节</h2><p>续</p>"}],
        )

    def test_full_marker_splits_articles(self):
        pages = [
            {"page": 1, "text": '<p>文章一</p><p>尾<span data-ptoe-marker="full">全文</span></p><p>文章二开始</p>'}
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": "<p>文章一</p><p>尾</p>"}, {"text": "<p>文章二开始</p>"}],
        )

    def test_marker_only_block(self):
        pages = [{"page": 1, "text": '<p>甲</p><p><span data-ptoe-marker="full">全文</span></p><p>乙</p>'}]
        self.assertEqual(apply_markers(pages), [{"text": "<p>甲</p>"}, {"text": "<p>乙</p>"}])

    def test_join_at_paragraph_start_merges_with_previous(self):
        # 段首段落标记：本段与上一段合并为一整段
        pages = [
            {"page": 1, "text": '<p>甲</p><p><span data-ptoe-marker="join">段落</span>乙</p><p>丙</p>'}
        ]
        self.assertEqual(apply_markers(pages), [{"text": "<p>甲乙</p><p>丙</p>"}])

    def test_join_mid_block_bridges_both_sides(self):
        # 段中段落标记：标记前后属于同一整段
        pages = [{"page": 1, "text": '<p>甲<span data-ptoe-marker="join">段落</span>乙</p><p>丙</p>'}]
        self.assertEqual(apply_markers(pages), [{"text": "<p>甲乙</p><p>丙</p>"}])

    def test_join_start_and_end_chain_three_blocks(self):
        # 段首 + 段尾同用：三块拼回一整段
        pages = [
            {"page": 1, "text": '<p>甲</p><p><span data-ptoe-marker="join">段落</span>乙<span data-ptoe-marker="join">段落</span></p><p>丙</p>'}
        ]
        self.assertEqual(apply_markers(pages), [{"text": "<p>甲乙丙</p>"}])

    def test_join_only_block_bridges_prev_and_next(self):
        # 只有段落标记的空块：把前后两段拼成一段
        pages = [
            {"page": 1, "text": '<p>甲</p><p><span data-ptoe-marker="join">段落</span></p><p>乙</p>'}
        ]
        self.assertEqual(apply_markers(pages), [{"text": "<p>甲乙</p>"}])

    def test_join_at_start_of_first_block_no_crash(self):
        # 段首标记但前面没有段落（全书第一块）→ 按普通段落渲染
        pages = [{"page": 1, "text": '<p><span data-ptoe-marker="join">段落</span>甲</p><p>乙</p>'}]
        self.assertEqual(apply_markers(pages), [{"text": "<p>甲</p><p>乙</p>"}])

    def test_no_markers_single_article(self):
        pages = [{"page": 1, "text": "<p>一</p><p>二</p>"}, {"page": 2, "text": "<p>三</p>"}]
        self.assertEqual(apply_markers(pages), [{"text": "<p>一</p><p>二</p><p>三</p>"}])

    def test_align_class_preserved_through_markers(self):
        # 对齐 class 在标记处理中保留；段落合并时保留首段（被合并段）的 class
        pages = [
            {"page": 1, "text": '<p class="ptoe-align-center">甲</p>'},
            {"page": 2, "text": '<p class="ptoe-align-right">乙<span data-ptoe-marker="join">段落</span></p><p>丙</p>'},
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p class="ptoe-align-center">甲</p><p class="ptoe-align-right">乙丙</p>'}],
        )

    def test_note_and_align_classes_coexist(self):
        # 注释段落（ptoe-note）经注释标记替换为行内注释 span；
        # 对齐 class 在普通段落上保留（注释段落本身被消费）
        pages = [
            {"page": 1, "text": '<p class="ptoe-align-center">正文<span data-ptoe-marker="note">注</span></p><p class="ptoe-note">注一</p>'}
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p class="ptoe-align-center">正文<span class="ptoe-note">（注一）</span></p>'}],
        )

    def test_note_marker_replaced_by_note_paragraph(self):
        # 注释标记位置由对应注释段落替换，并用中文括号括起
        pages = [
            {"page": 1, "text": '<p>甲<span data-ptoe-marker="note">注释</span>乙</p><p class="ptoe-note">注一</p>'}
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p>甲<span class="ptoe-note">（注一）</span>乙</p>'}],
        )

    def test_note_with_join_merges_into_one_annotation(self):
        # 注释支持段落标记：带段落标记的注释与上一条注释属于同一段（合并）
        pages = [
            {"page": 1, "text": '<p>甲<span data-ptoe-marker="note">注释</span></p><p class="ptoe-note">注一</p><p class="ptoe-note"><span data-ptoe-marker="join">段落</span>注二</p>'}
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p>甲<span class="ptoe-note">（注一注二）</span></p>'}],
        )

    def test_note_parens_unified_and_no_double_wrap(self):
        # 注释含 ASCII 括号 → 统一为中文括号；注释已带括号（已在正文中）→ 不再重复加括号
        pages = [
            {"page": 1, "text": '<p>甲<span data-ptoe-marker="note">注释</span></p><p class="ptoe-note">（见(毛选)第一卷）</p>'}
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p>甲<span class="ptoe-note">（见（毛选）第一卷）</span></p>'}],
        )

    def test_page_marker_mid_block_breaks_page(self):
        # 段中换页标记：标记之后的内容（乙）前插入换页元素，文章不拆分
        pages = [
            {"page": 1, "text": '<p>甲<span data-ptoe-marker="page">换页</span>乙</p><p>丙</p>'}
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p>甲</p><p class="ptoe-page-break"> </p><p>乙</p><p>丙</p>'}],
        )

    def test_page_marker_at_paragraph_start(self):
        pages = [{"page": 1, "text": '<p>甲</p><p><span data-ptoe-marker="page">换页</span>乙</p>'}]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p>甲</p><p class="ptoe-page-break"> </p><p>乙</p>'}],
        )

    def test_page_marker_trailing_applies_to_next_block(self):
        # 段尾换页标记：作用于下一个内容块（乙）
        pages = [{"page": 1, "text": '<p>甲<span data-ptoe-marker="page">换页</span></p><p>乙</p>'}]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p>甲</p><p class="ptoe-page-break"> </p><p>乙</p>'}],
        )

    def test_page_marker_after_full_marker_keeps_articles(self):
        # 全文（拆文章）+ 换页（文章内分页）互不干扰
        pages = [
            {"page": 1, "text": '<p>文章一<span data-ptoe-marker="full">全文</span></p><p><span data-ptoe-marker="page">换页</span>续</p>'}
        ]
        self.assertEqual(
            apply_markers(pages),
            [
                {"text": "<p>文章一</p>"},
                {"text": '<p class="ptoe-page-break"> </p><p>续</p>'},
            ],
        )

    def test_note_marker_count_mismatch_raises(self):
        # 数量不匹配（标记 1 个 vs 注释 2 段）→ 抛 ValueError 提示
        pages = [
            {"page": 1, "text": '<p>甲<span data-ptoe-marker="note">注释</span></p><p class="ptoe-note">注一</p><p class="ptoe-note">注二</p>'}
        ]
        with self.assertRaises(ValueError) as cm:
            apply_markers(pages)
        self.assertIn("数量不匹配", str(cm.exception))

    def test_note_marker_at_trailing_position(self):
        # 段尾注释标记：注释追加到该段末尾
        pages = [
            {"page": 1, "text": '<p>甲<span data-ptoe-marker="note">注释</span></p><p class="ptoe-note">注一</p>'}
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p>甲<span class="ptoe-note">（注一）</span></p>'}],
        )


class TestRenderFragment(unittest.TestCase):
    def setUp(self):
        self.conv = htmlmanage.HTMLConverter(tempfile.mkdtemp(prefix="test_correct_"))

    def tearDown(self):
        shutil.rmtree(self.conv.output_dir, ignore_errors=True)

    def test_plain_text_legacy_split(self):
        self.assertEqual(self.conv._render_fragment("a\n\nb"), "<p>a</p>\n<p>b</p>")

    def test_plain_text_escaped(self):
        self.assertEqual(
            self.conv._render_fragment("a < b & c"), "<p>a &lt; b &amp; c</p>"
        )

    def test_markup_rendered(self):
        text = "<p>正文<strong>粗</strong>与<em>斜</em></p><h2>章</h2>"
        out = self.conv._render_fragment(text)
        self.assertIn("<p>正文<strong>粗</strong>与<em>斜</em></p>", out)
        self.assertIn('<h2 id="h1">章</h2>', out)
        # 同一片段内多个标题锚点递增
        out2 = self.conv._render_fragment("<h2>甲</h2><p>x</p><h3>乙</h3>")
        self.assertIn('<h2 id="h1">甲</h2>', out2)
        self.assertIn('<h3 id="h2">乙</h3>', out2)

    def test_markup_no_double_escape(self):
        out = self.conv._render_fragment("<p>a &amp; b</p>")
        self.assertIn("&amp;", out)
        self.assertNotIn("&amp;amp;", out)

    def test_marker_span_stripped_in_render(self):
        out = self.conv._render_fragment('<p>a<span data-ptoe-marker="full">全文</span></p>')
        self.assertNotIn("<span", out)
        self.assertNotIn("data-ptoe-marker", out)
        self.assertIn("a全文", out)

    def test_align_classes_preserved_in_render(self):
        out = self.conv._render_fragment('<p class="ptoe-align-center">x</p><h2>章</h2>')
        self.assertIn('<p class="ptoe-align-center">x</p>', out)
        self.assertIn('<h2 id="h1">章</h2>', out)
        # 注释 + 对齐并存
        out2 = self.conv._render_fragment('<p class="ptoe-note ptoe-align-right">注</p>')
        self.assertIn('<p class="ptoe-note ptoe-align-right">注</p>', out2)

    def test_align_css_in_stylesheet(self):
        css = self.conv.cssm.generate_stylesheet()
        self.assertIn("ptoe-align-center", css)
        self.assertIn("ptoe-align-left", css)
        self.assertIn("ptoe-align-right", css)

    def test_page_break_class_preserved_in_render(self):
        out = self.conv._render_fragment('<p>甲</p><p class="ptoe-page-break"> </p><p>乙</p>')
        self.assertIn('<p class="ptoe-page-break"> </p>', out)
        self.assertIn("<p>甲</p>", out)
        self.assertIn("<p>乙</p>", out)
        css = self.conv.cssm.generate_stylesheet()
        self.assertIn("ptoe-page-break", css)
        self.assertIn("page-break-before", css)

    def test_markup_through_convert_document(self):
        # 端到端：标记文本经 convert_document 进入最终 XHTML
        import tempfile as _tf

        outdir = _tf.mkdtemp(prefix="test_correct_doc_")
        try:
            doc = {
                "pages": [
                    {"page": 1, "text": "<h2>标题</h2><p>正文<strong>重点</strong></p>"}
                ],
                "body": "",
                "paragraphs": [],
                "meta": {"title": "T", "package_epub": False},
            }
            res = htmlmanage.HTMLConverter(outdir).convert_document(doc, merge_pages=True)
            content = Path(outdir) / res["content_files"][0]
            xhtml = content.read_text(encoding="utf-8")
            self.assertIn('<h2 id="h1">标题</h2>', xhtml)
            self.assertIn("<p>正文<strong>重点</strong></p>", xhtml)
        finally:
            shutil.rmtree(outdir, ignore_errors=True)

    def test_no_duplicate_title_across_articles(self):
        # 书名=第一章大标题（单章 PDF / --title 用章节名）：无标题的文章
        # 不再重复补 h1，避免同一大标题在每页重复出现
        import tempfile as _tf

        outdir = _tf.mkdtemp(prefix="test_dup_title_")
        try:
            doc = {
                "pages": [],
                "body": "",
                "paragraphs": [],
                "articles": [
                    {"text": "<h1>第一章 引言</h1><p>第一段。</p>"},
                    {"text": "<p>第二章内容开始。</p>"},
                    {"text": "<p>后续内容。</p>"},
                ],
                "meta": {"title": "第一章 引言", "package_epub": False},
            }
            res = htmlmanage.HTMLConverter(outdir).convert_document(doc, merge_pages=True)
            self.assertEqual(len(res["content_files"]), 3)
            texts = [
                (Path(outdir) / f).read_text(encoding="utf-8") for f in res["content_files"]
            ]
            # 标题元素只应出现在第一个内容页（<head><title> 不算正文标题）
            import re as _re

            def _headings(t):
                return _re.findall(r"<h[1-6][^>]*>(.*?)</h[1-6]>", t)

            self.assertEqual(_headings(texts[0]), ["第一章 引言"])
            self.assertEqual(_headings(texts[1]), [])
            self.assertEqual(_headings(texts[2]), [])
            # TOC 只列一次
            toc_html = (Path(outdir) / res["toc_file"]).read_text(encoding="utf-8")
            self.assertEqual(toc_html.count("第一章 引言"), 1)
        finally:
            shutil.rmtree(outdir, ignore_errors=True)

    def test_convert_document_with_articles(self):
        # 全文标记拆出的文章结构 → 每篇一个内容页（开新页）
        outdir = tempfile.mkdtemp(prefix="test_articles_")
        try:
            doc = {
                "pages": [],
                "body": "",
                "paragraphs": [],
                "articles": [
                    {"text": "<p>文章一</p><p>尾</p>"},
                    {"text": "<h2>第二章节</h2><p>文章二开始</p>"},
                ],
                "meta": {"title": "T", "package_epub": False},
            }
            res = htmlmanage.HTMLConverter(outdir).convert_document(doc, merge_pages=True)
            self.assertEqual(len(res["content_files"]), 2)
            t1 = (Path(outdir) / res["content_files"][0]).read_text(encoding="utf-8")
            t2 = (Path(outdir) / res["content_files"][1]).read_text(encoding="utf-8")
            self.assertIn("<p>文章一</p>\n<p>尾</p>", t1)
            self.assertIn('<h2 id="h1">第二章节</h2>\n<p>文章二开始</p>', t2)
        finally:
            shutil.rmtree(outdir, ignore_errors=True)


def _make_pdf(path: Path, n: int = 3) -> None:
    import fitz

    doc = fitz.open()
    for i in range(1, n + 1):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i} content line")
    doc.save(path)
    doc.close()


class TestPdfToEpubWithCorrection(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="test_correct_mian_"))
        self._pdf = self._tmp / "sample.pdf"
        _make_pdf(self._pdf, n=3)

    def tearDown(self):
        data_dir = Path(pdfmanage.__file__).resolve().parent / "data"
        for d in data_dir.glob("sample*"):
            shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_correct_flag_wired_into_pipeline(self):
        def fake_batch_infer(images, prompts, model_key="HY", max_workers=3, thinking=False, timeout=600, on_progress=None):
            # 乱序返回，验证流水线按页码排序
            out = []
            for i, p in reversed(list(enumerate(images, start=1))):
                out.append({"img": str(p), "result": f"第{i}页内容", "error": None})
            return out

        calls = []

        def fake_correct_pages(pages, **kw):
            calls.append(pages)
            return pages

        import correctmanage as _cm
        import llamamanage as _ll

        orig = (_ll.batch_infer, mian._ensure_server, _cm.correct_pages)
        _ll.batch_infer = fake_batch_infer
        mian._ensure_server = lambda model_key: None
        _cm.correct_pages = fake_correct_pages
        try:
            epub_out = self._tmp / "out.epub"
            result = mian.pdf_to_epub(self._pdf, epub_path=epub_out, correct=True)
        finally:
            _ll.batch_infer, mian._ensure_server, _cm.correct_pages = orig

        self.assertTrue(epub_out.is_file(), f"epub not created: {result}")
        self.assertEqual(len(calls), 1, "correct_pages 应被调用一次")
        self.assertEqual([p["page"] for p in calls[0]], [1, 2, 3])


class TestBrowserGoneMonitor(unittest.TestCase):
    """浏览器关闭监测：_browser_gone 判定 + 心跳/信标 HTTP 端点。"""

    def _state(self, idle_timeout=600, last_heartbeat=1000.0, gone_at=None):
        return {
            "last_heartbeat": last_heartbeat,
            "gone_at": gone_at,
            "idle_timeout": float(idle_timeout),
        }

    def test_heartbeat_fresh_never_gone(self):
        # 心跳新鲜（远小于超时）→ 无论过了多久都不判定
        st = self._state(idle_timeout=600, last_heartbeat=1000.0)
        self.assertEqual(_browser_gone(st, now=1300.0), (False, None))

    def test_beacon_expiry_triggers(self):
        # pagehide 信标确认关闭，倒计时满 → 判定
        st = self._state(idle_timeout=600, gone_at=2000.0)
        self.assertEqual(_browser_gone(st, now=2600.0), (True, None))
        self.assertEqual(_browser_gone(st, now=2599.0), (False, None))

    def test_heartbeat_stale_requires_confirm_window(self):
        # 心跳失联（无信标）：需连续失联 _STALE_CONFIRM_SECONDS 才判定，
        # 防电脑休眠唤醒后短暂失联误判
        st = self._state(idle_timeout=600, last_heartbeat=1000.0)
        t1 = 1601.0  # 失联刚满 601 秒（>= 600）
        gone, stale = _browser_gone(st, now=t1)
        self.assertFalse(gone, "首次失联只记时刻，不判定")
        self.assertEqual(stale, t1)
        gone, _ = _browser_gone(st, now=t1 + 2.9, stale_since=stale)
        self.assertFalse(gone, "确认窗口未满")
        gone, _ = _browser_gone(st, now=t1 + 3.1, stale_since=stale)
        self.assertTrue(gone, "连续失联超过确认窗口 → 判定")

    def test_heartbeat_recovers_resets_confirm(self):
        # 失联中恢复心跳 → 不再判定
        st = self._state(idle_timeout=600, last_heartbeat=1000.0)
        gone, stale = _browser_gone(st, now=1601.0)
        self.assertFalse(gone)
        st["last_heartbeat"] = 1602.0
        self.assertEqual(_browser_gone(st, now=1602.5, stale_since=stale), (False, None))

    def test_http_endpoints_track_state(self):
        # 心跳/信标端点：/api/heartbeat 刷新并取消倒计时，/api/gone 开始倒计时
        from http.server import ThreadingHTTPServer
        from threading import Thread

        import requests
        from correctmanage import _CorrectionHandler

        state = {
            "pages": {1: "<p>x</p>"},
            "finished": __import__("threading").Event(),
            "preview_cache": {},
            "pdf_path": None,
            "img_dir": None,
            "preview_dpi": 110,
            "preview_quality": 82,
            "last_heartbeat": 0.0,
            "gone_at": None,
            "idle_timeout": 600.0,
            "auto_finished": False,
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CorrectionHandler)
        server.daemon_threads = True
        server.state = state
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            requests.post(base + "/api/gone").raise_for_status()
            self.assertIsNotNone(state["gone_at"], "信标应记录关闭时刻")
            gone_first = state["gone_at"]

            requests.post(base + "/api/heartbeat").raise_for_status()
            self.assertIsNone(state["gone_at"], "心跳应取消关闭倒计时")
            self.assertGreater(state["last_heartbeat"], 0.0)

            requests.get(base + "/api/heartbeat").raise_for_status()
            self.assertGreaterEqual(state["last_heartbeat"], gone_first)
        finally:
            server.shutdown()
            server.server_close()

    def test_finish_repeatable_and_stage_writes_history(self):
        # 「完成并转换」可重复点击：服务不关闭，on_convert 每次调用并返回结果；
        # 「暂存」把当前内容写入本地历史缓存文件（支持再次矫正时恢复）
        import json as _json
        from http.server import ThreadingHTTPServer
        from threading import Thread

        import requests
        from correctmanage import _CorrectionHandler

        calls = []

        def fake_convert(pages, **kw):
            calls.append([p["page"] for p in pages])
            return {"ok": True, "message": "转换完成", "epub": "out.epub"}

        hist_dir = Path(tempfile.mkdtemp(prefix="test_hist_"))
        state = {
            "pages": {1: "<p>x</p>", 2: "<p>y</p>"},
            "finished": __import__("threading").Event(),
            "preview_cache": {},
            "pdf_path": "C:/fake/book.pdf",
            "img_dir": None,
            "preview_dpi": 110,
            "preview_quality": 82,
            "last_heartbeat": 0.0,
            "gone_at": None,
            "idle_timeout": 600.0,
            "auto_finished": False,
            "on_convert": fake_convert,
            "convert_lock": __import__("threading").Lock(),
            "history_prefix": "testprefix",
            "history_lock": __import__("threading").Lock(),
        }
        # 指向临时目录：_write_history_version 用 state["history_prefix"] 生成
        # <prefix>_<时间戳>_<随机>.json；这里把目录临时替换以隔离测试
        import correctmanage as _cm
        _orig_dir = _cm._history_dir
        _cm._history_dir = lambda: hist_dir
        self.addCleanup(lambda: setattr(_cm, "_history_dir", _orig_dir))
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CorrectionHandler)
        server.daemon_threads = True
        server.state = state
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        body = _json.dumps({"pages": [{"page": 1, "html": "<p>改1</p>"}]})
        try:
            r1 = requests.post(base + "/api/finish", data=body).json()
            self.assertTrue(r1["ok"])
            self.assertTrue(r1["converted"]["ok"])
            r2 = requests.post(base + "/api/finish", data=body).json()
            self.assertTrue(r2["ok"])
            self.assertTrue(r2["converted"]["ok"])
            self.assertEqual(len(calls), 2, "每次点击完成并转换都应触发一次转换")
            self.assertFalse(state["finished"].is_set(), "完成并转换不应关闭服务")
            self.assertEqual(state["pages"][1], "<p>改1</p>")
            requests.post(base + "/api/stage", data=body).raise_for_status()
            files = sorted(hist_dir.glob("testprefix_*.json"))
            self.assertTrue(files, "暂存应写入本地历史缓存（版本文件）")
            cached = _json.loads(files[0].read_text(encoding="utf-8"))
            self.assertIn("1", cached["pages"])
            self.assertIn("<p>改1</p>", cached["pages"]["1"])
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(hist_dir, ignore_errors=True)

    def test_save_overwrites_current_version_stage_still_new(self):
        # 「保存」不新建历史版本：同一份内容反复保存只覆盖同一个文件；
        # 「暂存」仍保持原逻辑，每次生成一个新版本
        import json as _json
        from http.server import ThreadingHTTPServer
        from threading import Thread

        import requests
        import correctmanage as _cm
        from correctmanage import _CorrectionHandler

        hist_dir = Path(tempfile.mkdtemp(prefix="test_save_"))
        state = {
            "pages": {1: "<p>x</p>"},
            "finished": __import__("threading").Event(),
            "preview_cache": {},
            "pdf_path": "C:/fake/book.pdf",
            "img_dir": None,
            "preview_dpi": 110,
            "preview_quality": 82,
            "last_heartbeat": 0.0,
            "gone_at": None,
            "idle_timeout": 600.0,
            "auto_finished": False,
            "on_convert": None,
            "convert_lock": __import__("threading").Lock(),
            "history_prefix": "testprefix",
            "history_lock": __import__("threading").Lock(),
        }
        _orig_dir = _cm._history_dir
        _cm._history_dir = lambda: hist_dir
        self.addCleanup(lambda: setattr(_cm, "_history_dir", _orig_dir))
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CorrectionHandler)
        server.daemon_threads = True
        server.state = state
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            body1 = _json.dumps({"pages": [{"page": 1, "html": "<p>第一次保存</p>"}]})
            body2 = _json.dumps({"pages": [{"page": 1, "html": "<p>第二次保存</p>"}]})
            r1 = requests.post(base + "/api/save", data=body1).json()
            self.assertTrue(r1["ok"])
            r2 = requests.post(base + "/api/save", data=body2).json()
            self.assertTrue(r2["ok"])
            files = sorted(hist_dir.glob("testprefix_*.json"))
            self.assertEqual(len(files), 1, "保存不新建版本：两次保存应只有一个缓存文件")
            cached = _json.loads(files[0].read_text(encoding="utf-8"))
            self.assertIn("<p>第二次保存</p>", cached["pages"]["1"],
                          "保存应覆盖当前缓存内容（最新一次保存生效）")
            self.assertNotIn("<p>第一次保存</p>", cached["pages"]["1"])
            # 暂存保持原逻辑：每次生成一个新版本
            requests.post(base + "/api/stage", data=body1).raise_for_status()
            files_after = sorted(hist_dir.glob("testprefix_*.json"))
            self.assertEqual(len(files_after), 2, "暂存仍应新建历史版本")
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(hist_dir, ignore_errors=True)

    def test_history_list_versioning_and_delete(self):
        # 历史记录：同一文件多版本（v1 最新）、文件名/路径分列；选中删除与全部删除
        import json as _json
        from http.server import ThreadingHTTPServer
        from threading import Thread

        import requests
        import correctmanage as _cm
        from correctmanage import _CorrectionHandler

        hist_dir = Path(tempfile.mkdtemp(prefix="test_histlist_"))
        _orig_dir = _cm._history_dir
        _cm._history_dir = lambda: hist_dir
        self.addCleanup(lambda: setattr(_cm, "_history_dir", _orig_dir))
        # 预置：A.pdf 两个版本（同名不同路径由不同前缀区分）、B.pdf 一个版本
        seed = [
            ("aaa111_20260101000000_0001", "C:/books/A.pdf", "2026-01-01 00:00:00", "旧版"),
            ("aaa111_20260102000000_0002", "C:/books/A.pdf", "2026-01-02 00:00:00", "新版"),
            ("bbb222_20260103000000_0003", "D:/other/A.pdf", "2026-01-03 00:00:00", "另一处同名"),
        ]
        for fid, pdf, updated, content in seed:
            (hist_dir / f"{fid}.json").write_text(
                _json.dumps({"pdf": pdf, "name": "A.pdf", "updated": updated, "pages": {"1": f"<p>{content}</p>"}},
                            ensure_ascii=False), encoding="utf-8")
        state = {
            "pages": {}, "finished": __import__("threading").Event(), "preview_cache": {},
            "pdf_path": None, "img_dir": None, "preview_dpi": 110, "preview_quality": 82,
            "last_heartbeat": 0.0, "gone_at": None, "idle_timeout": 600.0, "auto_finished": False,
            "on_convert": None, "convert_lock": __import__("threading").Lock(),
            "history_prefix": None, "history_lock": __import__("threading").Lock(),
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CorrectionHandler)
        server.daemon_threads = True
        server.state = state
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            items = requests.get(base + "/api/history").json()["items"]
            self.assertEqual(len(items), 3)
            by_id = {it["id"]: it for it in items}
            # 同一文件多版本：C:/books/A.pdf 两个版本编号 v1(新)/v2(旧)
            group = [it for it in items if it["path"] == "C:/books/A.pdf"]
            self.assertEqual(len(group), 2)
            versions = sorted(it["version"] for it in group)
            self.assertEqual(versions, [1, 2])
            v1 = next(it for it in group if it["version"] == 1)
            self.assertEqual(v1["updated"], "2026-01-02 00:00:00", "v1 应为最新版本")
            # 文件名/路径分列：同名不同路径的两个条目都能看到
            self.assertTrue(all(it["name"] == "A.pdf" for it in items))
            self.assertEqual({it["path"] for it in items}, {"C:/books/A.pdf", "D:/other/A.pdf"})
            # 删除选中（多选：删掉旧版 + 另一处同名）
            r = requests.post(base + "/api/history/delete",
                              data=_json.dumps({"ids": ["aaa111_20260101000000_0001", "bbb222_20260103000000_0003"]})).json()
            self.assertEqual(r["deleted"], 2)
            self.assertFalse((hist_dir / "aaa111_20260101000000_0001.json").exists())
            self.assertFalse((hist_dir / "bbb222_20260103000000_0003.json").exists())
            self.assertTrue((hist_dir / "aaa111_20260102000000_0002.json").exists())
            # 全部删除
            r2 = requests.post(base + "/api/history/delete", data=_json.dumps({"all": True})).json()
            self.assertEqual(r2["deleted"], 1)
            self.assertEqual(list(hist_dir.glob("*.json")), [])
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(hist_dir, ignore_errors=True)


class TestConvertTextHtml(unittest.TestCase):
    """繁简转换：只转换文本节点，标签/属性（含标记 span）不变。"""

    def test_t2s_converts_text_only(self):
        out = convert_text_html(
            '<p>繁體中文<span data-ptoe-marker="note">注釋</span>測試</p>', "t2s"
        )
        self.assertEqual(
            out, '<p>繁体中文<span data-ptoe-marker="note">注释</span>测试</p>'
        )

    def test_s2t_converts_text_only(self):
        out = convert_text_html('<p>简体中文<b>加粗</b></p>', "s2t")
        self.assertEqual(out, "<p>簡體中文<b>加粗</b></p>")

    def test_bad_mode_raises(self):
        with self.assertRaises(ValueError):
            convert_text_html("<p>x</p>", "nope")

    def test_entities_and_ascii_untouched(self):
        out = convert_text_html("<p>a &amp; b</p>", "t2s")
        self.assertEqual(out, "<p>a &amp; b</p>")


class TestHistoryPreloadFlag(unittest.TestCase):
    """preload_history 开关：重新识别（epub 流水线）不加载旧暂存内容。"""

    def test_preload_off_returns_empty(self):
        self.assertEqual(
            _history_pages_for_init("x.pdf", history=True, preload_history=False), {}
        )

    def test_preload_off_even_with_history_disabled(self):
        self.assertEqual(
            _history_pages_for_init("x.pdf", history=False, preload_history=False), {}
        )

    def test_preload_on_loads_latest(self):
        from unittest import mock

        with mock.patch("correctmanage._load_latest_history", return_value={"1": "<p>旧</p>"}):
            got = _history_pages_for_init("x.pdf", history=True, preload_history=True)
        self.assertEqual(got, {"1": "<p>旧</p>"})

    def test_history_disabled_never_loads(self):
        from unittest import mock

        with mock.patch(
            "correctmanage._load_latest_history",
            side_effect=AssertionError("不应加载历史"),
        ):
            got = _history_pages_for_init("x.pdf", history=False, preload_history=True)
        self.assertEqual(got, {})


class TestConvertEndpoint(unittest.TestCase):
    """/api/convert：繁简转换端点，无状态（只返回结果，不改服务端内容）。"""

    def test_convert_endpoint_roundtrip(self):
        import json as _json
        from http.server import ThreadingHTTPServer
        from threading import Thread

        import requests
        from correctmanage import _CorrectionHandler

        state = {
            "pages": {1: "<p>原文</p>"},
            "finished": __import__("threading").Event(),
            "preview_cache": {},
            "pdf_path": None,
            "img_dir": None,
            "preview_dpi": 110,
            "preview_quality": 82,
            "last_heartbeat": 0.0,
            "gone_at": None,
            "idle_timeout": 600.0,
            "auto_finished": False,
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CorrectionHandler)
        server.daemon_threads = True
        server.state = state
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            body = _json.dumps(
                {"mode": "t2s", "pages": [{"page": 1, "html": "<p>繁體中文</p>"}]}
            )
            res = requests.post(base + "/api/convert", data=body).json()
            self.assertTrue(res["ok"])
            self.assertEqual(res["pages"], [{"page": 1, "html": "<p>繁体中文</p>"}])
            # 无状态：服务端内容不变
            self.assertEqual(state["pages"][1], "<p>原文</p>")
            # 非法 mode → 400
            bad = requests.post(
                base + "/api/convert", data=_json.dumps({"mode": "x", "pages": []})
            )
            self.assertEqual(bad.status_code, 400)
        finally:
            server.shutdown()
            server.server_close()


class TestCleanEndpoint(unittest.TestCase):
    """/api/clean：文本智能清理端点，无状态（只返回结果，不改服务端内容）。"""

    def test_clean_endpoint_roundtrip(self):
        import json as _json
        from http.server import ThreadingHTTPServer
        from threading import Thread

        import requests
        from correctmanage import _CorrectionHandler

        state = {
            "pages": {1: "<p>原文</p>"},
            "finished": __import__("threading").Event(),
            "preview_cache": {},
            "pdf_path": None,
            "img_dir": None,
            "preview_dpi": 110,
            "preview_quality": 82,
            "last_heartbeat": 0.0,
            "gone_at": None,
            "idle_timeout": 600.0,
            "auto_finished": False,
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CorrectionHandler)
        server.daemon_threads = True
        server.state = state
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            body = _json.dumps(
                {"pages": [{"page": 1, "html": "<p>第一段</p><p>续文</p>"}]}
            )
            res = requests.post(base + "/api/clean", data=body).json()
            self.assertTrue(res["ok"])
            self.assertEqual(res["pages"], [{"page": 1, "html": "<p>第一段续文</p>"}])
            # 无状态：服务端内容不变
            self.assertEqual(state["pages"][1], "<p>原文</p>")
        finally:
            server.shutdown()
            server.server_close()


class TestExport(unittest.TestCase):
    """_html_to_export_blocks：白名单块提取（跳 script/style、br→换行、实体还原）。"""

    def test_html_to_export_blocks(self):
        from correctmanage import _html_to_export_blocks

        blocks = _html_to_export_blocks(
            '<h2>标题</h2><p>第一段<br/>换行</p>'
            '<script>var x=1;</script><style>.a{}</style>'
            '<p>a <strong>b</strong> &amp; c</p>'
        )
        self.assertEqual(
            blocks,
            [
                ("h2", "标题"),
                ("p", "第一段\n换行"),
                ("p", "a b & c"),
            ],
        )
        # 空块/纯图片块不产生输出
        self.assertEqual(_html_to_export_blocks('<p>  </p><img src="x">'), [])


class TestExportEndpoint(unittest.TestCase):
    """/api/export：导出 TXT/DOCX 端点。body 带 path 时跳过保存对话框（测试用）。"""

    def _post(self, base, body):
        import json as _json

        import requests

        return requests.post(base + "/api/export", data=_json.dumps(body)).json()

    def _start(self):
        import threading
        from http.server import ThreadingHTTPServer

        from correctmanage import _CorrectionHandler

        state = {
            "pages": {1: "<p>原文</p>"},
            "finished": threading.Event(),
            "preview_cache": {},
            "pdf_path": None,
            "img_dir": None,
            "preview_dpi": 110,
            "preview_quality": 82,
            "last_heartbeat": 0.0,
            "gone_at": None,
            "idle_timeout": 600.0,
            "auto_finished": False,
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CorrectionHandler)
        server.daemon_threads = True
        server.state = state
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    def _stop(self, server):
        server.shutdown()
        server.server_close()

    def test_txt_export_with_path(self):
        server, base = self._start()
        try:
            out = Path(tempfile.mkdtemp(prefix="test_export_")) / "book.txt"
            res = self._post(
                base,
                {
                    "format": "txt",
                    "path": str(out),
                    "pages": [
                        {"page": 2, "html": "<p>第二页</p>"},
                        {"page": 1, "html": "<h2>标题</h2><p>第一段<br/>换行</p>"},
                    ],
                },
            )
            self.assertTrue(res["ok"])
            self.assertEqual(str(out), res["path"])
            raw = out.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))  # utf-8-sig BOM
            self.assertEqual(
                raw.decode("utf-8-sig").replace("\r\n", "\n"),
                "标题\n\n第一段\n换行\n\n第二页\n",
            )
        finally:
            self._stop(server)

    def test_docx_export_with_path(self):
        import zipfile

        server, base = self._start()
        try:
            out = Path(tempfile.mkdtemp(prefix="test_export_")) / "book.docx"
            res = self._post(
                base,
                {
                    "format": "docx",
                    "path": str(out),
                    "pages": [
                        {"page": 1, "html": "<h2>标题</h2><p>第一段<br/>A &amp; B</p>"},
                    ],
                },
            )
            self.assertTrue(res["ok"])
            with zipfile.ZipFile(out) as zf:
                names = set(zf.namelist())
                self.assertIn("[Content_Types].xml", names)
                self.assertIn("_rels/.rels", names)
                self.assertIn("word/document.xml", names)
                xml = zf.read("word/document.xml").decode("utf-8")
            # h2 → 大纲级别 1 + 加粗加大；<br> → w:br；& → &amp;
            self.assertIn('<w:outlineLvl w:val="1"/>', xml)
            self.assertIn("<w:b/>", xml)
            self.assertIn("<w:t xml:space=\"preserve\">标题</w:t>", xml)
            self.assertIn("第一段", xml)
            self.assertIn("<w:br/>", xml)
            self.assertIn("A &amp; B", xml)
        finally:
            self._stop(server)

    def test_bad_format_400(self):
        import requests

        server, base = self._start()
        try:
            r = requests.post(
                base + "/api/export",
                data='{"format": "pdf", "pages": []}',
            )
            self.assertEqual(r.status_code, 400)
            self.assertFalse(r.json()["ok"])
        finally:
            self._stop(server)

    def _post_dialog(self, base, body, server, timeout=5.0):
        """POST /api/export（走保存对话框路径），测试线程扮演主循环完成弹框。

        生产环境中对话框由 correct_pages 主循环 _drain_dialog_queue 弹出；
        测试里 _pick_export_path 被 mock，直接在测试线程 drain 即可。
        """
        import threading
        import time

        import correctmanage as _cm

        res: dict = {}

        def do_post():
            res["r"] = self._post(base, body)

        t = threading.Thread(target=do_post, daemon=True)
        t.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if server.state.get("dlg_queue"):
                break
            time.sleep(0.01)
        else:
            t.join(timeout=1)
            self.fail("导出对话框请求未进入队列")
        _cm._drain_dialog_queue(server.state)
        t.join(timeout=5)
        return res["r"]

    def test_dialog_cancelled_and_headless_fallback(self):
        import unittest.mock as mock

        import correctmanage as _cm

        server, base = self._start()
        try:
            # 用户在保存对话框中取消
            with mock.patch.object(_cm, "_pick_export_path", lambda st, fmt, fb: (None, True)):
                res = self._post_dialog(base, {"format": "txt", "pages": []}, server)
            self.assertFalse(res["ok"])
            self.assertTrue(res["cancelled"])
            # headless：无对话框可用，回退当前目录默认文件名
            out = Path(tempfile.mkdtemp(prefix="test_export_")) / "fallback.txt"
            with mock.patch.object(_cm, "_pick_export_path", lambda st, fmt, fb: (None, False)), mock.patch.object(
                _cm, "_default_export_path", lambda initial: str(out)
            ):
                res = self._post_dialog(
                    base, {"format": "txt", "pages": [{"page": 1, "html": "<p>兜底</p>"}]}, server
                )
            self.assertTrue(res["ok"])
            self.assertEqual(res["path"], str(out))
            self.assertFalse(res["used_dialog"])
        finally:
            self._stop(server)

    def test_dialog_queue_roundtrip(self):
        """主线程弹框路径：_pick_export_path 返回 (path, True) → ok + used_dialog。"""
        import unittest.mock as mock

        import correctmanage as _cm

        server, base = self._start()
        try:
            out = Path(tempfile.mkdtemp(prefix="test_export_")) / "roundtrip.txt"
            with mock.patch.object(_cm, "_pick_export_path", lambda st, fmt, fb: (str(out), True)):
                res = self._post_dialog(
                    base, {"format": "txt", "pages": [{"page": 1, "html": "<p>队列</p>"}]}, server
                )
            self.assertTrue(res["ok"])
            self.assertEqual(res["path"], str(out))
            self.assertTrue(res["used_dialog"])
            self.assertTrue(out.read_bytes().startswith(b"\xef\xbb\xbf"))
        finally:
            self._stop(server)

    def test_dialog_aborted_when_server_closing(self):
        """界面关闭（主循环 finally）唤醒等待对话框的 handler → 500 放弃导出。"""
        import threading
        import time

        import correctmanage as _cm

        server, base = self._start()
        try:
            res: dict = {}

            def do_post():
                res["r"] = self._post(base, {"format": "txt", "pages": []})

            t = threading.Thread(target=do_post, daemon=True)
            t.start()
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if server.state.get("dlg_queue"):
                    break
                time.sleep(0.01)
            _cm._abort_dialog_queue(server.state)
            t.join(timeout=5)
            self.assertFalse(res["r"]["ok"])
            self.assertIn("已关闭", res["r"].get("error", ""))
        finally:
            self._stop(server)


class TestHistoryLoadEndpoint(unittest.TestCase):
    """/api/history/load：按 id 读取某一历史版本并返回 pages（再次矫正用）。"""

    def test_load_returns_version_pages_sorted(self):
        import json as _json
        from http.server import ThreadingHTTPServer
        from threading import Thread

        import requests
        import correctmanage as _cm
        from correctmanage import _CorrectionHandler

        hist_dir = Path(tempfile.mkdtemp(prefix="test_histload_"))
        _orig_dir = _cm._history_dir
        _cm._history_dir = lambda: hist_dir
        self.addCleanup(lambda: setattr(_cm, "_history_dir", _orig_dir))
        self.addCleanup(lambda: shutil.rmtree(hist_dir, ignore_errors=True))

        vid = "abc123_20260101000000_0001"
        (hist_dir / f"{vid}.json").write_text(
            _json.dumps(
                {
                    "pdf": "C:/books/A.pdf",
                    "updated": "2026-01-01 00:00:00",
                    "pages": {"3": "<p>第三页旧版</p>", "1": "<p>第一页旧版</p>"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        state = {
            "pages": {}, "finished": __import__("threading").Event(), "preview_cache": {},
            "pdf_path": None, "img_dir": None, "preview_dpi": 110, "preview_quality": 82,
            "last_heartbeat": 0.0, "gone_at": None, "idle_timeout": 600.0, "auto_finished": False,
            "on_convert": None, "convert_lock": __import__("threading").Lock(),
            "history_prefix": None, "history_lock": __import__("threading").Lock(),
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CorrectionHandler)
        server.daemon_threads = True
        server.state = state
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            r = requests.post(
                base + "/api/history/load", data=_json.dumps({"id": vid})
            ).json()
            self.assertTrue(r["ok"])
            # 字段约定与 /api/convert 一致：{page, html}
            self.assertEqual(
                r["pages"],
                [
                    {"page": 1, "html": "<p>第一页旧版</p>"},
                    {"page": 3, "html": "<p>第三页旧版</p>"},
                ],
                "返回的 pages 应按页码升序，字段为 page/html",
            )
            self.assertEqual(r["pdf"], "C:/books/A.pdf")
            # 版本内容同步进服务端状态；记录名用于后续暂存/保存
            self.assertEqual(
                state["pages"],
                {1: "<p>第一页旧版</p>", 3: "<p>第三页旧版</p>"},
                "打开历史版本后 state 应同步该版本内容",
            )
            self.assertEqual(state["history_name"], "A.pdf")
            # 未知 id → 404
            bad = requests.post(base + "/api/history/load", data=_json.dumps({"id": "nope"}))
            self.assertEqual(bad.status_code, 404)
        finally:
            server.shutdown()
            server.server_close()

    def test_load_switches_preview_pdf_source(self):
        # 打开历史版本时，若该版本所属 PDF 存在，切换预览图来源并清空预览缓存
        import json as _json
        from http.server import ThreadingHTTPServer
        from threading import Thread

        import requests
        import correctmanage as _cm
        from correctmanage import _CorrectionHandler

        hist_dir = Path(tempfile.mkdtemp(prefix="test_histload_pdf_"))
        _orig_dir = _cm._history_dir
        _cm._history_dir = lambda: hist_dir
        self.addCleanup(lambda: setattr(_cm, "_history_dir", _orig_dir))
        self.addCleanup(lambda: shutil.rmtree(hist_dir, ignore_errors=True))

        fake_pdf = Path(tempfile.mkdtemp(prefix="test_histload_src_")) / "book.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake")
        self.addCleanup(lambda: shutil.rmtree(fake_pdf.parent, ignore_errors=True))

        vid = "def456_20260102000000_0002"
        (hist_dir / f"{vid}.json").write_text(
            _json.dumps({"pdf": str(fake_pdf), "pages": {"1": "<p>x</p>"}}, ensure_ascii=False),
            encoding="utf-8",
        )
        state = {
            "pages": {}, "finished": __import__("threading").Event(),
            "preview_cache": {1: ("image/jpeg", b"old")},  # 旧缓存必须被清空
            "pdf_path": "C:/old/book.pdf", "img_dir": None,
            "preview_dpi": 110, "preview_quality": 82,
            "last_heartbeat": 0.0, "gone_at": None, "idle_timeout": 600.0, "auto_finished": False,
            "on_convert": None, "convert_lock": __import__("threading").Lock(),
            "history_prefix": None, "history_lock": __import__("threading").Lock(),
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CorrectionHandler)
        server.daemon_threads = True
        server.state = state
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            r = requests.post(
                base + "/api/history/load", data=_json.dumps({"id": vid})
            ).json()
            self.assertTrue(r["ok"])
            self.assertEqual(state["pdf_path"], str(fake_pdf), "预览图来源应切换为该版本所属 PDF")
            self.assertEqual(state["preview_cache"], {}, "换书后旧页码的预览缓存应清空")
        finally:
            server.shutdown()
            server.server_close()


class TestCorrectPdfNoFileConversion(unittest.TestCase):
    """无文件模式（correct 不带 PDF）打开历史记录后也能转换 EPUB。"""

    def test_no_file_convert_with_loaded_record_name(self):
        import zipfile

        import correctmanage as _cm
        import mian

        tmp = Path(tempfile.mkdtemp(prefix="test_no_file_conv_"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        orig = _cm.correct_pages

        def fake_correct_pages(pages, **kw):
            # 模拟浏览器：打开历史记录后点「完成并转换」，带记录名
            conv = kw["on_convert"](
                [{"page": 1, "text": "<p>第一章</p><p>正文内容</p>"}],
                name="某书",
            )
            self.assertTrue(conv["ok"], f"无文件模式转换应成功: {conv}")
            return [{"page": 1, "text": "<p>第一章</p><p>正文内容</p>"}]

        _cm.correct_pages = fake_correct_pages
        try:
            res = mian.correct_pdf(None, out_dir=str(tmp))
        finally:
            _cm.correct_pages = orig

        self.assertIn("epub", res)
        epub_path = Path(res["epub"])
        self.assertTrue(epub_path.is_file(), f"epub 未生成: {res}")
        # 标题取自打开的历史记录名（无文件模式下默认未命名 → 某书）
        self.assertEqual(epub_path.name, "某书.epub")
        with zipfile.ZipFile(epub_path) as zf:
            names = zf.namelist()
            self.assertEqual(names[0], "mimetype")
            content = next(n for n in names if n.startswith("OEBPS/Text/content_"))
            html = zf.read(content).decode("utf-8")
            self.assertIn("第一章", html)
            self.assertIn("正文内容", html)

    def test_no_file_empty_content_still_rejected(self):
        import correctmanage as _cm
        import mian

        tmp = Path(tempfile.mkdtemp(prefix="test_no_file_empty_"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        orig = _cm.correct_pages
        seen = {}

        def fake_correct_pages(pages, **kw):
            seen["conv"] = kw["on_convert"]([{"page": 1, "text": ""}], name="某书")
            return [{"page": 1, "text": ""}]

        _cm.correct_pages = fake_correct_pages
        try:
            res = mian.correct_pdf(None, out_dir=str(tmp))
        finally:
            _cm.correct_pages = orig

        self.assertFalse(seen["conv"]["ok"], "空内容不应转换")
        self.assertIn("没有可转换的内容", seen["conv"]["message"])
        self.assertEqual(res, {"content_files": []}, "空内容时无转换结果")


class TestNoFileStageSave(unittest.TestCase):
    """无文件模式：打开历史版本后 保存/暂存/完成 能正常写入状态与历史缓存。"""

    def _state(self, on_convert=None):
        return {
            "pages": {},  # 无文件模式初始为空
            "finished": __import__("threading").Event(),
            "preview_cache": {},
            "pdf_path": None,
            "img_dir": None,
            "preview_dpi": 110,
            "preview_quality": 82,
            "last_heartbeat": 0.0,
            "gone_at": None,
            "idle_timeout": 600.0,
            "auto_finished": False,
            "on_convert": on_convert,
            "convert_lock": __import__("threading").Lock(),
            "history_prefix": "manual_abc123",
            "history_name": "手动录入",
            "history_lock": __import__("threading").Lock(),
        }

    def _serve(self, state):
        import json as _json
        from http.server import ThreadingHTTPServer
        from threading import Thread

        import correctmanage as _cm
        from correctmanage import _CorrectionHandler

        hist_dir = Path(tempfile.mkdtemp(prefix="test_nofile_save_"))
        _orig = _cm._history_dir
        _cm._history_dir = lambda: hist_dir
        self.addCleanup(lambda: setattr(_cm, "_history_dir", _orig))
        self.addCleanup(lambda: shutil.rmtree(hist_dir, ignore_errors=True))
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CorrectionHandler)
        server.daemon_threads = True
        server.state = state
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return f"http://127.0.0.1:{server.server_address[1]}", hist_dir

    def test_save_upserts_pages_outside_initial_state(self):
        import json as _json

        import requests

        state = self._state()
        base, hist_dir = self._serve(state)
        body = _json.dumps(
            {
                "pages": [
                    {"page": 1, "html": "<p>甲</p>"},
                    {"page": 2, "html": "<p>乙</p>"},
                    {"page": 3, "html": "<p>丙</p>"},
                ],
                "name": "某书",
            }
        )
        r = requests.post(base + "/api/save", data=body).json()
        self.assertEqual(r["saved"], 3, "会话外页码也应保存成功")
        self.assertEqual(state["pages"][1], "<p>甲</p>")
        self.assertEqual(state["pages"][3], "<p>丙</p>")
        self.assertEqual(state["history_name"], "某书")
        files = sorted(hist_dir.glob("manual_abc123_*.json"))
        self.assertTrue(files, "无文件模式暂存/保存也应写入历史缓存")
        data = _json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "某书")
        self.assertIn("1", data["pages"])
        self.assertIn("3", data["pages"])

    def test_finish_orders_upserted_pages_for_on_convert(self):
        import json as _json

        import requests

        calls = []

        def fake_convert(pages, **kw):
            calls.append([p["page"] for p in pages])
            return {"ok": True, "message": "转换完成", "epub": "out.epub"}

        state = self._state(on_convert=fake_convert)
        base, _hist = self._serve(state)
        body = _json.dumps(
            {"pages": [{"page": 5, "html": "<p>五</p>"}, {"page": 7, "html": "<p>七</p>"}]}
        )
        r = requests.post(base + "/api/finish", data=body).json()
        self.assertTrue(r["ok"])
        self.assertEqual(calls, [[5, 7]], "on_convert 应收到 upsert 后的全部页面（升序）")


if __name__ == "__main__":
    unittest.main()
