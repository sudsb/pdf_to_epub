"""
htmlmanage.py

Convert structured documents (from stringmanage.clean_and_structure_text) into
XHTML/HTML5 files suitable for EPUB packaging. Provides CSS generation and
basic validation/fixing utilities.

This module intentionally has no external dependencies beyond the stdlib so it
can be used in constrained environments.

API overview
- HTMLConverter(output_dir, epub_version='3.0').convert_document(structured_doc)
- CSSManager(font_family='serif', line_height=1.6).generate_stylesheet()
- HTMLValidator(strict_mode=True).validate_xhtml(html_string)
"""
from __future__ import annotations

import os
import re
import html
import base64
from typing import Dict, List, Any, Optional, Tuple

# 手动矫正（correctmanage.sanitize_html）白名单标签：出现任一即走标记渲染路径
_MARKUP_RE = re.compile(r"</?(?:p|h[1-6]|strong|em|br|span)([^>]*)>", flags=re.I)

# 块级保留 class：注释（ptoe-note）+ 对齐类（ptoe-align-*）+ 换页（ptoe-page-break）
_NOTE_CLASS = "ptoe-note"
_ALIGN_CLASSES = ("ptoe-align-left", "ptoe-align-center", "ptoe-align-right")
_PAGE_BREAK_CLASS = "ptoe-page-break"
# 插入图片的显示模式 class（全画幅 / 局部），随 <p> 块与 <img> 一起保留
# 尺寸 class（ptoe-img-w25/50/75/100）控制图片宽度百分比
# 位置 class（ptoe-img-left/center/right）控制图片对齐
_IMG_CLASSES = ("ptoe-img-full", "ptoe-img-fit", "ptoe-img-inline",
               "ptoe-img-w25", "ptoe-img-w50", "ptoe-img-w75", "ptoe-img-w100",
               "ptoe-img-left", "ptoe-img-center", "ptoe-img-right",
               "ptoe-img-vtop", "ptoe-img-vmid", "ptoe-img-vbot")


def _is_img_page(text: str) -> bool:
    """整页图片判定：含 <img> 且剥标签后无文字（或仅剩 OCR 噪声如 #、标点等非文字字符）。"""
    if '<img' not in text:
        return False
    stripped = re.sub(r'<[^>]+>', '', text).strip()
    if not stripped:
        return True
    return not re.search(r'[\u4e00-\u9fffA-Za-z0-9]', stripped)


# 防御性自闭合 <img> 标签（XHTML 规范要求空元素必须自闭合）。
# sanitize_html 已确保 img 自闭合，但阅读器对未自闭合的 <img> 容错不一，
# 此处做最终兜底：把 <img ...> 转为 <img .../>，已自闭合的不重复处理。
_SELF_CLOSE_IMG_RE = re.compile(r'<img\b([^>]*?)(?<!/)>', flags=re.I)


def _self_close_img(html: str) -> str:
    """确保所有 <img> 标签自闭合（XHTML 兼容）。"""
    return _SELF_CLOSE_IMG_RE.sub(r'<img\1/>', html)


def _block_class_html(attrs: str) -> str:
    """从块标签属性中提取应保留的 class（ptoe-note + 对齐类 + 换页 + 图片模式），返回 class 属性。"""
    m = re.search(r'class="([^"]*)"', attrs)
    if not m:
        return ""
    keep = [
        c
        for c in m.group(1).split()
        if c == _NOTE_CLASS or c in _ALIGN_CLASSES or c == _PAGE_BREAK_CLASS or c in _IMG_CLASSES
    ]
    return f' class="{" ".join(keep)}"' if keep else ""


