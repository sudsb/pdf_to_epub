# -*- coding: utf-8 -*-
"""临时探测（可删除）：注释标记/注释内容是否落在标题块内（标题字号更大 → 注释字号不一）。"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, ".")

from correctmanage import apply_markers, sanitize_html  # noqa: E402
from htmlmanage import HTMLConverter  # noqa: E402

BLOCK_RE = re.compile(r"<(/?)(h[1-6]|p|div)\b([^>]*)>", re.I)
NOTE_MARK = re.compile(r'<span[^>]*data-ptoe-marker="note"[^>]*>', re.I)

# 1) 语料里"注释标记出现在标题块内"的页面
hits: list[tuple[str, str, str]] = []
for fp in sorted(Path("data/correction_history").glob("*.json"), key=lambda p: p.stat().st_size, reverse=True):
    if fp.name.endswith(".images.json"):
        continue
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        continue
    pages = d.get("pages")
    if not isinstance(pages, dict):
        continue
    for k, v in pages.items():
        t = str(v or "")
        if 'data-ptoe-marker="note"' not in t:
            continue
        cur: str | None = None
        for m in BLOCK_RE.finditer(t):
            closing, tag, _attrs = m.group(1), m.group(2).lower(), m.group(3)
            cur = None if closing else tag
        # 简化：按块切分，找标题块里含注释标记
        for bm in re.finditer(r"<(h[1-6])\b[^>]*>(.*?)</\1>", t, re.S | re.I):
            if NOTE_MARK.search(bm.group(2)):
                hits.append((fp.name, str(k), bm.group(0)[:260]))
print("注释标记位于标题块内的页面数:", len(hits))
for h in hits[:4]:
    print("   ", h[0], "页", h[1], "→", h[2].replace("\n", " ")[:200])

# 2) 走一遍导出，量出每个 ptoe-note 元素的实际字号（按父元素继承）
if hits:
    fn, k, _ = hits[0]
    data = json.loads((Path("data/correction_history") / fn).read_text(encoding="utf-8"))
    page_html = str((data.get("pages") or {}).get(k) or "")
    items = [{"page": int(k), "text": sanitize_html(page_html)}]
    arts = apply_markers(items)
    print("\n=== apply_markers 后含注释标记的文章片段 ===")
    joined = "\n".join(a.get("text", "") for a in arts)
    m = re.search(r"<h[1-6][^>]*>.{0,200}", joined)
    print("   ", (m.group(0) if m else joined[:300]).replace("\n", " "))
    import tempfile
    tmp = tempfile.mkdtemp(prefix="probe_note_")
    res = HTMLConverter(output_dir=tmp, epub_version="3.0").convert_document({
        "articles": arts, "pages": items,
        "body": "\n\n".join(p["text"] for p in items),
        "paragraphs": [{"page": items[0]["page"], "text": items[0]["text"]}],
        "meta": {"title": "t", "author": "", "language": "zh-CN",
                 "package_epub": True, "epub_version": "3.0"},
    })
    xhtml = "".join(p.read_text(encoding="utf-8") for p in Path(tmp).rglob("content_*.xhtml"))
    for hm in re.finditer(r"<h[1-6][^>]*>.*?</h[1-6]>", xhtml, re.S):
        if "ptoe-note" in hm.group(0):
            print("\n=== 导出后标题内的注释 ===")
            print("   ", hm.group(0).replace("\n", " ")[:300])
    css = Path(tmp, "OEBPS", "style.css").read_text(encoding="utf-8")
    print("\n=== EPUB CSS 里的标题/注释字号规则 ===")
    for line in css.splitlines():
        if re.search(r"(h1|h2|font-size|ptoe-note)", line):
            print("   ", line.strip())
