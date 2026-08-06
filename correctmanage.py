"""
correctmanage.py — 手动矫正（OCR 文字 与 原图 对照）界面。

在 OCR 结构化之后、XHTML/EPUB 渲染之前插入的可选人工校对环节：
- 起一个本地 HTTP 服务（纯 stdlib，无第三方依赖），在浏览器中打开 HTML 界面；
- 左侧显示原始页面图片（低分辨率预览，点击切换原图），右侧显示识别出的文字（可编辑）；
- 文字按行渲染为块级元素，保留原始段落结构（HTML 会把文本节点里的换行折叠成空格）；
- 选中文字后弹出快捷菜单设置格式（粗体/斜体/标题），或点「设置」为每个操作绑定快捷键；
- 标记系统：
  * 全文标记：标明当前文章到此结束，后续内容属于新的一篇文章（生成 EPUB 时开新的一页）；
  * 段落标记：标明该处没有段落边界（OCR 把一整段拆成几段时，在断口处放一个即可拼回整段）；
    置于段首 → 与上一段合并为一整段；置于段尾 → 与下一段合并为一整段；
  * 注释标记：插入到正文中，由对应的注释段落（注释格式，小字）替换；
    一个注释段落对应一个注释标记，数量不匹配时提示（apply_markers 抛 ValueError）；
    插入的注释用中文括号（ ）括起，注释内部括号统一为中文括号；注释本身已带
    括号（视为已在正文中）时只改字号，不再重复加括号。
- 标记插入到光标处（段落标记的语义由位置决定：段首=与上一段合并，段尾=与下一段合并）；
- 注释格式：整段转为 class="ptoe-note"（字号小于正文），支持段落标记合并（有段落
  标记的注释属于同一段）；
- 左右两栏等高（CSS grid 拉伸），图片栏完整显示整张原图（点击切换预览/原图）；
- 「暂存」把当前修改保存到本地历史缓存（data/correction_history/，按 PDF 哈希，
  同一文件多版本，每文件保留最近 20 个）；「完成并转换」同样生成一个新版本；
  「保存」不新建版本，直接覆盖当前（最新）历史版本文件——同一份修改反复保存
  只更新同一个文件，不污染多版本列表；下次对同一 PDF 运行 --correct/correct
  自动加载最新版本；工具栏「历史记录」弹窗可查看（文件名/路径分列）并单删/
  多选删/全部删；
- 点「完成并转换」不关闭服务：每次点击都重新转换（on_convert 回调）并弹出完成/
  未完成提示（可留在页面继续修改后再次点击），询问是否关闭当前页面（浏览器禁止
  脚本自动关闭时提示手动关闭）；浏览器关闭超过 idle_timeout 才结束等待。
- 点「完成并转换」后服务关闭，返回校正后的 pages（与输入同构：{'page': int, 'text': str}）。

下游处理：mian 在矫正后调用 apply_markers() 把标记转换为文章结构
（全文 → 新文章/新页面，章节 → <h2> 标题，段落 → 合并段落），再交给
HTMLConverter 渲染；默认流水线（不带 --correct）完全不受影响。

性能/稳定性设计（1000+ 页）：
- 预览图按页从 PDF 低 DPI 惰性渲染（fitz），服务生命周期内复用同一个
  fitz.Document（避免每页重开 PDF），JPEG 内存缓存为 LRU（上限 200 页）+ Cache-Control；
- 历史列表用轻量索引（目录签名未变时复用解析结果），不每次都全量读所有版本文件；
- 浏览器端采用虚拟列表：只渲染视口附近 ~60 行，DOM 大小与页数无关；
- 图片 loading="lazy"；ThreadingHTTPServer 并发处理图片与保存请求；
- pages 读写共用锁（pages_lock），保存/暂存/完成与页面读取互斥；
- 历史缓存写入失败会向浏览器报错（不静默丢数据）。

用法：
    from correctmanage import correct_pages, apply_markers
    corrected = correct_pages(structured['pages'], pdf_path=pdf, img_dir=img_dir)
    articles = apply_markers(corrected)   # 有标记时生成文章结构
"""

from __future__ import annotations

import html as _html
import json
import re
import threading
import time
import webbrowser
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import OrderedDict
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = ["correct_pages", "sanitize_html", "apply_markers", "initial_html", "clean_page_html"]

# ---------------------------------------------------------------------------
# HTML 白名单清洗
# ---------------------------------------------------------------------------

_BLOCK_RE = re.compile(r"h[1-6]")

# 非内容标签：连同其文本内容整体丢弃（script/style/iframe 等）
_SKIP_TAGS = {"script", "style", "head", "iframe", "object", "embed"}

# 标记 span 的 data-ptoe-marker 合法值：全文 / 段落 / 注释 / 换页 / 第N章节（章节为旧数据兼容）
_MARKER_RE = re.compile(r"^(?:full|join|note|page|chapter:\d{1,2})$")

# apply_markers 用：匹配清洗后的标记 span
_MARKER_SPAN_RE = re.compile(
    r'<span\s+data-ptoe-marker="([^"]+)"[^>]*>(.*?)</span>',
    flags=re.IGNORECASE | re.DOTALL,
)

# 块级标签（apply_markers / htmlmanage 共用语义）；捕获尾部属性以识别 ptoe-note 类
_BLOCK_TAG_RE = re.compile(r"</?(p|h[1-6])([^>]*)>", flags=re.IGNORECASE)

# 注释格式：块级 class="ptoe-note"（字号小于正文，经注释标记插入正文）
_NOTE_CLASS = "ptoe-note"

# 块级对齐类（居中/居左/居右）：与 ptoe-note 一样在下游渲染中保留
_ALIGN_CLASSES = ("ptoe-align-left", "ptoe-align-center", "ptoe-align-right")

# 换页标记渲染为块级元素使用的 class（CSS 强制分页）
_PAGE_BREAK_CLASS = "ptoe-page-break"

# 插入图片的显示模式 class（全画幅 / 局部），随 <p> 块与 <img> 一起保留
_IMG_CLASSES = ("ptoe-img-full", "ptoe-img-fit")


def _block_class_html(attrs: str) -> str:
    """从块标签属性中提取应保留的 class（ptoe-note + 对齐类 + 换页 + 图片模式），返回 class 属性。"""
    m = re.search(r'class="([^"]*)"', attrs)
    if not m:
        return ""
    keep = [
        c
        for c in m.group(1).split()
        if c == _NOTE_CLASS or c in _ALIGN_CLASSES or c == _PAGE_BREAK_CLASS or c in _IMG_CLASSES
    ]
    return f' class="{" ".join(keep)}"' if keep else ""


def _block_classes(attrs: List[Tuple[str, Optional[str]]]) -> List[str]:
    """块级标签应保留的 class 列表（ptoe-note + 对齐类 + 换页 + 图片模式）。"""
    keep: List[str] = []
    for k, v in attrs:
        if k == "class":
            for c in (v or "").split():
                if c == _NOTE_CLASS or c in _ALIGN_CLASSES or c == _PAGE_BREAK_CLASS or c in _IMG_CLASSES:
                    keep.append(c)
    return keep


def _normalize_note(text: str) -> str:
    """注释文本规范：ASCII 半角括号统一为中文全角括号（（ ））。"""
    return text.replace("(", "（").replace(")", "）")


def _note_already_parenthesized(text: str) -> bool:
    """注释是否已带括号（视为「已在正文中」）：剥掉行内标签后首尾为中文括号。"""
    t = re.sub(r"<[^>]+>", "", text).strip()
    return t.startswith("（") and t.endswith("）")

# 令牌化：优先把「完整的标记 span 元素」作为一个令牌，其余按单个标签切分。
# 这样块内切段时能直接拿到标记的类型与文本，不必在三个令牌（开标签/文本/闭标签）间拼凑。
_TOKEN_RE = re.compile(
    r'(<span\s+data-ptoe-marker="[^"]*"[^>]*>.*?</span>|<[^>]+>)',
    flags=re.IGNORECASE | re.DOTALL,
)


def _is_note_block(attrs: List[Tuple[str, Optional[str]]]) -> bool:
    """判断块级标签是否带 class="ptoe-note"（注释格式）。"""
    for k, v in attrs:
        if k == "class" and _NOTE_CLASS in (v or "").split():
            return True
    return False


class _Sanitizer(HTMLParser):
    """把矫正界面提交的 HTML 清洗为仅含白名单标签的规范片段。

    输出只含 <p>/<h1-6>/<strong>/<em>/<br/>、<img> 与标记 span
    （<span data-ptoe-marker="...">），无其他属性，供下游安全渲染。
    <img> 仅保留 src（data URI / 本地相对路径）、alt 与显示模式 class
    （ptoe-img-full / ptoe-img-fit）。
    <div> 归一化为 <p>，<b>/<i> 归一化为 <strong>/<em>；其余标签整体丢弃
    仅保留文本；script/style 等非内容标签连同内容一起丢弃。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[str] = []
        self.buf: List[str] = []
        self.stack: List[str] = []  # 未闭合的行内标签（strong/em/span）
        self.block: Optional[Tuple[str, int]] = None  # ('p', 0) | ('h', level)
        self.classes: List[str] = []  # 当前块保留的 class（ptoe-note + 对齐类）
        self.skip: int = 0  # 非内容标签嵌套深度

    def _flush(self) -> None:
        if self.block is None:
            return
        closes = "".join(f"</{t}>" for t in reversed(self.stack))
        content = "".join(self.buf) + closes
        if content.strip():
            cls = f' class="{" ".join(self.classes)}"' if self.classes else ""
            if self.block[0] == "p":
                self.blocks.append(f"<p{cls}>{content}</p>")
            else:
                lv = self.block[1]
                self.blocks.append(f"<h{lv}{cls}>{content}</h{lv}>")
        self.buf = []
        self.stack = []
        self.block = None
        self.classes = []

    def _open_block(self, kind: str, level: int = 0, classes: Optional[List[str]] = None) -> None:
        self._flush()
        self.block = (kind, level)
        self.classes = list(classes) if classes else []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("b", "strong"):
            self.buf.append("<strong>")
            self.stack.append("strong")
        elif tag in ("i", "em"):
            self.buf.append("<em>")
            self.stack.append("em")
        elif tag == "br":
            self.buf.append("<br/>")
        elif tag in ("p", "div"):
            self._open_block("p", classes=_block_classes(attrs))
        elif _BLOCK_RE.fullmatch(tag):
            self._open_block("h", int(tag[1]), classes=_block_classes(attrs))
        elif tag == "span":
            attrs_d = dict(attrs)
            val = attrs_d.get("data-ptoe-marker")
            if val and _MARKER_RE.fullmatch(val):
                # 保留标记 span：data-ptoe-marker + ptoe-marker 显示类。
                # class 随载荷落盘，任意渲染路径（/api/pages、历史载入等）
                # 无需依赖 serve 时补回；旧版本历史仍由 _ensure_marker_classes 兜底。
                v = _html.escape(val, quote=True)
                cls = (
                    "ptoe-marker"
                    if "ptoe-marker" in (attrs_d.get("class") or "").split()
                    else ""
                )
                cls_html = f' class="{cls}"' if cls else ""
                self.buf.append(f'<span data-ptoe-marker="{v}"{cls_html}>')
                self.stack.append("span")
        elif tag == "img":
            # 插入的图片：仅保留 src（data URI 或相对路径）、alt 与显示模式 class
            attrs_d = dict(attrs)
            src = attrs_d.get("src") or ""
            if src:
                cls = [
                    c for c in (attrs_d.get("class") or "").split() if c in _IMG_CLASSES
                ]
                alt = (attrs_d.get("alt") or "插图")[:100]
                cls_html = f' class="{" ".join(cls)}"' if cls else ""
                self.buf.append(
                    f'<img src="{_html.escape(src, quote=True)}" alt="{_html.escape(alt, quote=True)}"{cls_html}/>'
                )
        # 其余标签（a/...）丢弃，仅保留其文本内容

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag == "br":
            self.buf.append("<br/>")
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if tag in ("strong", "b") and "strong" in self.stack:
            self.buf.append("</strong>")
            self.stack.pop()
        elif tag in ("em", "i") and "em" in self.stack:
            self.buf.append("</em>")
            self.stack.pop()
        elif tag == "span" and self.stack and self.stack[-1] == "span":
            self.buf.append("</span>")
            self.stack.pop()
        elif tag in ("p", "div") and self.block is not None and self.block[0] == "p":
            self._flush()
        elif (
            _BLOCK_RE.fullmatch(tag)
            and self.block is not None
            and self.block[0] == "h"
            and self.block[1] == int(tag[1])
        ):
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        self.buf.append(_html.escape(data, quote=False))

    def result(self) -> str:
        self._flush()
        if self.buf:
            content = "".join(self.buf)
            if content.strip():
                self.blocks.append(f"<p>{content}</p>")
        return "".join(self.blocks)
def _ensure_marker_classes(html: str) -> str:
    """Ensure marker spans have the frontend marker class (ptoe-marker).

    sanitize_html keeps data-ptoe-marker and (since 2026-08) the ptoe-marker
    class for new saves; older history payloads may only carry the data
    attribute. The UI applies visual styling to elements with class
    `ptoe-marker`. When serving stored pages back to the browser we add the
    class back to any marker spans so they render highlighted without
    mutating the on-disk history payload.
    """
    if not html or 'data-ptoe-marker' not in html:
        return html
    def _repl(m: re.Match) -> str:
        attrs = m.group(1) or ""
        # find existing class attr
        cls_m = re.search(r'class="([^"]*)"', attrs)
        if cls_m:
            classes = cls_m.group(1).split()
            if 'ptoe-marker' in classes:
                return m.group(0)
            # insert ptoe-marker into existing class list
            new_cls = cls_m.group(1) + ' ptoe-marker'
            attrs2 = attrs[: cls_m.start()] + f'class="{new_cls}"' + attrs[cls_m.end():]
            return f'<span{attrs2}>'
        else:
            # no class present: add class attribute
            return f'<span{attrs} class="ptoe-marker">'
    return re.sub(r'<span(.*?)>', _repl, html, flags=re.IGNORECASE | re.DOTALL)
def sanitize_html(raw: str) -> str:
    """清洗界面提交的 HTML：只放行白名单标签（含标记 span）并保证结构平衡。"""
    s = _Sanitizer()
    try:
        s.feed(raw)
    except Exception:
        # 解析异常时退化为纯文本（不丢内容、不产生非法标签）
        return _html.escape(raw, quote=False)
    return s.result()


# 句末标点：以这些结尾的段落视为「完整段落」，不与下一段合并
# （；、：、，等为句中停顿，OCR 拆段时应当合并，故不在此列；
#   闭合括号/书名号（），】」』》〉）结尾同样视为完整段落）
_SENT_END_PUNCT = "。！？!?…）)】」』》〉"
# 段首需清理的装饰符号（OCR 常在段首添加）
_LEADING_SYMBOL_RE = re.compile(r"^[#*＊+•·▪]+\s*")
# OCR/文本残留的 Markdown 加粗符号 **（可能多处，含句中）
_MD_BOLD_RE = re.compile(r"\*{2,}")
# 中英文标点归一：与汉字相邻的半角标点转全角；与字母/数字相邻的全角标点转半角
_CJK_RANGE = "\u4e00-\u9fff\u3400-\u4dbf"
_LATIN_RANGE = "A-Za-z0-9"
_HALF_TO_FULL = ((",", "，"), (";", "；"), (":", "："), ("?", "？"), ("!", "！"), ("(", "（"), (")", "）"))
_FULL_TO_HALF = (("，", ","), ("；", ";"), ("：", ":"), ("？", "?"), ("！", "!"), ("（", "("), ("）", ")"))
# 清理时剥掉的非白名单标签（保留 p/h1-6/strong/em/b/i/br/span/img，
# b/i 留给 sanitize 归一化为 strong/em；其余剥掉但保留内容）
_STRIP_TAG_RE = re.compile(
    r"</?(?!p\b|h[1-6]\b|strong\b|em\b|b\b|i\b|br\b|span\b|img\b)[a-zA-Z][^>]*>"
)


def _normalize_punctuation(text: str) -> str:
    """中英文标点归一化。

    汉字相邻处的半角 ,;:?!() 转全角（中文排版）；字母/数字相邻处的全角
    标点转半角（英文/数字上下文）。避开小数点/网址等被误伤的常见情况。
    """
    s = str(text)
    for half, full in _HALF_TO_FULL:
        # 半角 → 全角：左邻或右邻是汉字（覆盖 汉字,  /  ,汉字 两种）
        s = re.sub(rf"(?<=[{_CJK_RANGE}]){re.escape(half)}", full, s)
        s = re.sub(rf"{re.escape(half)}(?=[{_CJK_RANGE}])", full, s)
    for full, half in _FULL_TO_HALF:
        # 全角 → 半角：左右都是字母/数字（纯英文语境）
        s = re.sub(rf"(?<=[{_LATIN_RANGE}]){re.escape(full)}(?=[{_LATIN_RANGE}])", half, s)
    return s


def _strip_leading_symbols(text: str) -> str:
    """段首符号清理：去掉 OCR 常在段首添加的 #、*、• 等装饰符号。"""
    return _LEADING_SYMBOL_RE.sub("", text)


def _block_text(toks: List[str]) -> str:
    """块内纯文本（跳过标签 token）。"""
    return "".join(t for t in toks if not t.startswith("<"))


def _block_is_plain(toks: List[str]) -> bool:
    """块是否「普通段落」：不含任何行内标签 token（strong/em/span/img/br 等）。

    已设标题（h1-6）或带行内格式/标记/图片的段落视为有结构意图，
    不与附近段落合并。
    """
    return all(not t.startswith("<") for t in toks)


def _close_of(open_tag: str) -> str:
    """由块开标签推导闭合标签（<p ...> → </p>，<h2 ...> → </h2>）。"""
    if open_tag.startswith("<p"):
        return "</p>"
    return "</h" + open_tag[2] + ">"


def _clean_blocks(
    html: str,
    *,
    merge_paragraphs: bool,
    strip_leading_symbols: bool,
    normalize_punctuation: bool,
) -> str:
    """按块级标签切分已清洗的 HTML，逐块做文本处理并可选合并相邻段落。

    合并启发式（保守，避免把真正的段落粘在一起）：仅合并两个无 class 的
    普通 <p> 块，且前块不以句末标点（。！？…；）结尾、前后块均非空。
    <h1-6> 与带 class 的 <p>（注释/对齐/图片）视为有结构意图，不合并。
    """
    tokens = re.split(r"(<[^>]+>)", html)
    blocks: List[Tuple[str, List[str]]] = []  # (开标签或 "<p>" 兜底, 块内 tokens)
    cur_open: Optional[str] = None
    cur: List[str] = []
    for tok in tokens:
        if not tok:
            continue
        if re.fullmatch(r"</?(p|h[1-6])([^>]*)>", tok, flags=re.I):
            if cur or cur_open is not None:
                blocks.append((cur_open or "<p>", cur))
            cur_open = None
            cur = []
            if not tok.startswith("</"):
                cur_open = tok
            continue
        cur.append(tok)
    if cur or cur_open is not None:
        blocks.append((cur_open or "<p>", cur))

    # 块内文本处理（段首符号只作用于块开头第一个文本 token）
    for open_tag, toks in blocks:
        first = True
        for i, t in enumerate(toks):
            if not t or t.startswith("<"):
                continue
            t2 = t
            if first and strip_leading_symbols:
                t2 = _strip_leading_symbols(t2)
                first = False
            if normalize_punctuation:
                t2 = _normalize_punctuation(t2)
            # OCR/文本残留的 ** 加粗符号（全 token 处理，含句中）
            t2 = _MD_BOLD_RE.sub("", t2)
            toks[i] = t2

    # 相邻普通 <p> 段落合并（OCR 拆段修复）
    # 仅合并两个「普通段落」：无 class 的 <p> 且块内无任何行内标签
    # （已设标题/格式/标记/图片的段落保留原结构，不与附近段落合并）
    if merge_paragraphs:
        merged: List[Tuple[str, List[str]]] = []
        for open_tag, toks in blocks:
            if (
                merged
                and open_tag == "<p>"
                and merged[-1][0] == "<p>"
                and _block_is_plain(merged[-1][1])
                and _block_is_plain(toks)
                and (not merged[-1][1] or not toks)
            ):
                # 任一为空：直接并入（空段不保留）
                merged[-1][1].extend(toks)
                continue
            if (
                merged
                and open_tag == "<p>"
                and merged[-1][0] == "<p>"
                and _block_is_plain(merged[-1][1])
                and _block_is_plain(toks)
            ):
                prev_txt = _block_text(merged[-1][1]).strip()
                next_txt = _block_text(toks).strip()
                if (
                    prev_txt
                    and next_txt
                    and prev_txt[-1] not in _SENT_END_PUNCT
                ):
                    # 英文/数字相接处补一个空格（CJK 之间不加，避免中文被拆开）
                    gap = ""
                    if prev_txt[-1:] and next_txt[:1]:
                        # isalnum 对 CJK 也为真，必须限定 ASCII 才补空格
                        if (
                            prev_txt[-1].isascii()
                            and prev_txt[-1].isalnum()
                            and next_txt[0].isascii()
                            and next_txt[0].isalnum()
                        ):
                            gap = " "
                    merged[-1][1].append(gap)
                    merged[-1][1].extend(toks)
                    continue
            merged.append((open_tag, toks))
        blocks = merged

    # 重建（丢弃空块）
    parts: List[str] = []
    for open_tag, toks in blocks:
        inner = "".join(toks).strip()
        if not inner:
            continue
        parts.append(f"{open_tag}{inner}{_close_of(open_tag)}")
    return "\n".join(parts)


def clean_page_html(
    raw: str,
    *,
    merge_paragraphs: bool = True,
    strip_leading_symbols: bool = True,
    normalize_punctuation: bool = True,
    strip_tags: bool = True,
) -> str:
    """矫正界面的智能清理入口：合并被 OCR 拆散的段落、清理段首 #/* 等符号、
    归一化中英文标点、移除残留的 HTML 标签，返回 sanitize 后的规范 HTML。

    幂等：已清理的内容再次清理结果不变（不会把完整段落拆开）。
    """
    text = str(raw or "")
    if strip_tags:
        text = _STRIP_TAG_RE.sub("", text)
    html = sanitize_html(text)
    return _clean_blocks(
        html,
        merge_paragraphs=merge_paragraphs,
        strip_leading_symbols=strip_leading_symbols,
        normalize_punctuation=normalize_punctuation,
    )


def _page_text(raw: str) -> str:
    """矫正界面初始内容：普通 OCR 文本按行转 <div>；已清洗的 HTML（含标记）原样返回。

    保存/暂存/历史缓存里存的是 sanitize_html 后的片段（如 <p>…<span
    data-ptoe-marker=...>…</p>），刷新或历史预加载时不能再走 initial_html
    （会把标签转义成可见文本）。
    """
    if re.search(r"</?(?:p|div|h[1-6]|span)([^>]*)>", raw, flags=re.IGNORECASE):
        return raw
    return initial_html(raw)


def initial_html(text: str) -> str:
    """把一页 OCR 文本转成界面初始 HTML：每行一个 <div>。

    HTML 会把文本节点里的换行折叠成空格（导致“内容拥挤到一整段”），
    所以必须按行生成块级元素，才能在编辑区保留原始段落/行结构。
    清洗器会把 <div> 归一化为 <p>，保证往返（保存→清洗）不丢结构。
    """
    out = []
    for line in str(text).split("\n"):
        line = line.strip()
        if line:
            out.append(f"<div>{_html.escape(line, quote=False)}</div>")
    return "".join(out)


