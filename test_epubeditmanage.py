# -*- coding: utf-8 -*-
"""epubeditmanage 单元测试（stdlib unittest，风格对齐 test_correctmanage）。

覆盖：read_book（含序言回退/图片内联/无效文件）、split_articles 纯函数、
default_save_path 去重、save_book 往返（HTMLConverter 真实打包）、
_EpubEditHandler 各端点（BaseHTTPRequestHandler.__new__ + 假请求对象模式）。
"""

from __future__ import annotations

import base64
import io
import json as _json
import os
import shutil
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

import epubeditmanage as em

# 1x1 透明 PNG（用于测试 _BodyExtractor 图片内联为 data URI 的路径）
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _xhtml(title: str, body: str) -> str:
    """构造一份完整 XHTML 文档字符串。"""
    t = f"<title>{title}</title>" if title else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" lang="zh-CN">\n'
        f"<head><meta charset=\"utf-8\"/>{t}</head>\n"
        f"<body>\n{body}\n</body>\n</html>"
    )


def make_book(path, n_docs=3):
    """构建最小合法 EPUB（mimetype 首项 ZIP_STORED + container + OPF + nav + 1 图）。

    - 文档 0：序言，无 <h1>（用于测试回退标题「正文」）；
    - 文档 1..n-1：<h1>第X章</h1> + <p>正文X</p>；文档 1 另含一张内联图。
    - 图片 zip 成员放 OEBPS/Text/Images/img1.png（img src 相对文档自身目录）。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    docs = [("OEBPS/Text/chap0.xhtml", _xhtml("", "<p>这是前言。</p>"))]
    for i in range(1, n_docs):
        body = f"<h1>第{i}章</h1><p>正文{i}</p>"
        if i == 1:
            body += '<p><img src="Images/img1.png" alt="图"/></p>'
        docs.append((f"OEBPS/Text/chap{i}.xhtml", _xhtml(f"第{i}章", body)))

    manifest = "\n".join(
        f'    <item id="c{i}" href="Text/chap{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(n_docs)
    )
    spine = "\n".join(f'    <itemref idref="c{i}"/>' for i in range(n_docs))
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>测试书</dc:title>
    <dc:creator>作者</dc:creator>
    <dc:identifier id="uid">test-book</dc:identifier>
    <dc:language>zh-CN</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
{manifest}
  </manifest>
  <spine>
{spine}
  </spine>
</package>"""
    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>目录</title></head>'
        '<body><nav epub:type="toc"><ol><li><a href="Text/chap1.xhtml">第1章</a></li>'
        "</ol></nav></body></html>"
    )
    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

    with zipfile.ZipFile(path, "w") as zf:
        # mimetype 必须是第一个成员且不压缩（EPUB 规范）
        zf.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container.encode("utf-8"))
        zf.writestr("OEBPS/content.opf", opf.encode("utf-8"))
        for member, data in docs:
            zf.writestr(member, data.encode("utf-8"))
        zf.writestr("OEBPS/nav.xhtml", nav.encode("utf-8"))
        zf.writestr("OEBPS/Text/Images/img1.png", _TINY_PNG)


