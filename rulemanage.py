"""
rulemanage.py — 格式规则服务端应用引擎（纯 stdlib）。

将前端 JS 的格式规则求值与应用逻辑迁移到 Python 端：
- 迷你 DOM 解析/序列化（基于 html.parser）
- 文本索引（收集文本节点及累计偏移，唯一文本基准）
- 条件求值（contains/prefix/suffix/regex；scope: selection/paragraph/page）
- 规则求值（mode=first/all；target=match/before/after/between；group_formats/match_formats）
- 应用引擎（倒序应用保证偏移有效；行内 op 拆分/包裹文本节点；块级 op 改标签/类；
  冲突模型 first-wins；merge 合并相邻块）

对外 API：
    apply_rules(html, rules, rule_id=None, all=False, sel_start=None, sel_end=None)
        -> (new_html: str, error: str|None)
"""

from __future__ import annotations

import html
import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

# 格式操作白名单（与前端 _VALID_FORMAT_OPS / FORMAT_RULE_OPTS 保持一致）
VALID_FORMAT_OPS = {
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
    "no_bold",
    "citation",
    "flush",
    "indent",
    "first_indent",
    "hang_indent",
    "strip_ws",
    # 2026-09 新增行内格式（7 项）
    "underline",
    "strike",
    "charbox",
    "shade",
    "highlight",
    "sup",
    "sub",
}

# 8 种行内包装格式类（2026-09-19）：单一事实来源。
# 前端（ui/app.js INLINE_CLASSES、ui/epubedit.js INLINE_CLASSES）、
# correctmanage (sanitize/导出 DOCX/MD)、rulemanage remove op、htmlmanage CSS 均与此保持一致。
INLINE_FORMAT_CLASSES = (
    "ptoe-underline",
    "ptoe-underdot",
    "ptoe-strike",
    "ptoe-charbox",
    "ptoe-shade",
    "ptoe-highlight",
    "ptoe-sup",
    "ptoe-sub",
)

# span 上允许保留的 class 全集（MiniDOMParser 白名单的单一事实来源）。
# = 8 个行内包装格式类（INLINE_FORMAT_CLASSES，构造时求值，永不漂移）
#   + 标记/注释/引用 + 行内 ptoe-align-*（前端部分选区对齐用 span 承载）。
# 字符格式（字号/字体/颜色）走 style 属性，不需要也不新增 class。
ALLOWED_SPAN_CLASSES = frozenset(INLINE_FORMAT_CLASSES) | {
    "ptoe-marker",
    "ptoe-note",
    "ptoe-citation",
    "ptoe-align-left",
    "ptoe-align-center",
    "ptoe-align-right",
}

# 空元素（void elements）——无闭合标签、无子节点
VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "base", "area", "col", "embed", "param", "source", "track", "wbr"}

# 序列化时允许的标签（与 sanitize_html 白名单兼容）
# 2026-09-15：补充 div —— 矫正界面未应用规则前的页面 HTML 就是 <div> 行块，
# 若排除 div 则 MiniDOMParser 把 div 当 __skip_ 透明容器、其内文本全部丢弃，
# 纯 div 页面（新打开的 OCR 页）规则匹配数为 0（静默失效）。div 语义与 p 相同
# （BLOCK_TAGS 已含 div），序列化原样保留。
# 2026-09-20：补充表格结构标签（table/thead/tbody/tfoot/tr/th/td）——矫正界面
# 插入表格后页面含表格 HTML，若不加入白名单，MiniDOMParser 同样把表格标签当
# __skip_ 透明容器、表格文本全部丢弃（规则匹配 0，静默失效）。表格标签不加入
# BLOCK_TAGS：规则文本匹配仍只针对段落级内容。
ALLOWED_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "div", "strong", "em", "br", "span", "img",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
}

# 块级标签
BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "div"}

# 段落设置 data-* 属性（缩进/间距/行距，导出 EPUB 时由 htmlmanage 转内联样式）
_INDENT_DATA_ATTRS = (
    "data-pl", "data-pr", "data-ind", "data-indv",
    "data-spb", "data-spa", "data-lh",
)
_INDENT_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_INDENT_MODES = ("first", "hang")


def _indent_data_valid(k: str, v: str) -> bool:
    if k == "data-ind":
        return v in _INDENT_MODES
    return bool(_INDENT_NUM_RE.match(v))


# =============================================================================
# 字符级样式（字号 / 字体 / 颜色）——单一事实来源（2026-09-27）
#
# 载体 = span 的 style 属性。最多四条声明，且**恒按下列固定顺序重新序列化**：
#     text-align ; font-size ; font-family ; color
# 白名单之外的任何 CSS 属性一律丢弃（position/top/letter-spacing/… 不存活），
# 延续 2026-09-19 定的 EPUB 便携性取舍：只放行可移植的字符格式子集。
#
# 放在 rulemanage 而非 correctmanage：correctmanage 已 import rulemanage，
# 反向 import 会成环。前端（ui/app.js）镜像同一契约（同样的属性顺序与白名单）。
# =============================================================================

# 声明顺序即序列化顺序（幂等的基础）
CHAR_STYLE_PROPS = ("text-align", "font-size", "font-family", "color")

# 允许的字体栈（10 项，序列化时原样输出）
CHAR_FONT_STACKS = (
    "宋体, SimSun, serif",
    "黑体, SimHei, sans-serif",
    "楷体, KaiTi, serif",
    "仿宋, FangSong, serif",
    "微软雅黑, Microsoft YaHei, sans-serif",
    "等线, DengXian, sans-serif",
    "serif",
    "sans-serif",
    "monospace",
    "cursive",
)
_CHAR_FONT_STACK_SET = frozenset(CHAR_FONT_STACKS)

# 字号上下限（像素）与对齐取值
CHAR_SIZE_MIN = 8
CHAR_SIZE_MAX = 72
CHAR_ALIGN_VALUES = ("left", "center", "right")

_FONT_SIZE_RE = re.compile(r"^(\d{1,4}(?:\.\d+)?)\s*px$", re.IGNORECASE)
# font-family 归一：去引号 + 折叠空白，使 '"黑体", SimHei, sans-serif' 也能命中白名单
_FONT_QUOTE_RE = re.compile(r"[\"']")
_FONT_WS_RE = re.compile(r"\s+")
# 颜色取值白名单。
# 与 correctmanage._COLOR_VALUE_RE **刻意同形**（# + 3-8 位十六进制，或 3-20 位颜色名）：
# 字符样式是既有清洗链的延伸而非新契约，若此处收窄为 #rgb/#rrggbb，
# <font color> 转换与 position 过滤等既有行为会出现分叉（同一属性两种规则），
# 且会推翻 test_correctmanage 既有的 color:red 断言。十六进制统一转小写，
# 颜色名原样透传；DOCX 导出侧另行只映射十六进制（命名色无法表达为 OOXML 枚举）。
CHAR_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$|^[a-zA-Z]{3,20}$")


def _canon_font_family(raw: str) -> str:
    """字体栈归一（去引号/折叠空白）；不在白名单则返回 ''。"""
    v = _FONT_QUOTE_RE.sub("", str(raw or ""))
    v = _FONT_WS_RE.sub(" ", v).strip()
    return v if v in _CHAR_FONT_STACK_SET else ""


def _canon_char_decl(prop: str, val: str) -> str:
    """校验并归一单条声明，返回归一后的值（非法返回 ''）。"""
    if prop == "text-align":
        v = val.strip().lower()
        return v if v in CHAR_ALIGN_VALUES else ""
    if prop == "font-size":
        m = _FONT_SIZE_RE.match(val.strip())
        if not m:
            return ""
        # 四舍五入到整数像素后钳制到 8..72
        px = int(round(float(m.group(1))))
        return f"{max(CHAR_SIZE_MIN, min(CHAR_SIZE_MAX, px))}px"
    if prop == "font-family":
        return _canon_font_family(val)
    if prop == "color":
        v = val.strip()
        if not CHAR_COLOR_RE.match(v):
            return ""
        # 十六进制统一小写（颜色名原样）
        return v.lower() if v.startswith("#") else v
    return ""


def normalize_char_style(raw: str) -> str:
    """原始 style 串 → 规范化后的字符样式串（无存活声明时返回 ""）。

    行为要点：
    - 只保留 CHAR_STYLE_PROPS 中的 4 条声明，其余 CSS 属性（含 position/top 等）丢弃；
    - 固定顺序 text-align ; font-size ; font-family ; color，分隔符 ';' 且无尾分号；
    - **幂等**：normalize_char_style(normalize_char_style(x)) == normalize_char_style(x)；
    - 同一属性重复出现时后者覆盖前者（与 CSS 层叠一致），输出只留一条。
    """
    vals: dict[str, str] = {}
    for decl in str(raw or "").split(";"):
        if not decl.strip():
            continue
        prop, sep, val = decl.partition(":")
        if not sep:
            continue
        key = prop.strip().lower()
        if key not in CHAR_STYLE_PROPS:
            continue
        canon = _canon_char_decl(key, val)
        if canon:
            vals[key] = canon
    return ";".join(f"{k}:{vals[k]}" for k in CHAR_STYLE_PROPS if k in vals)


def char_style_get(raw: str, prop: str) -> str:
    """取规范化字符样式中的单个声明值（不存在或非法返回 ''）。"""
    key = str(prop or "").strip().lower()
    if key not in CHAR_STYLE_PROPS:
        return ""
    norm = normalize_char_style(raw)
    prefix = f"{key}:"
    for part in norm.split(";"):
        if part.startswith(prefix):
            return part[len(prefix):]
    return ""


# 冲突组（照抄前端 FORMAT_OP_GROUPS / opsConflict）
FORMAT_OP_GROUPS = {
    "block_tag": ["p", "heading1", "heading2", "heading3", "heading4", "heading5", "heading6"],
    "align": ["align_left", "align_center", "align_right"],
    "merge": ["merge"],
    # 缩进模式互斥（2026-08-23）：顶格/缩进/首行缩进/悬挂缩进 同一规则链中先到先得
    "indent_mode": ["flush", "indent", "first_indent", "hang_indent"],
    # 上标/下标互斥（2026-09）：同一文本不能同时是上标和下标
    "sup_sub": ["sup", "sub"],
}


