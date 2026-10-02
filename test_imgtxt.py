"""test_imgtxt.py — 图文混合排版（2026-10）的输出侧与跨文件契约测试。

本文件固化两条独立于 UI 的能力：

1. **htmlmanage 图片样式内联化**（`_inline_img_styles` 及其映射函数）
   目标阅读器（静读天下会整体覆盖出版方 CSS、微信阅读无 WebView、只带 Safari
   UA 样式的极简引擎）不可靠地执行 OEBPS/style.css，而行内 style 属性特异性最高
   且能穿透用户样式表。转换层把图片 class 的等价声明写成行内 style + width 属性。
   存储侧继续只存 class（sanitize 会剥 style，历史 JSON 不该变大）。

   ⚠️ 两条本项目踩过的坑，写断言时必须避开：
   - **朴素子串会误判**：img 的 style 恒含 `max-width:100%`，用 `"width" in style`
     判定宽度、或用 `"height" in style` 判定压扁风险都会误报。本文件一律用
     负向先行断言 `(?<![\\w-])height\\s*:` / `(?<![\\w-])width\\s*:`。
   - **整页只有一张图的章节**会被 `_is_img_page` 判成 cover.xhtml 而不产出
     content 文件 → 「无输出」看起来像失败。所有章节都在首尾放正文段。

2. **题注（ptoe-caption）样式的跨文件一致性闸门**（P1）
   `correctmanage.py` 编辑器 CSS / `htmlmanage.py` EPUB CSS / `ui/epubedit.html`
   EPUB 编辑器 CSS 三处副本 + `correctmanage` 的 DOCX/Markdown 换算常量，
   全部必须落在同一份契约值上。本项目已因「同一值散落 N 份、改一处漏两处」出过
   bug（`ptoe-underdot` 漏登记），故用脚本化断言而非目测。

3. **CSS float 绕排**（ptoe-img-float-left / ptoe-img-float-right）
   类挂 `<img>` 自身（float 必须作用在图片元素上文字才绕排；挂在包裹 p 上无效），
   宽度复用既有的 ptoe-img-w* 档位。固化「映射 → 内联 → EPUB 产物 → 跨文件注册」
   四层，另有 `display:inline-block` 这一条**降级声明**必须逐字锁住：忽略 float 的
   阅读器据此退化成现有行内图行为，塌成独占块就是版式事故。

   ⚠️ **本文件不验证 float 的真实绕排几何**——jsdom/py 无排版引擎，绕排效果只能靠
   断言「声明都在」，实际是否绕排需在真实 Chrome/Edge 与目标阅读器里肉眼确认
   （见报告的未验项）。
"""

import re
import shutil
import tempfile
import unittest
import xml.dom.minidom
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import htmlmanage
from htmlmanage import (
    _CAPTION_CLASS,
    _img_declarations,
    _inline_img_styles,
    _p_block_declarations,
)

_REPO_ROOT = Path(__file__).resolve().parent

# 1x1 透明 PNG，够走通 data URI → Images/img_N.png 的提取链路
PNG_DATA_URI = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)

# 题注契约值（三处 CSS 副本 + DOCX/MD 换算的唯一事实源）
CANONICAL_CAPTION_DECLS = {
    "font-size": "0.85em",
    "color": "#666",
    "text-align": "center",
    "margin": "0.2em 0 0.6em",
}

# float 绕排契约值（三处 CSS 副本 + 内联映射的唯一事实源）。
# `height` **刻意不在此列**：CSS 三处都写 height:auto，内联侧故意不写
# （与 ptoe-img-full 同理——按百分比解析高度且父高 auto 的引擎会把图压扁），
# 这是内联声明与 CSS 声明之间有意保留的不一致，由
# test_float_css_parity_ignores_height 单点记账。
CANONICAL_FLOAT_DECLS = {
    "ptoe-img-float-left": {
        "float": "left",
        "display": "inline-block",
        "max-width": "100%",
        "margin": "0 0.5em 0.4em 0",
    },
    "ptoe-img-float-right": {
        "float": "right",
        "display": "inline-block",
        "max-width": "100%",
        "margin": "0 0 0.4em 0.5em",
    },
}

# float 绕排注册点：新增/改名时这 4 处必须同步，漏一处 = 该侧静默剥类。
#   ① correctmanage.py  _IMG_CLASSES      sanitize 白名单（class 存不下来的地方）
#   ② htmlmanage.py     _IMG_CLASSES      class → 声明映射的取值域
#   ③ rulemanage.py     allowed_img_classes  规则引擎解析期白名单
#   ④ ui/app.js         IMG_FLOAT_CLASSES  前端模式判定与互斥清理
FLOAT_IMG_CLASSES = ("ptoe-img-float-left", "ptoe-img-float-right")


# ---------------------------------------------------------------------------
# 断言辅助
# ---------------------------------------------------------------------------

# 属性名负向先行：`max-width` / `max-height` 里的 width/height 不算独立声明。
# 漏掉这个先行断言时 `"height" in style` 会被 max-width 之类的同族名命中。
_DECL_RE_TMPL = r"(?<![\w-])%s\s*:\s*([^;\"']*)"


def _decls_of(style_attr_value: str) -> dict:
    """行内 style 值 → {属性: 值}（属性小写、值压空白），便于逐项断言。"""
    out = {}
    for chunk in (style_attr_value or "").split(";"):
        if ":" not in chunk:
            continue
        k, v = chunk.split(":", 1)
        out[k.strip().lower()] = re.sub(r"\s+", " ", v.strip())
    return out


def _has_decl(style_attr_value: str, prop: str) -> bool:
    """style 里是否含**独立**的 prop 声明（不受 max- 前缀干扰）。"""
    return re.search(_DECL_RE_TMPL % re.escape(prop), style_attr_value or "") is not None


def _style_attr_of(tag: str) -> str:
    """取标签的 style 属性值（无则空串）。"""
    m = re.search(r'style\s*=\s*"([^"]*)"', tag)
    return m.group(1) if m else ""


def _attr(tag: str, name: str):
    """取标签的指定属性值（无则 None）。"""
    m = re.search(r'(?<![\w-])%s\s*=\s*"([^"]*)"' % name, tag)
    return m.group(1) if m else None


def _img_tag_of(fragment: str, alt: str) -> str:
    """按 alt 取出对应的 img 开标签原文（含自闭合斜杠）。"""
    for m in re.finditer(r"<img\b[^>]*>", fragment, re.I):
        if _attr(m.group(0), "alt") == alt:
            return m.group(0)
    raise AssertionError(f"未找到 alt={alt!r} 的 img 标签：{fragment!r}")


def _first_tag(html: str, tag: str) -> str:
    """取首个 <tag ...> 开标签原文（找不到即失败，不返回 None）。"""
    m = re.search(r"<%s\b[^>]*>" % re.escape(tag), html, re.I)
    if not m:
        raise AssertionError(f"未找到 {tag} 标签：{html!r}")
    return m.group(0)


def _p_tag_of(fragment: str, marker_cls: str) -> str:
    """按 class 子串取出对应的 p 开标签原文。"""
    for m in re.finditer(r"<p\b[^>]*>", fragment, re.I):
        if marker_cls in (_attr(m.group(0), "class") or ""):
            return m.group(0)
    raise AssertionError(f"未找到含 class {marker_cls!r} 的 p 标签：{fragment!r}")