# ---------------------------------------------------------------------------
# 标记 → 文章结构
# ---------------------------------------------------------------------------


def _split_segments(
    html: str,
) -> Tuple[List[Tuple[str, List[Tuple[str, str]]]], List[Tuple[str, str]]]:
    """把块内 html 按标记 span 切分为内容段，返回 (segments, trailing)。

    - segments: [(内容 html, 段首标记列表), ...]，标记作用于紧随其后的内容段；
    - trailing: 最后一段内容之后的标记（段尾标记），作用于后续块的内容。
    段内行内标签（strong/em）在标记处保持闭合平衡：标记切在行内标签内部时，
    前段自动补闭合、后段重新打开，保证每段都是合法片段。
    """
    segments: List[Tuple[str, List[Tuple[str, str]]]] = []
    trailing: List[Tuple[str, str]] = []
    buf: List[str] = []
    stack: List[str] = []  # 当前段未闭合的行内标签（strong/em）
    pending: List[Tuple[str, str]] = []  # 段首标记（位于当前内容之前）

    def flush(reopen: bool = False) -> List[str]:
        """输出当前内容段；reopen=True 时返回需在下一段重新打开的行内标签。"""
        if not (buf or stack):
            return []
        content = "".join(buf) + "".join(f"</{t}>" for t in reversed(stack))
        # 无可见文本（如连续标记之间的空行内标签）不产出段，标记继续累积
        if re.sub(r"<[^>]+>", "", content).strip():
            segments.append((content.strip(), list(pending)))
            pending.clear()
        saved = list(stack) if reopen else []
        buf.clear()
        stack.clear()
        return saved

    for tok in _TOKEN_RE.split(html):
        if not tok:
            continue
        m = _MARKER_SPAN_RE.fullmatch(tok)
        if m:
            for t in flush(reopen=True):
                buf.append(f"<{t}>")
                stack.append(t)
            pending.append((m.group(1), m.group(2).strip()))
            continue
        m = re.fullmatch(r"</?(?:strong|em)>", tok, flags=re.IGNORECASE)
        if m:
            if tok.startswith("</"):
                if stack:
                    stack.pop()
            else:
                stack.append(tok[1:-1].lower())
            buf.append(tok)
            continue
        buf.append(tok)  # 文本 / <br/> / 其他（清洗后只可能是合法内容）
    flush()
    trailing = pending
    return segments, trailing


def apply_markers(pages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """提取矫正文本中的章节/段落标记，生成文章结构。

    输入：校正后的 pages [{'page', 'text'}]（text 为 sanitize_html 白名单 HTML）。
    返回：[{'text': article_html}, ...]，按全文标记拆分文章。标记的语义由
    所在位置决定（段首=块内内容之前的标记，段尾=块内最后一段内容之后的标记）：
    - 段落标记（join）：段首 → 本段与上一段合并为一个 <p>；
      段尾 → 本段与下一段合并为一个 <p>（OCR 把一整段拆成几段时，在断口处
      放一个段落标记即可拼回整段；标记在段中同样按该语义在标记处拼接）；
    - 章节标记（chapter:N）：段尾 → 后续内容前插入 <h2>标签文本</h2>；
      段首 → 本段内容前插入 <h2>（旧数据兼容，界面已移除章节标记）；
    - 全文标记（full）：段尾 → 后续内容属于新文章（新的一页）；
      段首 → 本段内容属于新文章；
    - 换页标记（page）：从该标记之后的内容显示在新的一页（同一文章内强制
      分页，不拆文章；输出 `<p class="ptoe-page-break">`，CSS page-break-before
      实现）；段中/段首/段尾位置均按「标记之后的内容换页」处理；
    - 注释（class="ptoe-note" 的段落）：作为注释内容按文档顺序编号；正文中
      的注释标记（note）所在位置由对应注释替换（一个注释段落对应一个标记，
      数量不匹配时抛 ValueError 提示）；注释同样支持段落标记合并。
    没有标记时返回单篇文章（等价于把所有 <p> 顺序拼接）。
    """
    # 1) 全书按页解析为块流（跨页连续处理，段落标记可跨页合并）
    blocks: List[Dict[str, Any]] = []
    for p in sorted(pages, key=lambda x: x["page"]):
        text = p.get("text") or ""
        kind: Optional[str] = None
        note = False  # 当前块是否为注释（class="ptoe-note"）
        attrs = ""  # 当前块保留的 class 属性（对齐等）
        cur: List[str] = []
        for tok in _TOKEN_RE.split(text):
            if not tok:
                continue
            m = _BLOCK_TAG_RE.fullmatch(tok)
            if m:
                if kind:
                    blocks.append(
                        {"kind": kind, "html": "".join(cur), "note": note, "attrs": attrs}
                    )
                    cur = []
                    kind = None
                    note = False
                    attrs = ""
                if not tok.startswith("</"):
                    kind = m.group(1).lower()
                    tag_attrs = m.group(2) or ""
                    note = _NOTE_CLASS in tag_attrs
                    attrs = _block_class_html(tag_attrs)
                continue
            cur.append(tok)
        if kind or cur:
            blocks.append(
                {"kind": kind or "p", "html": "".join(cur), "note": note, "attrs": attrs}
            )

    # 2) 块内按标记切段；3) 收集注释段落；4) 按标记重排
    parsed: List[Dict[str, Any]] = []
    for b in blocks:
        segments, trailing = _split_segments(b["html"])
        parsed.append(
            {
                "kind": b["kind"],
                "note": b.get("note"),
                "attrs": b.get("attrs", ""),
                "segments": segments,
                "trailing": trailing,
            }
        )

    # 注释段落（class="ptoe-note"）按文档顺序收集；段落标记（join）合并相邻注释
    annotations: List[str] = []
    merge_next = False
    for item in parsed:
        if item["note"]:
            text = "".join(h for h, _m in item["segments"]).strip()
            first_join = any(t == "join" for h, ms in item["segments"][:1] for t, _l in ms)
            if text:
                if (merge_next or first_join) and annotations:
                    annotations[-1] += text
                else:
                    annotations.append(text)
            merge_next = any(t == "join" for t, _l in item["trailing"])
        else:
            merge_next = False

    # 注释标记计数：正文中每个 note 标记对应一个注释段落，须一一匹配
    note_markers = sum(
        1
        for item in parsed
        if not item["note"]
        for _h, markers in item["segments"]
        for t, _l in markers
        if t == "note"
    ) + sum(
        1
        for item in parsed
        if not item["note"]
        for t, _l in item["trailing"]
        if t == "note"
    )
    if note_markers != len(annotations):
        raise ValueError(
            f"注释标记与注释数量不匹配：正文注释标记 {note_markers} 个，"
            f"注释段落 {len(annotations)} 个（一个注释段落对应一个注释标记）"
        )

    def render_block(kind: str, html: str, attrs: str = "") -> str:
        return f"<{kind}{attrs}>{html}</{kind}>"

    articles: List[str] = []
    cur_article: List[str] = []
    deferred_full = False
    deferred_chapter: Optional[str] = None
    deferred_join = False
    deferred_page = False

    def flush_article() -> None:
        nonlocal cur_article
        if cur_article:
            articles.append({"text": "".join(cur_article)})
            cur_article = []

    def push_content(kind: str, html: str, attrs: str = "") -> None:
        """把一段内容渲染进当前文章：先应用全文/章节/换页/段落合并标记。"""
        nonlocal deferred_full, deferred_chapter, deferred_join, deferred_page
        if deferred_full:
            flush_article()
            deferred_full = False
        elif deferred_chapter is not None:
            cur_article.append(f"<h2>{deferred_chapter}</h2>")
            deferred_chapter = None
        if deferred_page:
            # 换页标记：从该位置之后的内容显示在新的一页（同一文章内强制分页，
            # 不拆文章；CSS 用 page-break-before 实现）
            cur_article.append('<p class="ptoe-page-break"> </p>')
            deferred_page = False
        if deferred_join:
            # 只合并 段落+段落；目标块必须是当前文章最后的 <p>（可带 class）
            if (
                kind == "p"
                and cur_article
                and cur_article[-1].startswith("<p")
                and cur_article[-1].endswith("</p>")
            ):
                cur_article[-1] = cur_article[-1][: -len("</p>")] + html + "</p>"
            else:
                cur_article.append(render_block(kind, html, attrs))
            deferred_join = False
        else:
            cur_article.append(render_block(kind, html, attrs))

    def defer(markers: List[Tuple[str, str]]) -> None:
        nonlocal deferred_full, deferred_chapter, deferred_join, deferred_page
        for mtype, label in markers:
            if mtype == "full":
                deferred_full = True
                deferred_chapter = None
            elif mtype == "page":
                deferred_page = True
            elif mtype == "join":
                deferred_join = True
            elif mtype.startswith("chapter:"):
                deferred_chapter = label or f"第{mtype.split(':')[1]}章节"

    def note_spans(anns: List[str]) -> str:
        """把注释文本渲染为行内注释 span（CSS 控制小字号）。

        插入正文的注释用中文括号（ ）括起，内部括号统一为中文括号；
        注释本身已带括号（视为已在正文中）时只改字号，不再重复加括号。
        """
        parts = []
        for a in anns:
            norm = _normalize_note(a)
            if _note_already_parenthesized(norm):
                parts.append(f'<span class="{_NOTE_CLASS}">{norm}</span>')
            else:
                parts.append(f'<span class="{_NOTE_CLASS}">（{norm}）</span>')
        return "".join(parts)

    note_idx = 0
    pending_notes: List[str] = []  # 段尾注释在无可依附段落时顺延到下一段内容
    for item in parsed:
        if item["note"]:
            continue
        for html, markers in item["segments"]:
            seg_notes: List[str] = []
            rest: List[Tuple[str, str]] = []
            for t, label in markers:
                if t == "note":
                    seg_notes.append(annotations[note_idx])
                    note_idx += 1
                else:
                    rest.append((t, label))
            defer(rest)
            if html:
                if pending_notes:
                    html = note_spans(pending_notes) + html
                    pending_notes = []
                if seg_notes:
                    # 注释标记不打断段落：本段并入上一段，注释插在标记处
                    deferred_join = True
                push_content(item["kind"], note_spans(seg_notes) + html, item.get("attrs", ""))
            else:
                pending_notes.extend(seg_notes)
        tail_notes: List[str] = []
        rest_tail: List[Tuple[str, str]] = []
        for t, label in item["trailing"]:
            if t == "note":
                tail_notes.append(annotations[note_idx])
                note_idx += 1
            else:
                rest_tail.append((t, label))
        defer(rest_tail)
        if tail_notes:
            spans = note_spans(tail_notes)
            if (
                cur_article
                and cur_article[-1].startswith("<p")
                and cur_article[-1].endswith("</p>")
            ):
                cur_article[-1] = cur_article[-1][: -len("</p>")] + spans + "</p>"
            else:
                pending_notes.extend(tail_notes)
    if pending_notes:
        # 极少数：注释标记悬空（其后没有内容段），补到当前文章末尾
        if (
            cur_article
            and cur_article[-1].startswith("<p")
            and cur_article[-1].endswith("</p>")
        ):
            cur_article[-1] = cur_article[-1][: -len("</p>")] + note_spans(pending_notes) + "</p>"
        else:
            cur_article.append(f"<p>{note_spans(pending_notes)}</p>")
    if deferred_chapter is not None:
        cur_article.append(f"<h2>{deferred_chapter}</h2>")
    flush_article()
    return articles


# ---------------------------------------------------------------------------
# 图片服务
# ---------------------------------------------------------------------------

_PREVIEW_CACHE_MAX = 200  # 预览 JPEG LRU 缓存上限（页）。1024 页全尺寸 JPEG
# 常驻会占 ~100-300MB 内存；200 页 + 虚拟列表视口足够，超出淘汰最久未用。

# 原图模式的高清回退渲染 DPI（无分割图片时用 PDF 直接渲染，保证原图可看）
_FULL_DPI = 220.0


def _preview_doc(state: Dict[str, Any]):
    """返回服务生命周期内复用的 fitz.Document（首次打开后缓存）。

    P2：避免每页预览都重新 `fitz.open(pdf_path)`（打开+解析 PDF 开销大）。
    线程安全：双检锁保证只开一次；correct_pages 结束时统一 close。
    返回 None 表示 PDF 不可用。
    """
    doc = state.get("preview_doc")
    if doc is not None:
        return doc
    lock = state.get("preview_doc_lock")
    if lock is None:
        return None
    with lock:
        doc = state.get("preview_doc")
        if doc is not None:
            return doc
        pdf_path = state.get("pdf_path")
        if not pdf_path:
            return None
        try:
            import fitz

            doc = fitz.open(pdf_path)
            state["preview_doc"] = doc
        except Exception:
            doc = None
            state["preview_doc"] = None
        return doc


def _render_jpeg(
    state: Dict[str, Any], page_no: int, dpi: float
) -> Optional[Tuple[str, bytes]]:
    """用复用的 fitz.Document 把指定页渲染为 JPEG bytes（持 preview_doc_lock）。

    供预览（低 DPI）与原图回退（高 DPI）共用；PDF 不可用或渲染失败返回 None。
    """
    doc = _preview_doc(state)
    if doc is None:
        return None
    try:
        import fitz

        lock = state.get("preview_doc_lock")
        if lock is not None:
            # fitz.Document 非线程安全：渲染放锁内
            with lock:
                if 1 <= page_no <= doc.page_count:
                    pix = doc[page_no - 1].get_pixmap(
                        matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0)
                    )
                    return (
                        "image/jpeg",
                        pix.tobytes(
                            "jpeg", jpg_quality=int(state.get("preview_quality", 82))
                        ),
                    )
        else:
            if 1 <= page_no <= doc.page_count:
                pix = doc[page_no - 1].get_pixmap(
                    matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0)
                )
                return (
                    "image/jpeg",
                    pix.tobytes(
                        "jpeg", jpg_quality=int(state.get("preview_quality", 82))
                    ),
                )
    except Exception:
        pass
    return None


def _preview_bytes(state: Dict[str, Any], page_no: int) -> Optional[Tuple[str, bytes]]:
    """返回 (content_type, bytes)。优先从 PDF 低 DPI 渲染 JPEG（惰性 + LRU 缓存），
    失败则回退到原始页面图片文件。"""
    cache = state["preview_cache"]
    cached = cache.get(page_no)
    if cached is not None:
        if hasattr(cache, "move_to_end"):
            cache.move_to_end(page_no)  # LRU：命中刷新到队尾
        return cached
    data = _render_jpeg(state, page_no, float(state.get("preview_dpi", 110)))
    if data is None:
        data = _full_bytes(state, page_no)
    if data is not None:
        cache[page_no] = data
        if hasattr(cache, "popitem"):
            # LRU：超过上限时淘汰最久未用（队首）
            while len(cache) > _PREVIEW_CACHE_MAX:
                cache.popitem(last=False)
        elif len(cache) > _PREVIEW_CACHE_MAX:
            # 兼容测试手工构造的普通 dict
            cache.clear()
    return data


def _full_bytes(state: Dict[str, Any], page_no: int) -> Optional[Tuple[str, bytes]]:
    """返回原始页面图片文件（用于点击查看原图）。

    `correct <pdf>` 直接命令没有 img_dir（未切图），或分割图片缺失时，
    回退到 PDF 高 DPI 渲染，保证原图模式始终有图可看。
    """
    img_dir = state.get("img_dir")
    if img_dir:
        for ext, ctype in (("png", "image/png"), ("jpg", "image/jpeg"), ("jpeg", "image/jpeg")):
            fp = Path(img_dir) / f"{page_no}.{ext}"
            if fp.is_file():
                return (ctype, fp.read_bytes())
    return _render_jpeg(state, page_no, _FULL_DPI)


# ---------------------------------------------------------------------------
# 浏览器存活监测
#   页面每 30s 发一次心跳（/api/heartbeat），关闭标签页时发 pagehide 信标
#   （/api/gone，sendBeacon）。correct_pages 的等待循环据此判断浏览器是否已
#   关闭：超过 idle_timeout 秒后自动继续后续流程（保留已保存内容）。
# ---------------------------------------------------------------------------

# 心跳失联场景（未收到信标，如浏览器被强杀/崩溃）需连续失联这么久才判定，
# 避免电脑休眠唤醒后短暂失联导致误判。
_STALE_CONFIRM_SECONDS = 3.0


_HISTORY_DIR_NAME = "correction_history"
_HISTORY_KEEP = 20  # 每个文件保留的最新版本数

# P3：历史列表轻量索引缓存。_history_entries 不再每次全量 json.loads 所有
# 版本文件，而是先算目录签名（文件名+mtime+size），签名未变则复用缓存。
_HISTORY_INDEX: Dict[str, Any] = {"sig": None, "items": None}


def _history_dir() -> Path:
    """历史缓存目录：data/correction_history/。"""
    return Path(__file__).resolve().parent / "data" / _HISTORY_DIR_NAME


def _history_prefix(pdf_path: Optional[str]) -> Optional[str]:
    """同一 PDF 的版本文件名前缀：按 PDF 绝对路径哈希（同名不同路径互不干扰）。"""
    if not pdf_path:
        return None
    import hashlib

    return hashlib.sha1(
        str(Path(pdf_path).resolve()).encode("utf-8", errors="replace")
    ).hexdigest()[:16]


