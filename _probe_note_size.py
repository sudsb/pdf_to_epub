# -*- coding: utf-8 -*-
"""临时验证（可删除）：修复前/后，各类注释形态的等效字号对比。"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from correctmanage import apply_markers, sanitize_html  # noqa: E402
from htmlmanage import HTMLConverter  # noqa: E402

UA = {"h1": 2.0, "h2": 1.5, "h3": 1.17, "h4": 1.0, "h5": 0.83, "h6": 0.67}
TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w:-]*)([^>]*?)(/?)>", re.S)

PAGE = (
    '<h1 class="ptoe-note">标题上的注释</h1>'
    '<p class="ptoe-note">普通注释段落</p>'
    '<p class="ptoe-note">注释段落里又套了<span class="ptoe-note">嵌套注释</span></p>'
    '<p>正文里的行内注释<span class="ptoe-note">行内注释</span></p>'
)


def class_list(attrs: str) -> list[str]:
    m = re.search(r'class="([^"]*)"', attrs)
    return m.group(1).split() if m else []


def sizes_of(html: str, note_any: bool, nest_guard: bool) -> list[tuple[float, str]]:
    """按给定 CSS 口径算每个注释元素的等效字号。

    note_any:   .ptoe-note 对任意元素生效（修复后）；False = 只有 p/span（修复前）
    nest_guard: .ptoe-note .ptoe-note{font-size:1em}（修复后）
    """
    stack: list[tuple[str, bool, float]] = []  # (tag, is_note, scale)
    out: list[tuple[float, str]] = []
    for m in TAG_RE.finditer(html):
        closing, tag, attrs, selfclose = m.group(1), m.group(2).lower(), m.group(3) or "", m.group(4)
        cls = class_list(attrs)
        is_note = "ptoe-note" in cls
        if closing:
            for idx in range(len(stack) - 1, -1, -1):
                if stack[idx][0] == tag:
                    del stack[idx:]
                    break
            continue
        parent_scale = stack[-1][2] if stack else 1.0
        parent_note = stack[-1][1] if stack else False
        s = parent_scale
        if is_note:
            applies = note_any or tag in ("p", "span")
            if applies:
                s = parent_scale * (1.0 if (nest_guard and parent_note) else 0.85)
            out.append((round(s, 4), f"<{tag} class=\"{' '.join(cls)}\">"))
        elif tag in ("sup", "sub") or "ptoe-sup" in cls or "ptoe-sub" in cls:
            s = parent_scale * 0.7
        elif tag in UA:
            s = parent_scale * UA[tag]
        if not selfclose:
            stack.append((tag, is_note, s))
    return out


items = [{"page": 1, "text": sanitize_html(PAGE)}]
arts = apply_markers(items)
tmp = tempfile.mkdtemp(prefix="probe_note_size_")
HTMLConverter(output_dir=tmp, epub_version="3.0").convert_document({
    "articles": arts, "pages": items,
    "body": items[0]["text"], "paragraphs": [{"page": 1, "text": items[0]["text"]}],
    "meta": {"title": "t", "author": "", "language": "zh-CN",
             "package_epub": True, "epub_version": "3.0"},
})
xhtml = "".join(p.read_text(encoding="utf-8") for p in Path(tmp).rglob("content_*.xhtml"))
xhtml = xhtml.split("<body>", 1)[-1]
css = Path(tmp, "OEBPS", "style.css").read_text(encoding="utf-8")

print("导出正文片段:", " ".join(xhtml.split())[:200])
print("\n修复前（.ptoe-note 只匹配 p/span、无嵌套保护）：")
for s, label in sizes_of(xhtml, note_any=False, nest_guard=False):
    print(f"   {s:<7} {label}")
print("\n修复后（.ptoe-note 任意元素 + 嵌套不复合）：")
for s, label in sizes_of(xhtml, note_any=True, nest_guard=True):
    print(f"   {s:<7} {label}")

print("\n样式表实际规则:")
for line in css.splitlines():
    if "ptoe-note" in line or ("font-size" in line):
        print("   ", line.strip())
