# -*- coding: utf-8 -*-
"""临时复现（验证后删除）：文字包围（行内格式）在 EPUB 导出链路中的留存情况。"""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, ".")

from correctmanage import apply_markers, sanitize_html  # noqa: E402
from htmlmanage import HTMLConverter  # noqa: E402

PAGE = (
    "<p>普通文字<span class=\"ptoe-underline\">下划线</span>"
    "<span class=\"ptoe-underdot\">下加点</span>"
    "<span class=\"ptoe-strike\">删除线</span>"
    "<span class=\"ptoe-charbox\">字符边框</span>"
    "<span class=\"ptoe-shade\">底纹</span>"
    "<span class=\"ptoe-highlight\">突显</span>"
    "<span class=\"ptoe-sup\">上标</span>"
    "<span class=\"ptoe-sub\">下标</span>后文</p>"
    "<p>第二段<span class=\"ptoe-underline\">同样带下划线</span>。</p>"
)

src_items = [{"page": 1, "text": sanitize_html(PAGE)}]
print("1) sanitize 后：")
print("   ", src_items[0]["text"][:160])

articles = apply_markers(src_items)
print("\n2) apply_markers 后（articles[0] 前 3 块）：")
for blk in (articles[0].get("blocks") or articles[0].get("content") or [])[:3]:
    print("   ", json.dumps(blk, ensure_ascii=False)[:200])
if not articles:
    print("   (articles 为空)")
else:
    print("   articles[0] keys:", list(articles[0].keys()))
    print("   article html 片段:", json.dumps(articles[0], ensure_ascii=False)[:400])

tmp = tempfile.mkdtemp(prefix="probe_epub_")
structured = {
    "articles": articles,
    "pages": src_items,
    "body": "\n\n".join(p["text"] for p in src_items),
    "paragraphs": [{"page": p["page"], "text": p["text"]} for p in src_items],
    "meta": {"title": "探测", "author": "", "language": "zh-CN",
             "package_epub": True, "epub_version": "3.0"},
}
res = HTMLConverter(output_dir=tmp, epub_version="3.0").convert_document(structured)
print("\n3) convert_document 返回：", {k: str(v)[:120] for k, v in res.items() if k != "toc"})

xhtmls = sorted(Path(tmp).rglob("*.xhtml"))
print("\n4) 生成的 xhtml：", [str(p.relative_to(tmp)) for p in xhtmls])
for p in xhtmls:
    t = p.read_text(encoding="utf-8")
    if "ptoe-underline" in t or "ptoe-sup" in t or "普通文字" in t:
        i = max(0, t.find("普通文字") - 120)
        print(f"\n   ---- {p.name} 片段 ----")
        print("   ", t[i:i + 600].replace("\n", " "))

epub = res.get("epub")
if epub and Path(epub).is_file():
    with zipfile.ZipFile(epub) as z:
        names = z.namelist()
        css_names = [n for n in names if n.endswith(".css")]
        print("\n5) EPUB 内文件：", names)
        for n in css_names:
            css = z.read(n).decode("utf-8", "ignore")
            for cls in ("ptoe-underline", "ptoe-underdot", "ptoe-sup", "ptoe-charbox"):
                print(f"   CSS {n} 含 {cls}: {('.' + cls) in css}")