class CSSManager:
    def __init__(self, css_template: Optional[str] = None, font_family: str = "serif", line_height: float = 1.6):
        self.css_template = css_template
        self.font_family = font_family
        self.line_height = line_height

    def generate_stylesheet(self) -> str:
        """Return a CSS stylesheet string suitable for e-readers.
        Avoids modern layout features and keeps the rules conservative.
        """
        if self.css_template:
            # if provided a template string, do lightweight interpolation
            return self.css_template.replace("{font_family}", self.font_family).replace("{line_height}", str(self.line_height))

        css = f"""
        /* Basic ebook stylesheet - conservative for reader compatibility */
        html, body {{
          margin: 0;
          padding: 0.8em;
          font-family: {self.font_family};
          line-height: {self.line_height};
          font-size: 1em;
          /* 不硬编码背景/文字颜色（2026-08-13）：交阅读器自身的阅读背景/夜间模式等主题设置，
             否则 epub 内固定白底黑字会覆盖阅读器内的背景/主题设置 */
        }}
        h1, h2, h3, h4, h5, h6 {{
          font-weight: bold;
          margin: 1em 0 0.5em 0;
          text-align: center;
          /* 标题默认金光红（2026-08）：R255 G0 B0 */
          color: #ff0000;
        }}
        h1 {{
          /* 一级标题下方分隔线（2026-08）：全宽自适应（随版面宽度伸展） */
          border-bottom: 1px solid #999;
          padding-bottom: 0.35em;
        }}
        p {{
          text-indent: 1.5em;
          margin: 0.5em 0;
          orphans: 2; widows: 2;
        }}
        .ptoe-note {{
          font-size: 0.85em;
        }}
        .ptoe-align-center {{
          text-align: center;
        }}
        .ptoe-align-left {{
          text-align: left;
        }}
        .ptoe-align-right {{
          text-align: right;
        }}
        /* 对齐段落取消首行缩进（2026-08-15）：p 默认 text-indent 1.5em 会让
           居中/居右段落首行偏移，与矫正界面（无缩进）显示不一致 */
        p.ptoe-align-center, p.ptoe-align-left, p.ptoe-align-right {{
          text-indent: 0;
        }}
        .ptoe-page-break {{
          page-break-before: always;
          break-before: page;
          margin: 0;
          padding: 0;
          height: 0;
          overflow: hidden;
        }}
        img {{
          display: block;
          max-width: 100%;
          height: auto;
        }}
        /* 插入图片：全画幅（独立占页 + 占满整页）与局部（按原尺寸居中）
           全画幅（2026-08-15 用户要求）：page-break 保证图片单独一页不与文字
           同页；width/height 100% + object-fit:contain 让图片按比例填满
           整个页面（不裁切）。局部保持原尺寸居中。 */
        p.ptoe-img-full, p.ptoe-img-fit {{
          text-indent: 0;
          margin: 0.8em 0;
        }}
        p.ptoe-img-full {{
          page-break-before: always;
          page-break-after: always;
          margin: 0;
          padding: 0;
          text-align: center;
          height: 100%;
        }}
        /* 全画幅图片位于内容文件首位时不再强制前置分页（否则封面后出现空白页，2026-08 修复） */
        p.ptoe-img-full:first-child {{
          page-break-before: auto;
        }}
        p.ptoe-img-full img, p.ptoe-img-fit img {{
          display: inline-block;
          max-width: 100%;
          vertical-align: middle;
        }}
        p.ptoe-img-full img {{
          width: 100%;
          height: 100%;
          object-fit: contain;
        }}
        p.ptoe-img-fit img {{
          height: auto;
        }}
        /* 尺寸 class：唯一宽度控制（全画幅默认 w100，局部默认无尺寸=原图） */
        .ptoe-img-w25 {{ width: 25%; }}
        .ptoe-img-w50 {{ width: 50%; }}
        .ptoe-img-w75 {{ width: 75%; }}
        .ptoe-img-w100 {{ width: 100%; }}
        /* 位置 class：p 上 text-align 控制 img 对齐 */
        p.ptoe-img-left {{ text-align: left; }}
        p.ptoe-img-center {{ text-align: center; }}
        p.ptoe-img-right {{ text-align: right; }}
        /* 行内图片（2026-08-10）：直接嵌在文字流中（无 <p> 包裹），
           vertical-align 控制上下对齐；尺寸 class 同样生效。
           img.ptoe-img-inline 特异性(0,1,1)高于通用 img(0,0,1)，覆盖 display:block */
        img.ptoe-img-inline {{
          display: inline-block;
          max-width: 100%;
          height: auto;
          vertical-align: middle;
        }}
        img.ptoe-img-vtop {{ vertical-align: top; }}
        img.ptoe-img-vmid {{ vertical-align: middle; }}
        img.ptoe-img-vbot {{ vertical-align: bottom; }}
        .cover {{
          text-align: center;
          margin-top: 2em;
        }}
        nav.toc {{
          margin: 1em 0;
        }}
        /* 目录编号与文字不重叠：序号以文本显式输出（.toc-num），关闭列表 marker——
           部分阅读器对多位数字（>9）的 list-style marker 渲染会截断/数字叠加（2026-08） */
        nav.toc ol {{
          list-style: none;
          padding-left: 1.8em;
          margin: 0.2em 0;
        }}
        nav.toc li {{
          margin: 0.2em 0;
        }}
        nav.toc .toc-num {{
          display: inline-block;
          min-width: 2.2em;
          text-align: right;
          margin-right: 0.4em;
        }}
        """
        return css

    def inject_styles(self, html_content: str, inline: bool = True) -> str:
        """If inline is True, inject a minimal style into the head of the document.
        Otherwise return original content unchanged (expect external style link).
        """
        if not inline:
            return html_content
        css = self.generate_stylesheet()
        # safe insertion into <head>
        if "<head>" in html_content:
            return html_content.replace("<head>", "<head>\n<style type='text/css'>\n" + css + "\n</style>\n")
        # fallback: prepend
        return "<style type='text/css'>\n" + css + "\n</style>\n" + html_content

    def handle_images(self, css_rules: Dict[str, str]) -> Dict[str, str]:
        """Return modified css_rules with conservative image rules applied.
        css_rules is a dict of selector -> rule; we ensure img max-width present.
        """
        rules = dict(css_rules)
        img_rule = rules.get('img', '')
        if 'max-width' not in img_rule:
            img_rule = (img_rule + '; ' if img_rule else '') + 'max-width:100%; height:auto;'
        rules['img'] = img_rule
        return rules


