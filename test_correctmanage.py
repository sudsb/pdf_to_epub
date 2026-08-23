"""test_correctmanage.py — unittest suite for the manual-correction stage.

Covers:
- correctmanage.sanitize_html: whitelist cleaning of UI-submitted HTML
- htmlmanage.HTMLConverter._render_fragment: markup-aware rendering
  (legacy plain-text path must stay byte-identical)
- mian.pdf_to_epub(correct=True): wiring — correct_pages called between
  structuring and HTML conversion, EPUB still produced
"""

import os
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
    _parse_llm_suggestions,
    _proofread_plain_text,
    _full_punct,
    diff_reocr_texts,
    _page_text,
    _headings_to_body,
    _build_embedded_images,
    _prerender_embedded_images,
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
        # 尺寸 class 保留，evil/onerror 剥除
        out3 = sanitize_html('<p><img src="x.png" class="ptoe-img-full ptoe-img-w50" onerror="alert(1)" width="100"></p>')
        self.assertEqual(out3, '<p><img src="x.png" alt="插图" class="ptoe-img-full ptoe-img-w50"/></p>')

    def test_img_position_class_preserved(self):
        # <p> 上的位置 class（ptoe-img-left/center/right）保留
        out = sanitize_html('<p class="ptoe-img-full ptoe-img-right"><img src="x.png" alt="图"/></p>')
        self.assertEqual(
            out, '<p class="ptoe-img-full ptoe-img-right"><img src="x.png" alt="图"/></p>'
        )
        out2 = sanitize_html('<p class="ptoe-img-fit ptoe-img-left ptoe-align-center"><img src="y.png"/></p>')
        # ptoe-align-center 在 _ALIGN_CLASSES 中，保留；ptoe-img-left 也保留
        self.assertEqual(
            out2, '<p class="ptoe-img-fit ptoe-img-left ptoe-align-center"><img src="y.png" alt="插图"/></p>'
        )

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

    def test_search_mark_stripped(self):
        # 搜索高亮 <mark class="ptoe-search"> 不在白名单内，sanitize 会剥掉标签保留文字
        out = sanitize_html('<p>a<mark class="ptoe-search">b</mark>c</p>')
        self.assertNotIn('<mark', out)
        self.assertIn('abc', out)


class TestCleanPageHtml(unittest.TestCase):
    """clean_page_html：段落合并 / 段首符号 / 中英文标点 / 残留 HTML 标签清理。"""

    def test_merge_adjacent_paragraphs(self):
        # 显式 merge_paragraphs=True → 前块不以句末标点结尾时与下一块合并
        self.assertEqual(
            clean_page_html('<p>第一段</p><p>续文</p>', merge_paragraphs=True),
            '<p>第一段续文</p>',
        )

    def test_no_merge_by_default(self):
        # 默认 merge_paragraphs=False → 不合并相邻段落
        self.assertEqual(
            clean_page_html('<p>第一段</p><p>续文</p>'), '<p>第一段</p>\n<p>续文</p>'
        )

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
        # 对照组：普通结尾在 merge_paragraphs=True 时仍合并
        self.assertEqual(
            clean_page_html('<p>见附录</p><p>续文</p>', merge_paragraphs=True),
            '<p>见附录续文</p>',
        )

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


class TestPageText(unittest.TestCase):
    """矫正界面传入文本一律为正文：<h1>-<h6> 归一为 <p>，纯文本按行转 <div>。"""

    def test_plain_text_becomes_divs(self):
        self.assertEqual(_page_text("a\nb"), "<div>a</div><div>b</div>")

    def test_headings_normalized_to_body(self):
        self.assertEqual(
            _page_text("<h1>标题</h1><p>正文</p>"),
            "<p>标题</p><p>正文</p>",
        )
        self.assertEqual(
            _page_text("<h2>章标题</h2><p>正文</p>"),
            "<p>章标题</p><p>正文</p>",
        )

    def test_heading_attrs_preserved(self):
        self.assertEqual(
            _page_text('<h2 class="ptoe-align-center">标题</h2>'),
            '<p class="ptoe-align-center">标题</p>',
        )

    def test_markers_and_inline_preserved(self):
        self.assertEqual(
            _page_text(
                '<h1>a<span data-ptoe-marker="note">注</span><strong>b</strong></h1><p>正文</p>'
            ),
            '<p>a<span data-ptoe-marker="note">注</span><strong>b</strong></p><p>正文</p>',
        )

    def test_plain_p_and_marker_unchanged(self):
        self.assertEqual(_page_text("<p>正文</p>"), "<p>正文</p>")
        self.assertEqual(
            _page_text('<p>正文<span data-ptoe-marker="full">全文</span></p>'),
            '<p>正文<span data-ptoe-marker="full">全文</span></p>',
        )

    def test_headings_to_body_direct(self):
        self.assertEqual(
            _headings_to_body('<h3>三</h3><h6 class="x">六</h6><p>段</p>'),
            '<p>三</p><p class="x">六</p><p>段</p>',
        )

    def test_normalize_headings_false_preserves_user_headings(self):
        # 2026-08-15 修复：历史/已保存内容按原样 serve（normalize_headings=False）——
        # 用户手动设置的标题必须保留，否则「保存后重开，已设置的标题格式丢失」
        self.assertEqual(
            _page_text("<h1>用户标题</h1><p>正文</p>", normalize_headings=False),
            "<h1>用户标题</h1><p>正文</p>",
        )
        self.assertEqual(
            _page_text(
                '<h2 class="ptoe-align-center">居中标题</h2>',
                normalize_headings=False,
            ),
            '<h2 class="ptoe-align-center">居中标题</h2>',
        )
        # 纯文本仍按行转 <div>（与 normalize_headings 无关）
        self.assertEqual(
            _page_text("a\nb", normalize_headings=False),
            "<div>a</div><div>b</div>",
        )


class TestPagesEndpoint(unittest.TestCase):
    """/api/pages：传入矫正界面的文本一律为正文（<h1>-<h6> 归一为 <p>）。"""

    def _start(self):
        import threading
        from http.server import ThreadingHTTPServer

        from correctmanage import _CorrectionHandler

        state = {
            "pages": {
                1: "<h1>第一章</h1><p>正文段落</p>",
                2: '<h2 class="ptoe-align-center">第二节</h2><p>内容</p>',
                3: "纯文本行一\n纯文本行二",
            },
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

    def test_pages_served_as_body_text(self):
        import requests

        server, base = self._start()
        try:
            res = requests.get(base + "/api/pages").json()
            pages = {p["page"]: p["text"] for p in res["pages"]}
            # 2026-08-15 修复：已保存/历史内容按原样 serve——用户手动设置的标题
            # （<h1>-<h6>）必须保留，否则「保存后重开，已设置的标题格式丢失」；
            # OCR 自动标题的归一只在写入历史时做一次（_save_ocr_history）
            self.assertEqual(pages[1], "<h1>第一章</h1><p>正文段落</p>")
            self.assertEqual(
                pages[2],
                '<h2 class="ptoe-align-center">第二节</h2><p>内容</p>',
            )
            self.assertEqual(pages[3], "<div>纯文本行一</div><div>纯文本行二</div>")
        finally:
            self._stop(server)


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

    def test_note_no_marker_kept_in_place(self):
        # 文中没有注释标记：注释段落原位保留（仅套用注释格式，不移动、不报错）
        pages = [{"page": 1, "text": '<p>正文</p><p class="ptoe-note">注一</p>'}]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p>正文</p><p class="ptoe-note">注一</p>'}],
        )

    def test_note_no_marker_multiple_kept_in_place(self):
        # 多个注释段落且无注释标记：全部按原顺序原位保留
        pages = [
            {"page": 1, "text": '<p>正文一</p><p class="ptoe-note">注一</p><p>正文二</p><p class="ptoe-note">注二</p>'}
        ]
        self.assertEqual(
            apply_markers(pages),
            [
                {
                    "text": '<p>正文一</p><p class="ptoe-note">注一</p>'
                    '<p>正文二</p><p class="ptoe-note">注二</p>'
                }
            ],
        )

    def test_note_no_marker_join_stripped(self):
        # 无注释标记时，注释块内的段落标记按切段语义消费（标记本身不残留）
        pages = [
            {"page": 1, "text": '<p>正文</p><p class="ptoe-note"><span data-ptoe-marker="join">段落</span>注一</p>'}
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p>正文</p><p class="ptoe-note">注一</p>'}],
        )

    def test_note_no_marker_join_merges_adjacent_notes(self):
        # 无注释标记时，段落标记仍生效：相邻两个注释段落合并为一个 <p>
        # （2026-08-22 修复：原位保留路径此前忽略 join，注释合并失效）
        pages = [
            {"page": 1, "text": '<p>正文</p><p class="ptoe-note">注一</p>'
             '<p class="ptoe-note"><span data-ptoe-marker="join">段落</span>注二</p>'}
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p>正文</p><p class="ptoe-note">注一注二</p>'}],
        )

    def test_note_no_marker_trailing_join_merges_next_note(self):
        # 段尾段落标记：本注释与下一注释合并
        pages = [
            {"page": 1, "text": '<p>正文</p><p class="ptoe-note">注一<span data-ptoe-marker="join">段落</span></p>'
             '<p class="ptoe-note">注二</p>'}
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p>正文</p><p class="ptoe-note">注一注二</p>'}],
        )

    def test_note_no_marker_cross_page_join_merge(self):
        # 因分页折断的注释（无注释标记路径）：后半段带段落标记 → 合并为一个 <p>
        pages = [
            {"page": 1, "text": '<p>正文</p><p class="ptoe-note">注一前半</p>'},
            {"page": 2, "text": '<p class="ptoe-note"><span data-ptoe-marker="join">段落</span>注一后半</p>'},
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p>正文</p><p class="ptoe-note">注一前半注一后半</p>'}],
        )

    def test_note_no_marker_join_not_into_body(self):
        # 正文段尾的段落标记不把注释并进正文：注释保持独立 <p>
        pages = [
            {"page": 1, "text": '<p>正文前半<span data-ptoe-marker="join">段落</span></p>'
             '<p class="ptoe-note">注一</p>'}
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p>正文前半</p><p class="ptoe-note">注一</p>'}],
        )

    def test_note_no_marker_join_chain_three_notes(self):
        # 连续多个段落标记：三个注释段落合并为一个
        pages = [
            {"page": 1, "text": '<p>正文</p><p class="ptoe-note">注一</p>'
             '<p class="ptoe-note"><span data-ptoe-marker="join">段落</span>注二</p>'
             '<p class="ptoe-note"><span data-ptoe-marker="join">段落</span>注三</p>'}
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p>正文</p><p class="ptoe-note">注一注二注三</p>'}],
        )

    def test_note_no_marker_join_broken_by_body(self):
        # 中间隔了正文块：段落标记不跨非注释块生效
        pages = [
            {"page": 1, "text": '<p>正文</p><p class="ptoe-note">注一</p><p>中间正文</p>'
             '<p class="ptoe-note"><span data-ptoe-marker="join">段落</span>注二</p>'}
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p>正文</p><p class="ptoe-note">注一</p><p>中间正文</p><p class="ptoe-note">注二</p>'}],
        )

    def test_note_cross_page_join_merge(self):
        # 因分页被折断的注释：后半段带段落标记 → 与前半段合并为一条后插入正文
        pages = [
            {"page": 1, "text": '<p>甲<span data-ptoe-marker="note">注释</span></p><p class="ptoe-note">注一前半</p>'},
            {"page": 2, "text": '<p class="ptoe-note"><span data-ptoe-marker="join">段落</span>注一后半</p>'},
        ]
        self.assertEqual(
            apply_markers(pages),
            [{"text": '<p>甲<span class="ptoe-note">（注一前半注一后半）</span></p>'}],
        )

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
        # 2026-08-23: 导出清理空白符——非英数相邻的空白被移除
        self.assertEqual(
            self.conv._render_fragment("a < b & c"), "<p>a&lt;b&amp;c</p>"
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
        # 2026-08-23: 空白符清理后分页占位段内部空格被移除
        self.assertIn('<p class="ptoe-page-break"></p>', out)
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

            # 2026-08-23: 标题文本经空白符清理（「第一章 引言」→「第一章引言」）
            self.assertEqual(_headings(texts[0]), ["第一章引言"])
            self.assertEqual(_headings(texts[1]), [])
            self.assertEqual(_headings(texts[2]), [])
            # TOC 只列一次
            toc_html = (Path(outdir) / res["toc_file"]).read_text(encoding="utf-8")
            self.assertEqual(toc_html.count("第一章引言"), 1)
            # EPUB 3 导航声明 + TOC 片段↔正文 id 一致（2026-08 回归）：
            # 缺 epub:type="toc" / xmlns:epub 时严格阅读器不识别目录无法跳转
            self.assertIn('epub:type="toc"', toc_html)
            self.assertIn('xmlns:epub="http://www.idpf.org/2007/ops"', toc_html)
            toc_hrefs = _re.findall(r'<a href="([^"]+)"', toc_html)
            self.assertEqual(toc_hrefs, ["content_1.xhtml#h1"])
            self.assertIn('id="h1"', texts[0])
        finally:
            shutil.rmtree(outdir, ignore_errors=True)

    def test_align_css_no_text_indent(self):
        # 对齐段落取消首行缩进（2026-08-15）：p 默认 text-indent 1.5em 会让
        # 居中/居右段落首行偏移，与矫正界面（无缩进）显示不一致
        css = self.conv.cssm.generate_stylesheet()
        self.assertIn("p.ptoe-align-center, p.ptoe-align-left, p.ptoe-align-right", css)
        self.assertIn("text-indent: 0", css)

    def test_note_css_no_text_indent(self):
        # 注释段落顶格（2026-08-22）：p 默认 text-indent 1.5em，注释段落
        # 不需要首行缩进，直接顶格开始
        css = self.conv.cssm.generate_stylesheet()
        self.assertIn("p.ptoe-note", css)
        note_rule = css[css.index("p.ptoe-note"):]
        note_rule = note_rule[: note_rule.index("}")]
        self.assertIn("text-indent: 0", note_rule)

    def test_no_auto_book_title_h1_in_body(self):
        # 无标题正文不再自动补 <h1>书名</h1>（2026-08-15）：书名仅保留在
        # 导航栏目录条目（href 指向该页），正文不出现
        import tempfile as _tf

        outdir = _tf.mkdtemp(prefix="test_no_autoh1_")
        try:
            doc = {
                "pages": [{"page": 1, "text": "<p>正文第一段</p><p>正文第二段</p>"}],
                "body": "",
                "paragraphs": [],
                "meta": {"title": "我的书", "package_epub": False},
            }
            res = htmlmanage.HTMLConverter(outdir).convert_document(doc, merge_pages=True)
            xhtml = (Path(outdir) / res["content_files"][0]).read_text(encoding="utf-8")
            self.assertNotIn("<h1>我的书</h1>", xhtml)
            self.assertIn("<p>正文第一段</p>", xhtml)
            # 导航栏目录条目保留：书名 → 该内容页（无片段）
            toc_html = (Path(outdir) / res["toc_file"]).read_text(encoding="utf-8")
            self.assertIn("我的书", toc_html)
            self.assertIn('href="content_1.xhtml"', toc_html)
        finally:
            shutil.rmtree(outdir, ignore_errors=True)

    def test_first_img_page_cover_image_only_no_duplication(self):
        # 首章为整页图片（含 <img> 且剥标签后仅剩 OCR 噪声）时：封面 = 仅图片
        # （无书名页 h1），首章不再重复出现在正文（2026-08-15）
        import tempfile as _tf

        outdir = _tf.mkdtemp(prefix="test_img_cover_")
        try:
            doc = {
                "pages": [],
                "body": "",
                "paragraphs": [],
                "articles": [
                    {"text": '<p><img src="data:image/png;base64,AAA" alt="插图" class="ptoe-img-w100"/></p><p>#</p>'},
                    {"text": "<p>正文开始</p>"},
                ],
                "meta": {"title": "T", "package_epub": False},
            }
            res = htmlmanage.HTMLConverter(outdir).convert_document(doc, merge_pages=True)
            cover_path = Path(outdir) / "OEBPS" / "cover.xhtml"
            self.assertTrue(cover_path.exists())
            cover_html = cover_path.read_text(encoding="utf-8")
            self.assertIn("<img", cover_html)
            self.assertNotIn("<h1", cover_html)  # 无书名页
            # 首章已独立为封面，正文只剩第二篇
            self.assertEqual(len(res["content_files"]), 1)
            content_html = (Path(outdir) / res["content_files"][0]).read_text(encoding="utf-8")
            # 封面图不重复出现在正文（data URI 已提取为 Images/img_1.png）
            self.assertNotIn("data:image/png;base64,AAA", content_html)
            self.assertNotIn("Images/img_1.png", content_html)
        finally:
            shutil.rmtree(outdir, ignore_errors=True)

    def test_text_first_chapter_no_cover_page(self):
        # 首章为普通文字页且 meta 无封面图：不生成 cover.xhtml（2026-08-15）
        import tempfile as _tf

        outdir = _tf.mkdtemp(prefix="test_no_cover_")
        try:
            doc = {
                "pages": [{"page": 1, "text": "<p>正文</p>"}],
                "body": "",
                "paragraphs": [],
                "meta": {"title": "T", "package_epub": False},
            }
            res = htmlmanage.HTMLConverter(outdir).convert_document(doc, merge_pages=True)
            self.assertFalse(os.path.exists(os.path.join(outdir, "OEBPS", "cover.xhtml")))
            self.assertTrue((Path(outdir) / res["content_files"][0]).exists())
        finally:
            shutil.rmtree(outdir, ignore_errors=True)

    def test_full_img_css_page_break_and_fill(self):
        # 全画幅图片独立占页（page-break 前后）+ 占满整页（width/height 100% +
        # object-fit，2026-08-15；2026-08 修复：max-height:100vh 改 height:100%
        # 真正占满页面 + :first-child 不强制前置分页消除封面后空白页）
        css = self.conv.cssm.generate_stylesheet()
        self.assertIn("page-break-before: always", css)
        self.assertIn("page-break-after: always", css)
        self.assertIn("height: 100%", css)
        self.assertIn("object-fit: contain", css)
        self.assertIn("p.ptoe-img-full:first-child", css)

    def test_landmarks_toc_links_content_not_nav(self):
        # nav.xhtml 移出 spine 后，landmarks 链接必须指向 spine 内资源
        # （否则 epubcheck RSC-011 报错，2026-08-15）
        toc_html = self.conv.render_toc_page(
            [{"title": "章一", "href": "content_1.xhtml#h1", "level": 1}]
        )
        self.assertIn('epub:type="landmarks"', toc_html)
        self.assertNotIn('href="nav.xhtml"', toc_html)
        self.assertIn('href="content_1.xhtml"', toc_html)

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
        def fake_batch_infer(images, prompts, model_key="HY", max_workers=3, thinking=False, timeout=600, on_progress=None, on_result=None):
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
        mian._ensure_server = lambda model_key, workers=None: None
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
            # 默认不合并段落（merge_paragraphs=False）
            self.assertEqual(res["pages"], [{"page": 1, "html": "<p>第一段</p>\n<p>续文</p>"}])
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
        # 空块不产生输出；图片在块上下文内成为独立图片块（周围文本自动拆块）
        self.assertEqual(_html_to_export_blocks('<p>  </p>'), [])
        self.assertEqual(
            _html_to_export_blocks('<p><img src="x.png"/></p>'),
            [('img', 'x.png', '插图', '')],
        )
        self.assertEqual(
            _html_to_export_blocks('<p>前<img src="a"/>后</p>'),
            [('p', '前'), ('img', 'a', '插图', ''), ('p', '后')],
        )
        # 图片的 alt / class 属性被捕获；孤立 <img>（无块上下文）不产生块
        self.assertEqual(
            _html_to_export_blocks('<p><img src="x.png" alt="图" class="ptoe-img-w50"/></p>'),
            [('img', 'x.png', '图', 'ptoe-img-w50')],
        )
        self.assertEqual(_html_to_export_blocks('<img src="x.png"/>'), [])

    def test_build_docx_embeds_data_uri_png(self):
        import base64
        import zipfile

        from correctmanage import _build_docx

        # 1x1 透明 PNG（已知字节，IHDR 宽高均为 1）
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
        )
        src = "data:image/png;base64," + png_b64
        out = Path(tempfile.mkdtemp(prefix="test_docx_img_")) / "book.docx"
        _build_docx([("img", src, "插图", "")], str(out))
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            self.assertIn("word/media/image1.png", names)
            self.assertEqual(
                zf.read("word/media/image1.png"), base64.b64decode(png_b64)
            )
            rels = zf.read("word/_rels/document.xml.rels").decode("utf-8")
            self.assertIn('Id="rIdImg1"', rels)
            self.assertIn('Target="media/image1.png"', rels)
            self.assertIn("relationships/image", rels)
            ct = zf.read("[Content_Types].xml").decode("utf-8")
            self.assertIn('Extension="png" ContentType="image/png"', ct)
            xml = zf.read("word/document.xml").decode("utf-8")
            self.assertIn('r:embed="rIdImg1"', xml)
            # 1x1 PNG：aspect=1 → cy == cx；无尺寸 class → cx = 5 英寸 EMU
            self.assertIn('<wp:extent cx="4572000" cy="4572000"/>', xml)
        # 尺寸 class ptoe-img-w50 → 宽度减半
        out2 = Path(tempfile.mkdtemp(prefix="test_docx_img_")) / "book2.docx"
        _build_docx([("img", src, "插图", "ptoe-img-w50")], str(out2))
        with zipfile.ZipFile(out2) as zf:
            xml = zf.read("word/document.xml").decode("utf-8")
            self.assertIn('<wp:extent cx="2286000" cy="2286000"/>', xml)

    def test_build_docx_non_data_uri_img_placeholder(self):
        import zipfile

        from correctmanage import _build_docx

        out = Path(tempfile.mkdtemp(prefix="test_docx_img_")) / "book.docx"
        _build_docx([("img", "x.png", "插图", "")], str(out))
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            self.assertNotIn("word/media/image1.png", names)
            xml = zf.read("word/document.xml").decode("utf-8")
            self.assertIn("[图片]", xml)

    def test_txt_join_img_placeholder(self):
        # TXT 导出：图片块渲染为 [图片] 占位符（_html_to_export_blocks + join 表达式）
        from correctmanage import _html_to_export_blocks

        blocks = _html_to_export_blocks('<p>前<img src="a"/>后</p>')
        self.assertEqual(
            blocks,
            [("p", "前"), ("img", "a", "插图", ""), ("p", "后")],
        )
        text = "\n\n".join(('[图片]' if b[0] == 'img' else b[1]) for b in blocks) + "\n"
        self.assertEqual(text, "前\n\n[图片]\n\n后\n")


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


