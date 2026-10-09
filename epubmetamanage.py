"""epubmetamanage.py — 读取 / 编辑「已存在」EPUB 的元数据（书名 / 作者 / 简介 / 封面）。

GUI「书籍信息」工具页的后端能力：打开一个现成的 .epub，解析其 OPF 元数据，
修改后原地重新打包（mimetype 仍为首项且不压缩，保持 EPUB 合规）。

仅依赖标准库（zipfile + xml.etree.ElementTree），不引入 pymupdf 等重依赖；
所有文件读取失败都以标准异常抛出，由调用方（guimanage 端点）转成中文错误。
"""

from __future__ import annotations

import base64
import errno
import os
import stat
import tempfile
import time
import zipfile
from typing import Any, Optional
from urllib.parse import unquote
import xml.etree.ElementTree as ET

__all__ = ["read_epub_metadata", "save_epub_metadata"]

_DC = "http://purl.org/dc/elements/1.1/"
_OPF = "http://www.idpf.org/2007/opf"

# 常见图片扩展名 → MIME
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

ET.register_namespace("dc", _DC)
ET.register_namespace("opf", _OPF)


def _local(tag: Any) -> str:
    """去掉 ``{namespace}`` 前缀，只保留本地名。"""
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag


def _is_dc(elem: ET.Element, local: str) -> bool:
    return _local(elem.tag) == local and (
        "}" not in elem.tag or elem.tag.startswith("{" + _DC + "}")
    )


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    """定位 OPF：优先 META-INF/container.xml 的 rootfile，回退首个 *.opf。"""
    try:
        data = zf.read("META-INF/container.xml")
        root = ET.fromstring(data)
        for elem in root.iter():
            if _local(elem.tag) == "rootfile":
                fp = elem.get("full-path")
                if fp:
                    return fp.replace("\\", "/")
    except (KeyError, ET.ParseError):
        pass
    for name in zf.namelist():
        if name.lower().endswith(".opf") and not name.endswith("/"):
            return name
    raise FileNotFoundError("EPUB 缺少 OPF（container.xml 无 rootfile，且无 .opf 文件）")


def _resolve_href(opf_dir: str, href: str) -> str:
    """把 OPF 内的相对 href 解析为 zip 内的绝对条目名（正斜杠）。"""
    href = (href or "").replace("\\", "/").split("#", 1)[0]
    if href.startswith("/"):
        return href.lstrip("/")
    if not opf_dir:
        return href
    return opf_dir.rstrip("/") + "/" + href


def _manifest(root: ET.Element):
    for elem in root:
        if _local(elem.tag) == "manifest":
            return elem
    return None


def _metadata(root: ET.Element):
    for elem in root:
        if _local(elem.tag) == "metadata":
            return elem
    return None


def _manifest_items(manifest: Optional[ET.Element]) -> list:
    if manifest is None:
        return []
    return [it for it in manifest if _local(it.tag) == "item"]


def _find_cover_href(root: ET.Element, metadata: Optional[ET.Element]) -> Optional[str]:
    """按优先级找封面：properties 含 cover-image → meta name=cover → 首个 image/*。"""
    manifest = _manifest(root)
    items = _manifest_items(manifest)
    # 1) EPUB3 标称
    for it in items:
        props = (it.get("properties") or "").split()
        if "cover-image" in props:
            href = it.get("href")
            if href:
                return href
    # 2) EPUB2 <meta name="cover" content="id">
    if metadata is not None:
        by_id = {it.get("id"): it for it in items}
        for meta in metadata:
            if _local(meta.tag) == "meta" and (meta.get("name") or "").lower() == "cover":
                it = by_id.get(meta.get("content"))
                if it is not None and it.get("href"):
                    return it.get("href")
    # 3) 回退：第一张图片
    for it in items:
        if (it.get("media-type") or "").startswith("image/") and it.get("href"):
            return it.get("href")
    return None