class HTMLValidator:
    def __init__(self, strict_mode: bool = True):
        self.strict_mode = strict_mode

    def _balance_tags(self, s: str) -> str:
        """Very small best-effort attempt to close unclosed common tags.
        This is not a full HTML parser; it targets common mistakes: unclosed <p>, <div>, <span>.
        """
        # naive approach: ensure <br> -> <br/> for XHTML compatibility
        s = re.sub(r"<br\s*>", "<br/>", s, flags=re.I)
        # ensure common void elements are self-closed in xhtml
        voids = ['img', 'hr', 'br', 'meta', 'link', 'input']
        for v in voids:
            s = re.sub(rf"<({v})([^>/]*)>", rf"<\1\2/>", s, flags=re.I)
        return s

    def validate_xhtml(self, html_string: str) -> Tuple[bool, List[str]]:
        """Perform conservative checks and return (ok, messages).
        ok==True means the string likely meets the minimal XML well-formedness constraints.
        """
        msgs: List[str] = []
        s = html_string
        # quick checks
        if '<' in s and '>' in s:
            # check for unclosed angle brackets
            opens = s.count('<')
            closes = s.count('>')
            if opens != closes:
                msgs.append(f"angle-bracket mismatch: <({opens}) vs >({closes})")
        # check for plain & not entity-escaped
        for m in re.findall(r"&[^#a-zA-Z0-9]+", s):
            msgs.append(f"suspicious entity usage: {m}")
        # optionally balancing
        fixed = self.fix_malformed_tags(s)
        ok = len(msgs) == 0
        return ok, msgs

    def fix_malformed_tags(self, html_string: str) -> str:
        s = html_string
        s = self._balance_tags(s)
        # additional naive fixes could be added here
        return s

    def check_links(self, html_string: str) -> Dict[str, bool]:
        """Return a mapping of link -> reachable (only file: and relative checks done here).
        External HTTP checks are NOT performed to avoid network calls.
        """
        links = re.findall(r'href=["\']([^"\']+)["\']', html_string)
        res: Dict[str, bool] = {}
        for l in links:
            if l.startswith('http://') or l.startswith('https://'):
                # skip network checks
                res[l] = True
            else:
                # treat as relative file reference
                exists = os.path.exists(l)
                res[l] = exists
        return res


