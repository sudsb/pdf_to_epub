# -*- coding: utf-8 -*-
"""epubeditmanage.py — EPUB 编辑（后端服务）。

针对已有的 EPUB 文件提供一个轻量编辑界面：
- 启动本地 HTTP 服务 + 浏览器/pywebview 界面，左侧结构化为「文章列表」
  （每篇 = 一个一级标题 <h1> 章节，可改标题与正文）；
- 读取侧复用 epubmergemanage 的抽取管线（_find_opf / _spine_docs /
  _BodyExtractor）：按 spine 顺序保真抽取 <body> 内部 HTML，图片内联为
  data URI，再按 <h1> 切分为文章；
- 保存侧复用 htmlmanage.HTMLConverter：每篇文章正文 = <h1>标题</h1> + 清洗后
  的正文，交给 convert_document(merge_pages=True) 重新打包为 EPUB
  （merge_pages 会把全部页面正文合并后按 h1 重新切分——每篇恰好一个 <h1>，
  因此往返后仍保持「一篇 = 一页 = 一章」的结构，这是有意为之的语义）。

设计约束：
- 模块导入保持轻量（顶层只有标准库；epubmergemanage /
  correctmanage / htmlmanage / configmanage / webview / webbrowser
  全部在函数内部懒导入）；
- 所有 EPUB 内部路径一律使用 posixpath（正斜杠），禁止 os.path.join；
- 对外错误统一返回 {ok: False, error: 中文消息}。

用法：
    from epubeditmanage import epub_edit
    epub_edit("书.epub")
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET

__all__ = [
    "read_book",
    "default_save_path",
    "save_book",
    "epub_edit",
    "split_articles",
    "_EpubEditHandler",
]


# --------------------------------------------------------------------------
# 纯函数（无 HTTP，可单测）
# --------------------------------------------------------------------------

def split_articles(text: str, fallback_title: str):
    r"""把整本书的正文 HTML 按一级标题切分为文章列表。

    与 htmlmanage.convert_document 内部的 split_h1_chapters 同一套切分语义
    （该函数是嵌套的、未导出，这里按规格本地复刻，保证往返一致）：
    - 无 <h1 时：整段视为一篇文章（标题 = fallback_title）；
    - 有 <h1 时：按 (?=<h1(?:\s|>)) 前瞻切分，每个非空块为：
      * 以 <h1 开头 → 标题取首个 h1 内文（剥标签 / html.unescape / 压空白，
        空回退 fallback_title），文本为 </h1> 之后的剩余部分（保留原样，
        空串允许存在但不为 None）；
      * 首个 h1 之前的序言块 → 标题 = fallback_title。
    返回 [{'title': str, 'text': str}, ...]，只包含非空块。
    """
    if '<h1' not in text:
        return [{"title": fallback_title or "", "text": text.strip()}]
    out = []
    for chunk in (c for c in re.split(r'(?=<h1(?:\s|>))', text) if c.strip()):
        if chunk[0:3].lower() == '<h1':
            m = re.search(r'<h1[^>]*>(.*?)</h1>', chunk, flags=re.S)
            if m:
                t = html.unescape(re.sub(r'<[^>]+>', '', m.group(1)))
                t = re.sub(r'\s+', ' ', t).strip()
                out.append({"title": t or fallback_title, "text": chunk[m.end():].strip()})
            else:
                # 以 <h1 开头却匹配不到闭合标签（理论上不会走到）——按序言兜底
                out.append({"title": fallback_title, "text": chunk.strip()})
        else:
            out.append({"title": fallback_title, "text": chunk.strip()})
    return out


def _book_meta(zf: zipfile.ZipFile, opf_path: str):
    """从 OPF 解析书名/作者；解析失败或缺失时回退空串。"""
    title = ""
    author = ""
    try:
        root = ET.fromstring(zf.read(opf_path))
        for node in root.iter():
            if not isinstance(node.tag, str):
                continue
            if node.tag.endswith("title") and not title:
                title = (node.text or "").strip()
            elif node.tag.endswith("creator") and not author:
                author = (node.text or "").strip()
    except Exception:
        pass
    return title, author


_CACHE_DIR_NAME = "epubedit_cache"
_CACHE_VERSION = 1


def _cache_dir() -> Path:
    """解析后书籍缓存目录：程序目录/data/epubedit_cache（frozen 时为 exe 目录）。

    镜像 correctmanage._history_dir 模式——用户数据必须落在程序所在目录
    （frozen onefile 下 __file__ 指向 %TEMP% 解包目录，不能用于持久数据）。
    """
    from pdfmanage import app_base_dir

    return app_base_dir() / "data" / _CACHE_DIR_NAME


def _book_signature(path) -> str:
    """书籍签名：绝对路径 + 文件大小 + mtime_ns → sha1。

    任一源文件变化（编辑保存到 _编辑.epub 新文件、重新导出覆盖原文件等）
    都会改变签名，缓存自动失效。
    """
    st = os.stat(path)
    raw = f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _read_book_cache(cache_file: Path, sig: str):
    """读取缓存：版本/签名任一不符即视为失效（返回 None → 走完整解析）。"""
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        if data.get("version") != _CACHE_VERSION or data.get("sig") != sig:
            return None
        title, author = data.get("title"), data.get("author")
        articles = data.get("articles")
        if not isinstance(title, str) or not isinstance(author, str) or not isinstance(articles, list):
            return None
        return {
            "ok": True,
            "path": str(data.get("path") or ""),
            "title": title,
            "author": author,
            "articles": articles,
        }
    except Exception:
        return None


def _write_book_cache(cache_file: Path, payload: dict) -> None:
    """原子写缓存（mkstemp + os.replace，同 configmanage._atomic_write_json 模式）。

    任何失败静默跳过——缓存只是加速手段，绝不能因此破坏读取流程。
    """
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(cache_file.parent), prefix=".epubedit-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(cache_file))
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    except Exception:
        pass


def read_book(path):
    """读取 EPUB → {ok, path, title, author, articles}；失败返回 {ok: False, error}。

    正文抽取复用 epubmergemanage 的管线（spine 顺序、跳过导航页、<body> 保真、
    图片内联 data URI），全部正文按序 join('\n\n') 后再按 <h1> 切分为文章。

    性能（2026-09-13 新增）：
    - 多线程抽取：zip 各成员并发 read + 解析（ZipFile 内部 RLock 保护并发读），
      经 executor.map 保持 spine 顺序；
    - 磁盘缓存：按 绝对路径+大小+mtime_ns 签名缓存
      data/epubedit_cache/<sig>.json，命中时免去 zip 重读与图片重编码。
    """
    if not zipfile.is_zipfile(path):
        return {"ok": False, "error": "不是有效的 EPUB 文件（无法打开 zip 包）"}
    try:
        sig = _book_signature(path)
        cache_file = _cache_dir() / f"{sig}.json"
    except Exception:
        sig, cache_file = None, None
    if cache_file is not None and sig is not None:
        cached = _read_book_cache(cache_file, sig)
        if cached is not None:
            cached["path"] = str(path)
            return cached
    try:
        zf = zipfile.ZipFile(path)
    except Exception as exc:
        return {"ok": False, "error": f"打开 EPUB 失败：{exc}"}
    with zf:
        # 懒导入同一仓库的抽取 helper（epubmergemanage 自身也只依赖标准库）
        from epubmergemanage import _BodyExtractor, _find_opf, _spine_docs

        opf = _find_opf(zf)
        if not opf:
            return {"ok": False, "error": "EPUB 中未找到 container.xml 或 OPF 文件"}
        title, author = _book_meta(zf, opf)

        def read_member(name):
            try:
                return zf.read(name)
            except KeyError:
                return None

        def extract_doc(args):
            """单文档抽取：decode + BodyExtractor + body。
            任何异常按既有语义吞掉（坏文档跳过，不中断整批）。
            """
            href, base = args
            member = posixpath.normpath(posixpath.join(base, href)) if base else posixpath.normpath(href)
            data = read_member(member) or read_member(href)
            if data is None:
                return None
            try:
                doc_text = data.decode("utf-8", errors="replace")
            except Exception:
                return None
            # img src 相对的是文档自身目录（如 OEBPS/Text/），不是 OPF 目录
            parser = _BodyExtractor(read_member, posixpath.dirname(member))
            try:
                parser.feed(doc_text)
                parser.close()
            except Exception:
                return None
            body = parser.result()
            if body.strip():
                return body
            return None

        spine_items = list(_spine_docs(zf, opf))
        max_workers = max(1, min(8, os.cpu_count() or 2))
        bodies = []
        if spine_items:
            if len(spine_items) == 1:
                bodies = [b for b in (extract_doc(spine_items[0]),) if b is not None]
            else:
                with ThreadPoolExecutor(max_workers=max_workers) as ex:
                    bodies = [b for b in ex.map(extract_doc, spine_items) if b is not None]

    merged = "\n\n".join(bodies)
    fallback = (title or "").strip() or "正文"
    result = {
        "ok": True,
        "path": str(path),
        "title": title,
        "author": author,
        "articles": split_articles(merged, fallback),
    }
    if cache_file is not None:
        _write_book_cache(cache_file, {
            "sig": sig,
            "version": _CACHE_VERSION,
            "path": str(path),
            "title": title,
            "author": author,
            "articles": result["articles"],
        })
    return result
    if not zipfile.is_zipfile(path):
        return {"ok": False, "error": "不是有效的 EPUB 文件（无法打开 zip 包）"}
    try:
        zf = zipfile.ZipFile(path)
    except Exception as exc:
        return {"ok": False, "error": f"打开 EPUB 失败：{exc}"}
    with zf:
        # 懒导入同一仓库的抽取 helper（epubmergemanage 自身也只依赖标准库）
        from epubmergemanage import _BodyExtractor, _find_opf, _spine_docs

        opf = _find_opf(zf)
        if not opf:
            return {"ok": False, "error": "EPUB 中未找到 container.xml 或 OPF 文件"}
        title, author = _book_meta(zf, opf)

        def read_member(name):
            try:
                return zf.read(name)
            except KeyError:
                return None

        bodies = []
        for href, base in _spine_docs(zf, opf):
            member = posixpath.normpath(posixpath.join(base, href)) if base else posixpath.normpath(href)
            data = read_member(member) or read_member(href)
            if data is None:
                continue
            try:
                doc_text = data.decode("utf-8", errors="replace")
            except Exception:
                continue
            # img src 相对的是文档自身目录（如 OEBPS/Text/），不是 OPF 目录
            parser = _BodyExtractor(read_member, posixpath.dirname(member))
            try:
                parser.feed(doc_text)
                parser.close()
            except Exception:
                pass
            body = parser.result()
            if body.strip():
                bodies.append(body)

    merged = "\n\n".join(bodies)
    fallback = (title or "").strip() or "正文"
    return {
        "ok": True,
        "path": str(path),
        "title": title,
        "author": author,
        "articles": split_articles(merged, fallback),
    }


def default_save_path(path):
    """默认保存路径：源文件同目录 + 源文件名 + '_编辑.epub'；重名时在 .epub 前加 ' (n)'。"""
    p = Path(path)
    base = p.with_name(p.stem + "_编辑.epub")
    if not base.exists():
        return str(base)
    n = 1
    while True:
        cand = p.with_name(f"{p.stem}_编辑 ({n}).epub")
        if not cand.exists():
            return str(cand)
        n += 1


def save_book(articles, *, src_path, out_path=None):
    """把文章列表保存为新的 EPUB。

    每篇文章正文 = f"<h1>{html.escape(title)}</h1>" + "\n" + sanitize_html(text)；
    （标题单独转义，不经过清洗；正文用 correctmanage.sanitize_html 白名单清洗，
    保留 <img> data URI 等合法内容。）随后交给 HTMLConverter(merge_pages=True)
    打包——merge_pages 会把全部页面正文合并后再按 <h1> 切分，由于每篇恰好一个
    <h1>，往返后保持「一篇 = 一页 = 一章」的结构。返回 {ok, path} 或 {ok: False, error}。
    """
    if not articles:
        return {"ok": False, "error": "没有可保存的文章内容"}
    out_path = out_path or default_save_path(src_path)
    try:
        parent = os.path.dirname(os.path.abspath(out_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    except Exception as exc:
        return {"ok": False, "error": f"创建输出目录失败：{exc}"}

    # 懒导入重依赖（correctmanage 含内嵌 UI 大字符串，避免顶层加载）
    from correctmanage import sanitize_html

    pages = []
    for i, a in enumerate(articles, 1):
        a_title = a.get("title") or "" if isinstance(a, dict) else ""
        a_text = a.get("text") or "" if isinstance(a, dict) else ""
        escaped = html.escape(str(a_title), quote=True)
        try:
            safe_text = sanitize_html(str(a_text))
        except Exception:
            safe_text = str(a_text)
        if not safe_text:
            safe_text = ""
        pages.append({"page": i, "text": f"<h1>{escaped}</h1>" + "\n" + safe_text})

    first_title = (articles[0].get("title") or "").strip() if isinstance(articles[0], dict) else ""
    meta_title = first_title or Path(src_path).stem  # 无文章标题时用源文件名兜底

    tmp_dir = tempfile.mkdtemp(prefix="ptoe_epubedit_")
    try:
        from htmlmanage import HTMLConverter

        structured = {
            "pages": pages,
            "meta": {
                "title": meta_title,
                "author": "",
                "language": "zh-CN",
                "epub_version": "3.0",
                "package_epub": True,
                "epub_path": out_path,
            },
        }
        res = HTMLConverter(tmp_dir).convert_document(structured, merge_pages=True)
    except Exception as exc:
        return {"ok": False, "error": f"生成 EPUB 失败：{exc}"}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if not isinstance(res, dict):
        return {"ok": False, "error": "生成 EPUB 失败：未知错误"}
    err = res.get("epub_error")
    if err:
        return {"ok": False, "error": f"生成 EPUB 失败：{err}"}
    out = res.get("epub")
    if not out or not os.path.isfile(out):
        return {"ok": False, "error": "生成 EPUB 失败：未生成输出文件"}
    return {"ok": True, "path": out}


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------

class _EpubEditHandler(BaseHTTPRequestHandler):
    """EPUB 编辑界面后端：/（界面）、/ui/epubedit.js、/api/book、/api/ping、
    /api/save、/api/convert、/api/clean、/api/bye。"""

    protocol_version = "HTTP/1.1"
    server_version = "ptoe-epubedit/1.0"
    sys_version = ""

    # -- helpers --

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8", extra=None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            # 客户端断开/套接字错误：吞掉以保服务存活
            pass

    def _json(self, obj) -> bytes:
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")

    def _read_body(self):
        """读取请求体并解析 JSON；无请求体或解析失败返回 None。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return None
        try:
            data = self.rfile.read(length)
            return json.loads(data.decode("utf-8"))
        except Exception:
            return None

    def _asset_path(self, name: str) -> str:
        """定位前端静态资源（ui/ 目录）；frozen exe 时从 _MEIPASS 读取。"""
        if hasattr(sys, "_MEIPASS"):
            return os.path.join(sys._MEIPASS, "ui", name)
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", name)

    def log_message(self, format: str, *args) -> None:
        # 静默访问日志，避免终端刷屏
        return

    # -- GET --

    def do_GET(self) -> None:  # noqa: N802（http.server 命名约定）
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve_asset("epubedit.html", "text/html; charset=utf-8")
            return
        if path == "/ui/epubedit.js":
            self._serve_asset("epubedit.js", "application/javascript; charset=utf-8")
            return
        if path == "/api/book":
            with self.server.state["lock"]:
                book = dict(self.server.state["book"])
            self._send(200, self._json({"ok": True, **book}))
            return
        if path == "/api/ping":
            with self.server.state["lock"]:
                self.server.state["gone_at"] = None
            self._send(200, self._json({"ok": True}))
            return
        self._send(404, self._json({"ok": False, "error": "未找到接口"}))

    def _serve_asset(self, name: str, ctype: str) -> None:
        """读取 ui/ 下的静态资源返回；文件缺失（前端另一条线负责产出）→ 404。"""
        fp = self._asset_path(name)
        if not os.path.isfile(fp):
            self._send(404, self._json({"ok": False, "error": "未找到界面文件"}), "application/json; charset=utf-8")
            return
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except Exception as exc:
            self._send(500, self._json({"ok": False, "error": f"读取界面文件失败：{exc}"}))
            return
        self._send(200, data, ctype)

    # -- POST --

    def do_POST(self) -> None:  # noqa: N802（http.server 命名约定）
        path = self.path.split("?", 1)[0]
        if path == "/api/save":
            self._api_save()
            return
        if path == "/api/convert":
            self._api_convert()
            return
        if path == "/api/clean":
            self._api_clean()
            return
        if path == "/api/bye":
            with self.server.state["lock"]:
                self.server.state["gone_at"] = time.monotonic()
            self._send(200, self._json({"ok": True, "bye": True}))
            return
        self._send(404, self._json({"ok": False, "error": "未找到接口"}))

    def _api_save(self) -> None:
        """保存：body {path?: str, articles: [{title, text}]}。打包耗时，锁外执行。"""
        body = self._read_body()
        if body is None:
            self._send(400, self._json({"ok": False, "error": "无效的 JSON"}))
            return
        articles = body.get("articles")
        if not isinstance(articles, list):
            self._send(400, self._json({"ok": False, "error": "articles 必须是数组"}))
            return
        out_path = body.get("path") or None
        src_path = self.server.state["book"]["path"]
        # 打包可能耗时数秒：必须在锁外执行，避免阻塞 /api/book 等轻量接口
        res = save_book(articles, src_path=src_path, out_path=out_path)
        if not res.get("ok"):
            self._send(200, self._json({"ok": False, "error": res.get("error", "保存失败")}))
            return
        with self.server.state["lock"]:
            self.server.state["book"]["articles"] = articles
            self.server.state["saved"] = True
        self._send(200, self._json({"ok": True, "path": res.get("path")}))

    def _api_convert(self) -> None:
        """繁简转换：body {html: str, mode: 't2s'|'s2t'} → {ok: True, html}。
        无状态——只返回转换结果，由浏览器更新界面（保存时才落盘）。
        只转换文本节点、保留标签/标记不变（复用 correctmanage.convert_text_html，
        懒导入以保持模块顶层轻量）。"""
        body = self._read_body()
        if body is None:
            self._send(400, self._json({"ok": False, "error": "无效的 JSON"}))
            return
        html_text = body.get("html")
        mode = body.get("mode")
        if not isinstance(html_text, str):
            self._send(400, self._json({"ok": False, "error": "html 必须为字符串"}))
            return
        if mode not in ("t2s", "s2t"):
            self._send(400, self._json({"ok": False, "error": "mode 必须为 t2s 或 s2t"}))
            return
        try:
            from correctmanage import convert_text_html

            converted = convert_text_html(html_text, mode)
        except Exception as exc:
            self._send(200, self._json({"ok": False, "error": f"繁简转换失败：{exc}"}))
            return
        self._send(200, self._json({"ok": True, "html": converted}))

    def _api_clean(self) -> None:
        """文本智能清理：body {html: str} → {ok: True, html}。无状态。
        调用 correctmanage.clean_page_html（段首 #/* 符号、中英文标点归一、
        HTML 标签残留清理；默认不合并段落，懒导入保持模块顶层轻量）。"""
        body = self._read_body()
        if body is None:
            self._send(400, self._json({"ok": False, "error": "无效的 JSON"}))
            return
        html_text = body.get("html")
        if not isinstance(html_text, str):
            self._send(400, self._json({"ok": False, "error": "html 必须为字符串"}))
            return
        try:
            from correctmanage import clean_page_html

            cleaned = clean_page_html(html_text)
        except Exception as exc:
            self._send(200, self._json({"ok": False, "error": f"清理失败：{exc}"}))
            return
        self._send(200, self._json({"ok": True, "html": cleaned}))