class _BookCase(unittest.TestCase):
    """每个测试独立临时目录 + 3 文档测试书。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ptoe_epubedit_test_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        _patch_cache_dir(self)
        self.book = os.path.join(self.tmp, "src_book.epub")
        make_book(self.book)


def _patch_cache_dir(test):
    """把 epubeditmanage._cache_dir 指向本测试临时目录（测试不得写脏仓库 data/）。"""
    orig = em._cache_dir
    em._cache_dir = lambda: Path(test.tmp) / "epubedit_cache"
    test.addCleanup(lambda: setattr(em, "_cache_dir", orig))


class TestReadBook(_BookCase):
    def test_read_book_ok(self):
        res = em.read_book(self.book)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["title"], "测试书")
        self.assertEqual(res["author"], "作者")
        # n_docs=3：2 个 <h1> 章节 + 1 个序言 = 3 篇文章
        self.assertEqual(len(res["articles"]), 3)
        # 首篇 = 无 h1 的序言，标题回退为书名（规格：fallback = 书名，空才用「正文」）
        self.assertEqual(res["articles"][0]["title"], "测试书")
        self.assertIn("<p>这是前言。</p>", res["articles"][0]["text"])
        # 其余文章标题取自 <h1>
        self.assertEqual(res["articles"][1]["title"], "第1章")
        self.assertEqual(res["articles"][2]["title"], "第2章")
        # 文章 text 只保留 h1 之后的正文，不包含 h1 标签
        for art in res["articles"][1:]:
            self.assertNotIn("<h1", art["text"])
        self.assertIn("正文1", res["articles"][1]["text"])
        self.assertIn("正文2", res["articles"][2]["text"])
        # 图片已内联为 data URI（原文件名被替换；alt 属性保留）
        self.assertIn("data:image/", res["articles"][1]["text"])
        self.assertIn('alt="图"', res["articles"][1]["text"])

    def test_read_book_invalid(self):
        bad = os.path.join(self.tmp, "not_epub.txt")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("这不是一个 zip 文件")
        res = em.read_book(bad)
        self.assertFalse(res["ok"])
        self.assertIn("不是有效的 EPUB", res["error"])

    def test_read_book_missing(self):
        res = em.read_book(os.path.join(self.tmp, "不存在.epub"))
        self.assertFalse(res["ok"])

    def test_read_book_one_doc_no_h1(self):
        # 全书无 <h1>：整本作为单篇文章（回退标题）
        single = os.path.join(self.tmp, "no_h1.epub")
        make_book(single, n_docs=1)
        res = em.read_book(single)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(res["articles"]), 1)
        self.assertEqual(res["articles"][0]["title"], "测试书")
        self.assertIn("这是前言", res["articles"][0]["text"])


class TestSplitArticles(unittest.TestCase):
    def test_no_h1_single_article(self):
        arts = em.split_articles("<p>只有正文</p>", "书名")
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["title"], "书名")
        self.assertEqual(arts[0]["text"], "<p>只有正文</p>")

    def test_two_h1s(self):
        arts = em.split_articles(
            "<h1>第一章</h1><p>甲</p><h1>第二章</h1><p>乙</p>", "正文"
        )
        self.assertEqual(len(arts), 2)
        self.assertEqual(arts[0]["title"], "第一章")
        self.assertEqual(arts[0]["text"], "<p>甲</p>")
        self.assertEqual(arts[1]["title"], "第二章")
        self.assertEqual(arts[1]["text"], "<p>乙</p>")

    def test_empty_body_after_h1(self):
        arts = em.split_articles("<h1>标题</h1><p>甲</p><h1>空的</h1>", "回退")
        self.assertEqual(len(arts), 2)
        self.assertEqual(arts[0]["title"], "标题")
        self.assertEqual(arts[1]["title"], "空的")
        self.assertEqual(arts[1]["text"], "", "h1 后无内容时 text 应为空串而非 None")

    def test_preamble_plus_h1(self):
        arts = em.split_articles("<p>序言</p><h1>第一章</h1><p>甲</p>", "正文")
        self.assertEqual(len(arts), 2)
        self.assertEqual(arts[0]["title"], "正文")
        self.assertEqual(arts[0]["text"], "<p>序言</p>")
        self.assertEqual(arts[1]["title"], "第一章")

    def test_title_unescape_and_ws(self):
        arts = em.split_articles("<h1>A &amp;  B</h1><p>x</p>", "回退")
        self.assertEqual(arts[0]["title"], "A & B")

    def test_empty_title_falls_back(self):
        arts = em.split_articles("<h1>   </h1><p>x</p>", "回退")
        self.assertEqual(arts[0]["title"], "回退")

    def test_empty_text_returns_single_fallback(self):
        arts = em.split_articles("", "正文")
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["title"], "正文")
        self.assertEqual(arts[0]["text"], "")


class TestDefaultSavePath(_BookCase):
    def test_basic_and_dedup(self):
        p1 = em.default_save_path(self.book)
        self.assertTrue(p1.endswith("_编辑.epub"), p1)
        self.assertFalse(os.path.exists(p1))
        # 预先创建默认名 → 应得到 (1)
        open(p1, "wb").close()
        p2 = em.default_save_path(self.book)
        self.assertTrue(p2.endswith("_编辑 (1).epub"), p2)
        # 再创建 (1) → 应得到 (2)
        open(p2, "wb").close()
        p3 = em.default_save_path(self.book)
        self.assertTrue(p3.endswith("_编辑 (2).epub"), p3)


class TestSaveBook(_BookCase):
    @staticmethod
    def _norm_text(s: str) -> str:
        # htmlmanage pages+merge 路径在非末章块尾会产生空 <p></p>（join 分隔符
        # \n\n 落进前一 chunk 所致）：纯渲染产物，语义往返不受影响，比较前剔除。
        import re as _re

        return _re.sub(r"<p></p>\s*", "", s).strip()

    def test_save_round_trip(self):
        info = em.read_book(self.book)
        self.assertTrue(info["ok"], info)
        arts = info["articles"]
        # 修改第 2 篇文章（标题含 & 验证转义往返；不含 CJK 空隙空白——
        # htmlmanage._strip_ws_text 会清理 CJK 上下文的空格，是既有文档行为）
        arts[1]["title"] = "第一章&修改"
        arts[1]["text"] = "<p>修改后的正文。</p>"
        res = em.save_book(arts, src_path=self.book)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["path"].endswith("_编辑.epub"), res)
        self.assertTrue(os.path.isfile(res["path"]))
        self.assertTrue(zipfile.is_zipfile(res["path"]))
        # mimetype 必须是第一个成员
        with zipfile.ZipFile(res["path"]) as zf:
            names = zf.namelist()
        self.assertEqual(names[0], "mimetype")
        # 重新读取输出 → 文章数量与标题/正文应往返一致
        info2 = em.read_book(res["path"])
        self.assertTrue(info2["ok"], info2)
        self.assertEqual(len(info2["articles"]), len(arts))
        for a, b in zip(info2["articles"], arts):
            self.assertEqual(a["title"], b["title"])
            self.assertEqual(self._norm_text(a["text"]), self._norm_text(b["text"]))

    def test_save_explicit_out_path(self):
        info = em.read_book(self.book)
        out = os.path.join(self.tmp, "定制名.epub")
        res = em.save_book(info["articles"], src_path=self.book, out_path=out)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["path"], out)
        self.assertTrue(os.path.isfile(out))

    def test_save_empty_articles(self):
        res = em.save_book([], src_path=self.book)
        self.assertFalse(res["ok"])


# --------------------------------------------------------------------------
# Handler 假请求测试（BaseHTTPRequestHandler.__new__ 模式，见测试规格）
# --------------------------------------------------------------------------

def _make_handler(handler_cls, state, path, command="GET", body=None):
    """构造一个绕过 socket 的 handler 实例。

    用 __new__ 跳过 __init__（其 setup() 依赖真实 socket），手工填
    server/path/command/rfile/wfile/headers，并 stub 响应用来收集
    HTTP 状态码；wfile.getvalue() 即响应体（不含头）。
    """
    h = handler_cls.__new__(handler_cls)
    h.server = SimpleNamespace(state=state)
    h.path = path
    h.command = command
    h.request_version = "HTTP/1.1"
    body_bytes = b"" if body is None else body
    h.rfile = io.BytesIO(body_bytes)
    h.wfile = io.BytesIO()
    h.headers = SimpleNamespace(
        get=lambda k, d="0": str(len(body_bytes)) if k == "Content-Length" else d
    )
    h._sent_code = None
    h.send_response = lambda code, message=None: setattr(h, "_sent_code", code)
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    return h


class _HandlerCase(unittest.TestCase):
    """Handler 端点测试基类：独立临时目录 + 测试书 + 服务端 state。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ptoe_epubedit_handler_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        _patch_cache_dir(self)
        self.book = os.path.join(self.tmp, "book.epub")
        make_book(self.book)
        info = em.read_book(self.book)
        self.state = {
            "book": {
                "title": info["title"],
                "author": info["author"],
                "articles": info["articles"],
                "path": self.book,
            },
            "finished": threading.Event(),
            "lock": threading.RLock(),
            "gone_at": None,
            "saved": False,
        }


