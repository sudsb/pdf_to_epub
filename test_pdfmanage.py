import os
import shutil
import tempfile
import unittest
from pathlib import Path

import pdfmanage

_REAL_PDF = Path(r"E:\MYBooks\books\毛泽东思想\主席与毛远新同志谈话纪要.pdf")
_TEST_PDF = (
    Path(os.environ.get("TEST_PDF_PATH", str(_REAL_PDF)))
    if "TEST_PDF_PATH" in os.environ
    else (_REAL_PDF if _REAL_PDF.exists() else None)
)


class TestCreatedic(unittest.TestCase):
    def test_creates_subdirectory_under_data(self):
        name = "test_createdic"
        result = pdfmanage.createdic(name)
        try:
            self.assertTrue(result.exists() and result.is_dir())
            self.assertEqual(result.parent.name, "data")
            self.assertEqual(result.name, name)
        finally:
            shutil.rmtree(result, ignore_errors=True)

    def test_incremental_suffix_when_dir_exists(self):
        name = "test_incr"
        d1 = pdfmanage.createdic(name)
        d2 = pdfmanage.createdic(name)
        try:
            self.assertEqual(d1.name, name)
            self.assertEqual(d2.name, f"{name}_1")
        finally:
            shutil.rmtree(d1, ignore_errors=True)
            shutil.rmtree(d2, ignore_errors=True)


class TestIsPdfFile(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="test_ispdf_"))

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_detects_pdf_by_header(self):
        f = self._tmp / "test.pdf"
        f.write_bytes(b"%PDF-1.4\nsome content\n")
        self.assertTrue(pdfmanage.is_pdf_file(f))

    def test_rejects_non_pdf(self):
        f = self._tmp / "test.txt"
        f.write_text("hello world")
        self.assertFalse(pdfmanage.is_pdf_file(f))

    def test_returns_false_for_missing_file(self):
        self.assertFalse(pdfmanage.is_pdf_file(self._tmp / "nonexistent.pdf"))