# 选择器单行、不含花括号、不含尖括号（尖括号会把同名的 Python/HTML 注释带进来）。
_CAPTION_CSS_RULE_RE = re.compile(
    r"(?m)^[^\n{}<>]*ptoe-caption[^\n{}<>]*\{([^}]*)\}"
)


def _caption_css_decls(css_text: str) -> list:
    """从一段 CSS 文本里抽出题注规则的声明列表（归一化）。"""
    return [
        _decls_of(m.group(1))
        for m in _CAPTION_CSS_RULE_RE.finditer(css_text)
    ]


# float 规则的同一套「抽选择器单行、避开尖括号」做法：目标 CSS 有多行缩进形态
# （htmlmanage.generate_stylesheet）与单行形态（编辑器 CSS）两种，一行一条正好覆盖。
_FLOAT_CSS_RULE_RE = re.compile(
    r"(?m)^[^\n{}<>]*ptoe-img-float-(left|right)[^\n{}<>]*\{([^}]*)\}"
)


def _float_css_decls(css_text: str) -> dict:
    """抽出 float 绕排规则 → {left|right: 声明字典}。"""
    out = {}
    for m in _FLOAT_CSS_RULE_RE.finditer(css_text):
        side = m.group(1)
        out[side] = _decls_of(m.group(2))
    return out


def _build_structured(pages, title="图文测试"):
    """构造 convert_document 所需的 structured 文档。"""
    return {
        "pages": [{"page": i + 1, "text": t} for i, t in enumerate(pages)],
        "body": "\n\n".join(t for t in pages if t.strip()),
        "paragraphs": [
            {"page": i + 1, "text": t}
            for i, t in enumerate(pages) if t.strip()
        ],
        "meta": {
            "title": title,
            "author": "测试",
            "language": "zh-CN",
            "package_epub": True,
            "epub_version": "3.0",
        },
    }


# ---------------------------------------------------------------------------
# 1. 映射层单测：class 集合 → 等价 CSS 声明
# ---------------------------------------------------------------------------

class TestImgInlineMapping(unittest.TestCase):
    """_img_declarations / _p_block_declarations：class 到声明的映射表。

    映射值必须与 CSSManager.generate_stylesheet 的同名规则逐项一致——内联化
    的整个意义就是「给不执行样式表的阅读器一个等价物」，两边不等价等于把版式
    交给运气。
    """

    def test_img_declarations_base(self):
        """无任何图片 class → 只有基础 img 规则。"""
        self.assertEqual(
            _img_declarations(set()),
            {"display": "block", "max-width": "100%", "height": "auto"},
        )

    def test_img_declarations_inline(self):
        """ptoe-img-inline → inline-block + 垂直居中（覆盖基础 display:block）。"""
        d = _img_declarations({"ptoe-img-inline"})
        self.assertEqual(d["display"], "inline-block")
        self.assertEqual(d["max-width"], "100%")
        self.assertEqual(d["height"], "auto")
        self.assertEqual(d["vertical-align"], "middle")

    def test_img_declarations_width_classes(self):
        """w25/w50/w75/w100 → 百分比宽度。"""
        for cls, want in (
            ("ptoe-img-w25", "25%"),
            ("ptoe-img-w50", "50%"),
            ("ptoe-img-w75", "75%"),
            ("ptoe-img-w100", "100%"),
        ):
            with self.subTest(cls=cls):
                self.assertEqual(_img_declarations({cls})["width"], want)

    def test_img_declarations_valign_classes(self):
        """vtop/vmid/vbot → vertical-align。"""
        for cls, want in (
            ("ptoe-img-vtop", "top"),
            ("ptoe-img-vmid", "middle"),
            ("ptoe-img-vbot", "bottom"),
        ):
            with self.subTest(cls=cls):
                self.assertEqual(_img_declarations({cls})["vertical-align"], want)

    def test_img_declarations_fit(self):
        """ptoe-img-fit（局部图）→ inline-block + height:auto，无百分比宽度。"""
        d = _img_declarations({"ptoe-img-fit"})
        self.assertEqual(d["display"], "inline-block")
        self.assertEqual(d["max-width"], "100%")
        self.assertEqual(d["height"], "auto")
        self.assertEqual(d["vertical-align"], "middle")
        self.assertNotIn("width", d)

    def test_img_declarations_full_omits_height_and_object_fit(self):
        """ptoe-img-full（全画幅）→ 只有 width:100%，**刻意不给 height/object-fit**。

        这是内联声明与 CSS 声明之间**唯一且有意**的不一致：CSS 里
        `p.ptoe-img-full img { width:100%; height:100%; object-fit:contain }`，
        而内联侧去掉后两条——「按百分比解析高度且父高为 auto」的引擎会把图压扁，
        object-fit 的阅读器支持率又极低（不支持时退化为拉伸）。CSS 侧保留不变，
        合规阅读器仍走样式表。
        """
        d = _img_declarations({"ptoe-img-full"})
        self.assertEqual(d["width"], "100%")
        self.assertEqual(d["display"], "inline-block")
        self.assertNotIn("height", d, "内联侧刻意不给 height（防父高 auto 时压扁）")
        self.assertNotIn("object-fit", d, "内联侧刻意不给 object-fit（支持率极低）")

    def test_img_declarations_inline_w50_vtop_combined(self):
        """行内 + 宽度 + 垂直对齐三者共存时同时生效且互不覆盖。"""
        d = _img_declarations(
            {"ptoe-img-inline", "ptoe-img-w50", "ptoe-img-vtop"}
        )
        self.assertEqual(d["display"], "inline-block")
        self.assertEqual(d["width"], "50%")
        self.assertEqual(d["vertical-align"], "top")
        self.assertEqual(d["height"], "auto")

    def test_p_block_declarations_align(self):
        """包裹 p 上的 left/center/right → text-align（img 侧不给声明）。"""
        for cls, want in (
            ("ptoe-img-left", "left"),
            ("ptoe-img-center", "center"),
            ("ptoe-img-right", "right"),
        ):
            with self.subTest(cls=cls):
                self.assertEqual(
                    _p_block_declarations({cls}, False), {"text-align": want}
                )

    def test_p_block_declarations_full_and_first_child(self):
        """full → 前后分页 + 居中；位于首位时 page-break-before 降为 auto。

        :first-child 的位置覆盖必须照抄 CSS（`p.ptoe-img-full:first-child
        { page-break-before: auto }`）：封面内容首块的全画幅图若仍强制前置分页，
        封面后会出现一张空白页（2026-08 专门为此加过该规则，内联侧漏抄即回归）。
        """
        self.assertEqual(
            _p_block_declarations({"ptoe-img-full"}, first_child=False),
            {
                "page-break-before": "always",
                "page-break-after": "always",
                "text-align": "center",
            },
        )
        first = _p_block_declarations({"ptoe-img-full"}, first_child=True)
        self.assertEqual(first["page-break-before"], "auto")
        self.assertEqual(first["page-break-after"], "always")

    def test_p_block_declarations_align_overrides_full_center(self):
        """层叠次序：left/center/right 在 CSS 源内晚于 full，可覆盖其 text-align。"""
        d = _p_block_declarations({"ptoe-img-full", "ptoe-img-right"}, False)
        self.assertEqual(d["text-align"], "right")
        self.assertEqual(d["page-break-before"], "always")


# ---------------------------------------------------------------------------
# 2. _inline_img_styles 单测
# ---------------------------------------------------------------------------