class TestExportEpub(unittest.TestCase):
    """/api/export format=epub：标记→文章结构→XHTML→打包 EPUB。"""

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
            "history_name": "测试书",
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CorrectionHandler)
        server.daemon_threads = True
        server.state = state
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    def _stop(self, server):
        server.shutdown()
        server.server_close()

    def _post(self, base, body):
        import json as _json

        import requests

        return requests.post(base + "/api/export", data=_json.dumps(body)).json()

    def test_epub_export_with_path(self):
        import zipfile

        server, base = self._start()
        try:
            out = Path(tempfile.mkdtemp(prefix="test_export_epub_")) / "book.epub"
            # 全文标记 → 新文章（新页）
            res = self._post(
                base,
                {
                    "format": "epub",
                    "path": str(out),
                    "pages": [
                        {
                            "page": 1,
                            "html": '<p>第一段</p><span class="ptoe-marker" data-ptoe-marker="full"></span><p>第二段</p>',
                        },
                    ],
                },
            )
            self.assertTrue(res["ok"], f"export failed: {res.get('error')}")
            self.assertEqual(str(out), res["path"])
            # EPUB 结构校验
            with zipfile.ZipFile(out) as zf:
                names = set(zf.namelist())
                self.assertIn("mimetype", names)
                # mimetype 必须 ZIP_STORED 且第一个条目
                self.assertEqual(zf.infolist()[0].filename, "mimetype")
                self.assertEqual(zf.infolist()[0].compress_type, zipfile.ZIP_STORED)
                self.assertIn("META-INF/container.xml", names)
                self.assertIn("OEBPS/content.opf", names)
                # 至少一个内容页
                content_files = sorted(
                    n for n in names if n.startswith("OEBPS/Text/content_")
                )
                self.assertTrue(content_files, "应生成至少一个内容页")
                # 内容含文章文本（全文标记 → 多文章 → 多内容页，全部检查）
                xml = zf.read(content_files[0]).decode("utf-8")
                all_xml = "".join(
                    zf.read(n).decode("utf-8") for n in content_files
                )
                self.assertIn("第一段", all_xml)
                self.assertIn("第二段", all_xml)
                # EPUB 兼容性（2026-08）：dcterms:modified / toc.ncx / XHTML 命名空间
                opf = zf.read("OEBPS/content.opf").decode("utf-8")
                self.assertIn('dcterms:modified', opf, "content.opf 必须含 dcterms:modified")
                self.assertIn('OEBPS/toc.ncx', names, "toc.ncx 必须存在（EPUB2/EPUB3 均生成）")
                self.assertIn('xmlns="http://www.w3.org/1999/xhtml"', xml, "content 页必须声明 XHTML 默认命名空间")
                # nav.xhtml 必须含 ARIA doc-toc 角色 + landmarks nav
                nav_xml = zf.read("OEBPS/Text/nav.xhtml").decode("utf-8")
                self.assertIn('role="doc-toc"', nav_xml, "nav 必须含 role=\"doc-toc\"")
                self.assertIn('epub:type="landmarks"', nav_xml, "nav.xhtml 必须含 landmarks nav")
        finally:
            self._stop(server)

    def test_epub_export_image_size_position_preserved(self):
        # EPUB 导出保留图片的尺寸/位置 class（ptoe-img-w*/ptoe-img-left/center/right）
        import zipfile
        server, base = self._start()
        try:
            out = Path(tempfile.mkdtemp(prefix="test_export_epub_img_")) / "img.epub"
            res = self._post(
                base,
                {
                    "format": "epub",
                    "path": str(out),
                    "pages": [
                        {
                            "page": 1,
                            "html": '<p>带图正文</p><p class="ptoe-img-full ptoe-img-right"><img class="ptoe-img-w75" src="data:image/png;base64,AAA" alt="插图"/></p>',
                        },
                    ],
                },
            )
            self.assertTrue(res["ok"], f"export failed: {res.get('error')}")
            with zipfile.ZipFile(out) as zf:
                content_files = [n for n in zf.namelist() if n.startswith("OEBPS/Text/content_")]
                self.assertTrue(content_files, "应生成内容页")
                xml = zf.read(content_files[0]).decode("utf-8")
                self.assertIn('ptoe-img-full', xml)
                self.assertIn('ptoe-img-right', xml)
                self.assertIn('ptoe-img-w75', xml)
                self.assertIn('data:image/png;base64,AAA', xml)
        finally:
            self._stop(server)

    def test_epub_export_div_blocks_alignment_preserved(self):
        # 浏览器编辑产生的 <div> 块（Chrome contenteditable 回车）导出 epub 时
        # 必须先 sanitize 归一为 <p>（保留对齐 class），否则产出 <p><div…> 非法
        # 嵌套、对齐丢失（2026-08-15）
        import zipfile

        server, base = self._start()
        try:
            out = Path(tempfile.mkdtemp(prefix="test_export_epub_div_")) / "div.epub"
            res = self._post(
                base,
                {
                    "format": "epub",
                    "path": str(out),
                    "pages": [
                        {
                            "page": 1,
                            "html": '<div class="ptoe-align-center">居中段落</div><div class="ptoe-align-right">右对齐</div>',
                        },
                    ],
                },
            )
            self.assertTrue(res["ok"], f"export failed: {res.get('error')}")
            with zipfile.ZipFile(out) as zf:
                content_files = [
                    n for n in zf.namelist() if n.startswith("OEBPS/Text/content_")
                ]
                self.assertTrue(content_files, "应生成内容页")
                xml = zf.read(content_files[0]).decode("utf-8")
                self.assertIn('<p class="ptoe-align-center">居中段落</p>', xml)
                self.assertIn('<p class="ptoe-align-right">右对齐</p>', xml)
                self.assertNotIn("<div", xml)
        finally:
            self._stop(server)

    def test_epub_export_nav_not_in_spine(self):
        # nav.xhtml 回到 spine（2026-08-23）：以 linear="no" 存在（阅读器可发现
        # 目录但不进阅读顺序，正文不显示目录页）；manifest properties="nav" 保留
        import re as _re
        import zipfile

        server, base = self._start()
        try:
            out = Path(tempfile.mkdtemp(prefix="test_export_epub_spine_")) / "spine.epub"
            res = self._post(
                base,
                {
                    "format": "epub",
                    "path": str(out),
                    "pages": [{"page": 1, "html": "<p>正文</p>"}],
                },
            )
            self.assertTrue(res["ok"], f"export failed: {res.get('error')}")
            with zipfile.ZipFile(out) as zf:
                names = set(zf.namelist())
                self.assertIn("OEBPS/Text/nav.xhtml", names, "nav.xhtml 仍在 manifest 中")
                opf = zf.read("OEBPS/content.opf").decode("utf-8")
                # nav 项带 properties="nav"
                self.assertIn('properties="nav"', opf)
                # spine 含 nav.xhtml 且 itemref 带 linear="no"：把 idref 映射回 href 校验
                ids = dict(
                    _re.findall(
                        r'<item\b[^>]*\bid="([^"]+)"[^>]*\bhref="([^"]+)"', opf
                    )
                )
                refs = _re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"[^>]*>', opf)
                ref_attrs = {
                    m.group(1): m.group(0)
                    for m in _re.finditer(r'<itemref\b[^>]*\bidref="([^"]+)"[^>]*>', opf)
                }
                spine_hrefs = [ids.get(_re.search(r'idref="([^"]+)"', a).group(1), "") for a in ref_attrs.values()]
                self.assertTrue(spine_hrefs, "spine 应含 itemref")
                self.assertIn(
                    "Text/nav.xhtml", spine_hrefs, "nav.xhtml 应在 spine 中（linear=no）"
                )
                nav_ref = next(
                    (a for a in ref_attrs.values() if ids.get(_re.search(r'idref="([^"]+)"', a).group(1)) == "Text/nav.xhtml"),
                    "",
                )
                self.assertIn('linear="no"', nav_ref, "nav itemref 应为 linear=\"no\"")
                self.assertTrue(
                    any("content_" in h for h in spine_hrefs), "spine 应含正文页"
                )
        finally:
            self._stop(server)

    def test_epub_export_empty_pages_ok(self):
        server, base = self._start()
        try:
            out = Path(tempfile.mkdtemp(prefix="test_export_epub_")) / "empty.epub"
            res = self._post(base, {"format": "epub", "path": str(out), "pages": []})
            self.assertTrue(res["ok"], f"empty pages failed: {res.get('error')}")
        finally:
            self._stop(server)

    def test_epub_export_bad_dir_fails_gracefully(self):
        server, base = self._start()
        try:
            res = self._post(
                base,
                {
                    "format": "epub",
                    "path": "/nonexistent_dir_xyz/book.epub",
                    "pages": [{"page": 1, "html": "<p>x</p>"}],
                },
            )
            # 不抛异常（不 500）；目录创建失败时返回 ok:false
            self.assertIsInstance(res, dict)
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

    def test_load_preserves_user_headings(self):
        # 2026-08-15 修复：历史版本中用户手动设置的标题（<h1>-<h6>）必须保留，
        # 否则「保存后重开，已设置的标题格式丢失」——不再归一为 <p>
        import json as _json
        from http.server import ThreadingHTTPServer
        from threading import Thread

        import requests
        import correctmanage as _cm
        from correctmanage import _CorrectionHandler

        hist_dir = Path(tempfile.mkdtemp(prefix="test_histload_h_"))
        _orig_dir = _cm._history_dir
        _cm._history_dir = lambda: hist_dir
        self.addCleanup(lambda: setattr(_cm, "_history_dir", _orig_dir))
        self.addCleanup(lambda: shutil.rmtree(hist_dir, ignore_errors=True))

        vid = "abc123_20260101000000_0002"
        (hist_dir / f"{vid}.json").write_text(
            _json.dumps(
                {
                    "pdf": "C:/books/A.pdf",
                    "updated": "2026-01-01 00:00:00",
                    "pages": {
                        "1": '<h1 class="ptoe-align-center">用户标题</h1><p>正文</p>',
                        "2": "<p>普通段落</p>",
                    },
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
            self.assertEqual(
                r["pages"],
                [
                    {"page": 1, "html": '<h1 class="ptoe-align-center">用户标题</h1><p>正文</p>'},
                    {"page": 2, "html": "<p>普通段落</p>"},
                ],
                "历史版本中的用户标题应原样返回（不再归一为 <p>）",
            )
            self.assertEqual(
                state["pages"],
                {
                    1: '<h1 class="ptoe-align-center">用户标题</h1><p>正文</p>',
                    2: "<p>普通段落</p>",
                },
                "state 同步也应保留用户标题",
            )
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

        def _cleanup_server():
            # 顺序必须 shutdown 先于 server_close：addCleanup 按 LIFO 执行，
            # 若先 close 监听 socket，Windows 下 serve_forever 阻塞中的
            # select() 会抛 OSError [WinError 10038]（在非套接字对象上操作）。
            server.shutdown()
            server.server_close()

        self.addCleanup(_cleanup_server)
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


class TestProofreadSettingsEndpoint(unittest.TestCase):
    """/api/proofread_settings：LLM 深度校对设置服务端持久化（config.json），
    随机端口下 localStorage 每运行失效，故开关/模型改存配置。"""

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

    def _patch_cfg(self, enable_llm=False, llm_model="", choices=None, selected="HY"):
        import configmanage

        choices = choices if choices is not None else {"HY": {}, "QWEN.8": {}, "QWEN2": {}}
        cfg = {
            "model_choices": choices,
            "selected_model": selected,
            "proofread": {"enable_llm": enable_llm, "llm_model": llm_model},
        }
        self._orig_get = configmanage.get_config
        configmanage.get_config = lambda *a, **k: cfg
        self.addCleanup(lambda: setattr(configmanage, "get_config", self._orig_get))
        return cfg

    def _patch_setter(self):
        import configmanage

        calls = []
        self._orig_set = configmanage.set_proofread_param
        configmanage.set_proofread_param = lambda name, value: calls.append((name, value))
        self.addCleanup(lambda: setattr(configmanage, "set_proofread_param", self._orig_set))
        return calls

    def test_get_shape(self):
        import requests

        self._patch_cfg(enable_llm=True, llm_model="QWEN2")
        server, base = self._start()
        try:
            res = requests.get(base + "/api/proofread_settings").json()
            self.assertTrue(res["ok"])
            self.assertTrue(res["enabled"])
            self.assertEqual(res["model"], "QWEN2")
            self.assertEqual(res["available"], ["HY", "QWEN.8", "QWEN2"])
            self.assertEqual(res["selected"], "HY")
        finally:
            self._stop(server)

    def test_get_unregistered_model_treated_as_empty(self):
        import requests

        self._patch_cfg(enable_llm=False, llm_model="qwen2b")  # qwen2b 不在 model_choices
        server, base = self._start()
        try:
            res = requests.get(base + "/api/proofread_settings").json()
            self.assertEqual(res["model"], "", "未注册模型应视同未设置（跟随 selected_model）")
        finally:
            self._stop(server)

    def test_post_persists_via_set_proofread_param(self):
        import json as _json

        import requests

        self._patch_cfg()
        calls = self._patch_setter()
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/proofread_settings",
                data=_json.dumps({"enabled": True, "model": "QWEN.8"}),
            ).json()
            self.assertTrue(res["ok"])
            self.assertEqual(calls, [("enable_llm", True), ("llm_model", "QWEN.8")])
        finally:
            self._stop(server)

    def test_post_invalid_model_400(self):
        import json as _json

        import requests

        self._patch_cfg()
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/proofread_settings",
                data=_json.dumps({"enabled": True, "model": "不存在"}),
            ).json()
            self.assertFalse(res["ok"])
            self.assertIn("未在配置中注册", res["error"])
        finally:
            self._stop(server)


class TestProofreadLlmError(unittest.TestCase):
    """/api/proofread：LLM 深度校对失败时 llm_error 上浮、基础 errors 保留、不静默吞掉。"""

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

    def _patch_cfg(self):
        import configmanage

        # 本类测的是 LLM 错误上浮，基础 errors 需非空 → 显式开原有规则
        # （半角标点转全角，2026-08-09 起默认关闭）
        cfg = {
            "model_choices": {"HY": {}, "QWEN.8": {}},
            "selected_model": "HY",
            "proofread": {"enable_legacy_rules": True},
        }
        self._orig_get = configmanage.get_config
        configmanage.get_config = lambda *a, **k: cfg
        self.addCleanup(lambda: setattr(configmanage, "get_config", self._orig_get))

    def _patch_llama(self, result):
        import llamamanage

        calls = []
        self._orig_req = llamamanage.request

        def fake_request(prompt="Hello", model_key="HY", thinking=False, append_ocr_instruction=True):
            calls.append(
                {
                    "prompt": prompt,
                    "model_key": model_key,
                    "thinking": thinking,
                    "append_ocr_instruction": append_ocr_instruction,
                }
            )
            return result

        llamamanage.request = fake_request
        self.addCleanup(lambda: setattr(llamamanage, "request", self._orig_req))
        return calls

    def test_request_failure_surfaces_llm_error(self):
        import json as _json

        import requests

        self._patch_cfg()
        calls = self._patch_llama({"result": None, "error": "boom"})
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/proofread",
                data=_json.dumps({"html": "<p>这个,那个</p>", "use_llm": True, "llm_model": ""}),
            ).json()
            self.assertTrue(res["ok"])
            self.assertFalse(res["llm_used"])
            self.assertEqual(res["llm_error"], "boom")
            self.assertTrue(res["errors"], "基础规则纠错结果应保留")
            self.assertEqual(calls[0]["append_ocr_instruction"], False, "校对 prompt 不应追加 OCR 后缀")
            self.assertEqual(calls[0]["thinking"], False)
            self.assertEqual(calls[0]["model_key"], "HY")  # 空 model → 回退 selected_model
        finally:
            self._stop(server)

    def test_invalid_model_rejected_without_call(self):
        import json as _json

        import requests

        self._patch_cfg()
        calls = self._patch_llama({"result": None, "error": "不应被调用"})
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/proofread",
                data=_json.dumps({"html": "<p>测试</p>", "use_llm": True, "llm_model": "NOPE"}),
            ).json()
            self.assertTrue(res["ok"])
            self.assertFalse(res["llm_used"])
            self.assertIn("未在配置中注册", res["llm_error"])
            self.assertEqual(calls, [], "模型未注册时不应发起 LLM 调用")
        finally:
            self._stop(server)

    def test_llm_suggestions_appended_to_errors(self):
        import json as _json

        import requests

        self._patch_cfg()
        sug = {"start": 0, "end": 2, "wrong": "这个", "candidates": [{"text": "那个", "score": 0.9}]}
        calls = self._patch_llama({"result": _json.dumps({"suggestions": [sug]}), "error": None})
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/proofread",
                data=_json.dumps({"html": "<p>这个,那个</p>", "use_llm": True, "llm_model": ""}),
            ).json()
            self.assertTrue(res["ok"])
            self.assertTrue(res["llm_used"])
            self.assertIsNone(res["llm_error"])
            wrongs = [e.get("wrong") for e in res["errors"]]
            self.assertIn("这个", wrongs, "LLM 建议应并入 errors")
            self.assertTrue(calls, "应发起 LLM 调用")
        finally:
            self._stop(server)

    def test_conn_error_mapped_to_friendly(self):
        """连接失败（llama-server 未运行）→ 友好指引文案，而非原始 requests 异常串。"""
        import json as _json

        import requests

        self._patch_cfg()
        raw_err = (
            "HTTPConnectionPool(host='127.0.0.1', port=8080): Max retries exceeded with "
            "url: /v1/chat/completions (Caused by NewConnectionError(\"HTTPConnection("
            "host='127.0.0.1', port=8080): Failed to establish a new connection: "
            "[WinError 10061] 由于目标计算机积极拒绝，无法连接。 \"))"
        )
        self._patch_llama({"result": None, "error": raw_err})
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/proofread",
                data=_json.dumps({"html": "<p>这个,那个</p>", "use_llm": True, "llm_model": ""}),
            ).json()
            self.assertTrue(res["ok"])
            self.assertFalse(res["llm_used"])
            self.assertIn("无法连接本地 llama-server", res["llm_error"])
            self.assertNotIn("HTTPConnectionPool", res["llm_error"], "不应透出原始异常串")
            self.assertTrue(res["errors"], "基础规则纠错结果应保留")
        finally:
            self._stop(server)

    def test_timeout_error_mapped(self):
        """超时（WinError 10060/timed out）→ 超时友好提示。"""
        import json as _json

        import requests

        self._patch_cfg()
        self._patch_llama({"result": None, "error": "HTTPSConnectionPool: Read timed out (WinError 10060)"})
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/proofread",
                data=_json.dumps({"html": "<p>测试</p>", "use_llm": True, "llm_model": ""}),
            ).json()
            self.assertIn("超时", res["llm_error"])
            self.assertNotIn("HTTPSConnectionPool", res["llm_error"])
        finally:
            self._stop(server)

    def test_other_error_unchanged(self):
        """无法归类的错误原样透出（不猜测、不吞掉）。"""
        import json as _json

        import requests

        self._patch_cfg()
        self._patch_llama({"result": None, "error": "boom"})
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/proofread",
                data=_json.dumps({"html": "<p>测试</p>", "use_llm": True, "llm_model": ""}),
            ).json()
            self.assertEqual(res["llm_error"], "boom")
        finally:
            self._stop(server)

    def test_extra_data_scenario_now_parses(self):
        """用户场景回归：模型返回 JSON + 尾随说明（曾致 Extra data: line 1 column N）。
        中间层 raw_decode 取第一个完整对象 → llm_used=True、llm_error=None。"""
        import json as _json

        import requests

        self._patch_cfg()
        raw = (
            '{"suggestions": [{"start": 0, "end": 2, "wrong": "这个", '
            '"candidates": [{"text": "那个", "score": 0.9}]}]} 仅作参考'
        )
        self._patch_llama({"result": raw, "error": None})
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/proofread",
                data=_json.dumps({"html": "<p>这个,那个</p>", "use_llm": True, "llm_model": ""}),
            ).json()
            self.assertTrue(res["ok"])
            self.assertTrue(res["llm_used"])
            self.assertIsNone(res["llm_error"], "尾随说明不应再触发 Extra data")
            wrongs = [e.get("wrong") for e in res["errors"]]
            self.assertIn("这个", wrongs, "LLM 建议应并入 errors")
        finally:
            self._stop(server)

    def test_unparseable_llm_chinese_error(self):
        """模型响应完全不可解析 → 中文错误「模型响应解析失败」上浮，不透出 Extra data，
        基础规则纠错结果保留。"""
        import json as _json

        import requests

        self._patch_cfg()
        self._patch_llama({"result": "很抱歉，我无法完成这个任务。", "error": None})
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/proofread",
                data=_json.dumps({"html": "<p>这个,那个</p>", "use_llm": True, "llm_model": ""}),
            ).json()
            self.assertTrue(res["ok"])
            self.assertFalse(res["llm_used"])
            self.assertEqual(res["llm_error"], "模型响应解析失败：无法提取有效 JSON")
            self.assertNotIn("Extra data", res["llm_error"], "不应透出原始英文异常")
            self.assertTrue(res["errors"], "基础规则纠错结果应保留")
        finally:
            self._stop(server)


