# -*- coding: utf-8 -*-
"""文字表格 → 程序原生表格（HTML ``<table>``）转换器。

OCR 提示词要求「按原文原格式输出」，视觉模型遇到表格时最常产出 Markdown 管道表
（``| A | B |`` + ``|---|---|`` 分隔行），且带真实瑕疵：列数不齐、全角竖线、
表题与表头挤同一行。本模块把这些**文字表格**转成程序原生表格结构
（``<table><thead><tr><th>…`` + ``<tbody><tr><td>…``），供两条链路复用：

- 默认结构化管线（``stringmanage.clean_and_structure_text``，自动档）；
- 矫正编辑器（``correctmanage.initial_html`` 自动档、``/api/table_convert`` 手动档）。

纯函数，只依赖 ``re`` / ``html``（含 ``html.parser``）；不读写 config/session（开关由调用方决定）。
幂等：已是 ``<table>`` 的文本原样返回，转换结果再跑一次不变。

反向（2026-10）：``table_html_to_pipe_text`` 把原生 ``<table>`` 还原成 Markdown 管道文字，
供编辑器「表格转文本」入口复用；``convert_lines`` 额外回传「源行范围 → 表格 HTML」的
chunks，供编辑器把 DOM 里的连续文本块精确替换成表格（重识别/应用校正后的自动转换）。

规格见 docs/superpowers/specs/2026-10-17-text-table-to-native-table-design.md
（A=Markdown 管道表，B=ASCII 画框表，C=空格/制表符对齐列，D=既存 HTML 表格跳过）。
"""

import html
import re
from html.parser import HTMLParser

__all__ = [
    "convert_text_tables",
    "convert_lines",
    "expand_inline_rows",
    "table_html_to_pipe_text",
]

# 转义竖线 ``\|`` 在切分时的临时占位符（含 NUL，正常文本不会出现）。
_ESC_PIPE = "\x00ptoe-pipe\x00"

# 全角竖线（表格分隔符常见变体）统一为半角 ``|``。
_FULLWIDTH_PIPES = ("｜", "︱")

# 分隔行单元格：可选的左右对齐冒号 + 至少一个横线（半角减号或全角破折号）。
_SEP_CELL_RE = re.compile(r"^:?[-—－]+:?$")

# 画框表边框行（规格 B）：左角 + 横线/交点 + 右角（Unicode 制表符或 ASCII ``+``/``-``）。
_BORDER_RE = re.compile(r"^[┌├└┏┠┗+][─━\-┬┼┴┯┿┷+]*[┐┤┘┓┫┛+]$")

# 画框表内容行的列分隔（Unicode ``│`` 或半角 ``|``）。
_BOX_SEP_RE = re.compile(r"[│|]")

# 空格/制表符对齐列的列分隔：制表符，或 ≥2 个连续空格（含全角空格）（规格 C）。
_SPACE_SEP_RE = re.compile(r"\t+|[ \u3000]{2,}")


def _normalize_pipes(line: str) -> str:
    """全角竖线 → 半角 ``|``（仅用于分析/切分，正文行原样输出由调用方保留）。"""
    out = line
    for ch in _FULLWIDTH_PIPES:
        out = out.replace(ch, "|")
    return out


def _split_cells(line: str) -> list[str]:
    """按未转义 ``|`` 切分成单元格。

    仅去掉「整行以 ``|`` 开头」时行首的空单元与「整行以 ``|`` 结尾」时行尾的空单元；
    中间空单元是真实空列（如省市名列的表头本就是空的），必须保留。
    ``\\|`` 视为单元格内的字面竖线。
    """
    protected = line.replace("\\|", _ESC_PIPE)
    parts = protected.split("|")
    # 边界判定用去掉前导空白的行（OCR 常给表格行加缩进：`   | C | D |`）——
    # 否则缩进行会被当成「行首有一个真实空单元」，整行列数右移错位（终审 F1）。
    stripped = protected.strip()
    if stripped.startswith("|") and parts and parts[0].strip() == "":
        parts = parts[1:]
    if stripped.endswith("|") and parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [p.replace(_ESC_PIPE, "|").strip() for p in parts]


def _is_pipe_row(line: str) -> bool:
    """该行是否含至少一个未转义竖线（候选表格行）。"""
    return "|" in line.replace("\\|", "")