def _history_entries(prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出历史缓存条目（新→旧）：{id, pdf, name, path, updated, pages}。

    P3：轻量索引——先比对目录签名（文件名+mtime+size），文件未变化时
    直接复用上次解析结果，避免 /api/history 每次全量 json.loads 所有版本文件。
    """
    d = _history_dir()
    if not d.is_dir():
        return []
    fps = sorted(d.glob("*.json"), reverse=True)
    try:
        sig = "|".join(
            f"{fp.name}:{fp.stat().st_mtime_ns}:{fp.stat().st_size}" for fp in fps
        )
    except OSError:
        sig = "|".join(fp.name for fp in fps)
    if _HISTORY_INDEX.get("sig") != sig:
        items: List[Dict[str, Any]] = []
        for fp in fps:
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                pdf = str(data.get("pdf") or "")
                items.append(
                    {
                        "id": fp.stem,
                        "pdf": pdf,
                        "name": str(data.get("name") or Path(pdf).name or fp.stem),
                        "path": pdf,
                        "updated": str(data.get("updated") or ""),
                        "pages": len(data.get("pages") or {}),
                    }
                )
            except Exception:
                continue
        _HISTORY_INDEX["sig"] = sig
        _HISTORY_INDEX["items"] = items
    if prefix:
        return [it for it in _HISTORY_INDEX["items"] if it["id"].startswith(prefix)]
    return list(_HISTORY_INDEX["items"])


def _load_latest_history(pdf_path: Optional[str]) -> Dict[str, str]:
    """同一 PDF 最新版本的矫正内容（兼容旧版单文件格式 <prefix>.json）。"""
    prefix = _history_prefix(pdf_path)
    if not prefix:
        return {}
    fps = sorted(_history_dir().glob(f"{prefix}_*.json"), reverse=True)
    if not fps:
        legacy = _history_dir() / f"{prefix}.json"
        if legacy.is_file():
            fps = [legacy]
    if not fps:
        return {}
    try:
        data = json.loads(fps[0].read_text(encoding="utf-8"))
        return dict(data.get("pages") or {})
    except Exception:
        return {}


def _load_history_version(version_id: str) -> Optional[Dict[str, Any]]:
    """按历史条目 id（文件名 stem）读取某一版本的矫正内容。

    返回 {"pages": {str(page): html}, "pdf": str|None}；文件不存在或损坏返回 None。
    pdf 为该版本暂存时所属 PDF 的路径（打开版本时切换预览图来源，保证图与文对应）。
    """
    d = _history_dir()
    fp = d / f"{version_id}.json"
    if not fp.is_file():
        return None
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        return {
            "pages": dict(data.get("pages") or {}),
            "pdf": str(data.get("pdf") or "") or None,
        }
    except Exception:
        return None


def _history_pages_for_init(
    pdf_path: Optional[str],
    *,
    history: bool,
    preload_history: bool,
) -> Dict[str, str]:
    """启动矫正界面时的初始文本来源。

    preload_history=True 时返回同一 PDF 最新历史版本（覆盖传入文本）；
    False 时返回空 dict（完全使用传入的 pages —— 重新识别后的新文本优先）。
    """
    if history and preload_history:
        return _load_latest_history(pdf_path)
    return {}


def _write_history_version(state: Dict[str, Any]) -> bool:
    """把当前矫正内容作为新版本写入本地历史缓存（暂存/完成时用）。

    文件名 <prefix>_<时间戳>_<随机>。json；同名文件按时间戳保留最近
    _HISTORY_KEEP 个版本。下次对同一 PDF 运行 --correct/correct 时自动
    加载最新版本，支持对已修改内容再次矫正。保存动作不调用本函数，
    走 _overwrite_history 覆盖当前缓存。

    返回是否写入成功（S4）：失败返回 False 并打印原因，调用方（HTTP
    handler）据此向用户报错，不再静默吞掉导致“以为保存了实际没落盘”。
    """
    prefix = state.get("history_prefix")
    if not prefix:
        return True  # 未启用历史缓存：无需写入，视为成功
    try:
        d = _history_dir()
        d.mkdir(parents=True, exist_ok=True)
        payload = {
            "pdf": state.get("pdf_path"),
            # 无文件会话没有 pdf：用打开的历史记录名，缺省为「手动录入」
            "name": state.get("history_name")
            or Path(state.get("pdf_path") or "").name
            or "手动录入",
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pages": {str(k): v for k, v in state["pages"].items()},
        }
        with state["history_lock"]:
            stamp = time.strftime("%Y%m%d%H%M%S")
            fp = d / f"{prefix}_{stamp}_{uuid4().hex[:4]}.json"
            fp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _prune_history(prefix)
        return True
    except Exception as e:
        # S4：写入失败必须上报（磁盘错误/权限等），不能静默丢数据
        print(f"[correctmanage] 历史缓存写入失败: {e}")
        return False


def _overwrite_history(state: Dict[str, Any]) -> bool:
    """保存动作：不新建历史版本，直接覆盖当前（最新）历史版本文件。

    同一份内容反复保存只更新同一个文件，多版本列表不会被保存刷屏；
    无历史文件时（首次保存）按 _write_history_version 的规则新建一个。

    返回是否写入成功（S4），失败时调用方须向用户报错。
    """
    prefix = state.get("history_prefix")
    if not prefix:
        return True
    try:
        d = _history_dir()
        d.mkdir(parents=True, exist_ok=True)
        payload = {
            "pdf": state.get("pdf_path"),
            "name": state.get("history_name")
            or Path(state.get("pdf_path") or "").name
            or "手动录入",
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pages": {str(k): v for k, v in state["pages"].items()},
        }
        with state["history_lock"]:
            # S6：按 mtime 取最新版本覆盖（文件名时间戳同秒时不会覆盖错版本）
            latest = None
            for fp in d.glob(f"{prefix}_*.json"):
                try:
                    if latest is None or fp.stat().st_mtime_ns > latest.stat().st_mtime_ns:
                        latest = fp
                except OSError:
                    continue
            if latest is not None:
                fp = latest
            else:
                stamp = time.strftime("%Y%m%d%H%M%S")
                fp = d / f"{prefix}_{stamp}_{uuid4().hex[:4]}.json"
            fp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return True
    except Exception as e:
        print(f"[correctmanage] 历史缓存写入失败: {e}")
        return False


def _prune_history(prefix: str) -> None:
    """每个文件只保留最近 _HISTORY_KEEP 个版本，删掉更旧的。"""
    try:
        for fp in sorted(_history_dir().glob(f"{prefix}_*.json"), reverse=True)[_HISTORY_KEEP:]:
            fp.unlink(missing_ok=True)
    except Exception:
        pass


def _delete_history(ids: List[str], all_: bool = False) -> int:
    """删除历史缓存条目；all_=True 删除全部，否则按 id（文件名 stem）删除。"""
    d = _history_dir()
    if not d.is_dir():
        return 0
    deleted = 0
    try:
        for fp in d.glob("*.json"):
            if all_ or fp.stem in ids:
                fp.unlink(missing_ok=True)
                deleted += 1
    except Exception:
        pass
    return deleted


def _safe_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 导出 TXT / DOCX（工具栏「导出」，弹窗选择保存位置）
# ---------------------------------------------------------------------------

# TXT 用 utf-8-sig（带 BOM）：Windows 记事本按 BOM 识别编码，中文不乱码
_TXT_ENCODING = "utf-8-sig"

# DOCX 最小打包：Content_Types + _rels + word/document.xml（stdlib zipfile，
# 不引入 python-docx；标题用直接格式加粗加大 + outlineLvl 生成导航大纲）
_DOCX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_DOCX_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)
_DOCX_DOCUMENT_HEAD = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body>"
)
_DOCX_DOCUMENT_TAIL = (
    '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
    '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
    "</w:sectPr></w:body></w:document>"
)
# 标题级别 → 字号（半磅）；h4-h6 与正文拉开即可
_DOCX_HEADING_SZ = {1: 36, 2: 32, 3: 28, 4: 24, 5: 24, 6: 24}


def _html_to_export_blocks(html: str) -> List[Tuple[str, str]]:
    """已清洗 HTML → [(块类型, 文本)]，块类型为 'p' 或 'h1'..'h6'。

    导出 TXT/DOCX 用：剥掉全部标签（含标记 span），图片无文本被跳过，
    <br> 转为段内换行（文本中保留 \\n），HTML 实体还原。
    """

    class _Parser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.blocks: List[Tuple[str, str]] = []
            self.cur_kind: str = "p"
            self.cur: List[str] = []
            self.skip = 0  # >0 表示处于 script/style 等跳过区域

        def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
            if tag in _SKIP_TAGS:
                self.skip += 1
                return
            if tag == "p" or (len(tag) == 2 and tag[0] == "h" and tag[1] in "123456"):
                self._flush()
                self.cur_kind = "p" if tag == "p" else tag
                return
            if tag == "br":
                self.cur.append("\n")

        def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
            if tag == "br":
                self.cur.append("\n")

        def handle_endtag(self, tag: str) -> None:
            if tag in _SKIP_TAGS:
                self.skip = max(0, self.skip - 1)
                return
            if tag == "p" or (len(tag) == 2 and tag[0] == "h" and tag[1] in "123456"):
                self._flush()

        def handle_data(self, data: str) -> None:
            if not self.skip:
                self.cur.append(data)

        def _flush(self) -> None:
            txt = "".join(self.cur).strip()
            self.cur = []
            if txt:
                self.blocks.append((self.cur_kind, txt))
            self.cur_kind = "p"

    parser = _Parser()
    parser.feed(str(html or ""))
    parser.close()
    parser._flush()
    return parser.blocks


def _build_docx(blocks: List[Tuple[str, str]], path: str) -> None:
    """块列表 → 最小合法 .docx（zipfile 打包，无第三方依赖）。"""
    import zipfile

    parts: List[str] = [_DOCX_DOCUMENT_HEAD]
    for kind, text in blocks:
        xml_text = (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        # 段内换行（<br>）转 w:br
        xml_text = xml_text.replace(
            "\n", '</w:t><w:br/><w:t xml:space="preserve">'
        )
        if kind.startswith("h") and len(kind) == 2 and kind[1].isdigit():
            lvl = int(kind[1])
            sz = _DOCX_HEADING_SZ.get(lvl, 24)
            parts.append(
                f'<w:p><w:pPr><w:outlineLvl w:val="{lvl - 1}"/>'
                f'<w:rPr><w:b/><w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/></w:rPr></w:pPr>'
                f'<w:r><w:rPr><w:b/><w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/></w:rPr>'
                f'<w:t xml:space="preserve">{xml_text}</w:t></w:r></w:p>'
            )
        else:
            parts.append(
                f'<w:p><w:r><w:t xml:space="preserve">{xml_text}</w:t></w:r></w:p>'
            )
    parts.append(_DOCX_DOCUMENT_TAIL)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        zf.writestr("_rels/.rels", _DOCX_RELS)
        zf.writestr("word/document.xml", "".join(parts))


def _pick_export_path(
    state: Dict[str, Any], fmt: str, fallback_dir: Optional[str] = None
) -> Tuple[Optional[str], bool]:
    """弹保存对话框选导出路径。返回 (路径, 是否弹过对话框)。

    - 用户取消 → (None, True)，调用方直接回「已取消」；
    - tkinter 不可用（headless/无桌面）→ (None, False)，调用方用默认路径兜底。
    """
    ext = "txt" if fmt == "txt" else "docx"
    base = (state.get("history_name") or "").removesuffix(".pdf")
    base = (base or "矫正导出").strip() or "矫正导出"
    initial = f"{base}.{ext}"
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        # 置顶：避免对话框出现在浏览器窗口后面（root 已 withdraw，无任务栏入口，
        # 被遮挡时用户完全找不到它）
        try:
            root.attributes("-topmost", True)
        except Exception:  # noqa: BLE001  个别环境不支持 topmost，忽略
            pass
        try:
            path = filedialog.asksaveasfilename(
                title=f"导出为 {ext.upper()} 文件",
                defaultextension=f".{ext}",
                filetypes=[(f"{ext.upper()} 文件", f"*.{ext}"), ("所有文件", "*.*")],
                initialfile=initial,
                initialdir=fallback_dir or ".",
            )
        finally:
            try:
                root.destroy()
            except Exception:
                pass
        return (path if path else None), True
    except Exception:
        return None, False


def _default_export_path(initial: str) -> str:
    """headless 环境的兜底路径：当前目录 + 默认文件名，重名自动加序号。"""
    p = Path(initial)
    if not p.exists():
        return str(p)
    n = 1
    while True:
        q = p.with_name(f"{p.stem} ({n}){p.suffix}")
        if not q.exists():
            return str(q)
        n += 1


class _ExportAborted(Exception):
    """矫正界面已关闭（浏览器关闭/服务停止），导出保存对话框无法弹出。"""


def _ask_export_path(
    state: Dict[str, Any], fmt: str, fallback_dir: Optional[str] = None
) -> Tuple[Optional[str], bool]:
    """把保存对话框交给主线程弹出（tkinter 在主线程才能可靠显示/置顶）。

    返回与 _pick_export_path 相同：(路径, 是否弹过对话框)：
    - 用户取消 → (None, True)；
    - tkinter 不可用（headless）→ (None, False)，调用方默认路径兜底；
    - 界面已关闭（主循环退出前唤醒）→ 抛 _ExportAborted。
    """
    req: Dict[str, Any] = {
        "fmt": fmt,
        "fallback_dir": fallback_dir,
        "done": threading.Event(),
        "result": None,
        "aborted": False,
    }
    lock = state.get("dlg_lock")
    if lock is None:
        lock = threading.RLock()
    with lock:
        state.setdefault("dlg_queue", []).append(req)
    req["done"].wait()
    if req.get("aborted"):
        raise _ExportAborted()
    return req["result"]


def _drain_dialog_queue(state: Dict[str, Any]) -> None:
    """主线程循环调用：取出待弹的保存对话框请求，逐个弹框并回填结果。

    tkinter 不能在 HTTP worker 线程里可靠弹窗（对话框可能不显示/被浏览器
    遮挡/线程间调用报错），因此统一挪到 correct_pages 的主循环里执行。
    """
    lock = state.get("dlg_lock")
    if lock is None:
        lock = threading.RLock()
    with lock:
        reqs = list(state.get("dlg_queue") or [])
        state["dlg_queue"] = []
    for req in reqs:
        try:
            req["result"] = _pick_export_path(
                state, str(req.get("fmt") or "txt"), req.get("fallback_dir")
            )
        except Exception:  # noqa: BLE001  tkinter 不可用（headless）→ 调用方兜底
            req["result"] = (None, False)
        finally:
            req["done"].set()


def _abort_dialog_queue(state: Dict[str, Any]) -> None:
    """服务关闭时唤醒所有等待对话框的请求（标记 aborted，让 handler 放弃）。"""
    lock = state.get("dlg_lock")
    if lock is None:
        lock = threading.RLock()
    with lock:
        reqs = list(state.get("dlg_queue") or [])
        state["dlg_queue"] = []
    for req in reqs:
        req["aborted"] = True
        req["done"].set()


def _browser_gone(
    state: Dict[str, Any],
    *,
    now: Optional[float] = None,
    stale_since: Optional[float] = None,
) -> Tuple[bool, Optional[float]]:
    """判断浏览器是否已关闭且超过 idle_timeout 秒，应自动继续后续流程。

    返回 (gone, stale_since)：gone=True 表示判定成立；stale_since 用于心跳
    失联场景的连续确认（首次失联记时刻，持续 _STALE_CONFIRM_SECONDS 才认定）。
    """
    now = time.monotonic() if now is None else now
    idle = float(state.get("idle_timeout") or 600)
    gone_at = state.get("gone_at")
    if gone_at is not None:
        # 收到过 pagehide 信标（标签页被关闭）：信标为准，倒计时满即判定
        return (now - gone_at >= idle), None
    if now - state.get("last_heartbeat", 0.0) >= idle:
        # 心跳失联（无信标，如浏览器被强杀）：需连续失联确认，防休眠唤醒误判
        if stale_since is None:
            return False, now
        return (now - stale_since >= _STALE_CONFIRM_SECONDS), stale_since
    return False, None


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------


class _CorrectionHandler(BaseHTTPRequestHandler):
    server_version = "ptoe-correct/1.0"

    # -- helpers --

    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any) -> bytes:
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")

    def _touch_heartbeat(self) -> None:
        """页面心跳：刷新存活时刻，并取消可能存在的关闭倒计时（标签页被恢复）。"""
        st = self.server.state
        st["last_heartbeat"] = time.monotonic()
        st["gone_at"] = None

    # -- GET --

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, _UI_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/heartbeat":
            self._touch_heartbeat()
            self._send(204, b"", "text/plain")
        if path == "/api/pages":
            state = self.server.state
            # S5：跨线程读 pages 时加锁快照（与保存/暂存/完成写入并发）
            lock = state.get("pages_lock")
            if lock is not None:
                with lock:
                    pages_snapshot = dict(state["pages"])
            else:
                pages_snapshot = dict(state["pages"])
            pages_list = []
            for n in sorted(pages_snapshot):
                raw_html = pages_snapshot[n]
                # Add ptoe-marker class for marker spans so saved pages render
                # highlighted in the editor while leaving stored HTML unchanged.
                served_html = _ensure_marker_classes(raw_html)
                pages_list.append({"page": n, "text": _page_text(served_html)})
            payload = {"pages": pages_list}
            self._send(200, self._json(payload), "application/json; charset=utf-8")
            return
        if path == "/api/history":
            # 历史记录列表：文件名/路径分列显示（同名不同路径可区分），
            # 同一文件按时间倒序编号版本（v1=最新）
            items = _history_entries()
            by_pdf: Dict[str, List[Dict[str, Any]]] = {}
            for it in items:
                by_pdf.setdefault(it["pdf"], []).append(it)
            for group in by_pdf.values():
                group.sort(key=lambda x: x["updated"], reverse=True)
                for i, it in enumerate(group, start=1):
                    it["version"] = i
            self._send(200, self._json({"items": items}), "application/json; charset=utf-8")
            return
        m = re.fullmatch(r"/preview/(\d+)", path)
        if m:
            data = _preview_bytes(self.server.state, int(m.group(1)))
            if data is None:
                self._send(404, b"no image", "text/plain")
                return
            self._send(200, data[1], data[0], {"Cache-Control": "max-age=3600"})
            return
        m = re.fullmatch(r"/full/(\d+)", path)
        if m:
            data = _full_bytes(self.server.state, int(m.group(1)))
            if data is None:
                self._send(404, b"no image", "text/plain")
                return
            self._send(200, data[1], data[0], {"Cache-Control": "max-age=3600"})
            return
        self._send(404, b"not found", "text/plain")

    # -- POST --

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/heartbeat":
            self._touch_heartbeat()
            self._send(204, b"", "text/plain")
            return
        if path == "/api/gone":
            # pagehide 信标（sendBeacon）：标签页被关闭/导航离开，开始倒计时
            st = self.server.state
            st["gone_at"] = st.get("gone_at") or time.monotonic()
            self._send(204, b"", "text/plain")
            return
        if path == "/api/history/delete":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                deleted = _delete_history(list(body.get("ids") or []), bool(body.get("all")))
                self._send(200, self._json({"ok": True, "deleted": deleted}), "application/json; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                self._send(500, self._json({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            return
        if path == "/api/history/load":
            # 把某一历史版本重新载入浏览器编辑器（再次矫正）：
            # 返回该版本的 pages（按页码排序，字段与 /api/convert 一致用 html）；
            # 同时把预览图来源切换为该版本所属 PDF，保证图与文对应。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                pid = str(body.get("id") or "")
                loaded = _load_history_version(pid)
                if loaded is None:
                    self._send(
                        404,
                        self._json({"ok": False, "error": f"history version not found: {pid}"}),
                        "application/json; charset=utf-8",
                    )
                    return
                out = [
                    # 与 /api/pages 一致：serve 时补回 ptoe-marker 显示类，
                    # 否则保存时被 sanitize 剥掉的 class 会让标记渲染成纯文本
                    # （旧历史版本磁盘载荷无 class，必须在此补齐）。
                    {"page": int(k), "html": _ensure_marker_classes(str(v))}
                    for k, v in loaded["pages"].items()
                ]
                out.sort(key=lambda x: x["page"])
                pdf = loaded["pdf"]
                st = self.server.state
                # 把版本内容同步进服务端状态：刷新/再次载入/暂存/完成都以浏览器
                # 打开的内容为准（无文件模式下 state["pages"] 初始为空）
                # S5：与保存/暂存/完成写入并发时加锁
                lock = st.get("pages_lock")
                with lock if lock is not None else nullcontext():
                    for k, v in loaded["pages"].items():
                        try:
                            st["pages"][int(k)] = sanitize_html(str(v))
                        except (TypeError, ValueError):
                            continue
                if pdf and Path(pdf).is_file() and st.get("pdf_path") != pdf:
                    st["pdf_path"] = pdf
                    st["preview_cache"] = OrderedDict()  # 换书后旧页码缓存作废
                    st["preview_doc"] = None  # 旧 PDF 句柄作废（下次按需重开）
                if pdf:
                    st["history_name"] = Path(pdf).name  # 后续暂存沿用该书名称
                self._send(
                    200,
                    self._json({"ok": True, "pages": out, "pdf": pdf}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/convert":
            # 繁简转换（简→繁 / 繁→简）：只转换文本节点，标签/标记不变；
            # 无状态 —— 只返回转换结果，由浏览器更新界面（保存时才落盘）。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                mode = body.get("mode")
                if mode not in _CONVERT_MODES:
                    self._send(
                        400,
                        self._json({"ok": False, "error": f"bad mode: {mode}"}),
                        "application/json; charset=utf-8",
                    )
                    return
                converted = []
                for item in body.get("pages") or []:
                    try:
                        n = int(item.get("page"))
                    except (TypeError, ValueError):
                        continue
                    html_text = sanitize_html(str(item.get("html") or ""))
                    converted.append(
                        {"page": n, "html": convert_text_html(html_text, mode)}
                    )
                self._send(
                    200,
                    self._json({"ok": True, "pages": converted}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/clean":
            # 文本智能清理（段落合并 / 段首 #/* 符号 / 中英文标点 / HTML 标签）：
            # 无状态 —— 只返回清理结果，由浏览器更新界面（保存时才落盘）。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                cleaned = []
                for item in body.get("pages") or []:
                    try:
                        n = int(item.get("page"))
                    except (TypeError, ValueError):
                        continue
                    cleaned.append({"page": n, "html": clean_page_html(str(item.get("html") or ""))})
                self._send(
                    200,
                    self._json({"ok": True, "pages": cleaned}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/export":
            # 导出 TXT / DOCX：浏览器把当前全部页面（含未保存修改）发来，
            # 服务端转纯文本后由用户弹窗（tkinter 保存对话框）选择保存位置。
            # body 可带 "path" 直接指定路径（测试/脚本用，跳过对话框）。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                fmt = str(body.get("format") or "")
                if fmt not in ("txt", "docx"):
                    self._send(
                        400,
                        self._json({"ok": False, "error": f"bad format: {fmt}"}),
                        "application/json; charset=utf-8",
                    )
                    return
                items = sorted(
                    (x for x in (body.get("pages") or []) if isinstance(x, dict)),
                    key=lambda x: _safe_int(x.get("page")),
                )
                blocks: List[Tuple[str, str]] = []
                for item in items:
                    blocks.extend(
                        _html_to_export_blocks(str(item.get("html") or ""))
                    )
                st = self.server.state
                explicit = body.get("path")
                used_dialog = False
                if explicit:
                    out_path = str(explicit)
                else:
                    # 保存对话框统一交给主线程弹出（tkinter 不能在 HTTP
                    # worker 线程可靠弹窗）；界面已关闭则直接放弃本次导出
                    try:
                        out_path, used_dialog = _ask_export_path(st, fmt)
                    except _ExportAborted:
                        self._send(
                            500,
                            self._json(
                                {"ok": False, "error": "矫正界面已关闭，导出取消"}
                            ),
                            "application/json; charset=utf-8",
                        )
                        return
                    if out_path is None and used_dialog:
                        self._send(
                            200,
                            self._json({"ok": False, "cancelled": True}),
                            "application/json; charset=utf-8",
                        )
                        return
                    if out_path is None:
                        # headless 兜底：当前目录 + 默认文件名（重名自动加序号）
                        base = (st.get("history_name") or "矫正导出").removesuffix(".pdf")
                        base = (base or "矫正导出").strip() or "矫正导出"
                        out_path = _default_export_path(f"{base}.{fmt}")
                out = Path(out_path)
                out.parent.mkdir(parents=True, exist_ok=True)
                if fmt == "txt":
                    text = "\n\n".join(t for _, t in blocks) + "\n"
                    out.write_text(text, encoding=_TXT_ENCODING)
                else:
                    _build_docx(blocks, str(out))
                self._send(
                    200,
                    self._json(
                        {"ok": True, "path": str(out), "used_dialog": used_dialog}
                    ),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path not in ("/api/save", "/api/stage", "/api/finish"):
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8"))
            items = body.get("pages") or []
            state = self.server.state
            # 浏览器可能载入历史版本后新增了会话外页码（如无文件模式打开暂存），
            # 保存/暂存/完成一律按提交内容 upsert，而不是只更新已知页码。
            # S5：写入 pages 与「构建 ordered 快照」共用一把锁，保证与
            # /api/pages 读取、/api/history/load 写入互斥。
            saved = 0
            lock = state.get("pages_lock")
            if lock is not None:
                with lock:
                    for item in items:
                        try:
                            n = int(item.get("page"))
                        except (TypeError, ValueError):
                            continue
                        state["pages"][n] = sanitize_html(str(item.get("html") or ""))
                        saved += 1
                    pages_snapshot = dict(state["pages"])
            else:
                for item in items:
                    try:
                        n = int(item.get("page"))
                    except (TypeError, ValueError):
                        continue
                    state["pages"][n] = sanitize_html(str(item.get("html") or ""))
                    saved += 1
                pages_snapshot = dict(state["pages"])
            # 历史记录名（无文件模式打开历史版本后，保存/暂存/完成沿用该名称）
            if body.get("name"):
                state["history_name"] = str(body.get("name"))
            # 保存：不新建历史版本，直接覆盖当前缓存（同一份内容反复保存只更新
            # 同一个文件）；暂存/完成并转换仍各生成一个新历史版本（可随时恢复）
            # S4：写入失败返回 False → 前端报错提示（不静默丢数据）
            if path == "/api/save":
                ok = _overwrite_history(state)
            else:
                ok = _write_history_version(state)
            payload = {"ok": ok, "saved": saved}
            if not ok:
                payload["error"] = "历史缓存写入失败（磁盘错误或权限不足？）"
            if path == "/api/finish":
                # 完成并转换：不关闭服务，每次点击都重新转换（on_convert 回调），
                # 用户可留在页面继续修改后再次点击；浏览器关闭才结束等待。
                conv = None
                on_convert = state.get("on_convert")
                if on_convert:
                    ordered = [
                        {"page": n, "text": pages_snapshot[n]} for n in sorted(pages_snapshot)
                    ]
                    # name：浏览器打开的历史记录名（无文件模式下用作 EPUB 标题）
                    with state["convert_lock"]:
                        try:
                            conv = on_convert(ordered, name=body.get("name") or None)
                        except Exception as e:  # noqa: BLE001 - 转换异常回给浏览器提示
                            conv = {"ok": False, "message": str(e)}
                payload["converted"] = conv
            elif path == "/api/stage":
                payload["staged"] = True
            self._send(200, self._json(payload), "application/json; charset=utf-8")
        except Exception as e:  # noqa: BLE001 - 界面出错要回给浏览器而不是崩溃
            self._send(500, self._json({"ok": False, "error": str(e)}), "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args: Any) -> None:
        # 静默访问日志，避免终端刷屏
        return


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def correct_pages(
    pages: List[Dict[str, Any]],
    *,
    pdf_path: str | Path | None = None,
    img_dir: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    preview_dpi: int = 110,
    preview_quality: int = 82,
    idle_timeout: int = 600,
    on_convert: Optional[Callable[[List[Dict[str, Any]]], Dict[str, Any]]] = None,
    history: bool = True,
    preload_history: bool = True,
) -> List[Dict[str, Any]]:
    """启动手动矫正界面并阻塞，直到浏览器被关闭（或 Ctrl+C）。

    返回与输入同构的校正后 pages 列表：[{'page': int, 'text': str}, ...]，
    按页码升序；text 为 sanitize_html 清洗后的 HTML 片段（含白名单标记）。
    用户按 Ctrl+C 中断时放弃本次矫正结果（保持原 text）继续流程。

    on_convert：可选回调，收到按页码排序的 pages 列表并返回结果 dict。
    点「完成并转换」时（可重复点击）在服务线程中串行调用，结果经
    /api/finish 响应回给浏览器（转换完成/未完成提示）；服务不因一次
    「完成并转换」而关闭，用户可留在页面继续修改后再次点击。
    阻塞结束条件只有：浏览器关闭超过 idle_timeout（自动继续）或 Ctrl+C。

    history：为 True 时按 pdf_path 把矫正内容缓存到本地（data/correction_history/），
    保存/暂存/完成时写入；下次对同一 PDF 运行 --correct 自动加载已修改内容，
    支持对已矫正内容再次手动矫正。

    preload_history：为 True（默认）时，启动界面时用同一 PDF 最新历史版本覆盖
    传入的初始文本（适用于直接矫正/无 OCR 的 correct 命令，便于对已修改内容
    再次矫正）；为 False 时完全使用传入的 pages（适用于 epub 流水线：重新识别
    后的新文本必须优先展示，不能被上一次暂存/保存的历史内容覆盖）。

    idle_timeout：浏览器（页面）被关闭后的等待秒数，超过即自动继续后续流程
    （保留最后一次保存/完成的内容）；默认 600 秒（10 分钟）。
    页面每 30s 发心跳，关闭标签页时发 pagehide 信标，据此监测。
    """
    ordered = sorted(
        (
            {"page": int(p["page"]), "text": str(p.get("text") or "")}
            for p in pages
            if "page" in p and "text" in p
        ),
        key=lambda x: x["page"],
    )
    # 历史缓存：同一 PDF 最新版本的矫正内容优先作为初始内容；
    # preload_history=False 时（重新识别后）不加载历史，避免旧暂存覆盖新识别文本。
    history_pages: Dict[str, str] = _history_pages_for_init(
        str(pdf_path), history=history, preload_history=preload_history
    )
    if history_pages:
        loaded = sum(1 for p in ordered if str(p["page"]) in history_pages)
        if loaded:
            print(f"      已加载历史矫正记录（{loaded}/{len(ordered)} 页）")
    state: Dict[str, Any] = {
        "pages": {
            p["page"]: str(history_pages.get(str(p["page"]), p["text"])) for p in ordered
        },
        # S5：pages 读写共用锁（/api/pages、/api/history/load、保存/暂存/完成）
        "pages_lock": threading.Lock(),
        "finished": threading.Event(),
        # P2：预览 JPEG LRU 缓存（OrderedDict，上限 _PREVIEW_CACHE_MAX）；
        # preview_doc/preview_doc_lock 复用同一 fitz.Document，避免每页重开 PDF
        "preview_cache": OrderedDict(),
        "preview_doc": None,
        "preview_doc_lock": threading.Lock(),
        "pdf_path": str(pdf_path) if pdf_path else None,
        "img_dir": str(img_dir) if img_dir else None,
        "preview_dpi": preview_dpi,
        "preview_quality": preview_quality,
        # 浏览器存活监测（关闭浏览器后自动继续）
        "last_heartbeat": time.monotonic(),
        "gone_at": None,
        "idle_timeout": float(idle_timeout),
        "auto_finished": False,
        # 完成并转换（可重复）与本地历史缓存（同一 PDF 多版本）
        "on_convert": on_convert,
        "convert_lock": threading.Lock(),
        # 无文件会话（pdf_path 为 None）也允许暂存/保存：用会话前缀 manual_<随机>
        # 落盘历史缓存，之后可再次打开；名称默认「手动录入」。
        "history_prefix": (
            _history_prefix(str(pdf_path)) if pdf_path else f"manual_{uuid4().hex[:8]}"
        )
        if history
        else None,
        "history_name": None if pdf_path else "手动录入",
        "history_lock": threading.Lock(),
        # 导出保存对话框队列：tkinter 只能在主线程可靠弹窗——/api/export
        # handler 把请求入队，主循环 _drain_dialog_queue 弹框并回填结果
        "dlg_queue": [],
        "dlg_lock": threading.RLock(),
    }
    server = ThreadingHTTPServer((host, port), _CorrectionHandler)
    server.daemon_threads = True
    server.state = state
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()
    url = f"http://{server.server_address[0]}:{server.server_address[1]}/"
    print(f"      矫正界面已启动: {url}（对比原图与识别文字，完成后点「完成并转换」）")
    if open_browser:
        webbrowser.open(url)
    try:
        # 浏览器关闭监测：页面每 30s 发心跳；关闭标签页时发 pagehide 信标。
        # 信标确认关闭或心跳失联超过 idle_timeout 秒后，自动继续后续流程。
        stale_since: Optional[float] = None
        while not state["finished"].is_set():
            if state["finished"].wait(0.5):
                break  # 浏览器被判定关闭，自动继续（「完成并转换」不再关闭服务）
            # 导出保存对话框只能在主线程弹出（tkinter 线程安全），
            # 逐轮取走队列里的请求弹框，阻塞直到用户选择/取消
            _drain_dialog_queue(state)
            gone, stale_since = _browser_gone(state, stale_since=stale_since)
            if gone:
                state["auto_finished"] = True
                state["finished"].set()
                break
        if state.get("auto_finished"):
            idle = float(state.get("idle_timeout") or 600)
            secs = int(idle)
            print(
                f"      浏览器已关闭超过 {secs // 60} 分 {secs % 60} 秒，"
                "自动继续后续流程（未保存的修改已丢弃，保留已保存内容）"
            )
    except KeyboardInterrupt:
        print("\n      手动矫正被中断，放弃本次矫正结果，继续原流程")
    finally:
        server.shutdown()
        server.server_close()
        serve_thread.join(timeout=5)
        # 唤醒可能阻塞在保存对话框上的 /api/export 请求（handler 返回 500）
        _abort_dialog_queue(state)
        # P2：关闭复用的 fitz.Document，释放文件句柄
        doc = state.get("preview_doc")
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
            state["preview_doc"] = None
    # S5：返回前对 pages 加锁快照
    lock = state.get("pages_lock")
    if lock is not None:
        with lock:
            out = [{"page": n, "text": state["pages"][n]} for n in sorted(state["pages"])]
    else:
        out = [{"page": n, "text": state["pages"][n]} for n in sorted(state["pages"])]
    return out


# ---------------------------------------------------------------------------
# 内嵌 HTML 界面
#   虚拟列表（仅渲染视口附近行，DOM 与页数无关，支撑 1000+ 页）；
#   选中文字弹出快捷菜单（点击菜单按钮后菜单保持隐藏，不再自动弹出）；
#   可配置快捷键（每个操作绑定一个组合键，localStorage 持久化）；
#   标记按钮：全文 / 段落（插入到光标处；段落标记段首=与上一段合并、
#   段尾=与下一段合并）；
#   布局：左右两栏等高（CSS grid 拉伸），图片栏完整显示整张原图；
#   点「完成并转换」后弹出完成状态提示，并询问是否关闭当前页面。
# ---------------------------------------------------------------------------

_UI_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>矫正 - ptoe</title>
<style>
:root{--accent:#2f6fed;--border:#d8dee6;--bg:#f4f6f9;--editor-font-size:14px;}
*{box-sizing:border-box}
body{margin:0;font-family:"Microsoft YaHei",system-ui,sans-serif;background:var(--bg);color:#1c2733;}
#toolbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:4px;padding:5px 8px;background:#fff;border-bottom:1px solid var(--border);flex-wrap:wrap;font-size:12px;}
#toolbar .title{font-weight:700;margin-right:10px;}
#toolbar .spacer{flex:1;}
#toolbar .sep{width:1px;height:22px;background:var(--border);margin:0 4px;}
#toolbar label{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:#5a6b7c;}
#toolbar input[type=number]{width:64px;padding:3px 5px;border:1px solid var(--border);border-radius:4px;font:inherit;}
/* U1：按功能分组的浅色区块，替代细分隔线；主操作组不折行、右端常驻 */
#toolbar .tb-group{display:inline-flex;align-items:center;gap:3px;padding:2px 6px;background:#f4f6f9;border:1px solid #e4e9f0;border-radius:8px;white-space:nowrap;}
#toolbar .tb-group .tb-label{font-size:11px;color:#8a97a6;margin-right:2px;user-select:none;}
#toolbar .tb-main{flex-wrap:nowrap;background:transparent;border-color:transparent;margin-left:auto;}
#toolbar .ic-btn{width:26px;height:26px;padding:0;display:inline-flex;align-items:center;justify-content:center;}
/* 紧凑尺寸：工具栏内文字按钮/下拉/输入框缩小，配合 flex-wrap 保证最多两行 */
#toolbar button{padding:3px 8px;font-size:12px;}
#toolbar select{padding:3px 5px;font-size:12px;}
#toolbar .ic-btn:disabled{opacity:.4;cursor:default;}
button{font:inherit;padding:5px 11px;border:1px solid var(--border);border-radius:4px;background:#fff;cursor:pointer;}
button:hover{border-color:var(--accent);color:var(--accent);}
button.active{border-color:var(--accent);background:#eef3fb;color:var(--accent);}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent);}
button.primary:hover{background:#2256c2;color:#fff;}
button:disabled{opacity:.5;cursor:not-allowed;}
select{font:inherit;padding:5px 8px;border:1px solid var(--border);border-radius:4px;background:#fff;}
#status{font-size:12px;color:#5a6b7c;}
#pos{font-size:12px;color:#8a97a6;white-space:nowrap;}
/* U2：三色 toast 提示（成功/失败/警告），顶部居中，3s 自动消失 */
#toast{position:fixed;top:60px;left:50%;transform:translateX(-50%);z-index:90;display:flex;flex-direction:column;gap:6px;align-items:center;pointer-events:none;}
.toast{background:#1c2733;color:#fff;font-size:13px;line-height:1.5;padding:8px 16px;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.25);opacity:0;transform:translateY(-6px);transition:opacity .2s,transform .2s;max-width:70vw;}
.toast.show{opacity:1;transform:translateY(0);}
.toast.ok{background:#1a7f37;}
.toast.fail{background:#c0392b;}
.toast.warn{background:#b8860b;}
button.loading::after{content:'';display:inline-block;width:11px;height:11px;margin-left:8px;border:2px solid rgba(255,255,255,.45);border-top-color:#fff;border-radius:50%;animation:ptoe-spin .8s linear infinite;vertical-align:-2px;}
@keyframes ptoe-spin{to{transform:rotate(360deg);}}
/* U3：hintbar 可折叠（✕ 关闭，localStorage 记忆） */
#hintbar{padding:6px 14px;font-size:12px;color:#5a6b7c;background:#eef3fb;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;}
#hintbar .hint-text{flex:1;}
#hintClose{flex:none;width:22px;height:22px;padding:0;line-height:1;border:none;background:transparent;color:#8a97a6;font-size:14px;border-radius:4px;}
#hintClose:hover{background:#dfe7f3;color:#1c2733;border:none;}
#hintbar.hidden{display:none;}
#pages{position:relative;overflow-anchor:none;}
.page-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;align-items:stretch;background:#fff;border:1px solid var(--border);border-radius:8px;padding:12px;}
.page-head{grid-column:1 / -1;font-size:12px;color:#5a6b7c;border-bottom:1px dashed var(--border);padding-bottom:6px;}
.img-panel{position:relative;min-width:0;background:#fff;border:1px solid var(--border);border-radius:4px;padding:4px;}
.img-panel img{width:100%;height:auto;display:block;background:#fff;cursor:zoom-in;}
.badge{position:absolute;top:8px;left:8px;background:rgba(0,0,0,.55);color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;pointer-events:none;}
.editable{min-height:220px;padding:10px 14px;border:1px solid var(--border);border-radius:4px;line-height:1.7;font-size:var(--editor-font-size);outline:none;}
.editable:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(47,111,237,.15);}
.editable h1{font-size:1.45em;} .editable h2{font-size:1.28em;} .editable h3{font-size:1.12em;}
.editable h4,.editable h5,.editable h6{font-size:1.02em;}
.ptoe-align-left{text-align:left;} .ptoe-align-center{text-align:center;} .ptoe-align-right{text-align:right;}
.ptoe-marker{background:#fff3bf;border:1px solid #e8c24a;border-radius:3px;padding:0 4px;font-size:12px;color:#8a6d00;cursor:help;user-select:all;}
.ptoe-search{background:#fff1a8;border-radius:2px;padding:0 2px;color:inherit;}
.editable mark.ptoe-search{background:#fff1a8;color:inherit;border-radius:2px;padding:0 2px;}
 .editable .ptoe-note{font-size:12px;color:#556677;}
.pop-btn:hover{background:#eef3fb;border-color:var(--accent);}
/* 全局延迟提示：悬停超过设定时间才显示（含快捷键），延迟可在「快捷键」设置中调整 */
#tip{position:fixed;z-index:80;display:none;background:#1c2733;color:#fff;font-size:12px;line-height:1.5;padding:5px 9px;border-radius:4px;max-width:320px;pointer-events:none;}
#tip .tip-key{color:#bcd0e5;margin-left:6px;white-space:nowrap;}
#popup{position:fixed;z-index:60;display:none;flex-wrap:wrap;gap:4px;max-width:280px;padding:6px 8px;background:#fff;border:1px solid var(--border);border-radius:8px;box-shadow:0 4px 18px rgba(0,0,0,.22);}
.pop-btn{min-width:34px;height:30px;padding:0 8px;font-size:13px;border:1px solid var(--border);background:#fff;color:#1c2733;border-radius:6px;cursor:pointer;line-height:1;}
#popup .sep{width:100%;height:0;border-top:1px solid var(--border);margin:2px 0;}
.ic-b{font-weight:700;} .ic-i{font-style:italic;font-family:Georgia,'Times New Roman',serif;} .ic-h{font-weight:700;} .ic-p{font-weight:600;} .ic-t{font-weight:600;} .ic-n{font-size:12px;color:#556677;}
/* 左侧预览图上的「图」按钮：把当前显示的图片插入右侧文字光标处 */
.img-insert{position:absolute;right:8px;bottom:8px;z-index:5;padding:3px 10px;font-size:13px;border:1px solid var(--border);background:#fff;color:#1c2733;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.18);cursor:pointer;}
.img-insert:hover{background:#eef3fb;border-color:var(--accent);}
/* 插入图片：全画幅（占满文字宽度）/ 局部（按原尺寸居中） */
.editable p.ptoe-img-full{text-align:center;text-indent:0;}
.editable p.ptoe-img-fit{text-align:center;text-indent:0;}
.editable p.ptoe-img-full img{display:block;width:100%;height:auto;margin:0 auto;}
.editable p.ptoe-img-fit img{display:block;max-width:100%;height:auto;margin:0 auto;}
/* 搜索 / 替换弹窗（工具栏「搜」按钮打开；结果列表点击跳转、↑↓上一个/下一个） */
#searchModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
.search-modal{max-width:640px;width:94%;display:flex;flex-direction:column;}
/* 导出弹窗（工具栏「导出」按钮打开；复用 .search-modal 布局） */
#exportModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
.export-desc{font-size:13px;color:#5a6b7c;margin:0 0 14px;line-height:1.6;}
.export-actions{display:flex;gap:10px;justify-content:flex-end;}
.search-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;}
.search-head h3{margin:0;}
.search-head .x-btn{width:26px;height:26px;padding:0;line-height:1;border:none;background:transparent;color:#8a97a6;font-size:16px;border-radius:4px;cursor:pointer;}
.search-head .x-btn:hover{background:#dfe7f3;color:#1c2733;}
.search-row{display:flex;align-items:center;gap:8px;margin-bottom:8px;}
.search-row input[type="text"]{flex:1;min-width:0;padding:6px 8px;border:1px solid var(--border);border-radius:5px;font:inherit;}
.search-regex{display:inline-flex;align-items:center;gap:3px;font-size:13px;color:#33414f;white-space:nowrap;}
.search-nav{display:flex;align-items:center;gap:8px;margin:2px 0 8px;}
.search-nav button{width:32px;height:28px;padding:0;border:1px solid var(--border);background:#fff;border-radius:5px;cursor:pointer;font-size:14px;}
.search-nav button:hover{background:#eef3fb;border-color:var(--accent);}
#searchPos{font-size:12px;color:#5a6b7c;min-width:70px;text-align:center;}
#searchList{max-height:45vh;overflow:auto;border:1px solid var(--border);border-radius:6px;padding:6px;background:#fafbfc;font-size:12px;}
.sr-head{display:flex;align-items:center;gap:8px;padding:2px 0 6px;color:#5a6b7c;font-weight:600;}
.sr-item{padding:6px 8px;border:1px solid var(--border);border-radius:6px;margin-bottom:6px;cursor:pointer;background:#fff;}
.sr-item:hover{border-color:var(--accent);background:#f2f7ff;}
.sr-item.current{border-color:var(--accent);background:#eaf2ff;box-shadow:0 0 0 1px var(--accent);}
.sr-page{font-size:11px;color:#5a6b7c;margin-bottom:2px;}
.sr-ctx{color:#33414f;line-height:1.5;word-break:break-all;}
.sr-ctx mark{background:#ffe08a;color:#5c4000;border-radius:2px;padding:0 2px;}
.sr-empty{color:#8a97a6;padding:6px 2px;}
#modalBg{position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#finishModalBg{position:fixed;inset:0;z-index:70;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#historyModalBg{position:fixed;inset:0;z-index:70;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#helpModalBg{position:fixed;inset:0;z-index:65;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#historyTable th{position:sticky;top:0;background:#f4f6f9;}
.modal{background:#fff;border-radius:10px;padding:18px 22px;max-width:520px;width:92%;max-height:80vh;overflow:auto;}
.modal h3{margin:0 0 10px;}
.modal h4{margin:14px 0 6px;font-size:14px;color:#1c2733;}
.help-table{width:100%;border-collapse:collapse;font-size:13px;}
.help-table td{padding:4px 8px;border-bottom:1px solid var(--border);vertical-align:top;}
.help-table td:first-child{white-space:nowrap;color:#33414f;font-weight:600;}
#shortcutTable{width:100%;border-collapse:collapse;}
#shortcutTable td{padding:6px 8px;border-bottom:1px solid var(--border);font-size:14px;}
#shortcutTable tr{cursor:pointer;}
#shortcutTable tr:hover td{background:#f7fafd;}
kbd{background:#eef1f5;border:1px solid #c9d1da;border-radius:3px;padding:1px 6px;font-size:12px;font-family:inherit;}
#closeSettings{margin-top:12px;}
@media (max-width:900px){
  .page-row{grid-template-columns:1fr;}
  .editable{font-size:calc(var(--editor-font-size) + 2px);}
}
</style>
</head>
<body>
<div id="toolbar">
  <div class="tb-group" role="group" aria-label="格式">
    <button type="button" class="ic-btn" data-op="bold" onmousedown="event.preventDefault()" title="粗体" aria-label="粗体"><span class="ic-b">B</span></button>
    <button type="button" class="ic-btn" data-op="italic" onmousedown="event.preventDefault()" title="斜体" aria-label="斜体"><span class="ic-i">I</span></button>
    <button type="button" class="ic-btn" data-op="heading" onmousedown="event.preventDefault()" title="标题：正文↔一级标题↔…↔六级标题循环" aria-label="标题"><span class="ic-h">标</span></button>
    <button type="button" class="ic-btn" data-op="p" onmousedown="event.preventDefault()" title="正文：转为普通段落" aria-label="正文"><span class="ic-p">正</span></button>
    <button type="button" class="ic-btn" data-op="remove" onmousedown="event.preventDefault()" title="清除格式" aria-label="清除格式"><span class="ic-t">清</span></button>
    <button type="button" class="ic-btn" data-op="note" onmousedown="event.preventDefault()" title="注释：把当前块设为注释（小字灰色）" aria-label="注释">注</button>
    <button type="button" class="ic-btn" id="colorBtn" onmousedown="event.preventDefault()" title="文本颜色" aria-label="文本颜色">色</button>
    <button type="button" class="ic-btn" id="formatBrushBtn" onmousedown="event.preventDefault()" title="格式刷" aria-label="格式刷">刷</button>
  </div>
  <div class="tb-group" role="group" aria-label="对齐">
    <button type="button" class="ic-btn" data-op="align_left" onmousedown="event.preventDefault()" title="居左" aria-label="居左">左</button>
    <button type="button" class="ic-btn" data-op="align_center" onmousedown="event.preventDefault()" title="居中" aria-label="居中">中</button>
    <button type="button" class="ic-btn" data-op="align_right" onmousedown="event.preventDefault()" title="居右" aria-label="居右">右</button>
  </div>
  <div class="tb-group" role="group" aria-label="标记">
    <button type="button" class="ic-btn" data-op="marker_full" onmousedown="event.preventDefault()" title="全文标记：当前文章到此结束，后续内容属于新文章（开新页）" aria-label="全文标记">篇</button>
    <button type="button" class="ic-btn" data-op="marker_note" onmousedown="event.preventDefault()" title="注释标记：插入到光标处，由对应注释段落替换（数量需一一匹配）" aria-label="注释标记">释</button>
    <button type="button" class="ic-btn" data-op="marker_join" onmousedown="event.preventDefault()" title="段落标记：插入到光标处；段首=与上一段合并，段尾=与下一段合并" aria-label="段落标记">段</button>
    <button type="button" class="ic-btn" data-op="marker_page" onmousedown="event.preventDefault()" title="换页标记：从此处之后的内容显示在新的一页" aria-label="换页标记">页</button>
  </div>
  <div class="tb-group" role="group" aria-label="转换">
    <button type="button" id="toSimplifiedBtn" title="把全部页面文字转为简体（繁体→简体）">繁→简</button>
    <button type="button" id="toTraditionBtn" title="把全部页面文字转为繁体（简体→繁体）">简→繁</button>
    <button type="button" id="mdToggleBtn" title="切换 Markdown 源码 / 富文本编辑模式（详见帮助）">Markdown</button>
  </div>
  <div class="tb-group" role="group" aria-label="文本">
    <button type="button" id="cleanBtn" title="智能清理：合并被 OCR 拆散的小段落、清除段首 #/* 等符号、归一化中英文标点、移除残留的 HTML 标签">清理</button>
  </div>
  <div class="tb-group" role="group" aria-label="撤销重做">
    <button type="button" id="undoBtn" class="ic-btn" onmousedown="event.preventDefault()" disabled title="撤回上一步（Ctrl+Z）" aria-label="撤回（Ctrl+Z）">↶</button>
    <button type="button" id="redoBtn" class="ic-btn" onmousedown="event.preventDefault()" disabled title="前进下一步（Ctrl+Y / Ctrl+Shift+Z）" aria-label="前进（Ctrl+Y）">↷</button>
  </div>
  <div class="tb-group" role="group" aria-label="图片">
    <select id="imgModeSel" title="插入图片的显示模式：全画幅=占满文字宽度，局部=按原尺寸居中">
      <option value="full">全画幅</option>
      <option value="fit">局部</option>
    </select>
  </div>
  <div class="tb-group" role="group" aria-label="搜索替换">
    <button type="button" id="searchOpenBtn" class="primary" title="搜索/替换全部页面：弹出窗口显示所有匹配结果，支持上一个/下一个跳转、替换当前与全部替换">搜</button>
  </div>
  <div class="tb-group" role="group" aria-label="字号与跳转">
    <label>字号 <select id="fontSizeSel">
      <option value="12">12</option><option value="13">13</option><option value="14" selected>14</option>
      <option value="15">15</option><option value="16">16</option><option value="17">17</option>
      <option value="18">18</option><option value="20">20</option>
    </select></label>
    <label>跳转 <input type="number" id="pageJump" min="1" placeholder="页码"></label>
    <button type="button" id="jumpBtn" title="跳转到指定页码">跳转</button>
  </div>
  <span class="spacer"></span>
  <div class="tb-group tb-main" role="group" aria-label="工具与操作">
    <button type="button" id="helpBtn" title="帮助：Markdown 格式、快捷键与标记说明">帮助</button>
    <button type="button" id="historyBtn" title="历史记录：查看/管理本地矫正缓存（文件名与路径分列、多版本）">历史记录</button>
    <button type="button" id="settingsBtn" title="快捷键与提示设置">快捷键</button>
    <button type="button" id="exportBtn" title="导出：把全部页面的文字（含未保存的修改）导出为 TXT / DOCX 文件，保存位置由弹窗选择">导出</button>
    <span id="pos" aria-live="off"></span>
    <span id="status">加载中 ...</span>
    <button type="button" id="stageBtn" title="暂存：把当前修改暂时保存到本地历史缓存（不转换，可随时恢复）">暂存</button>
    <button type="button" id="saveBtn">保存</button>
    <button type="button" id="finishBtn" class="primary">完成并转换</button>
  </div>
</div>
<div id="hintbar">
  <span class="hint-text">左侧原图（点击切换预览/原图），右侧文字可直接编辑。选中文字弹出<b>图标快捷菜单</b>（悬停有提示）；支持<b>粗体</b>、<i>斜体</i>、标题、注释、居左/居中/居右与<span class="ptoe-marker">全文/段落/注释/换页标记</span>（标记插入到光标处；段落标记段首=合上段、段尾=合下段；换页标记=此后内容显示在新的一页）。可切换 <b>Markdown 模式</b>（#标题、**粗体**、*斜体*）、繁简转换、字号调整与页码跳转，详见「帮助」。</span>
  <button type="button" id="hintClose" title="关闭提示（可随时在「帮助」中查看）" aria-label="关闭提示">✕</button>
</div>
<div id="pages"></div>
<div id="popup"></div>
<div id="tip"></div>
<div id="searchModalBg"><div class="modal search-modal">
  <div class="search-head"><h3>搜索 / 替换</h3><button type="button" id="searchCloseBtn" class="x-btn" title="关闭搜索" aria-label="关闭搜索">✕</button></div>
  <div class="search-row">
    <input type="text" id="searchInput" placeholder="搜索词（可正则）">
    <label class="search-regex" title="勾选后按正则表达式搜索，否则按普通文本"><input type="checkbox" id="searchRegex">正则</label>
    <button type="button" id="searchBtn" class="primary">搜索</button>
  </div>
  <div class="search-row">
    <input type="text" id="replaceInput" placeholder="替换为">
    <button type="button" id="replaceBtn" title="替换当前选中的匹配（支持正则）">替换当前</button>
    <button type="button" id="replaceAllBtn" title="把当前搜索词在所有页面中替换为「替换为」的内容（支持正则）">全部替换</button>
  </div>
  <div class="search-nav">
    <button type="button" id="searchPrevBtn" title="上一个匹配" aria-label="上一个匹配">↑</button>
    <span id="searchPos"></span>
    <button type="button" id="searchNextBtn" title="下一个匹配" aria-label="下一个匹配">↓</button>
  </div>
  <div class="sr-head"><span id="srCount"></span></div>
  <div id="searchList"></div>
</div></div>
<div id="exportModalBg"><div class="modal search-modal">
  <div class="search-head"><h3>导出</h3><button type="button" id="exportCloseBtn" class="x-btn" title="关闭导出" aria-label="关闭导出">✕</button></div>
  <p class="export-desc">把全部页面的文字（含未保存的修改）导出为文本文件；点击下方按钮后弹出窗口选择保存位置。DOCX 中标题自动加粗加大，并带章节大纲。</p>
  <div class="export-actions">
    <button type="button" id="exportDocxBtn" title="导出为 Word 文档（.docx）">导出为 DOCX</button>
    <button type="button" id="exportTxtBtn" class="primary" title="导出为纯文本文件（.txt）">导出为 TXT</button>
  </div>
</div></div>
<div id="modalBg"><div class="modal">
  <h3>快捷键设置</h3>
  <p style="font-size:12px;color:#5a6b7c;">每个操作绑定一个组合键；点击某行后按下新组合键完成绑定，Del/Backspace 清除，Esc 取消。绑定保存在本浏览器（localStorage）。</p>
  <table id="shortcutTable"></table>
  <h4>提示延迟</h4>
  <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">鼠标悬停按钮超过设定时间（毫秒）才显示提示文字，提示中会附带对应快捷键；0 = 立即显示。</p>
  <label style="font-size:13px;">提示延迟（毫秒） <input type="number" id="tipDelayInput" min="0" max="5000" step="100" style="width:90px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;"></label>
  <button type="button" id="closeSettings">关闭</button>
</div></div>
<div id="finishModalBg"><div class="modal">
  <h3 id="finishTitle">转换完成</h3>
  <p id="finishMsg" style="font-size:14px;color:#33414f;">是否关闭当前页面？</p>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px;">
    <button type="button" id="closePageBtn">关闭页面</button>
    <button type="button" id="stayPageBtn" class="primary">留在本页</button>
  </div>
</div></div>
<div id="historyModalBg"><div class="modal" style="max-width:780px;">
  <h3>历史记录</h3>
  <p style="font-size:12px;color:#5a6b7c;">本地矫正缓存（同一文件保留多个版本，v1 为最新）。文件名与路径分列显示，同名不同路径的文件可区分；勾选后可删除（支持多选）。</p>
  <div style="max-height:50vh;overflow:auto;border:1px solid var(--border);border-radius:4px;margin-top:6px;">
    <table id="historyTable" style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead><tr style="text-align:left;color:#33414f;">
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);"><input type="checkbox" id="historyCheckAll" title="全选"></th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);">文件名</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);">文件路径</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);">版本</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);">更新时间</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);">操作</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;">
    <button type="button" id="historyDeleteBtn">删除选中</button>
    <button type="button" id="historyDeleteAllBtn">全部删除</button>
    <button type="button" id="historyCloseBtn" class="primary">关闭</button>
  </div>
</div></div>
<div id="helpModalBg"><div class="modal" style="max-width:680px;">
  <h3>帮助</h3>
  <h4>Markdown 格式（Markdown 模式）</h4>
  <table class="help-table">
    <tr><td>标题</td><td># 一级标题　## 二级标题　### 三级标题　……（1-6 级）</td></tr>
    <tr><td>粗体</td><td>**文字** 或 __文字__</td></tr>
    <tr><td>斜体</td><td>*文字* 或 _文字_</td></tr>
    <tr><td>行内代码</td><td>`代码`</td></tr>
    <tr><td>链接</td><td>[文字](网址)</td></tr>
    <tr><td>列表 / 引用 / 代码块</td><td>- 项目 / 1. 项目 / &gt; 引用 / ```代码块```；保存时按普通段落输出（保留文字与换行）</td></tr>
  </table>
  <p style="font-size:12px;color:#5a6b7c;">Markdown 模式下直接输入上述语法即可；「全文/段落/注释标记」以行内 HTML（&lt;span data-ptoe-marker=…&gt;）形式插入并原样保留。点击工具栏「Markdown模式」在源码与富文本之间切换。</p>
  <h4>选中文字后的快捷菜单（图标按钮）</h4>
  <p style="font-size:12px;color:#5a6b7c;">B=粗体，I=斜体，标=标题（循环 H1→H6→正文），正=正文，清=清除格式，注=注释格式（整段转为小字注释），左=居左，中=居中，右=居右，篇=全文标记（当前文章到此结束，后续内容属于新文章，生成 EPUB 时开新页），释=注释标记（插入到光标处，由对应注释段落替换，数量需一一匹配；自动加中文括号，已带括号的注释仅改字号），段=段落标记（段首=与上一段合并，段尾=与下一段合并），页=换页标记（从此处之后的内容显示在新的一页）。</p>
  <h4>快捷键</h4>
  <p style="font-size:12px;color:#5a6b7c;">默认：Ctrl+B 粗体、Ctrl+I 斜体、Ctrl+1 标题、Ctrl+0 正文、Ctrl+Shift+N 注释、Ctrl+Shift+←/↑/→ 居左/居中/居右、Ctrl+Shift+F 全文、Ctrl+Shift+M 注释标记、Ctrl+Shift+J 段落标记、Ctrl+Shift+P 换页标记。可在「快捷键」弹窗中修改（每个操作绑定一个组合键）。</p>
  <h4>其他功能</h4>
  <p style="font-size:12px;color:#5a6b7c;">「繁→简 / 简→繁」把全部页面文字整体转换并更新界面（转换后需保存）；「字号」调整正文与各级标题的显示大小（标题按正文比例缩放，手机上自动放大），设置保存在本浏览器；「跳转」输入页码直达指定页；「暂存」把当前修改存入本地历史（同 PDF 多版本，可在「历史记录」中查看/删除）；「保存」覆盖当前缓存（不新建版本）；「完成并转换」生成 EPUB（可重复点击）。</p>
  <button type="button" id="closeHelp" class="primary">关闭</button>
</div></div>
<script>
'use strict';
const BUFFER = 15, GAP = 12;
// 内存换平滑（2026-08）：滚动方向前方额外预挂载 PRELOAD 行，图片提前加载、行高提前稳定——
// 滚到那里时高度已测量，滚动中几乎不再触发补偿（减少跳变诱因）。代价：常驻 DOM/预览图内存略增（可接受）。
const PRELOAD = 15;
let _scrollDir = 1, _viewportY = 0, _lastLo = 0; // 最近一次滚动方向（1 向下 / -1 向上）、视口位置与上次窗口下界，供 updateViewport 动态预挂载/空白兜底
const OPS = [
  ['bold','粗体'], ['italic','斜体'], ['heading','标题'], ['p','正文'],
  ['remove','清除格式'], ['note','注释'],
  ['align_left','居左'], ['align_center','居中'], ['align_right','居右'],
  ['marker_full','全文标记'], ['marker_note','注释标记'], ['marker_join','段落标记'],
  ['marker_page','换页标记']
];
const OP_ICON = {
  bold:'<span class="ic-b">B</span>', italic:'<span class="ic-i">I</span>',
  heading:'<span class="ic-h">标</span>', p:'<span class="ic-p">正</span>',
  remove:'<span class="ic-t">清</span>', note:'注',
  align_left:'左', align_center:'中', align_right:'右',
  marker_full:'篇', marker_note:'释', marker_join:'段', marker_page:'页'
};
const OP_TIP = {
  bold:'粗体', italic:'斜体', heading:'标题（循环 H1→H6→正文）', p:'正文',
  remove:'清除格式', note:'注释格式（整段小字）',
  align_left:'居左', align_center:'居中', align_right:'居右',
  marker_full:'全文标记（文章到此结束，开新页）',
  marker_note:'注释标记（由对应注释段落替换）',
  marker_join:'段落标记（段首合上段，段尾合下段）',
  marker_page:'换页标记（从此处之后的内容显示在新的一页）'
};
const DEFAULTS = {
  bold:'Ctrl+B', italic:'Ctrl+I', heading:'Ctrl+1', p:'Ctrl+0',
  note:'Ctrl+Shift+N',
  align_left:'Ctrl+Shift+Left', align_center:'Ctrl+Shift+Up', align_right:'Ctrl+Shift+Right',
  marker_full:'Ctrl+Shift+F', marker_note:'Ctrl+Shift+M', marker_join:'Ctrl+Shift+J',
  marker_page:'Ctrl+Shift+P'
};
let pages = [];
let contentMap = new Map();     // index -> 该行最近一次 innerHTML（虚拟列表离屏保留）
let editedSet = new Set();
let dirty = false;
let mdMode = false;             // Markdown 源码模式
let mdSourceMap = new Map();    // index -> markdown 源码（仅 md 模式使用）
let loadNonce = 0;              // 历史版本载入计数：图片 URL 加 ?v= 防换书后缓存错图
let loadedTitle = null;         // 当前打开的历史记录名（无文件模式下作为 EPUB 标题）
const heights = new Array(0);
let est = 420;
let bindings = loadBindings();
const host = document.getElementById('pages');
const popup = document.getElementById('popup');
let capturingOp = null;
let suppressPopupUntil = 0;  // 操作按钮点击后的抑制窗口：选中菜单不再自动弹出
const tipEl = document.getElementById('tip');   // 全局延迟提示（悬停超时后显示）
let tipTimer = null;                             // 提示显示计时器
let tipAnchor = null;                            // 当前悬停元素（供定时器回调定位）

// 提示延迟（毫秒）：悬停超过该时间才显示提示文字；0 = 立即显示（localStorage 可配置）
function tipDelay() { return loadInt('ptoe_tip_delay', 600); }
// 提示文字：操作说明 + 对应快捷键（若有绑定）
function tipTextFor(op) {
  const combo = bindings[op];
  if (combo) return OP_TIP[op] + '<span class="tip-key">(' + combo + ')</span>';
  return OP_TIP[op];
}
function positionTip(anchor) {
  const r = anchor.getBoundingClientRect();
  const tw = tipEl.offsetWidth, th = tipEl.offsetHeight;
  let x = r.left + r.width / 2 - tw / 2;
  x = Math.max(4, Math.min(x, window.innerWidth - tw - 4));  // 防左右溢出
  let y = r.bottom + 8;
  if (y + th > window.innerHeight - 4) y = r.top - th - 8;   // 下方放不下则显示在按钮上方
  tipEl.style.left = x + 'px';
  tipEl.style.top = y + 'px';
}
function scheduleTip(e) {
  const anchor = e.currentTarget;
  const op = anchor.dataset.op;
  if (!op || !OP_TIP[op]) return;
  clearTimeout(tipTimer);
  tipAnchor = anchor;
  tipTimer = setTimeout(function () {
    if (!tipAnchor) return;
    tipEl.innerHTML = tipTextFor(op);
    tipEl.style.display = 'block';
    positionTip(tipAnchor);
  }, tipDelay());
}
function hideTip() {
  clearTimeout(tipTimer);
  tipAnchor = null;
  tipEl.style.display = 'none';
}

function loadBindings() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem('ptoe_shortcuts') || '{}'); } catch (e) {}
  return Object.assign({}, DEFAULTS, saved);
}
function saveBindings() { try { localStorage.setItem('ptoe_shortcuts', JSON.stringify(bindings)); } catch (e) {} }
function reverseBindings() { const m = {}; for (const op in bindings) if (bindings[op]) m[bindings[op]] = op; return m; }
function loadBool(key) { try { return localStorage.getItem(key) === '1'; } catch (e) { return false; } }
function saveStr(key, v) { try { localStorage.setItem(key, String(v)); } catch (e) {} }
function loadInt(key, def) { try { const v = parseInt(localStorage.getItem(key), 10); return isFinite(v) ? v : def; } catch (e) { return def; } }
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(url + ' -> ' + r.status);
  return r.json();
}

// ---------- Markdown 源码 <-> HTML ----------
function inlineToMd(t) {
  return String(t)
    .replace(/<strong>(.*?)<\/strong>/gi, function(m, x) { return '**' + x + '**'; })
    .replace(/<em>(.*?)<\/em>/gi, function(m, x) { return '*' + x + '*'; })
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'").replace(/&amp;/g, '&');
}
function htmlToMd(html) {
  // 已清洗 HTML（p/h1-6/strong/em/br/span）→ markdown 源码；标记 span 原样保留
  let s = String(html || '');
  s = s.replace(/<br\s*\/?>/gi, '\n');
  s = s.replace(/<h([1-6])([^>]*)>([\s\S]*?)<\/h\1>/gi, function(m, l, a, inner) {
    return new Array(Number(l) + 1).join('#') + ' ' + inlineToMd(inner).trim() + '\n\n';
  });
  s = s.replace(/<p([^>]*)>([\s\S]*?)<\/p>/gi, function(m, a, inner) { return inlineToMd(inner).trim() + '\n\n'; });
  s = inlineToMd(s);
  s = s.replace(/\n{3,}/g, '\n\n');
  return s.trim();
}
function inlineMd(t) {
  // 行内 Markdown → HTML：md 记号转标签，原样 HTML（标记 span）放行，
  // 其余文本转义（防止裸 < 破坏下游解析）
  const out = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*\s][^*]*\*)|(_[^_\s][^_]*_)|(\[[^\]]+\]\([^)]+\))|(<[^>]+>)/g;
  let last = 0, m;
  const s = String(t);
  while ((m = re.exec(s))) {
    if (m.index > last) out.push(esc(s.slice(last, m.index)));
    if (m[1]) out.push('<code>' + m[1].slice(1, -1) + '</code>');
    else if (m[2]) out.push('<strong>' + m[2].slice(2, -2) + '</strong>');
    else if (m[3]) out.push('<strong>' + m[3].slice(2, -2) + '</strong>');
    else if (m[4]) out.push('<em>' + m[4].slice(1, -1) + '</em>');
    else if (m[5]) out.push('<em>' + m[5].slice(1, -1) + '</em>');
    else if (m[6]) { const i2 = m[6].indexOf(']('); out.push('<a href="' + m[6].slice(i2 + 2, -1) + '">' + m[6].slice(1, i2) + '</a>'); }
    else if (m[7]) out.push(m[7]);
    last = m.index + m[0].length;
  }
  if (last < s.length) out.push(esc(s.slice(last)));
  return out.join('');
}
function mdToHtml(md) {
  // markdown 源码 → HTML（仅用现有白名单标签；列表/引用/代码块按段落输出）
  const lines = String(md || '').split('\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (/^```/.test(line)) {
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(esc(lines[i])); i++; }
      i++;
      out.push('<p>' + buf.join('<br/>') + '</p>');
      continue;
    }
    if (/^<(p|h[1-6]|div)(\s|>)/i.test(line)) { out.push(line); i++; continue; }
    let m = line.match(/^(#{1,6})\s+(.*)$/);
    if (m) { const lv = m[1].length; out.push('<h' + lv + '>' + inlineMd(m[2]) + '</h' + lv + '>'); i++; continue; }
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) { buf.push(inlineMd(lines[i].replace(/^>\s?/, ''))); i++; }
      out.push('<p>' + buf.join('<br/>') + '</p>');
      continue;
    }
    if (/^(\s*)([-*+]|\d+\.)\s+/.test(line)) {
      while (i < lines.length && /^(\s*)([-*+]|\d+\.)\s+/.test(lines[i])) {
        out.push('<p>' + inlineMd(lines[i].replace(/^(\s*)([-*+]|\d+\.)\s+/, '')) + '</p>');
        i++;
      }
      continue;
    }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^```/.test(lines[i]) && !/^#{1,6}\s/.test(lines[i]) && !/^>\s?/.test(lines[i]) && !/^(\s*)([-*+]|\d+\.)\s+/.test(lines[i]) && !/^<(p|h[1-6]|div)(\s|>)/i.test(lines[i])) {
      buf.push(inlineMd(lines[i]));
      i++;
    }
    out.push('<p>' + buf.join('<br/>') + '</p>');
  }
  return out.join('\n');
}
function editableSource(ed) {
  // 源码模式：取编辑区各子块/文本节点的纯文本（按行拼接）
  const lines = [];
  for (const c of ed.childNodes) {
    if (c.nodeType === 3) lines.push(c.textContent);
    else if (c.tagName === 'BR') lines.push('');
    else lines.push(c.textContent || '');
  }
  return lines.join('\n');
}
function displayHtml(i) {
  let base;
  if (!mdMode) base = contentMap.has(i) ? contentMap.get(i) : pages[i].text;
  else {
    const src = mdSourceMap.has(i) ? mdSourceMap.get(i) : htmlToMd(pages[i].text);
    base = String(src).split('\n').map(function(l) { return '<div>' + esc(l) + '</div>'; }).join('');
  }
  // If there's an active search highlight query, inject highlights into the
  // rendered HTML. Do NOT mutate underlying stored source (collect/pageSource
  // uses raw content). Regex validity already handled upstream; guard anyway.
  if (_searchHighlightQuery) {
    try {
      const re = searchRegexFor(_searchHighlightQuery);
      return _highlightInHtmlSource(base, re);
    } catch (e) {
      return base;
    }
  }
  return base;
}
function pageSource(i) {
  if (mdMode) return mdSourceMap.has(i) ? mdSourceMap.get(i) : htmlToMd(pages[i].text);
  return contentMap.has(i) ? contentMap.get(i) : pages[i].text;
}
function collect() {
  const out = [];
  for (let i = 0; i < pages.length; i++) {
    const src = pageSource(i);
    out.push({ page: pages[i].page, html: mdMode ? mdToHtml(src) : src });
  }
  return out;
}

// ---------- 虚拟列表 ----------
// P4：前缀高度数组 prefixH（prefixH[i] = 前 i 行累计高度），配合二分查找，
// 替代 O(n) 逐行累加 —— 滚动/布局每次 O(1)，千页级不卡顿。
const prefixH = [0];
function rebuildPrefix() {
  let s = 0;
  prefixH[0] = 0;
  for (let k = 0; k < pages.length; k++) {
    s += heights[k] || est;
    prefixH[k + 1] = s;
  }
}
function prefixTop(i) { return i <= 0 ? 0 : (prefixH[i] != null ? prefixH[i] : i * est); }
function totalHeight() { return prefixTop(pages.length); }

function updateStatus() {
  document.getElementById('status').textContent =
    '已编辑 ' + editedSet.size + '/' + pages.length + (dirty ? '（未保存）' : '');
}
function setStatus(s) { document.getElementById('status').textContent = s; }
function markDirty(i) {
  if (i >= 0 && !editedSet.has(i)) editedSet.add(i);
  dirty = true; updateStatus();
}
function syncContent(ed) {
  const row = ed.closest('.page-row');
  if (!row) return;
  const i = Number(row.dataset.i);
  if (mdMode) mdSourceMap.set(i, editableSource(ed));
  else contentMap.set(i, ed.innerHTML);
}
function currentEditable() {
  const a = document.activeElement;
  if (a && a.classList && a.classList.contains('editable')) return a;
  const sel = window.getSelection();
  if (sel && sel.rangeCount) {
    let n = sel.anchorNode;
    if (n) {
      if (n.nodeType !== 1) n = n.parentElement;
      const ed = n && n.closest ? n.closest('.editable') : null;
      if (ed) return ed;
    }
  }
  return null;
}

function pageRow(p, i) {
  const row = document.createElement('div');
  row.className = 'page-row';
  row.dataset.i = i;
  row.innerHTML =
    '<div class="page-head">第 ' + p.page + ' 页</div>' +
    '<div class="img-panel"><span class="badge">预览</span>' +
    '<button type="button" class="img-insert" title="插入图片到右侧文字光标处（居中；显示模式见工具栏「图片」）">图</button>' +
    '<img loading="lazy" decoding="async" src="/preview/' + p.page + '?v=' + loadNonce + '" alt="第' + p.page + '页原图"></div>' +
    '<div class="editable" contenteditable="true" spellcheck="false" aria-label="第 ' + p.page + ' 页文字" role="textbox" aria-multiline="true"></div>';
  const ed = row.querySelector('.editable');
  ed.innerHTML = displayHtml(i);
  ed.addEventListener('input', () => { syncContent(ed); markDirty(i); scheduleRemeasure(i); histTouchInput(i); histScheduleIdle(); });
  // 撤销/重做操作起点：beforeinput（现代浏览器，含 IME/粘贴/拖放/键盘）为主，
  // keydown（可打印键/退格/删除/回车）与 compositionstart/paste 作兼容兜底；
  // 均在 DOM 变更前触发，可捕获操作前快照。重复触发无副作用（幂等）。
  ed.addEventListener('beforeinput', () => { histBeginInput(i); });
  ed.addEventListener('keydown', (ev) => {
    if (ev.isComposing || ev.keyCode === 229) return;
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const k = ev.key;
    if (k === 'Backspace' || k === 'Delete' || k === 'Enter' || (k && k.length === 1)) histBeginInput(i);
  });
  ed.addEventListener('compositionstart', () => { histBeginInput(i); });
  ed.addEventListener('paste', () => { histBeginInput(i); });
  const insBtn = row.querySelector('.img-insert');
  insBtn.addEventListener('click', () => insertImage(row, i));
  const img = row.querySelector('img');
  const v = loadNonce;
  img.onload = () => scheduleRemeasure(i); // 图片加载完成：批量测量；行高变化即时补偿 scrollY，视口保持贴附（不跳页）
  // onerror 防循环：预览失败 → 尝试原图；原图也失败 → 显示「加载失败」不再请求
  img.onerror = () => {
    const badge = row.querySelector('.badge');
    if (!img.classList.contains('full')) {
      img.classList.add('full');
      img.src = '/full/' + p.page + '?v=' + v;
      if (badge) badge.textContent = '原图';
    } else if (badge) {
      badge.textContent = '加载失败';
    }
  };
  img.onclick = () => {
    const badge = row.querySelector('.badge');
    if (img.classList.contains('full')) {
      img.src = '/preview/' + p.page + '?v=' + v; img.classList.remove('full'); badge.textContent = '预览';
    } else {
      img.src = '/full/' + p.page + '?v=' + v; img.classList.add('full'); badge.textContent = '原图';
    }
  };
  return row;
}

function insertImage(row, i) {
  const img = row.querySelector('img');
  const ed = row.querySelector('.editable');
  if (!img || !ed) return;
  const mode = document.getElementById('imgModeSel').value;
  fetch(img.src)
    .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.blob(); })
    .then((blob) => new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve({ dataUrl: fr.result, size: blob.size });
      fr.onerror = () => reject(new Error('读取图片失败'));
      fr.readAsDataURL(blob);
    }))
    .then(({ dataUrl, size }) => {
      const html = '<p class="ptoe-img-' + mode + '"><img src="' + dataUrl + '" alt="插图"/></p>';
      const before = histBegin('插入图片', [i]);
      ed.focus();
      inDiscreteOp = true;
      try {
        withScrollStable(() => {
          if (mdMode) {
            document.execCommand('insertText', false, html);
          } else if (!document.execCommand('insertHTML', false, html)) {
            ed.appendChild(document.createElement('div')).innerHTML = html;
          }
        });
      } finally { inDiscreteOp = false; }
      syncContent(ed); markDirty(i); scheduleRemeasure(i);
      histEnd(before, '插入图片');
      if (size >= 2 * 1024 * 1024) {
        showToast('已插入图片（图片较大，保存/打包可能变慢）', 'warn');
      } else {
        showToast('已插入图片（居中，' + (mode === 'full' ? '全画幅' : '局部') + '显示）', 'ok');
      }
    })
    .catch((e) => showToast('插入图片失败：' + e.message, 'fail'));
}

const remeasurePending = new Map();
let _remeasureRaf = 0;
function scheduleRemeasure(i) {
  if (remeasurePending.has(i)) return;
  remeasurePending.set(i, true);
  if (_remeasureRaf) return;
  // 同一帧合并多行测量：全部处理后只重建/重排一次（原先每行一个 rAF + 每行 O(n) 重建）
  _remeasureRaf = requestAnimationFrame(() => {
    _remeasureRaf = 0;
    const items = [...remeasurePending.keys()];
    remeasurePending.clear();
    for (const idx of items) remeasure(idx, { deferLayout: true });
    rebuildPrefix();
    reposition();
  });
}

// 用户「有意」滚动的最后时间戳：wheel/touchmove 置位（手指/滚轮主动操作）。
// 程序性滚动还原（withScrollStable）只在用户未主动滚动时生效。
let lastUserScrollTs = 0;
// 任意滚动活动时间戳：再加 scroll 事件置位 —— 覆盖惯性滑动（touchmove 在
// 惯性期不触发）、滚动条拖拽、键盘翻页等。现仅服务 withScrollStable 还原判断。
let lastAnyScrollTs = 0;

// 滚动锚定：行高变化时，若该行整体位于视口上方，其高度变化会把下方
// 可见内容推下/拉上，需反向调整 scrollY，让视口内容保持贴附（不跳页）。
// （视口内的行自身高度变化由浏览器流式布局自然处理，无需补偿；
//   调用时机必须在 rebuildPrefix 之前——style.top/rect 仍是旧布局）
// 不做「滚动中不抢」门控：补偿与布局变化同帧原子生效（主线程顺序执行），
// 内容始终贴附视口——不会出现「滚动时滑走、停止后集中补偿」的累积-释放回弹。
// 判定必须用行当前的渲染位置（getBoundingClientRect，所见即所得），不能用
// 前缀估算：未测量行按 est 顶替会偏离真实布局（书内页高不均时偏差累积），
// 误判「位于视口上方」会对视口内/下方的行错误补偿 scrollY → 页面乱跳
// （向上滚时 180→190 / 196→180，2026-08 修复）。
function anchorScrollForHeightChange(i, oldH, newH) {
  if (oldH === newH) return;
  const row = host.querySelector('.page-row[data-i="' + i + '"]');
  if (!row) return;
  const rect = row.getBoundingClientRect();
  if (rect.bottom + 60 < 0) { // 行底部已整体滚出视口上方（留 60px 边距）
    window.scrollTo(0, Math.max(0, window.scrollY + (newH - oldH)));
  }
}

// 滚动稳定包装：execCommand 等操作可能触发浏览器自动滚动（跳到光标/
// 选中节点附近页），操作后把滚动位置还原（含 remeasure 之后二次修正）。
// 若操作期间用户已主动滚动，则不还原（避免把用户刚翻的页拉回来）。
function withScrollStable(fn) {
  const before = window.scrollY;
  const ts = lastUserScrollTs; // 操作开始时的用户滚动时间戳
  const restore = () => {
    if (lastUserScrollTs !== ts) return; // 期间用户滚动过：放弃还原
    const dy = window.scrollY - before;
    if (Math.abs(dy) > 2) window.scrollTo(0, Math.max(0, before));
  };
  try {
    const out = fn();
    requestAnimationFrame(restore);
    requestAnimationFrame(() => requestAnimationFrame(restore));
    return out;
  } catch (e) {
    requestAnimationFrame(restore);
    throw e;
  }
}

function measureRow(i) {
  const row = host.querySelector('.page-row[data-i="' + i + '"]');
  if (!row) return;
  const h = row.offsetHeight + GAP;
  // 无条件记录（含图片未就绪时的无图高度）：窗口内行高始终真实，不做 est
  // 顶替——否则未测量行按全局 est 估算，与已实测行错位（书内页高不均时
  // est 偏离累积），向上滚动时新挂载行与既有行重叠/间隙 → 视觉空白/乱跳
  // （2026-08 修复）。图片就绪后由 onload → scheduleRemeasure 修正高度并
  // 即时补偿 scrollY，视口保持贴附。
  if (h > 0) {
    heights[i] = h;
    est = Math.round((est * 3 + h) / 4);
  }
}
function attach(i, opts) {
  opts = opts || {};
  if (host.querySelector('.page-row[data-i="' + i + '"]')) return;
  const row = pageRow(pages[i], i);
  row.style.position = 'absolute';
  row.style.left = '14px'; row.style.right = '14px'; row.style.top = prefixTop(i) + 'px';
  row.style.margin = '0';
  host.appendChild(row);
  // 注意：不在 attach 里做滚动补偿——attach 发生在滚动驱动的 updateViewport 中，
  // 且此刻图片多为懒加载未就绪，测量高度偏小，补偿会与翻页手势互相打架。
  // 图片未就绪的行不记录「仅文字」高度：heights[i] 保持未设（走 est 估算），
  // 否则总高度被低估 → 浏览器滚动钳制把 scrollY 回拉（= 滚动中回滚）。
  if (opts.defer) return; // 批量路径：由 updateViewport 统一测量/重建前缀/重排（避免每行 O(n) 重建 + 强制 reflow）
  measureRow(i);
  rebuildPrefix();
  reposition();
}
function reposition() {
  for (const row of host.children) {
    const i = Number(row.dataset.i);
    const t = prefixTop(i);
    const cur = parseFloat(row.style.top) || 0; // 不读 offsetTop：避免逐行强制 reflow
    if (Math.abs(cur - t) > 1) row.style.top = t + 'px';
  }
  // 防滚动钳制：估算总高度被低估时，若小于当前滚动范围+视口，浏览器会把
  // scrollY 钳制回拉（表现=滚动中回滚）。下限设为滚动范围+缓冲，可滚动高度
  // 绝不在滚动过程中收缩；真实高度由 remeasure（图片加载/编辑）收敛后接管。
  host.style.height = Math.max(totalHeight(), window.scrollY + window.innerHeight + BUFFER * GAP) + 'px';
}
function remeasure(i, opts) {
  opts = opts || {};
  const row = host.querySelector('.page-row[data-i="' + i + '"]');
  if (!row) return;
  const h = row.offsetHeight + GAP;
  if (h <= 0) return;
  const old = heights[i] || est; // 未记录过（图片未就绪）时，此前按 est 估算
  heights[i] = h;
  est = Math.round((est * 3 + h) / 4);
  // 行高变化（编辑/撤销/图片加载）统一即时滚动补偿：仅当行整体位于视口上方时
  // 补偿，且与布局变化同帧原子生效 → 视口内容贴附，滚动/编辑都不再跳页。
  anchorScrollForHeightChange(i, old, h);
  if (!opts.deferLayout) { rebuildPrefix(); reposition(); } // 批量路径由调用方统一重建/重排
}
function updateViewport() {
  const sy = window.scrollY;
  if (sy !== _viewportY) { _scrollDir = sy > _viewportY ? 1 : -1; _viewportY = sy; } // 动态方向感知
  const hostTop = host.getBoundingClientRect().top + window.scrollY;
  const y = Math.max(0, window.scrollY - hostTop - 60);
  let lo = 0, hi = pages.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (prefixTop(mid) < y) lo = mid + 1; else hi = mid; }
  const viewRows = Math.ceil((window.innerHeight - 140) / (est || 420)) + 1;
  // 空白防护（2026-08）：scroll-lasso 允许 scrollY 超过内容总高（reposition 把
  // host 高度下限设为 scrollY+innerHeight+BUFFER*GAP）——用户滚过内容尾部进入
  // 空滚区时，二分 lo 钳到末尾、挂载的行渲染顶远在视口上方 → 视口空白且下方
  // 已挂载行会被 detach。此时回退上一窗口下界 _lastLo：保留用户刚看的窗口，
  // 绝不彻底空白；用户滚回内容区即恢复（二分回到真实位置）。正常滚动时
  // scrollY ≤ totalHeight()+innerHeight，不受影响。
  if (window.scrollY > totalHeight() + window.innerHeight) {
    lo = Math.min(lo, _lastLo);
  }
  const first = Math.max(0, lo - BUFFER - (_scrollDir < 0 ? PRELOAD : 0));
  let last = Math.min(pages.length, lo + viewRows + BUFFER + (_scrollDir > 0 ? PRELOAD : 0));
  const keep = new Set();
  const toAttach = [];
  for (let i = first; i < last; i++) {
    keep.add(i);
    if (!host.querySelector('.page-row[data-i="' + i + '"]')) toAttach.push(i);
  }
  for (const i of toAttach) attach(i, { defer: true });
  for (const i of toAttach) measureRow(i); // 全部挂载后再统一测量：一次布局，避免逐行强制 reflow
  for (const row of [...host.children]) {
    const i = Number(row.dataset.i);
    if (!keep.has(i)) {
      const ed = row.querySelector('.editable');
      if (ed) syncContent(ed);
      row.remove();
    }
  }
  _lastLo = lo;    // 记录本次窗口下界：下次无行在视口附近时兜底（防止彻底空白）
  rebuildPrefix(); // 批量：挂载/卸载结束后只重建一次前缀（原先每行都重建）
  reposition();    // 批量：只重排一次
}
// ---------- 多行/多块选择辅助与格式应用 ----------
function _blocksBetween(ed, startBlock, endBlock) {
  const blocks = [];
  let walker = document.createTreeWalker(ed, NodeFilter.SHOW_ELEMENT, {
    acceptNode: function(n) {
      const tag = n.tagName;
      return /^(P|DIV|H[1-6])$/.test(tag) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
    }
  });
  let cur = walker.nextNode();
  let started = false;
  // 选区起点/终点可能是 .editable 直属文本节点（startBlock/endBlock 回退为 ed）：
  // 此时 walker 遍历从 ed 的子节点开始，cur 永不 === ed，若仍以 startBlock===ed 判定
  // 会导致 blocks 恒为空、多块格式化全部失效——这里把 startBlock 置空改从首块收集。
  if (startBlock === ed) startBlock = null;
  while (cur) {
    if (!startBlock || cur === startBlock) started = true;
    if (started) blocks.push(cur);
    if (cur === endBlock) break;
    cur = walker.nextNode();
  }
  return blocks;
}

function applyToSelectedBlocks(ed, fn) {
  // If IME composition in progress, queue operation to run after compositionend
  if (typeof isComposing !== 'undefined' && isComposing) {
    _pendingOps.push(() => applyToSelectedBlocks(ed, fn));
    showToast('输入法中，已将操作排队，输入结束后自动应用', 'warn');
    return [];
  }
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) { return []; }
  const origRanges = [];
  for (let i = 0; i < sel.rangeCount; i++) origRanges.push(sel.getRangeAt(i).cloneRange());
  const range = sel.getRangeAt(0);
  const startNode = range.startContainer.nodeType === 3 ? range.startContainer.parentElement : range.startContainer;
  const endNode = range.endContainer.nodeType === 3 ? range.endContainer.parentElement : range.endContainer;
  const startBlock = (startNode && startNode.closest) ? (startNode.closest('p,div,h1,h2,h3,h4,h5,h6') || ed) : ed;
  const endBlock = (endNode && endNode.closest) ? (endNode.closest('p,div,h1,h2,h3,h4,h5,h6') || ed) : ed;
  const blocks = _blocksBetween(ed, startBlock, endBlock);
  for (const block of blocks) {
    try {
      const r = document.createRange();
      if (block === startBlock) r.setStart(range.startContainer, range.startOffset);
      else r.setStart(block, 0);
      if (block === endBlock) r.setEnd(range.endContainer, range.endOffset);
      else r.setEnd(block, block.childNodes.length);
      sel.removeAllRanges();
      sel.addRange(r);
      fn(block, r);
    } catch (e) {
      // best-effort: skip problematic block
      continue;
    }
  }
  // restore original selection
  sel.removeAllRanges();
  for (const rr of origRanges) sel.addRange(rr);
  return blocks;
}
// Capture basic inline/block formatting attributes from selection (for 格式刷)
let _formatBrush = null;
let _brushBefore = null; // aggregated history snapshot for persistent brush
function _convertBlockTag(block, newTag) {
  if (!block || !block.parentNode) return block;
  const newEl = document.createElement(newTag);
  // copy safe attributes: class/id/data-*/aria-*
  for (let i = 0; i < block.attributes.length; i++) {
    const a = block.attributes[i];
    const n = a.name.toLowerCase();
    if (n === 'class') {
      newEl.className = block.className; // preserve all classes
    } else if (n === 'id' || n.startsWith('data-') || n.startsWith('aria-')) {
      try { newEl.setAttribute(a.name, a.value); } catch (e) {}
    }
  }
  newEl.innerHTML = block.innerHTML;
  block.parentNode.replaceChild(newEl, block);
  return newEl;
}

function toggleNote(ed) {
  // Toggle ptoe-note on all blocks in selection
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  histRun('注释格式', [i], function () {
    applyToSelectedBlocks(ed, function(block) {
      block.classList.toggle('ptoe-note');
    });
    syncContent(ed);
    if (row) { markDirty(i); scheduleRemeasure(i); }
  });
}

function cycleHeading(ed) {
  // Apply heading level cycling per selected block
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  histRun('标题', [i], function () {
    applyToSelectedBlocks(ed, function(block) {
      const tag = block.tagName.toLowerCase();
      let next;
      if (tag === 'p' || tag === 'div') next = 'h1';
      else if (/^h[1-5]$/.test(tag)) next = 'h' + (parseInt(tag[1], 10) + 1);
      else next = 'p';
      _convertBlockTag(block, next);
    });
    syncContent(ed);
    if (row) { markDirty(i); scheduleRemeasure(i); }
  });
}
function applyAlign(ed, pos) {
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  histRun('对齐', [i], function () {
    applyToSelectedBlocks(ed, function(block) {
      block.classList.remove('ptoe-align-left', 'ptoe-align-center', 'ptoe-align-right');
      block.classList.add('ptoe-align-' + pos);
    });
    syncContent(ed);
    if (row) { markDirty(i); scheduleRemeasure(i); }
  });
}

function applyFormatBrushToSelection(format) {
  const ed = currentEditable();
  if (!ed || !format) return;
  // block classes
  applyToSelectedBlocks(ed, function(block, r) {
    if (format.blockClasses && format.blockClasses.length) {
      for (const c of ['ptoe-note','ptoe-align-left','ptoe-align-center','ptoe-align-right']) block.classList.remove(c);
      for (const c of format.blockClasses) block.classList.add(c);
    }
    // inline: bold/italic/color
    if (format.bold) withScrollStable(() => document.execCommand('bold'));
    if (format.italic) withScrollStable(() => document.execCommand('italic'));
    if (format.color) withScrollStable(() => document.execCommand('foreColor', false, format.color));
    const row = ed.closest('.page-row'); if (row) { markDirty(Number(row.dataset.i)); scheduleRemeasure(Number(row.dataset.i)); }
  });
}
function captureFormatFromSelection() {
  const ed = currentEditable();
  if (!ed) return null;
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  let node = range.commonAncestorContainer;
  if (node.nodeType === 3) node = node.parentElement;
  const block = (node && node.closest ? node.closest('p,div,h1,h2,h3,h4,h5,h6') : null) || ed;
  const fmt = { blockClasses: [], bold: false, italic: false, color: null };
  if (block && block.classList) {
    for (const c of ['ptoe-note','ptoe-align-left','ptoe-align-center','ptoe-align-right']) {
      if (block.classList.contains(c)) fmt.blockClasses.push(c);
    }
  }
  try { fmt.bold = document.queryCommandState('bold'); } catch (e) {}
  try { fmt.italic = document.queryCommandState('italic'); } catch (e) {}
  try { fmt.color = window.getComputedStyle(block).color; } catch (e) {}
  return fmt;
}
function applyOp(op) { const ed = currentEditable(); if (!ed) return;
  if (op.indexOf('marker_') === 0) { insertMarker(op); return; }
  if (op === 'note') { toggleNote(ed); return; }
  if (op === 'heading') { cycleHeading(ed); return; }
  if (op.indexOf('align_') === 0) { applyAlign(ed, op.slice(6)); return; }
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  histRun(OP_TIP[op] || op, [i], function () {
    // For multi-block selections, apply command per-block to guarantee
    // the change propagates to all selected lines/blocks.
    if (op === 'bold') applyToSelectedBlocks(ed, function() { withScrollStable(() => document.execCommand('bold')); });
    else if (op === 'italic') applyToSelectedBlocks(ed, function() { withScrollStable(() => document.execCommand('italic')); });
    else if (op === 'remove') applyToSelectedBlocks(ed, function() { withScrollStable(() => document.execCommand('removeFormat')); });
    else if (op === 'p') applyToSelectedBlocks(ed, function(block) { _convertBlockTag(block, 'p'); }); // 与 heading 一致逐块转换（execCommand formatBlock 对跨块选区只转起始块）
    syncContent(ed);
    if (row) { markDirty(i); scheduleRemeasure(i); }
  });
}
function insertMarkerAtCaret(block, span, range) {
  range.collapse(false);      // 只保留光标插入点
  range.insertNode(span);     // 终点在文本节点内时 insertNode 会自动切分文本节点
  range.setStartAfter(span);  // 光标移到标记之后，便于连续插入
  range.collapse(true);
}

function insertMarker(op) {
  const ed = currentEditable();
  if (!ed) return;
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  let type, label;
  if (op === 'marker_full') { type = 'full'; label = '全文'; }
  else if (op === 'marker_note') { type = 'note'; label = '注释'; }
  else if (op === 'marker_join') { type = 'join'; label = '段落'; }
  else if (op === 'marker_page') { type = 'page'; label = '换页'; }
  else return;
  if (mdMode) {
    // Markdown 源码模式：以行内 HTML 文本形式插入（md 转 html 时原样放行）
    histRun('标记', [i], function () {
      withScrollStable(() => document.execCommand('insertText', false, '<span data-ptoe-marker="' + type + '">' + label + '</span>'));
      syncContent(ed);
      markDirty(i);
      scheduleRemeasure(i);
    });
    return;
  }
  const sel = window.getSelection();
  const range = sel && sel.rangeCount ? sel.getRangeAt(0) : null;
  let node = range ? range.endContainer : ed;
  if (node.nodeType === 3) node = node.parentElement;
  let block = node && node.closest ? node.closest('p,div,h1,h2,h3,h4,h5,h6') : null;
  if (!block || !ed.contains(block)) block = ed;
  const span = document.createElement('span');
  span.className = 'ptoe-marker';
  span.dataset.ptoeMarker = type;
  span.textContent = label;
  histRun('标记', [i], function () {
    if (range && block.contains(range.endContainer)) {
      insertMarkerAtCaret(block, span, range);
    } else {
      block.appendChild(span);
    }
    ed.focus();
    syncContent(ed);
    markDirty(i);
    scheduleRemeasure(i);
  });
}

// ---------- 繁简转换 / Markdown 切换 / 字号 / 跳转 ----------
async function convertAll(mode) {
  const btn = mode === 's2t' ? document.getElementById('toTraditionBtn') : document.getElementById('toSimplifiedBtn');
  btn.disabled = true;
  try {
    const before = histBegin('繁简转换', null); // 全页快照；histEnd 只保留实际变化页
    const res = await fetchJSON('/api/convert', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: mode, pages: collect() })
    });
    const converted = res.pages || [];
    for (const it of converted) {
      let idx = -1;
      for (let i = 0; i < pages.length; i++) { if (pages[i].page === it.page) { idx = i; break; } }
      if (idx < 0) continue;
      if (mdMode) mdSourceMap.set(idx, htmlToMd(it.html));
      else contentMap.set(idx, it.html);
    }
    for (const row of [...host.children]) {
      const i = Number(row.dataset.i);
      const ed = row.querySelector('.editable');
      if (ed) { ed.innerHTML = displayHtml(i); remeasure(i); }
    }
    histEnd(before, '繁简转换');
    for (let i = 0; i < pages.length; i++) editedSet.add(i);
    dirty = true; updateStatus();
    setStatus('已完成' + (mode === 's2t' ? '简体→繁体' : '繁体→简体') + '转换，请保存');
  } catch (e) { setStatus('转换失败: ' + e); }
  finally { btn.disabled = false; }
}
function setMdMode(on) {
  if (!!on === mdMode) return;
  if (on) {
    for (let i = 0; i < pages.length; i++) {
      mdSourceMap.set(i, htmlToMd(contentMap.has(i) ? contentMap.get(i) : pages[i].text));
    }
  } else {
    for (let i = 0; i < pages.length; i++) {
      contentMap.set(i, mdToHtml(mdSourceMap.has(i) ? mdSourceMap.get(i) : htmlToMd(pages[i].text)));
    }
    mdSourceMap.clear();
  }
  mdMode = !!on;
  histClear(); // 模式切换后快照源（md/富文本）不一致，撤销/重做历史失效
  saveStr('ptoe_md_mode', mdMode ? '1' : '0');
  const btn = document.getElementById('mdToggleBtn');
  btn.textContent = mdMode ? '富文本模式' : 'Markdown模式';
  btn.classList.toggle('active', mdMode);
  const keep = [...host.children].map(r => Number(r.dataset.i));
  host.innerHTML = '';
  for (const i of keep) attach(i);
  for (const i of keep) attach(i);
  setStatus(mdMode ? '已切换为 Markdown 源码模式（保存时按 Markdown 转 HTML）' : '已切换为富文本模式');
  // IME composition guard and pending ops queue
  window.isComposing = false;
  window._pendingOps = [];
  function _flushPendingOps() { while (window._pendingOps.length) { const f = window._pendingOps.shift(); try { f(); } catch (e) { console.error('pending op failed', e); } } }
  document.addEventListener('compositionstart', () => { window.isComposing = true; });
  document.addEventListener('compositionend', () => { window.isComposing = false; setTimeout(_flushPendingOps, 0); });
}
function jumpToPage() {
  const v = parseInt(document.getElementById('pageJump').value, 10);
  if (!v) { setStatus('请输入页码'); return; }
  let idx = -1;
  for (let i = 0; i < pages.length; i++) { if (pages[i].page === v) { idx = i; break; } }
  if (idx < 0) { setStatus('未找到第 ' + v + ' 页'); return; }
  const hostTop = host.getBoundingClientRect().top + window.scrollY;
  window.scrollTo({ top: Math.max(0, hostTop + prefixTop(idx) - 60), behavior: 'smooth' });
  hidePopup();
  setStatus('已跳转到第 ' + v + ' 页');
}

// ---------- 智能清理 / 搜索替换 ----------
async function cleanAll() {
  // 逐页调用 /api/clean：段落合并、段首符号、中英文标点、残留 HTML 标签
  try {
    const before = histBegin('智能清理', null); // 全页快照；histEnd 只保留实际变化页
    const res = await fetchJSON('/api/clean', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pages: collect() })
    });
    const cleaned = res.pages || [];
    for (const it of cleaned) {
      let idx = -1;
      for (let i = 0; i < pages.length; i++) { if (pages[i].page === it.page) { idx = i; break; } }
      if (idx < 0) continue;
      if (mdMode) mdSourceMap.set(idx, htmlToMd(it.html));
      else contentMap.set(idx, it.html);
    }
    for (const row of [...host.children]) {
      const idx = Number(row.dataset.i);
      const ed = row.querySelector('.editable');
      if (ed) { ed.innerHTML = displayHtml(idx); scheduleRemeasure(idx); }
    }
    histEnd(before, '智能清理');
    for (let i = 0; i < pages.length; i++) editedSet.add(i);
    dirty = true;
    updateStatus();
    setStatus('已清理 ' + cleaned.length + ' 页');
    showToast('已清理 ' + cleaned.length + ' 页（段落合并 / 段首符号 / 标点 / 标签）', 'ok');
  } catch (e) {
    showToast('清理失败: ' + e.message, 'fail');
  }
}

// 搜索结果状态：searchResults 为当前结果列表（上限 200 条），searchCurrent 为
// 当前选中序号（用于上一个/下一个跳转与「替换当前」）
let searchResults = [];
let searchCurrent = -1;

function pageText(i) {
  // 搜索用的纯文本：与 replaceAll/replaceCurrent 完全相同的 token 切分
  // （标签之间按原文，含实体），保证搜索序号与替换位置一一对应
  return (pageSource(i) || '').split(/(<[^>]+>)/).filter(function (t) { return t && t.charAt(0) !== '<'; }).join('');
}

function decodeEntities(s) {
  // 仅用于结果预览显示：把页面源码里的实体还原成可读文本
  return String(s).replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'");
}

function searchRegexFor(query) {
  const regexMode = document.getElementById('searchRegex').checked;
  const q = regexMode ? query : query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(q, regexMode ? 'gimu' : 'giu');
}

function updateSearchNav() {
  const pos = document.getElementById('searchPos');
  if (pos) pos.textContent = searchResults.length ? (searchCurrent + 1) + ' / ' + searchResults.length : '';
  const list = document.getElementById('searchList');
  if (list) {
    for (let k = 0; k < list.children.length; k++) list.children[k].classList.toggle('current', k === searchCurrent);
  }
}

function renderSearchResults(results, total, MAX) {
  const list = document.getElementById('searchList');
  const count = document.getElementById('srCount');
  count.textContent = '共 ' + total + ' 处匹配' + (total > MAX ? '，仅显示前 ' + MAX + ' 条' : '');
  list.innerHTML = '';
  if (!results.length) {
    list.innerHTML = '<div class="sr-empty">未找到匹配内容</div>';
    return;
  }
  results.forEach(function (r, k) {
    const item = document.createElement('div');
    item.className = 'sr-item';
    item.innerHTML = '<div class="sr-page">第 ' + r.page + ' 页</div><div class="sr-ctx">' + r.ctx + '</div>';
    item.addEventListener('click', function () {
      searchCurrent = k;
      updateSearchNav();
      scrollToIndex(r.i);
    });
    list.appendChild(item);
  });
}

function searchPages() {
  const query = document.getElementById('searchInput').value;
  const list = document.getElementById('searchList');
  if (!query) { showToast('请先输入搜索词', 'warn'); return; }
  let re;
  try { re = searchRegexFor(query); }
  catch (e) { showToast('正则表达式无效：' + e.message, 'fail'); return; }
  // 性能：一次遍历统计总数，超出 MAX 只存储前 MAX 条（列表仍显示真实总数）
  const CONTEXT = 40, MAX = 200;
  const results = [];
  let total = 0, pageStart = 0;
  for (let i = 0; i < pages.length; i++) {
    const text = pageText(i);
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      const withinPage = total - pageStart; // 本页内第几处（0 起）
      const pageOrd = total;                // 全局第几处（0 起）
      total++;
      if (results.length >= MAX) continue;
      const s = Math.max(0, m.index - CONTEXT);
      const e2 = Math.min(text.length, m.index + m[0].length + CONTEXT);
      results.push({
        i: i, page: pages[i].page, pageOrd: pageOrd, withinPage: withinPage,
        ctx: esc(decodeEntities(text.slice(s, m.index))) + '<mark>' + esc(decodeEntities(m[0])) + '</mark>' + esc(decodeEntities(text.slice(m.index + m[0].length, e2)))
      });
    }
    pageStart = total; // 下一页匹配的起点
  }
  searchResults = results;
  searchCurrent = results.length ? 0 : -1;
  renderSearchResults(results, total, MAX);
  updateSearchNav();
  // Highlight matches in visible pages for quick preview
  applySearchHighlights();
  openSearchModal();
  if (total === 0) showToast('未找到匹配内容', 'warn');
}
// ---------- 搜索高亮（在编辑区预览中高亮，输入为空时去高亮） ----------
let _searchHighlightQuery = '';
function debounce(fn, ms) {
  let t = null;
  return function(...args) {
    clearTimeout(t);
    t = setTimeout(() => fn.apply(this, args), ms);
  };
}

function _highlightInHtmlSource(html, re) {
  // Split by tags; only replace in text tokens to avoid touching attributes
  return String(html).split(/(<[^>]+>)/).map(function(tok) {
    if (!tok) return '';
    if (tok.charAt(0) === '<') return tok;
    return tok.replace(re, function(m) { return '<mark class="ptoe-search">' + esc(m) + '</mark>'; });
  }).join('');
}

function applySearchHighlights() {
  const q = (document.getElementById('searchInput').value || '').trim();
  if (!q) { clearSearchHighlights(); _searchHighlightQuery = ''; return; }
  if (q === _searchHighlightQuery) return; // avoid redundant work
  let re;
  try { re = searchRegexFor(q); } catch (e) { return; }
  _searchHighlightQuery = q;
  // Only process attached rows (virtual list) for performance
  for (const row of host.children) {
    const idx = Number(row.dataset.i);
    const ed = row.querySelector('.editable');
    if (!ed) continue;
    // avoid replacing while user is editing to preserve caret
    if (ed === document.activeElement || ed.contains(document.activeElement)) continue;
    const src = displayHtml(idx);
    const highlighted = _highlightInHtmlSource(src, re);
    if (highlighted !== ed.innerHTML) {
      ed.innerHTML = highlighted;
      scheduleRemeasure(idx);
    }
  }
}

function clearSearchHighlights() {
  for (const row of host.children) {
    const idx = Number(row.dataset.i);
    const ed = row.querySelector('.editable');
    if (!ed) continue;
    if (ed === document.activeElement || ed.contains(document.activeElement)) continue;
    const src = displayHtml(idx);
    if (ed.innerHTML.indexOf('ptoe-search') !== -1) {
      ed.innerHTML = src;
      scheduleRemeasure(idx);
    }
  }
}

// Debounced input handler for live highlighting
const _applySearchHighlightsDebounced = debounce(applySearchHighlights, 200);
document.getElementById('searchInput').addEventListener('input', _applySearchHighlightsDebounced);

async function exportFile(fmt) {
  try {
    // 兜底：先同步当前编辑框内容到 map，确保导出的是最新内容
    const ed = currentEditable();
    if (ed) syncContent(ed);
    const res = await fetchJSON('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ format: fmt, pages: collect() }),
    });
    if (res.cancelled) { setStatus('已取消导出'); return; }
    if (!res.ok) { showToast('导出失败：' + (res.error || '未知错误'), 'fail'); return; }
    showToast('导出成功：' + res.path, 'ok');
    setStatus('已导出：' + res.path);
  } catch (e) {
    showToast('导出失败：' + e.message, 'fail');
  }
}
function gotoMatch(dir) {
  if (!searchResults.length) return;
  searchCurrent = (searchCurrent + dir + searchResults.length) % searchResults.length;
  updateSearchNav();
  const cur = searchResults[searchCurrent];
  const list = document.getElementById('searchList');
  if (list.children[searchCurrent]) list.children[searchCurrent].scrollIntoView({ block: 'nearest' });
  scrollToIndex(cur.i);
}
function replaceCurrent() {
  // 只替换当前选中的那处匹配（第 searchCurrent 条），其余匹配保持不动
  if (!searchResults.length || searchCurrent < 0) { showToast('请先搜索', 'warn'); return; }
  const query = document.getElementById('searchInput').value;
  const repl = document.getElementById('replaceInput').value;
  let re;
  try { re = searchRegexFor(query); }
  catch (e) { showToast('正则表达式无效：' + e.message, 'fail'); return; }
  const cur = searchResults[searchCurrent];
  const i = cur.i;
  const target = cur.withinPage; // 该页内第 target 处（0 起）
  const before = histBegin('替换当前', [i]);
  const src = pageSource(i);
  let out, c = 0;
  if (mdMode) {
    out = src.replace(re, function (m) { const n = c++; return (n === target) ? repl : m; });
  } else {
    out = src.split(/(<[^>]+>)/).map(function (tok) {
      if (tok.charAt(0) === '<') return tok;
      return tok.replace(re, function (m) { const n = c++; return (n === target) ? repl : m; });
    }).join('');
  }
  if (c <= target) { showToast('该处匹配已变化，请重新搜索', 'warn'); return; }
  if (mdMode) mdSourceMap.set(i, out);
  else contentMap.set(i, out);
  const row = host.querySelector('.page-row[data-i="' + i + '"]');
  const ed = row && row.querySelector('.editable');
  if (ed) { ed.innerHTML = displayHtml(i); scheduleRemeasure(i); }
  editedSet.add(i);
  dirty = true;
  updateStatus();
  histEnd(before, '替换当前');
  showToast('已替换当前匹配（第 ' + (searchCurrent + 1) + ' 条）', 'ok');
  searchPages(); // 替换后刷新结果列表与序号
}

function scrollToIndex(idx) {
  const hostTop = host.getBoundingClientRect().top + window.scrollY;
  window.scrollTo({ top: Math.max(0, hostTop + prefixTop(idx) - 60), behavior: 'smooth' });
  hidePopup();
}

function replaceAll() {
  const query = document.getElementById('searchInput').value;
  const repl = document.getElementById('replaceInput').value;
  if (!query) { showToast('请先输入搜索词', 'warn'); return; }
  let re;
  try { re = searchRegexFor(query); }
  catch (e) { showToast('正则表达式无效：' + e.message, 'fail'); return; }
  const changed = [];
  let count = 0;
  const before = histBegin('全部替换', null); // 全页快照；histEnd 只保留实际变化页
  for (let i = 0; i < pages.length; i++) {
    const src = pageSource(i);
    let out, c = 0;
    if (mdMode) {
      // Markdown 源码：直接整体替换
      out = src.replace(re, function () { c++; return repl; });
    } else {
      // 富文本：只替换标签之间的文本 token，不触碰标签/属性，避免破坏 HTML 结构
      out = src.split(/(<[^>]+>)/).map(function (tok) {
        if (tok.charAt(0) === '<') return tok;
        return tok.replace(re, function () { c++; return repl; });
      }).join('');
    }
    if (c > 0) changed.push({ i: i, out: out });
    count += c;
  }
  if (count === 0) { showToast('未找到匹配内容，未替换', 'warn'); return; }
  for (const ch of changed) {
    if (mdMode) mdSourceMap.set(ch.i, ch.out);
    else contentMap.set(ch.i, ch.out);
  }
  for (const row of [...host.children]) {
    const idx = Number(row.dataset.i);
    const ed = row.querySelector('.editable');
    if (ed) { ed.innerHTML = displayHtml(idx); scheduleRemeasure(idx); }
  }
  for (let i = 0; i < pages.length; i++) editedSet.add(i);
  dirty = true;
  updateStatus();
  histEnd(before, '全部替换');
  showToast('已替换 ' + count + ' 处', 'ok');
  searchPages(); // 替换后刷新结果列表（匹配数可能变化）
}

// ---------- 保存 / 完成 ----------
// U2：三色 toast（ok 成功 / fail 失败 / warn 警告），顶部居中，3s 自动消失
function showToast(msg, kind) {
  let wrap = document.getElementById('toast');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.id = 'toast';
    document.body.appendChild(wrap);
  }
  const t = document.createElement('div');
  t.className = 'toast' + (kind ? ' ' + kind : '');
  t.textContent = msg;
  wrap.appendChild(t);
  requestAnimationFrame(function () { t.classList.add('show'); });
  setTimeout(function () {
    t.classList.remove('show');
    setTimeout(function () { t.remove(); }, 250);
  }, 3000);
}
async function save() {
  const btn = document.getElementById('saveBtn');
  btn.disabled = true;
  try {
    const res = await fetchJSON('/api/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pages: collect() })
    });
    if (!res || res.ok === false) throw new Error((res && res.error) || '保存失败');
    dirty = false;
    setStatus('已保存 ' + res.saved + ' 页，' + new Date().toLocaleTimeString());
    showToast('已保存 ' + res.saved + ' 页', 'ok');
  } catch (e) {
    setStatus('保存失败: ' + e);
    showToast('保存失败：' + e, 'fail');
  }
  finally { btn.disabled = false; }
}
async function stage() {
  const btn = document.getElementById('stageBtn');
  btn.disabled = true;
  try {
    const res = await fetchJSON('/api/stage', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pages: collect() })
    });
    if (!res || res.ok === false) throw new Error((res && res.error) || '暂存失败');
    dirty = false;
    setStatus('已暂存 ' + res.saved + ' 页到本地历史，' + new Date().toLocaleTimeString());
    showToast('已暂存 ' + res.saved + ' 页', 'ok');
  } catch (e) {
    setStatus('暂存失败: ' + e);
    showToast('暂存失败：' + e, 'fail');
  }
  finally { btn.disabled = false; }
}
async function finish() {
  const btn = document.getElementById('finishBtn');
  btn.disabled = true;
  btn.classList.add('loading');
  setStatus('正在提交并生成 EPUB，请稍候 ...');
  let res = null, ok = false;
  try {
    res = await fetchJSON('/api/finish', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pages: collect(), name: loadedTitle || undefined })
    });
    ok = !!(res && res.ok);
  } catch (e) { /* 服务端异常；是否成功以响应为准 */ }
  btn.classList.remove('loading');
  const conv = res && res.converted;
  if (ok && conv && conv.ok) {
    setStatus('转换完成，等待确认');
    showToast('转换完成', 'ok');
    showFinishModal('done', conv.message);
  } else if (ok && conv && !conv.ok) {
    btn.disabled = false;
    setStatus('转换未完成：' + (conv.message || '请检查注释标记数量'));
    showFinishModal('fail', conv.message);
  } else if (ok) {
    setStatus('转换完成，等待确认');
    showToast('转换完成', 'ok');
    showFinishModal('done');
  } else if (res && res.converted && res.converted.ok) {
    // S4：历史缓存写入失败但转换成功 —— 提示警告，转换结果仍有效
    btn.disabled = false;
    setStatus('转换完成，但历史缓存写入失败（磁盘错误？）');
    showToast('转换完成，但历史缓存写入失败', 'warn');
    showFinishModal('done', res.converted.message);
  } else {
    btn.disabled = false;
    setStatus('提交失败，转换未完成（可重试）');
    showToast('提交失败：' + ((res && res.error) || '未知错误'), 'fail');
    showFinishModal('fail');
  }
}

// ---------- 完成/失败弹窗 ----------
let finishModalKind = null;
function showFinishModal(kind, msg) {
  finishModalKind = kind;
  const title = document.getElementById('finishTitle');
  const msgEl = document.getElementById('finishMsg');
  const closeBtn = document.getElementById('closePageBtn');
  const stayBtn = document.getElementById('stayPageBtn');
  if (kind === 'done') {
    title.textContent = '转换完成';
    msgEl.textContent = msg || '矫正内容已提交，EPUB 正在生成。是否关闭当前页面？';
    closeBtn.style.display = '';
    stayBtn.textContent = '留在本页';
  } else {
    title.textContent = '转换未完成';
    msgEl.textContent = msg || '提交失败（服务器可能已关闭），请点击「完成并转换」重试。';
    closeBtn.style.display = 'none';
    stayBtn.textContent = '知道了';
  }
  document.getElementById('finishModalBg').style.display = 'flex';
}
document.getElementById('closePageBtn').addEventListener('click', () => {
  document.getElementById('finishModalBg').style.display = 'none';
  window.close();
  setTimeout(() => { alert('浏览器不允许脚本自动关闭此标签页，请手动关闭。'); }, 300);
});
document.getElementById('stayPageBtn').addEventListener('click', () => {
  document.getElementById('finishModalBg').style.display = 'none';
  if (finishModalKind === 'done') setStatus('转换完成，可手动关闭此页面');
  finishModalKind = null;
});

// ---------- 历史记录弹窗（列表 / 单删 / 多选删 / 全部删） ----------
function historyRow(it) {
  const tr = document.createElement('tr');
  const tdCheck = document.createElement('td'); tdCheck.style.padding = '6px 8px';
  const cb = document.createElement('input');
  cb.type = 'checkbox'; cb.className = 'hist-check'; cb.dataset.id = it.id;
  tdCheck.appendChild(cb);
  const tdName = document.createElement('td'); tdName.style.padding = '6px 8px'; tdName.textContent = it.name;
  const tdPath = document.createElement('td'); tdPath.style.padding = '6px 8px'; tdPath.style.color = '#5a6b7c'; tdPath.textContent = it.path;
  const tdVer = document.createElement('td'); tdVer.style.padding = '6px 8px'; tdVer.textContent = 'v' + (it.version || 1);
  const tdTime = document.createElement('td'); tdTime.style.padding = '6px 8px'; tdTime.style.color = '#5a6b7c'; tdTime.textContent = it.updated;
  const tdOp = document.createElement('td'); tdOp.style.padding = '6px 8px';
  const btn = document.createElement('button');
  btn.type = 'button'; btn.textContent = '打开';
  btn.title = '把该版本的文本重新载入编辑器进行再次矫正（覆盖当前未保存的修改）';
  btn.addEventListener('click', () => loadHistoryVersion(it.id, it.name, it.version || 1));
  tdOp.appendChild(btn);
  tr.append(tdCheck, tdName, tdPath, tdVer, tdTime, tdOp);
  return tr;
}
async function loadHistory() {
  const tbody = document.querySelector('#historyTable tbody');
  tbody.innerHTML = '<tr><td colspan="6" style="padding:12px;color:#9aa7b4;">加载中 ...</td></tr>';
  document.getElementById('historyCheckAll').checked = false;
  try {
    const res = await fetchJSON('/api/history');
    const items = res.items || [];
    tbody.innerHTML = '';
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="6" style="padding:12px;color:#9aa7b4;">暂无历史记录</td></tr>';
      return;
    }
    for (const it of items) tbody.appendChild(historyRow(it));
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="5" style="padding:12px;color:#b3543a;">加载失败: ' + e + '</td></tr>';
  }
}
function openHistory() { loadHistory(); document.getElementById('historyModalBg').style.display = 'flex'; }
function closeHistory() { document.getElementById('historyModalBg').style.display = 'none'; }
// 搜索/导出模态框开关：与 historyModalBg 同一模式（CSS 默认 display:none）。
// 曾因重构丢失这四个函数导致加载期 ReferenceError，后续所有绑定（含工具栏）
// 全部失效——编辑后务必 node --check 并核对每个顶层绑定目标函数已定义。
function openSearchModal() { document.getElementById('searchModalBg').style.display = 'flex'; document.getElementById('searchInput').focus(); }
function closeSearchModal() { document.getElementById('searchModalBg').style.display = 'none'; }
function openExportModal() { document.getElementById('exportModalBg').style.display = 'flex'; }
function closeExportModal() { document.getElementById('exportModalBg').style.display = 'none'; }
async function loadHistoryVersion(id, name, ver) {
  const displayName = name + (ver ? ' v' + ver : '');
  if (!confirm('确定用该历史版本（' + displayName + '）替换当前编辑内容？未保存的修改将被覆盖。')) return;
  try {
    const res = await fetchJSON('/api/history/load', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id })
    });
    const loaded = res.pages || [];
    const map = {};
    for (const it of loaded) map[it.page] = it.html;
    // 旧内容按页码收集（未编辑页取初始 text）
    const oldByPage = {};
    for (let i = 0; i < pages.length; i++) {
      oldByPage[pages[i].page] = contentMap.has(i) ? contentMap.get(i) : pages[i].text;
    }
    const oldMdByPage = {};
    for (let i = 0; i < pages.length; i++) if (mdSourceMap.has(i)) oldMdByPage[pages[i].page] = mdSourceMap.get(i);
    // 页码取并集：历史版本可能包含当前会话没有的页（如无 PDF 启动后打开暂存）
    const pageSet = new Set();
    for (const p of pages) pageSet.add(p.page);
    for (const it of loaded) pageSet.add(it.page);
    const newPages = [...pageSet].sort((a, b) => a - b).map(function(p) { return { page: p, text: '' }; });
    const newContent = new Map();
    const newMd = new Map();
    for (let i = 0; i < newPages.length; i++) {
      const p = newPages[i].page;
      const cur = map[p] !== undefined ? map[p] : (oldByPage[p] !== undefined ? oldByPage[p] : '');
      newContent.set(i, cur);
      if (mdMode) newMd.set(i, oldMdByPage[p] !== undefined ? oldMdByPage[p] : htmlToMd(cur));
    }
    pages = newPages;
    contentMap = newContent;
    mdSourceMap = newMd;
    histClear(); // 整体替换内容，撤销/重做历史失效
    editedSet.clear();
    for (let i = 0; i < pages.length; i++) editedSet.add(i);
    heights.length = pages.length; heights.fill(0);
    est = pages.length ? 420 : 420;
    loadNonce++;  // 换书后图片 URL 加 ?v= 强制重新加载（缓存/来源已切换）
    host.innerHTML = '';
    rebuildPrefix(); // heights 已重置，prefixH 需按 est 重建（旧累计值不可复用）
    host.style.height = totalHeight() + 'px';
    updateViewport();
    dirty = true; updateStatus();
    closeHistory();
    loadedTitle = name.replace(/\.[^.\/\\]+$/, '');  // 去扩展名，无文件模式下作为 EPUB 标题
    setStatus('已从历史版本载入 ' + loaded.length + ' 页，可继续矫正（保存/完成将生成新版本）');
  } catch (e) { alert('加载历史版本失败: ' + e); }
}
async function deleteHistory(ids, all) {
  try {
    const res = await fetchJSON('/api/history/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: ids, all: !!all })
    });
    alert('已删除 ' + res.deleted + ' 条历史记录');
    loadHistory();
  } catch (e) { alert('删除失败: ' + e); }
}
document.getElementById('historyBtn').addEventListener('click', openHistory);
document.getElementById('historyCloseBtn').addEventListener('click', closeHistory);
document.getElementById('historyCheckAll').addEventListener('change', (e) => {
  document.querySelectorAll('.hist-check').forEach(c => { c.checked = e.target.checked; });
});
document.getElementById('historyDeleteBtn').addEventListener('click', () => {
  const ids = [...document.querySelectorAll('.hist-check:checked')].map(c => c.dataset.id);
  if (!ids.length) { alert('请先勾选要删除的历史记录'); return; }
  if (!confirm('确定删除选中的 ' + ids.length + ' 条历史记录？')) return;
  deleteHistory(ids, false);
});
document.getElementById('historyDeleteAllBtn').addEventListener('click', () => {
  if (!confirm('确定删除全部历史记录？此操作不可恢复。')) return;
  deleteHistory([], true);
});

// ---------- 弹出快捷菜单（图标 + 悬停提示，置于选中文字正上方） ----------
function buildPopup() {
  popup.innerHTML = '';
  // 两行显示：13 个操作分成 7 + 6 两组（行间用 .sep 分隔）
  const groups = [OPS.slice(0, 7), OPS.slice(7)];
  groups.forEach((group, gi) => {
    if (gi > 0) { const d = document.createElement('div'); d.className = 'sep'; popup.appendChild(d); }
    for (const op of group.map(g => g[0])) {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'pop-btn'; b.dataset.op = op;
      b.innerHTML = OP_ICON[op] || op;
      // 提示文字：悬停超过设定时间才显示（延迟可配置），含快捷键内容
      b.setAttribute('aria-label', OP_TIP[op] || op);
      b.addEventListener('mouseenter', scheduleTip);
      b.addEventListener('mouseleave', hideTip);
      b.addEventListener('mousedown', (e) => {
        e.preventDefault();
        hideTip();
        suppressPopupUntil = performance.now() + 250;  // 点击后菜单保持隐藏，不再自动弹出
        applyOp(op); hidePopup();
      });
      popup.appendChild(b);
    }
  });
}
function hidePopup() { hideTip(); popup.style.display = 'none'; }
function showPopup(range) {
  buildPopup();
  popup.style.display = 'flex';
  const r = popup.getBoundingClientRect();
  const rect = range.getBoundingClientRect();
  let left = rect.left + rect.width / 2 - r.width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - r.width - 8));
  let top = rect.top - r.height - 8;   // 选中文字正上方，不遮盖选中内容
  if (top < 8) top = rect.bottom + 8;  // 上方空间不足 → 移到下方
  popup.style.left = left + 'px';
  popup.style.top = top + 'px';
}
// 选中文字 → 弹出快捷菜单。触发点：mouseup（鼠标框选）与 keyup（Shift+方向键
// 键盘选择）；Ctrl/Meta/Alt 组合键是快捷键操作，不弹菜单。点击操作按钮后
// suppressPopupUntil 窗口内不弹（避免格式操作后菜单反复弹出）。
function maybeShowPopup() {
  if (performance.now() < suppressPopupUntil) return;
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) { hidePopup(); return; }
  const range = sel.getRangeAt(0);
  let n = range.commonAncestorContainer;
  if (n && n.nodeType === 3) n = n.parentNode;
  const ed = n && n.closest ? n.closest('.editable') : null;
  if (!ed) { hidePopup(); return; }
  showPopup(range);
}
document.addEventListener('mouseup', maybeShowPopup);
document.addEventListener('keyup', (e) => { if (!e.isComposing && !e.ctrlKey && !e.metaKey && !e.altKey) maybeShowPopup(); });
document.addEventListener('selectionchange', () => {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) hidePopup();
});

// 字号下拉：仅调整编辑区显示字号（CSS 变量 --editor-font-size；视图偏好，不写入保存内容）
function applyFontSize(v) {
  document.documentElement.style.setProperty('--editor-font-size', (v || 14) + 'px');
  setStatus('编辑字号：' + (v || 14) + 'px');
}

// ---------- 快捷键绑定 ----------
function comboOf(e) {
  const mods = [];
  if (e.ctrlKey) mods.push('Ctrl');
  if (e.altKey) mods.push('Alt');
  if (e.shiftKey) mods.push('Shift');
  const k = e.key;
  if (k === 'Control' || k === 'Alt' || k === 'Shift' || k === 'Meta') return null;
  let key = k;
  if (/^[a-zA-Z]$/.test(key)) key = key.toUpperCase();
  if (key === ' ') key = 'Space';
  if (mods.length === 0 && !/^F\d{1,2}$/.test(key)) return null; // 必须带修饰键或功能键
  return [...mods, key].join('+');
}
function renderShortcutTable() {
  const tbody = document.getElementById('shortcutTable');
  tbody.innerHTML = '';
  for (const [op, label] of OPS) {
    const tr = document.createElement('tr');
    tr.dataset.op = op;
    const combo = bindings[op];
    tr.innerHTML = '<td>' + label + '</td><td>' + (combo ? '<kbd>' + combo.replace(/\+/g, '</kbd>+<kbd>') + '</kbd>' : '<span style="color:#9aa7b4">未绑定</span>') + '</td>';
    tr.addEventListener('click', () => {
      capturingOp = op;
      renderShortcutTable();
      const row = tbody.querySelector('tr[data-op="' + op + '"]');
      if (row) row.querySelector('td:nth-child(2)').textContent = '按下新组合键…（Esc 取消，Del 清除）';
    });
    tbody.appendChild(tr);
  }
}
function openSettings() {
  renderShortcutTable();
  document.getElementById('tipDelayInput').value = tipDelay();
  document.getElementById('modalBg').style.display = 'flex';
}
function closeSettings() { capturingOp = null; document.getElementById('modalBg').style.display = 'none'; }

// ---------- 撤销 / 重做 ----------
// 快照粒度为「操作」：一次连续输入（间隔 < UNDO_IDLE_MS）算一次操作；
// 格式按钮/标记/对齐/搜索替换/智能清理/繁简转换/插入图片等离散操作各算一次。
// undoStack/redoStack 各保留最近 UNDO_LIMIT（10）步，超出丢最旧；新操作清空重做。
// 快照只记录「源」（pageSource，即当前模式对应的 map 或回退到初始 text），
// 且 histEnd 只保留实际变化的页，避免全页操作把整本书复制 10 份。
const UNDO_LIMIT = 10;
const UNDO_IDLE_MS = 800; // 两次输入间隔超过此值 → 视为新操作起点
let undoStack = [];   // [{before: Map(i→源), after: Map(i→源), label}]
let redoStack = [];
let currentUndo = null;  // 进行中的输入操作 {before, pages:Set, label}；空闲超时后落栈
let undoIdleTimer = null;
let inDiscreteOp = false; // 离散操作正在改 DOM（抑制 beforeinput 误开输入操作）

function histPush(before, after, label) {
  undoStack.push({ before: before, after: after, label: label });
  if (undoStack.length > UNDO_LIMIT) undoStack.shift();
  redoStack.length = 0; // 新操作使重做历史失效
  histUpdateButtons();
}
function histCommitInput() {
  if (undoIdleTimer) { clearTimeout(undoIdleTimer); undoIdleTimer = null; }
  if (!currentUndo) return;
  const after = new Map();
  for (const i of currentUndo.pages) after.set(i, pageSource(i));
  histPush(currentUndo.before, after, currentUndo.label);
  currentUndo = null;
}
function histIdle() { undoIdleTimer = null; histCommitInput(); }
function histScheduleIdle() {
  if (undoIdleTimer) clearTimeout(undoIdleTimer);
  undoIdleTimer = setTimeout(histIdle, UNDO_IDLE_MS);
}
function histBeginInput(i) {
  // beforeinput/keydown/compositionstart/paste 均在 DOM 变更前触发 → 可捕获操作前快照。
  // 重复触发无副作用（幂等）；离散操作改 DOM 期间（execCommand 也会派发 beforeinput）
  // 忽略，防止把格式操作误记为「输入」。
  if (i < 0 || inDiscreteOp) return;
  if (currentUndo) { currentUndo.pages.add(i); return; }
  const before = new Map();
  before.set(i, pageSource(i));
  currentUndo = { before: before, pages: new Set([i]), label: '输入' };
}
function histTouchInput(i) {
  // input 事件（变更后）触发：只扩展进行中操作的页面集合并续期空闲计时；
  // 操作起点由「变更前」事件建立，这里不补建（否则快照已含本次变更）。
  if (i < 0 || inDiscreteOp) return;
  if (currentUndo) { currentUndo.pages.add(i); histScheduleIdle(); }
}
// 离散（同步）操作包装：先收掉进行中的输入操作，捕获 before，执行 fn，提交
function histRun(label, pagesArr, fn) {
  histCommitInput();
  const before = new Map();
  for (const i of (pagesArr || [])) before.set(i, pageSource(i));
  inDiscreteOp = true;
  let out;
  try { out = fn(); }
  finally { inDiscreteOp = false; }
  histEnd(before, label);
  return out;
}
// 离散（异步/多页）操作：histBegin 返回 before 快照，操作完成后 histEnd 提交；
// 只保留实际变化的页（before/after 均按变化页裁剪）。histBegin 之后若提前
// return（未发生变更），before 自然丢弃、不入栈。
function histBegin(label, pagesArr) {
  histCommitInput();
  const before = new Map();
  if (pagesArr === null || pagesArr === undefined) {
    for (let i = 0; i < pages.length; i++) before.set(i, pageSource(i));
  } else {
    for (const i of pagesArr) before.set(i, pageSource(i));
  }
  return before;
}
function histEnd(before, label) {
  const after = new Map();
  for (const [i, src] of before) {
    const now = pageSource(i);
    if (now !== src) after.set(i, now); else before.delete(i);
  }
  if (before.size) histPush(before, after, label);
}
function histClear() {
  if (undoIdleTimer) { clearTimeout(undoIdleTimer); undoIdleTimer = null; }
  currentUndo = null;
  undoStack = []; redoStack = [];
  histUpdateButtons();
}
function restoreHistorySnapshot(snap) {
  // 恢复指定页的源（写入当前模式对应 map），重渲染已挂载行；若当前编辑页被
  // 恢复则重聚焦并置光标到末尾。恢复只写 map + innerHTML，不派发 beforeinput/
  // input，不会触发新的历史记录。
  for (const [i, src] of snap) {
    if (mdMode) mdSourceMap.set(i, src);
    else contentMap.set(i, src);
    const row = host.querySelector('.page-row[data-i="' + i + '"]');
    if (row) {
      const ed = row.querySelector('.editable');
      if (ed) { ed.innerHTML = displayHtml(i); remeasure(i); }
    }
  }
  const ed = currentEditable();
  if (ed) {
    const row = ed.closest('.page-row');
    const i = row ? Number(row.dataset.i) : -1;
    if (snap.has(i)) {
      ed.focus();
      const r = document.createRange();
      r.selectNodeContents(ed); r.collapse(false);
      const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
    }
  }
}
function undoHistory() {
  histCommitInput(); // 先落栈进行中的输入操作，才能撤到它
  const entry = undoStack.pop();
  if (!entry) { setStatus('没有可撤回的操作'); return false; }
  restoreHistorySnapshot(entry.before);
  redoStack.push(entry);
  if (redoStack.length > UNDO_LIMIT) redoStack.shift();
  histUpdateButtons();
  setStatus('已撤回：' + entry.label);
  return true;
}
function redoHistory() {
  const entry = redoStack.pop();
  if (!entry) { setStatus('没有可前进的操作'); return false; }
  restoreHistorySnapshot(entry.after);
  undoStack.push(entry);
  if (undoStack.length > UNDO_LIMIT) undoStack.shift();
  histUpdateButtons();
  setStatus('已前进：' + entry.label);
  return true;
}
function histUpdateButtons() {
  const u = document.getElementById('undoBtn');
  const r = document.getElementById('redoBtn');
  if (u) u.disabled = !undoStack.length;
  if (r) r.disabled = !redoStack.length;
}
// 工具栏按钮（onmousedown preventDefault 保持编辑焦点不丢）
document.getElementById('undoBtn').addEventListener('click', () => { hideTip(); undoHistory(); });
document.getElementById('redoBtn').addEventListener('click', () => { hideTip(); redoHistory(); });
// 快捷键：Ctrl+Z 撤回 / Ctrl+Y、Ctrl+Shift+Z 前进。本段位于「全局事件」之前，
// 故本监听器先注册、优先处理；stopImmediatePropagation 阻止用户把 Ctrl+Z/Y 绑到
// 其他操作时二次触发。输入框/快捷键录制（capturingOp）中保留原生/绑定行为。
document.addEventListener('keydown', (e) => {
  if (capturingOp) return; // 正在录制快捷键：Ctrl+Z 应作为新组合键绑定
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return;
  if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
  const k = e.key;
  if (k !== 'z' && k !== 'Z' && k !== 'y' && k !== 'Y') return;
  const did = (k === 'y' || k === 'Y' || e.shiftKey) ? redoHistory() : undoHistory();
  if (did) { e.preventDefault(); e.stopImmediatePropagation(); }
}, true);

// ---------- 全局事件 ----------
document.addEventListener('keydown', (e) => {
  if (capturingOp) {
    e.preventDefault(); e.stopPropagation();
    if (e.key === 'Escape') { capturingOp = null; renderShortcutTable(); return; }
    if (e.key === 'Delete' || e.key === 'Backspace') { bindings[capturingOp] = ''; saveBindings(); capturingOp = null; renderShortcutTable(); return; }
    const combo = comboOf(e);
    if (combo) { bindings[capturingOp] = combo; saveBindings(); capturingOp = null; renderShortcutTable(); }
    return;
  }
  const combo = comboOf(e);
  if (!combo) return;
  const op = reverseBindings()[combo];
  if (!op) return;
  if (!currentEditable()) return;
  applyOp(op);
  e.preventDefault();
  return;
});
  document.getElementById('searchInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') searchPages(); });
  // Color picker input (hidden). Append to body to avoid messing toolbar layout.
  const colorInput = document.createElement('input'); colorInput.type = 'color'; colorInput.id = 'colorInput'; colorInput.style.display = 'none'; document.body.appendChild(colorInput);
  const colorBtn = document.getElementById('colorBtn');
  if (colorBtn) {
    colorBtn.addEventListener('click', (e) => { e.preventDefault(); colorInput.click(); });
    colorInput.addEventListener('input', (e) => {
      const color = e.target.value;
      const ed = currentEditable(); if (!ed) return;
      const row = ed.closest('.page-row'); const i = row ? Number(row.dataset.i) : -1;
      histRun('颜色', [i], function() {
        applyToSelectedBlocks(ed, function() { withScrollStable(() => document.execCommand('foreColor', false, color)); });
        syncContent(ed);
        if (row) { markDirty(Number(row.dataset.i)); scheduleRemeasure(Number(row.dataset.i)); }
      });
    });
  }
  const brushBtn = document.getElementById('formatBrushBtn');
  if (brushBtn) {
    brushBtn.addEventListener('click', (e) => {
      e.preventDefault();
      if (!_formatBrush) {
        const fmt = captureFormatFromSelection();
        if (!fmt) { showToast('请先选中含有格式的文本以捕获格式', 'warn'); return; }
        _formatBrush = fmt;
        // start aggregated history entry
        _brushBefore = histBegin('格式刷', null);
        brushBtn.classList.add('active');
        setStatus('已捕获格式（持续模式）。在目标文本上点击或选区后格式将被应用；再次点击 格式刷 可提交并结束。');
      } else {
        // finish aggregated history entry and clear
        try { histEnd(_brushBefore, '格式刷'); } catch (err) { /* best-effort */ }
        _brushBefore = null;
        _formatBrush = null;
        brushBtn.classList.remove('active');
        setStatus('已提交格式刷并结束');
      }
    });
    host.addEventListener('mouseup', () => {
      if (_formatBrush) {
        const ed = currentEditable(); if (!ed) return;
        applyFormatBrushToSelection(_formatBrush);
        syncContent(ed);
        const row = ed.closest('.page-row'); if (row) { markDirty(Number(row.dataset.i)); scheduleRemeasure(Number(row.dataset.i)); }
        setStatus('已应用格式（持续模式）。要结束请再次点击 格式刷 或按 Esc。');
      }
    });
  }
// Escape: hide popup and cancel format brush if active
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    hidePopup();
    if (_formatBrush) {
      try { if (_brushBefore) histEnd(_brushBefore, '格式刷'); } catch (err) { /* best-effort */ }
      _brushBefore = null;
      _formatBrush = null;
      const b = document.getElementById('formatBrushBtn'); if (b) b.classList.remove('active');
      setStatus('已取消格式刷');
    }
  }
});
const markUserScroll = () => { lastUserScrollTs = Date.now(); lastAnyScrollTs = Date.now(); };
const markAnyScroll = () => { lastAnyScrollTs = Date.now(); };
// 滚动驱动虚拟列表：滚动后挂载视口附近的行（rAF 节流，避免每帧重复挂载）。
// 曾因滚动稳定重构丢掉 updateViewport 调用，导致只挂载初始 ~18 行、
// 后续页空白（2026-08 修复）。lastAnyScrollTs/lastUserScrollTs 现仅服务
// withScrollStable 还原判断（anchorScrollForHeightChange 无门控：行高变化即时同帧补偿）。
let _viewportRaf = 0;
const scheduleViewport = () => {
  if (_viewportRaf) return;
  _viewportRaf = requestAnimationFrame(() => { _viewportRaf = 0; updateViewport(); });
};
window.addEventListener('wheel', markUserScroll, { passive: true });
window.addEventListener('touchmove', markUserScroll, { passive: true });
window.addEventListener('scroll', () => { markAnyScroll(); scheduleViewport(); hidePopup(); }, { passive: true });
window.addEventListener('beforeunload', (e) => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });

// ---------- 浏览器存活监测 ----------
setInterval(() => { fetch('/api/heartbeat', { method: 'POST' }).catch(() => {}); }, 30000);
window.addEventListener('pagehide', () => { navigator.sendBeacon('/api/gone'); });
window.addEventListener('pageshow', () => { fetch('/api/heartbeat', { method: 'POST' }).catch(() => {}); });

document.getElementById('saveBtn').addEventListener('click', save);
document.getElementById('stageBtn').addEventListener('click', stage);
document.getElementById('finishBtn').addEventListener('click', finish);
// U3：hintbar 可折叠（✕ 关闭，localStorage 记忆；可在「帮助」中随时查看）
const hintbar = document.getElementById('hintbar');
if (hintbar) {
  if (loadBool('ptoe_hint_hidden')) hintbar.classList.add('hidden');
  document.getElementById('hintClose').addEventListener('click', () => {
    hintbar.classList.add('hidden');
    saveStr('ptoe_hint_hidden', '1');
  });
}
document.getElementById('settingsBtn').addEventListener('click', openSettings);
document.getElementById('tipDelayInput').addEventListener('change', (e) => {
  const v = parseInt(e.target.value, 10);
  saveStr('ptoe_tip_delay', String(Math.max(0, Math.min(5000, isNaN(v) ? 0 : v))));
});
document.getElementById('cleanBtn').addEventListener('click', cleanAll);
document.getElementById('searchOpenBtn').addEventListener('click', openSearchModal);
document.getElementById('searchBtn').addEventListener('click', searchPages);
document.getElementById('searchInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') searchPages(); });
document.getElementById('replaceBtn').addEventListener('click', replaceCurrent);
document.getElementById('replaceAllBtn').addEventListener('click', replaceAll);
document.getElementById('replaceInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') replaceCurrent(); });
document.getElementById('searchPrevBtn').addEventListener('click', () => gotoMatch(-1));
document.getElementById('searchNextBtn').addEventListener('click', () => gotoMatch(1));
document.getElementById('searchCloseBtn').addEventListener('click', closeSearchModal);
document.getElementById('searchModalBg').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeSearchModal(); });
document.getElementById('exportBtn').addEventListener('click', openExportModal);
document.getElementById('exportTxtBtn').addEventListener('click', () => exportFile('txt'));
document.getElementById('exportDocxBtn').addEventListener('click', () => exportFile('docx'));
document.getElementById('exportCloseBtn').addEventListener('click', closeExportModal);
document.getElementById('exportModalBg').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeExportModal(); });
document.getElementById('imgModeSel').addEventListener('change', (e) => { saveStr('ptoe_img_mode', e.target.value); });
document.getElementById('closeSettings').addEventListener('click', closeSettings);
document.getElementById('modalBg').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeSettings(); });
document.getElementById('mdToggleBtn').addEventListener('click', () => setMdMode(!mdMode));
document.getElementById('helpBtn').addEventListener('click', () => { document.getElementById('helpModalBg').style.display = 'flex'; });
document.getElementById('closeHelp').addEventListener('click', () => { document.getElementById('helpModalBg').style.display = 'none'; });
document.getElementById('helpModalBg').addEventListener('click', (e) => { if (e.target === e.currentTarget) document.getElementById('helpModalBg').style.display = 'none'; });
document.getElementById('fontSizeSel').addEventListener('change', (e) => applyFontSize(parseInt(e.target.value, 10) || 14));
document.getElementById('jumpBtn').addEventListener('click', jumpToPage);
document.getElementById('pageJump').addEventListener('keydown', (e) => { if (e.key === 'Enter') jumpToPage(); });
document.getElementById('toTraditionBtn').addEventListener('click', () => convertAll('s2t'));
document.getElementById('toSimplifiedBtn').addEventListener('click', () => convertAll('t2s'));
document.querySelectorAll('#toolbar button[data-op]').forEach((b) => {
  b.addEventListener('mouseenter', scheduleTip);
  b.addEventListener('mouseleave', hideTip);
  b.addEventListener('click', () => { hideTip(); suppressPopupUntil = performance.now() + 250; applyOp(b.dataset.op); });
});
// ---------- 初始化 ----------
(async function init() {
  try {
    pages = (await fetchJSON('/api/pages')).pages;
  } catch (e) { document.body.textContent = '加载失败: ' + e; return; }
  heights.length = pages.length; heights.fill(0);
  est = pages.length ? 420 : 420;
  mdMode = loadBool('ptoe_md_mode');
  if (mdMode) {
    for (let i = 0; i < pages.length; i++) mdSourceMap.set(i, htmlToMd(pages[i].text));
    const btn = document.getElementById('mdToggleBtn');
    btn.textContent = '富文本模式';
    btn.classList.add('active');
  }
  const fs = loadInt('ptoe_font_size', 14);
  document.getElementById('fontSizeSel').value = fs;
  document.documentElement.style.setProperty('--editor-font-size', fs + 'px');
  // 图片插入显示模式（全画幅/局部）从 localStorage 恢复
  const im = localStorage.getItem('ptoe_img_mode');
  if (im === 'fit' || im === 'full') document.getElementById('imgModeSel').value = im;
  host.style.height = totalHeight() + 'px';
  updateViewport();
  setStatus('已加载 ' + pages.length + ' 页');
})();
</script>
</body>
</html>
"""
