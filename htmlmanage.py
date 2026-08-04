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
from typing import Dict, List, Any, Optional, Tuple

# 手动矫正（correctmanage.sanitize_html）白名单标签：出现任一即走标记渲染路径
_MARKUP_RE = re.compile(r"</?(?:p|h[1-6]|strong|em|br|span)([^>]*)>", flags=re.I)

# 块级保留 class：注释（ptoe-note）+ 对齐类（ptoe-align-*）+ 换页（ptoe-page-break）
_NOTE_CLASS = "ptoe-note"
_ALIGN_CLASSES = ("ptoe-align-left", "ptoe-align-center", "ptoe-align-right")
_PAGE_BREAK_CLASS = "ptoe-page-break"


def _block_class_html(attrs: str) -> str:
    """从块标签属性中提取应保留的 class（ptoe-note + 对齐类 + 换页），返回 class 属性。"""
    m = re.search(r'class="([^"]*)"', attrs)
    if not m:
        return ""
    keep = [
        c
        for c in m.group(1).split()
        if c == _NOTE_CLASS or c in _ALIGN_CLASSES or c == _PAGE_BREAK_CLASS
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
          color: #111;
          background: #fff;
        }}
        h1, h2, h3, h4, h5, h6 {{
          font-weight: bold;
          margin: 1em 0 0.5em 0;
          text-align: center;
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
        .cover {{
          text-align: center;
          margin-top: 2em;
        }}
        nav.toc {{
          margin: 1em 0;
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

    def render_cover_page(self, cover_info: Dict[str, Any]) -> str:
        title = self._escape_text(cover_info.get('title', ''))
        author = self._escape_text(cover_info.get('author', ''))
        cover_img = cover_info.get('cover_image')  # relative path expected
        img_html = ''
        if cover_img:
            img_html = f"<div class='cover'><img alt='{title} cover' src='{self._escape_text(cover_img)}'/></div>"
        html_doc = f"""<?xml version='1.0' encoding='{self.encoding}'?>
<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='{self.encoding}' />
<title>{title}</title>
</head>
<body>
<h1>{title}</h1>
<h2>{author}</h2>
{img_html}
</body>
</html>
"""
        return html_doc

    def render_toc_page(self, toc_items: List[Dict[str, Any]]) -> str:
        """Generate a nav.xhtml-like page (HTML5) for table of contents.
        toc_items: list of {'title': str, 'href': str, 'level': int}，按 level 嵌套 <ol>。
        """
        out: List[str] = []
        depth = 0
        for it in toc_items:
            t = self._escape_text(it.get('title', ''))
            href = self._escape_text(it.get('href', '#'))
            level = max(1, int(it.get('level', 1)))
            while depth < level:
                out.append('<ol>')
                depth += 1
            while depth > level:
                out.append('</ol>')
                depth -= 1
            out.append(f'<li><a href="{href}">{t}</a></li>')
        while depth > 0:
            out.append('</ol>')
            depth -= 1
        nav_html = '<nav class="toc">' + ''.join(out) + '</nav>'
        html_doc = f"""<?xml version='1.0' encoding='{self.encoding}'?>
<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='{self.encoding}' />
<title>Table of Contents</title>
</head>
<body>
<h1>Contents</h1>
{nav_html}
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
                    # 无标题：仅当该标题此前从未作为正文标题出现过才补 h1
                    # （避免「书名=第一章大标题」时标题在后续每页重复）
                    if sub_title.strip() not in used_titles:
                        toc.append({'title': sub_title, 'level': 1, 'id': None})
                        body = f"<h1>{self._escape_text(sub_title)}</h1>\n" + body
                for it in toc:
                    used_titles.add(it['title'])
                html_doc = f"<?xml version='1.0' encoding='{self.encoding}'?>\n<!DOCTYPE html>\n<html lang='en'>\n<head>\n<meta charset='{self.encoding}'/>\n<title>{self._escape_text(sub_title)}</title>\n</head>\n<body>\n{body}\n</body>\n</html>"
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
                    cover_rel = os.path.join('Images', os.path.basename(cover_src))
                else:
                    cover_rel = cover_src  # maybe already relative
            except Exception:
                cover_rel = cover
        else:
            cover_rel = None

        cover_html = self.render_cover_page({'title': title, 'author': author, 'cover_image': cover_rel})
        with open(os.path.join(oebps, cover_fname), 'w', encoding=self.encoding) as f:
            f.write(self.cssm.inject_styles(cover_html))

        # content pages
        pages = structured_doc.get('pages', [])
        chapters = []
        # 手动矫正的标记结构：每篇文章 = 一个 EPUB 内容页（全文标记处开新页）
        articles = structured_doc.get('articles')
        if articles:
            for a in articles:
                chapters.append({'title': title, 'text': a.get('text', '')})
        elif merge_pages:
            # 合并模式：全部页面正文按页序合并为单一正文（跳过空白页）
            merged = "\n\n".join(
                p.get('text', '').strip() for p in pages if (p.get('text') or '').strip()
            )
            if merged:
                chapters.append({'title': title, 'text': merged})
        else:
            for p in pages:
                chapters.append({'title': f"Page {p.get('page')}", 'page': p.get('page'), 'text': p.get('text', '')})

        # Before rendering, detect <img src='...'> occurrences in page texts and copy referenced images
        img_pattern = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", flags=re.I)
        for p in pages:
            txt = p.get('text', '')
            for m in img_pattern.findall(txt):
                src = m
                # only handle local file paths (not http)
                if src and not src.lower().startswith(('http://', 'https://')):
                    src_path = os.path.abspath(src)
                    if os.path.isfile(src_path):
                        dst = os.path.join(images_dir, os.path.basename(src_path))
                        try:
                            with open(src_path, 'rb') as rf, open(dst, 'wb') as wf:
                                wf.write(rf.read())
                            # replace occurrences in chapters text to relative Images/ path
                            rel = os.path.join('Images', os.path.basename(src_path))
                            for ch in chapters:
                                ch['text'] = ch['text'].replace(src, rel)
                        except Exception:
                            # ignore copy errors; leave original src
                            pass

        content_outputs, toc_items = self.render_content_pages(chapters, split_by_chars=200_000 if merge_pages else 5000)
        content_files = []
        for fname, content in content_outputs:
            path = os.path.join(oebps, fname)
            # inject stylesheet link
            doc = content.replace('</head>', '<link rel="stylesheet" type="text/css" href="style.css"/>\n</head>')
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

