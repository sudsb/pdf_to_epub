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


if __name__ == '__main__':
    unittest.main()