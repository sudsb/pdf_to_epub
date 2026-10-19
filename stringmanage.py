try:
    from zhconv import zhconv
except Exception:
    zhconv = None


def ttos(text):
    return text if zhconv is None else zhconv.convert(text, "zh-cn")


def stot(text):
    return text if zhconv is None else zhconv.convert(text, "zh-tw")


import html
import re
from typing import Any, Dict, List

from tableconvertmanage import convert_text_tables

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


# -- ULQ 杂符引注清理 + 括号对统一（与 correctmanage._normalize_brackets 同实现；
# 不能反向导入 correctmanage（会循环导入），故在此复制一份小实现） --
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
_BRACKET_PAIR_RES = (
    re.compile(r"【([^【】]*)】"),
    re.compile(r"\[([^\[\]\n]{1,32})\]"),
    re.compile(r"［([^［］]*)］"),
)
# $ 包裹剥除（2026-09-21，与 correctmanage._strip_dollar_bracket_hug 同实现）：
# OvisOCR2 重识别把原文 〔x〕 输出成 $〔x〕$——$ 为模型自造标记符，紧贴括号时剥掉。
_DOLLAR_OPEN_BRACKET_RE = re.compile(r"\$+\s*([〔【［\[(（])")
_DOLLAR_CLOSE_BRACKET_RE = re.compile(r"([〕】］\])）])\s*\$+")


def normalize_brackets(text: str) -> str:
    """ULQ 模型杂符引注清理 + 括号对统一。

    ① ``（^{[1]】}`` 等垃圾包裹的数字引注 → ``〔1〕``；
    ② 【x】/[x]/［x］（可能混用）→ 统一为 〔x〕；
    ③ $ 紧贴括号的模型杂符包裹（如 $〔x〕$）→ 剥掉 $。
    """
    text = _DOLLAR_OPEN_BRACKET_RE.sub(r"\1", text)
    text = _DOLLAR_CLOSE_BRACKET_RE.sub(r"\1", text)
    text = _ULQ_JUNK_BRACKET_RE.sub(r"〔\1〕", text)
    for pat in _BRACKET_PAIR_RES:
        text = pat.sub(r"〔\1〕", text)
    return text


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


def _tables_enabled(flag: bool | None = None) -> bool:
    """文字表格自动转换开关：显式 flag 优先，否则懒读 config 的顶层 ``table_convert``。"""
    if flag is not None:
        return bool(flag)
    try:
        from configmanage import get_config

        return bool(get_config(show_dialogs=False).get("table_convert", True))
    except Exception:
        return True