class TestLlmServerControl(unittest.TestCase):
    """/api/llm_status|llm_start|llm_stop：矫正界面手动启停 llama-server（文本校对用，不附加 --mmproj）。"""

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

    def _patch_cfg(self, **overrides):
        import configmanage

        cfg = {
            "model_choices": {"HY": {"name": "HY.gguf"}, "QWEN.8": {"name": "qwen3.5.gguf"}},
            "selected_model": "HY",
            "proofread": {},
        }
        cfg.update(overrides)
        self._orig_get = configmanage.get_config
        configmanage.get_config = lambda *a, **k: cfg
        self.addCleanup(lambda: setattr(configmanage, "get_config", self._orig_get))
        return cfg

    def _patch_llama_attr(self, name, value):
        import llamamanage

        orig = getattr(llamamanage, name)
        setattr(llamamanage, name, value)
        self.addCleanup(lambda: setattr(llamamanage, name, orig))

    def test_llm_status_probe(self):
        import requests

        self._patch_cfg()
        calls = []
        self._patch_llama_attr(
            "_probe_server",
            lambda model_name: calls.append(model_name) or ("none" if model_name == "HY.gguf" else "match"),
        )
        server, base = self._start()
        try:
            res = requests.get(base + "/api/llm_status").json()
            self.assertTrue(res["ok"])
            self.assertFalse(res["running"])
            self.assertEqual(res["model"], "HY")  # proofread.llm_model 空 → selected_model
            self.assertEqual(calls, ["HY.gguf"], "探测应使用 model_choices 中注册的模型名")
        finally:
            self._stop(server)

    def test_llm_status_running_when_probe_match(self):
        import requests

        self._patch_cfg(proofread={"llm_model": "QWEN.8"})
        self._patch_llama_attr("_probe_server", lambda model_name: "match")
        server, base = self._start()
        try:
            res = requests.get(base + "/api/llm_status").json()
            self.assertTrue(res["ok"])
            self.assertTrue(res["running"])
            self.assertEqual(res["model"], "QWEN.8")
            self.assertFalse(res["loading"], "已运行时不应报启动中")
        finally:
            self._stop(server)

    def test_llm_status_loading_when_process_alive_but_probe_none(self):
        # 进程存活但 health 仍 503（模型加载中）→ loading=True（2026-08-09）
        import requests

        class _Proc:
            def poll(self):
                return None

        self._patch_cfg()
        self._patch_llama_attr("_probe_server", lambda model_name: "none")
        self._patch_llama_attr("_server_process", _Proc())
        server, base = self._start()
        try:
            res = requests.get(base + "/api/llm_status").json()
            self.assertTrue(res["ok"])
            self.assertFalse(res["running"])
            self.assertTrue(res["loading"])
        finally:
            self._stop(server)

    def test_llm_status_not_loading_when_no_process(self):
        import requests

        self._patch_cfg()
        self._patch_llama_attr("_probe_server", lambda model_name: "none")
        self._patch_llama_attr("_server_process", None)
        server, base = self._start()
        try:
            res = requests.get(base + "/api/llm_status").json()
            self.assertFalse(res["running"])
            self.assertFalse(res["loading"], "无进程时不应报启动中")
        finally:
            self._stop(server)

    def test_llm_status_mismatch_reports_running(self):
        # 端口被其他模型占用（probe=mismatch）：应报运行中且 mismatch=True，
        # 让「停止服务」按钮可用（2026-08-13：模型不符时无法停止的修复）
        import requests

        self._patch_cfg()
        self._patch_llama_attr("_probe_server", lambda model_name: "mismatch")
        server, base = self._start()
        try:
            res = requests.get(base + "/api/llm_status").json()
            self.assertTrue(res["ok"])
            self.assertTrue(res["running"])
            self.assertTrue(res["mismatch"])
            self.assertFalse(res["loading"])
        finally:
            self._stop(server)

    def test_llm_stop_reports_still_occupied(self):
        # stopserver 后端口仍被占用（杀不掉）→ 提示手动关闭而非误报已停止
        import requests

        self._patch_llama_attr("stopserver", lambda: None)
        self._patch_llama_attr("_probe_server", lambda model_name: "mismatch")
        server, base = self._start()
        try:
            res = requests.post(base + "/api/llm_stop").json()
            self.assertTrue(res["ok"])
            self.assertIn("仍有进程占用", res["message"])
        finally:
            self._stop(server)

    def test_llm_start_calls_runserver_with_mmproj_false(self):
        import json as _json

        import requests

        self._patch_cfg()
        calls = []
        self._patch_llama_attr(
            "runserver",
            lambda model_key, with_mmproj=True: calls.append((model_key, with_mmproj)) or True,
        )
        server, base = self._start()
        try:
            res = requests.post(base + "/api/llm_start", data=_json.dumps({"model": ""})).json()
            self.assertTrue(res["ok"])
            self.assertTrue(res["running"])
            self.assertEqual(res["message"], "llama-server 已就绪")
            self.assertEqual(calls, [("HY", False)], "空 model → 回退 selected_model，且不附加 --mmproj")
        finally:
            self._stop(server)

    def test_llm_start_body_model_wins(self):
        import json as _json

        import requests

        self._patch_cfg()
        calls = []
        self._patch_llama_attr(
            "runserver",
            lambda model_key, with_mmproj=True: calls.append((model_key, with_mmproj)) or True,
        )
        server, base = self._start()
        try:
            res = requests.post(base + "/api/llm_start", data=_json.dumps({"model": "QWEN.8"})).json()
            self.assertTrue(res["ok"])
            self.assertEqual(calls, [("QWEN.8", False)], "body.model 应优先于配置")
        finally:
            self._stop(server)

    def test_llm_start_invalid_model_400(self):
        import json as _json

        import requests

        self._patch_cfg()
        calls = []
        self._patch_llama_attr(
            "runserver",
            lambda model_key, with_mmproj=True: calls.append((model_key, with_mmproj)) or True,
        )
        server, base = self._start()
        try:
            res = requests.post(base + "/api/llm_start", data=_json.dumps({"model": "NOPE"}))
            self.assertEqual(res.status_code, 400)
            body = res.json()
            self.assertFalse(body["ok"])
            self.assertIn("未在配置中注册", body["error"])
            self.assertIn("HY", body["error"], "错误信息应列出可用模型")
            self.assertEqual(calls, [], "模型未注册时不应调用 runserver")
        finally:
            self._stop(server)

    def test_llm_stop_calls_stopserver(self):
        import requests

        self._patch_llama_attr("stopserver", lambda: None)
        self._patch_llama_attr("_probe_server", lambda model_name: "none")
        server, base = self._start()
        try:
            res = requests.post(base + "/api/llm_stop").json()
            self.assertTrue(res["ok"])
            self.assertEqual(res["message"], "已停止 llama-server")
        finally:
            self._stop(server)

    def test_llm_start_mmproj_model_uses_image_mode(self):
        """带 mmproj 的模型 → runserver 以 with_mmproj=True 调用（图像重识别）。"""
        import json as _json

        import requests

        self._patch_cfg(
            model_choices={
                "HY": {"name": "HY.gguf"},
                "VLM": {"name": "vlm.gguf", "mmproj": "vlm.mmproj"},
            }
        )
        calls = []
        self._patch_llama_attr(
            "runserver",
            lambda model_key, with_mmproj=True: calls.append((model_key, with_mmproj)) or True,
        )
        server, base = self._start()
        try:
            res = requests.post(base + "/api/llm_start", data=_json.dumps({"model": "VLM"})).json()
            self.assertTrue(res["ok"])
            self.assertTrue(res["running"])
            self.assertTrue(res["image_model"], "带 mmproj 的模型应标记 image_model=True")
            self.assertEqual(calls, [("VLM", True)], "带 mmproj 的模型应以图像模式启动")
        finally:
            self._stop(server)

    def test_llm_start_no_mmproj_model_uses_text_mode(self):
        """无 mmproj 的模型 → runserver 以 with_mmproj=False 调用（纯文本）。"""
        import json as _json

        import requests

        self._patch_cfg(
            model_choices={
                "HY": {"name": "HY.gguf"},
                "VLM": {"name": "vlm.gguf", "mmproj": "vlm.mmproj"},
            }
        )
        calls = []
        self._patch_llama_attr(
            "runserver",
            lambda model_key, with_mmproj=True: calls.append((model_key, with_mmproj)) or True,
        )
        server, base = self._start()
        try:
            res = requests.post(base + "/api/llm_start", data=_json.dumps({"model": "HY"})).json()
            self.assertTrue(res["ok"])
            self.assertFalse(res["image_model"], "无 mmproj 的模型应标记 image_model=False")
            self.assertEqual(calls, [("HY", False)], "无 mmproj 的模型应以纯文本模式启动")
        finally:
            self._stop(server)


