import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import llamamanage
import mian
import pdfmanage
from epubmanage import _natural_key


def _make_pdf(path: Path, n: int = 3) -> None:
    import fitz

    doc = fitz.open()
    for i in range(1, n + 1):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i} content line")
    doc.save(path)
    doc.close()


def _cleanup_histories(pdf_path: Path) -> None:
    """删除测试产生的历史记录条目（无矫正 epub 流程现在会自动写历史）。"""
    from correctmanage import _history_prefix

    prefix = _history_prefix(str(pdf_path))
    hist_dir = (
        Path(pdfmanage.__file__).resolve().parent / "data" / "correction_history"
    )
    if prefix:
        for fp in hist_dir.glob(f"{prefix}_*.json"):
            fp.unlink(missing_ok=True)


class TestPdfToEpub(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="test_mian_"))
        self._pdf = self._tmp / "sample.pdf"
        _make_pdf(self._pdf, n=3)

    def tearDown(self):
        # split_pdf_to_images writes to data/<pdf stem>/ next to pdfmanage.py
        data_dir = Path(pdfmanage.__file__).resolve().parent / "data"
        _cleanup_histories(self._pdf)
        for d in data_dir.glob("sample*"):
            shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_pdf_to_epub_full_pipeline(self):
        def fake_batch_infer(images, prompts, model_key="HY", max_workers=3, thinking=False, timeout=600, on_progress=None, on_result=None):
            # return results shuffled to prove the pipeline sorts by page number
            out = []
            for i, img in enumerate(images):
                item = {"img": img, "result": f"这是第 {i + 1} 页的正文内容：OCR text for page {i + 1}。本句足够长，超过三十个字符，不会被标题启发式误判为标题行。", "error": None}
                out.append(item) if i % 2 == 0 else out.insert(0, item)
            if on_progress is not None:
                on_progress(len(out), len(out))
            return out

        original = llamamanage.batch_infer
        original_ensure = mian._ensure_server
        llamamanage.batch_infer = fake_batch_infer
        mian._ensure_server = lambda model_key, workers=None: None
        try:
            epub_out = self._tmp / "out.epub"
            result = mian.pdf_to_epub(
                self._pdf,
                out_dir=self._tmp / "out",
                epub_path=epub_out,
                title="Sample Doc",
                author="tester",
            )
        finally:
            llamamanage.batch_infer = original
            mian._ensure_server = original_ensure

        self.assertTrue(epub_out.is_file(), f"epub not created: {result}")
        with zipfile.ZipFile(epub_out) as zf:
            names = zf.namelist()
            self.assertEqual(names[0], "mimetype")
            self.assertEqual(zf.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)
            self.assertIn("META-INF/container.xml", names)
            self.assertIn("OEBPS/content.opf", names)
            content = sorted(n for n in names if n.startswith("OEBPS/Text/content_"))
            self.assertEqual(len(content), 1)
            # merged mode: all pages merged into a single content file, order preserved
            merged_html = zf.read("OEBPS/Text/content_1.xhtml").decode("utf-8")
            i1 = merged_html.find("OCR text for page 1")
            i2 = merged_html.find("OCR text for page 2")
            i3 = merged_html.find("OCR text for page 3")
            self.assertTrue(-1 < i1 < i2 < i3, "merged content must keep page order")
            # regression: spine itemrefs must reference manifest ids and every
            # manifest href must exist inside the zip (they used to be raw
            # un-mapped hrefs / flat paths that ResourceMapper moved away)
            opf = zf.read("OEBPS/content.opf").decode("utf-8")
            manifest_ids = set(re.findall(r'<item\b[^>]*\bid="([^"]+)"', opf))
            manifest_hrefs = set(re.findall(r'<item\b[^>]*\bhref="([^"]+)"', opf))
            spine_refs = re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"', opf)
            self.assertTrue(spine_refs, "spine must contain itemrefs")
            for ref in spine_refs:
                self.assertIn(ref, manifest_ids, f"spine idref {ref} not in manifest ids")
            for href in manifest_hrefs:
                self.assertIn(f"OEBPS/{href}", names, f"manifest href {href} missing from zip")
            # regression (2026-08): EPUB 3 导航文档必须带 epub:type="toc" 与
            # xmlns:epub 命名空间，否则严格阅读器不识别目录、链接无法跳转
            nav_html = zf.read("OEBPS/Text/nav.xhtml").decode("utf-8")
            self.assertIn('epub:type="toc"', nav_html)
            self.assertIn('xmlns:epub="http://www.idpf.org/2007/ops"', nav_html)
            # 目录锚点必须与正文标题 id 对应（跳转目标存在）：nav 中带 # 片段的
            # 链接，其目标 id 必须在对应 content 文件里出现（mock OCR 无标题时
            # 走 fallback → href 无片段，跳过校验）
            nav_hrefs = re.findall(r'<a href="([^"]+)"', nav_html)
            self.assertTrue(nav_hrefs, "nav.xhtml must contain TOC links")
            for h in nav_hrefs:
                if "#" not in h:
                    continue
                fname, frag = h.split("#", 1)
                self.assertIn(f"OEBPS/Text/{fname}", names, f"TOC target {fname} missing")
                body = zf.read(f"OEBPS/Text/{fname}").decode("utf-8")
                self.assertIn(f'id="{frag}"', body, f"TOC anchor #{frag} missing in {fname}")
            # EPUB 兼容性（2026-08）：dcterms:modified / dc 命名空间前缀 / NCX 兜底目录
            self.assertIn('dcterms:modified', opf, "content.opf 必须含 dcterms:modified（EPUB 3.3 §5.4.1）")
            self.assertIn('dc:title', opf, "content.opf 必须使用 dc:title 命名空间前缀（非 ns0:title）")
            self.assertIn('OEBPS/toc.ncx', names, "toc.ncx 必须存在（EPUB2/EPUB3 均生成，给旧阅读器兜底）")
            self.assertIn('id="ncx"', opf, "manifest 必须含 id=\"ncx\" 的 toc.ncx 项")
            self.assertIn('toc="ncx"', opf, "spine 必须引用 toc.ncx（toc=\"ncx\"）")
            # nav.xhtml 必须含 XHTML 默认命名空间 + ARIA doc-toc 角色 + landmarks nav
            self.assertIn('xmlns="http://www.w3.org/1999/xhtml"', nav_html, "nav.xhtml 必须声明 XHTML 默认命名空间")
            self.assertIn('role="doc-toc"', nav_html, "nav 必须含 role=\"doc-toc\"（ARIA 目录角色）")
            self.assertIn('epub:type="landmarks"', nav_html, "nav.xhtml 必须含 landmarks nav（EPUB 3.3 §11.3）")
            # content_1.xhtml 必须含 XHTML 默认命名空间 + 中文 lang
            self.assertIn('xmlns="http://www.w3.org/1999/xhtml"', merged_html, "content 页必须声明 XHTML 默认命名空间")
            self.assertIn("lang='zh-CN'", merged_html, "content 页 lang 必须为 zh-CN（非 en）")

    def test_content_filename_natural_sort(self):
        # 字典序会把 content_10 排在 content_2 前；自然排序必须相反
        self.assertLess(_natural_key("content_2.xhtml"), _natural_key("content_10.xhtml"))

    def test_no_args_piped_still_nothing_to_do(self):
        # 无参数 + 非交互 stdin（管道/重定向）：保持原 "nothing to do" 行为
        import contextlib
        import io

        old_stdin = sys.stdin
        sys.stdin = io.StringIO("")
        try:
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                rc = mian.main([])
            self.assertEqual(rc, 0)
            self.assertIn("nothing to do", buf.getvalue())
        finally:
            sys.stdin = old_stdin

    def test_no_args_interactive_menu(self):
        # 无参数 + 交互终端（含打包 exe 双击启动）：进入终端菜单，输入 0 退出
        import contextlib
        import io

        class _TTY(io.StringIO):
            def isatty(self):
                return True

        old_stdin = sys.stdin
        sys.stdin = _TTY("0\n")
        try:
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                rc = mian.main([])
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("请选择操作", out, "无参数交互模式应显示终端菜单")
            self.assertIn("PDF → EPUB 转换", out)
            self.assertIn("手动矫正", out)
            self.assertNotIn("nothing to do", out)
        finally:
            sys.stdin = old_stdin


    def test_no_args_interactive_menu_eof_exits(self):
        # 菜单读 stdin 遇 EOF（管道关闭/控制台关闭）必须安全退出，不能死循环
        import contextlib
        import io

        class _TTY(io.StringIO):
            def isatty(self):
                return True

        old_stdin = sys.stdin
        sys.stdin = _TTY("")
        try:
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                rc = mian.main([])
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("请选择操作", out, "EOF 前应显示菜单")
            self.assertIn("已退出", out)
        finally:
            sys.stdin = old_stdin


