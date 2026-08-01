"""
epubmanage.py

Create an EPUB file from an OEBPS directory tree produced by htmlmanage.py.

Provides:
- EPUBMetadata for metadata handling
- ResourceMapper to map source files into EPUB-relative paths safely
- EPUBPacker that streams files into a .epub with correct mimetype first (stored)

This implementation avoids reading all resources into memory and writes files
one-by-one to the zip archive. It supports generating minimal container.xml,
content.opf and a basic toc.ncx for EPUB 2 compatibility.
"""
from __future__ import annotations

import os
import re
import zipfile
import uuid
import xml.etree.ElementTree as ET
from typing import Dict, List, Tuple, Optional
from datetime import datetime
from urllib.parse import quote as urlquote


def _rewrite_flat_refs(content: str) -> str:
    """Rewrite XHTML references written for a flat OEBPS/ layout so they stay valid
    after ResourceMapper moves XHTML files into OEBPS/Text/ (siblings move up one
    level: Images/ -> ../Images/, Styles/ -> ../Styles/, style.css -> ../Styles/style.css)."""
    content = re.sub(r'(src|href)=(["\'])(Images|Styles)/', r'\1=\2../\3/', content)
    content = re.sub(r'href=(["\'])style\.css\1', r'href=\1../Styles/style.css\1', content)
    return content


def _natural_key(name: str):
    """数字感知排序键：content_2.xhtml < content_10.xhtml（避免字典序乱序）。"""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)]


class EPUBMetadata:
    def __init__(self, title: str, author: str, language: str = 'en', identifier: Optional[str] = None, publisher: Optional[str] = None, date: Optional[str] = None, description: Optional[str] = None):
        self.title = title
        self.author = author
        self.language = language
        self.identifier = identifier or self.generate_uuid()
        self.publisher = publisher
        self.date = date or datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        self.description = description

    def generate_uuid(self) -> str:
        # stable-ish UUID based on title+author
        ns = uuid.uuid5(uuid.NAMESPACE_URL, f"{self.title}|{self.author}")
        return f"urn:uuid:{ns}"

    def validate(self) -> None:
        if not self.title:
            raise ValueError('title is required')
        if not self.language:
            raise ValueError('language is required')
        if not self.identifier:
            self.identifier = self.generate_uuid()

    def to_opf_metadata(self) -> ET.Element:
        nsmap = {
            'dc': 'http://purl.org/dc/elements/1.1/'
        }
        metadata = ET.Element('metadata')
        dc_title = ET.SubElement(metadata, '{http://purl.org/dc/elements/1.1/}title')
        dc_title.text = self.title
        dc_lang = ET.SubElement(metadata, '{http://purl.org/dc/elements/1.1/}language')
        dc_lang.text = self.language
        dc_id = ET.SubElement(metadata, '{http://purl.org/dc/elements/1.1/}identifier')
        dc_id.set('id', 'pub-id')
        dc_id.text = self.identifier
        dc_creator = ET.SubElement(metadata, '{http://purl.org/dc/elements/1.1/}creator')
        dc_creator.text = self.author
        if self.publisher:
            dc_pub = ET.SubElement(metadata, '{http://purl.org/dc/elements/1.1/}publisher')
            dc_pub.text = self.publisher
        if self.date:
            dc_date = ET.SubElement(metadata, '{http://purl.org/dc/elements/1.1/}date')
            dc_date.text = self.date
        if self.description:
            dc_desc = ET.SubElement(metadata, '{http://purl.org/dc/elements/1.1/}description')
            dc_desc.text = self.description
        return metadata


