"""批量识别 / 排除页 / 跨页错误快捷键的回归测试。

GUI 部分主要是 guimanage.py 内嵌 JS 与校正页 ui/app.js，用源码断言 +
node --check 保证关键入口没有被后续重构改掉；CLI 批量转换用 mock 掉
pdf_to_epub 的方式验证「按顺序逐本转换、单本失败不影响后续」。
"""

import os
import shutil
import subprocess
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.abspath(__file__))


def _read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


class BatchConvertTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gui = _read("guimanage.py")

    def test_multi_pick_and_queue(self):
        self.assertIn('multiple:true', self.gui)
        self.assertIn("function pickPdfs()", self.gui)
        self.assertIn("function queueAddPaths(", self.gui)
        self.assertIn("convertQueue", self.gui)
        self.assertIn('id="cvtPdfList"', self.gui)
        self.assertIn('id="cvtQueueClearBtn"', self.gui)

    def test_batch_runs_sequentially(self):
        self.assertIn("async function runConvertBatch(", self.gui)
        # 队列逐个启动，单本失败不中断后续（失败分支是 continue 而非 break）
        self.assertIn('await apiPost("/api/convert/start"', self.gui)
        self.assertIn("function waitConvertCompletion()", self.gui)
        self.assertIn("function waitConvertPrompt(", self.gui)

    def test_batch_uses_per_book_output_dir(self):
        # 指定输出目录时，批量必须按书名建子目录，避免 OEBPS 与 EPUB 互相覆盖
        self.assertIn("function joinOutDir(", self.gui)
        self.assertIn("params.out_dir=joinOutDir(outBase,stem+", self.gui)
        self.assertIn("if(batch){params.title=\"\";if(outBase)", self.gui)

    def test_blank_exclude_sent_as_null(self):
        self.assertIn("exclude:_ex||null", self.gui)
        # config.json 的 exclude_pages 允许字符串或数组，渲染时都要能显示
        self.assertIn("Array.isArray(_exCfg)", self.gui)


class CliBatchConvertTest(unittest.TestCase):
    """CLI：mian.py epub 支持一次给多个 PDF，按顺序逐本转换，单本失败不影响后续。"""

    def _run(self, argv, fake):
        import mian

        with mock.patch.object(mian, "pdf_to_epub", fake):
            return mian.main(argv)

    def test_multiple_pdfs_converted_in_order(self):
        calls = []

        def fake(pdf, **kw):
            calls.append((pdf, kw))
            return {"epub": pdf + ".epub"}

        rc = self._run(["epub", "a.pdf", "b.pdf"], fake)
        self.assertEqual(rc, 0)
        self.assertEqual([c[0] for c in calls], ["a.pdf", "b.pdf"])
        # 多本时 --title / --epub-path 无意义，必须忽略（否则 EPUB 互相覆盖）
        for _pdf, kw in calls:
            self.assertIsNone(kw["title"])
            self.assertIsNone(kw["epub_path"])

    def test_single_pdf_keeps_title_and_out_dir(self):
        calls = []

        def fake(pdf, **kw):
            calls.append((pdf, kw))
            return {"epub": pdf + ".epub"}

        rc = self._run(["epub", "a.pdf", "--title", "书名", "--out-dir", "out"], fake)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["title"], "书名")
        self.assertEqual(calls[0][1]["out_dir"], "out")

    def test_batch_uses_per_book_subdir(self):
        calls = []

        def fake(pdf, **kw):
            calls.append((pdf, kw))
            return {"epub": pdf + ".epub"}

        rc = self._run(["epub", "a.pdf", "b.pdf", "--out-dir", "out"], fake)
        self.assertEqual(rc, 0)
        self.assertTrue(calls[0][1]["out_dir"].endswith(os.path.join("out", "a")))
        self.assertTrue(calls[1][1]["out_dir"].endswith(os.path.join("out", "b")))

    def test_failure_does_not_stop_following_books(self):
        calls = []

        def fake(pdf, **kw):
            calls.append(pdf)
            if pdf == "a.pdf":
                raise RuntimeError("boom")
            return {"epub": pdf + ".epub"}

        rc = self._run(["epub", "a.pdf", "b.pdf"], fake)
        self.assertEqual(rc, 1)
        self.assertEqual(calls, ["a.pdf", "b.pdf"])


class CrossPageErrorShortcutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read(os.path.join("ui", "app.js"))

    def test_shortcut_registered(self):
        self.assertIn("['proofread_next_page_error'", self.js)
        self.assertIn("['proofread_prev_page_error'", self.js)
        self.assertIn("proofread_next_page_error: 'Ctrl+PageDown'", self.js)
        self.assertIn("proofread_prev_page_error: 'Ctrl+PageUp'", self.js)
        self.assertIn("proofread_next_page_error: () => jumpToErrorPage(1)", self.js)
        self.assertIn("proofread_prev_page_error: () => jumpToErrorPage(-1)", self.js)

    def test_jump_scans_across_pages(self):
        self.assertIn("function jumpToErrorPage(dir)", self.js)
        self.assertIn("function _firstLiveErrorIndex(i)", self.js)
        # 向前/向后都从当前页之外开始扫描，不能只停留在同页
        self.assertIn("for (let i = cur + 1; i < pages.length; i++)", self.js)
        self.assertIn("for (let i = cur - 1; i >= 0; i--)", self.js)
        self.assertIn("jumpToPageIndex(target)", self.js)
        self.assertIn("showErrPopup(rect)", self.js)

    def test_app_js_syntax(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        r = subprocess.run(
            [node, "--check", os.path.join(ROOT, "ui", "app.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
