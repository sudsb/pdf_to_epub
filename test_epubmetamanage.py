"""test_epubmetamanage.py — 既有 EPUB 的元数据读取/改写（书名/作者/简介/封面）。

构造一个最小合规 EPUB（mimetype 首项 ZIP_STORED + container.xml + OPF），
验证 read_epub_metadata / save_epub_metadata 的往返与原子性。
"""

import os
import shutil
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

import epubmetamanage

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
_PNG2 = _PNG + b"\x00"

_CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>旧标题</dc:title>
    <dc:creator>旧作者</dc:creator>
    <dc:language>zh-CN</dc:language>
    <dc:description>旧简介</dc:description>
    <dc:identifier id="pub-id">urn:uuid:1234</dc:identifier>
    <meta name="cover" content="cover-img"/>
  </metadata>
  <manifest>
    <item id="cover-img" href="Images/cover.png" media-type="image/png"/>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="c1" href="content.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="c1"/>
  </spine>
</package>"""

_OPF_NO_COVER = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>无封面</dc:title>
    <dc:creator>某人</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="c1" href="content.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="c1"/></spine>
</package>"""

_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>
<body><p>正文</p></body></html>"""


def _write_epub(path, opf=_OPF, with_cover=True):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _CONTAINER)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/content.xhtml", _XHTML)
        zf.writestr("OEBPS/nav.xhtml", _XHTML)
        if with_cover:
            zf.writestr("OEBPS/Images/cover.png", _PNG)


class _EpubCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_epubmeta_")
        self.epub = os.path.join(self.tmp, "book.epub")
        _write_epub(self.epub)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _opf_root(self, path):
        with zipfile.ZipFile(path) as zf:
            return ET.fromstring(zf.read("OEBPS/content.opf")), zf.namelist()


class TestReadMetadata(_EpubCase):
    def test_read_fields_and_cover(self):
        data = epubmetamanage.read_epub_metadata(self.epub)
        self.assertTrue(data["ok"])
        self.assertEqual(data["title"], "旧标题")
        self.assertEqual(data["author"], "旧作者")
        self.assertEqual(data["description"], "旧简介")
        self.assertEqual(data["language"], "zh-CN")
        self.assertEqual(data["cover_name"], "cover.png")
        self.assertTrue(data["cover_data_uri"].startswith("data:image/png;base64,"))

    def test_read_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            epubmetamanage.read_epub_metadata(os.path.join(self.tmp, "nope.epub"))

    def test_read_no_cover(self):
        p = os.path.join(self.tmp, "nc.epub")
        _write_epub(p, opf=_OPF_NO_COVER, with_cover=False)
        data = epubmetamanage.read_epub_metadata(p)
        self.assertEqual(data["title"], "无封面")
        self.assertIsNone(data["cover_data_uri"])


class TestSaveMetadata(_EpubCase):
    def test_save_updates_fields_preserves_others(self):
        res = epubmetamanage.save_epub_metadata(
            self.epub, title="新标题", author="新作者", description="新简介"
        )
        self.assertEqual(res["path"], os.path.abspath(self.epub))
        data = epubmetamanage.read_epub_metadata(self.epub)
        self.assertEqual(data["title"], "新标题")
        self.assertEqual(data["author"], "新作者")
        self.assertEqual(data["description"], "新简介")
        self.assertEqual(data["language"], "zh-CN")  # 未传 → 保留
        root, names = self._opf_root(self.epub)
        # 结构完整：manifest/spine 与封面条目仍在
        self.assertIn("OEBPS/content.xhtml", names)
        self.assertIn("OEBPS/Images/cover.png", names)
        self.assertEqual(names[0], "mimetype")

    def test_save_mimetype_first_stored(self):
        epubmetamanage.save_epub_metadata(self.epub, title="X")
        with zipfile.ZipFile(self.epub) as zf:
            info = zf.infolist()
        self.assertEqual(info[0].filename, "mimetype")
        self.assertEqual(info[0].compress_type, zipfile.ZIP_STORED)
        # OPF 必须仍是良构 XML
        root, _ = self._opf_root(self.epub)
        self.assertTrue(root.tag.endswith("package"))

    def test_save_replaces_cover(self):
        epubmetamanage.save_epub_metadata(
            self.epub, cover_bytes=_PNG2, cover_ext=".png"
        )
        with zipfile.ZipFile(self.epub) as zf:
            self.assertEqual(zf.read("OEBPS/Images/cover.png"), _PNG2)
        data = epubmetamanage.read_epub_metadata(self.epub)
        self.assertEqual(data["cover_name"], "cover.png")
        # 仍只有一条 cover 元数据条目
        root, _ = self._opf_root(self.epub)
        metas = [m for m in root.iter() if epubmetamanage._local(m.tag) == "meta"]
        cover_metas = [m for m in metas if (m.get("name") or "").lower() == "cover"]
        self.assertEqual(len(cover_metas), 1)

    def test_save_adds_cover_when_missing(self):
        p = os.path.join(self.tmp, "nc.epub")
        _write_epub(p, opf=_OPF_NO_COVER, with_cover=False)
        epubmetamanage.save_epub_metadata(p, cover_bytes=_PNG, cover_ext=".png")
        with zipfile.ZipFile(p) as zf:
            names = zf.namelist()
        self.assertTrue(any(n.endswith("cover.png") for n in names), names)
        data = epubmetamanage.read_epub_metadata(p)
        self.assertIsNotNone(data["cover_data_uri"])

    def test_save_out_path_leaves_original(self):
        dest = os.path.join(self.tmp, "copy.epub")
        epubmetamanage.save_epub_metadata(self.epub, title="副本", out_path=dest)
        self.assertTrue(os.path.isfile(dest))
        self.assertEqual(epubmetamanage.read_epub_metadata(self.epub)["title"], "旧标题")
        self.assertEqual(epubmetamanage.read_epub_metadata(dest)["title"], "副本")

    def test_save_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            epubmetamanage.save_epub_metadata(os.path.join(self.tmp, "nope.epub"), title="x")

    def test_save_noop_keeps_metadata(self):
        epubmetamanage.save_epub_metadata(self.epub)
        data = epubmetamanage.read_epub_metadata(self.epub)
        self.assertEqual(data["title"], "旧标题")
        self.assertEqual(data["author"], "旧作者")


if __name__ == "__main__":
    unittest.main()