def clean_and_structure_text(
    pages: List[Dict[str, Any]],
    remove_thoughts: bool = True,
    normalize_spaces: bool = True,
    split_paragraphs: bool = True,
    merge: bool = True,
    to_simplified: bool = False,
    to_traditional: bool = False,
    convert_tables: bool | None = None,
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
        # -- ULQ 杂符引注清理 + 括号对统一：【x】/[x]/［x］→〔x〕，混用归一 --
        t = normalize_brackets(t)
        # -- 文字表格 → 原生 <table>（自动档，规格 A/B；设置 table_convert 可关）--
        # 必须在 detect_headings 之前：否则表行会被包成 <h1>/<p>；表格转成单行
        # <table> 后，detect_headings 的「含 < 的行原样保留」规则会自然放行。
        if _tables_enabled(convert_tables):
            t = convert_text_tables(t)
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


# ---------------------------------------------------------------------------
# 扩展标点/符号归一化（用于智能清理）
# ---------------------------------------------------------------------------

# 中英文标点归一化的基础映射（与 correctmanage._normalize_punctuation 保持一致）
_CJK_RANGE = "\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF"
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

# 扩展符号映射：英文符号 → 中文符号（仅在 CJK 上下文生效）
# 引号需按「引号内容是否含汉字」成对判定，不能用简单替换，见 _normalize_quotes。
_EXT_HALF_TO_FULL = (
    ("[", "【"),
    ("]", "】"),
    # 半角破折号 -- → ——
    ("--", "——"),
    # 半角波浪号 ~ → ～
    ("~", "～"),
)

# 反向映射：中文符号 → 英文符号（仅在纯拉丁语境生效）。
# 省略号 ……→... 单独处理（需要「同行边界」判定），不放进本表。
_EXT_FULL_TO_HALF = (
    ("【", "["),
    ("】", "]"),
    ("——", "--"),
    ("～", "~"),
)

_CJK_RE = re.compile(f"[{_CJK_RANGE}]")

# 省略号归一化：一行内多对省略号 → 单对标准省略号（……）
# 匹配连续 3 个以上半角点，或连续 1 个以上全角省略号点。
_ELLIPSIS_RE = re.compile(r"(?:\.{3,}|…+)")


def _normalize_ellipsis(text: str) -> str:
    """归一化省略号长度：一行内多对省略号合并为一对标准省略号（……）。

    策略（按 \\n 分行，不跨行）：
    - 每个省略号序列（3 个以上半角点，或 1 个以上全角省略号点）统一为标准 ……；
    - 一行内若出现多对省略号，仅保留第一对，其余省略号标记删除，**文字原样保留**。
    - 单个省略号也统一为标准 ……。
    """
    lines = text.split("\n")
    out_lines = []
    for line in lines:
        matches = list(_ELLIPSIS_RE.finditer(line))
        if not matches:
            out_lines.append(line)
            continue
        first = matches[0]
        # 保留第一对省略号，删除同行内后续省略号标记，两侧文字一字不丢
        rest = _ELLIPSIS_RE.sub("", line[first.end():])
        out_lines.append(line[:first.start()] + "……" + rest)
    return "\n".join(out_lines)


def _normalize_quotes(text: str) -> str:
    """ASCII 直/单引号 → 中文弯引号（仅当引号内容含汉字时成对转换）。

    - 双引号 `"..."` → “...”，单引号 `'...'` → ‘...’；
    - 引号内容为纯英文/数字（如 `他说 "hello"`、`It 'is' good`）保持 ASCII，
      避免误伤英文引语；
    - 单引号左邻/右邻为字母数字时不配对（避免 `it's` 被当作引号）。
    """

    def _dq(m: re.Match) -> str:
        inner = m.group(1)
        return "\u201c" + inner + "\u201d" if _CJK_RE.search(inner) else m.group(0)

    def _sq(m: re.Match) -> str:
        inner = m.group(1)
        return "\u2018" + inner + "\u2019" if _CJK_RE.search(inner) else m.group(0)

    text = re.sub(r'(?<![A-Za-z0-9])"([^"\n]*)"', _dq, text)
    text = re.sub(r"(?<![A-Za-z0-9])'([^'\n]*?)'(?![A-Za-z0-9])", _sq, text)
    return text


def _punct_ctx_cjk(text: str) -> bool:
    """标点归一是否按「中文语境」处理（2026-10 页级判定；correctmanage 按整页传入）。

    中文语境 = 含汉字，或全篇没有拉丁字母（例如只在空页上输了一个 `()` 的
    测试场景）；两者都不满足（纯英文/数字内容）→ 保留半角。
    """
    return bool(re.search(f"[{_CJK_RANGE}]", text)) or not re.search(r"[A-Za-z]", text)


# 中文语境下半角标点的保护写法（命中 → 保持半角，避免改坏数据与英文写法）。
# 括号（()）不在此表：中文页内半角括号一律成对/整体转全角（修「测试（)」
# 这种只转一半的问题）。
_PUNCT_PROTECT_CTX = {
    ",": r"(?<=\d),(?=\d)",                     # 千分位/数字序列 1,000 / 1,2
    ";": r"(?<=\d);(?=\d)|&[A-Za-z0-9#]{1,16};",  # 数字序列 / HTML 实体 &amp;
    ":": r"(?<=\d):(?=\d)|:(?=//)",             # 时间 12:30、URL scheme https://
    "?": r"\?(?=[A-Za-z0-9_#]+=[A-Za-z0-9%._~/?#&-])",  # 查询串 ?a=1
    "!": r"!(?:==?)",                           # 比较符 != / !==
}
# 句号保护（中文语境）：小数点、token 内点（e.g / Fig.1 / test.OK）、
# 缩写点（e.g. / i.e. 链式点、Dr. / Fig. / No. 等小写缩写、Fig. 1 后接数字
# 或英文）、行首小数 .5、省略号 .. / ...
_CTX_DOT_PROTECT = (
    r"(?<=\d)\.(?=\d)"
    r"|(?<=[A-Za-z])\.(?=[A-Za-z0-9])"
    r"|(?<=[A-Za-z])\.(?=[ \t]+[A-Za-z0-9])"
    r"|(?<=\d)\.(?=[ \t]+[A-Za-z0-9])"
    r"|(?<=\.)[A-Za-z]{1,4}\."
    r"|(?<![A-Za-z])[A-Z][a-z]{0,2}\.(?=[ \t])"
    r"|(?<!\S)\.(?=\d)"
    r"|(?<!\.)\.(?=\.)"
)


def _ctx_half_to_full(text: str) -> str:
    """中文语境（页级）：半角 ,;:?!(). 与括号整体转全角（带保护项）。

    相对旧「与汉字直接相邻才转」的放宽：隔空格（他说abc, 后面）、行尾
    （结束. ）、括号内是英文（中文 (abc) 混排）、孤立 () 都会转换；
    幂等：已是全角的不再变化。
    """
    s = str(text)
    s = s.replace("(", "（").replace(")", "）")
    for half, full in _HALF_TO_FULL:
        if half in "()":
            continue
        prot = _PUNCT_PROTECT_CTX.get(half)
        if prot:
            s = re.sub(
                rf"(?P<prot>{prot})|(?P<half>{re.escape(half)})",
                lambda m: m.group(0) if m.group("prot") is not None else full,
                s,
            )
        else:
            s = s.replace(half, full)
    # 句号（放宽：不再要求与汉字直接相邻；保护项原样保留）
    s = re.sub(
        rf"(?P<prot>{_CTX_DOT_PROTECT})|(?P<dot>(?<!\.)\.(?!\.))",
        lambda m: m.group(0) if m.group("prot") is not None else "。",
        s,
    )
    return s


def _normalize_punctuation_base(text: str, *, ctx_cjk: bool | None = None) -> str:
    """基础中英文标点归一化（与 correctmanage._normalize_punctuation 保持一致）。

    2026-10 页级中文语境：
    - 中文语境（ctx_cjk=True；默认按 text 自身判定）→ 整段半角转全角，
      见 _ctx_half_to_full（保护项：小数/千分位/时间/URL/查询串/缩写点/省略号）；
    - 纯英文语境 → 原行为：汉字相邻转全角 + 字母/数字相邻的全角标点转回半角。
    `.` 带「非省略号点」保护（(?<!\\.)/(?!\\.)），不会把 `...` 拆成句号。
    """
    s = str(text)
    if ctx_cjk is None:
        ctx_cjk = _punct_ctx_cjk(s)
    if ctx_cjk:
        return _ctx_half_to_full(s)
    for half, full in _HALF_TO_FULL:
        s = re.sub(rf"(?<=[{_CJK_RANGE}]){re.escape(half)}", full, s)
        s = re.sub(rf"{re.escape(half)}(?=[{_CJK_RANGE}])", full, s)
    # 句号：单独的 . （非 .. 序列成员）与汉字相邻 → 。
    s = re.sub(rf"(?<=[{_CJK_RANGE}])\.(?!\.)", "\u3002", s)
    s = re.sub(rf"\.(?=[{_CJK_RANGE}])", "\u3002", s)
    for full, half in _FULL_TO_HALF:
        # 全角 → 半角：左右都是字母/数字，或在字符串边界且另一侧是字母/数字
        s = re.sub(
            rf"(?<=[{_LATIN_RANGE}]){re.escape(full)}(?=[{_LATIN_RANGE}])", half, s
        )
        # 处理字符串开头/结尾的情况
        s = re.sub(rf"^{re.escape(full)}(?=[{_LATIN_RANGE}])", half, s)
        s = re.sub(rf"(?<=[{_LATIN_RANGE}]){re.escape(full)}$", half, s)
    # 全角句号 → 半角句号（纯英文语境）
    s = re.sub(rf"(?<=[{_LATIN_RANGE}])\u3002(?=[{_LATIN_RANGE}])", ".", s)
    return s


def _normalize_symbols_core(text: str, half_to_full=None) -> str:
    """扩展符号核心：方括号/破折号/波浪号半角↔全角 + 省略号 ……→...（拉丁语境）。

    不含引号配对与省略号长度归一（那两步见 _normalize_quotes / _normalize_ellipsis）。
    """
    s = str(text)
    _hf = _EXT_HALF_TO_FULL if half_to_full is None else tuple(tuple(p) for p in half_to_full)
    _fh = tuple((f, h) for h, f in _hf) if _hf else tuple(_EXT_FULL_TO_HALF)
    for half, full in _hf:
        s = re.sub(rf"(?<=[{_CJK_RANGE}]){re.escape(half)}", full, s)
        s = re.sub(rf"{re.escape(half)}(?=[{_CJK_RANGE}])", full, s)
    for full, half in _fh:
        s = re.sub(
            rf"(?<=[{_LATIN_RANGE}]){re.escape(full)}(?=[{_LATIN_RANGE}])", half, s
        )
        s = re.sub(rf"^{re.escape(full)}(?=[{_LATIN_RANGE}])", half, s)
        s = re.sub(rf"(?<=[{_LATIN_RANGE}]){re.escape(full)}$", half, s)
    # 省略号：拉丁上下文（含行尾紧邻字母）转半角 ...
    s = re.sub(rf"(?<=[{_LATIN_RANGE}])……(?=[{_LATIN_RANGE}])", "...", s)
    s = re.sub(rf"^……(?=[{_LATIN_RANGE}])", "...", s)
    s = re.sub(rf"(?<=[{_LATIN_RANGE}])……$", "...", s)
    return s


def _normalize_symbols_extended(text: str, half_to_full=None) -> str:
    """扩展符号归一化（智能清理 clean_symbols 开关用）：

    引号配对 → 省略号长度归一 → 方括号/破折号/波浪号半角↔全角。
    基础标点（,;:?!(). ）不在此处理，由 clean_punctuation 负责。
    """
    s = _normalize_quotes(str(text))
    s = _normalize_ellipsis(s)
    return _normalize_symbols_core(s, half_to_full=half_to_full)


def _normalize_punctuation_extended(text: str) -> str:
    """扩展标点/符号归一化（一站式）：

    1. 省略号长度归一（一行内多对省略号 → 一对标准 ……）
    2. ASCII 引号 → 中文弯引号（引号内容含汉字时）
    3. 基础标点归一化（半角↔全角，按 CJK/拉丁上下文）
    4. 扩展符号归一化（方括号/破折号/波浪号）
    """
    s = _normalize_ellipsis(str(text))
    s = _normalize_quotes(s)
    s = _normalize_punctuation_base(s)
    return _normalize_symbols_core(s)


# 数字里的中文标点 → 英文（智能清理「数字标点归一」；2026-10 规格：
# 1。2 → 1.2、1，2 → 1,2、1。 → 1.）。表可在设置里自定义
# （config.json ui_settings.clean_digit_punct_map）；仅认单字符源 + 前一字符是数字/字母。
DIGIT_PUNCT_DEFAULT = (("。", "."), ("，", ","), ("、", ","), ("：", ":"), ("；", ";"), ("．", "."))
_DIGIT_CHARS = "0-9０-９"
_ALNUM_CHARS = "0-9Ａ-Ｚａ-ｚA-Za-z０-９"
_DIGIT_PUNCT_CACHE: dict = {}
_DIGIT_PUNCT_END_CACHE: dict = {}


def normalize_digit_punct(text: str, pairs=None) -> str:
    """把「数字/字母里的」中文标点换成英文（不动正文里其它中文标点）。

    ``pairs`` 为 ``[("。", "."), …]`` 形式的替换表（None → ``DIGIT_PUNCT_DEFAULT``）；
    只取单字符源。命中位置（前一字符必须是数字/字母，半角/全角皆可）：
    1. 夹在数字/字母之间：``1。2`` → ``1.2``、``1，2`` → ``1,2``、``1、2`` → ``1,2``、
       ``H，W`` → ``H,W``、``1。A`` → ``1.A``、``A。1`` → ``A.1``（跨类型同理）；
    2. 数字/字母之后即收尾（行尾/串尾或紧跟空白）：``1。`` → ``1.``、``共 1。 条`` → ``共 1. 条``。
       注意：字母收尾（如 ``H。``）不转换，避免误伤中文语境。
    3. 容忍行内空格/换行：``1 。 2`` → ``1.2``、``H , W`` → ``H,W``、``1。\n2`` → ``1.\n2``。
    正文里「数字/字母后接中文」的标点属正常中文标点，**不**替换：
    ``圆周率 3.14，和`` 保持不变（保底：只认数字/字母/空白/串尾收尾）。
    英文词组内的逗号（如 ``test,OK``）属正常文本标点，**不**替换。
    空表/无命中原样返回。
    """
    s = str(text if text is not None else "")
    if not s:
        return s
    mapping: dict = {}
    for item in (DIGIT_PUNCT_DEFAULT if pairs is None else pairs):
        try:
            src, dst = item
        except (TypeError, ValueError):
            continue
        src, dst = str(src), str(dst)
        if len(src) == 1 and src not in mapping:
            mapping[src] = dst
    if not mapping:
        return s
    key = tuple(sorted(mapping.items()))
    
    # 统一处理：alnum 之间（数字-数字、字母-字母、数字-字母、字母-数字）
    # 单字符 = 单个数字或单个 ASCII/全角字母（非单词内部）
    # 要求：前一个字符不是字母（避免匹配单词内部如 "test" 里的 "t"）
    #       后一个字符不是字母（避免匹配单词内部如 "OK" 里的 "O"）
    # 但允许数字-字母、字母-数字
    alnum = r"[0-9０-９A-Za-zＡ-Ｚａ-ｚ]"
    letter = r"[A-Za-zＡ-Ｚａ-ｚ]"
    punct_alts = "|".join(re.escape(k) for k in mapping)
    # 空白字符（不含换行）：空格、制表符、全角空格
    ws = r"[ \t\u3000]"
    
    # 正则：前向断言非字母，捕获 alnum，可选空白(不含换行)，标点，可选空白(不含换行)，后向断言 alnum(非字母结尾)
    # 这样处理：1。2、1。A、A。1、H，W、1\u3000。2
    # 注意：用 [ \t\u3000]* 而不是 \s* 以保留换行符
    pat = _DIGIT_PUNCT_CACHE.get(key)
    if pat is None:
        pat = re.compile(
            r"(?<!%s)(%s)%s*(?:%s)%s*(?=%s(?!%s))" % (letter, alnum, ws, punct_alts, ws, alnum, letter)
        )
        _DIGIT_PUNCT_CACHE[key] = pat
    def _repl_between(m):
        return m.group(1) + mapping[m.group(0)[len(m.group(1)):].lstrip()[0]]
    s = pat.sub(_repl_between, s)
    
    # 第二步：处理收尾情况（仅限数字，不处理字母收尾）
    # 也保留换行：只匹配空格/制表符/全角空格，不匹配换行
    pat_end = _DIGIT_PUNCT_END_CACHE.get(key)
    if pat_end is None:
        digit = r"[0-9０-９]"
        pat_end = re.compile(
            r"(%s)%s*(?:%s)%s*(?=\s|$)" % (digit, ws, punct_alts, ws)
        )
        _DIGIT_PUNCT_END_CACHE[key] = pat_end
    def _repl_end(m):
        return m.group(1) + mapping[m.group(0)[len(m.group(1)):].lstrip()[0]]
    return pat_end.sub(_repl_end, s)


def clean_alnum_spaces(text: str) -> str:
    """清理数字间/单字母间的多余空格，保留英文词组内部空格。

    规则：
    - 连续数字间的空格：``1 2 3`` → ``123``
    - 单字母序列（3个以上连续单字母词）的空格：``M a r x`` → ``Marx``、``U S A`` → ``USA``、
      ``Ｍ ａ ｒ ｘ`` → ``Ｍａｒｘ``
    - 英文词组（含连字符/撇号的单词）内部空格保留：``hello world`` → ``hello world``、
      ``state-of-the-art`` → ``state-of-the-art``
    - 普通单字母词（如 "I am a man" 中的 "I"、"a"）不合并
    - 换行符保留不变
    - 全角空格(U+3000)同理
    - 全角字母同理

    使用正则：先合并数字/单字母序列，再清理其中的空格。
    """
    if not text:
        return ""
    # 1. 数字序列内的空格：(?<=\d)\s+(?=\d) - 只匹配空格/制表符/全角空格，不匹配换行
    text = re.sub(r"(?<=[0-9０-９])[ \t\u3000]+(?=[0-9０-９])", "", text)
    # 2. 单字母缩写序列处理
    #    单字母词 = 单个大写ASCII字母(A-Z)或全角字母(Ａ-Ｚａ-ｚ)，前后为空格/边界
    #    算法：扫描文本，找出连续的单字母词序列，按规则合并
    ws = r"[ \t\u3000]"
    # 先用正则找出所有单字母词的位置（单字母 = A-Z, a-z, 全角字母，前后为空白/边界）
    single_letter_pat = re.compile(r"(?:^|%s)([A-Za-zＡ-Ｚａ-ｚ])(?=%s|$)" % (ws, ws))
    matches = list(single_letter_pat.finditer(text))
    if len(matches) >= 2:
        # 分组连续的单字母词
        groups = []
        current_group = [matches[0]]
        for i in range(1, len(matches)):
            prev = current_group[-1]
            curr = matches[i]
            # 检查是否连续（中间只有空白字符）
            # prev.end() 指向字母后的位置，curr.start() 指向字母前的位置（可能含空白）
            # 需要检查 prev.end() 到 curr.start(1) 之间是否全是空白
            # curr.start(1) 是捕获组1的开始位置
            gap_start = prev.end()
            gap_end = curr.start(1)
            if gap_start < gap_end and all(c in " \t\u3000" for c in text[gap_start:gap_end]):
                current_group.append(curr)
            else:
                if len(current_group) >= 2:
                    groups.append(current_group)
                current_group = [curr]
        if len(current_group) >= 2:
            groups.append(current_group)
        
        # 从后往前替换，避免位置偏移
        text_list = list(text)
        for group in reversed(groups):
            if len(group) >= 3:
                # 3个以上：全部合并
                start = group[0].start(1)
                end = group[-1].end(1)
                merged = "".join(m.group(1) for m in group)
                text_list[start:end] = list(merged)
            elif len(group) == 2:
                # 2个：仅当都是大写ASCII时合并
                c1, c2 = group[0].group(1), group[1].group(1)
                if 'A' <= c1 <= 'Z' and 'A' <= c2 <= 'Z':
                    start = group[0].start(1)
                    end = group[1].end(1)
                    merged = c1 + c2
                    text_list[start:end] = list(merged)
        text = "".join(text_list)
    return text