class TestReocr(unittest.TestCase):
    """diff_reocr_texts 纯函数 + /api/reocr 端点：大模型重识别后逐行逐字对比。"""

    def test_diff_identical(self):
        self.assertEqual(diff_reocr_texts("hello", "hello"), [])

    def test_diff_trailing_insert_annotated(self):
        # "你好世界" vs "你好世界。" → 末尾少字（原文本缺字）→ 锚定前邻字符"界"
        diff = diff_reocr_texts("你好世界", "你好世界。")
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0]["start"], 3)
        self.assertEqual(diff[0]["end"], 4)
        self.assertEqual(diff[0]["wrong"], "界")
        self.assertEqual(diff[0]["candidates"], ["界。"])
        self.assertEqual(diff[0]["line"], 1)

    def test_diff_inline_replace(self):
        # "你好世界" vs "你们世界" → "好" 被替换为 "们"
        diff = diff_reocr_texts("你好世界", "你们世界")
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0]["wrong"], "好")
        self.assertEqual(diff[0]["candidates"], ["们"])
        self.assertEqual(diff[0]["start"], 1)
        self.assertEqual(diff[0]["end"], 2)
        for d in diff:
            self.assertIsInstance(d["candidates"], list, "candidates 必须是列表")
            for c in d["candidates"]:
                self.assertIsInstance(c, str, "candidates 元素必须是字符串，严禁 dict")

    def test_diff_whole_line_delete(self):
        # "a\nb\nc" vs "a\nc" → b 整行删除（空白差异忽略后字符级 diff）
        diff = diff_reocr_texts("a\nb\nc", "a\nc")
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0]["start"], 2)
        self.assertEqual(diff[0]["end"], 3)
        self.assertEqual(diff[0]["wrong"], "b")
        self.assertEqual(diff[0]["candidates"], [])
        self.assertEqual(diff[0]["line"], 2)

    def test_diff_whole_line_insert(self):
        # "a\nc" vs "a\nb\nc" → b 行插入（原文本少字）→ 锚定前邻字符"a"
        diff = diff_reocr_texts("a\nc", "a\nb\nc")
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0]["start"], 0)
        self.assertEqual(diff[0]["end"], 1)
        self.assertEqual(diff[0]["wrong"], "a")
        self.assertEqual(diff[0]["candidates"], ["ab"])
        self.assertEqual(diff[0]["line"], 1)

    def test_diff_multi_line_replace_uneven(self):
        # "第一行\n第二行" vs "合并行" → 去空白后"第一行第二行" vs "合并行"
        # SequenceMatcher 找到"行"为公共字符 → replace "第一"→"合并" + delete "第二行"
        diff = diff_reocr_texts("第一行\n第二行", "合并行")
        self.assertEqual(len(diff), 2)
        self.assertEqual(diff[0]["start"], 0)
        self.assertEqual(diff[0]["end"], 2)
        self.assertEqual(diff[0]["wrong"], "第一")
        self.assertEqual(diff[0]["candidates"], ["合并"])
        self.assertEqual(diff[0]["line"], 1)
        self.assertEqual(diff[1]["start"], 4)
        self.assertEqual(diff[1]["end"], 7)
        self.assertEqual(diff[1]["wrong"], "第二行")
        self.assertEqual(diff[1]["candidates"], [])
        self.assertEqual(diff[1]["line"], 2)

    def test_diff_line_split_ignored(self):
        # 核心需求：段落/换行分割差异不产生标注
        diff = diff_reocr_texts("第一行\n第二行", "第一行第二行")
        self.assertEqual(diff, [], "换行分割差异应被忽略")

    def test_diff_line_split_middle_ignored(self):
        diff = diff_reocr_texts("第一行\n第二行\n第三行", "第一行第二行第三行")
        self.assertEqual(diff, [], "多处换行分割差异应被忽略")

    def test_diff_extra_char_current(self):
        # 原文本增字："啊" 在 new_text 中没有 → 纯划线无候选
        diff = diff_reocr_texts("你好世界啊", "你好世界")
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0]["start"], 4)
        self.assertEqual(diff[0]["end"], 5)
        self.assertEqual(diff[0]["wrong"], "啊")
        self.assertEqual(diff[0]["candidates"], [])
        self.assertEqual(diff[0]["line"], 1)

    def test_diff_missing_char_current(self):
        # 原文本少字："新" 在 new_text 中有但 current 没有 → 锚定前邻字符"好"
        diff = diff_reocr_texts("你好世界", "你好新世界")
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0]["start"], 1)
        self.assertEqual(diff[0]["end"], 2)
        self.assertEqual(diff[0]["wrong"], "好")
        self.assertEqual(diff[0]["candidates"], ["好新"])
        self.assertEqual(diff[0]["line"], 1)

    def test_diff_insert_at_start(self):
        # 文首插入：锚定后邻字符"b"
        diff = diff_reocr_texts("bc", "abc")
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0]["start"], 0)
        self.assertEqual(diff[0]["end"], 1)
        self.assertEqual(diff[0]["wrong"], "b")
        self.assertEqual(diff[0]["candidates"], ["ab"])
        self.assertEqual(diff[0]["line"], 1)

    def test_diff_space_only_ignored(self):
        diff = diff_reocr_texts("你 好", "你好")
        self.assertEqual(diff, [], "纯空格差异应被忽略")

    def test_diff_ws_around_change_ignored(self):
        diff = diff_reocr_texts("第 一 行", "第一行")
        self.assertEqual(diff, [], "文字间空格差异应被忽略")

    def test_diff_empty_current(self):
        # 空 current 全 insert → []
        diff = diff_reocr_texts("", "任意文本")
        self.assertEqual(diff, [])

    def test_diff_candidates_are_strings(self):
        # 确保 candidates 元素是字符串（前端 join('/') 渲染，dict 会渲染成 [object Object]）
        diff = diff_reocr_texts("abc", "axc")
        for d in diff:
            self.assertIsInstance(d["candidates"], list)
            for c in d["candidates"]:
                self.assertIsInstance(c, str, "candidates 元素必须是字符串")

    # ---- /api/reocr 端点测试 ----

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

    def _patch_cfg(self, **overrides):
        import configmanage

        cfg = {
            "model_choices": {"HY": {"name": "HY.gguf"}, "QWEN.8": {"name": "qwen3.5.gguf"}},
            "selected_model": "HY",
            "proofread": {},
        }
        cfg.update(overrides)
        self._orig_get = configmanage.get_config
        configmanage.get_config = lambda *a, **k: cfg
        self.addCleanup(lambda: setattr(configmanage, "get_config", self._orig_get))
        # 2026-08-09：/api/reocr 新增探测前置（probe-before-post），默认 mock 为模型匹配，
        # 避免测试打到真实 8080 服务（新测试可再次 patch 覆盖为 none/mismatch）。
        self._patch_llama_attr("_probe_server", lambda name: "match")
        return cfg

    def _patch_llama_attr(self, name, value):
        import llamamanage

        orig = getattr(llamamanage, name)
        setattr(llamamanage, name, value)
        self.addCleanup(lambda: setattr(llamamanage, name, orig))

    def _patch_correct_attr(self, name, value):
        import correctmanage

        orig = getattr(correctmanage, name)
        setattr(correctmanage, name, value)
        self.addCleanup(lambda: setattr(correctmanage, name, orig))

    def test_reocr_ok(self):
        import json as _json

        import requests

        self._patch_cfg()
        self._patch_correct_attr("_full_bytes", lambda state, pn: ("image/png", b"fake"))
        self._patch_llama_attr(
            "_request_image_new",
            lambda prompt, img, model_key="HY", thinking=False, timeout=600, **kw: {
                "result": "当前文\n本内容",
                "error": None,
            },
        )
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/reocr",
                data=_json.dumps({"page": 1, "model": "", "html": "<p>当前文本</p>"}),
            ).json()
            self.assertTrue(res["ok"])
            self.assertIn("diff", res)
            self.assertIsInstance(res["diff"], list)
        finally:
            self._stop(server)

    def test_reocr_invalid_model_400(self):
        import json as _json

        import requests

        self._patch_cfg()
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/reocr",
                data=_json.dumps({"page": 1, "model": "NOPE", "html": "<p>test</p>"}),
            )
            self.assertEqual(res.status_code, 400)
            body = res.json()
            self.assertFalse(body["ok"])
            self.assertIn("未在配置中注册", body["error"])
        finally:
            self._stop(server)

    def test_reocr_no_image_404(self):
        import json as _json

        import requests

        self._patch_cfg()
        self._patch_correct_attr("_full_bytes", lambda state, pn: None)
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/reocr",
                data=_json.dumps({"page": 1, "model": "", "html": "<p>test</p>"}),
            )
            self.assertEqual(res.status_code, 404)
            body = res.json()
            self.assertFalse(body["ok"])
            self.assertIn("图像不可用", body["error"])
        finally:
            self._stop(server)

    def test_reocr_llm_error_friendly(self):
        import json as _json

        import requests

        self._patch_cfg()
        self._patch_correct_attr("_full_bytes", lambda state, pn: ("image/png", b"fake"))
        self._patch_llama_attr(
            "_request_image_new",
            lambda prompt, img, model_key="HY", thinking=False, timeout=600, **kw: {
                "result": None,
                "error": "Max retries exceeded with url: http://127.0.0.1:8080/v1/chat/completions",
            },
        )
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/reocr",
                data=_json.dumps({"page": 1, "model": "", "html": "<p>test</p>"}),
            ).json()
            self.assertFalse(res["ok"])
            self.assertIn("llama-server", res["error"], "错误信息应提示 llama-server 未运行")
            self.assertNotIn("Max retries", res["error"], "不应透出原始英文异常")
        finally:
            self._stop(server)

    def test_reocr_invalid_page_400(self):
        import json as _json

        import requests

        self._patch_cfg()
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/reocr",
                data=_json.dumps({"page": "abc", "model": "", "html": "<p>test</p>"}),
            )
            self.assertEqual(res.status_code, 400)
            body = res.json()
            self.assertFalse(body["ok"])
            self.assertIn("page 参数无效", body["error"])
        finally:
            self._stop(server)

    def test_reocr_traditional_to_simplified(self):
        # 模型返回繁体文本，当前文本为简体 → diff candidates 应为简体（2026-08-08）
        import json as _json

        import requests

        self._patch_cfg()
        self._patch_correct_attr("_full_bytes", lambda state, pn: ("image/png", b"fake"))
        self._patch_llama_attr(
            "_request_image_new",
            lambda prompt, img, model_key="HY", thinking=False, timeout=600, **kw: {
                "result": "裏面發生",  # 繁体：裏面→里面，發生→发生
                "error": None,
            },
        )
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/reocr",
                data=_json.dumps({"page": 1, "model": "", "html": "<p>里面发生</p>"}),
            ).json()
            self.assertTrue(res["ok"])
            # 繁转简后 new_text 与 current_text 应一致 → diff 为空
            self.assertEqual(res["diff"], [], "繁转简后文本一致应无 diff")
            # 响应 text 字段也应为简体
            self.assertEqual(res["text"], "里面发生")
        finally:
            self._stop(server)

    def test_reocr_half_punct_normalized(self):
        # 模型返回半角标点，当前文本为全角 → 标点归一后 diff 为空（2026-08-09）
        import json as _json

        import requests

        self._patch_cfg()
        self._patch_correct_attr("_full_bytes", lambda state, pn: ("image/png", b"fake"))
        self._patch_llama_attr(
            "_request_image_new",
            lambda prompt, img, model_key="HY", thinking=False, timeout=600, **kw: {
                "result": '他说:"你好,世界!"',
                "error": None,
            },
        )
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/reocr",
                data=_json.dumps(
                    {"page": 1, "model": "", "html": "<p>他说：“你好，世界！”</p>"}
                ),
            ).json()
            self.assertTrue(res["ok"])
            self.assertEqual(res["diff"], [], "标点归一后文本一致应无 diff")
            self.assertEqual(res["text"], "他说：“你好，世界！”")
        finally:
            self._stop(server)

    def test_reocr_bbox_tokens_cleaned(self):
        # ULQ4/ULQ8 等 PaddleOCR 系模型输出带 bbox 坐标前缀与 page_number 行（2026-08-09）
        import json as _json

        import requests

        self._patch_cfg()
        self._patch_correct_attr("_full_bytes", lambda state, pn: ("image/png", b"fake"))
        self._patch_llama_attr(
            "_request_image_new",
            lambda prompt, img, model_key="HY", thinking=False, timeout=600, **kw: {
                "result": (
                    "text [21, 152, 327, 170]当前\n"
                    "text [21, 152, 327, 170]文本\n"
                    "page_number [78, 904, 94, 918]2"
                ),
                "error": None,
            },
        )
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/reocr",
                data=_json.dumps({"page": 1, "model": "", "html": "<p>当前文本</p>"}),
            ).json()
            self.assertTrue(res["ok"])
            # bbox 前缀与 page_number 行被剥离 → 文本与当前一致 → 无 diff
            self.assertEqual(res["text"], "当前\n文本")
            self.assertEqual(res["diff"], [], "bbox 格式 token 不应成为纠错项")
        finally:
            self._stop(server)

    def test_reocr_probe_none_no_process(self):
        # 探测无服务且无本进程启动的服务 → 提示先启动服务（2026-08-09）
        import json as _json

        import requests

        self._patch_cfg()
        self._patch_llama_attr("_probe_server", lambda name: "none")
        self._patch_llama_attr("_server_process", None)
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/reocr",
                data=_json.dumps({"page": 1, "model": "", "html": "<p>test</p>"}),
            ).json()
            self.assertFalse(res["ok"])
            self.assertIn("未检测到运行中的模型服务", res["error"])
        finally:
            self._stop(server)

    def test_reocr_probe_none_loading(self):
        # 探测无响应但本进程启动的服务仍在加载 → 提示稍候重试
        import json as _json

        import requests

        class _FakeProc:
            def poll(self):
                return None

        self._patch_cfg()
        self._patch_llama_attr("_probe_server", lambda name: "none")
        self._patch_llama_attr("_server_process", _FakeProc())
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/reocr",
                data=_json.dumps({"page": 1, "model": "", "html": "<p>test</p>"}),
            ).json()
            self.assertFalse(res["ok"])
            self.assertIn("正在加载中", res["error"])
        finally:
            self._stop(server)

    def test_reocr_probe_mismatch(self):
        # 端口被占用但模型不符 → 提示先停止再启动所选模型
        import json as _json

        import requests

        self._patch_cfg()
        self._patch_llama_attr("_probe_server", lambda name: "mismatch")
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/reocr",
                data=_json.dumps({"page": 1, "model": "", "html": "<p>test</p>"}),
            ).json()
            self.assertFalse(res["ok"])
            self.assertIn("与所选模型 HY 不符", res["error"])
        finally:
            self._stop(server)


class TestFullPunct(unittest.TestCase):
    """_full_punct：英文标点 → 中文标点（含引号配对轮换）。"""

    def test_basic_half_to_full(self):
        self.assertEqual(
            _full_punct("你好,世界.好吗?好!比如:分号;(括号)[方括号]"),
            "你好，世界。好吗？好！比如：分号；（括号）【方括号】",
        )

    def test_paired_double_quotes(self):
        self.assertEqual(_full_punct('他说"你好"了'), "他说“你好”了")

    def test_paired_single_quotes(self):
        self.assertEqual(_full_punct("他说'你好'了"), "他说‘你好’了")

    def test_multiple_quote_pairs_rotate(self):
        self.assertEqual(
            _full_punct('第一"甲"第二"乙"'), "第一“甲”第二“乙”"
        )

    def test_pure_ascii_unchanged(self):
        s = 'Hello, world. Is it "ok"?'
        self.assertEqual(_full_punct(s), s, "无 CJK 上下文应原样返回")

    def test_empty_unchanged(self):
        self.assertEqual(_full_punct(""), "")

    def test_full_width_kept(self):
        s = "已经是全角，无需变化。"
        self.assertEqual(_full_punct(s), s)


class TestProofreadPlainText(unittest.TestCase):
    """_proofread_plain_text：标记 span 整 span 剥离（含 label 内容一并删）。

    实际 DOM 中标记 span 格式为 <span data-ptoe-marker="type" class="ptoe-marker">label</span>
    （data-ptoe-marker 在前，class 在后；或仅有 data-ptoe-marker）。
    """

    def test_marker_span_stripped_with_label(self):
        # 段落标记：整 span 含 label 内容一并删除
        html = '<p>正文<span data-ptoe-marker="段落" class="ptoe-marker">【段落】</span>继续</p>'
        result = _proofread_plain_text(html)
        self.assertIn("正文", result)
        self.assertIn("继续", result)
        self.assertNotIn("【段落】", result)
        self.assertNotIn("ptoe-marker", result)

    def test_full_marker_stripped(self):
        html = '<p>前文</p><span data-ptoe-marker="全文" class="ptoe-marker">【全文】</span><p>后文</p>'
        result = _proofread_plain_text(html)
        self.assertIn("前文", result)
        self.assertIn("后文", result)
        self.assertNotIn("【全文】", result)

    def test_page_marker_stripped(self):
        html = '<p>上页</p><span data-ptoe-marker="page" class="ptoe-marker">【换页】</span><p>下页</p>'
        result = _proofread_plain_text(html)
        self.assertIn("上页", result)
        self.assertIn("下页", result)
        self.assertNotIn("【换页】", result)

    def test_note_marker_stripped(self):
        html = '<p>正文<span data-ptoe-marker="note" class="ptoe-marker">【注】</span>后续</p>'
        result = _proofread_plain_text(html)
        self.assertIn("正文", result)
        self.assertIn("后续", result)
        self.assertNotIn("【注】", result)

    def test_marker_without_class(self):
        # 旧历史版本可能仅有 data-ptoe-marker 无 class
        html = '<p>前<span data-ptoe-marker="join">段落</span>后</p>'
        result = _proofread_plain_text(html)
        self.assertIn("前", result)
        self.assertIn("后", result)
        self.assertNotIn("段落", result)

    def test_plain_text_unchanged(self):
        # 无标记时行为不变
        html = '<p>你好</p><p>世界</p>'
        result = _proofread_plain_text(html)
        self.assertEqual(result, "你好世界")

    def test_marker_with_nested_content(self):
        # 标记 span 内含嵌套标签（如 <em>）也一并剥离
        html = '<p>A<span data-ptoe-marker="段落" class="ptoe-marker"><em>label</em></span>B</p>'
        result = _proofread_plain_text(html)
        self.assertIn("A", result)
        self.assertIn("B", result)
        self.assertNotIn("label", result)


class TestParseLlmSuggestions(unittest.TestCase):
    """_parse_llm_suggestions 响应中间层：模型返回的 JSON 建议解析与归一校验。

    曾因贪婪正则 `{.*}` 吞入 JSON 后尾随内容 → json.loads 抛 "Extra data"
    （用户实测「深度校对失败： Extra data: line 1 column 141」）。本组测试覆盖
    多对象/围栏/前后说明/截断/位置漂移/候选归一/去重/中文错误文案。
    """

    TEXT = "这是一个测试文本，其中有个错字。"

    def _sug(self, start, end, wrong, cands=None):
        return {"start": start, "end": end, "wrong": wrong, "candidates": cands or [{"text": "错词", "score": 0.9}]}

    def test_normal_object(self):
        raw = '{"suggestions": [{"start": 13, "end": 15, "wrong": "错字", "candidates": [{"text": "错词", "score": 0.95}]}]}'
        sugs, err = _parse_llm_suggestions(raw, self.TEXT)
        self.assertIsNone(err)
        self.assertEqual(len(sugs), 1)
        self.assertEqual(sugs[0]["start"], 13)
        self.assertEqual(sugs[0]["end"], 15)
        self.assertEqual(sugs[0]["wrong"], "错字")
        self.assertEqual(sugs[0]["candidates"], [{"text": "错词", "score": 0.95}])

    def test_extra_data_after_json(self):
        # 用户报告的场景：JSON 对象后跟额外说明文字 → 旧实现抛 Extra data
        raw = (
            '{"suggestions": [{"start": 13, "end": 15, "wrong": "错字", '
            '"candidates": [{"text": "错词", "score": 0.9}]}]} 这是我补充的说明，请忽略。'
        )
        sugs, err = _parse_llm_suggestions(raw, self.TEXT)
        self.assertIsNone(err, "尾随文字不应导致解析失败: " + str(err))
        self.assertEqual(len(sugs), 1)

    def test_multiple_objects_first_wins(self):
        raw = (
            '{"suggestions": [{"start": 13, "end": 15, "wrong": "错字", '
            '"candidates": [{"text": "错词", "score": 0.9}]}]} '
            '{"suggestions": [{"start": 0, "end": 2, "wrong": "这是"}]}'
        )
        sugs, err = _parse_llm_suggestions(raw, self.TEXT)
        self.assertIsNone(err)
        self.assertEqual(len(sugs), 1)
        self.assertEqual(sugs[0]["wrong"], "错字", "应取第一个完整 JSON 对象")

    def test_markdown_fence(self):
        raw = '```json\n{"suggestions": [{"start": 13, "end": 15, "wrong": "错字", "candidates": [{"text": "错词"}]}]}\n```'
        sugs, err = _parse_llm_suggestions(raw, self.TEXT)
        self.assertIsNone(err)
        self.assertEqual(len(sugs), 1)
        self.assertEqual(sugs[0]["candidates"][0]["score"], 0.9, "score 缺省 0.9")

    def test_prefix_suffix_text(self):
        raw = '以下是校对建议：{"suggestions": [{"start": 13, "end": 15, "wrong": "错字", "candidates": [{"text": "错词", "score": 0.8}]}]} 请查收。'
        sugs, err = _parse_llm_suggestions(raw, self.TEXT)
        self.assertIsNone(err)
        self.assertEqual(len(sugs), 1)
        self.assertEqual(sugs[0]["candidates"][0]["score"], 0.8)

    def test_trailing_comma_tolerated(self):
        raw = '{"suggestions": [{"start": 13, "end": 15, "wrong": "错字", "candidates": [{"text": "错词"}]}],}'
        sugs, err = _parse_llm_suggestions(raw, self.TEXT)
        self.assertIsNone(err)
        self.assertEqual(len(sugs), 1)

    def test_truncated_json_chinese_error(self):
        raw = '{"suggestions": [{"start": 13, "end": 15, "wrong": "错字"'
        sugs, err = _parse_llm_suggestions(raw, self.TEXT)
        self.assertEqual(sugs, [])
        self.assertIsNotNone(err)
        self.assertIn("模型响应解析失败", err or "")

    def test_position_drift_repositioned(self):
        # start/end 与 wrong 不符（模型位置漂移）→ 按 find 重定位
        raw = '{"suggestions": [{"start": 0, "end": 2, "wrong": "错字", "candidates": [{"text": "错词"}]}]}'
        sugs, err = _parse_llm_suggestions(raw, self.TEXT)
        self.assertIsNone(err)
        self.assertEqual(len(sugs), 1)
        self.assertEqual((sugs[0]["start"], sugs[0]["end"]), (13, 15))

    def test_wrong_not_found_dropped(self):
        raw = '{"suggestions": [{"start": 0, "end": 2, "wrong": "不存在词", "candidates": [{"text": "x"}]}]}'
        sugs, err = _parse_llm_suggestions(raw, self.TEXT)
        self.assertIsNone(err)
        self.assertEqual(sugs, [])

    def test_candidates_normalized(self):
        raw = (
            '{"suggestions": [{"start": 13, "end": 15, "wrong": "错字", '
            '"candidates": [{"text": "错字", "score": 0.99}, {"text": "错词"}, "bad", {"text": "  优选 ", "score": 0.7}]}]}'
        )
        sugs, err = _parse_llm_suggestions(raw, self.TEXT)
        self.assertIsNone(err)
        cands = sugs[0]["candidates"]
        self.assertEqual(
            cands,
            [{"text": "错词", "score": 0.9}, {"text": "优选", "score": 0.7}],
            "候选=wrong 的丢弃、score 缺省 0.9、去空白、非 dict 跳过",
        )

    def test_duplicate_positions_merged(self):
        raw = (
            '{"suggestions": ['
            '{"start": 13, "end": 15, "wrong": "错字", "candidates": [{"text": "错词", "score": 0.9}]},'
            '{"start": 13, "end": 15, "wrong": "错字", "candidates": [{"text": "错别字", "score": 0.7}]}'
            "]}"
        )
        sugs, err = _parse_llm_suggestions(raw, self.TEXT)
        self.assertIsNone(err)
        self.assertEqual(len(sugs), 1, "同位置重复项应合并")
        self.assertEqual([c["text"] for c in sugs[0]["candidates"]], ["错词", "错别字"])

    def test_empty_candidates_kept_as_marker(self):
        raw = '{"suggestions": [{"start": 13, "end": 15, "wrong": "错字", "candidates": []}]}'
        sugs, err = _parse_llm_suggestions(raw, self.TEXT)
        self.assertIsNone(err)
        self.assertEqual(len(sugs), 1)
        self.assertEqual(sugs[0]["candidates"], [], "无候选条目保留（仅标注，前端只显示删除线）")

    def test_not_json_chinese_error(self):
        sugs, err = _parse_llm_suggestions("很抱歉，我无法处理这个请求。", self.TEXT)
        self.assertEqual(sugs, [])
        self.assertEqual(err, "模型响应解析失败：无法提取有效 JSON")

    def test_empty_response(self):
        sugs, err = _parse_llm_suggestions("", self.TEXT)
        self.assertEqual(sugs, [])
        self.assertEqual(err, "模型响应为空")

    def test_top_level_not_object(self):
        sugs, err = _parse_llm_suggestions("[1, 2, 3]", self.TEXT)
        self.assertEqual(sugs, [])
        self.assertEqual(err, "模型响应解析失败：JSON 顶层不是对象")

    def test_suggestions_missing(self):
        sugs, err = _parse_llm_suggestions('{"foo": 1}', self.TEXT)
        self.assertEqual(sugs, [])
        self.assertEqual(err, "模型响应解析失败：缺少 suggestions 字段")

    # --- 繁体→简体转换测试（convert_t2s，2026-08-08）---

    def test_t2s_traditional_wrong_hits_simplified_text(self):
        # 模型返回繁体「裡」，原文为简体「里」→ convert_t2s=True 应命中
        text = "我去里面。"  # 里 at index 2 (我0 去1 里2 面3 。4)
        raw = '{"suggestions": [{"start": 2, "end": 3, "wrong": "裡", "candidates": [{"text": "入", "score": 0.9}]}]}'
        sugs, err = _parse_llm_suggestions(raw, text, convert_t2s=True)
        self.assertIsNone(err)
        self.assertEqual(len(sugs), 1)
        self.assertEqual(sugs[0]["wrong"], "里")
        self.assertEqual((sugs[0]["start"], sugs[0]["end"]), (2, 3))

    def test_t2s_default_no_conversion_keeps_behavior(self):
        # convert_t2s=False（默认）时繁体 wrong 在简体原文 find 不到 → 丢弃
        text = "这里面有一个错字。"
        raw = '{"suggestions": [{"start": 2, "end": 3, "wrong": "裡", "candidates": [{"text": "里", "score": 0.9}]}]}'
        sugs, err = _parse_llm_suggestions(raw, text)
        self.assertIsNone(err)
        self.assertEqual(sugs, [], "默认不转换时繁体 wrong 应找不到而被丢弃")

    def test_t2s_candidates_traditional_converted(self):
        # candidates text 繁体 → convert_t2s=True 输出简体 candidates
        text = "这里面有一个错字。"
        raw = '{"suggestions": [{"start": 2, "end": 3, "wrong": "裡", "candidates": [{"text": "裏", "score": 0.9}]}]}'
        sugs, err = _parse_llm_suggestions(raw, text, convert_t2s=True)
        self.assertIsNone(err)
        self.assertEqual(len(sugs), 1)
        # wrong 转「里」，candidate 转「里」→ 相等被去重 → candidates 为空
        self.assertEqual(sugs[0]["candidates"], [])

    def test_t2s_candidates_different_simplified(self):
        # candidates 繁体转简体后与 wrong 不同 → 保留
        text = "我去里面。"  # 里 at index 2 (我0 去1 里2 面3 。4)
        raw = '{"suggestions": [{"start": 2, "end": 3, "wrong": "裡", "candidates": [{"text": "入", "score": 0.95}]}]}'
        sugs, err = _parse_llm_suggestions(raw, text, convert_t2s=True)
        self.assertIsNone(err)
        self.assertEqual(len(sugs), 1)
        self.assertEqual(sugs[0]["wrong"], "里")
        self.assertEqual(sugs[0]["candidates"], [{"text": "入", "score": 0.95}])

    def test_t2s_idempotent_for_simplified_input(self):
        # 纯简体输入 → 转换前后结果一致（幂等）
        raw = '{"suggestions": [{"start": 13, "end": 15, "wrong": "错字", "candidates": [{"text": "错词", "score": 0.9}]}]}'
        sugs_default, err_default = _parse_llm_suggestions(raw, self.TEXT)
        sugs_t2s, err_t2s = _parse_llm_suggestions(raw, self.TEXT, convert_t2s=True)
        self.assertIsNone(err_default)
        self.assertIsNone(err_t2s)
        self.assertEqual(sugs_default, sugs_t2s, "纯简体输入转换前后应一致")

    def test_t2s_wrong_becomes_empty_dropped(self):
        # 极端：ttos 转换后 wrong 变空（理论罕见）→ 沿用空 wrong 丢弃逻辑
        # 使用一个只含空白字符的 wrong（strip 后为空）
        text = "这是一个测试文本。"
        raw = '{"suggestions": [{"start": 0, "end": 2, "wrong": "  ", "candidates": [{"text": "x"}]}]}'
        sugs, err = _parse_llm_suggestions(raw, text, convert_t2s=True)
        self.assertIsNone(err)
        self.assertEqual(sugs, [])