def op_group(op: str) -> str | None:
    """返回 op 所属冲突组名；不在任何组返回 None。"""
    for g, ops in FORMAT_OP_GROUPS.items():
        if op in ops:
            return g
    return None


def ops_conflict(a: str, b: str) -> bool:
    """判断两个格式操作是否冲突（first-wins 策略）。"""
    if a == b:
        return False
    if a == "remove" or b == "remove":
        return True
    ga, gb = op_group(a), op_group(b)
    if ga is None or gb is None:
        return False
    return ga == gb


def parse_regex_pattern(pattern: str) -> tuple[str, str]:
    """
    解析 /pattern/flags 语法（照抄前端 parseRegexPattern）。
    返回 (pattern, flags)。无斜杠包裹时按普通表达式处理。
    """
    m = re.match(r"^/(.+)/([a-z]*)$", pattern)
    if m:
        return m.group(1), m.group(2)
    return pattern, ""


# 编译正则缓存（同一规则的正则在 eval_condition 与 find_matches 中各编译一次）。
# 有界 LRU（OrderedDict + 锁）：频繁使用场景下不再反复整体 clear 后重新编译，
# 也不会无限增长；多线程 serve（ThreadingHTTPServer）下读写经锁保护。
_REGEX_CACHE: "OrderedDict[str, re.Pattern]" = OrderedDict()
_REGEX_CACHE_LOCK = threading.Lock()
_REGEX_CACHE_MAX = 256

# 灾难性回溯（ReDoS）启发式：量词内再套量词，如 (a+)+、(a*)*、(a?){2}、(?:ab+)+。
# 这类模式对超长文本可能指数级回溯，挂死同步 serve 线程（UI 冻结），直接拦截。
# 普通形态（(a){2}、(ab+)、(\\d{4})year 等）正确不误伤。
# 改进（2026-08）：仅当「捕获组内包含 非通配符 原子上的量词」且「该组外紧跟 +/* 量词」
# 才判定为嵌套量词灾难性回溯，否则放行。原启发式把 (.*?)?、([\\s\\S]*?)? 等
# 通配/可选形态一并误杀（如 `([\\s\\S]*)(日期)([\\s\\S]*?)(可选)?(注释)([\\s\\S]*)` 这类
# 合法规则被拒绝，报「无效或存在潜在性能风险」）。通配符（. / [\\s\\S] / 字符类）上的量词
# 不会引发指数级重划分，反向量词 ?（可选）也不产生重划分，故扫描放行。
# 说明：原单次正则（含负向回顾断言）在部分解释器报错「multiple repeat」，故改为显式扫描，
# 行为更可控：(.+)+、([\\s\\S]+)+、(.*?)+ 等指数级重划分仍判危险；(.*?)?、([\\s\\S]*?)? 等可选通配放行。
_DANGEROUS_PATTERN_MAX_LEN = 512

# 内层量词前一位若是这些字符（通配/类/自身量词/转义）则不视为危险原子
_WILD_PREV = set(".[]*+?\\")


def _inner_has_concrete_quant(pat: str, start: int, end: int) -> bool:
    """判断 [start, end) 区间内是否含「具体原子上的量词」（危险内层）。"""
    i = start
    while i < end:
        c = pat[i]
        if c == "\\":
            # 转义原子（\\d \\s \\w 等），其后紧跟量词即危险（\\d+、\\w+）
            if i + 2 < end and pat[i + 2] in "+*?":
                return True
            i += 2
            continue
        if c == "[":
            # 字符类：其后紧跟量词时，含 \\s/\\S（通配）则安全，[abc]+ 等具体则危险
            j = i + 1
            inner = ""
            while j < end and pat[j] != "]":
                if pat[j] == "\\":
                    inner += pat[j:j + 2]
                    j += 2
                else:
                    inner += pat[j]
                    j += 1
            if j + 1 < end and pat[j + 1] in "+*?":
                if "\\s" not in inner and "\\S" not in inner:
                    return True
            i = j + 1 if j < end else end
            continue
        if c in "+*?":
            # 量词：前一个有效字符若是具体原子则危险
            prev = pat[i - 1] if i > start else ""
            if prev and prev not in _WILD_PREV:
                return True
            i += 1
            continue
        i += 1
    return False