class TestHandler(_HandlerCase):

    def test_get_api_book(self):
        h = _make_handler(em._EpubEditHandler, self.state, "/api/book", "GET")
        h.do_GET()
        self.assertEqual(h._sent_code, 200)
        data = _json.loads(h.wfile.getvalue().decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["title"], "测试书")
        self.assertEqual(data["path"], self.book)
        self.assertEqual(len(data["articles"]), 3)

    def test_post_api_save(self):
        articles = [
            {"title": a["title"], "text": a["text"]} for a in self.state["book"]["articles"]
        ]
        articles[1]["text"] = "<p>已修改</p>"
        body = _json.dumps({"articles": articles}).encode("utf-8")
        h = _make_handler(em._EpubEditHandler, self.state, "/api/save", "POST", body)
        h.do_POST()
        self.assertEqual(h._sent_code, 200)
        data = _json.loads(h.wfile.getvalue().decode("utf-8"))
        self.assertTrue(data["ok"], data)
        self.assertTrue(os.path.isfile(data["path"]))
        # 锁内更新保存后的文章快照与此前一致
        self.assertEqual(self.state["book"]["articles"], articles)
        self.assertTrue(self.state["saved"])
        # 再次 GET /api/book 返回的是最新快照
        h2 = _make_handler(em._EpubEditHandler, self.state, "/api/book", "GET")
        h2.do_GET()
        data2 = _json.loads(h2.wfile.getvalue().decode("utf-8"))
        self.assertEqual(data2["articles"][1]["text"], "<p>已修改</p>")

    def test_post_api_save_invalid_articles(self):
        h = _make_handler(
            em._EpubEditHandler, self.state, "/api/save", "POST",
            _json.dumps({"articles": "不是数组"}).encode("utf-8"),
        )
        h.do_POST()
        self.assertEqual(h._sent_code, 400)
        data = _json.loads(h.wfile.getvalue().decode("utf-8"))
        self.assertFalse(data["ok"])

    def test_post_api_save_invalid_json(self):
        h = _make_handler(em._EpubEditHandler, self.state, "/api/save", "POST", b"{bad json")
        h.do_POST()
        self.assertEqual(h._sent_code, 400)
        data = _json.loads(h.wfile.getvalue().decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "无效的 JSON")

    def test_post_api_bye_sets_gone_at(self):
        h = _make_handler(
            em._EpubEditHandler, self.state, "/api/bye", "POST",
            _json.dumps({}).encode("utf-8"),
        )
        h.do_POST()
        self.assertEqual(h._sent_code, 200)
        data = _json.loads(h.wfile.getvalue().decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertTrue(data["bye"])
        self.assertIsNotNone(self.state["gone_at"])
        self.assertGreater(self.state["gone_at"], 0)

    def test_get_api_ping_clears_gone_at(self):
        self.state["gone_at"] = 12345.0
        h = _make_handler(em._EpubEditHandler, self.state, "/api/ping", "GET")
        h.do_GET()
        self.assertEqual(h._sent_code, 200)
        data = _json.loads(h.wfile.getvalue().decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertIsNone(self.state["gone_at"])

    def test_unknown_route_404(self):
        h = _make_handler(em._EpubEditHandler, self.state, "/api/nope", "GET")
        h.do_GET()
        self.assertEqual(h._sent_code, 404)
        data = _json.loads(h.wfile.getvalue().decode("utf-8"))
        self.assertFalse(data["ok"])
        # POST 未知路径同样 404
        h2 = _make_handler(
            em._EpubEditHandler, self.state, "/api/nope", "POST",
            _json.dumps({}).encode("utf-8"),
        )
        h2.do_POST()
        self.assertEqual(h2._sent_code, 404)

    def test_root_serves_ui_html(self):
        # / 应返回 ui/epubedit.html（前端线产出）；用打桩 _asset_path 确定性地
        # 验证「存在→200」与「缺失→404 中文错误」两个分支，不依赖并行线文件。
        ui_html = os.path.join(self.tmp, "epubedit.html")
        with open(ui_html, "w", encoding="utf-8") as f:
            f.write("<!DOCTYPE html><html><body>编辑界面</body></html>")
        h = _make_handler(em._EpubEditHandler, self.state, "/", "GET")
        h._asset_path = lambda name: os.path.join(self.tmp, name)
        h.do_GET()
        self.assertEqual(h._sent_code, 200)
        self.assertIn(b"<html>", h.wfile.getvalue())
        # 缺失 → 404
        h2 = _make_handler(em._EpubEditHandler, self.state, "/", "GET")
        h2._asset_path = lambda name: os.path.join(self.tmp, "不存在_" + name)
        h2.do_GET()
        self.assertEqual(h2._sent_code, 404)
        data = _json.loads(h2.wfile.getvalue().decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertIn("未找到界面文件", data["error"])


class TestApiConvert(_HandlerCase):
    """/api/convert：繁简转换端点，无状态（只返回结果，不改服务端内容，
    由浏览器更新界面、保存时才落盘）。"""

    def _post(self, obj=None, raw=None):
        body = _json.dumps(obj).encode("utf-8") if obj is not None else raw
        h = _make_handler(em._EpubEditHandler, self.state, "/api/convert", "POST", body)
        h.do_POST()
        return h._sent_code, _json.loads(h.wfile.getvalue().decode("utf-8"))

    def test_convert_t2s(self):
        code, data = self._post({"html": "<p>繁體中文</p>", "mode": "t2s"})
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["html"], "<p>繁体中文</p>")

    def test_convert_s2t(self):
        code, data = self._post({"html": "<p>简体中文</p>", "mode": "s2t"})
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["html"], "<p>簡體中文</p>")

    def test_convert_keeps_tags_and_attrs(self):
        # 只转换文本节点，标签与属性原样保留
        code, data = self._post(
            {"html": '<p class="x"><b>繁體</b>中文</p>', "mode": "t2s"}
        )
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["html"], '<p class="x"><b>繁体</b>中文</p>')

    def test_convert_invalid_mode(self):
        code, data = self._post({"html": "<p>x</p>", "mode": "x"})
        self.assertEqual(code, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "mode 必须为 t2s 或 s2t")

    def test_convert_html_not_str(self):
        code, data = self._post({"html": 123, "mode": "t2s"})
        self.assertEqual(code, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "html 必须为字符串")

    def test_convert_invalid_json(self):
        code, data = self._post(raw=b"{bad json")
        self.assertEqual(code, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "无效的 JSON")