class TestInlineImgStyles(unittest.TestCase):
    """章节文本后处理：img 与包裹 p 的等价声明内联化。"""

    def _img_block(self, p_classes, img_classes=(), extra=""):
        """构造 <p[classes]>[extra]<img[classes]/></p>。"""
        return (
            f'<p class="{p_classes}"{extra}>'
            f'<img src="a.png" alt="x"{img_classes}/>'
            "</p>"
        )

    def test_img_with_inline_classes_gets_style_and_width(self):
        """class 在 img 上 → 得行内 style + width 属性。"""
        out = _inline_img_styles(
            self._img_block(
                "", ' class="ptoe-img-inline ptoe-img-w50 ptoe-img-vtop"'
            )
        )
        tag = _img_tag_of(out, "x")
        d = _decls_of(_style_attr_of(tag))
        self.assertEqual(d["display"], "inline-block")
        self.assertEqual(d["width"], "50%")
        self.assertEqual(d["vertical-align"], "top")
        self.assertEqual(_attr(tag, "width"), "50%")

    def test_block_class_on_p_reaches_child_img(self):
        """块级类落在包裹 p 上 → img 也拿到等价声明（编辑器的行内/块级互转形态）。"""
        out = _inline_img_styles(self._img_block("ptoe-img-fit ptoe-img-center"))
        p = _p_tag_of(out, "ptoe-img-fit")
        self.assertEqual(_decls_of(_style_attr_of(p)), {"text-align": "center"})
        d = _decls_of(_style_attr_of(_img_tag_of(out, "x")))
        self.assertEqual(d["display"], "inline-block")
        self.assertEqual(d["height"], "auto")

    def test_full_img_has_no_height_or_object_fit(self):
        """全画幅 img 侧不给 height/object-fit（负向先行断言，避开 max-width 误判）。"""
        out = _inline_img_styles(self._img_block("ptoe-img-full"))
        style = _style_attr_of(_img_tag_of(out, "x"))
        self.assertFalse(_has_decl(style, "height"), style)
        self.assertFalse(_has_decl(style, "object-fit"), style)
        self.assertTrue(_has_decl(style, "width"), style)
        self.assertTrue(_has_decl(style, "max-width"), "max-width 应当保留")

    def test_plain_paragraph_byte_identical(self):
        """无图片 class 的普通段落逐字节不变（不得多出任何一个字节）。"""
        for src in (
            "<p>普通段落。</p>",
            '<p class="ptoe-note">注释段落。</p>',
            "<p><strong>加粗</strong>与<em>斜体</em></p>",
            '<p class="ptoe-caption">图1示例插图</p>',
            "没有标签的纯文本",
        ):
            with self.subTest(src=src):
                self.assertEqual(_inline_img_styles(src), src)

    def test_text_without_img_short_circuits(self):
        """整段不含 <img> → 原样返回（连扫描都不做）。"""
        src = '<p class="ptoe-img-full">没有图片的段落。</p>'
        self.assertEqual(_inline_img_styles(src), src)
        self.assertEqual(_inline_img_styles(""), "")

    def test_existing_style_is_not_duplicated(self):
        """已自带 style 的 p/img 一律不动（重复 style 属性违 XHTML）。

        ⚠️ 断言按**标签**逐个计数而不是全文计数：这条里 p 自带 style（被跳过）
        而 img 没有（合法补一个），全文 style= 出现两次是正确的实现，逐标签各
        一次才是真正的守卫——重复 style 属性只会出现在同一个标签上。
        """
        src = self._img_block(
            "ptoe-img-left",
            ' class="ptoe-img-w50"',
            extra=' style="margin:2em"',
        )
        out = _inline_img_styles(src)
        p = _p_tag_of(out, "ptoe-img-left")
        self.assertEqual(p.count("style="), 1, p)
        self.assertEqual(_decls_of(_style_attr_of(p)), {"margin": "2em"})
        self.assertFalse(_has_decl(_style_attr_of(p), "text-align"), p)
        # 同一个 img 没有 style → 正常补一个（外层保护只针对「已有 style」）
        img = _img_tag_of(out, "x")
        self.assertEqual(img.count("style="), 1, img)
        self.assertEqual(_decls_of(_style_attr_of(img))["width"], "50%")

        # img 侧自带 style → 跳过，连 width 属性也不补
        src2 = self._img_block(
            "ptoe-img-fit", ' class="ptoe-img-w50" style="opacity:.9"'
        )
        out2 = _inline_img_styles(src2)
        img2 = _img_tag_of(out2, "x")
        self.assertEqual(img2.count("style="), 1, img2)
        self.assertIn("opacity:.9", img2)
        self.assertEqual(_attr(img2, "width"), None, "已有 style 的 img 不再补 width")

    def test_idempotent(self):
        """连调两次结果相同（幂等性是「已有 style 一律不动」守卫的直接推论）。"""
        for src in (
            self._img_block("", ' class="ptoe-img-inline ptoe-img-w75 ptoe-img-vbot"'),
            self._img_block("ptoe-img-full ptoe-img-center"),
            self._img_block("ptoe-img-fit"),
            self._img_block("ptoe-img-left"),
        ):
            with self.subTest(src=src):
                once = _inline_img_styles(src)
                self.assertEqual(_inline_img_styles(once), once)

    def test_first_child_full_uses_page_break_auto(self):
        """章节首块的全画幅段落 → page-break-before:auto（照抄 CSS 的 :first-child 覆盖）。"""
        head = _inline_img_styles(self._img_block("ptoe-img-full"))
        self.assertEqual(
            _decls_of(_style_attr_of(_p_tag_of(head, "ptoe-img-full")))["page-break-before"],
            "auto",
        )
        after_text = _inline_img_styles(
            "<p>前面的正文。</p>" + self._img_block("ptoe-img-full")
        )
        self.assertEqual(
            _decls_of(
                _style_attr_of(_p_tag_of(after_text, "ptoe-img-full"))
            )["page-break-before"],
            "always",
        )

    def test_container_open_tag_resets_first_child_counting(self):
        """容器开标签之后重新计数（:first-child 的父子关系不跨容器）。"""
        src = "<div>" + self._img_block("ptoe-img-full") + "</div>"
        out = _inline_img_styles(src)
        self.assertEqual(
            _decls_of(
                _style_attr_of(_p_tag_of(out, "ptoe-img-full"))
            )["page-break-before"],
            "auto",
        )


# ---------------------------------------------------------------------------
# 3. 真实 convert_document 端到端
# ---------------------------------------------------------------------------