# --------------------------------------------------------------------------
# 入口：启动服务 + 打开界面 + 等待浏览器关闭
# --------------------------------------------------------------------------

def _open_browser(url: str) -> None:
    """打开外部浏览器；优先使用 config.json browser 键指定的浏览器路径。"""
    _br_path = ""
    try:
        from configmanage import get_config

        _br_path = (get_config(show_dialogs=False) or {}).get("browser", "")
    except Exception:
        pass
    try:
        if _br_path:
            import subprocess

            subprocess.Popen([_br_path, url])
        else:
            webbrowser.open(url)
    except Exception:
        try:
            webbrowser.open(url)
        except Exception:
            pass


def _open_display(url: str, title: str, state: dict) -> None:
    """按 config.json gui_display 打开界面（简化版，不含 tabmanage 合并窗口）。

    - 'pywebview'：内嵌窗口；webview.start() 必须在主线程 —— 已在主线程则内联
      阻塞（窗口关闭后返回），否则在独立线程运行；任何异常回退浏览器。
    - 其他值（含 'browser'）：config.json browser 路径或系统默认浏览器。
    """
    mode = "browser"
    try:
        from configmanage import get_config

        mode = (get_config(show_dialogs=False) or {}).get("gui_display", "browser")
    except Exception:
        mode = "browser"
    if mode == "pywebview":
        try:
            import webview  # noqa: PLC0415（可选依赖，失败即回退浏览器）

            win = webview.create_window(title, url)
            win.events.closed += lambda: state["finished"].set()
            if threading.current_thread() is threading.main_thread():
                webview.start()
            else:
                threading.Thread(target=webview.start, daemon=True).start()
            return
        except Exception:
            pass
    _open_browser(url)


