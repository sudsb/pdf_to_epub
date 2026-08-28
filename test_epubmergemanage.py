# -*- coding: utf-8 -*-
"""epubmergemanage 单元测试：合成 EPUB 端到端合并 + 命名/取消/损坏容错。"""

import base64
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from epubmergemanage import _default_out_path, merge_epubs

# 1x1 红色 PNG
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _make_epub(path, pages, title="测试书"):
    """用 htmlmanage 合成一本真实 EPUB（与主流程同一打包链路）。"""
    from htmlmanage import HTMLConverter

    tmp = tempfile.mkdtemp(prefix="ptoe_mkbook_")
    try:
        structured = {
            "pages": [{"page": i + 1, "text": t} for i, t in enumerate(pages)],
            "meta": {
                "title": title,
                "author": "",
                "language": "zh-CN",
                "epub_version": "3.0",
                "package_epub": True,
                "epub_path": str(path),
            },
        }
        res = HTMLConverter(tmp).convert_document(structured, merge_pages=True)
        assert res.get("epub"), f"合成输入书失败: {res}"
        return res["epub"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class TestDefaultOutPath(unittest.TestCase):
    def test_basic_naming(self):
        first = Path(tempfile.mkdtemp()) / "a.epub"
        out = Path(_default_out_path([str(first)], ""))
        self.assertEqual(out.name, "合并_a.epub")
        self.assertEqual(out.parent, first.parent)

    def test_title_used_when_given(self):
        first = Path(tempfile.mkdtemp()) / "a.epub"
        out = Path(_default_out_path([str(first)], "我的书"))
        self.assertEqual(out.name, "合并_我的书.epub")

    def test_collision_suffix(self):
        d = Path(tempfile.mkdtemp())
        (d / "合并_x.epub").write_bytes(b"x")
        out = Path(_default_out_path([str(d / "x.epub")], ""))
        self.assertEqual(out.name, "合并_x (1).epub")
        (d / "合并_x (1).epub").write_bytes(b"x")
        out2 = Path(_default_out_path([str(d / "x.epub")], ""))
        self.assertEqual(out2.name, "合并_x (2).epub")


class TestMergeEpubs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ptoe_merge_t_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _two_books(self):
        p1 = _make_epub(
            self.tmp / "b1.epub",
            ["<h1>第一卷 上</h1><p>甲书第一章内容</p>", "<h1>第一卷 下</h1><p>甲书第二章内容</p>"],
            title="甲书",
        )
        img_uri = f"data:image/png;base64,{_PNG_B64}"
        p2 = _make_epub(
            self.tmp / "b2.epub",
            [f'<h1>第二卷</h1><p>乙书正文</p><img src="{img_uri}" alt="图"/>'],
            title="乙书",
        )
        return p1, p2

    def test_merge_end_to_end(self):
        p1, p2 = self._two_books()
        logs = []
        res = merge_epubs([str(p1), str(p2)], progress=logs.append)
        self.assertTrue(res["ok"], res)
        out = Path(res["out_path"])
        self.assertTrue(out.is_file())
        self.assertEqual(out.parent, self.tmp)  # 默认存第一个文件同目录
        self.assertTrue(any("正在读取" in s for s in logs))
        self.assertTrue(any("完成" in s for s in logs))

        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            # mimetype 必须第一个且不压缩
            self.assertEqual(names[0], "mimetype")
            self.assertEqual(zf.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)
            blob = "".join(zf.read(n).decode("utf-8", "ignore") for n in names if n.endswith(".xhtml"))
            self.assertIn("甲书第一章内容", blob)
            self.assertIn("乙书正文", blob)
            # 两本书的 h1 标题都应进目录（htmlmanage 会压掉 h1 内空格）
            self.assertIn("第一卷", blob)
            self.assertIn("第二卷", blob)
            # 图片被提取为 OEBPS/Images/ 下真实文件
            images = [n for n in names if n.startswith("OEBPS/Images/")]
            self.assertTrue(images, f"无图片输出: {names}")

    def test_explicit_out_path_and_meta(self):
        p1, p2 = self._two_books()
        target = self.tmp / "自定义.epub"
        res = merge_epubs([str(p1), str(p2)], out_path=str(target), title="合集", author="某人")
        self.assertTrue(res["ok"], res)
        self.assertEqual(Path(res["out_path"]), target)

    def test_should_stop_cancels(self):
        p1, p2 = self._two_books()
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 1  # 第一次放行，第二次取消

        res = merge_epubs([str(p1), str(p2)], should_stop=stop)
        self.assertFalse(res["ok"])
        self.assertIn("取消", res["error"])

    def test_damaged_zip_skipped(self):
        p1, _ = self._two_books()
        bad = self.tmp / "bad.epub"
        bad.write_bytes(b"this is not a zip file")
        res = merge_epubs([str(bad), str(p1)])
        self.assertTrue(res["ok"], res)
        with zipfile.ZipFile(res["out_path"]) as zf:
            blob = "".join(zf.read(n).decode("utf-8", "ignore") for n in zf.namelist() if n.endswith(".xhtml"))
            self.assertIn("甲书第一章内容", blob)

    def test_all_damaged_error(self):
        bad1 = self.tmp / "bad1.epub"
        bad2 = self.tmp / "bad2.epub"
        bad1.write_bytes(b"nope")
        bad2.write_bytes(b"also nope")
        res = merge_epubs([str(bad1), str(bad2)])
        self.assertFalse(res["ok"])
        self.assertIn("有效正文", res["error"])

    def test_too_few_paths(self):
        res = merge_epubs([])
        self.assertFalse(res["ok"])
        res = merge_epubs(["only.epub"])
        self.assertFalse(res["ok"])
        self.assertIn("至少", res["error"])

    def test_missing_file_error(self):
        good, _ = self._two_books()
        ghost = self.tmp / "ghost.epub"
        res = merge_epubs([str(good), str(ghost)])
        self.assertFalse(res["ok"])
        self.assertIn("不存在", res["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