class ResourceMapper:
    def __init__(self, oebps_root: str):
        self.oebps_root = oebps_root
        self.source_to_epub_path: Dict[str, str] = {}
        self.counter: Dict[str, int] = {}

    @staticmethod
    def sanitize_filename(name: str) -> str:
        # keep ASCII, hyphen, underscore, dot; percent-encode other chars
        safe_chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
        return ''.join(c if c in safe_chars else urlquote(c, safe='') for c in name)

    def map_file(self, original_path: str, category: str) -> str:
        """Map a source file to a path inside the epub (relative to OEBPS).
        category in ['Text','Styles','Images','Other']
        """
        basename = os.path.basename(original_path)
        safe = self.sanitize_filename(basename)
        folder = {
            'Text': 'Text',
            'Styles': 'Styles',
            'Images': 'Images',
            'Other': 'Misc'
        }.get(category, 'Misc')
        key = os.path.join(folder, safe)
        # avoid collisions
        if key in self.source_to_epub_path.values():
            # append numeric suffix
            base, ext = os.path.splitext(safe)
            i = self.counter.get(key, 1)
            while True:
                candidate = os.path.join(folder, f"{base}_{i}{ext}")
                if candidate not in self.source_to_epub_path.values():
                    key = candidate
                    self.counter[key] = i + 1
                    break
                i += 1
        self.source_to_epub_path[original_path] = key
        return key

    def get_manifest_items(self) -> List[Tuple[str, str]]:
        """Return list of (epub_path, mime_type).
        Mime types guessed from extension.
        """
        items = []
        for src, epub_path in self.source_to_epub_path.items():
            ext = os.path.splitext(epub_path)[1].lower()
            mime = 'application/octet-stream'
            if ext in ('.xhtml', '.html', '.htm'):
                mime = 'application/xhtml+xml'
            elif ext in ('.css',):
                mime = 'text/css'
            elif ext in ('.jpg', '.jpeg'):
                mime = 'image/jpeg'
            elif ext in ('.png',):
                mime = 'image/png'
            elif ext in ('.svg',):
                mime = 'image/svg+xml'
            items.append((epub_path.replace('\\', '/'), mime))
        return items