class TestApiClean(_HandlerCase):
    """/api/clean：文本智能清理端点，无状态（只返回结果，不改服务端内容）。"""

    def _post(self, obj=None, raw=None):
        body = _json.dumps(obj).encode("utf-8") if obj is not None else raw
        h = _make_handler(em._EpubEditHandler, self.state, "/api/clean", "POST", body)
        h.do_POST()
        return h._sent_code, _json.loads(h.wfile.getvalue().decode("utf-8"))

    def test_clean_paragraphs_kept(self):
        # 与 test_correctmanage /api/clean 同一组输入/期望：默认不合并段落，
        # 块间以 \n 连接（clean_page_html 默认 merge_paragraphs=False）
        code, data = self._post({"html": "<p>第一段</p><p>续文</p>"})
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["html"], "<p>第一段</p>\n<p>续文</p>")

    def test_clean_trims_outer_whitespace(self):
        # 段首/段尾空白剔除；块内空白串保留（clean_page_html 的既有行为）
        code, data = self._post({"html": "<p> 有   多余   空格 </p>"})
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["html"], "<p>有   多余   空格</p>")

    def test_clean_plain_text_wraps_p(self):
        # 纯文本（无标签）经 sanitize 归一封为 <p>
        code, data = self._post({"html": "  #标题 正文 "})
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"], data)
        self.assertIn("<p>", data["html"])

    def test_clean_missing_html(self):
        code, data = self._post({"other": 1})
        self.assertEqual(code, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "html 必须为字符串")

    def test_clean_invalid_json(self):
        code, data = self._post(raw=b"{bad json")
        self.assertEqual(code, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "无效的 JSON")


