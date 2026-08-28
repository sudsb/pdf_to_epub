"""synth_epub_test.py — 端到端 EPUB 合成测试（2026-08-23）。

构建一个最小 structured_doc（含 h1 章节 + h3 子章节 + 普通段落），运行
HTMLConverter.convert_document + EPUBPacker.pack，验证：
  1. content_1.xhtml 中 h1 带内联 text-align:center
  2. content 中 h3 也带内联 text-align:center（2026-08-23 修复）
  3. style.css 含 h1-h6 居中规则
  4. EPUB zip 包含 OEBPS/Styles/style.css
  5. 所有 .xhtml 均能被 xml.etree 解析（XML 良构）

运行：python synth_epub_test.py
"""
import os
import sys
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET

import htmlmanage
from epubmanage import pack_from_oebps, EPUBMetadata


def _build_structured_doc():
    """构造含 h1 章节（带 ptoe-align-left 类） + h3 子章节 + 普通段落的最小 structured_doc。"""
    return {
        'meta': {
            'title': '测试书',
            'author': '测试作者',
            'language': 'zh-CN',
            'package_epub': True,
        },
        'pages': [
            {'page': 1, 'text': '<h1 class="ptoe-align-left">第一章</h1><p>第一章的正文内容。</p><h3>子节</h3><p>子节内容。</p>'},
            {'page': 2, 'text': '<p>第二章的正文内容。</p>'},
        ],
    }


def _check(label, condition):
    """简单断言助手：打印 PASS/FAIL。"""
    if condition:
        print(f"  PASS: {label}")
        return True
    else:
        print(f"  FAIL: {label}")
        return False


def main():
    tmpdir = tempfile.mkdtemp(prefix="synth_epub_")
    passed = 0
    failed = 0

    try:
        # 1. 转换文档
        converter = htmlmanage.HTMLConverter(output_dir=tmpdir)
        structured = _build_structured_doc()
        result = converter.convert_document(structured, merge_pages=True)

        # 2. 打包 EPUB
        epub_path = result.get('epub')
        if not epub_path or not os.path.isfile(epub_path):
            print(f"  FAIL: EPUB 文件未生成: {epub_path}")
            failed += 1
            return 1

        print(f"  EPUB 生成: {epub_path}")

        # 3. 检查 content_1.xhtml 中的 h1 内联居中
        content_files = result.get('content_files', [])
        if not content_files:
            print("  FAIL: 无 content 文件")
            failed += 1
            return 1

        content_1_path = os.path.join(tmpdir, content_files[0])
        with open(content_1_path, 'r', encoding='utf-8') as f:
            content_1 = f.read()

        if _check("content_1.xhtml 含 h1 内联居中",
                  'text-align:center' in content_1 and '<h1' in content_1):
            passed += 1
        else:
            failed += 1

        # 3. 检查 content 中 h3 也带内联居中（2026-08-23 修复：之前只处理 h1/h2）
        if _check("content_1.xhtml 含 h3 内联居中",
                  'text-align:center' in content_1 and '<h3' in content_1):
            passed += 1
        else:
            failed += 1

        # 3b. 检查带 ptoe-align-left 类的 h1 仍然内联居中（2026-08-23 修复：
        #     之前豁免 ptoe-align-* 类导致历史数据标题不居中）
        if _check("content_1.xhtml 含 ptoe-align-left h1 内联居中",
                  'ptoe-align-left' in content_1 and 'text-align:center' in content_1):
            passed += 1
        else:
            failed += 1

        # 4. 检查 style.css 含 h1-h6 居中规则
        css_path = os.path.join(tmpdir, 'OEBPS', 'style.css')
        if os.path.isfile(css_path):
            with open(css_path, 'r', encoding='utf-8') as f:
                css = f.read()
            if _check("style.css 含 h1/h2 + h3-h6 居中规则",
                      'text-align: center' in css and 'h1, h2' in css and 'h3, h4, h5, h6' in css):
                passed += 1
            else:
                failed += 1
        else:
            print("  FAIL: style.css 不存在")
            failed += 1

        # 5. 检查 EPUB zip 包含 OEBPS/Styles/style.css
        with zipfile.ZipFile(epub_path, 'r') as zf:
            names = zf.namelist()
            if _check("EPUB zip 包含 OEBPS/Styles/style.css",
                      'OEBPS/Styles/style.css' in names):
                passed += 1
            else:
                failed += 1

            # 6. 检查所有 .xhtml 均为良构 XML
            xhtml_files = [n for n in names if n.endswith('.xhtml')]
            all_xml_ok = True
            for xname in xhtml_files:
                try:
                    data = zf.read(xname)
                    ET.fromstring(data)
                except ET.ParseError as e:
                    print(f"  FAIL: {xname} XML 解析错误: {e}")
                    all_xml_ok = False
            if _check(f"所有 {len(xhtml_files)} 个 .xhtml 均为良构 XML", all_xml_ok):
                passed += 1
            else:
                failed += 1

            # 7. 检查 mimetype 是第一个条目
            if _check("mimetype 是第一个条目", names[0] == 'mimetype'):
                passed += 1
            else:
                failed += 1

            # 8. 检查 container.xml 存在
            if _check("META-INF/container.xml 存在", 'META-INF/container.xml' in names):
                passed += 1
            else:
                failed += 1

            # 9. 检查 content.opf 存在
            if _check("OEBPS/content.opf 存在", 'OEBPS/content.opf' in names):
                passed += 1
            else:
                failed += 1

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n  总计: {passed} 通过, {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