class TestCpath(unittest.TestCase):
    def test_returns_true_for_existing_directory(self):
        tmp = Path(tempfile.mkdtemp(prefix="test_cpath_"))
        try:
            self.assertTrue(pdfmanage.cpath(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_returns_false_for_missing_directory(self):
        self.assertFalse(
            pdfmanage.cpath(Path(tempfile.mkdtemp(prefix="test_")) / "nope")
        )


class TestSplitPdfToImages(unittest.TestCase):
    def setUp(self):
        try:
            import fitz  # noqa: F401
        except Exception:
            self.skipTest("PyMuPDF (fitz) is not installed")

        # use real PDF if available, otherwise generate a 3-page test PDF
        if _TEST_PDF is not None and _TEST_PDF.exists():
            self.pdf_path = _TEST_PDF
            self.pdf_name = _TEST_PDF.stem
            self._work_dir = None
        else:
            self._work_dir = Path(tempfile.mkdtemp(prefix="test_splitpdf_"))
            self.pdf_name = "test_split"
            self.pdf_path = self._work_dir / f"{self.pdf_name}.pdf"

            import fitz

            doc = fitz.open()
            for _ in range(3):
                page = doc.new_page()
                page.insert_text((72, 72), "Test page")
            doc.save(str(self.pdf_path))
            doc.close()

        # scrub output from previous run
        leftover = Path("data") / self.pdf_name
        if leftover.exists():
            shutil.rmtree(leftover)

    def tearDown(self):
        if self._work_dir is not None:
            shutil.rmtree(self._work_dir, ignore_errors=True)

    def test_png_conversion(self):
        out_dir, images = pdfmanage.split_pdf_to_images(
            self.pdf_path, dpi=300, fmt="png"
        )

        self.assertTrue((out_dir / "1.png").exists())
        self.assertEqual(out_dir.parent.name, "data")
        self.assertEqual(out_dir.name, self.pdf_name)
        self.assertGreaterEqual(len(images), 1)
        names = [p.name for p in images]
        expected = [f"{i}.png" for i in range(1, len(images) + 1)]
        self.assertEqual(names, expected)
        for img in images:
            self.assertGreater(img.stat().st_size, 0)

    def test_jpg_conversion(self):
        out_dir, images = pdfmanage.split_pdf_to_images(
            self.pdf_path, dpi=300, fmt="jpg"
        )

        self.assertTrue((out_dir / "1.jpg").exists())
        self.assertEqual(out_dir.parent.name, "data")
        self.assertEqual(out_dir.name, self.pdf_name)
        self.assertGreaterEqual(len(images), 1)
        for img in images:
            self.assertEqual(img.suffix, ".jpg")
            self.assertGreater(img.stat().st_size, 0)


class TestSplitReuse(unittest.TestCase):
    """相同 PDF + 相同参数复用已有分割图片；内容/参数变化时重新分割。"""

    def setUp(self):
        try:
            import fitz  # noqa: F401
        except Exception:
            self.skipTest("PyMuPDF (fitz) is not installed")
        self._work_dir = Path(tempfile.mkdtemp(prefix="test_reuse_"))
        self.pdf_name = "reuse_test"
        self.pdf_path = self._work_dir / f"{self.pdf_name}.pdf"

        import fitz

        doc = fitz.open()
        for _ in range(3):
            page = doc.new_page()
            page.insert_text((72, 72), "Reuse page")
        doc.save(str(self.pdf_path))
        doc.close()
        for d in Path("data").glob(f"{self.pdf_name}*"):
            shutil.rmtree(d, ignore_errors=True)

    def tearDown(self):
        for d in Path("data").glob(f"{self.pdf_name}*"):
            shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(self._work_dir, ignore_errors=True)

    def test_same_params_reuses_existing_split(self):
        d1, imgs1 = pdfmanage.split_pdf_to_images(self.pdf_path, dpi=200, fmt="png")
        self.assertTrue((d1 / pdfmanage._SPLIT_MARKER).is_file(), "分割后应写入标记")
        # 相同参数再次分割 → 同一目录、同一批图片，不生成 _1 新目录
        d2, imgs2 = pdfmanage.split_pdf_to_images(self.pdf_path, dpi=200, fmt="png")
        self.assertEqual(d1, d2)
        self.assertEqual([p.name for p in imgs1], [p.name for p in imgs2])
        self.assertFalse((d1.parent / f"{self.pdf_name}_1").exists())

    def test_param_change_triggers_resplit(self):
        d1, _ = pdfmanage.split_pdf_to_images(self.pdf_path, dpi=200, fmt="png")
        d2, _ = pdfmanage.split_pdf_to_images(self.pdf_path, dpi=300, fmt="png")
        self.assertNotEqual(d1, d2)
        self.assertEqual(d2.name, f"{self.pdf_name}_1")
        # 参数改回后仍复用原目录
        d3, _ = pdfmanage.split_pdf_to_images(self.pdf_path, dpi=200, fmt="png")
        self.assertEqual(d1, d3)

    def test_pdf_change_triggers_resplit(self):
        import json as _json

        d1, _ = pdfmanage.split_pdf_to_images(self.pdf_path, dpi=200, fmt="png")
        # 伪造旧标记为不同哈希（等价于 PDF 内容变化）→ 必须重新分割
        marker = d1 / pdfmanage._SPLIT_MARKER
        meta = _json.loads(marker.read_text(encoding="utf-8"))
        meta["pdf_hash"] = "0" * 64
        marker.write_text(_json.dumps(meta), encoding="utf-8")
        d2, _ = pdfmanage.split_pdf_to_images(self.pdf_path, dpi=200, fmt="png")
        self.assertNotEqual(d1, d2)

    def test_missing_page_triggers_resplit(self):
        d1, _ = pdfmanage.split_pdf_to_images(self.pdf_path, dpi=200, fmt="png")
        (d1 / "1.png").unlink()  # 残缺目录（缺页图）不得复用
        d2, imgs = pdfmanage.split_pdf_to_images(self.pdf_path, dpi=200, fmt="png")
        self.assertNotEqual(d1, d2)
        self.assertEqual(len(imgs), 3)


if __name__ == "__main__":
    unittest.main()