class TestProofreadHistory(unittest.TestCase):
    """文字纠错状态随历史缓存保存/恢复。"""

    def _state(self, on_convert=None):
        return {
            "pages": {},
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
            "history_prefix": "manual_test123",
            "history_name": "手动录入",
            "history_lock": __import__("threading").Lock(),
            "proofread": {"errors": {}, "original": {}, "dismissed": {}},
            "last_proofread_page": None,
        }

    def _serve(self, state):
        import json as _json
        from http.server import ThreadingHTTPServer
        from threading import Thread

        import correctmanage as _cm
        from correctmanage import _CorrectionHandler

        hist_dir = Path(tempfile.mkdtemp(prefix="test_proofread_hist_"))
        _orig = _cm._history_dir
        _cm._history_dir = lambda: hist_dir
        self.addCleanup(lambda: setattr(_cm, "_history_dir", _orig))
        self.addCleanup(lambda: shutil.rmtree(hist_dir, ignore_errors=True))
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CorrectionHandler)
        server.daemon_threads = True
        server.state = state
        Thread(target=server.serve_forever, daemon=True).start()

        def _cleanup():
            server.shutdown()
            server.server_close()

        self.addCleanup(_cleanup)
        return f"http://127.0.0.1:{server.server_address[1]}", hist_dir

    def test_save_persists_proofread_and_last_page(self):
        import json as _json
        import requests

        state = self._state()
        base, hist_dir = self._serve(state)
        proofread_data = {
            "errors": {"0": [{"start": 0, "end": 2, "wrong": "你好", "candidates": ["您好"]}]},
            "original": {"0": "<div>原始</div>"},
            "dismissed": {"0": ["0:你好"]},
        }
        body = _json.dumps({
            "pages": [{"page": 1, "html": "<p>甲</p>"}],
            "proofread": proofread_data,
            "last_proofread_page": 5,
        })
        r = requests.post(base + "/api/save", data=body).json()
        self.assertTrue(r["ok"])
        self.assertEqual(state["last_proofread_page"], 5)
        self.assertIn("0", state["proofread"]["errors"])
        files = sorted(hist_dir.glob("manual_test123_*.json"))
        self.assertTrue(files)
        data = _json.loads(files[-1].read_text(encoding="utf-8"))
        self.assertEqual(data["last_proofread_page"], 5)
        self.assertIn("0", data["proofread"]["errors"])
        self.assertIn("0", data["proofread"]["dismissed"])

    def test_stage_persists_proofread(self):
        import json as _json
        import requests

        state = self._state()
        base, hist_dir = self._serve(state)
        body = _json.dumps({
            "pages": [{"page": 1, "html": "<p>x</p>"}],
            "proofread": {"errors": {"0": []}, "original": {}, "dismissed": {}},
            "last_proofread_page": 3,
        })
        r = requests.post(base + "/api/stage", data=body).json()
        self.assertTrue(r["ok"])
        self.assertEqual(state["last_proofread_page"], 3)
        files = sorted(hist_dir.glob("manual_test123_*.json"))
        self.assertTrue(files)
        data = _json.loads(files[-1].read_text(encoding="utf-8"))
        self.assertEqual(data["last_proofread_page"], 3)

    def test_history_list_includes_last_proofread_page(self):
        import json as _json
        import requests

        state = self._state()
        base, hist_dir = self._serve(state)
        vid = "manual_test123_20260101000000_0001"
        (hist_dir / f"{vid}.json").write_text(
            _json.dumps({
                "pdf": "C:/books/A.pdf",
                "name": "A书",
                "updated": "2026-01-01 00:00:00",
                "pages": {"1": "<p>x</p>"},
                "proofread": {"errors": {}, "original": {}, "dismissed": {}},
                "last_proofread_page": 7,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        # 清除缓存，强制重新读取
        import correctmanage as _cm
        _cm._HISTORY_INDEX = {"sig": None, "items": None}
        r = requests.get(base + "/api/history").json()
        self.assertTrue(r["items"])
        self.assertEqual(r["items"][0]["last_proofread_page"], 7)

    def test_history_load_returns_proofread(self):
        import json as _json
        import requests

        state = self._state()
        base, hist_dir = self._serve(state)
        vid = "manual_test123_20260101000000_0001"
        (hist_dir / f"{vid}.json").write_text(
            _json.dumps({
                "pdf": "C:/books/A.pdf",
                "name": "A书",
                "updated": "2026-01-01 00:00:00",
                "pages": {"1": "<p>x</p>"},
                "proofread": {
                    "errors": {"0": [{"start": 0, "end": 2, "wrong": "ab", "candidates": ["cd"]}]},
                    "original": {"0": "<div>orig</div>"},
                    "dismissed": {"0": ["0:ab"]},
                },
                "last_proofread_page": 2,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        r = requests.post(base + "/api/history/load", data=_json.dumps({"id": vid})).json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["last_proofread_page"], 2)
        self.assertIn("0", r["proofread"]["errors"])
        self.assertIn("0", r["proofread"]["dismissed"])

    def test_history_load_legacy_no_proofread_key(self):
        import json as _json
        import requests

        state = self._state()
        base, hist_dir = self._serve(state)
        vid = "legacy_20260101000000_0001"
        (hist_dir / f"{vid}.json").write_text(
            _json.dumps({
                "pdf": "C:/books/B.pdf",
                "name": "B书",
                "updated": "2026-01-01 00:00:00",
                "pages": {"1": "<p>y</p>"},
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        r = requests.post(base + "/api/history/load", data=_json.dumps({"id": vid})).json()
        self.assertTrue(r["ok"])
        # 旧版文件无 proofread → 返回空 dict + None，不报错
        self.assertIsNone(r["last_proofread_page"])
        self.assertEqual(r["proofread"], {"errors": {}, "original": {}, "dismissed": {}})


class TestProofreadConsecutivePunct(unittest.TestCase):
    """proofread_page 规则7：连续标点（中英文均计）整串标注（candidates 为空）。

    默认只跑三条新规则（连续重复/连续标点/中文中的连续字母）；涉及原有规则的用例
    显式传 enable_legacy_rules=True。
    """

    def _errs(self, s, legacy=False):
        from correctmanage import proofread_page

        return proofread_page(s, enable_legacy_rules=legacy)

    def _find(self, errs, wrong):
        return [e for e in errs if e["wrong"] == wrong]

    def test_consecutive_cn_punct(self):
        errs = self._errs("他说，，这个")
        hit = self._find(errs, "，，")
        self.assertEqual(len(hit), 1, f"errs={errs}")
        self.assertEqual(hit[0]["candidates"], [], "连续标点为纯标注，无候选")
        self.assertEqual(hit[0]["start"], 2)
        self.assertEqual(hit[0]["end"], 4)

    def test_consecutive_cn_period(self):
        errs = self._errs("结束了。。下一句")
        self.assertTrue(self._find(errs, "。。"), f"errs={errs}")

    def test_consecutive_en_punct(self):
        errs = self._errs("好的!!真的")
        hit = self._find(errs, "!!")
        self.assertEqual(len(hit), 1, f"errs={errs}")
        self.assertEqual(hit[0]["candidates"], [])

    def test_consecutive_mixed_cn_en(self):
        # 中文全角 + 英文半角相邻：也算连续标点
        errs = self._errs("测试，!混合")
        hit = self._find(errs, "，!")
        self.assertEqual(len(hit), 1, f"errs={errs}")
        self.assertEqual(hit[0]["candidates"], [])

    def test_consecutive_en_question_bang(self):
        errs = self._errs("真的吗?!这样")
        self.assertTrue(self._find(errs, "?!"), f"errs={errs}")

    def test_single_punct_not_flagged(self):
        errs = self._errs("单个，正常句子。")
        self.assertFalse(
            any(len(e["wrong"]) > 1 and all(c in "，。！？；：、" for c in e["wrong"]) for e in errs),
            f"单个标点不应被连续标点规则标注 errs={errs}",
        )

    def test_normal_quote_combo_not_flagged(self):
        # 「。”」属正常排版组合（分隔类 + 引号类），不标
        errs = self._errs("他说：“好的。”然后走了")
        self.assertFalse(
            any(e["wrong"] in ("。”", "：“") for e in errs), f"errs={errs}"
        )

    def test_digit_separator_not_flagged_as_run(self):
        # 数字中的 , . 不构成连续串（中间隔着数字），不产生连续标点条目
        errs = self._errs("数字1,234.5的")
        self.assertFalse(any(len(e["wrong"]) > 1 and e["wrong"][0] in ".," for e in errs), f"errs={errs}")

    def test_consecutive_run_replaces_halffull_items(self):
        # 开启原有规则时：命中连续标点的位置不再逐字出「半角转全角」条目（整串一条优先）
        errs = self._errs("好的,,真的", legacy=True)
        self.assertTrue(self._find(errs, ",,"), f"errs={errs}")
        self.assertFalse(self._find(errs, ","), f"串内不应再出逐字条目 errs={errs}")

    def test_existing_rules_still_work(self):
        # 规则2 连续重复
        errs = self._errs("啊啊啊，这个句子有叠字")
        self.assertTrue(any(e["wrong"].startswith("啊") and e["candidates"] for e in errs), f"errs={errs}")
        # 规则6 中英混排
        errs2 = self._errs("这是英文ABC混排测试")
        self.assertTrue(any(e["wrong"] == "ABC" for e in errs2), f"errs={errs2}")
        # 叠词白名单仍生效
        errs3 = self._errs("好好学习，天天向上")
        self.assertFalse(any("好好" in e["wrong"] or "天天" in e["wrong"] for e in errs3), f"errs={errs3}")
        # 规则1 单个半角标点转全角（原有规则，需显式开启）
        errs4 = self._errs("这是中文,后面", legacy=True)
        self.assertTrue(any(e["wrong"] == "," and e["candidates"] == ["，"] for e in errs4), f"errs={errs4}")

    def test_sorted_and_non_overlapping(self):
        errs = self._errs("第一句，，第二句!!第三句ABC结尾")
        starts = [e["start"] for e in errs]
        self.assertEqual(starts, sorted(starts), f"必须按 start 排序 errs={errs}")
        for a, b in zip(errs, errs[1:]):
            self.assertGreaterEqual(b["start"], a["end"], f"不得重叠 errs={errs}")


class TestProofreadLegacyRulesToggle(unittest.TestCase):
    """proofread_page(enable_legacy_rules)：默认只跑三条新规则，开关打开才跑原有规则。

    三条新规则：② 连续重复文字 / ⑦ 连续标点（中英文）/ ⑥ 中文中的连续字母。
    原有规则：① 半角转全角 / ③ 引号配对 / ④ 混淆表 / ⑤ 词典滑窗。
    """

    def _errs(self, s, legacy=False):
        from correctmanage import proofread_page

        return proofread_page(s, enable_legacy_rules=legacy)

    # ---- 默认（False）：原有规则不出条目 ----

    def test_default_signature_is_legacy_off(self):
        import inspect

        from correctmanage import proofread_page

        sig = inspect.signature(proofread_page)
        self.assertIn("enable_legacy_rules", sig.parameters)
        self.assertIs(sig.parameters["enable_legacy_rules"].default, False)

    def test_default_no_halfwidth_conversion(self):
        # 规则① 半角标点转全角：默认不执行
        errs = self._errs("这是中文,后面还有.句号")
        self.assertEqual(errs, [], f"默认不应产生半角转全角条目 errs={errs}")

    def test_default_no_quote_pairing(self):
        # 规则③ 引号配对（奇数个）：默认不执行
        errs = self._errs("他说「引用没有闭合")
        self.assertEqual(errs, [], f"默认不应产生引号配对条目 errs={errs}")

    def test_default_no_confusables(self):
        # 规则④ 混淆表：默认不执行（构造一个开启时会命中的串）
        text = "曰月星辰己经土大夫"
        self.assertEqual(self._errs(text), [], f"默认不应产生混淆字条目 errs={self._errs(text)}")

    def test_default_no_dictionary_entries(self):
        # 规则⑤ 词典滑窗：默认不执行 —— 无重复/连续标点/字母的纯中文在开关关闭时不出条目
        # 注意：文本不得含连续重复字（如"文文"会触发规则②），故用"这是一段普通的中文内容"
        text = "这是一段普通的中文内容"
        self.assertEqual(self._errs(text), [], f"默认不应产生词典条目 errs={self._errs(text)}")

    # ---- 默认（False）：三条新规则照常生效 ----

    def test_default_repeated_text_still_flagged(self):
        errs = self._errs("啊啊啊，这个句子有叠字")
        self.assertTrue(
            any(e["wrong"].startswith("啊") and e["candidates"] == ["啊"] for e in errs),
            f"规则② 连续重复应生效 errs={errs}",
        )

    def test_default_consecutive_punct_still_flagged(self):
        errs = self._errs("他说，，这个")
        self.assertTrue(any(e["wrong"] == "，，" for e in errs), f"规则⑦ 应生效 errs={errs}")
        errs2 = self._errs("好的!!真的")
        self.assertTrue(any(e["wrong"] == "!!" for e in errs2), f"规则⑦（英文）应生效 errs={errs2}")

    def test_default_letters_in_chinese_still_flagged(self):
        errs = self._errs("这是英文ABC混排测试")
        self.assertTrue(any(e["wrong"] == "ABC" for e in errs), f"规则⑥ 应生效 errs={errs}")

    def test_default_three_rules_together(self):
        errs = self._errs("重重复复，，中间ABC结尾")
        wrongs = [e["wrong"] for e in errs]
        self.assertTrue(any("，，" == w for w in wrongs), f"errs={errs}")
        self.assertTrue(any("ABC" == w for w in wrongs), f"errs={errs}")
        # 半角/引号/混淆/词典条目一个都不应出现
        self.assertFalse(any(w in (",", ".", "「", "”") for w in wrongs), f"errs={errs}")

    # ---- True：原有规则恢复 ----

    def test_legacy_on_halfwidth_conversion(self):
        errs = self._errs("这是中文,后面", legacy=True)
        self.assertTrue(
            any(e["wrong"] == "," and e["candidates"] == ["，"] for e in errs), f"errs={errs}"
        )

    def test_legacy_on_quote_pairing(self):
        errs = self._errs("他说「引用没有闭合", legacy=True)
        self.assertTrue(any(e["wrong"] == "「" for e in errs), f"errs={errs}")

    def test_legacy_on_yields_more_or_equal_entries(self):
        text = '他说 "这是引用",里面有「未闭合'
        base = self._errs(text)
        full = self._errs(text, legacy=True)
        self.assertGreater(len(full), len(base), f"开启后条目应更多 base={base} full={full}")

    def test_legacy_on_new_rules_unaffected(self):
        # 开启原有规则不影响三条新规则
        errs = self._errs("啊啊啊，，这个ABC", legacy=True)
        wrongs = [e["wrong"] for e in errs]
        self.assertTrue(any(w.startswith("啊") for w in wrongs), f"errs={errs}")
        self.assertTrue(any(w == "，，" for w in wrongs), f"errs={errs}")
        self.assertTrue(any(w == "ABC" for w in wrongs), f"errs={errs}")

    def test_sorted_non_overlapping_both_modes(self):
        text = '第一句，，第二句"引用"第三句ABC末尾。。'
        for legacy in (False, True):
            errs = self._errs(text, legacy=legacy)
            starts = [e["start"] for e in errs]
            self.assertEqual(starts, sorted(starts), f"legacy={legacy} errs={errs}")
            for a, b in zip(errs, errs[1:]):
                self.assertGreaterEqual(b["start"], a["end"], f"legacy={legacy} errs={errs}")


class TestProofreadLegacyRulesEndpoint(unittest.TestCase):
    """/api/proofread_settings 的 enable_legacy_rules 往返 + /api/proofread 是否按开关执行。"""

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

    def _patch_cfg(self, legacy=None):
        import configmanage

        pr = {"enable_llm": False, "llm_model": ""}
        if legacy is not None:
            pr["enable_legacy_rules"] = legacy
        cfg = {"model_choices": {"HY": {}}, "selected_model": "HY", "proofread": pr}
        orig = configmanage.get_config
        configmanage.get_config = lambda *a, **k: cfg
        self.addCleanup(lambda: setattr(configmanage, "get_config", orig))
        return cfg

    def _patch_setter(self):
        import configmanage

        calls = []
        orig = configmanage.set_proofread_param
        configmanage.set_proofread_param = lambda name, value: calls.append((name, value))
        self.addCleanup(lambda: setattr(configmanage, "set_proofread_param", orig))
        return calls

    def test_get_includes_flag_default_false(self):
        import requests

        self._patch_cfg(legacy=None)  # 配置无该键
        server, base = self._start()
        try:
            res = requests.get(base + "/api/proofread_settings").json()
            self.assertTrue(res["ok"])
            self.assertIn("enable_legacy_rules", res)
            self.assertFalse(res["enable_legacy_rules"])
        finally:
            self._stop(server)

    def test_get_reflects_true(self):
        import requests

        self._patch_cfg(legacy=True)
        server, base = self._start()
        try:
            res = requests.get(base + "/api/proofread_settings").json()
            self.assertTrue(res["enable_legacy_rules"])
        finally:
            self._stop(server)

    def test_post_persists_flag(self):
        import json as _json

        import requests

        self._patch_cfg(legacy=False)
        calls = self._patch_setter()
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/proofread_settings",
                data=_json.dumps({"enabled": False, "model": "", "enable_legacy_rules": True}),
            ).json()
            self.assertTrue(res["ok"])
            self.assertIn(("enable_legacy_rules", True), calls)
        finally:
            self._stop(server)

    def test_post_non_bool_400(self):
        import json as _json

        import requests

        self._patch_cfg(legacy=False)
        calls = self._patch_setter()
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/proofread_settings",
                data=_json.dumps({"enabled": False, "model": "", "enable_legacy_rules": "yes"}),
            ).json()
            self.assertFalse(res["ok"])
            self.assertIn("布尔值", res["error"])
            self.assertEqual(calls, [], "非法值不应落盘任何键")
        finally:
            self._stop(server)

    def test_post_omitted_flag_untouched(self):
        import json as _json

        import requests

        self._patch_cfg(legacy=True)
        calls = self._patch_setter()
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/proofread_settings",
                data=_json.dumps({"enabled": False, "model": ""}),
            ).json()
            self.assertTrue(res["ok"])
            self.assertFalse(
                any(name == "enable_legacy_rules" for name, _ in calls),
                "载荷未给该键时不应改动",
            )
        finally:
            self._stop(server)

    def test_api_proofread_respects_flag_off(self):
        import json as _json

        import requests

        self._patch_cfg(legacy=False)
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/proofread", data=_json.dumps({"html": "<p>这是中文,后面</p>"})
            ).json()
            self.assertTrue(res["ok"])
            self.assertEqual(res["errors"], [], f"关闭时半角标点不应被标注 res={res}")
        finally:
            self._stop(server)

    def test_api_proofread_respects_flag_on(self):
        import json as _json

        import requests

        self._patch_cfg(legacy=True)
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/proofread", data=_json.dumps({"html": "<p>这是中文,后面</p>"})
            ).json()
            self.assertTrue(res["ok"])
            self.assertTrue(
                any(e["wrong"] == "," for e in res["errors"]), f"开启时应标注半角逗号 res={res}"
            )
        finally:
            self._stop(server)

    def test_api_proofread_new_rules_always_on(self):
        import json as _json

        import requests

        self._patch_cfg(legacy=False)
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/proofread", data=_json.dumps({"html": "<p>他说，，这个ABC</p>"})
            ).json()
            wrongs = [e["wrong"] for e in res["errors"]]
            self.assertIn("，，", wrongs, f"res={res}")
            self.assertIn("ABC", wrongs, f"res={res}")
        finally:
            self._stop(server)


class TestShortcutsEndpoint(unittest.TestCase):
    """/api/shortcuts：快捷键绑定服务端持久化（config.json 顶层 shortcuts）。

    随机端口下 localStorage 每运行失效（与 LLM 深度校对设置同因）。
    """

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

    def _patch_cfg(self, shortcuts=None):
        import configmanage

        cfg = {"model_choices": {}, "selected_model": None}
        if shortcuts is not None:
            cfg["shortcuts"] = shortcuts
        orig = configmanage.get_config
        configmanage.get_config = lambda *a, **k: cfg
        self.addCleanup(lambda: setattr(configmanage, "get_config", orig))
        return cfg

    def _patch_setter(self):
        import configmanage

        calls = []
        orig = configmanage.set_shortcuts
        configmanage.set_shortcuts = lambda sc: calls.append(sc)
        self.addCleanup(lambda: setattr(configmanage, "set_shortcuts", orig))
        return calls

    def test_get_returns_shortcuts(self):
        import requests

        self._patch_cfg({"bold": "Ctrl+B", "save": "Ctrl+S"})
        server, base = self._start()
        try:
            res = requests.get(base + "/api/shortcuts").json()
            self.assertTrue(res["ok"])
            self.assertEqual(res["shortcuts"], {"bold": "Ctrl+B", "save": "Ctrl+S"})
        finally:
            self._stop(server)

    def test_get_missing_key_returns_empty(self):
        import requests

        self._patch_cfg(None)  # 配置无 shortcuts 键
        server, base = self._start()
        try:
            res = requests.get(base + "/api/shortcuts").json()
            self.assertTrue(res["ok"])
            self.assertEqual(res["shortcuts"], {})
        finally:
            self._stop(server)

    def test_post_persists_via_set_shortcuts(self):
        import json as _json

        import requests

        self._patch_cfg({})
        calls = self._patch_setter()
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/shortcuts",
                data=_json.dumps({"shortcuts": {"bold": "Ctrl+B"}}),
            ).json()
            self.assertTrue(res["ok"])
            self.assertEqual(calls, [{"bold": "Ctrl+B"}])
        finally:
            self._stop(server)

    def test_post_invalid_type_400(self):
        import json as _json

        import requests

        self._patch_cfg({})
        calls = self._patch_setter()
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/shortcuts", data=_json.dumps({"shortcuts": "nope"})
            ).json()
            self.assertFalse(res["ok"])
            self.assertIn("对象", res["error"])
            self.assertEqual(calls, [], "非法载荷不应落盘")
        finally:
            self._stop(server)

    def test_post_non_string_value_400(self):
        import json as _json

        import requests

        self._patch_cfg({})
        calls = self._patch_setter()
        server, base = self._start()
        try:
            res = requests.post(
                base + "/api/shortcuts", data=_json.dumps({"shortcuts": {"bold": 5}})
            ).json()
            self.assertFalse(res["ok"])
            self.assertIn("字符串", res["error"])
            self.assertEqual(calls, [])
        finally:
            self._stop(server)

    def test_post_too_many_entries_400(self):
        import json as _json

        import requests

        self._patch_cfg({})
        calls = self._patch_setter()
        server, base = self._start()
        try:
            big = {f"op{i}": "Ctrl+A" for i in range(101)}
            res = requests.post(
                base + "/api/shortcuts", data=_json.dumps({"shortcuts": big})
            ).json()
            self.assertFalse(res["ok"])
            self.assertIn("上限", res["error"])
            self.assertEqual(calls, [])
        finally:
            self._stop(server)