def _has_nested_quantifier(pat: str) -> bool:
    """组内具体原子量词 + 外层 +/* 才判危险（可选 ? 不引发重划分）。"""
    n = len(pat)
    stack: list[int] = []
    i = 0
    while i < n:
        c = pat[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            j = i + 1
            while j < n and pat[j] != "]":
                if pat[j] == "\\":
                    j += 2
                else:
                    j += 1
            i = j + 1 if j < n else n
            continue
        if c == "(":
            stack.append(i)
            i += 1
            continue
        if c == ")":
            if stack:
                open_idx = stack.pop()
                # 仅当 ) 后紧跟 + 或 *（可选 ? 不引发重划分）才检查内部结构
                if i + 1 < n and pat[i + 1] in ("+", "*"):
                    if _inner_has_concrete_quant(pat, open_idx + 1, i):
                        return True
            i += 1
            continue
        i += 1
    return False


def _is_dangerous(pattern: str) -> bool:
    """启发式检测可能灾难性回溯的正则（嵌套量词形态）；超长模式一并拦截。"""
    if len(pattern) > _DANGEROUS_PATTERN_MAX_LEN:
        return True
    return _has_nested_quantifier(pattern)


def _compile_cached(raw: str) -> re.Pattern:
    """带缓存的线程安全正则编译（eval_condition / find_matches / target 共享）。

    缓存有界：超过 _REGEX_CACHE_MAX 时按 LRU 淘汰最旧条目（原先整体 clear，
    频繁使用场景下每次请求都重新编译）。命中时 move_to_end 保持访问序。
    危险模式（嵌套量词）编译前拦截，抛 re.error 由调用方按既有 try/except 处理。
    """
    with _REGEX_CACHE_LOCK:
        pat = _REGEX_CACHE.get(raw)
        if pat is not None:
            _REGEX_CACHE.move_to_end(raw)
            return pat
        pattern, flags = parse_regex_pattern(raw)
        if _is_dangerous(pattern):
            raise re.error(f"表达式 {raw!r} 含嵌套量词（灾难性回溯风险），已拒绝执行")
        fl = 0
        if "i" in flags:
            fl |= re.IGNORECASE
        if "m" in flags:
            fl |= re.MULTILINE
        if "s" in flags:
            fl |= re.DOTALL
        pat = re.compile(pattern, fl)
        if len(_REGEX_CACHE) >= _REGEX_CACHE_MAX:
            _REGEX_CACHE.popitem(last=False)
        _REGEX_CACHE[raw] = pat
        return pat


def compile_pattern(raw: str) -> re.Pattern:
    """按引擎口径编译条件正则（对外入口）。

    - 支持 `/pattern/flags` 写法（flags: i/m/s），与 find_matches / eval_condition 完全一致；
    - 拦截灾难性回溯的危险模式；
    - 写法非法时抛 re.error，由调用方捕获。

    供服务端保存校验、匹配预览等复用：这些地方原先直接用裸 re.compile，
    会把 USAGE.md 里文档化的 `/pattern/flags` 写法判为非法而静默丢弃整条条件。
    """
    return _compile_cached(raw)


# =============================================================================
# 迷你 DOM：解析 → 树 → 序列化
# =============================================================================

# 注意：eq=False —— 节点带 parent 回指，dataclass 默认结构化 __eq__ 会在
# siblings.index() 等比较中沿 parent→children→兄弟 无限递归（RecursionError，
# 即使自比较也会成环）。DOM 节点相等性一律按对象身份（is）判定。
@dataclass(eq=False)
class TextNode:
    """文本节点。"""
    text: str = ""
    parent: "ElementNode | None" = None

    def to_html(self) -> str:
        return html.escape(self.text, quote=False)


@dataclass(eq=False)
class ElementNode:
    """元素节点。"""
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    parent: "ElementNode | None" = None

    def __post_init__(self):
        for child in self.children:
            child.parent = self

    def to_html(self) -> str:
        # 属性序列化：class 保序列表，其余按字典序（确定性）
        attr_parts = []
        for k, v in sorted(self.attrs.items()):
            if k == "class" and isinstance(v, list):
                v = " ".join(v)
            attr_parts.append(f'{k}="{html.escape(v, quote=True)}"')
        attr_str = " " + " ".join(attr_parts) if attr_parts else ""

        if self.tag in VOID_TAGS:
            return f"<{self.tag}{attr_str}/>"

        inner = "".join(child.to_html() for child in self.children)
        return f"<{self.tag}{attr_str}>{inner}</{self.tag}>"


Node = TextNode | ElementNode


class MiniDOMParser(HTMLParser):
    """基于 html.parser 的迷你 DOM 解析器。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = ElementNode("root")
        self.stack: list[ElementNode] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        # 归一化标签：b -> strong, i -> em
        if tag == "b":
            tag = "strong"
        elif tag == "i":
            tag = "em"
        # 只保留白名单标签；其余标签当作透明容器（仅保留文本内容）
        if tag not in ALLOWED_TAGS:
            # 空元素无闭合标签（html.parser 不会为其派发 handle_endtag），
            # 压入 __skip_ 后无法出栈 → 块内后续文本全被当透明内容丢弃。
            # 例：<p>前文<input type=hidden>后文</p> 的「后文」消失。
            if tag in VOID_TAGS:
                return
            # 记录一个透明标记，用于后续 handle_data 知道在非白名单标签内
            self.stack.append(ElementNode(f"__skip_{tag}"))
            return

        # 处理属性：class 保序列表，其余字符串
        attr_dict: dict[str, str | list[str]] = {}
        for k, v in attrs:
            if v is None:
                continue
            kl = k.lower()
            if kl == "class":
                attr_dict[kl] = v.split()
            else:
                attr_dict[kl] = v

        # img 特殊处理：只保留 src/alt/显示模式 class
        if tag == "img":
            allowed_img_attrs = {"src", "alt"}
            allowed_img_classes = {
                "ptoe-img-full", "ptoe-img-fit", "ptoe-img-inline",
                # 浮动绕排（2026-10 图文混合）：与 ptoe-img-inline 互斥，类挂 img 自身
                "ptoe-img-float-left", "ptoe-img-float-right",
                "ptoe-img-w25", "ptoe-img-w50", "ptoe-img-w75", "ptoe-img-w100",
                "ptoe-img-left", "ptoe-img-center", "ptoe-img-right",
                "ptoe-img-vtop", "ptoe-img-vmid", "ptoe-img-vbot",
            }
            new_attrs: dict[str, str | list[str]] = {}
            for k, v in attr_dict.items():
                if k in allowed_img_attrs:
                    new_attrs[k] = v
                elif k == "class" and isinstance(v, list):
                    filtered = [c for c in v if c in allowed_img_classes]
                    if filtered:
                        new_attrs[k] = filtered
            attr_dict = new_attrs
            # 如果没有 src，标记为丢弃
            if "src" not in attr_dict:
                self.stack.append(ElementNode(f"__skip_{tag}"))
                return

        # span 特殊处理：只保留 data-ptoe-marker、允许的 class 与经校验的字符样式
        # （2026-09-27 修复两处静默丢失）：
        #   ① style 曾被整条丢弃 —— 每应用一次格式规则就会抹掉 #colorBtn 产出的
        #      style="color:…" 以及逐字符字号/字体（现按 normalize_char_style 放行）；
        #   ② 允许类集合曾手写硬编码，遗漏 ptoe-underdot（明明在 INLINE_FORMAT_CLASSES）
        #      与行内 ptoe-align-*（前端对部分选区用行内 span 包裹对齐）——
        #      现从 INLINE_FORMAT_CLASSES 派生，构造时求值一次，永不再漂移。
        if tag == "span":
            new_attrs = {}
            for k, v in attr_dict.items():
                if k == "data-ptoe-marker":
                    new_attrs[k] = v
                elif k == "class" and isinstance(v, list):
                    filtered = [c for c in v if c in ALLOWED_SPAN_CLASSES]
                    if filtered:
                        new_attrs[k] = filtered
                elif k == "style":
                    # 字符样式：白名单校验 + 规范序列化；无存活声明则整条丢弃
                    norm = normalize_char_style(str(v))
                    if norm:
                        new_attrs[k] = norm
            attr_dict = new_attrs

        # p/h1-h6 保留 class（ptoe-note/ptoe-citation/ptoe-align-*/ptoe-flush/
        # ptoe-indent/ptoe-divider*/ptoe-caption）与段落设置 data-* 属性（缩进/间距/行距）
        if tag in BLOCK_TAGS:
            new_attrs = {}
            for k, v in attr_dict.items():
                if k == "class" and isinstance(v, list):
                    allowed = [
                        c for c in v
                        # 题注段落（2026-10-01）：图片块之后的独立 <p class="ptoe-caption">，
                        # 块级样式（不是行内格式，故不入 INLINE_FORMAT_CLASSES），
                        # 不加则每应用一次格式规则题注身份就被剥掉（与 ptoe-divider 同型丢失）
                        if c in {"ptoe-note", "ptoe-citation", "ptoe-flush", "ptoe-indent",
                                 "ptoe-caption"}
                        or c.startswith("ptoe-align-")
                        # 题注样式后缀类（2026-10-01 修正）：判定口径必须与
                        # _is_caption_block 逐字一致的**严格前缀**——基类已在上面的
                        # 精确集合里，本行只放行「ptoe-caption + 连字符 + 后缀」。
                        # 若用宽松 startswith("ptoe-caption")（分隔线 ptoe-divider 的
                        # 现状即如此），ptoe-captionx 这类恰好以此前缀开头、无连字符
                        # 分隔的无关 class 会被误判受保护；而 _is_caption_block 不保护
                        # 它 → 两侧口径分叉，题注保护形同虚设。
                        # 修前此处只做精确匹配，ptoe-caption-xxx 在**解析期**即被剥掉，
                        # 使 _is_caption_block 的前缀分支成为端到端死代码。
                        or c.startswith("ptoe-caption-")
                        # 分隔线段落（2026-09-27）：基类 + 样式后缀两个类都要留
                        or c.startswith("ptoe-divider")
                    ]
                    if allowed:
                        new_attrs[k] = allowed
                elif k in _INDENT_DATA_ATTRS:
                    # 解析器把非 class 属性存为 str（:194），list 仅在直接构造时出现——两者都收，
                    # 否则矫正界面「段落设置」写入的 data-ind 等会在应用规则时被剥掉
                    val = (" ".join(str(x) for x in v) if isinstance(v, list) else str(v)).strip()
                    if val and _indent_data_valid(k, val):
                        new_attrs[k] = val
            attr_dict = new_attrs

        # 归一化属性值类型
        norm_attrs: dict[str, str] = {}
        for k, v in attr_dict.items():
            if isinstance(v, list):
                norm_attrs[k] = " ".join(v)
            else:
                norm_attrs[k] = v

        el = ElementNode(tag, norm_attrs)
        self.stack[-1].children.append(el)
        el.parent = self.stack[-1]
        # 空元素不得入栈（img/br/hr…）。
        # 否则块内后续文本会成为其子节点，而 to_html 序列化空元素时直接丢弃
        # children（:468）→ 图片/换行之后的所有文字凭空消失：
        #   <p>前文<img src=..>后文</p>  →  <p>前文<img/></p>
        # 这不是边界情况：浏览器 innerHTML 序列化对 void 元素**永不**输出自闭合
        # 斜杠（jsdom 实测两种输入都得到 <img src="x">），而矫正界面格式规则
        # 走的正是 ed.innerHTML，故自闭合形态永远不会被送到本解析器。
        if tag not in VOID_TAGS:
            self.stack.append(el)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        # 归一化结束标签：b -> strong, i -> em
        if tag == "b":
            tag = "strong"
        elif tag == "i":
            tag = "em"
        if self.stack and self.stack[-1].tag == tag:
            self.stack.pop()
        elif self.stack and self.stack[-1].tag.startswith("__skip_"):
            self.stack.pop()
        # 其余情况忽略（容错）

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not data:
            return
        # 跳过非白名单标签内的文本（由 __skip_ 标记）
        if self.stack and self.stack[-1].tag.startswith("__skip_"):
            return
        text_node = TextNode(data)
        text_node.parent = self.stack[-1]
        self.stack[-1].children.append(text_node)

    def get_root(self) -> ElementNode:
        return self.root


def parse_html(html_text: str) -> ElementNode:
    """解析 HTML 字符串为迷你 DOM 树。"""
    parser = MiniDOMParser()
    parser.feed(html_text)
    return parser.get_root()


def _ensure_wrapped_in_block(root: ElementNode) -> None:
    """确保 root 的直接子节点都是块级元素（p/h1-h6）。裸文本节点包裹在 <p> 中。"""
    new_children = []
    i = 0
    while i < len(root.children):
        child = root.children[i]
        if isinstance(child, TextNode):
            # 收集连续的文本节点
            text_parts = [child.text]
            j = i + 1
            while j < len(root.children) and isinstance(root.children[j], TextNode):
                text_parts.append(root.children[j].text)
                j += 1
            combined = "".join(text_parts)
            if combined.strip():
                p = ElementNode("p")
                p.children.append(TextNode(combined))
                new_children.append(p)
            i = j
        elif isinstance(child, ElementNode) and child.tag in BLOCK_TAGS:
            new_children.append(child)
            i += 1
        else:
            # 其他元素（strong, em, span, img 等）包裹在 <p> 中
            p = ElementNode("p")
            p.children.append(child)
            new_children.append(p)
            i += 1
    root.children = new_children
    for c in new_children:
        c.parent = root


def serialize_html(node: Node) -> str:
    """序列化迷你 DOM 节点为 HTML 字符串（不包含 root 包装器）。"""
    if isinstance(node, ElementNode) and node.tag == "root":
        _ensure_wrapped_in_block(node)
        return "".join(child.to_html() for child in node.children)
    return node.to_html()


# =============================================================================
# 文本索引：收集文本节点及累计偏移
# =============================================================================

@dataclass
class TextNodeInfo:
    """文本节点信息：节点引用、在纯文本中的起止偏移。"""
    node: TextNode
    start: int
    end: int


def collect_text_nodes(root: ElementNode) -> tuple[str, list[TextNodeInfo]]:
    """
    遍历树收集所有文本节点及其在纯文本中的累计偏移。
    返回 (纯文本, [(node, start, end), ...])。
    这是唯一文本基准（textContent 口径），彻底消除 innerText/textContent 不一致类 bug。
    """
    text_parts: list[str] = []
    nodes_info: list[TextNodeInfo] = []
    offset = 0

    def walk(n: Node):
        nonlocal offset
        if isinstance(n, TextNode):
            length = len(n.text)
            if length > 0:
                text_parts.append(n.text)
                nodes_info.append(TextNodeInfo(n, offset, offset + length))
                offset += length
        elif isinstance(n, ElementNode):
            for child in n.children:
                walk(child)

    walk(root)
    return "".join(text_parts), nodes_info


def range_from_offsets(nodes_info: list[TextNodeInfo], start_off: int, end_off: int) -> tuple[TextNode | None, int, TextNode | None, int] | None:
    """
    根据字符偏移量定位到文本节点及节点内偏移。
    返回 (start_node, start_idx, end_node, end_idx) 或 None。
    """
    if start_off >= end_off or not nodes_info:
        return None
    start_node = None
    start_idx = 0
    end_node = None
    end_idx = 0
    for info in nodes_info:
        if start_node is None and info.end > start_off:
            start_node = info.node
            start_idx = start_off - info.start
        if info.end >= end_off:
            end_node = info.node
            end_idx = end_off - info.start
            break
    if start_node is None or end_node is None:
        return None
    # 钳制到节点长度内（防御性）
    start_idx = min(start_idx, len(start_node.text))
    end_idx = min(end_idx, len(end_node.text))
    return start_node, start_idx, end_node, end_idx


# =============================================================================
# 条件求值
# =============================================================================

@dataclass
class Condition:
    """格式规则条件（已校验、归一化）。"""
    type: str  # regex | contains | prefix | suffix
    pattern: str
    scope: str  # selection | paragraph | page
    formats: list[str]
    target: str = "match"  # match | before | after | between
    between_end_pattern: str = ""
    group_formats: list[list[str]] = field(default_factory=list)
    match_formats: list[list[str]] = field(default_factory=list)


def eval_condition(cond: Condition, text: str, selection: tuple[int, int] | None = None) -> bool:
    """
    评估单个条件是否匹配。
    - 空 pattern = 无条件（恒真）
    - scope=selection: 用选中区间 [sel_start:sel_end] 切片（越界钳制）
    - scope=paragraph: 这里无法直接获取段落文本，由调用方传入对应文本
    - scope=page: 整页文本
    """
    if not cond.pattern:
        return True  # 无条件恒匹配

    t = text or ""
    ctype = cond.type

    if ctype == "regex":
        try:
            return _compile_cached(cond.pattern).search(t) is not None
        except re.error:
            return False
    elif ctype == "contains":
        return cond.pattern in t
    elif ctype == "prefix":
        return t.startswith(cond.pattern)
    elif ctype == "suffix":
        return t.endswith(cond.pattern)
    return False


def find_matches(cond: Condition, text: str) -> list[re.Match]:
    """在文本中查找正则的所有匹配（全局匹配），返回 match 对象列表。"""
    if cond.type != "regex" or not cond.pattern:
        return []
    try:
        regex = _compile_cached(cond.pattern)
        return list(regex.finditer(text))
    except re.error:
        return []


# =============================================================================
# 规则求值
# =============================================================================

@dataclass
class Rule:
    """格式规则（已校验、归一化）。"""
    id: str
    name: str
    mode: str  # first | all
    conditions: list[Condition]


def rule_from_dict(d: dict[str, Any]) -> Rule:
    """从字典构建 Rule 对象（假设已通过 _validate_format_rules 校验）。"""
    conditions = []
    for c in d.get("conditions", []):
        conditions.append(Condition(
            type=c.get("type", "contains"),
            pattern=c.get("pattern", ""),
            scope=c.get("scope", "selection"),
            formats=[op for op in c.get("formats", []) if op in VALID_FORMAT_OPS],
            target=c.get("target", "match"),
            between_end_pattern=c.get("between_end_pattern", ""),
            group_formats=[[op for op in g if op in VALID_FORMAT_OPS] for g in c.get("group_formats", [])],
            match_formats=[[op for op in m if op in VALID_FORMAT_OPS] for m in c.get("match_formats", [])],
        ))
    return Rule(
        id=d.get("id", ""),
        name=d.get("name", ""),
        mode=d.get("mode", "first"),
        conditions=conditions,
    )


@dataclass
class EvalResult:
    """规则求值结果。"""
    fmt_entries: list[dict] = field(default_factory=list)  # [{fmts, page_scope}] - 无条件或 selection scope
    pattern_conds: list[Condition] = field(default_factory=list)  # contains/prefix/suffix/regex with formats
    group_conds: list[Condition] = field(default_factory=list)
    match_conds: list[Condition] = field(default_factory=list)
    target_conds: list[Condition] = field(default_factory=list)


def eval_format_rule(rule: Rule, page_text: str, selection: tuple[int, int] | None = None) -> EvalResult:
    """
    求值单条规则，返回应用计划。
    - mode=first: 首个匹配条件生效即停（none-only 条件=该处不处理守卫）
    - mode=all: 全部匹配条件按序各自应用
    - 条件分类（**三类格式可叠加，不再互斥**）：
      * target != match -> target_conds (before/after/between)
      * regex + group_formats -> group_conds（逐捕获组）
      * regex + match_formats -> match_conds（逐匹配）
      * contains/prefix/suffix/regex with formats -> pattern_conds（作用到全部匹配）
      * 无条件或 selection scope -> fmt_entries

    2026-09-13 修复：这三类此前用 `elif` 串联，导致只要条件带了 group_formats /
    match_formats（哪怕数组里只有一行填了格式、甚至全是空行），条件级 `formats`
    就被整条丢弃 —— 症状是「一条正则匹配到多个对象，却只有最后一个匹配对象被改了
    格式」。现在三类叠加生效：条件级 formats 作用于**全部匹配**，分组/逐匹配格式
    在其之上追加，互不吞并。
    """
    out = EvalResult()
    for cond in rule.conditions:
        # 根据 scope 确定评估文本
        eval_text = page_text
        if cond.scope == "selection" and selection:
            s, e = selection
            s = max(0, min(s, len(page_text)))
            e = max(0, min(e, len(page_text)))
            if s < e:
                eval_text = page_text[s:e]
            else:
                eval_text = ""
        # paragraph scope: 调用方需传入段落文本，这里简化为整页（前端 paragraph scope 也是基于光标所在块）
        # 实际 paragraph 语义在前端由选区决定，服务端无法精确复现，退回整页文本

        if not eval_condition(cond, eval_text):
            continue
        if cond.target != "match":
            # target 条件（之前/之后/两条件之间）在 first 模式下也不中断，全部求值
            out.target_conds.append(cond)
            continue

        has_group = cond.type == "regex" and bool(cond.group_formats)
        has_match = cond.type == "regex" and bool(cond.match_formats)
        if has_group:
            out.group_conds.append(cond)
        if has_match:
            out.match_conds.append(cond)

        if cond.formats:
            if cond.pattern:
                # 有模式的条件：放入 pattern_conds，后续按**每一个**匹配位置应用
                out.pattern_conds.append(cond)
            elif cond.scope == "selection" and not selection:
                # 无条件规则（空 pattern）+ 选区 scope 且无选区：整体范围无意义
                # （选区工具如「中标」无选区时不应用——否则「应用全部规则」会退化
                # 为整页范围，把全页段落都改成该工具的格式）。不算命中，不中断 first。
                continue
            else:
                fmts = [op for op in cond.formats if op != "none"]
                page_scope = (cond.scope == "page")
                out.fmt_entries.append({"fmts": fmts, "page_scope": page_scope})
        elif not has_group and not has_match:
            # 既无条件级格式、也无分组/逐匹配格式：维持原语义（无条件/守卫条目）
            if cond.scope == "selection" and not selection:
                continue
            out.fmt_entries.append({"fmts": [], "page_scope": cond.scope == "page"})

        if rule.mode == "first":
            break
    return out


# =============================================================================
# 应用引擎：DOM 变更操作
# =============================================================================

def _replace_node_in_parent(old_node: Node, new_nodes: list[Node]) -> None:
    """在父节点中替换旧节点为新节点列表。"""
    parent = old_node.parent
    if not parent:
        return
    try:
        idx = parent.children.index(old_node)
    except ValueError:
        return
    parent.children[idx:idx+1] = new_nodes
    for n in new_nodes:
        n.parent = parent


def apply_inline_format(nodes_info: list[TextNodeInfo], start_off: int, end_off: int, op: str) -> bool:
    """
    在指定偏移范围内应用行内格式（bold/italic/no_bold/remove/align/underline/strike/charbox/shade/highlight/sup/sub）。
    通过拆分/包裹文本节点实现。
    返回是否成功应用。
    """
    rng = range_from_offsets(nodes_info, start_off, end_off)
    if not rng:
        return False
    start_node, start_idx, end_node, end_idx = rng

    if start_node is None or end_node is None:
        return False

    if op == "bold":
        return _wrap_inline(nodes_info, start_node, start_idx, end_node, end_idx, "strong")
    elif op == "italic":
        return _wrap_inline(nodes_info, start_node, start_idx, end_node, end_idx, "em")
    elif op == "note":
        # note 作为行内格式：包裹在 <span class="ptoe-note"> 中
        return _wrap_inline(nodes_info, start_node, start_idx, end_node, end_idx, "span", {"class": "ptoe-note"})
    elif op == "citation":
        # 引用作为行内格式：包裹在 <span class="ptoe-citation"> 中（与块级类同名，
        # sanitize/导出链路的白名单已支持行内 span 保此类，见 :403-409/:849-850）
        return _wrap_inline(nodes_info, start_node, start_idx, end_node, end_idx, "span", {"class": "ptoe-citation"})
    elif op.startswith("align_"):
        # align_* 作为行内格式：用内联样式包裹，允许多个不同对齐在同一块内
        pos = op[6:]  # left/center/right
        return _wrap_inline(nodes_info, start_node, start_idx, end_node, end_idx, "span", {"style": f"text-align:{pos}"})
    elif op == "underline":
        # 下划线
        return _wrap_inline(nodes_info, start_node, start_idx, end_node, end_idx, "span", {"class": "ptoe-underline"})
    elif op == "strike":
        # 删除线
        return _wrap_inline(nodes_info, start_node, start_idx, end_node, end_idx, "span", {"class": "ptoe-strike"})
    elif op == "charbox":
        # 字符边框
        return _wrap_inline(nodes_info, start_node, start_idx, end_node, end_idx, "span", {"class": "ptoe-charbox"})
    elif op == "shade":
        # 底纹
        return _wrap_inline(nodes_info, start_node, start_idx, end_node, end_idx, "span", {"class": "ptoe-shade"})
    elif op == "highlight":
        # 突显
        return _wrap_inline(nodes_info, start_node, start_idx, end_node, end_idx, "span", {"class": "ptoe-highlight"})
    elif op == "sup":
        # 上标：先移除可能存在的下标，再包裹上标（互斥）
        _unwrap_inline_class(nodes_info, start_node, start_idx, end_node, end_idx, "ptoe-sub")
        return _wrap_inline(nodes_info, start_node, start_idx, end_node, end_idx, "span", {"class": "ptoe-sup"})
    elif op == "sub":
        # 下标：先移除可能存在的上标，再包裹下标（互斥）
        _unwrap_inline_class(nodes_info, start_node, start_idx, end_node, end_idx, "ptoe-sup")
        return _wrap_inline(nodes_info, start_node, start_idx, end_node, end_idx, "span", {"class": "ptoe-sub"})
    elif op == "no_bold":
        return _unwrap_inline(nodes_info, start_node, start_idx, end_node, end_idx, "strong")
    elif op == "remove":
        # 移除 strong/em 以及所有新增行内格式 span，保留标记 span 与 img
        _unwrap_inline(nodes_info, start_node, start_idx, end_node, end_idx, "strong")
        _unwrap_inline(nodes_info, start_node, start_idx, end_node, end_idx, "em")
        for cls in ("ptoe-note", "ptoe-citation") + INLINE_FORMAT_CLASSES:
            _unwrap_inline_class(nodes_info, start_node, start_idx, end_node, end_idx, cls)
        return True
    return False


def _wrap_inline(nodes_info: list[TextNodeInfo], start_node: TextNode, start_idx: int, end_node: TextNode, end_idx: int, tag: str, attrs: dict[str, str] | None = None) -> bool:
    """在范围内包裹标签。支持跨多个文本节点的范围。"""
    # 处理单文本节点情况
    if start_node is end_node:
        text = start_node.text
        before_text = text[:start_idx]
        middle_text = text[start_idx:end_idx]
        after_text = text[end_idx:]
        if not middle_text:
            return False
        parent = start_node.parent
        if not parent:
            return False
        new_children = []
        if before_text:
            new_children.append(TextNode(before_text))
        wrapper = ElementNode(tag, attrs or {}, [TextNode(middle_text)])
        new_children.append(wrapper)
        if after_text:
            new_children.append(TextNode(after_text))
        _replace_node_in_parent(start_node, new_children)
        return True

    # 跨节点情况：收集所有与 [start_off, end_off) 重叠的文本节点，逐个包裹重叠段
    # 先算出绝对偏移区间
    # 从 nodes_info 找到 start_node 和 end_node 的绝对偏移
    start_abs = None
    end_abs = None
    for info in nodes_info:
        if info.node is start_node:
            start_abs = info.start + start_idx
        if info.node is end_node:
            end_abs = info.start + end_idx
        if start_abs is not None and end_abs is not None:
            break
    if start_abs is None or end_abs is None or start_abs >= end_abs:
        return False

    # 找出所有重叠的节点（按文档顺序）
    overlapping: list[tuple[TextNode, int, int]] = []  # (node, node_rel_start, node_rel_end)
    for info in nodes_info:
        node_start = info.start
        node_end = info.end
        # 检查是否与 [start_abs, end_abs) 相交
        if node_end <= start_abs or node_start >= end_abs:
            continue
        # 相交：计算节点内相对偏移
        rel_start = max(0, start_abs - node_start)
        rel_end = min(len(info.node.text), end_abs - node_start)
        if rel_start < rel_end:
            overlapping.append((info.node, rel_start, rel_end))

    if not overlapping:
        return False

    # 倒序处理（从后往前），避免前面的替换影响后面节点的索引/父节点查找
    # 注意：每次替换后 nodes_info 会在 _apply_op 中刷新，但这里我们在单次 _wrap_inline 内部
    # 处理多个节点，所以需要小心。策略：收集所有要处理的节点及其相对偏移，
    # 然后对每个节点单独调用单节点包裹逻辑（复用上方逻辑）。
    success = False
    for node, rel_start, rel_end in reversed(overlapping):
        text = node.text
        before_text = text[:rel_start]
        middle_text = text[rel_start:rel_end]
        after_text = text[rel_end:]
        if not middle_text:
            continue
        parent = node.parent
        if not parent:
            continue
        new_children = []
        if before_text:
            new_children.append(TextNode(before_text))
        wrapper = ElementNode(tag, attrs or {}, [TextNode(middle_text)])
        new_children.append(wrapper)
        if after_text:
            new_children.append(TextNode(after_text))
        _replace_node_in_parent(node, new_children)
        success = True

    return success


def _unwrap_inline(nodes_info: list[TextNodeInfo], start_node: TextNode, start_idx: int, end_node: TextNode, end_idx: int, tag: str) -> bool:
    """在范围内解包 <strong> 或 <em>（移除标签保留内容）。"""
    # 收集范围内所有指定标签的元素节点
    affected_elements: list[ElementNode] = []

    def collect_elements(node: Node):
        if isinstance(node, ElementNode) and node.tag == tag:
            # 检查该元素是否与范围相交
            # 简化：收集所有匹配标签，后续统一展平
            affected_elements.append(node)
        elif isinstance(node, ElementNode):
            for child in node.children:
                collect_elements(child)

    # 从根节点开始收集
    root = start_node
    while root.parent:
        root = root.parent
    collect_elements(root)

    for el in affected_elements:
        # 展平：将子节点移到父节点中
        parent = el.parent
        if not parent:
            continue
        try:
            idx = parent.children.index(el)
        except ValueError:
            continue
        parent.children[idx:idx+1] = el.children
        for c in el.children:
            c.parent = parent
    return True


def _unwrap_inline_class(nodes_info: list[TextNodeInfo], start_node: TextNode, start_idx: int, end_node: TextNode, end_idx: int, cls: str) -> bool:
    """在范围内解包指定 class 的 <span>（移除标签保留内容）。用于清除行内格式。"""
    affected_elements: list[ElementNode] = []

    def collect_elements(node: Node):
        if isinstance(node, ElementNode) and node.tag == "span":
            classes = node.attrs.get("class", "").split()
            if cls in classes:
                affected_elements.append(node)
        elif isinstance(node, ElementNode):
            for child in node.children:
                collect_elements(child)

    root = start_node
    while root.parent:
        root = root.parent
    collect_elements(root)

    for el in affected_elements:
        parent = el.parent
        if not parent:
            continue
        try:
            idx = parent.children.index(el)
        except ValueError:
            continue
        parent.children[idx:idx+1] = el.children
        for c in el.children:
            c.parent = parent
    return True


def _blocks_text_extent(nodes_info: list[TextNodeInfo], blocks: list[ElementNode]) -> tuple[int | None, int | None]:
    """计算 blocks 覆盖的纯文本区间 [first_start, last_end)（2026-09-22 新增）。

    每个文本节点沿 .parent 链向上查找是否落在任一 block（按 id() 身份比较）；
    块内直属文本与嵌套行内元素（span/strong/em 等）内的文本都计入。
    返回 (first_start, last_end)，即含文本块的最早 start / 最晚 end；
    没有任何文本节点落在给定块内时返回 (None, None)。
    """
    block_ids = {id(b) for b in blocks}
    first_start: int | None = None
    last_end: int | None = None
    for info in nodes_info:
        cur: Node | None = info.node
        while cur is not None:
            if id(cur) in block_ids:
                break
            cur = cur.parent
        if cur is None:
            continue  # 该文本节点不在任何目标块内
        if first_start is None or info.start < first_start:
            first_start = info.start
        if last_end is None or info.end > last_end:
            last_end = info.end
    return first_start, last_end


def _is_divider_block(el: ElementNode | None) -> bool:
    """块是否带 ptoe-divider class（分隔线段落，2026-09-27）。

    class 有两个：基类 ptoe-divider + 样式后缀 ptoe-divider-solid/dashed/dotted/double。
    判定用「前缀 + 空或连字符」：既让未知后缀（ptoe-divider-wavy，将来新增线型）
    也算分隔线（规则不得改写版式元素），又不会把 ptoe-dividerless 这类
    恰好以此前缀开头、无连字符分隔的无关 class 误判进来。
    """
    if el is None or not isinstance(el, ElementNode):
        return False
    for c in (el.attrs.get("class") or "").split():
        if c == "ptoe-divider" or c.startswith("ptoe-divider-"):
            return True
    return False


def _is_caption_block(el: ElementNode | None) -> bool:
    """块是否带 ptoe-caption class（图片题注段落，2026-10-01）。

    形态 = 图片块之后的独立说明段落 <p class="ptoe-caption">图1 示例插图</p>
    （契约见 correctmanage._CAPTION_CLASS / htmlmanage._CAPTION_CLASS），
    与分隔线一样是**用户手动放置的版式元素**，但对标的是配图而非正文：
    题注-图片是一对，拆开或搬走就失去意义。

    判定口径与 _is_divider_block 严格一致（「精确相等 或 前缀+连字符」）：
      ① 让将来的样式后缀（ptoe-caption-xxx）也受保护；
      ② 不会把 ptoe-captionx 这类恰好以此前缀开头、无连字符分隔的无关 class
         误判为题注。注意解析侧块级白名单用的是宽松 startswith
         （handle_starttag 的 BLOCK_TAGS 分支），故本判定必须比它更严。
    """
    if el is None or not isinstance(el, ElementNode):
        return False
    for c in (el.attrs.get("class") or "").split():
        if c == "ptoe-caption" or c.startswith("ptoe-caption-"):
            return True
    return False


def apply_block_format(
    root: ElementNode,
    nodes_info: list[TextNodeInfo],
    start_off: int,
    end_off: int,
    op: str,
    block_conflicts: dict[int, set[str]] | None = None,
    ignore_block_conflicts: bool = False,
    selection_driven: bool = False,
) -> bool:
    """
    在指定偏移范围所在的块级元素上应用块级格式。
    op: p, heading1-6, note, citation, align_left/center/right, merge
    ignore_block_conflicts: 用于 match_formats，允许同块多匹配各自独立应用格式
    selection_driven: 区间源自用户选区（scope=selection）时为 True——引用/注释
        选区只覆盖块的一部分时改走行内 span，避免整段变色（2026-09-22）。
    """
    rng = range_from_offsets(nodes_info, start_off, end_off)
    if not rng:
        return False
    start_node, _, end_node, _ = rng

    # 找到包含起始节点的块级祖先
    def find_block_ancestor(node: TextNode) -> ElementNode | None:
        cur: Node | None = node
        while cur and cur.parent:
            if isinstance(cur.parent, ElementNode) and cur.parent.tag in BLOCK_TAGS:
                return cur.parent
            cur = cur.parent
        return None

    start_block = find_block_ancestor(start_node)
    end_block = find_block_ancestor(end_node)
    if not start_block:
        return False

    # 分隔线段落（ptoe-divider，2026-09-27）：整块就是一行字形，是用户手动插入的
    # 版式元素。任何块级格式（标题/对齐/缩进/合并）都不得作用其上——否则一条恰好
    # 命中字形行的正则会把分隔线变成标题，或把它与相邻段落 merge 掉。
    # 题注段落（ptoe-caption，2026-10-01）同受此保护，处理方式与分隔线**完全一致**
    # （一律跳过、返回 False、不改写不合并）：一条恰好命中「图1」这类题注文字的
    # 正则会把题注变成 <h1>（进而进入 EPUB 目录）并让题注-配图关系失效。
    if _is_divider_block(start_block) or _is_caption_block(start_block):
        return False

    # 对于 merge 操作：将选区覆盖的全部块（start_block..end_block）合并为一段
    # （与前端 _mergeSelectedBlocks 语义一致）；选区未跨块时退化为合并下一个兄弟块。
    if op == "merge":
        if not start_block.parent:
            return False
        # 收集受影响的块（从 start_block 到 end_block，与下方通用收集逻辑一致）
        # 遇到分隔线即停（2026-09-27）：分隔线是独立版式元素，合并既不能吞掉它，
        # 也不能跨过它把两侧正文拼到一起（那会改变文段顺序）
        # 题注同样「即停」（2026-10-01）：跨过题注合并会把题注留在原地而把其配图
        # 与说明拆到不同段落（题注失去指代对象），或反过来吞掉题注使图片无说明。
        merge_blocks: list[ElementNode] = []
        cur_block = start_block
        while cur_block and not (_is_divider_block(cur_block) or _is_caption_block(cur_block)):
            merge_blocks.append(cur_block)
            if cur_block is end_block:
                break
            if not cur_block.parent:
                break
            siblings = cur_block.parent.children
            try:
                idx = siblings.index(cur_block)
            except ValueError:
                break
            nxt = None
            for i in range(idx + 1, len(siblings)):
                sib = siblings[i]
                if isinstance(sib, ElementNode) and sib.tag in BLOCK_TAGS:
                    nxt = sib
                    break
            cur_block = nxt
        # 选区未跨块（仅 start_block 自身）：退化为合并下一个块级兄弟
        if len(merge_blocks) < 2:
            siblings = start_block.parent.children
            try:
                idx = siblings.index(start_block)
            except ValueError:
                return False
            next_block = None
            for i in range(idx + 1, len(siblings)):
                sib = siblings[i]
                if isinstance(sib, ElementNode) and sib.tag in BLOCK_TAGS:
                    # 紧邻兄弟就是分隔线 → 无可合并目标（不跨线合并）
                    # 题注同理（2026-10-01）：紧邻题注不合并——把题注并进上一段
                    # 会让「图片 → 题注」的对应关系断开
                    if _is_divider_block(sib) or _is_caption_block(sib):
                        return False
                    next_block = sib
                    break
            if not next_block:
                return False
            merge_blocks.append(next_block)
        # 合并：后续块内容依次追加到首块（块间补空格），再移除后续块
        first = merge_blocks[0]
        for blk in merge_blocks[1:]:
            if first.children and blk.children:
                first.children.append(TextNode(" "))
            while blk.children:
                first.children.append(blk.children.pop(0))
            if blk.parent:
                try:
                    blk.parent.children.remove(blk)
                except ValueError:
                    pass
        return True

    # 收集受影响的块（从 start_block 到 end_block）
    blocks: list[ElementNode] = []
    cur_block = start_block
    while cur_block:
        blocks.append(cur_block)
        if cur_block is end_block:
            break        # 找下一个兄弟块
        if not cur_block.parent:
            break
        siblings = cur_block.parent.children
        try:
            idx = siblings.index(cur_block)
        except ValueError:
            break
        nxt = None
        for i in range(idx + 1, len(siblings)):
            sib = siblings[i]
            if isinstance(sib, ElementNode) and sib.tag in BLOCK_TAGS:
                nxt = sib
                break
        cur_block = nxt

    # 跨块区间里的分隔线块同样排除（start_block 已单独守卫，这里补中间块）
    # 题注同理排除（2026-10-01）：跨块区间若只选中首尾而中间压着题注，题注不能
    # 跟着被改写（改标签 → 变成 h1 进目录；加类 → 破坏题注版式）。
    # 与分隔线同口径：只从受影响块列表里剔除，区间其余块照常应用。
    blocks = [b for b in blocks if not _is_divider_block(b) and not _is_caption_block(b)]
    if not blocks:
        return False

    # per-block first-wins 冲突（2026-08）：同一块内先到先得，不同块互不影响。
    # 原实现用全局 applied_ops/_seen_groups 跟踪，导致「匹配对象分别设置了独立格式」
    # 时，第二个匹配块因同组已全局命中而被错误跳过（如两段各设 heading1 只剩一段）。
    # match_formats (ignore_block_conflicts=True) 例外：同块多匹配各自独立应用。
    og = op_group(op)
    if not ignore_block_conflicts and block_conflicts is not None and og is not None:
        # per-block first-wins：仅跳过已命中同一冲突组的块，不因个别块冲突而放弃整段
        # （不同块互不影响——原 any() 检查会让多块区间中一个冲突块拖垮其余合法块）。
        filtered = [b for b in blocks if og not in block_conflicts.get(id(b), ())]
        if not filtered:
            return False
        blocks = filtered

    # 引用/注释选区只覆盖块的一部分 → 行内 span 包裹（2026-09-22 修复：原实现整块加类，
    # 选中段内几个字会把整个段落变为引用/注释样式，未选中内容格式被破坏）。
    # 仅 selection_driven（scope=selection 的用户选区）触发；page 作用域的正则/contains
    # 匹配区间仍走块级类（保持 test_note_idempotent_multiple_matches 等既有语义：匹配
    # 落在同一段落时块类幂等添加一次，而不是逐匹配拆成多个行内 span）。
    if op in ("citation", "note") and selection_driven:
        first_start, last_end = _blocks_text_extent(nodes_info, blocks)
        if first_start is not None and last_end is not None and (start_off > first_start or end_off < last_end):
            return apply_inline_format(nodes_info, start_off, end_off, op)

    if op == "p":
        for b in blocks:
            b.tag = "p"
    elif op.startswith("heading"):
        level = op[7:]  # "1"-"6"
        for b in blocks:
            b.tag = f"h{level}"
    elif op == "note":
        for b in blocks:
            classes = b.attrs.get("class", "").split()
            if "ptoe-note" not in classes:
                classes.append("ptoe-note")
            b.attrs["class"] = " ".join(classes)
    elif op == "citation":
        for b in blocks:
            classes = b.attrs.get("class", "").split()
            if "ptoe-citation" not in classes:
                classes.append("ptoe-citation")
            b.attrs["class"] = " ".join(classes)
    elif op.startswith("align_"):
        pos = op[6:]  # left/center/right
        for b in blocks:
            classes = b.attrs.get("class", "").split()
            classes = [c for c in classes if not c.startswith("ptoe-align-")]
            classes.append(f"ptoe-align-{pos}")
            b.attrs["class"] = " ".join(classes)
    elif op in ("flush", "indent"):
        # 顶格/缩进互斥：先剥掉两者再追加目标类
        target = "ptoe-flush" if op == "flush" else "ptoe-indent"
        for b in blocks:
            classes = b.attrs.get("class", "").split()
            classes = [c for c in classes if c not in ("ptoe-flush", "ptoe-indent")]
            if target not in classes:
                classes.append(target)
            b.attrs["class"] = " ".join(classes)
    elif op in ("first_indent", "hang_indent"):
        # 首行/悬挂缩进（2026-08-23）：写 data-ind/data-indv 属性，导出 EPUB 时由
        # htmlmanage._indent_style_attrs 转内联样式；与顶格/缩进类互斥（indent_mode 组）
        mode = "first" if op == "first_indent" else "hang"
        for b in blocks:
            classes = b.attrs.get("class", "").split()
            classes = [c for c in classes if c not in ("ptoe-flush", "ptoe-indent")]
            b.attrs["class"] = " ".join(classes)
            b.attrs["data-ind"] = mode
            b.attrs["data-indv"] = "2"
    elif op == "strip_ws":
        # 去空：遍历块内所有文本节点，移除空白字符但保留换行 \n
        for b in blocks:
            def walk_text_nodes(node: Node):
                if isinstance(node, TextNode):
                    # 移除所有空白字符（空格、制表符、全角空格等），保留 \n
                    node.text = re.sub(r'[^\S\n]+', '', node.text)
                elif isinstance(node, ElementNode):
                    for child in node.children:
                        walk_text_nodes(child)
            walk_text_nodes(b)
    else:
        return False

    if not ignore_block_conflicts and block_conflicts is not None and og is not None:
        for b in blocks:
            block_conflicts.setdefault(id(b), set()).add(og)
    return True


# =============================================================================
# 主应用函数
# =============================================================================

def apply_rules(
    html: str,
    rules: list[dict[str, Any]],
    rule_id: str | None = None,
    all_rules: bool = False,
    sel_start: int | None = None,
    sel_end: int | None = None,
) -> tuple[str, str | None]:
    """
    应用格式规则到 HTML。

    参数:
        html: 输入 HTML 字符串（单页内容）
        rules: 规则列表（已通过 _validate_format_rules 校验）
        rule_id: 单条规则 ID（all_rules=False 时使用）
        all_rules: 是否应用所有规则（按列表顺序）
        sel_start: 选区起始偏移（相对整页纯文本）
        sel_end: 选区结束偏移

    返回:
        (new_html, error_msg_or_None)
    """
    try:
        # 解析 HTML
        root = parse_html(html)
        page_text, nodes_info = collect_text_nodes(root)

        # 选区归一化
        selection = None
        if sel_start is not None and sel_end is not None:
            s = max(0, min(sel_start, len(page_text)))
            e = max(0, min(sel_end, len(page_text)))
            if s < e:
                selection = (s, e)

        # 构建 Rule 对象列表
        rule_objs = [rule_from_dict(r) for r in rules]

        # 确定要应用的规则
        target_rules = rule_objs
        if not all_rules and not rule_id:
            # 2026-08-23 修复：单条应用请求缺 rule_id 时不再静默回退为「应用全部」，
            # 否则前端漏传 id 会把单条规则的建议应用到全局。
            return html, "缺少 rule_id（单条应用必须指定规则）"
        if not all_rules and rule_id:
            target_rules = [r for r in rule_objs if r.id == rule_id]
            if not target_rules:
                return html, f"规则不存在: {rule_id}"

        # 正则模式预检：灾难性回溯（嵌套量词）等危险模式直接拦截，返回中文错误提示
        # （经 /api/format_rules/apply → 400 → 前端错误 toast），避免同步 serve 线程
        # 被指数级回溯挂死（UI 冻结）。语法错误的正则也在此显式报错而非静默无操作。
        for r in target_rules:
            for c in r.conditions:
                if c.type == "regex" and c.pattern:
                    try:
                        _compile_cached(c.pattern)
                    except re.error as e:
                        return html, f"正则表达式无效或存在潜在性能风险：{e}"
                if c.between_end_pattern:
                    try:
                        _compile_cached(c.between_end_pattern)
                    except re.error as e:
                        return html, f"正则表达式无效或存在潜在性能风险：{e}"

        # 应用规则（按列表顺序，跨规则累计 applied_ops 实现 first-wins）
        applied_ops: list[str] = []
        # 按块维度的 first-wins 冲突记录（id(block) -> {group}）：
        # 同一块内同名/同组格式先到先得，不同块互不影响——修复「匹配对象分别设置了独立
        # 格式之后不能正常应用」的问题。
        block_conflicts: dict[int, set[str]] = {}

        for rule in target_rules:
            # 求值
            eval_result = eval_format_rule(rule, page_text, selection)

            # 1. target_conds (before/after/between)
            for cond in eval_result.target_conds:
                _apply_target_formats(root, nodes_info, cond, page_text, selection, applied_ops, block_conflicts)

            # 2. group_conds (regex + group_formats)
            for cond in eval_result.group_conds:
                _apply_group_formats(root, nodes_info, cond, page_text, selection, applied_ops, block_conflicts)

            # 3. match_conds (regex + match_formats)
            for cond in eval_result.match_conds:
                _apply_match_formats(root, nodes_info, cond, page_text, selection, applied_ops, block_conflicts)

            # 4. pattern_conds (contains/prefix/suffix/regex with formats)
            for cond in eval_result.pattern_conds:
                _apply_pattern_conds(root, nodes_info, cond, page_text, selection, applied_ops, block_conflicts)

            # 5. fmt_entries (普通格式：无条件或 selection scope)
            for entry in eval_result.fmt_entries:
                fmts = entry["fmts"]
                page_scope = entry["page_scope"]
                _apply_fmt_entry(root, nodes_info, fmts, page_scope, page_text, selection, applied_ops, block_conflicts)

        # 序列化结果
        new_html = serialize_html(root)
        return new_html, None

    except Exception as e:
        return html, f"应用格式规则失败: {e}"


# 行内格式操作白名单（不同应用路径对 note 的处理不同：
# pattern/target/fmt_entry 视 note 为块级（改 span.ptoe-note 类），
# match/group 视 note 为行内（_wrap_inline 包 span.ptoe-note））
# align_* 已移至块级：text-align 对 inline span 无效，改用 ptoe-align-* 类
# 新增 7 项行内格式（2026-09）：underline/strike/charbox/shade/highlight/sup/sub
_INLINE_LEAF_OPS = {"bold", "italic", "no_bold", "remove", "underline", "strike", "charbox", "shade", "highlight", "sup", "sub"}

_INLINE_LEAF_OPS_M = {"bold", "italic", "no_bold", "remove", "note", "underline", "strike", "charbox", "shade", "highlight", "sup", "sub"}


def _selection_bounds(
    cond: Condition, selection: tuple[int, int] | None, page_len: int
) -> tuple[int, int] | None:
    """scope=selection 且存在合法选区时，返回钳制到页面范围的选区区间；否则 None。"""
    if cond.scope == "selection" and selection:
        s, e = selection
        return max(0, min(s, page_len)), max(0, min(e, page_len))
    return None


def _apply_op(
    root: ElementNode,
    nodes_info: list[TextNodeInfo],
    start_off: int,
    end_off: int,
    op: str,
    inline_ops: set[str] = _INLINE_LEAF_OPS,
    block_conflicts: dict[int, set[str]] | None = None,
    ignore_block_conflicts: bool = False,
    selection_driven: bool = False,
) -> bool:
    """应用单个格式操作；成功后就地刷新 nodes_info。

    绝对文本偏移在行内包裹/块级改标签/合并等操作下不变（文本内容从未变化），
    变的只是"节点→偏移”映射。不在每次成功修改后刷新 nodes_info，后续匹配会
    命中已脱离树的旧节点（parent.children.index 抛 ValueError 被静默吞掉），
    症状：同一节点内多个匹配只有最后一个被格式化（匹配对象混乱）。

    selection_driven: 区间是否源自用户选区（scope=selection）。块级引用/注释
    仅在 selection_driven 且区间只覆盖块的一部分时转为行内 span（2026-09-22）；
    page 作用域的正则/contains 匹配区间仍走块级类，保持既有幂等语义。
    """
    if op in inline_ops:
        ok = apply_inline_format(nodes_info, start_off, end_off, op)
    else:
        ok = apply_block_format(root, nodes_info, start_off, end_off, op, block_conflicts, ignore_block_conflicts, selection_driven)
    if ok:
        nodes_info[:] = collect_text_nodes(root)[1]
    return ok


def _pattern_match_ranges(cond: Condition, page_text: str) -> list[tuple[int, int]]:
    """返回条件全部匹配区间（contains/prefix/suffix/regex 统一为 (start,end) 对）。

    替代原先 _apply_pattern_conds 内重复定义的局部 Match 类（同名遮蔽 re.Match，
    pyrefly 报错），并统一 selection 过滤语义。
    """
    if cond.type == "regex":
        return [m.span() for m in find_matches(cond, page_text)]
    if cond.type == "contains":
        if not cond.pattern:
            return []
        out: list[tuple[int, int]] = []
        start = 0
        while True:
            idx = page_text.find(cond.pattern, start)
            if idx == -1:
                break
            out.append((idx, idx + len(cond.pattern)))
            start = idx + 1
        return out
    if cond.type == "prefix":
        if not cond.pattern:
            return []
        if page_text.startswith(cond.pattern):
            return [(0, len(cond.pattern))]
        return []
    if cond.type == "suffix":
        if not cond.pattern:
            return []
        if page_text.endswith(cond.pattern):
            start = len(page_text) - len(cond.pattern)
            return [(start, len(page_text))]
        return []
    return []


def _apply_target_formats(
    root: ElementNode,
    nodes_info: list[TextNodeInfo],
    cond: Condition,
    page_text: str,
    selection: tuple[int, int] | None,
    applied_ops: list[str],
    block_conflicts: dict[int, set[str]] | None = None,
) -> None:
    """应用 target=before/after/between 的格式（匹配须在选区内，范围钳制到选区）。"""
    if cond.type != "regex" or not cond.pattern:
        return
    matches = find_matches(cond, page_text)
    sel = _selection_bounds(cond, selection, len(page_text))
    if sel:
        s, e = sel
        matches = [m for m in matches if m.start() >= s and m.end() <= e]
    if not matches:
        return
    match = matches[0]  # 取第一个匹配
    match_start, match_end = match.span()

    range_start, range_end = -1, -1
    if cond.target == "before":
        range_start, range_end = 0, match_start
    elif cond.target == "after":
        range_start, range_end = match_end, len(page_text)
    elif cond.target == "between":
        if not cond.between_end_pattern:
            return
        try:
            end_match = _compile_cached(cond.between_end_pattern).search(page_text, match_end)
            if not end_match:
                return
            range_start, range_end = match_end, end_match.start()
        except re.error:
            return

    if sel:
        s, e = sel
        range_start = max(range_start, s)
        range_end = min(range_end, e)
    if range_start < 0 or range_end <= range_start:
        return

    fmts = [op for op in cond.formats if op != "none"]
    # 块级冲突改由 apply_block_format 按块 first-wins 处理（不同块互不影响）；
    # 此处仅保留 remove 与任意已应用操作冲突的全局语义。
    _has_remove = "remove" in applied_ops
    for op in fmts:
        if op == "remove" and applied_ops:
            continue
        if _has_remove:
            continue
        _apply_op(root, nodes_info, range_start, range_end, op, _INLINE_LEAF_OPS, block_conflicts)
        applied_ops.append(op)
        if op == "remove":
            _has_remove = True


def _apply_group_formats(
    root: ElementNode,
    nodes_info: list[TextNodeInfo],
    cond: Condition,
    page_text: str,
    selection: tuple[int, int] | None,
    applied_ops: list[str],
    block_conflicts: dict[int, set[str]] | None = None,
) -> None:
    """应用 regex + group_formats（捕获组独立格式）。

    按绝对文本偏移逐捕获组应用格式（match.regs），同一匹配内按起始偏移倒序
    （高偏移先应用，低偏移不受影响）；每个操作成功后就地刷新 nodes_info
    （_apply_op），保证同节点多匹配、跨匹配的偏移始终有效。

    原先的“pretty 路径”（temp_root 重新解析拼接 HTML 替换文本节点）在新节点
    替换后不再刷新节点索引，同节点多匹配时后续匹配命中已脱离树的旧节点，
    前序匹配及其前后文本整体丢失（只剩最后一个匹配生效），已删除。
    """
    if cond.type != "regex" or not cond.pattern or not cond.group_formats:
        return
    matches = find_matches(cond, page_text)
    sel = _selection_bounds(cond, selection, len(page_text))
    if sel:
        s, e = sel
        matches = [m for m in matches if m.start() >= s and m.end() <= e]
    # 块级冲突改由 apply_block_format 按块 first-wins 处理（不同匹配块互不影响）；
    # 此处不再做全局格式组过滤，保证「匹配对象分别设置了独立格式」都能正常应用。
    # 倒序应用，保证偏移有效
    for match in reversed(matches):
        # 收集该匹配的所有组格式操作
        group_ops = []  # [(group_start, group_end, fmts)]
        for gi, fmts in enumerate(cond.group_formats):
            fmts = [op for op in fmts if op != "none"]
            if not fmts:
                continue
            if gi + 1 < len(match.regs) and match.regs[gi + 1][0] >= 0:
                group_start, group_end = match.regs[gi + 1]
            else:
                continue
            # 修剪区间两端孤立的换行符（<p> 块间 \n 是 root 直属 TextNode，
            # find_block_ancestor 返回 None，end_block=None 会导致块收集遍历
            # 后续全部兄弟块，格式错误扩散到不属于该组的块上）
            while group_start < group_end and page_text[group_start] in '\n\r':
                group_start += 1
            while group_end > group_start and page_text[group_end - 1] in '\n\r':
                group_end -= 1
            if group_start >= group_end:
                continue  # 修剪后范围为空，跳过
            if fmts:
                group_ops.append((group_start, group_end, fmts))

        # 组区间按起始偏移倒序应用（文本内容不变，绝对偏移恒有效）
        for group_start, group_end, fmts in sorted(
            group_ops, key=lambda x: x[0], reverse=True
        ):
            for op in fmts:
                # match_formats 和 group_formats 例外：同块多范围各自独立应用格式
                # align_* 例外（2026-08）：组区间可能因块内部分覆盖（如 `## 注释`
                # 块中「注释」是组5 而 `## ` 前缀落入组4 区间）把相邻组的目标块拉进
                # 本组对齐。对齐是块级属性，同块应按偏移倒序先到先得——高偏移组
                # （更靠近块尾/更特异）先应用并记录冲突，低偏移组在同块被跳过，
                # 避免「注释」等尾部标签被前一组右对齐污染。
                ignore_cf = not op.startswith("align_")
                _apply_op(root, nodes_info, group_start, group_end, op, _INLINE_LEAF_OPS_M, block_conflicts, ignore_cf)
                applied_ops.append(op)


def _apply_match_formats(
    root: ElementNode,
    nodes_info: list[TextNodeInfo],
    cond: Condition,
    page_text: str,
    selection: tuple[int, int] | None,
    applied_ops: list[str],
    block_conflicts: dict[int, set[str]] | None = None,
) -> None:
    """应用 regex + match_formats（逐匹配独立格式）。"""
    if cond.type != "regex" or not cond.pattern or not cond.match_formats:
        return
    matches = find_matches(cond, page_text)
    # 收集所有匹配的格式操作（match_formats[mi] 对应第 mi 个匹配，索引必须先于选区过滤）
    all_ops = []  # [(match_start, match_end, fmts)]
    for mi, match in enumerate(matches):
        if mi >= len(cond.match_formats):
            continue
        fmts = [op for op in cond.match_formats[mi] if op != "none"]
        if not fmts:
            continue
        match_start, match_end = match.span()
        # 修剪区间两端孤立的换行符（与 _apply_group_formats 同理）
        while match_start < match_end and page_text[match_start] in '\n\r':
            match_start += 1
        while match_end > match_start and page_text[match_end - 1] in '\n\r':
            match_end -= 1
        if match_start >= match_end:
            continue
        all_ops.append((match_start, match_end, fmts))

    # 选区过滤（仅保留落在选区内的匹配；保持 match_formats 与匹配的对应关系）
    sel = _selection_bounds(cond, selection, len(page_text))
    if sel:
        s, e = sel
        all_ops = [(ms, me, fmts) for ms, me, fmts in all_ops if ms >= s and me <= e]

    if not all_ops:
        return

    # 按起始偏移倒序排序，保证偏移有效
    all_ops.sort(key=lambda x: x[0], reverse=True)

    for match_start, match_end, fmts in all_ops:
        for op in fmts:
            # 成功修改后就地刷新 nodes_info（_apply_op 内部处理）；
            # match_formats 例外：同块多匹配各自独立应用，不触发 per-block first-wins。
            # align_* 例外（2026-08）：同块对齐按偏移倒序先到先得，见 _apply_group_formats。
            ignore_cf = not op.startswith("align_")
            _apply_op(root, nodes_info, match_start, match_end, op, _INLINE_LEAF_OPS_M, block_conflicts, ignore_cf)
            applied_ops.append(op)


def _apply_pattern_conds(
    root: ElementNode,
    nodes_info: list[TextNodeInfo],
    cond: Condition,
    page_text: str,
    selection: tuple[int, int] | None,
    applied_ops: list[str],
    block_conflicts: dict[int, set[str]] | None = None,
) -> None:
    """应用 contains/prefix/suffix/regex 条件的格式（按匹配位置应用）。

    行内格式（bold/italic/no_bold/remove）逐匹配应用（scope=selection 时仅选区内的匹配）；
    块级格式（note/heading/对齐/合并等）在 scope=selection 时作用于整个选区区间
    （选中块统一格式化），否则作用于每个匹配所在块。
    每个操作成功后就地刷新 nodes_info（_apply_op），保证同节点多匹配、跨条件应用时
    偏移→节点映射始终有效（修复多匹配只剩最后一个生效的 bug）。
    """
    if not cond.formats:
        return
    fmts = [op for op in cond.formats if op != "none"]
    if not fmts:
        return

    # 行内格式（对齐已移至块级：ptoe-align-* 类，text-align 对 inline span 无效）
    # 新增 7 项行内格式（2026-09）：underline/strike/charbox/shade/highlight/sup/sub
    inline_ops = {"bold", "italic", "no_bold", "remove", "underline", "strike", "charbox", "shade", "highlight", "sup", "sub"}
    inline_fmts = [op for op in fmts if op in inline_ops]
    block_fmts = [op for op in fmts if op not in inline_ops]

    # 块级冲突改由 apply_block_format 按块 first-wins 处理（不同块互不影响）；
    # 此处仅保留 remove 与任意已应用操作冲突的全局语义。
    _has_remove_p = "remove" in applied_ops

    sel = _selection_bounds(cond, selection, len(page_text))

    # 行内格式：逐匹配应用（仅限选区内匹配）；倒序保证偏移有效
    if inline_fmts:
        ranges = _pattern_match_ranges(cond, page_text)
        if sel:
            s, e = sel
            ranges = [(ms, me) for ms, me in ranges if ms >= s and me <= e]
        for match_start, match_end in reversed(ranges):
            for op in inline_fmts:
                if op == "remove" and applied_ops:
                    continue
                if _has_remove_p:
                    continue
                _apply_op(root, nodes_info, match_start, match_end, op, inline_ops)
                applied_ops.append(op)
                if op == "remove":
                    _has_remove_p = True

    # 块级格式：scope=selection → 整个选区（全部选中块）；否则 per 匹配所在块
    if block_fmts:
        if sel:
            ranges = [sel]
        else:
            ranges = _pattern_match_ranges(cond, page_text)
        for match_start, match_end in reversed(ranges):
            for op in block_fmts:
                if op == "remove" and applied_ops:
                    continue
                if _has_remove_p:
                    continue
                _apply_op(root, nodes_info, match_start, match_end, op, inline_ops, block_conflicts,
                          selection_driven=bool(sel))
                applied_ops.append(op)
                if op == "remove":
                    _has_remove_p = True


def _apply_fmt_entry(
    root: ElementNode,
    nodes_info: list[TextNodeInfo],
    fmts: list[str],
    page_scope: bool,
    page_text: str,
    selection: tuple[int, int] | None,
    applied_ops: list[str],
    block_conflicts: dict[int, set[str]] | None = None,
) -> None:
    """应用普通格式条目。"""
    if not fmts:
        return

    # 确定应用范围
    if page_scope:
        range_start, range_end = 0, len(page_text)
        selection_driven = False
    elif selection:
        range_start, range_end = selection
        selection_driven = True
    else:
        # 无选区且非 page scope：不应用（前端 paragraph scope 退回整页，这里同理）
        range_start, range_end = 0, len(page_text)
        selection_driven = False

    # 块级冲突改由 apply_block_format 按块 first-wins 处理（不同块互不影响）；
    # 此处仅保留 remove 与任意已应用操作冲突的全局语义。
    _has_remove_f = "remove" in applied_ops
    for op in fmts:
        if op == "remove" and applied_ops:
            continue
        if _has_remove_f:
            continue
        _apply_op(root, nodes_info, range_start, range_end, op, _INLINE_LEAF_OPS, block_conflicts,
                  selection_driven=selection_driven)
        applied_ops.append(op)
        if op == "remove":
            _has_remove_f = True


# =============================================================================
# 导出
# =============================================================================

__all__ = [
    "apply_rules",
    "VALID_FORMAT_OPS",
    "parse_regex_pattern",
    "parse_html",
    "serialize_html",
    "collect_text_nodes",
    "Rule",
    "Condition",
    "eval_format_rule",
    "op_group",
    "ops_conflict",
]