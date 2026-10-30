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
- ThreadingHTTPServer 并发处理图片与保存请求；
- pages 读写共用锁（pages_lock），保存/暂存/完成与页面读取互斥；
- 历史缓存写入失败会向浏览器报错（不静默丢数据）。

用法：
    from correctmanage import correct_pages, apply_markers
    corrected = correct_pages(structured['pages'], pdf_path=pdf, img_dir=img_dir)
    articles = apply_markers(corrected)   # 有标记时生成文章结构
"""

from __future__ import annotations

import base64
import concurrent.futures
import gzip
import hashlib
import html as _html
import json
import os
import re
import struct
import sys
import tempfile
import threading
import time
import webbrowser
from collections import OrderedDict
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from difflib import SequenceMatcher
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

# 插入图片布局模式值域：**与 configmanage.IMG_MODE_CHOICES 同源**（前端 ui/app.js 的
# IMG_MODES 必须与之一致）。configmanage 平时只在函数内惰性导入，这里单独取一次常量，
# 供 /api/ui_settings 的 POST 校验使用（2026-10：此前这里写死 3 种，选「绕排」时整批
# ui_settings 被 400 拒绝 → 所有界面设置都存不下来）。
try:  # pragma: no cover - 极端打包场景下回退到同值字面量
    from configmanage import IMG_MODE_CHOICES
except Exception:  # noqa: BLE001
    IMG_MODE_CHOICES = ("", "full", "fit", "inline", "float-left", "float-right")

# 可选 msgpack 支持：用于内嵌预览图 sidecar 存储（比 gzip+JSON 更快、更小）
try:
    import msgpack
except ImportError:
    msgpack = None

# 词典增强模块（并行任务写入）；若尚未就绪则降级跳过规则⑤与反馈回写
try:
    import dictionarymanage
except ImportError:
    dictionarymanage = None

# 格式规则服务端引擎（任务 A：rulemanage.py）
try:
    import rulemanage
except ImportError:
    rulemanage = None

# 加粗注释标签转换（htmlmanage.py）
# strip_block_ws / blank_block_ws：多行对齐段（ptoe-align-lines）的 &nbsp; 填充是
# 版式的一部分，默认口径的 str.strip() 会连它一起剥掉——导出后左右两侧就对不齐了
try:
    from htmlmanage import transform_note_labels, strip_block_ws, blank_block_ws
except ImportError:  # pragma: no cover - htmlmanage 缺失时的兜底
    transform_note_labels = None
    _WS_KEEP_NBSP = " \t\n\r\f\v\u3000\u200b\u200c\ufeff"

    def strip_block_ws(text, keep_nbsp=False):
        """块首尾空白清理；keep_nbsp=True 时保留 &nbsp;（U+00A0）不剥。"""
        return text.strip(_WS_KEEP_NBSP) if keep_nbsp else text.strip()

    def blank_block_ws(text, keep_nbsp=False):
        """文本是否只有空白；keep_nbsp=True 时 &nbsp; 算内容（不算空白）。"""
        return not strip_block_ws(text, keep_nbsp)

__all__ = [
    "apply_markers",
    "clean_page_html",
    "correct_pages",
    "initial_html",
    "sanitize_html",
]

# ---------------------------------------------------------------------------
# HTML 白名单清洗
# ---------------------------------------------------------------------------

_BLOCK_RE = re.compile(r"h[1-6]")

# 非内容标签：连同其文本内容整体丢弃（script/style/iframe 等）
_SKIP_TAGS = {"script", "style", "head", "iframe", "object", "embed"}
_MARKER_RE = re.compile(r"^(?:full|join|note|page|chapter:\d{1,2})$")
_NOTE_CLASS = "ptoe-note"
# 题注（2026-10 图文混合）：图片块之后的独立说明段落 <p class="ptoe-caption">。
# 纯样式承载类（无行内语义），须随保存/导出落盘；漏进 _block_classes/_block_class_html
# 会被 sanitize 静默剥掉，症状为「保存后题注样式丢失」（本项目已有同型 bug）。
_CAPTION_CLASS = "ptoe-caption"
# 表格级保留 class（2026-10 无边框表格）：表格菜单「不显示边框」写入的
# ptoe-noborder（分栏/批注版式）。表格级 class 白名单——不在表内的 class 一律丢弃。
TBL_TABLE_CLASSES = ("ptoe-noborder",)


_ALIGN_CLASSES = {"ptoe-align-center", "ptoe-align-left", "ptoe-align-right"}
# 多行对齐标记类（2026-10-20）：矫正界面「多行对齐」给对齐段落打的块级标记。
# 作用只有一个——导出 EPUB 时该段豁免空白符清理（对齐用的空白是版式的一部分，
# 与居右段 ptoe-align-right、注释标签段 ptoe-note-label 同策略）。
# 不并入 _ALIGN_CLASSES：那是「水平对齐」语义（左/中/右互斥、各留一个），
# 多行对齐与左/中/右可以并存，塞进去会挤掉真正的水平对齐类。
_ALIGN_LINES_CLASS = "ptoe-align-lines"
# 单元格垂直对齐类（2026-10 表格增强）：与水平对齐类同策略，单元格内各保留一个。
_VALIGN_CLASSES = {"ptoe-vtop", "ptoe-vmid", "ptoe-vbot"}
# ---------------------------------------------------------------------------
# MathML 公式（2026-10-21）—— LaTeX 的载体
#
# 矫正界面用 KaTeX（ui/vendor/katex.min.js）把 LaTeX 源码渲染成 MathML 存进正文：
#     <math xmlns="…" alttext="E=mc^2" display="inline"><semantics>…</semantics></math>
# 导出 EPUB 时原样保留（EPUB 3 阅读器原生支持 MathML → 公式照常显示）；
# DOCX/MD/TXT 无 MathML 概念 → 走 alttext 里的 LaTeX 源码做纯文本兜底（$…$）。
#
# 标签/属性白名单统一放在 rulemanage（单一事实来源，与 MiniDOMParser 同口径）——
# 两条链路（保存清洗 / 规则引擎重排）必须放行同一批标签，否则「应用一次格式规则
# 就把公式清空」。
# ---------------------------------------------------------------------------
_MATHML_TAGS = frozenset(getattr(rulemanage, "MATHML_TAGS", ()) or ())
_MATHML_ATTRS = frozenset(getattr(rulemanage, "MATHML_ATTRS", ()) or ())
_MATHML_NS = "http://www.w3.org/1998/Math/MathML"


def _mathml_open_tag(tag: str, attrs) -> str:
    """MathML 开标签 → 规范化 HTML 串（属性走白名单，xmlns 只认 MathML 命名空间）。"""
    parts: list[str] = []
    for k, v in attrs:
        kl = (k or "").lower()
        if kl not in _MATHML_ATTRS:
            continue
        val = v or ""
        if kl == "xmlns" and val != _MATHML_NS:
            continue
        parts.append(f'{kl}="{_html.escape(val, quote=True)}"')
    return "<" + tag + ("".join(" " + p for p in parts)) + ">"


# 行内对齐样式正则：style="text-align:left|center|right"（含空格容忍）
_ALIGN_STYLE_RE = re.compile(r"text-align\s*:\s*(left|center|right)\s*(?:;|$)")

# ---------------------------------------------------------------------------
# 行内格式「不静默丢失」归一（2026-09-16）
#
# 症状：某一句/某几个字在界面上设置了「文字包围」（下划线/删除线/上标/下标…），
#      编辑器里显示正常，但保存或导出 EPUB 后效果消失。
# 根因：多条链路会产出**不在白名单里的原生/外部标签**（浏览器原生
#      execCommand('underline'/'strikeThrough'/'superscript'/'subscript')、
#      粘贴自 Word/网页的 HTML），sanitize_html 只保留白名单标签，于是这些
#      格式被静默丢弃——界面（未清洗的 DOM）看得到，落盘与导出（清洗后）没有。
# 归一：把这些标签/样式折成应用自身的行内格式类（CSS 已在矫正界面与 EPUB 样式表
#      中定义），格式因此全程可保留。
# ---------------------------------------------------------------------------
_ALIAS_INLINE_TAGS = {
    "u": "ptoe-underline",
    "s": "ptoe-strike",
    "strike": "ptoe-strike",
    "del": "ptoe-strike",
    "sup": "ptoe-sup",
    "sub": "ptoe-sub",
    "mark": "ptoe-highlight",
}
# 外部行内样式 → 等价格式类（顺序即优先级；循环会追加每个命中 pattern 的类，
# 故 underline 的实线 pattern 用负向前瞻排除 dotted/dashed/double/wavy 变体，
# 避免 text-decoration 虚线（下加点）同时折出 ptoe-underdot + ptoe-underline 两个类 —— 2026-09-19）
_STYLE_TO_INLINE_CLASS = (
    # 下加点（dotted 变体优先匹配，互斥于实线下划线）
    (re.compile(r"text-decoration[^;]*\bdotted\b", re.IGNORECASE), "ptoe-underdot"),
    # 波浪下划线（2026-10-21）：underline wavy 变体，须先于实线下划线匹配
    (re.compile(r"text-decoration[^;]*\bunderline\b[^;]*\bwavy\b", re.IGNORECASE), "ptoe-wavy"),
    # 双下划线（2026-10-21）：underline double 变体
    (re.compile(r"text-decoration[^;]*\bunderline\b[^;]*\bdouble\b", re.IGNORECASE), "ptoe-double"),
    # 上划线（2026-10-21）
    (re.compile(r"text-decoration[^;]*\boverline\b", re.IGNORECASE), "ptoe-overline"),
    # 着重号（2026-10-21）：text-emphasis: dot / circle / sesame
    (re.compile(r"text-emphasis\s*:\s*(?:dot|circle|sesame)", re.IGNORECASE), "ptoe-emphasis"),
    (re.compile(r"text-decoration[^;]*\bunderline\b(?!\s*(?:dotted|dashed|double|wavy))", re.IGNORECASE), "ptoe-underline"),
    (re.compile(r"text-decoration[^;]*line-through", re.IGNORECASE), "ptoe-strike"),
    (re.compile(r"vertical-align\s*:\s*super", re.IGNORECASE), "ptoe-sup"),
    (re.compile(r"vertical-align\s*:\s*sub\b", re.IGNORECASE), "ptoe-sub"),
    # 背景色无法区分底纹/突显，统一折为突显（有颜色总比被丢弃更接近用户意图）
    (re.compile(r"background(?:-color)?\s*:", re.IGNORECASE), "ptoe-highlight"),
)
# font color 取值白名单（#rrggbb / 颜色名），其余丢弃
_COLOR_VALUE_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$|^[a-zA-Z]{3,20}$")
# 行内格式类白名单：note/citation 等由规则引擎产生的行内 span 类 + 8 种统一行内包装格式
# （单一事实来源 = rulemanage.INLINE_FORMAT_CLASSES；2026-09-19 防漂移）
# 2026-09-24 补 ptoe-align-*：前端对部分选区用 <span class="ptoe-align-left|center|right">
# 行内包裹（句中/词上独立对齐），须随保存落盘；对齐信息同时承载于 span 类上
# （块级 p/h 仍走 _block_classes，两者互不影响）。
_INLINE_FORMAT_CLASSES = {"ptoe-note", "ptoe-citation"} | set(
    getattr(rulemanage, "INLINE_FORMAT_CLASSES", ()) or ()
) | set(_ALIGN_CLASSES)

# 块级标签：p / h1-6 / div / table。div 与 sanitize_html 的处理保持一致（统一按
# <p> 处理）：若把 <div> 当行内内容，未清洗数据（历史版本/外部直接调用
# apply_markers）会被包成 <p><div …></div></p> 非法嵌套，并且注释段落识别失败
# → 注释数量校验误报。
#
# table（2026-09-27 修复正文丢失）：table 必须自成一个顶层块并原样透传。此前 table
# 不在块标签集合里，它的 token 会一路累积进 cur，随后有两种坏结局：
#   1) 表格后面跟真段落时，<p> 处的 `if kind:` 为假 → 不 flush，而 kind 又被改成
#      "p"，于是整张表被认领为该段落的内容，把表后的正文一起吞进包裹元素（丢字）；
#   2) 表格在块流末尾时，EOF 的 `if kind or cur:` 用 `kind or "p"` 把表包进 <p>。
# 两者都产出 render_block("p", "<table>…") = <p><table>…</table></p> 非法嵌套。
#
# 表格内部结构标签（thead/tbody/tfoot/tr/td/th）**刻意不进块标签集合**（保持透明、
# 不参与切块）：它们只在 table 内合法、顶层不可能出现；一旦各自成为独立 kind，
# render_block 会给每个结构标签单独裹一层，把整张表拆碎。单元格内也不会有块标签
# ——_Sanitizer.handle_starttag 遇 td/th 内的 p/div/hN 只丢标签、保留行内内容，
# handle_endtag 同理丢弃闭合标签，故 <td><p>…</p></td> 不会切断表格（未清洗的
# 外部数据另由「表格整块原样透传」兜底）。
_BLOCK_TAG_RE = re.compile(
    r"</?(p|div|h[1-6]|table)([^>]*)>", flags=re.IGNORECASE
)

_PAGE_BREAK_CLASS = "ptoe-page-break"
# 可插入的分隔线（直线/线段/点线/双线，2026-09-27）——单一事实来源。
# 标记形态：<p class="ptoe-divider ptoe-divider-{style}">{字形×8}</p>
# 必须是**非空**段落：sanitize_html._flush / _split_segments.flush /
# _html_to_rich_blocks._flush / _clean_blocks 四处都以 `if not text` 删空块
# （且 test_correctmanage 锁定 sanitize_html("<p></p>") == ""）。
# class 有两个：基类 ptoe-divider + 样式后缀 ptoe-divider-<style>；白名单一律用
# startswith("ptoe-divider") 前缀判定，两个类都要放行。
DIVIDER_CLASS = "ptoe-divider"
DIVIDER_STYLES = {
    "solid": "ptoe-divider-solid",    # 直线 ── U+2500
    "dashed": "ptoe-divider-dashed",  # 线段 ╌╌ U+254C
    "dotted": "ptoe-divider-dotted",  # 点线 ╌┈ U+2508
    "double": "ptoe-divider-double",  # 双线 ══ U+2550
    # —— 花样分割符号（2026-10-21）：非线条型，用装饰字形排成分隔 ——
    "stars": "ptoe-divider-stars",        # ★ 五角星
    "diamonds": "ptoe-divider-diamonds",  # ◆ 菱形
    "circles": "ptoe-divider-circles",    # ● 圆点
    "flowers": "ptoe-divider-flowers",    # ❀ 花纹
    "snow": "ptoe-divider-snow",          # ❄ 雪花
    "wave": "ptoe-divider-wave",          # ≈ 波浪
    "note": "ptoe-divider-note",          # ※ 注记
    "leaf": "ptoe-divider-leaf",          # ❦ 叶形
}
# 每个分隔线固定字形个数：线条型 8 个（视觉宽度一致），花样型 5 个（全角字形更宽）
DIVIDER_GLYPHS = {
    "solid": "─" * 8,
    "dashed": "╌" * 8,
    "dotted": "┈" * 8,
    "double": "═" * 8,
    "stars": "★" * 5,
    "diamonds": "◆" * 5,
    "circles": "●" * 5,
    "flowers": "❀" * 5,
    "snow": "❄" * 5,
    "wave": "≈" * 5,
    "note": "※" * 5,
    "leaf": "❦" * 5,
}
# 花样型（字形排布，不是线条）：导出到 Word/纯文本时按字形输出（无边框可画）
DIVIDER_DECORATIVE = {
    "stars",
    "diamonds",
    "circles",
    "flowers",
    "snow",
    "wave",
    "note",
    "leaf",
}
_DIVIDER_STYLE_BY_CLASS = {cls: name for name, cls in DIVIDER_STYLES.items()}
# 缺省样式（未知/缺失后缀时用直线）
DIVIDER_DEFAULT_STYLE = "solid"


def _divider_glyph(style: str) -> str:
    """分隔线样式名 → 8 字形串（未知样式回退默认）。"""
    return DIVIDER_GLYPHS.get(style, DIVIDER_GLYPHS[DIVIDER_DEFAULT_STYLE])


def _divider_class(attrs) -> str:
    """块属性里是否带 ptoe-divider 前缀的 class；有则返回该类名，否则 ''。"""
    d = dict(attrs) if not isinstance(attrs, str) else {}
    classes = (d.get("class") or "").split()
    for c in classes:
        if c == DIVIDER_CLASS or c.startswith(DIVIDER_CLASS):
            return c
    return ""


def _divider_style_from_attrs(attrs) -> str:
    """块属性 → 分隔线样式名（solid/dashed/dotted/double；无分隔线类返回 ''）。"""
    d = dict(attrs) if not isinstance(attrs, str) else {}
    for c in (d.get("class") or "").split():
        if c in _DIVIDER_STYLE_BY_CLASS:
            return _DIVIDER_STYLE_BY_CLASS[c]
    if _divider_class(d):
        return DIVIDER_DEFAULT_STYLE
    return ""


def _block_has_divider(attrs: str) -> bool:
    """块属性串（正则捕获的 attrs 文本）是否含 ptoe-divider 前缀类。"""
    m = re.search(r'class="([^"]*)"', attrs or "")
    return bool(m) and any(c.startswith(DIVIDER_CLASS) for c in m.group(1).split())

_IMG_CLASSES = {
    "ptoe-img-full",
    "ptoe-img-fit",
    "ptoe-img-inline",
    # 浮动绕排（2026-10 图文混合）：与 ptoe-img-inline 互斥，类挂在 img 自身上
    # （float 必须作用在图片元素上文字才会绕排；挂在包裹 p 上无法生效）。
    # 宽度仍复用既有的 ptoe-img-w* 档位，不新增尺寸类。
    "ptoe-img-float-left",
    "ptoe-img-float-right",
    "ptoe-img-w25",
    "ptoe-img-w50",
    "ptoe-img-w75",
    "ptoe-img-w100",
    "ptoe-img-left",
    "ptoe-img-center",
    "ptoe-img-right",
    "ptoe-img-vtop",
    "ptoe-img-vmid",
    "ptoe-img-vbot",
}

# apply_markers / _proofread_plain_text 用：匹配清洗后的标记 span。
# 属性顺序无关（前端普通模式 createElement 先设 class 再设 dataset，
# 序列化后为 <span class="ptoe-marker" data-ptoe-marker="...">；旧写法要求
# data-ptoe-marker 紧跟在 <span 之后会漏匹配，导致标记 label 被当纠错对象）。
_MARKER_SPAN_RE = re.compile(
    r'<span\b[^>]*\bdata-ptoe-marker="([^"]+)"[^>]*>(.*?)</span>',
    flags=re.IGNORECASE | re.DOTALL,
)


_LLM_CONN_ERROR_HINT = (
    "无法连接本地 llama-server（127.0.0.1:8080）。深度校对依赖 llama-server："
    "epub 转换流程会自动启动；若通过 correct 直接矫正，需先手动启动"
    "（并确认 config 中 llama_server 路径正确、服务监听 8080 端口）。"
)
_LLM_TIMEOUT_HINT = "深度校对请求超时。可稍后重试，或检查 llama-server 负载/是否卡住。"
_LLM_TIMEOUT_MARKERS = (
    "Timeout",
    "timed out",
    "WinError 10060",
    "ETIMEDOUT",
    "Read timed out",
)
_LLM_CONN_ERROR_MARKERS = (
    "ConnectionError",
    "Max retries exceeded",
    "WinError 10061",
    "Connection refused",
    "Failed to establish a new connection",
    "ECONNREFUSED",
    "NameResolutionError",
)
# 2026-08-09：llama-server 对非法请求返回 400（模型加载中 / 服务与所选模型不符等），
# 原样透出会显示「400 Client Error: Bad Request for url: ...」（用户报「选择 qwen4 报 400」）。
_LLM_BAD_REQUEST_MARKERS = (
    "400 Client Error",
    "400 Bad Request",
    "Bad Request for url",
)
# 2026-09-01：llama-server 推理期内部错误返回 500（模板处理失败 / 上下文不足 /
# 模型加载异常 / mmproj 与模型不匹配等）。此前 _request_image_new 用
# raise_for_status() 只透出笼统的「500 Server Error」，无法定位原因；现已在
# llamamanage 层透出响应体，这里给出可操作的引导提示（并保留原详情）。
_LLM_SERVER_ERROR_MARKERS = (
    "500 Server Error",
    "500 Internal Server Error",
    "HTTP 500",
)
# 2026-09-01：llama-server 多模态（vision）推理失败——模型收图但无法产出符合
# 预期的视觉投影输出（如 mmproj 与主模型不匹配/版本不兼容/纯文本服务误收图）。
# 报 500，但需要比通用 server-error 更明确的定位提示。
_LLM_PEG_MARKERS = (
    "peg-native format",
    "expected peg-native",
    "does not match the expected peg",
)


# 格式规则允许的格式操作（与前端 applySingleFormat 一一对应；none=无，不处理文本）
_VALID_FORMAT_OPS = {
    "bold",
    "italic",
    "remove",
    "note",
    "p",
    "none",
    "merge",
    "align_left",
    "align_center",
    "align_right",
    "heading1",
    "heading2",
    "heading3",
    "heading4",
    "heading5",
    "heading6",
    "no_bold",      # 不加粗：移除加粗但保留其他格式
    "citation",     # 引用：斜体 + 独立字体设置
    "strip_ws",     # 去空：去除段落内全部空白字符（保留换行）
    # 新增行内格式操作（2026-09）：与 ui/app.js FORMAT_RULE_OPTS 一一对应
    "underline",    # 下划线
    "strike",       # 删除线
    "charbox",      # 字符边框
    "shade",        # 底纹
    "highlight",    # 突显
    "sup",          # 上标
    "sub",          # 下标
    # 2026-10-21 新增行内格式操作：与 ui/app.js FORMAT_RULE_OPTS 一一对应
    "wavy",         # 波浪下划线
    "double",       # 双下划线
    "overline",     # 上划线
    "emphasis",     # 着重号
    "highlight_green",   # 多色突显（绿）
    "highlight_blue",    # 多色突显（蓝）
    "highlight_pink",    # 多色突显（粉）
    "highlight_orange",  # 多色突显（橙）
    "highlight_purple",  # 多色突显（紫）
}

# 预览图磁盘缓存预热：模块级引用 ProcessPoolExecutor，便于测试 monkeypatch。
# Windows spawn 模式下 worker 必须在模块顶层可 picklable。
_PREVIEW_POOL_CLS = concurrent.futures.ProcessPoolExecutor
# 预热守卫：避免 correct_pages 重复进入时启动多份后台线程
_preview_warm_started = threading.Lock()
# 已预热过的 (pdf_path, dpi) 集合（配合 _preview_warm_started 去重）
_preview_warmed_keys: set[tuple[str, int]] = set()
# 页数低于该阈值的书不起进程池预热（小书现场渲染足够快，也避免测试误触池）
_WARM_MIN_PAGES = 80

# 预渲染上限——embedded_images 仅作跨电脑打开历史时 PDF 缺失的兜底，
# 全量驻留内存对大书不可接受（实测 4000 页 ≈ 800MB）。
_PRERENDER_MAX_PAGES = 300


def _clean_format_ops(value) -> list:
    """过滤出合法的格式操作列表（去重、保序）。"""
    if not isinstance(value, list):
        return []
    seen: list[str] = []
    for op in value:
        s = str(op)
        if s in _VALID_FORMAT_OPS and s not in seen:
            seen.append(s)
    return seen


def _regex_valid(pattern: str) -> bool:
    """条件正则合法性：与规则引擎同一编译口径。

    必须复用 rulemanage.compile_pattern（而不是裸 re.compile），否则
    `/pattern/flags` 写法（USAGE.md 中文档化的写法，如 `/^(〔\\d+〕)/m`）
    会被误判为非法 → 整条条件被静默丢弃，用户看到"规则保存了却不生效"。
    """
    try:
        rulemanage.compile_pattern(pattern)
    except Exception:  # noqa: BLE001 — re.error 以及危险模式拦截
        return False
    return True


def _validate_format_rules(rules) -> list:
    """校验前端提交的格式规则列表；非法项丢弃，返回干净列表。

    新模型（2026-08-15）：每条规则 = {id, name, mode(first|all), conditions:[...]}。
    conditions 为有序条件列表，每个条件 = {type(regex|contains|prefix|suffix),
    pattern, scope(selection|paragraph), formats:[...]}；空 pattern = 无条件（恒匹配）。
    mode=first 时第一个匹配条件生效即停；mode=all 时所有匹配条件的格式按序叠加。
    formats 允许 "none"（无，不处理文本），应用前由前端过滤。

    旧模型迁移：含 condition/else_formats/顶层 formats 键的规则自动转换——
    condition.enabled 为真 → 单条件（type/pattern/scope 沿用，formats 沿用）；
    condition.enabled 为假 → 无条件条件（type=contains, pattern='', scope=selection）；
    else_formats 直接丢弃（用户确认移除）。非法正则的条件整条丢弃。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(rules, list):
        return out
    for r in rules:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        mode = str(r.get("mode") or "first")
        if mode not in ("first", "all"):
            mode = "first"
        conditions: list[dict[str, Any]] = []
        # 旧模型迁移：condition / else_formats / 顶层 formats 键存在 → 转单条件
        if "condition" in r or "else_formats" in r or "formats" in r:
            cond_raw = r.get("condition")
            if isinstance(cond_raw, dict) and cond_raw.get("enabled"):
                ctype = str(cond_raw.get("type") or "contains")
                if ctype not in ("regex", "contains", "prefix", "suffix"):
                    ctype = "contains"
                pattern = str(cond_raw.get("pattern") or "")
                scope = str(cond_raw.get("scope") or "selection")
                if scope not in ("selection", "paragraph", "page", "all_pages"):
                    scope = "selection"
                if not pattern:
                    continue  # 启用了条件但没写内容：整条丢弃（与旧行为一致）
                if ctype == "regex" and not _regex_valid(pattern):
                    continue  # 非法正则：整条丢弃（支持 /pattern/flags 写法）
                conditions.append(
                    {
                        "type": ctype,
                        "pattern": pattern,
                        "scope": scope,
                        "formats": _clean_format_ops(r.get("formats")),
                        "target": "match",
                    }
                )
            else:
                # 无条件规则：空 pattern 恒匹配
                conditions.append(
                    {
                        "type": "contains",
                        "pattern": "",
                        "scope": "selection",
                        "formats": _clean_format_ops(r.get("formats")),
                        "target": "match",
                    }
                )
        else:
            # 新模型：conditions 列表
            raw_conds = r.get("conditions")
            if not isinstance(raw_conds, list):
                continue
            for c in raw_conds:
                if not isinstance(c, dict):
                    continue
                ctype = str(c.get("type") or "contains")
                if ctype not in ("regex", "contains", "prefix", "suffix"):
                    ctype = "contains"
                pattern = str(c.get("pattern") or "")
                scope = str(c.get("scope") or "selection")
                if scope not in ("selection", "paragraph", "page", "all_pages"):
                    scope = "selection"
                if ctype == "regex" and pattern and not _regex_valid(pattern):
                    continue  # 非法正则：该条件丢弃（支持 /pattern/flags 写法）
                # target: 匹配对象——决定格式作用于匹配文本/之前/之后/两条件之间
                target = str(c.get("target") or "match")
                if target not in ("match", "before", "after", "between"):
                    target = "match"
                cond_d: dict[str, Any] = {
                    "type": ctype,
                    "pattern": pattern,
                    "scope": scope,
                    "formats": _clean_format_ops(c.get("formats")),
                    "target": target,
                }
                if target == "between":
                    between_end = str(c.get("between_end_pattern") or "")
                    if between_end and ctype == "regex" and not _regex_valid(between_end):
                        between_end = ""  # 非法正则：清空（支持 /pattern/flags 写法）
                    cond_d["between_end_pattern"] = between_end
                # 正则条件可携带 group_formats：每个捕获组独立格式列表
                if ctype == "regex" and isinstance(c.get("group_formats"), list):
                    gf = [
                        _clean_format_ops(sub)
                        for sub in c["group_formats"]
                        if isinstance(sub, list)
                    ]
                    if gf:
                        cond_d["group_formats"] = gf
                # 新：正则条件可携带 match_formats：针对同一正则的多次匹配（全局匹配）
                # 每个子项为该次匹配要应用的格式操作数组（与 formats 格式一致）
                if ctype == "regex" and isinstance(c.get("match_formats"), list):
                    mf = [
                        _clean_format_ops(sub)
                        for sub in c["match_formats"]
                        if isinstance(sub, list)
                    ]
                    if mf:
                        cond_d["match_formats"] = mf
                conditions.append(cond_d)
        if not conditions:
            continue  # 无有效条件：整条丢弃
        # Compute optional fields: pin and label
        pin_val = bool(r.get("pin", False))  # default False if missing
        label_val = str(r.get("label") or "").strip()
        if label_val:
            label_val = label_val[:4]  # cap at 4 chars
        else:
            label_val = None
        rule_out = {
            "id": str(r.get("id") or uuid4().hex),
            "name": name,
            "mode": mode,
            "conditions": conditions,
        }
        if label_val is not None:
            rule_out["label"] = label_val
        if "pin" in r:  # only include if present in input (even if False)
            rule_out["pin"] = pin_val
        out.append(rule_out)
    return out


def _filter_rules_selection_only(rules: list) -> list:
    """过滤规则列表：仅保留 scope=selection 的条件，丢弃其余条件的规则。

    用于 sel_ranges 多区间选区的后续区间——page/paragraph 作用域条件已在首个区间
    完整应用，后续区间重复应用会导致双重包裹（<strong><strong>）。
    空条件列表的规则保留（rule_id/mode 语义不变，引擎视为空操作）。
    """
    out: list[dict] = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        filtered_conds = [
            c for c in r.get("conditions", [])
            if isinstance(c, dict) and c.get("scope", "selection") == "selection"
        ]
        r_copy = dict(r)
        r_copy["conditions"] = filtered_conds
        out.append(r_copy)
    return out


def _friendly_llm_error(err: str) -> str:
    """将 llamamanage.request 的原始错误串映射为友好提示；无法归类的原样返回。"""
    if any(k in err for k in _LLM_TIMEOUT_MARKERS):
        return _llm_timeout_hint()
    if any(k in err for k in _LLM_BAD_REQUEST_MARKERS):
        return _llm_bad_request_hint()
    if any(k in err for k in _LLM_CONN_ERROR_MARKERS):
        return _llm_conn_error_hint()
    if any(k in err for k in _LLM_PEG_MARKERS):
        return _llm_peg_native_hint(err)
    if any(k in err for k in _LLM_SERVER_ERROR_MARKERS):
        return _llm_server_error_hint(err)
    return err


def _active_engine_label() -> str:
    """当前推理引擎显示名（vLLM-Omni / llama-server），探测失败回退 llama-server。"""
    try:
        from llamamanage import _active_engine

        return "vLLM-Omni" if _active_engine() == "vllm" else "llama-server"
    except Exception:
        return "llama-server"


def _llm_conn_error_hint() -> str:
    """按当前推理引擎生成「无法连接服务」的友好提示（llama-server 8080 / vLLM-Omni 8000）。"""
    try:
        from configmanage import get_config

        cfg = get_config(show_dialogs=False) or {}
        if _active_engine_label() == "vLLM-Omni":
            port = (cfg.get("vllm_server_args") or {}).get("port") or "8000"
            return (
                f"无法连接本地 vLLM-Omni（127.0.0.1:{port}）。深度校对依赖 vLLM-Omni："
                "epub 转换流程会自动启动；若通过 correct 直接矫正，需先手动启动"
                f"（并确认 config 中 vllm_server 路径正确、服务监听 {port} 端口）。"
            )
        port = (cfg.get("llama_server_args") or {}).get("port") or "8080"
        return (
            f"无法连接本地 llama-server（127.0.0.1:{port}）。深度校对依赖 llama-server："
            "epub 转换流程会自动启动；若通过 correct 直接矫正，需先手动启动"
            f"（并确认 config 中 llama_server 路径正确、服务监听 {port} 端口）。"
        )
    except Exception:
        return _LLM_CONN_ERROR_HINT


def _llm_timeout_hint() -> str:
    """按当前推理引擎生成超时提示。"""
    try:
        return f"深度校对请求超时。可稍后重试，或检查 {_active_engine_label()} 负载/是否卡住。"
    except Exception:
        return _LLM_TIMEOUT_HINT


def _llm_bad_request_hint() -> str:
    """按当前推理引擎生成 400 Bad Request 提示（模型加载中 / 服务与所选模型不符）。"""
    try:
        engine = _active_engine_label()
    except Exception:
        engine = "模型服务"
    return (
        f"{engine} 返回 400 错误（Bad Request）。常见原因：模型仍在加载中"
        "（请稍后重试），或当前服务加载的模型与所选模型不符"
        "（请先停止服务，再启动所选模型后重试）。"
    )


def _llm_server_error_hint(detail: str = "") -> str:
    """按当前推理引擎归属返回 500 服务端错误的可操作提示（保留原详情）。"""
    extra = f"（服务端详情：{detail}）" if detail.strip() else ""
    try:
        engine = _active_engine_label()
    except Exception:
        engine = "模型服务"
    if engine == "vLLM-Omni":
        return (
            f"{engine} 返回 500 内部错误。常见原因：上下文长度不足、模型/mmproj 加载"
            f"异常或 GPU 显存不足。建议：降低识别精度/缩小页面图像，或重启服务后重试。"
        ) + extra
    return (
        f"{engine} 返回 500 内部错误。常见原因：上下文（--ctx-size）不足、模型与图像"
        f"投影（mmproj）不匹配、模型文件未完整加载，或 GPU 显存不足。建议：检查服务"
        f"日志定位具体原因，必要时停服务后重启加载所选模型再重试。"
    ) + extra


def _llm_peg_native_hint(detail: str = "") -> str:
    """llama.cpp 多模态（vision）推理失败 'peg-native format' 的可操作提示。"""
    extra = f"（服务端详情：{detail}）" if (detail or "").strip() else ""
    engine = "vLLM-Omni" if _active_engine_label() == "vLLM-Omni" else "llama-server"
    if engine == "vLLM-Omni":
        return (
            "vLLM-Omni 多模态推理失败：模型未能产出符合预期的视觉投影输出。"
            "常见原因：mmproj 与主模型不匹配、模型未配置图像能力，或加载的模型"
            "并非视觉模型。建议：检查所选模型是否支持图像输入，或更换为视觉 OCR 模型后重试。"
        ) + extra
    return (
        "llama-server 多模态（vision）推理失败：当前服务未能产出符合预期的视觉投影"
        "输出（peg-native format）。常见原因：① 当前运行的是纯文本模式（未加载 mmproj"
        "视觉投影，例如为句子校正启动的服务），收图必然失败——请先「停止服务」，再"
        "「启动服务」加载所选视觉模型后重试；② mmproj 与主模型不匹配 / llama-server 版本不兼容"
        "——请核对 mmproj 文件名与模型匹配，必要时升级或更换 llama-server / mmproj 后重试。"
    ) + extra


def _strip_trailing_commas(s: str) -> str:
    """删除 JSON 中的尾随逗号（`[,]`/`{,}`），字符串内容不受影响。

    模型常在数组/对象末尾多打一个逗号导致整串解析失败；此清洗仅去掉
    紧邻 `]`/`}`（跳过空白）的逗号，带引号字符串状态机保证 `"],"` 等
    字符串内的逗号不被误删。
    """
    out = []
    in_str = False
    esc = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
        elif ch == ",":
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j < n and s[j] in "]}":
                i += 1  # 丢弃尾随逗号
                continue
            out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _parse_llm_suggestions(raw: str, text: str, convert_t2s: bool = False):
    """解析模型返回的 JSON 建议（响应中间层，2026-08-07）。

    容忍：markdown 代码围栏、前后说明文字、多个 JSON 对象/尾随内容
    （旧实现贪婪正则 `{.*}` 会把多余内容一并吞入 → json.loads 抛
    "Extra data"——用户实测「深度校对失败： Extra data: line 1 column 141」）、
    截断/尾随逗号。返回 (suggestions, error_str|None)。

    每项归一校验：{start:int, end:int, wrong:str, candidates:[{text,score}]}；
    start/end 越界或 wrong 与原文片段不符时用 str.find 重定位，找不到则丢弃该项；
    同位置重复项合并候选。所有失败路径返回中文提示，不透出原始英文异常。

    convert_t2s=True 时，wrong 与 candidates text 先做繁体→简体转换（zhconv），
    再与当前简体原文比对定位（LLM 深度校对场景，模型可能返回繁体）。默认 False 行为逐字节不变。
    """
    _ttos = None
    try:
        import json as _json

        cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw or "").strip()
        if not cleaned:
            return [], "模型响应为空"
        obj = None
        decoder = _json.JSONDecoder()

        def _loads_whole(s):
            # 返回 dict=整串解析成功；"non-dict"=成功但顶层不是对象；None=解析失败
            try:
                o = _json.loads(s)
            except (TypeError, ValueError):
                return None
            return o if isinstance(o, dict) else "non-dict"

        # 1) 整串直接解析（正常输出 / 尾随逗号已清洗——数组/对象末尾多逗号场景）
        whole_non_dict = False
        for cand in (cleaned, _strip_trailing_commas(cleaned)):
            o = _loads_whole(cand)
            if o == "non-dict":
                whole_non_dict = True
                continue
            if o is not None:
                obj = o
                break
        if obj is None and whole_non_dict:
            return [], "模型响应解析失败：JSON 顶层不是对象"
        if obj is None:
            # 2) 逐 '{' 起点 raw_decode：优先取带 suggestions 的完整对象
            #    （JSON 后尾随文字 / 多对象场景——旧贪婪正则 `{.*}` 的坑；
            #    截断 JSON 时内侧完整对象兜底，随后按缺字段报错）
            first_dict = None
            for m in re.finditer(r"\{", cleaned):
                try:
                    o, _ = decoder.raw_decode(cleaned[m.start() :])
                except _json.JSONDecodeError:
                    continue
                if not isinstance(o, dict):
                    continue
                if first_dict is None:
                    first_dict = o
                if "suggestions" in o:
                    obj = o
                    break
            if obj is None:
                obj = first_dict
        if obj is None:
            return [], "模型响应解析失败：无法提取有效 JSON"
        if not isinstance(obj, dict):
            return [], "模型响应解析失败：JSON 顶层不是对象"
        sugs = obj.get("suggestions")
        if sugs is None:
            return [], "模型响应解析失败：缺少 suggestions 字段"
        if not isinstance(sugs, list):
            return [], "模型响应解析失败：suggestions 不是数组"

        n = len(text)
        out = []
        index = {}
        for s in sugs:
            if not isinstance(s, dict):
                continue
            start_raw = s.get("start")
            end_raw = s.get("end")
            if not isinstance(start_raw, (int, float, str)) or not isinstance(
                end_raw, (int, float, str)
            ):
                continue
            start, end = int(start_raw), int(end_raw)
            wrong = str(s.get("wrong") or "").strip()
            if not wrong or start < 0 or end <= start:
                continue
            # 繁体→简体转换（LLM 深度校对场景，模型可能返回繁体）
            if convert_t2s and _ttos is None:
                from stringmanage import ttos as _ttos_fn

                _ttos = _ttos_fn
            if _ttos is not None:
                wrong = _ttos(wrong)
                if not wrong:
                    continue
            # 模型位置可能漂移：wrong 与原文片段不符时用 find 重定位
            if start >= n or end > n or text[start:end] != wrong:
                idx = text.find(wrong)
                if idx < 0:
                    continue
                start, end = idx, idx + len(wrong)
            cands = []
            for c in s.get("candidates") or []:
                if not isinstance(c, dict):
                    continue
                ctext = str(c.get("text") or "").strip()
                if not ctext:
                    continue
                if _ttos is not None:
                    ctext = _ttos(ctext)
                    if not ctext:
                        continue
                if ctext == wrong:
                    continue
                score_raw = c.get("score")
                if isinstance(score_raw, (int, float, str)):
                    try:
                        score = float(score_raw)
                    except (TypeError, ValueError):
                        score = 0.9
                else:
                    score = 0.9
                cands.append({"text": ctext, "score": score})
            key = (start, end, wrong)
            if key in index:
                # 同位置重复项：合并候选（按 text 去重）
                existing = index[key]
                for c in cands:
                    if not any(x["text"] == c["text"] for x in existing["candidates"]):
                        existing["candidates"].append(c)
                continue
            index[key] = {
                "start": start,
                "end": end,
                "wrong": wrong,
                "candidates": cands,
            }
            out.append(index[key])
        return out, None
    except Exception as e:
        return [], f"模型响应解析失败：{_friendly_llm_error(str(e))}"


@contextmanager
def _proofread_engine_ctx():
    """矫正/重识别操作期间把推理引擎钉扎为校正引擎（llamamanage._active_proofread_engine
    ——config.json 顶层 proofread_engine 键，缺省 llama），退出后恢复按配置。

    2026-08：矫正引擎与识别引擎分离——PDF 识别可能用 vllm/paddle，而矫正界面/深度
    校对/重识别这类逐字大模型操作统一走校正引擎，避免矫正请求发到错误的引擎端口。
    """
    import llamamanage

    pe = llamamanage._active_proofread_engine()
    llamamanage.set_engine(pe)
    try:
        yield pe
    finally:
        llamamanage.set_engine(None)


def ime_to_english() -> tuple[bool, str]:
    """把前台窗口的输入法从「中文模式」切到「英文模式」（Windows only）。

    浏览器 JS 无法切换输入法，只能由本地服务端调 Win32 接口：矫正界面在
    INSERT→NORMAL（Esc / Ctrl+[）时按设置 ui_settings.ime_switch_on_esc 调
    /api/ime_english（2026-10-30 新增）。真正的实现与兼容性说明见 imemanage：

      * 只用 IMM32「转换模式」消息（WM_IME_CONTROL + IMC_SETCONVERSIONMODE），
        把当前输入法内部的中/英状态切到英文；
      * **不切换键盘布局/输入语言**（不再请求把输入语言切到 en-US 键盘布局），
        所以搜狗/百度/微软拼音不会被换成「美式键盘」；
      * 已经是英文 → 什么也不做；读不到状态 → 不盲切。

    返回 (现在是否处于英文模式, 说明)；非 Windows / 任何失败都返回 (False, 原因)，
    调用方静默忽略，不影响按键本身。
    """
    import imemanage  # 惰性导入：IME 只在 Windows 下有实际动作

    return imemanage.ime_to_english()


def _proofread_llm_enhance(text: str, errors: list, model_key: str = "qwen2b"):
    """Call llama-server via llamamanage.request to get additional sentence-level suggestions.
    Returns (suggestions, error_str|None): suggestions is a list of error dicts matching
    proofread format; error_str is None on success. Best-effort: failures returned, not raised
    (caller decides how to surface them — 2026-08-07 起不再静默吞掉).
    """
    try:
        import llamamanage

        prompt = (
            "你是中文校对助手。输入为一段文本，请检查是否存在真实的错字、用词不当、语序问题或不通顺的片段。"
            " 如果存在，请只输出一个 JSON 对象，不要任何额外文字、说明或代码围栏："
            ' {"suggestions": [{"start": int, "end": int, "wrong": str, "candidates": [{"text": str, "score": float}]}]}'
            ' 示例：{"suggestions": [{"start": 2, "end": 4, "wrong": "那个", "candidates": [{"text": "这个", "score": 0.9}]}]}'
            " start/end 为 wrong 在文本中的字符位置（从 0 开始）；candidates 至少 1 项；"
            " 仅在确定有改进价值时返回建议；不要返回空候选。\n文本:\n" + text
        )
        # append_ocr_instruction=False：不要追加"按原文原格式输出"（会与 JSON 指令冲突，2026-08-07 修复）
        # 矫正/深度校对用校正引擎（proofread_engine），与识别引擎分离（2026-08 修复）
        with _proofread_engine_ctx():
            res = llamamanage.request(
                prompt, model_key=model_key, thinking=False, append_ocr_instruction=False
            )
        if res.get("error"):
            return [], _friendly_llm_error(str(res.get("error")))
        return _parse_llm_suggestions(
            str(res.get("result") or ""), text, convert_t2s=True
        )
    except Exception as e:
        return [], _friendly_llm_error(str(e))


def _strip_ws(text):
    """去全部空白字符（含 \\n、空格、\\u3000 等，ch.isspace()），返回 (去空白文本, 位置映射)。

    位置映射：norm[i] = 原文本中对应字符的下标。
    """
    out, pos = [], []
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        out.append(ch)
        pos.append(i)
    return "".join(out), pos


_TRAILING_PAGE_NUM_RE = re.compile(
    r"(?:"
    # 分支A：明确页码样式，前可有空白/换行，末尾直接剥（无需独立成行）
    r"[\n\r\s]*"
    r"(?:"
    r"第\s*\d+\s*页"  # 第 N 页
    r"|"
    r"[Pp][\s.]*\d{1,4}"  # P123 / p.123
    r"|"
    r"页码?\s*\d{1,4}"  # 页码123 / 页123
    r"|"
    r"No\.?\s*\d{1,4}"  # No.123 / No123
    r"|"
    r"[〔【\[（(［〈《「『]\s*\d{1,4}\s*[〕】\]）)］〉》」』]"  # 括号包裹：[121]/（121）等
    r"|"
    r"[·・•‧∙]\s*\d{1,4}\s*[·・•‧∙]"  # 间隔号包裹：·171· / ・171・ / •187• / ‧192‧ / ∙192∙
    r")"
    r"|"
    # 分支B：裸数字，必须独立成行（前有换行），避免误删正文末尾真实数字
    r"[\n\r][\s\-—–·・.。、_]*\d{1,4}"
    r")"
    r"[\s。.!?！？]*$"
)


def _strip_trailing_page_number(text: str) -> str:
    """剥掉重识别返回文字末尾的页码，再与原文本对比（2026-08-28 新增）。

    仅当页码位于整段返回文字的最末尾（允许末尾有空白/句号）才清理，避免误删
    正文。支持样式：
      - 第 N 页
      - 字符+数字：页123 / 页码123 / P123 / p.123 / No.123
      - 括号包裹：[121] 【121】 〔121〕 （121）等（保持原样不归一）
      - 裸数字 1-4 位：仅当独立成行（前有换行）或整段即页码，避免剥掉正文末尾真实数字
    返回清理后的文本（原串无末尾页码则原样返回）。
    """
    if not text:
        return text
    # 整段仅 1-4 位数字（纯页码）
    if re.fullmatch(r"\d{1,4}", text.strip()):
        return ""
    return _TRAILING_PAGE_NUM_RE.sub("", text)


def diff_reocr_texts(current: str, new_text: str) -> list:
    """逐字对比 current 与 new_text 的文字内容，忽略全部空白差异。

    只按文字内容对比，段落/换行分割不一致不产生标注；相同文本不标注；不同处
    标注（划线 + 校正结果）。逐字对齐（去空白后字符级 SequenceMatcher，
    autojunk=False 避免中文常见字被排除出匹配）。

    增字（原文本多字）→ candidates=[] 纯划线；少字（原文本缺字）→ 锚定相邻
    字符使前端可渲染插入文本。输出与 proofread_page 同形状：
    {start, end, wrong, candidates: [str, ...], line}，candidates 一律纯字符串列表
    （前端 join('/') 渲染、candidates[0] 替换，dict 会渲染成 [object Object]，严禁 dict）。
    start/end 为 current 原始字符偏移（可含内部空白）。空 current + 新文本有内容
    → 返回单条插入建议（wrong=""，candidates=[new_text.strip()]），供前端填充空白页；
    双方均空 → []。
    """
    cur_norm, cur_pos = _strip_ws(current)
    new_norm, _ = _strip_ws(new_text)
    out = []
    # 空 current + 新文本有内容 → 视为空白页填充：返回单条插入建议
    if not cur_norm and new_norm:
        return [
            {
                "start": 0,
                "end": 0,
                "wrong": "",
                "candidates": [new_text.strip()],
                "line": 1,
            }
        ]
    # autojunk=False：中文常见字（如「的」）默认会被 autojunk 排除出匹配，导致对齐错乱
    sm = SequenceMatcher(None, cur_norm, new_norm, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            # 防御性检查：去空白后理论上不会相等，但以防万一
            if cur_norm[i1:i2] == new_norm[j1:j2]:
                continue
            # 收窄替换区间：剥掉两端相同的字符，只标注真正差异的核心。
            # 否则 SequenceMatcher 的粗粒度 replace 会把大段相同文字一并划线，
            # 视觉上像「文字错位/整段被标红」（2026-08 用户反馈修复）。
            a_seg = cur_norm[i1:i2]
            b_seg = new_norm[j1:j2]
            p = 0
            while p < len(a_seg) and p < len(b_seg) and a_seg[p] == b_seg[p]:
                p += 1
            s = 0
            while (
                s < len(a_seg) - p
                and s < len(b_seg) - p
                and a_seg[len(a_seg) - 1 - s] == b_seg[len(b_seg) - 1 - s]
            ):
                s += 1
            ni1, ni2 = i1 + p, i2 - s
            nj1, nj2 = j1 + p, j2 - s
            if ni1 >= ni2:
                # 收窄后原文本侧为空 → 纯增字：锚定相邻现有字符使前端可渲染
                if not cur_norm:
                    continue
                if ni1 > 0:
                    anchor = ni1 - 1
                    start = cur_pos[anchor]
                    end = start + 1
                    out.append(
                        {
                            "start": start,
                            "end": end,
                            "wrong": current[start:end],
                            "candidates": [cur_norm[anchor] + new_norm[nj1:nj2]],
                            "line": 1 + current.count("\n", 0, start),
                        }
                    )
                else:
                    start = cur_pos[0]
                    end = start + 1
                    out.append(
                        {
                            "start": start,
                            "end": end,
                            "wrong": current[start:end],
                            "candidates": [new_norm[nj1:nj2] + cur_norm[0]],
                            "line": 1 + current.count("\n", 0, start),
                        }
                    )
            elif nj1 >= nj2:
                # 收窄后新文本侧为空 → 原文本增字：纯划线无候选
                start = cur_pos[ni1]
                end = cur_pos[ni2 - 1] + 1
                out.append(
                    {
                        "start": start,
                        "end": end,
                        "wrong": current[start:end],
                        "candidates": [],
                        "line": 1 + current.count("\n", 0, start),
                    }
                )
            else:
                start = cur_pos[ni1]
                end = cur_pos[ni2 - 1] + 1
                out.append(
                    {
                        "start": start,
                        "end": end,
                        "wrong": current[start:end],
                        "candidates": [new_norm[nj1:nj2]],
                        "line": 1 + current.count("\n", 0, start),
                    }
                )
        elif tag == "delete":
            # 原文本增字：新文本没有这些字符 → 纯划线无候选
            start = cur_pos[i1]
            end = cur_pos[i2 - 1] + 1
            out.append(
                {
                    "start": start,
                    "end": end,
                    "wrong": current[start:end],
                    "candidates": [],
                    "line": 1 + current.count("\n", 0, start),
                }
            )
        elif tag == "insert":
            # 原文本少字：新文本有字符原文本没有 → 锚定相邻现有字符使前端可渲染
            if not cur_norm:
                # 空 current 全 insert → 无可锚定 → 跳过
                continue
            if i1 > 0:
                # 插入点在中间/末尾：锚定前邻字符
                anchor = i1 - 1
                start = cur_pos[anchor]
                end = start + 1
                out.append(
                    {
                        "start": start,
                        "end": end,
                        "wrong": current[start:end],
                        "candidates": [cur_norm[anchor] + new_norm[j1:j2]],
                        "line": 1 + current.count("\n", 0, start),
                    }
                )
            else:
                # i1 == 0，文首插入：锚定后邻字符（anchor = 0）
                start = cur_pos[0]
                end = start + 1
                out.append(
                    {
                        "start": start,
                        "end": end,
                        "wrong": current[start:end],
                        "candidates": [new_norm[j1:j2] + cur_norm[0]],
                        "line": 1 + current.count("\n", 0, start),
                    }
                )
    out.sort(key=lambda x: x["start"])
    return out


def _table_open_attrs(d: dict) -> str:
    """表格级保留属性串（无边框 class + 固定布局 style）；无存活项返回 ""。

    单一事实来源：``<table>`` 的两处重建（``_Sanitizer.handle_starttag`` 与
    ``apply_markers`` 块流）都调用它，口径一致——class 白名单 ``TBL_TABLE_CLASSES``
    （分栏/批注版式的 ``ptoe-noborder``）+ ``rulemanage.normalize_table_style``
    （只认 ``table-layout:fixed``）。2026-10 修复：此前 apply_markers 用
    ``_block_class_html`` 重建表标签，白名单不含 ``ptoe-noborder`` 也不带 style，
    导出 EPUB 的 ``<table>`` 丢掉无边框 class → 分栏表格显示框线。
    """
    tcls = [c for c in str(d.get("class") or "").split() if c in TBL_TABLE_CLASSES]
    tstyle = rulemanage.normalize_table_style(str(d.get("style") or ""))
    return (f' class="{" ".join(tcls)}"' if tcls else "") + (
        f' style="{tstyle}"' if tstyle else ""
    )


def _table_open_attrs_str(attrs: str) -> str:
    """``<table …>`` 属性**字符串** → 表格级保留属性串（见 ``_table_open_attrs``）。"""
    return _table_open_attrs(dict(re.findall(r'([\w:-]+)\s*=\s*"([^"]*)"', attrs or "")))


def _block_class_html(attrs: str) -> str:
    """从块标签属性中提取应保留的 class（ptoe-note + 对齐类 + 换页 + 图片模式 + 题注 + 分隔线），返回 class 属性。"""
    m = re.search(r'class="([^"]*)"', attrs)
    if not m:
        return ""
    keep = [
        c
        for c in m.group(1).split()
        if c == _NOTE_CLASS
        or c in _ALIGN_CLASSES
        or c == _ALIGN_LINES_CLASS
        or c == _PAGE_BREAK_CLASS
        or c in _IMG_CLASSES
        or c == "ptoe-flush"
        or c == "ptoe-indent"
        or c == "ptoe-citation"
        or c == _CAPTION_CLASS
        # 分隔线（2026-09-27）：基类 + 样式后缀两个类都要留，前缀判定
        or c.startswith(DIVIDER_CLASS)
    ]
    return f' class="{" ".join(keep)}"' if keep else ""


def _block_classes(attrs: list[tuple[str, str | None]]) -> list[str]:
    """块级标签应保留的 class 列表（ptoe-note + 对齐类 + 换页 + 图片模式 + 题注 + 分隔线）。"""
    keep: list[str] = []
    for k, v in attrs:
        if k == "class":
            for c in (v or "").split():
                if (
                    c == _NOTE_CLASS
                    or c in _ALIGN_CLASSES
                    or c == _ALIGN_LINES_CLASS
                    or c == _PAGE_BREAK_CLASS
                    or c in _IMG_CLASSES
                    or c == "ptoe-flush"
                    or c == "ptoe-indent"
                    or c == "ptoe-citation"
                    or c == _CAPTION_CLASS
                    # 分隔线（2026-09-27）：基类 + 样式后缀两个类都要留，前缀判定
                    or c.startswith(DIVIDER_CLASS)
                ):
                    keep.append(c)
    return keep


# 段落缩进/间距设置（2026-08）：随块级标签落盘的 data 属性白名单。
# data-pl/data-pr=左/右缩进(em 字符)、data-ind=特殊格式(first=首行|hang=悬挂)、
# data-indv=特殊缩进值(em)、data-spb/data-spa=段前/段后(行)、data-lh=行距倍数；
# 导出 EPUB 时由 htmlmanage 转为内联样式（margin/text-indent/line-height）。
_INDENT_DATA_ATTRS = (
    "data-pl",
    "data-pr",
    "data-ind",
    "data-indv",
    "data-spb",
    "data-spa",
    "data-lh",
)
_INDENT_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_INDENT_MODES = ("first", "hang")


def _block_data_attrs(attrs: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
    """提取块级标签应保留的段落缩进/间距 data 属性（值非法则丢弃）。"""
    keep: list[tuple[str, str]] = []
    for k, v in attrs:
        if k in _INDENT_DATA_ATTRS and v:
            v = v.strip()
            if k == "data-ind":
                if v in _INDENT_MODES:
                    keep.append((k, v))
            elif _INDENT_NUM_RE.match(v):
                keep.append((k, v))
    return keep


_INDENT_DATA_ATTR_RE = re.compile(
    r'(data-(?:pl|pr|ind|indv|spb|spa|lh))="([^"]*)"', flags=re.I
)


def _block_indent_attr_html(attrs: str) -> str:
    """从块标签**属性串**提取段落设置 data 属性，返回 ' data-x="1" …' 或 ''。

    apply_markers 按块重建开标签（render_block）时必须用它：只保留 class 的旧实现
    会把「段落设置」面板写入的 data-pl/data-ind/data-spb/data-lh 等全部剥掉，
    htmlmanage._indent_style_attrs 拿不到任何属性 → 导出 EPUB 退回默认顶格排版
    （用户可见症状：段前缩进/首行缩进/行距等设置不生效，2026-09-30 修复）。
    取值规则与 _block_data_attrs 一致：data-ind 只认 first/hang，其余只认数值，
    重复属性只留第一个，保证输出标签属性合法且可被 XHTML 解析。
    """
    keep: list[str] = []
    seen: set[str] = set()
    for m in _INDENT_DATA_ATTR_RE.finditer(attrs or ""):
        k, v = m.group(1).lower(), m.group(2).strip()
        if not v or k in seen:
            continue
        if k == "data-ind":
            if v not in _INDENT_MODES:
                continue
        elif not _INDENT_NUM_RE.match(v):
            continue
        seen.add(k)
        keep.append(f'{k}="{v}"')
    return f' {" ".join(keep)}' if keep else ""


def _normalize_note(text: str) -> str:
    """注释文本规范：ASCII 半角括号统一为中文全角括号（（ ））。"""
    return text.replace("(", "（").replace(")", "）")


def _note_already_parenthesized(text: str) -> bool:
    """注释是否已带括号（视为「已在正文中」）：剥掉行内标签后首尾为中文括号。"""
    t = re.sub(r"<[^>]+>", "", text).strip()
    return t.startswith("（") and t.endswith("）")


# 令牌化：优先把「完整的标记 span 元素」作为一个令牌，其余按单个标签切分。
# 这样块内切段时能直接拿到标记的类型与文本，不必在三个令牌（开标签/文本/闭标签）间拼凑。
# 属性顺序无关（class 可能在 data-ptoe-marker 之前，见 _MARKER_SPAN_RE 注释）。
_TOKEN_RE = re.compile(
    r'(<span\b[^>]*\bdata-ptoe-marker="[^"]*"[^>]*>.*?</span>|<[^>]+>)',
    flags=re.IGNORECASE | re.DOTALL,
)

# 行内标签白名单（与 sanitize_html 放行集合一致）：标记切在行内标签内部时，
# 前段补闭合、后段重新打开，保证每段都是合法片段。
_INLINE_TAGS = frozenset({"strong", "em", "span"})

# 已渲染块的结构化判定（取代脆弱的 `startswith("<p")` / `endswith("</p>")` 字符串比较，
# 避免换标签/加属性后合并判断静默失效——这类"看起来只是样式微调却弄坏合并"的改动
# 曾多次导致注释段落标记不生效）：
#   1) _RENDERED_BLOCK_RE：可合并的段落块 = 单个 <p>/<div> 元素；
#   2) _PAGE_BREAK_ATTR_RE：换页占位块不是段落，不能作为合并目标。
_RENDERED_BLOCK_RE = re.compile(r"^(<(p|div)\b[^>]*>)(.*)(</\2>)$", re.DOTALL | re.IGNORECASE)
_PAGE_BREAK_ATTR_RE = re.compile(r'class="[^"]*\bptoe-page-break\b', re.IGNORECASE)
_NOTE_ATTR_RE = re.compile(r'class="[^"]*\bptoe-note\b', re.IGNORECASE)


def _wrap_note_span(html: str) -> str:
    """把内容包成行内注释 span（小字灰字，CSS 对 p.ptoe-note / span.ptoe-note 同款样式）。

    用于"注释段落与正文段落被段落标记合并"：合并后的块只能是"整段注释"或
    "整段正文"一种，另一侧的内容必须包成行内注释，才能各自保留它在矫正界面里
    的字号（否则整段被染成小字灰字——标记前的正文也会跟着缩小）。
    """
    if not html or not html.strip():
        return html
    return f'<span class="{_NOTE_CLASS}">{html}</span>'


def _strip_note_class(open_tag: str) -> str:
    """去掉开标签 class 中的 ptoe-note（注释段被正文并入时降级为正文段）。"""
    m = re.search(r'class="([^"]*)"', open_tag)
    if not m:
        return open_tag
    keep = [c for c in m.group(1).split() if c != _NOTE_CLASS]
    if keep:
        return open_tag[: m.start(1)] + " ".join(keep) + open_tag[m.end(1):]
    return re.sub(r"\s+>", ">", open_tag[: m.start()] + open_tag[m.end():])


def _merge_paragraph(last_rendered: str, html: str, *, note: bool) -> str | None:
    """把 html 并入上一个已渲染段落块并返回新块 HTML；不可合并时返回 None。

    合并规则（2026-09 统一，替代此前"只允许注释↔注释"的特殊分支）：
    - 段落标记（join）合并相邻的两个段落，与是否为注释段落无关；
    - 前一块的对齐/缩进等属性保留（既有约定：合并保留首段属性）；
    - **字号不受污染**：只有当参与合并的各段"都是注释"时合并结果才是注释段；
      一旦有正文段参与（注释+正文混合），合并结果是正文段，其中的注释内容用
      `<span class="ptoe-note">` 包起来——这样标记两侧各自的字号与矫正界面一致
      （此前把 ptoe-note 直接并到段落上，会让标记前的正文一起变成小字灰字）。
    """
    m = _RENDERED_BLOCK_RE.match(last_rendered.strip())
    if not m:
        return None
    open_tag, inner, close_tag = m.group(1), m.group(3), m.group(4)
    if _PAGE_BREAK_ATTR_RE.search(open_tag):
        return None  # 换页占位块不是段落
    if _NOTE_ATTR_RE.search(open_tag):
        if not note:
            # 注释段被正文并入 → 整块降级为正文段，已并入的注释内容包成行内注释
            open_tag = _strip_note_class(open_tag)
            inner = _wrap_note_span(inner)
    elif note:
        # 正文段并入注释内容 → 该段内容包成行内注释，段落本身保持正文
        html = _wrap_note_span(html)
    return open_tag + inner + html + close_tag


def _is_note_block(attrs: list[tuple[str, str | None]]) -> bool:
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
    （ptoe-img-full / ptoe-img-fit）、尺寸 class（ptoe-img-w25/50/75/100）；
    <p> 额外保留位置 class（ptoe-img-left/center/right）。
    <div> 归一化为 <p>，<b>/<i> 归一化为 <strong>/<em>；其余标签整体丢弃
    仅保留文本；script/style 等非内容标签连同内容一起丢弃。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.buf: list[str] = []
        self.stack: list[tuple[str, str]] = []  # 未闭合的行内标签 [(名, 原开标签)]
        self.block: tuple[str, int] | None = None  # ('p', 0) | ('h', level)
        self.classes: list[str] = []  # 当前块保留的 class（ptoe-note + 对齐类）
        self.data_attrs: list[tuple[str, str]] = []  # 当前块保留的缩进/间距 data 属性
        self.skip: int = 0  # 非内容标签嵌套深度
        self._reopen: list[tuple[str, str]] = []  # 跨块未闭合的行内标签（顺延重开）
        # 表格累积模式（2026-09-20 插入表格支持）：
        # 进入 <table> 后暂存结构，整表结束时产出规范 <table>…</table> 块。
        self._tbl_depth = 0  # 表格嵌套深度（>0 = 处于表格累积模式）
        self._tbl_skip = 0  # 嵌套表格丢弃深度（内容整体跳过，防递归炸）
        self._tbl_has_thead = False  # 输入是否出现过 <thead>
        self._tbl_in_thead = False  # 当前行是否属于 thead 分组
        self._tbl_head: list[list[str]] = []  # thead 行（每行 = cell inner html 列表）
        self._tbl_body: list[list[str]] = []  # tbody 行
        self._tbl_row: list[str] | None = None  # 当前行 cells
        self._cell_open = False  # 当前是否处于 th/td 单元格
        self._cell_th = False  # 当前单元格是否为 th
        self._cell_attrs: list[str] = []  # 当前单元格保留的 colspan/rowspan 属性串
        self._cell_buf: list[str] = []  # 单元格行内内容缓冲
        self._cell_stack: list[tuple[str, str]] = []  # 单元格内未闭合行内标签
        # 单元格内未闭合的块级（段落）深度（2026-10）：单元格内 p/div/hN 转成
        # <p>…</p> 保留段落边界，不再整段拍平（否则同一格内多段会在导出后并成一段）
        self._cell_block_depth = 0
        # 表格级保留属性（2026-10 列宽按比例生效）：目前只可能携带 style="table-layout:fixed"
        self._tbl_table_attrs = ""

    def _inline_target(self) -> tuple[list[str], list[tuple[str, str]]]:
        """当前行内写入目标：表格单元格内 → (cell_buf, cell_stack)；否则 → (buf, stack)。"""
        if self._tbl_depth and self._cell_open:
            return self._cell_buf, self._cell_stack
        return self.buf, self.stack

    def _tbl_finish_cell(self) -> None:
        """结束当前单元格：把 cell inner HTML 附加到当前行（空单元格也保留）。"""
        if not self._cell_open:
            return
        if self._tbl_row is None:
            self._tbl_row = []
        # 单元格内未闭合的段落（<p>）先收尾，保证 <td><p>…</p></td> 结构合法
        if self._cell_block_depth:
            self._cell_buf.append("</p>" * self._cell_block_depth)
            self._cell_block_depth = 0
        closes = "".join(
            f"</{t}>" for t, _ in reversed(self._cell_stack) if t != "__skip_mark__"
        )
        inner = "".join(self._cell_buf) + closes
        tag = "th" if self._cell_th else "td"
        attrs = (" " + " ".join(self._cell_attrs)) if self._cell_attrs else ""
        self._tbl_row.append(f"<{tag}{attrs}>{inner}</{tag}>")
        self._cell_open = False
        self._cell_buf = []
        self._cell_stack = []

    def _tbl_finish_row(self) -> None:
        """结束当前行：按所属分组（thead/tbody）收进行列表。"""
        self._tbl_finish_cell()
        if self._tbl_row is not None:
            (self._tbl_head if self._tbl_in_thead else self._tbl_body).append(
                self._tbl_row
            )
            self._tbl_row = None

    def _tbl_finish_table(self) -> None:
        """表格结束：产出规范 <table>…</table> 块（行内 tight 无多余空白；无 thead 则省略）。"""
        if not self._tbl_depth:
            return
        self._tbl_finish_row()
        parts: list[str] = [f"<table{self._tbl_table_attrs}>"]
        if self._tbl_head:
            parts.append("<thead>")
            for row in self._tbl_head:
                parts.append("<tr>" + "".join(row) + "</tr>")
            parts.append("</thead>")
        if self._tbl_body:
            parts.append("<tbody>")
            for row in self._tbl_body:
                parts.append("<tr>" + "".join(row) + "</tr>")
            parts.append("</tbody>")
        parts.append("</table>")
        self.blocks.append("".join(parts))
        self._tbl_depth = 0
        self._tbl_skip = 0
        self._tbl_has_thead = False
        self._tbl_in_thead = False
        self._tbl_head = []
        self._tbl_body = []
        self._tbl_row = None

    def _flush(self) -> None:
        if self.block is None:
            return
        closes = "".join(
            f"</{t}>" for t, _ in reversed(self.stack) if t != "__skip_mark__"
        )
        content = "".join(self.buf) + closes
        if content.strip():
            cls = f' class="{" ".join(self.classes)}"' if self.classes else ""
            dattr = "".join(
                f' {k}="{_html.escape(v, quote=True)}"' for k, v in self.data_attrs
            )
            if self.block[0] == "p":
                self.blocks.append(f"<p{cls}{dattr}>{content}</p>")
            else:
                lv = self.block[1]
                self.blocks.append(f"<h{lv}{cls}{dattr}>{content}</h{lv}>")
        # 跨块未闭合的行内格式 span：本块补闭合，同时在下一块开头重新打开——
        # 否则「选中跨段文字设格式」时从第二块起格式被静默丢弃（2026-09-16）。
        # 只顺延本应用的行内格式 span（ptoe-*，不含 data-ptoe-marker 标记 span，
        # 标记顺延会污染下一块）；strong/em 不顺延：外部脏数据里一个未闭合的
        # <b> 会把后续所有段落都变粗（旧行为更安全，且界面自身的加粗按块应用）。
        self._reopen = [
            (t, o)
            for t, o in self.stack
            if t == "span" and "data-ptoe-marker" not in o
        ]
        self.buf = []
        self.stack = []
        self.block = None
        self.classes = []
        self.data_attrs = []

    def _open_block(
        self,
        kind: str,
        level: int = 0,
        classes: list[str] | None = None,
        data_attrs: list[tuple[str, str]] | None = None,
    ) -> None:
        self._flush()
        self.block = (kind, level)
        self.classes = list(classes) if classes else []
        self.data_attrs = list(data_attrs) if data_attrs else []
        # 承接上一块跨块未闭合的行内标签：在新块开头重新打开
        if self._reopen:
            for name, open_html in self._reopen:
                self.buf.append(open_html)
                self.stack.append((name, open_html))
            self._reopen = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if self._tbl_skip:
            # 嵌套表格丢弃区：内容整体跳过（深度守卫防递归炸）
            if tag == "table":
                self._tbl_skip += 1
            return
        if tag == "table":
            if self._tbl_depth:
                self._tbl_skip += 1
                return
            self._flush()
            self._tbl_depth = 1
            self._tbl_has_thead = False
            self._tbl_in_thead = False
            self._tbl_head = []
            self._tbl_body = []
            self._tbl_row = None
            self._cell_open = False
            # 表格级保留属性（2026-10）：class 白名单（TBL_TABLE_CLASSES，无边框表格
            # ptoe-noborder）+ style="table-layout:fixed"（用户拖拽列宽按比例精确生效
            # 所需，见 rulemanage.normalize_table_style）。其余 class/style 一律丢弃。
            # 与 apply_markers 共用 _table_open_attrs（单一事实来源，防口径漂移）。
            self._tbl_table_attrs = _table_open_attrs(dict(attrs))
            return
        if self._tbl_depth:
            # 表格结构标签：只作结构累积（thead/tbody/tfoot/tr/th/td）
            if tag in ("thead", "tfoot"):
                self._tbl_has_thead = self._tbl_has_thead or tag == "thead"
                self._tbl_in_thead = tag == "thead"
                self._tbl_finish_cell()
                return
            if tag == "tbody":
                self._tbl_in_thead = False
                self._tbl_finish_cell()
                return
            if tag == "tr":
                self._tbl_finish_row()
                self._tbl_row = []
                return
            if tag in ("th", "td"):
                self._tbl_finish_cell()
                d = dict(attrs)
                # 单元格属性：
                # - 对齐/垂直对齐 class（ptoe-align-* / ptoe-v*）各保留一个，其余 class 丢弃；
                # - colspan/rowspan 合法值保留（非法值不保留，与块级属性同策略）；
                # - 尺寸声明 style（width/height/vertical-align）经窄口径白名单过滤后保留
                #   （2026-10 表格增强：允许用户调整单元格/行列大小，见
                #   rulemanage.normalize_table_cell_style——绝不整体透传任意 style）。
                cell_attrs: list[str] = []
                cls_keep: list[str] = []
                for c in (d.get("class") or "").split():
                    if c in _ALIGN_CLASSES and not any(
                        x in _ALIGN_CLASSES for x in cls_keep
                    ):
                        cls_keep.append(c)
                    elif c in _VALIGN_CLASSES and not any(
                        x in _VALIGN_CLASSES for x in cls_keep
                    ):
                        cls_keep.append(c)
                if cls_keep:
                    cell_attrs.append(f'class="{" ".join(cls_keep)}"')
                for k in ("colspan", "rowspan"):
                    v = (d.get(k) or "").strip()
                    if re.fullmatch(r"\d{1,2}", v):
                        cell_attrs.append(f'{k}="{v}"')
                _cstyle = rulemanage.normalize_table_cell_style(d.get("style") or "")
                if _cstyle:
                    cell_attrs.append(f'style="{_html.escape(_cstyle, quote=True)}"')
                self._cell_open = True
                self._cell_th = tag == "th"
                self._cell_attrs = cell_attrs
                self._cell_buf = []
                self._cell_stack = []
                self._cell_block_depth = 0
                return
            if tag in ("p", "div") or _BLOCK_RE.fullmatch(tag):
                if self._cell_open:
                    # 单元格内块级标签 → 段落边界：转成 <p>…</p> 保留段落结构
                    # （2026-10 修复：此前直接丢弃标签，导致同一格内多段被并成一段，
                    #  也与「段落标记合并前后段」的分栏语义冲突）
                    # 2026-10-19 修复：段落级 class（引用/注释/对齐…）与「段落设置」
                    # data-* 必须随开标签保留。此前只输出裸 <p>，于是表格单元格里的
                    # 引用段保存/导出后降级为正文（界面显示引用、导出变正文），
                    # 段落缩进/行距等设置也一并丢失。白名单与表外块级共用同一套
                    # helper，防口径漂移（单元格内会被降级为 <p>，标题级语义不保留）。
                    if self._cell_block_depth == 0:
                        cls = _block_classes(attrs)
                        dattr = _block_data_attrs(attrs)
                        open_p = "<p"
                        if cls:
                            open_p += f' class="{" ".join(cls)}"'
                        for k, v in dattr:
                            open_p += f' {k}="{_html.escape(v, quote=True)}"'
                        self._cell_buf.append(open_p + ">")
                    self._cell_block_depth += 1
                    return
                # 表格结构层的块级标签：视为表格非正常结束，落入正常块级逻辑
                self._tbl_finish_table()
            elif self._cell_open:
                # 单元格内行内标签：走统一行内逻辑（路由到 cell buf）
                pass
            else:
                # 不在单元格内的行内/其它标签：表格结构层内容丢弃
                return
        if tag in ("b", "strong"):
            buf, stack = self._inline_target()
            buf.append("<strong>")
            stack.append(("strong", "<strong>"))
        elif tag in ("i", "em"):
            buf, stack = self._inline_target()
            buf.append("<em>")
            stack.append(("em", "<em>"))
        elif tag in _ALIAS_INLINE_TAGS:
            # 原生/外部标签（<u>/<s>/<strike>/<del>/<sup>/<sub>/<mark>）→ 等价格式类，
            # 避免「界面有格式、导出后丢失」（2026-09-16）。
            # 例外：<mark class="ptoe-search"> 是搜索高亮（临时视图态），仍按原样剥掉标签。
            # 压入占位栈项，保证对应的 </mark> 只弹栈、不误关其它行内标签。
            if "ptoe-search" in (dict(attrs).get("class") or "").split():
                _, stack = self._inline_target()
                stack.append(("__skip_mark__", ""))
                return
            open_html = f'<span class="{_ALIAS_INLINE_TAGS[tag]}">'
            buf, stack = self._inline_target()
            buf.append(open_html)
            stack.append(("span", open_html))
        elif tag == "font":
            # <font color="...">（浏览器 execCommand('foreColor') / 粘贴内容）→ 内联样式 span
            color = (dict(attrs).get("color") or "").strip()
            if _COLOR_VALUE_RE.match(color):
                open_html = f'<span style="color:{_html.escape(color, quote=True)}">'
                buf, stack = self._inline_target()
                buf.append(open_html)
                stack.append(("span", open_html))
        elif tag == "br":
            buf, _ = self._inline_target()
            buf.append("<br/>")
        elif tag in _MATHML_TAGS:
            # MathML 公式（2026-10-21）：<math …>…</math> 原样透传（属性走白名单）。
            # LaTeX 源码存放在 <math alttext="…"> 里——EPUB 导出用 MathML 渲染，
            # DOCX/MD/TXT 导出用 alttext 里的源码做纯文本兜底。
            open_html = _mathml_open_tag(tag, attrs)
            buf, stack = self._inline_target()
            buf.append(open_html)
            stack.append((tag, open_html))
        elif tag in ("p", "div"):
            self._open_block(
                "p", classes=_block_classes(attrs), data_attrs=_block_data_attrs(attrs)
            )
        elif _BLOCK_RE.fullmatch(tag):
            self._open_block(
                "h",
                int(tag[1]),
                classes=_block_classes(attrs),
                data_attrs=_block_data_attrs(attrs),
            )
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
                open_html = f'<span data-ptoe-marker="{v}"{cls_html}>'
                buf, stack = self._inline_target()
                buf.append(open_html)
                stack.append(("span", open_html))
            else:
                # 保留行内格式 span：经校验的字符样式（style 载体）+ 白名单格式类；
                # 外部/粘贴样式按等价语义折成格式类（2026-09-16 修复：原先只保留第一个
                # 匹配的格式类，且 style 里的下划线/上下标被整条丢弃 → 导出后格式消失）
                style = attrs_d.get("style") or ""
                classes = [
                    c
                    for c in (attrs_d.get("class") or "").split()
                    if c in _INLINE_FORMAT_CLASSES
                ]
                for pat, cls in _STYLE_TO_INLINE_CLASS:
                    if cls not in classes and pat.search(style):
                        classes.append(cls)
                keep_attrs = []
                # 行内样式白名单（EPUB 便携性取舍，2026-09-19 起）：
                # 只放行 4 条声明 text-align ; font-size ; font-family ; color——
                #   text-align  规则引擎用内联样式实现多对齐共存
                #   font-size    逐字符字号（整数 px，钳制 8..72）
                #   font-family  逐字符字体（10 项字体栈白名单之一）
                #   color        文本颜色（浏览器 execCommand('foreColor') /
                #                #colorBtn 产出的 style="color:…"）
                # 其余粘贴进来的行内样式（position/top/letter-spacing/…）一律丢弃（防注入）。
                # 校验与规范序列化统一走 rulemanage.normalize_char_style（单一事实来源，
                # 与 MiniDOMParser 规则引擎、ui/app.js 同一契约）；无存活声明则不输出 style。
                norm_style = rulemanage.normalize_char_style(style)
                if norm_style:
                    keep_attrs.append(
                        f'style="{_html.escape(norm_style, quote=True)}"'
                    )
                if classes:
                    keep_attrs.append(
                        f'class="{_html.escape(" ".join(classes), quote=True)}"'
                    )
                if "ptoe-ruby" in classes:
                    # 拼音注音（2026-10-21）：注音内容放 data-rt 属性；
                    # 规范化（去控制字符、限长 32）后放行，属性值不含换行/引号
                    rt = rulemanage.normalize_ruby_rt(attrs_d.get("data-rt") or "")
                    if rt:
                        keep_attrs.append(
                            f'data-rt="{_html.escape(rt, quote=True)}"'
                        )
                if keep_attrs:
                    open_html = f"<span {' '.join(keep_attrs)}>"
                    # 必须走 _inline_target()：单元格内须写入 _cell_buf/_cell_stack，
                    # 否则开标签落到文档缓冲 buf、闭标签被 cell 栈丢弃 → 单元格样式
                    # 丢失且 </table> 后多出一个孤立 <p><span …></p>（2026-09-27 修复）
                    cbuf, cstack = self._inline_target()
                    cbuf.append(open_html)
                    cstack.append(("span", open_html))
                # 其余 span 丢弃，仅保留文本内容
        elif tag == "img":
            # 插入的图片：仅保留 src（data URI 或相对路径）、alt 与显示模式 class
            # （含尺寸 class ptoe-img-w25/50/75/100）
            attrs_d = dict(attrs)
            src = attrs_d.get("src") or ""
            if src:
                cls = [
                    c for c in (attrs_d.get("class") or "").split() if c in _IMG_CLASSES
                ]
                alt = (attrs_d.get("alt") or "插图")[:100]
                cls_html = f' class="{_html.escape(" ".join(cls), quote=True)}"' if cls else ""
                # 自定义图片宽度（2026-10）：行内 style="width:NN%" 是自定义档位的唯一
                # 载体，不放行则保存/载历史会丢掉用户设的宽度（只保留白名单 1 条声明：
                # 校验与规范序列化统一走 rulemanage.normalize_img_width_style ——
                # 窄口径白名单，像素值/小数/其它声明一律丢弃，绝不透传任意 style 文本）。
                # 与尺寸 class 互斥（前端 applyImgLayoutMode/size 分支负责），
                # 同时存在时以行内样式为准。无合法宽度 → style_html 为空串，
                # 输出与 2026-10 之前逐字节相同。
                norm_w = rulemanage.normalize_img_width_style(attrs_d.get("style") or "")
                style_html = (
                    f' style="{_html.escape(norm_w, quote=True)}"' if norm_w else ""
                )
                # 同上：单元格内须写入单元格缓冲（否则图片被搬出表格）
                ibuf, _ = self._inline_target()
                ibuf.append(
                    f'<img src="{_html.escape(src, quote=True)}"'
                    f' alt="{_html.escape(alt, quote=True)}"{style_html}{cls_html}/>'
                )
        # 其余标签（a/...）丢弃，仅保留其文本内容

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
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
        if self._tbl_skip:
            if tag == "table":
                self._tbl_skip -= 1
            return
        if self._tbl_depth:
            if tag == "table":
                self._tbl_finish_table()
                return
            if tag in ("thead", "tfoot"):
                self._tbl_in_thead = False
                self._tbl_finish_cell()
                return
            if tag == "tbody":
                self._tbl_in_thead = False
                self._tbl_finish_cell()
                return
            if tag == "tr":
                self._tbl_finish_row()
                return
            if tag in ("th", "td"):
                self._tbl_finish_cell()
                return
            if tag in ("p", "div") or _BLOCK_RE.fullmatch(tag):
                if self._cell_open:
                    # 单元格内块级闭合 → 收一段（与 handle_starttag 的 <p> 配对）
                    if self._cell_block_depth > 0:
                        self._cell_block_depth -= 1
                        if self._cell_block_depth == 0:
                            self._cell_buf.append("</p>")
                    return
                # 表格结构层的块级闭合：视为表格非正常结束，落入正常闭合逻辑
                self._tbl_finish_table()
            elif not self._cell_open:
                # 表格结构层行内闭标签丢弃
                return
            # 单元格内行内闭标签：落入统一行内闭合逻辑（路由到 cell stack）
        buf, stack = self._inline_target()
        # 搜索高亮 <mark class="ptoe-search"> 只压了占位栈项：这里只弹栈
        if tag == "mark" and stack and stack[-1][0] == "__skip_mark__":
            stack.pop()
            return
        # MathML 公式（2026-10-21）：弹到匹配的 mathml 标签；中间未闭合的标签
        # 一并补闭（脏数据容错），保证输出的 XHTML 标签栈平衡。
        if tag in _MATHML_TAGS:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag:
                    while len(stack) > i:
                        name, _ = stack.pop()
                        buf.append(f"</{name}>")
                    return
            return
        # 原生/外部行内标签已归一为 span（见 _ALIAS_INLINE_TAGS / font 分支）
        if tag in _ALIAS_INLINE_TAGS:
            tag = "span"
        names = [n for n, _ in stack]
        if tag in ("strong", "b") and "strong" in names:
            buf.append("</strong>")
            stack.pop()
        elif tag in ("em", "i") and "em" in names:
            buf.append("</em>")
            stack.pop()
        elif tag in ("span", "font") and stack and stack[-1][0] == "span":
            buf.append("</span>")
            stack.pop()
        elif (
            tag in ("p", "div")
            and self.block is not None
            and self.block[0] == "p"
            or (
                _BLOCK_RE.fullmatch(tag)
                and self.block is not None
                and self.block[0] == "h"
                and self.block[1] == int(tag[1])
            )
        ):
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self._tbl_skip:
            return
        if self._tbl_depth:
            if self._cell_open:
                self._cell_buf.append(_html.escape(data, quote=False))
            # 表格结构层裸文本丢弃（单元格内文本已累计）
            return
        self.buf.append(_html.escape(data, quote=False))

    def result(self) -> str:
        self._tbl_finish_table()
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

    2026-09-06 修复：只给「真正带 data-ptoe-marker 属性的标记 span」补 class，
    绝不触碰其他普通/格式 span。此前用 ``re.sub(r"<span(.*?)>", ...)`` 匹配
    全部 span，会把 ptoe-marker 高亮类错误地加到注释内/其他位置的普通 span 上，
    「注释里有段落标记」时整段注释被渲染成高亮，只有标记该高亮。
    """
    if not html or "data-ptoe-marker" not in html:
        return html

    def _repl(m: re.Match) -> str:
        attrs = m.group(1) or ""
        # 仅处理带 data-ptoe-marker 的标记 span；其余 span（普通文字、格式 span、
        # 证明/纠错 span 等）一律原样返回，绝不加高亮类。
        if not re.search(r"data-ptoe-marker\s*=", attrs):
            return m.group(0)
        # find existing class attr
        cls_m = re.search(r'class="([^"]*)"', attrs)
        if cls_m:
            classes = cls_m.group(1).split()
            if "ptoe-marker" in classes:
                return m.group(0)
            # insert ptoe-marker into existing class list
            new_cls = cls_m.group(1) + " ptoe-marker"
            attrs2 = (
                attrs[: cls_m.start()] + f'class="{new_cls}"' + attrs[cls_m.end() :]
            )
            return f"<span{attrs2}>"
        else:
            # no class present: add class attribute
            return f'<span{attrs} class="ptoe-marker">'

    return re.sub(r"<span(.*?)>", _repl, html, flags=re.IGNORECASE | re.DOTALL)


# 纠错标注剥离（服务端兜底，供 sanitize_html 统一调用）。前端生成的标注形态固定：
#   <span class="ptoe-fix" data-err-i="N">候选</span>
#   <s class="ptoe-err" data-err-i="N" [data-err-empty="1"]>文本</s>
_PTOE_FIX_RE = re.compile(
    r'<span\b[^>]*\bclass\s*=\s*"[^"]*\bptoe-fix\b[^"]*"[^>]*>.*?</span>',
    re.IGNORECASE | re.DOTALL,
)
_PTOE_ERR_EMPTY_RE = re.compile(
    r"<s\b[^>]*\bdata-err-empty\b[^>]*>.*?</s>",
    re.IGNORECASE | re.DOTALL,
)
_PTOE_ERR_UNWRAP_RE = re.compile(
    r'<s\b[^>]*\bclass\s*=\s*"[^"]*\bptoe-err\b[^"]*"[^>]*>(.*?)</s>',
    re.IGNORECASE | re.DOTALL,
)


def _strip_proofread_markup(html: str) -> str:
    """兜底剥离残留的纠错标注（落库前防线，由 sanitize_html 统一调用）。

    界面把纠错标注叠加在编辑区 live DOM 上，个别路径（syncContent 同步
    ed.innerHTML）会把标注带进待存 HTML；若不剥离，正文会被污染：
      - ``.ptoe-fix``（候选建议展示）文本是模型建议，非原文 → 整体删除；
      - ``<s class="ptoe-err" data-err-empty>``（空白页填充建议）文本是"拟插入
        的候选字"→ 整体删除；
      - 其余 ``<s class="ptoe-err">``（错误原文标注）→ 解包保留原文。
    前端 collect() 已做同样处理，这里再兜一层（2026-10-17 修复：data-err-empty
    的候选字曾被 _Sanitizer 折成 <span class="ptoe-strike"> 保留下来污染正文）。
    """
    if not html or ("ptoe-err" not in html and "ptoe-fix" not in html):
        return html
    s = _PTOE_FIX_RE.sub("", html)
    s = _PTOE_ERR_EMPTY_RE.sub("", s)
    s = _PTOE_ERR_UNWRAP_RE.sub(r"\1", s)
    return s
    if not html or ("ptoe-err" not in html and "ptoe-fix" not in html):
        return html
    s = _PTOE_FIX_RE.sub("", html)
    s = _PTOE_ERR_EMPTY_RE.sub("", s)
    s = _PTOE_ERR_UNWRAP_RE.sub(r"\1", s)
    return s


def sanitize_html(raw: str) -> str:
    """清洗界面提交的 HTML：只放行白名单标签（含标记 span）并保证结构平衡。"""
    raw = _strip_proofread_markup(raw)
    s = _Sanitizer()
    try:
        s.feed(raw)
    except Exception:
        # 解析异常时退化为纯文本（不丢内容、不产生非法标签）
        return _html.escape(raw, quote=False)
    return s.result()


def convert_text_html(html_text: str, mode: str) -> str:
    """只对 HTML 文本节点进行繁/简转换，保留标签与属性不变。

    参数:
    - html_text: 包含标签的 HTML 片段
    - mode: 't2s'（繁体→简体）或 's2t'（简体→繁体）

    行为:
    - 仅转换标签外的文本节点；保留 HTML 实体（如 &amp;）原样不动。
    - 未安装 zhconv 时抛出 ImportError（调用方应在测试/运行环境安装依赖）。
    """
    if mode not in ("t2s", "s2t"):
        raise ValueError("mode must be 't2s' or 's2t'")

    try:
        from zhconv import zhconv
    except Exception as e:  # pragma: no cover - environment may lack zhconv
        raise ImportError(
            "zhconv is required for convert_text_html; install with `pip install zhconv`"
        ) from e

    import re

    def _convert_piece(text: str) -> str:
        if not text:
            return text
        # 保留 HTML 实体不参与转换：把实体当作边界分割，分别转换非实体片段
        if "&" in text:
            parts = re.split(r"(&[^;]+;)", text)
            out_parts = []
            for p in parts:
                if p.startswith("&") and p.endswith(";"):
                    out_parts.append(p)
                else:
                    out_parts.append(
                        zhconv.convert(p, "zh-cn")
                        if mode == "t2s"
                        else zhconv.convert(p, "zh-tw")
                    )
            return "".join(out_parts)
        return (
            zhconv.convert(text, "zh-cn")
            if mode == "t2s"
            else zhconv.convert(text, "zh-tw")
        )

    # 粗分标签与文本段，标签段原样保留
    parts = re.split(r"(<[^>]+>)", html_text)
    converted = [
        (_convert_piece(p) if not (p.startswith("<") and p.endswith(">")) else p)
        for p in parts
    ]
    return "".join(converted)


# 句末标点：以这些结尾的段落视为「完整段落」，不与下一段合并
# （；、：、，等为句中停顿，OCR 拆段时应当合并，故不在此列；
#   闭合括号/书名号（），】」』》〉）结尾同样视为完整段落）
_SENT_END_PUNCT = "。！？!?…）)】」』》〉"
# 段首需清理的装饰符号（OCR 常在段首添加）
_LEADING_SYMBOL_RE = re.compile(r"^[#*＊+•·▪]+\s*")
# OCR/文本残留的 Markdown 加粗符号 **（可能多处，含句中）
_MD_BOLD_RE = re.compile(r"\*{2,}")
# Markdown 内联标记残留（智能清理 clean_md_tags 开关）：**粗体** / ~~删除~~ / `代码`
_MD_TAG_RE = re.compile(r"\*{2,}|~{2,}|`+")

# 统计用
_TAG_COUNT_RE = re.compile(r"<[a-zA-Z/!]")


def _char_delta(a: str, b: str) -> int:
    n = min(len(a), len(b))
    d = sum(1 for i in range(n) if a[i] != b[i]) + abs(len(a) - len(b))
    return d


def _count_tags(s: str) -> int:
    return len(_TAG_COUNT_RE.findall(s))
# 清理：[1]/【2】/［3］ 等方括号包裹的数字统一修正为全角六角括号 〔n〕（2026-08）
# x = 任意数字（半角/全角均可，内容原样保留）；已是 〔n〕 的不动
_BRACKET_DIGIT_RE = re.compile(r"[\[［【]\s*([0-9０-９]+)\s*[\]］】]")
# 中英文标点归一（2026-10 页级中文语境）：实现委托 stringmanage._normalize_punctuation_base——
# 中文页（含汉字或全无拉丁字母）内半角标点整体转全角（含成对括号与保护项），纯英文页全角转半角
_CJK_RANGE = "\u4e00-\u9fff\u3400-\u4dbf"
_LATIN_RANGE = "A-Za-z0-9"
_HALF_TO_FULL = (
    (",", "，"),
    (";", "；"),
    (":", "："),
    ("?", "？"),
    ("!", "！"),
    ("(", "（"),
    (")", "）"),
)
_FULL_TO_HALF = (
    ("，", ","),
    ("；", ";"),
    ("：", ":"),
    ("？", "?"),
    ("！", "!"),
    ("（", "("),
    ("）", ")"),
)
# 清理时剥掉的非白名单标签（保留 p/h1-6/strong/em/b/i/br/span/img，
# b/i 留给 sanitize 归一化为 strong/em；其余剥掉但保留内容）
# 2026-09-27：表格结构标签一并豁免（table/thead/tfoot/tbody/tr/th/td，
# 即 _Sanitizer 表格累积模式消费的整套标签）。表格是 UI 主动插入的结构化
# 内容，不是 OCR 残留标签；剥掉它会把单元格文本摊平成裸文本，再被后续 <p>
# 认领 → 一次点击「清理」即毁表：
#   <p>before</p><table>…<td>c1</td><td>c2</td>…</table><p>AFTER</p>
#   → <p>before</p><p>c1c2AFTER</p>（表格消失，AFTER 段被吸收）
#
# 2026-10-21：MathML 公式标签（<math>/<semantics>/<mrow>/<mi>…）一并豁免——
# 与表格同型的静默丢失：剥掉标签会把公式拆成一堆散字（"E=mc2"），
# 一次「智能清理」即毁公式。标签清单取自 rulemanage.MATHML_TAGS（单一事实来源）。
_STRIP_TAG_RE = re.compile(
    r"</?(?!p\b|h[1-6]\b|strong\b|em\b|b\b|i\b|br\b|span\b|img\b"
    r"|table\b|thead\b|tfoot\b|tbody\b|tr\b|th\b|td\b|"
    + "|".join(t + r"\b" for t in sorted(rulemanage.MATHML_TAGS))
    + r")[a-zA-Z][^>]*>"
)

# 表格整块识别（_clean_blocks 用，2026-09-27）：<table …> / </table>。
# 与 htmlmanage._TABLE_TAG_RE 同样式（含属性、re.I）。
_TABLE_OPEN_RE = re.compile(r"<table\b[^>]*>", flags=re.IGNORECASE)
_TABLE_CLOSE_RE = re.compile(r"</table\s*>", flags=re.IGNORECASE)
# blocks 里代表「整表原子块」的开标签哨兵（真实段落开标签不可能等于它）
_TABLE_BLOCK = "<table>"

# ---------------------------------------------------------------------------
AUTO_FIX_SCORE = 0.85  # candidate score threshold to mark auto_fixable
# ---------------------------------------------------------------------------

# 半角标点 → 全角（文字纠错用）；引号需按出现次序轮换左右，单独处理
_PROOFREAD_HALF_FULL: dict[str, str] = {
    ",": "，",
    ".": "。",
    "?": "？",
    "!": "！",
    ":": "：",
    ";": "；",
    "(": "（",
    ")": "）",
    "[": "【",
    "]": "】",
}

# 连续标点检测（规则7）：中文全角与英文半角标点均计入。
# 分隔/终止类（连用即视为异常，如「，，」「。。」「!!」「?!」）
_CONSEC_PUNCT_SEP: frozenset = frozenset("，。！？；：、,.!?;:")
# 引号/括号类（与分隔类相邻属正常排版，如「。”」「：“」，故仅在同字符重复时才算异常）
_CONSEC_PUNCT_OTHER: frozenset = frozenset(
    "\u201c\u201d\u2018\u2019（）《》〈〉【】「」『』\"'()[]"
)
_CONSEC_PUNCT_ALL: frozenset = _CONSEC_PUNCT_SEP | _CONSEC_PUNCT_OTHER
_CONSEC_PUNCT_RE = re.compile(
    "[" + re.escape("".join(sorted(_CONSEC_PUNCT_ALL))) + "]{2,}"
)

# 常见混淆字（OCR 易混）：键为常见错字，值为候选正确字
_OCR_CONFUSABLES: dict[str, list[str]] = {
    "日": ["曰"],
    "未": ["末"],
    "己": ["已", "巳"],
    "土": ["士"],
    "人": ["入"],
    "干": ["千", "于"],
    "王": ["玉"],
    "鸟": ["乌"],
    "天": ["夫"],
    "大": ["太"],
    "问": ["间"],
    "千": ["干", "于"],
    "处": ["外"],
    "设": ["没"],
    "主": ["住"],
}

# 常见叠词排除表：连续重复但属正常词汇，不报错误
_DIE_WORDS: frozenset = frozenset(
    {
        "弟弟",
        "妹妹",
        "人人",
        "年年",
        "天天",
        "日日",
        "时时",
        "处处",
        "个个",
        "家家",
        "常常",
        "久久",
        "刚刚",
        "大大",
        "小小",
        "宝宝",
        "妈妈",
        "爸爸",
        "爷爷",
        "奶奶",
        "哥哥",
        "姐姐",
        "慢慢",
        "明明",
        "好好",
        "高高",
        "长长",
        "深深",
        "浅浅",
        "纷纷",
        "种种",
        "件件",
        "层层",
        "滴滴",
    }
)

# 常见正常词排除表：以混淆字为中心的 2 字窗命中此表则跳过（宁多勿漏）
_PROOFREAD_SAFE_WORDS: frozenset = frozenset(
    {
        # 日
        "日本",
        "日记",
        "日子",
        "每日",
        "今日",
        "昨日",
        "明日",
        "生日",
        "节日",
        "日期",
        "日常",
        "日光",
        "日落",
        "日出",
        "日报",
        "周日",
        "平日",
        "假日",
        "佳日",
        "吉日",
        "忌日",
        "来日",
        "去日",
        "连日",
        "整日",
        "全日",
        "半日",
        "一日",
        "三日",
        "七日",
        "十日",
        "百日",
        "前日",
        "后日",
        "当日",
        "次日",
        "他日",
        "某日",
        "往日",
        "昔日",
        # 未
        "未来",
        "未必",
        "未曾",
        "尚未",
        "未知",
        "未能",
        "未遂",
        "未尝",
        # 己
        "自己",
        "知己",
        "克己",
        "律己",
        "利己",
        "损己",
        "安己",
        # 已
        "已经",
        "已然",
        "已往",
        "而已",
        "早已",
        "久已",
        # 土
        "土地",
        "泥土",
        "尘土",
        "土壤",
        "国土",
        "领土",
        "出土",
        "入土",
        # 士
        "士兵",
        "士气",
        "学士",
        "硕士",
        "博士",
        "女士",
        "人士",
        "护士",
        # 人
        "人民",
        "工人",
        "农民",
        "人口",
        "主人",
        "别人",
        "个人",
        "大人",
        "人类",
        "人生",
        "人才",
        "人物",
        "人间",
        "人气",
        "人道",
        "人事",
        "人造",
        "人文",
        "人选",
        "人均",
        "人影",
        "人潮",
        # 入
        "进入",
        "加入",
        "输入",
        "收入",
        "深入",
        "陷入",
        "出入",
        "介入",
        "渗入",
        "混入",
        "切入",
        "闯入",
        "纳入",
        "注入",
        "流入",
        "汇入",
        # 干
        "干净",
        "干杯",
        "干扰",
        "干涉",
        "干线",
        "干活",
        "干旱",
        "干枯",
        # 千
        "千万",
        "千年",
        "千古",
        "千金",
        "千里",
        "千秋",
        "成千",
        "上千",
        # 王
        "国王",
        "王国",
        "帝王",
        "大王",
        "女王",
        "君王",
        "王公",
        "王侯",
        # 玉
        "玉石",
        "玉器",
        "玉帛",
        "玉成",
        "玉人",
        "玉体",
        "玉颜",
        "玉手",
        # 鸟
        "鸟儿",
        "鸟类",
        "候鸟",
        "飞鸟",
        "花鸟",
        "鸟巢",
        "鸟雀",
        # 乌
        "乌云",
        "乌鸦",
        "乌黑",
        "乌有",
        "乌龟",
        "乌贼",
        "乌木",
        # 天
        "天下",
        "今天",
        "明天",
        "天空",
        "白天",
        "天气",
        "春天",
        "夏天",
        "秋天",
        "冬天",
        "天上",
        "天然",
        "天地",
        "天堂",
        "天文",
        "天天",
        "天色",
        "天涯",
        "天际",
        "天边",
        "天窗",
        "天井",
        "天日",
        # 夫
        "丈夫",
        "夫人",
        "夫妇",
        "夫妻",
        "夫子",
        "渔夫",
        "农夫",
        "车夫",
        "马夫",
        "船夫",
        "轿夫",
        "匹夫",
        "武夫",
        "懦夫",
        "姐夫",
        "妹夫",
        # 大
        "大家",
        "大学",
        "大约",
        "大夫",
        "大地",
        "大米",
        "大门",
        "大小",
        "大妈",
        "大众",
        "庞大",
        "巨大",
        "宏大",
        "宽大",
        "壮大",
        "博大",
        # 太
        "太阳",
        "太空",
        "太平",
        "太多",
        "太少",
        "太大",
        "太小",
        "太太",
        # 问
        "问题",
        "问好",
        "问候",
        "提问",
        "访问",
        "询问",
        "追问",
        "盘问",
        "责问",
        "拷问",
        "学问",
        "疑问",
        "审问",
        "慰问",
        "问罪",
        # 间
        "时间",
        "空间",
        "房间",
        "期间",
        "瞬间",
        "民间",
        "车间",
        "厨房",
        "卫生间",
        "间隔",
        "间断",
        "间接",
        "间距",
        "其间",
        "中间",
        # 处
        "处理",
        "到处",
        "处罚",
        "处分",
        "处境",
        "处决",
        "处置",
        "相处",
        "查处",
        "难处",
        "好处",
        "坏处",
        "用处",
        "益处",
        "出处",
        # 外
        "外面",
        "外国",
        "之外",
        "以外",
        "分外",
        "野外",
        "郊外",
        "海外",
        "意外",
        "额外",
        "外汇",
        "外人",
        "外乡",
        "外行",
        "外向",
        "外观",
        # 设
        "设计",
        "设立",
        "设备",
        "建设",
        "假设",
        "设法",
        "铺设",
        "陈设",
        # 没
        "没有",
        "没事",
        "没法",
        "没落",
        "没收",
        "淹没",
        "沉没",
        "出没",
        "没趣",
        "没命",
        "没辙",
        "没世",
        "没齿",
        # 主
        "主要",
        "主张",
        "主意",
        "主角",
        "主权",
        "主持",
        "主导",
        "主观",
        "主动",
        "主干",
        "主力",
        "主流",
        "主旨",
        "主修",
        "主攻",
        # 住
        "住房",
        "居住",
        "住所",
        "住宅",
        "住址",
        "抓住",
        "记住",
        "站住",
        "停住",
        "拦挡住",
        "握住",
        "抱住",
        "捂住",
        "扣住",
        "拴住",
    }
)


def _normalize_punctuation(text: str, *, ctx_cjk: bool | None = None) -> str:
    """中英文标点归一化（2026-10 页级中文语境；实现统一在 stringmanage）。

    ctx_cjk 由 _clean_blocks 按整页判定后传入：中文页内半角 ,;:?!(). 与括号
    整体转全角（括号成对转换，修「测试（)」只转一半、中文页里的半角括号与
    隔空格标点不转的问题；保护小数/千分位/时间/URL/查询串/缩写点/省略号）；
    纯英文页维持原行为（全角 → 半角反向）。
    ctx_cjk=None（独立调用/测试）时按 text 自身判定：含汉字，或全篇没有
    拉丁字母（如只输了一个 ()）→ 中文语境。
    """
    from stringmanage import _normalize_punctuation_base as _base

    return _base(text, ctx_cjk=ctx_cjk)


# 括号归一（2026-08）：ULQ/PD 系模型输出的引注标记常带杂符包裹（如 （^{[1]】}、
# [\（^{〔1]〕}\）），实际内容只是 [1]；且全角/半角/方头括号混用。统一清理为〔n〕。
# 策略：数字两侧只要出现垃圾符号（\ ^ ~ ` | · { }）即视为引注包裹，允许任意
# 括号汤（各类中英括号任意混排）垫在周围——比逐个枚举包裹形状稳健得多。
_ULQ_JUNK_BRACKET_RE = re.compile(
    r"[\[\]{}()（）【】［］〔〕〈〉《》「」『』\\^~`|·\s]{0,8}"
    r"[\\^~`|·{}]+"
    r"[\[\]{}()（）【】［］〔〕〈〉《》「」『』\\^~`|·\s]{0,8}"
    r"(\d{1,3})"
    r"[\[\]{}()（）【】［］〔〕〈〉《》「」『』\\^~`|·\s]{0,8}"
    r"[\\^~`|·{}]+"
    r"[\[\]{}()（）【】［］〔〕〈〉《》「」『』\\^~`|·\s]{0,8}"
)
# 括号对归一仅处理「方/方头括号 + 数字引注」这类的 OCR 误识别形状（【x】/[x]/［x］
# 及杂符包裹），统一为 〔x〕。**绝不能把全角圆括号（x）也归一为〔x〕** —— （x）是
# 正常的正文标点（2026-09-02 用户报「原文（x）被改成〔x〕」），其可能是字母/文字/数字，
# 必须原样保留。杂符包裹由 _ULQ_JUNK_BRACKET_RE 单独处理（要求数字两侧有 \ ^ { } 等
# 真正垃圾字符才折叠），不靠这里的（...）模式。
_BRACKET_PAIR_RES = (
    re.compile(r"【([^【】]*)】"),
    re.compile(r"\[([^\[\]\n]{1,32})\]"),
    re.compile(r"［([^［］]*)］"),
)
# $ 包裹剥除（2026-09-21）：OvisOCR2（oo）重识别把原文 〔x〕 输出成 $〔x〕$
# ——$ 是模型自造标记符，原图没有，必须在归一/对比前剥掉。策略：$ 紧贴括号
# （可容忍中间空白）时剥除，含 【】/[]/［］/〔〕/（）/()。$ 也可能合法出现于
# 价格/数学语境，但中文书籍 OCR 语境下 $ 紧贴括号几乎必为模型杂符，剥除安全；
# 不紧贴括号的 $（如 $5、$x$）原样保留。
_DOLLAR_OPEN_BRACKET_RE = re.compile(r"\$+\s*([〔【［\[(（])")
_DOLLAR_CLOSE_BRACKET_RE = re.compile(r"([〕】］\])）])\s*\$+")


def _strip_dollar_bracket_hug(text: str) -> str:
    """剥掉紧贴括号的 $ 包裹（如 $〔x〕$ → 〔x〕）。"""
    text = _DOLLAR_OPEN_BRACKET_RE.sub(r"\1", text)
    text = _DOLLAR_CLOSE_BRACKET_RE.sub(r"\1", text)
    return text


def _clean_ulq_bracket_junk(text: str) -> str:
    """把 ULQ 输出的杂符包裹引注（如 （^{[1]】}、^{[2]}）折叠为〔n〕。"""
    return _ULQ_JUNK_BRACKET_RE.sub(r"〔\1〕", text)


# LaTeX 圈码（2026-08-02）：部分视觉模型会把原文的 ① 等圈码识别成 LaTeX 形式
# $\textcircled{1}$ / \textcircled{1} / $\textcircled{12}$。若先做括号归一，
# {1} 的 { } 会被 _ULQ_JUNK_BRACKET_RE 折叠为 〔1〕，留下残缺的 $\textcircled〔1〕$。
# 故须在括号归一之前把 \textcircled{n} 还原为 ①②…（1-20），超出范围回退 〔n〕，
# 再交给 _normalize_brackets（此时无 { } 残留，不再误伤）。
_TEXT_CIRCLED_RE = re.compile(
    r"\$?\s*\\textcircled\s*\{\s*(\d{1,3})\s*\}\s*\$?"
)

# ① ① ~ ⑳ （U+2460 .. U+2473），索引 n-1
_CIRCLED_MAP = [
    "\u2460", "\u2461", "\u2462", "\u2463", "\u2464",
    "\u2465", "\u2466", "\u2467", "\u2468", "\u2469",
    "\u246a", "\u246b", "\u246c", "\u246d", "\u246e",
    "\u246f", "\u2470", "\u2471", "\u2472", "\u2473",
]


def _circled_digit(n: int) -> str:
    """把圈码编号还原为 ①…⑳；超出 20 回退 〔n〕。"""
    if 1 <= n <= 20:
        return _CIRCLED_MAP[n - 1]
    return f"〔{n}〕"


def _normalize_textcircled(text: str) -> str:
    """把 LaTeX 形式的 \textcircled{n}（可带 $ 包裹）还原为 ① 等圈码。

    仅处理 1-3 位数字（圈码常见范围），其他形状（如 \textcircled{abc}）原样保留。
    """
    if not text or "textcircled" not in text:
        return text
    return _TEXT_CIRCLED_RE.sub(
        lambda m: _circled_digit(int(m.group(1))), text
    )


def _normalize_bracket_pairs(text: str) -> str:
    """把成对的 【x】/[x]/［x］ 统一替换为 〔x〕（允许符号混用）。"""
    for pat in _BRACKET_PAIR_RES:
        text = pat.sub(r"〔\1〕", text)
    return text


def _normalize_brackets(text: str) -> str:
    """先剥 $ 紧贴括号包裹，再清 ULQ 杂符包裹，最后统一括号对。"""
    text = _strip_dollar_bracket_hug(text)
    return _normalize_bracket_pairs(_clean_ulq_bracket_junk(text))


def _clean_bracket_junk_html(html: str) -> str:
    """对 HTML 片段做杂符括号清理（token 级，只动文本节点）。

    部分大模型会把原文的 〔x〕 引注识别成 \\〔^{x〕}\\ 这类杂符包裹格式
    （\\ ^ { } 等无效字符夹着括号）；OvisOCR/o 系模型还会自造 $ 包裹引注
    括号（$〔1〕$，注释见 _strip_dollar_bracket_hug）。这些字符进入矫正界面/
    对比前必须清除。守卫字符集含 $：一旦命中即进入 _normalize_brackets
    （其内部先 _strip_dollar_bracket_hug 剥 $ 紧贴括号，再做杂符清理 +
    括号对统一，见 _ULQ_JUNK_BRACKET_RE）。
    逐 token 处理：`<...>` 与标记 span 原样保留（绝不能拆标签，否则会破坏
    img 属性/标记结构），仅对文本 token 做 _normalize_brackets。
    """
    if not html or re.search(r"[\\^~`|·{}$]", html) is None:
        return html
    if _TOKEN_RE.search(html) is None:
        return _normalize_brackets(html)
    out = []
    for tok in _TOKEN_RE.split(html):
        if not tok:
            continue
        if tok.startswith("<"):
            out.append(tok)
        else:
            out.append(_normalize_brackets(tok))
    return "".join(out)


def _full_punct(text: str) -> str:
    """将文本中的英文标点替换为中文标点（含引号配对轮换）。无 CJK 上下文时原样返回。

    复用 _PROOFREAD_HALF_FULL 映射表与 proofread_page 规则1 的引号轮换逻辑
    （偶数次出现为左引号、奇数次为右引号）。供 /api/reocr 归一化大模型返回
    文本，避免半角/全角标点差异被当作纠错项（2026-08-09）。
    """
    if not text or not re.search(f"[{_CJK_RANGE}]", text):
        return text
    out = []
    dquote_n = 0  # 双引号出现次序（偶=左“，奇=右”）
    squote_n = 0  # 单引号出现次序（偶=左‘，奇=右’）
    for ch in text:
        if ch == '"':
            out.append("\u201c" if dquote_n % 2 == 0 else "\u201d")
            dquote_n += 1
        elif ch == "'":
            out.append("\u2018" if squote_n % 2 == 0 else "\u2019")
            squote_n += 1
        else:
            out.append(_PROOFREAD_HALF_FULL.get(ch, ch))
    return "".join(out)


def _strip_leading_symbols(text: str) -> str:
    """段首符号清理：去掉 OCR 常在段首添加的 #、*、• 等装饰符号。"""
    return _LEADING_SYMBOL_RE.sub("", text)


# ---------------------------------------------------------------------------
# 校正格式清理（2026-10）：重识别返回文本进入对比前剥离 MD / LaTeX 格式字符
#
# 模型（尤其视觉 OCR）常在返回文本里混入 Markdown 标记与 LaTeX 公式/命令，
# 这些与正文内容无关的「格式字符」会被 diff 逐字判为纠错项（用户报
# 「重识别结果里全是 **、$、\frac 之类的标注」）。本模块提供统一清理：
#   默认（strip_markdown / strip_latex）只去标记、保留文字与公式内容；
#   remove_formula 进一步整段移除公式及其内容。
# 由设置界面「校正格式」栏控制（config.json: correction_format，默认关闭）。
# 性能：仅在 enabled=True 时于 /api/reocr 每页调用一次；正则均模块级预编译，
#       函数入口按开关短路，关闭时零开销。
# ---------------------------------------------------------------------------

# --- Markdown 标记 ---
_CF_MD_FENCE_RE = re.compile(r"^[ \t]*`{3,}.*$", re.M)               # ``` 围栏行
_CF_MD_HEAD_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M)        # # 标题
_CF_MD_QUOTE_RE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.M)            # > 引用
_CF_MD_UL_RE = re.compile(r"^[ \t]{0,3}[-*+][ \t]+", re.M)           # - 无序列表
_CF_MD_OL_RE = re.compile(r"^[ \t]{0,3}\d{1,3}[.)][ \t]+", re.M)     # 1. 有序列表
_CF_MD_HR_RE = re.compile(r"^[ \t]{0,3}(?:[-*_][ \t]*){3,}$", re.M)  # --- 分隔线
_CF_MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")               # 图片
_CF_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")               # 链接
_CF_MD_STRIKE_RE = re.compile(r"~~(?=\S)([^~\n]+?)(?<=\S)~~")        # 删除线
_CF_MD_AST_EMPH_RE = re.compile(r"\*{1,3}(?=\S)([^*\n]+?)(?<=\S)\*{1,3}")  # **粗**/*斜*
_CF_MD_UND_EMPH_RE = re.compile(
    r"(?<![\w])_{1,3}(?=\S)([^_\n]+?)(?<=\S)_{1,3}(?![\w])"
)  # __粗__/_斜_（避开 snake_case）
_CF_MD_CODE_RE = re.compile(r"`+([^`]*)`+")                          # `代码`
_CF_MD_MARK_RE = re.compile(r"\*{2,}|~{2,}|`+")                       # 残留标记

# --- LaTeX 公式 / 命令 ---
_CF_LTX_ENV_RE = re.compile(r"\\begin\s*\{[^}]*\}(.*?)\\end\s*\{[^}]*\}", re.S)
_CF_LTX_DISPLAY_RE = re.compile(r"\$\$(.+?)\$\$", re.S)              # $$…$$
_CF_LTX_INLINE_RE = re.compile(r"(?<!\$)\$([^$\n]{1,400})\$(?!\$)")  # $…$（不跨行）
_CF_LTX_PAREN_RE = re.compile(r"\\\((.+?)\\\)", re.S)                # \(…\)
_CF_LTX_BRACKET_RE = re.compile(r"\\\[(.+?)\\\]", re.S)              # \[…\]
_CF_LTX_TEXT_CMD_RE = re.compile(
    r"\\(?:text|mathrm|mathbf|mathit|mathsf|mathtt|mathcal|operatorname|mbox|hbox|"
    r"textnormal|textrm|textbf|textit|textup|textmd|textsf|texttt)\s*\{([^{}]*)\}"
)
_CF_LTX_FRAC_RE = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_CF_LTX_SQRT_RE = re.compile(r"\\sqrt\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}")
_CF_LTX_BRACE_CMD_RE = re.compile(r"\\[a-zA-Z]+\s*(?:\{[^{}]*\})+")   # \cmd{…} 块
_CF_LTX_NOSPACE_RE = re.compile(
    r"\\(?:quad|qquad|thinspace|enspace|medspace|thickspace|negthinspace|"
    r"left|right|big|Big|bigg|Bigg|bigl|bigr|Bigl|Bigr|biggl|biggr|Biggl|Biggr)\b"
)
_CF_LTX_MICROSPACE_RE = re.compile(r"\\[ ,;:!]")                     # \, \; \: 等
_CF_LTX_CMD_RE = re.compile(r"\\[a-zA-Z]+")                          # 剩余未知命令
_CF_LTX_MATH_JUNK_RE = re.compile(r"[{}$^_&]|\\\\")                  # 结构残符
_CF_LTX_SYMBOLS = {
    "times": "×", "div": "÷", "pm": "±", "mp": "∓", "cdot": "·",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "equiv": "≡", "propto": "∝", "sim": "∼", "simeq": "≃",
    "ll": "≪", "gg": "≫",
    "rightarrow": "→", "to": "→", "leftarrow": "←", "leftrightarrow": "↔",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔",
    "infty": "∞", "sum": "Σ", "prod": "∏", "int": "∫", "partial": "∂",
    "nabla": "∇", "angle": "∠", "circ": "∘", "degree": "°",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "iota": "ι",
    "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ",
    "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Delta": "Δ", "Gamma": "Γ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ",
    "Omega": "Ω",
    "cdots": "…", "ldots": "…", "dots": "…", "vdots": "⋮", "ddots": "⋱",
}
# 符号命令正则：命令名按长度降序，避免 \le 抢在 \leq 之前匹配
_CF_LTX_SYMBOL_RE = re.compile(
    r"\\(?:"
    + "|".join(
        sorted((re.escape(k) for k in _CF_LTX_SYMBOLS), key=len, reverse=True)
    )
    + r")(?![a-zA-Z])"
)


def _cf_latex_symbol_sub(m: "re.Match[str]") -> str:
    """符号命令 → 对应 Unicode（未收录则原样返回）。"""
    return _CF_LTX_SYMBOLS.get(m.group(0)[1:], m.group(0))


def _cf_clean_latex_piece(s: str, *, aggressive: bool) -> str:
    """清理一段 LaTeX：去命令/结构符，保留文字内容。

    aggressive=True 时额外剥除未知命令与 {} ^ _ $ & \\ 等结构字符（用于
    $…$ / \\(…\\) / 环境内部）；False 仅做已知命令→符号/文字替换（用于无分隔符
    的裸 LaTeX，避免误伤 C:\\Users 这类路径）。
    """
    if not s:
        return s
    s = _normalize_textcircled(s)
    s = _CF_LTX_TEXT_CMD_RE.sub(r"\1", s)
    for _ in range(4):  # 处理浅层嵌套分数 \frac{\frac{a}{b}}{c}
        s2 = _CF_LTX_FRAC_RE.sub(r"\1/\2", s)
        if s2 == s:
            break
        s = s2
    s = _CF_LTX_SQRT_RE.sub(r"√\1", s)
    s = _CF_LTX_NOSPACE_RE.sub("", s)
    s = _CF_LTX_MICROSPACE_RE.sub("", s)
    s = _CF_LTX_SYMBOL_RE.sub(_cf_latex_symbol_sub, s)
    if aggressive:
        s = _CF_LTX_CMD_RE.sub("", s)
        s = _CF_LTX_MATH_JUNK_RE.sub("", s)
    return s


def _cf_strip_math_keep(text: str) -> str:
    """去 LaTeX 公式/环境标记，保留公式内容（strip_latex、remove_formula=False）。"""
    text = _CF_LTX_ENV_RE.sub(
        lambda m: _cf_clean_latex_piece(m.group(1), aggressive=True), text
    )
    text = _CF_LTX_DISPLAY_RE.sub(
        lambda m: _cf_clean_latex_piece(m.group(1), aggressive=True), text
    )
    text = _CF_LTX_PAREN_RE.sub(
        lambda m: _cf_clean_latex_piece(m.group(1), aggressive=True), text
    )
    text = _CF_LTX_BRACKET_RE.sub(
        lambda m: _cf_clean_latex_piece(m.group(1), aggressive=True), text
    )
    text = _CF_LTX_INLINE_RE.sub(
        lambda m: _cf_clean_latex_piece(m.group(1), aggressive=True), text
    )
    # 无分隔符的裸 LaTeX 命令（模型偶发直接输出 \frac{a}{b} / \textcircled{1}）
    if "\\" in text:
        text = _cf_clean_latex_piece(text, aggressive=False)
    return text


def _cf_remove_math(text: str) -> str:
    """整段移除 LaTeX 公式/环境及其内容（remove_formula=True）。"""
    text = _CF_LTX_ENV_RE.sub("", text)
    text = _CF_LTX_DISPLAY_RE.sub("", text)  # $$…$$ 须先于 $…$
    text = _CF_LTX_PAREN_RE.sub("", text)
    text = _CF_LTX_BRACKET_RE.sub("", text)
    text = _CF_LTX_INLINE_RE.sub("", text)
    # 无分隔符的裸命令块 \command{…}
    text = _CF_LTX_BRACE_CMD_RE.sub("", text)
    return text


def _cf_strip_markdown(text: str) -> str:
    """剥离 Markdown 标记（保留文字内容）。"""
    text = _CF_MD_FENCE_RE.sub("", text)
    text = _CF_MD_HEAD_RE.sub("", text)
    text = _CF_MD_QUOTE_RE.sub("", text)
    text = _CF_MD_UL_RE.sub("", text)
    text = _CF_MD_OL_RE.sub("", text)
    text = _CF_MD_HR_RE.sub("", text)
    text = _CF_MD_IMG_RE.sub(r"\1", text)
    text = _CF_MD_LINK_RE.sub(r"\1", text)
    text = _CF_MD_STRIKE_RE.sub(r"\1", text)
    text = _CF_MD_AST_EMPH_RE.sub(r"\1", text)
    text = _CF_MD_UND_EMPH_RE.sub(r"\1", text)
    text = _CF_MD_CODE_RE.sub(r"\1", text)
    text = _CF_MD_MARK_RE.sub("", text)
    return text


def _cf_apply_custom_rules(text: str, rules) -> str:
    """按顺序应用自定义清理规则（正则替换）。

    flags（2026-10-30 增强）：i/m/s/x 全部映射到 Python re 对应标志（原来只认
    i/m，写 s 会被静默忽略）；g/u 接受但无效果。正则先过方言归一
    （rulemanage.normalize_pattern：\\z、\\h、\\R、JS (?<name>…) 等），与替换规则、
    格式规则引擎同一口径。
    """
    import rulemanage

    for rule in rules or ():
        if not isinstance(rule, dict):
            continue
        if rule.get("enabled", True) is False:
            continue
        pat = rule.get("pattern")
        if not isinstance(pat, str) or not pat:
            continue
        repl = rule.get("replacement")
        if not isinstance(repl, str):
            repl = ""
        try:
            text = re.sub(
                rulemanage.normalize_pattern(pat),
                repl,
                text,
                flags=rulemanage.compile_flags(str(rule.get("flags") or "")),
            )
        except re.error:
            continue  # 非法规则已在设置端校验，这里宽容跳过不阻断主链路
    return text


def clean_correction_format(
    text: str,
    *,
    strip_markdown: bool = True,
    strip_latex: bool = True,
    remove_formula: bool = False,
    custom_rules=None,
) -> str:
    """剥离模型返回文本中的无关格式字符（Markdown / LaTeX），供与原文对比前使用。

    由设置界面「校正格式」栏控制（config.json: correction_format，默认关闭）；
    仅在 /api/reocr 归一化链路、enabled=True 时调用（见该处理器注释）。

    参数：
      strip_markdown  去 Markdown 标记（保留文字）        —— **粗体** → 粗体
      strip_latex     去 LaTeX 命令/环境标记（保留公式内容）—— $\\frac{a}{b}$ → a/b
      remove_formula  整段移除 LaTeX 公式及其内容         —— $x^2$ →（删除）
      custom_rules    自定义规则列表（结构同 replace_rules，仅用 i/m/u）

    实现：模块级预编译正则 + 入口短路，性能开销可忽略。
    """
    if not text:
        return text
    if remove_formula or strip_latex:
        if remove_formula:
            text = _cf_remove_math(text)
        else:
            text = _cf_strip_math_keep(text)
    if strip_markdown:
        text = _cf_strip_markdown(text)
    if custom_rules:
        text = _cf_apply_custom_rules(text, custom_rules)
    return text


def _proofread_plain_text(html: str) -> str:
    """剥标签取纯文本（参考 clean_page_html 的文本提取逻辑）。

    先整 span 剥离标记（含 label 内容一并删），这样 /api/proofread 与 /api/reocr
    的 current_text 均不含标记 label——标记不再被规则纠错、不被 diff 当增字。
    """
    h = _MARKER_SPAN_RE.sub("", str(html or ""))
    s = re.sub(r"<[^>]+>", "", h)
    return _html.unescape(s)


def proofread_page(
    text: str, enable_legacy_rules: bool = False
) -> list[dict[str, Any]]:
    """文字纠错：对纯文本逐条检测常见 OCR 错误。

    返回错误列表，每项 {start, end, wrong, candidates}（字符偏移，按 start 排序、
    互相不重叠：后条 start ≥ 前条 end）。

    **默认（enable_legacy_rules=False）只执行三条新规则**（2026-08-09 用户决定）：
      2. 连续重复：2+ 连串的 1-4 字组（排除纯数字、命中 _DIE_WORDS 的叠词）
      6. 中文中突然出现的字母/英语（含全角字母）：至少一侧是中文上下文才标；纯英文段落/数字不标；
         P2 排除表命中跳过（start 重叠保留既有规则条目）
      7. 连续标点（中英文均计）：2+ 连续标点串中出现「两个相邻分隔类标点」或「相邻同字符重复」
         → 整串标一条（candidates 为空，纯标注）。正常排版如 「。”」「：“」「……」不标；
         数字千分位/小数场景（两侧均为数字的 ASCII .,）跳过。

    enable_legacy_rules=True 时额外执行原有规则（矫正界面设置里的「启用原有规则」开关，
    持久化于 config.json proofread.enable_legacy_rules）：
      1. 半角标点（文本含 CJK 时）：, . ? ! : ; ( ) [ ] → 全角；双引号/单引号按出现次序轮换左右
         （命中规则7 串内的位置不再逐字出条目，避免整串标注被重叠剔除吞掉）
      3. 引号配对：「」与“”分别统计，奇数个 → 最后一个该引号位置标一条
      4. 混淆表：逐字命中键 → 以该字为中心的 2 字窗任一在 _PROOFREAD_SAFE_WORDS 则跳过
      5. 词典词级检测（可选，依赖 dictionarymanage）：连续 CJK 段 2-4 字滑窗未知词 → generate_candidates
         出候选（start 重叠时保留既有规则条目、丢弃词典条目）
    """
    s = str(text or "")
    legacy = bool(enable_legacy_rules)
    raw_errors: list[dict[str, Any]] = []

    # 规则7 连续标点：先算出命中区间，供规则1 跳过（整串一条标注优先于逐字半角转全角）
    consec_spans: list[tuple] = []
    for m in _CONSEC_PUNCT_RE.finditer(s):
        run = m.group(0)
        start, end = m.start(), m.end()
        # 异常判定：相邻两个分隔/终止类标点，或相邻同字符重复
        bad = False
        for k in range(len(run) - 1):
            a, b = run[k], run[k + 1]
            if a == b or (a in _CONSEC_PUNCT_SEP and b in _CONSEC_PUNCT_SEP):
                bad = True
                break
        if not bad:
            continue  # 「。”」「：“」等正常排版组合不标
        # 数字场景跳过（两侧均为数字的 ASCII 逗号/句点串，如千分位/小数误连）
        left = s[start - 1] if start > 0 else ""
        right = s[end] if end < len(s) else ""
        if left.isdigit() and right.isdigit() and all(c in ".," for c in run):
            continue
        consec_spans.append((start, end))
        raw_errors.append({"start": start, "end": end, "wrong": run, "candidates": []})

    def _in_consec(idx: int) -> bool:
        for a, b in consec_spans:
            if a <= idx < b:
                return True
        return False

    # 规则1 半角标点（原有规则，默认关闭）：文本含 CJK 时才检测
    has_cjk = re.search(f"[{_CJK_RANGE}]", s) is not None
    if legacy and has_cjk:
        dquote_n = 0  # 双引号出现次序（偶=左“，奇=右”）
        squote_n = 0  # 单引号出现次序（偶=左‘，奇=右’）
        for i, ch in enumerate(s):
            # 已被规则7 整串标注的位置不再逐字出条目（否则重叠剔除会让整串标注被吞掉）
            if ch == '"':
                full = "\u201c" if dquote_n % 2 == 0 else "\u201d"  # “ ”
                dquote_n += 1  # 次序仍需推进，保证串外引号左右轮换正确
                if not _in_consec(i):
                    raw_errors.append(
                        {"start": i, "end": i + 1, "wrong": ch, "candidates": [full]}
                    )
            elif ch == "'":
                full = "\u2018" if squote_n % 2 == 0 else "\u2019"  # ‘ ’
                squote_n += 1
                if not _in_consec(i):
                    raw_errors.append(
                        {"start": i, "end": i + 1, "wrong": ch, "candidates": [full]}
                    )
            elif ch in _PROOFREAD_HALF_FULL:
                if not _in_consec(i):
                    raw_errors.append(
                        {
                            "start": i,
                            "end": i + 1,
                            "wrong": ch,
                            "candidates": [_PROOFREAD_HALF_FULL[ch]],
                        }
                    )

    # 规则2 连续重复：([\u4e00-\u9fffA-Za-z]{1,4})\1{1,}（2+ 连串）
    for m in re.finditer(r"([\u4e00-\u9fffA-Za-z]{1,4})\1{1,}", s):
        wrong = m.group(0)
        unit = m.group(1)
        if re.fullmatch(r"[0-9]+", unit):  # 纯数字不报
            continue
        if wrong in _DIE_WORDS:  # 常见叠词排除
            continue
        raw_errors.append(
            {"start": m.start(), "end": m.end(), "wrong": wrong, "candidates": [unit]}
        )

    # 规则3 引号配对（原有规则，默认关闭）：「」与“”分别统计，奇数个 → 最后一个该引号位置标一条
    _quote_pairs = (
        (("\u300c", "\u300d"), ("\u201c", "\u201d")) if legacy else ()
    )  # 「」 / “”
    for pair in _quote_pairs:
        open_q, close_q = pair
        positions = [i for i, ch in enumerate(s) if ch == open_q or ch == close_q]
        if len(positions) % 2 == 1:
            last = positions[-1]
            raw_errors.append(
                {"start": last, "end": last + 1, "wrong": s[last], "candidates": []}
            )

    # 规则4 混淆字（原有规则，默认关闭）：逐字命中 → 仅在上下文未形成已知词时才考虑；
    # 以词典候选为准，形近表作为回退。
    for i, ch in enumerate(s) if legacy else ():
        if ch not in _OCR_CONFUSABLES:
            continue
        prev_ch = s[i - 1] if i > 0 else ""
        next_ch = s[i + 1] if i + 1 < len(s) else ""
        win1 = prev_ch + ch
        win2 = ch + next_ch
        # 若二字窗口本身为已知词，则不报
        try:
            if dictionarymanage is not None:
                dictionarymanage.load_dicts()
                if dictionarymanage.is_word(win1) or dictionarymanage.is_word(win2):
                    continue
        except Exception:
            # 回退到旧的安全词表判断
            if win1 in _PROOFREAD_SAFE_WORDS or win2 in _PROOFREAD_SAFE_WORDS:
                continue

        # 获取候选（优先词典缓存，失败时用形近表作为回退）
        cands = []
        try:
            if dictionarymanage is not None:
                cands = dictionarymanage.cached_candidates_for_token(
                    ch, prev_ch, next_ch
                )
        except Exception:
            cands = [(c, 0.0) for c in _OCR_CONFUSABLES.get(ch, [])]

        accepted = []
        accepted_scores = []
        for cand in cands:
            if isinstance(cand, tuple) and len(cand) == 2:
                cand_word, cand_score = cand
            else:
                cand_word, cand_score = cand, 0.0
            # enforce minimum score unless the candidate is already a known word
            # threshold read from dictionarymanage when available, else conservative default
            if dictionarymanage is not None and hasattr(dictionarymanage, "_SCORE_MIN"):
                threshold = getattr(dictionarymanage, "_SCORE_MIN", 0.4)
            else:
                threshold = 0.4
            score_ok = cand_score >= threshold

            if dictionarymanage is not None and dictionarymanage.is_word(
                cand_word + next_ch
            ):
                if score_ok or dictionarymanage.is_word(cand_word):
                    accepted.append(cand_word)
                    accepted_scores.append(cand_score)
                    break
            if dictionarymanage is not None and dictionarymanage.is_word(cand_word):
                accepted.append(cand_word)
                accepted_scores.append(cand_score)
        if accepted:
            err = {"start": i, "end": i + 1, "wrong": ch, "candidates": accepted}
            if accepted_scores:
                err["candidate_scores"] = accepted_scores
                try:
                    err["auto_fixable"] = bool(max(accepted_scores) >= AUTO_FIX_SCORE)
                except Exception:
                    err["auto_fixable"] = False
            else:
                err["auto_fixable"] = False
            raw_errors.append(err)
    # 规则5 词典词级检测（原有规则，默认关闭）：对连续 CJK 段做 2-4 字滑窗未知词检测
    # （依赖 dictionarymanage，未就绪则跳过）
    if legacy and dictionarymanage is not None:
        try:
            dictionarymanage.load_dicts()
            for m in re.finditer(r"[\u4e00-\u9fff\u3400-\u4dbf]+", s):
                seg = m.group(0)
                seg_start = m.start()
                i = 0
                while i < len(seg):
                    hit = False
                    # 从长到短滑窗（4→2），本位置取最长命中
                    for win in (4, 3, 2):
                        if i + win > len(seg):
                            continue
                        sub = seg[i : i + win]
                        if dictionarymanage.is_word(sub):
                            continue
                        ctx_before = seg[i - 1] if i > 0 else ""
                        ctx_after = seg[i + win] if i + win < len(seg) else ""

                        # Use cached candidate generator to avoid repeated work
                        try:
                            cands = dictionarymanage.cached_candidates_for_token(
                                sub, ctx_before, ctx_after
                            )
                        except Exception:
                            try:
                                cands = dictionarymanage.generate_candidates(
                                    sub, ctx_before, ctx_after
                                )
                            except Exception:
                                cands = []

                        # Conservative acceptance: only suggest if candidate is a known word and
                        # replacing increases known-word coverage in the local window (reduces false positives).
                        if cands:

                            def _count_known(segx: str) -> int:
                                cnt = 0
                                j = 0
                                while j < len(segx):
                                    matched = False
                                    for L in (4, 3, 2, 1):
                                        if j + L > len(segx):
                                            continue
                                        piece = segx[j : j + L]
                                        if dictionarymanage.is_word(piece):
                                            cnt += 1
                                            j += L
                                            matched = True
                                            break
                                    if not matched:
                                        j += 1
                                return cnt

                            before_seg = sub
                            before_cnt = _count_known(before_seg)
                            accepted_cands: list = []
                            accepted_scores: list = []
                            for cand in cands:
                                if isinstance(cand, tuple) and len(cand) == 2:
                                    cand_word, cand_score = cand
                                else:
                                    cand_word, cand_score = cand, 0.0
                                after_seg = cand_word
                                after_cnt = _count_known(after_seg)
                                if after_cnt > before_cnt or dictionarymanage.is_word(
                                    cand_word
                                ):
                                    accepted_cands.append(cand_word)
                                    accepted_scores.append(cand_score)
                            if accepted_cands:
                                err = {
                                    "start": seg_start + i,
                                    "end": seg_start + i + win,
                                    "wrong": sub,
                                    "candidates": accepted_cands,
                                }
                                if accepted_scores:
                                    err["candidate_scores"] = accepted_scores
                                    try:
                                        err["auto_fixable"] = bool(
                                            max(accepted_scores) >= AUTO_FIX_SCORE
                                        )
                                    except Exception:
                                        err["auto_fixable"] = False
                                else:
                                    err["auto_fixable"] = False
                                raw_errors.append(err)
                                i += win
                                hit = True
                                break  # 本位置取最长命中
                    if not hit:
                        i += 1
        except Exception:
            pass  # 词典检测失败不影响既有四规则

    # 规则6 中文中突然出现的字母/英语标注（含全角字母）；纯英文段落/数字不标
    _CN_PUNCT = "，。！？；：“”‘’（）《》、…—·"
    for m in re.finditer(r"[A-Za-z\uFF21-\uFF3A\uFF41-\uFF5A]+", s):
        frag = m.group(0)
        start, end = m.start(), m.end()
        # 上下文判定：至少一侧是中文（CJK 或中文标点）
        left = s[start - 1] if start > 0 else ""
        right = s[end] if end < len(s) else ""
        left_zh = bool(left) and ("\u4e00" <= left <= "\u9fff" or left in _CN_PUNCT)
        right_zh = bool(right) and ("\u4e00" <= right <= "\u9fff" or right in _CN_PUNCT)
        if not (left_zh or right_zh):
            continue  # 纯英文段落/行首独立英文不标
        # 排除表过滤（P2 用户点 ✗ 忽略后不再标）
        if dictionarymanage is not None and dictionarymanage.is_ignored(frag):
            continue
        raw_errors.append({"start": start, "end": end, "wrong": frag, "candidates": []})

    # 按 start 排序，剔除重叠（保留先出现的）
    raw_errors.sort(key=lambda e: (e["start"], e["end"]))
    errors: list[dict[str, Any]] = []
    for e in raw_errors:
        if errors and e["start"] < errors[-1]["end"]:
            continue
        errors.append(e)
    return errors


def _block_text(toks: list[str]) -> str:
    """块内纯文本（跳过标签 token）。"""
    return "".join(t for t in toks if not t.startswith("<"))


def _block_is_plain(toks: list[str]) -> bool:
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
    clean_symbols: bool = False,
    clean_md_tags: bool = True,
    symbol_pairs=None,
    digit_punct_pairs=None,
    clean_spaces: bool = False,
    keep_word_gap: bool = True,
    stats=None,
) -> str:
    """按块级标签切分已清洗的 HTML，逐块做文本处理并可选合并相邻段落。

    合并启发式（保守，避免把真正的段落粘在一起）：仅合并两个无 class 的
    普通 <p> 块，且前块不以句末标点（。！？…；）结尾、前后块均非空。
    <h1-6> 与带 class 的 <p>（注释/对齐/图片）视为有结构意图，不合并。
    合并成功后自动清理合并段的段内空白（2026-10-22）：软换行、行首行尾
    空格、中文之间的空白一并删除，英文/数字单词间空白折叠为一个空格，
    因此无需额外开启 clean_spaces 也能得到干净的自然段。

    表格是**原子块**（2026-09-27）：<table>…</table> 整表逐字节透传，既不切块、
    不套文本处理（段首符号/中英标点/**/[n]），也不与任何相邻段落合并。
    修复前表格 token 落进 cur，被后续 <p> 认领 → 单元格文本被摊平、表格整体
    消失。清理器的规则都是**段落级**的；单元格是数据表列，套用半角→全角会
    改写数值（18,5 / 1.5 / 3-4）与参考文献序号 [1]，属静默数据损坏。

    收尾用 tbl_depth 计数而非「见到第一个 </table>」：<td> 内嵌套表时，内层
    </table> 只是嵌套结束，若据此收尾，外层剩余的 </td></tr></table> 会落进
    cur 并被兜底包成 <p>…</p>（非法嵌套，XHTML 解析直接报错）。
    孤立 </table>（无开标签的脏数据）必须就地丢弃，不能落进 cur —— 同样会
    产出非法的 <p></table></p>。
    生产路径上 clean_page_html 必先过 sanitize_html，而 _Sanitizer 表格累积
    模式总会闭合表格、丢弃嵌套表、剥除单元格内块级标签，故上述两条分支属
    纵深防御：直接调用 _clean_blocks（测试/未来调用方）时才可能命中。
    """
    tokens = re.split(r"(<[^>]+>)", html)
    blocks: list[tuple[str, list[str]]] = []  # (开标签或 "<p>" 兜底, 块内 tokens)
    cur_open: str | None = None
    cur: list[str] = []
    tbl_buf: list[str] | None = None  # 非 None = 正在累积整张表格
    tbl_depth = 0  # <table> 嵌套深度：归零才收尾（内层 </table> 不得提前结束整表）
    for tok in tokens:
        if not tok:
            continue
        if tbl_buf is not None:
            # 表内一切 token 原样累加（含 strong/span/空白文本 token）
            if _TABLE_OPEN_RE.fullmatch(tok):
                tbl_depth += 1  # <td> 内嵌套表：直到**外层** </table> 才收尾
            elif _TABLE_CLOSE_RE.fullmatch(tok):
                tbl_depth -= 1
            tbl_buf.append(tok)
            if tbl_depth == 0:
                blocks.append((_TABLE_BLOCK, tbl_buf))
                tbl_buf = None
            continue
        if _TABLE_OPEN_RE.fullmatch(tok) or _TABLE_CLOSE_RE.fullmatch(tok):
            if tok.startswith("</"):
                # 孤立 </table>（无对应开标签）：就地丢弃。若放行会落进 cur，
                # 再被兜底包成 <p></table></p> —— 非法嵌套。
                continue
            # 表格开标签前先切出已累积的段落，避免表格被并进上一个块
            if cur or cur_open is not None:
                blocks.append((cur_open or "<p>", cur))
            cur_open = None
            cur = []
            tbl_buf = [tok]
            tbl_depth = 1
            continue
        if re.fullmatch(r"</?(p|h[1-6])([^>]*)>", tok, flags=re.IGNORECASE):
            if cur or cur_open is not None:
                blocks.append((cur_open or "<p>", cur))
            cur_open = None
            cur = []
            if not tok.startswith("</"):
                cur_open = tok
            continue
        cur.append(tok)
    if tbl_buf is not None:
        # 未闭合表格（外部脏数据）：整段原样收尾，不套 <p> 包裹
        blocks.append((_TABLE_BLOCK, tbl_buf))
    elif cur or cur_open is not None:
        blocks.append((cur_open or "<p>", cur))

    # 块内文本处理（段首符号只作用于块开头第一个文本 token）
    # 页级中文语境判定（2026-10）：整页含汉字，或剥离标签/实体后全无拉丁
    # 字母（如只在空页输了一个 () 的测试）→ 中文语境，本页半角标点整体转
    # 全角（含成对括号、隔空格/行尾标点）；纯英文页保持半角。表格虽逐字节
    # 透传不处理，但其文字同样构成本页语境。
    _ctx_plain = re.sub(r"<[^>]+>", "", str(html))
    _ctx_plain = re.sub(r"&#?\w+;", " ", _ctx_plain)  # &nbsp; 等实体不算拉丁字母
    page_ctx_cjk = bool(re.search(f"[{_CJK_RANGE}]", _ctx_plain)) or not re.search(
        r"[A-Za-z]", _ctx_plain
    )
    _normalize_symbols_extended = None
    if clean_symbols:
        # 惰性导入：stringmanage 只依赖 stdlib，但沿用本仓「重模块内延迟导入」约定
        from stringmanage import _normalize_symbols_extended as _nse

        _normalize_symbols_extended = _nse
    _normalize_digit_punct = None
    _clean_alnum_spaces = None
    if digit_punct_pairs:
        # 数字/字母里的中文标点 → 英文（2026-10「数字标点归一"}）：放在标点/符号归一之后，
        # 用户自定义映射表拥有最终解释权（如 1。2 → 1.2、1，2 → 1,2、1。 → 1.）。
        # 2026-10-18 扩展：字母间标点（H，W → H,W）+ 行内空格容忍（1 。2 → 1.2）；
        # 随后清理数字间/单字母间空格（1 2 → 12、M a r x → Marx，英文短语保留）。
        from stringmanage import normalize_digit_punct as _ndp
        from stringmanage import clean_alnum_spaces as _cas

        _normalize_digit_punct = _ndp
        _clean_alnum_spaces = _cas
    _strip_inline_spaces = None
    if clean_spaces or merge_paragraphs:
        # 惰性导入：与本仓「重模块内延迟导入」约定一致。
        # 合并段落也依赖它：合并后的段落自动清理段内空白（2026-10-22）。
        from stringmanage import strip_inline_spaces as _sis

        _strip_inline_spaces = _sis

    def _clean_merged_ws(block_toks: list[str]) -> None:
        """合并后的普通段落自动去空白（仅文本，不含标签）。

        与「清理段落内空白」同一套规则：删除水平空白/不可见字符并合并
        OCR 软换行；英文/数字单词之间的空白折叠为一个空格。合并块本身
        不含行内标签（_block_is_plain），可直接整体处理成一个文本 token。
        """
        if _strip_inline_spaces is None:
            return
        raw = "".join(block_toks)
        cleaned = _strip_inline_spaces(
            raw, keep_word_gap=keep_word_gap, keep_newlines=False
        )
        if cleaned == raw:
            return
        if stats is not None:
            stats["spaces"] = stats.get("spaces", 0) + max(0, len(raw) - len(cleaned))
        block_toks[:] = [cleaned]
    for open_tag, toks in blocks:
        if open_tag == _TABLE_BLOCK:
            continue  # 表格：逐字节透传，不做任何文本处理
        first = True
        for i, t in enumerate(toks):
            if not t or t.startswith("<"):
                continue
            t2 = t
            if first and strip_leading_symbols:
                t2 = _strip_leading_symbols(t2)
                first = False
            # OCR/文本残留的 Markdown 标记（**粗体** / ~~删除~~ / `代码`）：
            # 必须在符号归一前剥离，否则 ~~ 会被 ~ 的半角→全角规则拆开
            if clean_md_tags:
                if stats is not None:
                    spans = re.findall(r"\*{2,}.*?\*{2,}|\~{2,}.*?\~{2,}|`+.*?`+", t2, flags=re.S)
                    if spans:
                        stats["md"] += len(spans)
                t2_new = _MD_TAG_RE.sub("", t2)
                t2 = t2_new
            if normalize_punctuation:
                nt = _normalize_punctuation(t2, ctx_cjk=page_ctx_cjk)
                if stats is not None:
                    stats["punct"] += _char_delta(t2, nt)
                t2 = nt
            # 扩展符号归一（引号/方括号/省略号/破折号/波浪号）
            if _normalize_symbols_extended is not None:
                nt = _normalize_symbols_extended(t2, half_to_full=symbol_pairs)
                if stats is not None:
                    stats["symbols"] += _char_delta(t2, nt)
                t2 = nt
            # [1]/【2】等方括号数字统一修正为 〔n〕（全 token 处理）
            before_b = t2
            t2 = _BRACKET_DIGIT_RE.sub("〔\\1〕", t2)
            if stats is not None and t2 != before_b:
                stats["symbols"] += _char_delta(before_b, t2)
            # 数字/字母里的中文标点 → 英文（2026-10「数字标点归一"}）：放在标点/符号归一之后，
            # 用户自定义映射表拥有最终解释权（如 1。2 → 1.2、1，2 → 1,2、1。 → 1.）。
            # 2026-10-18：字母间标点（H，W → H,W）+ 行内空格容忍（1 。2 → 1.2）；
            # 随后清理数字间/单字母间空格（1 2 → 12、M a r x → Marx，英文短语保留）。
            if _normalize_digit_punct is not None:
                nd = _normalize_digit_punct(t2, digit_punct_pairs)
                if _clean_alnum_spaces is not None:
                    nd = _clean_alnum_spaces(nd)
                if stats is not None:
                    stats["punct"] += _char_delta(t2, nd)
                t2 = nd
            # 去除段落内空白（2026-10-21）：普通段落把 OCR 软换行也合并掉
            # （换行在浏览器里会渲染成词间空隙，是「清完仍有空白」的根因）；
            # ptoe-align-lines 是逐行排版，必须保留换行。keep_word_gap=True 时
            # 英文单词之间的空白折叠为一个空格。
            if _strip_inline_spaces is not None and clean_spaces:
                preserve_lines = _ALIGN_LINES_CLASS in open_tag
                ns = _strip_inline_spaces(t2, keep_word_gap=keep_word_gap,
                                          keep_newlines=preserve_lines)
                if stats is not None and ns != t2:
                    stats["spaces"] += len(t2) - len(ns)
                t2 = ns
            toks[i] = t2

    # 相邻普通 <p> 段落合并（OCR 拆段修复）
    # 仅合并两个「普通段落」：无 class 的 <p> 且块内无任何行内标签
    # （已设标题/格式/标记/图片的段落保留原结构，不与附近段落合并）
    if merge_paragraphs:
        merged: list[tuple[str, list[str]]] = []
        for open_tag, toks in blocks:
            if open_tag == _TABLE_BLOCK:
                # 表格既不做合并源（下面两个分支要求两侧都是 "<p>"）也不做合并
                # 目标（此处 open_tag != "<p>"）：作为合并屏障原样落列，表格两侧
                # 的段落也不会跨过它粘在一起。
                merged.append((open_tag, toks))
                continue
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
                _clean_merged_ws(merged[-1][1])
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
                if prev_txt and next_txt and prev_txt[-1] not in _SENT_END_PUNCT:
                    # 屏障：列表/缩进/冒号引入等
                    _MERGE_BARRIER_RE = re.compile(r"^\s*(?:[•·▪◦*\-–—]\s|\d{1,3}\s*[.、)）]|[（(]\s*\d{1,3}\s*[）)]|　)")
                    _MERGE_TAIL_INTRO_RE = re.compile(r"[:：]\s*$")
                    if _MERGE_BARRIER_RE.match(next_txt) or _MERGE_TAIL_INTRO_RE.search(prev_txt):
                        merged.append((open_tag, toks))
                        continue
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
                    # 合并后自动清理段内空白：即使「清理段落内空白」未开启，
                    # 也不会留下软换行/双空格/中文间空格（2026-10-22）。
                    _clean_merged_ws(merged[-1][1])
                    if stats is not None:
                        stats["merged"] = stats.get("merged", 0) + 1
                    continue
            merged.append((open_tag, toks))
        blocks = merged

    # 重建（丢弃空块）
    parts: list[str] = []
    for open_tag, toks in blocks:
        if open_tag == _TABLE_BLOCK:
            # 表格：既不 strip 也不套 _close_of 闭合标签 —— 逐字节原样输出
            raw = "".join(toks)
            if raw:
                parts.append(raw)
            continue
        # 多行对齐段（2026-10-20）：段尾的 &nbsp; 填充是版式的一部分，默认口径的
        # .strip() 会连 &nbsp; 一起剥掉——导出后左右两侧就对不齐了
        inner = strip_block_ws("".join(toks), keep_nbsp=_ALIGN_LINES_CLASS in open_tag)
        if not inner:
            continue
        parts.append(f"{open_tag}{inner}{_close_of(open_tag)}")
    return "\n".join(parts)


def _clean_pages(items: list, clean_kw: dict) -> list:
    """批量清理多页 HTML，返回每页的清理结果与统计。

    单页同步执行；多页用 ThreadPoolExecutor 并行（顺序与输入一致）。
    每项返回 {"page": int, "html": str, "stats": dict}。
    page 转换失败的项跳过。
    """
    valid = []
    for item in items:
        try:
            n = int(item.get("page"))
        except (TypeError, ValueError):
            continue
        valid.append((n, str(item.get("html") or "")))

    if not valid:
        return []

    # clean_blank_lines 是导出专用开关（clean_page_html 无此参数），调用方若整体
    # 传入 get_clean_settings() 需在此剔除，避免 **clean_kw 触发 TypeError。
    clean_kw = {k: v for k, v in clean_kw.items() if k != "clean_blank_lines"}

    def one(pn_html):
        n, html = pn_html
        out, st = clean_page_html(html, with_stats=True, **clean_kw)
        return {"page": n, "html": out, "stats": st}

    if len(valid) == 1:
        return [one(valid[0])]

    import os
    from concurrent.futures import ThreadPoolExecutor

    max_workers = min(4, os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(one, valid))


def clean_history_pages(
    history_id: str | None = None,
    pdf_path: str | None = None,
    pages=None,
    *,
    clean_kw: dict | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """对既有矫正历史（data/correction_history）按页范围做智能清理（headless）。

    供 CLI ``mian.py clean`` / 简称 ``cl`` 使用：不起 HTTP 服务器、不开界面，
    直接读历史版本 JSON、逐页跑 ``clean_page_html``，改动写回同一版本文件。

    - ``history_id``：历史版本 id（文件名 stem；``--list`` 可查）；
    - ``pdf_path``：改用该 PDF 的**最新**版本（无 history_id 时）；
    - ``pages``：1-based 页码集合（list/set），None 或空 = 全部页；
    - ``clean_kw``：清理规则（缺省读 config 的 ui_settings：开关 + 符号表 + 数字标点表）；
    - ``dry_run``：只统计不落盘。

    返回 ``{"ok", "history_id", "pdf", "total", "cleaned", "changed", "pages", "chars",
    "dry_run"}``；``pages`` 为实际改动页码（升序）。目标不存在/读取失败抛 ``ValueError``
    （调用方转成提示与退出码）。
    """
    vid = str(history_id or "").strip()
    if vid.lower().endswith(".json"):  # 允许直接粘贴历史文件名
        vid = vid[:-5]
    if not vid:
        target = str(pdf_path or "").strip()
        if not target:
            raise ValueError(
                "请指定历史版本（--history-id）或 PDF（--pdf）；用 --list 可查看可用版本"
            )
        pref = _history_prefix(os.path.abspath(target))
        entries = _history_entries(pref) if pref else []
        if not entries:
            raise ValueError(f"没有找到该 PDF 的矫正历史：{target}")
        vid = str(entries[0].get("id") or "")
        if not vid:
            raise ValueError(f"矫正历史条目缺少 id：{target}")
    fp = _history_dir() / f"{vid}.json"
    if not fp.is_file():
        raise ValueError(f"矫正历史不存在：{vid}")
    try:
        raw = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"矫正历史读取失败：{vid}（{e}）") from e
    if not isinstance(raw, dict):
        raise ValueError(f"矫正历史格式异常：{vid}")
    stored = raw.get("pages")
    if not isinstance(stored, dict):
        stored = {}
    if clean_kw is None:
        from configmanage import (
            get_clean_digit_punct_map,
            get_clean_settings,
            get_clean_symbol_map,
        )

        clean_kw = dict(get_clean_settings())
        clean_kw["symbol_map"] = get_clean_symbol_map()
        clean_kw["digit_punct_map"] = get_clean_digit_punct_map()
    # clean_blank_lines 是导出专用开关（clean_page_html 无此参数），此处剔除
    clean_kw = {k: v for k, v in clean_kw.items() if k != "clean_blank_lines"}
    sel = {int(p) for p in pages} if pages else None
    cleaned = 0
    touched: list[int] = []
    delta = 0
    for key in sorted(stored, key=lambda k: int(k)):
        num = int(key)
        if sel is not None and num not in sel:
            continue
        src = str(stored.get(key) or "")
        out = clean_page_html(src, **clean_kw)
        cleaned += 1
        if out != src:
            delta += len(out) - len(src)
            touched.append(num)
            if not dry_run:
                stored[key] = out
    if touched and not dry_run:
        raw["pages"] = stored
        raw["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        from configmanage import _atomic_write_json

        _atomic_write_json(str(fp), raw)
    return {
        "ok": True,
        "history_id": vid,
        "pdf": raw.get("pdf"),
        "total": len(stored),
        "cleaned": cleaned,
        "changed": len(touched),
        "pages": touched,
        "chars": delta,
        "dry_run": bool(dry_run),
    }


def clean_page_html(
    raw: str,
    *,
    merge_paragraphs: bool = False,
    strip_leading_symbols: bool = True,
    normalize_punctuation: bool = True,
    strip_tags: bool = True,
    clean_punctuation: bool = True,
    clean_symbols: bool = True,
    clean_md_tags: bool = True,
    clean_html_tags: bool = True,
    clean_paragraphs: bool = False,
    symbol_map=None,
    clean_digit_punct: bool = True,
    digit_punct_map=None,
    clean_spaces: bool = False,
    clean_spaces_keep_word_gap: bool = True,
    with_stats: bool = False,
):
    """矫正界面的智能清理入口：清理段首 #/* 等符号、归一化中英文标点、
    移除残留的 HTML 标签，返回 sanitize 后的规范 HTML。

    merge_paragraphs=False（默认不合并）：段落合并已移出默认清理流程，
    仅在显式传入 merge_paragraphs=True 时生效（供格式规则/段落合并 op 使用）。

    2026-10-16 智能清理规则开关（与 config.json 的 ui_settings 同名键对应）：
    - clean_punctuation：中英文标点归一化（页级语境：中文页内半角标点整体
      转全角、括号成对转换；纯英文页保留半角）默认开；
      与旧参数 normalize_punctuation 取「与」，任一为 False 即关闭。
    - clean_symbols：扩展符号归一化（引号/方括号/省略号/破折号/波浪号）
      + 省略号长度归一（一行内多对省略号 → 一对标准 ……）默认开。
    - clean_md_tags：剥离残留 Markdown 标记（**粗体** / ~~删除~~ / `代码`）默认开。
    - clean_html_tags：剥离非白名单 HTML 标签（保留 rich text）默认开；
      与旧参数 strip_tags 取「与」，任一为 False 即不剥标签。
    - clean_paragraphs：段落合并优化（OCR 拆段修复）默认关；
      与旧参数 merge_paragraphs 取「或」，任一为 True 即合并。合并成功的
      段落会自动清理段内空白（软换行/行首行尾/中文间空格等），即使
      clean_spaces=False 也生效；未参与合并的段落不受影响。
    - clean_digit_punct：数字里的中文标点 → 英文（2026-10，映射表可在设置里自定义，
      见 configmanage.CLEAN_DIGIT_PUNCT_MAP_DEFAULT / ui_settings.clean_digit_punct_map）：
      1。2 → 1.2、1，2 → 1,2、1。 → 1.。与 clean_punctuation 独立，且在其之后执行
      （标点/符号归一先跑完，用户表拥有最终解释权）。

    2026-09-21：剥除 $ 紧贴括号包裹（$〔1〕$ → 〔1〕）——OvisOCR/o 系模型
    自造 $ 包裹引注括号；不紧贴括号的 $（价格 $5 / $x$ 等）原样保留。

    - clean_spaces：去除段落内的空白（2026-10-21 新增，默认关）：
      空格/制表符/全角空格等一律删除，OCR 软换行也合并回自然段（换行在
      浏览器里会渲染成词间空隙）；ptoe-align-lines 逐行段落仍保留换行。
      clean_spaces_keep_word_gap=True（默认）时保留英文单词之间的单个空格。

    幂等：已清理的内容再次清理结果不变。
    """
    text = str(raw or "")
    effective_strip_tags = strip_tags and clean_html_tags
    effective_punct = normalize_punctuation and clean_punctuation
    effective_merge = merge_paragraphs or clean_paragraphs
    if effective_strip_tags:
        text = _STRIP_TAG_RE.sub("", text)
    # $ 紧贴括号包裹剥除：必须在标签剥除后 / sanitize 前执行（此时括号
    # 相邻关系最完整，且不依赖标签结构）；幂等（剥完一遍后无 $ 紧贴括号残留）。
    text = _strip_dollar_bracket_hug(text)
    html = sanitize_html(text)
    stats: dict = {
        "punct": 0,
        "symbols": 0,
        "md": 0,
        "merged": 0,
        "tags": 0,
        "chars": 0,
        "spaces": 0,
    }
    # 数字标点映射表三态：None → stringmanage 默认表（调用方没配过）；
    # [] → 显式空表（不替换）；list → 按用户表替换。clean_digit_punct=False 则整步跳过。
    digit_pairs = None
    if clean_digit_punct:
        if digit_punct_map is None:
            from stringmanage import DIGIT_PUNCT_DEFAULT as _dpd

            digit_pairs = [list(p) for p in _dpd]
        else:
            digit_pairs = digit_punct_map
    out = _clean_blocks(
        html,
        merge_paragraphs=effective_merge,
        strip_leading_symbols=strip_leading_symbols,
        normalize_punctuation=effective_punct,
        clean_symbols=clean_symbols,
        clean_md_tags=clean_md_tags,
        symbol_pairs=symbol_map,
        digit_punct_pairs=digit_pairs,
        clean_spaces=clean_spaces,
        keep_word_gap=clean_spaces_keep_word_gap,
        stats=stats,
    )
    # 计算 tags/chars
    try:
        stats["tags"] = max(0, _count_tags(str(raw or "")) - _count_tags(out))
    except Exception:
        stats["tags"] = 0
    r = str(raw or "")
    if not r.strip():
        stats = {"punct": 0, "symbols": 0, "md": 0, "merged": 0, "tags": 0, "chars": 0}
    else:
        stats["chars"] = _char_delta(r, out)
    # 补齐
    for k in ("punct", "symbols", "md", "merged", "tags", "chars"):
        stats[k] = int(stats.get(k, 0))
    if with_stats:
        return out, stats
    return out


_HEADING_TAG_RE = re.compile(r"</?h[1-6]([^>]*)>", flags=re.IGNORECASE)


def _headings_to_body(raw: str) -> str:
    """把块级标题标签（<h1>-<h6>）归一为正文 <p>，属性与内部内容原样保留。

    矫正界面收到的文本一律按正文展示（2026-08-15）：OCR 自动结构（detect_headings
    整页 <h1>、bbox 标题转 <h2>）多为启发式结果，不再以标题样式进入编辑器——
    标题应由用户在界面中手动标记。标记 span/注释/对齐类/图片/行内格式等不受影响。
    """

    def _repl(m: re.Match) -> str:
        if m.group(0).startswith("</"):
            return "</p>"
        attrs = m.group(1)
        return f"<p{attrs}>" if attrs else "<p>"

    return _HEADING_TAG_RE.sub(_repl, str(raw))


# 块级元素开标签：p / div / h1-h6。属性部分用「非引号内 > 才算结束」的
# 保守写法（属性里可能含 >，如 data-x="a>b"），避免把标签切断。
_BLOCK_OPEN_TAG_RE = re.compile(
    r"<(p|div|h[1-6])((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>(?![ \t]*\r?\n)",
    re.IGNORECASE,
)


def _ensure_block_newlines(html: str) -> str:
    """历史遗留/无换行 HTML 归一：每个块级开标签后补一个换行。

    rulemanage 的页面纯文本约定是「段与段的边界就是换行符 \n」（测试
    TestParagraphStartExclusion）。旧版本矫正界面生成的页面 HTML（\u201c\u201d.join
    的 <div> 行）与部分历史缓存没有该换行，导致 [^\\n] 类规则正则跨越块边界
    （行首引注〔n〕被误判为行内引注）。幂等：已紧跟换行/空白+换行的块不动。
    """
    return _BLOCK_OPEN_TAG_RE.sub(lambda m: f"{m.group(0)}\n", str(html))


_TABLE_WHOLE_LINE_RE = re.compile(
    r"^<(?:table|p)\b[^>]*>.*</(?:table|p)>$", flags=re.IGNORECASE
)


def _tables_enabled() -> bool:
    """矫正编辑器是否自动把识别文本里的文字表格转成原生表格。

    读 config.json 顶层 ``table_convert``（缺失或读取异常一律回退 True，即默认开）；
    与 ``stringmanage._tables_enabled`` 消费同一键，保证两条链路行为一致。
    """
    try:
        from configmanage import get_config

        return bool(get_config(show_dialogs=False).get("table_convert", True))
    except Exception:
        return True


def _page_text(raw: str, *, normalize_headings: bool = True) -> str:
    """矫正界面初始内容：普通 OCR 文本按行转 <div>；已清洗的 HTML 原样返回。

    保存/暂存/历史缓存里存的是 sanitize_html 后的片段（如 <p>…<span
    data-ptoe-marker=...>…</p>），刷新或历史预加载时不能再走 initial_html
    （会把标签转义成可见文本）。所有传入矫正界面的文本一律按正文展示：
    结构化产生的 <h1>-<h6> 标题统一归一为 <p>（_headings_to_body，2026-08-15）。

    normalize_headings=False（2026-08-15 修复）：历史缓存/已保存内容按原样
    serve——其中可能含用户在界面手动设置的标题（<h1>-<h6>），不能再归一为
    <p>（否则「保存后重开，已设置的标题格式丢失」）。OCR 自动标题的归一
    只在写入历史时做一次（_save_ocr_history 走默认 True）。
    """
    if re.search(
        r"</?(?:p|div|h[1-6]|span|table|thead|tbody|tfoot|tr|th|td)([^>]*)>",
        raw,
        flags=re.IGNORECASE,
    ):
        # 2026-08-30：历史/已存 HTML 内容 serve 前做杂符括号清理（token 级，
        # 只动文本节点）——历史版本可能保存过 \\〔^{x〕}\\ 之类大模型杂符包裹，
        # 不清理则界面正文与 reocr 的 current_text 基准不一致、diff 偏移错位。
        cleaned = _clean_bracket_junk_html(raw)
        # 2026-09-15：历史/已存 HTML 也可能缺块间换行（旧 initial_html 输出），
        # 统一归一，保证 rulemanage 正则的行边界语义对存储内容也成立。
        cleaned = _ensure_block_newlines(cleaned)
        if normalize_headings:
            return _headings_to_body(cleaned)
        return cleaned
    return initial_html(raw)


def initial_html(text: str) -> str:
    """把一页 OCR 文本转成界面初始 HTML：每行一个 <div>。

    HTML 会把文本节点里的换行折叠成空格（导致“内容拥挤到一整段”），
    所以必须按行生成块级元素，才能在编辑区保留原始段落/行结构。
    清洗器会把 <div> 归一化为 <p>，保证往返（保存→清洗）不丢结构。

    2026-08-30：进入矫正界面前先做杂符括号清理（_normalize_brackets）——
    部分大模型把原文 〔x〕 引注识别成 \\〔^{x〕}\\ 的杂符包裹格式，这些
    无效字符须在界面可见/保存前清除（与 reocr 对比前清理保持一致，否则
    界面正文与 reocr 的 current_text 基准不一致，diff 偏移会错位）。

    2026-10-17：开关开启时（config 顶层 table_convert，默认开）先把文本里的
    文字表格转成原生 <table>；转换器产出的整行块（<table>…</table> 与表题
    <p>…</p>）原样放行，不再按纯文本转义成可见字符。
    """
    text = str(text)
    if _tables_enabled():
        from tableconvertmanage import convert_text_tables

        text = convert_text_tables(text)
    out = []
    for line in str(text).split("\n"):
        line = _normalize_brackets(line.strip())
        if not line:
            continue
        # 转换器产出的整行块（<table>…</table> / 表题 <p>…</p>）原样放行，
        # 不再当纯文本转义；其余行仍按行转 <div> 以保留段落/行结构。
        if _TABLE_WHOLE_LINE_RE.match(line):
            out.append(line)
        else:
            out.append(f"<div>{_html.escape(line, quote=False)}</div>")
    # 2026-09-15：块之间以 \n 分隔（rulemanage 的页面纯文本约定：段与段的边界
    # 就是换行符）。若无换行，[^\n] 类正则会把相邻 <div> 视作同一行，导致
    # 「行首引注被误判为行内引注」（如正文 2 处〔n〕+ 注释 2 处〔n〕共匹配 4 处）。
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 标记 → 文章结构
# ---------------------------------------------------------------------------


def _split_segments(
    html: str,
    keep_nbsp: bool = False,
) -> tuple[list[tuple[str, list[tuple[str, str]]]], list[tuple[str, str]]]:
    """把块内 html 按标记 span 切分为内容段，返回 (segments, trailing)。

    - segments: [(内容 html, 段首标记列表), ...]，标记作用于紧随其后的内容段；
    - trailing: 最后一段内容之后的标记（段尾标记），作用于后续块的内容。
    段内行内标签（strong/em/span，见 _INLINE_TAGS）在标记处保持闭合平衡：
    标记切在行内标签内部时，前段自动补闭合、后段重新打开，保证每段都是合法
    片段。（此前只跟踪 strong/em，注释 span 被标记切断时会漏掉 </span>，
    合并段落后产生未闭合标签 → 导出 EPUB 后段落结构错乱。）
    keep_nbsp=True：多行对齐段（ptoe-align-lines），段尾的 &nbsp; 填充是版式的
    一部分，strip() 默认口径会把它剥掉——剥了对齐就废了。
    """
    segments: list[tuple[str, list[tuple[str, str]]]] = []
    trailing: list[tuple[str, str]] = []
    buf: list[str] = []
    stack: list[tuple[str, str]] = []  # 未闭合的行内标签 [(标签名, 原始开标签)]
    pending: list[tuple[str, str]] = []  # 段首标记（位于当前内容之前）

    def flush(reopen: bool = False) -> list[tuple[str, str]]:
        """输出当前内容段；reopen=True 时返回需在下一段重新打开的行内标签。"""
        if not (buf or stack):
            return []
        content = "".join(buf) + "".join(f"</{t}>" for t, _o in reversed(stack))
        # 无可见文本（如连续标记之间的空行内标签）不产出段，标记继续累积；
        # 但纯图片块（<img> 无文字）必须保留——剥标签后为空但内容有效
        # 多行对齐段：&nbsp; 算内容不算空白（否则只剩填充的行会被丢掉）
        if not blank_block_ws(re.sub(r"<[^>]+>", "", content), keep_nbsp) or "<img" in content.lower():
            segments.append((strip_block_ws(content, keep_nbsp), list(pending)))
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
            for name, open_html in flush(reopen=True):
                buf.append(open_html)
                stack.append((name, open_html))
            pending.append((m.group(1), m.group(2).strip()))
            continue
        m = re.fullmatch(r"<(/)?([A-Za-z][\w:-]*)([^>]*)>", tok)
        if m and m.group(2).lower() in _INLINE_TAGS:
            tag = m.group(2).lower()
            if m.group(1):  # 闭标签：弹出到最近的同名标签（容忍交叉闭合）
                for i in range(len(stack) - 1, -1, -1):
                    if stack[i][0] == tag:
                        del stack[i:]
                        break
            else:
                stack.append((tag, tok))
            buf.append(tok)
            continue
        buf.append(tok)  # 文本 / <br/> / <img/> / 其他（清洗后只可能是合法内容）
    flush()
    trailing = pending
    return segments, trailing


# ---------------------------------------------------------------------------
# 分栏（ptoe-noborder 无边框表格）导出增强（2026-10）
# ---------------------------------------------------------------------------
# 分栏 = 无边框两列表：左 25% 旁注 / 右 75% 正文，每格一段（见 ui/app.js
# convertSelectionToColumns）。导出时需要：
#   （1）单元格内多段保留（不再被并成一段）；
#   （2）单元格内段落标记（join）把标记前后两段合为一个整段；
#   （3）一篇文章里连续的多个分栏自动并为一张表（中间的纯段落标记段视为桥接）。
_CELL_PARA_TAG_RE = re.compile(r"</?(p|div|h[1-6])\b[^>]*>", re.IGNORECASE)
# 单元格段落开标签（捕获属性串）：段落级 class（引用/注释/对齐…）与「段落设置」
# data-* 需在并段/跨表桥接重建 <p> 时逐段保留，否则表格里的引用/缩进导出后被清成正文。
_CELL_PARA_OPEN_RE = re.compile(r"<(p|div|h[1-6])\b([^>]*)>", re.IGNORECASE)
_CELL_INNER_RE = re.compile(r"(<(td|th)\b[^>]*>)(.*?)(</\2>)", re.DOTALL | re.IGNORECASE)
_TABLE_OPEN_RE = re.compile(r"<table\b[^>]*>", re.IGNORECASE)
_TABLE_CLOSE_RE = re.compile(r"</table>", re.IGNORECASE)
_TBODY_INNER_RE = re.compile(r"<tbody\b[^>]*>(.*)</tbody>", re.DOTALL | re.IGNORECASE)
_THEAD_RE = re.compile(r"<thead\b[^>]*>.*?</thead>", re.DOTALL | re.IGNORECASE)
# 无边框分栏表的整元素（TXT/MD/DOCX 导出前置处理用；EPUB 走 apply_markers 块级等价实现）
_TABLE_ELEM_NOBORDER_RE = re.compile(
    r'<table\b[^>]*\bclass="[^"]*\bptoe-noborder\b[^"]*"[^>]*>.*?</table\s*>',
    re.DOTALL | re.IGNORECASE,
)
# 仅含段落标记（join）的桥接段：标签间除空白/标记 span 外无其它内容
_MARKER_ONLY_PARA_RE = re.compile(
    r'<p\b[^>]*>(?:\s*<span\b[^>]*data-ptoe-marker[^>]*>\s*</span\s*>\s*)*</p\s*>',
    re.DOTALL | re.IGNORECASE,
)


def _cell_para_tokens(cell_html: str) -> list[list[tuple[str, str]]]:
    """把单元格内容拆成「段落 token 流」：每段 = [('attrs', 属性串) | ('text', html)
    | ('join'|'marker', '')]。

    段落边界 = 块级标签（<p>/<div>/<h1-6>）；无块级包裹的裸文本也算一段。
    标记 span（data-ptoe-marker）从其位置移除，类型作为 token 保留在所在段落中
    ——段外的标记挂到下一段段首（桥接段），供合并时判断前/后向合并方向。
    段首的 ('attrs', 属性串) 记录该段的 class/data-* （白名单过滤后）：重建 <p>
    时必须回填，否则表格单元格里的引用/注释/段落设置在导出后降级为正文。
    """
    paras: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] | None = None
    pending: list[tuple[str, str]] = []
    in_para = False

    def close_para() -> None:
        nonlocal cur
        if cur is not None:
            paras.append(cur)
        cur = None

    for tok in _TOKEN_RE.split(cell_html):
        if not tok:
            continue
        m = _MARKER_SPAN_RE.fullmatch(tok)
        if m:
            mt = "join" if m.group(1) == "join" else "marker"
            if cur is not None:
                cur.append((mt, ""))
            else:
                pending.append((mt, ""))
            continue
        if _CELL_PARA_TAG_RE.fullmatch(tok):
            if tok.startswith("</"):
                if in_para:
                    close_para()
                    in_para = False
            else:
                if cur is not None:  # 前一个块（裸文本/嵌套块）先收尾
                    close_para()
                in_para = True
                cur = pending
                pending = []
                om = _CELL_PARA_OPEN_RE.fullmatch(tok)
                raw_attrs = (om.group(2) if om else "") or ""
                # 段落级 class + 段落设置 data-* 白名单过滤（与表外块级同一套 helper）
                cur.append(
                    (
                        "attrs",
                        _block_class_html(raw_attrs)
                        + _block_indent_attr_html(raw_attrs),
                    )
                )
            continue
        if cur is None:
            cur = pending
            pending = []
        cur.append(("text", tok))
    close_para()
    if pending:
        paras.append(pending)
    return paras


def _cell_paragraph_items(cell_html: str) -> list[tuple[str, str]]:
    """单元格的 (段落属性串, 段落 HTML) 列表（标记全部丢弃）。

    属性串用于跨表桥接重建 <p>（保留首段格式），见 ``_cell_para_tokens``。
    """
    out: list[tuple[str, str]] = []
    for toks in _cell_para_tokens(cell_html):
        attrs = ""
        for k, h in toks:
            if k == "attrs":
                attrs = h
                break
        text = "".join(h for k, h in toks if k == "text").strip()
        if text:
            out.append((attrs, text))
    return out


def _cell_paragraph_texts(cell_html: str) -> list[str]:
    """单元格的段落纯文本列表（标记全部丢弃，用于跨表桥接取首/末段）。"""
    return [text for _attrs, text in _cell_paragraph_items(cell_html)]


def _apply_markers_in_cell(cell_html: str) -> str:
    """单元格内段落标记处理：join 把标记前后两段合为一个整段（<p>）。

    - 段首 join / 前段段尾 join → 与上一段合并；
    - 段尾 join → 与下一段合并；
    - 段中 join → 标记处拼接（同段内即删标记）；
    - 纯标记段（无文本）→ 把前后两段连起来（桥接）。
    其余标记（full/chapter/page/note）在单元格内无法表达，一并丢弃。
    无标记时原样返回（不引入多余的 <p> 包装）。
    """
    if "data-ptoe-marker" not in cell_html:
        return cell_html
    out: list[list[str]] = []  # 每项 [段落属性串, 段落 HTML]——合并时保留前一段属性
    merge_prev = False
    for toks in _cell_para_tokens(cell_html):
        attrs = ""
        for k, h in toks:
            if k == "attrs":
                attrs = h
                break
        idxs = [i for i, (k, h) in enumerate(toks) if k == "text" and h.strip()]
        if not idxs:
            if any(k == "join" for k, _ in toks):
                merge_prev = True
            continue
        content = "".join(h for k, h in toks if k == "text").strip()
        lead = any(k == "join" for k, _ in toks[: idxs[0]])
        tail = any(k == "join" for k, _ in toks[idxs[-1] + 1:])
        if (lead or merge_prev) and out:
            out[-1][1] += content  # 合并进上一段：保留上一段（前段）的格式
        elif content:
            out.append([attrs, content])
        merge_prev = tail
    return "".join(f"<p{a}>{c}</p>" for a, c in out)


def _apply_markers_in_table(table_html: str) -> str:
    """对整张表格的每个单元格施加段落标记处理；表内其它位置残留标记一并丢弃。"""
    if "data-ptoe-marker" not in table_html:
        return table_html

    def repl(m: "re.Match[str]") -> str:
        return m.group(1) + _apply_markers_in_cell(m.group(3)) + m.group(4)

    return _MARKER_SPAN_RE.sub("", _CELL_INNER_RE.sub(repl, table_html))


def _table_rows_inner(tbl: str) -> str:
    """取表格的行内容（tbody 内层；无 tbody 则退回 table 内层并去掉 thead）。"""
    m = _TBODY_INNER_RE.search(tbl)
    if m:
        return m.group(1)
    inner = _TABLE_OPEN_RE.sub("", tbl, count=1)
    close = _TABLE_CLOSE_RE.search(inner)
    if close:
        inner = inner[: close.start()]
    return _THEAD_RE.sub("", inner)


def _join_last_first_cell(rows1: str, rows2: str) -> tuple[str, str]:
    """把前表最后一格的末段与后表第一格的首段拼接为一段（段落标记跨表桥接）。"""
    c1 = list(_CELL_INNER_RE.finditer(rows1))
    c2 = list(_CELL_INNER_RE.finditer(rows2))
    if not c1 or not c2:
        return rows1, rows2
    last1, first2 = c1[-1], c2[0]
    p1 = _cell_paragraph_items(last1.group(3))
    p2 = _cell_paragraph_items(first2.group(3))
    if not p1 or not p2:
        return rows1, rows2
    a1, t1 = p1[-1]
    _a2, t2 = p2[0]
    tail = t1 + t2
    # 合并段保留前表末段的格式（与表内 join 合并语义一致）
    new1 = "".join(f"<p{a}>{t}</p>" for a, t in p1[:-1]) + f"<p{a1}>{tail}</p>"
    new2 = "".join(f"<p{a}>{t}</p>" for a, t in p2[1:])
    rows1 = rows1[: last1.start(3)] + new1 + rows1[last1.end(3):]
    rows2 = rows2[: first2.start(3)] + new2 + rows2[first2.end(3):]
    return rows1, rows2


def _merge_two_column_tables(t1: str, t2: str, *, join_cells: bool = False) -> str:
    """把两张无边框分栏表的**内层**（tbody…）并成一份。join_cells=True 时把跨表前后两段合一。

    入参是表格块的 html 内层（不含 <table>/</table>，由 apply_markers 的块解析器
    剥离、render_block 再包回），所以这里只返回内层。
    """
    head = _THEAD_RE.search(t1)
    rows1 = _table_rows_inner(t1)
    rows2 = _table_rows_inner(t2)
    if join_cells:
        rows1, rows2 = _join_last_first_cell(rows1, rows2)
    return (head.group(0) if head else "") + f"<tbody>{rows1}{rows2}</tbody>"


def _merge_noborder_tables(t1: str, t2: str, *, join_cells: bool = False) -> str:
    """把两张完整的无边框分栏 <table> 元素并成一张（返回完整表格元素）。

    join_cells=True 时把跨表的前表末格末段与后表首格首段拼成一段（段落标记桥接）。
    """
    open_m = _TABLE_OPEN_RE.match(t1)
    if not open_m:
        return t1
    head = _THEAD_RE.search(t1)
    rows1 = _table_rows_inner(t1)
    rows2 = _table_rows_inner(t2)
    if join_cells:
        rows1, rows2 = _join_last_first_cell(rows1, rows2)
    return (
        open_m.group(0)
        + (head.group(0) if head else "")
        + f"<tbody>{rows1}{rows2}</tbody></table>"
    )


def _resolve_noborder_tables(html: str) -> str:
    """TXT/MD/DOCX 导出前置处理（EPUB 走 apply_markers 的块级等价实现）。

    1. 逐表施加单元格内段落标记（join）合并（``_apply_markers_in_table``）；
    2. 把连续的无边框分栏 <table> 并为一张——中间只隔一个「纯段落标记段」时
       视为跨表桥接（首尾两段合一），紧邻或仅空白相隔则直接并表。
    与 2026-10 需求「一篇文章多个连续分栏自动合并 + 标记前后两段合一」一致。
    """
    html = str(html or "")
    if "<table" not in html.lower():
        return html
    # 1) 表内标记（跨表桥接段在表外，此步不碰）
    html = re.sub(
        r"(<table\b[^>]*>)(.*?)(</table\s*>)",
        lambda m: m.group(1) + _apply_markers_in_table(m.group(2)) + m.group(3),
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # 2) 连续无边框分栏表合并
    matches = list(_TABLE_ELEM_NOBORDER_RE.finditer(html))
    if len(matches) < 2:
        return html
    out: list[str] = []
    cur_tbl: str | None = None
    prev_end = 0
    for m in matches:
        if cur_tbl is None:
            out.append(html[: m.start()])
            cur_tbl = m.group(0)
        else:
            gap = html[prev_end : m.start()]
            if gap.strip() == "":
                cur_tbl = _merge_noborder_tables(cur_tbl, m.group(0), join_cells=False)
            elif _MARKER_ONLY_PARA_RE.fullmatch(gap.strip()):
                cur_tbl = _merge_noborder_tables(cur_tbl, m.group(0), join_cells=True)
            else:
                out.append(cur_tbl)
                out.append(gap)
                cur_tbl = m.group(0)
        prev_end = m.end()
    out.append(cur_tbl or "")
    out.append(html[prev_end:])
    return "".join(out)


def _is_column_table_item(item: dict[str, Any]) -> bool:
    """是否为无边框分栏表格块（只有它参与连续合并）。"""
    return item.get("kind") == "table" and "ptoe-noborder" in (item.get("attrs") or "")


def _is_pure_join_marker_item(item: dict[str, Any]) -> bool:
    """是否为「仅含段落标记（join）」的段落块（连续分栏之间的桥接段）。"""
    if item.get("kind") == "table":
        return False
    if any(html.strip() for html, _m in item.get("segments") or []):
        return False
    markers = [t for _h, ms in item.get("segments") or [] for t, _l in ms]
    markers += [t for t, _l in item.get("trailing") or []]
    return bool(markers) and all(t == "join" for t in markers)


def _coalesce_column_tables(
    parsed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并连续的无边框分栏表（含中间仅含 join 的桥接段）。

    多个分栏在文章里连续出现时，导出应呈现为一张连续分栏表（否则每张表之间会
    出现额外间距/断线）。桥接段的 join 语义：把前表最后一格的末段与后表第一格
    的首段并为一个整段（见 _join_last_first_cell）。
    """
    out: list[dict[str, Any]] = []
    i, n = 0, len(parsed)
    while i < n:
        item = parsed[i]
        if not _is_column_table_item(item):
            out.append(item)
            i += 1
            continue
        html = item["segments"][0][0] if item["segments"] else ""
        j = i + 1
        while j < n:
            nxt = parsed[j]
            if _is_column_table_item(nxt):
                html = _merge_two_column_tables(html, nxt["segments"][0][0])
                j += 1
            elif (
                _is_pure_join_marker_item(nxt)
                and j + 1 < n
                and _is_column_table_item(parsed[j + 1])
            ):
                html = _merge_two_column_tables(
                    html, parsed[j + 1]["segments"][0][0], join_cells=True
                )
                j += 2
            else:
                break
        out.append({**item, "segments": [(html, [])]})
        i = j
    return out


def apply_markers(pages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """提取矫正文本中的章节/段落标记，生成文章结构。

    输入：校正后的 pages [{'page', 'text'}]（text 为 sanitize_html 白名单 HTML）。
    返回：[{'text': article_html}, ...]，按全文标记拆分文章。标记的语义由
    所在位置决定（段首=块内内容之前的标记，段尾=块内最后一段内容之后的标记）：
    - 段落标记（join）：段首 → 本段与上一段合并为一个 <p>；
      段尾 → 本段与下一段合并为一个 <p>（OCR 把一整段拆成几段时，在断口处
      放一个段落标记即可拼回整段；标记在段中同样按该语义在标记处拼接）。
      **合并与段落是否为注释无关**（2026-09 统一）：任一侧是注释段落都会正常
      合并，合并结果保留前一段的对齐等属性；合并后是否为注释段取决于参与者
      ——各段都是注释才是注释段，只要混入正文段就合并为正文段，其中的注释内容
      用行内 <span class="ptoe-note"> 包起来，保证标记两侧的字号与矫正界面里
      各自设置的格式一致（整段被染成注释会让标记前的正文也变成小字灰字）；
      仅换页占位块（ptoe-page-break）与标题不参与合并；
    - 章节标记（chapter:N）：段尾 → 后续内容前插入 <h2>标签文本</h2>；
      段首 → 本段内容前插入 <h2>（旧数据兼容，界面已移除章节标记）；
    - 全文标记（full）：段尾 → 后续内容属于新文章（新的一页）；
      段首 → 本段内容属于新文章；
    - 换页标记（page）：从该标记之后的内容显示在新的一页（同一文章内强制
      分页，不拆文章；输出 `<p class="ptoe-page-break">`，CSS page-break-before
      实现）；段中/段首/段尾位置均按「标记之后的内容换页」处理；
    - 注释（class="ptoe-note" 的段落）：作为注释内容按文档顺序编号；正文中
      的注释标记（note）所在位置由对应注释替换（一个注释段落对应一个标记，
      数量不匹配时抛 ValueError 提示）；注释同样支持段落标记合并（因分页
      被折断的注释，后半段加段落标记即可与前半段合并为一条后再插入正文）。
      **文中没有任何注释标记时**：不做数量校验、不移动位置，注释段落原位
      保留（仅套用注释格式/小字），块内的段落标记按切段语义消费——
      注释段落的段落标记与正文段落同等生效，且可与正文段落合并。
    没有标记时返回单篇文章（等价于把所有 <p> 顺序拼接）。
    """
    # 1) 全书按页解析为块流（跨页连续处理，段落标记可跨页合并）
    #    表格整表原子累积：进入 <table> 后其内部的 <p>/<div>/<hN>（单元格段落）
    #    不再被当作块边界切分（2026-10：单元格现在会保留 <p>，若不整表原子化，
    #    表内段落标签会把表格切碎 → 结构错乱）。
    blocks: list[dict[str, Any]] = []
    for p in sorted(pages, key=lambda x: x["page"]):
        text = p.get("text") or ""
        kind: str | None = None
        note = False  # 当前块是否为注释（class="ptoe-note"）
        attrs = ""  # 当前块保留的 class 属性（对齐等）
        cur: list[str] = []
        tbl_depth = 0  # >0 = 正在累积表格块
        for tok in _TOKEN_RE.split(text):
            if not tok:
                continue
            m = _BLOCK_TAG_RE.fullmatch(tok)
            if not m:
                cur.append(tok)
                continue
            tag = m.group(1).lower()
            closing = tok.startswith("</")
            if tbl_depth:
                if tag == "table":
                    tbl_depth += -1 if closing else 1
                    if tbl_depth == 0:
                        blocks.append(
                            {"kind": "table", "html": "".join(cur), "note": False,
                             "attrs": attrs}
                        )
                        cur = []
                        kind = None
                        attrs = ""
                        continue
                cur.append(tok)  # 表格内层（含单元格内的 <p> 等）原样累积
                continue
            if tag == "table" and not closing:
                if kind:
                    blocks.append(
                        {
                            "kind": kind,
                            "html": "".join(cur),
                            "note": note,
                            "attrs": attrs,
                        }
                    )
                    cur = []
                    note = False
                kind = "table"
                tbl_depth = 1
                attrs = _table_open_attrs_str(m.group(2) or "")
                continue
            if kind:
                blocks.append(
                    {
                        "kind": kind,
                        "html": "".join(cur),
                        "note": note,
                        "attrs": attrs,
                    }
                )
                cur = []
                kind = None
                note = False
                attrs = ""
            if not closing:
                # div 一律按 <p> 处理（与 sanitize_html 一致）：块流只产出
                # <p>/<h1-6>，不会出现 <p><div>…</div></p> 非法嵌套；
                # 注释段落（ptoe-note）在 div 上同样能被正确识别。
                kind = "p" if tag == "div" else tag
                tag_attrs = m.group(2) or ""
                note = _NOTE_CLASS in tag_attrs
                # class + 段落设置 data-* 都要留：data-* 是 htmlmanage
                # _indent_style_attrs 转内联缩进/间距的唯一来源，剥掉则导出
                # EPUB 退回默认顶格（见 _block_indent_attr_html 注释）。
                attrs = _block_class_html(tag_attrs) + _block_indent_attr_html(tag_attrs)
            continue
        if kind or cur:
            blocks.append(
                {
                    "kind": kind or "p",
                    "html": "".join(cur),
                    "note": note,
                    "attrs": attrs,
                }
            )

    # 2) 块内按标记切段；3) 收集注释段落；4) 按标记重排
    parsed: list[dict[str, Any]] = []
    for b in blocks:
        if b["kind"] == "table":
            # 表格是**原子块**：整表作单个内容段，不按表内标记切段、不参与段落
            # 合并（_split_segments 会把表格结构切碎 → 每段各裹一层 <table>）。
            # 但单元格**内部**的段落标记仍需处理（2026-10）：join 把标记前后两段
            # 合为一个整段；full/chapter/page/note 在单元格内无法表达，丢弃。
            # 连续分栏表的合并见 _coalesce_column_tables（对所有表格块生效）。
            # push_content 的 `kind == "p"` 闸门天然让表格不进 _merge_paragraph。
            segments = [(_apply_markers_in_table(b["html"]), [])]
            trailing: list[tuple[str, str]] = []
        else:
            # 多行对齐段（2026-10-20）：段尾的 &nbsp; 填充是版式的一部分，切段时不能剥
            segments, trailing = _split_segments(
                b["html"], keep_nbsp=_ALIGN_LINES_CLASS in (b.get("attrs") or "")
            )
        parsed.append(
            {
                "kind": b["kind"],
                "note": b.get("note"),
                "attrs": b.get("attrs", ""),
                "segments": segments,
                "trailing": trailing,
            }
        )

    # 连续的无边框分栏表合并为一张（含中间仅含 join 的桥接段，2026-10）
    parsed = _coalesce_column_tables(parsed)

    # 注释段落（class="ptoe-note"）按文档顺序收集；段落标记（join）合并相邻注释
    annotations: list[str] = []
    merge_next = False  # 前一个 join 标记要求下一注释并入当前最后一条注释
    prev_note = False   # 当前文档位置紧邻的前一个实质块是否为注释（供“段首 join”判断）
    for item in parsed:
        if item["note"]:
            text = "".join(h for h, _m in item["segments"]).strip()
            first_join = any(
                t == "join" for h, ms in item["segments"][:1] for t, _l in ms
            )
            if text:
                if (merge_next or (first_join and prev_note)) and annotations:
                    annotations[-1] += text
                else:
                    annotations.append(text)
                prev_note = True
            merge_next = (
                any(t == "join" for t, _l in item["trailing"])
                if (text or prev_note)
                else False
            )
        else:
            has_content = bool(item["segments"])
            if not has_content and prev_note and any(
                t == "join" for t, _l in item["trailing"]
            ):
                # 独立段落标记块（例如跨页拆注释时单独放一个 join 标记）不打断
                # 注释合并链；把它视作“要求下一注释并入上一条”。
                merge_next = True
            else:
                # 正文/标题/图片等有内容块，或前面没有可合并注释时的纯标记块，
                # 都会打断注释合并链（防止 join 跨正文误并入更早注释）。
                prev_note = False
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
    # 文中没有任何注释标记时不校验数量：注释段落原位保留（仅套用注释格式）
    if note_markers and note_markers != len(annotations):
        raise ValueError(
            f"注释标记与注释数量不匹配：正文注释标记 {note_markers} 个，"
            f"注释段落 {len(annotations)} 个（一个注释段落对应一个注释标记）"
        )

    def render_block(kind: str, html: str, attrs: str = "") -> str:
        return f"<{kind}{attrs}>{html}</{kind}>"

    articles: list[str] = []
    cur_article: list[str] = []
    deferred_full = False
    deferred_chapter: str | None = None
    deferred_join = False
    deferred_page = False

    def flush_article() -> None:
        nonlocal cur_article
        if cur_article:
            articles.append({"text": "".join(cur_article)})
            cur_article = []

    def push_content(kind: str, html: str, attrs: str = "", note: bool = False) -> None:
        """把一段内容渲染进当前文章：先应用全文/章节/换页/段落合并标记。

        note：本段是否为注释段落（block class="ptoe-note"）。合并时注释属性取并集，
        与"只允许注释↔注释合并"的旧行为不同——段落标记的核心语义是「标记处没有
        段落边界」，两侧是不是注释段落不影响是否合并。
        """
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
            # 段落标记合并：仅"段落+段落"，换页占位块/标题不参与合并
            merged = None
            if kind == "p" and cur_article:
                merged = _merge_paragraph(cur_article[-1], html, note=note)
            deferred_join = False
            if merged is not None:
                cur_article[-1] = merged
                return
        cur_article.append(render_block(kind, html, attrs))

    def defer(markers: list[tuple[str, str]]) -> None:
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

    def note_spans(anns: list[str]) -> str:
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
    pending_notes: list[str] = []  # 段尾注释标记在无可依附段落时顺延到下一段内容
    for item in parsed:
        is_note = bool(item.get("note"))
        if is_note and note_markers:
            # 有正文注释标记时，注释段已被上方 annotations 收集并替换进正文，
            # 不再参与正文排版。注释段附近的段落标记只用于注释合并，不得顺延进
            # 正文流，否则会误合并注释段之后的正文段落。
            deferred_join = False
            continue
        # 段落标记（join）语义统一 —— 注释段落与正文段落一视同仁：
        #   段首 join / 上一块段尾 join → 与上一段合并；
        #   段尾 join                     → 与下一段合并；
        #   段中 join                     → 标记前后拼接为同一段。
        # 纯标记块（无可视内容段，如跨页单独放一个段落标记）不打断合并链：
        # 其 join 经 defer → deferred_join 顺延到下一个内容段。
        for html, markers in item["segments"]:
            seg_notes: list[str] = []
            rest: list[tuple[str, str]] = []
            for t, label in markers:
                if t != "note":
                    rest.append((t, label))
                elif not is_note:
                    seg_notes.append(annotations[note_idx])
                    note_idx += 1
                # 注释段内部的「注释标记」无意义（该段自身就是注释内容），忽略
            defer(rest)
            if html:
                if pending_notes:
                    html = note_spans(pending_notes) + html
                    pending_notes = []
                if seg_notes:
                    # 注释标记不打断段落：本段并入上一段，注释插在标记处
                    deferred_join = True
                push_content(
                    item["kind"],
                    note_spans(seg_notes) + html,
                    item.get("attrs", ""),
                    is_note,
                )
            else:
                pending_notes.extend(seg_notes)
        tail_notes: list[str] = []
        rest_tail: list[tuple[str, str]] = []
        for t, label in item["trailing"]:
            if t != "note":
                rest_tail.append((t, label))
            elif not is_note:
                tail_notes.append(annotations[note_idx])
                note_idx += 1
        defer(rest_tail)
        if tail_notes:
            spans = note_spans(tail_notes)
            merged = (
                _merge_paragraph(cur_article[-1], spans, note=False)
                if cur_article
                else None
            )
            if merged is not None:
                cur_article[-1] = merged
            else:
                pending_notes.extend(tail_notes)
    if pending_notes:
        # 极少数：注释标记悬空（其后没有内容段），补到当前文章末尾
        spans = note_spans(pending_notes)
        merged = (
            _merge_paragraph(cur_article[-1], spans, note=False) if cur_article else None
        )
        if merged is not None:
            cur_article[-1] = merged
        else:
            cur_article.append(f"<p>{spans}</p>")
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


def _preview_doc(state: dict[str, Any]):
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


def _page_dims(state: dict[str, Any]) -> dict[int, tuple[int, int]]:
    """读取各页原始宽高（PDF 页面矩形，pt；宽高比与 DPI 无关）。

    供 /api/pages 下发给前端：按「图片宽度为基准」预计算每行高度，
    虚拟列表未挂载行的前缀和也能精确计算（跳转定位不再依赖估算）。
    PDF 不可用时返回空 dict（前端回退到图片 onload 后逐行测量收敛）。
    """
    doc = _preview_doc(state)
    if doc is None:
        return {}
    lock = state.get("preview_doc_lock")
    try:
        with lock if lock is not None else nullcontext():
            return {
                i + 1: (round(doc[i].rect.width), round(doc[i].rect.height))
                for i in range(len(doc))
            }
    except Exception:
        return {}


def _render_jpeg(
    state: dict[str, Any], page_no: int, dpi: float
) -> tuple[str, bytes] | None:
    """用复用的 fitz.Document 把指定页渲染为 JPEG bytes（持 preview_doc_lock）。

    供预览（低 DPI）与原图回退（高 DPI）共用；PDF 不可用或渲染失败返回 None。
    优化：alpha=False 去透明通道、默认 DPI 90/质量 70。
    """
    doc = _preview_doc(state)
    if doc is None:
        return None
    # 防 use-after-close：文档已被关闭（会话结束/历史载入换档）时直接放弃渲染，
    # 避免 fitz C 层崩溃拖垮整个进程（前端表现为持续 NetworkError）
    if getattr(doc, "is_closed", False):
        return None
    try:
        import fitz

        lock = state.get("preview_doc_lock")
        quality = int(state.get("preview_quality", 70))
        if lock is not None:
            # fitz.Document 非线程安全：渲染放锁内
            with lock:
                if 1 <= page_no <= doc.page_count:
                    pix = doc[page_no - 1].get_pixmap(
                        matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0),
                        alpha=False,
                    )
                    return (
                        "image/jpeg",
                        pix.tobytes("jpeg", jpg_quality=quality),
                    )
        else:
            if 1 <= page_no <= doc.page_count:
                pix = doc[page_no - 1].get_pixmap(
                    matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0),
                    alpha=False,
                )
                return (
                    "image/jpeg",
                    pix.tobytes("jpeg", jpg_quality=quality),
                )
    except Exception:
        pass
    return None


def _read_preview_disk_cache(
    state: dict[str, Any], page_no: int
) -> tuple[str, bytes] | None:
    """读单页预览图磁盘缓存（多进程预热/现场渲染回写产物）。

    未命中或读取失败返回 None（回退现场渲染，不阻断）。
    """
    path = _preview_cache_path(
        state.get("pdf_path"), float(state.get("preview_dpi", 110)), page_no
    )
    if not path or not os.path.isfile(path):
        return None
    try:
        return ("image/jpeg", Path(path).read_bytes())
    except Exception:
        return None


def _write_preview_disk_cache(state: dict[str, Any], page_no: int, jpeg: bytes) -> None:
    """把现场渲染的预览 JPEG 原子写入磁盘缓存（best-effort，失败忽略）。"""
    path = _preview_cache_path(
        state.get("pdf_path"), float(state.get("preview_dpi", 110)), page_no
    )
    if not path:
        return
    try:
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(jpeg)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
    except Exception:
        pass


def _preview_bytes(state: dict[str, Any], page_no: int) -> tuple[str, bytes] | None:
    """返回 (content_type, bytes)。优先内存 LRU → 磁盘缓存 → 现场 PDF 渲染，
    失败则回退到原始页面图片文件。"""
    cache = state["preview_cache"]
    cached = cache.get(page_no)
    if cached is not None:
        if hasattr(cache, "move_to_end"):
            cache.move_to_end(page_no)  # LRU：命中刷新到队尾
        return cached
    # 磁盘缓存命中：多进程预热/历史回写产物，免渲染锁竞争直接读文件
    data = _read_preview_disk_cache(state, page_no)
    if data is None:
        data = _render_jpeg(state, page_no, float(state.get("preview_dpi", 110)))
        if data is not None:
            # 现场渲染成功 → 回写磁盘缓存（best-effort），下次会话免渲染
            _write_preview_disk_cache(state, page_no, data[1])
        else:
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


def _full_bytes(state: dict[str, Any], page_no: int) -> tuple[str, bytes] | None:
    """返回原始页面图片文件（用于点击查看原图）。

    `correct <pdf>` 直接命令没有 img_dir（未切图），或分割图片缺失时，
    回退到 PDF 高 DPI 渲染，保证原图模式始终有图可看。
    PDF/分割图均不可用时（如跨电脑导入历史记录），回退到内嵌预览图。
    """
    img_dir = state.get("img_dir")
    if img_dir:
        for ext, ctype in (
            ("png", "image/png"),
            ("jpg", "image/jpeg"),
            ("jpeg", "image/jpeg"),
        ):
            fp = Path(img_dir) / f"{page_no}.{ext}"
            if fp.is_file():
                return (ctype, fp.read_bytes())
    result = _render_jpeg(state, page_no, _FULL_DPI)
    if result is not None:
        return result
    # 内嵌预览图兜底：历史缓存中保存的 base64 JPEG（跨电脑时 PDF 不可用）
    embedded = (state.get("embedded_images") or {}).get(str(page_no))
    if embedded:
        try:
            return ("image/jpeg", base64.b64decode(embedded))
        except Exception:
            pass
    return None


# 重识别（reocr）页图的最大边长（px）。足够中文 OCR 的默认值，同时把图像 token
# 与 KV 压在 llama-server max_pixels（默认≈3.2M px）与 ctx-size（2026-09-01 VRAM
# 修复后 8192）预算内——高 DPI 分割图 / 原图回退(220 DPI) 的大图（A4@220≈4.68M px）
# 会超限或撑爆上下文，致 500（2026-09-01）。
_REOCR_MAX_SIDE = 1560


def _reocr_image(state: dict[str, Any], page_no: int) -> tuple[str, bytes] | None:
    """取重识别用的页图，超大页有界降采样。

    按 PDF 页面矩形计算一个有界 DPI，使最大边≈_REOCR_MAX_SIDE px：低于阈值的页
    不受影响、放大到阈值（窄小页面也得到足够分辨率），高于阈值的页被压下去，避免
    llama-server 因图像过大返回 500。复用已打开的 preview_doc（持 preview_doc_lock，
    不重开 PDF）。PDF 不可用时回退 _full_bytes（尽量有图可发）。
    """
    try:
        doc = _preview_doc(state)
        lock = state.get("preview_doc_lock")
        if (
            doc is not None
            and not getattr(doc, "is_closed", False)
            and 1 <= page_no <= doc.page_count
        ):
            with lock if lock is not None else nullcontext():
                r = doc[page_no - 1].rect
            max_dim = max(r.width, r.height)
            if max_dim > 0:
                dpi = (_REOCR_MAX_SIDE * 72.0) / max_dim
                res = _render_jpeg(state, page_no, dpi)
                if res is not None:
                    return res
    except Exception:
        pass
    return _full_bytes(state, page_no)


def _build_embedded_images(state: dict[str, Any]) -> dict[str, str]:
    """为历史缓存内嵌预览图：逐页渲染 JPEG → base64 字符串。

    低分辨率（110 DPI, quality=50），用于 PDF 路径不一致时的对比矫正。
    preview_doc 不可用时返回空 dict（不阻断写入）。

    增量缓存：复用 state["embedded_images"] 中已渲染的页面，只渲染缺失页，
    结果回写 state["embedded_images"]。PDF 页面内容不变，重复保存/暂存/完成
    不会重复渲染，避免大书每次保存都全量重渲染导致 CPU 飙升与按钮卡死。
    载入历史版本时 load 路径会预填 embedded_images，保存时直接复用。

    注意（2026-08-18）：保存/暂存/完成已不再调用本函数——版本文件 payload
    移除 images 键，预览图改由后台线程写入共享 sidecar <prefix>.images.json
    （见 _schedule_images_flush / _images_cache_path）。本函数保留供测试与
    兼容引用，非生产保存路径。
    """
    doc = _preview_doc(state)
    if doc is None:
        return {}
    lock = state.get("preview_doc_lock")
    # 复用会话内已渲染的页面（首次为空；载入历史版本时由 load 路径预填）
    images: dict[str, str] = dict(state.get("embedded_images") or {})
    dpi = state.get("preview_dpi", 110)
    quality = state.get("preview_quality", 82)
    try:
        import fitz as _fitz
    except ImportError:
        return images
    try:
        with lock if lock is not None else nullcontext():
            for i in range(len(doc)):
                key = str(i + 1)
                if key in images:
                    continue  # 已缓存：跳过渲染，避免重复 CPU 开销
                try:
                    pix = doc[i].get_pixmap(matrix=_fitz.Matrix(dpi / 72.0, dpi / 72.0))
                    jpg_bytes = pix.tobytes("jpeg", jpg_quality=min(quality, 50))
                    images[key] = base64.b64encode(jpg_bytes).decode("ascii")
                except Exception:
                    continue
            # 回写缓存：后续保存/暂存/完成直接复用，不再重复渲染
            state["embedded_images"] = images
    except Exception:
        return images
    return images


def _resolve_prerender_max() -> int:
    """预渲染上限：默认 _PRERENDER_MAX_PAGES；config.json 顶层键
    prerender_max_pages 可覆盖（非法值回退默认）。"""
    try:
        from configmanage import get_config

        v = (get_config(show_dialogs=False) or {}).get("prerender_max_pages")
        if v is None:
            return _PRERENDER_MAX_PAGES
        n = int(v)
        if n > 0:
            return n
    except Exception:
        pass
    return _PRERENDER_MAX_PAGES


# ---------------------------------------------------------------------------
# 预览图磁盘缓存（Plan A：多进程分块渲染 → 磁盘缓存，避免重复现场渲染）
# ---------------------------------------------------------------------------

def _preview_cache_dir(pdf_path: str | None, dpi: float) -> str | None:
    """预览图磁盘缓存目录：<dir-of-pdf>/preview_cache/<prefix>_<dpi>/。

    无 pdf_path 时返回 None（禁用磁盘缓存，回退现有路径）。
    """
    if not pdf_path:
        return None
    prefix = _history_prefix(pdf_path)
    if not prefix:
        return None
    return str(Path(pdf_path).resolve().parent / "preview_cache" / f"{prefix}_{int(dpi)}")


def _preview_cache_path(pdf_path: str | None, dpi: float, page_no: int) -> str | None:
    """单页预览图磁盘缓存路径：<cache_dir>/<page_no>.jpg。"""
    d = _preview_cache_dir(pdf_path, dpi)
    if d is None:
        return None
    return os.path.join(d, f"{page_no}.jpg")


# 模块顶层 worker（必须 picklable；Windows spawn 模式下不能是嵌套函数）
def _render_preview_chunk(args: tuple) -> list[tuple[int, bytes]]:
    """渲染一个页号块：打开独立 fitz.Document，逐页 JPEG 编码。

    args = (pdf_path, dpi, page_numbers_list)
    返回 [(page_no, jpeg_bytes), ...]；单页失败跳过，不中断整块。
    """
    pdf_path, dpi, page_numbers = args
    out: list[tuple[int, bytes]] = []
    try:
        import fitz as _fitz

        doc = _fitz.open(pdf_path)
        for pn in page_numbers:
            try:
                if 1 <= pn <= doc.page_count:
                    pix = doc[pn - 1].get_pixmap(
                        matrix=_fitz.Matrix(dpi / 72.0, dpi / 72.0),
                        alpha=False,
                    )
                    out.append((pn, pix.tobytes("jpeg", jpg_quality=70)))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _warm_preview_cache(state: dict[str, Any]) -> None:
    """后台线程：多进程预热预览图磁盘缓存。

    跳过已有缓存页；剩余页按 ~16 页/块提交 ProcessPoolExecutor。
    配置键 preview_workers（int，默认 min(4, cpu_count)）控制进程数。
    任何异常（含 frozen exe spawn 失败）打印一行提示后静默返回。
    """
    pdf_path = state.get("pdf_path")
    if not pdf_path:
        return
    dpi = float(state.get("preview_dpi", 110))
    cache_dir = _preview_cache_dir(pdf_path, dpi)
    if cache_dir is None:
        return
    # 读取进程数配置
    try:
        from configmanage import get_config

        cfg = get_config(show_dialogs=False) or {}
        pw = cfg.get("preview_workers")
        n_workers = int(pw) if pw is not None else 0
        if n_workers <= 0:
            n_workers = min(4, os.cpu_count() or 1)
    except Exception:
        n_workers = min(4, os.cpu_count() or 1)
    # 打开 PDF 统计页数 + 扫描已有缓存
    try:
        import fitz as _fitz

        doc = _fitz.open(pdf_path)
        total = doc.page_count
    except Exception:
        return
    # 小书不值得起进程池（现场渲染足够快，也避免测试误触真实进程池）
    if total < _WARM_MIN_PAGES:
        return
    os.makedirs(cache_dir, exist_ok=True)
    existing: set[int] = set()
    try:
        for fn in os.listdir(cache_dir):
            if fn.endswith(".jpg"):
                try:
                    existing.add(int(fn[:-4]))
                except ValueError:
                    continue
    except Exception:
        pass
    # 收集待渲染页号
    pending = [pn for pn in range(1, total + 1) if pn not in existing]
    if not pending:
        return
    chunks: list[list[int]] = []
    i = 0
    while i < len(pending):
        chunks.append(pending[i : i + 16])
        i += 16
    print(f"      预览图缓存预热：共 {total} 页，{n_workers} 进程")
    try:
        # 使用模块级引用，便于测试 monkeypatch
        pool = _PREVIEW_POOL_CLS(max_workers=n_workers)
    except Exception:
        print("      预览图缓存预热不可用，回退现场渲染")
        return
    written = 0
    try:
        futures = {
            pool.submit(_render_preview_chunk, (pdf_path, dpi, chunk)): chunk
            for chunk in chunks
        }
        for fut in concurrent.futures.as_completed(futures):
            try:
                results = fut.result()
            except Exception:
                continue
            for pn, jpeg_bytes in results:
                if not jpeg_bytes:
                    continue
                fp = os.path.join(cache_dir, f"{pn}.jpg")
                try:
                    fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
                    try:
                        with os.fdopen(fd, "wb") as f:
                            f.write(jpeg_bytes)
                        os.replace(tmp, fp)
                        written += 1
                    except Exception:
                        try:
                            os.remove(tmp)
                        except Exception:
                            pass
                except Exception:
                    pass
    finally:
        pool.shutdown(wait=False)
    mb = sum(
        os.path.getsize(os.path.join(cache_dir, f"{pn}.jpg"))
        for pn in range(1, total + 1)
        if os.path.isfile(os.path.join(cache_dir, f"{pn}.jpg"))
    ) / 1048576
    print(f"      预览图缓存预热完成：新增 {written} 页，约 {mb:.0f} MB")


def _prerender_embedded_images(state: dict[str, Any]) -> None:
    """后台线程：渐进式渲染预览图填充 state["embedded_images"]。

    让首次保存/暂存/完成（无前置保存预热缓存）也快——用户在浏览器编辑期间
    把所有页面渲染好。渲染结果由保存时的后台 flush 与线程结束时的补写
    （见 _schedule_images_flush / _write_images_cache）落到每 book 一份的
    共享 sidecar，不再有同步全量渲染/全量序列化阻塞按钮（曾致 CPU 飙升、
    按钮卡死、完成转换超时）。

    设计要点：
    - 每页单独持 preview_doc_lock（释放间隔 → UI 预览可穿插），不整循环持锁
      （否则大书渲染期间预览图全部被阻塞，AGENTS 强调预览渲染性能敏感）
    - 复用已缓存页（用户先保存过一次 → 后台跳过这些页）
    - 渲染失败页置空字符串占位（key 存在 → 后续 flush 跳过；
      _full_bytes 的 `if embedded:` 判定 falsy → 不用坏图，回退其它路径）
    - 每页 sleep ~50ms 让出 CPU 给 UI/请求线程
    - 每次循环重取 doc/embedded_images（历史版本载入可能重置两者，见
      /api/history/load :3683-3694），避免基于旧引用渲染落空
    - 用户点「完成」后 state["finished"] 置位 → 线程退出
    """
    try:
        import fitz as _fitz
    except ImportError:
        return  # 无 fitz：预览回退原始图片文件，无需预渲染
    dpi = float(state.get("preview_dpi", 110))
    quality = int(state.get("preview_quality", 82))
    finished = state.get("finished")
    max_pages = int(state.get("prerender_max_pages") or _PRERENDER_MAX_PAGES)
    while True:
        if finished is not None and finished.is_set():
            return
        doc = _preview_doc(state)
        if doc is None:
            # PDF 不可用（无文件会话或打开失败）：无可预渲染，退出
            return
        # 每次循环重读 embedded_images（历史载入会整体替换该 dict）
        embedded = state.get("embedded_images")
        if embedded is None:
            embedded = {}
            state["embedded_images"] = embedded
        # 找首个未缓存页（1-based → key "1".."N"）
        target = None
        for i in range(len(doc)):
            key = str(i + 1)
            if key not in embedded:
                target = (i, key)
                break
        if target is None:
            # 全部页已缓存：把完整预览图写入共享 sidecar（版本文件不携带
            # images）——保证侧车最终完整，覆盖用户保存时缓存尚不完整、
            # 后台 flush 只落盘了部分页的场景
            prefix = state.get("history_prefix")
            if prefix and embedded:
                _write_images_cache(prefix, dict(embedded))
            mb = sum(len(v) for v in embedded.values()) / 1048576
            print(f"      预览图预渲染完成：{len(embedded)} 页，约 {mb:.0f} MB")
            return  # 全部页已缓存
        i, key = target
        if len(embedded) >= max_pages:
            mb = sum(len(v) for v in embedded.values()) / 1048576
            prefix = state.get("history_prefix")
            if prefix and embedded:
                _write_images_cache(prefix, dict(embedded))
            print(f"      预览图预渲染达上限 {max_pages} 页，已停止（已缓存约 {mb:.0f} MB；跨电脑兜底仅覆盖前段页面）")
            return
        lock = state.get("preview_doc_lock")
        try:
            with lock if lock is not None else nullcontext():
                pix = doc[i].get_pixmap(matrix=_fitz.Matrix(dpi / 72.0, dpi / 72.0))
                jpg_bytes = pix.tobytes("jpeg", jpg_quality=min(quality, 50))
                embedded[key] = base64.b64encode(jpg_bytes).decode("ascii")
        except Exception:
            # 渲染失败：置空字符串占位（key 存在 → _build_embedded_images 跳过；
            # _full_bytes `if embedded:` falsy → 回退其它路径，不用坏图）
            embedded[key] = ""
        time.sleep(0.05)  # 让出 CPU 给 UI/请求线程


# ---------------------------------------------------------------------------
# 内嵌预览图后台刷盘
#   版本文件不再携带 images：大书每次保存/暂存曾把整本书预览图 base64 一起
#   json.dumps + 写盘（实测 536 页书单文件 110MB），CPU 飙升、按钮卡死数秒。
#   预览图改为写入每本 book 一份的共享 sidecar <prefix>.images.json——
#   保存/暂存/完成只在有新增内容时于后台线程快照刷写一次，绝不阻塞请求线程；
#   预渲染线程跑完全部页后也会补写一次保证侧车完整。
# ---------------------------------------------------------------------------


def _schedule_images_flush(state: dict[str, Any]) -> None:
    """后台把 state["embedded_images"] 当前快照写入 sidecar（单次，不阻塞调用方）。

    保存/暂存/完成各自触发一次；同一时间已有 flush 在跑时直接跳过
    （_images_flush_started 守卫去重，前一线程写完后标志复位，之后的下一次
    保存会再次触发）。不是周期循环：避免每次保存都对 ~100MB 侧车全量重写。
    """
    if state.get("_images_flush_started"):
        return
    state["_images_flush_started"] = True
    threading.Thread(
        target=_flush_embedded_images_once, args=(state,), daemon=True
    ).start()


def _flush_embedded_images_once(state: dict[str, Any]) -> None:
    """后台线程体：快照当前已缓存的预览图并写入 sidecar，随后复位守卫。"""
    try:
        prefix = state.get("history_prefix")
        if not prefix:
            return
        lock = state.get("preview_doc_lock")
        # 持锁快照：预渲染线程可能正在往 embedded_images 追加新页，
        # 避免 dict 在迭代/序列化期间被并发修改
        with lock if lock is not None else nullcontext():
            snap = dict(state.get("embedded_images") or {})
        if snap:
            _write_images_cache(prefix, snap)
    finally:
        state["_images_flush_started"] = False


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
_HISTORY_INDEX: dict[str, Any] = {"sig": None, "items": None}


def _history_dir() -> Path:
    """历史缓存目录：程序所在目录 data/correction_history/。

    冻结（onefile）运行时 __file__ 指向一次性解包临时目录，历史记录必须
    跟随 exe 所在目录（pdfmanage.app_base_dir），否则写进 %TEMP% 丢失。
    """
    from pdfmanage import app_base_dir

    return app_base_dir() / "data" / _HISTORY_DIR_NAME


def _server_info_path() -> Path:
    """矫正服务信息 sidecar 路径：data/correct_server.json。

    记录当前正在运行的矫正 HTTP 服务的端口、PID 和启动时间，供 GUI 配置
    中心发现并恢复已存活的矫正界面（浏览器关闭但服务仍在等待时）。
    """
    from pdfmanage import app_base_dir

    return app_base_dir() / "data" / "correct_server.json"


# 多实例 sidecar 互斥锁：correct_server.json 可能同时被多个矫正实例
# （同一进程内双开、或与 GUI 配置中心）读写，_write/_clear 必须串行化，
# 且删除条目只能针对本进程 pid+本实例端口，绝不误删其他实例的记录。
_CORRECT_INFO_LOCK = threading.Lock()


def _read_correct_server_info() -> list[dict]:
    """读取矫正服务信息 sidecar，返回实例列表 list[dict]。

    兼容两种历史形状：
    - {"instances": [{port,pid,started,pdf,history_id}, ...]}（新，多实例列表）
    - 旧单对象 {"port", "pid", "started"} → 包装为 [dict]
    损坏/缺失 → []。
    """
    try:
        p = _server_info_path()
        if not p.exists():
            return []
        with open(p, "r", encoding="utf-8") as f:
            info = json.load(f)
        if isinstance(info, list):
            return [x for x in info if isinstance(x, dict)]
        if isinstance(info, dict):
            inst = info.get("instances")
            if isinstance(inst, list):
                return [x for x in inst if isinstance(x, dict)]
            # 旧单对象形状（多实例改造之前的写入格式）
            if "port" in info or "pid" in info:
                return [info]
    except Exception:  # noqa: BLE001 — 损坏 sidecar 视为无记录
        pass
    return []


def _write_server_info(port: int, *, pdf: str = "", history_id: str = "") -> None:
    """原子写矫正服务信息 sidecar，追加/更新本实例（pid+port）条目。

    列表结构 {"instances": [...]}——同一进程可同时运行多个矫正实例
    （多开不同历史记录/PDF），互不覆盖；GUI 配置中心按列表发现全部实例。

    失败仅打印警告，不抛出——矫正流程不应因 sidecar 写入失败而中断。
    """
    try:
        from configmanage import _atomic_write_json

        entry = {
            "port": int(port),
            "pid": os.getpid(),
            "started": time.time(),
            "pdf": str(pdf or ""),
            "history_id": str(history_id or ""),
        }
        with _CORRECT_INFO_LOCK:
            p = _server_info_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            instances = _read_correct_server_info()
            replaced = False
            for it in instances:
                if it.get("pid") == os.getpid() and it.get("port") == entry["port"]:
                    it.update(entry)
                    replaced = True
                    break
            if not replaced:
                instances.append(entry)
            _atomic_write_json(str(p), {"instances": instances})
    except Exception as e:  # noqa: BLE001
        print(f"[correctmanage] 写入矫正服务信息 sidecar 失败: {e}")


def _clear_server_info(port: int | None = None) -> None:
    """清除矫正服务信息 sidecar 中本实例的条目，仅当记录的 pid 匹配当前进程时删除。

    port 给定时只删除「本 pid + 该端口」的条目（多实例并存时各清各的）；
    port 为 None 时删除本进程全部条目（兼容旧语义）。空列表则删除文件。
    任何异常静默吞掉。
    """
    try:
        with _CORRECT_INFO_LOCK:
            p = _server_info_path()
            if not p.exists():
                return
            instances = _read_correct_server_info()
            if port is None:
                kept = [it for it in instances if it.get("pid") != os.getpid()]
            else:
                kept = [
                    it
                    for it in instances
                    if not (
                        it.get("pid") == os.getpid() and it.get("port") == int(port)
                    )
                ]
            if not kept:
                p.unlink()
            elif len(kept) != len(instances):
                from configmanage import _atomic_write_json

                _atomic_write_json(str(p), {"instances": kept})
    except Exception:  # noqa: BLE001
        pass


def _history_prefix(pdf_path: str | None) -> str | None:
    """同一 PDF 的版本文件名前缀：按 PDF 绝对路径哈希（同名不同路径互不干扰）。"""
    if not pdf_path:
        return None
    import hashlib

    return hashlib.sha1(
        str(Path(pdf_path).resolve()).encode("utf-8", errors="replace")
    ).hexdigest()[:16]


def _version_prefix(version_id: str) -> str:
    """由版本文件 stem（<prefix>_<时间戳>_<随机>）还原 book 前缀。

    pdf 会话前缀为 16 位 sha1（无下划线）；无文件手动会话形如 manual_<随机>。
    用于从版本 id 定位该 book 共享的内嵌预览图 sidecar。
    """
    if version_id.startswith("manual_"):
        parts = version_id.split("_")
        return "_".join(parts[:2]) if len(parts) >= 2 else version_id
    return version_id.split("_", 1)[0]


def _images_cache_path(prefix: str) -> Path:
    """内嵌预览图共享 sidecar 路径：data/correction_history/<prefix>.images.json。

    同一 PDF 的预览图在所有版本间不变，故所有版本共享一份 sidecar——
    版本文件 payload 不再携带 images（曾致大书每次保存/暂存都 json.dumps +
    写盘 ~100MB base64，CPU 飙升、按钮卡死数秒；实测 536 页书单文件
    110MB）。跨电脑导入历史时把 <prefix>.images.json 与版本文件一并拷走即可。
    """
    return _history_dir() / f"{prefix}.images.json"


def _write_images_cache(prefix: str, images: dict[str, str]) -> bool:
    """原子写入内嵌预览图 sidecar（临时文件 + os.replace，同 configmanage 模式）。

    预览图是可再生成的缓存：失败仅打印原因返回 False，不影响保存主流程。
    优先使用 msgpack（更快、更小），回退到 gzip+JSON，再回退到纯 JSON。
    gzip 压缩级别用 1（实测 level 9 对 JPEG 派生数据压缩比几乎无增益但耗时 ~10 倍）。
    """
    if not prefix:
        return True
    try:
        d = _history_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{prefix}.images.{uuid4().hex[:6]}.tmp"
        # 优先 msgpack（二进制，无 base64 开销，序列化/反序列化 2-3x 更快）
        if msgpack is not None:
            with open(tmp, "wb") as f:
                msgpack.pack(images, f)
        else:
            # 回退：gzip 压缩 JSON（level 1 对 JPEG 已足够，速度远高于 level 9）
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=1) as gz:
                json.dump(images, gz, ensure_ascii=False)
        tmp.replace(_images_cache_path(prefix))
        return True
    except Exception as e:
        print(f"[correctmanage] 内嵌预览图缓存写入失败: {e}")
        return False


def _log_images_loaded(data: dict[str, str]) -> None:
    """载入 sidecar 后打印规模（页数 + base64 总量），让大书内存占用可见。"""
    mb = sum(len(v) for v in data.values()) / 1048576
    print(f"[correctmanage] 已载入预览图缓存 {len(data)} 页（约 {mb:.0f} MB）")


def _load_images_cache(prefix: str) -> dict[str, str]:
    """读取内嵌预览图 sidecar；缺失/损坏返回空 dict（调用方回退其它预览来源）。

    支持三种格式（按优先级）：msgpack（新）、gzip+JSON（中）、纯 JSON（旧）。
    成功载入时打印规模（_log_images_loaded）。
    """
    if not prefix:
        return {}
    data: dict[str, str] | None = None
    try:
        fp = _images_cache_path(prefix)
        if fp.is_file():
            # 1. 尝试 msgpack（新格式，二进制）
            if msgpack is not None and data is None:
                try:
                    with open(fp, "rb") as f:
                        parsed = msgpack.unpackb(f.read(), raw=False)
                    if isinstance(parsed, dict):
                        data = {str(k): str(v) for k, v in parsed.items()}
                except Exception:
                    pass  # 回退到 gzip/JSON
            # 2. 尝试 gzip 解压（中格式）
            if data is None:
                try:
                    with gzip.open(fp, "rt", encoding="utf-8") as gz:
                        content = gz.read()
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        data = {str(k): str(v) for k, v in parsed.items()}
                except Exception:
                    pass  # 回退到纯 JSON
            # 3. 回退：旧格式未压缩 JSON
            if data is None:
                parsed = json.loads(fp.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    data = {str(k): str(v) for k, v in parsed.items()}
    except Exception:
        return {}
    if data is not None:
        _log_images_loaded(data)
        return data
    return {}


def _history_entries(prefix: str | None = None) -> list[dict[str, Any]]:
    """列出历史缓存条目（新→旧）：{id, pdf, name, path, updated, pages}。

    P3：轻量索引——先比对目录签名（文件名+mtime+size），文件未变化时
    直接复用上次解析结果，避免 /api/history 每次全量 json.loads 所有版本文件。
    排除内嵌预览图共享 sidecar（<prefix>.images.json，见 _images_cache_path）：
    它不属于版本条目，且其内容（页→base64 dict）不满足版本文件结构。
    """
    d = _history_dir()
    if not d.is_dir():
        return []
    fps = sorted(
        (fp for fp in d.glob("*.json") if not fp.name.endswith(".images.json")),
        reverse=True,
    )
    try:
        sig = "|".join(
            f"{fp.name}:{fp.stat().st_mtime_ns}:{fp.stat().st_size}" for fp in fps
        )
    except OSError:
        sig = "|".join(fp.name for fp in fps)
    if _HISTORY_INDEX.get("sig") != sig:
        items: list[dict[str, Any]] = []
        for fp in fps:
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                pdf = str(data.get("pdf") or "")
                items.append(
                    {
                        "id": fp.stem,
                        "pdf": pdf,
                        "name": str(data.get("name") or Path(pdf).name or fp.stem),
                        "display_name": str(data.get("display_name") or "") or None,
                        "path": pdf,
                        "updated": str(data.get("updated") or ""),
                        "pages": len(data.get("pages") or {}),
                        "last_proofread_page": data.get("last_proofread_page"),
                    }
                )
            except Exception:
                continue
        _HISTORY_INDEX["sig"] = sig
        _HISTORY_INDEX["items"] = items
    if prefix:
        return [it for it in _HISTORY_INDEX["items"] if it["id"].startswith(prefix)]
    return list(_HISTORY_INDEX["items"])


def _load_latest_history_meta(pdf_path: str | None) -> dict[str, Any] | None:
    """同一 PDF 最新版本的完整历史数据（pages / proofread / last_proofread_page）。

    兼容旧版单文件格式 <prefix>.json。文件不存在/损坏返回 None。
    2026-10-17：从 _load_latest_history 抽出——启动矫正界面时除 pages 外还需
    回灌 proofread（已识别未应用的错误/建议/已忽略集合）。
    """
    prefix = _history_prefix(pdf_path)
    if not prefix:
        return None
    fps = sorted(_history_dir().glob(f"{prefix}_*.json"), reverse=True)
    if not fps:
        legacy = _history_dir() / f"{prefix}.json"
        if legacy.is_file():
            fps = [legacy]
    if not fps:
        return None
    try:
        return json.loads(fps[0].read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_latest_history(pdf_path: str | None) -> dict[str, str]:
    """同一 PDF 最新版本的矫正内容（兼容旧版单文件格式 <prefix>.json）。"""
    data = _load_latest_history_meta(pdf_path)
    return dict((data or {}).get("pages") or {})


def _load_history_version(version_id: str) -> dict[str, Any] | None:
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
        images = data.get("images")
        if not images:
            # 新格式（2026-08-18）：版本文件不再携带 images，改读该 book 的
            # 共享 sidecar <prefix>.images.json；旧版本文件仍带 images 键直接复用
            images = _load_images_cache(_version_prefix(version_id))
        return {
            "pages": dict(data.get("pages") or {}),
            "pdf": str(data.get("pdf") or "") or None,
            "proofread": data.get("proofread")
            or {"errors": {}, "original": {}, "dismissed": {}},
            "last_proofread_page": data.get("last_proofread_page"),
            "embedded_images": images or {},
        }
    except Exception:
        return None


def _history_pages_for_init(
    pdf_path: str | None,
    *,
    history: bool,
    preload_history: bool,
    history_id: str | None = None,
) -> dict[str, str]:
    """启动矫正界面时的初始文本来源。

    preload_history=True 时返回同一 PDF 最新历史版本（覆盖传入文本）；
    False 时返回空 dict（完全使用传入的 pages —— 重新识别后的新文本优先）。
    history_id 给定时直接返回该指定版本的 pages（绕过"最新版本"逻辑，
    由 correct_pages 在调用前负责校验版本存在并抛出 ValueError）。
    """
    if history_id:
        ver = _load_history_version(history_id)
        return dict(ver["pages"]) if ver is not None else {}
    if history and preload_history:
        return _load_latest_history(pdf_path)
    return {}


def _write_history_version(state: dict[str, Any]) -> bool:
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
            # 重命名功能的结果：保存/暂存/完成时保留，避免新版本回退到原始名
            "display_name": state.get("display_name"),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pages": {str(k): v for k, v in state["pages"].items()},
            # 文字纠错状态（errors/original/dismissed key 均为 str(页码)）
            "proofread": state.get("proofread")
            or {"errors": {}, "original": {}, "dismissed": {}},
            "last_proofread_page": state.get("last_proofread_page"),
            # 预览图不入版本文件（P 大书 100MB+ base64 曾致保存/暂存卡死数秒）：
            # 改由后台 flush 写入共享 sidecar <prefix>.images.json，见
            # _schedule_images_flush / _images_cache_path
        }
        with state["history_lock"]:
            stamp = time.strftime("%Y%m%d%H%M%S")
            fp = d / f"{prefix}_{stamp}_{uuid4().hex[:4]}.json"
            fp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _prune_history(prefix)
        # 预览图后台写 sidecar（不阻塞保存；预渲染线程跑完后也会补全）
        _schedule_images_flush(state)
        return True
    except Exception as e:
        # S4：写入失败必须上报（磁盘错误/权限等），不能静默丢数据
        print(f"[correctmanage] 历史缓存写入失败: {e}")
        return False


def _overwrite_history(state: dict[str, Any], *, history_id: str | None = None) -> bool:
    """保存动作：不新建历史版本，直接覆盖历史版本文件。

    history_id 给定时（打开指定历史版本矫正）直接覆盖该版本文件——
    修改仍在原版本迭代，不产生新版本条目；
    默认（无 history_id）覆盖当前（最新）历史版本文件——同一份内容反复保存
    只更新同一个文件，多版本列表不会被保存刷屏；
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
            # 重命名功能的结果：保存/暂存/完成时保留，避免新版本回退到原始名
            "display_name": state.get("display_name"),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pages": {str(k): v for k, v in state["pages"].items()},
            # 文字纠错状态（errors/original/dismissed key 均为 str(页码)）
            "proofread": state.get("proofread")
            or {"errors": {}, "original": {}, "dismissed": {}},
            "last_proofread_page": state.get("last_proofread_page"),
            # 预览图不入版本文件（P 大书 100MB+ base64 曾致保存/暂存卡死数秒）：
            # 改由后台 flush 写入共享 sidecar <prefix>.images.json，见
            # _schedule_images_flush / _images_cache_path
        }
        with state["history_lock"]:
            fp: Path | None = None
            if history_id:
                # 指定版本：直接覆盖该版本文件（id 即文件名 stem）；防路径穿越，
                # 校验解析后仍在历史目录内（<prefix>.images.json 共享 sidecar 除外）。
                cand = d / f"{history_id}.json"
                if (
                    cand.parent == d
                    and cand.is_file()
                    and not cand.name.endswith(".images.json")
                ):
                    fp = cand
            if fp is None:
                # S6：按 mtime 取最新版本覆盖（文件名时间戳同秒时不会覆盖错版本）
                latest = None
                for can in d.glob(f"{prefix}_*.json"):
                    try:
                        if (
                            latest is None
                            or can.stat().st_mtime_ns > latest.stat().st_mtime_ns
                        ):
                            latest = can
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
        # 预览图后台写 sidecar（不阻塞保存；预渲染线程跑完后也会补全）
        _schedule_images_flush(state)
        return True
    except Exception as e:
        print(f"[correctmanage] 历史缓存写入失败: {e}")
        return False


def _prune_history(prefix: str) -> None:
    """每个文件只保留最近 _HISTORY_KEEP 个版本，删掉更旧的。"""
    try:
        for fp in sorted(_history_dir().glob(f"{prefix}_*.json"), reverse=True)[
            _HISTORY_KEEP:
        ]:
            fp.unlink(missing_ok=True)
    except Exception:
        pass


def _delete_history(ids: list[str], all_: bool = False) -> int:
    """删除历史缓存条目；all_=True 删除全部，否则按 id（文件名 stem）删除。

    all_ 分支的 "*.json" glob 同时命中共享 sidecar（<prefix>.images.json）；
    按 ids 删除时顺带清理不再有版本文件的孤儿 sidecar（避免 100MB 幽灵文件）。
    """
    d = _history_dir()
    if not d.is_dir():
        return 0
    deleted = 0
    try:
        for fp in d.glob("*.json"):
            if all_ or fp.stem in ids:
                fp.unlink(missing_ok=True)
                deleted += 1
        if not all_ and ids:
            # 按 ids 删除：清理已无任何版本文件的孤儿 sidecar（.images.json）
            prefixes = {_version_prefix(x) for x in ids}
            for prefix in prefixes:
                sidecar = _images_cache_path(prefix)
                if sidecar.is_file() and not list(d.glob(f"{prefix}_*.json")):
                    sidecar.unlink(missing_ok=True)
    except Exception:
        pass
    return deleted


def rename_history(vid: str, new_name: str) -> dict[str, Any]:
    """重命名历史记录条目：把 display_name 写入同一 PDF 组的所有版本文件。

    返回 {"ok": True, "display_name": <该组首条显示名>, "prefix": <组前缀>}。
    校验失败抛 ValueError（中文消息）；无法定位所属 PDF 组抛 LookupError。
    矫正界面（correctmanage）与配置中心（guimanage）共用本函数，行为一致。
    """
    vid = str(vid or "").strip()
    new_name = str(new_name or "").strip()
    if not vid:
        raise ValueError("缺少版本 ID")
    if not new_name:
        raise ValueError("新名称不能为空")
    # 拒绝路径分隔符，避免重命名被当成路径操作
    if "\\" in new_name or "/" in new_name or ":" in new_name:
        raise ValueError("名称含非法字符（含 \\ / :）")
    if len(new_name) > 100:
        raise ValueError("名称过长（最多 100 字符）")
    prefix = _version_prefix(vid)
    if not prefix:
        raise LookupError("无法定位所属 PDF 组")
    d = _history_dir()
    # 遍历该 PDF 组的所有版本文件（前缀匹配），仅更新/写入 display_name
    for fp in d.glob(f"{prefix}_*.json"):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            data["display_name"] = new_name
            import tempfile
            import os as _os

            fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".rename-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                _os.replace(tmp, str(fp))
            except Exception:
                try:
                    _os.unlink(tmp)
                except Exception:
                    pass
        except Exception:
            continue
    first_items = _history_entries(prefix)
    first_display = (
        first_items[0].get("display_name", first_items[0].get("name", ""))
        if first_items
        else new_name
    )
    return {"ok": True, "display_name": first_display, "prefix": prefix}


def export_history_backup(
    ids: list[str] | None, save_path: str | None = None
) -> dict[str, Any]:
    """把选中的历史版本打包为 ZIP 并写入磁盘，返回 {ok, path, count}。

    为什么不用浏览器下载（2026-09-16）：pywebview WebView2 默认
    ALLOW_DOWNLOADS=False 会取消 blob/附件下载。主界面「矫正历史」页原先用
    <a download href="/api/history/export?id=..."> 触发下载，文件被静默取消，
    界面却照样提示"已开始导出"——即用户报的"提示已导出、实际没有文件"。
    这里统一改成服务端写盘并回传路径，前端提示保存位置（与矫正界面历史弹窗
    「导出」同一实现，两处共用本函数）。

    save_path：用户「另存为」指定路径（来自主线程保存对话框）。为空/None 时
    回退默认位置 data/data/ptoe_history_<时间戳>.zip。

    失败语义：未选择版本 → ValueError；选中项都不存在 → LookupError；
    save_path 不可写 → OSError（上游转 500）。
    """
    wanted = [str(i) for i in (ids or []) if str(i or "")]
    if not wanted:
        raise ValueError("未选择要导出的历史版本")
    import io
    import time as _time
    import zipfile

    from pdfmanage import app_base_dir

    hist_dir = Path(str(_history_dir()))
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for pid in wanted:
            fp = hist_dir / f"{pid}.json"
            if not fp.is_file():
                continue
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {"pages": {}}
                images = data.get("images") or _load_images_cache(
                    _version_prefix(pid)
                )
                data["images"] = images or {}
                zf.writestr(
                    f"{pid}.json", json.dumps(data, ensure_ascii=False, indent=2)
                )
                count += 1
            except Exception:  # noqa: BLE001
                continue
    if count == 0:
        raise LookupError("没有可导出的历史版本")
    if save_path:
        # 「另存为」：用户从保存对话框指定的路径
        out_path = Path(str(save_path))
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        data_dir = app_base_dir() / "data" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        stamp = _time.strftime("%Y%m%d%H%M%S")
        out_path = data_dir / f"ptoe_history_{stamp}.zip"
    out_path.write_bytes(buf.getvalue())
    return {"ok": True, "path": str(out_path), "count": count}


def _import_history(
    content: dict[str, Any], filename: str = ""
) -> tuple[bool, str, str]:
    """把导出的历史版本 JSON 导入本地 correction_history/（跨平台矫正）。

    版本文件不携带 images（与 _write_history_version 一致，避免大书版本文件
    膨胀），预览图写回该 book 共享 sidecar <prefix>.images.json，保证另一台
    电脑上 PDF 路径不可用（甚至无 PDF）时也能靠内嵌预览图继续矫正。

    返回 (ok, message, version_stem)；ok=False 时 message 为错误原因。
    """
    if not isinstance(content, dict) or not (content.get("pages") or {}):
        return False, "导入内容缺少 pages 字段", ""
    stem = Path(str(filename or "")).stem
    # 优先沿用导出文件名自带的前缀（<prefix>_<ts>_<rand> 或 <prefix>.json），
    # 使版本文件与 sidecar 前缀一致；否则按 pdf 路径哈希，再否则随机。
    prefix = _version_prefix(stem) if stem else ""
    if not prefix:
        pdf = content.get("pdf")
        # 收窄为 str：pdf 有值时 _history_prefix 可能返回 None，or 兜底随机前缀
        prefix = (
            _history_prefix(str(pdf)) if pdf else None
        ) or f"import_{uuid4().hex[:8]}"
    try:
        images = content.get("images") or {}
        payload = {k: v for k, v in content.items() if k != "images"}
        payload.setdefault(
            "name", Path(str(content.get("pdf") or "")).name or "手动录入"
        )
        payload.setdefault("updated", time.strftime("%Y-%m-%d %H:%M:%S"))
        payload.setdefault("proofread", {"errors": {}, "original": {}, "dismissed": {}})
        payload.setdefault("last_proofread_page", None)
        d = _history_dir()
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d%H%M%S")
        version_stem = f"{prefix}_{stamp}_{uuid4().hex[:4]}"
        (d / f"{version_stem}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if images:
            _write_images_cache(prefix, {str(k): str(v) for k, v in images.items()})
        _prune_history(prefix)  # 导入后同样裁剪，避免跨平台导入无限累积
        _HISTORY_INDEX["sig"] = None  # 使历史列表签名失效，下次 _history_entries 重读
        return True, "", version_stem
    except Exception as e:
        return False, str(e), ""


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

# DOCX 最小打包：Content_Types + _rels + word/document.xml + word/_rels/document.xml.rels
# + word/media/*（stdlib zipfile，不引入 python-docx；标题用直接格式加粗加大 +
# outlineLvl 生成导航大纲；图片以 data URI 内嵌为真实字节）
_DOCX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="png" ContentType="image/png"/>'
    '<Default Extension="jpeg" ContentType="image/jpeg"/>'
    '<Default Extension="jpg" ContentType="image/jpeg"/>'
    '<Default Extension="gif" ContentType="image/gif"/>'
    '<Default Extension="webp" ContentType="image/webp"/>'
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
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
    "<w:body>"
)
_DOCX_DOCUMENT_TAIL = (
    '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
    '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
    "</w:sectPr></w:body></w:document>"
)
# word/_rels/document.xml.rels：图片关系（rIdImgN）写入其间
_DOCX_DOCUMENT_RELS_HEAD = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
)
_DOCX_DOCUMENT_RELS_TAIL = "</Relationships>"
# 标题级别 → 字号（半磅）；h4-h6 与正文拉开即可
_DOCX_HEADING_SZ = {1: 36, 2: 32, 3: 28, 4: 24, 5: 24, 6: 24}
# 图片基准宽度（5 英寸 = 4572000 EMU）；ptoe-img-w25/50/75/100 按比例缩放
_DOCX_IMG_BASE_CX = 4572000
# 尺寸 class → 宽度比例
_DOCX_IMG_FRAC = {
    "ptoe-img-w25": 0.25,
    "ptoe-img-w50": 0.5,
    "ptoe-img-w75": 0.75,
    "ptoe-img-w100": 1.0,
}
# 分隔线样式 → OOXML 段落边框线型（2026-09-27）：用真正的边框而非字形文本，
# 保证 Word 里线宽/线型可随主题缩放，也不受阅读字体盒绘字符缺失影响
_DOCX_DIVIDER_BORDER = {
    "solid": "single",
    "dashed": "dashed",
    "dotted": "dotted",
    "double": "double",
}
# 字符样式字号 → w:sz 上下界（半磅）。与 CSS 端 8..72px 换算后约为
# 12..108，取 2..3276（OOXML 规范硬界）以防 0 字号被 Word 吞掉
_DOCX_MIN_SZ = 2
_DOCX_MAX_SZ = 3276
# CT_RPr（wordprocessingml 字符属性）的子元素固定次序，摘录本文件用到的部分：
# rFonts < b < i < strike < color < sz(+szCs) < u < bdr < shd < vertAlign。
# 序错会让 Word 判定文档损坏或静默丢弃格式（2026-09-27 取证修复）。
_DOCX_RPR_ORDER = (
    "rFonts",
    "b",
    "i",
    "strike",
    "color",
    "sz",
    "u",
    "bdr",
    "shd",
    "vertAlign",
    "em",
)

# ---- 题注（ptoe-caption）版式契约（2026-10 非 EPUB 导出落地）--------------------
# 权威样式 = 编辑器 CSS（.editable p.ptoe-caption）与 htmlmanage EPUB CSS 的同源值：
#     font-size:0.85em; color:#666; text-align:center; margin:0.2em 0 0.6em
# EPUB 侧由 htmlmanage 的 style.css 承载（无此文件即可），本组常量供 TXT/DOCX/MD
# 三条非 EPUB 链路各自换算：字号比例 / 颜色 / 对齐三值只定义一次，避免散落各处。
_CAPTION_FONT_SCALE = 0.85
# 颜色：CSS 侧沿用契约字面量 #666；OOXML w:color 是无 # 的 6 位十六进制枚举，
# 故另存一份 666666（同为 rgb(102,102,102)，不改变实际观感）
_CAPTION_COLOR_CSS = "#666"
_CAPTION_COLOR_HEX = "666666"
_CAPTION_JC = "center"
# Markdown 内联样式串：GFM 没有字号/颜色/居中语法，但 HTML 块允许 style 属性，
# 于是「保留 class（mdToHtml 可无损还原题注）+ 补内联样式（无样式表的渲染器也能
# 正确显示）」双保险。margin 一并内联以还原题注与图之间的紧凑间距。
_CAPTION_MD_STYLE = (
    f"font-size:{_CAPTION_FONT_SCALE}em;color:{_CAPTION_COLOR_CSS};"
    f"text-align:{_CAPTION_JC};margin:0.2em 0 0.6em"
)
# DOCX 正文基准字号（半磅）：五号 10.5pt = 21。题注按 0.85em 换算 →
# 21 × 0.85 = 17.85 半磅。OOXML 字号只接受整数半磅，必须四舍五入（截断会偏小
# 0.4pt），结果 18 半磅 = 9pt。
_DOCX_BODY_SZ = 21
_DOCX_CAPTION_SZ = int(round(_DOCX_BODY_SZ * _CAPTION_FONT_SCALE))


def _html_to_export_blocks(html: str) -> list[tuple]:
    """已清洗 HTML → 块列表，供 TXT/DOCX 导出。

    块形状：
    - 文本块：('p'|'h1'..'h6', 文本) 二元组；
    - 图片块：('img', src, alt, cls) 四元组（src 可为 data URI，cls 为 class 字符串）。

    剥掉全部标签（含标记 span），图片在块上下文内成为独立图片块
    （周围文本自动拆成独立文本块），<br> 转为段内换行（文本中保留 \\n），
    HTML 实体还原。
    """

    class _Parser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.blocks: list[tuple] = []
            self.cur_kind: str = "p"
            self.cur: list[str] = []
            self.block_seen = False  # 是否已进入过 <p>/<h> 块（孤立 img 不产生块）
            self.skip = 0  # >0 表示处于 script/style 等跳过区域

        def _img_block(self, attrs: list[tuple[str, str | None]]) -> None:
            # 图片在块上下文内成为独立图片块：先冲刷当前文本块（非空时），
            # 再追加 ('img', src, alt, cls) 四元组——周围文本自动拆成独立块
            attrs_d = dict(attrs)
            src = attrs_d.get("src") or ""
            alt = attrs_d.get("alt") or "插图"
            cls = attrs_d.get("class") or ""
            self._flush()
            self.blocks.append(("img", src, alt, cls))

        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            if tag in _SKIP_TAGS:
                self.skip += 1
                return
            if tag == "p" or (len(tag) == 2 and tag[0] == "h" and tag[1] in "123456"):
                self._flush()
                self.cur_kind = "p" if tag == "p" else tag
                self.block_seen = True
                return
            if tag == "br":
                self.cur.append("\n")
            if tag == "img" and self.block_seen:
                self._img_block(attrs)

        def handle_startendtag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            if tag == "br":
                self.cur.append("\n")
            elif tag == "img" and self.block_seen:
                # 自闭合 <img/> 由 HTMLParser 走 handle_startendtag
                self._img_block(attrs)

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


# 富文本块的行内标签集合（加粗/斜体/通用 span）
_RICH_INLINE_TAGS = ("span", "strong", "b", "em", "i")
# 新增 8 种行内格式类（2026-09）：下划线/下加点/删除线/字符边框/底纹/突显/上标/下标
# 单一事实来源 = rulemanage.INLINE_FORMAT_CLASSES（2026-09-19）；保留既有成员经并集合入
_RICH_INLINE_FORMAT_CLASSES = set(
    getattr(rulemanage, "INLINE_FORMAT_CLASSES", ()) or ()
)


def _rich_parse_indent(attrs_d: dict[str, str]) -> dict[str, Any]:
    """块属性 → 段落设置字典（与 htmlmanage._indent_style_attrs 同一语义）。

    data-pl/data-pr=左/右缩进(em)、data-ind=first|hang、data-indv=缩进量(em)、
    data-spb/data-spa=段前/段后(行)、data-lh=行距倍数；缺失键一律为 None
    （data-ind 非法值归一为 ""）。
    """

    def _num(key: str) -> float | None:
        v = (attrs_d.get(key) or "").strip()
        try:
            return float(v)
        except ValueError:
            return None

    mode = (attrs_d.get("data-ind") or "").strip()
    return {
        "pl": _num("data-pl"),
        "pr": _num("data-pr"),
        "ind": mode if mode in ("first", "hang") else "",
        "indv": _num("data-indv"),
        "spb": _num("data-spb"),
        "spa": _num("data-spa"),
        "lh": _num("data-lh"),
    }


def _rich_align(attrs_str: str) -> str:
    """从块属性串提取对齐类 → ""|"center"|"right"|"justify"。"""
    m = re.search(r'class="([^"]*)"', attrs_str or "")
    toks = (m.group(1) if m else "").split()
    for a in ("center", "right", "justify"):
        if f"ptoe-align-{a}" in toks:
            return a
    return ""


def _cell_align(attrs: list[tuple[str, str | None]]) -> str:
    """从 td/th 标签 attrs 提取对齐类 → ""|"left"|"center"|"right"。

    契约：矫正界面只产出 ptoe-align-left/center/right 三种值，
    其余 class（含非法 ptoe-align-*）一律忽略（与 _Sanitizer 同策略）。
    """
    for k, v in attrs:
        if k == "class":
            for c in (v or "").split():
                if c in _ALIGN_CLASSES:
                    return c[len("ptoe-align-"):]
            break
    return ""


# 垂直对齐类 → 规范值（""|"top"|"middle"|"bottom"）。与前端 _tblVAlign 的
# {top:ptoe-vtop, middle:ptoe-vmid, bottom:ptoe-vbot} 一一对应。
_CELL_VALIGN_VALUES = {"ptoe-vtop": "top", "ptoe-vmid": "middle", "ptoe-vbot": "bottom"}
# 规范垂直对齐 → OOXML w:vAlign 取值（middle 在 OOXML 里叫 center）。
_DOCX_VALIGN = {"top": "top", "middle": "center", "bottom": "bottom"}


def _cell_valign(attrs: list[tuple[str, str | None]]) -> str:
    """从 td/th 标签 attrs 提取垂直对齐类 → ""|"top"|"middle"|"bottom"。

    与 _cell_align 同策略：只认矫正界面产出的 ptoe-v* 三种值，其余忽略。
    """
    for k, v in attrs:
        if k == "class":
            for c in (v or "").split():
                if c in _CELL_VALIGN_VALUES:
                    return _CELL_VALIGN_VALUES[c]
            break
    return ""


def _html_to_rich_blocks(html: str) -> list[dict]:
    """已清洗 HTML → 富文本块列表，供 TXT/DOCX/MD 导出（保留格式信息）。

    与 _html_to_export_blocks 的差异：
    - 行内加粗/斜体保留为 runs（[{text,bold,italic}]），不再拍平成纯文本；
    - 对齐类（ptoe-align-*）、注释类（ptoe-note）、段落设置 data-* 属性
      （data-pl/pr/ind/indv/spb/spa/lh）逐块捕获；
    - 保留块原始标签名/属性串/内嵌 HTML（供 Markdown 导出原样透传，
      与前端 htmlToMd「带 class=/data- 属性的块保留 raw HTML」规则一致）；
    - <div> 视为段落边界（与 EPUB 路径 sanitize 归一 <p> 的行为一致）。

    块形状：
    - 文本块：{"kind": "p"|"h1".."h6", "tag": 原始标签名, "text": 纯文本,
      "runs": [...], "align": ..., "note": bool, "caption": bool（题注块，
      2026-10：非 EPUB 导出据此还原「小号灰字居中」，块上仍保留 class 供 raw
      透传/往返）, "attrs": 属性串, "inner": 内嵌 HTML,
      "indent": _rich_parse_indent 形状}；
      每个 run 额外带 "style"（规范化字符样式串，rulemanage.normalize_char_style
      产物，无格式时为 ""）——供 DOCX 导出 w:sz/w:rFonts/w:color 使用；
    - 图片块：{"kind": "img", "src", "alt", "cls"}；**独占整块**时即此四键形状
      （历史契约，勿改）。图片与前后文本同属一个源段落时（段内行内图，
      2026-10 图文混合）追加第 5 键 "meta": {"inline": True, "part": <源段落序号>}，
      相邻的两个纯文本块同时带 "part"，供 Markdown 导出把「文字 + 图 + 文字」
      合并回同一段（GFM 支持段内 ![alt](src)，是唯一能表达图文同段的格式）；
      TXT/DOCX 忽略 meta，维持「文字 / 图 / 文字」三段的既有降级行为不变。
      注：带 class/data- 属性或行内格式类的段落整块 raw HTML 透传（既有规则），
      其段内图不参与合并，同样不丢内容。
    - 分隔线块：{"kind": "divider", "style": solid|dashed|dotted|double,
      "glyph": 8 字形, "text": 同一字形, "runs": [单条], ...}（2026-09-27）。
    标记 span（ptoe-marker）整体剥除（文本与 inner 均不含）。
    """

    class _Parser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.blocks: list[dict] = []
            self.kind = "p"  # 当前块语义类型（div 归一为 p）
            self.tag = "p"  # 当前块原始标签名
            self.attrs_str = ""  # 当前块开标签属性串
            self.runs: list[dict] = []
            self.inner: list[str] = []
            self.note = False
            self.caption = False  # 题注块（ptoe-caption），2026-10 非 EPUB 导出版式
            self.align_lines = False  # 多行对齐段（ptoe-align-lines），2026-10-20 段尾 &nbsp; 填充不剥
            self.indent = _rich_parse_indent({})
            self.divider = ""  # 分隔线样式名（solid/dashed/dotted/double），非分隔线为 ""
            self.block_seen = False  # 是否已进入过块（孤立 img 不产生块）
            self.skip = 0  # >0 表示处于 script/style 等跳过区域
            # 栈元素：(tag, is_marker, fmt_dict) — fmt_dict 记录该 span 携带的行内格式类
            self.stack: list[tuple[str, bool, dict]] = []
            self._join_at_start = False
            self._join_last = False
            self._has_non_ws_text = False
            # MathML 公式（2026-10-21）：DOCX/MD/TXT 无 MathML 概念 → 用
            # <math alttext="…"> 里的 LaTeX 源码做纯文本兜底（$…$）。
            # math_depth>0 表示处于公式内部（内部标签/文本一律不参与富文本解析，
            # 否则 <mi>x</mi> 的 "x" 会变成正文里一个孤零零的字母）。
            self.math_depth = 0
            # 表格块累积（2026-09-20）：<table> 整表产出一个 "table" 块
            self.tbl_depth = 0  # 表格嵌套深度（>0 = 处于表格内）
            self.tbl_skip = 0  # 嵌套表格丢弃深度
            self.tbl_has_thead = False
            self.tbl_in_thead = False
            self.tbl_head: list[list[str]] = []  # thead 行（每行 = cell inner html 列表）
            self.tbl_body: list[list[str]] = []  # tbody 行
            self.tbl_row: list[str] | None = None
            self.tbl_row_align: list[str] | None = None  # 当前行各 cell 对齐（algn 并行行）
            self.tbl_cell_open = False
            self.tbl_cell: list[str] = []
            self.tbl_cell_align = ""  # 当前单元格对齐（""|"left"|"center"|"right"）
            # 垂直对齐（2026-10）：「对齐 ▾」九宫格产出的 ptoe-v* 类，
            # 与 algn 同维并行（valgn），供 DOCX 输出 w:vAlign（EPUB 走原 HTML+CSS）。
            self.tbl_row_valign: list[str] | None = None
            self.tbl_cell_valign = ""  # 当前单元格垂直对齐（""|"top"|"middle"|"bottom"）
            self.tbl_valign_head: list[list[str]] = []  # thead 行垂直对齐（并行 tbl_head）
            self.tbl_valign_body: list[list[str]] = []  # tbody 行垂直对齐（并行 tbl_body）
            self.tbl_cell_stack: list[str] = []  # 单元格内未闭合标签（"__marker__" = 内容剥除）
            self.tbl_raw: list[str] = []  # 表格原始 HTML（防御：含 colspan/rowspan 时 MD raw 透传）
            self.tbl_has_span = False  # 是否出现过 colspan/rowspan
            # 无边框表格（2026-10）：<table class="ptoe-noborder"> → DOCX 输出无边框
            self.tbl_noborder = False
            self.tbl_align_head: list[list[str]] = []  # thead 行对齐（并行 tbl_head）
            self.tbl_align_body: list[list[str]] = []  # tbody 行对齐（并行 tbl_body）
            # 图文混合（2026-10）：源段落序号（同一 <p> 内被图片切开的多块共用
            # 同一序号），段内行内图与相邻文本块据此在 Markdown 导出里合并为一段。
            self._part_seq = 0  # 已开块计数（1 起）
            self._part = 0  # 当前源段落序号
            self._pend_imgs: list[dict] = []  # 本段落内已产出、待确认是否段内的图片块

        def _tbl_cell_end(self) -> None:
            """结束当前单元格：cell inner html 附加到当前行（空单元格保留）。"""
            if not self.tbl_cell_open:
                return
            if self.tbl_row is None:
                self.tbl_row = []
            if self.tbl_row_align is None:
                self.tbl_row_align = []
            if self.tbl_row_valign is None:
                self.tbl_row_valign = []
            self.tbl_row.append("".join(self.tbl_cell))
            self.tbl_row_align.append(self.tbl_cell_align)
            self.tbl_row_valign.append(self.tbl_cell_valign)
            self.tbl_cell_open = False
            self.tbl_cell = []
            self.tbl_cell_align = ""
            self.tbl_cell_valign = ""

        def _tbl_row_end(self) -> None:
            self._tbl_cell_end()
            if self.tbl_row is not None:
                (self.tbl_head if self.tbl_in_thead else self.tbl_body).append(
                    self.tbl_row
                )
                (self.tbl_align_head if self.tbl_in_thead else self.tbl_align_body).append(
                    self.tbl_row_align or []
                )
                (self.tbl_valign_head if self.tbl_in_thead else self.tbl_valign_body).append(
                    self.tbl_row_valign or []
                )
                self.tbl_row = None
                self.tbl_row_align = None
                self.tbl_row_valign = None

        def _tbl_table_end(self) -> None:
            """表格结束：产出 {"kind":"table","header","rows","algn"} 块（可选 raw/has_span/noborder）。"""
            if not self.tbl_depth:
                return
            self._tbl_row_end()
            if self.tbl_head or self.tbl_body:
                block: dict = {
                    "kind": "table",
                    "header": bool(self.tbl_head),
                    "rows": self.tbl_head + self.tbl_body,
                    "algn": self.tbl_align_head + self.tbl_align_body,
                    # raw 恒附带：单元格对齐同列不一致时 MD 导出也要整块透传
                    "raw": "".join(self.tbl_raw),
                }
                # valgn 仅在确实有垂直对齐时附带，保持旧块形状（无该键）不变
                valgn = self.tbl_valign_head + self.tbl_valign_body
                if any(any(v for v in row) for row in valgn):
                    block["valgn"] = valgn
                if self.tbl_has_span:
                    block["has_span"] = True
                if self.tbl_noborder:
                    block["noborder"] = True
                self.blocks.append(block)
            self.tbl_depth = 0
            self.tbl_skip = 0
            self.tbl_has_thead = False
            self.tbl_in_thead = False
            self.tbl_head = []
            self.tbl_body = []
            self.tbl_align_head = []
            self.tbl_align_body = []
            self.tbl_valign_head = []
            self.tbl_valign_body = []
            self.tbl_row = None
            self.tbl_row_align = None
            self.tbl_row_valign = None
            self.tbl_raw = []
            self.tbl_has_span = False
            self.tbl_noborder = False

        def _flags(self) -> dict:
            """返回当前栈状态对应的行内格式标志字典。"""
            bold = False
            italic = False
            underline = False
            underdot = False
            strike = False
            charbox = False
            shade = False
            highlight = False
            sup = False
            sub = False
            # 2026-10-21 新增行内格式标志
            wavy = False
            double = False
            overline = False
            emphasis = False
            highlight_color = ""
            ruby = ""
            # 字符样式（字号/字体/颜色的 style 载体，2026-09-27）：栈自外向内，
            # 由内向外取首个非空（最内层 span 的声明更具体，优先）
            char_style = ""
            for _, is_marker, fmt in reversed(self.stack):
                if is_marker:
                    continue
                s = fmt.get("style") or ""
                if s:
                    char_style = s
                    break
            for _, is_marker, fmt in self.stack:
                if is_marker:
                    continue
                if fmt.get("bold"):
                    bold = True
                if fmt.get("italic"):
                    italic = True
                if fmt.get("underline"):
                    underline = True
                if fmt.get("underdot"):
                    underdot = True
                if fmt.get("strike"):
                    strike = True
                if fmt.get("charbox"):
                    charbox = True
                if fmt.get("shade"):
                    shade = True
                if fmt.get("highlight"):
                    highlight = True
                if fmt.get("sup"):
                    sup = True
                if fmt.get("sub"):
                    sub = True
                if fmt.get("wavy"):
                    wavy = True
                if fmt.get("double"):
                    double = True
                if fmt.get("overline"):
                    overline = True
                if fmt.get("emphasis"):
                    emphasis = True
            # 多色突显与注音内容：取最内层（与字符样式同口径）
            for _, is_marker, fmt in reversed(self.stack):
                if is_marker:
                    continue
                if not highlight_color:
                    highlight_color = fmt.get("highlight_color") or ""
                if not ruby:
                    ruby = fmt.get("ruby") or ""
                if highlight_color and ruby:
                    break
            return {
                "bold": bold,
                "italic": italic,
                "underline": underline,
                "underdot": underdot,
                "strike": strike,
                "charbox": charbox,
                "shade": shade,
                "highlight": highlight,
                "sup": sup,
                "sub": sub,
                # 2026-10-21 新增行内格式
                "wavy": wavy,
                "double": double,
                "overline": overline,
                "emphasis": emphasis,
                "highlight_color": highlight_color,
                "ruby": ruby,
                # 规范化字符样式串（"" = 无）；DOCX 导出据此写 w:sz/w:rFonts/w:color
                "style": char_style,
            }

        def _push_text(self, txt: str) -> None:
            if not txt:
                return
            flags = self._flags()
            if self.runs:
                last = self.runs[-1]
                # 比较所有格式标志
                if all(last.get(k) == flags[k] for k in flags):
                    last["text"] += txt
                    return
            # 新 run：包含所有格式标志
            run = {"text": txt}
            run.update(flags)
            self.runs.append(run)

        def _open_block(self, tag: str, attrs) -> None:
            self._flush()
            self._part_seq += 1
            self._part = self._part_seq
            d: dict[str, str] = {}
            parts: list[str] = []
            for k, v in attrs:
                parts.append(f'{k}="{v}"' if v is not None else k)
                d.setdefault(k, v or "")
            self.kind = "p" if tag == "div" else tag
            self.tag = tag
            self.attrs_str = " ".join(parts)
            self.runs = []
            self.inner = []
            self.note = "ptoe-note" in (d.get("class") or "").split()
            # 题注块（2026-10）：登记到块模型，供 DOCX（0.85 倍字号 + 灰 + 居中）与
            # Markdown（内联样式）两条非 EPUB 链路还原版式
            self.caption = _CAPTION_CLASS in (d.get("class") or "").split()
            # 多行对齐段（2026-10-20）：段尾的 &nbsp; 填充是版式的一部分，导出时不能剥
            self.align_lines = _ALIGN_LINES_CLASS in (d.get("class") or "").split()
            self.indent = _rich_parse_indent(d)
            # 分隔线段落（2026-09-27）：class 前缀 ptoe-divider + 样式后缀
            self.divider = _divider_style_from_attrs(d)
            self.block_seen = True
            self._join_at_start = False
            self._join_last = False
            self._has_non_ws_text = False

        def _flush(self) -> None:
            runs = self.runs
            self.runs = []
            inner = "".join(self.inner)
            self.inner = []
            # 去掉块首尾空白（与旧导出的整块 .strip() 等价）
            # 多行对齐段（2026-10-20）：段尾的 &nbsp; 填充是版式的一部分，默认口径会
            # 连 &nbsp; 一起剥掉——导出后左右两侧就对不齐了
            if self.align_lines:
                while runs and blank_block_ws(runs[0]["text"], True):
                    runs.pop(0)
                while runs and blank_block_ws(runs[-1]["text"], True):
                    runs.pop()
                if runs:
                    runs[0]["text"] = strip_block_ws(runs[0]["text"], True)
                    runs[-1]["text"] = strip_block_ws(runs[-1]["text"], True)
            else:
                while runs and not runs[0]["text"].strip():
                    runs.pop(0)
                while runs and not runs[-1]["text"].strip():
                    runs.pop()
                if runs:
                    runs[0]["text"] = runs[0]["text"].lstrip()
                    runs[-1]["text"] = runs[-1]["text"].rstrip()
            text = "".join(r["text"] for r in runs)
            if text:
                if self.divider:
                    # 分隔线：整块是版式元素而非文字内容，导出为专用 kind。
                    # 字形统一取自 DIVIDER_GLYPHS（不信任块内文本），保证 DOCX 边框
                    # 段落 / MD 横线 / TXT 字形行三者一致；runs 只放字形，
                    # TXT 导出无需改动即可原样输出。
                    glyph = _divider_glyph(self.divider)
                    self.blocks.append(
                        {
                            "kind": "divider",
                            "style": self.divider,
                            "glyph": glyph,
                            "text": glyph,
                            "runs": [{"text": glyph, "bold": False, "italic": False}],
                            "tag": self.tag,
                            "attrs": self.attrs_str,
                            "inner": inner,
                            "align": "center",
                            "note": False,
                            "indent": {},
                        }
                    )
                else:
                    block = {
                        "kind": self.kind,
                        "tag": self.tag,
                        "text": text,
                        "runs": runs,
                        "align": _rich_align(self.attrs_str),
                        "note": self.note,
                        "caption": self.caption,
                        "attrs": self.attrs_str,
                        "inner": inner,
                        "indent": self.indent,
                    }
                    if self._join_at_start or self._join_last:
                        block["join_prev"] = bool(self._join_at_start)
                        block["join_next"] = bool(self._join_last)
                    if self._pend_imgs and self.kind == "p" and not self.divider:
                        # 图文同段的「后」半边：与段内图片共用同一源段落序号
                        block["part"] = self._part
                    self.blocks.append(block)
            # 图文混合（2026-10）：本块有文本 → 此前同一源段落内产出的图片块与
            # 本块构成「图文同段」，补 meta（Markdown 导出据此合并回一段）。
            # 图片独占整块（text 为空）时不补，四键形状保持不变；分隔线块是版式
            # 元素而非文字内容，也不参与合并。
            if text and self._pend_imgs and self.kind == "p" and not self.divider:
                for ib in self._pend_imgs:
                    ib.setdefault("meta", {"inline": True, "part": self._part})
            self._pend_imgs = []
            self.kind, self.tag, self.attrs_str = "p", "p", ""
            self.note = False
            self.caption = False
            self.divider = ""
            self.indent = _rich_parse_indent({})

        def _emit_img(self, attrs) -> None:
            d = dict(attrs)
            # 图文同段判定（2026-10）：图片之前同块内已有非空白文本 → 与前文同段；
            # 仅普通文字段落参与（分隔线块是版式元素，div 块走 raw HTML 透传）。
            had_text = (
                self.kind == "p"
                and not self.divider
                and bool("".join(r["text"] for r in self.runs).strip())
            )
            self._flush()
            blk = {
                "kind": "img",
                "src": d.get("src") or "",
                "alt": d.get("alt") or "插图",
                "cls": d.get("class") or "",
            }
            self.blocks.append(blk)
            if had_text:
                blk["meta"] = {"inline": True, "part": self._part}
                prev = self.blocks[-2]
                if prev.get("kind") == "p":
                    # 图文同段的「前」半边（Markdown 合并用；分隔线/表格等不动）
                    prev["part"] = self._part
            self._pend_imgs.append(blk)

        def handle_starttag(self, tag, attrs) -> None:
            if tag in _SKIP_TAGS:
                self.skip += 1
                return
            if tag == "table":
                if self.tbl_depth:
                    self.tbl_skip += 1
                    self.tbl_raw.append(self.get_starttag_text() or "<table>")
                    return
                self._flush()
                self.tbl_depth = 1
                self.tbl_has_thead = False
                self.tbl_in_thead = False
                self.tbl_head = []
                self.tbl_body = []
                self.tbl_align_head = []
                self.tbl_align_body = []
                self.tbl_valign_head = []
                self.tbl_valign_body = []
                self.tbl_row = None
                self.tbl_row_align = None
                self.tbl_row_valign = None
                self.tbl_cell_open = False
                self.tbl_cell = []
                self.tbl_cell_align = ""
                self.tbl_cell_valign = ""
                self.tbl_cell_stack = []
                self.tbl_raw = [self.get_starttag_text() or "<table>"]
                self.tbl_has_span = False
                # 无边框表格（2026-10）：class 白名单只认 ptoe-noborder（与 _Sanitizer 同口径）
                self.tbl_noborder = "ptoe-noborder" in str(dict(attrs).get("class") or "").split()
                return
            if self.tbl_skip:
                if tag == "table":
                    self.tbl_skip += 1
                self.tbl_raw.append(self.get_starttag_text() or f"<{tag}>")
                return
            if self.tbl_depth:
                self.tbl_raw.append(self.get_starttag_text() or f"<{tag}>")
                if self.math_depth > 0:
                    return  # 公式内部标签不进单元格文本（否则漏出 <semantics> 等字面量）
                if tag in ("thead", "tfoot"):
                    self.tbl_has_thead = self.tbl_has_thead or tag == "thead"
                    self.tbl_in_thead = tag == "thead"
                    self._tbl_cell_end()
                    return
                if tag == "tbody":
                    self.tbl_in_thead = False
                    self._tbl_cell_end()
                    return
                if tag == "tr":
                    self._tbl_row_end()
                    self.tbl_row = []
                    self.tbl_row_align = []
                    self.tbl_row_valign = []
                    return
                if tag in ("th", "td"):
                    self._tbl_cell_end()
                    d = dict(attrs)
                    for k in ("colspan", "rowspan"):
                        v = (d.get(k) or "").strip()
                        if re.fullmatch(r"\d{1,2}", v):
                            self.tbl_has_span = True
                    self.tbl_cell_open = True
                    self.tbl_cell = []
                    self.tbl_cell_align = _cell_align(attrs)
                    self.tbl_cell_valign = _cell_valign(attrs)
                    self.tbl_cell_stack = []
                    return
                if tag == "br":
                    self.tbl_cell.append("<br>")
                    return
                if tag == "math":
                    # MathML 公式（2026-10-21）：单元格内公式同样退化为 $LaTeX$
                    # （公式内部结构不进单元格文本，否则会漏出一堆散字）
                    tex = str(dict(attrs).get("alttext") or "").strip()
                    if tex:
                        self.tbl_cell.append("$" + tex + "$")
                    self.math_depth += 1
                    return
                if tag == "img":
                    return  # 单元格内图片不导出（与 block 分支 img 语义一致即可，防御）
                if tag in _RICH_INLINE_TAGS:
                    attrs_d = dict(attrs)
                    cls = (attrs_d.get("class") or "").split()
                    if "ptoe-marker" in cls:
                        self.tbl_cell_stack.append("__marker__")
                        return
                    self.tbl_cell.append(self.get_starttag_text() or f"<{tag}>")
                    self.tbl_cell_stack.append(tag)
                    return
                # 其它标签（含 p/div/h 等防御性）：raw 透传进 cell
                self.tbl_cell.append(self.get_starttag_text() or f"<{tag}>")
                self.tbl_cell_stack.append(tag)
                return
            if tag in ("p", "div") or (
                len(tag) == 2 and tag[0] == "h" and tag[1] in "123456"
            ):
                self._open_block(tag, attrs)
                return
            if self.skip:
                return
            if tag == "math":
                # MathML 公式（2026-10-21）：DOCX/MD/TXT 无 MathML 概念 →
                # 用 alttext 里的 LaTeX 源码做纯文本兜底（$…$），公式内部结构丢弃。
                self.math_depth += 1
                tex = str(dict(attrs).get("alttext") or "").strip()
                if tex:
                    self._push_text("$" + tex + "$")
                return
            if self.math_depth > 0:
                # 公式内部标签（mrow/mi/mo/…）：不参与富文本解析
                return
            if tag == "br":
                self._push_text("\n")
                self.inner.append("<br>")
                return
            if tag == "img":
                if self.block_seen:
                    self._emit_img(attrs)
                return
            if tag in _RICH_INLINE_TAGS:
                attrs_d = dict(attrs)
                cls = (attrs_d.get("class") or "").split()
                if "ptoe-note" in cls:
                    self.note = True
                is_marker = "ptoe-marker" in cls
                # 解析 span 上的行内格式类
                fmt = {}
                if tag == "span" and not is_marker:
                    for c in cls:
                        if c == "ptoe-underline":
                            fmt["underline"] = True
                        elif c == "ptoe-underdot":
                            fmt["underdot"] = True
                        elif c == "ptoe-strike":
                            fmt["strike"] = True
                        elif c == "ptoe-charbox":
                            fmt["charbox"] = True
                        elif c == "ptoe-shade":
                            fmt["shade"] = True
                        elif c == "ptoe-highlight":
                            fmt["highlight"] = True
                        elif c == "ptoe-sup":
                            fmt["sup"] = True
                        elif c == "ptoe-sub":
                            fmt["sub"] = True
                        elif c == "ptoe-wavy":
                            fmt["wavy"] = True
                        elif c == "ptoe-double":
                            fmt["double"] = True
                        elif c == "ptoe-overline":
                            fmt["overline"] = True
                        elif c == "ptoe-emphasis":
                            fmt["emphasis"] = True
                        elif c in rulemanage.HIGHLIGHT_COLORS and c != "ptoe-highlight":
                            fmt["highlight_color"] = c
                        elif c == "ptoe-ruby":
                            fmt["ruby"] = rulemanage.normalize_ruby_rt(
                                attrs_d.get("data-rt") or ""
                            )
                    # 字符样式（字号/字体/颜色，style 载体，2026-09-27）：
                    # 走与 sanitize_html 同一校验/序列化契约，保证 DOCX 导出
                    # 拿到的值与落盘 HTML 一致
                    if tag == "span" and not is_marker:
                        norm = rulemanage.normalize_char_style(
                            attrs_d.get("style") or ""
                        )
                        if norm:
                            fmt["style"] = norm
                # strong/b/em/i 标签也记录格式
                if tag in ("strong", "b"):
                    fmt["bold"] = True
                elif tag in ("em", "i"):
                    fmt["italic"] = True
                if is_marker and attrs_d.get("data-ptoe-marker") == "join":
                    if not self._has_non_ws_text:
                        self._join_at_start = True
                    self._join_last = True
                self.stack.append((tag, is_marker, fmt))
                if not is_marker:
                    self.inner.append(self.get_starttag_text() or f"<{tag}>")
                return
            # 其他标签（白名单外，罕见）：不进 runs，仅透传进 inner
            self.inner.append(self.get_starttag_text() or f"<{tag}>")

        def handle_startendtag(self, tag, attrs) -> None:
            if self.tbl_skip or self.tbl_depth:
                # 表格内的自闭合标签
                stxt = self.get_starttag_text() or f"<{tag}/>"
                self.tbl_raw.append(stxt)
                if tag == "br" and self.tbl_cell_open:
                    self.tbl_cell.append("<br>")
                elif tag not in ("td", "th") and self.tbl_cell_open:
                    self.tbl_cell.append(stxt)
                return
            if tag in _SKIP_TAGS:
                return
            if tag == "br":
                self._push_text("\n")
                self.inner.append("<br>")
            elif tag == "img" and self.block_seen:
                self._emit_img(attrs)

        def handle_endtag(self, tag) -> None:
            if tag in _SKIP_TAGS:
                self.skip = max(0, self.skip - 1)
                return
            # MathML 公式（2026-10-21）：</math> 结束公式；公式内部其它闭合标签忽略
            if tag == "math":
                if self.math_depth > 0:
                    self.math_depth -= 1
                return
            if self.math_depth > 0:
                return
            if self.tbl_skip:
                if tag == "table":
                    self.tbl_skip -= 1
                self.tbl_raw.append(f"</{tag}>")
                return
            if self.tbl_depth:
                self.tbl_raw.append(f"</{tag}>")
                if tag == "table":
                    self._tbl_table_end()
                    return
                if tag in ("thead", "tfoot", "tbody"):
                    self.tbl_in_thead = False
                    self._tbl_cell_end()
                    return
                if tag == "tr":
                    self._tbl_row_end()
                    return
                if tag in ("th", "td"):
                    self._tbl_cell_end()
                    return
                # 单元格内行内/其它标签闭合（容忍不匹配/未闭合）
                if self.tbl_cell_stack:
                    t = self.tbl_cell_stack.pop()
                    if t != "__marker__":
                        self.tbl_cell.append(f"</{tag}>")
                return
            if tag in ("p", "div") or (
                len(tag) == 2 and tag[0] == "h" and tag[1] in "123456"
            ):
                self._flush()
                return
            if tag in _RICH_INLINE_TAGS:
                # 弹栈到匹配标签（容忍未闭合嵌套）；被弹掉的标记 span 不写入 inner
                for idx in range(len(self.stack) - 1, -1, -1):
                    if self.stack[idx][0] == tag:
                        seg = self.stack[idx:]
                        del self.stack[idx:]
                        if not any(mk for _, mk, _ in seg):
                            self.inner.append(f"</{tag}>")
                        break

        def handle_data(self, data) -> None:
            if self.skip:
                return
            if self.math_depth > 0:
                return  # 公式内部文本（mrow/mi/… 的内容）不进正文
            if self.tbl_skip:
                self.tbl_raw.append(data)
                return
            if self.tbl_depth:
                if self.math_depth > 0:
                    return  # 公式内部文本（mrow/mi/… 的内容）不进单元格
                self.tbl_raw.append(data)
                if self.tbl_cell_open and (
                    not self.tbl_cell_stack or self.tbl_cell_stack[-1] != "__marker__"
                ):
                    # inner 重转义（convert_charrefs 已解码实体；保持 HTML 形态供透传）
                    self.tbl_cell.append(
                        str(data)
                        .replace("&", "&")
                        .replace("<", "<")
                        .replace(">", ">")
                    )
                return
            if any(mk for _, mk, _ in self.stack):
                return  # 标记 span 内容整体剥除
            self._push_text(data)
            if data.strip():
                self._join_last = False
                self._has_non_ws_text = True
            # inner 重转义（convert_charrefs 已解码实体；保持 HTML 形态供透传）
            self.inner.append(
                str(data).replace("&", "&").replace("<", "<").replace(">", ">")
            )

    parser = _Parser()
    parser.feed(_resolve_noborder_tables(str(html or "")))
    parser.close()
    parser._tbl_table_end()
    parser._flush()
    return parser.blocks


def _can_join_merge(a: dict, b: dict) -> bool:
    """两块能否按 join 标记合并成一个段落。

    题注块（ptoe-caption）不参与合并（2026-10）：合并后只保留前一块的属性，
    题注要么被相邻正文吞掉（属性丢失），要么把整段正文都降级成 0.85em 灰字居中
    ——两种都是版式损坏，故在合并前就拦掉（与注释↔正文不混并同一取向）。
    """
    return (
        a.get("kind") == "p"
        and b.get("kind") == "p"
        and a.get("note") == b.get("note")
        and not _is_caption_block(a)
        and not _is_caption_block(b)
    )


def _apply_join_marks(blocks: list[dict]) -> list[dict]:
    """对富文本块列表应用段落标记（join）合并规则。

    - ``join_next``：当前块与下一块合并；
    - ``join_prev``：当前块并入上一块。
    仅当两块均为 ``<p>`` 且 ``note`` 标志一致时才合并（注释↔正文不混并）。
    非 ``<p>`` 块（标题、图片等）打断合并链。
    """
    if not blocks:
        return []

    out: list[dict] = []
    i = 0
    while i < len(blocks):
        cur = dict(blocks[i])
        cur_join_next = cur.pop("join_next", False)
        cur_join_prev = cur.pop("join_prev", False)

        if cur.get("kind") in ("table", "divider"):
            # 表格/分隔线块打断合并链（表格与文本不合并，前后段落标记不跨表格生效；
            # 分隔线是独立版式元素，同理不能被 join 标记并入邻段，2026-09-27）
            out.append(cur)
            i += 1
            continue

        # 尝试与上一输出块合并：上一块带 join_next 或当前块带 join_prev
        if out:
            prev = out[-1]
            prev_join_next = prev.pop("join_next", False)
            if (prev_join_next or cur_join_prev) and _can_join_merge(prev, cur):
                prev["text"] = (prev.get("text") or "") + (cur.get("text") or "")
                prev["runs"] = (prev.get("runs") or []) + (cur.get("runs") or [])
                prev["inner"] = (prev.get("inner") or "") + (cur.get("inner") or "")
                prev["join_next"] = cur.pop("join_next", False)
                i += 1
                continue
            prev["join_next"] = prev_join_next

        # 不与前驱合并：检查是否可与后继合并
        if cur_join_next and i + 1 < len(blocks):
            nxt = dict(blocks[i + 1])
            nxt_join_next = nxt.pop("join_next", False)
            if _can_join_merge(cur, nxt):
                cur["text"] = (cur.get("text") or "") + (nxt.get("text") or "")
                cur["runs"] = (cur.get("runs") or []) + (nxt.get("runs") or [])
                cur["inner"] = (cur.get("inner") or "") + (nxt.get("inner") or "")
                cur["join_next"] = nxt_join_next
                out.append(cur)
                i += 2
                continue

        out.append(cur)
        i += 1

    for b in out:
        b.pop("join_next", None)
        b.pop("join_prev", None)
    return out


def _image_dims(data: bytes) -> tuple[int, int] | None:
    """从图片字节解析固有尺寸 (宽, 高)；无法解析返回 None。

    支持 PNG / GIF / JPEG（扫描 SOF0/SOF1/SOF2 段）；越界读取一律返回 None。
    """
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">I", data[16:20])[0], struct.unpack(
                ">I", data[20:24]
            )[0]
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<H", data[6:8])[0], struct.unpack("<H", data[8:10])[0]
        if data[:2] == b"\xff\xd8":
            # JPEG：从偏移 2 起逐段扫描，找 SOF0/SOF1/SOF2（0xC0/0xC1/0xC2）
            off = 2
            n = len(data)
            while off + 9 <= n:
                if data[off] != 0xFF:
                    off += 1
                    continue
                marker = data[off + 1]
                if marker in (0xC0, 0xC1, 0xC2):
                    height = struct.unpack(">H", data[off + 5 : off + 7])[0]
                    width = struct.unpack(">H", data[off + 7 : off + 9])[0]
                    return width, height
                if marker in (0xD8, 0xD9, 0xDA) or marker == 0x01:
                    # SOI/EOI/SOS/TEM：无长度段，无法继续可靠扫描
                    break
                seg_len = struct.unpack(">H", data[off + 2 : off + 4])[0]
                if seg_len < 2:
                    break  # 防死循环
                off += 2 + seg_len
    except (IndexError, struct.error):
        return None
    return None


def _data_uri_bytes(src: str) -> tuple[bytes, str] | None:
    """data URI → (图片字节, 扩展名)；非 data URI 返回 None（无法内嵌）。

    扩展名按 MIME 映射：image/png→png、image/jpeg→jpg、image/gif→gif、
    image/webp→webp；其余 MIME 或解码失败返回 None。
    """
    if not src.startswith("data:"):
        return None
    try:
        head, _, b64 = src.partition(",")
        mime = head[5:].split(";", 1)[0].strip().lower()
        ext = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/gif": "gif",
            "image/webp": "webp",
        }.get(mime)
        if ext is None:
            return None
        return base64.b64decode(b64), ext
    except Exception:
        return None


def _norm_export_block(block: Any) -> dict:
    """兼容旧元组块与富文本字典块：统一归一为富文本块字典。

    - dict → 原样返回（_html_to_rich_blocks 产物，含 kind="divider" 分隔线块
      与图文同段图片块的 "meta" 附加键）；
    - ('img', src, alt, cls[, meta]) → 图片块字典（第 5 元组元素为段内元信息，
      缺省即无 meta，形状与历史一致）；
    - ('p'|'hN', 文本) → 无格式信息的普通文本块（runs 单条；旧元组没有题注
      语义，caption 恒 False）。
    """
    if isinstance(block, dict):
        return block
    if block[0] == "img":
        out = {"kind": "img", "src": block[1], "alt": block[2], "cls": block[3]}
        if len(block) > 4 and isinstance(block[4], dict):
            out["meta"] = block[4]
        return out
    kind, text = str(block[0]), str(block[1])
    return {
        "kind": kind,
        "tag": kind,
        "text": text,
        "runs": [{"text": text, "bold": False, "italic": False}],
        "align": "",
        "note": False,
        "caption": False,
        "attrs": "",
        "inner": "",
        "indent": {},
    }


def _img_inline_part(block: dict) -> object:
    """图片块的「段内行内图」源段落序号；非段内图片返回 None。

    meta 只在「图片与前后文本同属一个源段落」时由 _html_to_rich_blocks 附加；
    独占整块的图片没有 meta，块形状与历史四键完全一致（_html_to_export_blocks
    的 ('img', src, alt, cls) 元组同样没有 meta）。
    """
    meta = block.get("meta")
    if not isinstance(meta, dict) or not meta.get("inline"):
        return None
    part = meta.get("part")
    return part if isinstance(part, int) else None


def _is_caption_block(block: dict) -> bool:
    """块是否为题注段落（class 含 ptoe-caption）。

    主判据是 _html_to_rich_blocks 在 _open_block 里登记的 "caption" 布尔键；
    键缺失时回退到 attrs 属性串的 class 解析（与 _rich_align 同一手法），使旧元组
    归一后的块（_norm_export_block，无 caption 键）与手工构造的块也能被识别，
    与 _img_inline_part「meta 缺失即无附加语义」的向后兼容风格一致。
    """
    if block.get("caption"):
        return True
    m = re.search(r'class="([^"]*)"', str(block.get("attrs") or ""))
    return _CAPTION_CLASS in (m.group(1) if m else "").split()


def _docx_escape(text: str) -> str:
    """XML 文本转义（& < > "）。"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _cell_plain_text(inner: str) -> str:
    """表格单元格 inner HTML → 纯文本（<br> → 换行，块级段落 → 换行，其余标签剥除）。

    供 TXT/DOCX 表格导出使用；实体已在 HTMLParser convert_charrefs 阶段解码，
    此处只需还原解析器「no-op 重转义」产生的 &lt;/&gt;/&amp;。

    2026-10：单元格内多个段落（<p>/<div>/<h1-6>）此前被并成一行，
    现按段边界输出换行——DOCX 转 <w:br/>、TXT 保留换行，段落不再粘合。
    """
    s = str(inner or "")
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(?:p|div|h[1-6])\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<(?:p|div|h[1-6])\b[^>]*>", "", s, flags=re.I)
    s = re.sub(r"<[^>]*>", "", s)
    s = s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return s.strip("\n")


def _md_cell_text(inner: str) -> str:
    """表格单元格 inner HTML → Markdown 单元格文本（加粗/斜体转 * 记号，其余剥标签）。

    2026-10：单元格内段落边界转为空格（Markdown 表格单元格不能含裸换行，
    否则表格结构被破坏），避免多段粘合成一行。
    """
    s = str(inner or "")
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"</(?:p|div|h[1-6])\s*>", " ", s, flags=re.I)
    s = re.sub(r"<(?:p|div|h[1-6])\b[^>]*>", "", s, flags=re.I)
    s = re.sub(r"</?(?:strong|b)\s*>", "**", s, flags=re.I)
    s = re.sub(r"</?(?:em|i)\s*>", "*", s, flags=re.I)
    s = re.sub(r"<[^>]*>", "", s)
    s = s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return s.strip()


def _build_docx_table(
    rows: list[list[str]],
    header: bool,
    algn: list[list[str]] | None = None,
    noborder: bool = False,
    valgn: list[list[str]] | None = None,
) -> str:
    """富文本表格块 → OOXML <w:tbl>（单线边框 4 边 + 内横/竖线；表头行加粗）。

    algn：与 rows 同维的二维数组（单元格对齐 ""|"left"|"center"|"right"）；
    对齐为 center/right 时单元格段落输出 <w:jc>（left/空 不输出，与块段落一致）。

    valgn（2026-10）：与 rows 同维的二维数组（单元格垂直对齐
    ""|"top"|"middle"|"bottom"）；非空时单元格 tcPr 输出 <w:vAlign>
    （middle → center），与矫正界面「对齐 ▾」九宫格及 EPUB CSS 一致。

    noborder（2026-10）：表格 HTML 带 class="ptoe-noborder"（表格菜单「不显示
    边框」，分栏/批注版式）时输出六向 w:val="none" 边框，与 EPUB 导出一致。

    单元格内行内格式的**显式作用域**（2026-09-27）：本实现对每个单元格取纯文本
    （_cell_plain_text）后整体输出，故单元格内的加粗/斜体/字符样式（span style 的
    字号/字体/颜色）**一律不落到 DOCX**——与既有行为一致（表头行加粗是唯一例外，
    由 header 标志整体加粗）。这是刻意的取舍：逐单元格还原行内 run 需要把富文本
    块解析器下沉到单元格粒度，收益低于复杂度。文字内容不丢，只是格式丢失；
    需要保留单元格内格式时请用 Markdown/EPUB 导出（两者整块透传原始 HTML）。
    """
    if not rows:
        return "<w:p/>"
    row_count = len(rows)
    col_count = max((len(r) for r in rows), default=0)
    if noborder:
        # 无边框：六向 w:val="none"（阅读器/Word 均不画线）
        border_decls = "".join(
            f'<w:{side} w:val="none" w:sz="0" w:space="0" w:color="auto"/>'
            for side in ("top", "left", "bottom", "right", "insideH", "insideV")
        )
    else:
        border_decls = (
            '<w:top w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
            '<w:left w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
            '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
            '<w:right w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
            '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
            '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
        )
    xml: list[str] = [
        "<w:tbl><w:tblPr>",
        '<w:tblW w:w="0" w:type="auto"/>',
        f"<w:tblBorders>{border_decls}</w:tblBorders></w:tblPr>",
    ]
    for ri, row in enumerate(rows):
        xml.append("<w:tr>")
        bold = bool(header) and ri == 0
        row_align = (algn[ri] if algn and ri < len(algn) else None) or []
        row_valign = (valgn[ri] if valgn and ri < len(valgn) else None) or []
        for ci, cell in enumerate(row):
            text = _cell_plain_text(cell)
            t = _docx_escape(text).replace(
                "\n", '</w:t><w:br/><w:t xml:space="preserve">'
            )
            rpr = "<w:rPr><w:b/></w:rPr>" if bold else "<w:rPr/>"
            a = row_align[ci] if ci < len(row_align) else ""
            jc = f'<w:pPr><w:jc w:val="{a}"/></w:pPr>' if a in ("center", "right") else ""
            va_val = _DOCX_VALIGN.get(row_valign[ci] if ci < len(row_valign) else "", "")
            vxml = f'<w:vAlign w:val="{va_val}"/>' if va_val else ""
            xml.append(
                "<w:tc><w:tcPr><w:tcW w:w=\"0\" w:type=\"auto\"/>" + vxml + "</w:tcPr>"
                f"<w:p>{jc}<w:r>{rpr}<w:t xml:space=\"preserve\">{t}</w:t></w:r></w:p></w:tc>"
            )
        xml.append("</w:tr>")
    xml.append("</w:tbl>")
    return "".join(xml)


def _build_docx(blocks: list[Any], path: str) -> None:
    """块列表 → 最小合法 .docx（zipfile 打包，无第三方依赖）。

    接受两种块形状：富文本字典（_html_to_rich_blocks 产物，保留对齐/缩进/
    行内样式/注释）与旧二元/四元组（无格式信息，行为不变）。

    与 EPUB 导出对齐的段落格式（2026-08-24）：
    - h1-h6 默认居中；h1 底部加分隔线（对应 EPUB CSS border-bottom）；
    - 对齐类 ptoe-align-center/right/justify → w:jc center/right/both；
    - 段落设置 data-* 属性 → w:spacing/w:ind（1em≈240 twips、段前/后 1 行
      ≈360 twips、行距 ×240，与 htmlmanage._indent_style_attrs 同语义）；
    - 注释块（ptoe-note）→ 斜体 + 灰色（808080）；行内加粗/斜体逐 run 保留；
    - 题注块（ptoe-caption，2026-10）→ 居中 + 灰色 666666 + 0.85 倍字号
      （_DOCX_CAPTION_SZ = round(21 半磅 × 0.85) = 18 半磅；CSS 0.85em 的 DOCX
      对应物），对应 CSS 的 text-align:center / color:#666 / font-size:0.85em；
      显式对齐类仍优先（同标题默认居中的处置方式）；
    - 字符样式（span style 的字号/字体/颜色）→ 逐 run w:sz/w:rFonts/w:color
      （标题块已有块级字号时不重复写 w:sz；命名色无法映射为 OOXML hex 故忽略）；
    - 分隔线块（kind="divider"）→ 空段落 + 下边框，线型映射 single/dashed/
      dotted/double（不写字形文本，避免阅读字体缺字）；
    - 图片块内嵌真实图片字节（data URI）：尺寸 class ptoe-img-w25/50/75/100
      → 宽度 5 英寸 × 比例，高度按固有宽高比；非 data URI 或解析失败 →
      以 [图片] 占位段落输出（与 TXT 一致）。
    """
    import zipfile

    EM_TWIPS = 240  # 1em ≈ 240 twips（12pt 基准的近似换算）

    parts: list[str] = [_DOCX_DOCUMENT_HEAD]
    media: list[tuple[str, bytes]] = []  # (word/media/imageN.ext, 字节)
    rels: list[str] = []
    img_no = 0
    for raw in blocks:
        block = _norm_export_block(raw)
        if block["kind"] == "img":
            src = block.get("src") or ""
            cls = block.get("cls") or ""
            decoded = _data_uri_bytes(src)
            if decoded is None:
                # 无法内嵌（非 data URI）：输出 [图片] 占位段落
                parts.append(
                    '<w:p><w:r><w:t xml:space="preserve">[图片]</w:t></w:r></w:p>'
                )
                continue
            data, ext = decoded
            img_no += 1
            fname = f"word/media/image{img_no}.{ext}"
            media.append((fname, data))
            rels.append(
                f'<Relationship Id="rIdImg{img_no}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                f'Target="media/image{img_no}.{ext}"/>'
            )
            # 尺寸：ptoe-img-w25/50/75/100 → 宽度比例；无尺寸 class → 100%
            frac = 1.0
            for c in cls.split():
                if c in _DOCX_IMG_FRAC:
                    frac = _DOCX_IMG_FRAC[c]
            cx = int(_DOCX_IMG_BASE_CX * frac)
            dims = _image_dims(data)
            if dims:
                w, h = dims
                aspect = (w / h) if h else (4 / 3)
            else:
                aspect = 4 / 3  # 无法解析固有尺寸时按 4:3 兜底
            cy = int(cx / aspect)
            parts.append(
                f'<w:p><w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
                f'<wp:extent cx="{cx}" cy="{cy}"/>'
                f'<wp:docPr id="{img_no}" name="Picture {img_no}"/>'
                f'<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
                f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{img_no}" name="Picture {img_no}"/><pic:cNvPicPr/></pic:nvPicPr>'
                f'<pic:blipFill><a:blip r:embed="rIdImg{img_no}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
                f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
                f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
                f"</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
            )
            continue

        if block["kind"] == "table":
            # 表格 → OOXML <w:tbl>（单线边框；表头行加粗；跨列/跨行防御性拍平；
            # 单元格对齐 algn → w:jc；class="ptoe-noborder" → 无边框，2026-10）
            parts.append(
                _build_docx_table(
                    block.get("rows") or [],
                    bool(block.get("header")),
                    block.get("algn"),
                    noborder=bool(block.get("noborder")),
                    valgn=block.get("valgn"),
                )
            )
            continue

        if block["kind"] == "divider":
            # 分隔线（2026-09-27）→ 空段落 + 下边框；线型映射到 OOXML 枚举。
            # 不写任何 <w:t> 文本：字形交给边框画，避免不同字体缺字。
            # 花样型（2026-10-21）没有可画的边框 → 改为输出居中的字形文本。
            style = block.get("style") or DIVIDER_DEFAULT_STYLE
            if style in DIVIDER_DECORATIVE:
                glyph = _divider_glyph(style)
                parts.append(
                    "<w:p><w:pPr>"
                    '<w:spacing w:before="180" w:after="180"/>'
                    '<w:jc w:val="center"/>'
                    "</w:pPr>"
                    "<w:r><w:rPr><w:color w:val=\"999999\"/></w:rPr>"
                    '<w:t xml:space="preserve">' + glyph + "</w:t></w:r></w:p>"
                )
                continue
            val = _DOCX_DIVIDER_BORDER.get(style, "single")
            parts.append(
                "<w:p><w:pPr>"
                '<w:pBdr><w:bottom w:val="' + val + '" w:sz="6" w:space="1" '
                'w:color="999999"/></w:pBdr>'
                '<w:spacing w:before="180" w:after="180"/>'
                "</w:pPr></w:p>"
            )
            continue

        kind = block["kind"]
        runs = block.get("runs") or [
            {"text": block.get("text", ""), "bold": False, "italic": False}
        ]
        is_heading = kind.startswith("h") and len(kind) == 2 and kind[1].isdigit()
        lvl = int(kind[1]) if is_heading else 0
        align = block.get("align") or ""
        indent = block.get("indent") or {}
        note = bool(block.get("note"))
        # 题注（2026-10）：块登记了 caption（或 class 里带 ptoe-caption，见
        # _is_caption_block）即按题注版式输出——居中 + 灰 666666 + 0.85 倍字号
        caption = _is_caption_block(block)

        # -- pPr（按 OOXML schema 顺序：pBdr → spacing → ind → jc → outlineLvl）--
        ppr_parts: list[str] = []
        if is_heading and lvl == 1 and not align:
            # 与 EPUB 一致：一级标题下加分隔线（CSS border-bottom 的 DOCX 对应物）
            ppr_parts.append(
                '<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="4" '
                'w:color="999999"/></w:pBdr>'
            )
        sp_attrs = ""
        if indent.get("spb") is not None:
            # 段前：1 行 ≈ 1.5em ≈ 360 twips（与 EPUB margin-top ×1.5em 同源）
            sp_attrs += f' w:before="{int(round(indent["spb"] * 360))}"'
        if indent.get("spa") is not None:
            sp_attrs += f' w:after="{int(round(indent["spa"] * 360))}"'
        if indent.get("lh") is not None and indent["lh"] > 0:
            sp_attrs += f' w:line="{int(round(indent["lh"] * 240))}" w:lineRule="auto"'
        if sp_attrs:
            ppr_parts.append(f"<w:spacing{sp_attrs}/>")
        ind_attrs = ""
        pl = indent.get("pl")
        pr = indent.get("pr")
        indv = indent.get("indv")
        mode = indent.get("ind") or ""
        left = pl
        hanging = 0.0
        firstline = 0.0
        if mode == "hang":
            # 悬挂缩进：左缩进 = pl + indv，悬挂量 = indv（同 htmlmanage）
            left = (pl or 0.0) + (indv if indv is not None else 2.0)
            hanging = indv if indv is not None else 2.0
        elif mode == "first":
            # 首行缩进：默认 2 字符（与 htmlmanage 缺省一致）
            firstline = indv if indv is not None else 2.0
        if left:
            ind_attrs += f' w:left="{int(round(left * EM_TWIPS))}"'
        if pr:
            ind_attrs += f' w:right="{int(round(pr * EM_TWIPS))}"'
        if firstline:
            ind_attrs += f' w:firstLine="{int(round(firstline * EM_TWIPS))}"'
        if hanging:
            ind_attrs += f' w:hanging="{int(round(hanging * EM_TWIPS))}"'
        if ind_attrs:
            ppr_parts.append(f"<w:ind{ind_attrs}/>")
        jc = align  # center / right / justify
        if not jc and (is_heading or caption):
            # 与 EPUB 一致：标题默认居中；题注的 CSS 契约 text-align:center 同理
            # （显式对齐类优先，与标题默认居中的既有处置方式一致）
            jc = "center"
        if jc:
            ppr_parts.append(f'<w:jc w:val="{jc}"/>')
        if is_heading:
            ppr_parts.append(f'<w:outlineLvl w:val="{lvl - 1}"/>')

        sz = _DOCX_HEADING_SZ.get(lvl, 24) if is_heading else None
        if caption and not is_heading:
            # 题注字号：CSS 0.85em × 正文基准（五号 21 半磅）= 18 半磅（9pt）
            sz = _DOCX_CAPTION_SZ
        run_xml_parts: list[str] = []
        for r in runs:
            # CT_RPr 子元素次序由 OOXML schema 固定（见 _DOCX_RPR_ORDER），乱序会让
            # Word 判定文档损坏 / 静默丢弃格式。按槽位收集后按固定次序拼接，取代
            # 旧代码的「按 if 顺序追加」（逐字符样式的 rFonts/color 追加在末尾，2026-09-27 修复）。
            rpr_slot: dict[str, str] = {}
            if is_heading or r.get("bold"):
                rpr_slot["b"] = "<w:b/>"
            if r.get("italic") or note:
                rpr_slot["i"] = "<w:i/>"
            if note:
                rpr_slot["color"] = '<w:color w:val="808080"/>'
            if caption:
                # 题注灰（CSS #666 → OOXML 十六进制 666666）；w:color 只能出现一次，
                # 题注与注释不共存（不参与 join 合并），此处以题注为准
                rpr_slot["color"] = f'<w:color w:val="{_CAPTION_COLOR_HEX}"/>'
            if r.get("underline"):
                rpr_slot["u"] = '<w:u w:val="single"/>'
            if r.get("wavy"):
                # 波浪下划线（2026-10-21）：OOXML 有 wave 取值
                rpr_slot["u"] = '<w:u w:val="wave"/>'
            if r.get("double"):
                # 双下划线（2026-10-21）
                rpr_slot["u"] = '<w:u w:val="double"/>'
            if r.get("underdot"):
                # w:u 只能出现一次：下加点与下划线同开时以下加点为准
                rpr_slot["u"] = '<w:u w:val="dotted"/>'
            if r.get("strike"):
                rpr_slot["strike"] = "<w:strike/>"
            if r.get("sup"):
                rpr_slot["vertAlign"] = '<w:vertAlign w:val="superscript"/>'
            if r.get("sub"):
                rpr_slot["vertAlign"] = '<w:vertAlign w:val="subscript"/>'
            if r.get("shade"):
                rpr_slot["shd"] = '<w:shd w:val="clear" w:fill="EEF1F4"/>'
            if r.get("highlight"):
                rpr_slot["shd"] = '<w:shd w:val="clear" w:fill="E0E0E0"/>'
            if r.get("charbox"):
                rpr_slot["bdr"] = (
                    '<w:bdr w:val="single" w:sz="4" w:space="1" w:color="333333"/>'
                )
            if r.get("emphasis"):
                # 着重号（2026-10-21）：OOXML 的 w:em 就是着重号标记
                rpr_slot["em"] = '<w:em w:val="dot"/>'
            if r.get("highlight_color"):
                # 多色突显（2026-10-21）：w:shd 只能出现一次，
                # 颜色突显优先于单色突显（此处后写 → 覆盖）
                fill = (
                    rulemanage.HIGHLIGHT_COLORS.get(r["highlight_color"], "")
                    .lstrip("#")
                    .upper()
                )
                if fill:
                    rpr_slot["shd"] = f'<w:shd w:val="clear" w:fill="{fill}"/>'
            if sz:
                rpr_slot["sz"] = f'<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/>'
            # 字符样式（字号/字体/颜色，2026-09-27）：span style → w:sz/w:rFonts/w:color
            cs = r.get("style") or ""
            if cs:
                cs_px = rulemanage.char_style_get(cs, "font-size")
                # 标题已有块级字号且 w:sz 只能出现一次，标题内字符样式不再改字号
                if cs_px and not sz:
                    # CSS px → 半磅（half-point）：1px = 1/96 英寸，1pt = 1/72 英寸
                    hp = int(round(float(cs_px[:-2]) * 0.75 * 2))
                    hp = max(_DOCX_MIN_SZ, min(_DOCX_MAX_SZ, hp))
                    rpr_slot["sz"] = f'<w:sz w:val="{hp}"/><w:szCs w:val="{hp}"/>'
                cs_fam = rulemanage.char_style_get(cs, "font-family")
                if cs_fam:
                    # DOCX 的 w:ascii/hAnsi 只能取一个字体名：取栈首位（主字体），
                    # eastAsia 同样取首位；引号已由 normalize 剥除
                    fam = cs_fam.split(",")[0].strip().strip("'\"")
                    if fam:
                        rpr_slot["rFonts"] = (
                            f'<w:rFonts w:ascii="{_docx_escape(fam)}" '
                            f'w:hAnsi="{_docx_escape(fam)}" '
                            f'w:eastAsia="{_docx_escape(fam)}"/>'
                        )
                cs_color = rulemanage.char_style_get(cs, "color")
                if cs_color and re.fullmatch(r"#[0-9a-fA-F]{3,8}", cs_color):
                    # 只接受十六进制（DOCX 颜色是 hex 枚举，命名色无法映射）
                    hexv = cs_color[1:]
                    if len(hexv) == 3:
                        hexv = "".join(ch * 2 for ch in hexv)
                    if len(hexv) in (6, 8):
                        # w:color 只能出现一次：注释灰为底色，逐字符颜色覆盖
                        rpr_slot["color"] = f'<w:color w:val="{hexv[:6].upper()}"/>'
            rpr = "".join(rpr_slot[k] for k in _DOCX_RPR_ORDER if k in rpr_slot)
            t = _docx_escape(r.get("text", "")).replace(
                "\n", '</w:t><w:br/><w:t xml:space="preserve">'
            )
            run_xml_parts.append(
                f"<w:r><w:rPr>{rpr}</w:rPr>"
                f'<w:t xml:space="preserve">{t}</w:t></w:r>'
            )
            # 拼音注音（2026-10-21）：OOXML 有原生 w:ruby，但结构复杂、兼容性差；
            # 这里退化为「正文 + 括号注音（小字）」，注音信息不丢
            ruby_rt = r.get("ruby") or ""
            if ruby_rt:
                ann_sz = max(
                    _DOCX_MIN_SZ, int(round((sz or _DOCX_BODY_SZ) * 0.6))
                )
                ann_rpr = (
                    f'<w:sz w:val="{ann_sz}"/><w:szCs w:val="{ann_sz}"/>'
                )
                at = _docx_escape(f"({ruby_rt})")
                run_xml_parts.append(
                    f"<w:r><w:rPr>{ann_rpr}</w:rPr>"
                    f'<w:t xml:space="preserve">{at}</w:t></w:r>'
                )
        ppr_xml = f"<w:pPr>{''.join(ppr_parts)}</w:pPr>" if ppr_parts else ""
        parts.append(f"<w:p>{ppr_xml}{''.join(run_xml_parts)}</w:p>")
    parts.append(_DOCX_DOCUMENT_TAIL)
    rels_xml = _DOCX_DOCUMENT_RELS_HEAD + "".join(rels) + _DOCX_DOCUMENT_RELS_TAIL
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        zf.writestr("_rels/.rels", _DOCX_RELS)
        zf.writestr("word/document.xml", "".join(parts))
        zf.writestr("word/_rels/document.xml.rels", rels_xml)
        for fname, data in media:
            zf.writestr(fname, data)


def _build_md(blocks: list[Any], path: str) -> None:
    """块列表 → Markdown 文件。

    转换规则与前端 ui/app.js 的 htmlToMd/inlineToMd 保持一致（格式一致性）：
    - h1-h6 → '#'*N 标题；普通段落 → 正文；块间空一行；文件末尾单个换行；
    - 带 class=/data- 属性的块整块原样透传原始 HTML（对齐/缩进等版式信息
      不丢失，且可被界面 Markdown 模式 mdToHtml 无损还原）；
    - 加粗 **…**、斜体 *…*（同时加粗斜体 ***…***）；<br> 已在解析时转为换行；
    - 图片 ![alt](src)，data URI 原样内联（单文件自包含，与 DOCX/EPUB 内嵌一致）；
      **段内行内图**（meta.inline，_html_to_rich_blocks 判定）与同一源段落的前后
      纯文本段落合并为同一个 Markdown 段落（图文同段语义，2026-10）；整块透传
      raw HTML 的块（带 class/data- 或行内格式类）不参与合并，图片自成一段；
    - 字符样式（字号/字体/颜色，span style）与分隔线块（'---'）：Markdown 无对应
      语法，分别走 raw HTML 整块透传与 GFM 分隔线记号；
    - **题注段（ptoe-caption，2026-10）**：GFM 同样没有「字号/颜色/居中」语法，
      故沿用 raw HTML 透传通道输出带 class 的块（界面 Markdown 模式 mdToHtml 可
      无损还原题注），并**补一份内联 style**（_CAPTION_MD_STYLE）——纯 class 在没有
      本项目样式表的渲染器（VSCode 预览 / GitHub / 各类阅读器）里不产生任何视觉
      差异，等于退化成普通段落，这里正是要补的缺口。不用 *斜体* / **粗** 记号：
      那表达的是「字形强调」而非「题注」语义，且 mdToHtml 还原时会退化成 <em>，
      题注类直接丢失（不可逆）。
    - 不做额外 Markdown 转义（与前端 inlineToMd 一致，避免同一内容两种输出）。
    编码 utf-8（无 BOM，Markdown 标准形态；TXT 才用 utf-8-sig）。
    """

    def _inline(rs: list[dict]) -> str:
        out: list[str] = []
        for r in rs:
            t = r.get("text", "")
            if r.get("bold") and r.get("italic"):
                out.append(f"***{t}***")
            elif r.get("bold"):
                out.append(f"**{t}**")
            elif r.get("italic"):
                out.append(f"*{t}*")
            else:
                out.append(t)
        return "".join(out)

    def _has_inline_formats(inner: str) -> bool:
        """检查 inner HTML 是否包含新增的行内格式类或字符样式 style 属性。"""
        # 行内格式类：出现在 class 属性值里即算（含多类共存，如
        # class="ptoe-wavy ptoe-underline"）—— 单词边界匹配，避免 ptoe-highlight
        # 误命中 ptoe-highlight-green
        for cls in ("ptoe-underline", "ptoe-underdot", "ptoe-strike", "ptoe-charbox",
                      "ptoe-shade", "ptoe-highlight", "ptoe-sup", "ptoe-sub",
                      "ptoe-wavy", "ptoe-double", "ptoe-overline", "ptoe-emphasis",
                      "ptoe-highlight-green", "ptoe-highlight-blue",
                      "ptoe-highlight-pink", "ptoe-highlight-orange",
                      "ptoe-highlight-purple", "ptoe-ruby"):
            if re.search(
                r'class=["\'][^"\']*\b' + re.escape(cls) + r'\b[^"\']*["\']',
                inner,
            ):
                return True
        # 字符样式（2026-09-27）：Markdown 无字号/字体/颜色语法，走与行内格式类
        # 相同的 raw HTML 整块透传，mdToHtml 可无损还原，不丢 span style
        if 'style="' in inner or "style='" in inner:
            return True
        return False

    def _raw_passthrough(b: dict) -> bool:
        """块是否整块原样透传原始 HTML（判据单一来源：输出分支与合并逻辑共用）。"""
        attrs = b.get("attrs") or ""
        inner = b.get("inner") or ""
        return bool("class=" in attrs or "data-" in attrs or _has_inline_formats(inner))

    def _merge_part(b: dict) -> object:
        """该块可与同段内容合并时的源段落序号；不可合并返回 None。

        只认「纯文本 <p>」：标题、整块透传 raw HTML 的块、表格、分隔线一律不合并
        ——把 Markdown 记号塞进已闭合的 HTML 标签里既不生效（GFM 不解析 HTML 块内
        语法）也会破坏 mdToHtml 的往返还原。
        """
        part = b.get("part")
        if not isinstance(part, int) or b.get("kind") != "p" or _raw_passthrough(b):
            return None
        return part

    def _raw_html_block(b: dict, attrs: str) -> str:
        """按标签 + 属性串 + inner 组装一个 raw HTML 块片段（与 raw 透传同形状）。"""
        tag = b.get("tag") or b["kind"]
        inner = b.get("inner") or ""
        return f"<{tag}{(' ' + attrs) if attrs else ''}>{inner}</{tag}>"

    def _caption_md_html(b: dict) -> str:
        """题注块 → 带内联样式的 raw HTML 片段（2026-10）。

        class 保留（mdToHtml 无损还原题注），style 补齐视觉契约；块上已自带
        style 时不再叠加（重复声明无效，且会让 XHTML 属性重复）。
        """
        attrs = b.get("attrs") or ""
        if "style=" not in attrs:
            attrs = f'{attrs} style="{_CAPTION_MD_STYLE}"'.strip()
        return _raw_html_block(b, attrs)

    def _block_md(b: dict) -> tuple[str, object] | None:
        """块 → (Markdown 片段, 可合并的源段落序号|None)；None = 该块不产出片段。"""
        if b["kind"] == "img":
            # 图文混合（2026-10）：段内行内图携带 meta.inline+part，可与同源段落
            # 的纯文本段落合并成一段；独占整块的图片 part=None，自成一段。
            return (
                f"![{b.get('alt') or ''}]({b.get('src') or ''})",
                _img_inline_part(b),
            )
        if b["kind"] == "table":
            # 表格 → GFM 管线表（表头行 + | --- | --- | 分隔线）；无表头时补
            # 空表头行 + 分隔线，保证首行显示为数据行。
            # 对齐（2026-09-20）：每列取表头+首数据行的对齐（任一非空）为列对齐；
            # 同列全部单元格（含表头）非空对齐须一致，否则整表回退 raw HTML 透传
            # （与 colspan/rowspan 同一兜底路径）。
            if b.get("has_span"):
                # 防御：含 colspan/rowspan 的表格无法用管线表表达 → 整块 raw HTML 透传
                return (b.get("raw") or "", None)
            rows: list[list[str]] = b.get("rows") or []
            algn: list[list[str]] = b.get("algn") or []
            if not rows:
                return None

            def _md_row(cells: list[str]) -> str:
                return "| " + " | ".join(_md_cell_text(c) for c in cells) + " |"

            # 列对齐决定：优先表头行、其次首数据行进位填充（任一非空）。
            # 同列内非空对齐值若不一致（如表头居中、数据行左对齐）→ 冲突 → raw 透传。
            ncols = max((len(r) for r in rows), default=1)
            col_aligns: list[str] = []
            raw_fallback = False
            for c in range(ncols):
                vals: set[str] = set()
                for ri, row in enumerate(rows):
                    if c < len(row) and algn and ri < len(algn) and c < len(algn[ri]):
                        v = algn[ri][c]
                        if v:
                            vals.add(v)
                if len(vals) > 1:
                    raw_fallback = True
                    break
                col_aligns.append(next(iter(vals), ""))
            if raw_fallback:
                # 同一列单元格对齐各异：管线表无法表达 → 整块 raw HTML 透传
                return (b.get("raw") or "", None)

            def _md_sep(cols: int) -> str:
                # GFM 分隔符：左 :--- / 中 :---: / 右 ---:；无对齐 ---
                marks = {"left": ":---", "center": ":---:", "right": "---:"}
                return (
                    "| "
                    + " | ".join(marks.get(col_aligns[c], "---") for c in range(cols))
                    + " |"
                )

            tbl_lines: list[str] = []
            if b.get("header"):
                tbl_lines.append(_md_row(rows[0]))
                tbl_lines.append(_md_sep(len(rows[0])))
                tbl_lines.extend(_md_row(r) for r in rows[1:])
            else:
                # 无表头：补空表头行；列对齐按首行数据行推
                n = max((len(r) for r in rows), default=1)
                tbl_lines.append("| " + " | ".join("" for _ in range(n)) + " |")
                tbl_lines.append(_md_sep(n))
                tbl_lines.extend(_md_row(r) for r in rows)
            return ("\n".join(tbl_lines), None)
        if b["kind"] == "divider":
            # 分隔线（2026-09-27）→ GFM 分隔线 '---'（Markdown 只有一个横线记号，
            # 写 '- - -' 会与列表项混淆；四种线型差异由 EPUB/DOCX 承载）
            # 花样型（2026-10-21）没有横线记号 → 直接写字形（保留装饰效果）
            style = b.get("style") or DIVIDER_DEFAULT_STYLE
            if style in DIVIDER_DECORATIVE:
                return (_divider_glyph(style), None)
            return ("---", None)
        if _is_caption_block(b):
            # 题注段（2026-10）：raw HTML 通道 + 内联样式（居中/小号/灰色在 GFM
            # 里无语法，见 _caption_md_html 的取舍说明）。part 恒 None——题注是
            # 独立版式元素，绝不与相邻段落合并成同一段 Markdown。
            return (_caption_md_html(b), None)
        attrs = b.get("attrs") or ""
        # 带属性块、或含新增行内格式类：原样透传（与前端 htmlToMd 规则一致）
        if _raw_passthrough(b):
            return (_raw_html_block(b, attrs), None)
        kind = b["kind"]
        if kind.startswith("h") and len(kind) == 2 and kind[1].isdigit():
            return ("#" * int(kind[1]) + " " + _inline(b["runs"]), None)
        return (_inline(b["runs"]), _merge_part(b))

    # 图文混合（2026-10）：把「文字 / 段内行内图 / 文字」按源段落序号拼回同一段。
    # GFM 支持段内 ![alt](src)，这是唯一能表达「图文同段」的导出格式；TXT/DOCX
    # 维持「文字 / 图 / 文字」三段的既有降级行为（见 _html_to_rich_blocks）。
    parts: list[str] = []
    open_part: object = None  # 上一片段的可合并源段落序号（None = 不可追加）
    for raw in blocks:
        res = _block_md(_norm_export_block(raw))
        if res is None:
            continue
        md, part = res
        if part is not None and part == open_part:
            parts[-1] += md
        else:
            parts.append(md)
        open_part = part
    Path(path).write_text("\n\n".join(parts) + "\n", encoding="utf-8")


def _pick_export_path(
    state: dict[str, Any], fmt: str, fallback_dir: str | None = None
) -> tuple[str | None, bool]:
    """弹保存对话框选导出路径。返回 (路径, 是否弹过对话框)。

    - 用户取消 → (None, True)，调用方直接回「已取消」；
    - tkinter 不可用（headless/无桌面）→ (None, False)，调用方用默认路径兜底。
    """
    ext = {"txt": "txt", "epub": "epub", "docx": "docx", "md": "md"}.get(fmt, "docx")
    label = "Markdown 文件" if fmt == "md" else f"{ext.upper()} 文件"
    # 默认文件名优先用重命名后的 display_name，回退 history_name（2026-08-30）
    base = (
        state.get("display_name")
        or state.get("history_name")
        or ""
    ).removesuffix(".pdf")
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
                title=f"导出为 {label}",
                defaultextension=f".{ext}",
                filetypes=[(label, f"*.{ext}"), ("所有文件", "*.*")],
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
    state: dict[str, Any], fmt: str, fallback_dir: str | None = None
) -> tuple[str | None, bool]:
    """把保存对话框交给主线程弹出（tkinter 在主线程才能可靠显示/置顶）。

    返回与 _pick_export_path 相同：(路径, 是否弹过对话框)：
    - 用户取消 → (None, True)；
    - tkinter 不可用（headless）→ (None, False)，调用方默认路径兜底；
    - 界面已关闭（主循环退出前唤醒）→ 抛 _ExportAborted。
    """
    req: dict[str, Any] = {
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


def _txt_indent_prefix(ind: dict) -> int:
    """TXT 导出段首全角空格数（clamp 0..8）。

    纯文本没有「续行」概念：
    - data-ind=first + indv：首行缩进 → 全角空格前缀（既有行为，clamp 1..8）；
    - data-ind=hang  + indv：悬挂缩进在 HTML 里是 margin-left + text-indent:-N，
      落到纯文本 ≈ 整段右移 pl+indv（段左距）→ min(round(pl+indv),8)；
    - data-pl（无 first/hang 时）：普通左缩进 → 每行前缀 min(round(pl),8)；
    - data-pr / data-lh：纯文本无法表达，一律跳过（返回 0）。
    """
    mode = ind.get("ind")
    indv = ind.get("indv")
    pl = ind.get("pl")
    if mode == "first":
        n = indv if indv is not None else 2
        # 既有行为：int 截断 + clamp 1..8（至少 1 个全角空格）
        return max(1, min(8, int(n)))
    if mode == "hang":
        base = (pl or 0.0) + (indv if indv is not None else 0.0)
        return max(0, min(8, int(round(base))))
    if pl is not None:
        return max(0, min(8, int(round(pl))))
    return 0


def _txt_spacing(v: Any) -> int:
    """段前/段后空行数（clamp 0..9）。缺失/非法 → 0。

    与 EPUB 语义一致但不跨介质换算：纯文本里 1 行间距 ≈ 1 个空行。
    """
    if v is None:
        return 0
    try:
        return max(0, min(9, int(round(float(v)))))
    except (TypeError, ValueError):
        return 0


_BLANK_PARA_RE = re.compile(
    r"<(p|div)\b[^>]*>(?:"
    r"\s|&nbsp;|&#160;|&emsp;|&ensp;|&#8194;|&#8195;|&#12288;|<br\s*/?>"
    r")*</\1>",
    re.IGNORECASE,
)

_BLANK_LINE_RUN_RE = re.compile(r"(?:\r?\n[ \t]*){2,}")


def export_clean_blank_lines_enabled() -> bool:
    """「导出时清理空白行」开关（设置界面 → 智能清理规则）。

    读 ui_settings.clean_blank_lines（默认 False）。开启后，导出（txt/md/docx/epub）
    会删掉纯空白的段落、把连续空行压缩为一个；关闭则原样保留空白换行。
    """
    try:
        from configmanage import get_clean_settings

        return bool(get_clean_settings().get("clean_blank_lines"))
    except Exception:
        return False


def _strip_blank_paragraphs(html: str) -> str:
    """删掉纯空白的 <p>/<div> 块（EPUB 导出用，配合「清理空白行」开关）。

    表格内部（<td>/<th> 之间）的空白段落**保留**——分栏/批注版式里那可能是有意
    的留白空行；只清理表格外的顶层空段。按顶层 <table>…</table> 切分逐段替换，
    表内原样透传（表格不嵌套，取开标签后第一个 </table> 配对）。
    """
    s = str(html or "")
    if not s:
        return s
    out: list[str] = []
    pos = 0
    for m in _TABLE_OPEN_RE.finditer(s):
        close = _TABLE_CLOSE_RE.search(s, m.end())
        if not close:
            break
        out.append(_BLANK_PARA_RE.sub("", s[pos : m.start()]))
        out.append(s[m.start() : close.end()])
        pos = close.end()
    out.append(_BLANK_PARA_RE.sub("", s[pos:]))
    return "".join(out)


def _drop_blank_blocks(blocks: list[dict]) -> list[dict]:
    """去掉纯空白文本的段落块（txt/md/docx 导出用，配合「清理空白行」开关）。

    表格块、图片块、分隔线块与题注块保留（题注即使文本为空也代表版式元素）；
    _html_to_rich_blocks 本身已不产出空文本块，这里是防御性兜底。
    """
    out: list[dict] = []
    for b in blocks or []:
        if b.get("kind") == "p" and not "".join(
            r.get("text") or "" for r in (b.get("runs") or [])
        ).strip():
            continue
        out.append(b)
    return out


def _collapse_blank_lines(text: str) -> str:
    """把连续空行压缩为一个、去掉首尾空白行（txt 导出用，配合「清理空白行」开关）。"""
    s = _BLANK_LINE_RUN_RE.sub("\n\n", str(text or ""))
    return s.strip("\n")


def export_content_to_file(
    items: list[dict], fmt: str, out_path: str, title: str = "矫正导出"
) -> str:
    """把已排序的 {page, html} 页面列表导出为 fmt(txt/docx/md/epub)，写入 out_path。

    与矫正界面 /api/export 同一转换链路（同一份实现，保证两边输出一致）：
    - txt/md/docx 走 _html_to_rich_blocks → _apply_join_marks → 各格式写入器；
    - epub 走 apply_markers → HTMLConverter.convert_document → 复制到 out_path。
    items 须含 'page'/'html' 且已排序。返回实际写出路径。

    供两处复用：矫正界面工具栏「完成并转换」旁导出下拉，以及主界面
    「矫正历史」行的「另存为」（多格式，弹窗选保存位置）。
    """
    if fmt not in ("txt", "docx", "epub", "md"):
        raise ValueError(f"不支持的导出格式：{fmt}")
    blocks: list[dict] = []
    for item in items:
        # 应用加粗注释标签转换（注　　释：）
        html_text = transform_note_labels(str(item.get("html") or ""))
        blocks.extend(_html_to_rich_blocks(html_text))
    blocks = _apply_join_marks(blocks)
    # 「清理空白行」（设置界面 → 智能清理规则，默认关）：开启时删纯空白段落、
    # 压缩连续空行；关闭则导出原样保留空白换行。
    _clean_blank = export_clean_blank_lines_enabled()
    if _clean_blank:
        blocks = _drop_blank_blocks(blocks)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "txt":

        def _txt_line(b: dict) -> str:
            # 图片块以 [图片] 占位符表示；段落设置（缩进/间距）在纯文本中以
            # 全角空格前缀与空行近似（见 _txt_indent_prefix / _txt_spacing）
            # 题注（ptoe-caption）在此**有意降级为普通段落**：纯文本没有字号、
            # 颜色、对齐的概念，题注与正文同样是一行文字，逐字节维持现状即可；
            # 需要「小号灰字居中」版式请用 DOCX（w:sz/w:color/w:jc）或 EPUB 导出。
            if b["kind"] == "img":
                return "[图片]"
            if b["kind"] == "table":
                # 表格 → Tab 分隔行（每行一个表格行）；跨列/跨行防御性拍平
                rows: list[list[str]] = b.get("rows") or []
                if not rows:
                    return ""
                return "\n".join(
                    "\t".join(_cell_plain_text(c) for c in row) for row in rows
                )
            line = "".join(r["text"] for r in b["runs"])
            ind = b.get("indent") or {}
            n = _txt_indent_prefix(ind)
            if n:
                line = "\u3000" * n + line
            spb = _txt_spacing(ind.get("spb"))
            spa = _txt_spacing(ind.get("spa"))
            # 段前/段后空行：spb 前缀、spa 后缀（与段落间原有的 \n\n 叠加，
            # 空行总数 = max(spb,spa) 之外的各插各的，互不抵消）
            if spb:
                line = "\n" * spb + line
            if spa:
                line = line + "\n" * spa
            return line

        text = "\n\n".join(_txt_line(b) for b in blocks)
        if _clean_blank:
            text = _collapse_blank_lines(text)
        out.write_text(text + "\n", encoding=_TXT_ENCODING)
    elif fmt == "md":
        # Markdown 导出：与前端 htmlToMd 规则一致（见 _build_md）
        _build_md(blocks, str(out))
    elif fmt == "epub":
        # epub：标记→文章结构→XHTML→打包（临时目录隔离，完成后清理）
        import shutil as _sh
        import tempfile as _tf
        from htmlmanage import HTMLConverter

        tmp_dir = _tf.mkdtemp(prefix="ptoe_export_epub_")
        try:
            # 浏览器/历史提交的 html 可能含 <div> 块（Chrome contenteditable
            # 回车产生），apply_markers 只认 p/h1-6——先 sanitize 归一为
            # <p>（保留对齐/注释/图片 class），与「完成并转换」路径一致；
            # 否则产出 <p><div class="ptoe-align-center">…</div></p>
            # 非法嵌套、对齐丢失（2026-08-15）
            src_items = [
                {"page": p["page"], "text": sanitize_html(str(p["html"] or ""))}
                for p in items
            ]
            if _clean_blank:
                # 删表格外的空白段落（表内保留，见 _strip_blank_paragraphs）
                for _it in src_items:
                    _it["text"] = _strip_blank_paragraphs(_it["text"])
            articles = apply_markers(src_items)
            structured = {
                "articles": articles,
                "pages": src_items,
                "body": "\n\n".join(
                    (p.get("text") or "").strip()
                    for p in src_items
                    if (p.get("text") or "").strip()
                ),
                "paragraphs": [
                    {"page": p["page"], "text": p["text"]}
                    for p in src_items
                    if (p.get("text") or "").strip()
                ],
                "meta": {
                    "title": title,
                    "author": "",
                    "language": "zh-CN",
                    "package_epub": True,
                    "epub_version": "3.0",
                },
            }
            result = HTMLConverter(
                output_dir=tmp_dir, epub_version="3.0"
            ).convert_document(structured)
            generated = result.get("epub")
            if not generated or not Path(generated).is_file():
                raise RuntimeError(result.get("epub_error") or "EPUB 打包失败")
            _sh.copy2(generated, str(out))
        finally:
            _sh.rmtree(tmp_dir, ignore_errors=True)
    else:
        _build_docx(blocks, str(out))
    return str(out)


def _drain_dialog_queue(state: dict[str, Any]) -> None:
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


def _abort_dialog_queue(state: dict[str, Any]) -> None:
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
    state: dict[str, Any],
    *,
    now: float | None = None,
    stale_since: float | None = None,
) -> tuple[bool, float | None]:
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


def _ui_js_path() -> str:
    """定位 ui/app.js（PyInstaller 冻结时从 _MEIPASS 读取）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "ui", "app.js")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "app.js")


def _ui_asset_path(rel: str) -> str:
    """定位 ui/ 下的静态资源（冻结 exe 从 _MEIPASS 读取）。

    用于 KaTeX（ui/vendor/katex.min.js）等随包分发的前端库：离线可用，
    不依赖 CDN（矫正界面可能处于无外网环境）。
    """
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "ui", *rel.split("/"))
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", *rel.split("/"))


# ---------------------------------------------------------------------------
# 系统剪贴板（Windows）：右键菜单「复制 / 粘贴」的可靠通路
#
# 浏览器 Clipboard API 的限制：navigator.clipboard.read()/readText() 需要
# clipboard-read 权限（WebView2 与未授权环境会直接拒绝，非安全上下文连
# navigator.clipboard 都不存在），writeText() 还要求焦点 + 用户激活——只用
# 浏览器 API 时，右键菜单的复制/粘贴会"点了没反应"。
# 本服务运行在本机（127.0.0.1），直接读写 Windows 系统剪贴板即可 100% 可用，
# 并且与 Word / 记事本等程序互通。非 Windows 或调用失败时返回空结果，
# 由前端回退到 Clipboard API / Ctrl+V 提示。
# ---------------------------------------------------------------------------
_CF_UNICODETEXT = 13
_CLIP_RETRY = 8          # OpenClipboard 被其他程序占用时的重试次数
_CLIP_RETRY_SLEEP = 0.05
_CF_HTML_PRE = "<html><body><!--StartFragment-->"
_CF_HTML_POST = "<!--EndFragment--></body></html>"


def _clip_api() -> tuple[Any, Any]:
    """返回 (user32, kernel32)；非 Windows 或初始化失败 → (None, None)。

    **必须同时声明 argtypes 与 restype**：句柄是指针，ctypes 默认按 32 位 int
    传递/返回，64 位下会被截断（SetClipboardData/GlobalLock 静默失败）。
    """
    if sys.platform != "win32":
        return None, None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterClipboardFormatW.restype = wintypes.UINT
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HANDLE
        kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
        kernel32.GlobalFree.restype = wintypes.HANDLE
        kernel32.GlobalSize.argtypes = [wintypes.HANDLE]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        return user32, kernel32
    except Exception:
        return None, None


def _clip_open(user32: Any) -> bool:
    """打开剪贴板（被其他程序占用时短暂重试）。"""
    for _ in range(_CLIP_RETRY):
        try:
            if user32.OpenClipboard(None):
                return True
        except Exception:
            return False
        time.sleep(_CLIP_RETRY_SLEEP)
    return False


def _cf_html_bytes(fragment: str) -> bytes:
    """构造 CF_HTML 载荷（头部偏移按规范用 UTF-8 字节数，必须准确）。"""
    frag = fragment.encode("utf-8")
    header = (
        "Version:0.9\r\n"
        "StartHTML:{0:010d}\r\n"
        "EndHTML:{1:010d}\r\n"
        "StartFragment:{2:010d}\r\n"
        "EndFragment:{3:010d}\r\n"
    )
    head_len = len(header.format(0, 0, 0, 0).encode("utf-8"))
    pre = _CF_HTML_PRE.encode("utf-8")
    post = _CF_HTML_POST.encode("utf-8")
    start_frag = head_len + len(pre)
    end_frag = start_frag + len(frag)
    end_html = end_frag + len(post)
    return (
        header.format(head_len, end_html, start_frag, end_frag).encode("utf-8")
        + pre
        + frag
        + post
    )


def _cf_html_fragment(raw: bytes) -> str:
    """从 CF_HTML 载荷中取出片段（偏移不合法时回退整体内容）。"""
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace")
    start = re.search(r"StartFragment:(\d+)", text)
    end = re.search(r"EndFragment:(\d+)", text)
    if start and end:
        try:
            return raw[int(start.group(1)): int(end.group(1))].decode(
                "utf-8", errors="replace"
            )
        except Exception:
            pass
    return text if "<" in text else ""


def _clip_set_data(user32: Any, kernel32: Any, fmt: int, data: bytes) -> bool:
    """把字节写入 HGLOBAL 并 SetClipboardData（成功后内存所有权移交系统）。"""
    import ctypes

    size = len(data) + 2  # 结尾双字节 NUL
    handle = kernel32.GlobalAlloc(0x0002, size)  # GMEM_MOVEABLE
    if not handle:
        return False
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        return False
    ctypes.memmove(ptr, data, len(data))
    ctypes.memset(ptr + len(data), 0, 2)
    kernel32.GlobalUnlock(handle)
    if not user32.SetClipboardData(fmt, handle):
        kernel32.GlobalFree(handle)
        return False
    return True


def clipboard_write(text: str, html: str = "") -> bool:
    """写系统剪贴板：text → CF_UNICODETEXT，html → CF_HTML（可选）。"""
    user32, kernel32 = _clip_api()
    if user32 is None:
        return False
    if not _clip_open(user32):
        return False
    try:
        user32.EmptyClipboard()
        ok = False
        if text:
            ok = _clip_set_data(
                user32, kernel32, _CF_UNICODETEXT, text.encode("utf-16-le") + b"\x00\x00"
            ) or ok
        if html:
            fmt = user32.RegisterClipboardFormatW("HTML Format")
            if fmt:
                ok = _clip_set_data(
                    user32, kernel32, fmt, _cf_html_bytes(html) + b"\x00"
                ) or ok
        return ok
    except Exception:
        return False
    finally:
        try:
            user32.CloseClipboard()
        except Exception:
            pass


def clipboard_snapshot(rich: bool = False) -> tuple[str, str]:
    """读系统剪贴板：返回 (纯文本, HTML 片段)；rich=False 时不读 HTML。"""
    user32, kernel32 = _clip_api()
    if user32 is None:
        return "", ""
    if not _clip_open(user32):
        return "", ""
    try:
        import ctypes

        text = ""
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if handle:
            ptr = kernel32.GlobalLock(handle)
            if ptr:
                try:
                    text = ctypes.wstring_at(ptr)
                finally:
                    kernel32.GlobalUnlock(handle)
        html = ""
        if rich:
            fmt = user32.RegisterClipboardFormatW("HTML Format")
            if fmt:
                h2 = user32.GetClipboardData(fmt)
                if h2:
                    ptr2 = kernel32.GlobalLock(h2)
                    if ptr2:
                        try:
                            size = int(kernel32.GlobalSize(h2) or 0)
                            raw = (
                                ctypes.string_at(ptr2, size)
                                if size
                                else ctypes.string_at(ptr2)
                            )
                            html = _cf_html_fragment(raw)
                        finally:
                            kernel32.GlobalUnlock(h2)
        return text, html
    except Exception:
        return "", ""
    finally:
        try:
            user32.CloseClipboard()
        except Exception:
            pass


class _CorrectionHandler(BaseHTTPRequestHandler):
    server_version = "ptoe-correct/1.0"

    # -- helpers --

    def _send(
        self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            # client disconnected or socket error; swallow to keep server alive
            pass

    def _json(self, obj: Any) -> bytes:
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")

    def _touch_heartbeat(self) -> None:
        """页面心跳：刷新存活时刻，并取消可能存在的关闭倒计时（标签页被恢复）。"""
        st = self.server.state
        st["last_heartbeat"] = time.monotonic()
        st["gone_at"] = None

    # -- GET --

    def _proofread_settings(self) -> None:
        """校对设置：GET 读取 / POST 写入 config.json proofread 段。

        管理的键：enable_llm / llm_model（LLM 深度校对，2026-08-07）+
        enable_legacy_rules（原有规则开关，2026-08-09；默认 False 时校正只跑
        三条新规则：连续重复 / 连续标点 / 中文中的连续字母）。
        服务端持久化 —— 随机端口下 localStorage 每次运行失效。"""
        try:
            from configmanage import get_config, set_proofread_param

            cfg = get_config(show_dialogs=False) or {}
            model_choices = cfg.get("model_choices") or {}
            choices = (
                sorted(model_choices.keys()) if isinstance(model_choices, dict) else []
            )
            if self.command == "GET":
                pr_cfg = cfg.get("proofread") or {}
                model_cfg = pr_cfg.get("llm_model") or ""
                if model_cfg and model_cfg not in model_choices:
                    model_cfg = (
                        ""  # 配置了未注册模型 → 视同未设置（跟随 selected_model）
                    )
                self._send(
                    200,
                    self._json(
                        {
                            "ok": True,
                            "enabled": bool(pr_cfg.get("enable_llm")),
                            "model": model_cfg,
                            "available": choices,
                            "selected": cfg.get("selected_model") or "",
                            # 原有规则开关（默认 False：校正只跑三条新规则）
                            "enable_legacy_rules": bool(
                                pr_cfg.get("enable_legacy_rules")
                            ),
                        }
                    ),
                    "application/json; charset=utf-8",
                )
                return
            # POST: {enabled: bool, model: str, enable_legacy_rules?: bool}
            # （model 空表示跟随 selected_model；enable_legacy_rules 缺省则不改动该键）
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8"))
            enabled = bool(body.get("enabled"))
            model = str(body.get("model") or "").strip()
            if model and model not in model_choices:
                self._send(
                    400,
                    self._json(
                        {
                            "ok": False,
                            "error": f"模型 '{model}' 未在配置中注册（可用: {', '.join(choices) or '无'}）",
                        }
                    ),
                    "application/json; charset=utf-8",
                )
                return
            legacy_given = "enable_legacy_rules" in body
            legacy_val = body.get("enable_legacy_rules")
            if legacy_given and not isinstance(legacy_val, bool):
                self._send(
                    400,
                    self._json(
                        {
                            "ok": False,
                            "error": "enable_legacy_rules 必须是布尔值（true/false）",
                        }
                    ),
                    "application/json; charset=utf-8",
                )
                return
            set_proofread_param("enable_llm", enabled)
            set_proofread_param("llm_model", model)
            if legacy_given:
                set_proofread_param("enable_legacy_rules", bool(legacy_val))
            self._send(200, self._json({"ok": True}), "application/json; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            self._send(
                500,
                self._json({"ok": False, "error": str(e)}),
                "application/json; charset=utf-8",
            )

    def _shortcuts(self) -> None:
        """快捷键绑定：GET 读取 / POST 写入 config.json 顶层 shortcuts。

        服务端持久化 —— correct_pages 每次运行随机端口，localStorage 按 origin
        隔离会导致设置每运行失效（与 LLM 深度校对设置同因，2026-08-09 修复）。
        """
        try:
            from configmanage import get_config, set_shortcuts

            if self.command == "GET":
                cfg = get_config(show_dialogs=False) or {}
                sc = cfg.get("shortcuts")
                if not isinstance(sc, dict):
                    sc = {}
                self._send(
                    200,
                    self._json({"ok": True, "shortcuts": sc}),
                    "application/json; charset=utf-8",
                )
                return
            # POST: {shortcuts: {op: combo}}
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8"))
            sc = body.get("shortcuts")
            if not isinstance(sc, dict):
                self._send(
                    400,
                    self._json(
                        {"ok": False, "error": "shortcuts 必须是对象（op -> 组合键）"}
                    ),
                    "application/json; charset=utf-8",
                )
                return
            if len(sc) > 100:
                self._send(
                    400,
                    self._json({"ok": False, "error": "快捷键条目过多（上限 100 条）"}),
                    "application/json; charset=utf-8",
                )
                return
            for k, v in sc.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    self._send(
                        400,
                        self._json({"ok": False, "error": "快捷键键值必须均为字符串"}),
                        "application/json; charset=utf-8",
                    )
                    return
            set_shortcuts(sc)
            self._send(200, self._json({"ok": True}), "application/json; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            self._send(
                500,
                self._json({"ok": False, "error": str(e)}),
                "application/json; charset=utf-8",
            )

    def _mouse_shortcuts(self) -> None:
        """鼠标手势绑定：GET 读取 / POST 写入 config.json 顶层 mouse_shortcuts。

        服务端持久化 —— correct_pages 每次运行随机端口，localStorage 按 origin
        隔离会导致设置每运行失效（与快捷键绑定同因）。
        """
        try:
            from configmanage import get_config, set_mouse_shortcuts

            if self.command == "GET":
                cfg = get_config(show_dialogs=False) or {}
                sc = cfg.get("mouse_shortcuts")
                if not isinstance(sc, dict):
                    sc = {}
                self._send(
                    200,
                    self._json({"ok": True, "mouse_shortcuts": sc}),
                    "application/json; charset=utf-8",
                )
                return
            # POST: {mouse_shortcuts: {op: gesture}}
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8"))
            sc = body.get("mouse_shortcuts")
            if not isinstance(sc, dict):
                self._send(
                    400,
                    self._json(
                        {
                            "ok": False,
                            "error": "mouse_shortcuts 必须是对象（op -> 手势）",
                        }
                    ),
                    "application/json; charset=utf-8",
                )
                return
            if len(sc) > 100:
                self._send(
                    400,
                    self._json(
                        {"ok": False, "error": "鼠标动作条目过多（上限 100 条）"}
                    ),
                    "application/json; charset=utf-8",
                )
                return
            for k, v in sc.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    self._send(
                        400,
                        self._json(
                            {"ok": False, "error": "鼠标动作键值必须均为字符串"}
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
            set_mouse_shortcuts(sc)
            self._send(200, self._json({"ok": True}), "application/json; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            self._send(
                500,
                self._json({"ok": False, "error": str(e)}),
                "application/json; charset=utf-8",
            )

    def _ui_settings(self) -> None:
        """矫正界面 UI 偏好：GET 读取 / POST 写入 config.json 顶层 ui_settings。

        服务端持久化 —— correct_pages 每次运行随机端口，localStorage 按 origin
        隔离会导致设置每运行失效（与 shortcuts / proofread_settings 同因）。
        """
        try:
            from configmanage import (
                get_config,
                set_ui_settings,
                DEFAULT_CONFIG,
                POPUP_ELIGIBLE_OPS,
                POPUP_ROW1_DEFAULT,
                POPUP_ROW2_DEFAULT,
                POPUP_RULE_COUNT_DEFAULT,
                POPUP_RULE_COUNT_MIN,
                POPUP_RULE_COUNT_MAX,
                UNDO_DEPTH_DEFAULT,
                UNDO_DEPTH_CHOICES,
                SEARCH_MAX_RESULTS_DEFAULT,
                SEARCH_MAX_RESULTS_MIN,
                SEARCH_MAX_RESULTS_MAX,
                LINE_NUM_SIZE_DEFAULT,
                LINE_NUM_SIZE_CHOICES,
                CLEAN_SYMBOL_MAP_DEFAULT,
                CLEAN_DIGIT_PUNCT_MAP_DEFAULT,
                _CLEAN_DIGIT_PUNCT_MAP_MAX_ITEMS,
            )

            if self.command == "GET":
                cfg = get_config(show_dialogs=False) or {}
                stored = cfg.get("ui_settings")
                if not isinstance(stored, dict):
                    stored = {}
                defaults = DEFAULT_CONFIG.get("ui_settings", {
                    "tip_delay": 600,
                    "err_hover_delay": 1500,
                    "brush_merge_delay": 800,
                    "brush_merge_on_leave": True,
                    "editor_font_size": 14,
                    "img_mode": "",
                    "rule_all_pages_confirm": True,
                    "popup_row1": POPUP_ROW1_DEFAULT,
                    "popup_row2": POPUP_ROW2_DEFAULT,
                    "popup_rule_count": POPUP_RULE_COUNT_DEFAULT,
                    "proofread_batch_pages": 0,
                    "undo_depth": UNDO_DEPTH_DEFAULT,
                    "search_max_results": SEARCH_MAX_RESULTS_DEFAULT,
                    "show_line_numbers": False,
                    # 纠错序号角标显示（2026-10-29）：序号用于 N Enter / N Delete
                    # 按序应用或取消，显示只是视觉开关，默认开。与 configmanage
                    # DEFAULT_CONFIG["ui_settings"] 同名同默认。
                    "show_err_number": True,
                    # Esc 后自动切英文输入法（2026-10-30）：默认开（只切输入法内部的
                    # 中/英模式，不换键盘布局）。与 configmanage
                    # DEFAULT_CONFIG["ui_settings"] 同名同默认。
                    "ime_switch_on_esc": True,
                    "line_num_size": LINE_NUM_SIZE_DEFAULT,
                    "text_justify": True,
                    "clean_punctuation": True,
                    "clean_symbols": True,
                    "clean_md_tags": True,
                    "clean_html_tags": True,
                    "clean_paragraphs": False,
                    "clean_digit_punct": True,
                    # 导出时清理空白行（2026-10）：默认关；只作用于导出
                    "clean_blank_lines": False,
                    # 清理段落内空白（2026-10-21）：默认关；保留英文单词间空格默认开
                    "clean_spaces": False,
                    "clean_spaces_keep_word_gap": True,
                    "proofread_auto_jump": True,
                    "tbl_popup_menu": False,
                    "tbl_ctx_menu": False,
                    "tbl_tbl_menu": False,
                })
                merged = {
                    "tip_delay": stored.get("tip_delay", defaults["tip_delay"]),
                    "err_hover_delay": stored.get("err_hover_delay", defaults["err_hover_delay"]),
                    "brush_merge_delay": stored.get("brush_merge_delay", defaults.get("brush_merge_delay", 800)),
                    "brush_merge_on_leave": bool(stored.get("brush_merge_on_leave", defaults.get("brush_merge_on_leave", True))),
                    "editor_font_size": stored.get("editor_font_size", defaults["editor_font_size"]),
                    "img_mode": stored.get("img_mode", defaults["img_mode"]),
                    "rule_all_pages_confirm": stored.get("rule_all_pages_confirm", defaults["rule_all_pages_confirm"]),
                    "popup_row1": stored.get("popup_row1", defaults["popup_row1"]),
                    "popup_row2": stored.get("popup_row2", defaults["popup_row2"]),
                    "popup_rule_count": stored.get("popup_rule_count", defaults["popup_rule_count"]),
                    "proofread_batch_pages": stored.get("proofread_batch_pages", defaults.get("proofread_batch_pages", 0)),
                    "undo_depth": stored.get("undo_depth", defaults.get("undo_depth", UNDO_DEPTH_DEFAULT)),
                    "search_max_results": stored.get("search_max_results", defaults.get("search_max_results", SEARCH_MAX_RESULTS_DEFAULT)),
                    "show_line_numbers": bool(stored.get("show_line_numbers", defaults.get("show_line_numbers", False))),
                    "show_err_number": bool(stored.get("show_err_number", defaults.get("show_err_number", True))),
                    "ime_switch_on_esc": bool(stored.get("ime_switch_on_esc", defaults.get("ime_switch_on_esc", True))),
                    "line_num_size": stored.get("line_num_size", defaults.get("line_num_size", LINE_NUM_SIZE_DEFAULT)),
                    "text_justify": bool(stored.get("text_justify", defaults.get("text_justify", True))),
                    "clean_punctuation": bool(stored.get("clean_punctuation", defaults.get("clean_punctuation", True))),
                    "clean_symbols": bool(stored.get("clean_symbols", defaults.get("clean_symbols", True))),
                    "clean_md_tags": bool(stored.get("clean_md_tags", defaults.get("clean_md_tags", True))),
                    "clean_html_tags": bool(stored.get("clean_html_tags", defaults.get("clean_html_tags", True))),
                    "clean_paragraphs": bool(stored.get("clean_paragraphs", defaults.get("clean_paragraphs", False))),
                    "clean_symbol_map": stored.get("clean_symbol_map", defaults.get("clean_symbol_map", CLEAN_SYMBOL_MAP_DEFAULT)),
                    "clean_digit_punct": bool(stored.get("clean_digit_punct", defaults.get("clean_digit_punct", True))),
                    "clean_digit_punct_map": stored.get("clean_digit_punct_map", defaults.get("clean_digit_punct_map", CLEAN_DIGIT_PUNCT_MAP_DEFAULT)),
                    # 导出时清理空白行（2026-10）：默认关；只作用于导出
                    "clean_blank_lines": bool(stored.get("clean_blank_lines", defaults.get("clean_blank_lines", False))),
                    # 清理段落内空白（2026-10-21）：删空白并合并 OCR 软换行（对齐段保留换行）
                    "clean_spaces": bool(stored.get("clean_spaces", defaults.get("clean_spaces", False))),
                    "clean_spaces_keep_word_gap": bool(stored.get("clean_spaces_keep_word_gap", defaults.get("clean_spaces_keep_word_gap", True))),
                    "proofread_auto_jump": bool(stored.get("proofread_auto_jump", defaults.get("proofread_auto_jump", True))),
                    # 表格内浮层菜单开关（2026-10）：只影响表格范围，默认全关
                    "tbl_popup_menu": bool(stored.get("tbl_popup_menu", defaults.get("tbl_popup_menu", False))),
                    "tbl_ctx_menu": bool(stored.get("tbl_ctx_menu", defaults.get("tbl_ctx_menu", False))),
                    "tbl_tbl_menu": bool(stored.get("tbl_tbl_menu", defaults.get("tbl_tbl_menu", False))),
                }
                self._send(
                    200,
                    self._json({"ok": True, "ui_settings": merged}),
                    "application/json; charset=utf-8",
                )
                return
            # POST: {ui_settings: {...}}
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8"))
            ui = body.get("ui_settings")
            if not isinstance(ui, dict):
                self._send(
                    400,
                    self._json({"ok": False, "error": "ui_settings 必须是对象"}),
                    "application/json; charset=utf-8",
                )
                return
            # 逐键校验（非法 → 400，不落盘）
            clean = {}
            if "tip_delay" in ui:
                try:
                    v = int(ui["tip_delay"])
                except (TypeError, ValueError):
                    self._send(400, self._json({"ok": False, "error": "tip_delay 必须是数字"}), "application/json; charset=utf-8")
                    return
                if not (0 <= v <= 5000):
                    self._send(400, self._json({"ok": False, "error": "tip_delay 超出范围（0-5000）"}), "application/json; charset=utf-8")
                    return
                clean["tip_delay"] = v
            if "err_hover_delay" in ui:
                try:
                    v = int(ui["err_hover_delay"])
                except (TypeError, ValueError):
                    self._send(400, self._json({"ok": False, "error": "err_hover_delay 必须是数字"}), "application/json; charset=utf-8")
                    return
                if not (0 <= v <= 10000):
                    self._send(400, self._json({"ok": False, "error": "err_hover_delay 超出范围（0-10000）"}), "application/json; charset=utf-8")
                    return
                clean["err_hover_delay"] = v
            if "brush_merge_delay" in ui:
                try:
                    v = int(ui["brush_merge_delay"])
                except (TypeError, ValueError):
                    self._send(400, self._json({"ok": False, "error": "brush_merge_delay 必须是数字"}), "application/json; charset=utf-8")
                    return
                if not (0 <= v <= 5000):
                    self._send(400, self._json({"ok": False, "error": "brush_merge_delay 超出范围（0-5000）"}), "application/json; charset=utf-8")
                    return
                clean["brush_merge_delay"] = v
            if "editor_font_size" in ui:
                try:
                    v = int(ui["editor_font_size"])
                except (TypeError, ValueError):
                    self._send(400, self._json({"ok": False, "error": "editor_font_size 必须是数字"}), "application/json; charset=utf-8")
                    return
                if not (10 <= v <= 28):
                    self._send(400, self._json({"ok": False, "error": "editor_font_size 超出范围（10-28）"}), "application/json; charset=utf-8")
                    return
                clean["editor_font_size"] = v
            if "img_mode" in ui:
                # 值域与前端 IMG_MODES / configmanage.IMG_MODE_CHOICES 保持一致（含 2 种绕排）
                v = str(ui["img_mode"])
                if v not in IMG_MODE_CHOICES:
                    self._send(400, self._json({"ok": False, "error": "img_mode 取值无效"}), "application/json; charset=utf-8")
                    return
                clean["img_mode"] = v
            if "rule_all_pages_confirm" in ui:
                v = ui["rule_all_pages_confirm"]
                if not isinstance(v, bool):
                    self._send(400, self._json({"ok": False, "error": "rule_all_pages_confirm 必须是布尔值"}), "application/json; charset=utf-8")
                    return
                clean["rule_all_pages_confirm"] = v
            if "popup_row1" in ui:
                v = ui["popup_row1"]
                if not (
                    isinstance(v, list)
                    and all(isinstance(x, str) and x in POPUP_ELIGIBLE_OPS for x in v)
                    and len(v) == len(set(v))
                    and len(v) <= 25
                ):
                    self._send(400, self._json({"ok": False, "error": "弹出菜单第一行按钮配置无效"}), "application/json; charset=utf-8")
                    return
                clean["popup_row1"] = list(v)
            if "popup_row2" in ui:
                v = ui["popup_row2"]
                if not (
                    isinstance(v, list)
                    and all(isinstance(x, str) and x in POPUP_ELIGIBLE_OPS for x in v)
                    and len(v) == len(set(v))
                    and len(v) <= 25
                ):
                    self._send(400, self._json({"ok": False, "error": "弹出菜单第二行按钮配置无效"}), "application/json; charset=utf-8")
                    return
                clean["popup_row2"] = list(v)
            if "popup_rule_count" in ui:
                v = ui["popup_rule_count"]
                if not (
                    isinstance(v, int)
                    and not isinstance(v, bool)
                    and POPUP_RULE_COUNT_MIN <= v <= POPUP_RULE_COUNT_MAX
                ):
                    self._send(400, self._json({"ok": False, "error": "弹出菜单规则按钮数量无效（需为 0-10 的整数）"}), "application/json; charset=utf-8")
                    return
                clean["popup_rule_count"] = v
            if "proofread_batch_pages" in ui:
                try:
                    v = int(ui["proofread_batch_pages"])
                except (TypeError, ValueError):
                    self._send(400, self._json({"ok": False, "error": "proofread_batch_pages 必须是数字"}), "application/json; charset=utf-8")
                    return
                if not (0 <= v <= 50):
                    self._send(400, self._json({"ok": False, "error": "proofread_batch_pages 超出范围（0-50，0=全部页）"}), "application/json; charset=utf-8")
                    return
                clean["proofread_batch_pages"] = v
            if "proofread_auto_jump" in ui:
                v = ui["proofread_auto_jump"]
                if not isinstance(v, bool):
                    self._send(400, self._json({"ok": False, "error": "proofread_auto_jump 必须是布尔值"}), "application/json; charset=utf-8")
                    return
                clean["proofread_auto_jump"] = v
            # 表格内浮层菜单开关（2026-10）：三个独立布尔键，非法 → 400 不落盘
            for _tk in ("tbl_popup_menu", "tbl_ctx_menu", "tbl_tbl_menu"):
                if _tk in ui:
                    v = ui[_tk]
                    if not isinstance(v, bool):
                        self._send(400, self._json({"ok": False, "error": f"{_tk} 必须是布尔值"}), "application/json; charset=utf-8")
                        return
                    clean[_tk] = v
            if "undo_depth" in ui:
                v = ui["undo_depth"]
                if not (isinstance(v, int) and not isinstance(v, bool) and v in UNDO_DEPTH_CHOICES):
                    self._send(400, self._json({"ok": False, "error": "undo_depth 取值无效（需为 50/100/200/500）"}), "application/json; charset=utf-8")
                    return
                clean["undo_depth"] = v
            if "search_max_results" in ui:
                # 搜索结果列表显示上限：10-1000 的整数（与 proofread_batch_pages 同口径，越界 400）
                if not isinstance(ui["search_max_results"], int) or isinstance(ui["search_max_results"], bool):
                    self._send(400, self._json({"ok": False, "error": "search_max_results 必须是数字"}), "application/json; charset=utf-8")
                    return
                if not (SEARCH_MAX_RESULTS_MIN <= ui["search_max_results"] <= SEARCH_MAX_RESULTS_MAX):
                    self._send(400, self._json({"ok": False, "error": "search_max_results 超出范围（%d-%d）" % (SEARCH_MAX_RESULTS_MIN, SEARCH_MAX_RESULTS_MAX)}), "application/json; charset=utf-8")
                    return
                clean["search_max_results"] = ui["search_max_results"]
            if "show_line_numbers" in ui:
                v = ui["show_line_numbers"]
                if not isinstance(v, bool):
                    self._send(400, self._json({"ok": False, "error": "show_line_numbers 必须是布尔值"}), "application/json; charset=utf-8")
                    return
                clean["show_line_numbers"] = v
            if "show_err_number" in ui:
                v = ui["show_err_number"]
                if not isinstance(v, bool):
                    self._send(400, self._json({"ok": False, "error": "show_err_number 必须是布尔值"}), "application/json; charset=utf-8")
                    return
                clean["show_err_number"] = v
            if "ime_switch_on_esc" in ui:
                v = ui["ime_switch_on_esc"]
                if not isinstance(v, bool):
                    self._send(400, self._json({"ok": False, "error": "ime_switch_on_esc 必须是布尔值"}), "application/json; charset=utf-8")
                    return
                clean["ime_switch_on_esc"] = v
            if "line_num_size" in ui:
                v = ui["line_num_size"]
                if v not in LINE_NUM_SIZE_CHOICES:
                    self._send(400, self._json({"ok": False, "error": "line_num_size 取值无效（需为 small/medium/large）"}), "application/json; charset=utf-8")
                    return
                clean["line_num_size"] = v
            if "text_justify" in ui:
                v = ui["text_justify"]
                if not isinstance(v, bool):
                    self._send(400, self._json({"ok": False, "error": "text_justify 必须是布尔值"}), "application/json; charset=utf-8")
                    return
                clean["text_justify"] = v
            for _clean_key in (
                "clean_punctuation",
                "clean_symbols",
                "clean_md_tags",
                "clean_html_tags",
                "clean_paragraphs",
                "clean_digit_punct",
                "brush_merge_on_leave",
                "clean_blank_lines",
                "clean_spaces",
                "clean_spaces_keep_word_gap",
            ):
                if _clean_key in ui:
                    v = ui[_clean_key]
                    if not isinstance(v, bool):
                        self._send(400, self._json({"ok": False, "error": f"{_clean_key} 必须是布尔值"}), "application/json; charset=utf-8")
                        return
                    clean[_clean_key] = v
            if "clean_symbol_map" in ui:
                v = ui["clean_symbol_map"]
                if not isinstance(v, list) or not v:
                    self._send(400, self._json({"ok": False, "error": "clean_symbol_map 必须是数组"}), "application/json; charset=utf-8")
                    return
                _seen = set()
                for item in v:
                    if not isinstance(item, list) or len(item) != 2 or not all(isinstance(x, str) and x for x in item) or item[0] in _seen:
                        self._send(400, self._json({"ok": False, "error": "clean_symbol_map 格式无效（每项为 [源, 目标] 非空字符串，源不可重复）"}), "application/json; charset=utf-8")
                        return
                    _seen.add(item[0])
                if len(v) > 32:
                    self._send(400, self._json({"ok": False, "error": "clean_symbol_map 最多 32 项"}), "application/json; charset=utf-8")
                    return
                clean["clean_symbol_map"] = [list(p) for p in v]
            if "clean_digit_punct_map" in ui:
                v = ui["clean_digit_punct_map"]
                if not isinstance(v, list) or not v:
                    self._send(400, self._json({"ok": False, "error": "clean_digit_punct_map 必须是数组"}), "application/json; charset=utf-8")
                    return
                _seen = set()
                for item in v:
                    if (
                        not isinstance(item, list)
                        or len(item) != 2
                        or not all(isinstance(x, str) and x for x in item)
                        or len(item[0]) != 1
                        or item[0] in _seen
                    ):
                        self._send(400, self._json({"ok": False, "error": "clean_digit_punct_map 格式无效（每项为 [单字符源, 目标] 非空字符串，源不可重复）"}), "application/json; charset=utf-8")
                        return
                    _seen.add(item[0])
                if len(v) > _CLEAN_DIGIT_PUNCT_MAP_MAX_ITEMS:
                    self._send(400, self._json({"ok": False, "error": "clean_digit_punct_map 最多 %d 项" % _CLEAN_DIGIT_PUNCT_MAP_MAX_ITEMS}), "application/json; charset=utf-8")
                    return
                clean["clean_digit_punct_map"] = [list(p) for p in v]
            set_ui_settings(clean)
            self._send(200, self._json({"ok": True}), "application/json; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            self._send(
                500,
                self._json({"ok": False, "error": str(e)}),
                "application/json; charset=utf-8",
            )

    def _auto_save(self) -> None:
        """矫正界面自动保存设置：GET 读取 / POST 写入 config.json 顶层 auto_save。

        服务端持久化 —— correct_pages 每次运行随机端口，localStorage 按 origin
        隔离会导致设置每运行失效（与 shortcuts / ui_settings 同因，2026-09）。
        POST 接受 {auto_save: {...}} 或扁平键 {enabled, interval_minutes, new_backup}。
        """
        try:
            from configmanage import get_config, set_auto_save, DEFAULT_CONFIG

            if self.command == "GET":
                cfg = get_config(show_dialogs=False) or {}
                stored = cfg.get("auto_save")
                if not isinstance(stored, dict):
                    stored = {}
                defaults = DEFAULT_CONFIG.get(
                    "auto_save",
                    {"enabled": True, "interval_minutes": 5, "new_backup": True},
                )
                merged = {
                    "enabled": stored.get("enabled", defaults["enabled"]),
                    "interval_minutes": stored.get(
                        "interval_minutes", defaults["interval_minutes"]
                    ),
                    "new_backup": stored.get("new_backup", defaults["new_backup"]),
                }
                self._send(
                    200,
                    self._json({"ok": True, "auto_save": merged}),
                    "application/json; charset=utf-8",
                )
                return
            # POST: {auto_save: {...}} 或扁平键
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                self._send(
                    400,
                    self._json({"ok": False, "error": "请求体必须为 JSON 对象"}),
                    "application/json; charset=utf-8",
                )
                return
            ui = body.get("auto_save")
            if ui is None and any(
                k in body for k in ("enabled", "interval_minutes", "new_backup")
            ):
                ui = body
            if not isinstance(ui, dict):
                self._send(
                    400,
                    self._json({"ok": False, "error": "auto_save 必须是对象"}),
                    "application/json; charset=utf-8",
                )
                return
            # 逐键严格校验（非法 → 400，不落盘）
            clean = {}
            if "enabled" in ui:
                v = ui["enabled"]
                if not isinstance(v, bool):
                    self._send(
                        400,
                        self._json({"ok": False, "error": "enabled 必须是布尔值"}),
                        "application/json; charset=utf-8",
                    )
                    return
                clean["enabled"] = v
            if "interval_minutes" in ui:
                v = ui["interval_minutes"]
                if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 60):
                    self._send(
                        400,
                        self._json(
                            {"ok": False, "error": "interval_minutes 必须是 1-60 的整数"}
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                clean["interval_minutes"] = v
            if "new_backup" in ui:
                v = ui["new_backup"]
                if not isinstance(v, bool):
                    self._send(
                        400,
                        self._json({"ok": False, "error": "new_backup 必须是布尔值"}),
                        "application/json; charset=utf-8",
                    )
                    return
                clean["new_backup"] = v
            cfg = set_auto_save(clean)
            merged = ((cfg or {}).get("auto_save")) or clean
            self._send(
                200,
                self._json({"ok": True, "auto_save": merged}),
                "application/json; charset=utf-8",
            )
        except Exception as e:  # noqa: BLE001
            self._send(
                500,
                self._json({"ok": False, "error": str(e)}),
                "application/json; charset=utf-8",
            )

    def _replace_rules(self) -> None:
        """替换规则管理：GET 读取 / POST 写入 config.json 顶层 replace_rules。

        服务端持久化（与 shortcuts / format_rules 同因）。规则结构
        {id, name, pattern, flags, keep_groups, replacement, builtin, enabled}；
        校验失败返回 400 中文错误，不落盘。
        """
        try:
            from configmanage import (
                get_config,
                set_replace_rules,
                DEFAULT_CONFIG,
            )

            if self.command == "GET":
                cfg = get_config(show_dialogs=False) or {}
                rules = cfg.get("replace_rules")
                if not isinstance(rules, list):
                    rules = list(DEFAULT_CONFIG.get("replace_rules", []))
                self._send(
                    200,
                    self._json({"ok": True, "rules": rules}),
                    "application/json; charset=utf-8",
                )
                return
            # POST: {rules: [...]}
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                self._send(
                    400,
                    self._json({"ok": False, "error": "请求体必须为 JSON 对象"}),
                    "application/json; charset=utf-8",
                )
                return
            rules = body.get("rules")
            if not isinstance(rules, list):
                self._send(
                    400,
                    self._json({"ok": False, "error": "rules 必须是数组"}),
                    "application/json; charset=utf-8",
                )
                return
            clean = set_replace_rules(rules)
            self._send(
                200,
                self._json({"ok": True, "rules": clean}),
                "application/json; charset=utf-8",
            )
        except ValueError as e:  # 校验失败 → 400（中文信息）
            self._send(
                400,
                self._json({"ok": False, "error": str(e)}),
                "application/json; charset=utf-8",
            )
        except Exception as e:  # noqa: BLE001
            self._send(
                500,
                self._json({"ok": False, "error": str(e)}),
                "application/json; charset=utf-8",
            )

    def _correction_format(self) -> None:
        """校正格式清理设置：GET 读取 / POST 写入 config.json 顶层 correction_format。

        结构 {enabled, strip_markdown, strip_latex, remove_formula, custom_rules}。
        POST 两种模式：
          - 预览：body 含字符串 preview 时，用提交的设置（不落盘）清理该文本，
            返回 {ok, preview}（供设置界面实时预览）。
          - 保存：否则校验并落盘，返回 {ok, settings}。
        校验失败返回 400（中文信息），不落盘。清理逻辑见 clean_correction_format。
        """
        try:
            from configmanage import (
                get_correction_format,
                normalize_correction_format,
                set_correction_format,
            )

            if self.command == "GET":
                self._send(
                    200,
                    self._json({"ok": True, "settings": get_correction_format()}),
                    "application/json; charset=utf-8",
                )
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                self._send(
                    400,
                    self._json({"ok": False, "error": "请求体必须为 JSON 对象"}),
                    "application/json; charset=utf-8",
                )
                return
            if isinstance(body.get("preview"), str):
                # 预览：用提交的设置（未落盘）清理给定文本
                opts = normalize_correction_format(body)
                preview = clean_correction_format(
                    body["preview"],
                    strip_markdown=opts.get("strip_markdown", True),
                    strip_latex=opts.get("strip_latex", True),
                    remove_formula=opts.get("remove_formula", False),
                    custom_rules=opts.get("custom_rules"),
                )
                self._send(
                    200,
                    self._json({"ok": True, "preview": preview}),
                    "application/json; charset=utf-8",
                )
                return
            settings = set_correction_format(body)
            self._send(
                200,
                self._json({"ok": True, "settings": settings}),
                "application/json; charset=utf-8",
            )
        except ValueError as e:  # 校验失败 → 400（中文信息）
            self._send(
                400,
                self._json({"ok": False, "error": str(e)}),
                "application/json; charset=utf-8",
            )
        except Exception as e:  # noqa: BLE001
            self._send(
                500,
                self._json({"ok": False, "error": str(e)}),
                "application/json; charset=utf-8",
            )

    def _autosave_action(self) -> None:
        """自动保存动作：POST /api/autosave。

        载荷与 /api/save 相同（{pages, proofread, last_proofread_page, name,
        display_name}，.get 容错）。与 save 的唯一区别：总是**新建**一个历史
        版本文件（复用 _write_history_version 的 20 版上限/清理机制），供前端
        后台静默备份当前进度；不改动 state["dirty"] 等前端标志（由浏览器自行
        维护其脏状态语义）。不弹任何对话框。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                self._send(
                    400,
                    self._json({"ok": False, "error": "请求体必须为 JSON 对象"}),
                    "application/json; charset=utf-8",
                )
                return
            items = body.get("pages")
            if items is None:
                items = []
            if not isinstance(items, list):
                self._send(
                    400,
                    self._json({"ok": False, "error": "pages 必须是数组"}),
                    "application/json; charset=utf-8",
                )
                return
            state = self.server.state
            # 与 /api/save 一致：写 pages 与「构建快照」共用一把锁（pages_lock），
            # 保证与 /api/pages 读取、/api/history/load 写入互斥。
            saved = 0
            lock = state.get("pages_lock")
            with lock if lock is not None else nullcontext():
                for item in items:
                    try:
                        n = int(item.get("page"))
                    except (TypeError, ValueError):
                        continue
                    state["pages"][n] = sanitize_html(
                        str(item.get("html") or "")
                    )
                    saved += 1
                pages_snapshot = dict(state["pages"])
            # 历史记录名 / 重命名显示名（无文件模式打开历史版本后沿用）
            if body.get("name"):
                state["history_name"] = str(body.get("name"))
            if body.get("display_name"):
                state["display_name"] = str(body.get("display_name"))
            # 文字纠错状态随历史缓存落盘（与 save 同构，.get 容错）
            if body.get("proofread"):
                state["proofread"] = {
                    "errors": body["proofread"].get("errors") or {},
                    "original": body["proofread"].get("original") or {},
                    "dismissed": body["proofread"].get("dismissed") or {},
                }
            if body.get("last_proofread_page") is not None:
                try:
                    state["last_proofread_page"] = int(
                        body["last_proofread_page"]
                    )
                except (TypeError, ValueError):
                    pass
            # 自动保存：总是新建历史版本（后台静默）
            # S4：写入失败返回 False → 前端报错提示（不静默丢数据）
            ok = _write_history_version(state)
            payload = {"ok": ok, "saved": saved}
            if not ok:
                payload["error"] = "历史缓存写入失败（磁盘错误或权限不足？）"
            self._send(200, self._json(payload), "application/json; charset=utf-8")
        except Exception as e:  # noqa: BLE001 — 界面出错要回给浏览器而不是崩溃
            self._send(
                500,
                self._json({"ok": False, "error": str(e)}),
                "application/json; charset=utf-8",
            )

    def _config(self) -> None:
        """字体/界面配置：POST 写入 config.json fonts + citationItalicEnabled + table_convert。

        与 _shortcuts/_proofread_settings 同构：get_config 读取 → 合并 →
        update_config 原子写回。仅在确有变更时写盘。
        """
        try:
            from configmanage import get_config, update_config

            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8"))
            fonts = body.get("fonts")
            citationItalicEnabled = body.get("citationItalicEnabled")
            if fonts is not None and not isinstance(fonts, dict):
                self._send(
                    400,
                    self._json({"ok": False, "error": "fonts 必须是对象"}),
                    "application/json; charset=utf-8",
                )
                return
            if citationItalicEnabled is not None and not isinstance(
                citationItalicEnabled, bool
            ):
                self._send(
                    400,
                    self._json(
                        {"ok": False, "error": "citationItalicEnabled 必须是布尔值"}
                    ),
                    "application/json; charset=utf-8",
                )
                return
            # 识别文字表格自动转原生表格（2026-10）：顶层 table_convert，与主设置同一开关
            tableConvert = body.get("table_convert")
            if tableConvert is not None and not isinstance(tableConvert, bool):
                self._send(
                    400,
                    self._json({"ok": False, "error": "table_convert 必须是布尔值"}),
                    "application/json; charset=utf-8",
                )
                return
            cfg = get_config(show_dialogs=False) or {}
            changed = False
            if fonts is not None:
                cur = cfg.get("fonts")
                if not isinstance(cur, dict):
                    cur = {}
                merged = dict(cur)
                for k in ("body", "heading", "note", "citation"):
                    if k in fonts:
                        merged[k] = str(fonts[k])
                if merged != cur:
                    cfg["fonts"] = merged
                    changed = True
            if citationItalicEnabled is not None:
                if cfg.get("citationItalicEnabled") != citationItalicEnabled:
                    cfg["citationItalicEnabled"] = citationItalicEnabled
                    changed = True
            if tableConvert is not None:
                if cfg.get("table_convert") != tableConvert:
                    cfg["table_convert"] = tableConvert
                    changed = True
            if changed:
                update_config("fonts", cfg.get("fonts"))
                update_config(
                    "citationItalicEnabled", cfg.get("citationItalicEnabled")
                )
                if tableConvert is not None:
                    update_config("table_convert", cfg.get("table_convert"))
            self._send(200, self._json({"ok": True}), "application/json; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            self._send(
                500,
                self._json({"ok": False, "error": str(e)}),
                "application/json; charset=utf-8",
            )

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, _UI_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/tabhost":
            # 标签页宿主页面：由 tabmanage 提供 HTML（含冻结 exe 兜底）
            try:
                import tabmanage  # noqa: PLC0415

                html_bytes = tabmanage.tab_host_html()
            except Exception:
                html_bytes = b"<meta charset='utf-8'><body>\xe7\x95\x8c\xe9\x9d\xa2\xe5\x8a\xa0\xe8\xbd\xbd\xe5\xa4\xb1\xe8\xb4\xa5</body>"
            self._send(200, html_bytes, "text/html; charset=utf-8")
            return
        if path == "/api/tabs":
            # 标签栏数据：tabs 列表 + position
            try:
                import tabmanage  # noqa: PLC0415

                payload = tabmanage.tabs_payload()
            except Exception:
                payload = {"ok": True, "tabs": [], "position": "top"}
            self._send(200, self._json(payload), "application/json; charset=utf-8")
            return
        if path == "/ui/app.js":
            # 矫正界面外部脚本（2026-08-23 从内联 <script> 抽出，便于 node --check）
            js_path = _ui_js_path()
            try:
                with open(js_path, "rb") as f:
                    data = f.read()
            except OSError:
                self._send(404, b"// ui/app.js missing", "application/javascript; charset=utf-8")
            else:
                self._send(200, data, "application/javascript; charset=utf-8")
            return
        if path.startswith("/ui/vendor/"):
            # 随包分发的前端库（2026-10-21：KaTeX，LaTeX 公式渲染成 MathML）。
            # 离线可用——矫正界面可能在无外网环境，不能依赖 CDN。
            rel = path[len("/ui/vendor/"):]
            # 只允许 vendor 下的单层文件名，杜绝 ../ 穿越
            if not rel or "/" in rel or "\\" in rel or rel.startswith("."):
                self._send(404, b"// not found", "application/javascript; charset=utf-8")
                return
            try:
                with open(_ui_asset_path("vendor/" + rel), "rb") as f:
                    data = f.read()
            except OSError:
                self._send(404, b"// not found", "application/javascript; charset=utf-8")
            else:
                self._send(200, data, "application/javascript; charset=utf-8")
            return
        if path == "/api/heartbeat":
            self._touch_heartbeat()
            self._send(204, b"", "text/plain")
            return
        if path == "/api/ping":
            # 轻量存活探测：仅返回 ok，不修改任何状态。供外部（如 GUI）
            # 发现并恢复已存活的矫正界面。
            self._send(200, self._json({"ok": True}), "application/json; charset=utf-8")
            return
        if path == "/api/clipboard":
            # 读系统剪贴板：右键菜单「粘贴」的兜底通路（浏览器 Clipboard API 的
            # read()/readText() 需要 clipboard-read 权限，WebView2/未授权环境会被拒，
            # 表现为"点了粘贴没反应"）。?rich=1 时同时返回 HTML 片段。
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            text, html = clipboard_snapshot(rich="rich=1" in query)
            self._send(
                200,
                self._json({"ok": bool(text or html), "text": text, "html": html}),
                "application/json; charset=utf-8",
            )
            return
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
            # 各页原始宽高（PDF 不可用时为空 dict，前端回退 onload 测量）
            dims = _page_dims(state)
            for n in sorted(pages_snapshot):
                raw_html = pages_snapshot[n]
                # Add ptoe-marker class for marker spans so saved pages render
                # highlighted in the editor while leaving stored HTML unchanged.
                served_html = _ensure_marker_classes(raw_html)
                # 2026-08-15 修复：已保存/历史内容按原样 serve（normalize_headings=False）
                # ——其中可能含用户手动设置的标题，不能再归一为 <p>（否则「保存后重开，
                # 已设置的标题格式丢失」）；OCR 自动标题的归一只在写入历史时做一次。
                item = {
                    "page": n,
                    "text": _page_text(served_html, normalize_headings=False),
                }
                if n in dims:
                    item["w"], item["h"] = dims[n]
                pages_list.append(item)
            # 纠错状态随 pages 一并下发（2026-10-17 修复）：此前 /api/pages 只回 pages，
            # 前端启动即丢失「已识别未应用」的错误/建议/已忽略集合——保存过的纠错状态
            # 在重开矫正界面后全部消失，必须重新识别一次。
            payload = {
                "pages": pages_list,
                "proofread": state.get("proofread")
                or {"errors": {}, "original": {}, "dismissed": {}},
                "last_proofread_page": state.get("last_proofread_page"),
                # 实时跟随 OCR 进度（2026-10-21）：前端据此开启增量轮询
                "live": bool(state.get("live_ocr_path")),
                "live_version": int(state.get("live_version") or 0),
                "live_done": int(state.get("live_done") or 0),
                "live_total": int(state.get("live_total") or 0),
                "live_status": str(state.get("live_status") or ""),
            }
            self._send(200, self._json(payload), "application/json; charset=utf-8")
            return
        if path == "/api/live_status":
            # 实时跟随 OCR 进度（2026-10-21）：返回「自 since 版本号之后」新增的
            # 页面（转换进行中识别完成的页），前端增量插入编辑器。
            import urllib.parse as _up

            state = self.server.state
            try:
                qs = _up.parse_qs(_up.urlsplit(self.path).query)
                since = int((qs.get("since") or ["0"])[0])
            except (TypeError, ValueError):
                since = 0
            lock = state.get("pages_lock")
            added: list[dict[str, Any]] = []
            with lock if lock is not None else nullcontext():
                for n, item in (state.get("live_added") or {}).items():
                    try:
                        ver = int((item or {}).get("version") or 0)
                        pno = int(n)
                    except (TypeError, ValueError):
                        continue
                    if ver > since:
                        added.append(
                            {"page": pno, "text": (item or {}).get("text") or ""}
                        )
            added.sort(key=lambda x: x["page"])
            self._send(
                200,
                self._json(
                    {
                        "ok": True,
                        "live": bool(state.get("live_ocr_path")),
                        "version": int(state.get("live_version") or 0),
                        "done": int(state.get("live_done") or 0),
                        "total": int(state.get("live_total") or 0),
                        "status": str(state.get("live_status") or ""),
                        "added": added,
                    }
                ),
                "application/json; charset=utf-8",
            )
            return
        if path == "/api/format_rules":
            # 格式规则列表（config.json format_rules 键；缺失返回空列表）。
            # 读取时经 _validate_format_rules 迁移旧模型规则，前端始终拿到新格式。
            try:
                from configmanage import get_config

                cfg = get_config(show_dialogs=False) or {}
                rules = _validate_format_rules(cfg.get("format_rules") or [])
                self._send(
                    200,
                    self._json({"ok": True, "rules": rules}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/history":
            # 历史记录列表：文件名/路径分列显示（同名不同路径可区分），
            # 同一文件按时间倒序编号版本（v1=最新）
            items = _history_entries()
            by_pdf: dict[str, list[dict[str, Any]]] = {}
            for it in items:
                by_pdf.setdefault(it["pdf"], []).append(it)
            for group in by_pdf.values():
                group.sort(key=lambda x: x["updated"], reverse=True)
                for i, it in enumerate(group, start=1):
                    it["version"] = i
            # 2026-08-23：全局按修改时间倒序——最近修改/读取的文件排在历史记录第一位
            # （此前仅组内排序用于版本号，返回列表仍是文件名序 ≈ 哈希序，与新旧无关）
            items.sort(key=lambda x: x.get("updated") or "", reverse=True)
            # 为每条记录确定 display_name：优先使用版本文件中的 display_name，否则回退到 name
            for it in items:
                pid = it["id"]
                # 读取对应版本文件检查 display_name
                fp = _history_dir() / f"{pid}.json"
                disp = None
                if fp.is_file():
                    try:
                        data = json.loads(fp.read_text(encoding="utf-8"))
                        disp = data.get("display_name")
                    except Exception:
                        pass
                it["display_name"] = disp if disp else it["name"]
            self._send(
                200, self._json({"items": items}), "application/json; charset=utf-8"
            )
            return
        if path == "/api/history/export":
            # 把某一历史版本导出为独立 JSON 文件（含内嵌预览图），供其他电脑
            # 通过 /api/history/import 导入继续矫正（跨平台矫正活动）。
            # 载荷 = 版本原始内容 + images 键（本机共享 sidecar 合并；旧版本
            # 文件自带 images 键则直接用）。Content-Disposition 让浏览器下载。
            import urllib.parse as _up

            try:
                qs = _up.parse_qs(_up.urlsplit(self.path).query)
                pid = (qs.get("id") or [""])[0]
                fp = _history_dir() / f"{pid}.json" if pid else None
                if not pid or not (fp and fp.is_file()):
                    self._send(
                        404,
                        self._json(
                            {"ok": False, "error": f"history version not found: {pid}"}
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                data = json.loads(fp.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {"pages": {}}
                images = data.get("images") or _load_images_cache(_version_prefix(pid))
                data["images"] = images or {}
                body = self._json(data)
                self._send(
                    200,
                    body,
                    "application/json; charset=utf-8",
                    {"Content-Disposition": f'attachment; filename="{pid}.json"'},
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/proofread_settings":
            self._proofread_settings()
            return
        if path == "/api/shortcuts":
            self._shortcuts()
            return
        if path == "/api/mouse_shortcuts":
            self._mouse_shortcuts()
            return
        if path == "/api/ui_settings":
            self._ui_settings()
            return
        if path == "/api/auto_save":
            self._auto_save()
            return
        if path == "/api/replace_rules":
            self._replace_rules()
            return
        if path == "/api/correction_format":
            self._correction_format()
            return
        if path == "/api/config":
            # 字体/界面配置：GET 读取 config.json fonts + citationItalicEnabled + table_convert
            from configmanage import get_config

            cfg = get_config(show_dialogs=False) or {}
            fonts = cfg.get("fonts") or {}
            if not isinstance(fonts, dict):
                fonts = {}
            self._send(
                200,
                self._json(
                    {
                        "ok": True,
                        "fonts": {
                            "body": fonts.get("body", ""),
                            "heading": fonts.get("heading", ""),
                            "note": fonts.get("note", ""),
                            "citation": fonts.get("citation", ""),
                        },
                        "citationItalicEnabled": bool(
                            cfg.get("citationItalicEnabled", False)
                        ),
                        # 回退规则与 stringmanage 保持一致：缺失/读取异常一律视为开启
                        "table_convert": bool(cfg.get("table_convert", True)),
                    }
                ),
                "application/json; charset=utf-8",
            )
            return
        if path == "/api/llm_status":
            # llama-server 运行状态探测（深度校对/句子校正用）
            try:
                import llamamanage
                from configmanage import get_config
                from llamamanage import _probe_server

                cfg = get_config(show_dialogs=False) or {}
                model_choices = cfg.get("model_choices") or {}
                pr_cfg = cfg.get("proofread") or {}
                model_key = str(
                    pr_cfg.get("llm_model") or cfg.get("selected_model") or ""
                )
                model_info = (
                    (model_choices or {}).get(model_key)
                    if isinstance(model_choices, dict)
                    else None
                )
                model_name = (
                    str((model_info or {}).get("name") or model_key)
                    if model_info
                    else None
                )
                # 探测必须与「启动服务」用同一个引擎（proofread_engine）：矫正界面把
                # 服务起在校正引擎上，这里若按识别引擎（engine）探测，端口/引擎口径
                # 不一致 → 状态永远 running:false / mismatch，下拉框与运行模型对不上
                # （2026-10-30 修复「切换模型失败」）。
                with _proofread_engine_ctx():
                    probe = _probe_server(model_name) if model_name else "none"
                    # 实际运行模型反查（2026-10-29）：probe 只回答「端口上跑的是不是配置
                    # 里的那个模型」。多开矫正界面时，早先打开的窗口内存/下拉框里仍是旧
                    # 模型键，重识别按旧键探测 → 报「当前服务加载的模型与所选模型不符」。
                    # mismatch 时把服务端真实加载的 id 反查成 model_choices 键返回，前端
                    # 据此把下拉框与持久化值对齐到正在运行的模型（match 时无需额外请求，
                    # 保证 5s 轮询不增加开销）。
                    running_model = model_key if probe == "match" else None
                    if probe == "mismatch" and isinstance(model_choices, dict):
                        try:
                            ids = llamamanage._loaded_model_ids()
                        except Exception:  # noqa: BLE001  探测失败按未知处理
                            ids = []
                        if ids:
                            for k in sorted(model_choices):
                                nm = str((model_choices.get(k) or {}).get("name") or k)
                                try:
                                    hit = llamamanage._model_id_matches(nm, ids)
                                except Exception:  # noqa: BLE001
                                    hit = False
                                if hit:
                                    running_model = k
                                    break
                # 大模型加载耗时可达数分钟，此期间进程存活但 /health 仍 503：
                # 报「启动中」而非「未运行」，避免用户误判启动失败（2026-08-09）
                proc = getattr(llamamanage, "_server_process", None)
                loading = (
                    bool(proc is not None and proc.poll() is None) and probe == "none"
                )
                self._send(
                    200,
                    self._json(
                        {
                            "ok": True,
                            "running": probe != "none",
                            "mismatch": probe == "mismatch",
                            "loading": loading,
                            "model": model_key or None,
                            # 服务端实际运行的模型键（未知为 None）：前端据此记忆
                            # 「最近使用的模型」并对齐下拉框/持久化值（2026-10-29）
                            "running_model": running_model or None,
                        }
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
        if path == "/help.md":
            try:
                help_path = Path(__file__).resolve().parent / "help.md"
                if help_path.is_file():
                    content = help_path.read_text(encoding="utf-8")
                    self._send(200, content.encode("utf-8"), "text/markdown; charset=utf-8")
                else:
                    self._send(404, b"help.md not found", "text/plain")
            except Exception as e:
                self._send(500, str(e).encode("utf-8"), "text/plain; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")

    # -- POST --

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/tabs":
            # 标签栏配置持久化：仅支持 position 字段
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send(400, self._json({"ok": False, "error": "无效的 JSON"}), "application/json; charset=utf-8")
                return
            if not isinstance(body, dict):
                self._send(400, self._json({"ok": False, "error": "请求体必须为 JSON 对象"}), "application/json; charset=utf-8")
                return
            try:
                import tabmanage  # noqa: PLC0415

                ok, err = tabmanage.handle_tabs_post(body)
            except Exception as e:
                self._send(500, self._json({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
                return
            if not ok:
                self._send(400, self._json({"ok": False, "error": err}), "application/json; charset=utf-8")
            else:
                self._send(200, self._json({"ok": True}), "application/json; charset=utf-8")
            return
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
        if path == "/api/clipboard":
            # 写系统剪贴板：右键菜单「复制」的兜底通路（浏览器侧 copy 失败时用）。
            # body: {text: str, html?: str}
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception as e:  # noqa: BLE001
                self._send(
                    400,
                    self._json({"ok": False, "error": f"请求体解析失败：{e}"}),
                    "application/json; charset=utf-8",
                )
                return
            ok = clipboard_write(
                str(body.get("text") or ""), str(body.get("html") or "")
            )
            self._send(200, self._json({"ok": ok}), "application/json; charset=utf-8")
            return
        if path == "/api/history/delete":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                deleted = _delete_history(
                    list(body.get("ids") or []), bool(body.get("all"))
                )
                self._send(
                    200,
                    self._json({"ok": True, "deleted": deleted}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/history/export/bulk":
            # 多选导出：把多个历史版本打包为一个 ZIP（每成员为 {pid}.json，
            # 含内嵌预览图 sidecar 合并），供批量迁移/备份。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                ids = list(body.get("ids") or [])
                if not ids:
                    self._send(
                        400,
                        self._json({"ok": False, "error": "未选择要导出的历史版本"}),
                        "application/json; charset=utf-8",
                    )
                    return
                import io
                import zipfile

                buf = io.BytesIO()
                count = 0
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for pid in ids:
                        fp = _history_dir() / f"{pid}.json"
                        if not fp.is_file():
                            continue
                        try:
                            data = json.loads(fp.read_text(encoding="utf-8"))
                            if not isinstance(data, dict):
                                data = {"pages": {}}
                            images = data.get("images") or _load_images_cache(
                                _version_prefix(pid)
                            )
                            data["images"] = images or {}
                            zf.writestr(
                                f"{pid}.json",
                                json.dumps(data, ensure_ascii=False, indent=2),
                            )
                            count += 1
                        except Exception:  # noqa: BLE001
                            continue
                if count == 0:
                    self._send(
                        404,
                        self._json({"ok": False, "error": "没有可导出的历史版本"}),
                        "application/json; charset=utf-8",
                    )
                    return
                import time as _time

                stamp = _time.strftime("%Y%m%d%H%M%S")
                self._send(
                    200,
                    buf.getvalue(),
                    "application/zip",
                    {
                        "Content-Disposition": f'attachment; filename="ptoe_history_{stamp}.zip"'
                    },
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/history/export/backup":
            # 导出为备份文件：把选中的历史版本打包为 ZIP 并直接保存到
            # data/data/ 目录（不走浏览器下载——pywebview WebView2 默认
            # ALLOW_DOWNLOADS=False 会取消 blob/附件下载，导致导出静默失败）。
            # 返回 {ok, path, count}，前端提示已保存位置。
            # 打包逻辑见 export_history_backup（与主界面「矫正历史」页共用）。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                ids = list(body.get("ids") or [])
                if not ids:
                    self._send(
                        400,
                        self._json({"ok": False, "error": "未选择要导出的历史版本"}),
                        "application/json; charset=utf-8",
                    )
                    return
                try:
                    result = export_history_backup(ids)
                except LookupError as e:
                    self._send(
                        404,
                        self._json({"ok": False, "error": str(e)}),
                        "application/json; charset=utf-8",
                    )
                    return
                self._send(
                    200, self._json(result), "application/json; charset=utf-8"
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/history/import":
            # 把导出的历史版本 JSON 或 ZIP 导入本机（跨平台矫正活动）。
            # body 两种形态：
            #   {filename, content} —— 单 JSON（向后兼容）
            #   {filename, is_zip: true, content_b64} —— ZIP 包（多版本）
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                filename = str(body.get("filename") or "")
                is_zip = bool(body.get("is_zip"))
                if is_zip:
                    import base64
                    import io
                    import zipfile

                    b64 = str(body.get("content_b64") or "")
                    try:
                        zip_bytes = base64.b64decode(b64)
                    except Exception:
                        self._send(
                            400,
                            self._json(
                                {"ok": False, "error": "ZIP 内容 base64 解码失败"}
                            ),
                            "application/json; charset=utf-8",
                        )
                        return
                    ids = []
                    errors = []
                    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                        for name in zf.namelist():
                            if not name.lower().endswith(".json"):
                                continue
                            try:
                                member_content = json.loads(
                                    zf.read(name).decode("utf-8")
                                )
                                ok, msg, stem = _import_history(member_content, name)
                                if ok:
                                    ids.append(stem)
                                else:
                                    errors.append(f"{name}: {msg}")
                            except Exception as exc:  # noqa: BLE001
                                errors.append(f"{name}: {exc}")
                    if not ids:
                        err_msg = "导入失败：" + (
                            "；".join(errors) if errors else "ZIP 中无有效 JSON"
                        )
                        self._send(
                            400,
                            self._json({"ok": False, "error": err_msg}),
                            "application/json; charset=utf-8",
                        )
                        return
                    self._send(
                        200,
                        self._json({"ok": True, "ids": ids, "errors": errors or None}),
                        "application/json; charset=utf-8",
                    )
                    return
                # 原有单 JSON 路径
                content = body.get("content")
                ok, msg, stem = _import_history(content, filename)
                if not ok:
                    self._send(
                        400 if "缺少 pages" in msg else 500,
                        self._json({"ok": False, "error": msg}),
                        "application/json; charset=utf-8",
                    )
                    return
                self._send(
                    200,
                    self._json({"ok": True, "id": stem}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
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
                        self._json(
                            {"ok": False, "error": f"history version not found: {pid}"}
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                out = [
                    # 与 /api/pages 一致：serve 时补回 ptoe-marker 显示类，
                    # 否则保存时被 sanitize 剥掉的 class 会让标记渲染成纯文本
                    # （旧历史版本磁盘载荷无 class，必须在此补齐）。
                    # 2026-08-15 修复：历史内容按原样返回（不再归一 <h1>-<h6>）——
                    # 其中可能含用户手动设置的标题，归一会导致「保存后重开，
                    # 已设置的标题格式丢失」；OCR 自动标题的归一只在写入历史时做一次。
                    # 2026-08-30：serve 前做杂符括号清理（token 级）——历史版本
                    # 可能保存过 \\〔^{x〕}\\ 之类大模型杂符包裹，须在界面可见/再次
                    # 矫正前清除（与 reocr 对比基准保持一致）。
                    {
                        "page": int(k),
                        "html": _ensure_marker_classes(
                            _clean_bracket_junk_html(str(v))
                        ),
                    }
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
                            # 2026-08-15 修复：历史版本内容按原样同步（不再归一标题）——
                            # 用户手动设置的 <h1>-<h6> 必须保留，否则「保存后重开，
                            # 已设置的标题格式丢失」；OCR 自动标题的归一只在写入历史时做一次
                            # 2026-08-30：同步前做杂符括号清理（与 serve 路径一致）
                            st["pages"][int(k)] = sanitize_html(
                                _clean_bracket_junk_html(str(v))
                            )
                        except (TypeError, ValueError):
                            continue
                if pdf and Path(pdf).is_file() and st.get("pdf_path") != pdf:
                    st["pdf_path"] = pdf
                    st["preview_cache"] = OrderedDict()  # 换书后旧页码缓存作废
                    # 旧 PDF 句柄作废（下次按需重开）
                    try:
                        st["preview_doc"] = None
                    except Exception:
                        st["preview_doc"] = None
                # 记录 history_name 供后续暂存/保存使用（测试断言）
                st["history_name"] = Path(pdf).name if pdf else None
                # 从版本文件读取 display_name（重命名功能的结果），供 EPUB 导出 fallback 使用
                # 遵循 GET /api/history 同一模式：版本 JSON 中有则优先，无则回退到 name
                _disp = None
                vp = _history_dir() / f"{pid}.json"
                if vp.is_file():
                    try:
                        _ddata = json.loads(vp.read_text(encoding="utf-8"))
                        _disp = _ddata.get("display_name")
                    except Exception:
                        pass
                st["display_name"] = _disp if _disp else st.get("display_name")
                # 加载内嵌预览图供跨电脑时 fallback
                st["embedded_images"] = loaded.get("embedded_images") or {}
                # 返回页面数据（与 /api/convert 约定一致）+ 文字纠错状态
                self._send(
                    200,
                    self._json(
                        {
                            "ok": True,
                            "pages": out,
                            "pdf": pdf,
                            "proofread": loaded.get("proofread")
                            or {"errors": {}, "original": {}, "dismissed": {}},
                            "last_proofread_page": loaded.get("last_proofread_page"),
                        }
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
        if path == "/api/history/rename":
            # 重命名历史记录条目：调用共享 helper rename_history 写入 display_name
            # 到同一 PDF 组的所有版本文件（guimanage 的 /api/history/rename 复用同一 helper）
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                new_name = str(body.get("newName") or "").strip()
                result = rename_history(body.get("id"), new_name)
                # 若重命名的是当前编辑中的书（前缀一致），同步会话内的 display_name，
                # 使随后的保存/暂存/完成沿用它而非回退到原名
                st = self.server.state
                if st.get("history_prefix") == result.get("prefix"):
                    st["display_name"] = new_name
                self._send(
                    200,
                    self._json({"ok": True, "display_name": result.get("display_name")}),
                    "application/json; charset=utf-8",
                )
                return
            except ValueError as e:
                self._send(
                    400,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            except LookupError as e:
                self._send(
                    404,
                    self._json({"ok": False, "error": str(e)}),
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
                convert_modes = globals().get("_CONVERT_MODES", {"t2s", "s2t"})
                if mode not in convert_modes:
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
            # 文本智能清理（段落合并 / 段首 #/* 符号 / 中英文标点 / HTML 标签 /
            # 扩展符号+省略号归一 / Markdown 残留剥离）：无状态 —— 只返回清理结果，
            # 由浏览器更新界面（保存时才落盘）。各步骤开关读取 ui_settings，
            # body 可传同名字段覆盖（bool）。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                from configmanage import (
                    CLEAN_SETTINGS_DEFAULTS,
                    get_clean_digit_punct_map,
                    get_clean_settings,
                    get_clean_symbol_map,
                )

                opts = get_clean_settings()
                for _k in CLEAN_SETTINGS_DEFAULTS:
                    if isinstance(body.get(_k), bool):
                        opts[_k] = body[_k]
                sym_map = get_clean_symbol_map()
                if isinstance(body.get("clean_symbol_map"), list):
                    sym_map = body["clean_symbol_map"]
                digit_map = get_clean_digit_punct_map()
                if isinstance(body.get("clean_digit_punct_map"), list):
                    digit_map = body["clean_digit_punct_map"]
                clean_kw = {
                    "clean_punctuation": opts["clean_punctuation"],
                    "clean_symbols": opts["clean_symbols"],
                    "clean_md_tags": opts["clean_md_tags"],
                    "clean_html_tags": opts["clean_html_tags"],
                    "clean_paragraphs": opts["clean_paragraphs"],
                    "symbol_map": sym_map,
                    "clean_digit_punct": opts["clean_digit_punct"],
                    "digit_punct_map": digit_map,
                    # 去除段落内空白（2026-10-21）：普通段落合并软换行，对齐段保留换行
                    "clean_spaces": opts["clean_spaces"],
                    "clean_spaces_keep_word_gap": opts["clean_spaces_keep_word_gap"],
                }
                cleaned = _clean_pages(body.get("pages") or [], clean_kw)
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
        if path == "/api/table_convert":
            # 手动把选中文字转成原生表格（无状态）：body {"text": str} →
            # {"ok": true, "html": str}。手动档不强制 Markdown 分隔行，并额外识别
            # 无竖线的空格/制表符对齐列（设计文档规格 C，仅手动提供）。
            # 自动档（重识别/应用校正后补转，2026-10）：body {"lines": [str, ...]} →
            # {"ok": true, "lines": [...], "chunks": [[start, end, html], ...]}；chunks
            # 标明输入行 [start, end) 被换成了哪个输出行（编辑器据此替换连续 DOM 块）。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                lines = body.get("lines")
                if lines is not None:
                    if not isinstance(lines, list) or not all(
                        isinstance(x, str) for x in lines
                    ):
                        self._send(
                            400,
                            self._json({"ok": False, "error": "lines 必须是字符串数组"}),
                            "application/json; charset=utf-8",
                        )
                        return
                    from tableconvertmanage import convert_lines

                    out_lines, chunks = convert_lines(lines)
                    self._send(
                        200,
                        self._json(
                            {
                                "ok": True,
                                "lines": out_lines,
                                "chunks": [[s, e, h] for s, e, h in chunks],
                            }
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                text = str(body.get("text") or "")
                if not text.strip():
                    self._send(
                        400,
                        self._json({"ok": False, "error": "text 不能为空"}),
                        "application/json; charset=utf-8",
                    )
                    return
                from tableconvertmanage import convert_text_tables

                html_text = convert_text_tables(
                    text, allow_space_aligned=True, require_separator=False
                )
                self._send(
                    200,
                    self._json({"ok": True, "html": html_text}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/table_to_text":
            # 表格转文本（无状态，2026-10）：body {"htmls": [str, ...]} →
            # {"ok": true, "texts": [str, ...]}。每项是一段 <table> HTML，返回对应的
            # Markdown 管道文字（解析不到表格时该项为空串）。编辑器「表格转文本」用。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                htmls = body.get("htmls")
                if isinstance(htmls, str):
                    htmls = [htmls]
                if not isinstance(htmls, list) or not htmls:
                    self._send(
                        400,
                        self._json({"ok": False, "error": "htmls 不能为空"}),
                        "application/json; charset=utf-8",
                    )
                    return
                if not all(isinstance(h, str) for h in htmls):
                    self._send(
                        400,
                        self._json({"ok": False, "error": "htmls 必须是字符串数组"}),
                        "application/json; charset=utf-8",
                    )
                    return
                if len(htmls) > 200:
                    self._send(
                        400,
                        self._json({"ok": False, "error": "一次最多转换 200 个表格"}),
                        "application/json; charset=utf-8",
                    )
                    return
                from tableconvertmanage import table_html_to_pipe_text

                texts = [table_html_to_pipe_text(h) for h in htmls]
                self._send(
                    200,
                    self._json({"ok": True, "texts": texts}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/format_rules":
            # 整体保存格式规则（弹窗编辑后一次提交）
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                rules = _validate_format_rules(body.get("rules") or [])
                from configmanage import set_format_rules

                set_format_rules(rules)
                self._send(
                    200,
                    self._json({"ok": True, "rules": rules}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/format_rules/apply":
            # 应用格式规则到指定页 HTML（任务 B：服务端应用引擎）
            # body: {page:int, html:str, rule_id?:str, all?:bool, sel_start?:int, sel_end?:int}
            #       或全部页模式 {mode:'all_pages', pages:{页码:html}, rules:[...], rule_id:str}
            # 单页返回: {ok:bool, html:str}；全部页返回: {ok:bool, results:[{page:int, html:str}]}
            try:
                if rulemanage is None:
                    self._send(
                        500,
                        self._json({"ok": False, "error": "rulemanage 模块未加载"}),
                        "application/json; charset=utf-8",
                    )
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))

                # ---- 全部页模式（条件 scope=all_pages）：跨页逐页应用，一次操作处理所有页面 ----
                if body.get("mode") == "all_pages":
                    pages_in = body.get("pages")
                    if not isinstance(pages_in, dict) or not pages_in:
                        self._send(
                            400,
                            self._json({"ok": False, "error": "pages 必须为非空对象（页码 -> HTML）"}),
                            "application/json; charset=utf-8",
                        )
                        return
                    rules = _validate_format_rules(body.get("rules") or [])
                    if not rules:
                        self._send(
                            400,
                            self._json({"ok": False, "error": "规则列表为空或全部无效"}),
                            "application/json; charset=utf-8",
                        )
                        return
                    rule_id = body.get("rule_id")
                    if not isinstance(rule_id, str) or not rule_id:
                        self._send(
                            400,
                            self._json({"ok": False, "error": "rule_id 必须为字符串"}),
                            "application/json; charset=utf-8",
                        )
                        return
                    # 归一化 scope：全部页模式下 all_pages 与 selection 条件都按整页文本求值
                    # （等价于该页的 page 语义；不要在全部页模式依赖 sel_start/sel_end，
                    # sel_ranges 多区间选区同样只服务单页分支——此处静默忽略，不报错）
                    for rf in rules:
                        for c in rf.get("conditions", []):
                            if c.get("scope") in ("all_pages", "selection"):
                                c["scope"] = "page"
                    results = []
                    for page_no, html_text in pages_in.items():
                        try:
                            n = int(page_no)
                        except (TypeError, ValueError):
                            continue
                        if not isinstance(html_text, str):
                            continue
                        try:
                            new_html, err = rulemanage.apply_rules(
                                html_text,
                                rules,
                                rule_id=rule_id,
                                all_rules=False,
                                sel_start=None,
                                sel_end=None,
                            )
                        except Exception as e:  # noqa: BLE001
                            self._send(
                                400,
                                self._json({"ok": False, "error": f"应用格式规则失败: {e}"}),
                                "application/json; charset=utf-8",
                            )
                            return
                        if err:
                            self._send(
                                400,
                                self._json({"ok": False, "error": err}),
                                "application/json; charset=utf-8",
                            )
                            return
                        new_html = sanitize_html(new_html)
                        # 只返回内容实际变化的页（前后 html 不等才列入，减少前端开销）
                        if new_html != html_text:
                            results.append({"page": n, "html": new_html})
                    self._send(
                        200,
                        self._json({"ok": True, "results": results}),
                        "application/json; charset=utf-8",
                    )
                    return

                # ---- 单页模式（原有行为保持不变）----
                # 校验参数
                page = body.get("page")
                html_text = body.get("html")
                rule_id = body.get("rule_id")
                all_rules = bool(body.get("all"))
                sel_start = body.get("sel_start")
                sel_end = body.get("sel_end")
                # 可选多区间选区（2026-09）：[[起,止], ...] 相对整页纯文本偏移，
                # 一次请求对多个不连续区间应用格式规则（多段选中）。sel_start/sel_end 保留兼容。
                sel_ranges = body.get("sel_ranges")
                if sel_ranges is not None:
                    if (
                        not isinstance(sel_ranges, list)
                        or not sel_ranges
                        or len(sel_ranges) > 64
                        or not all(
                            isinstance(rng, (list, tuple))
                            and len(rng) == 2
                            and isinstance(rng[0], int)
                            and not isinstance(rng[0], bool)
                            and isinstance(rng[1], int)
                            and not isinstance(rng[1], bool)
                            and rng[0] < rng[1]
                            for rng in sel_ranges
                        )
                    ):
                        self._send(
                            400,
                            self._json({"ok": False, "error": "sel_ranges 格式错误：应为 [[起,止], ...] 的整数区间列表"}),
                            "application/json; charset=utf-8",
                        )
                        return
                    sel_ranges = [tuple(rng) for rng in sel_ranges]

                if not isinstance(page, int) or page < 1:
                    self._send(
                        400,
                        self._json({"ok": False, "error": "page 必须为正整数"}),
                        "application/json; charset=utf-8",
                    )
                    return
                if not isinstance(html_text, str):
                    self._send(
                        400,
                        self._json({"ok": False, "error": "html 必须为字符串"}),
                        "application/json; charset=utf-8",
                    )
                    return

                # 读取规则（与 GET /api/format_rules 相同来源）
                from configmanage import get_config
                cfg = get_config(show_dialogs=False) or {}
                rules = cfg.get("format_rules") or []
                rules = _validate_format_rules(rules)

                # 调用 rulemanage 引擎
                if sel_ranges is not None:
                    # 多区间选区：逐区间应用。首区间跑全量规则；后续区间仅保留
                    # selection 作用域条件——page/paragraph 作用域会重复应用导致
                    # 双重包裹（<strong><strong>），故在后续区间过滤掉。
                    cur_html = html_text
                    err = None
                    for idx, (s, e) in enumerate(sel_ranges):
                        iter_rules = rules if idx == 0 else _filter_rules_selection_only(rules)
                        cur_html, err = rulemanage.apply_rules(
                            cur_html,
                            iter_rules,
                            rule_id=rule_id,
                            all_rules=all_rules,
                            sel_start=s,
                            sel_end=e,
                        )
                        if err:
                            break
                    if err:
                        self._send(
                            400,
                            self._json({"ok": False, "error": err}),
                            "application/json; charset=utf-8",
                        )
                        return
                    new_html = sanitize_html(cur_html)
                    self._send(
                        200,
                        self._json({"ok": True, "html": new_html}),
                        "application/json; charset=utf-8",
                    )
                else:
                    new_html, err = rulemanage.apply_rules(
                        html_text,
                        rules,
                        rule_id=rule_id,
                        all_rules=all_rules,
                        sel_start=sel_start if isinstance(sel_start, int) else None,
                        sel_end=sel_end if isinstance(sel_end, int) else None,
                    )
                    if err:
                        self._send(
                            400,
                            self._json({"ok": False, "error": err}),
                            "application/json; charset=utf-8",
                        )
                        return

                    # 结果过一遍 sanitize_html 再返回
                    new_html = sanitize_html(new_html)
                    self._send(
                        200,
                        self._json({"ok": True, "html": new_html}),
                        "application/json; charset=utf-8",
                    )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": f"应用格式规则失败: {e}"}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/format_rules/preview":
            # 规则匹配预览：把条件的正则作用于当前页文本，返回匹配清单（含各捕获组文本），
            # 供规则编辑器在「应用」之前核对「到底匹配到几个对象、每处的分组是什么」。
            # 背景（2026-09-13）：贪婪前缀写法（如 `[\t\S]+(〔\d+〕)`）会把同一行里后面
            # 出现的对象一并吞进本次匹配，用户以为匹配了 4 个对象、实际只匹配到 2 个
            # （只有这 2 处的分组被套格式），表现为"只有最后一个匹配对象应用了规则格式"。
            # 预览把真实匹配数与每个匹配的捕获组显式摊开，避免这类误判反复出现。
            # body: {html:str, type?:str, pattern:str, max?:int}
            try:
                if rulemanage is None:
                    self._send(
                        500,
                        self._json({"ok": False, "error": "rulemanage 模块未加载"}),
                        "application/json; charset=utf-8",
                    )
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8") or "{}")
                html_text = body.get("html")
                if not isinstance(html_text, str):
                    self._send(
                        400,
                        self._json({"ok": False, "error": "html 必须为字符串"}),
                        "application/json; charset=utf-8",
                    )
                    return
                ctype = str(body.get("type") or "regex")
                if ctype not in ("regex", "contains", "prefix", "suffix"):
                    ctype = "regex"
                pattern = str(body.get("pattern") or "")
                try:
                    limit = int(body.get("max") or 20)
                except (TypeError, ValueError):
                    limit = 20
                limit = max(1, min(limit, 100))
                if not pattern:
                    self._send(
                        200,
                        self._json({
                            "ok": True, "count": 0, "shown": 0, "matches": [],
                            "hint": "条件内容为空 = 无条件恒匹配（不逐个匹配对象）",
                        }),
                        "application/json; charset=utf-8",
                    )
                    return
                if ctype == "regex":
                    try:
                        # 与引擎同一编译口径：支持 /pattern/flags 写法（i/m/s）
                        rulemanage.compile_pattern(pattern)
                    except Exception as e:  # noqa: BLE001 — re.error 及危险模式拦截
                        self._send(
                            400,
                            self._json({"ok": False, "error": f"正则表达式无效：{e}"}),
                            "application/json; charset=utf-8",
                        )
                        return

                cond = rulemanage.Condition(
                    type=ctype, pattern=pattern, scope="page", formats=[], target="match"
                )
                page_text, _ni = rulemanage.collect_text_nodes(
                    rulemanage.parse_html(sanitize_html(html_text))
                )

                def _clip(text: str, n: int = 60) -> str:
                    text = text.replace("\n", "⏎")
                    return text if len(text) <= n else text[:n] + "…"

                spans: list[tuple[int, int, list[str | None]]] = []
                if ctype == "regex":
                    for m in rulemanage.find_matches(cond, page_text):
                        spans.append((
                            m.start(),
                            m.end(),
                            [
                                (page_text[gs:ge] if gs >= 0 else None)
                                for gs, ge in list(m.regs)[1:]
                            ],
                        ))
                else:
                    for s, e in rulemanage._pattern_match_ranges(cond, page_text):
                        spans.append((s, e, []))

                matches = [
                    {
                        "index": i + 1,
                        "text": _clip(page_text[s:e]),
                        "groups": [_clip(g, 30) if g is not None else None for g in groups],
                        "start": s,
                        "end": e,
                    }
                    for i, (s, e, groups) in enumerate(spans[:limit])
                ]
                self._send(
                    200,
                    self._json({
                        "ok": True,
                        "count": len(spans),
                        "shown": len(matches),
                        "matches": matches,
                    }),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": f"匹配预览失败: {e}"}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/proofread_settings":
            self._proofread_settings()
            return
        if path == "/api/shortcuts":
            self._shortcuts()
            return
        if path == "/api/mouse_shortcuts":
            self._mouse_shortcuts()
            return
        if path == "/api/ui_settings":
            self._ui_settings()
            return
        if path == "/api/auto_save":
            self._auto_save()
            return
        if path == "/api/replace_rules":
            self._replace_rules()
            return
        if path == "/api/ime_english":
            # Esc 进 NORMAL 后把输入法切英文（设置-界面「Esc 后自动切英文输入法」）。
            # 浏览器不能切输入法，只能由本地服务端调 Win32 API；失败一律静默降级。
            try:
                switched, msg = ime_to_english()
                self._send(
                    200,
                    self._json({"ok": True, "switched": switched, "message": msg}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/correction_format":
            self._correction_format()
            return
        if path == "/api/config":
            self._config()
            return
        if path == "/api/llm_start":
            # 启动 llama-server（默认附加 --mmproj 图像投影，供大模型重识别 OCR 使用；
            # 模型未配置 mmproj 时回退纯文本）。
            try:
                from configmanage import get_config
                from llamamanage import runserver

                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                cfg = get_config(show_dialogs=False) or {}
                model_choices = cfg.get("model_choices") or {}
                pr_cfg = cfg.get("proofread") or {}
                model_key = str(
                    body.get("model")
                    or pr_cfg.get("llm_model")
                    or cfg.get("selected_model")
                    or ""
                )
                # 规范化模型键（去空白 + 大小写不敏感）后再校验，避免因大小写或空白被误判为未注册
                _ambiguous_msg = None
                if isinstance(model_choices, dict):
                    from configmanage import find_canonical_model_key

                    canonical, _matches = find_canonical_model_key(
                        model_choices, model_key.strip()
                    )
                    if canonical is not None:
                        model_key = canonical
                    elif _matches:
                        _ambiguous_msg = (
                            f"模型 '{model_key}' 存在多个大小写不敏感匹配 "
                            f"{_matches}，请使用精确键名或移除重复项"
                        )
                if (
                    not isinstance(model_choices, dict)
                    or model_key not in model_choices
                ):
                    self._send(
                        400,
                        self._json(
                            {
                                "ok": False,
                                "error": _ambiguous_msg
                                or (
                                    f"模型 '{model_key}' 未在配置中注册"
                                    f"（可用: {', '.join(sorted(model_choices)) if isinstance(model_choices, dict) else '无'}）"
                                ),
                            }
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                model_info = model_choices.get(model_key) or {}
                has_mmproj = bool(model_info.get("mmproj"))
                # 快速切换支持（2026-08 修复）：若端口上运行的是其他模型，先停掉
                # 本进程管理的旧实例再启动新模型；仍被占用（外部进程）则给出明确提示，
                # 避免直接调 runserver 撞端口报出难懂的占用错误。
                #
                # 引擎口径（2026-10-30 修复）：eng / 端口 / 探测 / 停止 / 启动全部在
                # 校正引擎上下文内完成。此前只有 runserver 在 ctx 内，前面按识别引擎
                # （engine）探测+停止——engine=vllm、proofread_engine=llama 时切换会去
                # 探测/杀掉 vLLM 的 8000 端口，报「端口 8000 被外部 vLLM-Omni 占用」，
                # 真正的模型切换则从未发生。
                # 矫正/重识别为单请求顺序处理（用户逐页点击，一次一个请求），并发
                # 槽位取 1 即可——llama-server 的 KV cache ≈ n_ctx × parallel，默认
                # 取 config 的 parallel(6)×max_tokens(8192) 会把 KV 预分配撑到数 GB，
                # 显著超过直接启动（--parallel 1）的显存占用（2026-09-01 修复）。
                with _proofread_engine_ctx():
                    from llamamanage import _active_engine

                    eng = _active_engine()
                    eng_label = "vLLM-Omni" if eng == "vllm" else "llama-server"
                    eng_port = (
                        (cfg.get("vllm_server_args") or {}).get("port") or "8000"
                        if eng == "vllm"
                        else (cfg.get("llama_server_args") or {}).get("port") or "8080"
                    )
                    from llamamanage import _probe_server as _probe_pre, stopserver

                    pre_name = str(model_info.get("name") or model_key)
                    if _probe_pre(pre_name) == "mismatch":
                        stopserver()  # 仅能停掉本进程启动的实例（含端口兜底）
                        time.sleep(1.0)
                    if _probe_pre(pre_name) == "mismatch":
                        self._send(
                            200,
                            self._json(
                                {
                                    "ok": False,
                                    "error": (
                                        f"端口 {eng_port} 被外部 {eng_label} 占用"
                                        "（非本程序启动），请手动关闭后重试"
                                    ),
                                    "engine": eng,
                                    "port": str(eng_port),
                                }
                            ),
                            "application/json; charset=utf-8",
                        )
                        return
                    running = bool(runserver(model_key, with_mmproj=has_mmproj, parallel=1, ctx_size=8192))
                if running:
                    # Issue 1 fix: persist model choice so it survives UI restart
                    try:
                        from configmanage import set_proofread_param
                        set_proofread_param("llm_model", model_key)
                    except Exception:
                        pass  # Best-effort; don't fail startup if config write fails
                    message = f"{eng_label} 已就绪"
                else:
                    # 启动失败：区分「端口被其他模型占用」与「启动超时/失败」，给出可操作提示
                    from llamamanage import _probe_server

                    model_name = str(model_info.get("name") or model_key)
                    # 与启动同一引擎口径探测（否则 vLLM/llama 混用时误报「端口被其他占用」）
                    with _proofread_engine_ctx():
                        _probe_after = _probe_server(model_name)
                    if _probe_after == "mismatch":
                        message = (
                            f"端口 {eng_port} 已被其他 {eng_label} 占用（模型不符），"
                            f"请先停止旧服务（或手动关闭任务管理器中的 {eng_label}）后重试"
                        )
                    else:
                        message = f"{eng_label} 启动失败（请检查模型路径/服务日志）"
                self._send(
                    200,
                    self._json(
                        {
                            "ok": True,
                            "running": running,
                            "image_model": has_mmproj,
                            "message": message,
                        }
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
        if path == "/api/llm_stop":
            try:
                from llamamanage import _active_engine, _probe_server, stopserver

                # 停止/探测同样按校正引擎（proofread_engine）口径，与启动保持一致
                with _proofread_engine_ctx():
                    eng_label = (
                        "vLLM-Omni" if _active_engine() == "vllm" else "llama-server"
                    )
                    stopserver()
                    # stopserver 已兜底杀端口上的遗留实例；再探测确认端口真正释放
                    # （杀不掉时提示手动关闭，避免界面误报已停止）
                    still_busy = _probe_server(None) != "none"
                if still_busy:
                    self._send(
                        200,
                        self._json(
                            {
                                "ok": True,
                                "message": f"已停止 {eng_label}（端口仍有进程占用，请手动关闭）",
                            }
                        ),
                        "application/json; charset=utf-8",
                    )
                else:
                    self._send(
                        200,
                        self._json({"ok": True, "message": f"已停止 {eng_label}"}),
                        "application/json; charset=utf-8",
                    )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/proofread":
            # 文字纠错：剥标签取纯文本 → proofread_page → 错误列表。
            # 无状态 —— 只返回检测结果，由浏览器叠加标注（不入 undo 快照）。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                text = _proofread_plain_text(str(body.get("html") or ""))
                # 原有规则（半角转全角/引号配对/混淆表/词典）默认关闭，
                # 由 config.json proofread.enable_legacy_rules 开关控制（矫正界面设置）。
                legacy_rules = False
                try:
                    from configmanage import get_config as _get_cfg

                    _pr_cfg = (_get_cfg(show_dialogs=False) or {}).get(
                        "proofread"
                    ) or {}
                    legacy_rules = bool(_pr_cfg.get("enable_legacy_rules"))
                except Exception:
                    legacy_rules = False  # 配置读取失败 → 只跑三条新规则
                errors = proofread_page(text, enable_legacy_rules=legacy_rules)
                # optional LLM enhancement: client must opt-in via use_llm flag.
                # 模型与开关持久化在 config.json（/api/proofread_settings），前端不再用 localStorage
                # （随机端口下 localStorage 每次运行失效，2026-08-07 修复）。失败不再静默吞掉，
                # 通过 llm_error 字段上浮给前端（基础 errors 照常返回）。
                use_llm = bool(body.get("use_llm"))
                llm_model = str(body.get("llm_model") or "").strip() or None
                llm_used = False
                llm_error = None
                if use_llm:
                    from configmanage import get_config

                    cfg = get_config(show_dialogs=False) or {}
                    model_choices = cfg.get("model_choices") or {}
                    pr_cfg = cfg.get("proofread") or {}
                    default_model = pr_cfg.get("llm_model")
                    model_key = str(
                        llm_model or default_model or cfg.get("selected_model") or ""
                    )
                    # 规范化模型键（去空白 + 大小写不敏感）后再校验，避免因大小写或空白被误判为未注册
                    _ambiguous_msg = None
                    if isinstance(model_choices, dict):
                        from configmanage import find_canonical_model_key

                        canonical, _matches = find_canonical_model_key(
                            model_choices, model_key.strip()
                        )
                        if canonical is not None:
                            model_key = canonical
                        elif _matches:
                            _ambiguous_msg = (
                                f"模型 '{model_key}' 存在多个大小写不敏感匹配 "
                                f"{_matches}，请使用精确键名或移除重复项"
                            )
                    if (
                        not isinstance(model_choices, dict)
                        or model_key not in model_choices
                    ):
                        llm_error = _ambiguous_msg or (
                            f"模型 '{model_key}' 未在配置中注册"
                            f"（可用: {', '.join(sorted(model_choices)) if isinstance(model_choices, dict) else '无'}）"
                        )
                    else:
                        llm_sugs, llm_err = _proofread_llm_enhance(
                            text, errors, model_key
                        )
                        if llm_err:
                            llm_error = llm_err
                        else:
                            llm_used = True
                            for s in llm_sugs:
                                # append non-overlapping suggestions（模型返回项可能缺字段：跳过非法项）
                                if (
                                    not isinstance(s, dict)
                                    or "start" not in s
                                    or "end" not in s
                                    or "wrong" not in s
                                ):
                                    continue
                                if not any(
                                    not (
                                        s["end"] <= e["start"] or s["start"] >= e["end"]
                                    )
                                    for e in errors
                                ):
                                    errors.append(s)
                self._send(
                    200,
                    self._json(
                        {
                            "ok": True,
                            "errors": errors,
                            "llm_used": llm_used,
                            "llm_error": llm_error,
                        }
                    ),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                import traceback

                tb = traceback.format_exc()
                # return traceback in response for local debugging; caller (UI) can show it
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e), "trace": tb}),
                    "application/json; charset=utf-8",
                )
            return
        if path == "/api/reocr":
            # 大模型重识别：对指定页重新 OCR，逐行逐字对比当前文本，差异以纠错标注返回。
            # 无状态 —— 只返回新 OCR 文本与 diff 标注，由浏览器叠加显示（不入 undo 快照）。
            tmp_path = None
            try:
                import llamamanage
                from configmanage import get_config

                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                state = self.server.state
                cfg = get_config(show_dialogs=False) or {}
                model_choices = cfg.get("model_choices") or {}
                pr_cfg = cfg.get("proofread") or {}
                model_key = str(
                    body.get("model")
                    or pr_cfg.get("llm_model")
                    or cfg.get("selected_model")
                    or ""
                )
                # 规范化模型键（去空白 + 大小写不敏感）后再校验，避免因大小写或空白被误判为未注册
                _ambiguous_msg = None
                if isinstance(model_choices, dict):
                    from configmanage import find_canonical_model_key

                    canonical, _matches = find_canonical_model_key(
                        model_choices, model_key.strip()
                    )
                    if canonical is not None:
                        model_key = canonical
                    elif _matches:
                        _ambiguous_msg = (
                            f"模型 '{model_key}' 存在多个大小写不敏感匹配 "
                            f"{_matches}，请使用精确键名或移除重复项"
                        )
                if (
                    not isinstance(model_choices, dict)
                    or model_key not in model_choices
                ):
                    self._send(
                        400,
                        self._json(
                            {
                                "ok": False,
                                "error": _ambiguous_msg
                                or (
                                    f"模型 '{model_key}' 未在配置中注册"
                                    f"（可用: {', '.join(sorted(model_choices)) if isinstance(model_choices, dict) else '无'}）"
                                ),
                            }
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                # 2026-08-09：重识别前先探测服务端已加载模型，避免「所选模型与服务不符」时
                # 静默用错模型 OCR，或把 llama-server 的 400 原样透出（用户报「选择 qwen4 报 400」）。
                model_name = (model_choices.get(model_key) or {}).get(
                    "name"
                ) or model_key
                probe = llamamanage._probe_server(model_name)
                if probe == "none":
                    proc = getattr(llamamanage, "_server_process", None)
                    if proc is not None and proc.poll() is None:
                        self._send(
                            200,
                            self._json(
                                {
                                    "ok": False,
                                    "error": "模型服务正在加载中，请稍候片刻后重试。",
                                }
                            ),
                            "application/json; charset=utf-8",
                        )
                    else:
                        self._send(
                            200,
                            self._json(
                                {
                                    "ok": False,
                                    "error": "未检测到运行中的模型服务，请先点击「启动服务」加载所选模型。",
                                }
                            ),
                            "application/json; charset=utf-8",
                        )
                    return
                if probe == "mismatch":
                    self._send(
                        200,
                        self._json(
                            {
                                "ok": False,
                                "error": (
                                    f"当前服务加载的模型与所选模型 {model_key} 不符。"
                                    "请先点击「停止服务」，再点击「启动服务」加载所选模型后重试。"
                                ),
                            }
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                # 2026-09-01：所选模型是视觉模型（配置了 mmproj），但当前运行的
                # llama-server 若为纯文本模式（未加载 --mmproj），收图会报 500
                # 「peg-native format」。发送前先探测一次多模态能力，命中则给出
                # 明确指引，避免把难懂的 500 直接抛给用户。
                sel_has_mmproj = bool((model_choices.get(model_key) or {}).get("mmproj"))
                mmproj_ok = llamamanage._probe_mmproj()
                if sel_has_mmproj and mmproj_ok is False:
                    self._send(
                        200,
                        self._json(
                            {
                                "ok": False,
                                "error": (
                                    f"当前服务为纯文本模式（未加载 mmproj 视觉投影），"
                                    f"无法对模型 {model_key} 执行重识别。"
                                    "请先点击「停止服务」，再点击「启动服务」加载所选视觉模型后重试。"
                                ),
                            }
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                try:
                    page_no = int(body.get("page"))
                except (TypeError, ValueError):
                    self._send(
                        400,
                        self._json({"ok": False, "error": "page 参数无效"}),
                        "application/json; charset=utf-8",
                    )
                    return
                img = _reocr_image(state, page_no)
                if img is None:
                    self._send(
                        404,
                        self._json(
                            {"ok": False, "error": f"第 {page_no} 页图像不可用"}
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                content_type, img_bytes = img
                ocr_prompt = cfg.get("ocr_prompt") or llamamanage.OCR_PROMPT
                # 2026-10-05：重识别走同一个 llama-server，长时间逐页重识别时宿主
                # RAM 会持续上涨（残余的每 token 匿名内存泄漏只有重启进程才回收）。
                # 阈值未配置时本调用零开销立即返回；参数与下方 peg-native 自动重启
                # 的 runserver 保持一致（单页请求 ctx_size=8192 + 视觉模型）。
                try:
                    llamamanage.maybe_recycle_server(
                        model_key, with_mmproj=True, ctx_size=8192
                    )
                except Exception as e:  # noqa: BLE001  回收失败不阻断重识别
                    print(f"[reocr] llama-server 回收检查失败（忽略）: {e}")
                with _proofread_engine_ctx():
                    res = llamamanage._request_image_new(
                        ocr_prompt,
                        "",
                        model_key=model_key,
                        # 重识别为纯 OCR 任务：thinking=True 会触发 Qwen 隐藏思考链长生成，
                        # KV 缓存暴涨占满显存且拖慢识别 ~7 倍（2026-08 修复）
                        thinking=False,
                        timeout=llamamanage.REQUEST_TIMEOUT,
                        img_bytes=img_bytes,
                        # 2026-08-31：把真实 MIME 传给 _request_image_new——页图为 PNG 时
                        # 若数据 URI 错标 image/jpeg，llama-server 解图失败返回 500
                        # （曾致「重识别失败:500 Server Error」）
                        content_type=content_type,
                    )
                    # 2026-09-01：自动修复——若命中 peg-native format（多模态推理失败，常因
                    # 纯文本模式服务误收图），且所选模型配置了 mmproj，先探测服务端多模态能力，
                    # 仅当确认为纯文本模式（probe_mmproj=False）时才自动重启视觉服务并重试一次。
                    # 若探测为视觉模式或探测不明，则判定为单页图片异常（过大/损坏/MIME异常），
                    # 不重启服务，直接返回单页失败提示（含图片大小便于自查）。
                    err_str = str(res.get("error") or "")
                    auto_heal_attempted = False
                    if (
                        res.get("error")
                        and sel_has_mmproj
                        and llamamanage._active_engine() == "llama"
                        and any(k in err_str for k in _LLM_PEG_MARKERS)
                    ):
                        # 先探测：当前服务是否真正加载了 mmproj（视觉投影）
                        mmproj_probe = llamamanage._probe_mmproj()
                        if mmproj_probe is False:
                            # 确认为纯文本模式：执行自动重启并重试
                            try:
                                llamamanage.stopserver()
                                time.sleep(0.5)
                                # ctx_size=8192：矫正/重识别为单页请求，8192 足够，
                                # 默认 16384 配置对纯文本/视觉单页无用且多占显存。
                                ok = llamamanage.runserver(model_key, with_mmproj=True, ctx_size=8192)
                                if ok:
                                    auto_heal_attempted = True
                                    res = llamamanage._request_image_new(
                                        ocr_prompt,
                                        "",
                                        model_key=model_key,
                                        thinking=False,
                                        timeout=llamamanage.REQUEST_TIMEOUT,
                                        img_bytes=img_bytes,
                                        content_type=content_type,
                                    )
                                    err_str = str(res.get("error") or "")
                            except Exception:
                                # 自动修复过程出错：静默忽略，走统一错误处理
                                pass
                        else:
                            # 探测为视觉模式 或 探测不明：判定为单页图片异常，不重启服务
                            # 先尝试按页降分辨率重试一次，再决定是否返回错误
                            img_size = len(img_bytes) if img_bytes else 0
                            retry_bytes = None
                            retry_ct = None
                            try:
                                # 以当前 _REOCR_MAX_SIDE 的一半为目标最大边，重新渲染更小的 JPEG
                                # 复用 preview_doc 与锁，避免重开 PDF
                                doc = _preview_doc(state)
                                lock = state.get("preview_doc_lock")
                                if (
                                    doc is not None
                                    and not getattr(doc, "is_closed", False)
                                    and 1 <= page_no <= doc.page_count
                                ):
                                    import fitz
                                    with lock if lock is not None else nullcontext():
                                        r = doc[page_no - 1].rect
                                    max_dim = max(r.width, r.height)
                                    if max_dim > 0:
                                        # 目标最大边 = _REOCR_MAX_SIDE // 2 (约 780px)，足够 OCR 且 token 大幅减少
                                        target_side = _REOCR_MAX_SIDE // 2
                                        dpi = (target_side * 72.0) / max_dim
                                        quality = int(state.get("preview_quality", 70))
                                        with lock if lock is not None else nullcontext():
                                            pix = doc[page_no - 1].get_pixmap(
                                                matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0),
                                                alpha=False,
                                            )
                                            retry_bytes = pix.tobytes("jpeg", jpg_quality=quality)
                                            retry_ct = "image/jpeg"
                            except Exception:
                                # 任何渲染异常静默回退，走原错误分支
                                retry_bytes = None
                                retry_ct = None

                            if retry_bytes and len(retry_bytes) < img_size:
                                # 用降采样图再次请求
                                res2 = llamamanage._request_image_new(
                                    ocr_prompt,
                                    "",
                                    model_key=model_key,
                                    thinking=False,
                                    timeout=llamamanage.REQUEST_TIMEOUT,
                                    img_bytes=retry_bytes,
                                    content_type=retry_ct,
                                )
                                if not res2.get("error"):
                                    # 重试成功：用新结果继续走正常流程
                                    res = res2
                                    err_str = ""
                                else:
                                    # 重试仍失败：更新错误信息并落入下方统一错误处理
                                    err_str = str(res2.get("error") or err_str)
                            if res.get("error"):
                                # 无重试或重试失败：构造友好提示并返回
                                friendly = _friendly_llm_error(err_str)
                                if retry_bytes:
                                    friendly += f"（该页图片可能过大/损坏/MIME异常，原始 {img_size} 字节→降采样 {len(retry_bytes)} 字节重试仍失败，已跳过自动重启；可尝试对该页单独降低分辨率或检查原图）"
                                else:
                                    friendly += f"（该页图片可能过大/损坏/MIME异常，大小 {img_size} 字节，已跳过自动重启；可尝试对该页单独降低分辨率或检查原图）"
                                self._send(
                                    200,
                                    self._json({"ok": False, "error": friendly}),
                                    "application/json; charset=utf-8",
                                )
                                return
                            # 重试成功：已用降采样图取得结果，落入下方成功分支
                if res.get("error"):
                    friendly = _friendly_llm_error(err_str)
                    # 若经历过自动修复尝试（上述分支已执行），在提示中追加说明
                    if auto_heal_attempted:
                        friendly += " （已尝试自动重启视觉服务仍失败，请手动点击「停止服务」再「启动服务」后重试）"
                    self._send(
                        200,
                        self._json(
                            {
                                "ok": False,
                                "error": friendly,
                            }
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                new_text = str(res.get("result") or "")
                # 2026-08-09：ULQ4/ULQ8 等 PaddleOCR 系模型输出带 bbox 坐标前缀与思考块，
                # 先剥离再繁简/标点归一，避免格式 token 被当成纠错项（用户报「ulq 输出含格式参数」）。
                if new_text:
                    from stringmanage import clean_bbox_text, strip_think_blocks, ttos

                    new_text = strip_think_blocks(new_text)
                    new_text = clean_bbox_text(new_text)
                    # 2026-08-08：重识别结果先繁转简再与当前文本对比（与 /api/proofread 的 ⑫b 一致）
                    # 2026-08-30：识别结果中的括号对（【x】/[x]/［x］ 等）在对比前统一
                    # 归一为 〔x〕（与 clean 流程一致），避免括号样式差异被当作纠错项；
                    # 括号内内容原样保留（此前 2026-08-28 是「不处理、保留原始输出」）
                    new_text = ttos(new_text)
                    # 2026-10：校正格式清理（设置界面「校正格式」栏）——剥离模型返回
                    # 文本中的 Markdown 标记 / LaTeX 公式与命令，避免无关格式字符被
                    # diff 逐字判为纠错项。默认关闭（enabled=False 时零开销）；
                    # 详见 clean_correction_format 与 config.json: correction_format。
                    from configmanage import get_correction_format

                    cf_opts = get_correction_format(cfg)
                    if cf_opts.get("enabled"):
                        new_text = clean_correction_format(
                            new_text,
                            strip_markdown=cf_opts["strip_markdown"],
                            strip_latex=cf_opts["strip_latex"],
                            remove_formula=cf_opts["remove_formula"],
                            custom_rules=cf_opts["custom_rules"],
                        )
                # 2026-08-23/28：模型可能把图片页脚的页码一并识别进来，先剥掉末尾页码
                # （第 N 页 / 字符+数字：页123·P123·No.123 / 括号包裹 / 独立成行裸数字），
                # 避免被当成正文差异标注；仅清理返回文字最末尾的页码，正文不受影响。
                # 须在英文标点归一（_full_punct）之前执行，否则 "No.123" 的 "." 被转成
                # "。" 后无法匹配页码样式。
                new_text = _strip_trailing_page_number(new_text)
                # 2026-08-09：再将英文标点归一为中文标点，避免半角/全角差异被当成纠错项
                new_text = _full_punct(new_text)
                # 2026-08-02：先把模型输出的 LaTeX 圈码 \textcircled{n}（可带 $ 包裹）
                # 还原为 ① 等，否则后续 {1} 的 { } 会被杂符括号清理误折叠为〔1〕，
                # 留下残缺的 $\textcircled〔1〕$（用户报「① 变 $\textcircled〔1〕$」）。
                new_text = _normalize_textcircled(new_text)
                # 2026-08-30：对比前做杂符括号清理 + 括号对统一（〔x〕）——
                # 部分大模型把原文 〔x〕 引注识别成 \\〔^{x〕}\\ 的杂符包裹格式
                # （\\ ^ { } 等无效字符夹着括号），须先折叠为 〔x〕 再与原文比较，
                # 否则杂符被逐字判为纠错项。括号对归一为逐字符 1:1 替换；
                # 杂符清理改变长度但只作用在模型返回文本侧（后文 current_text
                # 已由进入矫正界面时的清理保证无杂符，见 _page_text/initial_html）。
                new_text = _normalize_brackets(new_text)
                current_text = _proofread_plain_text(str(body.get("html") or ""))
                # 与 new_text 同样做半角→全角标点归一：否则相同内容因标点宽度差异
                # 被逐字判为差异，产生大量非预期位置的纠错标注（2026-08 修复）
                current_text = _full_punct(current_text)
                # 与 new_text 做同等括号归一：否则「原文是〔1〕、模型输出【1】」这种
                # 纯粹样式差异会被逐字判为差异（2026-08-30）
                current_text = _normalize_brackets(current_text)
                diff = diff_reocr_texts(current_text, new_text)
                self._send(
                    200,
                    self._json({"ok": True, "text": new_text, "diff": diff}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:  # noqa: BLE001
                self._send(
                    500,
                    self._json({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            finally:
                pass
            return
        if path == "/api/proofread_feedback":
            # 纠错反馈回写：accept → add_user_fix；ignore → ignore_word；支持批量 items。
            # 有状态持久化（写 data/proofread_dict.json），失败不阻塞前端 UI。
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                fb_type = str(body.get("type") or "")
                if fb_type not in ("accept", "ignore"):
                    self._send(
                        400,
                        self._json({"ok": False, "error": f"bad type: {fb_type}"}),
                        "application/json; charset=utf-8",
                    )
                    return
                if dictionarymanage is None:
                    self._send(
                        503,
                        self._json(
                            {"ok": False, "error": "dictionarymanage not ready"}
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                # 批量 accept（proofreadApplyCurrent 一次发多条）
                if fb_type == "accept" and isinstance(body.get("items"), list):
                    for item in body["items"]:
                        w = str(item.get("wrong") or "")
                        f = str(item.get("fixed") or "")
                        if w and f and w != f:
                            dictionarymanage.add_user_fix(w, f)
                elif fb_type == "accept":
                    w = str(body.get("wrong") or "")
                    f = str(body.get("fixed") or "")
                    if w and f and w != f:
                        dictionarymanage.add_user_fix(w, f)
                else:  # ignore
                    w = str(body.get("wrong") or "")
                    if w:
                        dictionarymanage.ignore_word(w)
                self._send(
                    200,
                    self._json({"ok": True}),
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
                if fmt not in ("txt", "docx", "epub", "md"):
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
                        # 默认名优先用重命名后的 display_name，回退 history_name（2026-08-30）
                        base = (
                            st.get("display_name")
                            or st.get("history_name")
                            or "矫正导出"
                        ).removesuffix(".pdf")
                        base = (base or "矫正导出").strip() or "矫正导出"
                        out_path = _default_export_path(f"{base}.{fmt}")
                # 与「矫正历史」另存为共用同一转换链路（保证两边输出一致）
                export_content_to_file(
                    items,
                    fmt,
                    str(out_path),
                    title=st.get("display_name")
                    or (st.get("history_name") or "矫正导出"),
                )
                self._send(
                    200,
                    self._json(
                        {"ok": True, "path": str(out_path), "used_dialog": used_dialog}
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
        if path == "/api/autosave":
            # 自动保存动作：与 /api/save 同载荷，但总是新建历史版本（后台静默）
            self._autosave_action()
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
            # 文字纠错状态（保存/暂存/完成时随历史缓存落盘）
            if body.get("proofread"):
                state["proofread"] = {
                    "errors": body["proofread"].get("errors") or {},
                    "original": body["proofread"].get("original") or {},
                    "dismissed": body["proofread"].get("dismissed") or {},
                }
            if body.get("last_proofread_page") is not None:
                try:
                    state["last_proofread_page"] = int(body["last_proofread_page"])
                except (TypeError, ValueError):
                    pass
            # 保存：不新建历史版本，直接覆盖当前缓存（同一份内容反复保存只更新
            # 同一个文件）；暂存/完成并转换仍各生成一个新历史版本（可随时恢复）
            # S4：写入失败返回 False → 前端报错提示（不静默丢数据）
            if path == "/api/save":
                # 打开指定历史版本时（state["history_id"]）定向覆盖该版本文件，
                # 不在版本列表产生新条目；普通会话按 mtime 覆盖最新版本。
                ok = _overwrite_history(state, history_id=state.get("history_id"))
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
                        {"page": n, "text": pages_snapshot[n]}
                        for n in sorted(pages_snapshot)
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
            self._send(
                500,
                self._json({"ok": False, "error": str(e)}),
                "application/json; charset=utf-8",
            )

    def log_message(self, fmt: str, *args: Any) -> None:
        # 静默访问日志，避免终端刷屏
        return


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


_CORRECT_WINDOW_TITLE = "ptoe 文字矫正"


def _open_display(url: str, title: str) -> tuple[str, object | None]:
    """默认用 pywebview 内嵌窗口加载界面；config.json gui_display='browser' 时改用浏览器。

    返回 (role, win) 元组：
    - role: "browser" | "owner" | "guest"
      * "browser": 浏览器模式或 pywebview 不可用，已回退 _open_browser，win 为 None
      * "owner": 当前进程为标签页宿主（owner），win 为 pywebview Window 对象
      * "guest": 当前进程作为 guest 加入已有合并窗口，不创建窗口，win 为 None
    - win: pywebview Window 对象（仅 owner 时非 None），调用方需在 webview.start() 前注册
      window.events.closed；browser/guest 模式为 None（调用方在主线程跑监视循环）。

    pywebview 窗口默认最大化（config.json window_maximized=true，缺省）。
    """
    try:
        from configmanage import get_config as _get_cfg

        _mode = (_get_cfg(show_dialogs=False) or {}).get("gui_display", "pywebview")
        _maximized = (_get_cfg(show_dialogs=False) or {}).get("window_maximized", True)
        # 兼容字符串 "false"/"0"
        if isinstance(_maximized, str):
            _maximized = _maximized.lower() not in ("false", "0")
        _maximized = bool(_maximized)
    except Exception:  # noqa: BLE001
        _mode = "pywebview"
        _maximized = True
    if _mode == "browser":
        _open_browser(url)
        return "browser", None
    try:
        import tabmanage  # noqa: PLC0415

        _role = tabmanage.register_tab(title, url, base_url=url)
    except Exception:  # noqa: BLE001  tabmanage 异常不阻塞，回退浏览器
        _open_browser(url)
        return "browser", None
    if _role == "guest":
        # guest 模式：不创建窗口，静默加入已有合并窗口
        return "guest", None
    try:
        import webview as _webview  # noqa: PLC0415

        _win = _webview.create_window(title, url + "/tabhost", maximized=_maximized)
    except Exception:  # noqa: BLE001  pywebview 不可用/初始化失败 → 浏览器兜底
        try:
            import tabmanage as _tm  # noqa: PLC0415

            _tm.reset_session()
        except Exception:
            pass
        _open_browser(url)
        return "browser", None
    return "owner", _win


def _open_browser(url: str) -> None:
    """打开外部浏览器访问矫正界面。

    优先使用 config.json 的 browser 键指定的浏览器路径，否则用系统默认浏览器。
    browser 模式（gui_display='browser'）或 pywebview 不可用/启动失败时使用。

    浏览器经 guimanage._spawn_detached **完全脱离**启动：常驻浏览器若继承宿主
    stdin/stdout/stderr 控制台句柄，宿主主流程结束也退不出去（Ctrl+C 无效，
    实测墙钟 45s vs 脱离后 2.3s）。惰性 import 避免 guimanage ← correctmanage
    顶层循环依赖（反之 guimanage 也只在函数内 import correctmanage）。
    """
    _br_path = ""
    try:
        from configmanage import get_config as _get_cfg

        _br_path = (_get_cfg(show_dialogs=False) or {}).get("browser", "")
    except Exception:  # noqa: BLE001
        pass
    try:
        if _br_path:
            from guimanage import _spawn_detached  # noqa: PLC0415

            _spawn_detached([_br_path, url])
        else:
            webbrowser.open(url)
    except Exception:  # noqa: BLE001  打不开浏览器不阻断服务
        webbrowser.open(url)


def _serve_loop(
    state: dict[str, Any],
    *,
    watch_gone: bool,
    tab_key: str | None = None,
    tab_base: str | None = None,
) -> None:
    """correct_pages 的监视主循环：逐轮处理保存对话框并监测界面关闭。

    watch_gone=False（pywebview 窗口模式）：窗口关闭由 window.events.closed
    置 finished，不依赖心跳/信标判定。

    tab_key/tab_base：guest 模式下传入（标签标题、可选的期望 owner 基址；
    缺省 None 时以会话内 owner_base 为准），每 ~2s 探测一次
    tabmanage.guest_session_ok()；若返回 False 则置 state["tab_lost"]=True 并 break，
    由 correct_pages 外层负责接管重建窗口。
    """
    stale_since: float | None = None
    _last_probe = 0.0
    while not state["finished"].is_set():
        if state["finished"].wait(0.5):
            break
        _drain_dialog_queue(state)
        if watch_gone:
            gone, stale_since = _browser_gone(state, stale_since=stale_since)
            if gone:
                state["auto_finished"] = True
                state["finished"].set()
                break
        # guest 模式：定期探测会话是否仍有效（owner 存活且自身标签在列表中）
        if tab_key is not None:
            now = time.monotonic()
            if now - _last_probe >= 2.0:
                _last_probe = now
                try:
                    import tabmanage as _tm  # noqa: PLC0415

                    if not _tm.guest_session_ok(tab_key, tab_base):
                        state["tab_lost"] = True
                        break
                except Exception:
                    # tabmanage 异常视为会话丢失，触发接管
                    state["tab_lost"] = True
                    break


# ---------------------------------------------------------------------------
# 实时跟随 OCR 进度（2026-10-21）
#   转换与矫正分离后，转换进行中也能启动矫正界面：识别完成的页面实时进入
#   矫正页（不必等整本转换结束）。
#   进度文件由 mian.py 写入：data/<PDF名>[_n]/.ocr_progress.json
#     {"pdf":..., "total":N, "status":"running|ocr_done",
#      "pages":{"<页码>":{"status":"done|error","result":"识别文本"}}}
# ---------------------------------------------------------------------------


def _ocr_progress_files(pdf_path: str | Path) -> list[Path]:
    """列出与该 PDF 相关的 OCR 进度文件（按修改时间倒序）。

    分页目录名是 PDF 名（同名重复时自动加 _1/_2 后缀），故按前缀匹配扫描。
    """
    try:
        stem = Path(pdf_path).stem
    except Exception:  # noqa: BLE001
        return []
    if not stem:
        return []
    try:
        from pdfmanage import app_base_dir

        data_dir = app_base_dir() / "data"
    except Exception:  # noqa: BLE001
        return []
    try:
        if not data_dir.is_dir():
            return []
        cands = [
            child / ".ocr_progress.json"
            for child in data_dir.iterdir()
            if child.is_dir() and child.name.startswith(stem)
        ]
        cands = [p for p in cands if p.is_file()]
    except Exception:  # noqa: BLE001
        return []
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands


def _read_ocr_progress(path: Path) -> dict[str, Any]:
    """读取 OCR 进度文件（损坏/不存在返回空 dict）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _seed_live_pages(state: dict[str, Any], path: Path) -> list[int]:
    """把进度文件里「已识别完成」的页面立即写入矫正界面（初始内容）。

    返回新增的页码列表。已存在的页（用户编辑过/历史载入）不覆盖。
    """
    data = _read_ocr_progress(path)
    pages = data.get("pages") or {}
    added: list[int] = []
    lock = state.get("pages_lock")
    with lock if lock is not None else nullcontext():
        for key, info in pages.items():
            if not isinstance(info, dict) or info.get("status") != "done":
                continue
            try:
                n = int(key)
            except (TypeError, ValueError):
                continue
            if n in state["pages"]:
                continue
            state["pages"][n] = _page_text(str(info.get("result") or ""))
            added.append(n)
        state["live_done"] = len(added)
        try:
            state["live_total"] = int(data.get("total") or 0)
        except (TypeError, ValueError):
            state["live_total"] = 0
        state["live_status"] = str(data.get("status") or "")
    return added


def _watch_ocr_progress(
    state: dict[str, Any], stop_event: threading.Event, interval: float = 2.0
) -> None:
    """后台轮询 OCR 进度文件，把新识别完成的页面塞进矫正界面。

    - 只新增「尚未出现在 state['pages'] 里」的页（不覆盖用户已编辑的页面）；
    - 每批新页共享一个版本号（live_version），前端按 since=版本号 增量拉取；
    - 页面内容与初始页一致地走 _page_text（标题归一为正文）。
    """
    raw_path = state.get("live_ocr_path")
    pdf_hint = state.get("live_ocr_pdf") or ""
    try:
        path = Path(raw_path) if raw_path else None
    except Exception:  # noqa: BLE001
        path = None
    while not stop_event.wait(interval):
        if path is None:
            # 进度文件尚未落盘（转换刚起步）：持续按 PDF 名查找，找到即接管
            if not pdf_hint:
                return
            _found = _ocr_progress_files(pdf_hint)
            if not _found:
                continue
            path = _found[0]
            state["live_ocr_path"] = str(path)
            _seed_live_pages(state, path)
            print(f"      实时跟随：已接入 OCR 进度文件 {path.parent.name}")
        data = _read_ocr_progress(path)
        if not data:
            continue
        pages = data.get("pages") or {}
        added: list[int] = []
        lock = state.get("pages_lock")
        try:
            with lock if lock is not None else nullcontext():
                version = int(state.get("live_version") or 0) + 1
                for key, info in pages.items():
                    if not isinstance(info, dict) or info.get("status") != "done":
                        continue
                    try:
                        n = int(key)
                    except (TypeError, ValueError):
                        continue
                    if n in state["pages"]:
                        continue
                    html = _page_text(str(info.get("result") or ""))
                    state["pages"][n] = html
                    state["live_added"][n] = {"version": version, "text": html}
                    added.append(n)
                if added:
                    state["live_version"] = version
        except Exception:  # noqa: BLE001  轮询异常不致命，下一轮继续
            continue
        state["live_done"] = sum(
            1
            for i in pages.values()
            if isinstance(i, dict) and i.get("status") == "done"
        )
        try:
            state["live_total"] = int(data.get("total") or 0)
        except (TypeError, ValueError):
            state["live_total"] = 0
        state["live_status"] = str(data.get("status") or "")
        if added:
            print(
                f"      实时跟随：新增 {len(added)} 页已识别内容"
                f"（{min(added)}-{max(added)}）"
            )


def correct_pages(
    pages: list[dict[str, Any]],
    *,
    pdf_path: str | Path | None = None,
    img_dir: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    preview_dpi: int = 90,
    preview_quality: int = 70,
    idle_timeout: int = 600,
    on_convert: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    history: bool = True,
    preload_history: bool = True,
    history_id: str | None = None,
    live_ocr: bool = False,
) -> list[dict[str, Any]]:
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

    history_id：指定打开的历史版本 id（版本文件名 stem，如 mian.py correct
    --history-id）。给定后初始内容直接载入该版本（不走 _load_latest_history），
    pdf_path 为空时用版本内记录的 pdf 兜底，保存（/api/save）定向覆盖该版本
    文件而非"按 mtime 取最新"。
    """
    # 指定历史版本优先：直接载入该版本内容，绕过"同一 PDF 最新版本"逻辑
    history_id_meta: dict[str, Any] | None = None
    if history_id:
        history_id_meta = _load_history_version(history_id)
        if history_id_meta is None:
            raise ValueError(f"历史记录不存在或已损坏：{history_id}")
        # 打开指定历史记录时强制启用历史缓存（否则保存无处可写）
        history = True
        if not pdf_path and history_id_meta.get("pdf"):
            pdf_path = history_id_meta["pdf"]
    ordered = sorted(
        (
            {"page": int(p["page"]), "text": str(p.get("text") or "")}
            for p in pages
            if "page" in p and "text" in p
        ),
        key=lambda x: x["page"],
    )
    # 历史缓存：同一 PDF 最新版本的矫正内容优先作为初始内容；
    # preload_history=False 时（重新识别后）不加载历史，避免旧暂存覆盖新识别文本；
    # history_id 给定时不走 _load_latest_history，直接用该版本内容。
    history_pages: dict[str, str] = (
        history_id_meta["pages"] if history_id_meta is not None else _history_pages_for_init(
            str(pdf_path), history=history, preload_history=preload_history
        )
    )
    if history_pages:
        loaded = sum(1 for p in ordered if str(p["page"]) in history_pages)
        if loaded:
            print(f"      已加载历史矫正记录（{loaded}/{len(ordered)} 页）")
    # 初始内容：ordered 之外的页（如 pdf_path=None 打开历史版本）一并并入——
    # 2026-08-15 说明：传入矫正界面的文本一律按正文展示——OCR 自动结构产生的
    # <h1>-<h6> 标题归一为 <p>（标题由用户在界面手动标记），保证界面所见
    # 与浏览器关闭后的返回结果一致；历史缓存内容按原样载入（normalize_headings=
    # False，2026-08-15 修复）——其中可能含用户手动设置的标题，不能再归一为
    # <p>（否则「保存后重开，已设置的标题格式丢失」）；OCR 自动标题的归一
    # 只在写入历史时做一次（_save_ocr_history），此处仅对无历史的原始 OCR 文本兜底归一。
    state_pages: dict[int, str] = {
        int(p["page"]): _page_text(
            str(history_pages.get(str(p["page"]), p["text"])),
            normalize_headings=str(p["page"]) not in history_pages,
        )
        for p in ordered
    }
    for _pno, _html in history_pages.items():
        try:
            _n = int(_pno)
        except (TypeError, ValueError):
            continue
        state_pages.setdefault(_n, _page_text(str(_html), normalize_headings=False))
    state: dict[str, Any] = {
        "pages": state_pages,
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
        # 文字纠错状态（保存/暂存/完成时随历史缓存落盘，加载时恢复）
        "proofread": {"errors": {}, "original": {}, "dismissed": {}},
        "last_proofread_page": None,
        # 预渲染页数上限：可经 config.json 顶层键 prerender_max_pages 覆盖
        "prerender_max_pages": _resolve_prerender_max(),
        "embedded_images": {},
        "history_id": None,
        # 实时跟随 OCR 进度（2026-10-21）：live_ocr=True 时后台轮询进度文件，
        # 识别完成的页面实时进入矫正界面（前端按 live_version 增量拉取）
        "live_ocr_path": None,
        "live_ocr_pdf": "",
        "live_version": 0,
        "live_added": {},
        "live_done": 0,
        "live_total": 0,
        "live_status": "",
    }
    # 指定历史版本（history_id）：把该版本的纠错状态/预览图等元数据灌入 state，
    # 保存（/api/save）时按 history_id 定向覆盖原文件而非"按 mtime 取最新"。
    if history_id_meta is not None:
        state["history_id"] = history_id
        if history_id_meta.get("proofread"):
            state["proofread"] = history_id_meta["proofread"]
        if history_id_meta.get("last_proofread_page") is not None:
            state["last_proofread_page"] = history_id_meta["last_proofread_page"]
        if history_id_meta.get("embedded_images"):
            state["embedded_images"] = history_id_meta["embedded_images"]
        # 展示名：优先 display_name，其次 name（无文件会话/打开历史记录时用作书名）
        _meta_entry = next(
            (it for it in _history_entries() if it["id"] == history_id), None
        )
        _shown = (
            str(_meta_entry.get("display_name") or _meta_entry.get("name") or "")
            if _meta_entry
            else ""
        )
        if _shown:
            state["history_name"] = _shown
            state["display_name"] = _shown
    elif history_pages:
        # 无指定历史版本、但载入了同一 PDF 最新历史内容时，把其中的纠错状态
        # （已识别未应用的错误/建议 + 已忽略集合 + 上次纠错页）一并回灌——
        # 否则「模型纠错后保存、重新打开」会丢失未落地的错误，需重新识别；
        # 2026-10-17 修复（正文仍由历史 pages 提供，纠错状态不得污染正文）。
        _latest_meta = _load_latest_history_meta(str(pdf_path))
        if _latest_meta:
            if _latest_meta.get("proofread"):
                state["proofread"] = _latest_meta["proofread"]
            if _latest_meta.get("last_proofread_page") is not None:
                state["last_proofread_page"] = _latest_meta["last_proofread_page"]
    server = ThreadingHTTPServer((host, port), _CorrectionHandler)
    server.daemon_threads = True
    server.state = state
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()
    # 实时跟随 OCR 进度（2026-10-21）：转换进行中启动矫正时，先把「已识别完成」
    # 的页面立即灌入初始内容（不必等第一轮轮询），再由后台线程持续跟进新页。
    if live_ocr and pdf_path:
        state["live_ocr_pdf"] = str(pdf_path)
        _progress_files = _ocr_progress_files(str(pdf_path))
        if _progress_files:
            state["live_ocr_path"] = str(_progress_files[0])
            _seeded = _seed_live_pages(state, _progress_files[0])
            print(
                f"      实时跟随 OCR 进度：已载入 {_seeded} 页"
                f"（{_progress_files[0].parent.name}）"
            )
        else:
            print("      未找到 OCR 进度文件，等待转换写入…")
        _live_stop = threading.Event()
        state["live_stop"] = _live_stop
        threading.Thread(
            target=_watch_ocr_progress, args=(state, _live_stop), daemon=True
        ).start()
    # 后台渐进式预渲染预览图：用户在浏览器编辑期间把所有页渲染好，
    # 首次保存/暂存/完成时 _build_embedded_images 全命中缓存直接返回，
    # 不再阻塞按钮。每页单独持锁 + 50ms 间隔，不阻塞 UI 预览/请求线程。
    threading.Thread(
        target=_prerender_embedded_images, args=(state,), daemon=True
    ).start()
    # 多进程预热预览图磁盘缓存：大书（≥_WARM_MIN_PAGES）提前并行渲染到
    # <pdf目录>/preview_cache/<prefix>_<dpi>/，/preview 命中后免渲染锁竞争。
    # 同一 (pdf, dpi) 只预热一次；线程内自检页数阈值，小书/异常静默跳过。
    if pdf_path:
        _warm_key = (str(pdf_path), int(preview_dpi))
        with _preview_warm_started:
            _warm_new = _warm_key not in _preview_warmed_keys
            if _warm_new:
                _preview_warmed_keys.add(_warm_key)
        if _warm_new:
            threading.Thread(
                target=_warm_preview_cache, args=(state,), daemon=True
            ).start()
    url = f"http://{server.server_address[0]}:{server.server_address[1]}/"
    print(f"      矫正界面已启动: {url}（对比原图与识别文字，完成后点「完成并转换」）")
    # 合并窗口 tab 标题（2026-08）：同一 PDF 多开矫正时标题必须唯一——否则
    # tabmanage._append_tab 按 title 去重会把两个矫正实例挤进同一个 tab。
    # 带端口/历史版本名保证多实例互不冲突。
    _pdf_name = str(Path(pdf_path).name) if pdf_path else ""
    _win_title = (
        f"矫正：{_pdf_name}（{server.server_address[1]}）"
        if _pdf_name
        else f"矫正：{state.get('history_name') or '手动录入'}（{server.server_address[1]}）"
    )
    # 记录服务信息 sidecar（列表结构，多实例互不覆盖；GUI 配置中心按 pid+port 区分），
    # 供 GUI 配置中心发现并恢复已存活的矫正界面
    _write_server_info(
        server.server_address[1],
        pdf=str(pdf_path) if pdf_path else "",
        history_id=history_id or "",
    )
    _win = None
    _role = None
    if open_browser:
        _role, _win = _open_display(url, _win_title)
        if _role in ("owner", "guest"):
            pass  # 合并窗口角色已由 tabmanage 决定（owner 建窗 / guest 静默加入）
        if _win is not None:
            _win.events.closed += lambda: state["finished"].set()

    def _start_tabbed_window() -> None:
        # 供 owner 首次进入与 guest 接管后复用：主线程 webview.start
        nonlocal _win, _role
        _role, _win = _open_display(url, _win_title)
        if _win is not None:
            _win.events.closed += lambda: state["finished"].set()
            try:
                import webview as _webview  # noqa: PLC0415
                _webview.start(func=lambda: _serve_loop(state, watch_gone=False))
            except Exception:  # noqa: BLE001
                print("pywebview 启动失败，改用浏览器打开界面")
                _open_browser(url)
                _serve_loop(state, watch_gone=True)
        else:
            _serve_loop(state, watch_gone=True)

    try:
        if _win is not None:
            # pywebview 模式（owner）：GUI 事件循环阻塞主线程；监视/对话框循环在
            # webview.start(func=...) 的独立线程运行。窗口关闭 → events.closed
            # 置 finished → func 循环退出 → start() 返回。
            try:
                import webview as _webview  # noqa: PLC0415
                _webview.start(func=lambda: _serve_loop(state, watch_gone=False))
            except Exception:  # noqa: BLE001
                print("pywebview 启动失败，改用浏览器打开界面")
                _open_browser(url)
                _serve_loop(state, watch_gone=True)
        elif _role == "guest":
            # 加入已有合并窗口：不建窗，监视会话；宿主关闭/本标签移除 → 接管
            _serve_loop(state, watch_gone=False,
                        tab_key=_win_title, tab_base=None)
            if state.get("tab_lost"):
                import tabmanage as _tm  # noqa: PLC0415
                _tm.reset_session()   # 清旧会话，重新注册即成为 owner
                _start_tabbed_window()
        else:
            _serve_loop(state, watch_gone=True)
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
        # 停止实时跟随 OCR 进度的后台线程（2026-10-21）
        _live_stop = state.get("live_stop")
        if _live_stop is not None:
            try:
                _live_stop.set()
            except Exception:
                pass
        # 仅当我们是 owner 时才清会话；guest Ctrl+C 不应删除 owner 的会话
        if _role == "owner":
            try:
                import tabmanage as _tm  # noqa: PLC0415
                _tm.reset_session()
            except Exception:
                pass
        # 清除服务信息 sidecar 中本实例（本 pid + 端口）条目——多实例并存时各清各的
        _clear_server_info(server.server_address[1])
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
            out = [
                {"page": n, "text": state["pages"][n]} for n in sorted(state["pages"])
            ]
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
:root{
  --accent:#3B6FFF;--accent-hover:#2D5AE0;--accent-soft:#E8EEFF;
  --border:#D8DEE6;--border-strong:#C4CCD8;
  --bg:#F4F6F9;--bg-elev:#FFFFFF;--bg-hover:#EEF2F7;--bg-active:#E1E8F0;
  --text:#1C2733;--text-muted:#5A6B7C;--text-dim:#8A97A6;
  --editor-font-size:14px;
  --toolbar-h:40px;--btn-size:26px;--radius:6px;--radius-sm:4px;
  --shadow-sm:0 1px 2px rgba(0,0,0,.06);--shadow-md:0 4px 12px rgba(0,0,0,.1);
  --glass-bg:rgba(255,255,255,.75);--glass-border:rgba(216,222,230,.6);
  --focus-ring:0 0 0 2px rgba(59,111,255,.35);
  --transition:0.15s ease;
}
.dark{
  --accent:#4D7FFF;--accent-hover:#6B94FF;--accent-soft:#1E2A4A;
  --border:#3A4256;--border-strong:#4A546E;
  --bg:#161B26;--bg-elev:#1E2535;--bg-hover:#262E42;--bg-active:#2E3852;
  --text:#E4E8F0;--text-muted:#9AA4B8;--text-dim:#707A94;
  --glass-bg:rgba(30,35,45,.75);--glass-border:rgba(58,66,86,.6);
  --focus-ring:0 0 0 2px rgba(77,127,255,.4);
  --shadow-sm:0 1px 2px rgba(0,0,0,.25);--shadow-md:0 4px 12px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
body[data-no-anim="1"] *,body[data-no-anim="1"] *::before,body[data-no-anim="1"] *::after{animation:none !important;transition:none !important;}
body{margin:0;font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;text-rendering:optimizeLegibility;background:var(--bg);color:var(--text);}
#toolbar{
  position:sticky;top:0;z-index:20;
  display:flex;align-items:center;gap:4px;
  min-height:var(--toolbar-h);padding:0 10px;
  background:var(--glass-bg);
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  border-bottom:1px solid var(--glass-border);
  flex-wrap:wrap;font-size:12px;
}
#toolbar .spacer{flex:1;}
#toolbar label{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:var(--text-muted);}
#prCount{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--text-muted);white-space:nowrap;}
#prCountNum{
  display:inline-flex;align-items:center;justify-content:center;
  min-width:20px;height:18px;padding:0 6px;
  background:rgba(224,32,32,.15);color:#E02020;
  font-weight:700;font-size:11px;border-radius:999px;
  border:1px solid rgba(224,32,32,.3);
}
.dark #prCountNum{background:rgba(224,32,32,.25);border-color:rgba(224,32,32,.4);}
#toolbar input[type=number]{
  width:64px;height:28px;padding:0 8px;
  border:1px solid var(--border);border-radius:var(--radius-sm);
  background:var(--bg-elev);color:var(--text);font:inherit;
  transition:border-color var(--transition),box-shadow var(--transition);
}
#toolbar input[type=number]:focus{
  outline:none;border-color:var(--accent);box-shadow:var(--focus-ring);
}
#toolbar select{
  height:28px;padding:0 28px 0 8px;
  border:1px solid var(--border);border-radius:var(--radius-sm);
  background:var(--bg-elev) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%235A6B7C' stroke-width='2'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E") no-repeat right 8px center;
  background-size:12px;color:var(--text);font:inherit;cursor:pointer;
  appearance:none;-webkit-appearance:none;
  transition:border-color var(--transition),box-shadow var(--transition);
}
#toolbar select:focus{outline:none;border-color:var(--accent);box-shadow:var(--focus-ring);}
.dark #toolbar select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%239AA4B8' stroke-width='2'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E");}

/* ---------- Ribbon 选项卡工具栏 ---------- */
/* 三段**纵向**堆叠：选项卡行 28px / 面板行（min-height 32px，窄窗口按组换行）/ 状态行 28px。
   2026-10 修复：此前 #toolbar 仍是旧的单行 flex 容器（display:flex 默认 row + flex-wrap），
   选项卡、面板、状态栏被当成同一行的 flex item 争宽、各自换行 → 工具栏内部错位、
   状态栏跑到面板右侧或挤成第二行、面板右侧按钮被挤出可视区。
   总高度随窗口宽度变化，故跳转落点不再写死 60px，改由 ui/app.js 的 toolbarOffset() 实测。 */
#toolbar{position:sticky;top:0;z-index:40;display:flex;flex-direction:column;align-items:stretch;flex-wrap:nowrap;gap:0;min-height:0;padding:0;}

/* 选项卡行：固定 28px，单行，不换行 */
.ribbon-tabs{display:flex;align-items:center;gap:0;height:28px;padding:0 6px;flex:0 0 auto;width:100%;box-sizing:border-box;border-bottom:1px solid var(--border);background:var(--bg-elev);}
.ribbon-tab{display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 12px;border:none;background:transparent;color:var(--text-muted);font:inherit;font-size:12px;cursor:pointer;border-bottom:2px solid transparent;transition:color var(--transition),border-color var(--transition),background var(--transition);white-space:nowrap;}
.ribbon-tab:hover{color:var(--accent);background:var(--bg-hover);}
.ribbon-tab.active{color:var(--accent);border-bottom-color:var(--accent);background:var(--bg-elev);font-weight:600;}

/* 内容面板：min-height 32px 自适应高度；**不能** overflow:hidden——
   下拉菜单（包围/上下标/格式刷/导出/更多）是面板内 .tb-more 的绝对定位子元素，
   裁剪后整个菜单被裁掉，表现为「点了按钮没反应」；窄窗口按组换行，按钮不会被挤出可视区。 */
.ribbon-panel{display:none;align-items:center;gap:6px;min-height:32px;padding:3px 6px;flex:0 0 auto;width:100%;box-sizing:border-box;flex-wrap:wrap;white-space:nowrap;overflow:visible;}
.ribbon-panel.active{display:flex;}
.ribbon-panel .tb-group{display:inline-flex;align-items:center;gap:2px;padding:0 3px;height:28px;background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);white-space:nowrap;transition:background var(--transition),border-color var(--transition),box-shadow var(--transition);}
.ribbon-panel .tb-group:hover{background:var(--bg-hover);border-color:var(--border-strong);}
.ribbon-panel .tb-group:focus-within{border-color:var(--accent);box-shadow:var(--focus-ring);}

/* 面板内的分隔线 */
.ribbon-sep{width:1px;height:20px;background:var(--border);margin:0 4px;display:inline-block;vertical-align:middle;}

/* Icon / text buttons（沿用原变量，仅缩小适配 Ribbon 行高） */
#toolbar .ic-btn{width:var(--btn-size);height:var(--btn-size);padding:0;display:inline-flex;align-items:center;justify-content:center;border:none;background:transparent;color:var(--text);border-radius:var(--radius-sm);cursor:pointer;transition:background var(--transition),color var(--transition),transform .05s ease;}
#toolbar .ic-btn:hover{background:var(--bg-hover);color:var(--accent);}
#toolbar .ic-btn:active{background:var(--bg-active);transform:scale(.96);}
#toolbar .ic-btn:focus-visible{outline:none;box-shadow:var(--focus-ring);}
#toolbar .ic-btn:disabled{opacity:.4;cursor:default;pointer-events:none;}
#toolbar .ic-btn svg{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;}
#toolbar .ic-btn.active{background:var(--accent-soft);color:var(--accent);}

#toolbar .tb-btn{display:inline-flex;align-items:center;justify-content:center;height:26px;padding:0 7px;border:1px solid var(--border);background:var(--bg-elev);color:var(--text);border-radius:var(--radius-sm);font:inherit;font-size:12px;cursor:pointer;transition:background var(--transition),border-color var(--transition),color var(--transition),transform .05s ease;white-space:nowrap;}
/* 2026-10 修复：带图标的文字按钮（插入面板的 图片/表格/公式/分隔线/分栏/颜色）此前没有
   svg 尺寸约束 → 无 width/height 的 <svg> 默认 300×150、fill 默认黑，把按钮撑成黑块并
   把文字挤没（截图里的黑方块/竖条）。统一 14px 线性图标，与 .ic-btn/.tb-more-btn 一致。 */
#toolbar .tb-btn svg{width:14px;height:14px;flex:0 0 auto;margin-right:4px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;}
#toolbar .tb-btn:hover{background:var(--bg-hover);border-color:var(--border-strong);color:var(--accent);}
#toolbar .tb-btn:active{background:var(--bg-active);transform:scale(.98);}
#toolbar .tb-btn:focus-visible{outline:none;box-shadow:var(--focus-ring);}
#toolbar .tb-btn:disabled{opacity:.5;cursor:not-allowed;}

/* 主操作按钮（保留原有视觉区分） */
#toolbar #saveBtn{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600;padding:0 14px;}
#toolbar #saveBtn:hover{background:var(--accent-hover);border-color:var(--accent-hover);color:#fff;}
#toolbar #finishBtn{background:#1a7f37;border-color:#1a7f37;color:#fff;font-weight:600;padding:0 12px;box-shadow:var(--shadow-sm);}
#toolbar #finishBtn:hover{background:#15692e;border-color:#15692e;box-shadow:var(--shadow-md);transform:translateY(-1px);}

/* 下拉菜单 */
#toolbar .tb-more{position:relative;}
/* 高度 26px：与 .ic-btn/.tb-btn 对齐，且能放进 28px 高的 .tb-group（1px 上下边框内剩 26px） */
#toolbar .tb-more-btn{display:inline-flex;align-items:center;gap:4px;height:26px;padding:0 10px;border:1px solid var(--border);background:var(--bg-elev);color:var(--text);border-radius:var(--radius-sm);font:inherit;font-size:12px;cursor:pointer;transition:background var(--transition),border-color var(--transition);}
#toolbar .tb-more-btn:hover{background:var(--bg-hover);border-color:var(--border-strong);}
/* 下拉展开态：按钮保持高亮，用户能看出当前是哪个下拉打开着 */
#toolbar .tb-more.open > .tb-more-btn,#toolbar .tb-more-btn.active{background:var(--accent-soft);border-color:var(--accent);color:var(--accent);}
#toolbar .tb-more-btn svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:2;}
/* max-height + 滚动：包围菜单条目较多，小窗口下不至于溢出视口底部点不到 */
#toolbar .tb-more-panel{position:absolute;top:calc(100% + 4px);right:0;z-index:30;min-width:160px;max-height:70vh;overflow-y:auto;padding:6px;background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow-md);display:none;flex-direction:column;gap:2px;}
#toolbar .tb-more.open .tb-more-panel{display:flex;}
#toolbar .tb-more-panel .tb-btn{width:100%;text-align:left;justify-content:flex-start;}
#toolbar .tb-more-panel .tb-btn:active{background:var(--bg-active);}

/* 按压反馈水波 */
#toolbar .ic-btn,#toolbar .tb-btn{position:relative;overflow:hidden;}
#toolbar .ic-btn::after,#toolbar .tb-btn:not(.loading)::after{content:'';position:absolute;left:50%;top:50%;width:10px;height:10px;margin:-5px 0 0 -5px;border-radius:50%;background:currentColor;opacity:0;pointer-events:none;transform:scale(0);}
@keyframes ptoeRipple{from{opacity:.25;transform:scale(0);}to{opacity:0;transform:scale(7);}}
#toolbar .ic-btn:active::after,#toolbar .tb-btn:not(.loading):active::after{animation:ptoeRipple .25s ease-out;}

/* 进度条与状态（底部常驻，不随 Ribbon 选项卡切换） */
#toolbar .tb-statusbar{display:flex;align-items:center;gap:8px;height:28px;width:100%;box-sizing:border-box;flex:0 0 auto;flex-wrap:nowrap;white-space:nowrap;background:var(--bg-elev);border-top:1px solid var(--border);padding:0 10px;}
#toolbar .tb-status{display:inline-flex;align-items:center;font-size:11px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:0 4px;}

/* 窄窗口：按钮文字隐藏，仅保留图标/缩写 */
@media (max-width:1200px){
  #toolbar .tb-btn{padding:0 6px;font-size:11px;}
  #toolbar .tb-more-btn{padding:0 8px;}
  #toolbar .tb-btn span{display:none;}
}
@media (max-width:900px){
  .ribbon-panel{flex-wrap:wrap;height:auto;min-height:32px;overflow:visible;white-space:normal;}
  .ribbon-panel .tb-group{margin-bottom:2px;}
  .ribbon-tabs{flex-wrap:wrap;height:auto;}
}

/* Groups as pills */
#toolbar .tb-group{
  display:inline-flex;align-items:center;gap:1px;
  padding:0 3px;height:28px; /* 与 .ribbon-panel 行高一致（此处 ID 选择器会覆盖上面的 28px 声明） */
  background:var(--bg-elev);border:1px solid var(--border);
  border-radius:var(--radius);
  white-space:nowrap;
  transition:background var(--transition),border-color var(--transition),box-shadow var(--transition);
}
#toolbar .tb-group:hover{background:var(--bg-hover);border-color:var(--border-strong);}
#toolbar .tb-group:focus-within{border-color:var(--accent);box-shadow:var(--focus-ring);}

/* Icon buttons */
#toolbar .ic-btn{
  width:var(--btn-size);height:var(--btn-size);padding:0;
  display:inline-flex;align-items:center;justify-content:center;
  border:none;background:transparent;color:var(--text);
  border-radius:var(--radius-sm);cursor:pointer;
  transition:background var(--transition),color var(--transition),transform .05s ease;
}
#toolbar .ic-btn:hover{background:var(--bg-hover);color:var(--accent);}
#toolbar .ic-btn:active{background:var(--bg-active);transform:scale(.96);}
#toolbar .ic-btn:focus-visible{outline:none;box-shadow:var(--focus-ring);}
#toolbar .ic-btn:disabled{opacity:.4;cursor:default;pointer-events:none;}
#toolbar .ic-btn svg{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;}
#toolbar .ic-btn.active{background:var(--accent-soft);color:var(--accent);}

/* Text buttons */
#toolbar .tb-btn{
  height:26px;padding:0 7px;
  border:1px solid var(--border);background:var(--bg-elev);color:var(--text);
  border-radius:var(--radius-sm);font:inherit;font-size:12px;cursor:pointer;
  transition:background var(--transition),border-color var(--transition),color var(--transition),transform .05s ease;
}
#toolbar .tb-btn:hover{background:var(--bg-hover);border-color:var(--border-strong);color:var(--accent);}
#toolbar .tb-btn:active{background:var(--bg-active);transform:scale(.98);}
#toolbar .tb-btn:focus-visible{outline:none;box-shadow:var(--focus-ring);}
#toolbar .tb-btn:disabled{opacity:.5;cursor:not-allowed;}
/* 按压反馈：统一微暗 + 微缩，并叠加 CSS-only 水波脉动（不依赖 JS 点击坐标） */
#toolbar .ic-btn,#toolbar .tb-btn{position:relative;overflow:hidden;}
#toolbar .ic-btn::after,#toolbar .tb-btn:not(.loading)::after{content:'';position:absolute;left:50%;top:50%;width:10px;height:10px;margin:-5px 0 0 -5px;border-radius:50%;background:currentColor;opacity:0;pointer-events:none;transform:scale(0);}
@keyframes ptoeRipple{from{opacity:.25;transform:scale(0);}to{opacity:0;transform:scale(7);}}
#toolbar .ic-btn:active::after,#toolbar .tb-btn:not(.loading):active::after{animation:ptoeRipple .25s ease-out;}
#toolbar .tb-btn.primary{
  background:var(--accent);border-color:var(--accent);color:#fff;
}
#toolbar .tb-btn.primary:hover{background:var(--accent-hover);border-color:var(--accent-hover);}

/* Prominent action buttons */
#toolbar #stageBtn{background:transparent;border-color:var(--border);color:var(--text);}
#toolbar #stageBtn:hover{background:var(--bg-hover);border-color:var(--accent);color:var(--accent);}
#toolbar #saveBtn{
  background:var(--accent-soft);border-color:var(--accent);color:var(--accent);
  font-weight:600;padding:0 14px;
}
#toolbar #saveBtn:hover{background:var(--accent);border-color:var(--accent);color:#fff;}
#toolbar #finishBtn{
  background:var(--accent);border-color:var(--accent);color:#fff;
  font-weight:600;padding:0 12px;box-shadow:var(--shadow-sm);
}
#toolbar #finishBtn:hover{background:var(--accent-hover);box-shadow:var(--shadow-md);transform:translateY(-1px);}
#toolbar #finishBtn:active{transform:translateY(0);}

/* Dropdown for "更多" */
#toolbar .tb-more{position:relative;}
#toolbar .tb-more-btn{
  display:inline-flex;align-items:center;gap:4px;
  height:26px;padding:0 10px;
  border:1px solid var(--border);background:var(--bg-elev);color:var(--text);
  border-radius:var(--radius-sm);font:inherit;font-size:12px;cursor:pointer;
  transition:background var(--transition),border-color var(--transition);
}
#toolbar .tb-more-btn:hover{background:var(--bg-hover);border-color:var(--border-strong);}
#toolbar .tb-more-btn:focus-visible{outline:none;box-shadow:var(--focus-ring);}
#toolbar .tb-more-btn svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:2;}
#toolbar .tb-more-panel{
  position:absolute;top:calc(100% + 4px);right:0;z-index:30;
  min-width:160px;padding:6px;
  background:var(--bg-elev);border:1px solid var(--border);
  border-radius:var(--radius);box-shadow:var(--shadow-md);
  display:none;flex-direction:column;gap:2px;
}
#toolbar .tb-more.open .tb-more-panel{display:flex;}
#toolbar .tb-more-panel .tb-btn{width:100%;text-align:left;justify-content:flex-start;}
#toolbar .tb-more-panel .tb-btn:active{background:var(--bg-active);}

/* Progress bar (center zone) */
#progressWrap{flex-shrink:1;}
#progressBar{transition:width 0.15s ease;}
#progressPct{font-variant-numeric:tabular-nums;}
#errBadge{transition:background var(--transition),border-color var(--transition),transform .05s ease;}
#errBadge:hover{background:rgba(224,32,32,.18);border-color:rgba(224,32,32,.45);transform:scale(1.05);}
#errBadge:active{transform:scale(0.97);}
#errBadgeNum{font-variant-numeric:tabular-nums;}
.page-err-badge{display:inline-block;margin-left:8px;padding:0 6px;border-radius:9px;font-size:11px;font-weight:700;line-height:17px;color:#fff;background:#E02020;vertical-align:middle;}
.page-err-badge[hidden]{display:none;}

/* 保存 / 完成并转换 强视觉区分 */
#toolbar #saveBtn{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600;padding:0 14px;}
#toolbar #saveBtn:hover{background:var(--accent-hover);border-color:var(--accent-hover);color:#fff;}
#toolbar #finishBtn{background:#1a7f37;border-color:#1a7f37;color:#fff;font-weight:600;padding:0 12px;box-shadow:var(--shadow-sm);}
#toolbar #finishBtn:hover{background:#15692e;border-color:#15692e;box-shadow:var(--shadow-md);transform:translateY(-1px);}
#toolbar #finishBtn:active{transform:translateY(0);}

/* 完成确认弹窗 */
#finishConfirmBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#finishConfirmBg .modal{background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow-md);padding:16px;min-width:320px;max-width:90vw;}
#finishConfirmTitle{margin:0 0 8px;font-size:15px;color:var(--text);}
#finishConfirmMsg{margin:0 0 16px;font-size:13px;color:var(--text-muted);line-height:1.5;}
#finishConfirmActions{display:flex;justify-content:flex-end;gap:8px;}
#finishConfirmCancel{padding:6px 14px;font-size:13px;}
#finishConfirmOk{padding:6px 14px;font-size:13px;background:#1a7f37;border-color:#1a7f37;color:#fff;font-weight:600;}
#finishConfirmOk:hover{background:#15692e;border-color:#15692e;color:#fff;}

/* 状态（紧凑） */
#toolbar .tb-status{display:inline-flex;align-items:center;font-size:11px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:0 4px;}

/* Status (truncate long text, never force toolbar reflow) */
#status{font-size:12px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px;}
/* 2026-10 修复：状态行高 28px，这几项曾写死 height:32px（旧单行工具栏遗留）→ 在状态行内上下溢出 2px、与相邻元素错位 */
#pos{font-size:12px;color:var(--text-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:80px;flex-shrink:0;}
#pagePos{font-size:12px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100px;flex-shrink:0;}
#prLastPage{font-size:12px;color:var(--accent);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100px;flex-shrink:0;}
/* Status spans as standalone flex items in .tb-right (wrap independently of action buttons) */
#toolbar .tb-status{
  display:inline-flex;align-items:center;
  font-size:12px;color:var(--text-muted);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  padding:0 4px;
}

/* Global button styles (outside toolbar) */
button{font:inherit;padding:5px 11px;border:1px solid var(--border);border-radius:4px;background:var(--bg-elev);color:var(--text);cursor:pointer;transition:background var(--transition),border-color var(--transition),color var(--transition),transform .05s ease;}
button:hover{border-color:var(--accent);color:var(--accent);}
button:active{background:var(--bg-active);border-color:var(--border-strong);transform:scale(.97);}
button.active{border-color:var(--accent);background:var(--accent-soft);color:var(--accent);}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent);}
button.primary:hover{background:var(--accent-hover);color:#fff;}
button.primary:active{background:var(--accent-hover);border-color:var(--accent-hover);}
button:disabled{opacity:.5;cursor:not-allowed;}
select{font:inherit;padding:5px 8px;border:1px solid var(--border);border-radius:4px;background:var(--bg-elev);color:var(--text);}

/* 底部居中、不贴边（bottom 固定偏移 + left 50% 居中），3s 自动消失；
   fixed 定位随窗口缩放保持相对位置不变，不会贴边或被裁切 */
#toast{position:fixed;bottom:30px;left:50%;transform:translateX(-50%);z-index:96;display:flex;flex-direction:column;gap:6px;align-items:center;pointer-events:none;}
.toast{background:#1c2733;color:#fff;font-size:13px;line-height:1.5;padding:8px 16px;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.25);opacity:0;transform:translateY(-6px);transition:opacity .2s,transform .2s,background .2s ease,color .2s ease;max-width:70vw;}
.toast.show{opacity:1;transform:translateY(0);}
.toast.ok{background:#1a7f37;}
.toast.fail{background:#c0392b;}
.toast.warn{background:#b8860b;}
/* 预览图加载提示胶囊（底部居中）：加载中显示，完成后闪现「加载完成」 */
#loadHint{position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(8px);z-index:70;background:#334155;color:#fff;font-size:13px;padding:6px 14px;border-radius:16px;opacity:0;pointer-events:none;transition:opacity .18s ease,transform .18s ease;box-shadow:0 2px 10px rgba(0,0,0,.25);}
#loadHint.show{opacity:1;transform:translateX(-50%) translateY(0);}
#loadHint.done{background:#166534;}
/* 行级微光占位：预览图未就绪时填充图片区域，替代纯白 */
.ptoe-img-loading{background:linear-gradient(100deg,#eceff3 40%,#f7f9fb 50%,#eceff3 60%);background-size:200% 100%;animation:ptoeShimmer 1.1s linear infinite;}
@keyframes ptoeShimmer{to{background-position:-200% 0;}}
button.loading::after{content:'';display:inline-block;width:11px;height:11px;margin-left:8px;border:2px solid rgba(255,255,255,.45);border-top-color:#fff;border-radius:50%;animation:ptoe-spin .8s linear infinite;vertical-align:-2px;}
@keyframes ptoe-spin{to{transform:rotate(360deg);}}
/* U11 入场动画：菜单/弹窗出现时轻微上浮淡入；尊重系统「减少动态效果」偏好 */
@keyframes ptoeFadeIn{from{opacity:0;transform:translateY(-4px);}to{opacity:1;transform:none;}}
@keyframes ptoeFade{from{opacity:0;}to{opacity:1;}}
.anim-in{animation:ptoeFadeIn .16s ease both;}
.anim-fade{animation:ptoeFade .16s ease both;}
#contextMenu,#popup{animation:ptoeFadeIn .16s ease both;}
#searchModalBg,#exportModalBg,#indentModalBg,#tableModalBg,#charFormatModalBg,#rubyModalBg,#latexModalBg,#finishModalBg,#historyModalBg,#helpModalBg,#formatRulesModalBg,#frRuleModalBg,#replaceRulesModalBg,#rrEditModalBg{animation:ptoeFade .16s ease both;}
@media (prefers-reduced-motion: reduce){
  .anim-in,.anim-fade,#contextMenu,#popup,#searchModalBg,#exportModalBg,#indentModalBg,#tableModalBg,#charFormatModalBg,#rubyModalBg,#latexModalBg,#finishModalBg,#historyModalBg,#helpModalBg,#formatRulesModalBg,#frRuleModalBg,#replaceRulesModalBg,#rrEditModalBg{animation:none !important;}
  button,#errBadge,.page-row,.toast{transition:none !important;transform:none !important;}
  #toolbar .ic-btn::after,#toolbar .tb-btn:not(.loading)::after{animation:none !important;content:none !important;}
}
/* U3：hintbar 可折叠（✕ 关闭，仅本次会话生效；刷新后恢复） */
#hintbar{padding:6px 14px;font-size:12px;color:#5a6b7c;background:#eef3fb;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;}
#hintbar .hint-text{flex:1;}
#hintClose{flex:none;width:22px;height:22px;padding:0;line-height:1;border:none;background:transparent;color:#8a97a6;font-size:14px;border-radius:4px;}
#hintClose:hover{background:#dfe7f3;color:#1c2733;border:none;}
#hintbar.hidden{display:none;}
#pages{position:relative;overflow-anchor:none;}
/* 宽度基准动态行高（2026-08）：行高由左侧图片按栏宽等比撑出（服务端 /api/pages
   下发各页原始宽高，前端预计算 heights[]，未挂载行前缀和也精确 → 跳转瞬时定位）。
   每页用各自真实宽高比，个别异常大小页面只影响自身行高，不干扰其他页。
   文字窗内容超出图片高度时在行内滚动（height:0+min-height:100% 不参与撑行）。 */
.page-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;align-items:stretch;background:#fff;border:1px solid var(--border);border-radius:8px;padding:12px;transition:transform var(--transition),box-shadow var(--transition);}
.page-row:hover{transform:translateY(-1px);box-shadow:var(--shadow-md);}
.page-head{grid-column:1 / -1;font-size:12px;color:#5a6b7c;border-bottom:1px dashed var(--border);padding-bottom:6px;}
.img-panel{position:relative;min-width:0;overflow:hidden;background:#fff;border:1px solid var(--border);border-radius:4px;padding:4px;}
.img-panel img{width:100%;height:auto;display:block;background:#fff;cursor:zoom-in;}
.badge{position:absolute;top:8px;left:8px;background:rgba(0,0,0,.55);color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;pointer-events:none;}
/* overflow-x 显式声明（2026-10）：此前只写 overflow-y:auto，横向滚动条靠
   「overflow-x 的 visible 与 overflow-y 的非 visible 同时出现时，visible 计算为
   auto」这条隐式规则才成立；一旦 overflow-y 被改成 visible（如窄屏媒体查询），
   溢出内容就会直接顶出编辑区。宽表格/长图超宽时显式 auto 保证出横滚条 */
.editable{height:0;min-height:100%;overflow-y:auto;overflow-x:auto;padding:10px 14px;border:1px solid var(--border);border-radius:4px;line-height:1.7;font-size:var(--editor-font-size);outline:none;}
.editable:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(47,111,237,.15);}
.editable h1{font-size:1.5em;} .editable h2{font-size:1.38em;} .editable h3{font-size:1.26em;}
.editable h4{font-size:1.16em;} .editable h5{font-size:1.07em;} .editable h6{font-size:1em;}
/* 标题级别指示（2026-09）：h1-h6 块显示「H1..H6」徽章（::before 伪元素）+
   级别色浅底条，一眼区分各级标题与正文。纯显示层——::before 不序列化，
   innerHTML/历史/导出正文均不含徽章；正文段落与 ptoe-note/ptoe-marker 样式不变。 */
.editable h1,.editable h2,.editable h3,.editable h4,.editable h5,.editable h6{border-radius:3px;}
.editable h1{--h-lv:#2D5AE0;background:rgba(45,90,224,.09);}
.editable h2{--h-lv:#3B6FFF;background:rgba(59,111,255,.07);}
.editable h3{--h-lv:#4E7CE8;background:rgba(78,124,232,.07);}
.editable h4{--h-lv:#6E96ED;background:rgba(110,150,237,.07);}
.editable h5{--h-lv:#8FADF2;background:rgba(143,173,242,.06);}
.editable h6{--h-lv:#A8C0F7;background:rgba(168,192,247,.05);}
.editable h1::before,.editable h2::before,.editable h3::before,
.editable h4::before,.editable h5::before,.editable h6::before{
  display:inline-block;margin-right:6px;padding:2px 5px;border-radius:3px;
  font-size:11px;font-weight:700;line-height:1;vertical-align:1px;
  background:var(--h-lv);user-select:none;-webkit-user-select:none;pointer-events:none;
}
.editable h1::before{content:"H1";color:#fff;}
.editable h2::before{content:"H2";color:#fff;}
.editable h3::before{content:"H3";color:#fff;}
.editable h4::before{content:"H4";color:#1C2733;}
.editable h5::before{content:"H5";color:#1C2733;}
.editable h6::before{content:"H6";color:#5A6B7C;}
/* 文字页面行号（2026-10）：#pages 带 .ptoe-line-num 时，每页 .editable 的直属块
   （p/div/h1-6）用 CSS 计数器编号。counter-reset 落在**每个 .editable** 上 →
   每页从 1 重新起算，与原书页码无关。行号写在 ::after 上——伪元素内容不进
   innerHTML，故 syncContent/collect/保存/导出都不含行号，存储源保持原样。
   块需 position:relative 才能把绝对定位的 ::after 锚在本块左上（否则会以
   .page-row/#pages 为定位祖先全部叠在一处）。命令行寻址口径与之一致：只数
   .editable 的直属 p/div/h1-6（table 等非文字块不编号、也不可寻址）。 */
/* 尺寸档位（2026-10 调整）：行号最多 3 位数（单页一般 < 500），列宽按 3 位等宽
   数字留一点余量即可，故三档均比原先收窄；--ln-numw/--ln-gap/--ln-pad 统一用 px，
   不再用 em——否则 --ln-pad 会随正文字号放大、而行号字号固定，越调越宽且对不齐。
   --ln-pad 只到「灰底行号栏 + 分隔线」为止；正文另加 --ln-textgap 留白，避免首列
   文字贴着分隔线。档位值满足 --ln-pad = --ln-numw + --ln-gap + 左侧少量余量。 */
#pages.ptoe-line-num{--ln-font:12px;--ln-numw:22px;--ln-gap:16px;--ln-pad:44px;--ln-textgap:6px;}
#pages.ptoe-line-num.ptoe-ln-md{--ln-font:11px;--ln-numw:20px;--ln-gap:15px;--ln-pad:40px;}
#pages.ptoe-line-num.ptoe-ln-sm{--ln-font:10px;--ln-numw:18px;--ln-gap:13px;--ln-pad:35px;}
#pages.ptoe-line-num .editable{padding-left:calc(var(--ln-pad,44px) + var(--ln-textgap,6px));counter-reset:ptoe-ln;
  /* 行号栏独立背景：inset 阴影画出左侧留白灰底 + 1px 右侧分隔线，与正文白底明显
     区分。inset 阴影固定在元素盒（不随内容滚动），长文档滚动时行号栏始终铺满。 */
  box-shadow:inset calc(var(--ln-pad,44px) - 1px) 0 0 var(--ln-gutter,#eef1f6),
             inset var(--ln-pad,44px) 0 0 var(--ln-border,#d3dae3);}
#pages.ptoe-line-num .editable:focus{
  box-shadow:inset calc(var(--ln-pad,44px) - 1px) 0 0 var(--ln-gutter,#eef1f6),
             inset var(--ln-pad,44px) 0 0 var(--ln-border,#d3dae3),
             0 0 0 2px rgba(47,111,237,.15);}
#pages.ptoe-line-num .editable > p,
#pages.ptoe-line-num .editable > div,
#pages.ptoe-line-num .editable > h1,
#pages.ptoe-line-num .editable > h2,
#pages.ptoe-line-num .editable > h3,
#pages.ptoe-line-num .editable > h4,
#pages.ptoe-line-num .editable > h5,
#pages.ptoe-line-num .editable > h6{position:relative;counter-increment:ptoe-ln;}
#pages.ptoe-line-num .editable > p::after,
#pages.ptoe-line-num .editable > div::after,
#pages.ptoe-line-num .editable > h1::after,
#pages.ptoe-line-num .editable > h2::after,
#pages.ptoe-line-num .editable > h3::after,
#pages.ptoe-line-num .editable > h4::after,
#pages.ptoe-line-num .editable > h5::after,
#pages.ptoe-line-num .editable > h6::after{
  content:counter(ptoe-ln);
  position:absolute;left:calc(-1 * (var(--ln-numw,22px) + var(--ln-gap,16px)));top:0;
  font-size:var(--ln-font,12px);line-height:1.7;color:#8b98a6;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  -webkit-user-select:none;user-select:none;pointer-events:none;
  width:var(--ln-numw,22px);text-align:right;
}
/* 表格行行号（2026-10）：插入的表格每行也占一个行号，与段落共用 ptoe-ln 计数器 →
   编号连续。只给「.editable 直属表格」的行计数/编号（与只数直属 p/div/h 同口径）；
   行号挂在首格 td/th 上再用绝对定位摆到左侧行号栏（比挂在 tr 上更稳）。命令行寻址
   _cmdLineBlocks 同样把每个 tr 计为一行，保证显示行号与命令行行号 1:1。 */
#pages.ptoe-line-num .editable > table tr{counter-increment:ptoe-ln;}
#pages.ptoe-line-num .editable > table tr > td:first-child,
#pages.ptoe-line-num .editable > table tr > th:first-child{position:relative;}
#pages.ptoe-line-num .editable > table tr > td:first-child::after,
#pages.ptoe-line-num .editable > table tr > th:first-child::after{
  content:counter(ptoe-ln);
  position:absolute;left:calc(-1 * (var(--ln-numw,22px) + var(--ln-gap,16px)));top:0;
  font-size:var(--ln-font,12px);line-height:1.7;color:#8b98a6;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  -webkit-user-select:none;user-select:none;pointer-events:none;
  width:var(--ln-numw,22px);text-align:right;
}
/* 光标所在行高亮（2026-10）：选区锚点所在块加 .ptoe-cur-line。用 background-image 叠加
   浅蓝底（保留标题块自身的级别底色）。2026-10 调整：去掉原先压在首列文字上的不透明
   左侧竖条（inset 3px accent），改为「行号本身变色加粗 + 更淡的整行底色」——竖条会盖住
   行首字符，且与行号栏贴得太近时更明显；纯视图层，仍不进保存/历史。 */
#pages.ptoe-line-num .editable > .ptoe-cur-line{
  background-image:linear-gradient(rgba(59,111,255,.09),rgba(59,111,255,.09));}
#pages.ptoe-line-num .editable > .ptoe-cur-line::after{
  color:var(--accent,#316dca);font-weight:700;}
/* 表格行处于当前行：行号变色加粗 + 首格淡蓝底（tr 上铺底不稳妥，落在首格） */
#pages.ptoe-line-num .editable > table tr.ptoe-cur-line > td:first-child::after,
#pages.ptoe-line-num .editable > table tr.ptoe-cur-line > th:first-child::after{
  color:var(--accent,#316dca);font-weight:700;}
#pages.ptoe-line-num .editable > table tr.ptoe-cur-line > td:first-child{
  background-image:linear-gradient(rgba(59,111,255,.09),rgba(59,111,255,.09));}
.ptoe-align-left{text-align:left;} .ptoe-align-center{text-align:center;} .ptoe-align-right{text-align:right;}
/* 2026-10 修复：单元格对齐类必须用 .editable th/td 级选择器（特异性 0,2,1），否则被
   下方 .editable th,.editable td{text-align:left}（0,1,1）压过 —— 表格里设「居中/居右」
   看不到效果（用户反馈「对齐设置后不生效」）。与 htmlmanage 导出 CSS 同契约，勿单边改动。 */
.editable th.ptoe-align-left,.editable td.ptoe-align-left{text-align:left;}
.editable th.ptoe-align-center,.editable td.ptoe-align-center{text-align:center;}
.editable th.ptoe-align-right,.editable td.ptoe-align-right{text-align:right;}
/* 2026-09-24：行内对齐 span（部分选区包裹 <span class="ptoe-align-*">…</span>），
   与 EPUB 导出 CSS（htmlmanage 同规则，含 th/td 兼容）保持一致 */
.editable span.ptoe-align-left{display:block;text-align:left;}
.editable span.ptoe-align-center{display:block;text-align:center;}
.editable span.ptoe-align-right{display:block;text-align:right;}
.editable th span.ptoe-align-left,.editable td span.ptoe-align-left{display:inline;text-align:left;}
.editable th span.ptoe-align-center,.editable td span.ptoe-align-center{display:inline;text-align:center;}
.editable th span.ptoe-align-right,.editable td span.ptoe-align-right{display:inline;text-align:right;}
/* 正文两端对齐/分散对齐（2026-10，req4）：#pages.ptoe-justify 时仅给 .editable 声明
   text-align:justify，靠 text-align 继承让所有直属块两端对齐；逐块自带的
   .ptoe-align-* 是元素自身显式声明，会覆盖继承值，故手动对齐设置仍优先生效。
   纯视图层，不影响保存/导出。 */
#pages.ptoe-justify .editable{text-align:justify;}
.ptoe-marker{background:#fff3bf;border:1px solid #e8c24a;border-radius:3px;padding:0 4px;font-size:12px;color:#8a6d00;cursor:help;user-select:all;}
.ptoe-search{background:#fff1a8;border-radius:2px;padding:0 2px;color:inherit;}
.editable mark.ptoe-search{background:#fff1a8;color:inherit;border-radius:2px;padding:0 2px;}
 .editable .ptoe-note{font-size:12px;color:#556677;}
 .editable .ptoe-citation{font-family:var(--font-citation,'宋体',SimSun,serif);font-style:var(--citation-italic, italic);}
.pop-btn:hover{background:#eef3fb;border-color:var(--accent);}
/* 全局延迟提示：悬停超过设定时间才显示（含快捷键），延迟可在「快捷键」设置中调整 */
#tip{position:fixed;z-index:80;display:none;background:#1c2733;color:#fff;font-size:12px;line-height:1.5;padding:5px 9px;border-radius:4px;max-width:320px;pointer-events:none;}
#tip .tip-key{color:#bcd0e5;margin-left:6px;white-space:nowrap;}
#popup{position:fixed;z-index:65;display:none;flex-wrap:wrap;gap:3px;max-width:300px;padding:4px 6px;background:#fff;border:1px solid var(--border);border-radius:7px;box-shadow:0 4px 18px rgba(0,0,0,.22);}
.pop-btn{min-width:28px;height:26px;padding:0 6px;font-size:12px;border:1px solid var(--border);background:#fff;color:#1c2733;border-radius:5px;cursor:pointer;line-height:1;}
/* 右键上下文菜单（2026-08-08）：浅色主题、圆角、分组分隔线、二级菜单向右展开（右缘不足向左） */
#contextMenu{position:fixed;z-index:80;min-width:172px;padding:5px;background:#fff;border:1px solid var(--border);border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.22);display:flex;flex-direction:column;gap:1px;user-select:none;}
#contextMenu[hidden]{display:none;} /* 必须显式覆盖：作者样式 #contextMenu{display:flex} 会压过 UA 的 [hidden]{display:none} */
.ctx-item{display:flex;align-items:center;justify-content:space-between;gap:10px;width:100%;padding:7px 12px;border:none;background:transparent;color:#1c2733;font-size:13px;text-align:left;border-radius:5px;cursor:pointer;white-space:nowrap;font-family:inherit;}
.ctx-item:hover{background:#eef3fb;color:var(--accent);}
.ctx-arrow{font-size:11px;color:#8a97a6;}
.ctx-sub{position:relative;}
.ctx-submenu{position:absolute;top:-5px;left:100%;margin-left:4px;min-width:150px;padding:5px;background:#fff;border:1px solid var(--border);border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.22);display:none;flex-direction:column;gap:1px;z-index:81;}
.ctx-sub.open > .ctx-submenu{display:flex;}
.ctx-submenu.ctx-left{left:auto;right:100%;margin-left:0;margin-right:4px;}
/* 视口下缘不足：二级菜单改为向上对齐（底边与父项底边齐平） */
.ctx-submenu.ctx-up{top:auto;bottom:-5px;}
/* hover 间隙桥：父项与二级菜单之间的 4px margin 用透明伪元素补上，
   鼠标横移过间隙不会触发 mouseleave（配合 JS 的 200/300ms hover-intent 延时） */
.ctx-submenu::before{content:'';position:absolute;top:0;bottom:0;left:-8px;width:8px;}
.ctx-submenu.ctx-left::before{left:auto;right:-8px;}
.ctx-sep{height:1px;background:var(--border);margin:4px 2px;}
.ctx-empty{padding:7px 12px;font-size:12px;color:#9aa7b4;white-space:nowrap;cursor:default;}
/* 格式刷：激活态高亮 + 激活时光标变复制样式 */
.pop-btn.active{background:#ffe9a8;outline:1px solid #d9a400;}
.pop-rule-wrap{position:relative;display:flex;flex-wrap:nowrap;gap:4px;align-items:center;min-width:224px;}
.pop-rule-sub{display:none;position:absolute;left:100%;top:-4px;margin-left:4px;min-width:120px;max-height:260px;overflow-y:auto;background:#fff;border:1px solid var(--border);border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,.15);z-index:61;padding:4px 0;}
.pop-rule-sub .ctx-item{display:block;width:100%;text-align:left;padding:4px 10px;border:none;background:none;cursor:pointer;font-size:13px;white-space:nowrap;}
.pop-rule-sub .ctx-item:hover{background:#f0f0f0;}
.pop-rule-sub .ctx-empty{padding:4px 10px;color:#999;font-size:12px;}
body.paint-mode{cursor:copy;}
/* 文字纠错：错误标注（删除线红色）+ 候选正确字（绿色）+ 确认悬浮窗 */
.ptoe-err{text-decoration:line-through;color:#c00;background:#ffe0e0;padding:0 3px;border-radius:3px;cursor:pointer;}
.ptoe-fix{color:#080;font-size:0.9em;}
/* 7 new inline format classes (2026-09) */
.ptoe-underline{text-decoration:underline;}
/* 下加点：整条虚线式下划线（2026-09）；text-decoration 虚线属 CSS3，
   部分阅读器整条丢弃 → 改用 border-bottom（CJK 无 descender，视觉一致；2026-09-19） */
.ptoe-underdot{border-bottom:1px dotted #333;}
.ptoe-strike{text-decoration:line-through;}
.ptoe-charbox{border:1px solid #333;padding:0 .15em;border-radius:2px;}
.ptoe-shade{background:#eef1f4;}
.ptoe-highlight{background:#e0e0e0;}
.ptoe-sup{vertical-align:super;font-size:.7em;line-height:1;}
.ptoe-sub{vertical-align:sub;font-size:.7em;line-height:1;}
/* 2026-10-21 新增行内格式：波浪下划线/双下划线/上划线/着重号/多色突显/拼音注音 */
.ptoe-wavy{text-decoration:underline wavy;text-decoration-style:wavy;border-bottom:1px solid #333;}
.ptoe-double{text-decoration:underline double;}
.ptoe-overline{text-decoration:overline;}
.ptoe-emphasis{text-emphasis:dot;-webkit-text-emphasis:dot;text-emphasis-position:under right;-webkit-text-emphasis-position:under right;}
.ptoe-highlight-green{background:#c8e6c9;}
.ptoe-highlight-blue{background:#bbdefb;}
.ptoe-highlight-pink{background:#f8bbd0;}
.ptoe-highlight-orange{background:#ffe0b2;}
.ptoe-highlight-purple{background:#e1bee7;}
.ptoe-ruby{ruby-position:over;}
.ptoe-ruby::after{content:"(" attr(data-rt) ")";font-size:.6em;vertical-align:super;}
#errPopup{position:fixed;z-index:65;display:none;background:#fff;border:1px solid #ccc;border-radius:6px;padding:4px;box-shadow:0 2px 8px rgba(0,0,0,.2);gap:6px;}
/* 图片设置弹窗：点击编辑区内的图片弹出，调整大小/位置/删除 */
#imgPopup{position:fixed;z-index:65;display:none;flex-direction:column;gap:6px;padding:10px;background:#fff;border:1px solid #ccc;border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.2);min-width:140px;}
.img-pop-btn{background:#f5f5f5;border:1px solid #ddd;border-radius:4px;padding:3px 10px;font-size:13px;cursor:pointer;}
.img-pop-btn:hover{background:#eef3fb;border-color:var(--accent);}
.img-pop-btn:disabled{opacity:.5;cursor:not-allowed;background:#f5f5f5;border-color:#ddd;}
/* 「大小」行的自定义宽度输入（2026-10）：观感对齐字符格式弹窗的 #cfSizeNum */
#imgCustomW{width:62px;padding:3px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;font-size:13px;}
#imgCustomW:disabled{opacity:.5;background:#f7f7f7;}
#errOk{background:#2e8b57;color:#fff;border:none;border-radius:4px;padding:2px 10px;cursor:pointer;font-size:14px;}
#errNo{background:#c0392b;color:#fff;border:none;border-radius:4px;padding:2px 10px;cursor:pointer;font-size:14px;}
/* 文字纠错下拉菜单 / 文字包围 / 上标下标 / 分隔线 */
/* 分隔线菜单 #dividerMenu 必须与前三个同构：openDropMenu 写 left/top 并 display:block，
   缺 position:fixed 时坐标被忽略、static 块又被 sticky 的 #toolbar 盖住 → 点击像没反应。 */
/* 2026-10 修复：原选择器里含 #charWrapMenu / #supSubMenu / #proofreadMenu——这些 id 现在都属于
   Ribbon 工具栏内的 .tb-more-panel 下拉（见 #toolbar），ID 选择器特异性更高，会把工具栏下拉
   改成 position:fixed（脱离 .tb-more 定位、悬浮到错误位置）、display:none 盖过 .open 展开规则。
   故此处只保留仍为独立浮层的分隔线菜单。 */
#dividerMenu{position:fixed;z-index:70;background:#fff;border:1px solid #ddd;border-radius:6px;box-shadow:0 2px 10px rgba(0,0,0,.15);min-width:120px;padding:4px;display:none;}
#dividerMenu button{display:block;width:100%;text-align:left;padding:6px 10px;border:none;background:none;cursor:pointer;border-radius:4px;font-size:13px;}
#dividerMenu button:hover{background:#f0f0f0;}
/* 下拉指示符：小号低对比三角，提示「校」为下拉菜单；菜单展开时按钮高亮 */
#proofreadBtn .ptoe-caret{font-size:9px;color:#8a97a6;margin-left:3px;vertical-align:1px;}
#popup .sep{width:100%;height:0;border-top:1px solid var(--border);margin:2px 0;}
.ic-b{font-weight:700;} .ic-i{font-style:italic;font-family:Georgia,'Times New Roman',serif;} .ic-h{font-weight:700;} .ic-p{font-weight:600;} .ic-t{font-weight:600;} .ic-n{font-size:12px;color:#556677;}
/* 左侧预览图上的「图」按钮：把当前显示的图片插入右侧文字光标处 */
.img-insert{position:absolute;right:8px;bottom:8px;z-index:5;padding:3px 10px;font-size:13px;border:1px solid var(--border);background:#fff;color:#1c2733;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.18);cursor:pointer;}
.img-insert:hover{background:#eef3fb;border-color:var(--accent);}
.img-crop{position:absolute;right:8px;bottom:34px;z-index:5;padding:3px 10px;font-size:13px;border:1px solid var(--border);background:#fff;color:#1c2733;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.18);cursor:pointer;}
.img-crop:hover{background:#eef3fb;border-color:var(--accent);}
.crop-layer{position:absolute;inset:0;z-index:30;background:rgba(0,0,0,.18);border-radius:4px;touch-action:none;}
.crop-box{position:absolute;box-shadow:0 0 0 9999px rgba(0,0,0,.55);border:1px dashed #fff;cursor:move;touch-action:none;}
.crop-box .crop-handle{position:absolute;width:11px;height:11px;background:#fff;border:1px solid #2a5db0;border-radius:2px;}
.crop-box .crop-handle.tl{top:-6px;left:-6px;cursor:nwse-resize;}
.crop-box .crop-handle.tr{top:-6px;right:-6px;cursor:nesw-resize;}
.crop-box .crop-handle.bl{bottom:-6px;left:-6px;cursor:nesw-resize;}
.crop-box .crop-handle.br{bottom:-6px;right:-6px;cursor:nwse-resize;}
.crop-actions{position:absolute;left:50%;bottom:8px;transform:translateX(-50%);display:flex;gap:6px;z-index:31;}
.crop-actions button{padding:3px 10px;font-size:12px;}
/* 插入图片：全画幅（占满文字宽度）/ 局部（按原尺寸居中）
   重构（2026-08-10）：避免 specificity war，img 不设宽度，
   改用尺寸 class（ptoe-img-w*）作为唯一宽度控制；
   p 用 text-align 控制位置（inline-block img 响应对齐） */
.editable p.ptoe-img-full{text-indent:0;}
.editable p.ptoe-img-fit{text-indent:0;}
.editable p.ptoe-img-full img{display:inline-block;max-width:100%;height:auto;vertical-align:middle;}
.editable p.ptoe-img-fit img{display:inline-block;max-width:100%;height:auto;vertical-align:middle;}
/* 尺寸 class：唯一宽度控制（全画幅默认 w100，局部默认无尺寸=原图） */
.editable .ptoe-img-w25{width:25%}
.editable .ptoe-img-w50{width:50%}
.editable .ptoe-img-w75{width:75%}
.editable .ptoe-img-w100{width:100%}
/* 位置 class：p 上 text-align 控制 img 对齐 */
.editable p.ptoe-img-left{text-align:left}
.editable p.ptoe-img-center{text-align:center}
.editable p.ptoe-img-right{text-align:right}
/* 行内图片（2026-08-10）：直接嵌在文字流中（无 <p> 包裹），
   vertical-align 控制上下对齐；尺寸 class 同样生效 */
.editable img.ptoe-img-inline{display:inline-block;max-width:100%;height:auto;vertical-align:middle;}
.editable img.ptoe-img-vtop{vertical-align:top;}
.editable img.ptoe-img-vmid{vertical-align:middle;}
.editable img.ptoe-img-vbot{vertical-align:bottom;}
/* 浮动绕排（2026-10 图文混合）：ptoe-img-float-left / ptoe-img-float-right，
   类挂在 img 自身上（float 必须作用在图片元素上，挂在包裹 p 标签上文字不绕排），
   宽度仍复用既有的 ptoe-img-w* 档位；与 ptoe-img-inline 互斥（前端负责去重）。
   display:inline-block 是**刻意的降级声明**：目标阅读器多看实测支持 CSS float 绕排，
   而忽略 float 的阅读器（微信阅读无 WebView、静读天下覆盖出版方 CSS）会退化成
   「按 ptoe-img-w* 比例行内排版、文字接在图片后面」，即现有行内图行为，
   绝不塌成独占块——所以绝不写成裸 float。
   margin 取值：内侧 0.5em（图片与绕排文字的间距，em 随正文字号缩放，不贴图）
              + 下侧 0.4em（图片底部与后续整宽文字的间距，勿让文字紧贴图片下沿）。
   声明值与 htmlmanage 的 EPUB style.css、ui/epubedit.html 三处逐字相同（勿单边改动）。 */
.editable img.ptoe-img-float-left{float:left;display:inline-block;max-width:100%;height:auto;margin:0 0.5em 0.4em 0;}
.editable img.ptoe-img-float-right{float:right;display:inline-block;max-width:100%;height:auto;margin:0 0 0.4em 0.5em;}
/* 题注（2026-10 图文混合）：图片块之后的独立说明段落 <p class="ptoe-caption">。
   样式值与 htmlmanage 导出 EPUB 的题注规则保持同一份契约（勿单边改动）。 */
.editable p.ptoe-caption{font-size:0.85em;color:#666;text-align:center;margin:0.2em 0 0.6em;}
/* 插入表格（2026-09）：编辑区内表格渲染；边框/表头底色走 CSS 变量，暗色主题自适应。
   2026-09 反馈修复：默认撑满行宽（width:100%）、单元格最小宽度 + 顶对齐 */
.editable table{border-collapse:collapse;margin:.5em 0;width:100%;max-width:100%;}
.editable th,.editable td{border:1px solid var(--border);padding:.3em .6em;text-align:left;vertical-align:top;min-width:3em;}
.editable th{font-weight:bold;text-align:center;background:#f2f5fa;}
.dark .editable th{background:var(--bg-hover);}
/* 无边框表格（2026-10）：表格菜单「不显示边框」给 <table> 加 class="ptoe-noborder"。
   分栏/批注版式（左窄栏批注 + 右宽栏正文）用它隐藏全部框线；保存白名单只放行
   该 class，EPUB（htmlmanage）/DOCX 导出按同一 class 输出无边框（勿单边改动）。 */
.editable table.ptoe-noborder th,.editable table.ptoe-noborder td{border:none;}
.editable table.ptoe-noborder th{background:transparent;}
/* 编辑区内的 LaTeX 公式（2026-10-21）：<math> 元素由浏览器原生渲染（MathML），
   这里只做版式微调——跟随正文字号、与基线对齐；不写 display（部分阅读器对
   math 的 display 覆盖敏感，写死 inline 会让 display=block 的公式塌成一行） */
.editable math{font-size:1em;vertical-align:middle;}
/* 表格操作条（2026-09）：光标进入表格单元格时由 JS 定位并显示；
   加行/删行/加列/删列/对齐/删除表格；对齐按钮按当前单元格 class 高亮 */
#tblBar{position:fixed;z-index:64;display:none;flex-wrap:wrap;gap:2px;align-items:center;padding:4px 6px;background:rgba(255,255,255,.94);border:1px solid var(--border);border-radius:8px;box-shadow:0 4px 14px rgba(0,0,0,.18);}
.tbl-bar-sep{width:1px;height:16px;background:#d5dbe3;margin:0 3px;}
#tblBar button{background:transparent;border:1px solid transparent;border-radius:5px;padding:2px 7px;font-size:12px;color:#1c2733;cursor:pointer;white-space:nowrap;}
#tblBar button:hover{background:#eef3fb;border-color:var(--accent);color:var(--accent);}
#tblBar button.active{background:#e0eaff;border-color:var(--accent);color:var(--accent);font-weight:600;}
#tblBar button.danger{color:#c0392b;}
#tblBar button.danger:hover{background:#fdecea;border-color:#e74c3c;}
.dark #tblBar{background:rgba(30,35,45,.94);border-color:#3a4150;}
.dark #tblBar button{color:#ddd;}
.dark .tbl-bar-sep{background:#3a4150;}
/* ---- 表格增强（2026-10）----
   用户反馈三条：插入的表格太小 / 鼠标光标与表格错位 / 无法调整宽高。
   ① 最小行高：空表不再塌成一条线；② 垂直对齐类；③ 含表格的行参与撑行
   （默认 .editable{height:0} 不参与 → 表格被图片高度裁切并内部滚动，同时造成
   点击落点与所见错位）；④ 合并选区高亮；⑤ 列/行拖拽手柄层；⑥ 更多菜单；⑦ 尺寸弹窗 */
.editable th,.editable td{height:1.9em;}
.editable td.ptoe-vtop,.editable th.ptoe-vtop{vertical-align:top;}
.editable td.ptoe-vmid,.editable th.ptoe-vmid{vertical-align:middle;}
.editable td.ptoe-vbot,.editable th.ptoe-vbot{vertical-align:bottom;}
/* 2026-10 修订：含表格的行不再被撑高——用户要求「页高与图片等高、内容在页内滚动」。
   旧规则把行高交给表格内容（height:auto / min-height:0），会让行比左侧图片高一截且
   不出现滚动条，已移除：.editable 恢复 height:0 / min-height:100%（= 行高 = 图片高），
   超出部分由 overflow-y:auto 内部滚动。 */
.editable td.ptoe-cell-sel,.editable th.ptoe-cell-sel{outline:2px solid var(--accent,#316dca);outline-offset:-2px;background:rgba(49,109,202,.12);}
#tblResizeLayer{position:fixed;z-index:63;display:none;pointer-events:none;}
#tblResizeLayer .tbl-col-h{position:absolute;width:9px;cursor:col-resize;pointer-events:auto;}
#tblResizeLayer .tbl-col-h::after{content:'';position:absolute;left:3px;top:0;bottom:0;width:2px;background:var(--accent,#316dca);opacity:0;}
#tblResizeLayer .tbl-col-h:hover::after{opacity:.95;}
#tblResizeLayer .tbl-row-h{position:absolute;height:9px;cursor:row-resize;pointer-events:auto;}
#tblResizeLayer .tbl-row-h::after{content:'';position:absolute;top:3px;left:0;right:0;height:2px;background:var(--accent,#316dca);opacity:0;}
#tblResizeLayer .tbl-row-h:hover::after{opacity:.95;}
#tblMenu{position:fixed;z-index:71;background:#fff;border:1px solid #ddd;border-radius:6px;box-shadow:0 2px 10px rgba(0,0,0,.15);min-width:158px;padding:4px;display:none;max-height:72vh;overflow:auto;}
#tblMenu button{display:block;width:100%;text-align:left;padding:6px 10px;border:none;background:none;cursor:pointer;border-radius:4px;font-size:13px;color:#1c2733;}
#tblMenu button:hover{background:#f0f0f0;}
#tblMenu button.active{background:#e0eaff;color:var(--accent);font-weight:600;}
#tblMenu button.danger{color:#c0392b;}
#tblMenu .tbl-menu-sep{height:1px;background:#e6e6e6;margin:4px 6px;}
#tblMenu .tbl-menu-head{padding:4px 10px 2px;font-size:11px;color:var(--text-muted);}
.dark #tblMenu{background:#232a35;border-color:#3a4150;}
.dark #tblMenu button{color:#ddd;}
.dark #tblMenu button:hover{background:#2e3746;}
.dark #tblMenu .tbl-menu-sep{background:#3a4150;}
/* 单元格对齐九宫格（2026-10）：Word 式「上下 × 左右」。3×3 按钮，每个按钮用三条线
   的摆放位置直观示意对齐方向；data-tbl-ag-h=left|center|right，data-tbl-ag-v=top|middle|bottom。 */
#tblAlignGrid{position:fixed;z-index:72;background:#fff;border:1px solid #ddd;border-radius:6px;box-shadow:0 2px 10px rgba(0,0,0,.15);padding:6px;display:none;}
#tblAlignGrid .tbl-ag-title{padding:0 2px 5px;font-size:11px;color:var(--text-muted);}
#tblAlignGrid .tbl-ag-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:3px;}
#tblAlignGrid .tbl-ag-cell{display:flex;width:34px;height:34px;padding:3px;background:#fafbfc;border:1px solid #e4e8ee;border-radius:5px;cursor:pointer;color:#5a6672;}
#tblAlignGrid .tbl-ag-cell:hover{background:#eef3fb;border-color:var(--accent);color:var(--accent);}
#tblAlignGrid .tbl-ag-cell.active{background:#e0eaff;border-color:var(--accent);color:var(--accent);}
#tblAlignGrid .tbl-ag-lines{display:flex;flex-direction:column;justify-content:space-between;width:22px;height:16px;flex:0 0 auto;}
#tblAlignGrid .tbl-ag-lines i{display:block;height:2px;border-radius:1px;background:currentColor;opacity:.85;}
#tblAlignGrid .tbl-ag-lines i:nth-child(1){width:100%;}
#tblAlignGrid .tbl-ag-lines i:nth-child(2){width:66%;}
#tblAlignGrid .tbl-ag-lines i:nth-child(3){width:100%;}
/* 水平：用按钮的 justify-content 摆放线条块；垂直：用 align-items 摆放（顶/中/底可见位移） */
#tblAlignGrid .tbl-ag-cell[data-tbl-ag-h="left"]{justify-content:flex-start;}
#tblAlignGrid .tbl-ag-cell[data-tbl-ag-h="center"]{justify-content:center;}
#tblAlignGrid .tbl-ag-cell[data-tbl-ag-h="right"]{justify-content:flex-end;}
#tblAlignGrid .tbl-ag-cell[data-tbl-ag-v="top"]{align-items:flex-start;}
#tblAlignGrid .tbl-ag-cell[data-tbl-ag-v="middle"]{align-items:center;}
#tblAlignGrid .tbl-ag-cell[data-tbl-ag-v="bottom"]{align-items:flex-end;}
.dark #tblAlignGrid{background:#232a35;border-color:#3a4150;}
.dark #tblAlignGrid .tbl-ag-cell{background:#1b212b;border-color:#3a4150;color:#b7c0cc;}
.dark #tblAlignGrid .tbl-ag-cell:hover{background:#2e3746;color:#8fb4ff;}
.dark #tblAlignGrid .tbl-ag-cell.active{background:#2a3a5c;color:#8fb4ff;}
#tblSizeModalBg{position:fixed;inset:0;z-index:67;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
.tbl-size-grid{display:grid;grid-template-columns:auto 1fr;gap:8px 12px;margin:10px 0;font-size:13px;align-items:center;}
.tbl-size-grid input[type="number"]{width:92px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;}
.tbl-size-grid select{padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;background:#fff;}
/* 搜索 / 替换弹窗（工具栏「搜」按钮打开；结果列表点击跳转、↑↓上一个/下一个） */
#searchModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
.search-modal{max-width:640px;width:94%;display:flex;flex-direction:column;}
/* 导出弹窗（工具栏「导出」按钮打开；复用 .search-modal 布局） */
#exportModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#indentModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
.indent-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;margin:10px 0;font-size:13px;}
.indent-grid label{display:flex;align-items:center;gap:6px;white-space:nowrap;}
.indent-grid input[type="number"]{width:72px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;}
.indent-grid select{padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;background:#fff;}
.indent-preview{border:1px dashed var(--border);border-radius:6px;padding:10px 12px;margin:8px 0 12px;min-height:56px;overflow:auto;background:#fafbfc;}
.indent-preview p{margin:0;}
/* 段落设置应用范围（2026-09-24：选中段落 / 当前页全部正文段落 / 全部正文） */
.indent-mode-row{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;margin:6px 0 2px;padding:6px 10px;background:#f2f5f9;border:1px solid var(--border);border-radius:6px;}
.indent-mode-row label{display:flex;align-items:center;gap:5px;cursor:pointer;color:#33475b;}
.indent-mode-row input[type="radio"]{margin:0;accent-color:var(--accent,#316dca);cursor:pointer;}
/* 插入表格弹窗：行列数输入与「首行表头」勾选（外层复用 .search-modal/.export-actions） */
#tableModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
.table-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;margin:10px 0;font-size:13px;}
/* 字符格式弹窗（2026-09-27）：外层复用 .search-modal/.export-actions/.indent-mode-row */
#charFormatModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
/* 拼音注音弹窗（2026-10-21）：内嵌浏览器不保证支持 window.prompt，改用应用内弹窗 */
#rubyModalBg{position:fixed;inset:0;z-index:67;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#rubyModalBg .modal{width:92%;max-width:420px;}
#rubyModalBg input[type=text]{width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;font:inherit;background:var(--bg);color:var(--fg);}
/* LaTeX 公式弹窗（2026-10-21）：源码输入 + MathML 实时预览 */
#latexModalBg{position:fixed;inset:0;z-index:67;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#latexModalBg .modal{width:94%;max-width:560px;}
#latexModalBg textarea{width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;font:inherit;background:var(--bg);color:var(--fg);resize:vertical;font-family:Consolas,Menlo,monospace;}
#latexModalBg code{background:rgba(127,127,127,.14);padding:0 3px;border-radius:3px;font-family:Consolas,Menlo,monospace;}
.latex-preview-row{display:flex;align-items:flex-start;gap:8px;margin:10px 0 6px;}
.latex-preview-label{flex:0 0 auto;font-size:12px;color:var(--text-muted);padding-top:6px;}
.latex-preview{flex:1 1 auto;min-height:44px;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);overflow:auto;}
.latex-preview-empty{color:var(--text-muted);font-size:12px;}
.latex-preview-error{color:#c0392b;font-size:12px;}
.latex-display-opt{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--text-muted);margin-bottom:4px;}
#cfSizeNum{width:86px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;}
#cfFontSel{max-width:190px;}
#cfPreview p{margin:0;}
/* 颜色面板（2026-10-21）：常用色 + 色板 + #HEX/rgb() 输入，字符格式弹窗与工具栏共用 */
.cf-color-panel{margin-top:10px;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);}
.cf-color-group{margin-bottom:8px;}
.cf-color-group:last-child{margin-bottom:0;}
.cf-color-label{display:block;font-size:12px;color:var(--text-muted);margin-bottom:5px;}
.cf-swatches{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:4px;}
.cf-palette{display:grid;grid-template-columns:repeat(12,20px);gap:3px;justify-content:start;}
.cf-swatch{width:20px;height:20px;padding:0;border:1px solid rgba(0,0,0,.25);border-radius:3px;cursor:pointer;background-clip:padding-box;}
.cf-swatch:hover{outline:2px solid var(--accent,#3b82f6);outline-offset:1px;}
.cf-color-row{display:flex;gap:6px;align-items:center;flex-wrap:wrap;}
.cf-color-input{flex:1 1 130px;min-width:110px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;background:var(--bg);color:var(--fg);}
/* 浮动颜色面板（工具栏「颜色」按钮） */
.color-pop{position:fixed;z-index:80;width:300px;padding:8px;border:1px solid var(--border);border-radius:7px;background:var(--bg,#fff);box-shadow:0 4px 18px rgba(0,0,0,.22);}
/* 分隔线下拉菜单里的字形预览（与正文里最终写入的 8 字形串一致） */
#dividerMenu .div-glyph{margin-left:8px;font-family:monospace;color:var(--text-muted);letter-spacing:0;}
#dividerMenu .sep{width:100%;height:0;border-top:1px solid var(--border);margin:4px 0;}
#dividerMenu .div-dashed{letter-spacing:.12em;}
#dividerMenu .div-dotted{letter-spacing:.12em;}
#dividerMenu .div-double{font-weight:bold;letter-spacing:.05em;}
/* 分隔线（2026-09-27）：与 htmlmanage 导出 EPUB 的规则保持一致（同一份视觉预期）。
   标记形态 = 非空段落 + 两个类：基类 + 样式后缀（correctmanage.DIVIDER_STYLES）。
   四种样式必须一眼可辨；居中、不继承段首缩进、不受逐字符字号/颜色影响。 */
.editable p.ptoe-divider{text-align:center;text-indent:0;margin:.6em 0;color:#999999;white-space:nowrap;overflow:hidden;font-size:1em;letter-spacing:0;}
.editable p.ptoe-divider-solid{letter-spacing:0;font-weight:normal;}
.editable p.ptoe-divider-dashed{letter-spacing:.12em;}
.editable p.ptoe-divider-dotted{letter-spacing:.12em;}
.editable p.ptoe-divider-double{letter-spacing:.05em;font-weight:bold;}
/* 花样分割符号（2026-10-21）：非线条型，装饰字形排布，字距放宽 */
.editable p.ptoe-divider-stars{letter-spacing:.35em;}
.editable p.ptoe-divider-diamonds{letter-spacing:.35em;}
.editable p.ptoe-divider-circles{letter-spacing:.35em;}
.editable p.ptoe-divider-flowers{letter-spacing:.35em;}
.editable p.ptoe-divider-snow{letter-spacing:.35em;}
.editable p.ptoe-divider-wave{letter-spacing:.25em;}
.editable p.ptoe-divider-note{letter-spacing:.35em;}
.editable p.ptoe-divider-leaf{letter-spacing:.35em;}
.editable p.ptoe-divider span{color:inherit;font-size:inherit;font-family:inherit;}
.table-grid label{display:flex;align-items:center;gap:6px;white-space:nowrap;}
.table-grid input[type="number"]{width:72px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;}
.export-desc{font-size:13px;color:#5a6b7c;margin:0 0 14px;line-height:1.6;}
.export-actions{display:flex;gap:10px;justify-content:flex-end;}
.search-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;}
.search-head h3{margin:0;}
.search-head .x-btn{width:26px;height:26px;padding:0;line-height:1;border:none;background:transparent;color:#8a97a6;font-size:16px;border-radius:4px;cursor:pointer;}
.search-head .x-btn:hover{background:#dfe7f3;color:#1c2733;}
.search-row{display:flex;align-items:center;gap:8px;margin-bottom:8px;}
.search-row input[type="text"]{flex:1;min-width:0;padding:6px 8px;border:1px solid var(--border);border-radius:5px;font:inherit;}
/* 按格式查找/替换（2026-10-21）：格式下拉 + 取值输入 */
.search-row select{padding:6px 8px;border:1px solid var(--border);border-radius:5px;font:inherit;background:#fff;color:var(--fg);max-width:190px;}
.dark .search-row select{background:var(--bg-elev);color:var(--fg);}
.fmt-value-wrap{display:inline-flex;align-items:center;gap:4px;}
.fmt-value-input{width:120px;padding:5px 7px;border:1px solid var(--border);border-radius:5px;font:inherit;background:var(--bg);color:var(--fg);}
.fmt-value-input[type=text]{flex:none;}
.search-regex{display:inline-flex;align-items:center;gap:3px;font-size:13px;color:#33414f;white-space:nowrap;}
.search-nav{display:flex;align-items:center;gap:8px;margin:2px 0 8px;}
.search-nav button{width:32px;height:28px;padding:0;border:1px solid var(--border);background:#fff;border-radius:5px;cursor:pointer;font-size:14px;}
.search-nav button:hover{background:#eef3fb;border-color:var(--accent);}
#searchPos{font-size:12px;color:#5a6b7c;min-width:70px;text-align:center;}
/* 结果列表是虚拟滚动（ui/app.js 的 _srRenderWindow）：窗口滑动时会改写「上占位块」高度。
   浏览器滚动锚定（scroll anchoring）会把这段高度增量补进 scrollTop，再驱动下一帧的窗口
   滑动 → 正反馈：滚过第十行左右就一路加速冲到列表末尾，从末尾向上滚同样会冲到开头。
   overflow-anchor:none 关掉锚定后，scrollTop 只跟随用户滚动。 */
#searchList{max-height:45vh;overflow:auto;border:1px solid var(--border);border-radius:6px;padding:6px;background:#fafbfc;font-size:12px;overflow-anchor:none;}
.sr-head{display:flex;align-items:center;gap:8px;padding:2px 0 6px;color:#5a6b7c;font-weight:600;}
.sr-item{height:44px;box-sizing:border-box;overflow:hidden;padding:4px 8px;border:1px solid var(--border);border-radius:6px;margin:0 0 6px;cursor:pointer;background:#fff;}
.sr-item:hover{border-color:var(--accent);background:#f2f7ff;}
.sr-item.current{border-color:var(--accent);background:#eaf2ff;box-shadow:0 0 0 1px var(--accent);}
.sr-page{font-size:11px;color:#5a6b7c;margin-bottom:1px;line-height:1.3;}
.sr-ctx{color:#33414f;line-height:1.5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.sr-ctx mark{background:#ffe08a;color:#5c4000;border-radius:2px;padding:0 2px;}
.sr-empty{color:#8a97a6;padding:6px 2px;}
/* 2026-09：搜索范围分段按钮 + 保留组 chips + 替换规则列表 */
.scope-seg{display:inline-flex;background:rgba(0,0,0,.06);border-radius:8px;padding:2px;gap:2px;}
.scope-btn{border:0;background:transparent;border-radius:6px;padding:4px 10px;font-size:12px;color:#5a6b7c;cursor:pointer;}
.scope-btn:hover{color:#1c2733;}
.scope-btn.active{background:#fff;color:#1c2733;box-shadow:0 1px 3px rgba(0,0,0,.15);font-weight:600;}
.search-row-group{display:flex;align-items:center;gap:6px;margin-bottom:6px;}
.srow-label{flex:none;min-width:2.6em;font-size:12px;color:#8a97a6;text-align:right;}
.kg-row{display:none;align-items:center;gap:6px;margin:2px 0 6px;padding:4px 8px;background:rgba(59,111,255,.06);border:1px solid rgba(59,111,255,.25);border-radius:6px;}
.kg-hint{flex:none;font-size:12px;color:#3b6fff;font-weight:600;}
.kg-chips{display:flex;flex-wrap:wrap;gap:4px;}
.kg-chip{border:1px solid var(--border);background:#fff;border-radius:999px;padding:2px 9px;font-size:12px;color:#5a6b7c;cursor:pointer;}
.kg-chip:hover{border-color:#3b6fff;color:#3b6fff;}
.kg-chip.on{background:#3b6fff;border-color:#3b6fff;color:#fff;font-weight:600;}
.sr-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:2px 0 6px;}
.sr-rules-btn{border:1px solid rgba(59,111,255,.4);background:rgba(59,111,255,.08);color:#3b6fff;border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer;}
.sr-rules-btn:hover{background:rgba(59,111,255,.16);}
#rrList{max-height:320px;overflow:auto;}
.rr-item{border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin-bottom:8px;background:#fff;}
.rr-item-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13px;}
.rr-name{font-weight:600;color:#1c2733;}
.rr-builtin{flex:none;font-size:11px;color:#fff;background:#8a97a6;border-radius:4px;padding:1px 6px;}
.rr-pat{font-size:12px;background:rgba(0,0,0,.05);border-radius:4px;padding:1px 6px;color:#7c3aed;}
.rr-repl{font-size:12px;color:#5a6b7c;}
.rr-item-acts{display:flex;gap:6px;margin-top:8px;}
.rr-item-acts button{border:1px solid var(--border);background:#fff;border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer;}
.rr-item-acts button:hover{border-color:#3b6fff;color:#3b6fff;}
.rr-item-acts button.danger:hover{border-color:#c62828;color:#c62828;}
.rr-edit-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px 14px;margin-top:4px;}
.rr-edit-grid label{display:flex;flex-direction:column;gap:4px;font-size:13px;color:#33414f;}
.rr-edit-grid input{padding:5px 8px;border:1px solid var(--border);border-radius:6px;font:inherit;width:100%;box-sizing:border-box;}
.search-head{display:flex;align-items:center;gap:10px;}
.search-head h3{margin:0;flex:none;}
.search-head .scope-seg{margin-left:auto;}
/* 搜索高亮在亮色下的蓝色调（替换默认黄，排在其后以覆盖） */
.sr-ctx mark{background:#cfe0ff;color:#2047d8;}
/* 暗色模式适配（2026-09） */
.dark .scope-seg{background:rgba(255,255,255,.08);}
.dark .scope-btn{color:#9aa7b8;}
.dark .scope-btn:hover{color:#e8edf4;}
.dark .scope-btn.active{background:#2a3442;color:#e8edf4;box-shadow:0 1px 3px rgba(0,0,0,.4);}
.dark .kg-row{background:rgba(59,111,255,.12);border-color:rgba(59,111,255,.4);}
.dark .kg-chip{background:#2a3442;border-color:#46525f;color:#b8c2cf;}
.dark .kg-chip:hover{border-color:#3b6fff;color:#8fb0ff;}
.dark .kg-chip.on{background:#3b6fff;border-color:#3b6fff;color:#fff;}
.dark .sr-rules-btn{background:rgba(59,111,255,.2);color:#8fb0ff;border-color:rgba(59,111,255,.5);}
.dark .rr-item{background:#24303e;border-color:#3a4653;}
.dark .rr-name{color:#e8edf4;}
.dark .rr-pat{background:rgba(255,255,255,.08);color:#c9a8ff;}
.dark .rr-repl{color:#9aa7b8;}
.dark .rr-item-acts button{background:#2a3442;border-color:#46525f;color:#c7d0da;}
.dark .rr-edit-grid input{background:#2a3442;border-color:#46525f;color:#e8edf4;}
.dark .rr-edit-grid label{color:#b8c2cf;}
.dark .sr-ctx mark{background:rgba(59,111,255,.35);color:#d7e5ff;}
.dark .sr-item{background:#24303e;border-color:#3a4653;}
#modalBg{position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#finishModalBg{position:fixed;inset:0;z-index:70;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#historyModalBg{position:fixed;inset:0;z-index:70;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#helpModalBg{position:fixed;inset:0;z-index:65;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
/* 清理预览模态框（2026-10-16）：遮罩层定位与其它 modal 同构。
   缺失此规则会让预览框落在文档流末尾不可见，表现为点「清理」毫无反应。 */
#cleanPreviewBg{position:fixed;inset:0;z-index:68;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
.clean-preview-item{padding:3px 6px;border-bottom:1px dashed var(--border);}
.clean-preview-item:last-child{border-bottom:none;}
#historyTable{table-layout:fixed;}
#historyTable th{position:sticky;top:0;background:#f4f6f9;}
#historyTable td{word-break:break-all;vertical-align:top;}
/* 历史记录行：文件名支持 inline rename、路径三行 clamp、✎ 图标 */
.hist-name-display {display:inline-block;max-width:calc(100% - 20px);word-break:break-all;line-height:1.4;}
.hist-rename-icon {display:inline-block;cursor:pointer;margin-left:4px;color:#666;font-size:12px;vertical-align:middle;}
.hist-rename-input {font-size:12px;padding:2px 3px;margin:2px 0;background:#fafafa;border:1px solid #ddd;border-radius:3px;width:auto;}
/* 路径列：最多 3 行，悬停显示完整路径 */
#historyTable td.hist-path {display: -webkit-box;-webkit-line-clamp: 3;-webkit-box-orient: vertical;overflow: hidden;cursor:help;}
.modal{background:#fff;border-radius:10px;padding:18px 22px;max-width:520px;width:92%;max-height:80vh;overflow:auto;}
.modal h3{margin:0 0 10px;}
.modal h4{margin:14px 0 6px;font-size:14px;color:#1c2733;}
/* 设置弹窗标签栏（JS-only 切换，样式补全局按钮默认外观） */
.settings-tabs{display:flex;gap:2px;border-bottom:1px solid var(--border);margin-bottom:10px;}
.settings-tab{flex:1;padding:7px 8px;border:none;background:transparent;color:var(--text-muted);font-size:13px;border-bottom:2px solid transparent;border-radius:6px 6px 0 0;cursor:pointer;white-space:nowrap;}
.settings-tab:hover{background:#f4f7fb;color:var(--accent);}
.settings-tab.active{color:var(--accent);border-bottom-color:var(--accent);background:#eef3fb;}
/* 设置-弹出菜单：两行按钮 chips 编辑器 */
.pop-edit-list{display:flex;flex-wrap:wrap;gap:6px;padding:8px;border:1px solid var(--border);border-radius:6px;background:#fafbfd;min-height:38px;margin-top:6px;}
.pop-edit-chip{display:inline-flex;align-items:center;gap:5px;padding:2px 3px 2px 8px;background:#fff;border:1px solid var(--border);border-radius:14px;font-size:13px;color:#1c2733;box-shadow:0 1px 2px rgba(0,0,0,.04);}
.pop-edit-chip .pe-ico{color:var(--accent);font-weight:600;min-width:1em;text-align:center;}
.pop-edit-chip .pe-label{line-height:1.6;}
.pop-edit-chip .pe-acts{display:inline-flex;border-left:1px solid var(--border);margin-left:1px;padding-left:1px;}
.pop-edit-chip .pe-act{width:20px;height:20px;padding:0;border:none;background:transparent;color:#8a97a6;font-size:11px;line-height:1;border-radius:3px;cursor:pointer;}
.pop-edit-chip .pe-act:hover{background:#eef3fb;color:var(--accent);}
.pop-edit-chip .pe-act[data-pe-act="del"]:hover{color:#c0392b;background:#fdeeee;}
.pop-edit-add{display:flex;gap:8px;align-items:center;margin-top:8px;}
.pop-edit-add select{flex:0 0 auto;min-width:180px;max-width:240px;}
.help-table{width:100%;border-collapse:collapse;font-size:13px;}
.help-table td{padding:4px 8px;border-bottom:1px solid var(--border);vertical-align:top;}
.help-table td:first-child{white-space:nowrap;color:#33414f;font-weight:600;}
#shortcutTable{width:100%;border-collapse:collapse;}
#shortcutTable td{padding:6px 8px;border-bottom:1px solid var(--border);font-size:14px;}
#shortcutTable tr{cursor:pointer;}
#shortcutTable tr:hover td{background:#f7fafd;}
/* 鼠标侧键表（2026-10-21）：侧键单独绑定，无需组合键盘按键 */
#sideButtonTable{width:100%;border-collapse:collapse;}
#sideButtonTable td{padding:6px 8px;border-bottom:1px solid var(--border);font-size:14px;}
/* 分组标题行（功能刷）：不可点（不进入捕获），无悬停高亮 */
#shortcutTable tr.shortcut-group-row{cursor:default;}
#shortcutTable tr.shortcut-group-row:hover td{background:none;}
#shortcutTable tr.shortcut-group-row td{padding-top:14px;font-weight:600;color:#5a6b7c;font-size:12px;}
kbd{background:#eef1f5;border:1px solid #c9d1da;border-radius:3px;padding:1px 6px;font-size:12px;font-family:inherit;}
#closeSettings{margin-top:12px;}
#formatRulesModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#formatRulesTable{width:100%;border-collapse:collapse;font-size:13px;}
#formatRulesTable td{padding:6px 8px;border-bottom:1px solid var(--border);vertical-align:top;}
#formatRulesTable tr:hover td{background:#f7fafd;}
#formatRulesTable .fr-name{font-weight:600;white-space:nowrap;}
#formatRulesTable .fr-sum{color:#5a6b7c;font-size:12px;}
#formatRulesTable .fr-order{color:#5a6b7c;font-size:12px;white-space:nowrap;}
#formatRulesTable button{padding:2px 8px;font-size:12px;}
#formatRulesTable button:disabled{opacity:.45;cursor:default;}
#formatRulesModalBg .fr-opts{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:13px;}
#formatRulesModalBg .fr-opts label{display:inline-flex;align-items:center;gap:3px;color:#33414f;}
#formatRulesModalBg input[type=text]{padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;}
#formatRulesModalBg select{padding:3px 5px;border:1px solid var(--border);border-radius:4px;font:inherit;}
#formatRulesModalBg .fr-cond{font-size:13px;color:#33414f;}
#frRuleModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#frFmtPopupBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#replaceRulesModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#rrEditModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
#frRuleModalBg .fr-opts{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:13px;}
#frRuleModalBg .fr-opts label{display:inline-flex;align-items:center;gap:3px;color:#33414f;}
#frFmtPopupBg .fr-opts{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:13px;}
#frFmtPopupBg .fr-opts label{display:inline-flex;align-items:center;gap:3px;color:#33414f;}
#frRuleModalBg input[type=text]{padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;}
#frRuleModalBg select{padding:3px 5px;border:1px solid var(--border);border-radius:4px;font:inherit;}
.fr-cond-row{display:flex;align-items:center;gap:6px;margin:5px 0;flex-wrap:wrap;}
.fr-cond-row select{padding:3px 5px;border:1px solid var(--border);border-radius:4px;font:inherit;}
.fr-cond-row input[type=text]{padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;width:200px;}
.fr-cond-row button{padding:2px 8px;font-size:12px;}
.fr-cond-row button:disabled{opacity:.45;cursor:default;}
.fr-tags{display:inline-flex;flex-wrap:wrap;gap:2px;align-items:center;}
.fr-tag{display:inline-block;background:#e8f1fb;color:#1a5fb4;border:1px solid #bcd6f0;border-radius:3px;padding:1px 6px;font-size:12px;margin:2px;}
.fr-tag-none{background:#f0f2f4;color:#5a6b7c;border-color:#d5dbe1;}
.fr-tags-empty{color:#9aa7b4;font-size:12px;}
@media (max-width:900px){
  /* 窄屏单列布局：回退自然高度（图上文下纵向堆叠），文字窗不裁剪 */
  .page-row{grid-template-columns:1fr;}
  .img-panel img{width:100%;}
  .editable{font-size:calc(var(--editor-font-size) + 2px);height:auto;min-height:160px;overflow-y:visible;}
}

/* 统一视觉与轻量交互：只装饰视图层，不改变虚拟列表尺寸模型与正文导出契约。 */
:root{
  --accent:#3568B8;--accent-hover:#2A5596;--accent-soft:#E9F0FA;
  --border:#D9E0E8;--border-strong:#BEC9D6;
  --bg:#F3F5F8;--bg-hover:#F0F4F8;--bg-active:#E4EBF3;
  --text:#202B38;--text-muted:#526475;--text-dim:#748292;
  --radius:8px;--radius-sm:6px;
  --shadow-sm:0 1px 2px rgba(28,43,61,.05),0 2px 8px rgba(28,43,61,.035);
  --shadow-md:0 8px 24px rgba(28,43,61,.10),0 2px 6px rgba(28,43,61,.05);
  --shadow-lg:0 18px 48px rgba(20,35,55,.16),0 4px 12px rgba(20,35,55,.07);
  --glass-bg:rgba(255,255,255,.88);--glass-border:rgba(217,224,232,.85);
  --focus-ring:0 0 0 3px rgba(53,104,184,.18);
  --motion-fast:140ms;--motion-base:200ms;
}
.dark{
  --accent:#82A9E8;--accent-hover:#A2C1F1;--accent-soft:#263854;
  --bg:#141A23;--bg-elev:#1D2531;--bg-hover:#273242;--bg-active:#303E52;
  --text:#E6EBF2;--text-muted:#A9B5C4;--text-dim:#8492A5;
  --glass-bg:rgba(29,37,49,.88);--glass-border:rgba(74,86,105,.7);
  --shadow-lg:0 18px 48px rgba(0,0,0,.38),0 4px 12px rgba(0,0,0,.24);
}
::selection{background:rgba(53,104,184,.20);}
body{letter-spacing:.01em;}
button,select,input,textarea{transition-duration:var(--motion-fast);}
button:focus-visible,select:focus-visible,input:focus-visible,textarea:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
*::-webkit-scrollbar{width:9px;height:9px;}
*::-webkit-scrollbar-track{background:transparent;}
*::-webkit-scrollbar-thumb{background:rgba(116,130,146,.34);border:2px solid transparent;border-radius:8px;background-clip:padding-box;}
*::-webkit-scrollbar-thumb:hover{background:rgba(82,100,117,.55);border:2px solid transparent;background-clip:padding-box;}
#toolbar{box-shadow:0 1px 0 rgba(28,43,61,.02),0 5px 18px rgba(28,43,61,.045);}
.ribbon-tabs{height:30px;padding:0 10px;background:var(--glass-bg);}
.ribbon-tab{height:30px;padding:0 14px;letter-spacing:.03em;}
.ribbon-tab.active{font-weight:650;}
.ribbon-panel{padding:4px 8px;background:var(--glass-bg);}
#toolbar .tb-group{border-color:transparent;background:rgba(255,255,255,.58);box-shadow:inset 0 0 0 1px var(--border);}
.dark #toolbar .tb-group{background:rgba(255,255,255,.025);}
#toolbar .tb-group:hover{background:var(--bg-elev);border-color:transparent;}
#toolbar .ic-btn,#toolbar .tb-btn,#toolbar .tb-more-btn{transition:background var(--motion-fast),border-color var(--motion-fast),color var(--motion-fast),box-shadow var(--motion-fast),transform 80ms ease;}
#toolbar .tb-statusbar{background:var(--glass-bg);color:var(--text-muted);}
#status,#pagePos,#pos{font-variant-numeric:tabular-nums;}
#hintbar{color:var(--text-muted);background:var(--accent-soft);border-bottom-color:transparent;}
.page-row{background:var(--bg-elev);border-color:var(--border);border-radius:10px;box-shadow:var(--shadow-sm);transition:border-color var(--motion-base),box-shadow var(--motion-base);}
.page-row:hover{transform:none;border-color:var(--border-strong);box-shadow:var(--shadow-md);}
.page-head{color:var(--text-muted);font-weight:600;letter-spacing:.04em;border-bottom-style:solid;}
.page-err-badge{box-shadow:0 2px 6px rgba(192,57,43,.18);}
.img-panel{border-color:var(--border);border-radius:7px;background:var(--bg);}
.img-panel img{border-radius:3px;}
.editable{padding:14px 18px;border-color:var(--border);border-radius:7px;background:var(--bg-elev);line-height:1.85;color:var(--text);caret-color:var(--accent);transition:border-color var(--motion-fast),box-shadow var(--motion-fast);}
#pages.ptoe-line-num .editable>p::after,#pages.ptoe-line-num .editable>div::after,
#pages.ptoe-line-num .editable>h1::after,#pages.ptoe-line-num .editable>h2::after,
#pages.ptoe-line-num .editable>h3::after,#pages.ptoe-line-num .editable>h4::after,
#pages.ptoe-line-num .editable>h5::after,#pages.ptoe-line-num .editable>h6::after,
#pages.ptoe-line-num .editable>table tr>td:first-child::after,#pages.ptoe-line-num .editable>table tr>th:first-child::after{line-height:1.85;}
.editable:focus{border-color:var(--accent);box-shadow:var(--focus-ring);}
.editable h1,.editable h2,.editable h3,.editable h4,.editable h5,.editable h6{line-height:1.5;letter-spacing:.01em;}
.modal{background:var(--bg-elev);color:var(--text);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow-lg);padding:22px 24px;line-height:1.65;}
.modal h3{font-size:18px;letter-spacing:.01em;}
.modal h4{color:var(--text);}
.settings-tab,.scope-btn,.tb-btn,.pop-btn,.ctx-item{transition:background var(--motion-fast),border-color var(--motion-fast),color var(--motion-fast),box-shadow var(--motion-fast);}
#popup,#contextMenu,.ctx-submenu,#errPopup,#imgPopup,#dividerMenu,#tblMenu,#tblAlignGrid,.color-pop{background:var(--bg-elev);border-color:var(--border);box-shadow:var(--shadow-lg);}
#popup,.ctx-submenu,#errPopup,#imgPopup,#dividerMenu,#tblMenu,#tblAlignGrid,.color-pop{border-radius:10px;}
.ctx-item,.pop-btn,#tblMenu button{color:var(--text);}
.ctx-item:hover,#tblMenu button:hover{background:var(--accent-soft);color:var(--accent);}
#tip{border:1px solid rgba(255,255,255,.12);border-radius:7px;box-shadow:var(--shadow-md);}
.toast{border:1px solid rgba(255,255,255,.12);box-shadow:var(--shadow-lg);}
#toast .toast{transform:translateY(8px);}
#toast .toast.show{transform:translateY(0);}
@media (prefers-reduced-motion:no-preference){
  @keyframes ptoeUiModalIn{from{opacity:0;transform:translateY(8px) scale(.985);}to{opacity:1;transform:none;}}
  @keyframes ptoeUiMenuIn{from{opacity:0;transform:translateY(-4px);}to{opacity:1;transform:none;}}
  @keyframes ptoeUiFade{from{opacity:0;}to{opacity:1;}}
  .modal{animation:ptoeUiModalIn var(--motion-base) cubic-bezier(.2,.8,.2,1);}
  .ribbon-panel.active{animation:ptoeUiFade var(--motion-fast) ease-out;}
  #toolbar .tb-more.open .tb-more-panel,#contextMenu,#popup,#errPopup,#imgPopup,#dividerMenu,#tblMenu,#tblAlignGrid,.color-pop{animation:ptoeUiMenuIn var(--motion-fast) ease-out;}
}
@media (prefers-reduced-motion:reduce){
  .modal,.ribbon-panel.active,#toolbar .tb-more.open .tb-more-panel,#errPopup,#imgPopup,#dividerMenu,#tblMenu,#tblAlignGrid,.color-pop{animation:none !important;}
}
</style>
</head>
<body>
<div id="toolbar">
  <!-- Ribbon 选项卡行 -->
  <div class="ribbon-tabs" role="tablist" aria-label="功能选项卡">
    <button type="button" class="ribbon-tab active" id="tabHome" role="tab" aria-selected="true" aria-controls="panelHome" title="开始 (Alt+1)">开始</button>
    <button type="button" class="ribbon-tab" id="tabInsert" role="tab" aria-selected="false" aria-controls="panelInsert" title="插入 (Alt+2)">插入</button>
    <button type="button" class="ribbon-tab" id="tabProof" role="tab" aria-selected="false" aria-controls="panelProof" title="校对 (Alt+3)">校对</button>
    <button type="button" class="ribbon-tab" id="tabPage" role="tab" aria-selected="false" aria-controls="panelPage" title="页面 (Alt+4)">页面</button>
  </div>

  <!-- 面板：开始（默认可见） -->
  <div class="ribbon-panel active" id="panelHome" role="tabpanel" aria-labelledby="tabHome">
    <div class="tb-group" role="group" aria-label="编辑">
      <button type="button" class="ic-btn" id="undoBtn" onmousedown="event.preventDefault()" disabled title="撤回上一步 (Ctrl+Z)" aria-label="撤回 (Ctrl+Z)">
        <svg viewBox="0 0 24 24"><path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-9-9 9 9 0 0 0-6 2.3L3 13"/></svg>
      </button>
      <!-- 2026-10 修复：Ctrl+Shift+Z 实际绑给「回退原文」（proofread_revert），此处不得再宣传为重做 -->
      <button type="button" class="ic-btn" id="redoBtn" onmousedown="event.preventDefault()" disabled title="前进下一步 (Ctrl+Y)" aria-label="前进 (Ctrl+Y)">
        <svg viewBox="0 0 24 24"><path d="M21 7v6h-6"/><path d="M3 17a9 9 0 0 1 9-9 9 9 0 0 1 6 2.3L21 13"/></svg>
      </button>
      <!-- 编辑器显示字号（仅视图，不写入内容） -->
      <label style="display:inline-flex;align-items:center;gap:2px;" title="编辑器显示字号：只改变编辑区文字的显示大小，不修改正文、不写入内容（真正改文字字号请用「字符格式」）">
        <span aria-hidden="true" style="font-size:12px;line-height:1;color:var(--text-muted);letter-spacing:.5px;user-select:none;">Aa</span>
        <select id="fontSizeSel" aria-label="编辑器显示字号（仅影响视图，不写入内容）" title="编辑器显示字号（仅影响视图，不写入内容）" style="height:26px;padding:0 6px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg-elev);color:var(--text);font:inherit;">
          <option value="12">12</option><option value="13">13</option><option value="14" selected>14</option>
          <option value="15">15</option><option value="16">16</option><option value="17">17</option>
          <option value="18">18</option><option value="20">20</option>
        </select></label>
    </div>
    <span class="ribbon-sep"></span>
    <div class="tb-group" role="group" aria-label="段落格式">
      <button type="button" class="ic-btn" data-op="bold" onmousedown="event.preventDefault()" title="粗体 (Ctrl+B)" aria-label="粗体 (Ctrl+B)">
        <svg viewBox="0 0 24 24"><path d="M6 4h8a4 4 0 0 1 4 4v6a4 4 0 0 1-4 4H6z"/><path d="M6 12h9a4 4 0 0 0 0-8H6v16"/></svg>
      </button>
      <button type="button" class="ic-btn" data-op="italic" onmousedown="event.preventDefault()" title="斜体 (Ctrl+I)" aria-label="斜体 (Ctrl+I)">
        <svg viewBox="0 0 24 24"><line x1="19" y1="4" x2="10" y2="4"/><line x1="14" y1="20" x2="5" y2="20"/><line x1="15" y1="4" x2="9" y2="20"/></svg>
      </button>
      <button type="button" class="ic-btn" data-op="heading" onmousedown="event.preventDefault()" title="标题：正文↔一级标题↔…↔六级标题循环 (Ctrl+1)" aria-label="标题 (Ctrl+1)">
        <svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16M8 6v12M16 6v12"/></svg>
      </button>
      <button type="button" class="ic-btn" data-op="p" onmousedown="event.preventDefault()" title="正文：转为普通段落 (Ctrl+0)" aria-label="正文 (Ctrl+0)">
        <svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
      </button>
      <button type="button" class="ic-btn" data-op="remove" onmousedown="event.preventDefault()" title="清除格式" aria-label="清除格式">
        <svg viewBox="0 0 24 24"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
      </button>
      <button type="button" class="ic-btn" data-op="note" onmousedown="event.preventDefault()" title="注释：把当前块设为注释（小字灰色） (Ctrl+Shift+N)" aria-label="注释 (Ctrl+Shift+N)">
        <svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><line x1="10" y1="9" x2="8" y2="9"/></svg>
      </button>
      <button type="button" class="ic-btn" data-op="citation" onmousedown="event.preventDefault()" title="引用：把选中内容设为引用（斜体 + 独立字体，默认宋体；整块/光标处则切换整段）" aria-label="引用">
        <svg viewBox="0 0 24 24"><text x="12" y="16" text-anchor="middle" font-size="14" fill="currentColor" stroke="none">引</text></svg>
      </button>
      <div class="tb-more">
        <button type="button" class="tb-more-btn" id="charWrapBtn" onmousedown="event.preventDefault()" aria-label="文字包围" aria-expanded="false" aria-controls="charWrapMenu" title="文字包围：下划线 / 波浪线 / 双下划线 / 上划线 / 下加点 / 着重号 / 删除线 / 边框 / 底纹 / 突显 / 拼音注音">
          包围 <svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
        <div class="tb-more-panel" id="charWrapMenu" role="menu" aria-label="文字包围选项">
          <button type="button" class="tb-btn" role="menuitem" data-wrap="underline">下划线</button>
          <button type="button" class="tb-btn" role="menuitem" data-wrap="wavy">波浪线</button>
          <button type="button" class="tb-btn" role="menuitem" data-wrap="double">双下划线</button>
          <button type="button" class="tb-btn" role="menuitem" data-wrap="overline">上划线</button>
          <button type="button" class="tb-btn" role="menuitem" data-wrap="underdot">下加点</button>
          <button type="button" class="tb-btn" role="menuitem" data-wrap="emphasis">着重号</button>
          <!-- 2026-10 修复：删除线原写成 data-wrap="underdot"（下加点）→ 点「删除线」实际加下加点 -->
          <button type="button" class="tb-btn" role="menuitem" data-wrap="strike">删除线</button>
          <button type="button" class="tb-btn" role="menuitem" data-wrap="charbox">字符边框</button>
          <button type="button" class="tb-btn" role="menuitem" data-wrap="shade">底纹</button>
          <div style="height:1px;margin:3px 8px;background:var(--border);"></div>
          <button type="button" class="tb-btn" role="menuitem" data-wrap="highlight-green">突显·绿</button>
          <button type="button" class="tb-btn" role="menuitem" data-wrap="highlight-blue">突显·蓝</button>
          <button type="button" class="tb-btn" role="menuitem" data-wrap="highlight-pink">突显·粉</button>
          <button type="button" class="tb-btn" role="menuitem" data-wrap="highlight-orange">突显·橙</button>
          <button type="button" class="tb-btn" role="menuitem" data-wrap="highlight-purple">突显·紫</button>
          <div style="height:1px;margin:3px 8px;background:var(--border);"></div>
          <button type="button" class="tb-btn" role="menuitem" data-wrap="ruby">拼音注音</button>
        </div>
      </div>
      <div class="tb-more">
        <button type="button" class="tb-more-btn" id="supSubBtn" onmousedown="event.preventDefault()" aria-label="上标下标" aria-expanded="false" aria-controls="supSubMenu" title="上标/下标：把选中文字设为上标或下标的小字符">
          上下标 <svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
        <div class="tb-more-panel" id="supSubMenu" role="menu" aria-label="上标下标选项">
          <button type="button" class="tb-btn" role="menuitem" data-sup="sup">上标</button>
          <button type="button" class="tb-btn" role="menuitem" data-sup="sub">下标</button>
        </div>
      </div>
      <button type="button" class="tb-btn" id="formatRulesBtn" role="menuitem" title="格式规则：对选中文字一键应用自定义规则（可多条叠加；Ctrl+Shift+Q）" aria-label="格式规则">格式规则</button>
      <!-- 2026-10 修复：Ribbon 重构时这一个下拉只剩了一个 display:none 的空容器，
           里面的入口（去空/突显/转成表格/表格转文本，以及 app.js 注入的「插入题注」）
           既不可见也点不到 → 恢复成真正的 .tb-more 下拉。 -->
      <div class="tb-more">
        <button type="button" class="tb-more-btn" id="moreFormatBtn" aria-expanded="false" aria-controls="morePanelFormat" title="更多格式工具：去空 / 突显 / 转成表格 / 表格转文本">
          更多 <svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
        <div class="tb-more-panel" id="morePanelFormat" role="menu" aria-label="更多格式工具">
          <button type="button" class="tb-btn" data-op="strip_ws" role="menuitem" onmousedown="event.preventDefault()" title="去空（去除段落内全部空白，保留换行）" aria-label="去空">去空</button>
          <button type="button" class="tb-btn" data-op="highlight" role="menuitem" onmousedown="event.preventDefault()" title="突显文字：对选中文字设置背景色高亮" aria-label="突显文字">突显</button>
          <button type="button" class="tb-btn" id="tableConvertBtn" data-op="table" role="menuitem" onmousedown="event.preventDefault()" title="转成表格：把选中的文字转成原生表格（支持 Markdown 管道表 / 画框表 / 空格对齐列；手动档，不要求分隔行）" aria-label="转成表格">转成表格</button>
          <button type="button" class="tb-btn" id="tableToTextBtn" data-op="table2text" role="menuitem" onmousedown="event.preventDefault()" title="表格转文本：把光标所在表格还原成 Markdown 管道文字（光标不在表格内则转当前页全部表格；可再用「转成表格」转回）" aria-label="表格转文本">表格转文本</button>
        </div>
      </div>
    </div>
    <span class="ribbon-sep"></span>
    <div class="tb-group" role="group" aria-label="对齐">
      <button type="button" class="ic-btn" data-op="align_left" onmousedown="event.preventDefault()" title="居左 (Ctrl+Shift+Left)" aria-label="居左 (Ctrl+Shift+Left)">
        <svg viewBox="0 0 24 24"><line x1="3" y1="6" x2="15" y2="6"/><line x1="3" y1="12" x2="15" y2="12"/><line x1="3" y1="18" x2="15" y2="18"/></svg>
      </button>
      <button type="button" class="ic-btn" data-op="align_center" onmousedown="event.preventDefault()" title="居中 (Ctrl+Shift+Up)" aria-label="居中 (Ctrl+Shift+Up)">
        <svg viewBox="0 0 24 24"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
      </button>
      <button type="button" class="ic-btn" data-op="align_right" onmousedown="event.preventDefault()" title="居右 (Ctrl+Shift+Right)" aria-label="居右 (Ctrl+Shift+Right)">
        <svg viewBox="0 0 24 24"><line x1="9" y1="6" x2="21" y2="6"/><line x1="9" y1="12" x2="21" y2="12"/><line x1="9" y1="18" x2="21" y2="18"/></svg>
      </button>
      <button type="button" class="ic-btn" data-op="flush" onmousedown="event.preventDefault()" title="顶格" aria-label="顶格">
        <svg viewBox="0 0 24 24"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
      </button>
      <button type="button" class="ic-btn" data-op="indent" onmousedown="event.preventDefault()" title="缩进" aria-label="缩进">
        <svg viewBox="0 0 24 24"><line x1="3" y1="6" x2="21" y2="6"/><line x1="9" y1="12" x2="21" y2="12"/><line x1="9" y1="18" x2="21" y2="18"/></svg>
      </button>
    </div>
    <span class="ribbon-sep"></span>
    <div class="tb-group" role="group" aria-label="格式刷">
      <div class="tb-more">
        <button type="button" class="tb-more-btn" id="formatBrushBtn" aria-label="格式刷" aria-expanded="false" aria-controls="brushPanel" title="格式刷：捕获格式 / 功能刷（合并/标题等，可混合）；快捷键默认 Shift+Ctrl+1..0（可在设置中自定义）">
          格式刷 <svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
        <div class="tb-more-panel" id="brushPanel" role="menu" aria-label="格式刷菜单" style="min-width:150px;">
          <button type="button" class="tb-btn" id="brushFmtBtn" role="menuitem" title="捕获当前选区格式，持续应用到后续选中的文字（持续格式刷）" aria-label="格式刷（捕获）">格式刷（捕获）</button>
          <div style="height:1px;margin:3px 8px;background:var(--border);"></div>
          <div id="brushOpsList" style="display:flex;flex-direction:column;gap:1px;"></div>
          <button type="button" class="tb-btn" id="brushMixBtn" role="menuitem" title="混合刷：勾选多个功能组合成一个刷，点击段落依次生效" aria-label="混合刷">混合…</button>
        </div>
        <div class="tb-more-panel" id="brushMixPanel" role="menu" aria-label="混合刷多选" style="min-width:170px;">
          <div id="brushMixList" style="display:flex;flex-direction:column;gap:2px;padding:2px 0;"></div>
          <div style="display:flex;gap:4px;margin-top:4px;">
            <button type="button" class="tb-btn" id="brushMixApply" style="flex:1;" title="按勾选启用混合刷" aria-label="启用混合刷">启用</button>
            <button type="button" class="tb-btn" id="brushMixCancel" style="flex:1;" title="取消" aria-label="取消">取消</button>
          </div>
        </div>
      </div>
    </div>
    <span class="ribbon-sep"></span>
    <div class="tb-group" role="group" aria-label="导航与搜索">
      <button type="button" class="ic-btn" id="searchOpenBtn" onmousedown="event.preventDefault()" title="搜索/替换全部页面 (Ctrl+F)" aria-label="搜索 (Ctrl+F)">
        <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      </button>
      <label style="display:inline-flex;align-items:center;gap:2px;">
        <input type="number" id="pageJump" min="1" placeholder="页" aria-label="跳转页码 (Ctrl+G)" title="输入页码后回车跳转 (Ctrl+G)" style="width:52px;height:26px;padding:0 6px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg-elev);color:var(--text);font:inherit;">
      </label>
      <button type="button" class="tb-btn" id="jumpBtn" onmousedown="event.preventDefault()" title="跳转到指定页码 (Ctrl+G)">跳转</button>
    </div>
  </div>

  <!-- 面板：插入 -->
  <div class="ribbon-panel" id="panelInsert" role="tabpanel" aria-labelledby="tabInsert">
    <div class="tb-group" role="group" aria-label="插入对象">
      <button type="button" class="tb-btn" id="imgExternalBtn" onmousedown="event.preventDefault()" title="从本地文件选择图片，插入到文字光标处" aria-label="插入外部图片">
        <svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="3"/><line x1="21" y1="15" x2="16" y2="10"/></svg>
        图片
      </button>
      <input type="file" id="imgExternalInput" accept="image/*" style="display:none"/>
      <!-- 2026-10 修复：原内联 width:100%;margin:4px 0 是纵向表单写法，放进 32px 高的 Ribbon 行里会把
           行撑高/挤歪其它按钮 → 改为固定高度、按内容宽度 -->
      <select id="imgModeSel" title="插入图片的显示模式：全画幅=占满文字宽度，局部=按原尺寸居中，行内=嵌在文字中间（50% 宽度）" style="height:26px;padding:0 24px 0 6px;">
        <option value="full">全画幅</option><option value="fit">局部</option><option value="inline">行内</option>
      </select>
      <button type="button" class="tb-btn" id="tableBtn" onmousedown="event.preventDefault()" title="插入表格：在光标处插入空表格，行/列数与是否带表头可设（Ctrl+Alt+T）" aria-label="插入表格">
        <svg viewBox="0 0 24 24"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/><line x1="6" y1="3" x2="6" y2="21"/><line x1="12" y1="3" x2="12" y2="21"/><line x1="18" y1="3" x2="18" y2="21"/></svg>
        表格
      </button>
      <button type="button" class="tb-btn" id="latexBtn" role="menuitem" onmousedown="event.preventDefault()" title="插入 LaTeX 公式：输入 LaTeX 源码，KaTeX 渲染成 MathML；导出 EPUB 后照常显示" aria-label="插入公式">
        <svg viewBox="0 0 24 24"><path d="M6 3h12M6 21h12M6 12h12M6 6h12M6 18h12M3 6h2M3 12h2M3 18h2M21 6h-2M21 12h-2M21 18h-2"/></svg>
        公式
      </button>
      <button type="button" class="tb-btn" id="dividerBtn" role="menuitem" onmousedown="event.preventDefault()" title="分隔线：在光标处插入一条分隔线" aria-label="插入分隔线">
        <svg viewBox="0 0 24 24"><line x1="3" y1="12" x2="21" y2="12"/></svg>
        分隔线
      </button>
      <button type="button" class="tb-btn" id="colsBtn" role="menuitem" data-op="columns" onmousedown="event.preventDefault()" title="分栏：选中段落两两转成无边框两列表" aria-label="分栏">
        <svg viewBox="0 0 24 24"><rect x="3" y="3" width="8" height="18" rx="1"/><rect x="13" y="3" width="8" height="18" rx="1"/></svg>
        分栏
      </button>
    </div>
    <span class="ribbon-sep"></span>
    <div class="tb-group" role="group" aria-label="文本样式">
      <button type="button" class="tb-btn" id="colorBtn" role="menuitem" title="文本颜色" aria-label="文本颜色">
        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 8v4M12 16v4"/></svg>
        颜色
      </button>
      <button type="button" class="ic-btn" id="charFormatBtn" onmousedown="event.preventDefault()" title="字符格式：为选中文字设置逐字符的字号（8-72 像素）、字体与颜色" aria-label="字符格式">
        <svg viewBox="0 0 24 24"><path d="M4 19 9.5 5h1L16 19"/><line x1="6" y1="14" x2="14" y2="14"/><line x1="17" y1="19V9"/><line x1="21" y1="19v-5"/><line x1="20" y1="12h2"/></svg>
      </button>
      <button type="button" class="ic-btn" id="charFmtClearBtn" onmousedown="event.preventDefault()" title="清除字符格式：移除选中文字的逐字符字号/字体/颜色" aria-label="清除字符格式">
        <svg viewBox="0 0 24 24"><path d="M5 17 12 10l3 3 4-4"/><line x1="4" y1="20" x2="20" y2="20"/><path d="M4 4l16 16"/></svg>
      </button>
      <button type="button" class="tb-btn" id="mdToggleBtn" title="切换 Markdown 源码 / 富文本编辑模式（详见帮助） (Ctrl+Shift+D)">Markdown</button>
    </div>
  </div>

  <!-- 面板：校对 -->
  <div class="ribbon-panel" id="panelProof" role="tabpanel" aria-labelledby="tabProof">
    <div class="tb-group" role="group" aria-label="文本转换">
      <button type="button" class="tb-btn" id="toSimplifiedBtn" title="把全部页面文字转为简体（繁体→简体） (Ctrl+Shift+T)">繁→简</button>
      <button type="button" class="tb-btn" id="toTraditionBtn" title="把全部页面文字转为繁体（简体→繁体） (Ctrl+Shift+Y)">简→繁</button>
      <button type="button" class="tb-btn" id="cleanBtn" title="智能清理：合并被 OCR 拆散的小段落、清除段首 #/* 等符号、归一化中英文标点、移除残留的 HTML 标签 (Ctrl+Shift+C)">清理</button>
    </div>
    <span class="ribbon-sep"></span>
    <div class="tb-group" role="group" aria-label="纠错与段落">
      <!-- 2026-10：校正下拉从页面级浮层（position:fixed）改为本工具栏内的 .tb-more 下拉，
           开合统一走 .tb-more.open 的 CSS；菜单项仍沿用 prMenu* 这些 id，JS 绑定不变。 -->
      <div class="tb-more">
        <button type="button" class="tb-more-btn" id="proofreadBtn" aria-expanded="false" aria-controls="proofreadMenu" title="文字纠错：校正当前页 / 应用全部候选 / 清除标注 / 回退原文 (Ctrl+K)" aria-label="文字纠错 (Ctrl+K)">
          校正 <svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
        <div class="tb-more-panel" id="proofreadMenu" role="menu" aria-label="文字纠错" style="min-width:280px;left:0;right:auto;">
          <button type="button" class="tb-btn" id="prMenuCorrect" role="menuitem">校正</button>
          <button type="button" class="tb-btn" id="prMenuReocr" role="menuitem">重识别</button>
          <button type="button" class="tb-btn" id="prMenuApply" role="menuitem">应用</button>
          <button type="button" class="tb-btn" id="prMenuClear" role="menuitem">清除</button>
          <button type="button" class="tb-btn" id="prMenuRevert" role="menuitem">回退</button>
          <div style="height:1px;margin:3px 8px;background:var(--border);"></div>
          <button type="button" class="tb-btn" id="prMenuApplyAll" role="menuitem" title="对所有已挂载页应用纠错提示">全部应用</button>
          <button type="button" class="tb-btn" id="prMenuClearAll" role="menuitem" title="清除所有页的纠错标注">全部清除</button>
          <div style="height:1px;margin:3px 8px;background:var(--border);"></div>
          <div style="padding:2px 4px;">
            <label style="display:flex;align-items:center;gap:6px;font-size:12px;" title="默认只执行三条规则：连续重复文字 / 连续标点 / 中文中的连续字母">原有规则（半角转全角/引号配对/混淆表/词典） <input type="checkbox" id="prLegacyRules" style="margin-left:auto;"></label>
            <label style="display:flex;align-items:center;gap:6px;font-size:12px;margin-top:6px;">LLM 深度校对 <input type="checkbox" id="prLlmEnable" style="margin-left:auto;"></label>
            <label style="display:flex;align-items:center;gap:6px;font-size:12px;margin-top:6px;">模型 <select id="prLlmModel" style="flex:1;min-width:0;"></select></label>
            <small style="color:var(--text-muted);display:block;margin-top:4px;font-size:11px;line-height:1.5;">启用后每次校正会额外调用本地 llama-server 进行深度校对。模型留空则使用当前选中模型。</small>
            <div style="display:flex;gap:6px;margin-top:8px;">
              <button type="button" id="prLlmStart" style="flex:1;">启动服务</button>
              <button type="button" id="prLlmStop" style="flex:1;">停止服务</button>
              <button type="button" id="prLlmSwitch" style="flex:1;" title="用当前所选模型重启服务：自动停止旧模型并加载新模型（无需先手动停止）">切换模型</button>
            </div>
            <small id="prLlmStatus" style="color:var(--text-muted);display:block;margin-top:4px;font-size:11px;"></small>
          </div>
        </div>
      </div>
      <button type="button" class="ic-btn" id="indentDlgBtn" onmousedown="event.preventDefault()" title="段落设置：左/右缩进、首行/悬挂缩进、段前段后与行距（导出 EPUB 生效）" aria-label="段落设置">¶</button>
    </div>
  </div>

  <!-- 面板：页面 -->
  <div class="ribbon-panel" id="panelPage" role="tabpanel" aria-labelledby="tabPage">
    <div class="tb-group" role="group" aria-label="标记">
      <button type="button" class="ic-btn" data-op="marker_full" onmousedown="event.preventDefault()" title="全文标记：当前文章到此结束，后续内容属于新文章（开新页） (Ctrl+Shift+F)" aria-label="全文标记 (Ctrl+Shift+F)">
        <svg viewBox="0 0 24 24"><path d="M4 4h16a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/><line x1="12" y1="8" x2="12" y2="16"/></svg>
      </button>
      <button type="button" class="ic-btn" data-op="marker_note" onmousedown="event.preventDefault()" title="注释标记：插入到光标处，由对应注释段落替换（数量需一一匹配） (Ctrl+Shift+M)" aria-label="注释标记 (Ctrl+Shift+M)">
        <svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><circle cx="12" cy="14" r="1"/></svg>
      </button>
      <button type="button" class="ic-btn" data-op="marker_join" onmousedown="event.preventDefault()" title="段落标记：插入到光标处；段首=与上一段合并，段尾=与下一段合并 (Ctrl+Shift+J)" aria-label="段落标记 (Ctrl+Shift+J)">
        <svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/><path d="M12 6v6M12 12v6" stroke-width="3"/></svg>
      </button>
      <button type="button" class="ic-btn" data-op="marker_page" onmousedown="event.preventDefault()" title="换页标记：从此处之后的内容显示在新的一页 (Ctrl+Shift+P)" aria-label="换页标记 (Ctrl+Shift+P)">
        <svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><line x1="12" y1="10" x2="12" y2="14"/><line x1="10" y1="12" x2="14" y2="12"/></svg>
      </button>
    </div>
    <span class="ribbon-sep"></span>
    <div class="tb-group" role="group" aria-label="主要操作">
      <button type="button" class="tb-btn" id="saveBtn" onmousedown="event.preventDefault()" title="保存 (Ctrl+S)" aria-label="保存 (Ctrl+S)">
        <svg viewBox="0 0 24 24" width="14" height="14" style="stroke:currentColor;fill:none;stroke-width:2;vertical-align:-2px;margin-right:4px;"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
        保存
      </button>
      <button type="button" class="tb-btn" id="finishBtn" onmousedown="event.preventDefault()" title="完成并转换 (Ctrl+Enter)" aria-label="完成并转换 (Ctrl+Enter)">
        <svg viewBox="0 0 24 24" width="14" height="14" style="stroke:currentColor;fill:none;stroke-width:2;vertical-align:-2px;margin-right:4px;"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18"/></svg>
        完成并转换
      </button>
      <div class="tb-more">
        <button type="button" class="tb-more-btn" id="finishMoreBtn" aria-label="导出选项" aria-expanded="false">
          <svg viewBox="0 0 24 24" width="12" height="12" style="stroke:currentColor;fill:none;stroke-width:2;"><polyline points="6 9 12 15 18 9"/></svg>
          导出
        </button>
        <div class="tb-more-panel" id="finishMorePanel" role="menu" style="left:0;right:auto;min-width:120px;">
          <button type="button" class="tb-btn" id="exportTxtBtn" role="menuitem" title="导出为纯文本文件（.txt）">导出 TXT</button>
          <button type="button" class="tb-btn" id="exportDocxBtn" role="menuitem" title="导出为 Word 文档（.docx）">导出 DOCX</button>
          <button type="button" class="tb-btn" id="exportMdBtn" role="menuitem" title="导出为 Markdown 文件（.md）">导出 MD</button>
          <button type="button" class="tb-btn" id="exportEpubBtn" role="menuitem" title="导出为 EPUB 电子书（.epub）">导出 EPUB</button>
        </div>
      </div>
    </div>
    <span class="ribbon-sep"></span>
    <!-- 2026-10：原「更多」二级下拉里的 暂存/帮助/历史记录/设置 已全部平铺到本面板 -->
    <div class="tb-group" role="group" aria-label="工具">
      <button type="button" class="tb-btn" id="stageBtn" title="暂存：把当前修改暂时保存到本地历史缓存（不转换，可随时恢复） (Ctrl+Shift+S)" aria-label="暂存 (Ctrl+Shift+S)" style="justify-content:flex-start;">
        <svg viewBox="0 0 24 24" width="14" height="14" style="stroke:currentColor;fill:none;stroke-width:2;vertical-align:-2px;margin-right:4px;"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>
        暂存
      </button>
      <button type="button" class="tb-btn" id="helpBtn" title="帮助：Markdown 格式、快捷键与标记说明 (F1)" aria-label="帮助 (F1)" style="justify-content:flex-start;">
        <svg viewBox="0 0 24 24" width="14" height="14" style="stroke:currentColor;fill:none;stroke-width:2;"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        帮助
      </button>
      <button type="button" class="tb-btn" id="historyBtn" title="历史记录：查看/管理本地矫正缓存（文件名与路径分列、多版本） (Ctrl+H)" aria-label="历史记录 (Ctrl+H)" style="justify-content:flex-start;">
        <svg viewBox="0 0 24 24" width="14" height="14" style="stroke:currentColor;fill:none;stroke-width:2;vertical-align:-2px;margin-right:4px;"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        历史记录
      </button>
      <button type="button" class="tb-btn" id="settingsBtn" title="设置 (Ctrl+Shift+O)" aria-label="设置 (Ctrl+Shift+O)" style="justify-content:flex-start;">
        <svg viewBox="0 0 24 24" width="14" height="14" style="stroke:currentColor;fill:none;stroke-width:2;"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 15 19.4a1.65 1.65 0 0 0 1.82.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 15a1.65 1.65 0 0 0-1.51-1H21a2 2 0 0 1 2-2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
        设置
      </button>
    </div>
  </div>

  <!-- 底部状态行（常驻，不随选项卡切换） -->
  <div class="tb-statusbar">
    <button type="button" class="ic-btn" id="prevPageBtn" onmousedown="event.preventDefault()" title="上一页" aria-label="上一页">
      <svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg>
    </button>
    <span id="pagePos" aria-live="off" title="当前所在页码" style="font-size:12px;color:var(--text-muted);white-space:nowrap;">第 1 / 547 页</span>
    <div id="progressWrap" style="flex:1;min-width:120px;height:6px;background:var(--border);border-radius:3px;cursor:pointer;position:relative;overflow:hidden;" title="点击或拖拽跳转到指定进度">
      <div id="progressBar" style="width:0%;height:100%;background:var(--accent);border-radius:3px;transition:width 0.15s ease;"></div>
    </div>
    <span id="progressPct" style="font-size:11px;color:var(--text-muted);min-width:30px;text-align:right;">0%</span>
    <span id="errBadge" title="点击跳转到第一个待纠错页面" style="display:inline-flex;align-items:center;gap:3px;font-size:11px;color:#E02020;cursor:pointer;white-space:nowrap;padding:2px 8px;border-radius:12px;background:rgba(224,32,32,.1);border:1px solid rgba(224,32,32,.25);font-weight:600;">
      待纠错 <b id="errBadgeNum" style="min-width:14px;text-align:center;">0</b> 处
    </span>
    <button type="button" class="ic-btn" id="nextPageBtn" onmousedown="event.preventDefault()" title="下一页" aria-label="下一页">
      <svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
    </button>
    <span id="prLastPage" class="tb-status" aria-live="off" title="最后一次校正/重识别的页码" style="font-size:11px;color:var(--accent);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:90px;"></span>
    <span id="status" class="tb-status tb-status-main" style="font-size:11px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:180px;">加载中 ...</span>
    <span id="prCount" title="当前页面存在的可纠错文字数量" style="font-size:11px;color:var(--text-muted);white-space:nowrap;">可纠错：<b id="prCountNum" style="color:#E02020;">0</b></span>
  </div>
</div>
<div id="hintbar">
  <span class="hint-text">左侧原图（点击切换预览/原图），右侧文字可直接编辑。选中文字弹出<b>图标快捷菜单</b>（悬停有提示）；支持<b>粗体</b>、<i>斜体</i>、标题、注释、居左/居中/居右与<span class="ptoe-marker">全文/段落/注释/换页标记</span>（标记插入到光标处；段落标记段首=合上段、段尾=合下段；换页标记=此后内容显示在新的一页）。可切换 <b>Markdown 模式</b>（#标题、**粗体**、*斜体*）、繁简转换、字号调整与页码跳转，详见「帮助」。</span>
  <button type="button" id="hintClose" title="关闭提示（可随时在「帮助」中查看）" aria-label="关闭提示">✕</button>
</div>
<!-- 拼音注音弹窗（2026-10-21）：注音内容存进 span 的 data-rt 属性 -->
<div id="rubyModalBg"><div class="modal" role="dialog" aria-modal="true" aria-label="拼音注音">
  <div class="search-head">
    <h3>拼音注音</h3>
    <button type="button" class="x-btn" id="rubyCloseBtn" title="关闭 (Esc)" aria-label="关闭拼音注音">✕</button>
  </div>
  <p class="export-desc">为选中的文字添加注音（拼音 / 解释），最多 32 字。注音随内容保存，导出 EPUB 时显示在文字后的小括号里，导出 Word 时同样以小字括号注音呈现。</p>
  <input type="text" id="rubyInput" maxlength="32" placeholder="例如：nǐ hǎo" aria-label="注音内容">
  <div class="export-actions">
    <button type="button" class="btn" id="rubyOkBtn">应用</button>
    <button type="button" class="btn" id="rubyCancelBtn">取消</button>
  </div>
</div></div>
<!-- LaTeX 公式弹窗（2026-10-21）：输入 LaTeX 源码 → KaTeX 渲染成 MathML 插入正文。
     公式以 <math> 元素存进内容：导出 EPUB 原样保留（EPUB 3 原生支持 MathML），
     导出 Word/Markdown/TXT 退化为 $公式$ 纯文本。 -->
<div id="latexModalBg"><div class="modal" role="dialog" aria-modal="true" aria-label="LaTeX 公式">
  <div class="search-head">
    <h3>插入 LaTeX 公式</h3>
    <button type="button" class="x-btn" id="latexCloseBtn" title="关闭 (Esc)" aria-label="关闭公式弹窗">✕</button>
  </div>
  <p class="export-desc">输入 LaTeX 源码（如 <code>\frac{-b \pm \sqrt{b^2-4ac}}{2a}</code>），下方实时预览。公式以 MathML 存入正文，导出 EPUB 后照常显示；导出 Word/Markdown 时输出为 <code>$公式$</code>。</p>
  <textarea id="latexInput" rows="4" spellcheck="false" placeholder="例如：E=mc^2  或  \int_0^1 x^2\,dx" aria-label="LaTeX 源码"></textarea>
  <div class="latex-preview-row">
    <span class="latex-preview-label">预览</span>
    <div id="latexPreview" class="latex-preview"><span class="latex-preview-empty">（预览区）</span></div>
  </div>
  <label class="latex-display-opt" title="勾选后公式独立成行显示（display=block），否则与文字同行"><input type="checkbox" id="latexDisplay">独立成行</label>
  <div class="export-actions">
    <button type="button" class="btn primary" id="latexOkBtn">插入</button>
    <button type="button" class="btn" id="latexCancelBtn">取消</button>
  </div>
</div></div>
<div id="pages"></div>
<div id="popup"></div>
<div id="tip"></div>
<!-- 命令行（2026-10，nvim 风格）：Ctrl+; 打开；:7,10merge 按行号合并，默认作用于当前选中页 -->
<div id="cmdBar" role="dialog" aria-label="命令行">
  <span id="cmdPrefix">:</span>
  <input id="cmdInput" type="text" autocomplete="off" spellcheck="false" placeholder="如 7,10merge ／ 页 12 ／ 插入 注释（取剪贴板） ／ 选 3 5 10 ／ help" aria-label="命令输入">
  <span id="cmdHint">Esc 关闭 · Enter 执行 · ↑↓ 候选 · Ctrl+↑↓ 历史</span>
</div>
<div id="cmdSuggest">
  <div id="cmdSuggestGrip" title="拖动调整高度（双击折叠/展开）"></div>
  <div id="cmdSuggestList" role="listbox" aria-label="命令候选"></div>
</div>
<!-- 右键上下文菜单（2026-08-08）：编辑区内右键弹出；重识别/插入标记/导出/Markdown 提示/保存/暂存 -->
<div id="contextMenu" hidden>
  <div class="ctx-item ctx-sub" data-ctx="copy">复制 <span class="ctx-arrow">▸</span>
    <div class="ctx-submenu" id="ctxCopySub">
      <button type="button" class="ctx-item" data-ctx-copy="format">格式复制</button>
      <button type="button" class="ctx-item" data-ctx-copy="text">纯文本复制</button>
    </div>
  </div>
  <div class="ctx-item ctx-sub" data-ctx="paste">粘贴 <span class="ctx-arrow">▸</span>
    <div class="ctx-submenu" id="ctxPasteSub">
      <button type="button" class="ctx-item" data-ctx-paste="format">格式粘贴</button>
      <button type="button" class="ctx-item" data-ctx-paste="text">纯文本粘贴</button>
    </div>
  </div>
  <div class="ctx-sep"></div>
  <div class="ctx-item ctx-sub" data-ctx="marker">插入标记 <span class="ctx-arrow">▸</span>
    <div class="ctx-submenu" id="ctxMarkerSub">
      <button type="button" class="ctx-item" data-ctx-marker="join">段落标记</button>
      <button type="button" class="ctx-item" data-ctx-marker="page">换页标记</button>
      <button type="button" class="ctx-item" data-ctx-marker="full">全文标记</button>
      <button type="button" class="ctx-item" data-ctx-marker="note">注释标记</button>
    </div>
  </div>
  <div class="ctx-item ctx-sub" data-ctx="wrap">文字包围 <span class="ctx-arrow">▸</span>
    <div class="ctx-submenu" id="ctxWrapSub">
      <button type="button" class="ctx-item" data-ctx-wrap="underline">下划线</button>
      <button type="button" class="ctx-item" data-ctx-wrap="wavy">波浪线</button>
      <button type="button" class="ctx-item" data-ctx-wrap="double">双下划线</button>
      <button type="button" class="ctx-item" data-ctx-wrap="overline">上划线</button>
      <button type="button" class="ctx-item" data-ctx-wrap="underdot">下加点</button>
      <button type="button" class="ctx-item" data-ctx-wrap="emphasis">着重号</button>
      <button type="button" class="ctx-item" data-ctx-wrap="strike">删除线</button>
      <button type="button" class="ctx-item" data-ctx-wrap="charbox">字符边框</button>
      <button type="button" class="ctx-item" data-ctx-wrap="shade">底纹</button>
      <div class="ctx-sep"></div>
      <button type="button" class="ctx-item" data-ctx-wrap="highlight-green">突显·绿</button>
      <button type="button" class="ctx-item" data-ctx-wrap="highlight-blue">突显·蓝</button>
      <button type="button" class="ctx-item" data-ctx-wrap="highlight-pink">突显·粉</button>
      <button type="button" class="ctx-item" data-ctx-wrap="highlight-orange">突显·橙</button>
      <button type="button" class="ctx-item" data-ctx-wrap="highlight-purple">突显·紫</button>
      <div class="ctx-sep"></div>
      <button type="button" class="ctx-item" data-ctx-wrap="ruby">拼音注音</button>
    </div>
  </div>
  <div class="ctx-sep"></div>
  <button type="button" class="ctx-item" data-ctx="insertimg">插入图片</button>
  <div class="ctx-item ctx-sub" data-ctx="rules">格式规则 <span class="ctx-arrow">▸</span>
    <div class="ctx-submenu" id="ctxRulesSub"></div>
  </div>
  <button type="button" class="ctx-item" data-ctx="reocr">重识别</button>
  <button type="button" class="ctx-item" data-ctx="clear">清除校正</button>
  <button type="button" class="ctx-item" data-ctx="applypr">应用校正</button>
  <button type="button" class="ctx-item" data-ctx="clearfmt">清除格式</button>
  <button type="button" class="ctx-item" data-ctx="align_lines">多行对齐</button>
  <div class="ctx-sep"></div>
  <button type="button" class="ctx-item" data-ctx="clearpage">清空</button>
  <button type="button" class="ctx-item" data-ctx="fmtall">格式化</button>
  <div class="ctx-sep"></div>
  <button type="button" class="ctx-item" data-ctx="save">保存</button>
  <button type="button" class="ctx-item" data-ctx="stage">暂存</button>
  <div class="ctx-item ctx-sub" data-ctx="export">导出 <span class="ctx-arrow">▸</span>
    <div class="ctx-submenu" id="ctxExportSub">
      <button type="button" class="ctx-item" data-ctx-export="txt">txt格式</button>
      <button type="button" class="ctx-item" data-ctx-export="docx">docx格式</button>
      <button type="button" class="ctx-item" data-ctx-export="md">md格式</button>
      <button type="button" class="ctx-item" data-ctx-export="epub">epub格式</button>
    </div>
  </div>
</div>
<!-- 2026-10：原 #proofreadMenu 页面级浮层已迁入「校对」工具栏面板（见 #panelProof），此处不再保留。 -->
<!-- 2026-10 修复：此处原有的 #charWrapMenu / #supSubMenu 两个旧浮层菜单与 Ribbon 工具栏内的
     下拉面板 **id 重复**（getElementById 只取先出现的工具栏面板），导致工具栏下拉被旧 CSS
     （position:fixed + display:none）劫持、菜单关不掉。Ribbon 已自带同名面板，故删除这两个
     重复节点；波浪线/突显/注音等其余包围格式仍在右键菜单与选中浮动菜单中提供。 -->
<!-- 分隔线菜单（2026-09-27）：线条型四种 + 花样型八种（2026-10-21），字形预览即最终写入内容 -->
<div id="dividerMenu" class="tb-menu">
  <button type="button" data-div="solid" role="menuitem" title="实线分隔线">直线 <span class="div-glyph div-solid">────────</span></button>
  <button type="button" data-div="dashed" role="menuitem" title="线段分隔线">线段 <span class="div-glyph div-dashed">╌╌╌╌╌╌╌╌</span></button>
  <button type="button" data-div="dotted" role="menuitem" title="点线分隔线">点线 <span class="div-glyph div-dotted">┈┈┈┈┈┈┈</span></button>
  <button type="button" data-div="double" role="menuitem" title="双线分隔线">双线 <span class="div-glyph div-double">════════</span></button>
  <div class="sep"></div>
  <button type="button" data-div="stars" role="menuitem" title="星形花样分隔">星形 <span class="div-glyph div-stars">★★★★★</span></button>
  <button type="button" data-div="diamonds" role="menuitem" title="菱形花样分隔">菱形 <span class="div-glyph div-diamonds">◆◆◆◆◆</span></button>
  <button type="button" data-div="circles" role="menuitem" title="圆点花样分隔">圆点 <span class="div-glyph div-circles">●●●●●</span></button>
  <button type="button" data-div="flowers" role="menuitem" title="花纹花样分隔">花纹 <span class="div-glyph div-flowers">❀❀❀❀❀</span></button>
  <button type="button" data-div="snow" role="menuitem" title="雪花花样分隔">雪花 <span class="div-glyph div-snow">❄❄❄❄❄</span></button>
  <button type="button" data-div="wave" role="menuitem" title="波浪花样分隔">波浪 <span class="div-glyph div-wave">≈≈≈≈≈</span></button>
  <button type="button" data-div="note" role="menuitem" title="注记花样分隔">注记 <span class="div-glyph div-note">※※※※※</span></button>
  <button type="button" data-div="leaf" role="menuitem" title="叶形花样分隔">叶形 <span class="div-glyph div-leaf">❦❦❦❦❦</span></button>
</div>
<!-- 字符格式弹窗（2026-09-27）：逐字符 字号/字体/颜色，样式落在 span 的 style 上 -->
<div id="charFormatModalBg"><div class="modal search-modal" role="dialog" aria-modal="true" aria-label="字符格式">
  <div class="search-head">
    <h3>字符格式</h3>
    <button type="button" class="x-btn" id="cfCloseBtn" title="关闭 (Esc)" aria-label="关闭字符格式">✕</button>
  </div>
  <p class="export-desc">为选中的文字设置逐字符的字号、字体与颜色，随内容保存并在导出 EPUB / Word / Markdown 时生效。不修改的内容项请选「不修改」。</p>
  <div class="indent-mode-row" role="radiogroup" aria-label="字符格式作用范围">
    <label title="仅作用于当前选中文字（无选中时按光标所在整块）"><input type="radio" name="cfMode" id="cfModeSel" checked> 选中内容</label>
    <label title="作用于当前页的全部文字段落"><input type="radio" name="cfMode" id="cfModePage"> 当前页</label>
    <label title="作用于全书全部文字段落（整书一次撤销，需确认）"><input type="radio" name="cfMode" id="cfModeAll"> 全部</label>
  </div>
  <div class="indent-grid">
    <label title="字号：8-72 整数像素（留空或选「不修改」则不改）">字号
      <select id="cfSizeSel">
        <option value="">不修改</option>
        <option value="8">8</option><option value="9">9</option><option value="10">10</option>
        <option value="11">11</option><option value="12">12</option><option value="14">14</option>
        <option value="16">16</option><option value="18">18</option><option value="20">20</option>
        <option value="22">22</option><option value="24">24</option><option value="28">28</option>
        <option value="32">32</option><option value="36">36</option><option value="42">42</option>
        <option value="48">48</option><option value="56">56</option><option value="64">64</option>
        <option value="72">72</option>
      </select>
      <input type="number" id="cfSizeNum" min="8" max="72" step="1" placeholder="自定义 8-72" aria-label="自定义字号（8-72 像素）">
    </label>
    <label title="字体：仅下列 10 种字体栈可写入内容，其余会被丢弃">字体
      <select id="cfFontSel">
        <option value="">不修改</option>
        <option value="宋体, SimSun, serif">宋体</option>
        <option value="黑体, SimHei, sans-serif">黑体</option>
        <option value="楷体, KaiTi, serif">楷体</option>
        <option value="仿宋, FangSong, serif">仿宋</option>
        <option value="微软雅黑, Microsoft YaHei, sans-serif">微软雅黑</option>
        <option value="等线, DengXian, sans-serif">等线</option>
        <option value="serif">serif（衬线）</option>
        <option value="sans-serif">sans-serif（无衬线）</option>
        <option value="monospace">monospace（等宽）</option>
        <option value="cursive">cursive（手写）</option>
      </select>
    </label>
    <label title="文字颜色">颜色
      <span id="cfColorSwatch" aria-hidden="true" style="display:inline-block;width:26px;height:22px;border:1px solid var(--border);border-radius:4px;vertical-align:middle;background:#1c2733;"></span>
      <button type="button" class="tb-btn" id="cfColorBtn" style="height:22px;" title="选择文字颜色" aria-label="选择文字颜色">选色</button>
      <button type="button" class="tb-btn" id="cfColorClearBtn" style="height:22px;" title="不修改颜色" aria-label="不修改颜色">不修改</button>
    </label>
  </div>
  <!-- 颜色面板（2026-10-21）：常用色 + 色板 + #HEX/rgb() 输入 -->
  <div class="cf-color-panel" id="cfColorPanel"></div>
  <div class="indent-preview" id="cfPreview"><p>预览：示例文字 ABC 123，可查看字号、字体与颜色的实际效果。</p></div>
  <div class="export-actions">
    <button type="button" id="cfClearBtn" title="清除所选范围内的逐字符字号/字体/颜色">清除字符格式</button>
    <button type="button" id="cfCancelBtn" title="取消，不做任何修改">取消</button>
    <button type="button" id="cfOkBtn" class="primary" title="把字符格式应用到所选范围（选中内容/当前页/全部）">确定</button>
  </div>
</div></div>
<div id="searchModalBg"><div class="modal search-modal">
  <div class="search-head">
    <h3>搜索 / 替换</h3>
    <div class="scope-seg" role="radiogroup" aria-label="搜索范围" title="搜索/替换/高亮的生效范围">
      <button type="button" class="scope-btn active" data-scope="all">全部页</button>
      <button type="button" class="scope-btn" data-scope="page">当前页</button>
    </div>
    <button type="button" id="searchCloseBtn" class="x-btn" title="关闭搜索" aria-label="关闭搜索">✕</button>
  </div>
  <div class="search-row-group">
    <div class="srow-label">搜索</div>
    <div class="search-row">
      <input type="text" id="searchInput" placeholder="搜索词（可正则）">
      <label class="search-regex" title="勾选后按正则表达式搜索，否则按普通文本"><input type="checkbox" id="searchRegex">正则</label>
      <button type="button" id="searchBtn" class="primary">搜索</button>
      <button type="button" id="searchClearBtn" class="primary" title="清除全部文字标记与搜索结果" aria-label="清除搜索">清理</button>
    </div>
  </div>
  <div class="search-row-group">
    <div class="srow-label">替换</div>
    <div class="search-row">
      <input type="text" id="replaceInput" placeholder="替换为（$1 引用捕获组）">
      <button type="button" id="replaceBtn" title="替换当前选中的匹配（支持正则）">替换当前</button>
      <button type="button" id="replaceAllBtn" title="把当前搜索词在范围内全部替换为「替换为」的内容（支持正则）">全部替换</button>
    </div>
  </div>
  <div class="kg-row" id="keepGroupsRow" title="正则带捕获组时启用「部分替换」：勾选的组原样保留，只替换其余部分；组0=整段匹配全部保留">
    <span class="kg-hint">保留组</span>
    <div class="kg-chips" id="keepGroupsChips"></div>
  </div>
  <div class="search-row-group">
    <div class="srow-label">按格式</div>
    <div class="search-row">
      <select id="fmtFindSel" title="要查找的格式（下划线/突显/粗体/颜色/字号…）" aria-label="查找格式"></select>
      <button type="button" id="fmtFindBtn" class="primary" title="按格式查找：列出范围内所有带该格式的文字">查找格式</button>
    </div>
    <div class="search-row">
      <select id="fmtReplaceSel" title="要替换成的目标格式" aria-label="替换格式"></select>
      <span id="fmtValueWrap" class="fmt-value-wrap" style="display:none;">
        <input type="text" id="fmtValueInput" class="fmt-value-input" placeholder="#ff0000 或 red" aria-label="目标格式取值">
        <select id="fmtFontSel" class="fmt-value-input" style="display:none;" aria-label="目标字体"></select>
      </span>
      <button type="button" id="fmtReplaceBtn" title="把范围内所有「查找格式」命中的文字换成「替换格式」（颜色/字号/字体需填取值）">替换格式</button>
    </div>
  </div>
  <div class="search-nav">
    <button type="button" id="searchPrevBtn" title="上一个匹配" aria-label="上一个匹配">↑</button>
    <span id="searchPos"></span>
    <button type="button" id="searchNextBtn" title="下一个匹配" aria-label="下一个匹配">↓</button>
  </div>
  <div class="sr-head"><span id="srCount"></span><button type="button" id="replaceRulesOpenBtn" class="sr-rules-btn" title="把带保留组的替换保存为规则，一键复用">替换规则</button></div>
  <div id="searchList"></div>
</div></div>
<div id="exportModalBg"><div class="modal search-modal">
  <div class="search-head"><h3>导出</h3><button type="button" id="exportCloseBtn" class="x-btn" title="关闭导出" aria-label="关闭导出">✕</button></div>
  <p class="export-desc">导出功能已移至工具栏「完成并转换」旁的下拉菜单，请直接在该菜单中选择导出格式。</p>
  <div class="export-actions"></div>
</div></div>
<div id="finishConfirmBg"><div class="modal" style="min-width:320px;">
  <h3 id="finishConfirmTitle">确认转换</h3>
  <p id="finishConfirmMsg" style="font-size:13px;color:#33414f;margin:10px 0;">将保存并转换为 EPUB，确认继续？</p>
  <div id="finishConfirmActions" style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">
    <button type="button" id="finishConfirmCancel">取消</button>
    <button type="button" id="finishConfirmOk" class="primary" style="background:#1a7f37;border-color:#1a7f37;color:#fff;">继续转换</button>
  </div>
</div></div>
<div id="indentModalBg"><div class="modal search-modal">
  <div class="search-head"><h3>段落设置</h3><button type="button" id="indCloseBtn" class="x-btn" title="关闭段落设置" aria-label="关闭段落设置">✕</button></div>
  <p style="font-size:12px;color:#5a6b7c;margin:4px 0;">缩进单位为字符，间距单位为行；设置随内容保存并在导出 EPUB 时生效。</p>
  <div class="indent-mode-row" role="radiogroup" aria-label="段落设置应用范围">
    <label title="仅作用于当前选中/光标所在的段落（可跨多段）"><input type="radio" name="indMode" id="indModeSel" checked> 选中段落</label>
    <label title="作用于当前页的全部正文段落（p/div/h1-h6）"><input type="radio" name="indMode" id="indModePage"> 当前页全部正文段落</label>
    <label title="作用于全书全部正文段落（整书一次撤销）"><input type="radio" name="indMode" id="indModeAll"> 全部正文</label>
  </div>
  <div class="indent-grid">
    <label>左缩进 <input type="number" id="indLeft" step="0.5" min="0" max="16"> 字符</label>
    <label>右缩进 <input type="number" id="indRight" step="0.5" min="0" max="16"> 字符</label>
    <label>特殊格式
      <select id="indSpecial">
        <option value="">无</option>
        <option value="first">首行缩进</option>
        <option value="hang">悬挂缩进</option>
      </select>
    </label>
    <label>缩进值 <input type="number" id="indVal" step="0.5" min="0" max="16"> 字符</label>
    <label>段前 <input type="number" id="indBefore" step="0.5" min="0" max="8"> 行</label>
    <label>段后 <input type="number" id="indAfter" step="0.5" min="0" max="8"> 行</label>
    <label>行距
      <select id="indLh">
        <option value="">默认</option>
        <option value="1">单倍</option>
        <option value="1.5">1.5 倍</option>
        <option value="2">双倍</option>
      </select>
    </label>
  </div>
  <div class="indent-preview" id="indPreview"><p>预览：段落文本示例，用于查看缩进与间距效果。The quick brown fox 123.</p></div>
  <div class="export-actions">
    <button type="button" id="indClearBtn" title="清除所选范围内的全部缩进与间距设置">清除格式</button>
    <button type="button" id="indOkBtn" class="primary" title="把设置应用到所选范围（选中段落/当前页/全部）">确定</button>
  </div>
</div></div>
<div id="tableModalBg"><div class="modal search-modal">
  <div class="search-head">
    <h3>插入表格</h3>
    <button type="button" class="x-btn" id="tableCloseBtn" title="关闭 (Esc)" aria-label="关闭">✕</button>
  </div>
  <p class="export-desc">在光标处插入空表格。行数 1-200、列数 1-50；勾选「首行表头」则首行改为粗体表头行。</p>
  <div class="table-grid">
    <label>行数 <input type="number" id="tableRows" value="4" min="1" max="200" step="1"></label>
    <label>列数 <input type="number" id="tableCols" value="5" min="1" max="50" step="1"></label>
    <label><input type="checkbox" id="tableHeader" checked> 首行表头</label>
  </div>
  <div class="export-actions">
    <button type="button" id="tableCancelBtn" title="取消插入">取消</button>
    <button type="button" id="tableOkBtn" class="primary" title="在光标处插入表格">插入</button>
  </div>
</div></div>
<div id="modalBg"><div class="modal">
  <h3>设置</h3>
  <div class="settings-tabs">
    <button type="button" class="settings-tab active" data-tab="shortcuts">快捷键</button>
    <button type="button" class="settings-tab" data-tab="fonts">字体</button>
    <button type="button" class="settings-tab" data-tab="ui">界面</button>
    <button type="button" class="settings-tab" data-tab="popup">弹出菜单</button>
    <button type="button" class="settings-tab" data-tab="correction">校正格式</button>
  </div>
  <div class="settings-panels">
    <div class="settings-panel" id="panel-shortcuts">
      <p style="font-size:12px;color:#5a6b7c;margin:8px 0;">每个操作绑定一个组合键；点击某行后按下新组合键（或鼠标侧键 X1/X2）完成绑定，Del/Backspace 清除，Esc 取消。绑定保存在本浏览器（localStorage）并同步到配置文件。</p>
      <table id="shortcutTable"></table>
      <div style="margin-top:8px;"><button type="button" id="resetShortcutsBtn">恢复默认</button></div>
      <h4 style="margin:18px 0 4px;">鼠标动作</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">按住鼠标中键滑动触发对应操作（如中键上滑 = 居中）。点击某行后按住中键滑动完成绑定，Del/Backspace 清除，Esc 取消。绑定保存在配置文件。</p>
      <table id="mouseShortcutTable"></table>
      <div style="margin-top:8px;"><button type="button" id="resetMouseShortcutsBtn">恢复默认</button></div>
      <h4 style="margin:18px 0 4px;">鼠标侧键</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">鼠标侧键（X1 后退 / X2 前进）可以<b>单独</b>绑定到某个操作，<b>不需要</b>和键盘按键组合使用；也可以在上方「快捷键」表里点击某行后直接按侧键完成绑定。</p>
      <table id="sideButtonTable"></table>
      <p style="font-size:12px;color:#5a6b7c;margin:10px 0 0;">提示：选中文字后弹出的快捷菜单只响应鼠标左键与右键；中键专用于上/下/左/右滑动手势，落在菜单上不会误触发菜单操作。</p>
    </div>
    <div class="settings-panel" id="panel-fonts" style="display:none;">
      <p style="font-size:12px;color:#5a6b7c;margin:8px 0;">设置各类文本的字体（CSS font-family）。默认「不修改」= 使用阅读器默认字体，不额外指定。</p>
      <div style="display:grid;grid-template-columns:120px 1fr;gap:8px 12px;align-items:center;margin-top:8px;">
        <label>正文字体</label>
        <select id="fontBody" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font:inherit;">
          <option value="">默认（不修改，使用阅读器默认）</option>
          <option value="宋体, SimSun, serif">宋体</option>
          <option value="黑体, SimHei, sans-serif">黑体</option>
          <option value="楷体, KaiTi, serif">楷体</option>
          <option value="仿宋, FangSong, serif">仿宋</option>
          <option value="微软雅黑, Microsoft YaHei, sans-serif">微软雅黑</option>
          <option value="等线, DengXian, sans-serif">等线</option>
          <option value="serif">serif（衬线）</option>
          <option value="sans-serif">sans-serif（无衬线）</option>
          <option value="monospace">monospace（等宽）</option>
          <option value="cursive">cursive（手写）</option>
        </select>
        <label>标题字体</label>
        <select id="fontHeading" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font:inherit;">
          <option value="">默认（不修改，使用阅读器默认）</option>
          <option value="宋体, SimSun, serif">宋体</option>
          <option value="黑体, SimHei, sans-serif">黑体</option>
          <option value="楷体, KaiTi, serif">楷体</option>
          <option value="仿宋, FangSong, serif">仿宋</option>
          <option value="微软雅黑, Microsoft YaHei, sans-serif">微软雅黑</option>
          <option value="等线, DengXian, sans-serif">等线</option>
          <option value="serif">serif（衬线）</option>
          <option value="sans-serif">sans-serif（无衬线）</option>
          <option value="monospace">monospace（等宽）</option>
          <option value="cursive">cursive（手写）</option>
        </select>
        <label>注释字体</label>
        <select id="fontNote" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font:inherit;">
          <option value="">默认（不修改，使用阅读器默认）</option>
          <option value="宋体, SimSun, serif">宋体</option>
          <option value="黑体, SimHei, sans-serif">黑体</option>
          <option value="楷体, KaiTi, serif">楷体</option>
          <option value="仿宋, FangSong, serif">仿宋</option>
          <option value="微软雅黑, Microsoft YaHei, sans-serif">微软雅黑</option>
          <option value="等线, DengXian, sans-serif">等线</option>
          <option value="serif">serif（衬线）</option>
          <option value="sans-serif">sans-serif（无衬线）</option>
          <option value="monospace">monospace（等宽）</option>
          <option value="cursive">cursive（手写）</option>
        </select>
        <label>引用字体</label>
        <select id="fontCitation" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font:inherit;">
          <option value="">默认（不修改，使用阅读器默认）</option>
          <option value="宋体, SimSun, serif">宋体</option>
          <option value="黑体, SimHei, sans-serif">黑体</option>
          <option value="楷体, KaiTi, serif">楷体</option>
          <option value="仿宋, FangSong, serif">仿宋</option>
          <option value="微软雅黑, Microsoft YaHei, sans-serif">微软雅黑</option>
          <option value="等线, DengXian, sans-serif">等线</option>
          <option value="serif">serif（衬线）</option>
          <option value="sans-serif">sans-serif（无衬线）</option>
          <option value="monospace">monospace（等宽）</option>
          <option value="cursive">cursive（手写）</option>
        </select>
      </div>
      <div style="margin-top:12px;padding-top:8px;border-top:1px solid var(--border);">
        <label style="display:flex;align-items:center;gap:8px;font-size:13px;">
          <input type="checkbox" id="citationItalicEnabled" style="width:16px;height:16px;">
          启用引用斜体（citation 格式自动应用 italic）
        </label>
      </div>
      <div style="margin-top:12px;"><button type="button" id="resetFontsBtn">恢复默认</button></div>
    </div>
    <div class="settings-panel" id="panel-ui" style="display:none;">
      <h4 style="margin:8px 0 4px;">提示延迟</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">鼠标悬停按钮超过设定时间（毫秒）才显示提示文字，提示中会附带对应快捷键；0 = 立即显示。</p>
      <label style="font-size:13px;">提示延迟（毫秒） <input type="number" id="tipDelayInput" min="0" max="5000" step="100" style="width:90px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;"></label>
      <h4 style="margin:16px 0 4px;">纠错悬浮窗</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">鼠标悬停校正文本超过设定时间（毫秒）自动弹出采纳/忽略菜单；0 = 悬停即弹出。</p>
      <label style="font-size:13px;">悬停弹出延迟（毫秒） <input type="number" id="errHoverDelayInput" min="0" max="10000" step="100" style="width:90px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;"></label>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-top:8px;">
        <input type="checkbox" id="prAutoJump" style="width:16px;height:16px;">
        采纳/忽略后自动跳到下一个错误（默认开启）
      </label>
      <p style="font-size:12px;color:#5a6b7c;margin:4px 0 0;">开启后，点击「采纳」或「忽略」会自动滚动定位到本页下一个错误并弹出悬浮菜单（不会跳到其他页）；关闭后保持原位，需自行找到下一个错误。</p>
      <h4 style="margin:16px 0 4px;">增强格式刷</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">武装「合并段落」刷后，拖选多段并停稳超过该时间（毫秒）才自动合并，避免拖选途中瞬时覆盖多段就误合并；0 = 停稳即合并（最快）。正文 / 注释 / 引用 / 加粗 / 斜体单个武装时，先用鼠标选中词语或句子再点击段落，即只对选中文字生效；未选中或混合多个功能时作用于整段。</p>
      <label style="font-size:13px;">合并延迟（毫秒） <input type="number" id="brushMergeDelayInput" min="0" max="5000" step="100" style="width:90px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;"></label>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-top:8px;">
        <input type="checkbox" id="brushMergeOnLeaveInput" style="width:16px;height:16px;">
        选中后失去焦点/移开即合并（默认开启）
      </label>
      <h4 style="margin:16px 0 4px;">编辑器字号</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">调整编辑区显示字号（视图偏好，不写入保存内容）。</p>
      <label style="font-size:13px;">字号（px） <input type="number" id="editorFontSizeInput" min="10" max="28" step="1" style="width:70px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;"></label>
      <h4 style="margin:16px 0 4px;">纠错批处理</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">「校正 / 重识别」一次处理的页数：0 = 全部页（整本书，默认；校正后整本书的错误标注全部显示）；1-50 = 从当前页起向后处理 N 页。应用 / 清除默认仍只作用于当前页。重识别每页需模型推理，整本书耗时长，建议先设小批数。</p>
      <label style="font-size:13px;">每批页数（0=全部） <input type="number" id="prBatchPages" min="0" max="50" step="1" style="width:70px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;"></label>
      <h4 style="margin:16px 0 4px;">撤销 / 回退</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">撤销（Ctrl+Z）/ 前进（Ctrl+Y）保留的历史步数；步数越多占用内存越大。</p>
      <label style="font-size:13px;">历史步数
        <select id="undoDepthInput" style="padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;">
          <option value="50">50 步</option>
          <option value="100">100 步</option>
          <option value="200">200 步</option>
          <option value="500">500 步</option>
        </select>
      </label>
      <h4 style="margin:16px 0 4px;">搜索结果</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">搜索/替换弹窗结果列表最多显示的条数（10-1000，默认 200）。匹配总数始终完整统计并显示，超出上限时提示「仅显示前 N 条」；列表非虚拟渲染，设得越大滚动越可能卡顿。EPUB 编辑器的章内搜索读取同一设置。</p>
      <label style="font-size:13px;">显示上限（条） <input type="number" id="searchMaxResultsInput" min="10" max="1000000" step="10" style="width:70px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;"></label>
      <h4 style="margin:16px 0 4px;">文字页面行号</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">在虚拟列表各页的文字左侧显示行号（每页从 1 重新计起，与原书页码无关）。行号供命令行按行号寻址使用（Ctrl+; 打开命令行，例如 7,10merge 合并第 7-10 行）。纯视图层，不写入保存内容，也不影响导出。</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;">
        <input type="checkbox" id="lineNumShow" style="width:16px;height:16px;">
        显示行号
      </label>
      <label style="font-size:13px;display:flex;align-items:center;gap:8px;margin-top:8px;">行号栏尺寸
        <select id="lineNumSizeSel" style="padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;">
          <option value="small">小</option>
          <option value="medium">中</option>
          <option value="large">大</option>
        </select>
      </label>
      <h4 style="margin:16px 0 4px;">纠错序号</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">每页待处理纠错按文档顺序从 1 编号。序号的用途是<b>按序号操作纠错</b>：普通模式下 <b>N Enter</b> 应用第 N 处、<b>N Delete</b> 取消（忽略）第 N 处，命令行也可用 :err N / :errd N。角标只是可选的可视提示（CSS 伪元素，绝对定位），不写入正文/保存内容；关闭后只是不显示角标，按序号操作的编号口径完全不变。</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;">
        <input type="checkbox" id="errNumShow" style="width:16px;height:16px;">
        显示纠错序号角标
      </label>
      <p style="font-size:11px;color:#8a9bab;margin:4px 0 0 24px;">序号是<b>稳定编号</b>：采纳/忽略某一处后，其余各处的序号不会前移（被处理掉的序号消失）。</p>
      <h4 style="margin:16px 0 4px;">输入法</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">默认开启：按 <b>Esc</b> / <b>Ctrl+[</b> 进入普通（NORMAL）模式时，如果输入法正在<b>中文模式</b>，自动切到<b>英文模式</b>，避免接着敲 j/k/h/l 打出拼音候选。只切输入法内部的中/英状态（搜狗、百度、微软拼音等通用），<b>不会</b>把输入法换成美式键盘或切换输入语言；输入法已经是英文时不做任何操作。由本地服务端调用 Windows 输入法接口实现（浏览器无法自行切换），非 Windows 或调用失败时静默跳过，不影响按键。取消勾选可关闭。</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;">
        <input type="checkbox" id="imeSwitchChk" style="width:16px;height:16px;">
        Esc 后自动切英文输入法
      </label>
      <h4 style="margin:16px 0 4px;">正文对齐</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">矫正页正文默认两端对齐（分散对齐），段落左右边缘整齐更美观。关闭后按浏览器默认（左对齐）。纯视图层，不影响保存与导出；手动设置的逐块对齐仍然生效。</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;">
        <input type="checkbox" id="textJustifyToggle" style="width:16px;height:16px;">
        正文两端对齐
      </label>
      <h4 style="margin:16px 0 4px;">表格内浮层菜单</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">控制鼠标操作落在表格内时是否弹出对应浮层，三项独立控制，只影响表格范围（表格外不受影响）。默认全部关闭：表格内选中文字 / 右击不再弹出菜单，仅保留表格操作条；右击三项全关时回退浏览器默认菜单（复制 / 粘贴）。</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;">
        <input type="checkbox" id="tblPopupMenuChk" style="width:16px;height:16px;">
        表格内显示选中文字菜单（选中文字时弹出的快捷操作菜单）
      </label>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-top:8px;">
        <input type="checkbox" id="tblCtxMenuChk" style="width:16px;height:16px;">
        表格内显示右键菜单（右击弹出的页级操作菜单）
      </label>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-top:8px;">
        <input type="checkbox" id="tblTblMenuChk" style="width:16px;height:16px;">
        表格内右击显示表格菜单（加行 / 加列 / 合并等；操作条「更多 ▾」不受影响）
      </label>
      <h4 style="margin:16px 0 4px;">智能清理规则</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">「智能清理」按钮各步骤的启用开关。仅作用于清理结果，不影响未清理内容；关闭某步即清理时跳过该步。</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-top:8px;">
        <input type="checkbox" id="cleanPunctuationChk" style="width:16px;height:16px;">
        中英文标点归一（逗号 / 句号 / 括号等半角 → 全角；纯英文页保留半角）
      </label>
      <p style="font-size:11px;color:#8a9bab;margin:2px 0 0 24px;">示例：你好,世界 → 你好，世界；测试() → 测试（）</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-top:8px;">
        <input type="checkbox" id="cleanSymbolsChk" style="width:16px;height:16px;">
        扩展符号归一（引号 / 方括号 / 破折号 / 波浪号 + 省略号长度统一）
      </label>
      <p style="font-size:11px;color:#8a9bab;margin:2px 0 0 24px;">示例：看[注] → 看【注】，a--b → a——b</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-top:8px;">
        <input type="checkbox" id="cleanMdTagsChk" style="width:16px;height:16px;">
        清理 Markdown 残留标记（**粗体** / ~~删除~~ / `代码`）
      </label>
      <p style="font-size:11px;color:#8a9bab;margin:2px 0 0 24px;">示例：**重点** → 重点，~~删除~~ → 删除</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-top:8px;">
        <input type="checkbox" id="cleanHtmlTagsChk" style="width:16px;height:16px;">
        清理非富文本 HTML 标签（保留 strong / em / span / br / img 等）
      </label>
      <p style="font-size:11px;color:#8a9bab;margin:2px 0 0 24px;">示例：&lt;font color="red"&gt;红&lt;/font&gt; → 红</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-top:8px;">
        <input type="checkbox" id="cleanParagraphsChk" style="width:16px;height:16px;">
        段落合并优化（OCR 误拆段落自动合并，并自动清理合并后的段内空白，默认关闭）
      </label>
      <p style="font-size:11px;color:#8a9bab;margin:2px 0 0 24px;">示例：&lt;p&gt;未完&lt;/p&gt;&lt;p&gt;&amp;nbsp;待续&lt;/p&gt; → &lt;p&gt;未完待续&lt;/p&gt;</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-top:8px;">
        <input type="checkbox" id="cleanDigitPunctChk" style="width:16px;height:16px;">
        数字/字母标点归一（数字、字母间的中文标点 → 英文，含空格容忍；并清理数字间/单字母间空格）
      </label>
      <p style="font-size:11px;color:#8a9bab;margin:2px 0 0 24px;">示例：1。2 → 1.2，1，2 → 1,2，1。 → 1.；映射表可在下方自定义</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-top:8px;">
        <input type="checkbox" id="cleanSpacesChk" style="width:16px;height:16px;">
        清理段落内空白（删除空格 / 制表符 / 全角空格，合并 OCR 软换行；逐行对齐段保留换行，默认关闭）
      </label>
      <p style="font-size:11px;color:#8a9bab;margin:2px 0 0 24px;">示例：你好 世界 → 你好世界（行首行尾、中文之间的空格都会被删；OCR 软换行会合并，逐行对齐段保留换行）</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-top:8px;">
        <input type="checkbox" id="cleanSpacesGapChk" style="width:16px;height:16px;">
        保留英文单词之间的空格（清理段落内空白 / 合并段落时生效）
      </label>
      <p style="font-size:11px;color:#8a9bab;margin:2px 0 0 24px;">示例：hello world → hello world（不勾选则变成 helloworld）</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-top:8px;">
        <input type="checkbox" id="cleanBlankLinesChk" style="width:16px;height:16px;">
        导出时清理空白行（删掉空段落、连续空行合并为一个，默认关闭）
      </label>
      <p style="font-size:11px;color:#8a9bab;margin:2px 0 0 24px;">仅作用于导出（txt / md / docx / epub），不影响编辑区内容与「智能清理」结果。</p>
      <div style="margin-top:12px;padding:8px;background:#f8f9fa;border-radius:4px;">
        <p style="font-size:12px;color:#5a6b7c;margin:0 0 4px;">实时预览（输入文本查看清理效果）</p>
        <textarea id="cleanPreviewInput" rows="2" style="width:100%;box-sizing:border-box;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;resize:vertical;" placeholder="输入文本查看清理效果..."></textarea>
        <div id="cleanPreviewOut" style="margin-top:4px;padding:4px 6px;background:#fff;border:1px solid var(--border);border-radius:4px;font-size:13px;min-height:20px;"></div>
      </div>
      <div style="margin-top:12px;padding:8px;background:#f8f9fa;border-radius:4px;">
        <p style="font-size:12px;color:#5a6b7c;margin:0 0 4px;">符号映射表（每行一条：半角 → 全角，如 <code>[ → 【</code>）</p>
        <textarea id="cleanSymbolMapEditor" rows="5" style="width:100%;box-sizing:border-box;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;resize:vertical;" placeholder="[ → 【&#10;] → 】&#10;-- → ——&#10;~ → ～"></textarea>
        <p style="font-size:11px;color:#8a9bab;margin:4px 0 0;">自定义符号归一映射。留空则恢复默认。修改后自动保存并更新预览。</p>
      </div>
      <div style="margin-top:12px;padding:8px;background:#f8f9fa;border-radius:4px;">
        <p style="font-size:12px;color:#5a6b7c;margin:0 0 4px;">数字标点映射表（每行一条：中文标点 → 英文，如 <code>。 → .</code>）</p>
        <textarea id="cleanDigitPunctMapEditor" rows="5" style="width:100%;box-sizing:border-box;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;resize:vertical;" placeholder="。 → .&#10;， → ,&#10;、 → ,&#10;： → :&#10;； → ;"></textarea>
        <p style="font-size:11px;color:#8a9bab;margin:4px 0 0;">只替换「紧跟数字」的中文标点（1。2 → 1.2、1，2 → 1,2、1。 → 1.），且仅单字符源生效。留空则恢复默认。修改后自动保存并更新预览。</p>
      </div>
      <h4 style="margin:16px 0 4px;">表格</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">自动把识别/导入文本里的文字表格（Markdown 管道表、画框表）转成原生表格。关闭后仍可用工具栏「转成表格」或选中文字后的悬浮菜单手动转换。与主设置的「识别表格转换为表格」是同一个开关（config 顶层 table_convert）。</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;">
        <input type="checkbox" id="tableConvertChk" style="width:16px;height:16px;">
        自动把文字表格转换为表格
      </label>
      <h4 style="margin:16px 0 4px;">格式规则</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">把规则应用到全部页前先弹出确认，防止误操作大范围改动。</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;">
        <input type="checkbox" id="ruleAllPagesConfirm" style="width:16px;height:16px;">
        格式规则应用到全部页前需确认
      </label>
      <h4 style="margin:16px 0 4px;">自动备份</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">编辑过程中按间隔自动备份到本地历史，防止意外丢失。备份不改变「已保存」状态，也进入历史版本管理。</p>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-bottom:8px;">
        <input type="checkbox" id="autoSaveEnabled" style="width:16px;height:16px;">
        启用自动备份
      </label>
      <label style="font-size:13px;display:flex;align-items:center;gap:8px;">间隔（分钟）
        <input type="number" id="autoSaveInterval" min="1" max="60" step="1" style="width:70px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;">
      </label>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-top:8px;">
        <input type="checkbox" id="autoSaveNewBackup" style="width:16px;height:16px;">
        每次备份生成新历史版本（不勾选则更新最近一版）
      </label>
      <div style="margin-top:12px;"><button type="button" id="resetUiSettingsBtn">恢复默认</button></div>
    </div>
    <div class="settings-panel" id="panel-popup" style="display:none;">
      <p style="font-size:12px;color:#5a6b7c;margin:8px 0;">选中文字时弹出的快捷菜单分两行按钮，第三行是格式规则快捷按钮。每行可自由调整顺序、移出或加回按钮；改动即时生效并保存到配置文件。</p>
      <h4 style="margin:14px 0 4px;">第一行按钮</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 2px;">▲/▼ 调整顺序，✕ 移出菜单；移出的按钮回到下方下拉框中，可随时加回。</p>
      <div id="popupRow1List" class="pop-edit-list"></div>
      <div class="pop-edit-add">
        <select id="popupRow1AddSel" title="从备选操作中添加按钮到第一行"></select>
        <button type="button" id="popupRow1AddBtn">添加</button>
      </div>
      <h4 style="margin:18px 0 4px;">第二行按钮</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 2px;">同上：调整顺序或移出菜单。</p>
      <div id="popupRow2List" class="pop-edit-list"></div>
      <div class="pop-edit-add">
        <select id="popupRow2AddSel" title="从备选操作中添加按钮到第二行"></select>
        <button type="button" id="popupRow2AddBtn">添加</button>
      </div>
      <h4 style="margin:18px 0 4px;">规则快捷按钮</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">第三行显示格式规则菜单前 N 项（设置为 0 则不显示规则按钮）。</p>
      <label style="font-size:13px;">规则按钮数量 <input type="number" id="popupRuleCountInput" min="0" max="10" step="1" style="width:70px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;"></label>
      <div style="margin-top:12px;"><button type="button" id="resetPopupSettingsBtn">恢复默认</button></div>
    </div>
    <div class="settings-panel" id="panel-correction" style="display:none;">
      <p style="font-size:12px;color:#5a6b7c;margin:8px 0;">大模型「重识别」返回的文本在与原文逐字对比前，先剥离与内容无关的格式字符（Markdown 标记、LaTeX 公式与命令），避免 **、$、\frac 之类被当成纠错项。默认关闭；开启后仅作用于重识别结果，不影响普通 OCR 流程。</p>
      <label style="display:block;font-size:13px;margin:10px 0 6px;"><input type="checkbox" id="cfEnabled"> 启用校正格式清理（重识别结果对比前）</label>
      <div style="display:grid;grid-template-columns:1fr;gap:6px;margin:6px 0 6px 22px;">
        <label style="font-size:13px;"><input type="checkbox" id="cfStripMarkdown"> 去 Markdown 标记（**粗体**、# 标题、- 列表、`代码` 等，保留文字）</label>
        <label style="font-size:13px;"><input type="checkbox" id="cfStripLatex"> 去 LaTeX 命令/环境标记（$…$、\frac{a}{b}、\textcircled{n} 等，保留公式内容）</label>
        <label style="font-size:13px;"><input type="checkbox" id="cfRemoveFormula"> 整段移除 LaTeX 公式（连同公式内容一并删除，比上一项更激进）</label>
      </div>
      <h4 style="margin:16px 0 4px;">自定义清理规则</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">在上面的默认清理之后再按顺序执行（正则替换，作用于重识别返回文本）。用于补足默认规则未覆盖的格式残留。</p>
      <div id="cfRulesList"></div>
      <div class="rr-edit-grid" style="margin-top:8px;">
        <label>规则名称 <input type="text" id="cfRuleName" placeholder="如：去行首圈码"></label>
        <label>正则表达式 <input type="text" id="cfRulePattern" placeholder="如：^[①-⑳]\s*"></label>
        <label>标志 <input type="text" id="cfRuleFlags" placeholder="i m s u（忽略留空）" title="可选：i 忽略大小写 / m 多行 / s 点号跨行 / u Unicode"></label>
        <label>替换为 <input type="text" id="cfRuleReplacement" placeholder="留空 = 删除匹配内容"></label>
      </div>
      <div style="margin-top:8px;display:flex;gap:8px;align-items:center;">
        <button type="button" id="cfRuleSaveBtn" class="primary">添加规则</button>
        <button type="button" id="cfRuleCancelBtn" style="display:none;">取消编辑</button>
      </div>
      <h4 style="margin:16px 0 4px;">预览</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">粘贴一段模型返回的文本，实时查看按当前设置清理后的结果（仅预览，不保存、不落盘）。</p>
      <textarea id="cfPreviewInput" rows="3" style="width:100%;box-sizing:border-box;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font:inherit;resize:vertical;" placeholder="**加粗** 与 $\frac{1}{2}$ 与 \textcircled{1}"></textarea>
      <textarea id="cfPreviewOut" rows="3" readonly style="width:100%;box-sizing:border-box;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font:inherit;margin-top:6px;background:#f7f9fc;resize:vertical;"></textarea>
    </div>
  </div>
  <button type="button" id="closeSettings" class="primary" style="margin-top:12px;">关闭</button>
</div></div>
<div id="finishModalBg"><div class="modal">
  <h3 id="finishTitle">转换完成</h3>
  <p id="finishMsg" style="font-size:14px;color:#33414f;">是否关闭当前页面？</p>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px;">
    <button type="button" id="closePageBtn">关闭页面</button>
    <button type="button" id="stayPageBtn" class="primary">留在本页</button>
  </div>
</div></div>
<div id="formatRulesModalBg"><div class="modal" style="max-width:780px;">
  <div class="search-head"><h3>格式规则</h3><button type="button" id="formatRulesCloseBtn" class="x-btn" title="关闭" aria-label="关闭">✕</button></div>
  <p style="font-size:12px;color:#5a6b7c;margin-top:0;">对选中的文字一键应用自定义格式。每条规则含一个有序条件列表（像列表一样按顺序判断）：每个条件可单独设置格式（含「无」= 不处理文本）；求值模式「第一个匹配即停」= 首个匹配条件生效即停，「所有匹配都应用」= 全部匹配条件的格式按序叠加（冲突格式自动跳过）。选中文字为空时按光标所在段落处理块级格式。规则按列表顺序执行。</p>
  <table id="formatRulesTable">
    <thead><tr style="text-align:left;color:#33414f;">
      <th style="padding:6px 8px;border-bottom:1px solid var(--border);">顺序</th>
      <th style="padding:6px 8px;border-bottom:1px solid var(--border);">名称</th>
      <th style="padding:6px 8px;border-bottom:1px solid var(--border);">条件（含格式）</th>
      <th style="padding:6px 8px;border-bottom:1px solid var(--border);">操作</th>
    </tr></thead>
    <tbody id="formatRulesBody"></tbody>
  </table>
  <div style="margin-top:10px;display:flex;gap:8px;align-items:center;">
    <button type="button" id="formatRuleNewBtn" class="primary">新建规则</button>
    <button type="button" id="formatRulesApplyAllBtn" title="按列表顺序执行全部规则；冲突格式（对齐/块标签互斥、remove 与其他）自动跳过">应用全部规则</button>
  </div>
</div></div>
<div id="replaceRulesModalBg"><div class="modal" style="max-width:680px;">
  <div class="search-head"><h3>替换规则</h3><button type="button" id="replaceRulesCloseBtn" class="x-btn" title="关闭" aria-label="关闭">✕</button></div>
  <p style="font-size:12px;color:#5a6b7c;margin-top:0;">把带保留组的正则替换保存为规则，一键应用到当前页或全部页面。规则与「替换当前/全部替换」使用相同的保留组语义：勾选保留的捕获组原样保留，只替换其余部分。</p>
  <div id="rrList"></div>
  <div style="margin-top:10px;display:flex;gap:8px;align-items:center;">
    <button type="button" id="rrNewBtn" class="primary">新建规则</button>
  </div>
</div></div>
<div id="rrEditModalBg"><div class="modal" style="max-width:640px;">
  <div class="search-head"><h3 id="rrEditTitle">新建替换规则</h3><button type="button" id="rrEditCloseBtn" class="x-btn" title="关闭" aria-label="关闭">✕</button></div>
  <div class="rr-edit-grid">
    <label>规则名称 <input type="text" id="rrName" placeholder="如：章标题去圈码"></label>
    <label>正则表达式 <input type="text" id="rrPattern" placeholder="/第([一二三四五六七八九十百千]+)章/ 或 第([一二三四五六七八九十百千]+)章"></label>
    <label>标志 <input type="text" id="rrFlags" placeholder="i m s u（忽略留空）" title="可选：i 忽略大小写 / m 多行 / s 点号匹配换行 / u Unicode"></label>
    <label>替换为 <input type="text" id="rrReplacement" placeholder="如：$1 章（$1 引用捕获组）"></label>
  </div>
  <div class="kg-row" id="rrKeepGroupsRow" title="捕获组原样保留，只替换其余部分；组0=整段匹配全部保留">
    <span class="kg-hint">保留组</span>
    <div class="kg-chips" id="rrKeepChips"></div>
  </div>
  <div style="margin-top:12px;display:flex;justify-content:flex-end;gap:8px;">
    <button type="button" id="rrEditCancelBtn">取消</button>
    <button type="button" id="rrEditSaveBtn" class="primary">保存规则</button>
  </div>
</div></div>
<div id="frRuleModalBg"><div class="modal" style="max-width:760px;">
  <div class="search-head"><h3>编辑规则</h3><button type="button" id="frRuleCloseBtn" class="x-btn" title="关闭" aria-label="关闭">✕</button></div>
  <div style="margin-bottom:8px;">
    <label style="font-size:13px;">规则名称 <input type="text" id="frName" placeholder="如：书名标题" style="width:240px;"></label>
  </div>
  <div style="margin-bottom:8px;">
    <label style="font-size:13px;">快捷位 <input type="checkbox" id="frPin"></label>
  </div>
  <div style="margin-bottom:8px;">
    <label style="font-size:13px;">简称 <input type="text" id="frLabel" maxlength="4" placeholder="如：标"></label>
  </div>
  <div style="margin-bottom:10px;font-size:13px;color:#33414f;">
    求值模式
    <select id="frMode">
      <option value="first">第一个匹配即停</option>
      <option value="all">所有匹配都应用</option>
    </select>
    <span style="color:#5a6b7c;font-size:12px;margin-left:6px;">第一个匹配即停：首个匹配条件生效即停；所有匹配都应用：全部匹配条件的格式按序叠加（冲突自动跳过）。</span>
  </div>
  <div style="margin-bottom:6px;font-size:13px;color:#33414f;">条件列表（按顺序判断，空条件内容 = 无条件恒匹配）：</div>
  <div id="frConditions"></div>
  <div style="margin-top:8px;">
    <button type="button" id="frAddCondBtn">添加条件</button>
  </div>
  <div style="margin-top:12px;display:flex;gap:8px;">
    <button type="button" id="frSaveBtn" class="primary">保存规则</button>
    <button type="button" id="frCancelBtn">取消</button>
  </div>
</div></div>
<div id="frFmtPopupBg"><div class="modal" style="max-width:420px;">
  <div class="search-head"><h3>应用格式</h3><button type="button" id="frFmtPopupCloseBtn" class="x-btn" title="关闭" aria-label="关闭">✕</button></div>
  <div id="frFmtOpts" class="fr-opts"></div>
  <div style="margin-top:12px;display:flex;gap:8px;">
    <button type="button" id="frFmtOkBtn" class="primary">确认</button>
    <button type="button" id="frFmtCancelBtn">取消</button>
  </div>
</div></div>
<div id="historyModalBg"><div class="modal" style="max-width:960px; min-width:360px;">
  <h3>历史记录</h3>
  <div style="display:flex;gap:8px;margin:8px 0;">
    <input type="text" id="historySearchInput" placeholder="按文件名/路径搜索…" style="flex:1;min-width:200px;padding:4px 8px;border:1px solid var(--border);border-radius:4px;font:inherit;font-size:12px;">
    <button type="button" id="historySearchClear" style="display:none;padding:4px 12px;border:1px solid var(--border);border-radius:4px;background:var(--bg-elev);color:var(--text);font:inherit;font-size:12px;cursor:pointer;">清除</button>
  </div>
  <p style="font-size:12px;color:#5a6b7c;">本地矫正缓存（同一文件保留多个版本，v1 为最新）。文件名与路径分列显示，同名不同路径的文件可区分；勾选后可删除或导出（支持多选）。</p>
  <div style="max-height:50vh;overflow:auto;border:1px solid var(--border);border-radius:4px;margin-top:6px;">
    <table id="historyTable" style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead><tr style="text-align:left;color:#33414f;">
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);width:34px;"><input type="checkbox" id="historyCheckAll" title="全选"></th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);width:20%;">文件名</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);width:32%;">文件路径</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);width:7%;">版本</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);width:16%;">更新时间</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);width:11%;">校正页码</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);width:8%;">操作</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;">
    <button type="button" id="historyImportBtn">导入</button>
    <button type="button" id="historyExportBtn">导出</button>
    <input type="file" id="historyImportFile" accept=".json,application/json,.zip,application/zip" style="display:none">
    <button type="button" id="historyDeleteBtn">删除</button>
    <button type="button" id="historyDeleteAllBtn" style="display:none">全部删除</button>
    <button type="button" id="historyCloseBtn" class="primary">关闭</button>
  </div>
</div></div>
<div id="helpModalBg"><div class="modal" style="max-width:800px;max-height:80vh;overflow:auto;">
  <div class="search-head"><h3>帮助</h3><button type="button" id="closeHelp" class="x-btn" title="关闭" aria-label="关闭">✕</button></div>
  <div id="helpContent" style="line-height:1.7;font-size:13px;"></div>
</div></div>
<div id="errPopup" style="display:none;position:fixed;z-index:65;flex-direction:row;align-items:center;gap:8px;padding:8px;background:#fff;border:1px solid #ccc;border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.2);">
  <button id="errOk" title="采纳（Enter）">采纳</button>
  <button id="errNo" title="忽略（Esc）">忽略</button>
</div>
<!-- 图片设置弹窗：点击编辑区内的图片弹出，可调整大小/位置/删除 -->
<div id="imgPopup" style="display:none;position:fixed;z-index:65;flex-direction:column;gap:6px;padding:10px;background:#fff;border:1px solid #ccc;border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.2);min-width:140px;">
  <div style="font-weight:600;font-size:13px;margin-bottom:2px;">图片设置</div>
  <div style="display:flex;flex-wrap:wrap;gap:4px;">
    <span style="width:100%;font-size:12px;color:#666;">布局</span>
    <button type="button" class="img-pop-btn" data-img-op="layout" data-img-val="full" title="全画幅：导出 EPUB 时独占一页，前后内容另起一页；大小设置不影响导出">全画幅</button>
    <button type="button" class="img-pop-btn" data-img-op="layout" data-img-val="fit" title="局部：与前后内容共占一页（不强制分页）；大小设置在导出中生效">局部</button>
    <button type="button" class="img-pop-btn" data-img-op="layout" data-img-val="inline" title="行内：嵌在文字中间（默认 50% 宽度）">行内</button>
  </div>
  <div style="display:flex;flex-wrap:wrap;gap:4px;">
    <span style="width:100%;font-size:12px;color:#666;">大小</span>
    <button type="button" class="img-pop-btn" data-img-op="size" data-img-val="original">原尺寸</button>
    <button type="button" class="img-pop-btn" data-img-op="size" data-img-val="w25">25%</button>
    <button type="button" class="img-pop-btn" data-img-op="size" data-img-val="w50">50%</button>
    <button type="button" class="img-pop-btn" data-img-op="size" data-img-val="w75">75%</button>
    <button type="button" class="img-pop-btn" data-img-op="size" data-img-val="w100">100%</button>
    <!-- 自定义宽度（2026-10）：整数百分比 1-100，写入 img 的行内 style="width:NN%"
         （与五个固定档位互斥，见 ui/app.js 的 size / size-custom 分支） -->
    <input type="number" id="imgCustomW" min="1" max="100" step="1" placeholder="自定义 1-100"
           aria-label="自定义图片宽度（1-100 百分比）" title="输入 1-100 的整数百分比宽度，按回车或点「应用」生效（与五个固定档位互斥）">
    <button type="button" class="img-pop-btn" data-img-op="size-custom" title="按输入值设置图片宽度（1-100 整数百分比）">应用</button>
  </div>
  <div id="imgPosRow" style="display:flex;flex-wrap:wrap;gap:4px;">
    <span style="width:100%;font-size:12px;color:#666;">位置</span>
    <button type="button" class="img-pop-btn" data-img-op="pos" data-img-val="left">左</button>
    <button type="button" class="img-pop-btn" data-img-op="pos" data-img-val="center">中</button>
    <button type="button" class="img-pop-btn" data-img-op="pos" data-img-val="right">右</button>
  </div>
  <div id="imgVPosRow" style="display:none;flex-wrap:wrap;gap:4px;">
    <span style="width:100%;font-size:12px;color:#666;">位置（行内）</span>
    <button type="button" class="img-pop-btn" data-img-op="pos" data-img-val="vtop">顶</button>
    <button type="button" class="img-pop-btn" data-img-op="pos" data-img-val="vmid">中</button>
    <button type="button" class="img-pop-btn" data-img-op="pos" data-img-val="vbot">底</button>
  </div>
  <div style="display:flex;gap:4px;margin-top:2px;">
    <button type="button" class="img-pop-btn" data-img-op="delete" style="flex:1;color:#c0392b;">删除</button>
  </div>
</div>
<!-- 表格操作条（2026-10 增强）：光标进入表格单元格时弹出。快捷项=常用操作；
     「更多 ▾」菜单与表格内右键菜单同源（插入/移动行列、垂直对齐、表头、均分列宽、重置尺寸）。 -->
<div id="tblBar" style="display:none;" aria-label="表格操作">
  <button type="button" data-tbl-op="addrow" title="下方插入一行">加行</button>
  <button type="button" data-tbl-op="addcol" title="右侧插入一列">加列</button>
  <button type="button" data-tbl-op="merge" title="合并选中的多个单元格（在表格内拖选，或按住 Shift 点选）">合并</button>
  <button type="button" data-tbl-op="split" title="拆分当前已合并的单元格">拆分</button>
  <button type="button" data-tbl-op="colwidth" title="设置当前列宽（百分比）">列宽</button>
  <button type="button" data-tbl-op="rowheight" title="设置当前行高">行高</button>
  <span class="tbl-bar-sep"></span>
  <button type="button" data-tbl-op="alignleft" data-tbl-align="left" title="单元格左对齐">左</button>
  <button type="button" data-tbl-op="aligncenter" data-tbl-align="center" title="单元格居中">中</button>
  <button type="button" data-tbl-op="alignright" data-tbl-align="right" title="单元格右对齐">右</button>
  <button type="button" data-tbl-op="aligngrid" title="单元格对齐（上下 × 左右，Word 式九宫格）">对齐 ▾</button>
  <span class="tbl-bar-sep"></span>
  <button type="button" data-tbl-op="more" title="更多表格操作（插入/移动行列、垂直对齐、表头、均分列宽…）">更多 ▾</button>
  <button type="button" data-tbl-op="deltable" class="danger" title="删除整个表格">删除表格</button>
</div>
<!-- 表格「更多」菜单：与表格内右键菜单同源（JS 打开并填充） -->
<div id="tblMenu" style="display:none;" role="menu" aria-label="表格菜单"></div>
<!-- 表格对齐九宫格（2026-10）：Word 式「上下 × 左右」单元格对齐（JS 构建 9 个按钮） -->
<div id="tblAlignGrid" style="display:none;" role="menu" aria-label="单元格对齐"></div>
<!-- 表格列/行尺寸拖拽手柄层：覆盖在表格上；层本身 pointer-events:none，仅手柄可拖 -->
<div id="tblResizeLayer" aria-hidden="true"></div>
<!-- 单元格/行列尺寸弹窗（2026-10）：列宽(%)、行高(数值+单位) -->
<div id="tblSizeModalBg" class="modal-bg" style="display:none;">
  <div class="modal" style="max-width:420px;">
    <div class="search-head"><h3>单元格尺寸</h3><button type="button" id="tblSizeCancel" class="x-btn" title="关闭" aria-label="关闭">✕</button></div>
    <p id="tblSizeHint" style="font-size:12px;color:var(--text-muted);margin:6px 0 0;"></p>
    <div class="tbl-size-grid">
      <label for="tblSizeColWidth">列宽</label>
      <span><input type="number" id="tblSizeColWidth" min="1" max="100" step="1" placeholder="留空不改"> <span style="color:#889">%</span></span>
      <label for="tblSizeRowHeight">行高</label>
      <span><input type="number" id="tblSizeRowHeight" min="1" max="2000" step="1" placeholder="留空不改"> <select id="tblSizeRowUnit"><option value="em">em</option><option value="px">px</option></select></span>
    </div>
    <div class="export-actions">
      <button type="button" id="tblSizeCancelBtn">取消</button>
      <button type="button" id="tblSizeApply" class="primary">应用</button>
    </div>
  </div>
</div>
<!-- 清理预览模态框（2026-10-16）：展示清理统计报告，确认后应用 -->
<div id="cleanPreviewBg" class="modal-bg" style="display:none;">
  <div class="modal" style="max-width:640px;max-height:80vh;overflow:auto;">
    <div class="search-head"><h3>清理预览</h3><button type="button" id="cleanPreviewCancel" class="x-btn" title="关闭" aria-label="关闭">✕</button></div>
    <p id="cleanPreviewSummary" style="font-size:13px;color:#33414f;font-weight:600;margin:8px 0;"></p>
    <div id="cleanPreviewList" style="font-size:12px;color:#5a6b7c;line-height:1.8;max-height:40vh;overflow:auto;"></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;">
      <button type="button" id="cleanPreviewCancelBtn">取消</button>
      <button type="button" id="cleanPreviewApply" class="primary">应用清理</button>
    </div>
  </div>
</div>
<div id="toast" aria-hidden="true"></div>
<!-- KaTeX（2026-10-21）：LaTeX 公式渲染成 MathML。随包分发（ui/vendor/），
     离线可用——矫正界面可能在无外网环境，不能依赖 CDN。缺失时公式按钮会提示
     「公式功能不可用」，其余功能不受影响。 -->
<script src="/ui/vendor/katex.min.js"></script>
<script src="/ui/app.js"></script>
</body>
</html>
"""