class TestAutoHistory(unittest.TestCase):
    """无矫正 epub 流程：自动保存历史记录，供之后 correct 打开矫正。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="test_mian_hist_"))
        self._pdf = self._tmp / "sample.pdf"
        _make_pdf(self._pdf, n=3)
        self._data_dir = Path(pdfmanage.__file__).resolve().parent / "data"
        from correctmanage import _history_prefix

        self._prefix = _history_prefix(str(self._pdf))
        self._hist_dir = self._data_dir / "correction_history"

    def tearDown(self):
        _cleanup_histories(self._pdf)
        for d in self._data_dir.glob("sample*"):
            shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_pdf_to_epub_without_correct_saves_history(self):
        mian.split_pdf_to_images(self._pdf, dpi=100, fmt="png")
        original = llamamanage.batch_infer
        original_ensure = mian._ensure_server

        def fake(images, prompts, model_key="HY", max_workers=3, thinking=False,
                 timeout=600, on_progress=None, on_result=None):
            return [{"img": img, "result": "历史测试正文：本句足够长，超过三十个字符，不会被标题启发式误判为标题行。", "error": None} for img in images]

        llamamanage.batch_infer = fake
        mian._ensure_server = lambda model_key, workers=None: None
        try:
            result = mian.pdf_to_epub(
                self._pdf,
                out_dir=self._tmp / "out",
                epub_path=self._tmp / "out.epub",
                title="Hist Doc",
            )
        finally:
            llamamanage.batch_infer = original
            mian._ensure_server = original_ensure

        self.assertTrue((self._tmp / "out.epub").is_file())
        entries = sorted(self._hist_dir.glob(f"{self._prefix}_*.json"))
        self.assertEqual(len(entries), 1, f"应生成 1 条历史：{entries}")
        data = json.loads(entries[0].read_text(encoding="utf-8"))
        self.assertEqual(data["pdf"], str(self._pdf.resolve()))
        self.assertEqual(len(data["pages"]), 3)
        # 历史载荷与矫正界面格式一致：每页为 initial_html 的块级 HTML
        first = data["pages"].get("1")
        self.assertTrue(first and first.startswith("<div>"), f"pages[1] 应为块级 HTML: {first!r}")


class TestOcrResume(unittest.TestCase):
    """OCR 断点续传：进度持久化、继续识别、重新识别、resume 命令。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="test_mian_resume_"))
        self._pdf = self._tmp / "sample.pdf"
        _make_pdf(self._pdf, n=3)
        self._data_dir = Path(pdfmanage.__file__).resolve().parent / "data"

    def tearDown(self):
        _cleanup_histories(self._pdf)
        for d in self._data_dir.glob("sample*"):
            shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _split(self):
        return mian.split_pdf_to_images(self._pdf, dpi=100, fmt="png")

    def _write_progress(self, img_dir, pages, status="running"):
        import json

        fp = mian._ocr_progress_path(img_dir)
        fp.write_text(
            json.dumps(
                {
                    "pdf": str(self._pdf.resolve()),
                    "dpi": 100,
                    "model_key": "HY",
                    "total": 3,
                    "status": status,
                    "pages": pages,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _run_pdf_to_epub(self, resume=None, fake=None):
        original = llamamanage.batch_infer
        original_ensure = mian._ensure_server
        llamamanage.batch_infer = fake or self._fake
        mian._ensure_server = lambda model_key, workers=None: None
        try:
            return mian.pdf_to_epub(
                self._pdf,
                out_dir=self._tmp / "out",
                epub_path=self._tmp / "out.epub",
                title="Sample",
                resume=resume,
            )
        finally:
            llamamanage.batch_infer = original
            mian._ensure_server = original_ensure

    def _fake(self, images, prompts, model_key="HY", max_workers=3, thinking=False,
              timeout=600, on_progress=None, on_result=None):
        out = []
        for i, img in enumerate(images):
            r = {"img": img, "result": f"resume 页文本 {i + 1}：本句足够长，超过三十个字符，不会被标题启发式误判为标题行。", "error": None}
            out.append(r)
            if on_result is not None:
                on_result(r)
        if on_progress is not None:
            on_progress(len(out), len(out))
        return out

    def test_resume_skips_done_pages(self):
        # 上次中断：第 1、2 页已识别，第 3 页未处理 → 继续识别只处理第 3 页
        img_dir, img_paths = self._split()
        self._write_progress(
            img_dir,
            {
                "1": {"status": "done", "result": "第一页已识别内容：本句足够长，超过三十个字符，不会被标题启发式误判为标题行。"},
                "2": {"status": "done", "result": "第二页已识别内容：本句足够长，超过三十个字符，不会被标题启发式误判为标题行。"},
            },
        )
        seen = []

        def fake(images, prompts, model_key="HY", max_workers=3, thinking=False,
                 timeout=600, on_progress=None, on_result=None):
            seen.extend(images)
            out = []
            for img in images:
                r = {"img": img, "result": "第三页补充识别内容：本句足够长，超过三十个字符，不会被标题启发式误判为标题行。", "error": None}
                out.append(r)
                if on_result is not None:
                    on_result(r)
            return out

        self._run_pdf_to_epub(resume="resume", fake=fake)
        epub = self._tmp / "out.epub"
        self.assertTrue(epub.is_file())
        self.assertEqual(len(seen), 1, "继续识别应只处理未完成页")
        self.assertEqual(mian._page_of(seen[0]), 3)
        # 转换成功 → 进度文件删除
        self.assertFalse(mian._ocr_progress_path(img_dir).exists())
        # EPUB 合并了缓存页 + 新识别页
        with zipfile.ZipFile(epub) as zf:
            html = zf.read("OEBPS/Text/content_1.xhtml").decode("utf-8")
            self.assertIn("第一页已识别内容", html)
            self.assertIn("第二页已识别内容", html)
            self.assertIn("第三页补充识别内容", html)

    def test_restart_reidentifies_all(self):
        img_dir, img_paths = self._split()
        self._write_progress(img_dir, {"1": {"status": "done", "result": "旧文本"}})
        seen = []

        def fake(images, prompts, model_key="HY", max_workers=3, thinking=False,
                 timeout=600, on_progress=None, on_result=None):
            seen.extend(images)
            return [{"img": img, "result": "重新识别内容：本句足够长，超过三十个字符，不会被标题启发式误判为标题行。", "error": None} for img in images]

        self._run_pdf_to_epub(resume="restart", fake=fake)
        self.assertEqual(len(seen), 3, "重新识别应处理全部页面")
        self.assertFalse(mian._ocr_progress_path(img_dir).exists())

    def test_new_run_writes_then_cleans_progress(self):
        img_dir, img_paths = self._split()
        self._run_pdf_to_epub(resume=None, fake=self._fake)
        self.assertTrue((self._tmp / "out.epub").is_file())
        # 全新转换成功 → 进度文件被清理
        self.assertFalse(mian._ocr_progress_path(img_dir).exists())

    def test_resume_no_progress_asks_and_cancels(self):
        img_dir, img_paths = self._split()
        original_ask = mian._ask
        mian._ask = lambda prompt: ""  # EOF/回车 → 视为取消
        try:
            result = self._run_pdf_to_epub(resume="resume")
        finally:
            mian._ask = original_ask
        self.assertEqual(result.get("ok"), False)
        self.assertFalse((self._tmp / "out.epub").is_file())

    def test_progress_save_load_roundtrip(self):
        img_dir, _ = self._split()
        prog = {
            "pdf": str(self._pdf.resolve()),
            "total": 3,
            "status": "running",
            "pages": {"1": {"status": "done", "result": "x"}},
        }
        mian._save_ocr_progress(img_dir, prog)
        loaded = mian._load_ocr_progress(img_dir)
        self.assertEqual(loaded["total"], 3)
        self.assertEqual(loaded["status"], "running")
        self.assertIn("updated", loaded)
        mian._clear_ocr_progress(img_dir)
        self.assertIsNone(mian._load_ocr_progress(img_dir))


class TestStopCommand(unittest.TestCase):
    """mian.py stop 子命令：停止推理服务（2026-08-13）。"""

    def test_stop_calls_stopserver(self):
        import contextlib
        import io

        import llamamanage

        calls = []
        orig = llamamanage.stopserver
        llamamanage.stopserver = lambda: calls.append(1) or True
        try:
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                rc = mian.main(["stop"])
        finally:
            llamamanage.stopserver = orig
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [1])
        self.assertIn("已停止", buf.getvalue())

    def test_stop_engine_vllm_sets_engine(self):
        import contextlib
        import io

        import llamamanage

        orig_stop = llamamanage.stopserver
        orig_set = llamamanage.set_engine
        seen = []
        llamamanage.stopserver = lambda: True

        def fake_set_engine(e):
            # 记录调用并透传真实 set_engine，让 _active_engine() 能读到覆盖值
            seen.append(e)
            orig_set(e)

        llamamanage.set_engine = fake_set_engine
        try:
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                rc = mian.main(["stop", "--engine", "vllm"])
        finally:
            llamamanage.stopserver = orig_stop
            llamamanage.set_engine = orig_set
            orig_set(None)  # 清引擎覆盖（连同 _ENGINE_CACHE），避免污染后续测试
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["vllm"])
        self.assertIn("vLLM-Omni 已停止", buf.getvalue())


class TestGuiPrompt(unittest.TestCase):
    """GUI 转换子进程的弹窗询问协议（PTOE_UI_PROMPT=1，2026-08-17）。

    子进程无控制台时 OCR 断点续传选择不能走终端 stdin：置环境变量后
    _ask_ocr_resume 打印 __PTOE_PROMPT__ 标记行并从 stdin 读一行
    （guimanage 弹窗后写回）；EOF/非法选择回退默认值，绝不卡死。
    """

    def tearDown(self):
        os.environ.pop("PTOE_UI_PROMPT", None)

    def _progress(self, status="running", done=2, total=3):
        pages = {str(i + 1): {"status": "done", "result": "x"} for i in range(done)}
        return {"total": total, "status": status, "pages": pages}

    def test_gui_mode_prompts_via_marker_and_stdin(self):
        import contextlib
        import io

        os.environ["PTOE_UI_PROMPT"] = "1"
        with mock.patch("sys.stdin", io.StringIO("restart\n")), \
             contextlib.redirect_stdout(io.StringIO()) as buf:
            mode = mian._ask_ocr_resume(self._progress())
        self.assertEqual(mode, "restart")
        out = buf.getvalue()
        self.assertIn(mian._PROMPT_MARKER, out, "应打印弹窗标记行")
        self.assertIn("继续识别", out, "标记行应携带弹窗选项载荷")

    def test_gui_mode_eof_falls_back_to_default(self):
        import contextlib
        import io

        os.environ["PTOE_UI_PROMPT"] = "1"
        with mock.patch("sys.stdin", io.StringIO("")), \
             contextlib.redirect_stdout(io.StringIO()):
            mode = mian._ask_ocr_resume(self._progress())
        self.assertEqual(mode, "resume", "EOF 应回退默认（继续识别）")

    def test_gui_mode_invalid_choice_falls_back_to_default(self):
        import contextlib
        import io

        os.environ["PTOE_UI_PROMPT"] = "1"
        with mock.patch("sys.stdin", io.StringIO("bogus\n")), \
             contextlib.redirect_stdout(io.StringIO()):
            mode = mian._ask_ocr_resume(self._progress())
        self.assertEqual(mode, "resume")

    def test_gui_mode_ocr_done_convert_default(self):
        import contextlib
        import io

        os.environ["PTOE_UI_PROMPT"] = "1"
        prog = self._progress(status="ocr_done", done=3, total=3)
        with mock.patch("sys.stdin", io.StringIO("")), \
             contextlib.redirect_stdout(io.StringIO()):
            mode = mian._ask_ocr_resume(prog)
        self.assertEqual(mode, "convert", "OCR 已完成时默认直接转换")

    def test_terminal_mode_unchanged(self):
        # 未置环境变量：保持原终端 stdin 交互（选项序号 1..n）
        import contextlib
        import io

        os.environ.pop("PTOE_UI_PROMPT", None)
        with mock.patch("mian._ask", return_value="2"), \
             contextlib.redirect_stdout(io.StringIO()):
            mode = mian._ask_ocr_resume(self._progress())
        self.assertEqual(mode, "restart")


if __name__ == "__main__":
    unittest.main()