class EPUBPacker:
    def __init__(self, epub_path: str, root_dir: str, compression_level: int = 6, epub_version: str = '2.0'):
        self.epub_path = epub_path
        self.root_dir = root_dir
        self.compression_level = compression_level
        self.mapper = ResourceMapper(root_dir)
        self.epub_version = epub_version

    def initialize_structure(self):
        # ensure required folders exist
        meta_inf = os.path.join(self.root_dir, 'META-INF')
        oebps = os.path.join(self.root_dir, 'OEBPS')
        os.makedirs(meta_inf, exist_ok=True)
        os.makedirs(oebps, exist_ok=True)
        os.makedirs(os.path.join(oebps, 'Text'), exist_ok=True)
        os.makedirs(os.path.join(oebps, 'Styles'), exist_ok=True)
        os.makedirs(os.path.join(oebps, 'Images'), exist_ok=True)

    def add_resource(self, file_path: str, category: str) -> str:
        # map file into epub and return epub internal path
        epub_path = self.mapper.map_file(file_path, category)
        return epub_path

    def generate_mimetype(self):
        # must be uncompressed and first entry in zip
        return b'application/epub+zip'

    def generate_container_xml(self, opf_path: str = 'OEBPS/content.opf') -> str:
        container = ET.Element('container')
        container.set('version', '1.0')
        container.set('xmlns', 'urn:oasis:names:tc:opendocument:xmlns:container')
        rootfiles = ET.SubElement(container, 'rootfiles')
        rootfile = ET.SubElement(rootfiles, 'rootfile')
        rootfile.set('full-path', opf_path)
        rootfile.set('media-type', 'application/oebps-package+xml')
        return ET.tostring(container, encoding='unicode')

    def generate_content_opf(self, metadata: EPUBMetadata, manifest_items: List[Tuple[str, str]], spine_itemrefs: List[str], opf_id: str = 'pub-id') -> str:
        """Generate OPF compatible with EPUB2 or EPUB3 depending on self.epub_version."""
        pkg_version = '3.0' if str(self.epub_version).startswith('3') else '2.0'
        package = ET.Element('package')
        package.set('version', pkg_version)
        package.set('xmlns', 'http://www.idpf.org/2007/opf')
        package.set('unique-identifier', opf_id)

        # metadata
        meta_el = metadata.to_opf_metadata()
        package.append(meta_el)

        manifest = ET.SubElement(package, 'manifest')
        if not pkg_version.startswith('3'):
            ET.SubElement(manifest, 'item', id='ncx', href='toc.ncx', **{'media-type': 'application/x-dtbncx+xml'})

        # Files are re-mapped into OEBPS/Text|Styles|Images by ResourceMapper, so the
        # EPUB3 nav entry must reference the mapped location: mark the matching item
        # rather than adding a separate flat 'nav.xhtml' item that would not exist.
        nav_href = next((h for h, _m in manifest_items if os.path.basename(h) == 'nav.xhtml'), None)
        for i, (href, mtype) in enumerate(manifest_items):
            item_attrs = {'media-type': mtype}
            if pkg_version.startswith('3') and nav_href is not None and href == nav_href:
                item_attrs['properties'] = 'nav'
            ET.SubElement(manifest, 'item', id=f'item{i+1}', href=href, **item_attrs)

        # spine
        spine = ET.SubElement(package, 'spine')
        if not pkg_version.startswith('3'):
            spine.set('toc', 'ncx')
        for ref in spine_itemrefs:
            ET.SubElement(spine, 'itemref', idref=ref)

        return ET.tostring(package, encoding='unicode')

    def generate_toc_ncx(self, metadata: EPUBMetadata, toc_items: List[Dict[str, str]]) -> str:
        # minimal NCX
        ncx = ET.Element('ncx')
        ncx.set('xmlns', 'http://www.daisy.org/z3986/2005/ncx/')
        ncx.set('version', '2005-1')
        head = ET.SubElement(ncx, 'head')
        ET.SubElement(head, 'meta', name='dtb:uid', content=metadata.identifier)
        ET.SubElement(head, 'meta', name='dtb:depth', content='1')
        ET.SubElement(head, 'meta', name='dtb:totalPageCount', content='0')
        ET.SubElement(head, 'meta', name='dtb:maxPageNumber', content='0')
        docTitle = ET.SubElement(ncx, 'docTitle')
        ET.SubElement(docTitle, 'text').text = metadata.title
        docAuthor = ET.SubElement(ncx, 'docAuthor')
        ET.SubElement(docAuthor, 'text').text = metadata.author
        navMap = ET.SubElement(ncx, 'navMap')
        for i, it in enumerate(toc_items):
            navPoint = ET.SubElement(navMap, 'navPoint', id=f'navPoint-{i+1}', playOrder=str(i+1))
            navLabel = ET.SubElement(navPoint, 'navLabel')
            ET.SubElement(navLabel, 'text').text = it.get('title')
            ET.SubElement(navPoint, 'content', src=it.get('href'))
        return ET.tostring(ncx, encoding='unicode')

    def pack(self, metadata: EPUBMetadata, spine_order: List[str], toc_items: List[Dict[str, str]]) -> str:
        """Perform the pack. spine_order is list of epub internal hrefs in reading order (e.g. ['Text/cover.xhtml','Text/content_1.xhtml', ...])
        toc_items: list of {'title':..., 'href':...}
        """
        self.initialize_structure()
        metadata.validate()

        # gather files under OEBPS for mapping if not already mapped
        # scan OEBPS directory for files and register them
        oebps = os.path.join(self.root_dir, 'OEBPS')
        manifest_sources: List[Tuple[str,str]] = []  # (src_path, epub_href)
        for root, dirs, files in os.walk(oebps):
            for fn in files:
                src = os.path.join(root, fn)
                # compute relative to oebps
                rel = os.path.relpath(src, oebps)
                category = 'Other'
                if rel.lower().endswith(('.xhtml', '.html', '.htm')):
                    category = 'Text'
                elif rel.lower().endswith('.css'):
                    category = 'Styles'
                elif rel.lower().endswith(('.png', '.jpg', '.jpeg', '.svg')):
                    category = 'Images'
                epub_path = self.mapper.map_file(src, category)
                manifest_sources.append((src, epub_path))

        manifest_items = self.mapper.get_manifest_items()

        # manifest hrefs are mapped paths like 'Text/content_1.xhtml'; resolve the
        # requested spine hrefs (e.g. 'content_1.xhtml') to their manifest item ids
        href_to_id = {href: f'item{i+1}' for i, (href, _m) in enumerate(manifest_items)}

        def _resolve_id(href: str) -> Optional[str]:
            base = os.path.basename(href)
            for hid in href_to_id:
                if os.path.basename(hid) == base:
                    return href_to_id[hid]
            return None

        opf_content = self.generate_content_opf(metadata, manifest_items, [_resolve_id(r) or r for r in spine_order])
        ncx_content = ''
        if not str(self.epub_version).startswith('3'):
            # toc.ncx lives at OEBPS/ root; its srcs must point at the mapped paths
            mapped_toc = []
            for it in toc_items:
                href = it.get('href', '')
                base = os.path.basename(href)
                mapped = next(
                    (e.replace('\\', '/') for _s, e in manifest_sources if os.path.basename(e) == base),
                    href,
                )
                mapped_toc.append({'title': it.get('title'), 'href': mapped})
            ncx_content = self.generate_toc_ncx(metadata, mapped_toc)

        # write into epub zip: mimetype (stored), then rest
        # open zipfile and write mimetype first uncompressed
        with zipfile.ZipFile(self.epub_path, 'w') as zf:
            # write mimetype
            zf.writestr('mimetype', self.generate_mimetype(), compress_type=zipfile.ZIP_STORED)
            # container.xml
            container_xml = self.generate_container_xml()
            zf.writestr('META-INF/container.xml', container_xml)
            # write all mapped resources streaming from disk
            for src, epub_path in manifest_sources:
                arcname = os.path.join('OEBPS', epub_path).replace('\\', '/')
                if epub_path.lower().endswith(('.xhtml', '.html', '.htm')):
                    # XHTML files move into OEBPS/Text/; references written for the
                    # flat OEBPS/ layout (style.css, Images/, Styles/) move up a level
                    with open(src, 'r', encoding='utf-8') as f:
                        content = f.read()
                    zf.writestr(arcname, _rewrite_flat_refs(content))
                else:
                    zf.write(src, arcname)
            # write content.opf
            zf.writestr('OEBPS/content.opf', opf_content)
            # write toc.ncx for EPUB2
            if ncx_content:
                zf.writestr('OEBPS/toc.ncx', ncx_content)
        return self.epub_path

