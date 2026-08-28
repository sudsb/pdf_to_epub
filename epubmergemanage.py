# -*- coding: utf-8 -*-
"""多 EPUB 合并引擎（配置中心工具页「多 EPUB 合并」后端）。

纯标准库实现：逐个解包输入 EPUB（zipfile），按 OPF spine 顺序抽取正文
（html.parser 保真提取 <body> 内部 HTML），图片内联为 data URI，最后交给
htmlmanage.HTMLConverter.convert_document 统一打包成单个 EPUB。
重依赖（htmlmanage/epubmanage）在 merge_epubs 内部懒加载，模块导入零开销；
guimanage._merge_worker 通过「能否 import 本模块」判断功能是否就绪。
"""

from __future__ import annotations

import base64
import posixpath
import re
import shutil
import tempfile
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

__all__ = ["merge_epubs"]

# 单张图片超过该大小则跳过并提示（避免内存/EPUB 体积爆炸）
_MAX_IMG_BYTES = 20 * 1024 * 1024

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
}

# 自闭合（void）元素：重建 HTML 时统一写成 <tag .../> 以贴近 XHTML
_VOID_TAGS = {"img", "br", "hr", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}

# 原书目录/导航页文件名（无 properties 标记的 EPUB2 风格兜底）
_NAV_NAME_RE = re.compile(r"^(nav|toc|contents)[._-]?\w*\.(xhtml|html|htm)$", re.IGNORECASE)


def _default_out_path(epub_paths, title: str = "") -> str:
    """默认输出路径：第一个输入文件同目录下的 合并_<书名或首文件名>.epub，重名加 (n)。"""
    first = Path(epub_paths[0])
    stem = (title or "").strip() or first.stem
    candidate = first.with_name(f"合并_{stem}.epub")
    n = 1
    while candidate.exists():
        candidate = first.with_name(f"合并_{stem} ({n}).epub")
        n += 1
    return str(candidate)


class _BodyExtractor(HTMLParser):
    """保真抽取单个 XHTML 的 <body> 内部 HTML。

    - 丢弃 link[rel=stylesheet] 与 script（含内容）——样式由合并后的统一 CSS 提供；
    - img src 改写为 data URI（从同一 zip 内读取；缺失/超限/损坏则丢弃整个 img）；
      同时丢弃 srcset，避免残留失效引用；
    - 实体引用原样保留（convert_charrefs=False）。
    """

    def __init__(self, read_member, base_dir: str = ""):
        super().__init__(convert_charrefs=False)
        self._read = read_member          # zip 成员读取函数 (name) -> bytes | None
        self._base = base_dir             # OPF 所在目录（解析相对路径用）
        self._out = []
        self._skip = 0                    # >0 表示正在丢弃 link/script 子树
        self._in_body = False

    # -- 对外 --
    def result(self) -> str:
        return "".join(self._out)

    # -- 内部 --
    def _resolve(self, src: str) -> str | None:
        """把文档内相对路径解析成 zip 成员名；找不到返回 None。"""
        if not src or src.startswith(("data:", "http://", "https://", "//")):
            return None
        clean = src.split("#", 1)[0].split("?", 1)[0]
        if not clean:
            return None
        joined = posixpath.normpath(posixpath.join(self._base, clean)) if self._base else posixpath.normpath(clean)
        for name in (joined, clean.lstrip("/")):
            if self._read(name) is not None:
                return name
        return None

    def _inline_img(self, attrs):
        """构造内联后的 <img/> 标签字符串；无法内联时返回 None（整标签丢弃）。"""
        src = ""
        kept = []
        for name, value in attrs:
            if name is None:
                continue
            low = name.lower()
            if low == "src":
                src = value or ""
                continue
            if low in ("srcset",):  # 内联后失效，直接去掉
                continue
            kept.append((name, value))
        member = self._resolve(src)
        data = self._read(member) if member else None
        if not data:
            return None
        if len(data) > _MAX_IMG_BYTES:
            return None  # 超大图跳过（调用方已统计提示）
        ext = posixpath.splitext(member or "")[1].lower()
        mime = _MIME_BY_EXT.get(ext, "application/octet-stream")
        uri = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        parts = [f'src="{uri}"']
        for name, value in kept:
            if value is None:
                parts.append(name)
            else:
                escaped = (value or "").replace("&", "&amp;").replace('"', "&quot;")
                parts.append(f'{name}="{escaped}"')
        return "<img " + " ".join(parts) + "/>"

    def _emit_start(self, tag, attrs):
        pieces = [f"<{tag}"]
        for name, value in attrs:
            if name is None:
                continue
            if value is None:
                pieces.append(f" {name}")
            else:
                escaped = (value or "").replace("&", "&amp;").replace('"', "&quot;")
                pieces.append(f' {name}="{escaped}"')
        pieces.append("/>" if tag in _VOID_TAGS else ">")
        self._out.append("".join(pieces))

    # -- HTMLParser 回调 --
    def handle_starttag(self, tag, attrs):
        low = tag.lower()
        if low == "body":
            self._in_body = True
            return
        if low in ("link", "script"):
            if low == "script":
                self._skip += 1
            return
        if not self._in_body or self._skip:
            return
        if low == "img":
            inline = self._inline_img(attrs)
            if inline is not None:
                self._out.append(inline)
            return
        self._emit_start(low, attrs)

    def handle_startendtag(self, tag, attrs):
        low = tag.lower()
        if low == "body":
            self._in_body = True
            return
        if low in ("link", "script") or not self._in_body or self._skip:
            return
        if low == "img":
            inline = self._inline_img(attrs)
            if inline is not None:
                self._out.append(inline)
            return
        self._emit_start(low, attrs)

    def handle_endtag(self, tag):
        low = tag.lower()
        if low == "body":
            self._in_body = False
            return
        if low == "script" and self._skip:
            self._skip -= 1
            return
        if not self._in_body or self._skip or low in ("link", "img", "br", "hr"):
            return
        self._out.append(f"</{low}>")

    def handle_data(self, data):
        if self._in_body and not self._skip:
            self._out.append(data)

    def handle_entityref(self, name):
        if self._in_body and not self._skip:
            self._out.append(f"&{name};")

    def handle_charref(self, name):
        if self._in_body and not self._skip:
            self._out.append(f"&#{name};")