def _read_dc(root: ET.Element, local: str) -> str:
    metadata = _metadata(root)
    if metadata is None:
        return ""
    for elem in metadata:
        if _is_dc(elem, local):
            return (elem.text or "").strip()
    return ""


def read_epub_metadata(path: str) -> dict:
    """读取 EPUB 元数据与封面。

    返回 ``{ok, path, title, author, description, language,
    cover_name, cover_data_uri, cover_href}``；封面用 data URI 便于前端直接预览。
    """
    p = os.path.abspath(path)
    if not os.path.isfile(p):
        raise FileNotFoundError("文件不存在：%s" % path)
    with zipfile.ZipFile(p) as zf:
        opf_path = _find_opf_path(zf)
        opf_dir = os.path.dirname(opf_path)
        root = ET.fromstring(zf.read(opf_path))
        title = _read_dc(root, "title")
        author = _read_dc(root, "creator")
        description = _read_dc(root, "description")
        language = _read_dc(root, "language")

        cover_href = _find_cover_href(root, _metadata(root))
        cover_name = None
        cover_data_uri = None
        if cover_href:
            entry = _resolve_href(opf_dir, cover_href)
            raw = None
            try:
                raw = zf.read(entry)
            except KeyError:
                try:
                    raw = zf.read(unquote(entry))
                except KeyError:
                    raw = None
            if raw is not None:
                ext = os.path.splitext(entry)[1].lower()
                mime = _IMAGE_MIME.get(ext, "image/jpeg")
                cover_name = os.path.basename(entry)
                cover_data_uri = "data:%s;base64,%s" % (
                    mime,
                    base64.b64encode(raw).decode("ascii"),
                )
    return {
        "ok": True,
        "path": p,
        "title": title,
        "author": author,
        "description": description,
        "language": language,
        "cover_name": cover_name,
        "cover_data_uri": cover_data_uri,
        "cover_href": cover_href,
    }


def _set_dc(metadata: Optional[ET.Element], local: str, value: str) -> None:
    if metadata is None:
        return
    for elem in metadata:
        if _is_dc(elem, local):
            elem.text = value
            return
    ET.SubElement(metadata, "{%s}%s" % (_DC, local)).text = value


def _unique_entry(opf_dir: str, href: str, taken: set) -> str:
    base, ext = os.path.splitext(href)
    candidate = href
    n = 1
    while _resolve_href(opf_dir, candidate) in taken:
        candidate = "%s_%d%s" % (base, n, ext)
        n += 1
    return candidate


def _ensure_cover(
    root: ET.Element,
    metadata: Optional[ET.Element],
    opf_dir: str,
    cover_bytes: bytes,
    cover_ext: str,
    entries: dict,
) -> str:
    """就地更新 OPF 的封面声明，返回写入的 zip 条目名。"""
    manifest, items = _manifest(root), _manifest_items(_manifest(root))
    if manifest is None:
        manifest = ET.SubElement(root, "{%s}manifest" % _OPF)
        items = []

    ext = cover_ext.lower()
    if ext not in _IMAGE_MIME:
        ext = ".png"
    mime = _IMAGE_MIME[ext]

    existing_href = _find_cover_href(root, metadata)
    if existing_href:
        # 复用既有 href（避免破坏引用它的 cover.xhtml），仅刷新 MIME 与字节
        entry = _resolve_href(opf_dir, existing_href)
        for it in items:
            if it.get("href") == existing_href:
                it.set("media-type", mime)
                if "cover-image" not in (it.get("properties") or "").split():
                    it.set("properties", ((it.get("properties") or "") + " cover-image").strip())
                break
    else:
        href = _unique_entry(opf_dir, "Images/cover" + ext, set(entries))
        item = ET.SubElement(manifest, "{%s}item" % _OPF)
        item.set("id", "cover-image")
        item.set("href", href)
        item.set("media-type", mime)
        item.set("properties", "cover-image")
        if metadata is not None:
            already = any(
                _local(m.tag) == "meta" and (m.get("name") or "").lower() == "cover"
                for m in metadata
            )
            if not already:
                ET.SubElement(metadata, "{%s}meta" % _OPF, {"name": "cover", "content": "cover-image"})
        entry = _resolve_href(opf_dir, href)

    entries[entry] = cover_bytes
    return entry