class TestSetShortcutsConfig(unittest.TestCase):
    """configmanage.set_shortcuts：原子写 + 无变更不写盘。"""

    def setUp(self):
        import configmanage

        self.cm = configmanage
        self.tmp = tempfile.mkdtemp(prefix="ptoe_cfg_")
        self.path = str(Path(self.tmp) / "config.json")
        self._orig_path = configmanage._CONFIG_PATH
        configmanage._CONFIG_PATH = self.path
        self.addCleanup(lambda: setattr(configmanage, "_CONFIG_PATH", self._orig_path))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_writes_shortcuts(self):
        import json as _json

        self.cm.set_shortcuts({"bold": "Ctrl+B"})
        cfg = _json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(cfg["shortcuts"], {"bold": "Ctrl+B"})

    def test_no_write_when_unchanged(self):
        import os as _os

        self.cm.set_shortcuts({"bold": "Ctrl+B"})
        mtime1 = _os.stat(self.path).st_mtime_ns
        self.cm.set_shortcuts({"bold": "Ctrl+B"})  # 无变更 → 不写盘
        self.assertEqual(_os.stat(self.path).st_mtime_ns, mtime1)

    def test_values_coerced_to_str(self):
        cfg = self.cm.set_shortcuts({"bold": "Ctrl+B", "x": None})
        self.assertEqual(cfg["shortcuts"]["x"], "")

    def test_non_dict_raises(self):
        with self.assertRaises(ValueError):
            self.cm.set_shortcuts("nope")

    def test_default_config_seeds_shortcuts(self):
        self.assertIn("shortcuts", self.cm.DEFAULT_CONFIG)
        patched = self.cm.validate_and_patch_config({"llama_server": "x", "models_dir": "y"})
        self.assertEqual(patched["shortcuts"], {})


class TestFormatRules(unittest.TestCase):
    """格式规则：_validate_format_rules + /api/format_rules 端点。"""

    def test_validate_format_rules_filters_bad(self):
        from correctmanage import _validate_format_rules

        rules = _validate_format_rules(
            [
                {
                    "name": "标题居中",
                    "mode": "first",
                    "conditions": [
                        {
                            "type": "prefix", "pattern": "第", "scope": "paragraph",
                            "formats": ["bold", "align_center", "bogus"],
                        }
                    ],
                },
                {
                    "name": "坏正则",
                    "mode": "first",
                    "conditions": [
                        {
                            "type": "regex", "pattern": "(unclosed", "scope": "selection",
                            "formats": ["bold"],
                        }
                    ],
                },
                {
                    "name": "",
                    "mode": "first",
                    "conditions": [
                        {
                            "type": "contains", "pattern": "X", "scope": "selection",
                            "formats": ["bold"],
                        }
                    ],
                },
                {
                    "name": "条件全非法",
                    "mode": "first",
                    "conditions": [
                        {
                            "type": "regex", "pattern": "(unclosed", "scope": "selection",
                            "formats": ["bold"],
                        },
                        "not-a-dict",
                    ],
                },
                "not-a-dict",
            ]
        )
        self.assertEqual(len(rules), 1)
        r = rules[0]
        self.assertEqual(r["name"], "标题居中")
        self.assertEqual(r["mode"], "first")
        self.assertEqual(len(r["conditions"]), 1)
        c = r["conditions"][0]
        self.assertEqual(c["type"], "prefix")
        self.assertEqual(c["scope"], "paragraph")
        self.assertEqual(c["formats"], ["bold", "align_center"])  # bogus 被过滤
        self.assertTrue(r["id"])

    def test_validate_migrates_old_format(self):
        from correctmanage import _validate_format_rules

        rules = _validate_format_rules(
            [
                {
                    "name": "旧规则",
                    "formats": ["bold"],
                    "condition": {
                        "enabled": True, "type": "contains",
                        "pattern": "X", "scope": "selection",
                    },
                    "else_formats": ["align_left"],
                },
                {
                    "name": "旧无条件",
                    "formats": ["bold"],
                    "condition": {"enabled": False},
                    "else_formats": [],
                },
            ]
        )
        self.assertEqual(len(rules), 2)
        r = rules[0]
        self.assertEqual(r["name"], "旧规则")
        self.assertEqual(r["mode"], "first")
        self.assertEqual(len(r["conditions"]), 1)
        self.assertEqual(
            r["conditions"][0],
            {"type": "contains", "pattern": "X", "scope": "selection", "formats": ["bold"], "target": "match"},
        )
        self.assertNotIn("else_formats", r)
        self.assertNotIn("formats", r)
        self.assertNotIn("condition", r)
        r2 = rules[1]
        self.assertEqual(
            r2["conditions"][0],
            {"type": "contains", "pattern": "", "scope": "selection", "formats": ["bold"], "target": "match"},
        )

    def test_validate_accepts_page_scope(self):
        from correctmanage import _validate_format_rules

        rules = _validate_format_rules(
            [
                {
                    "name": "页面级",
                    "mode": "first",
                    "conditions": [
                        {
                            "type": "contains", "pattern": "X", "scope": "page",
                            "formats": ["bold"],
                        }
                    ],
                }
            ]
        )
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["conditions"][0]["scope"], "page")

    def test_validate_page_scope_default_still_selection_for_old(self):
        # 旧模型迁移：无 scope 的条件仍落 selection（页面级是显式新值）
        from correctmanage import _validate_format_rules

        rules = _validate_format_rules(
            [
                {
                    "name": "旧规则",
                    "formats": ["bold"],
                    "condition": {"enabled": True, "type": "contains", "pattern": "X"},
                    "else_formats": [],
                }
            ]
        )
        self.assertEqual(rules[0]["conditions"][0]["scope"], "selection")

    def test_validate_mode_and_none(self):
        from correctmanage import _validate_format_rules

        rules = _validate_format_rules(
            [
                {
                    "name": "全部应用",
                    "mode": "all",
                    "conditions": [
                        {
                            "type": "contains", "pattern": "X", "scope": "selection",
                            "formats": ["bold", "none"],
                        }
                    ],
                },
                {
                    "name": "非法模式",
                    "mode": "bogus",
                    "conditions": [
                        {
                            "type": "contains", "pattern": "Y", "scope": "selection",
                            "formats": ["none"],
                        }
                    ],
                },
            ]
        )
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0]["mode"], "all")
        self.assertIn("none", rules[0]["conditions"][0]["formats"])
        self.assertEqual(rules[1]["mode"], "first")
    def test_validate_accepts_match_formats(self):
        from correctmanage import _validate_format_rules

        rules = _validate_format_rules(
            [
                {
                    "name": "多次匹配格式",
                    "conditions": [
                        {
                            "type": "regex",
                            "pattern": "\\d+",
                            "scope": "selection",
                            "formats": ["bold"],
                            "match_formats": [["italic"], ["align_center", "bold"]],
                        }
                    ],
                }
            ]
        )
        self.assertEqual(len(rules), 1)
        cond = rules[0]["conditions"][0]
        self.assertIn("match_formats", cond)
        self.assertEqual(cond["match_formats"], [["italic"], ["align_center", "bold"]])

    def test_validate_accepts_target_before(self):
        from correctmanage import _validate_format_rules

        rules = _validate_format_rules(
            [
                {
                    "name": "条件之前",
                    "mode": "first",
                    "conditions": [
                        {
                            "type": "contains", "pattern": "标题", "scope": "page",
                            "formats": ["bold"], "target": "before",
                        }
                    ],
                }
            ]
        )
        self.assertEqual(len(rules), 1)
        cond = rules[0]["conditions"][0]
        self.assertEqual(cond["target"], "before")
        self.assertEqual(cond["pattern"], "标题")

    def test_validate_accepts_target_after(self):
        from correctmanage import _validate_format_rules

        rules = _validate_format_rules(
            [
                {
                    "name": "条件之后",
                    "mode": "first",
                    "conditions": [
                        {
                            "type": "regex", "pattern": "\\d+年", "scope": "page",
                            "formats": ["italic"], "target": "after",
                        }
                    ],
                }
            ]
        )
        self.assertEqual(len(rules), 1)
        cond = rules[0]["conditions"][0]
        self.assertEqual(cond["target"], "after")

    def test_validate_accepts_target_between(self):
        from correctmanage import _validate_format_rules

        rules = _validate_format_rules(
            [
                {
                    "name": "两条件之间",
                    "mode": "first",
                    "conditions": [
                        {
                            "type": "regex", "pattern": "开始", "scope": "page",
                            "formats": ["bold"],
                            "target": "between",
                            "between_end_pattern": "结束",
                        }
                    ],
                }
            ]
        )
        self.assertEqual(len(rules), 1)
        cond = rules[0]["conditions"][0]
        self.assertEqual(cond["target"], "between")
        self.assertEqual(cond["between_end_pattern"], "结束")

    def test_validate_target_default_match(self):
        from correctmanage import _validate_format_rules

        rules = _validate_format_rules(
            [
                {
                    "name": "无target",
                    "mode": "first",
                    "conditions": [
                        {
                            "type": "contains", "pattern": "X", "scope": "selection",
                            "formats": ["bold"],
                        }
                    ],
                }
            ]
        )
        cond = rules[0]["conditions"][0]
        self.assertEqual(cond["target"], "match")

    def test_validate_target_invalid_falls_back(self):
        from correctmanage import _validate_format_rules

        rules = _validate_format_rules(
            [
                {
                    "name": "坏target",
                    "mode": "first",
                    "conditions": [
                        {
                            "type": "contains", "pattern": "X", "scope": "selection",
                            "formats": ["bold"], "target": "bogus",
                        }
                    ],
                }
            ]
        )
        cond = rules[0]["conditions"][0]
        self.assertEqual(cond["target"], "match")

    def test_validate_between_bad_regex_clears_end_pattern(self):
        from correctmanage import _validate_format_rules

        rules = _validate_format_rules(
            [
                {
                    "name": "坏结束正则",
                    "mode": "first",
                    "conditions": [
                        {
                            "type": "regex", "pattern": "开始", "scope": "page",
                            "formats": ["bold"],
                            "target": "between",
                            "between_end_pattern": "(unclosed",
                        }
                    ],
                }
            ]
        )
        cond = rules[0]["conditions"][0]
        self.assertEqual(cond["target"], "between")
        self.assertEqual(cond["between_end_pattern"], "")  # bad regex cleared

    def test_validate_between_non_regex_passes_end_pattern(self):
        from correctmanage import _validate_format_rules

        rules = _validate_format_rules(
            [
                {
                    "name": "contains between",
                    "mode": "first",
                    "conditions": [
                        {
                            "type": "contains", "pattern": "开始", "scope": "page",
                            "formats": ["bold"],
                            "target": "between",
                            "between_end_pattern": "结束",
                        }
                    ],
                }
            ]
        )
        cond = rules[0]["conditions"][0]
        self.assertEqual(cond["between_end_pattern"], "结束")

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

    def _patch_cfg(self, rules=None):
        import configmanage

        cfg = {"model_choices": {}, "selected_model": None}
        if rules is not None:
            cfg["format_rules"] = rules
        orig = configmanage.get_config
        configmanage.get_config = lambda *a, **k: cfg
        self.addCleanup(lambda: setattr(configmanage, "get_config", orig))
        return cfg

    def _patch_setter(self):
        import configmanage

        calls = []
        orig = configmanage.set_format_rules
        configmanage.set_format_rules = lambda rules: calls.append(rules)
        self.addCleanup(lambda: setattr(configmanage, "set_format_rules", orig))
        return calls

    def test_get_returns_empty_by_default(self):
        import requests

        self._patch_cfg()
        server, base = self._start()
        try:
            res = requests.get(base + "/api/format_rules").json()
            self.assertTrue(res["ok"])
            self.assertEqual(res["rules"], [])
        finally:
            self._stop(server)

    def test_get_returns_saved_rules(self):
        import requests

        rules = [
            {
                "id": "r1", "name": "标题", "mode": "first",
                "conditions": [
                    {"type": "contains", "pattern": "", "scope": "selection", "formats": ["bold"], "target": "match"}
                ],
            }
        ]
        self._patch_cfg(rules)
        server, base = self._start()
        try:
            res = requests.get(base + "/api/format_rules").json()
            self.assertEqual(res["rules"], rules)
        finally:
            self._stop(server)

    def test_get_migrates_stored_rules(self):
        import requests

        old_rules = [
            {
                "id": "r1", "name": "旧标题", "formats": ["bold"],
                "condition": {
                    "enabled": True, "type": "prefix",
                    "pattern": "第", "scope": "paragraph",
                },
                "else_formats": ["align_left"],
            }
        ]
        self._patch_cfg(old_rules)
        server, base = self._start()
        try:
            res = requests.get(base + "/api/format_rules").json()
            self.assertTrue(res["ok"])
            self.assertEqual(len(res["rules"]), 1)
            r = res["rules"][0]
            self.assertEqual(r["id"], "r1")
            self.assertEqual(r["name"], "旧标题")
            self.assertEqual(r["mode"], "first")
            self.assertEqual(
                r["conditions"],
                [{"type": "prefix", "pattern": "第", "scope": "paragraph", "formats": ["bold"], "target": "match"}],
            )
            self.assertNotIn("else_formats", r)
            self.assertNotIn("formats", r)
            self.assertNotIn("condition", r)
        finally:
            self._stop(server)

    def test_post_saves_rules(self):
        import requests

        self._patch_cfg()
        calls = self._patch_setter()
        server, base = self._start()
        try:
            body = {
                "rules": [
                    {
                        "name": "居中",
                        "mode": "first",
                        "conditions": [
                            {
                                "type": "contains", "pattern": "", "scope": "selection",
                                "formats": ["align_center"],
                            }
                        ],
                    }
                ]
            }
            res = requests.post(base + "/api/format_rules", json=body).json()
            self.assertTrue(res["ok"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0]["name"], "居中")
            self.assertEqual(calls[0][0]["conditions"][0]["formats"], ["align_center"])
        finally:
            self._stop(server)