def _find_opf(zf: zipfile.ZipFile) -> str | None:
    """从 META-INF/container.xml 找 OPF 路径。"""
    try:
        data = zf.read("META-INF/container.xml")
    except KeyError:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    for node in root.iter():
        if isinstance(node.tag, str) and node.tag.endswith("rootfile"):
            full = node.get("full-path")
            if full:
                return full
    return None


def _spine_docs(zf: zipfile.ZipFile, opf_path: str):
    """按 spine 顺序返回 [(成员名, opf目录), ...]；无 spine 时回退 manifest 全部 html。

    跳过导航文档（EPUB3 properties="nav"、ncx、以及文件名形如 nav/toc/contents 的
    html）——原书目录页若被当作正文提取，会在合并结果里混入多余的「目录」章节。
    """
    try:
        root = ET.fromstring(zf.read(opf_path))
    except Exception:
        return []
    opf_dir = posixpath.dirname(opf_path)
    manifest = {}
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        if node.tag.endswith("item"):
            iid, href = node.get("id"), node.get("href")
            mt = (node.get("media-type") or "").lower()
            props = (node.get("properties") or "").split()
            if not iid or not href:
                continue
            if "nav" in props or "dtbncx" in mt:
                continue
            if _NAV_NAME_RE.match(posixpath.basename(href)):
                continue
            if "html" in mt or href.lower().endswith((".xhtml", ".html", ".htm")):
                manifest[iid] = href
    docs = []
    for node in root.iter():
        if isinstance(node.tag, str) and node.tag.endswith("itemref"):
            href = manifest.get(node.get("idref"))
            if href:
                docs.append((href, opf_dir))
    if not docs:
        docs = [(href, opf_dir) for href in manifest.values()]
    return docs