def _replace_with_retry(tmp: str, dest: str) -> None:
    """原子替换 tmp→dest，带重试以应对 Windows 文件被占用/只读（WinError 5/32）。

    Windows 下若目标 EPUB 正被阅读器打开或带只读属性，os.replace 会抛
    PermissionError / OSError（winerror 5 拒绝访问、32 被占用）。这里最多尝试
    5 次，每次先尽力清除目标只读属性，仍失败则抛中文 OSError 供上层提示；
    与占用无关的其它 OSError 立即原样抛出，不做掩盖。
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, 6):
        try:
            os.replace(tmp, dest)
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            errno_val = getattr(exc, "errno", None)
            is_lock = (
                isinstance(exc, PermissionError)
                or winerror in (5, 32)
                or errno_val in (errno.EACCES, errno.EPERM)
            )
            if not is_lock:
                raise
            last_exc = exc
            # 尽力清除目标只读属性（目标不存在或清除失败都忽略）
            try:
                if os.path.exists(dest):
                    os.chmod(dest, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            if attempt < 5:
                time.sleep(0.2 * attempt)
    raise OSError(
        "无法写入目标 EPUB（文件可能正被其他程序打开或为只读）：%s" % dest
    ) from last_exc


def save_epub_metadata(
    path: str,
    *,
    title: Optional[str] = None,
    author: Optional[str] = None,
    description: Optional[str] = None,
    language: Optional[str] = None,
    cover_bytes: Optional[bytes] = None,
    cover_ext: Optional[str] = None,
    out_path: Optional[str] = None,
) -> dict:
    """原地（或另存到 ``out_path``）重写 EPUB 元数据与封面。

    只更新显式传入（非 None）的字段；未传的字段保持原值。封面仅在传入
    ``cover_bytes`` 时替换/新增。返回 ``{ok, path}``。
    """
    src = os.path.abspath(path)
    if not os.path.isfile(src):
        raise FileNotFoundError("文件不存在：%s" % path)

    with zipfile.ZipFile(src) as zf:
        opf_path = _find_opf_path(zf)
        opf_dir = os.path.dirname(opf_path)
        # 保留顺序、跳过目录项
        names = [n for n in zf.namelist() if not n.endswith("/")]
        entries = {n: zf.read(n) for n in names}
        root = ET.fromstring(entries[opf_path])

    metadata = _metadata(root)
    if metadata is None:
        metadata = ET.Element("{%s}metadata" % _OPF)
        root.insert(0, metadata)

    if title is not None:
        _set_dc(metadata, "title", title)
    if author is not None:
        _set_dc(metadata, "creator", author)
    if description is not None:
        _set_dc(metadata, "description", description)
    if language is not None:
        _set_dc(metadata, "language", language)

    if cover_bytes is not None:
        _ensure_cover(root, metadata, opf_dir, cover_bytes, cover_ext or ".png", entries)

    new_opf = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    entries[opf_path] = new_opf

    # 保持原顺序；新封面若无既有条目则追加到末尾
    ordered = [n for n in names if n in entries]
    for n in entries:
        if n not in ordered:
            ordered.append(n)
    # 若原文件没有 mimetype，补一个到最前
    has_mimetype = "mimetype" in entries
    if not has_mimetype:
        entries["mimetype"] = b"application/epub+zip"

    dest = os.path.abspath(out_path) if out_path else src
    fd, tmp = tempfile.mkstemp(prefix=".epubmeta-", suffix=".epub", dir=os.path.dirname(dest) or ".")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
            out.writestr("mimetype", entries["mimetype"], compress_type=zipfile.ZIP_STORED)
            for n in ordered:
                if n == "mimetype":
                    continue
                out.writestr(n, entries[n], compress_type=zipfile.ZIP_DEFLATED)
        _replace_with_retry(tmp, dest)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"ok": True, "path": dest}