class TestFormatRulesConfig(unittest.TestCase):
    """configmanage.set_format_rules：原子写。"""

    def setUp(self):
        import configmanage

        self.cm = configmanage
        self.tmp = tempfile.mkdtemp(prefix="ptoe_fr_cfg_")
        self.path = str(Path(self.tmp) / "config.json")
        self._orig_path = configmanage._CONFIG_PATH
        configmanage._CONFIG_PATH = self.path
        self.addCleanup(lambda: setattr(configmanage, "_CONFIG_PATH", self._orig_path))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_writes_format_rules(self):
        import json as _json

        rules = [
            {
                "id": "a", "name": "标题", "mode": "first",
                "conditions": [
                    {
                        "type": "contains", "pattern": "", "scope": "selection",
                        "formats": ["bold", "align_center"],
                        "target": "match",
                    }
                ],
            }
        ]
        self.cm.set_format_rules(rules)
        cfg = _json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(cfg["format_rules"], rules)

    def test_rejects_non_list(self):
        with self.assertRaises(ValueError):
            self.cm.set_format_rules({"x": 1})

    def test_writes_format_rules_page_scope(self):
        import json as _json

        rules = [
            {
                "id": "p1", "name": "页面级", "mode": "first",
                "conditions": [
                    {
                        "type": "contains", "pattern": "", "scope": "page",
                        "formats": ["bold"],
                        "target": "match",
                    }
                ],
            }
        ]
        self.cm.set_format_rules(rules)
        cfg = _json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(cfg["format_rules"], rules)

    def test_default_config_seeds_format_rules(self):
        self.assertIn("format_rules", self.cm.DEFAULT_CONFIG)
        patched = self.cm.validate_and_patch_config(
            {"llama_server": "x", "models_dir": "y"}
        )
        self.assertEqual(patched["format_rules"], [])


# ---------------------------------------------------------------------------
# 内嵌预览图缓存（_build_embedded_images / _prerender_embedded_images）
#   2026-08-18 修复：保存/暂存/完成 CPU 飙升 + 按钮卡死的根因是每次都全量
#   重渲染所有 PDF 页。修复 = 增量缓存（只渲染缺失页）+ 后台预渲染（用户
#   编辑期间渐进填充缓存）。这里用 fake fitz 注入 sys.modules 验证。
# ---------------------------------------------------------------------------


class _FakePix:
    """fake fitz Pixmap：tobytes 返回页码标识 bytes（可 base64 编码对比）。"""

    def __init__(self, page_idx):
        self.page_idx = page_idx

    def tobytes(self, fmt, jpg_quality=50):
        return f"jpg-{self.page_idx}".encode("ascii")


class _FakePage:
    """fake fitz Page：rendered 计数用于断言「是否重复渲染」。"""

    def __init__(self, idx):
        self.idx = idx
        self.rendered = 0
        self._fail = False

    def get_pixmap(self, matrix=None):
        self.rendered += 1
        if self._fail:
            raise RuntimeError("render fail")
        return _FakePix(self.idx)


class _FakeDoc:
    """fake fitz Document：支持 len()、page_count、下标取页。"""

    def __init__(self, n):
        self._pages = [_FakePage(i) for i in range(n)]
        self.page_count = n

    def __len__(self):
        return len(self._pages)

    def __getitem__(self, i):
        return self._pages[i]


class _FakeFitz:
    """fake fitz 模块：Matrix + open。open 不用于这些测试（preview_doc 直接注入）。"""

    @staticmethod
    def Matrix(x, y):
        return (x, y)

    @staticmethod
    def open(path):
        return _FakeDoc(3)


class TestEmbeddedImages(unittest.TestCase):
    """增量缓存 + 后台预渲染：避免保存/暂存/完成时全量重渲染。"""

    def setUp(self):
        import sys

        self._sys = sys
        self._old_fitz = sys.modules.get("fitz")
        sys.modules["fitz"] = _FakeFitz()

    def tearDown(self):
        if self._old_fitz is not None:
            self._sys.modules["fitz"] = self._old_fitz
        else:
            self._sys.modules.pop("fitz", None)

    def _state(self, doc, **over):
        import threading

        st = {
            "preview_doc": doc,
            "preview_doc_lock": threading.Lock(),
            "preview_dpi": 110,
            "preview_quality": 82,
            "embedded_images": {},
            "finished": threading.Event(),
            "pdf_path": None,
        }
        st.update(over)
        return st

    def test_build_renders_all_pages_first_call(self):
        # 首次调用：渲染全部页，回写 embedded_images 缓存
        doc = _FakeDoc(3)
        st = self._state(doc)
        out = _build_embedded_images(st)
        self.assertEqual(set(out.keys()), {"1", "2", "3"})
        self.assertEqual(out["1"], _b64(b"jpg-0"))
        self.assertEqual(sum(p.rendered for p in doc._pages), 3)

    def test_build_incremental_cache_skips_cached(self):
        # 第二次调用：全命中缓存，不再重复渲染（rendered 计数不变）
        doc = _FakeDoc(3)
        st = self._state(doc)
        first = _build_embedded_images(st)
        rendered_after_first = sum(p.rendered for p in doc._pages)
        second = _build_embedded_images(st)
        self.assertEqual(rendered_after_first, sum(p.rendered for p in doc._pages))
        self.assertEqual(second, first)
        # 缓存已回写 state
        self.assertIs(st["embedded_images"], second)

    def test_build_partial_cache_only_renders_missing(self):
        # embedded_images 已有部分页 → 只渲染缺失页，保留已有
        doc = _FakeDoc(3)
        st = self._state(doc)
        st["embedded_images"] = {"1": _b64(b"jpg-0")}
        out = _build_embedded_images(st)
        self.assertEqual(out["1"], _b64(b"jpg-0"))  # 保留已有
        self.assertIn("2", out)
        self.assertIn("3", out)
        # 第 1 页未渲染（命中缓存），2、3 各渲染一次
        self.assertEqual(doc._pages[0].rendered, 0)
        self.assertEqual(doc._pages[1].rendered, 1)
        self.assertEqual(doc._pages[2].rendered, 1)

    def test_build_skips_empty_placeholder(self):
        # 后台预渲染失败的页置空串占位 → _build_embedded_images 跳过不重渲染
        doc = _FakeDoc(2)
        st = self._state(doc)
        st["embedded_images"] = {"1": ""}  # 占位
        out = _build_embedded_images(st)
        self.assertEqual(out["1"], "")  # 保留占位
        self.assertIn("2", out)
        self.assertEqual(doc._pages[0].rendered, 0)  # 跳过占位页
        self.assertEqual(doc._pages[1].rendered, 1)

    def test_build_no_doc_returns_empty(self):
        # preview_doc 为 None + pdf_path 为 None → 返回 {}（不阻断写入）
        import threading

        st = {
            "preview_doc": None,
            "preview_doc_lock": threading.Lock(),
            "preview_dpi": 110,
            "preview_quality": 82,
            "embedded_images": {},
            "pdf_path": None,
        }
        self.assertEqual(_build_embedded_images(st), {})

    def test_prerender_warms_cache_then_exits(self):
        # 后台线程：渲染全部页后退出（无未缓存页 → return）
        import threading
        import time

        doc = _FakeDoc(3)
        st = self._state(doc)
        t = threading.Thread(
            target=_prerender_embedded_images, args=(st,), daemon=True
        )
        t.start()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive(), "后台线程应及时结束")
        self.assertEqual(set(st["embedded_images"].keys()), {"1", "2", "3"})
        self.assertEqual(sum(p.rendered for p in doc._pages), 3)

    def test_prerender_stops_on_finished(self):
        # 用户点「完成」→ finished 置位 → 线程退出（未渲染完所有页）
        import threading
        import time

        doc = _FakeDoc(100)
        st = self._state(doc)
        t = threading.Thread(
            target=_prerender_embedded_images, args=(st,), daemon=True
        )
        t.start()
        time.sleep(0.2)  # 让它渲染几页
        st["finished"].set()
        t.join(timeout=2.0)
        self.assertFalse(t.is_alive(), "finished 后应退出")
        rendered = sum(p.rendered for p in doc._pages)
        self.assertGreater(rendered, 0, "应已渲染一些页")
        self.assertLess(rendered, 100, "不应渲染完所有页")

    def test_prerender_failed_page_placeholder(self):
        # 渲染失败的页置空串占位 → 后续保存的 _build_embedded_images 跳过
        doc = _FakeDoc(2)
        doc._pages[1]._fail = True  # 第 2 页渲染失败
        st = self._state(doc)
        _prerender_embedded_images(st)  # 同步调用（while 循环会跑完）
        self.assertEqual(st["embedded_images"]["2"], "")  # 占位空串
        self.assertIn("1", st["embedded_images"])
        self.assertNotEqual(st["embedded_images"]["1"], "")

    def test_prerender_no_doc_returns(self):
        # 无 PDF（preview_doc=None + pdf_path=None）→ 线程立即退出
        import threading

        st = self._state(None, pdf_path=None)
        t = threading.Thread(
            target=_prerender_embedded_images, args=(st,), daemon=True
        )
        t.start()
        t.join(timeout=2.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(st["embedded_images"], {})

    def test_prerender_skips_already_cached(self):
        # 已有缓存页 → 后台跳过这些页（rendered 不增加）
        import threading

        doc = _FakeDoc(3)
        st = self._state(doc)
        st["embedded_images"] = {"1": _b64(b"jpg-0")}  # 第 1 页已缓存
        _prerender_embedded_images(st)  # 同步跑完
        self.assertEqual(doc._pages[0].rendered, 0)  # 跳过已缓存
        self.assertEqual(doc._pages[1].rendered, 1)
        self.assertEqual(doc._pages[2].rendered, 1)
        # 第 1 页保留原缓存值
        self.assertEqual(st["embedded_images"]["1"], _b64(b"jpg-0"))

    def test_prerender_respects_cap(self):
        # 预渲染上限：达到 prerender_max_pages 后停止（大书内存有界，
        # 实测 4000 页全量驻留 ≈ 800MB，故默认上限 _PRERENDER_MAX_PAGES）
        doc = _FakeDoc(5)
        st = self._state(doc, prerender_max_pages=2)
        _prerender_embedded_images(st)  # 同步跑完
        self.assertEqual(set(st["embedded_images"].keys()), {"1", "2"})
        self.assertEqual(sum(p.rendered for p in doc._pages), 2)

    def test_prerender_cap_writes_sidecar(self):
        # 达上限停止时把已渲染页写入共享 sidecar（跨电脑兜底覆盖前段页面）
        import correctmanage as _cm

        hist_dir = Path(tempfile.mkdtemp(prefix="test_scc_"))
        self.addCleanup(shutil.rmtree, hist_dir, ignore_errors=True)
        orig = _cm._history_dir
        _cm._history_dir = lambda: hist_dir
        self.addCleanup(lambda: setattr(_cm, "_history_dir", orig))
        doc = _FakeDoc(3)
        st = self._state(doc, prerender_max_pages=1, history_prefix="precap")
        _prerender_embedded_images(st)
        loaded = _cm._load_images_cache("precap")
        self.assertEqual(set(loaded.keys()), {"1"})

    def test_resolve_prerender_max(self):
        # config.json 顶层键 prerender_max_pages 覆盖默认；非法值回退默认
        import configmanage as _cfg
        import correctmanage as _cm

        orig = _cfg.get_config
        self.addCleanup(setattr, _cfg, "get_config", orig)
        _cfg.get_config = lambda show_dialogs=False: {}
        self.assertEqual(_cm._resolve_prerender_max(), _cm._PRERENDER_MAX_PAGES)
        _cfg.get_config = lambda show_dialogs=False: {"prerender_max_pages": "7"}
        self.assertEqual(_cm._resolve_prerender_max(), 7)
        _cfg.get_config = lambda show_dialogs=False: {"prerender_max_pages": "abc"}
        self.assertEqual(_cm._resolve_prerender_max(), _cm._PRERENDER_MAX_PAGES)
        _cfg.get_config = lambda show_dialogs=False: {"prerender_max_pages": "0"}
        self.assertEqual(_cm._resolve_prerender_max(), _cm._PRERENDER_MAX_PAGES)


class TestImagesSidecar(unittest.TestCase):
    """预览图共享 sidecar（2026-08-18）：版本文件不再携带 images。

    大书（数百页）保存/暂存曾把整本书预览图 base64 一起 json.dumps+写盘
    （实测 536 页书 ~110MB/次），CPU 飙升、按钮卡死数秒。修复后版本文件
    只含文本（~1MB），预览图由后台线程写入每 book 一份的
    <prefix>.images.json，载入历史时按版本前缀回退读取。
    """

    def _patch_history_dir(self, hist_dir):
        import correctmanage as _cm

        orig = _cm._history_dir
        _cm._history_dir = lambda: hist_dir
        self.addCleanup(lambda: setattr(_cm, "_history_dir", orig))
        return _cm

    def test_version_prefix_manual_and_sha1(self):
        # manual 会话取前两段，sha1 pdf 前缀无下划线直接取首段
        import correctmanage as _cm

        self.assertEqual(
            _cm._version_prefix("manual_6fa6da96_20260818204035_2b02"),
            "manual_6fa6da96",
        )
        self.assertEqual(
            _cm._version_prefix("aaa111_20260101000000_0001"), "aaa111"
        )

    def test_images_cache_roundtrip_and_corrupt(self):
        # sidecar 写入/读取往返；缺失与损坏回退空 dict；原子写不留临时文件
        import json as _json

        hist_dir = Path(tempfile.mkdtemp(prefix="test_scc_"))
        cm = self._patch_history_dir(hist_dir)
        self.assertTrue(
            cm._write_images_cache("pre1", {"1": "AAAA", "2": "BBBB"})
        )
        self.assertEqual(
            cm._load_images_cache("pre1"), {"1": "AAAA", "2": "BBBB"}
        )
        self.assertEqual(cm._load_images_cache("nosuch"), {})
        (cm._images_cache_path("pre1")).write_text("not json{{", encoding="utf-8")
        self.assertEqual(cm._load_images_cache("pre1"), {})
        self.assertEqual(
            list(hist_dir.glob(".*.tmp")), [], "原子写不应残留临时文件"
        )

    def test_load_history_version_sidecar_fallback_and_legacy_pref(self):
        # 新格式版本文件（无 images 键）→ 回退读共享 sidecar；
        # 旧格式（版本文件自带 images 键）→ 优先用文件内 images，不回退
        import json as _json
        import correctmanage as _cm

        hist_dir = Path(tempfile.mkdtemp(prefix="test_loadsc_"))
        cm = self._patch_history_dir(hist_dir)
        (hist_dir / "testprefix_20260101000000_0001.json").write_text(
            _json.dumps({"pdf": "C:/a.pdf", "pages": {"1": "<p>x</p>"}}),
            encoding="utf-8",
        )
        cm._write_images_cache("testprefix", {"1": "AAAA"})
        loaded = cm._load_history_version("testprefix_20260101000000_0001")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["embedded_images"], {"1": "AAAA"})
        (hist_dir / "testprefix_20260102000000_0002.json").write_text(
            _json.dumps(
                {"pdf": "C:/a.pdf", "pages": {"1": "<p>x</p>"}, "images": {"1": "LEGACY"}}
            ),
            encoding="utf-8",
        )
        cm._write_images_cache("testprefix", {"1": "SIDECAR"})
        loaded = _cm._load_history_version("testprefix_20260102000000_0002")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["embedded_images"], {"1": "LEGACY"})

    def test_save_stage_payload_has_no_images_and_sidecar_written(self):
        # 保存/暂存后：版本文件不带 images 键；后台线程最终写出共享 sidecar；
        # 历史列表不把 sidecar 当版本条目
        import json as _json
        import time
        from http.server import ThreadingHTTPServer
        from threading import Thread

        import requests
        import correctmanage as _cm
        from correctmanage import _CorrectionHandler

        hist_dir = Path(tempfile.mkdtemp(prefix="test_imgsink_"))
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
            "on_convert": None,
            "convert_lock": __import__("threading").Lock(),
            "history_prefix": "testprefix",
            "history_lock": __import__("threading").Lock(),
            "embedded_images": {"1": "AAAA", "2": "BBBB"},
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
            body = _json.dumps({"pages": [{"page": 1, "html": "<p>改</p>"}]})
            r = requests.post(base + "/api/save", data=body).json()
            self.assertTrue(r["ok"])
            r2 = requests.post(base + "/api/stage", data=body).json()
            self.assertTrue(r2["ok"])
            files = sorted(hist_dir.glob("testprefix_*.json"))
            self.assertEqual(len(files), 2, "保存覆盖 + 暂存新建 = 2 个版本文件")
            for fp in files:
                data = _json.loads(fp.read_text(encoding="utf-8"))
                self.assertNotIn("images", data, "版本文件不得携带 images 键")
            # 后台 flush 最终写出共享 sidecar（轮询 ≤5s）
            # Sidecar 现在是 gzip 压缩格式，使用 Path.glob 定位文件
            import time
            sidecars = list(hist_dir.glob("*.images.json"))
            deadline = time.time() + 5.0
            while not sidecars and time.time() < deadline:
                time.sleep(0.02)
                sidecars = list(hist_dir.glob("*.images.json"))
            self.assertTrue(sidecars, "保存后后台线程应写出预览图 sidecar")
            sidecar = sidecars[0]
            # Sidecar 现在是 gzip 压缩格式
            import gzip
            with gzip.open(sidecar, 'rt', encoding='utf-8') as gz:
                cached = _json.loads(gz.read())
            self.assertEqual(cached, {"1": "AAAA", "2": "BBBB"})
            # 历史列表不应把 sidecar 当版本条目
            items = _cm._history_entries()
            ids = [it["id"] for it in items]
            self.assertNotIn("testprefix.images", ids)
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(hist_dir, ignore_errors=True)

    def test_delete_history_removes_orphan_sidecar(self):
        # 按 ids 删除：仍有版本文件时 sidecar 保留；删光该 book 全部版本后
        # 清理孤儿 sidecar（避免 ~100MB 幽灵文件）
        import json as _json
        import correctmanage as _cm

        hist_dir = Path(tempfile.mkdtemp(prefix="test_delsc_"))
        cm = self._patch_history_dir(hist_dir)
        for stem in ("aaa_20260101000000_0001", "aaa_20260102000000_0002"):
            (hist_dir / f"{stem}.json").write_text(
                _json.dumps({"pdf": "C:/a.pdf", "pages": {"1": "<p>x</p>"}}),
                encoding="utf-8",
            )
        cm._write_images_cache("aaa", {"1": "AAAA"})
        sidecar = hist_dir / "aaa.images.json"
        self.assertTrue(sidecar.is_file())
        _cm._delete_history(["aaa_20260101000000_0001"])
        self.assertTrue(sidecar.is_file(), "仍有版本引用时 sidecar 保留")
        _cm._delete_history(["aaa_20260102000000_0002"])
        self.assertFalse(sidecar.is_file(), "无版本文件时删除孤儿 sidecar")

    def test_prerender_completion_writes_sidecar(self):
        # 预渲染线程跑完全部页后补写完整 sidecar（覆盖保存时缓存不完整场景）
        import json as _json
        import sys
        import threading
        import correctmanage as _cm

        old = sys.modules.get("fitz")
        sys.modules["fitz"] = _FakeFitz()
        try:
            hist_dir = Path(tempfile.mkdtemp(prefix="test_presc_"))
            self._patch_history_dir(hist_dir)
            doc = _FakeDoc(2)
            st = {
                "preview_doc": doc,
                "preview_doc_lock": threading.Lock(),
                "preview_dpi": 110,
                "preview_quality": 82,
                "embedded_images": {},
                "finished": threading.Event(),
                "pdf_path": None,
                "history_prefix": "testprefix",
            }
            # Pre-render writes the complete sidecar as a background task
            _cm._prerender_embedded_images(st)
            import time
            sidecars = list(hist_dir.glob("*.images.json"))
            deadline = time.time() + 5.0
            while not sidecars and time.time() < deadline:
                time.sleep(0.02)
                sidecars = list(hist_dir.glob("*.images.json"))
            self.assertTrue(sidecars, "预渲染完成应补写完整 sidecar")
            sidecar = sidecars[0]
            # Sidecar 现在是 gzip 压缩格式
            import gzip
            with gzip.open(sidecar, 'rt', encoding='utf-8') as gz:
                cached = _json.loads(gz.read())
            self.assertEqual(set(cached.keys()), {"1", "2"})
            self.assertEqual(cached["1"], _b64(b"jpg-0"))
            self.assertEqual(cached["2"], _b64(b"jpg-1"))
        finally:
            if old is not None:
                sys.modules["fitz"] = old
            else:
                sys.modules.pop("fitz", None)