# --------------------------------------------------------------------------
# read_book 磁盘缓存 + 多线程抽取测试
# --------------------------------------------------------------------------

def _rewrite_member(epub_path, member_name, new_xhtml):
    """改写 zip 内一个文本成员（保持成员与顺序不变）并原子替换原文件。

    用于缓存失效测试：源文件大小/mtime 变化 → 签名失效 → 重新解析。
    """
    tmp_path = epub_path + ".rewrite.tmp"
    with zipfile.ZipFile(epub_path) as zin:
        items = [(info, zin.read(info.filename)) for info in zin.infolist()]
    with zipfile.ZipFile(tmp_path, "w") as zout:
        for info, data in items:
            if info.filename == member_name:
                data = new_xhtml.encode("utf-8")
            zout.writestr(info, data)
    os.replace(tmp_path, epub_path)


class _CacheCase(_BookCase):
    """缓存行为测试基类：_BookCase 已把 _cache_dir 指到临时目录。"""

    def cache_files(self):
        d = Path(self.tmp) / "epubedit_cache"
        return list(d.glob("*.json")) if d.is_dir() else []


class TestReadBookCache(_CacheCase):
    """read_book 磁盘缓存：命中免 zip 重读、失效/损坏回退、并行顺序保持。"""

    def test_cache_file_written_after_cold_read(self):
        res = em.read_book(self.book)
        self.assertTrue(res["ok"], res)
        files = self.cache_files()
        self.assertEqual(len(files), 1)
        data = _json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["sig"], em._book_signature(self.book))
        self.assertEqual(data["title"], "测试书")
        self.assertEqual(len(data["articles"]), 3)

    def test_cache_hit_skips_zip(self):
        res1 = em.read_book(self.book)
        self.assertTrue(res1["ok"], res1)
        opened = []
        orig = em.zipfile.ZipFile

        def boom(*a, **k):
            opened.append(a)
            raise AssertionError("缓存命中不应重新打开 zip")

        em.zipfile.ZipFile = boom
        self.addCleanup(lambda: setattr(em.zipfile, "ZipFile", orig))
        res2 = em.read_book(self.book)
        self.assertEqual(opened, [])
        self.assertEqual(res2, res1)

    def test_cache_invalidated_on_change(self):
        res1 = em.read_book(self.book)
        self.assertTrue(res1["ok"], res1)
        self.assertIn("正文2", res1["articles"][2]["text"])
        # 改写第 2 章成员并写回原路径（大小/mtime 变化 → 缓存失效）
        _rewrite_member(
            self.book,
            "OEBPS/Text/chap2.xhtml",
            _xhtml("第2章", "<h1>第2章</h1><p>正文2已修改</p>"),
        )
        opened = []
        orig = em.zipfile.ZipFile

        def counting(*a, **k):
            opened.append(a)
            return orig(*a, **k)

        em.zipfile.ZipFile = counting
        self.addCleanup(lambda: setattr(em.zipfile, "ZipFile", orig))
        res2 = em.read_book(self.book)
        self.assertTrue(res2["ok"], res2)
        text2 = res2["articles"][2]["text"]
        self.assertIn("正文2已修改", text2)
        self.assertNotIn("<p>正文2</p>", text2)
        # 冷路径重新打开一次（is_zipfile 在 3.11+ 走 _check_zipfile，不构造 ZipFile）
        self.assertEqual(len(opened), 1)

    def test_cache_signature_mismatch_falls_back(self):
        res1 = em.read_book(self.book)
        self.assertTrue(res1["ok"], res1)
        files = self.cache_files()
        self.assertEqual(len(files), 1)
        data = _json.loads(files[0].read_text(encoding="utf-8"))
        data["sig"] = "deadbeef" * 5
        files[0].write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
        res2 = em.read_book(self.book)
        self.assertTrue(res2["ok"], res2)
        self.assertEqual(res2["articles"], res1["articles"])

    def test_cache_corrupt_falls_back(self):
        res1 = em.read_book(self.book)
        self.assertTrue(res1["ok"], res1)
        files = self.cache_files()
        self.assertEqual(len(files), 1)
        files[0].write_text("{坏缓存 json", encoding="utf-8")
        res2 = em.read_book(self.book)
        self.assertTrue(res2["ok"], res2)
        self.assertEqual(res2["articles"], res1["articles"])

    def test_cache_version_mismatch_falls_back(self):
        # 缓存格式升级（version 变更）→ 旧缓存作废，重新解析
        res1 = em.read_book(self.book)
        self.assertTrue(res1["ok"], res1)
        files = self.cache_files()
        self.assertEqual(len(files), 1)
        data = _json.loads(files[0].read_text(encoding="utf-8"))
        data["version"] = 99
        files[0].write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
        res2 = em.read_book(self.book)
        self.assertTrue(res2["ok"], res2)
        self.assertEqual(res2["articles"], res1["articles"])
        # 重新解析后写回的缓存又回到当前版本
        new_files = self.cache_files()
        self.assertEqual(len(new_files), 1)
        data2 = _json.loads(new_files[0].read_text(encoding="utf-8"))
        self.assertEqual(data2["version"], 1)

    def test_parallel_order_preserved(self):
        # ≥3 文档走 ThreadPoolExecutor 分支；7 文档验证 spine 顺序保持不变
        big = os.path.join(self.tmp, "big.epub")
        make_book(big, n_docs=7)
        res = em.read_book(big)
        self.assertTrue(res["ok"], res)
        # 序言（无 h1）回退 + 6 个 <h1> 章节，顺序即 spine 顺序
        self.assertEqual(len(res["articles"]), 7)
        self.assertEqual(res["articles"][0]["title"], "测试书")
        self.assertIn("<p>这是前言。</p>", res["articles"][0]["text"])
        for i in range(1, 7):
            self.assertEqual(res["articles"][i]["title"], f"第{i}章")
            self.assertIn(f"正文{i}", res["articles"][i]["text"])
            self.assertNotIn("<h1", res["articles"][i]["text"])
        # 图片内联仍在（文档 1）
        self.assertIn("data:image/", res["articles"][1]["text"])


if __name__ == "__main__":
    unittest.main()