class HTMLConverter:
    def __init__(self, output_dir: str, encoding: str = 'utf-8', epub_version: str = '3.0'):
        self.output_dir = output_dir
        self.encoding = 'utf-8' if encoding is None else encoding
        self.epub_version = epub_version
        self.cssm = CSSManager(font_family='serif', line_height=1.6)
        self.validator = HTMLValidator(strict_mode=(epub_version != '3.0'))
        os.makedirs(self.output_dir, exist_ok=True)

    def _escape_text(self, text: str) -> str:
        return html.escape(text, quote=False)

    def render_cover_page(self, cover_info: Dict[str, Any], image_only: bool = False) -> str:
        """封面页。image_only=True 时整页仅图片（无标题无其他内容，2026-08）——
        第一页为整页图片时图片独立一页（书名保留在元数据与导航栏目录条目中）。
        2026-08-15 起 convert_document 一律以 image_only=True 调用（用户不要书名页）；
        非 image_only 分支保留供其他调用方使用。"""
        title = self._escape_text(cover_info.get('title', ''))
        author = self._escape_text(cover_info.get('author', ''))
        cover_img = cover_info.get('cover_image')  # relative path expected
        img_html = ''
        if cover_img:
            img_html = f"<div class='cover'><img alt='{title} cover' src='{self._escape_text(cover_img)}'/></div>"
        if image_only:
            body = img_html
        else:
            body = f"<h1>{title}</h1>\n<h2>{author}</h2>\n{img_html}"
        html_doc = f"""<?xml version='1.0' encoding='{self.encoding}'?>
<!DOCTYPE html>
<html lang='zh-CN' xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset='{self.encoding}' />
<title>{title}</title>
</head>
<body>
{body}
</body>
</html>
"""
        return html_doc

    def render_toc_page(self, toc_items: List[Dict[str, Any]]) -> str:
        """Generate a nav.xhtml-like page (HTML5) for table of contents.
        toc_items: list of {'title': str, 'href': str, 'level': int}，按 level 嵌套 <ol>。
        序号以文本显式输出（<span class="toc-num">N.</span>），不依赖阅读器
        list-style marker 渲染——多位数字（>9）在部分阅读器中被截断/数字叠加（2026-08）。
        """
        out: List[str] = []
        depth = 0
        counters: Dict[int, int] = {}  # 每级列表独立计数，新开一级从 1 重计
        for it in toc_items:
            t = self._escape_text(it.get('title', ''))
            href = self._escape_text(it.get('href', '#'))
            level = max(1, int(it.get('level', 1)))
            while depth < level:
                out.append('<ol>')
                depth += 1
                counters[depth] = 0
            while depth > level:
                out.append('</ol>')
                depth -= 1
            counters[level] = counters.get(level, 0) + 1
            out.append(f'<li><a href="{href}"><span class="toc-num">{counters[level]}.</span>{t}</a></li>')
        while depth > 0:
            out.append('</ol>')
            depth -= 1
        # EPUB 3.3 §11 导航文档规范：目录 <nav> 必须带 epub:type="toc"，
        # 且 <html> 需声明 xmlns:epub 命名空间——否则严格阅读器（Apple Books、
        # Google Play 等）不识别为目录，TOC 面板空白或链接无法跳转（2026-08）。
        # role="doc-toc" 为 ARIA 角色，辅助阅读器识别目录导航区域。
        nav_html = '<nav class="toc" epub:type="toc" role="doc-toc">' + ''.join(out) + '</nav>'
        # Landmarks nav：为阅读器提供目录/正文的语义入口（EPUB 3.3 §11.3）。
        # nav.xhtml 不在 spine 中（正文不显示目录页，导航栏经 manifest properties="nav"
        # 仍可用）：landmarks 链接必须指向 spine 内资源，否则 epubcheck RSC-011 报错
        # （2026-08-15）
        first_content_href = toc_items[0]['href'] if toc_items else 'content_1.xhtml'
        # 取纯文件路径（去掉 #fragment），landmarks 链接指向文件即可
        first_content_file = first_content_href.split('#', 1)[0]
        landmarks_html = (
            '<nav epub:type="landmarks" hidden="hidden">'
            f'<ol><li><a epub:type="toc" href="{first_content_file}">目录</a></li>'
            f'<li><a epub:type="bodymatter" href="{first_content_file}">正文</a></li></ol></nav>'
        )
        html_doc = f"""<?xml version='1.0' encoding='{self.encoding}'?>
<!DOCTYPE html>
<html lang='zh-CN' xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
<meta charset='{self.encoding}' />
<title>目录</title>
</head>
<body>
<h1>目录</h1>
{nav_html}
{landmarks_html}
</body>
</html>
"""
        return html_doc

    def _render_fragment(self, text: str, toc_out: Optional[List[Dict[str, Any]]] = None) -> str:
        """把矫正后的 HTML 片段渲染为 XHTML 正文片段。

        toc_out 不为 None 时，收集正文中的标题（h1-h6）为目录项
        {'title', 'level', 'id'}；标题元素带 id="hN" 锚点供目录跳转。
        注释块/注释 span（class="ptoe-note"）原样放行（CSS 控制小字号）。
        """
        # 无校正标记时保持原逻辑（逐行转义为 <p>），保证默认流水线输出不变。
        if not _MARKUP_RE.search(text):
            parts = []
            for p in [ln.strip() for ln in text.split('\n') if ln.strip()]:
                parts.append(f"<p>{self._escape_text(p)}</p>")
            return '\n'.join(parts)
        # 手动矫正路径：text 已经 correctmanage.sanitize_html 白名单清洗
        # （仅含 <p>/<h1-6>/<strong>/<em>/<br/>、ptoe-note span 与转义文本），
        # 按块级标签重排，块内文本直接放行（避免二次转义）。
        blocks: List[str] = []
        cur: List[str] = []
        kind: Optional[str] = None  # 当前块类型：'p' | 'h1'..'h6' | None
        open_tag: Optional[str] = None  # 当前块的开标签（含 id/class）
        hcount = 0  # 标题锚点计数（每片段内 h1..hN）
        heading: Optional[Tuple[int, List[str]]] = None  # 当前标题 (级别, 文本缓冲)

        def _flush_block() -> None:
            nonlocal cur, kind, open_tag, heading
            if kind is None:
                return
            blocks.append(f"{open_tag or f'<{kind}>'}{''.join(cur)}</{kind}>")
            if kind != 'p' and heading is not None and toc_out is not None:
                title = html.unescape(re.sub(r"<[^>]+>", "", "".join(heading[1]))).strip()
                if title:
                    toc_out.append({'title': title, 'level': heading[0], 'id': f'h{hcount}'})
            cur = []
            kind = None
            open_tag = None
            heading = None

        for tok in re.split(r"(<[^>]+>)", text):
            if not tok:
                continue
            # 防御：标记 span 已由 apply_markers 提取，此处兜底剔除
            if "data-ptoe-marker" in tok:
                continue
            m = re.fullmatch(r"</?(p|h[1-6])([^>]*)>", tok, flags=re.I)
            if m:
                tag = m.group(1).lower()
                if tok.startswith('</'):
                    if kind == tag:
                        _flush_block()
                    continue
                # 开标签：先收掉上一块；开标签本身不进 cur（flush 时合成包裹）
                if kind:
                    _flush_block()
                cls = _block_class_html(m.group(2) or "")
                if tag.startswith('h'):
                    hcount += 1
                    kind = tag
                    heading = (int(tag[1]), [])
                    open_tag = f'<{tag} id="h{hcount}"{cls}>'
                else:
                    kind = 'p'
                    open_tag = f'<p{cls}>'
                continue
            cur.append(tok)
            if heading is not None:
                heading[1].append(tok)
        if kind or cur:
            if kind is None:
                kind = 'p'
                open_tag = '<p>'
            blocks.append(f"{open_tag}{''.join(cur)}</{kind}>")
            if kind != 'p' and heading is not None and toc_out is not None:
                title = html.unescape(re.sub(r"<[^>]+>", "", "".join(heading[1]))).strip()
                if title:
                    toc_out.append({'title': title, 'level': heading[0], 'id': f'h{hcount}'})
        return '\n'.join(blocks)

    def render_content_pages(self, chapters: List[Dict[str, Any]], split_by_chars: int = 5000) -> Tuple[List[Tuple[str, str]], List[Dict[str, Any]]]:
        """Render chapters into a list of (filename, html_content) plus heading-based TOC items.

        chapters: [{'title':..., 'page':..., 'text':...}, ...] or higher-level grouped chapters.
        split_by_chars: soft limit to split large chapters into multiple HTML files.
        Returns (outputs, toc_items)：outputs 为 (relpath, content) 列表；
        toc_items 为 {'title','href','level'} 列表，href 形如 content_1.xhtml#h1。
        正文含标题时用标题作为该页的 h1/目录项（不再重复插入书名一级标题）；
        正文无标题时才补 h1（首个分卷用书名，后续分卷用「书名（第N部分）」）。
        已作为正文标题出现过的文本不再重复补 h1 —— 否则当书名等于第一章大标题
        （单章 PDF、--title 用了章节名等）时，同一标题会在后续每页重复出现。
        """
        outputs: List[Tuple[str, str]] = []
        toc_items: List[Dict[str, Any]] = []
        file_index = 1
        used_titles: set = set()  # 已作为正文标题（含补充 h1）出现过的文本
        for ch in chapters:
            title = ch.get('title') or f"Chapter {ch.get('page', file_index)}"
            text = ch.get('text', '')
            # split by chars if needed（后续分卷标题带「（第N部分）」）
            if len(text) <= split_by_chars:
                chunks = [(title, text)]
            else:
                chunks = [
                    (title if i == 0 else f"{title}（第{i + 1}部分）", text[start:start + split_by_chars])
                    for i, start in enumerate(range(0, len(text), split_by_chars))
                ]
            for sub_title, chunk in chunks:
                fname = f"content_{file_index}.xhtml"
                toc = []  # 本页标题（含锚点 id）
                body = self._render_fragment(chunk, toc_out=toc)
                if not toc:
                    # 无标题：目录项保留（导航栏可跳转到该页），但不再向正文补
                    # <h1>书名</h1>——「自动添加的图书名」从正文移除（2026-08-15），
                    # 书名仅保留在导航栏目录条目与 EPUB 元数据中。
                    # 全幅图片页（整页仅图片，无文字内容）不列目录项——整页即图片（2026-08）
                    is_full_img_page = _is_img_page(text)
                    if not is_full_img_page and sub_title.strip() not in used_titles:
                        toc.append({'title': sub_title, 'level': 1, 'id': None})
                for it in toc:
                    used_titles.add(it['title'])
                html_doc = f"<?xml version='1.0' encoding='{self.encoding}'?>\n<!DOCTYPE html>\n<html lang='zh-CN' xmlns=\"http://www.w3.org/1999/xhtml\">\n<head>\n<meta charset='{self.encoding}'/>\n<title>{self._escape_text(sub_title)}</title>\n</head>\n<body>\n{body}\n</body>\n</html>"
                outputs.append((fname, html_doc))
                for it in toc:
                    href = fname if not it['id'] else f"{fname}#{it['id']}"
                    toc_items.append({'title': it['title'], 'href': href, 'level': it['level']})
                file_index += 1
        return outputs, toc_items

    def convert_document(self, structured_doc: Dict[str, Any], merge_pages: bool = True) -> Dict[str, Any]:
        """Main entry: takes structured_doc from stringmanage and writes files to disk.
        structured_doc expected keys: 'pages', 'body', 'paragraphs', optional 'titles', 'meta' (title/author/cover_image)
        merge_pages=True: all page bodies are merged (in page order) into a single content file;
        merge_pages=False: legacy behaviour, one content_N.xhtml per page.
        Returns mapping with keys: 'content_files': [filenames], 'toc_file': filename, 'css_file': filename
        """
        meta = structured_doc.get('meta', {})
        title = meta.get('title', 'Document')
        author = meta.get('author', '')
        cover = meta.get('cover_image')

        # prepare directories
        oebps = os.path.join(self.output_dir, 'OEBPS')
        images_dir = os.path.join(oebps, 'Images')
        os.makedirs(oebps, exist_ok=True)
        os.makedirs(images_dir, exist_ok=True)

        # write stylesheet
        css = self.cssm.generate_stylesheet()
        css_path = os.path.join(oebps, 'style.css')
        with open(css_path, 'w', encoding=self.encoding) as f:
            f.write(css)

        # cover: copy cover image if path provided
        cover_fname = 'cover.xhtml'
        if cover:
            cover_src = cover
            try:
                if os.path.isfile(cover_src):
                    dst = os.path.join(images_dir, os.path.basename(cover_src))
                    # copy binary
                    with open(cover_src, 'rb') as rf, open(dst, 'wb') as wf:
                        wf.write(rf.read())
                    # EPUB 内相对路径必须用正斜杠：os.path.join 在 Windows 会产出
                    # 反斜杠，阅读器按 URI 解析失败导致图片/封面不显示（2026-08 修复）
                    cover_rel = 'Images/' + os.path.basename(cover_src)
                else:
                    cover_rel = cover_src  # maybe already relative
            except Exception:
                cover_rel = cover
        else:
            cover_rel = None

        cover_html = None  # cover 在图片提取之后构建（需解析后的图片路径），见下方

        # content pages
        def split_h1_chapters(text: str, fallback_title: str) -> List[Dict[str, str]]:
            """正文含 <h1> 时按一级标题切分为多篇文章（每篇 = 一个 EPUB 内容页，新页开始）。
            merged 与 articles 分支共用（2026-08）：标题取首个 h1 内文（剥标签/unescape/压空白，
            空回退 fallback_title），首个 h1 前的序言块沿用 fallback_title；无 <h1 单篇原样返回。"""
            if '<h1' not in text:
                return [{'title': fallback_title, 'text': text}]
            out = []
            for chunk in (c for c in re.split(r'(?=<h1(?:\s|>))', text) if c.strip()):
                m = re.search(r'<h1[^>]*>(.*?)</h1>', chunk, flags=re.S)
                if m:
                    ch_title = html.unescape(re.sub(r'<[^>]+>', '', m.group(1))).strip()
                    ch_title = re.sub(r'\s+', ' ', ch_title)
                    out.append({'title': ch_title or fallback_title, 'text': chunk})
                else:
                    # 首个 h1 之前的序言块：沿用书名标题
                    out.append({'title': fallback_title, 'text': chunk})
            return out

        pages = structured_doc.get('pages', [])
        chapters = []
        # 手动矫正的标记结构：每篇文章 = 一个 EPUB 内容页（全文标记处开新页）；
        # 文章正文含 <h1> 时同样按一级标题分页（与 merged 分支一致，2026-08）
        articles = structured_doc.get('articles')
        if articles:
            for a in articles:
                chapters.extend(split_h1_chapters(a.get('text', ''), title))
        elif merge_pages:
            # 合并模式：全部页面正文按页序合并为单一正文（跳过空白页）
            merged = "\n\n".join(
                p.get('text', '').strip() for p in pages if (p.get('text') or '').strip()
            )
            if merged:
                chapters.extend(split_h1_chapters(merged, title))
        else:
            for p in pages:
                chapters.append({'title': f"Page {p.get('page')}", 'page': p.get('page'), 'text': p.get('text', '')})

        # Before rendering, detect <img src='...'> occurrences and copy referenced images.
        # 同时扫描原始页面文本与章节文本：矫正 markers/articles 流下两者可能不一致
        # （历史载入、页面替换、文章重组等），保证图片不会漏提取（2026-08）
        img_pattern = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", flags=re.I)
        data_img_map: Dict[str, str] = {}  # data URI → Images/ 相对路径（同一图只写一次）
        img_seq = 0
        scan_texts = [p.get('text', '') for p in pages] + [ch.get('text', '') for ch in chapters]
        for txt in scan_texts:
            for m in img_pattern.findall(txt):
                src = m
                # data URI（矫正界面插入的图片）：解码写入 Images/ 并替换为相对路径
                if src.startswith('data:') and ';base64,' in src:
                    try:
                        if src in data_img_map:
                            rel = data_img_map[src]
                        else:
                            head, b64 = src.split(';base64,', 1)
                            mime = head.split(':', 1)[-1] if ':' in head else ''
                            ext = 'png'
                            for key, val in (('png', 'png'), ('jpeg', 'jpg'), ('jpg', 'jpg'), ('gif', 'gif'), ('webp', 'webp')):
                                if mime.endswith(key):
                                    ext = val
                                    break
                            img_seq += 1
                            fname = f"img_{img_seq}.{ext}"
                            dst = os.path.join(images_dir, fname)
                            with open(dst, 'wb') as wf:
                                wf.write(base64.b64decode(b64))
                            rel = 'Images/' + fname  # EPUB 路径必须正斜杠（Windows os.path.join 会产出反斜杠）
                            data_img_map[src] = rel
                        for ch in chapters:
                            ch['text'] = ch['text'].replace(src, rel)
                    except Exception:
                        # 解码/写盘失败则保留原样（不阻断打包）
                        pass
                    continue
                # only handle local file paths (not http)
                if src and not src.lower().startswith(('http://', 'https://')):
                    src_path = os.path.abspath(src)
                    if os.path.isfile(src_path):
                        dst = os.path.join(images_dir, os.path.basename(src_path))
                        try:
                            with open(src_path, 'rb') as rf, open(dst, 'wb') as wf:
                                wf.write(rf.read())
                            # replace occurrences in chapters text to relative Images/ path
                            rel = 'Images/' + os.path.basename(src_path)  # EPUB 路径必须正斜杠
                            for ch in chapters:
                                ch['text'] = ch['text'].replace(src, rel)
                        except Exception:
                            # ignore copy errors; leave original src
                            pass

        # cover：整页图片页独立为封面（cover.xhtml，仅图片无书名页——2026-08-15
        # 用户明确不要书名页，书名保留在 EPUB 元数据与导航栏目录条目中）；
        # 无封面图（meta 未提供且首章非整页图片）时不生成 cover.xhtml
        first_is_img_page = bool(chapters) and _is_img_page(chapters[0]['text'])
        cover_img = cover_rel
        if first_is_img_page:
            m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', chapters[0]['text'])
            if m:
                cover_img = m.group(1)
        cover_title = chapters[0]['title'] if chapters else title
        if cover_img:
            cover_html = self.render_cover_page({'title': cover_title, 'author': author, 'cover_image': cover_img}, image_only=True)
            with open(os.path.join(oebps, cover_fname), 'w', encoding=self.encoding) as f:
                f.write(self.cssm.inject_styles(cover_html))

        # 第一页为整页图片且已生成封面页（cover.xhtml）时，该章不再重复出现在正文（2026-08）
        render_chapters = chapters[1:] if (first_is_img_page and cover_img) else chapters
        content_outputs, toc_items = self.render_content_pages(render_chapters, split_by_chars=200_000 if merge_pages else 5000)
        content_files = []
        for fname, content in content_outputs:
            path = os.path.join(oebps, fname)
            # inject stylesheet link
            doc = content.replace('</head>', '<link rel="stylesheet" type="text/css" href="style.css"/>\n</head>')
            # 防御性自闭合：确保所有 <img ...> 都是 <img .../>（XHTML 规范要求）
            doc = _self_close_img(doc)
            with open(path, 'w', encoding=self.encoding) as f:
                f.write(doc)
            content_files.append(os.path.join('OEBPS', fname))

        # toc（与正文标题一一对应，带锚点跳转）
        toc_html = self.render_toc_page(toc_items)
        toc_fname = 'nav.xhtml'
        with open(os.path.join(oebps, toc_fname), 'w', encoding=self.encoding) as f:
            f.write(self.cssm.inject_styles(toc_html))

        result = {'content_files': content_files, 'toc_file': os.path.join('OEBPS', toc_fname), 'css_file': os.path.join('OEBPS', 'style.css')}
        # optional: package into .epub when meta requests it
        try:
            if meta.get('package_epub'):
                from epubmanage import pack_from_oebps, EPUBMetadata
                epub_path = meta.get('epub_path') or os.path.join(self.output_dir, f"{self._escape_text(title)}.epub")
                md = EPUBMetadata(title=title, author=author, language=meta.get('language', 'en'))
                pack_from_oebps(self.output_dir, epub_path, md, epub_version=meta.get('epub_version', '3.0'), toc_items=toc_items)
                result['epub'] = epub_path
        except Exception as e:
            result['epub_error'] = str(e)
        return result


# module-level convenience
if __name__ == '__main__':
    print('htmlmanage.py - utilities for converting structured text to XHTML/HTML5')

def _cli():
    import argparse
    p = argparse.ArgumentParser(description='Convert structured document to XHTML and optionally package as EPUB')
    p.add_argument('outdir', help='Output directory for HTML files')
    p.add_argument('--epub', help='If supplied, also package output as EPUB to given path', default=None)
    args = p.parse_args()
    print('CLI not wired to read structured input; use HTMLConverter in code or supply structured_doc programmatically')