class TestHistoryImportExport(unittest.TestCase):
    """历史记录导出/导入（跨平台矫正活动）。"""

    def _patch_history_dir(self, hist_dir):
        import correctmanage as _cm

        orig = _cm._history_dir
        _cm._history_dir = lambda: hist_dir
        self.addCleanup(lambda: setattr(_cm, "_history_dir", orig))
        return _cm

    def test_import_history_basic(self):
        import json as _json
        import correctmanage as _cm

        hist_dir = Path(tempfile.mkdtemp(prefix="test_imp_"))
        cm = self._patch_history_dir(hist_dir)
        content = {
            "pdf": "C:/books/test.pdf",
            "name": "test.pdf",
            "pages": {"1": "<p>Hello</p>", "2": "<p>World</p>"},
            "proofread": {"errors": {}, "original": {}, "dismissed": {}},
            "last_proofread_page": None,
        }
        ok, msg, stem = cm._import_history(content, "export_abc_20260101000000_0001.json")
        self.assertTrue(ok, msg)
        self.assertTrue(stem)
        fp = hist_dir / f"{stem}.json"
        self.assertTrue(fp.is_file())
        data = _json.loads(fp.read_text(encoding="utf-8"))
        self.assertEqual(data["pages"], content["pages"])
        self.assertEqual(data["pdf"], content["pdf"])
        items = cm._history_entries()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], stem)

    def test_import_history_with_images(self):
        import json as _json
        import correctmanage as _cm

        hist_dir = Path(tempfile.mkdtemp(prefix="test_impimg_"))
        cm = self._patch_history_dir(hist_dir)
        content = {
            "pdf": "C:/books/test.pdf",
            "pages": {"1": "<p>Hello</p>"},
            "images": {"1": "AAAA", "2": "BBBB"},
        }
        ok, msg, stem = cm._import_history(content, "export_abc_20260101000000_0001.json")
        self.assertTrue(ok, msg)
        sidecar = hist_dir / "export.images.json"
        self.assertTrue(sidecar.is_file())
        # Sidecar 现在是 gzip 压缩格式
        import gzip
        with gzip.open(sidecar, 'rt', encoding='utf-8') as gz:
            cached = _json.loads(gz.read())
        self.assertEqual(cached, {"1": "AAAA", "2": "BBBB"})

    def test_import_history_missing_pages_fails(self):
        import correctmanage as _cm

        hist_dir = Path(tempfile.mkdtemp(prefix="test_impfail_"))
        cm = self._patch_history_dir(hist_dir)
        ok, msg, stem = cm._import_history({}, "bad.json")
        self.assertFalse(ok)
        self.assertIn("pages", msg)
        self.assertEqual(stem, "")

    def test_import_history_no_pdf_uses_random_prefix(self):
        import json as _json
        import correctmanage as _cm

        hist_dir = Path(tempfile.mkdtemp(prefix="test_impnopdf_"))
        cm = self._patch_history_dir(hist_dir)
        content = {
            "pages": {"1": "<p>Hello</p>"},
        }
        ok, msg, stem = cm._import_history(content, "")
        self.assertTrue(ok, msg)
        self.assertTrue(stem.startswith("import_"))
        fp = hist_dir / f"{stem}.json"
        self.assertTrue(fp.is_file())

    def test_import_history_prunes_old_versions(self):
        import json as _json
        import correctmanage as _cm

        hist_dir = Path(tempfile.mkdtemp(prefix="test_impprune_"))
        cm = self._patch_history_dir(hist_dir)
        prefix = "testprefix"
        for i in range(cm._HISTORY_KEEP + 5):
            fp = hist_dir / f"{prefix}_2026010100000{i:02d}_{i:04d}.json"
            fp.write_text(
                _json.dumps({"pdf": "C:/a.pdf", "pages": {"1": f"<p>{i}</p>"}}),
                encoding="utf-8",
            )
        content = {
            "pdf": "C:/a.pdf",
            "pages": {"1": "<p>new</p>"},
            "images": {},
        }
        ok, msg, stem = cm._import_history(content, f"{prefix}_old.json")
        self.assertTrue(ok, msg)
        files = sorted(hist_dir.glob(f"{prefix}_*.json"))
        self.assertLessEqual(
            len(files), cm._HISTORY_KEEP,
            f"导入后应保留最多 {cm._HISTORY_KEEP} 个版本，实际 {len(files)}"
        )
        self.assertIn(stem, [fp.stem for fp in files])

    def test_history_export_import_roundtrip(self):
        import json as _json
        import correctmanage as _cm
        from http.server import ThreadingHTTPServer
        from threading import Thread
        import requests

        hist_dir = Path(tempfile.mkdtemp(prefix="test_rt_"))
        state = {
            "pages": {1: "<p>Hello</p>", 2: "<p>World</p>"},
            "finished": __import__("threading").Event(),
            "preview_cache": {},
            "pdf_path": "C:/books/test.pdf",
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
            "embedded_images": {"1": "AAAA", "2": "BBBB"},
        }
        _orig_dir = _cm._history_dir
        _cm._history_dir = lambda: hist_dir
        self.addCleanup(lambda: setattr(_cm, "_history_dir", _orig_dir))
        server = ThreadingHTTPServer(("127.0.0.1", 0), _cm._CorrectionHandler)
        server.daemon_threads = True
        server.state = state
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            body = _json.dumps({"pages": [{"page": 1, "html": "<p>改</p>"}]})
            r = requests.post(base + "/api/save", data=body).json()
            self.assertTrue(r["ok"])
            items = requests.get(base + "/api/history").json()["items"]
            self.assertTrue(len(items) > 0)
            vid = items[0]["id"]
            r = requests.get(base + f"/api/history/export?id={vid}")
            self.assertEqual(r.status_code, 200)
            exported = r.json()
            self.assertIn("pages", exported)
            r2 = requests.post(
                base + "/api/history/import",
                data=_json.dumps({"filename": f"{vid}.json", "content": exported}),
                headers={"Content-Type": "application/json"},
            ).json()
            self.assertTrue(r2.get("ok"), r2.get("error"))
            imported_id = r2["id"]
            r3 = requests.post(
                base + "/api/history/load",
                data=_json.dumps({"id": imported_id}),
            ).json()
            self.assertIn("pages", r3)
            self.assertEqual(r3["pages"][0]["page"], 1)
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(hist_dir, ignore_errors=True)

    def test_export_bulk_zip_roundtrip(self):
        import io
        import json as _json
        import zipfile
        import correctmanage as _cm
        from http.server import ThreadingHTTPServer
        from threading import Thread
        import requests

        hist_dir = Path(tempfile.mkdtemp(prefix="test_bulk_rt_"))
        _orig_dir = _cm._history_dir
        _cm._history_dir = lambda: hist_dir
        self.addCleanup(lambda: setattr(_cm, "_history_dir", _orig_dir))
        prefix = "testprefix"
        now = "20260101000000"
        ids = []
        for i in range(2):
            stem = f"{prefix}_{now}_{i:04d}"
            ids.append(stem)
            payload = {
                "pdf": "C:/books/test.pdf",
                "name": f"test_{i}.pdf",
                "pages": {"1": f"<p>Page {i}</p>"},
                "proofread": {"errors": {}, "original": {}, "dismissed": {}},
                "last_proofread_page": None,
            }
            (hist_dir / f"{stem}.json").write_text(
                _json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        state = {
            "pages": {},
            "finished": __import__("threading").Event(),
            "preview_cache": {},
            "pdf_path": "C:/books/test.pdf",
            "img_dir": None,
            "preview_dpi": 110,
            "preview_quality": 82,
            "last_heartbeat": 0.0,
            "gone_at": None,
            "idle_timeout": 600.0,
            "auto_finished": False,
            "on_convert": None,
            "convert_lock": __import__("threading").Lock(),
            "history_prefix": prefix,
            "history_lock": __import__("threading").Lock(),
            "embedded_images": {},
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _cm._CorrectionHandler)
        server.daemon_threads = True
        server.state = state
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            r = requests.post(
                base + "/api/history/export/bulk",
                data=_json.dumps({"ids": ids}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers.get("Content-Type"), "application/zip")
            zip_bytes = r.content
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                names = zf.namelist()
                self.assertEqual(sorted(names), sorted([f"{i}.json" for i in ids]))
                for name in names:
                    data = _json.loads(zf.read(name).decode("utf-8"))
                    self.assertIn("pages", data)
            b64 = _b64(zip_bytes)
            r2 = requests.post(
                base + "/api/history/import",
                data=_json.dumps({"filename": "x.zip", "is_zip": True, "content_b64": b64}),
                headers={"Content-Type": "application/json"},
            ).json()
            self.assertTrue(r2.get("ok"), r2.get("error"))
            self.assertEqual(len(r2["ids"]), 2)
            for imported_id in r2["ids"]:
                fp = hist_dir / f"{imported_id}.json"
                self.assertTrue(fp.is_file(), f"导入文件不存在: {fp}")
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(hist_dir, ignore_errors=True)

    def test_import_zip_multiple(self):
        import io
        import json as _json
        import zipfile
        import correctmanage as _cm
        from http.server import ThreadingHTTPServer
        from threading import Thread
        import requests

        hist_dir = Path(tempfile.mkdtemp(prefix="test_zipimp_"))
        _orig_dir = _cm._history_dir
        _cm._history_dir = lambda: hist_dir
        self.addCleanup(lambda: setattr(_cm, "_history_dir", _orig_dir))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("a.json", _json.dumps({
                "pdf": "C:/a.pdf", "pages": {"1": "<p>A</p>"}
            }))
            zf.writestr("b.json", _json.dumps({
                "pdf": "C:/b.pdf", "pages": {"1": "<p>B</p>"}
            }))
            zf.writestr("bad.json", "NOT JSON")
        state = {
            "pages": {},
            "finished": __import__("threading").Event(),
            "preview_cache": {},
            "pdf_path": "C:/books/test.pdf",
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
            "embedded_images": {},
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _cm._CorrectionHandler)
        server.daemon_threads = True
        server.state = state
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            b64 = _b64(buf.getvalue())
            r = requests.post(
                base + "/api/history/import",
                data=_json.dumps({"filename": "multi.zip", "is_zip": True, "content_b64": b64}),
                headers={"Content-Type": "application/json"},
            ).json()
            self.assertTrue(r.get("ok"), r.get("error"))
            self.assertEqual(len(r["ids"]), 2)
            self.assertTrue(r.get("errors"))
            self.assertEqual(len(r["errors"]), 1)
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(hist_dir, ignore_errors=True)

    def test_export_bulk_empty_ids(self):
        import correctmanage as _cm
        from http.server import ThreadingHTTPServer
        from threading import Thread
        import requests

        hist_dir = Path(tempfile.mkdtemp(prefix="test_bulk_empty_"))
        _orig_dir = _cm._history_dir
        _cm._history_dir = lambda: hist_dir
        self.addCleanup(lambda: setattr(_cm, "_history_dir", _orig_dir))
        state = {
            "pages": {},
            "finished": __import__("threading").Event(),
            "preview_cache": {},
            "pdf_path": "C:/books/test.pdf",
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
            "embedded_images": {},
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _cm._CorrectionHandler)
        server.daemon_threads = True
        server.state = state
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        import json as _json
        try:
            r = requests.post(
                base + "/api/history/export/bulk",
                data=_json.dumps({"ids": []}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(r.status_code, 400)
            data = r.json()
            self.assertIn("error", data)
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(hist_dir, ignore_errors=True)


class TestPreviewCache(unittest.TestCase):
    """预览图磁盘缓存：路径规则、/preview 读缓存与回写、多进程预热 worker。"""

    def setUp(self):
        import correctmanage as cm

        self.cm = cm
        self._tmp = Path(tempfile.mkdtemp(prefix="test_preview_cache_"))
        self._pdf = self._tmp / "book.pdf"
        _make_pdf(self._pdf, n=3)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _state(self, **over):
        import threading
        from collections import OrderedDict

        st = {
            "preview_cache": OrderedDict(),
            "pdf_path": str(self._pdf),
            "img_dir": None,
            "preview_dpi": 110,
            "preview_quality": 70,
            "preview_doc": None,
            "preview_doc_lock": threading.Lock(),
            "embedded_images": {},
        }
        st.update(over)
        return st

    def test_cache_path_uses_history_prefix(self):
        prefix = self.cm._history_prefix(str(self._pdf))
        p = self.cm._preview_cache_path(str(self._pdf), 110, 7)
        self.assertIsNotNone(p)
        self.assertTrue(
            p.endswith(f"{prefix}_110{os.sep}7.jpg"), f"unexpected path: {p}"
        )
        # 无 pdf → 磁盘缓存禁用
        self.assertIsNone(self.cm._preview_cache_path(None, 110, 1))

    def test_preview_serves_disk_cache_first(self):
        cache_dir = Path(self.cm._preview_cache_dir(str(self._pdf), 110))
        cache_dir.mkdir(parents=True)
        (cache_dir / "2.jpg").write_bytes(b"FAKEJPEG")
        state = self._state()

        def _boom(*a, **k):  # 不应被调用
            raise AssertionError("_render_jpeg must not run on disk-cache hit")

        orig = self.cm._render_jpeg
        self.cm._render_jpeg = _boom
        try:
            data = self.cm._preview_bytes(state, 2)
        finally:
            self.cm._render_jpeg = orig
        self.assertEqual(data, ("image/jpeg", b"FAKEJPEG"))
        # 命中结果进内存 LRU
        self.assertIn(2, state["preview_cache"])

    def test_preview_write_back_after_live_render(self):
        state = self._state()
        orig = self.cm._render_jpeg
        self.cm._render_jpeg = lambda *a, **k: ("image/jpeg", b"LIVE")
        try:
            data = self.cm._preview_bytes(state, 1)
        finally:
            self.cm._render_jpeg = orig
        self.assertEqual(data, ("image/jpeg", b"LIVE"))
        fp = Path(self.cm._preview_cache_path(str(self._pdf), 110, 1))
        self.assertTrue(fp.is_file())
        self.assertEqual(fp.read_bytes(), b"LIVE")

    def test_render_preview_chunk_real_pdf(self):
        out = self.cm._render_preview_chunk((str(self._pdf), 110, [1, 3]))
        self.assertEqual([pn for pn, _ in out], [1, 3])
        for _, raw in out:
            self.assertTrue(raw.startswith(b"\xff\xd8"))  # JPEG magic

    def test_warm_skips_small_books_without_pool(self):
        class _NoPool:
            def __init__(self, *a, **k):
                raise AssertionError("pool must not be created for small books")

        orig_cls = self.cm._PREVIEW_POOL_CLS
        orig_min = self.cm._WARM_MIN_PAGES
        self.cm._PREVIEW_POOL_CLS = _NoPool
        self.cm._WARM_MIN_PAGES = 80
        try:
            self.cm._warm_preview_cache(self._state())  # 3 页 < 80：静默返回
        finally:
            self.cm._PREVIEW_POOL_CLS = orig_cls
            self.cm._WARM_MIN_PAGES = orig_min

    def test_warm_skips_existing_pages(self):
        cache_dir = Path(self.cm._preview_cache_dir(str(self._pdf), 110))
        cache_dir.mkdir(parents=True)
        for i in range(1, 4):
            (cache_dir / f"{i}.jpg").write_bytes(b"X")

        submitted = []

        class _FakePool:
            def __init__(self, max_workers=None):
                pass

            def submit(self, fn, args):
                submitted.append(args)

                class _F:
                    def result(self):
                        return []

                return _F()

            def shutdown(self, wait=False):
                pass

        orig_cls = self.cm._PREVIEW_POOL_CLS
        orig_min = self.cm._WARM_MIN_PAGES
        self.cm._PREVIEW_POOL_CLS = _FakePool
        self.cm._WARM_MIN_PAGES = 0
        try:
            self.cm._warm_preview_cache(self._state())
        finally:
            self.cm._PREVIEW_POOL_CLS = orig_cls
            self.cm._WARM_MIN_PAGES = orig_min
        self.assertEqual(submitted, [])  # 全部页已有缓存，不提交任何块

    def test_warm_pool_failure_graceful(self):
        class _BoomPool:
            def __init__(self, *a, **k):
                raise RuntimeError("no spawn in tests")

        orig_cls = self.cm._PREVIEW_POOL_CLS
        orig_min = self.cm._WARM_MIN_PAGES
        self.cm._PREVIEW_POOL_CLS = _BoomPool
        self.cm._WARM_MIN_PAGES = 0
        try:
            self.cm._warm_preview_cache(self._state())  # 异常被吞，不外抛
        finally:
            self.cm._PREVIEW_POOL_CLS = orig_cls
            self.cm._WARM_MIN_PAGES = orig_min


def _b64(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode("ascii")


if __name__ == "__main__":
    unittest.main()