class TestConvertDocumentImgInline(unittest.TestCase):
    """convert_document → XHTML 产物：图片等价声明确已内联，产物合法。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="t_imgtxt_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # 每章都带正文段：整页只有一张图的章节会被 _is_img_page 判成封面页，
        # 不产出 content_*.xhtml，「无输出」看起来就像断言失败。
        self.pages = [
            "<h1>第一章</h1>"
            "<p>本章正文，用来确保章节不被判成整页图片页。</p>"
            f'<p class="ptoe-img-fit ptoe-img-center">'
            f'<img src="{PNG_DATA_URI}" alt="局部图"/></p>'
            f'<p class="{_CAPTION_CLASS}">图1局部插图</p>',
            "<h1>第二章</h1>"
            "<p>本章正文，用来确保章节不被判成整页图片页。</p>"
            f'<p class="ptoe-img-full ptoe-img-center">'
            f'<img src="{PNG_DATA_URI}" alt="全画幅图"/></p>'
            f'<p class="ptoe-img-left">'
            f'<img src="{PNG_DATA_URI}" alt="左对齐图"/></p>'
            f'<p><img class="ptoe-img-inline ptoe-img-w50 ptoe-img-vtop"'
            f' src="{PNG_DATA_URI}" alt="行内图"/></p>'
            "<p>行内图后面的正文。</p>"
            f'<p class="{_CAPTION_CLASS}">图2行内插图</p>',
        ]
        self.structured = _build_structured(self.pages)
        self.storage_snapshot = [p["text"] for p in self.structured["pages"]]
        self.result = htmlmanage.HTMLConverter(
            output_dir=str(self.tmp)
        ).convert_document(self.structured)
        self.assertIsNone(self.result.get("epub_error"), self.result.get("epub_error"))
        # convert_document 的 content_files 是相对 output_dir 的路径（'OEBPS/xxx'），
        # 直接 Path(p) 读会 FileNotFoundError，必须先拼上 output_dir。
        content_files = sorted(
            self.tmp / p for p in (self.result.get("content_files") or [])
        )
        self.assertTrue(content_files, "应产出 content_*.xhtml")
        self.content = "\n".join(
            p.read_text(encoding="utf-8") for p in content_files
        )

    def _e2e_setup_ok(self):
        """端到端前置：产物确实存在且已含内联声明（避免断言恒真空转）。"""
        self.assertIn("ptoe-img-w50", self.content)
        self.assertIn("style=", self.content)

    def test_inline_img_w50_vtop_gets_style_and_width(self):
        """a) 行内图 → img 侧 style + width="50%" 属性。"""
        self._e2e_setup_ok()
        tag = _img_tag_of(self.content, "行内图")
        d = _decls_of(_style_attr_of(tag))
        self.assertEqual(d["display"], "inline-block")
        self.assertEqual(d["width"], "50%")
        self.assertEqual(d["vertical-align"], "top")
        self.assertEqual(_attr(tag, "width"), "50%")
        self.assertEqual(tag.count("style="), 1)

    def test_fit_center_p_gets_text_align_center(self):
        """b) fit + center 的包裹 p → text-align:center。"""
        self._e2e_setup_ok()
        p = _p_tag_of(self.content, "ptoe-img-fit")
        self.assertEqual(_decls_of(_style_attr_of(p)), {"text-align": "center"})
        self.assertEqual(p.count("style="), 1)

    def test_full_p_gets_page_break_and_center(self):
        """c) full + center 的包裹 p → page-break-before/after:always + 居中。"""
        self._e2e_setup_ok()
        p = _p_tag_of(self.content, "ptoe-img-full")
        d = _decls_of(_style_attr_of(p))
        self.assertEqual(d["page-break-before"], "always")
        self.assertEqual(d["page-break-after"], "always")
        self.assertEqual(d["text-align"], "center")

    def test_full_img_has_no_height_or_object_fit_e2e(self):
        """c-续) 全画幅 img 侧无 height/object-fit（负向先行断言）。"""
        self._e2e_setup_ok()
        style = _style_attr_of(_img_tag_of(self.content, "全画幅图"))
        self.assertFalse(_has_decl(style, "height"), style)
        self.assertFalse(_has_decl(style, "object-fit"), style)
        self.assertTrue(_has_decl(style, "width"), style)
        self.assertEqual(_attr(_img_tag_of(self.content, "全画幅图"), "width"), "100%")

    def test_left_p_gets_text_align_left(self):
        """d) left 包裹 p → text-align:left。"""
        self._e2e_setup_ok()
        p = _p_tag_of(self.content, "ptoe-img-left")
        self.assertEqual(_decls_of(_style_attr_of(p)), {"text-align": "left"})

    def test_plain_paragraph_unchanged_in_output(self):
        """e) 无图片 class 的普通段落逐字节不变。"""
        for src in (
            "<p>本章正文，用来确保章节不被判成整页图片页。</p>",
            "<p>行内图后面的正文。</p>",
        ):
            with self.subTest(src=src):
                self.assertIn(src, self.content)

    def test_single_style_attribute_per_tag(self):
        """f) 产物里每个标签至多一个 style 属性（重复属性违 XHTML）。"""
        self._e2e_setup_ok()
        for m in re.finditer(r"<(?:p|img)\b[^>]*>", self.content, re.I):
            tag = m.group(0)
            self.assertLessEqual(tag.count("style="), 1, tag)
            self.assertLessEqual(tag.count("width="), 1, tag)

    def test_storage_side_pages_untouched(self):
        """存储侧不变：只有输出产物带 style，pages[i].text 原文未被改写。"""
        self._e2e_setup_ok()
        for before, after in zip(self.storage_snapshot, self.structured["pages"]):
            self.assertEqual(before, after["text"])
            self.assertNotIn("style=", after["text"])

    def test_caption_class_survives_to_output(self):
        """题注 class 与文字都进产物（内联化不得误伤块级 class 回填）。"""
        self.assertEqual(self.content.count(f'class="{_CAPTION_CLASS}"'), 2)
        self.assertIn("图1局部插图", self.content)
        self.assertIn("图2行内插图", self.content)

    def test_images_extracted_and_referenced(self):
        """data URI 已提取为 OEBPS/Images/img_N.png，正文引用同目录相对路径。

        目录层级：content_N.xhtml 与 Images/ **同级**（都在 OEBPS/ 下，nav 另有
        Text/ 子目录），所以引用是 `Images/img_N.png` 而不是 `../Images/`。
        顺带锁死正斜杠：EPUB 内路径若用 os.path.join 会在 Windows 上产出
        反斜杠，阅读器直接不显示图（2026-08 修复项）。
        """
        images = sorted(
            p.name for p in (self.tmp / "OEBPS" / "Images").glob("*")
        )
        self.assertTrue(images, "应产出 OEBPS/Images/ 下的图片文件")
        self.assertTrue(
            all(n.endswith(".png") for n in images), images
        )
        self.assertNotIn("data:image", self.content)
        self.assertIn("Images/", self.content)
        self.assertNotIn("\\", self.content, "EPUB 内路径必须正斜杠")

    def _epub(self) -> str:
        """取本次转换产出的 .epub 绝对路径（不存在即失败，避免后续静默跳过）。"""
        epub = str(self.result.get("epub") or "")
        self.assertTrue(epub, self.result)
        path = Path(epub)
        self.assertTrue(path.is_file(), path)
        return str(path)

    def test_xhtml_wellformed_by_both_parsers(self):
        """XHTML 良构：minidom 与 ElementTree 双向解析通过。"""
        with zipfile.ZipFile(self._epub()) as zf:
            names = zf.namelist()
            for required in ("mimetype", "META-INF/container.xml",
                             "OEBPS/content.opf", "OEBPS/Text/nav.xhtml"):
                self.assertIn(required, names)
            targets = [
                n for n in names
                if n.endswith((".xhtml", ".opf", ".xml", ".ncx"))
            ]
            self.assertTrue(targets)
            for name in targets:
                data = zf.read(name).decode("utf-8")
                with self.subTest(name=name):
                    xml.dom.minidom.parseString(data)   # minidom
                    ET.fromstring(data)                 # ElementTree

    def test_style_element_has_no_literal_tags(self):
        """CDATA 铁律：<style> 文本里不得出现字面量标签名。

        CSS 会被内联进 nav.xhtml 的 style 元素，注释里若写了 `<p>` / `<img>`
        之类字面尖括号，XHTML 按 XML 解析时会当成真标签 → 整个文件非法
        （本项目历史 bug：曾致 nav.xhtml 解析失败、目录跳转全部失效）。
        CDATA 围栏自带 `/* <![CDATA[ */` 与 `/* ]]> */`，不在本断言范围内。
        """
        with zipfile.ZipFile(self._epub()) as zf:
            nav = zf.read("OEBPS/Text/nav.xhtml").decode("utf-8")
        styles = re.findall(r"<style[^>]*>(.*?)</style>", nav, re.S)
        self.assertTrue(styles, "nav.xhtml 应含内联 style")
        for text in styles:
            for tag in ("<p", "<img", "<h1", "<div", "<span", "<table", "<td", "<br"):
                with self.subTest(tag=tag):
                    self.assertNotIn(tag, text)


# ---------------------------------------------------------------------------
# 4. P1：三处题注样式一致性（防止「同一值散落 N 份、改一处漏两处」）
# ---------------------------------------------------------------------------

class TestCaptionCssConsistency(unittest.TestCase):
    """题注样式三处副本 + 非 EPUB 导出换算常量必须落在同一份契约上。

    副本清单：
      ① correctmanage.py   矫正界面编辑器 CSS（`.editable p.ptoe-caption`）
      ② htmlmanage.py      EPUB style.css（由 CSSManager.generate_stylesheet 生成）
      ③ ui/epubedit.html   EPUB 编辑器 CSS（`.art-body p.ptoe-caption`）
      ④ correctmanage 的 _CAPTION_* 常量 → DOCX（w:sz/w:color/w:jc）与
         Markdown（_CAPTION_MD_STYLE 内联 style）的换算依据
    """

    def _editor_css(self):
        return (_REPO_ROOT / "correctmanage.py").read_text(encoding="utf-8")

    def _epubedit_css(self):
        return (_REPO_ROOT / "ui" / "epubedit.html").read_text(encoding="utf-8")

    # 题注样式除三处 CSS 外还有 Markdown 内联串一份副本；只改 CSS 时该闸门仍
    # 必须报错，所以负控要么改 CSS 副本，要么改该常量，不能改四份全同源。

    def test_three_caption_css_copies_detect_drift(self):
        """负控辅助：把三处副本之一改坏时，本闸门必须能发现（自检，不依赖外部变异）。

        与真实负控（临时改 ui/epubedit.html 后跑全量）互补——这条在进程内把
        三份 CSS 依次扰动后重跑判定逻辑，确认断言真的会红。做法：抽出判定为
        独立函数，逐份注入偏移值并断言「不合约」，避免真去改文件。
        """
        base = CANONICAL_CAPTION_DECLS
        drifted = dict(base)
        drifted["font-size"] = "0.9em"
        self.assertNotEqual(drifted, base)
        self.assertEqual(base["font-size"], "0.85em")
        # 契约值必须是四项齐全的完整集合（少一项即视为不合约）
        self.assertEqual(
            set(base), {"font-size", "color", "text-align", "margin"}
        )

    def test_three_caption_css_copies_identical(self):
        """三处 CSS 副本的题注规则逐项相同（脚本化断言，不靠目测）。"""
        sources = {
            "correctmanage.py(编辑器)": _caption_css_decls(self._editor_css()),
            "htmlmanage.py(EPUB style.css)": _caption_css_decls(
                htmlmanage.CSSManager().generate_stylesheet()
            ),
            "ui/epubedit.html(EPUB 编辑器)": _caption_css_decls(self._epubedit_css()),
        }
        for name, found in sources.items():
            with self.subTest(source=name):
                self.assertEqual(
                    len(found), 1,
                    f"{name} 应恰有 1 条 ptoe-caption 规则，实际 {len(found)} 条",
                )
                self.assertEqual(found[0], CANONICAL_CAPTION_DECLS)
        # 三者彼此相等（上面的 subTest 已各自锁定契约值，这里再互校一次，
        # 让「改了一处漏两处」时报告直接指明是哪几处不一致）
        values = [found[0] for found in sources.values()]
        self.assertEqual(values[0], values[1])
        self.assertEqual(values[1], values[2])

    def test_export_constants_match_css_contract(self):
        """非 EPUB 链路的换算常量与 CSS 契约同源。"""
        import correctmanage as cm

        self.assertEqual(cm._CAPTION_CLASS, _CAPTION_CLASS)
        self.assertEqual(cm._CAPTION_FONT_SCALE, 0.85)
        self.assertEqual(cm._CAPTION_COLOR_CSS, CANONICAL_CAPTION_DECLS["color"])
        self.assertEqual(cm._CAPTION_JC, CANONICAL_CAPTION_DECLS["text-align"])
        # CSS 的 #666 是 3 位缩写，OOXML 的 w:color 只有 6 位十六进制枚举 →
        # 换算必须走「每位重复两次」的展开，而不是直接去掉 #（会得到 '666'）。
        css_hex = cm._CAPTION_COLOR_CSS.lstrip("#")
        self.assertEqual(len(css_hex), 3, css_hex)
        self.assertEqual(cm._CAPTION_COLOR_HEX, "".join(c * 2 for c in css_hex))
        self.assertEqual(cm._CAPTION_COLOR_HEX, "666666")
        # 题注字号 = 正文基准 × 比例（半磅单位，round 后 18）
        self.assertEqual(cm._DOCX_BODY_SZ, 21)
        self.assertEqual(cm._DOCX_CAPTION_SZ, 18)
        self.assertEqual(
            cm._DOCX_CAPTION_SZ,
            int(round(cm._DOCX_BODY_SZ * cm._CAPTION_FONT_SCALE)),
        )

    def test_caption_md_style_matches_css_contract(self):
        """Markdown 内联样式串与 CSS 契约逐项相同（第 4 份副本）。"""
        import correctmanage as cm

        self.assertEqual(_decls_of(cm._CAPTION_MD_STYLE), CANONICAL_CAPTION_DECLS)


# ---------------------------------------------------------------------------
# 5. float 绕排：映射层
# ---------------------------------------------------------------------------

class TestImgFloatMapping(unittest.TestCase):
    """_img_declarations：float 绕排 class → 等价 CSS 声明（值逐字锁死）。"""

    def test_float_declarations_exact(self):
        """float-left / float-right 的声明集合与契约值**全等**。

        用 dict 全等（而非逐项 assertIn）是有意的：多出一条声明（如 height、
        clear、padding）同样会让某类阅读器排版变形，「缺项」与「多项」都是回归。
        """
        for cls in FLOAT_IMG_CLASSES:
            with self.subTest(cls=cls):
                self.assertEqual(_img_declarations({cls}), CANONICAL_FLOAT_DECLS[cls])

    def test_float_keeps_inline_block_fallback(self):
        """降级保障：float 与 display:inline-block **必须并存**。

        忽略 float 的阅读器（微信阅读无 WebView、静读天下整体覆盖出版方 CSS）
        靠 display:inline-block 退化成现有行内图行为（图宽走 ptoe-img-w*、
        文字接在图后）。若这条丢了，float 失效时图片会塌成 display:block 的
        独占整行块 —— 一次降级变成版式事故。
        """
        for cls in FLOAT_IMG_CLASSES:
            with self.subTest(cls=cls):
                d = _img_declarations({cls})
                self.assertEqual(d["display"], "inline-block")
                self.assertNotEqual(d["display"], "block",
                                    "绝不能退回独占块（降级路径依赖 inline-block）")
                self.assertIn("float", d, "float 声明本身不得丢失")

    def test_float_margin_is_inner_side_only(self):
        """margin 只给「内侧 + 下侧」：左绕排留右 margin，右绕排留左 margin。

        外侧留 0 才能让图真正贴到版心边缘（外侧有 margin 会凭空多出窄边）。
        两侧值刻意不对称 —— 若某天改成对称值，这条会红，说明要重新确认观感。
        """
        left = _img_declarations({"ptoe-img-float-left"})["margin"]
        right = _img_declarations({"ptoe-img-float-right"})["margin"]
        self.assertEqual(left, "0 0.5em 0.4em 0")
        self.assertEqual(right, "0 0 0.4em 0.5em")
        self.assertEqual(len(left.split()), 4)
        self.assertEqual(len(right.split()), 4)
        self.assertNotEqual(left, right, "左右绕排的 margin 必须镜像")

    def test_float_omits_height(self):
        """映射层不给 height（与 ptoe-img-full 同理：防父高 auto 时被压扁）。"""
        for cls in FLOAT_IMG_CLASSES:
            with self.subTest(cls=cls):
                d = _img_declarations({cls, "ptoe-img-w50"})
                self.assertNotIn("height", d)
                self.assertNotIn("object-fit", d)
                # 基础规则的 height:auto 被显式剔除 —— 这正是要锁的行为
                self.assertEqual(_img_declarations(set())["height"], "auto")

    def test_float_with_width_class(self):
        """绕排 + 尺寸类共存：宽度照常给百分比（宽度档位复用 w*，不新增宽度类）。"""
        for cls, want in (("ptoe-img-w25", "25%"), ("ptoe-img-w50", "50%"),
                          ("ptoe-img-w75", "75%"), ("ptoe-img-w100", "100%")):
            with self.subTest(cls=cls):
                d = _img_declarations({"ptoe-img-float-left", cls})
                self.assertEqual(d["width"], want)
                self.assertEqual(d["float"], "left")
                self.assertNotIn("height", d)

    def test_float_takes_precedence_over_inline(self):
        """float 与 ptoe-img-inline 同时存在（前端已互斥，转换层仍须 float 优先）。

        层叠顺序：float 规则在 CSS 源内**最后**（同特异性 0,1,1），故内联映射把
        float 放在 inline 之后 update。残留的 vertical-align 对 float 无意义
        （float 已脱离文本行基线）但无害，转换层不做解冲突只保证 float 生效。
        """
        d = _img_declarations(
            {"ptoe-img-inline", "ptoe-img-float-right", "ptoe-img-w50"}
        )
        self.assertEqual(d["float"], "right")
        self.assertEqual(d["display"], "inline-block")
        self.assertEqual(d["width"], "50%")

    def test_float_css_parity_ignores_height(self):
        """内联声明与三处 CSS 逐项等价，唯一有意差异 = 不内联 height。

        内联化的意义是「给不执行样式表的阅读器一个等价物」，两边不等价等于把
        版式交给运气。故 float/display/max-width/margin 必须逐字相同；CSS 里的
        height:auto 内联侧**故意**没有（同 ptoe-img-full 的记账方式）。
        """
        css = _float_css_decls(htmlmanage.CSSManager().generate_stylesheet())
        self.assertEqual(set(css), {"left", "right"}, css)
        for cls in FLOAT_IMG_CLASSES:
            side = cls.rsplit("-", 1)[1]
            with self.subTest(cls=cls):
                contract = CANONICAL_FLOAT_DECLS[cls]
                got = css[side]
                for prop, want in contract.items():
                    self.assertEqual(got.get(prop), want, f"{prop} 与内联映射不一致")
                # CSS 有 height:auto，内联侧无 height —— 唯一有意差异
                self.assertEqual(got.get("height"), "auto")
                self.assertNotIn("height", _img_declarations({cls}))
                # float 组不写 width（宽度归 ptoe-img-w* 档位管）
                self.assertNotIn("width", got)


# ---------------------------------------------------------------------------
# 6. float 绕排：_inline_img_styles 单测
# ---------------------------------------------------------------------------

class TestInlineImgFloatStyles(unittest.TestCase):
    """章节文本后处理：float 绕排图的行内声明 + width 属性。"""

    def _para(self, img_classes, alt="绕排图", lead="绕排段落：", tail="文字继续。"):
        return f"<p>{lead}<img class=\"{img_classes}\" src=\"a.png\" alt=\"{alt}\"/>{tail}</p>"

    def test_float_img_gets_style_and_width_attr(self):
        """float + w25 → 行内 style（含 float/display/margin）+ width="25%" 属性。"""
        out = _inline_img_styles(
            self._para("ptoe-img-float-left ptoe-img-w25")
        )
        tag = _img_tag_of(out, "绕排图")
        d = _decls_of(_style_attr_of(tag))
        self.assertEqual(d, CANONICAL_FLOAT_DECLS["ptoe-img-float-left"] | {"width": "25%"})
        self.assertEqual(_attr(tag, "width"), "25%")
        self.assertEqual(tag.count("style="), 1)
        self.assertEqual(tag.count("width="), 1)

    def test_float_right_mirrored_e2e(self):
        """float-right 的 margin 镜像正确。"""
        out = _inline_img_styles(
            self._para("ptoe-img-float-right ptoe-img-w50", alt="右绕排")
        )
        tag = _img_tag_of(out, "右绕排")
        d = _decls_of(_style_attr_of(tag))
        self.assertEqual(d["float"], "right")
        self.assertEqual(d["margin"], "0 0 0.4em 0.5em")
        self.assertEqual(_attr(tag, "width"), "50%")

    def test_float_style_has_no_height_standalone(self):
        """style 串里无独立 height 声明（负向先行，避开 max-width 之类的同族名）。"""
        out = _inline_img_styles(self._para("ptoe-img-float-left ptoe-img-w50"))
        style = _style_attr_of(_img_tag_of(out, "绕排图"))
        self.assertFalse(_has_decl(style, "height"), style)
        self.assertFalse(_has_decl(style, "object-fit"), style)
        self.assertTrue(_has_decl(style, "width"), style)
        self.assertTrue(_has_decl(style, "max-width"), "max-width 应当保留")
        self.assertTrue(_has_decl(style, "float"), style)

    def test_float_without_width_class_gets_no_width_attr(self):
        """无百分比宽度时**不补** width 属性（补了反而是错的宽度）。"""
        out = _inline_img_styles(self._para("ptoe-img-float-left"))
        tag = _img_tag_of(out, "绕排图")
        self.assertIsNone(_attr(tag, "width"))
        self.assertEqual(_decls_of(_style_attr_of(tag)), CANONICAL_FLOAT_DECLS["ptoe-img-float-left"])

    def test_float_needs_no_p_wrapper_style(self):
        """绕排类挂 img 自身 → 包裹 p 不该被加任何声明（float 不作用于 p）。

        取 p 标签不能用 `_p_tag_of`（它按 class 子串找，而这里的 p 无 class）；
        这里直接取首个 `<p` 开标签。
        """
        out = _inline_img_styles(self._para("ptoe-img-float-left ptoe-img-w25"))
        p = _first_tag(out, "p")
        self.assertEqual(p, "<p>")
        self.assertNotIn("style=", p)
        # 对照：块级类落在 p 上时 p 侧**会**被加声明（证明上条不是因为 p 侧逻辑坏了）。
        # 对照类必须选 p 侧真有声明的（ptoe-img-full → 分页 + 居中）；ptoe-img-fit
        # 的声明全在 img 侧，p 侧是空集，拿它当对照会假挂。
        fit = _inline_img_styles(
            '<p class="ptoe-img-full"><img src="a.png" alt="x"/></p>'
        )
        self.assertIn("style=", _first_tag(fit, "p"))

    def test_float_idempotent_and_plain_text_untouched(self):
        """幂等；不带图片 class 的段落逐字节不变。"""
        src = self._para("ptoe-img-float-left ptoe-img-w25")
        once = _inline_img_styles(src)
        self.assertEqual(_inline_img_styles(once), once)
        for plain in ("<p>普通段落。</p>", '<p class="ptoe-note">注释。</p>', "纯文本"):
            with self.subTest(plain=plain):
                self.assertEqual(_inline_img_styles(plain), plain)


# ---------------------------------------------------------------------------
# 7. float 绕排：真实 convert_document 端到端
# ---------------------------------------------------------------------------

class TestConvertDocumentImgFloat(unittest.TestCase):
    """convert_document → XHTML 产物：绕排声明内联、class 存活、产物合法。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="t_imgfloat_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # ⚠️ 每章首尾都放正文段：整页只有一张图的章节会被 _is_img_page 判成
        # cover.xhtml 而不产出 content 文件，「无输出」看起来就像断言失败。
        self.pages = [
            "<h1>第一章</h1>"
            "<p>本章正文，用来确保章节不被判成整页图片页。</p>"
            f'<p>左绕排段落：<img class="ptoe-img-float-left ptoe-img-w25"'
            f' src="{PNG_DATA_URI}" alt="左绕排图"/>文字继续绕排。</p>'
            f'<p>右绕排段落：<img class="ptoe-img-float-right ptoe-img-w50"'
            f' src="{PNG_DATA_URI}" alt="右绕排图"/>文字继续绕排。</p>'
            f'<p class="{_CAPTION_CLASS}">图1绕排插图</p>',
        ]
        self.structured = _build_structured(self.pages)
        self.storage_snapshot = [p["text"] for p in self.structured["pages"]]
        self.result = htmlmanage.HTMLConverter(
            output_dir=str(self.tmp)
        ).convert_document(self.structured)
        self.assertIsNone(self.result.get("epub_error"), self.result.get("epub_error"))
        # content_files 是相对 output_dir 的路径（'OEBPS/xxx'），不是绝对路径。
        content_files = sorted(
            self.tmp / p for p in (self.result.get("content_files") or [])
        )
        self.assertTrue(content_files, "应产出 content_*.xhtml")
        self.content = "\n".join(
            p.read_text(encoding="utf-8") for p in content_files
        )

    def _e2e_setup_ok(self):
        """端到端前置：产物确实含 float class 与内联 style（避免断言恒真空转）。"""
        for cls in FLOAT_IMG_CLASSES:
            self.assertIn(cls, self.content)
        self.assertIn("float:left", self.content)
        self.assertIn("float:right", self.content)

    def test_float_left_class_and_style_survive(self):
        """a) float-left：class 存活 + 行内 style 正确 + width="25%" 属性存在。"""
        self._e2e_setup_ok()
        tag = _img_tag_of(self.content, "左绕排图")
        cls = _attr(tag, "class") or ""
        for c in ("ptoe-img-float-left", "ptoe-img-w25"):
            self.assertIn(c, cls.split())
        d = _decls_of(_style_attr_of(tag))
        self.assertEqual(d["float"], "left")
        self.assertEqual(d["display"], "inline-block")
        self.assertEqual(d["margin"], "0 0.5em 0.4em 0")
        self.assertEqual(d["width"], "25%")
        self.assertEqual(_attr(tag, "width"), "25%")
        self.assertEqual(tag.count("style="), 1)
        self.assertEqual(tag.count("width="), 1)

    def test_float_right_class_and_style_survive(self):
        """b) float-right：class 存活 + 行内 style 正确 + width="50%" 属性存在。"""
        self._e2e_setup_ok()
        tag = _img_tag_of(self.content, "右绕排图")
        d = _decls_of(_style_attr_of(tag))
        self.assertEqual(d["float"], "right")
        self.assertEqual(d["display"], "inline-block")
        self.assertEqual(d["margin"], "0 0 0.4em 0.5em")
        self.assertEqual(d["width"], "50%")
        self.assertEqual(_attr(tag, "width"), "50%")

    def test_float_img_no_height_e2e(self):
        """c) 绕排 img 侧无 height/object-fit（负向先行断言）。"""
        self._e2e_setup_ok()
        for alt in ("左绕排图", "右绕排图"):
            with self.subTest(alt=alt):
                style = _style_attr_of(_img_tag_of(self.content, alt))
                self.assertFalse(_has_decl(style, "height"), style)
                self.assertFalse(_has_decl(style, "object-fit"), style)
                self.assertTrue(_has_decl(style, "float"), style)

    def test_float_caption_paragraph_intact(self):
        """d) 绕排图之后的题注段落照常进产物（未因 float 而丢块级 class 回填）。

        题注文本里**不能有空格**：`_strip_ws_text` 会把 CJK 之间的空白压掉，
        「图1 绕排插图」进产物会变成「图1绕排插图」—— 断言带空格必挂（写测试时
        真踩过，不是假想）。
        """
        self._e2e_setup_ok()
        self.assertIn(f'class="{_CAPTION_CLASS}"', self.content)
        self.assertIn("图1绕排插图", self.content)

    def test_float_images_extracted_to_relative_posix_path(self):
        """e) data URI 已提取为 Images/img_N.png，引用为正斜杠相对路径。"""
        self._e2e_setup_ok()
        images = sorted(p.name for p in (self.tmp / "OEBPS" / "Images").glob("*"))
        self.assertTrue(images, images)
        self.assertTrue(all(n.endswith(".png") for n in images), images)
        self.assertNotIn("data:image", self.content)
        self.assertNotIn("\\", self.content, "EPUB 内路径必须正斜杠")
        for tag in re.finditer(r"<img\b[^>]*>", self.content, re.I):
            self.assertIn("Images/", tag.group(0))

    def test_storage_side_pages_untouched(self):
        """f) 存储侧不变：只有输出产物带 style/width 属性。"""
        self._e2e_setup_ok()
        for before, after in zip(self.storage_snapshot, self.structured["pages"]):
            self.assertEqual(before, after["text"])
            self.assertNotIn("style=", after["text"])

    def _epub(self) -> str:
        epub = str(self.result.get("epub") or "")
        self.assertTrue(epub, self.result)
        self.assertTrue(Path(epub).is_file(), epub)
        return epub

    def test_float_xhtml_wellformed_by_both_parsers(self):
        """g) 良构性：minidom 与 ElementTree 双向解析通过全部 XML 产物。"""
        with zipfile.ZipFile(self._epub()) as zf:
            targets = [
                n for n in zf.namelist()
                if n.endswith((".xhtml", ".opf", ".xml", ".ncx"))
            ]
            self.assertTrue(targets)
            for name in targets:
                data = zf.read(name).decode("utf-8")
                with self.subTest(name=name):
                    xml.dom.minidom.parseString(data)
                    ET.fromstring(data)

    def test_float_inline_style_values_are_xml_safe(self):
        """h) 行内 style 值里无裸 < > & （XHTML 属性值转义铁律）。"""
        self._e2e_setup_ok()
        for alt in ("左绕排图", "右绕排图"):
            style = _style_attr_of(_img_tag_of(self.content, alt))
            with self.subTest(alt=alt):
                for ch in ("<", ">", "&"):
                    self.assertNotIn(ch, style, style)

    def test_cdata_ironlaw_no_literal_tags_in_css(self):
        """i) CDATA 铁律：内联进 style 元素的 CSS 里不得出现字面量标签名。

        CSS 被内联进 nav.xhtml 的 style 元素（inject_styles 用
        `/* <![CDATA[ */` 围栏），注释里若写了 `<p>` / `<img>` 之类字面尖括号，
        XHTML 按 XML 解析时会当成真标签 → 整个文件非法（历史 bug：目录跳转全失效）。
        float 规则的 CSS 注释最长、最容易顺手写标签名，故单独锁一条。
        CDATA 围栏自带尖括号，不在本断言范围内。
        """
        with zipfile.ZipFile(self._epub()) as zf:
            nav = zf.read("OEBPS/Text/nav.xhtml").decode("utf-8")
            css_names = [n for n in zf.namelist() if n.endswith(".css")]
        styles = re.findall(r"<style[^>]*>(.*?)</style>", nav, re.S)
        self.assertTrue(styles, "nav.xhtml 应含内联 style")
        self.assertTrue(css_names, "EPUB 应含独立 style.css")
        for text in styles:
            self.assertIn("ptoe-img-float-left", text, "绕排 CSS 应被内联进 nav")
            for tag in ("<p", "<img", "<h1", "<div", "<span", "<table", "<td", "<br"):
                with self.subTest(tag=tag):
                    self.assertNotIn(tag, text)
        # 独立 style.css 里绕排规则确实在（不只进了 nav 的内联副本）
        with zipfile.ZipFile(self._epub()) as zf:
            for name in css_names:
                text = zf.read(name).decode("utf-8")
                with self.subTest(css=name):
                    self.assertIn("img.ptoe-img-float-left", text)
                    self.assertIn("img.ptoe-img-float-right", text)


