import re
import shutil
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
                item = {"img": img, "result": f"OCR text for page {i + 1}", "error": None}
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


if __name__ == "__main__":
    unittest.main()
