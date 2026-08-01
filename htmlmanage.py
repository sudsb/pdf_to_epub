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
        h1, h2, h3, h4 {{
          font-weight: bold;
          margin: 1em 0 0.5em 0;
        }}
        p {{
          text-indent: 1.5em;
          margin: 0.5em 0;
          orphans: 2; widows: 2;
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
        toc_items: list of {'title': str, 'href': str, 'level': int}
        """
        nav_lines = []
        for it in toc_items:
            t = self._escape_text(it.get('title', ''))
            href = self._escape_text(it.get('href', '#'))
            level = int(it.get('level', 1))
            indent = '  ' * (level - 1)
            nav_lines.append(f"{indent}<li><a href=\"{href}\">{t}</a></li>")
        nav_html = '<nav class="toc"><ol>\n' + '\n'.join(nav_lines) + '\n</ol></nav>'
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

    def _render_fragment(self, text: str) -> str:
        # text is already escaped where needed by caller; assume plain paragraphs
        parts = []
        for p in [ln.strip() for ln in text.split('\n') if ln.strip()]:
            parts.append(f"<p>{self._escape_text(p)}</p>")
        return '\n'.join(parts)

    def render_content_pages(self, chapters: List[Dict[str, Any]], split_by_chars: int = 5000) -> List[Tuple[str, str]]:
        """Render chapters into a list of (filename, html_content).
        chapters: [{'title':..., 'page':..., 'text':...}, ...] or higher-level grouped chapters.
        split_by_chars: soft limit to split large chapters into multiple HTML files.
        Returns list of (relpath, content) where relpath is filename relative to output_dir.
        """
        outputs: List[Tuple[str, str]] = []
        file_index = 1
        toc_items: List[Dict[str, Any]] = []
        for ch in chapters:
            title = ch.get('title') or f"Chapter {ch.get('page', file_index)}"
            text = ch.get('text', '')
            # split by chars if needed
            if len(text) <= split_by_chars:
                fname = f"content_{file_index}.xhtml"
                body = self._render_fragment(text)
                html_doc = f"<?xml version='1.0' encoding='{self.encoding}'?>\n<!DOCTYPE html>\n<html lang='en'>\n<head>\n<meta charset='{self.encoding}'/>\n<title>{self._escape_text(title)}</title>\n</head>\n<body>\n<h1>{self._escape_text(title)}</h1>\n{body}\n</body>\n</html>"
                outputs.append((fname, html_doc))
                toc_items.append({'title': title, 'href': fname, 'level': 1})
                file_index += 1
            else:
                # chunk
                start = 0
                part = 1
                while start < len(text):
                    chunk = text[start:start + split_by_chars]
                    fname = f"content_{file_index}.xhtml"
                    body = self._render_fragment(chunk)
                    subtitle = f"{title} (Part {part})"
                    html_doc = f"<?xml version='1.0' encoding='{self.encoding}'?>\n<!DOCTYPE html>\n<html lang='en'>\n<head>\n<meta charset='{self.encoding}'/>\n<title>{self._escape_text(subtitle)}</title>\n</head>\n<body>\n<h1>{self._escape_text(subtitle)}</h1>\n{body}\n</body>\n</html>"
                    outputs.append((fname, html_doc))
                    toc_items.append({'title': subtitle, 'href': fname, 'level': 1})
                    start += split_by_chars
                    file_index += 1
                    part += 1
        return outputs

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
        if merge_pages:
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

        content_outputs = self.render_content_pages(chapters, split_by_chars=200_000 if merge_pages else 5000)
        content_files = []
        for fname, content in content_outputs:
            path = os.path.join(oebps, fname)
            # inject stylesheet link
            doc = content.replace('</head>', '<link rel="stylesheet" type="text/css" href="style.css"/>\n</head>')
            with open(path, 'w', encoding=self.encoding) as f:
                f.write(doc)
            content_files.append(os.path.join('OEBPS', fname))

        # toc
        toc_html = self.render_toc_page([{'title': os.path.splitext(os.path.basename(p[0]))[0], 'href': p[0], 'level': 1} for p in content_outputs])
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
                pack_from_oebps(self.output_dir, epub_path, md, epub_version=meta.get('epub_version', '3.0'))
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