def _extract_book(path: str, log) -> list:
    """抽取单本书的全部章节 body HTML；损坏/缺 OPF 返回 [] 并提示。"""
    try:
        zf = zipfile.ZipFile(path)
    except Exception as exc:
        log(f"警告：无法打开 {Path(path).name}（{exc}），已跳过")
        return []
    chapters = []
    with zf:
        opf = _find_opf(zf)
        if not opf:
            log(f"警告：{Path(path).name} 缺少 container.xml 或 OPF，已跳过")
            return []

        def read_member(name):
            try:
                return zf.read(name)
            except KeyError:
                return None

        for href, base in _spine_docs(zf, opf):
            member = posixpath.normpath(posixpath.join(base, href)) if base else posixpath.normpath(href)
            data = read_member(member) or read_member(href)
            if data is None:
                continue
            try:
                html = data.decode("utf-8", errors="replace")
            except Exception:
                continue
            # img src 相对的是文档自身目录（如 OEBPS/Text/），不是 OPF 目录
            parser = _BodyExtractor(read_member, posixpath.dirname(member))
            try:
                parser.feed(html)
                parser.close()
            except Exception:
                pass
            body = parser.result()
            if body.strip():
                chapters.append(body)
    return chapters


def merge_epubs(epub_paths, *, out_path=None, title="", author="", lang="zh-CN",
                progress=None, should_stop=None) -> dict:
    """合并多个 EPUB 为一个新 EPUB。

    Args:
      epub_paths: 输入 .epub 路径列表（≥2，顺序即合并顺序）。
      out_path:   输出路径；None 时存到第一个文件同目录（重名自动加 (n)）。
      title/author/lang: 元数据；title 空则用「合并电子书」。
      progress:   callable(str)，进度日志回调（GUI 环形缓冲）。
      should_stop: callable() -> bool，在书与章节之间检查，True 则取消。

    Returns:
      {ok: True, out_path: str} 或 {ok: False, error: 中文错误信息}
    """
    log = progress or (lambda msg: None)
    stop = should_stop or (lambda: False)
    tmp_dir = None
    try:
        paths = [str(p) for p in (epub_paths or [])]
        if len(paths) < 2:
            return {"ok": False, "error": "至少需要选择 2 个 EPUB 文件"}
        bad = [p for p in paths if not p.lower().endswith(".epub")]
        if bad:
            return {"ok": False, "error": f"不是 EPUB 文件：{Path(bad[0]).name}"}
        missing = [p for p in paths if not Path(p).is_file()]
        if missing:
            return {"ok": False, "error": f"文件不存在：{missing[0]}"}
        target = out_path or _default_out_path(paths, title)

        pages = []
        total = len(paths)
        for idx, p in enumerate(paths, 1):
            if stop():
                return {"ok": False, "error": "已取消"}
            name = Path(p).name
            log(f"[{idx}/{total}] 正在读取：{name}")
            chapters = _extract_book(p, log)
            if not chapters:
                log(f"[{idx}/{total}] {name} 未提取到正文，已跳过")
                continue
            for body in chapters:
                if stop():
                    return {"ok": False, "error": "已取消"}
                pages.append({"page": len(pages) + 1, "text": body})
            log(f"[{idx}/{total}] {name} 提取 {len(chapters)} 章")
        if not pages:
            return {"ok": False, "error": "所有文件均未提取到有效正文"}

        log(f"共提取 {len(pages)} 章，正在生成合并 EPUB …")
        from htmlmanage import HTMLConverter  # 懒加载重依赖

        tmp_dir = tempfile.mkdtemp(prefix="ptoe_merge_")
        structured = {
            "pages": pages,
            "meta": {
                "title": (title or "").strip() or "合并电子书",
                "author": (author or "").strip(),
                "language": lang or "zh-CN",
                "epub_version": "3.0",
                "package_epub": True,
                "epub_path": target,
            },
        }
        result = HTMLConverter(tmp_dir).convert_document(structured, merge_pages=True)
        err = result.get("epub_error") if isinstance(result, dict) else None
        if err:
            return {"ok": False, "error": f"生成 EPUB 失败：{err}"}
        out = result.get("epub") or target
        log(f"完成：{out}")
        return {"ok": True, "out_path": out}
    except Exception as exc:
        return {"ok": False, "error": f"合并失败：{exc}"}
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
