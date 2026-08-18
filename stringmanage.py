from zhconv import zhconv


def ttos(text):
    return zhconv.convert(text, "zh-cn")


def stot(text):
    return zhconv.convert(text, "zh-tw")


import html
import re
from typing import Any, Dict, List

# PaddleOCR 系视觉模型（如 ULQ4/ULQ8）原生输出带坐标的结构化行：
#   title [337, 99, 611, 123]工人夜校招生广告
#   text [21, 152, 327, 170]列位工人来听我们几句白话：
#   page_number [78, 904, 94, 918]2
# 该格式在识别阶段按行给出（类别 + 边界框 + 文本），需要转换为
# 正文 HTML 才能被 htmlmanage 渲染为段落/标题（标题进 EPUB 目录）。
_BBOX_LINE_RE = re.compile(
    r"^([a-z_]+)\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\s*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)
# 不进入正文的行类别（页码/纯装饰，直接丢弃）
_BBOX_SKIP_LABELS = {"page_number", "figure", "image"}


def convert_bbox_text(text: str) -> str:
    """把 PaddleOCR 风格 `label [x1,y1,x2,y2] 文本` 行转换为正文 HTML。

    - title → <h2>（标题，自动进 EPUB 目录，与手动矫正的章节标记一致）
    - text / 其他类别 → <p>（普通段落）
    - page_number / figure / image → 丢弃
    - 无坐标的行原样保留；整页不含该格式时原样返回（不影响其他模型输出）。
    """
    if not _BBOX_LINE_RE.search(text):
        return text
    out = []
    for line in text.split("\n"):
        m = _BBOX_LINE_RE.match(line.strip())
        if not m:
            out.append(line)
            continue
        label, content = m.group(1).lower(), m.group(6).strip()
        if not content or label in _BBOX_SKIP_LABELS:
            continue
        escaped = html.escape(content, quote=False)
        if label == "title":
            out.append(f"<h2>{escaped}</h2>")
        else:
            out.append(f"<p>{escaped}</p>")
    return "\n".join(out)


def clean_bbox_text(text: str) -> str:
    """去掉 PaddleOCR 风格 `label [x1,y1,x2,y2] 文本` 行的坐标前缀（纯文本版）。

    与 convert_bbox_text 不同：不生成 HTML，只把 bbox 行剥为纯文本
    （`text [21,2,327,109]列位工人…` → `列位工人…`），page_number/figure/image
    行直接丢弃；无坐标的行原样保留；整段不含该格式时原样返回。
    供矫正界面 /api/reocr 清理 ULQ4/ULQ8 等模型的返回内容（2026-08-09）。
    """
    if not _BBOX_LINE_RE.search(text):
        return text
    out = []
    for line in text.split("\n"):
        m = _BBOX_LINE_RE.match(line.strip())
        if not m:
            out.append(line)
            continue
        label, content = m.group(1).lower(), m.group(6).strip()
        if not content or label in _BBOX_SKIP_LABELS:
            continue
        out.append(content)
    return "\n".join(out)


# Qwen 系模型可能输出 <thinking>、</thinking> 思考块（即使 enable_thinking=False）
# 个别模型/版本仍会带出），重识别/校对路径需整体移除。
_THINK_BLOCK_RE = re.compile(r"<thinking>.*?</thinking>", re.DOTALL | re.IGNORECASE)


def strip_think_blocks(text: str) -> str:
    """移除模型输出中的 `<thinking>` 思考块。"""
    return _THINK_BLOCK_RE.sub("", text or "")


# 独立成行的页码（普通文本模型如 QWEN 的页面输出里，页码常单独成行）。
# 支持：纯数字、第X页、- 2 -、2 / 12。只清理出现在页面首行或末行的页码行，
# 避免误删正文中独立出现的数字（如年份 1918、章节编号等）。
_PAGE_NUM_LINE_RE = re.compile(
    r"^\s*(?:"
    r"第\s*[0-9一二三四五六七八九十百千]+\s*页"
    r"|[-\u2014\u2015·]\s*\d{1,3}\s*[-\u2014\u2015·]"
    r"|\d{1,3}\s*/\s*\d{1,3}"
    r"|\d{1,3}"
    r")\s*$"
)


def strip_page_numbers(text: str) -> str:
    """删除页面首行/末行的独立页码行（如 "2"、"第 3 页"、"- 4 -"、"5 / 12"）。

    仅处理首/末非空行：页码总是出现在页面顶部或底部，正文中间的独立
    数字（年份、编号等）不受影响。整页只有页码时结果为空字符串。
    """
    lines = text.split("\n")
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None:
        return text
    last = next(
        (i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None
    )
    changed = False
    if _PAGE_NUM_LINE_RE.match(lines[first]):
        lines[first] = ""
        changed = True
    if last is not None and last != first and _PAGE_NUM_LINE_RE.match(lines[last]):
        lines[last] = ""
        changed = True
    return "\n".join(lines) if changed else text


# 纯文本行级标题启发式（普通 OCR 模型无结构输出时，用行形态识别标题行）：
# 候选 = 去空白后 1..30 字符、不含句末/引出行标点（。！？!?…，、；：）、
# 含 CJK/字母、非纯数字。命中行 → <h1>（htmlmanage 按 h1 换页 = 每篇文章一个
# EPUB 新页 + 标题进目录）。
_CJK_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")
_HEADING_TERMINAL_RE = re.compile(r"[。！？!?…，、；：]$")
_DIGITS_ONLY_RE = re.compile(r"^[\d\s\u3000·\-—–]+$")


def detect_headings(text: str) -> str:
    """启发式把纯文本页转成 `<h1>`/`<p>` 混合 HTML：短行（≤30 字、非句末标点
    结尾、含文字）判为标题 → `<h1>`，其余行 → `<p>`。仅当整页至少命中 1 个
    标题才转换（否则原样返回，不影响无标题页面与其他路径）。已含 HTML 标签的
    行（bbox 输出等）原样保留——转换由 htmlmanage 的 h1-split 自动分页。
    """
    lines = text.split("\n")
    hit = 0
    out = []
    for line in lines:
        s = line.strip()
        if "<" in s or not s:
            out.append(line)
            continue
        if (
            1 <= len(s) <= 30
            and not _HEADING_TERMINAL_RE.search(s)
            and (_CJK_RE.search(s) or any(c.isalpha() for c in s))
            and not _DIGITS_ONLY_RE.match(s)
        ):
            out.append(f"<h1>{html.escape(s, quote=False)}</h1>")
            hit += 1
        else:
            out.append(f"<p>{html.escape(s, quote=False)}</p>")
    return "\n".join(out) if hit else text


def clean_and_structure_text(
    pages: List[Dict[str, Any]],
    remove_thoughts: bool = True,
    normalize_spaces: bool = True,
    split_paragraphs: bool = True,
    merge: bool = True,
    to_simplified: bool = False,
    to_traditional: bool = False,
) -> Dict[str, Any]:
    """多页文本清洗/结构化：移除<think>标签、规范空白、繁简转换、分段，保证顺序。输入 pages: [{'page':..., 'text':...}, ...] -> 输出每页/正文合并/所有段落。"""

    assert not (to_simplified and to_traditional), "只能二选一进行简繁转换"
    pages = sorted(
        [p for p in pages if "text" in p and "page" in p], key=lambda x: x["page"]
    )
    results = []
    all_paras = []
    body_segs = []
    for orig in pages:
        t = orig["text"]
        # -- 移除<think>标签及内容 --
        if remove_thoughts:
            t = strip_think_blocks(t)
        # -- PaddleOCR 系模型（ULQ4/ULQ8）带坐标输出转正文 HTML --
        # 必须在空白规范之前：bbox 行按行匹配，转换后 title/text 变为
        # <h2>/<p> 供 htmlmanage 渲染（title 进目录、page_number 丢弃）。
        t = convert_bbox_text(t)
        # -- 删除页面首/末行的独立页码（普通文本模型也适用） --
        t = strip_page_numbers(t)
        # -- 纯文本标题启发式识别（普通 OCR 无结构输出） --
        # 命中标题的页面整页转 <h1>/<p>，供 htmlmanage 按 h1 分页（每篇新页）
        # + 标题进 EPUB 目录；未命中（无标题页）原样返回，不影响其他路径。
        t = detect_headings(t)
        # -- 空白规范 --
        if normalize_spaces:
            t = re.sub(r"[ \u3000\t]+", " ", t)
            t = re.sub(r"[\r\n]+", "\n", t)
            t = re.sub(r"\n{2,}", "\n", t)
            t = t.strip()
        # -- 繁简字转换 -- (global disabled by default; support per-page flags)
        page_to_simplified = to_simplified or bool(orig.get("to_simplified", False))
        page_to_traditional = to_traditional or bool(orig.get("to_traditional", False))
        if page_to_simplified and not page_to_traditional:
            t = ttos(t)
        elif page_to_traditional and not page_to_simplified:
            t = stot(t)
        # -- 分割段落 --
        paras = (
            [p for p in re.split(r"(?:\n|^)[ \t\u3000]*", t) if p.strip()]
            if split_paragraphs
            else [t]
        )
        for p in paras:
            all_paras.append({"page": orig["page"], "text": p})
        results.append({"page": orig["page"], "text": t})
        body_segs.append(t)
    return {
        "pages": results,
        "body": "\n".join(body_segs) if merge else "",
        "paragraphs": all_paras,
    }