def pack_from_oebps(root_dir: str, epub_path: str, metadata: EPUBMetadata, epub_version: str = '2.0') -> str:
    """Convenience wrapper: discover OEBPS contents and package into epub_path.
    Order: cover.xhtml (if present), nav.xhtml (if present), then content_*.xhtml sorted.
    """
    oebps = os.path.join(root_dir, 'OEBPS')
    if not os.path.isdir(oebps):
        raise FileNotFoundError(f'OEBPS directory not found: {oebps}')
    files = sorted([f for f in os.listdir(oebps) if os.path.isfile(os.path.join(oebps, f))], key=_natural_key)
    cover = 'cover.xhtml' if 'cover.xhtml' in files else None
    nav = 'nav.xhtml' if 'nav.xhtml' in files else None
    contents = [f for f in files if f.startswith('content_') and f.endswith('.xhtml')]
    spine = []
    if cover:
        spine.append(os.path.join('OEBPS', cover))
    if nav:
        spine.append(os.path.join('OEBPS', nav))
    for c in contents:
        spine.append(os.path.join('OEBPS', c))
    toc_items = [{'title': os.path.splitext(os.path.basename(p))[0], 'href': os.path.relpath(p, 'OEBPS')} for p in spine]
    packer = EPUBPacker(epub_path, root_dir, epub_version=epub_version)
    return packer.pack(metadata, [os.path.relpath(s, 'OEBPS') for s in spine], toc_items)


# convenience
if __name__ == '__main__':
    print('epubmanage - create an .epub file from an OEBPS folder')
