import re
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

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


class TestPdfToEpub(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="test_mian_"))
        self._pdf = self._tmp / "sample.pdf"
        _make_pdf(self._pdf, n=3)

    def tearDown(self):
        # split_pdf_to_images writes to data/<pdf stem>/ next to pdfmanage.py
        data_dir = Path(pdfmanage.__file__).resolve().parent / "data"
        for d in data_dir.glob("sample*"):
            shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_pdf_to_epub_full_pipeline(self):
        def fake_batch_infer(images, prompts, model_key="HY", max_workers=3, thinking=False, timeout=600, on_progress=None):
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
        mian._ensure_server = lambda model_key: None
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


if __name__ == "__main__":
    unittest.main()
