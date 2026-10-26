# -*- coding: utf-8 -*-
"""LaTeX 公式（MathML 载体）端到端护栏（2026-10-21 新增功能）。

背景：矫正界面用 KaTeX 把 LaTeX 源码渲染成 MathML（<math>…</math>）存进正文。
公式要在**各端都不丢**：

  1. 保存清洗（correctmanage.sanitize_html）：<math> 与其属性必须原样保留
     （xmlns 一丢，导出的 XHTML 里 <math> 就不在 MathML 命名空间，公式不渲染）；
  2. 规则引擎（rulemanage.MiniDOMParser）：应用一次格式规则不能把公式清空
     （标签与属性走同一份白名单）；
  3. 导出 EPUB（htmlmanage._render_fragment）：MathML 原样进 XHTML；
  4. 导出 DOCX/MD/TXT（correctmanage._html_to_rich_blocks）：没有 MathML 概念，
     用 <math alttext="…"> 里的 LaTeX 源码做纯文本兜底（$公式$），
     且公式内部结构（<mi>x</mi> 的 "x"）不能漏进正文。

本测试锁定以上不变量。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MATHML_NS = "http://www.w3.org/1998/Math/MathML"

# 一段典型的 KaTeX MathML 输出（实测形态：span.katex > math > semantics > mrow）
MATHML_BODY = (
    "<semantics><mrow><mi>E</mi><mo>=</mo><mi>m</mi>"
    "<msup><mi>c</mi><mn>2</mn></msup></mrow>"
    "<annotation encoding=\"application/x-tex\">E=mc^2</annotation></semantics>"
)


def math_el(alt="E=mc^2", body=MATHML_BODY, display="inline"):
    return (
        '<math xmlns="%s" alttext="%s" display="%s">%s</math>'
        % (MATHML_NS, alt, display, body)
    )


class TestLatexFormulaPipeline(unittest.TestCase):
    """LaTeX 公式在各条链路上的保留 / 兜底。"""

    def test_sanitize_keeps_mathml(self):
        import correctmanage

        raw = "<p>前面 %s 后面</p>" % math_el()
        out = correctmanage.sanitize_html(raw)
        self.assertIn("<math", out, "sanitize_html 必须保留 <math> 元素")
        self.assertIn('xmlns="%s"' % MATHML_NS, out, "必须保留 MathML 命名空间")
        self.assertIn('alttext="E=mc^2"', out, "必须保留 alttext（LaTeX 源码载体）")
        self.assertIn("<semantics>", out)
        self.assertIn("<annotation", out)
        self.assertIn("</math>", out)

    def test_sanitize_keeps_mathml_inside_span(self):
        import correctmanage

        raw = '<p><span class="ptoe-underline">中 %s 后</span></p>' % math_el()
        out = correctmanage.sanitize_html(raw)
        self.assertIn('class="ptoe-underline"', out, "公式外层的行内格式类要保留")
        self.assertIn("<math", out, "span 内的公式不能被丢弃")

    def test_sanitize_drops_foreign_mathml_attrs(self):
        import correctmanage

        raw = (
            '<p><math xmlns="%s" alttext="x" onclick="alert(1)" '
            'style="position:fixed" href="javascript:alert(1)">'
            "<mi>x</mi></math></p>" % MATHML_NS
        )
        out = correctmanage.sanitize_html(raw)
        self.assertIn("<math", out)
        self.assertNotIn("onclick", out, "事件属性必须丢弃（安全边界）")
        self.assertNotIn("style=", out, "MathML 元素上的 style 不放行")
        self.assertNotIn("javascript:", out)

    def test_sanitize_drops_non_mathml_namespace(self):
        import correctmanage

        raw = '<p><math xmlns="http://example.com/x"><mi>x</mi></math></p>'
        out = correctmanage.sanitize_html(raw)
        self.assertIn("<math", out)
        self.assertNotIn("example.com", out, "非 MathML 命名空间必须丢弃")

    def test_rule_engine_roundtrip_keeps_mathml(self):
        """规则引擎重排 DOM 后公式不能丢（否则「应用一次规则就清空公式」）。"""
        import rulemanage

        dom = rulemanage.MiniDOMParser()
        dom.feed("<p>甲 %s 乙</p>" % math_el())
        dom.close()
        out = dom.get_root().to_html()
        self.assertIn("<math", out, "MiniDOMParser 必须放行 <math>")
        self.assertIn('xmlns="%s"' % MATHML_NS, out, "xmlns 必须保留")
        self.assertIn('alttext="E=mc^2"', out, "alttext 必须保留")
        self.assertIn("<semantics>", out)

    def test_epub_render_keeps_mathml(self):
        import correctmanage
        import htmlmanage

        html = correctmanage.sanitize_html("<p>甲 %s 乙</p>" % math_el())
        conv = htmlmanage.HTMLConverter(output_dir=".")
        body = conv._render_fragment(html)
        self.assertIn("<math", body, "EPUB 正文必须保留 MathML")
        self.assertIn('xmlns="%s"' % MATHML_NS, body)

    def test_epub_stylesheet_has_math_rule(self):
        import htmlmanage

        css = htmlmanage.CSSManager().generate_stylesheet()
        self.assertIn("math", css, "EPUB 样式表要有 math 规则（字号/基线微调）")

    def test_rich_blocks_fallback_to_latex_source(self):
        """DOCX/MD/TXT：无 MathML → 用 alttext 的 LaTeX 源码兜底成 $公式$。"""
        import correctmanage

        html = correctmanage.sanitize_html("<p>甲 %s 乙</p>" % math_el())
        blocks = correctmanage._html_to_rich_blocks(html)
        self.assertEqual(len(blocks), 1)
        text = blocks[0].get("text") or ""
        self.assertIn("$E=mc^2$", text, "公式应退化为 $LaTeX$ 纯文本")
        self.assertNotIn("E=mc2", text, "公式内部结构不得漏进正文（<mi> 的字母）")

    def test_rich_blocks_display_mode_keeps_source(self):
        import correctmanage

        html = correctmanage.sanitize_html(
            "<p>%s</p>" % math_el(alt="\\frac{1}{2}", display="block")
        )
        blocks = correctmanage._html_to_rich_blocks(html)
        self.assertIn("$\\frac{1}{2}$", blocks[0].get("text") or "")

    def test_clean_keeps_mathml(self):
        """智能清理不得把公式拆成散字（与表格同型的静默丢失）。"""
        import correctmanage

        raw = "<p>前 %s 后</p>" % math_el()
        out = correctmanage.clean_page_html(raw)
        self.assertIn("<math", out, "清理后公式必须还在")
        self.assertIn("<semantics>", out)
        self.assertIn("<annotation", out)
        # 其它非白名单标签仍要被剥（不能为了保公式把清理功能放废）
        self.assertEqual(correctmanage._STRIP_TAG_RE.sub("", "<pre>x</pre>"), "x")
        self.assertIn("<table>", correctmanage._STRIP_TAG_RE.sub("", "<table><tr><td>x</td></tr></table>"))

    def test_rich_blocks_table_cell_fallback(self):
        """表格单元格里的公式同样退化为 $公式$（且不漏出 <semantics> 等字面量）。"""
        import correctmanage

        raw = '<table><tr><td>前 %s 后</td></tr></table>' % math_el()
        html = correctmanage.sanitize_html(raw)
        blocks = correctmanage._html_to_rich_blocks(html)
        self.assertEqual(len(blocks), 1)
        rows = blocks[0].get("rows") or []
        self.assertTrue(rows, "表格块应有 rows")
        cell = rows[0][0]
        self.assertIn("$E=mc^2$", cell, "单元格里的公式应退化为 $LaTeX$")
        self.assertNotIn("<semantics", cell, "公式内部标签不得漏进单元格文本")

    def test_mathml_tags_registered_once(self):
        """白名单单一事实来源：rulemanage 导出，correctmanage 引用同一份。"""
        import rulemanage
        import correctmanage

        self.assertIn("math", rulemanage.MATHML_TAGS)
        self.assertIn("semantics", rulemanage.MATHML_TAGS)
        self.assertIn("mi", rulemanage.MATHML_TAGS)
        self.assertEqual(correctmanage._MATHML_TAGS, frozenset(rulemanage.MATHML_TAGS))
        self.assertEqual(correctmanage._MATHML_ATTRS, frozenset(rulemanage.MATHML_ATTRS))
        # 两条链路的标签集合必须一致（否则「保存保留、应用规则清空」）
        self.assertTrue(set(rulemanage.MATHML_TAGS).issubset(rulemanage.ALLOWED_TAGS))


class TestLatexFormulaAssets(unittest.TestCase):
    """前端资源：KaTeX 随包分发（离线可用），不依赖 CDN。"""

    def test_katex_vendored(self):
        p = os.path.join(ROOT, "ui", "vendor", "katex.min.js")
        self.assertTrue(os.path.isfile(p), "ui/vendor/katex.min.js 必须随包分发")
        self.assertGreater(os.path.getsize(p), 50_000, "KaTeX 文件不完整")
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
        self.assertIn("katex", head.lower(), "应是 KaTeX 库")

    def test_license_file_present(self):
        p = os.path.join(ROOT, "ui", "vendor", "katex.LICENSE.txt")
        self.assertTrue(os.path.isfile(p), "KaTeX 许可证文件必须随包（MIT）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