def epub_edit(path, *, host="127.0.0.1", port=0, open_browser=True, idle_timeout=600):
    """EPUB 编辑主入口：读取 → 起本地服务 → 打开界面 → 阻塞至浏览器关闭/超时。"""
    info = read_book(path)
    if not info.get("ok"):
        print(f"错误：{info.get('error')}")
        return

    state = {
        "book": {
            "title": info.get("title", ""),
            "author": info.get("author", ""),
            "articles": info.get("articles", []),
            "path": str(info.get("path") or path),
        },
        "finished": threading.Event(),
        "lock": threading.RLock(),
        "gone_at": None,
        "saved": False,
    }

    server = ThreadingHTTPServer((host, port), _EpubEditHandler)
    server.daemon_threads = True
    server.state = state
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()

    real_port = server.server_address[1]
    url = f"http://127.0.0.1:{real_port}/"
    # GUI 靠解析这行 stdout 拿到地址（guimanage._CORRECT_URL_RE），必须原样输出
    print(url, flush=True)
    if open_browser:
        _open_display(url, "EPUB 编辑", state)

    try:
        while not state["finished"].is_set():
            time.sleep(0.5)
            gone = state["gone_at"]
            # 浏览器发出 /api/bye（pagehide）后超时未再 /api/ping → 自动退出
            if gone and time.monotonic() - gone > idle_timeout:
                state["finished"].set()
    finally:
        # Windows 下必须先 shutdown 再 server_close（顺序颠倒抛 WinError 10038）
        server.shutdown()
        server.server_close()
        serve_thread.join(timeout=5)
        print("已退出 EPUB 编辑。")