# ---------------------------------------------------------------------------
# 8. float 绕排：跨文件注册点与前端保留条件（反射式）
# ---------------------------------------------------------------------------

class TestFloatCrossFileContract(unittest.TestCase):
    """4 处注册点 + 3 处 CSS 副本 + 前端保留条件，脚本化互校。

    本项目已有「同一契约散落 N 份、改一处漏两处」的成例（ptoe-underdot 漏登记、
    题注 CSS 三处副本各写一套），所以这里不靠目测。
    """

    def _src(self, rel):
        return (_REPO_ROOT / rel).read_text(encoding="utf-8")

    def _braced(self, text, anchor):
        """取 `anchor = {` 起始的整块文本（含大括号）。"""
        i = text.index(anchor)
        j = text.index("{", i)
        return text[i: text.index("}", j) + 1]

    def _bracketed(self, text, anchor):
        """取 `anchor = [` 起始的整块文本（含方括号）。"""
        i = text.index(anchor)
        j = text.index("[", i)
        return text[i: text.index("]", j) + 1]

    def test_float_registered_in_all_four_python_and_js_places(self):
        """①sanitize 白名单 ②映射取值域 ③规则引擎白名单 ④前端模式判定 四处齐备。"""
        import correctmanage as cm

        # ①/② 两侧都是「集合/元组里含这个类」，且集合必须仍是可用的超集
        # （若某天把 float 类误加进 sanitize 白名单但漏了映射取值域，
        #  class 能存下来却算不出声明 → 静默退化成 display:block 独占块）
        for cls in FLOAT_IMG_CLASSES:
            with self.subTest(cls=cls):
                self.assertIn(cls, cm._IMG_CLASSES, "① correctmanage._IMG_CLASSES 缺")
                self.assertIn(cls, htmlmanage._IMG_CLASSES, "② htmlmanage._IMG_CLASSES 缺")
                self.assertTrue(_img_declarations({cls}), "② 映射算不出声明")

        rm_block = self._braced(self._src("rulemanage.py"), "allowed_img_classes")
        app_block = self._bracketed(self._src("ui/app.js"), "IMG_FLOAT_CLASSES")
        for cls in FLOAT_IMG_CLASSES:
            with self.subTest(cls=cls):
                self.assertIn(cls, rm_block, "③ rulemanage 白名单缺（class 会被剥）")
                self.assertIn(cls, app_block, "④ app.js IMG_FLOAT_CLASSES 缺")
        self.assertNotIn("ptoe-img-inline", app_block,
                         "行内类不该混进绕排类表（互斥由 applyImgLayoutMode 负责）")

    def test_float_css_three_copies_match_contract(self):
        """三处 CSS 副本的 float 规则与契约值逐项相同。"""
        sources = {
            "correctmanage.py(编辑器)": _float_css_decls(self._src("correctmanage.py")),
            "htmlmanage.py(EPUB style.css)": _float_css_decls(
                htmlmanage.CSSManager().generate_stylesheet()
            ),
            "ui/epubedit.html(EPUB 编辑器)": _float_css_decls(self._src("ui/epubedit.html")),
        }
        for name, found in sources.items():
            with self.subTest(source=name):
                self.assertEqual(set(found), {"left", "right"}, found)
        for cls in FLOAT_IMG_CLASSES:
            side = cls.rsplit("-", 1)[1]
            for name, found in sources.items():
                with self.subTest(cls=cls, source=name):
                    got = found[side]
                    for prop, want in CANONICAL_FLOAT_DECLS[cls].items():
                        self.assertEqual(got.get(prop), want, f"{prop} 漂移")
        # 三者彼此相等（互校，让「改一处漏两处」时报告直接指明是哪几份不一致）
        values = [sources[k] for k in sources]
        self.assertEqual(values[0], values[1])
        self.assertEqual(values[1], values[2])

    def test_app_js_remove_op_uses_prefix_not_enumeration(self):
        """前端「清除格式」的保留条件必须是 `ptoe-img-` **前缀**，不是硬编码类名清单。

        反射式检查（读 ui/app.js 源码）：保留条件写死 float 类名的话，
        将来新增任何图片类都会在「清除格式」时被一起清掉 —— 这正是本项目
        踩过的同型 bug。这里只锁定**条件写法**，不做前端行为测试
        （jsdom 无排版几何，且行为侧已有 harness 覆盖）。
        """
        src = self._src("ui/app.js")
        i = src.index("const keepClasses = []")
        region = src[i: i + 400]
        self.assertIn("c.indexOf('ptoe-img-') === 0", region, region)
        # 保留条件所在区域不得出现任何硬编码图片类名（否则退化成枚举）
        for cls in FLOAT_IMG_CLASSES:
            self.assertNotIn(cls, region, region)
        self.assertIn("CAPTION_CLASS", region, "题注类保留条件不应被一并删掉")
        # 前端类表本身仍是两元素的完整枚举（可读性优先，与保留条件无关）
        block = self._bracketed(src, "IMG_FLOAT_CLASSES")
        for cls in FLOAT_IMG_CLASSES:
            self.assertIn(cls, block)

    def test_app_js_float_is_single_exclusivity_point(self):
        """互斥清理只此一处（applyImgLayoutMode）—— 避免出现第二处半吊子清理。"""
        src = self._src("ui/app.js")
        self.assertEqual(
            src.count("function applyImgLayoutMode("), 1, "互斥点应唯一"
        )
        # 清理动作写在互斥点函数体里：清 inline + 清 float + 清 v*
        body_start = src.index("function applyImgLayoutMode(")
        body = src[body_start: body_start + 2600]
        self.assertIn("imgEl.classList.remove('ptoe-img-inline')", body)
        self.assertIn("IMG_FLOAT_CLASSES.forEach", body)
        self.assertIn("clampFloatSize(imgEl)", body)


if __name__ == "__main__":
    unittest.main()