def _is_separator(line: str) -> bool:
    """该行是否是 Markdown 表分隔行（每格形如 ``---`` / ``:-:``，且至少一格）。"""
    if "|" not in line.replace("\\|", ""):
        return False
    cells = _split_cells(line)
    return bool(cells) and all(_SEP_CELL_RE.match(c) for c in cells)


def _align_class(cell: str) -> str | None:
    """分隔行单元格 → 程序既有的对齐 class（无对齐返回 None）。"""
    if cell.startswith(":") and cell.endswith(":"):
        return "ptoe-align-center"
    if cell.startswith(":"):
        return "ptoe-align-left"
    if cell.endswith(":"):
        return "ptoe-align-right"
    return None


def _fit(cells: list[str], width: int) -> list[str]:
    """把一行单元格归一为 ``width`` 列：不足补空，超出则多出的文本并入最后一格。"""
    if len(cells) == width:
        return cells
    if len(cells) < width:
        return cells + [""] * (width - len(cells))
    return cells[: width - 1] + ["".join(cells[width - 1 :])]


def _render_table(
    header_cells: list[str], rows: list[list[str]], aligns: list[str | None]
) -> str:
    """渲染成单行原生表格 HTML（无 colspan/rowspan，单元格文本已转义）。"""

    def cell(tag: str, text: str, idx: int) -> str:
        cls = aligns[idx] if idx < len(aligns) else None
        attr = f' class="{cls}"' if cls else ""
        return f"<{tag}{attr}>{html.escape(text, quote=False)}</{tag}>"

    head = "".join(cell("th", c, k) for k, c in enumerate(header_cells))
    body = "".join(
        "<tr>" + "".join(cell("td", c, k) for k, c in enumerate(row)) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _header_cells(
    header_line: str, width: int, *, with_caption: bool
) -> tuple[list[str], str | None]:
    """切出表头单元格，并在判据成立时顺带提取表题。

    判据（设计文档规格 A）：表头行**不以 ``|`` 开头**（表题是行首的非管道文字）且
    切分后单元数恰为 ``width + 1``，则首单元是表题。这样 `社数 | 户数` 这种无前导
    竖线的表头不会被误当表题。
    """
    cells = _split_cells(header_line)
    # 同样按去前导空白的行判定（缩进的表头行不应被误判成「表题 + 表头」）
    if (
        with_caption
        and not header_line.strip().startswith("|")
        and len(cells) == width + 1
    ):
        return cells[1:], cells[0]
    return cells, None


def _md_block_to_html(
    header_line: str, sep_line: str | None, body_lines: list[str]
) -> list[str]:
    """把一段管道表转成输出行列表（可选表题 ``<p>`` + 单行 ``<table>``）。"""
    caption = None
    if sep_line is not None:
        sep_cells = _split_cells(sep_line)
        width = len(sep_cells)
        aligns: list[str | None] = [_align_class(c) for c in sep_cells]
        header_cells, caption = _header_cells(
            header_line, width, with_caption=True
        )
    else:
        header_cells = _split_cells(header_line)
        width = max(
            [len(header_cells)] + [len(_split_cells(b)) for b in body_lines]
        )
        aligns = [None] * width
    if width < 1:
        return []
    header_cells = _fit(header_cells, width)
    rows = [_fit(_split_cells(b), width) for b in body_lines]
    out = []
    if caption:
        out.append(f"<p>{html.escape(caption, quote=False)}</p>")
    out.append(_render_table(header_cells, rows, aligns))
    return out


def _collect_body(lines: list[str], start: int) -> tuple[list[str], int]:
    """从 ``start`` 起收集连续管道行；行间允许空行，但空行后必须紧跟管道行。

    返回 (已归一竖线的管道行列表, 下一未消费行号)。
    """
    body: list[str] = []
    k = start
    n = len(lines)
    while k < n:
        norm = _normalize_pipes(lines[k])
        if _is_pipe_row(norm):
            body.append(norm)
            k += 1
        elif lines[k].strip() == "":
            m = k
            while m < n and lines[m].strip() == "":
                m += 1
            if m < n and _is_pipe_row(_normalize_pipes(lines[m])):
                k = m
            else:
                break
        else:
            break
    return body, k


def _try_pipe_table(
    lines: list[str], i: int, *, require_separator: bool
) -> tuple[list[str], int] | None:
    """尝试把从 ``i`` 起的管道表转成输出行；失败返回 None（调用方原样输出该行）。"""
    n = len(lines)
    j = i + 1
    while j < n and lines[j].strip() == "":
        j += 1
    sep_norm = None
    if j < n:
        cand = _normalize_pipes(lines[j])
        if _is_separator(cand):
            sep_norm = cand
    header_norm = _normalize_pipes(lines[i])
    if sep_norm is not None:
        body, next_i = _collect_body(lines, j + 1)
        if not body:
            return None
        out = _md_block_to_html(header_norm, sep_norm, body)
        return (out, next_i) if out else None
    if require_separator:
        return None
    body, next_i = _collect_body(lines, i + 1)
    if not body:
        return None
    out = _md_block_to_html(header_norm, None, body)
    return (out, next_i) if out else None


def _is_border(line: str) -> bool:
    """该行是否是画框表边框行（``┌─┬─┐`` / ``+--+--+`` 等）。"""
    return bool(_BORDER_RE.match(line.strip()))


def _is_box_content(line: str) -> bool:
    """该行是否是画框表内容行（含 ``│`` 或未转义 ``|``）。"""
    return ("│" in line) or _is_pipe_row(line)


def _split_box_cells(line: str) -> list[str]:
    """按 ``│``/``|`` 切分画框表内容行；边界空单元处理同 ``_split_cells``。"""
    protected = line.replace("\\|", _ESC_PIPE)
    parts = _BOX_SEP_RE.split(protected)
    stripped = line.strip()
    if stripped[:1] in ("│", "|") and parts and parts[0].strip() == "":
        parts = parts[1:]
    if stripped[-1:] in ("│", "|") and parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [p.replace(_ESC_PIPE, "|").strip() for p in parts]


def _try_box_table(lines: list[str], i: int) -> tuple[list[str], int] | None:
    """尝试把从 ``i`` 起的画框表转成输出行；失败返回 None。

    识别：以边框行开头，收集连续的「边框行 + 内容行」，必须以边框行收尾，
    且至少 1 个内容行（首内容行为表头）。首行边框是强信号，自动档也识别。
    """
    n = len(lines)
    if not _is_border(lines[i]):
        return None
    content: list[str] = []
    k = i
    last_border = i
    while k < n:
        if _is_border(lines[k]):
            last_border = k
            k += 1
        elif _is_box_content(lines[k]):
            content.append(lines[k])
            k += 1
        else:
            break
    if last_border == i or not content or not _is_border(lines[k - 1]):
        return None
    rows = [_split_box_cells(c) for c in content]
    width = max(len(r) for r in rows)
    if width < 1 or len(rows) < 2:
        return None
    header_cells = _fit(rows[0], width)
    body = [_fit(r, width) for r in rows[1:]]
    return ([_render_table(header_cells, body, [None] * width)], k)


def _space_cells(line: str) -> list[str]:
    """按「制表符 或 ≥2 连续空格」切分（规格 C）；单空格不切。"""
    return [c.strip() for c in _SPACE_SEP_RE.split(line.strip())]


def _try_space_table(lines: list[str], i: int) -> tuple[list[str], int] | None:
    """尝试把从 ``i`` 起的空格/制表符对齐列转成输出行；失败返回 None。

    要求 ≥2 连续行、每行都能切出 ≥2 个单元（列数取最大值、不足补空）。
    误判风险高，仅手动档（``allow_space_aligned=True``）调用。
    """
    if len(_space_cells(lines[i])) < 2:
        return None
    rows: list[list[str]] = []
    k = i
    n = len(lines)
    while k < n:
        if lines[k].strip() == "":
            break
        cells = _space_cells(lines[k])
        if len(cells) < 2:
            break
        rows.append(cells)
        k += 1
    if len(rows) < 2:
        return None
    width = max(len(r) for r in rows)
    header_cells = _fit(rows[0], width)
    body = [_fit(r, width) for r in rows[1:]]
    return ([_render_table(header_cells, body, [None] * width)], k)


# 「无换行表格」的行内行分隔：一行里用双竖线拼起多行（OCR 常见：
# `| 省 | 人口 || 广东 | 1.2亿 |`）。仅当拆出的每段都是同列数（≥2）的完整管道行时才拆。
_INLINE_ROW_SEP_RE = re.compile(r"[|｜]{2,}")


def _split_inline_row(line: str) -> list[str] | None:
    """``| a | b || c | d |`` → ``["| a | b |", "| c | d |"]``；不是拼行返回 None。

    保护：行必须以竖线开头（正文里偶然的 ``a | b || c | d`` 不拆）；拆出的每段列数
    必须相同且 ≥2，否则按「真实空单元」的普通管道行原样处理（如 ``| a || b |``）。
    """
    norm = _normalize_pipes(line).strip()
    if not norm.startswith("|") or "||" not in norm:
        return None
    frags = [f for f in _INLINE_ROW_SEP_RE.split(norm) if f.strip()]
    if len(frags) < 2:
        return None
    rows: list[str] = []
    for idx, frag in enumerate(frags):
        s = frag.strip()
        if idx > 0:
            s = "| " + s
        if idx < len(frags) - 1:
            s = s + " |"
        rows.append(s)
    counts = [len(_split_cells(r)) for r in rows]
    if counts[0] < 2 or any(c != counts[0] for c in counts):
        return None
    return rows


def expand_inline_rows(lines: list[str]) -> tuple[list[str], list[int], set[int]]:
    """把「一行内拼了多行」的表格行展开成多行（无换行表格）。

    返回 ``(展开后的行, 每行的源行下标, 拼行首段的展开下标集合)``。拼行（``||`` 分隔）
    本身就是表格结构的强证据，故这些位置允许「无 Markdown 分隔行」直接成表。
    """
    out: list[str] = []
    src: list[int] = []
    glued: set[int] = set()
    for k, line in enumerate(lines):
        rows = _split_inline_row(line)
        if rows is None:
            out.append(line)
            src.append(k)
            continue
        glued.add(len(out))
        for row in rows:
            out.append(row)
            src.append(k)
    return out, src, glued


def convert_lines(
    lines: list[str],
    *,
    allow_space_aligned: bool = False,
    require_separator: bool = True,
) -> tuple[list[str], list[tuple[int, int, str]]]:
    """按行转换，返回 ``(输出行, chunks)``。

    ``chunks`` 每项 ``(start, end, html)``：输入行 ``lines[start:end]`` 被替换成单行
    HTML ``html``（输出中对应的那一行）。编辑器据此把 DOM 里的连续文本块按源行范围
    精确换成表格，无需自己解析 HTML（重识别/应用校正后的自动转换）。

    ``start/end`` 始终是**传入 lines 的下标**：行内拼行（``||``）会先拆成多行参与识别，
    但 chunks 会折算回源行区间——调用方按源行替换 DOM 块即可。
    """
    exp, src, glued = expand_inline_rows(list(lines))
    out: list[str] = []
    chunks: list[tuple[int, int, str]] = []
    i = 0
    n = len(exp)
    while i < n:
        block = None
        if _is_border(exp[i]):
            block = _try_box_table(exp, i)
        if block is None and _is_pipe_row(_normalize_pipes(exp[i])):
            # 拼行（``||`` 分隔）本身即表格强证据：该位置放宽「必须有 Markdown 分隔行」
            block = _try_pipe_table(
                exp, i, require_separator=require_separator and i not in glued
            )
        if block is None and allow_space_aligned:
            block = _try_space_table(exp, i)
        if block is not None:
            html_lines, next_i = block
            s0, s1 = src[i], src[next_i - 1]
            for one in html_lines:
                chunks.append((s0, s1 + 1, one))
            out.extend(html_lines)
            i = next_i
            continue
        out.append(exp[i])
        i += 1
    return out, chunks


def convert_text_tables(
    text: str,
    *,
    allow_space_aligned: bool = False,
    require_separator: bool = True,
) -> str:
    """把文本里的文字表格转成原生 ``<table>``；非表格行原样保留。

    参数：
    - ``require_separator=True``（自动档，默认）：管道表必须有 Markdown 分隔行才识别
      （安全锚点，避免把正文里偶然的 ``|`` 当表格）。
    - ``require_separator=False``（手动档）：无分隔行也识别，首行当表头。
    - ``allow_space_aligned``：是否识别无竖线的空格/制表符对齐列（仅手动档使用）。
    """
    out, _chunks = convert_lines(
        str(text).split("\n"),
        allow_space_aligned=allow_space_aligned,
        require_separator=require_separator,
    )
    return "\n".join(out)


# ---------- 反向：原生 <table> → Markdown 管道文字（「表格转文本」，2026-10） ----------

_ALIGN_MARK = {"left": ":---", "center": ":---:", "right": "---:"}


class _TableTextParser(HTMLParser):
    """抽取 ``<table>`` 的行/单元格纯文本与列对齐。

    - ``tables``：``[[[cell, …], …], …]``，按文档中表格出现顺序；
    - ``aligns``：与 ``tables`` 平行，每格为该行的对齐列表（left/center/right 或 ""）。
    嵌套表格不单独成表：内层单元格文本并入外层单元格（与服务端压平规则一致）。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.aligns: list[list[list[str]]] = []
        self._depth = 0
        self._rows: list[list[str]] | None = None
        self._row_aligns: list[list[str]] | None = None
        self._cells: list[str] | None = None
        self._cell_aligns: list[str] | None = None
        self._buf: list[str] | None = None

    @staticmethod
    def _align_of(attrs) -> str:
        cls = ""
        for k, v in attrs:
            if k == "class" and v:
                cls = v
        for name in ("left", "center", "right"):
            if "ptoe-align-" + name in cls:
                return name
        return ""

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._rows, self._row_aligns = [], []
            return
        if self._depth != 1:
            return
        if tag == "tr":
            self._cells, self._cell_aligns = [], []
        elif tag in ("td", "th"):
            self._buf = []
            (self._cell_aligns if self._cell_aligns is not None else []).append(
                self._align_of(attrs)
            )
        elif tag == "br" and self._buf is not None:
            self._buf.append(" ")

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "table":
            if self._depth == 1:
                if self._rows:
                    self.tables.append(self._rows)
                    self.aligns.append(self._row_aligns or [])
                self._rows, self._row_aligns = None, None
            self._depth = max(0, self._depth - 1)
            return
        if self._depth != 1:
            return
        if tag in ("td", "th") and self._buf is not None and self._cells is not None:
            self._cells.append(" ".join("".join(self._buf).split()))
            self._buf = None
        elif tag == "tr" and self._cells is not None:
            if self._cells:
                (self._rows if self._rows is not None else []).append(self._cells)
                (self._row_aligns if self._row_aligns is not None else []).append(
                    self._cell_aligns or []
                )
            self._cells = None


def _pipe_row(cells: list[str]) -> str:
    """一行 → GFM 管线行；单元格内的字面 ``|`` 转义，保证可被本模块反解。"""
    return "| " + " | ".join((c or "").replace("|", "\\|") for c in cells) + " |"


def table_html_to_pipe_text(table_html: str) -> str:
    """把一段 ``<table>`` HTML 还原成 Markdown 管道文字（多行文本）。

    - 单元格取纯文本（剥标签、``<br>`` → 空格、空白折叠；实体已解码）；
    - 首行后补 GFM 分隔行，列对齐取首行单元格的 ``ptoe-align-*`` class（无则 ``---``）；
    - 行宽不齐按最宽行补空单元格；``colspan``/``rowspan`` 不展开（按实际单元格输出）；
    - 非法输入或无表格时返回空串。
    """
    parser = _TableTextParser()
    try:
        parser.feed(str(table_html))
        parser.close()
    except Exception:  # noqa: BLE001 - 输入来自用户页面，容错优先
        return ""
    if not parser.tables:
        return ""
    rows = parser.tables[0]
    aligns = parser.aligns[0] if parser.aligns else []
    width = max((len(r) for r in rows), default=0)
    if width <= 0:
        return ""
    padded = [list(r) + [""] * (width - len(r)) for r in rows]
    first_aligns = list(aligns[0]) if aligns else []
    lines = [_pipe_row(padded[0])]
    lines.append(
        "| "
        + " | ".join(
            _ALIGN_MARK.get(first_aligns[c], "---")
            if c < len(first_aligns)
            else "---"
            for c in range(width)
        )
        + " |"
    )
    for row in padded[1:]:
        lines.append(_pipe_row(row))
    return "\n".join(lines)
