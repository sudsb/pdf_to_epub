#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_epubmanage_pack.py
Tests for EPUBPacker.pack() method to ensure proper handling of non-XHTML resources.
"""

import os
import tempfile
import shutil
import zipfile
import unittest
from epubmanage import EPUBPacker, EPUBMetadata


class TestEPUBPackerPack(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for our test OEBPS structure
        self.temp_dir = tempfile.mkdtemp()
        self.oebps_dir = os.path.join(self.temp_dir, 'OEBPS')
        os.makedirs(self.oebps_dir)
        os.makedirs(os.path.join(self.oebps_dir, 'Images'))
        os.makedirs(os.path.join(self.oebps_dir, 'Styles'))

        # Create test files
        # content_1.xhtml with flat references (as expected after ResourceMapper processing)
        self.content_file = os.path.join(self.oebps_dir, 'content_1.xhtml')
        with open(self.content_file, 'w', encoding='utf-8') as f:
            f.write('''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
    <title>Test</title>
    <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
    <p>正文</p>
    <p><img src="Images/i.png" alt=""/></p>
</body>
</html>''')

        # Create a dummy image file
        self.image_file = os.path.join(self.oebps_dir, 'Images', 'i.png')
        with open(self.image_file, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')

        # Create a style.css file
        self.style_file = os.path.join(self.oebps_dir, 'Styles', 'style.css')
        with open(self.style_file, 'w') as f:
            f.write('p{}')

        # Set up EPUBPacker
        self.epub_path = os.path.join(self.temp_dir, 'test.epub')
        self.metadata = EPUBMetadata(title='Test EPUB', author='Test Author', language='zh-CN')
        self.packer = EPUBPacker(self.epub_path, self.temp_dir)

    def tearDown(self):
        # Clean up the temporary directory
        shutil.rmtree(self.temp_dir)

    def test_pack_includes_non_xhtml_resources(self):
        # Call pack method
        spine_order = ['OEBPS/content_1.xhtml']
        toc_items = [{'title': 'Test', 'href': 'OEBPS/content_1.xhtml'}]
        result_path = self.packer.pack(self.metadata, spine_order, toc_items)

        # Verify the EPUB was created
        self.assertTrue(os.path.exists(result_path))
        self.assertEqual(result_path, self.epub_path)

        # Open the EPUB as a zip file and check contents
        with zipfile.ZipFile(self.epub_path, 'r') as zf:
            namelist = zf.namelist()

            # Check that mimetype is first entry and is ZIP_STORED
            self.assertEqual(namelist[0], 'mimetype')
            mimetype_info = zf.getinfo('mimetype')
            self.assertEqual(mimetype_info.compress_type, zipfile.ZIP_STORED)

            # Check that non-XHTML resources are included
            self.assertIn('OEBPS/Images/i.png', namelist)
            self.assertIn('OEBPS/Styles/style.css', namelist)
            self.assertIn('OEBPS/Text/content_1.xhtml', namelist)

            # Verify image bytes round-trip identical
            with zf.open('OEBPS/Images/i.png') as image_file:
                image_data = image_file.read()
            with open(self.image_file, 'rb') as original_file:
                original_data = original_file.read()
            self.assertEqual(image_data, original_data)

            # Verify content_1.xhtml does NOT contain U+3000 (proves _add_epub_par_indent removal)
            with zf.open('OEBPS/Text/content_1.xhtml') as content_file:
                content_data = content_file.read().decode('utf-8')
            self.assertNotIn('\u3000', content_data, "Found U+3000 in content, indicating _add_epub_par_indent was not removed")

            # Also verify the content has the expected structure (after _rewrite_flat_refs)
            self.assertIn('<p>正文</p>', content_data)
            self.assertIn('<p><img src="../Images/i.png" alt=""/></p>', content_data)
            self.assertIn('<link rel="stylesheet" type="text/css" href="../Styles/style.css"/>', content_data)

    def test_pack_multi_level_toc_nested_ncx(self):
        # 2026-09-24：多级标题目录在 NCX 中按层级嵌套 navPoint，
        # dtb:depth 为实际最大深度，playOrder 按文档序连续递增
        import xml.etree.ElementTree as ET
        ns = '{http://www.daisy.org/z3986/2005/ncx/}'

        def _text(el):
            label = el.find(ns + 'navLabel/' + ns + 'text')
            return label.text if label is not None else None

        def _src(el):
            content = el.find(ns + 'content')
            return content.get('src') if content is not None else None

        spine_order = ['OEBPS/content_1.xhtml']
        toc_items = [
            {'title': '第一章', 'href': 'OEBPS/content_1.xhtml#h1', 'level': 1},
            {'title': '1.1', 'href': 'OEBPS/content_1.xhtml#h2', 'level': 2},
            {'title': '1.1.1', 'href': 'OEBPS/content_1.xhtml#h3', 'level': 3},
            {'title': '1.2', 'href': 'OEBPS/content_1.xhtml#h4', 'level': 2},
            {'title': '第二章', 'href': 'OEBPS/content_1.xhtml#h5', 'level': 1},
            {'title': '2.1', 'href': 'OEBPS/content_1.xhtml#h6', 'level': 2},
        ]
        result_path = self.packer.pack(self.metadata, spine_order, toc_items)
        self.assertTrue(os.path.exists(result_path))

        with zipfile.ZipFile(result_path, 'r') as zf:
            self.assertIn('OEBPS/toc.ncx', zf.namelist())
            root = ET.fromstring(zf.read('OEBPS/toc.ncx'))
            navMap = root.find('{http://www.daisy.org/z3986/2005/ncx/}navMap')
            self.assertIsNotNone(navMap)
            ns = '{http://www.daisy.org/z3986/2005/ncx/}'
            top = [ch for ch in navMap if ch.tag == ns + 'navPoint']
            # 顶层两章
            self.assertEqual([p.findtext(ns + 'navLabel/' + ns + 'text') for p in top],
                             ['第一章', '第二章'])
            # 第一章下嵌套 1.1（含孙级 1.1.1）与 1.2
            ch1 = [ch for ch in top[0] if ch.tag == ns + 'navPoint']
            self.assertEqual([p.findtext(ns + 'navLabel/' + ns + 'text') for p in ch1],
                             ['1.1', '1.2'])
            grand = [ch for ch in ch1[0] if ch.tag == ns + 'navPoint']
            self.assertEqual([p.findtext(ns + 'navLabel/' + ns + 'text') for p in grand], ['1.1.1'])
            # 第二章下嵌套 2.1（1.2 与 2.1 同级但挂在各自章下）
            ch2 = [ch for ch in top[1] if ch.tag == ns + 'navPoint']
            self.assertEqual([p.findtext(ns + 'navLabel/' + ns + 'text') for p in ch2], ['2.1'])
            # dtb:depth = 实际最大嵌套深度 3
            depth = root.find(ns + 'head/' + ns + "meta[@name='dtb:depth']")
            self.assertIsNotNone(depth)
            self.assertEqual(depth.get('content'), '3')
            # playOrder 与 id 按文档序连续
            orders = [p.get('playOrder') for p in root.iter(ns + 'navPoint')]
            self.assertEqual(orders, [str(i) for i in range(1, 7)])
            ids = [p.get('id') for p in root.iter(ns + 'navPoint')]
            self.assertEqual(ids, [f'navPoint-{i}' for i in range(1, 7)])
            # mapped href 保留 #fragment（相对 OEBPS/ 的路径）
            srcs = [p.find(ns + 'content').get('src') for p in root.iter(ns + 'navPoint')]
            self.assertEqual(srcs, [f'Text/content_1.xhtml#h{i}' for i in range(1, 7)])
            # 层级缺失的条目仍可打包（默认按一级，平铺顶层，不抛异常）
            flat_items = [{'title': 'Test', 'href': 'OEBPS/content_1.xhtml'}]
            flat_result = self.packer.pack(self.metadata, spine_order, flat_items)
            with zipfile.ZipFile(flat_result, 'r') as zf2:
                flat_root = ET.fromstring(zf2.read('OEBPS/toc.ncx'))
                flat_depth = flat_root.find(ns + 'head/' + ns + "meta[@name='dtb:depth']")
                self.assertEqual(flat_depth.get('content'), '1')


if __name__ == '__main__':
    unittest.main()