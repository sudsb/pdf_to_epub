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
import gzip
import html as _html
import json
import re
import struct
import threading
import time
import webbrowser
from collections import OrderedDict
from collections.abc import Callable
from contextlib import nullcontext
from difflib import SequenceMatcher
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

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


_ALIGN_CLASSES = {"ptoe-align-center", "ptoe-align-left", "ptoe-align-right"}

_BLOCK_TAG_RE = re.compile(r"</?(p|h[1-6])([^>]*)>", flags=re.IGNORECASE)

_PAGE_BREAK_CLASS = "ptoe-page-break"
_IMG_CLASSES = {
    "ptoe-img-full",
    "ptoe-img-fit",
    "ptoe-img-inline",
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

# apply_markers 用：匹配清洗后的标记 span
_MARKER_SPAN_RE = re.compile(
    r'<span\s+data-ptoe-marker="([^"]+)"[^>]*>(.*?)</span>',
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
}


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
                if scope not in ("selection", "paragraph", "page"):
                    scope = "selection"
                if not pattern:
                    continue  # 启用了条件但没写内容：整条丢弃（与旧行为一致）
                if ctype == "regex":
                    try:
                        re.compile(pattern)
                    except re.error:
                        continue  # 非法正则：整条丢弃
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
                if scope not in ("selection", "paragraph", "page"):
                    scope = "selection"
                if ctype == "regex" and pattern:
                    try:
                        re.compile(pattern)
                    except re.error:
                        continue  # 非法正则：该条件丢弃
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
                    if between_end and ctype == "regex":
                        try:
                            re.compile(between_end)
                        except re.error:
                            between_end = ""  # 非法正则：清空
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
        out.append(
            {
                "id": str(r.get("id") or uuid4().hex),
                "name": name,
                "mode": mode,
                "conditions": conditions,
            }
        )
    return out


def _friendly_llm_error(err: str) -> str:
    """将 llamamanage.request 的原始错误串映射为友好提示；无法归类的原样返回。"""
    if any(k in err for k in _LLM_TIMEOUT_MARKERS):
        return _llm_timeout_hint()
    if any(k in err for k in _LLM_BAD_REQUEST_MARKERS):
        return _llm_bad_request_hint()
    if any(k in err for k in _LLM_CONN_ERROR_MARKERS):
        return _llm_conn_error_hint()
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


def diff_reocr_texts(current: str, new_text: str) -> list:
    """逐字对比 current 与 new_text 的文字内容，忽略全部空白差异。

    只按文字内容对比，段落/换行分割不一致不产生标注；相同文本不标注；不同处
    标注（划线 + 校正结果）。逐字对齐（去空白后字符级 SequenceMatcher，
    autojunk=False 避免中文常见字被排除出匹配）。

    增字（原文本多字）→ candidates=[] 纯划线；少字（原文本缺字）→ 锚定相邻
    字符使前端可渲染插入文本。输出与 proofread_page 同形状：
    {start, end, wrong, candidates: [str, ...], line}，candidates 一律纯字符串列表
    （前端 join('/') 渲染、candidates[0] 替换，dict 会渲染成 [object Object]，严禁 dict）。
    start/end 为 current 原始字符偏移（可含内部空白）。空 current → []。
    """
    cur_norm, cur_pos = _strip_ws(current)
    new_norm, _ = _strip_ws(new_text)
    out = []
    # autojunk=False：中文常见字（如「的」）默认会被 autojunk 排除出匹配，导致对齐错乱
    sm = SequenceMatcher(None, cur_norm, new_norm, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            # 防御性检查：去空白后理论上不会相等，但以防万一
            if cur_norm[i1:i2] == new_norm[j1:j2]:
                continue
            start = cur_pos[i1]
            end = cur_pos[i2 - 1] + 1
            out.append(
                {
                    "start": start,
                    "end": end,
                    "wrong": current[start:end],
                    "candidates": [new_norm[j1:j2]],
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


def _block_class_html(attrs: str) -> str:
    """从块标签属性中提取应保留的 class（ptoe-note + 对齐类 + 换页 + 图片模式），返回 class 属性。"""
    m = re.search(r'class="([^"]*)"', attrs)
    if not m:
        return ""
    keep = [
        c
        for c in m.group(1).split()
        if c == _NOTE_CLASS
        or c in _ALIGN_CLASSES
        or c == _PAGE_BREAK_CLASS
        or c in _IMG_CLASSES
    ]
    return f' class="{" ".join(keep)}"' if keep else ""


def _block_classes(attrs: list[tuple[str, str | None]]) -> list[str]:
    """块级标签应保留的 class 列表（ptoe-note + 对齐类 + 换页 + 图片模式）。"""
    keep: list[str] = []
    for k, v in attrs:
        if k == "class":
            for c in (v or "").split():
                if (
                    c == _NOTE_CLASS
                    or c in _ALIGN_CLASSES
                    or c == _PAGE_BREAK_CLASS
                    or c in _IMG_CLASSES
                ):
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
        self.stack: list[str] = []  # 未闭合的行内标签（strong/em/span）
        self.block: tuple[str, int] | None = None  # ('p', 0) | ('h', level)
        self.classes: list[str] = []  # 当前块保留的 class（ptoe-note + 对齐类）
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

    def _open_block(
        self, kind: str, level: int = 0, classes: list[str] | None = None
    ) -> None:
        self._flush()
        self.block = (kind, level)
        self.classes = list(classes) if classes else []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
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
            # （含尺寸 class ptoe-img-w25/50/75/100）
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
        if tag in ("strong", "b") and "strong" in self.stack:
            self.buf.append("</strong>")
            self.stack.pop()
        elif tag in ("em", "i") and "em" in self.stack:
            self.buf.append("</em>")
            self.stack.pop()
        elif tag == "span" and self.stack and self.stack[-1] == "span":
            self.buf.append("</span>")
            self.stack.pop()
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
    if not html or "data-ptoe-marker" not in html:
        return html

    def _repl(m: re.Match) -> str:
        attrs = m.group(1) or ""
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


def sanitize_html(raw: str) -> str:
    """清洗界面提交的 HTML：只放行白名单标签（含标记 span）并保证结构平衡。"""
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
# 中英文标点归一：与汉字相邻的半角标点转全角；与字母/数字相邻的全角标点转半角
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
_STRIP_TAG_RE = re.compile(
    r"</?(?!p\b|h[1-6]\b|strong\b|em\b|b\b|i\b|br\b|span\b|img\b)[a-zA-Z][^>]*>"
)

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
        s = re.sub(
            rf"(?<=[{_LATIN_RANGE}]){re.escape(full)}(?=[{_LATIN_RANGE}])", half, s
        )
    return s


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
) -> str:
    """按块级标签切分已清洗的 HTML，逐块做文本处理并可选合并相邻段落。

    合并启发式（保守，避免把真正的段落粘在一起）：仅合并两个无 class 的
    普通 <p> 块，且前块不以句末标点（。！？…；）结尾、前后块均非空。
    <h1-6> 与带 class 的 <p>（注释/对齐/图片）视为有结构意图，不合并。
    """
    tokens = re.split(r"(<[^>]+>)", html)
    blocks: list[tuple[str, list[str]]] = []  # (开标签或 "<p>" 兜底, 块内 tokens)
    cur_open: str | None = None
    cur: list[str] = []
    for tok in tokens:
        if not tok:
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
        merged: list[tuple[str, list[str]]] = []
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
                if prev_txt and next_txt and prev_txt[-1] not in _SENT_END_PUNCT:
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
    parts: list[str] = []
    for open_tag, toks in blocks:
        inner = "".join(toks).strip()
        if not inner:
            continue
        parts.append(f"{open_tag}{inner}{_close_of(open_tag)}")
    return "\n".join(parts)


def clean_page_html(
    raw: str,
    *,
    merge_paragraphs: bool = False,
    strip_leading_symbols: bool = True,
    normalize_punctuation: bool = True,
    strip_tags: bool = True,
) -> str:
    """矫正界面的智能清理入口：清理段首 #/* 等符号、归一化中英文标点、
    移除残留的 HTML 标签，返回 sanitize 后的规范 HTML。

    merge_paragraphs=False（默认不合并）：段落合并已移出默认清理流程，
    仅在显式传入 merge_paragraphs=True 时生效（供格式规则/段落合并 op 使用）。

    幂等：已清理的内容再次清理结果不变。
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
    if re.search(r"</?(?:p|div|h[1-6]|span)([^>]*)>", raw, flags=re.IGNORECASE):
        if normalize_headings:
            return _headings_to_body(raw)
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
) -> tuple[list[tuple[str, list[tuple[str, str]]]], list[tuple[str, str]]]:
    """把块内 html 按标记 span 切分为内容段，返回 (segments, trailing)。

    - segments: [(内容 html, 段首标记列表), ...]，标记作用于紧随其后的内容段；
    - trailing: 最后一段内容之后的标记（段尾标记），作用于后续块的内容。
    段内行内标签（strong/em）在标记处保持闭合平衡：标记切在行内标签内部时，
    前段自动补闭合、后段重新打开，保证每段都是合法片段。
    """
    segments: list[tuple[str, list[tuple[str, str]]]] = []
    trailing: list[tuple[str, str]] = []
    buf: list[str] = []
    stack: list[str] = []  # 当前段未闭合的行内标签（strong/em）
    pending: list[tuple[str, str]] = []  # 段首标记（位于当前内容之前）

    def flush(reopen: bool = False) -> list[str]:
        """输出当前内容段；reopen=True 时返回需在下一段重新打开的行内标签。"""
        if not (buf or stack):
            return []
        content = "".join(buf) + "".join(f"</{t}>" for t in reversed(stack))
        # 无可见文本（如连续标记之间的空行内标签）不产出段，标记继续累积；
        # 但纯图片块（<img> 无文字）必须保留——剥标签后为空但内容有效
        if re.sub(r"<[^>]+>", "", content).strip() or "<img" in content.lower():
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


def apply_markers(pages: list[dict[str, Any]]) -> list[dict[str, str]]:
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
      数量不匹配时抛 ValueError 提示）；注释同样支持段落标记合并（因分页
      被折断的注释，后半段加段落标记即可与前半段合并为一条后再插入正文）。
      **文中没有任何注释标记时**：不做数量校验、不移动位置，注释段落原位
      保留（仅套用注释格式/小字），块内的段落标记按切段语义消费。
    没有标记时返回单篇文章（等价于把所有 <p> 顺序拼接）。
    """
    # 1) 全书按页解析为块流（跨页连续处理，段落标记可跨页合并）
    blocks: list[dict[str, Any]] = []
    for p in sorted(pages, key=lambda x: x["page"]):
        text = p.get("text") or ""
        kind: str | None = None
        note = False  # 当前块是否为注释（class="ptoe-note"）
        attrs = ""  # 当前块保留的 class 属性（对齐等）
        cur: list[str] = []
        for tok in _TOKEN_RE.split(text):
            if not tok:
                continue
            m = _BLOCK_TAG_RE.fullmatch(tok)
            if m:
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
                if not tok.startswith("</"):
                    kind = m.group(1).lower()
                    tag_attrs = m.group(2) or ""
                    note = _NOTE_CLASS in tag_attrs
                    attrs = _block_class_html(tag_attrs)
                continue
            cur.append(tok)
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
    annotations: list[str] = []
    merge_next = False
    for item in parsed:
        if item["note"]:
            text = "".join(h for h, _m in item["segments"]).strip()
            first_join = any(
                t == "join" for h, ms in item["segments"][:1] for t, _l in ms
            )
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
    pending_notes: list[str] = []  # 段尾注释在无可依附段落时顺延到下一段内容
    for item in parsed:
        if item["note"]:
            if not note_markers:
                # 无注释标记：注释段落原位保留（仅套用注释格式，不移动位置）
                html = "".join(h for h, _m in item["segments"]).strip()
                if html:
                    deferred_join = False  # 防止并入上一段落，保持原位
                    push_content(item["kind"], html, item.get("attrs", ""))
            continue
        for html, markers in item["segments"]:
            seg_notes: list[str] = []
            rest: list[tuple[str, str]] = []
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
                push_content(
                    item["kind"], note_spans(seg_notes) + html, item.get("attrs", "")
                )
            else:
                pending_notes.extend(seg_notes)
        tail_notes: list[str] = []
        rest_tail: list[tuple[str, str]] = []
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
            cur_article[-1] = (
                cur_article[-1][: -len("</p>")] + note_spans(pending_notes) + "</p>"
            )
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


def _preview_bytes(state: dict[str, Any], page_no: int) -> tuple[str, bytes] | None:
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
            return  # 全部页已缓存
        i, key = target
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
    """历史缓存目录：data/correction_history/。"""
    return Path(__file__).resolve().parent / "data" / _HISTORY_DIR_NAME


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
            # 回退：gzip 压缩 JSON
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as gz:
                json.dump(images, gz, ensure_ascii=False)
        tmp.replace(_images_cache_path(prefix))
        return True
    except Exception as e:
        print(f"[correctmanage] 内嵌预览图缓存写入失败: {e}")
        return False


def _load_images_cache(prefix: str) -> dict[str, str]:
    """读取内嵌预览图 sidecar；缺失/损坏返回空 dict（调用方回退其它预览来源）。

    支持三种格式（按优先级）：msgpack（新）、gzip+JSON（中）、纯 JSON（旧）。
    """
    if not prefix:
        return {}
    try:
        fp = _images_cache_path(prefix)
        if fp.is_file():
            # 1. 尝试 msgpack（新格式，二进制）
            if msgpack is not None:
                try:
                    with open(fp, "rb") as f:
                        data = msgpack.unpackb(f.read(), raw=False)
                    if isinstance(data, dict):
                        return {str(k): str(v) for k, v in data.items()}
                except Exception:
                    pass  # 回退到 gzip/JSON
            # 2. 尝试 gzip 解压（中格式）
            try:
                with gzip.open(fp, "rt", encoding="utf-8") as gz:
                    content = gz.read()
                data = json.loads(content)
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
            except Exception:
                pass  # 回退到纯 JSON
            # 3. 回退：旧格式未压缩 JSON
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
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


def _load_latest_history(pdf_path: str | None) -> dict[str, str]:
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
) -> dict[str, str]:
    """启动矫正界面时的初始文本来源。

    preload_history=True 时返回同一 PDF 最新历史版本（覆盖传入文本）；
    False 时返回空 dict（完全使用传入的 pages —— 重新识别后的新文本优先）。
    """
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


def _overwrite_history(state: dict[str, Any]) -> bool:
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
            # 文字纠错状态（errors/original/dismissed key 均为 str(页码)）
            "proofread": state.get("proofread")
            or {"errors": {}, "original": {}, "dismissed": {}},
            "last_proofread_page": state.get("last_proofread_page"),
            # 预览图不入版本文件（P 大书 100MB+ base64 曾致保存/暂存卡死数秒）：
            # 改由后台 flush 写入共享 sidecar <prefix>.images.json，见
            # _schedule_images_flush / _images_cache_path
        }
        with state["history_lock"]:
            # S6：按 mtime 取最新版本覆盖（文件名时间戳同秒时不会覆盖错版本）
            latest = None
            for fp in d.glob(f"{prefix}_*.json"):
                try:
                    if (
                        latest is None
                        or fp.stat().st_mtime_ns > latest.stat().st_mtime_ns
                    ):
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


def _build_docx(blocks: list[tuple], path: str) -> None:
    """块列表 → 最小合法 .docx（zipfile 打包，无第三方依赖）。

    文本块 ('p'|'hN', 文本) 渲染为普通段落/标题段落；
    图片块 ('img', src, alt, cls) 内嵌真实图片字节（data URI）：
    - 尺寸 class ptoe-img-w25/50/75/100 → 宽度 5 英寸 × 比例，高度按固有宽高比；
    - 非 data URI 或解析失败 → 以 [图片] 占位段落输出（与 TXT 一致）。
    """
    import zipfile

    parts: list[str] = [_DOCX_DOCUMENT_HEAD]
    media: list[tuple[str, bytes]] = []  # (word/media/imageN.ext, 字节)
    rels: list[str] = []
    img_no = 0
    for block in blocks:
        if block[0] == "img":
            _, src, alt, cls = block
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
            for c in (cls or "").split():
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
        kind, text = block[0], block[1]
        xml_text = (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        # 段内换行（<br>）转 w:br
        xml_text = xml_text.replace("\n", '</w:t><w:br/><w:t xml:space="preserve">')
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
    rels_xml = _DOCX_DOCUMENT_RELS_HEAD + "".join(rels) + _DOCX_DOCUMENT_RELS_TAIL
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        zf.writestr("_rels/.rels", _DOCX_RELS)
        zf.writestr("word/document.xml", "".join(parts))
        zf.writestr("word/_rels/document.xml.rels", rels_xml)
        for fname, data in media:
            zf.writestr(fname, data)


def _pick_export_path(
    state: dict[str, Any], fmt: str, fallback_dir: str | None = None
) -> tuple[str | None, bool]:
    """弹保存对话框选导出路径。返回 (路径, 是否弹过对话框)。

    - 用户取消 → (None, True)，调用方直接回「已取消」；
    - tkinter 不可用（headless/无桌面）→ (None, False)，调用方用默认路径兜底。
    """
    ext = "txt" if fmt == "txt" else ("epub" if fmt == "epub" else "docx")
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
                # 2026-08-15 修复：已保存/历史内容按原样 serve（normalize_headings=False）
                # ——其中可能含用户手动设置的标题，不能再归一为 <p>（否则「保存后重开，
                # 已设置的标题格式丢失」）；OCR 自动标题的归一只在写入历史时做一次。
                pages_list.append(
                    {
                        "page": n,
                        "text": _page_text(served_html, normalize_headings=False),
                    }
                )
            payload = {"pages": pages_list}
            self._send(200, self._json(payload), "application/json; charset=utf-8")
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
                probe = _probe_server(model_name) if model_name else "none"
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
                            # 2026-08-15 修复：历史版本内容按原样同步（不再归一标题）——
                            # 用户手动设置的 <h1>-<h6> 必须保留，否则「保存后重开，
                            # 已设置的标题格式丢失」；OCR 自动标题的归一只在写入历史时做一次
                            st["pages"][int(k)] = sanitize_html(str(v))
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
                    cleaned.append(
                        {
                            "page": n,
                            "html": clean_page_html(str(item.get("html") or "")),
                        }
                    )
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
        if path == "/api/proofread_settings":
            self._proofread_settings()
            return
        if path == "/api/shortcuts":
            self._shortcuts()
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
                if (
                    not isinstance(model_choices, dict)
                    or model_key not in model_choices
                ):
                    self._send(
                        400,
                        self._json(
                            {
                                "ok": False,
                                "error": (
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
                from llamamanage import _active_engine

                eng = _active_engine()
                eng_label = "vLLM-Omni" if eng == "vllm" else "llama-server"
                eng_port = (
                    (cfg.get("vllm_server_args") or {}).get("port") or "8000"
                    if eng == "vllm"
                    else (cfg.get("llama_server_args") or {}).get("port") or "8080"
                )
                running = bool(runserver(model_key, with_mmproj=has_mmproj))
                if running:
                    message = f"{eng_label} 已就绪"
                else:
                    # 启动失败：区分「端口被其他模型占用」与「启动超时/失败」，给出可操作提示
                    from llamamanage import _probe_server

                    model_name = str(model_info.get("name") or model_key)
                    if _probe_server(model_name) == "mismatch":
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

                eng_label = (
                    "vLLM-Omni" if _active_engine() == "vllm" else "llama-server"
                )
                stopserver()
                # stopserver 已兜底杀端口上的遗留实例；再探测确认端口真正释放
                # （杀不掉时提示手动关闭，避免界面误报已停止）
                if _probe_server(None) != "none":
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
                    if (
                        not isinstance(model_choices, dict)
                        or model_key not in model_choices
                    ):
                        llm_error = (
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
                if (
                    not isinstance(model_choices, dict)
                    or model_key not in model_choices
                ):
                    self._send(
                        400,
                        self._json(
                            {
                                "ok": False,
                                "error": (
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
                try:
                    page_no = int(body.get("page"))
                except (TypeError, ValueError):
                    self._send(
                        400,
                        self._json({"ok": False, "error": "page 参数无效"}),
                        "application/json; charset=utf-8",
                    )
                    return
                img = _full_bytes(state, page_no)
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
                res = llamamanage._request_image_new(
                    ocr_prompt,
                    "",
                    model_key=model_key,
                    thinking=True,
                    timeout=llamamanage.REQUEST_TIMEOUT,
                    img_bytes=img_bytes,
                )
                if res.get("error"):
                    self._send(
                        200,
                        self._json(
                            {
                                "ok": False,
                                "error": _friendly_llm_error(str(res.get("error"))),
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
                    new_text = ttos(new_text)
                # 2026-08-09：再将英文标点归一为中文标点，避免半角/全角差异被当成纠错项
                new_text = _full_punct(new_text)
                current_text = _proofread_plain_text(str(body.get("html") or ""))
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
                if fmt not in ("txt", "docx", "epub"):
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
                blocks: list[tuple[str, str]] = []
                for item in items:
                    blocks.extend(_html_to_export_blocks(str(item.get("html") or "")))
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
                        base = (st.get("history_name") or "矫正导出").removesuffix(
                            ".pdf"
                        )
                        base = (base or "矫正导出").strip() or "矫正导出"
                        out_path = _default_export_path(f"{base}.{fmt}")
                out = Path(out_path)
                out.parent.mkdir(parents=True, exist_ok=True)
                if fmt == "txt":
                    # 图片块（4 元组）在 TXT 中以 [图片] 占位符表示
                    text = (
                        "\n\n".join(
                            ("[图片]" if b[0] == "img" else b[1]) for b in blocks
                        )
                        + "\n"
                    )
                    out.write_text(text, encoding=_TXT_ENCODING)
                elif fmt == "epub":
                    # epub：标记→文章结构→XHTML→打包（临时目录隔离，完成后清理）
                    import shutil as _sh
                    import tempfile as _tf

                    tmp_dir = _tf.mkdtemp(prefix="ptoe_export_epub_")
                    try:
                        # 浏览器提交的 html 可能含 <div> 块（Chrome contenteditable
                        # 回车产生），apply_markers 只认 p/h1-6——先 sanitize 归一为
                        # <p>（保留对齐/注释/图片 class），与「完成并转换」路径一致；
                        # 否则产出 <p><div class="ptoe-align-center">…</div></p>
                        # 非法嵌套、对齐丢失（2026-08-15）
                        src_items = [
                            {
                                "page": p["page"],
                                "text": sanitize_html(str(p["html"] or "")),
                            }
                            for p in items
                        ]
                        articles = apply_markers(src_items)
                        title = st.get("history_name") or "矫正导出"
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
                        from htmlmanage import HTMLConverter

                        result = HTMLConverter(
                            output_dir=tmp_dir, epub_version="3.0"
                        ).convert_document(structured)
                        generated = result.get("epub")
                        if not generated or not Path(generated).is_file():
                            raise RuntimeError(
                                result.get("epub_error") or "EPUB 打包失败"
                            )
                        _sh.copy2(generated, str(out))
                    finally:
                        _sh.rmtree(tmp_dir, ignore_errors=True)
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
    history_pages: dict[str, str] = _history_pages_for_init(
        str(pdf_path), history=history, preload_history=preload_history
    )
    if history_pages:
        loaded = sum(1 for p in ordered if str(p["page"]) in history_pages)
        if loaded:
            print(f"      已加载历史矫正记录（{loaded}/{len(ordered)} 页）")
    state: dict[str, Any] = {
        "pages": {
            # 2026-08-15：传入矫正界面的文本一律按正文展示——OCR 自动结构产生的
            # <h1>-<h6> 标题归一为 <p>（标题由用户在界面手动标记），保证界面所见
            # 与浏览器关闭后的返回结果一致。
            # 2026-08-15 修复：历史缓存内容按原样载入（normalize_headings=False）——
            # 其中可能含用户手动设置的标题，不能再归一为 <p>（否则「保存后重开，
            # 已设置的标题格式丢失」）；OCR 自动标题的归一只在写入历史时做一次
            # （_save_ocr_history），此处仅对无历史的原始 OCR 文本兜底归一。
            p["page"]: _page_text(
                str(history_pages.get(str(p["page"]), p["text"])),
                normalize_headings=str(p["page"]) not in history_pages,
            )
            for p in ordered
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
        # 文字纠错状态（保存/暂存/完成时随历史缓存落盘，加载时恢复）
        "proofread": {"errors": {}, "original": {}, "dismissed": {}},
        "last_proofread_page": None,
        "embedded_images": {},
    }
    server = ThreadingHTTPServer((host, port), _CorrectionHandler)
    server.daemon_threads = True
    server.state = state
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()
    # 后台渐进式预渲染预览图：用户在浏览器编辑期间把所有页渲染好，
    # 首次保存/暂存/完成时 _build_embedded_images 全命中缓存直接返回，
    # 不再阻塞按钮。每页单独持锁 + 50ms 间隔，不阻塞 UI 预览/请求线程。
    threading.Thread(
        target=_prerender_embedded_images, args=(state,), daemon=True
    ).start()
    url = f"http://{server.server_address[0]}:{server.server_address[1]}/"
    print(f"      矫正界面已启动: {url}（对比原图与识别文字，完成后点「完成并转换」）")
    if open_browser:
        webbrowser.open(url)
    try:
        # 浏览器关闭监测：页面每 30s 发心跳；关闭标签页时发 pagehide 信标。
        # 信标确认关闭或心跳失联超过 idle_timeout 秒后，自动继续后续流程。
        stale_since: float | None = None
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
:root{--accent:#2f6fed;--border:#d8dee6;--bg:#f4f6f9;--editor-font-size:14px;}
*{box-sizing:border-box}
body{margin:0;font-family:"Microsoft YaHei",system-ui,sans-serif;background:var(--bg);color:#1c2733;}
#toolbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:4px;padding:5px 8px;background:#fff;border-bottom:1px solid var(--border);flex-wrap:wrap;font-size:12px;}
#toolbar .title{font-weight:700;margin-right:10px;}
#toolbar .spacer{flex:1;}
#toolbar .sep{width:1px;height:22px;background:var(--border);margin:0 4px;}
#toolbar label{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:#5a6b7c;}
#prCount{display:inline-flex;align-items:center;gap:2px;font-size:12px;color:#5a6b7c;white-space:nowrap;margin-left:4px;}
#prCountNum{color:#e02020;font-weight:700;}
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
.pop-rule-wrap{position:relative;}
.pop-rule-sub{display:none;position:absolute;left:100%;top:-4px;margin-left:4px;min-width:120px;max-height:260px;overflow-y:auto;background:#fff;border:1px solid var(--border);border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,.15);z-index:61;padding:4px 0;}
.pop-rule-sub .ctx-item{display:block;width:100%;text-align:left;padding:4px 10px;border:none;background:none;cursor:pointer;font-size:13px;white-space:nowrap;}
.pop-rule-sub .ctx-item:hover{background:#f0f0f0;}
.pop-rule-sub .ctx-empty{padding:4px 10px;color:#999;font-size:12px;}
body.paint-mode{cursor:copy;}
/* 文字纠错：错误标注（删除线红色）+ 候选正确字（绿色）+ 确认悬浮窗 */
.ptoe-err{text-decoration:line-through;color:#c00;background:#ffe0e0;padding:0 3px;border-radius:3px;cursor:pointer;}
.ptoe-fix{color:#080;font-size:0.9em;}
#errPopup{position:fixed;z-index:65;display:none;background:#fff;border:1px solid #ccc;border-radius:6px;padding:4px;box-shadow:0 2px 8px rgba(0,0,0,.2);gap:6px;}
/* 图片设置弹窗：点击编辑区内的图片弹出，调整大小/位置/删除 */
#imgPopup{position:fixed;z-index:65;display:none;flex-direction:column;gap:6px;padding:10px;background:#fff;border:1px solid #ccc;border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.2);min-width:140px;}
.img-pop-btn{background:#f5f5f5;border:1px solid #ddd;border-radius:4px;padding:3px 10px;font-size:13px;cursor:pointer;}
.img-pop-btn:hover{background:#eef3fb;border-color:var(--accent);}
#errOk{background:#2e8b57;color:#fff;border:none;border-radius:4px;padding:2px 10px;cursor:pointer;font-size:14px;}
#errNo{background:#c0392b;color:#fff;border:none;border-radius:4px;padding:2px 10px;cursor:pointer;font-size:14px;}
/* 文字纠错下拉菜单 */
#proofreadMenu{position:fixed;z-index:70;background:#fff;border:1px solid #ddd;border-radius:6px;box-shadow:0 2px 10px rgba(0,0,0,.15);min-width:120px;padding:4px;display:none;}
#proofreadMenu button{display:block;width:100%;text-align:left;padding:6px 10px;border:none;background:none;cursor:pointer;border-radius:4px;font-size:13px;}
#proofreadMenu button:hover{background:#f0f0f0;}
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
    <button type="button" class="ic-btn" id="formatRulesBtn" onmousedown="event.preventDefault()" title="格式规则：对选中文字一键应用自定义规则（可多条叠加 / 条件分支；Ctrl+Shift+Q）" aria-label="格式规则">规</button>
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
    <button type="button" id="proofreadBtn" title="文字纠错下拉菜单：校正当前页 / 应用全部候选 / 清除标注 / 回退原文" aria-label="文字纠错">校 <span class="ptoe-caret">▾</span></button>
  </div>
  <div class="tb-group" role="group" aria-label="撤销重做">
    <button type="button" id="undoBtn" class="ic-btn" onmousedown="event.preventDefault()" disabled title="撤回上一步（Ctrl+Z）" aria-label="撤回（Ctrl+Z）">↶</button>
    <button type="button" id="redoBtn" class="ic-btn" onmousedown="event.preventDefault()" disabled title="前进下一步（Ctrl+Y / Ctrl+Shift+Z）" aria-label="前进（Ctrl+Y）">↷</button>
  </div>
  <div class="tb-group" role="group" aria-label="图片">
    <select id="imgModeSel" title="插入图片的显示模式：全画幅=占满文字宽度，局部=按原尺寸居中，行内=嵌在文字中间（50% 宽度）">
      <option value="full">全画幅</option>
      <option value="fit">局部</option>
      <option value="inline">行内</option>
    </select>
    <button type="button" id="imgExternalBtn" title="从本地文件选择图片，插入到文字光标处">外部</button>
    <input type="file" id="imgExternalInput" accept="image/*" style="display:none"/>
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
  <span id="prCount" title="当前页面存在的可纠错文字数量（未采纳/未忽略的错误标注）">可纠错数：<b id="prCountNum">0</b></span>
  <div class="tb-group tb-main" role="group" aria-label="工具与操作">
    <button type="button" id="helpBtn" title="帮助：Markdown 格式、快捷键与标记说明">帮助</button>
    <button type="button" id="historyBtn" title="历史记录：查看/管理本地矫正缓存（文件名与路径分列、多版本）">历史记录</button>
    <button type="button" id="settingsBtn" title="设置">设置</button>
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
<!-- 右键上下文菜单（2026-08-08）：编辑区内右键弹出；重识别/插入标记/导出/Markdown 提示/保存/暂存 -->
<div id="contextMenu" hidden>
  <button type="button" class="ctx-item" data-ctx="reocr">重识别</button>
  <button type="button" class="ctx-item" data-ctx="clear">清除</button>
  <div class="ctx-item ctx-sub" data-ctx="marker">插入标记 <span class="ctx-arrow">▸</span>
    <div class="ctx-submenu" id="ctxMarkerSub">
      <button type="button" class="ctx-item" data-ctx-marker="full">全文标记</button>
      <button type="button" class="ctx-item" data-ctx-marker="page">换页标记</button>
      <button type="button" class="ctx-item" data-ctx-marker="join">段落标记</button>
      <button type="button" class="ctx-item" data-ctx-marker="note">注释标记</button>
    </div>
  </div>
  <div class="ctx-item ctx-sub" data-ctx="export">导出 <span class="ctx-arrow">▸</span>
    <div class="ctx-submenu" id="ctxExportSub">
      <button type="button" class="ctx-item" data-ctx-export="txt">txt格式</button>
      <button type="button" class="ctx-item" data-ctx-export="docx">docx格式</button>
      <button type="button" class="ctx-item" data-ctx-export="epub">epub格式</button>
    </div>
  </div>
  <div class="ctx-item ctx-sub" data-ctx="rules">添加规则 <span class="ctx-arrow">▸</span>
    <div class="ctx-submenu" id="ctxRulesSub"></div>
  </div>
  <button type="button" class="ctx-item" data-ctx="md">Markdown</button>
  <div class="ctx-sep"></div>
  <button type="button" class="ctx-item" data-ctx="save">保存</button>
  <button type="button" class="ctx-item" data-ctx="stage">暂存</button>
</div>
<div id="proofreadMenu">
  <button type="button" id="prMenuCorrect" role="menuitem">校正</button>
  <button type="button" id="prMenuReocr" role="menuitem">重识别</button>
  <button type="button" id="prMenuApply" role="menuitem">应用</button>
  <button type="button" id="prMenuClear" role="menuitem">清除</button>
  <button type="button" id="prMenuRevert" role="menuitem">回退</button>
  <div style="padding:6px 10px;border-top:1px solid #eee;margin-top:6px;">
    <label style="display:block;font-size:13px;" title="默认只执行三条规则：连续重复文字 / 连续标点 / 中文中的连续字母">启用原有规则（半角转全角/引号配对/混淆表/词典） <input type="checkbox" id="prLegacyRules"></label>
    <label style="display:block;font-size:13px;margin-top:6px;">启用 LLM 深度校对 <input type="checkbox" id="prLlmEnable"></label>
    <label style="display:block;font-size:13px;margin-top:6px;">模型 <select id="prLlmModel" style="width:130px;"></select></label>
    <small style="color:#666;display:block;margin-top:4px;">启用后每次校正会额外调用本地 llama-server 进行深度校对。模型留空则使用当前选中模型。</small>
    <div style="display:flex;gap:6px;margin-top:8px;">
      <button type="button" id="prLlmStart" style="flex:1;">启动服务</button>
      <button type="button" id="prLlmStop" style="flex:1;">停止服务</button>
    </div>
    <small id="prLlmStatus" style="color:#666;display:block;margin-top:4px;"></small>
  </div>
</div>
<div id="searchModalBg"><div class="modal search-modal">
  <div class="search-head"><h3>搜索 / 替换</h3><button type="button" id="searchCloseBtn" class="x-btn" title="关闭搜索" aria-label="关闭搜索">✕</button></div>
  <div class="search-row">
    <input type="text" id="searchInput" placeholder="搜索词（可正则）">
    <label class="search-regex" title="勾选后按正则表达式搜索，否则按普通文本"><input type="checkbox" id="searchRegex">正则</label>
    <button type="button" id="searchBtn" class="primary">搜索</button>
    <button type="button" id="searchClearBtn" class="primary" title="清除全部文字标记与搜索结果" aria-label="清除搜索">清理</button>
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
  <h3>设置</h3>
  <div class="settings-tabs">
    <button type="button" class="settings-tab active" data-tab="shortcuts">快捷键</button>
    <button type="button" class="settings-tab" data-tab="fonts">字体</button>
    <button type="button" class="settings-tab" data-tab="ui">界面</button>
  </div>
  <div class="settings-panels">
    <div class="settings-panel" id="panel-shortcuts">
      <p style="font-size:12px;color:#5a6b7c;margin:8px 0;">每个操作绑定一个组合键；点击某行后按下新组合键完成绑定，Del/Backspace 清除，Esc 取消。绑定保存在本浏览器（localStorage）并同步到配置文件。</p>
      <table id="shortcutTable"></table>
    </div>
    <div class="settings-panel" id="panel-fonts" style="display:none;">
      <p style="font-size:12px;color:#5a6b7c;margin:8px 0;">设置各类文本的字体族（CSS font-family），留空则使用浏览器默认。修改后实时生效，保存到配置文件。</p>
      <div style="display:grid;grid-template-columns:120px 1fr;gap:8px 12px;align-items:center;margin-top:8px;">
        <label>正文字体</label>
        <input type="text" id="fontBody" placeholder="如：serif, 'Microsoft YaHei', sans-serif" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font:inherit;">
        <label>标题字体</label>
        <input type="text" id="fontHeading" placeholder="如：sans-serif, 'Microsoft YaHei', serif" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font:inherit;">
        <label>注释字体</label>
        <input type="text" id="fontNote" placeholder="如：serif, 'KaiTi', sans-serif" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font:inherit;">
        <label>引用字体</label>
        <input type="text" id="fontCitation" placeholder="如：cursive, 'FangSong', serif" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font:inherit;">
      </div>
      <div style="margin-top:12px;padding-top:8px;border-top:1px solid var(--border);">
        <label style="display:flex;align-items:center;gap:8px;font-size:13px;">
          <input type="checkbox" id="citationItalicEnabled" style="width:16px;height:16px;">
          启用引用斜体（citation 格式自动应用 italic）
        </label>
      </div>
    </div>
    <div class="settings-panel" id="panel-ui" style="display:none;">
      <h4 style="margin:8px 0 4px;">提示延迟</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">鼠标悬停按钮超过设定时间（毫秒）才显示提示文字，提示中会附带对应快捷键；0 = 立即显示。</p>
      <label style="font-size:13px;">提示延迟（毫秒） <input type="number" id="tipDelayInput" min="0" max="5000" step="100" style="width:90px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;"></label>
      <h4 style="margin:16px 0 4px;">编辑器字号</h4>
      <p style="font-size:12px;color:#5a6b7c;margin:0 0 6px;">调整编辑区显示字号（视图偏好，不写入保存内容）。</p>
      <label style="font-size:13px;">字号（px） <input type="number" id="editorFontSizeInput" min="10" max="28" step="1" style="width:70px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;"></label>
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
<div id="frRuleModalBg"><div class="modal" style="max-width:760px;">
  <div class="search-head"><h3>编辑规则</h3><button type="button" id="frRuleCloseBtn" class="x-btn" title="关闭" aria-label="关闭">✕</button></div>
  <div style="margin-bottom:8px;">
    <label style="font-size:13px;">规则名称 <input type="text" id="frName" placeholder="如：书名标题" style="width:240px;"></label>
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
<div id="historyModalBg"><div class="modal" style="max-width:780px;">
  <h3>历史记录</h3>
  <p style="font-size:12px;color:#5a6b7c;">本地矫正缓存（同一文件保留多个版本，v1 为最新）。文件名与路径分列显示，同名不同路径的文件可区分；勾选后可删除或导出（支持多选）。</p>
  <div style="max-height:50vh;overflow:auto;border:1px solid var(--border);border-radius:4px;margin-top:6px;">
    <table id="historyTable" style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead><tr style="text-align:left;color:#33414f;">
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);"><input type="checkbox" id="historyCheckAll" title="全选"></th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);">文件名</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);">文件路径</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);">版本</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);">更新时间</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);">校正页码</th>
        <th style="padding:6px 8px;border-bottom:1px solid var(--border);">操作</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;">
    <button type="button" id="historyImportBtn">导入</button>
    <button type="button" id="historyExportBtn">导出</button>
    <input type="file" id="historyImportFile" accept=".json,application/json,.zip,application/zip" style="display:none">
    <button type="button" id="historyDeleteBtn">删除选中</button>
    <button type="button" id="historyDeleteAllBtn">全部删除</button>
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
    <span style="width:100%;font-size:12px;color:#666;">大小</span>
    <button type="button" class="img-pop-btn" data-img-op="size" data-img-val="original">原尺寸</button>
    <button type="button" class="img-pop-btn" data-img-op="size" data-img-val="w25">25%</button>
    <button type="button" class="img-pop-btn" data-img-op="size" data-img-val="w50">50%</button>
    <button type="button" class="img-pop-btn" data-img-op="size" data-img-val="w75">75%</button>
    <button type="button" class="img-pop-btn" data-img-op="size" data-img-val="w100">100%</button>
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
<div id="toast" aria-hidden="true"></div>
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
  ['marker_page','换页标记'],
  // 工具操作（无需 currentEditable，直接调用）
  ['search','搜索'], ['clean','智能清理'], ['convert_t2s','繁→简'], ['convert_s2t','简→繁'],
  ['toggle_md','Markdown模式'], ['undo','撤销'], ['redo','重做'], ['history','历史记录'],
  ['export','导出'], ['save','保存'], ['stage','暂存'], ['finish','完成并转换'],
  ['jump','跳转'], ['help','帮助'], ['settings','快捷键设置'],
  ['proofread_correct','校正'], ['proofread_reocr','重识别'], ['proofread_apply','应用'],
  ['proofread_clear','清除标注'], ['proofread_revert','回退'],
  ['proofread_accept', '采纳纠错'], ['proofread_ignore', '忽略纠错'],
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
  marker_page:'换页标记（从此处之后的内容显示在新的一页）',
  proofread_accept: '采纳纠错（替换为候选字）', proofread_ignore: '忽略纠错（消除标注）',
};
const DEFAULTS = {
  bold:'Ctrl+B', italic:'Ctrl+I', heading:'Ctrl+1', p:'Ctrl+0',
  note:'Ctrl+Shift+N',
  align_left:'Ctrl+Shift+Left', align_center:'Ctrl+Shift+Up', align_right:'Ctrl+Shift+Right',
  marker_full:'Ctrl+Shift+F', marker_note:'Ctrl+Shift+M', marker_join:'Ctrl+Shift+J',
  marker_page:'Ctrl+Shift+P',
  // 工具操作默认快捷键
  search:'Ctrl+F', clean:'Ctrl+Shift+C', convert_t2s:'Ctrl+Shift+T', convert_s2t:'Ctrl+Shift+Y',
  toggle_md:'Ctrl+Shift+D', undo:'Ctrl+Z', redo:'Ctrl+Y', history:'Ctrl+H',
  export:'Ctrl+E', save:'Ctrl+S', stage:'Ctrl+Shift+S', finish:'Ctrl+Enter',
  jump:'Ctrl+G', help:'F1', settings:'Ctrl+Shift+O',
  proofread_correct:'Ctrl+K', proofread_reocr:'Ctrl+Shift+R', proofread_apply:'Ctrl+Shift+A',
  proofread_clear:'Ctrl+Shift+X', proofread_revert:'Ctrl+Shift+Z',
  proofread_accept: 'Enter', proofread_ignore: 'Escape',
};
let pages = [];
let contentMap = new Map();     // index -> 该行最近一次 innerHTML（虚拟列表离屏保留）
let editedSet = new Set();
let dirty = false;
let mdMode = false;             // Markdown 源码模式
let mdSourceMap = new Map();    // index -> markdown 源码（仅 md 模式使用）
let loadNonce = 0;              // 历史版本载入计数：图片 URL 加 ?v= 防换书后缓存错图
const imgAspect = {};           // page -> "W / H"（首帧加载后缓存，重挂载/换全幅图不再改变行高）
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
// 服务端持久化（config.json shortcuts）：随机端口下 localStorage 每次运行失效，
// 故 init 时异步拉取服务端设置覆盖内存 bindings（服务端为准）；localStorage 仅作同步兜底。
async function loadBindingsFromServer() {
  try {
    const res = await fetchJSON('/api/shortcuts');
    if (!res || !res.ok) return;
    const sc = res.shortcuts;
    if (!sc || typeof sc !== 'object' || !Object.keys(sc).length) return;
    bindings = Object.assign({}, DEFAULTS, sc);
    try { localStorage.setItem('ptoe_shortcuts', JSON.stringify(bindings)); } catch (e) {}
    // 设置弹窗已打开时刷新表格（keydown 分发在派发时读 bindings 变量，无需额外处理）
    const bg = document.getElementById('modalBg');
    if (bg && bg.style.display === 'flex') renderShortcutTable();
  } catch (e) { console.warn('loadBindingsFromServer failed: ' + e.message); }
}
function saveBindings() {
  try { localStorage.setItem('ptoe_shortcuts', JSON.stringify(bindings)); } catch (e) {}
  // fire-and-forget 持久化到 config.json（失败静默，localStorage 仍生效）
  fetch('/api/shortcuts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ shortcuts: bindings }),
  }).catch(function () {});
}
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
// 保存兜底：剥掉残留的纠错标注（.ptoe-fix 整体删除、.ptoe-err 解包保留原文），
// 但保留 .ptoe-marker（标记是有意义的内容）。防止「清除后保存 → 载入历史版本
// 建议文字跟在原文后面复现」（2026-08-09 修复）。与 _plainNoAnno 不同：那个会连
// 标记一起剥掉，且返回纯文本。
function stripProofreadMarkup(html) {
  const s = String(html == null ? '' : html);
  if (s.indexOf('ptoe-err') < 0 && s.indexOf('ptoe-fix') < 0) return s; // 无标注快速返回
  const d = document.createElement('div');
  d.innerHTML = s;
  d.querySelectorAll('.ptoe-fix').forEach(function (el) { el.parentNode.removeChild(el); });
  d.querySelectorAll('.ptoe-err').forEach(function (el) {
    const t = document.createTextNode(el.textContent);
    el.parentNode.replaceChild(t, el);
  });
  d.normalize();
  return d.innerHTML;
}
function collect() {
  const out = [];
  for (let i = 0; i < pages.length; i++) {
    const src = pageSource(i);
    const html = mdMode ? mdToHtml(src) : stripProofreadMarkup(src);
    out.push({ page: pages[i].page, html: html });
  }
  return out;
}
function collectProofread() {
  // 收集非空纠错状态（key 均为 str(页码)）；dismissed Set → 数组
  const errors = {}, original = {}, dismissed = {};
  for (let i = 0; i < pages.length; i++) {
    const pageNo = pages[i].page;
    const errs = proofreadErrors[i];
    if (errs && errs.length) {
      const active = errs.filter(function (e) { return !e._gone; });
      if (active.length) errors[String(pageNo)] = JSON.parse(JSON.stringify(active));
    }
    if (proofreadOriginal[i]) original[String(pageNo)] = proofreadOriginal[i];
    if (proofreadDismissed[i] && proofreadDismissed[i].size) {
      dismissed[String(pageNo)] = Array.from(proofreadDismissed[i]);
    }
  }
  return { errors: errors, original: original, dismissed: dismissed };
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
  updatePrCount();
  let extra = '';
  const ed = currentEditable();
  if (ed) {
    const row = ed.closest('.page-row');
    if (row) {
      const i = Number(row.dataset.i);
      const t = (ed.textContent || '').trim();
      extra = ' ｜ 字符 ： ' + t.length;
    }
  }
  document.getElementById('status').textContent =
    '已编辑 ' + editedSet.size + '/' + pages.length + (dirty ? '（未保存）' : '') + extra;
}
function setStatus(s) { document.getElementById('status').textContent = s; }
function updatePrCount() {
  const el = document.getElementById('prCountNum');
  if (!el) return;
  const ed = currentEditable();
  if (!ed) { el.textContent = '0'; return; }
  const row = ed.closest('.page-row');
  if (!row) { el.textContent = '0'; return; }
  const i = Number(row.dataset.i);
  const errors = proofreadErrors[i];
  if (!errors || !errors.length) { el.textContent = '0'; return; }
  let n = 0;
  for (const e of errors) { if (!e._gone) n++; }
  el.textContent = String(n);
}
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
    '<button type="button" class="img-crop" title="裁剪左侧图片后插入到右侧文字光标处">裁</button>' +
    '<img decoding="async" src="/preview/' + p.page + '?v=' + loadNonce + '" alt="第' + p.page + '页原图"></div>' +
    '<div class="editable" contenteditable="true" spellcheck="false" aria-label="第 ' + p.page + ' 页文字" role="textbox" aria-multiline="true"></div>';
  const ed = row.querySelector('.editable');
  ed.innerHTML = displayHtml(i);
  _reapplyProofread(i);
  ed.addEventListener('input', (ev) => { syncContent(ed); markDirty(i); scheduleRemeasure(i); histTouchInput(i); histScheduleIdle(); const composing = window.isComposing || (ev && (ev.isComposing || ev.inputType === 'insertCompositionText')); if (composing) { _proofreadAutoDismiss(ed, i, true); _prRenderPending[i] = true; } else { _proofreadAutoDismiss(ed, i); } });
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
  ed.addEventListener('focus', updateStatus);
  ed.addEventListener('blur', updateStatus);
  const insBtn = row.querySelector('.img-insert');
  insBtn.addEventListener('click', () => insertImage(row, i));
  const cropBtn = row.querySelector('.img-crop');
  cropBtn.addEventListener('click', () => openCrop(row, i));
  const img = row.querySelector('img');
  const v = loadNonce;
  // 已知宽高比时先占位：重挂载/预览↔原图切换不再改变行高（避免视口内行突然长高 → 下方内容下移 → 跳页）
  if (imgAspect[p.page]) img.style.aspectRatio = imgAspect[p.page];
  img.onload = () => {
    // 首帧加载后缓存宽高比并占位；随后批量测量（行高变化即时补偿 scrollY，视口保持贴附）
    if (img.naturalWidth > 0 && img.naturalHeight > 0) {
      const ar = img.naturalWidth + ' / ' + img.naturalHeight;
      imgAspect[p.page] = ar;
      img.style.aspectRatio = ar;
    }
    scheduleRemeasure(i);
  };
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
  if (!img) return;
  fetch(img.src)
    .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.blob(); })
    .then((blob) => new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve({ dataUrl: fr.result, size: blob.size });
      fr.onerror = () => reject(new Error('读取图片失败'));
      fr.readAsDataURL(blob);
    }))
    .then(({ dataUrl, size }) => insertImageDataUrl(dataUrl, size, i))
    .catch((e) => showToast('插入图片失败：' + e.message, 'fail'));
}

// 把 dataUrl 图片插入到第 i 页文字光标处（整页图插入 / 外部插入 / 裁剪插入共用）。
// modeOverride 可选：显式指定插入模式（full/fit/inline），缺省读 imgModeSel 下拉框。
function insertImageDataUrl(dataUrl, size, i, modeOverride) {
  let ed = null;
  if (i != null) {
    const row = host.querySelector('.page-row[data-i="' + i + '"]');
    ed = row ? row.querySelector('.editable') : null;
  }
  if (!ed) ed = currentEditable();
  if (!ed) { showToast('未找到可插入的编辑区', 'fail'); return; }
  if (i == null) {
    const row = ed.closest('.page-row');
    i = row ? Number(row.dataset.i) : 0;
  }
  const mode = modeOverride || document.getElementById('imgModeSel').value;
  // 插入图片：mode 决定全画幅/局部/行内。
  // 全画幅=整块居中占满行宽（默认 w100）；局部=整块按原尺寸居中；
  // 行内=裸 <img> 嵌在文字光标处（50% 宽度），文字环绕。
  const isInline = mode === 'inline';
  let html;
  if (isInline) {
    html = '<img class="ptoe-img-inline ptoe-img-w50" src="' + dataUrl + '" alt="插图"/>';
  } else {
    const imgClass = mode === 'full' ? ' class="ptoe-img-w100"' : '';
    html = '<p class="ptoe-img-' + mode + ' ptoe-img-center"><img' + imgClass + ' src="' + dataUrl + '" alt="插图"/></p>';
  }
  const before = histBegin('插入图片', [i]);
  ed.focus();
  inDiscreteOp = true;
  try {
    withScrollStable(() => {
      // 恢复最近一次在 .editable 内的选区，使图片插入到光标处（而非末尾）
      if (_lastEditableRange) {
        const sel = window.getSelection();
        sel.removeAllRanges();
        try { sel.addRange(_lastEditableRange); } catch (e) { /* 选区已失效则回退到末尾 */ }
      }
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
  } else if (isInline) {
    showToast('已插入图片（行内，50% 宽度，点击图片可调整大小/位置）', 'ok');
  } else {
    showToast('已插入图片（居中，' + (mode === 'full' ? '全画幅' : '局部') + '显示）', 'ok');
  }
}

// 左侧原图裁剪后插入：叠加裁剪层，拖拽选区，确认后 canvas 裁剪为 dataUrl 插入
function openCrop(row, i) {
  const panel = row.querySelector('.img-panel');
  const img = row.querySelector('img');
  if (!panel || !img) return;
  // 关闭其它行已打开的裁剪层
  document.querySelectorAll('.crop-layer').forEach((el) => el.remove());
  const imgW = img.clientWidth, imgH = img.clientHeight;
  if (!imgW || !imgH) { showToast('图片尚未加载完成', 'warn'); return; }
  const layer = document.createElement('div');
  layer.className = 'crop-layer';
  const box = document.createElement('div');
  box.className = 'crop-box';
  for (const h of ['tl', 'tr', 'bl', 'br']) {
    const el = document.createElement('div');
    el.className = 'crop-handle ' + h;
    box.appendChild(el);
  }
  const actions = document.createElement('div');
  actions.className = 'crop-actions';
  const okBtn = document.createElement('button');
  okBtn.type = 'button'; okBtn.textContent = '裁剪插入';
  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button'; cancelBtn.textContent = '取消';
  actions.appendChild(okBtn); actions.appendChild(cancelBtn);
  layer.appendChild(box); layer.appendChild(actions);
  panel.appendChild(layer);

  let cur = {
    x: Math.round(imgW * 0.1), y: Math.round(imgH * 0.1),
    w: Math.round(imgW * 0.8), h: Math.round(imgH * 0.8),
  };
  function renderBox() {
    box.style.left = cur.x + 'px';
    box.style.top = cur.y + 'px';
    box.style.width = cur.w + 'px';
    box.style.height = cur.h + 'px';
  }
  renderBox();
  let drag = null; // {type:'move'|'resize', h?, sx, sy, ox, oy, ow, oh}
  box.addEventListener('mousedown', (ev) => {
    ev.preventDefault(); ev.stopPropagation();
    drag = { type: 'move', sx: ev.clientX, sy: ev.clientY, ox: cur.x, oy: cur.y };
  });
  box.querySelectorAll('.crop-handle').forEach((hEl) => {
    hEl.addEventListener('mousedown', (ev) => {
      ev.preventDefault(); ev.stopPropagation();
      drag = {
        type: 'resize', h: hEl.classList[1],
        sx: ev.clientX, sy: ev.clientY,
        ox: cur.x, oy: cur.y, ow: cur.w, oh: cur.h,
      };
    });
  });
  function onMove(ev) {
    if (!drag) return;
    const dx = ev.clientX - drag.sx, dy = ev.clientY - drag.sy;
    if (drag.type === 'move') {
      cur.x = Math.min(Math.max(0, drag.ox + dx), Math.max(0, imgW - 20));
      cur.y = Math.min(Math.max(0, drag.oy + dy), Math.max(0, imgH - 20));
    } else {
      let nx = drag.ox, ny = drag.oy, nw = drag.ow, nh = drag.oh;
      const h = drag.h;
      if (h.indexOf('r') >= 0) nw = drag.ow + dx;
      if (h.indexOf('l') >= 0) { nw = drag.ow - dx; nx = drag.ox + dx; }
      if (h.indexOf('b') >= 0) nh = drag.oh + dy;
      if (h.indexOf('t') >= 0) { nh = drag.oh - dy; ny = drag.oy + dy; }
      if (nw < 20) { if (h.indexOf('l') >= 0) nx = drag.ox + (drag.ow - 20); nw = 20; }
      if (nh < 20) { if (h.indexOf('t') >= 0) ny = drag.oy + (drag.oh - 20); nh = 20; }
      if (nx < 0) { nw += nx; nx = 0; }
      if (ny < 0) { nh += ny; ny = 0; }
      if (nx + nw > imgW) nw = imgW - nx;
      if (ny + nh > imgH) nh = imgH - ny;
      cur = { x: nx, y: ny, w: nw, h: nh };
    }
    renderBox();
  }
  function onUp() { drag = null; }
  function closeCrop() {
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
    layer.remove();
  }
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
  cancelBtn.addEventListener('click', closeCrop);
  okBtn.addEventListener('click', () => {
    if (cur.w < 5 || cur.h < 5) { showToast('选区过小', 'warn'); return; }
    // 用原图（/full/）裁剪更清晰；坐标按显示尺寸比例换算到原图像素
    const fullSrc = img.src.indexOf('/full/') >= 0
      ? img.src
      : img.src.replace('/preview/', '/full/');
    const c = document.createElement('canvas');
    const full = new Image();
    full.onload = () => {
      // 关键：坐标换算必须以原图（/full/）的自然尺寸为基准，绝不能按预览图
      // （img.naturalWidth）换算再对原图 drawImage——预览是低 DPI 缩略版
      // （preview_dpi=110 vs _FULL_DPI=220/分割原图），按预览图换算会导致
      // 从原图裁出的区域只有选区的 1/4（面积），即「截图与插入图不一致」。
      const fw = full.naturalWidth, fh = full.naturalHeight;
      if (!fw || !fh) { showToast('裁剪失败：原图尺寸无效', 'fail'); return; }
      // 选区坐标相对裁剪层（img-panel 含 padding），img 相对 panel 有 4px 偏移，
      // 需先减掉再按显示尺寸比例映射到原图像素。
      const offX = img.offsetLeft || 0, offY = img.offsetTop || 0;
      c.width = Math.max(1, Math.round(cur.w * fw / imgW));
      c.height = Math.max(1, Math.round(cur.h * fh / imgH));
      const ctx = c.getContext('2d');
      const sx = (cur.x - offX) * fw / imgW;
      const sy = (cur.y - offY) * fh / imgH;
      const sw = cur.w * fw / imgW;
      const sh = cur.h * fh / imgH;
      try {
        ctx.drawImage(full, sx, sy, sw, sh, 0, 0, c.width, c.height);
        // 小图/透明场景用 PNG，大图用 JPEG 控制体积
        const small = c.width * c.height < 400 * 400;
        const dataUrl = c.toDataURL(small ? 'image/png' : 'image/jpeg', 0.92);
        closeCrop();
        // 截图插入固定为行内模式（嵌在文字光标处），不随 imgModeSel 下拉框变化
        insertImageDataUrl(dataUrl, dataUrl.length, i, 'inline');
      } catch (e) { showToast('裁剪失败：' + e.message, 'fail'); }
    };
    full.onerror = () => { showToast('裁剪失败：原图加载失败', 'fail'); };
    full.src = fullSrc + '?v=' + loadNonce;
  });
}

// 从外部文件插入图片（工具栏「图片」组）：选择本地图片 → dataUrl → 光标处插入
(function initExternalImageInsert() {
  const btn = document.getElementById('imgExternalBtn');
  const input = document.getElementById('imgExternalInput');
  if (!btn || !input) return;
  btn.addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    const file = input.files && input.files[0];
    input.value = '';
    if (!file) return;
    const fr = new FileReader();
    fr.onload = () => {
      const ed = currentEditable();
      const row = ed ? ed.closest('.page-row') : null;
      insertImageDataUrl(fr.result, file.size, row ? Number(row.dataset.i) : null);
    };
    fr.onerror = () => showToast('读取图片失败', 'fail');
    fr.readAsDataURL(file);
  });
})();

// ---------- 编辑器内图片拖拽移动（2026-08-15） ----------
// 原生 contenteditable 拖放 <img> 会把 base64 src 作为可见文本插入，或把裸 <img>
// 拖出 <p class="ptoe-img-full ptoe-img-center"> 包裹（丢失全画幅/局部 class）。
// 这里在文档级接管拖拽：dragstart 记录被拖块，drop 时按落点插入克隆并移除原块。
let _dragImgBlock = null; // 被拖的整块（带 class 的 <p> 包裹，无包裹时为裸 <img>）
let _dragImgEd = null;    // 被拖块所在的编辑区
document.addEventListener('dragstart', function (e) {
  // 每次拖拽先清空旧状态（上次拖拽被取消时 drop 不会触发）
  _dragImgBlock = null;
  _dragImgEd = null;
  const t = e.target;
  if (!t || t.tagName !== 'IMG' || !t.closest('.editable')) return;
  _dragImgBlock = t.closest('p.ptoe-img-full, p.ptoe-img-fit') || t;
  _dragImgEd = t.closest('.editable');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', t.src); // 非空数据才能启动拖拽（勿 preventDefault）
});
document.addEventListener('dragover', function (e) {
  if (_dragImgBlock) e.preventDefault(); // 允许放置
});
document.addEventListener('drop', function (e) {
  if (!_dragImgBlock) return;
  e.preventDefault(); // 阻止浏览器把 base64 src 作为文本插入
  const srcEd = _dragImgEd;
  const dstEd = e.target.closest('.editable');
  try {
    if (!dstEd) return; // 落点不在编辑区 → 放弃（finally 清状态）
    const srcRow = srcEd ? srcEd.closest('.page-row') : null;
    const dstRow = dstEd.closest('.page-row');
    const iSrc = srcRow ? Number(srcRow.dataset.i) : -1;
    const iDst = dstRow ? Number(dstRow.dataset.i) : -1;
    const pagesArr = (iSrc >= 0 && iDst >= 0 && iSrc !== iDst) ? [iSrc, iDst]
      : (iSrc >= 0 ? [iSrc] : (iDst >= 0 ? [iDst] : []));
    histRun('移动图片', pagesArr, function () {
      // 落点光标：caretRangeFromPoint 非标准但 Chrome/Edge 均支持
      let range = null;
      if (document.caretRangeFromPoint) range = document.caretRangeFromPoint(e.clientX, e.clientY);
      if (!range) return; // 无法定位落点 → 放弃（histEnd 无变化不入栈）
      const startNode = range.startContainer;
      // 落点在被拖块内部 → 视为未移动（no-op）
      if (startNode === _dragImgBlock || _dragImgBlock.contains(startNode)) return;
      const el = startNode.nodeType === 3 ? startNode.parentNode : startNode;
      const hostBlock = el && el.closest ? el.closest('p, h1, h2, h3, h4, h5, h6, div') : null;
      const clone = _dragImgBlock.cloneNode(true);
      if (hostBlock && hostBlock !== _dragImgBlock) {
        // 落点在块内 → 插到该块之后（避免 p 套 p 非法嵌套）
        hostBlock.parentNode.insertBefore(clone, hostBlock.nextSibling);
      } else {
        range.insertNode(clone);
      }
      // 移除原块
      if (_dragImgBlock.parentNode) _dragImgBlock.parentNode.removeChild(_dragImgBlock);
      // 光标移到克隆之后
      const sel = window.getSelection();
      const r = document.createRange();
      r.setStartAfter(clone); r.collapse(true);
      sel.removeAllRanges(); sel.addRange(r);
      syncContent(dstEd);
      if (srcEd && srcEd !== dstEd) syncContent(srcEd);
    });
    // 受影响页行：标记脏 + 重测行高
    const affected = new Set();
    if (iSrc >= 0) affected.add(iSrc);
    if (iDst >= 0) affected.add(iDst);
    for (const idx of affected) { markDirty(idx); scheduleRemeasure(idx); }
  } finally {
    _dragImgBlock = null;
    _dragImgEd = null;
  }
});
// 拖拽被取消（Esc/移出窗口）时清理状态，避免残留影响下一次拖拽
document.addEventListener('dragend', function () {
  _dragImgBlock = null;
  _dragImgEd = null;
});

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

function toggleCitation(ed) {
  // Toggle ptoe-citation on all blocks in selection (斜体 + 独立字体)
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  histRun('引用格式', [i], function () {
    applyToSelectedBlocks(ed, function(block) {
      block.classList.toggle('ptoe-citation');
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
    else if (op === 'merge') _mergeSelectedBlocks(ed);
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
      if (ed) { ed.innerHTML = displayHtml(i); _reapplyProofread(i); remeasure(i); }
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
  // 逐页调用 /api/clean：段首符号、中英文标点、残留 HTML 标签
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
      if (ed) { ed.innerHTML = displayHtml(idx); _reapplyProofread(idx); scheduleRemeasure(idx); }
    }
    histEnd(before, '智能清理');
    for (let i = 0; i < pages.length; i++) editedSet.add(i);
    dirty = true;
    updateStatus();
    setStatus('已清理 ' + cleaned.length + ' 页');
    showToast('已清理 ' + cleaned.length + ' 页（段首符号 / 标点 / 标签）', 'ok');
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
  if (!q) { clearSearchHighlights(); return; } // query 已在 clearSearchHighlights 内置空
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
  _searchHighlightQuery = '';   // 先置空，否则 displayHtml 还原时会用旧 query 回注标记
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

// 标记只在点击「搜索」后应用（2026-08）；输入框清空时立即消除全部文字标记
document.getElementById('searchInput').addEventListener('input', function () {
  if (!(this.value || '').trim()) clearSearchHighlights();
});

// 清理搜索状态：清除全部文字标记 + 清空结果列表与计数 + 清空「x / y」位置显示（不动 #searchInput 的值）
function clearSearchState() {
  searchResults = [];
  searchCurrent = -1;
  clearSearchHighlights();          // 内部先置空 _searchHighlightQuery，还原不回注
  renderSearchResults([], 0, 200);  // 清空结果列表 + 计数
  updateSearchNav();                // 清空「x / y」位置显示
}
document.getElementById('searchClearBtn').addEventListener('click', clearSearchState);

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
  if (ed) { ed.innerHTML = displayHtml(i); _reapplyProofread(i); scheduleRemeasure(i); }
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
    if (ed) { ed.innerHTML = displayHtml(idx); _reapplyProofread(idx); scheduleRemeasure(idx); }
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
      body: JSON.stringify({ pages: collect(), proofread: collectProofread(), last_proofread_page: lastProofreadPage })
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
      body: JSON.stringify({ pages: collect(), proofread: collectProofread(), last_proofread_page: lastProofreadPage })
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
      body: JSON.stringify({ pages: collect(), name: loadedTitle || undefined, proofread: collectProofread(), last_proofread_page: lastProofreadPage })
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

// ---------- 历史记录弹窗（列表 / 单删 / 多选删 / 全部删 / 导出 / 导入） ----------
// 导出：把选中版本打包为 ZIP（每版本一个自包含 JSON，含预览图），可拷贝到其它电脑；
// 导入：读取导出的 JSON 或 ZIP 并落盘到本地历史缓存，供跨平台继续矫正。
async function exportHistoryVersion(id) {
  // 行内「导出」同样走 bulk ZIP 端点（单版本），保证导出格式统一为压缩包
  try {
    const res = await fetch('/api/history/export/bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: [id] }),
    });
    if (!res.ok) { let err = '导出失败'; try { const j = await res.json(); if (j && j.error) err = j.error; } catch (_) {} throw new Error(err); }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = id + '.zip';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast('已导出 ' + id + '.zip', 'ok');
  } catch (e) { showToast('导出失败：' + e, 'fail'); }
}
function _historyTimestamp() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds());
}
async function exportSelectedHistory() {
  try {
    const checks = document.querySelectorAll('.hist-check:checked');
    const ids = [...checks].map(c => c.dataset.id).filter(Boolean);
    if (!ids.length) { showToast('请先勾选要导出的历史记录', 'warn'); return; }
    const res = await fetch('/api/history/export/bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    });
    if (!res.ok) { let err = '导出失败'; try { const j = await res.json(); if (j && j.error) err = j.error; } catch (_) {} throw new Error(err); }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'ptoe_history_' + _historyTimestamp() + '.zip';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast('已导出 ' + ids.length + ' 个版本（ZIP）', 'ok');
  } catch (e) { showToast('导出失败：' + e, 'fail'); }
}
function importHistoryFile() { document.getElementById('historyImportFile').click(); }
function onHistoryImportFile(e) {
  const file = e.target && e.target.files && e.target.files[0];
  if (!file) return;
  const isZip = file.name.toLowerCase().endsWith('.zip');
  const finish = () => { document.getElementById('historyImportFile').value = ''; };
  if (isZip) {
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const bytes = new Uint8Array(reader.result);
        const CHUNK = 0x8000;
        let b64 = '';
        for (let i = 0; i < bytes.length; i += CHUNK) {
          b64 += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
        }
        const content_b64 = btoa(b64);
        const res = await fetch('/api/history/import', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: file.name, is_zip: true, content_b64 }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.ok === false) throw new Error(data && data.error ? data.error : '导入失败');
        const n = (data.ids || []).length;
        showToast('已导入 ' + n + ' 个版本' + (data.errors && data.errors.length ? '（' + data.errors.length + ' 个失败）' : ''), 'ok');
        loadHistory();
      } catch (err) { showToast('导入失败：' + err, 'fail'); }
      finally { finish(); }
    };
    reader.readAsArrayBuffer(file);
    return;
  }
  const fr = new FileReader();
  fr.onload = async () => {
    try {
      const content = JSON.parse(fr.result);
      const res = await fetchJSON('/api/history/import', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: file.name, content: content })
      });
      if (!res || res.ok === false) throw new Error((res && res.error) || '导入失败');
      showToast('已导入：' + (res.id || file.name), 'ok');
      loadHistory();
    } catch (err) { showToast('导入失败：' + err, 'fail'); }
    finally { finish(); }
  };
  fr.readAsText(file);
}
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
  const tdProof = document.createElement('td'); tdProof.style.padding = '6px 8px'; tdProof.style.color = '#5a6b7c'; tdProof.textContent = it.last_proofread_page ? '校正至第 ' + it.last_proofread_page + ' 页' : '-';
  const tdOp = document.createElement('td'); tdOp.style.padding = '6px 8px';
  const btn = document.createElement('button');
  btn.type = 'button'; btn.textContent = '打开';
  btn.title = '把该版本的文本重新载入编辑器进行再次矫正（覆盖当前未保存的修改）';
  btn.addEventListener('click', () => loadHistoryVersion(it.id, it.name, it.version || 1));
  const btnExport = document.createElement('button');
  btnExport.type = 'button'; btnExport.textContent = '导出';
  btnExport.title = '把该版本导出为 ZIP 压缩包（含预览图），可在其他电脑通过「导入」继续矫正';
  btnExport.addEventListener('click', () => exportHistoryVersion(it.id));
  tdOp.appendChild(btn);
  tdOp.appendChild(btnExport);
  tr.append(tdCheck, tdName, tdPath, tdVer, tdTime, tdProof, tdOp);
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
    // 恢复文字纠错状态（先重置再填充，防旧数据残留）
    proofreadErrors = {};
    proofreadOriginal = {};
    proofreadDismissed = {};
    lastProofreadPage = null;
    if (res.proofread) {
      try {
        // 页码→新索引映射（历史版本页数可能与当前会话不同）
        const pageIdx = new Map(newPages.map((p, idx) => [p.page, idx]));
        const remap = function (k) {
          const n = Number(k);
          if (pageIdx.has(n)) return pageIdx.get(n);
          // 旧格式兼容：key 为 pageIndex（整数且在新页数范围内）
          if (Number.isInteger(n) && n >= 0 && n < newPages.length) return n;
          return -1;
        };
        // errors：仅接受数组值，每项需有数字 start/end、字符串 wrong
        const srcErrors = res.proofread.errors || {};
        for (const k in srcErrors) {
          const idx = remap(k);
          if (idx < 0) continue;
          const arr = srcErrors[k];
          if (!Array.isArray(arr)) continue;
                const valid = arr.filter(function (e) {
                  return e && typeof e.start === 'number' && typeof e.end === 'number' && typeof e.wrong === 'string' && (!e.candidates || Array.isArray(e.candidates));
                });
          if (valid.length) proofreadErrors[idx] = JSON.parse(JSON.stringify(valid));
        }
        // original：仅接受字符串值
        const srcOrig = res.proofread.original || {};
        for (const k in srcOrig) {
          const idx = remap(k);
          if (idx < 0) continue;
          if (typeof srcOrig[k] === 'string') proofreadOriginal[idx] = srcOrig[k];
        }
        // dismissed：数组→Set
        const srcDismissed = res.proofread.dismissed || {};
        for (const k in srcDismissed) {
          const idx = remap(k);
          if (idx < 0) continue;
          const v = srcDismissed[k];
          proofreadDismissed[idx] = new Set(Array.isArray(v) ? v : []);
        }
      } catch (e) { /* 解析失败则保持清空 */ }
      lastProofreadPage = typeof res.last_proofread_page === 'number' ? res.last_proofread_page : null;
    }
    host.innerHTML = '';
    rebuildPrefix(); // heights 已重置，prefixH 需按 est 重建（旧累计值不可复用）
    host.style.height = totalHeight() + 'px';
    updateViewport();
    // 重注已挂载行的纠错标注
    for (const k in proofreadErrors) {
      if (proofreadErrors[k] && proofreadErrors[k].length) {
        const row = host.querySelector('.page-row[data-i="' + k + '"]');
        if (row) { const ed = row.querySelector('.editable'); if (ed) { ed.innerHTML = displayHtml(Number(k)); _reapplyProofread(Number(k)); } }
      }
    }
    dirty = true; updateStatus();
    closeHistory();
    loadedTitle = name.replace(/\.[^.\/\\]+$/, '');  // 去扩展名，无文件模式下作为 EPUB 标题
    setStatus('已从历史版本载入 ' + loaded.length + ' 页，可继续矫正（保存/完成将生成新版本）');
  } catch (e) { showToast('加载历史版本失败：' + (e && e.message ? e.message : e), 'fail'); }
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
document.getElementById('historyExportBtn').addEventListener('click', exportSelectedHistory);
document.getElementById('historyImportBtn').addEventListener('click', importHistoryFile);
document.getElementById('historyImportFile').addEventListener('change', onHistoryImportFile);

// ---------- 弹出快捷菜单（图标 + 悬停提示，置于选中文字正上方） ----------
function buildPopup() {
  popup.innerHTML = '';
  // 快捷菜单显示格式按钮（OPS 前 9 项：粗体/斜体/标题/正文/清除/注释/居左/居中/居右）；标记按钮已移至工具栏；规则按钮单独追加
  const ops = OPS.slice(0, 9);
  const groups = [ops.slice(0, 7), ops.slice(7)];
  groups.forEach((group, gi) => {
    if (gi > 0) { const d = document.createElement('div'); d.className = 'sep'; popup.appendChild(d); }
    for (const op of group.map(g => g[0])) {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'pop-btn'; b.dataset.op = op;
      b.innerHTML = OP_ICON[op] || op;
      b.setAttribute('aria-label', OP_TIP[op] || op);
      b.addEventListener('mouseenter', scheduleTip);
      b.addEventListener('mouseleave', hideTip);
      b.addEventListener('mousedown', (e) => {
        e.preventDefault();
        hideTip();
        suppressPopupUntil = performance.now() + 250;
        applyOp(op); hidePopup();
      });
      popup.appendChild(b);
    }
  });
  // 格式刷（单次模式）
  const paintBtn = document.createElement('button');
  paintBtn.type = 'button'; paintBtn.className = 'pop-btn'; paintBtn.id = 'popPaint';
  paintBtn.textContent = '刷';
  paintBtn.title = PAINT_TITLE;
  paintBtn.setAttribute('aria-label', '格式刷');
  if (paintActive) paintBtn.classList.add('active');
  paintBtn.addEventListener('mouseenter', scheduleTip);
  paintBtn.addEventListener('mouseleave', hideTip);
  paintBtn.addEventListener('mousedown', (e) => { e.preventDefault(); hideTip(); });
  paintBtn.addEventListener('click', () => {
    suppressPopupUntil = performance.now() + 250;
    if (paintActive) applyPaint(); else activatePaint();
  });
  popup.appendChild(paintBtn);
  // 规则按钮 + 子菜单（改为点击展开，不再依赖 hover）
  const sep = document.createElement('div'); sep.className = 'sep'; popup.appendChild(sep);
  const ruleWrap = document.createElement('div');
  ruleWrap.className = 'pop-rule-wrap';
  const ruleBtn = document.createElement('button');
  ruleBtn.type = 'button'; ruleBtn.className = 'pop-btn pop-rule-btn';
  ruleBtn.textContent = '规'; ruleBtn.title = '应用格式规则';
  ruleBtn.setAttribute('aria-label', '应用格式规则');
  ruleWrap.appendChild(ruleBtn);
  const ruleSub = document.createElement('div');
  ruleSub.className = 'pop-rule-sub';
  ruleSub.innerHTML = '<div class="ctx-empty">加载中…</div>';
  ruleWrap.appendChild(ruleSub);
  let _ruleSubOpen = false;
  function _toggleRuleSub() {
    _ruleSubOpen = !_ruleSubOpen;
    ruleSub.style.display = _ruleSubOpen ? 'block' : 'none';
    if (_ruleSubOpen) _refreshPopRuleSub(ruleSub);
  }
  ruleBtn.addEventListener('mousedown', (e) => {
    e.preventDefault();
    suppressPopupUntil = performance.now() + 250;
    hideTip();
  });
  ruleBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _toggleRuleSub();
  });
  // 点击 popup 外部（或规则子菜单项）自动关闭子菜单
  ruleSub.addEventListener('mousedown', (e) => {
    if (e.target.closest('.ctx-item')) {
      _ruleSubOpen = false;
      ruleSub.style.display = 'none';
    }
  });
  popup.appendChild(ruleWrap);
}
function _refreshPopRuleSub(box) {
  if (box.dataset.loaded) return;
  box.innerHTML = '<div class="ctx-empty">加载中…</div>';
  fetchJSON('/api/format_rules').then(function (res) {
    const rules = (res && res.rules) || [];
    box.innerHTML = '';
    if (!rules.length) {
      const empty = document.createElement('div');
      empty.className = 'ctx-empty';
      empty.textContent = '暂无规则';
      box.appendChild(empty);
      return;
    }
    rules.forEach(function (rule) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ctx-item';
      btn.textContent = rule.name || '（未命名规则）';
      btn.title = '应用规则「' + (rule.name || '') + '」';
      btn.addEventListener('mousedown', (e) => {
        e.preventDefault();
        e.stopPropagation();
        suppressPopupUntil = performance.now() + 250;
        hidePopup();
        const ed = currentEditable();
        if (ed) applyFormatRule(rule, ed);
      });
      box.appendChild(btn);
    });
    box.dataset.loaded = '1';
  }).catch(function () {
    box.innerHTML = '<div class="ctx-empty">加载失败</div>';
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
// 保存最近一次在 .editable 内的选区，供 insertImage 在光标处插入（而非末尾）
let _lastEditableRange = null;
document.addEventListener('selectionchange', () => {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) hidePopup();
  hideErrPopup();
  // 记录选区（仅在 .editable 内且非折叠时）
  if (sel && sel.rangeCount > 0 && !sel.isCollapsed) {
    const node = sel.getRangeAt(0).commonAncestorContainer;
    const el = node && node.nodeType === 3 ? node.parentNode : node;
    if (el && el.closest && el.closest('.editable')) {
      _lastEditableRange = sel.getRangeAt(0).cloneRange();
    }
  }
});

// ---------- 右键上下文菜单（2026-08-08） ----------
// 编辑区内右键弹出自定义菜单（重识别/插入标记/导出/Markdown 提示/保存/暂存），
// 编辑区外保留浏览器默认右键菜单。打开时抑制选中文字快捷菜单（suppressPopupUntil），
// Esc / 滚动 / 点击外部均关闭；二级菜单 hover 展开、点击父项切换。
let ctxMenuOpen = false;
const ctxMenu = document.getElementById('contextMenu');
let _ctxEscCapture = null; // 菜单打开期间的临时 Esc 捕获监听（capture 阶段先于既有 bubble 快捷键分发）
// 2026-08-09：右键目标页/光标位置。右键打开菜单不改变焦点/选区，页级操作
// （清除/重识别/应用/插入标记）若用 currentEditable() 会取到光标所在页而非被右键页
// （曾致右键「清除」清不掉被右键页的纠错标注）。ctxRun 捕获-关闭-恢复-执行-清空，
// 使 fn 同步段内可读右键目标，工具栏直调（不经 ctxRun）不受影响。
let _ctxEditable = null; // 被右键的 .editable（右键目标页）
let _ctxRange = null;    // 右键位置 caretRangeFromPoint 的 range（标记插入精确定位，jsdom 无则 null）

function closeContextMenu() {
  ctxMenuOpen = false;
  ctxMenu.hidden = true;
  if (typeof _ctxCancelTimers === 'function') _ctxCancelTimers(); // 关菜单时取消 hover-intent 延时
  ctxMenu.querySelectorAll('.ctx-sub.open').forEach(el => el.classList.remove('open'));
  if (_ctxEscCapture) { document.removeEventListener('keydown', _ctxEscCapture, true); _ctxEscCapture = null; }
  _ctxEditable = null; // 防陈旧右键目标泄漏到后续工具栏操作
  _ctxRange = null;
}

function toggleCtxSub(parent) {
  const wasOpen = parent.classList.contains('open');
  ctxMenu.querySelectorAll('.ctx-sub.open').forEach(el => el.classList.remove('open'));
  if (!wasOpen) {
    parent.classList.add('open');
    orientCtxSubs(); // 展开后按实测尺寸定向（打开前 display:none 量不到）
  }
}

// 二级菜单展开方向：右缘不足向左展开、向左后左缘不足再翻回、下缘不足向上对齐。
// 打开时（hover / 点击 / 右键开菜单）都要重跑：display:none 状态量不到真实尺寸。
function orientCtxSubs() {
  ctxMenu.querySelectorAll('.ctx-sub').forEach(sub => {
    const sm = sub.querySelector('.ctx-submenu');
    if (!sm) return;
    const pr = sub.getBoundingClientRect();
    // 未展开时 offsetWidth/Height 为 0 → 用兜底估算值（与 CSS min-width 一致）
    const sw = sm.offsetWidth || 150, sh = sm.offsetHeight || 160;
    // 横向：默认向右；右侧放不下则向左；向左后左缘越界（左侧更挤）则翻回向右
    let left = pr.right + 4;
    if (left + sw > window.innerWidth) {
      if (pr.left - 4 - sw >= 0) sm.classList.add('ctx-left');
      else sm.classList.remove('ctx-left');
    } else {
      sm.classList.remove('ctx-left');
    }
    // 纵向：默认顶边与父项对齐（top:-5px）；下缘越界则改为底边对齐（.ctx-up）
    if (pr.top - 5 + sh > window.innerHeight && pr.bottom + 5 - sh >= 0) sm.classList.add('ctx-up');
    else sm.classList.remove('ctx-up');
  });
}

function openContextMenu(x, y) {
  hidePopup();
  hideErrPopup();
  closeProofreadMenu();
  refreshCtxRulesSub(); // 每次打开刷新「添加规则」二级菜单（异步填充规则名列表）
  suppressPopupUntil = performance.now() + 300; // 右键后的 mouseup 不弹选中菜单
  orientCtxSubs();
  ctxMenu.hidden = false;
  const w = ctxMenu.offsetWidth || 172, h = ctxMenu.offsetHeight || 240;
  const cx = Math.max(8, Math.min(x, window.innerWidth - w - 8)); // clamp 到视口 8px 边距
  const cy = Math.max(8, Math.min(y, window.innerHeight - h - 8));
  ctxMenu.style.left = cx + 'px';
  ctxMenu.style.top = cy + 'px';
  orientCtxSubs(); // 定位后按最终位置重算二级菜单方向（边缘裁切修复）
  ctxMenuOpen = true;
  if (!_ctxEscCapture) {
    _ctxEscCapture = (e) => {
      if (e.key === 'Escape' && ctxMenuOpen) {
        e.preventDefault();
        e.stopPropagation(); // 菜单打开期间 Esc 只关菜单：拦截既有快捷键分发（capture 先于 bubble）
        closeContextMenu();
      }
    };
    document.addEventListener('keydown', _ctxEscCapture, true);
  }
}

// 菜单项执行：先关菜单再执行，异常 toast。
// 捕获-关闭-恢复-执行-清空：closeContextMenu 会清掉 _ctxEditable/_ctxRange，
// 故先取出保存、关菜单后恢复，fn 同步段内可用 ctxTargetEditable() 取右键目标页；
// finally 清空防止泄漏（fn 内异步操作不应依赖右键目标）。
function ctxRun(fn) {
  const target = _ctxEditable;
  const range = _ctxRange;
  closeContextMenu();
  _ctxEditable = target;
  _ctxRange = range;
  suppressPopupUntil = performance.now() + 300;
  try { fn(); } catch (e) { showToast('操作失败：' + e.message, 'fail'); }
  finally { _ctxEditable = null; _ctxRange = null; }
}
// 右键菜单页级操作目标 = 被右键的页；无右键目标（工具栏直调）回退当前光标/选区页
function ctxTargetEditable() {
  return _ctxEditable || currentEditable();
}
// 插入标记依赖光标位置（insertMarker 用当前 selection）：优先用右键目标页；
// 若光标不在右键页，把光标移到右键位置（无 caretRangeFromPoint 时落到页首）
function ctxMarkerInsert(type) {
  ctxRun(() => {
    const ed = ctxTargetEditable();
    if (!ed) { showToast('请先点击某一页的文字', 'warn'); return; }
    if (_ctxEditable && ed !== currentEditable()) {
      ed.focus();
      const sel = window.getSelection();
      const range = (_ctxRange && ed.contains(_ctxRange.startContainer)) ? _ctxRange : (() => {
        const r = document.createRange();
        r.selectNodeContents(ed);
        r.collapse(true);
        return r;
      })();
      sel.removeAllRanges();
      sel.addRange(range);
    }
    insertMarker('marker_' + type);
  });
}
function ctxExportRun(fmt) { ctxRun(() => exportFile(fmt)); }
// 右键菜单「添加规则」二级菜单：列出已保存格式规则，点击即应用到右键目标页。
// 每次打开菜单时刷新（fetch /api/format_rules，fire-and-forget）；子菜单 hover
// 才展开，异步填充通常已就绪。空列表显示「暂无规则」。
let _ctxFormatRules = [];
function refreshCtxRulesSub() {
  const box = document.getElementById('ctxRulesSub');
  if (!box) return;
  box.innerHTML = '<div class="ctx-empty">加载中…</div>';
  fetchJSON('/api/format_rules').then(function (res) {
    const rules = (res && res.rules) || [];
    _ctxFormatRules = rules;
    box.innerHTML = '';
    if (!rules.length) {
      const empty = document.createElement('div');
      empty.className = 'ctx-empty';
      empty.textContent = '暂无规则';
      box.appendChild(empty);
      return;
    }
    rules.forEach(function (rule, idx) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ctx-item';
      btn.dataset.ctxRule = String(idx);
      btn.textContent = rule.name || '（未命名规则）';
      btn.title = '应用规则「' + (rule.name || '') + '」到当前页';
      box.appendChild(btn);
    });
  }).catch(function () {
    box.innerHTML = '<div class="ctx-empty">加载失败</div>';
  });
}
// 右键菜单快速应用规则到右键目标页：光标不在右键页时先移入（同 ctxMarkerInsert），
// 然后 applyFormatRule(rule, ed)（传入 edArg 跳过 restoreFrRange——右键场景不恢复
// 弹窗捕获的选区）。
function ctxApplyFormatRule(rule) {
  const ed = ctxTargetEditable();
  if (!ed) { showToast('请先点击某一页的文字', 'warn'); return; }
  if (_ctxEditable && ed !== currentEditable()) {
    ed.focus();
    const sel = window.getSelection();
    const range = (_ctxRange && ed.contains(_ctxRange.startContainer)) ? _ctxRange : (() => {
      const r = document.createRange();
      r.selectNodeContents(ed);
      r.collapse(true);
      return r;
    })();
    sel.removeAllRanges();
    sel.addRange(range);
  }
  applyFormatRule(rule, ed);
}

// 菜单点击分发（委托）
ctxMenu.addEventListener('click', (e) => {
  // 添加规则 二级菜单叶子项：点击规则名 → 快速应用到右键目标页
  const ruleBtn = e.target.closest('.ctx-submenu .ctx-item[data-ctx-rule]');
  if (ruleBtn) {
    e.stopPropagation();
    const idx = parseInt(ruleBtn.dataset.ctxRule, 10);
    const rule = _ctxFormatRules[idx];
    if (!rule) { showToast('规则不存在，可能已被删除', 'warn'); return; }
    ctxRun(() => ctxApplyFormatRule(rule));
    return;
  }
  // 二级菜单叶子项（插入标记 / 导出 子项）
  const subBtn = e.target.closest('.ctx-submenu .ctx-item');
  if (subBtn) {
    e.stopPropagation();
    const mk = subBtn.dataset.ctxMarker;
    if (mk) { ctxMarkerInsert(mk); return; }
    const ex = subBtn.dataset.ctxExport;
    if (ex) { ctxExportRun(ex); return; }
    return;
  }
  // 一级父项（插入标记 / 导出）：点击切换二级菜单展开
  const parent = e.target.closest('.ctx-item.ctx-sub');
  if (parent) { e.stopPropagation(); toggleCtxSub(parent); return; }
  const item = e.target.closest('.ctx-item[data-ctx]');
  if (!item) return;
  const kind = item.dataset.ctx;
  if (kind === 'reocr') ctxRun(runReocr);
  else if (kind === 'clear') ctxRun(proofreadClearCurrent);
  else if (kind === 'md') ctxRun(() => showToast('单页 Markdown 编辑暂未实现。当前可使用工具栏 Markdown 按钮切换全局模式（会影响全部页面）。建议后续按页记录 md 状态并随历史持久化。', 'warn'));
  else if (kind === 'save') ctxRun(save);
  else if (kind === 'stage') ctxRun(stage);
});

// 二级菜单 hover 展开（hover-intent，2026-08-09）：
// 移入父项 200ms 后才展开（划过不误开）；移出后 300ms 宽限再收起——鼠标斜穿
// 父项与子菜单之间的 4px 间隙（CSS ::before 桥 + 本延时）不会中途关闭。
// 移入另一父项时立即互斥收起旧的；relatedTarget 仍在本 .ctx-sub 内则不收起。
const CTX_HOVER_OPEN_MS = 200;
const CTX_HOVER_CLOSE_MS = 300;
let _ctxOpenTimer = null;
let _ctxCloseTimer = null;
function _ctxCancelTimers() {
  if (_ctxOpenTimer) { clearTimeout(_ctxOpenTimer); _ctxOpenTimer = null; }
  if (_ctxCloseTimer) { clearTimeout(_ctxCloseTimer); _ctxCloseTimer = null; }
}
function openCtxSub(sub) {
  ctxMenu.querySelectorAll('.ctx-sub.open').forEach(el => { if (el !== sub) el.classList.remove('open'); });
  sub.classList.add('open');
  orientCtxSubs(); // 展开即定向（边缘裁切修复）
}
ctxMenu.querySelectorAll('.ctx-sub').forEach(sub => {
  sub.addEventListener('mouseenter', () => {
    _ctxCancelTimers();
    if (sub.classList.contains('open')) return;
    _ctxOpenTimer = setTimeout(() => { _ctxOpenTimer = null; openCtxSub(sub); }, CTX_HOVER_OPEN_MS);
  });
  sub.addEventListener('mouseleave', (e) => {
    if (_ctxOpenTimer) { clearTimeout(_ctxOpenTimer); _ctxOpenTimer = null; }
    // 移向自己的子菜单（子孙节点）不关闭
    if (e && e.relatedTarget && sub.contains(e.relatedTarget)) return;
    if (_ctxCloseTimer) clearTimeout(_ctxCloseTimer);
    _ctxCloseTimer = setTimeout(() => { _ctxCloseTimer = null; sub.classList.remove('open'); }, CTX_HOVER_CLOSE_MS);
  });
  const sm = sub.querySelector('.ctx-submenu');
  if (sm) {
    // 宽限期内进入子菜单 → 取消收起
    sm.addEventListener('mouseenter', () => { _ctxCancelTimers(); sub.classList.add('open'); });
  }
});

// 编辑区内右键：阻止默认 + 收起选中菜单 + 抑制 mouseup 弹窗 + 打开自定义菜单；编辑区外不干预
document.addEventListener('contextmenu', (e) => {
  const ed = e.target.closest('.editable');
  if (ed) {
    e.preventDefault();
    hidePopup();
    suppressPopupUntil = performance.now() + 300;
    _ctxEditable = ed; // 记录右键目标页：页级操作（清除/重识别/应用/插入标记）与光标位置解耦
    _ctxRange = null;
    if (document.caretRangeFromPoint) {
      const r = document.caretRangeFromPoint(e.clientX, e.clientY);
      if (r && ed.contains(r.startContainer)) _ctxRange = r;
    }
    openContextMenu(e.clientX, e.clientY);
  }
});

// 菜单外 mousedown 关闭（再次右键别处：mousedown 先关 → contextmenu 在新位置重开）
document.addEventListener('mousedown', (e) => {
  if (ctxMenuOpen && !e.target.closest('#contextMenu')) closeContextMenu();
});
// 菜单内 mousedown：阻止默认（保持编辑区选区/光标，供标记插入使用）+ 抑制选中菜单弹出
ctxMenu.addEventListener('mousedown', (e) => {
  e.preventDefault();
  suppressPopupUntil = performance.now() + 300;
});

// ---------- 格式刷（单次模式，Word 风格） ----------
// 选中含格式的文本 → 点「刷」捕获格式 → 再选目标文字 → 再点「刷」应用（单次即止）
let paintActive = false;
let paintSource = null; // 格式描述对象 {bold, italic, underline, strike, sup, sub, note}
const PAINT_TITLE = '格式刷：复制所选文字的格式，再选择目标文字应用';

// 捕获当前选区起点/终点的格式：Text 节点取 parentElement，向父链上溯到最近的格式元素，
// 用 getComputedStyle 判定（起点/终点任一满足即 true）
function captureFormat() {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  const fmt = { bold: false, italic: false, underline: false, strike: false, sup: false, sub: false, note: false };
  const probe = (node) => {
    if (!node) return;
    if (node.nodeType === 3) node = node.parentElement;
    let el = node;
    while (el && el !== document.body && !(el.classList && el.classList.contains('editable'))) {
      const cs = window.getComputedStyle(el);
      if (!fmt.bold && (parseFloat(cs.fontWeight) >= 600 || el.closest('strong, b'))) fmt.bold = true;
      if (!fmt.italic && (cs.fontStyle === 'italic' || el.closest('em, i'))) fmt.italic = true;
      if (!fmt.underline && (cs.textDecorationLine.indexOf('underline') !== -1 || el.closest('u'))) fmt.underline = true;
      if (!fmt.strike && (cs.textDecorationLine.indexOf('line-through') !== -1 || el.closest('s, strike, del'))) fmt.strike = true;
      if (!fmt.sup && (cs.verticalAlign === 'super' || el.closest('sup'))) fmt.sup = true;
      if (!fmt.sub && (cs.verticalAlign === 'sub' || el.closest('sub'))) fmt.sub = true;
      if (!fmt.note && el.classList && el.classList.contains('ptoe-note')) fmt.note = true;
      el = el.parentElement;
    }
  };
  probe(range.startContainer);
  probe(range.endContainer);
  return fmt;
}

// 对目标 range 应用格式：行内格式走 execCommand（withScrollStable 防滚动跳页），
// 注释走既有 applyToSelectedBlocks 机制（与「注释」按钮同路径）
function applyFormat(fmt, range) {
  if (!fmt || !range) return;
  const sel = window.getSelection();
  if (!sel) return;
  sel.removeAllRanges();
  sel.addRange(range);
  if (fmt.bold) withScrollStable(() => document.execCommand('bold'));
  if (fmt.italic) withScrollStable(() => document.execCommand('italic'));
  if (fmt.underline) withScrollStable(() => document.execCommand('underline'));
  if (fmt.strike) withScrollStable(() => document.execCommand('strikeThrough'));
  if (fmt.sup) withScrollStable(() => document.execCommand('superscript'));
  if (fmt.sub) withScrollStable(() => document.execCommand('subscript'));
  if (fmt.note) {
    const ed = currentEditable();
    if (ed) applyToSelectedBlocks(ed, function(block) { block.classList.add('ptoe-note'); });
  }
}

function activatePaint() {
  const fmt = captureFormat();
  if (!fmt) { showToast('请先选中含有格式的文本以捕获格式', 'warn'); return; }
  paintSource = fmt;
  paintActive = true;
  const b = document.getElementById('popPaint');
  if (b) { b.classList.add('active'); b.title = '点击应用到所选文字'; }
  setStatus('格式刷已激活：请选择要应用格式的文字');
  hidePopup();
  suppressPopupUntil = performance.now() + 250; // 与既有抑制机制同时间基准（performance.now）
  document.body.classList.add('paint-mode');
}

function applyPaint() {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) { showToast('请先选择要应用格式的文字', 'warn'); return; }
  const range = sel.getRangeAt(0);
  applyFormat(paintSource, range);
  paintActive = false;
  paintSource = null;
  const b = document.getElementById('popPaint');
  if (b) { b.classList.remove('active'); b.title = PAINT_TITLE; }
  document.body.classList.remove('paint-mode');
  updateStatus(); // 恢复状态栏（保持选区不 collapse，用户可看到效果）
}

function cancelPaint() {
  paintActive = false;
  paintSource = null;
  const b = document.getElementById('popPaint');
  if (b) { b.classList.remove('active'); b.title = PAINT_TITLE; }
  document.body.classList.remove('paint-mode');
  updateStatus();
}

// Esc 取消格式刷（与既有 Escape 处理器并存，互不干扰；纠错悬浮窗可见时优先走 errNo）
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && paintActive && !errKey) cancelPaint();
});
// 点击编辑区外（且不在 popup 内）→ 取消格式刷
document.addEventListener('mousedown', (e) => {
  if (!paintActive) return;
  const t = e.target;
  if (t && t.closest && (t.closest('.editable') || t.closest('#popup'))) return;
  cancelPaint();
});

// ---------- 文字纠错（proofread） ----------
// 视图层叠加标注（仿搜索高亮机制），不进 undo 快照；服务端 proofread_page 检测错误。
let proofreadErrors = {};   // pageIndex → errors 数组
let proofreadDismissed = {}; // pageIndex → Set('start:wrong')
let proofreadOriginal = {};  // pageIndex → 校正前的 innerHTML 快照（用于回退）
let lastProofreadPage = null;  // 最后一次校正/重识别的真实页码（1-based，用于历史记录显示）
let errKey = null; // 当前悬浮窗对应的 {i, k}
let proofreadMenuOpen = false; // 下拉菜单是否展开
let proofreadLlmEnabled = false; // LLM 深度校对开关（服务端持久化 config.json，随机端口下 localStorage 每运行失效）
let proofreadLlmModel = '';      // 深度校对模型 key（'' = 跟随 selected_model）
let proofreadLegacyRules = false; // 原有规则开关（默认关：校正只跑三条新规则；服务端持久化）

// 纠错当前编辑行：调 /api/proofread → 叠加标注（不入 undo）
async function runProofread() {
  const ed = currentEditable();
  if (!ed) { showToast('请先点击某一页的文字', 'warn'); return; }
  const row = ed.closest('.page-row');
  if (!row) return;
  const i = Number(row.dataset.i);
  // 先清旧标注（clearProofread 会连带清掉旧快照/忽略集），再取本轮校正前快照——
  // 顺序不可颠倒：clearProofread 内 delete proofreadOriginal[i]（2026-08-09 清除 bug 修复）。
  // 快照取清理后的 HTML（不含 .ptoe-err/.ptoe-fix），「回退」恢复得到干净原文。
  if (proofreadErrors[i]) clearProofread(i, true);
  proofreadOriginal[i] = ed.innerHTML;
  try {
    const res = await fetchJSON('/api/proofread', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        html: ed.innerHTML,
        // 可选 LLM 增强：读自内存变量（服务端 /api/proofread_settings 持久化）
        use_llm: proofreadLlmEnabled,
        llm_model: proofreadLlmModel,
      }),
    });
    if (!res.ok) { showToast('纠错失败: ' + (res.error || '未知错误'), 'fail'); return; }
    let errors = res.errors || [];
    if (res.llm_error) showToast('深度校对失败: ' + res.llm_error, 'warn');
    // 过滤已忽略的条目
    if (proofreadDismissed[i]) {
      errors = errors.filter(function (e) { return !proofreadDismissed[i].has(e.start + ':' + e.wrong); });
    }
    proofreadErrors[i] = errors;
    lastProofreadPage = pages[i].page;
    if (errors.length) {
      renderProofread(i);
      scheduleRemeasure(i);
      setStatus('找到 ' + errors.length + ' 处疑似错误');
      showToast('找到 ' + errors.length + ' 处疑似错误', 'ok');
    } else {
      setStatus('未发现明显错误');
      showToast('未发现明显错误', 'ok');
    }
  } catch (e) {
    showToast('纠错失败: ' + e.message, 'fail');
  }
}

// 把错误标注叠加到指定行的文本节点上（删除线 + 候选字）
// 支持跨多个文本节点的 wrong：每段包成独立 sEl（同 data-err-i），fixEl 只在首段插一次
function renderProofread(i) {
  const row = host.children ? [...host.children].find(function (r) { return Number(r.dataset.i) === i; }) : null;
  if (!row) return;
  const ed = row.querySelector('.editable');
  if (!ed) return;
  const errors = proofreadErrors[i] || [];
  if (!errors.length) return;
  // P2: 保存光标（相对 ed 文本偏移，排除 .ptoe-fix/.ptoe-marker 的展示文本）——重建后恢复，防 IME 光标跳段首
  let savedSelOffset = -1;
  try {
    const sel = window.getSelection();
    if (sel && sel.rangeCount > 0 && ed.contains(sel.anchorNode)) {
      const acc = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT, {
        acceptNode: function (node) {
          if (node.parentElement && node.parentElement.closest('.ptoe-fix, .ptoe-marker')) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      let cum = 0, tn;
      while ((tn = acc.nextNode())) {
        if (tn === sel.anchorNode) { savedSelOffset = cum + sel.anchorOffset; break; }
        cum += tn.textContent.length;
      }
      if (savedSelOffset < 0) savedSelOffset = cum;
    }
  } catch (e) { savedSelOffset = -1; }
  // 幂等：先清除本页既有标注（与 clearProofread 同构），避免重复叠加
  const _fixes = ed.querySelectorAll('.ptoe-fix');
  for (const el of _fixes) el.parentNode.removeChild(el);
  const _errs = ed.querySelectorAll('.ptoe-err');
  for (const el of _errs) {
    const t = document.createTextNode(el.textContent);
    el.parentNode.replaceChild(t, el);
  }
  ed.normalize();
  // 倒序处理，避免 DOM 修改影响后续偏移
  for (let k = errors.length - 1; k >= 0; k--) {
    const err = errors[k];
    if (err._gone) continue; // F5: 已标记消失的条目跳过渲染
    // Phase 1: 收集与 [err.start, err.end) 相交的文本节点（不修改树，偏移稳定）
    const segs = [];
    {
      const walker2 = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT, {
        acceptNode: function (node) {
          if (node.parentElement && node.parentElement.closest('.ptoe-err, .ptoe-fix, .ptoe-marker')) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      let cum2 = 0, tn2;
      while ((tn2 = walker2.nextNode())) {
        const len = tn2.textContent.length;
        const segStart = Math.max(0, err.start - cum2);
        const segEnd = Math.min(len, err.end - cum2);
        if (segStart < segEnd) segs.push({ node: tn2, segStart: segStart, segEnd: segEnd });
        cum2 += len;
        if (cum2 >= err.end) break;
      }
    }
    // Phase 2: 逐节点包裹（收集期间树未改，偏移有效；wrong 文本跨节点时每段一个 sEl）
    let firstSeg = true;
    for (const s of segs) {
      const tn3 = s.node, segStart = s.segStart, segEnd = s.segEnd;
      const text = tn3.textContent;
      const before = text.slice(0, segStart);
      const segText = text.slice(segStart, segEnd);
      const after = text.slice(segEnd);
      const frag = document.createDocumentFragment();
      if (before) frag.appendChild(document.createTextNode(before));
      const sEl = document.createElement('s');
      sEl.className = 'ptoe-err';
      sEl.setAttribute('data-err-i', k);
      sEl.textContent = segText;
      frag.appendChild(sEl);
      // fixEl 只在首段插一次（candidates 必须是数组，否则安全跳过）
      if (firstSeg && Array.isArray(err.candidates) && err.candidates.length) {
        const fixEl = document.createElement('span');
        fixEl.className = 'ptoe-fix';
        fixEl.setAttribute('data-err-i', k);
        fixEl.textContent = err.candidates.join('/');
        frag.appendChild(fixEl);
      }
      firstSeg = false;
      if (after) frag.appendChild(document.createTextNode(after));
      tn3.parentNode.replaceChild(frag, tn3);
    }
  }
  // C: 重建后同步基准文本（供 _proofreadAutoDismiss delta-rebase 使用）
  _prTextBefore[i] = _plainNoAnno(ed);
  updatePrCount();
  // P2: 恢复光标（按保存的字符偏移定位文本节点）
  if (savedSelOffset >= 0) {
    try {
      const w2 = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT, {
        acceptNode: function (node) {
          if (node.parentElement && node.parentElement.closest('.ptoe-fix, .ptoe-marker')) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      let cum2 = 0, target = savedSelOffset, hit = null, off = 0, tn2;
      while ((tn2 = w2.nextNode())) {
        const len = tn2.textContent.length;
        if (cum2 + len >= target) { hit = tn2; off = target - cum2; break; }
        cum2 += len;
      }
      if (!hit) { hit = ed; off = ed.childNodes.length; }
      const r = document.createRange();
      r.setStart(hit, off);
      r.collapse(true);
      const s2 = window.getSelection();
      s2.removeAllRanges();
      s2.addRange(r);
    } catch (e) { /* 恢复失败不阻塞 */ }
  }
}

// 清除指定行的全部错误标注（wrong 文本保留原位）
// 2026-08-09 修复「清除后保存、载入历史版本建议复现」：除清空 proofreadErrors 外，
// 还需清掉 proofreadOriginal（否则 collectProofread 仍把陈旧快照写进历史）与
// proofreadDismissed，并 syncContent 把去标注后的 HTML 同步回 contentMap
// （否则 collect() 保存的仍是含 .ptoe-fix/.ptoe-err 的旧 HTML）。
// keepDismissed=true：仅供 runProofread 重新校正前内部清理使用（保留「已忽略」记忆，
// 否则用户点 ✗ 忽略过的条目会在下一轮校正中重新冒出来）。
function clearProofread(i, keepDismissed) {
  const row = host.children ? [...host.children].find(function (r) { return Number(r.dataset.i) === i; }) : null;
  if (!row) return;
  const ed = row.querySelector('.editable');
  if (!ed) return;
  const fixes = ed.querySelectorAll('.ptoe-fix');
  for (const el of fixes) el.parentNode.removeChild(el);
  const errs = ed.querySelectorAll('.ptoe-err');
  for (const el of errs) {
    const t = document.createTextNode(el.textContent);
    el.parentNode.replaceChild(t, el);
  }
  ed.normalize();
  proofreadErrors[i] = [];
  delete proofreadOriginal[i];
  if (!keepDismissed) delete proofreadDismissed[i];
  delete _prTextBefore[i];
  syncContent(ed);
}

// 反馈回写（fire-and-forget）：accept 上报采纳 / ignore 上报忽略；失败静默
function proofreadFeedbackAccept(wrong, fixed) {
  if (!wrong || !fixed || wrong === fixed) return;
  fetch('/api/proofread_feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'accept', wrong: wrong, fixed: fixed }),
  }).catch(function (e) { console.warn('proofread feedback accept failed', e); });
}
function proofreadFeedbackIgnore(wrong) {
  if (!wrong) return;
  fetch('/api/proofread_feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'ignore', wrong: wrong }),
  }).catch(function (e) { console.warn('proofread feedback ignore failed', e); });
}
// 批量 accept（proofreadApplyCurrent 用）
function proofreadFeedbackAcceptBatch(items) {
  if (!items || !items.length) return;
  fetch('/api/proofread_feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'accept', items: items }),
  }).catch(function (e) { console.warn('proofread feedback accept batch failed', e); });
}

// 重建行后若该页有标注数据，重新叠加（与搜索高亮回注同构）
const _displayHtmlOrig = displayHtml;
displayHtml = function (i) {
  const html = _displayHtmlOrig(i);
  return html;
};
function _reapplyProofread(i) {
  if (proofreadErrors[i] && proofreadErrors[i].length) {
    renderProofread(i);
  }
}

// 取去除标注后的纯文本（用于重锚定偏移）
// B: 同时剥离 .ptoe-marker（与渲染 walker 及服务端 _proofread_plain_text 偏移基准一致）
// 注意：.ptoe-err 内含 wrong 原文、.ptoe-fix 内含候选文本——均需解包保留原文，仅去掉标签
function _plainNoAnno(ed) {
  const c = ed.cloneNode(true);
  c.querySelectorAll('.ptoe-marker').forEach(function (el) { el.remove(); });
  c.querySelectorAll('.ptoe-fix').forEach(function (el) { el.parentNode.removeChild(el); });
  c.querySelectorAll('.ptoe-err').forEach(function (el) {
    const t = document.createTextNode(el.textContent);
    el.parentNode.replaceChild(t, el);
  });
  c.normalize();
  return c.textContent || '';
}

// C: 基准缓存——renderProofread 后同步，供 _proofreadAutoDismiss delta-rebase 使用
let _prTextBefore = {};
let _prRenderPending = {};   // IME composition 期间累积的待重建标记（skipRender=true 时置位，compositionend 后 _flushPrPending 统一重建）

// D1: 深拷贝 proofreadErrors[i]（条目为纯对象）
function _copyErrors(i) {
  return (typeof proofreadErrors[i] === 'undefined') ? undefined : JSON.parse(JSON.stringify(proofreadErrors[i]));
}

// C: delta-rebase 重锚定——基于编辑前后 diff 精确修正偏移，避免全局 indexOf 误锚
function _proofreadAutoDismiss(ed, i, skipRender) {
  const errors = proofreadErrors[i];
  if (!errors || !errors.length) return;
  const after = _plainNoAnno(ed);
  const before = _prTextBefore[i];
  let changed = false;
  if (typeof before === 'string' && before !== after) {
    // 编辑区间：公共前缀 p；公共后缀 → before 编辑区间 [p, be)、after 编辑区间 [p, ae)
    let p = 0;
    const n = Math.min(before.length, after.length);
    while (p < n && before[p] === after[p]) p++;
    let be = before.length, ae = after.length;
    while (be > p && ae > p && before[be - 1] === after[ae - 1]) { be--; ae--; }
    const delta = (ae - p) - (be - p);
    for (const err of errors) {
      if (err._gone) continue;
      if (err.end <= p) continue;                     // 编辑点之前：不动
      if (err.start >= be) {                          // 编辑点之后：整体平移
        err.start += delta; err.end += delta;
        // 平移后校验 wrong 仍完整（如 wrong 内部被插入字符则 slice 不匹配 → _gone）
        const w = err.wrong || '';
        if (err.start < 0 || err.end > after.length || after.slice(err.start, err.end) !== w) {
          err._gone = true;
        } else {
          err.line = 1 + (after.slice(0, err.start).match(/\n/g) || []).length;
        }
        changed = true;
      } else {
        // 编辑区间（[p, be)，before 坐标）与 err 区间重叠 = 用户直接改了 wrong 文本
        // （删除/替换/内部插入）→ 标注失效 _gone，绝不窗口搜索重锚（会误锚到相邻重复文本）
        err._gone = true;
        changed = true;
      }
    }
  } else if (typeof before !== 'string') {
    // 无基准（首次编辑前）→ 位置感知兜底：从 err.start 附近起搜，-1 回退全局；找不到 _gone
    errors.forEach(function (err) {
      if (err._gone) return;
      const wl = err.wrong ? err.wrong.length : 0;
      let idx = after.indexOf(err.wrong, Math.min(err.start, Math.max(0, after.length - wl)));
      if (idx < 0) idx = after.indexOf(err.wrong);
      if (idx < 0) { err._gone = true; }
      else if (err.start !== idx) { err.start = idx; err.end = idx + wl; }
      changed = true;
    });
  }
  _prTextBefore[i] = after;
  if (changed && !skipRender) renderProofread(i);   // 有变化才重建（性能：避免每次 input 全量重绘）；skipRender=true 时仅 rebase 不重建（IME composition 期间用）
}

// IME composition 期间累积的待重建统一 flush（compositionend 触发）——直接无条件 renderProofread，不走 changed 判断（composition 期间数据已 rebase、before==after 无 changed，会漏渲染）
function _flushPrPending() { for (const iStr in _prRenderPending) { if (!_prRenderPending[iStr]) continue; _prRenderPending[iStr] = false; const idx = Number(iStr); if (proofreadErrors[idx] && proofreadErrors[idx].length) renderProofread(idx); } }

// 纠错确认悬浮窗（恒显示 errOk：有候选=采纳候选，无候选=删除 wrong）
function showErrPopup(rect) {
  const pop = document.getElementById('errPopup');
  pop.style.display = 'flex';
  const r = pop.getBoundingClientRect();
  let left = rect.left + rect.width / 2 - r.width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - r.width - 8));
  let top = rect.top - r.height - 8;
  if (top < 8) top = rect.bottom + 8;
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';
}
function hideErrPopup() {
  document.getElementById('errPopup').style.display = 'none';
  errKey = null;
}

// ---------- 图片设置弹窗（点击编辑区内的图片弹出） ----------
let _imgKey = null; // { i, pEl, imgEl } 当前弹窗对应的图片
const _imgSizeClasses = ['ptoe-img-w25', 'ptoe-img-w50', 'ptoe-img-w75', 'ptoe-img-w100'];
const _imgPosClasses = ['ptoe-img-left', 'ptoe-img-center', 'ptoe-img-right'];
// 行内图片（ptoe-img-inline）用 vertical-align 控制上下位置；块级图片用 p 的 text-align
const _imgVAlignClasses = ['ptoe-img-vtop', 'ptoe-img-vmid', 'ptoe-img-vbot'];

function showImgPopup(rect) {
  const pop = document.getElementById('imgPopup');
  pop.style.display = 'flex';
  // 行内图片 → 显示「位置（行内）」顶/中/底行；块级图片 → 显示「位置」左/中/右行
  const isInline = !!( _imgKey && _imgKey.imgEl
    && _imgKey.imgEl.classList.contains('ptoe-img-inline'));
  document.getElementById('imgPosRow').style.display = isInline ? 'none' : 'flex';
  document.getElementById('imgVPosRow').style.display = isInline ? 'flex' : 'none';
  const r = pop.getBoundingClientRect();
  let left = rect.left + rect.width / 2 - r.width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - r.width - 8));
  let top = rect.top - r.height - 8;
  if (top < 8) top = rect.bottom + 8;
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';
}
function hideImgPopup() {
  document.getElementById('imgPopup').style.display = 'none';
  _imgKey = null;
}

// 自动跳转到下一处校正文本（按文档顺序）：采纳/忽略后调用，滚动到下一处并弹出采纳/忽略窗
// 用户点 采纳/忽略 后自动顺序推进，直至当前页无更多校正项。
function advanceToNextError(i, k) {
  const errors = proofreadErrors[i];
  if (!errors || !errors.length) { hideErrPopup(); return; }
  // 从 k 开始向后找第一个未 _gone 的条目（splice(k,1) 后原 k+1 移到 k）
  let nextK = -1;
  for (let idx = k; idx < errors.length; idx++) {
    if (errors[idx] && !errors[idx]._gone) { nextK = idx; break; }
  }
  if (nextK < 0) { hideErrPopup(); return; }
  const row = [...host.children].find(function (r) { return Number(r.dataset.i) === i; });
  if (!row) { hideErrPopup(); return; }
  const ed = row.querySelector('.editable');
  const el = ed.querySelector('.ptoe-err[data-err-i="' + nextK + '"]');
  if (!el) { hideErrPopup(); return; }
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  errKey = { i: i, k: nextK };
  showErrPopup(el.getBoundingClientRect());
}

// ---------- 文字纠错下拉菜单 ----------
function positionProofreadMenu() {
  const btn = document.getElementById('proofreadBtn');
  const menu = document.getElementById('proofreadMenu');
  if (!btn || !menu) return;
  const r = btn.getBoundingClientRect();
  let left = r.left;
  left = Math.max(8, Math.min(left, window.innerWidth - menu.offsetWidth - 8));
  menu.style.left = left + 'px';
  menu.style.top = (r.bottom + 2) + 'px';
}
function openProofreadMenu() {
  positionProofreadMenu();
  document.getElementById('proofreadMenu').style.display = 'block';
  proofreadMenuOpen = true;
  document.getElementById('proofreadBtn').classList.add('active');
}
function closeProofreadMenu() {
  document.getElementById('proofreadMenu').style.display = 'none';
  proofreadMenuOpen = false;
  document.getElementById('proofreadBtn').classList.remove('active');
}
function toggleProofreadMenu() {
  if (proofreadMenuOpen) closeProofreadMenu();
  else openProofreadMenu();
}

// 子项1 校正：对当前页执行纠错
function proofreadCorrect() {
  closeProofreadMenu();
  runProofread();
}

// 子项2 重识别：对当前页重新 OCR，差异以纠错标注叠加显示
async function runReocr() {
  closeProofreadMenu();
  const ed = ctxTargetEditable(); // 右键菜单目标页优先（与光标位置解耦）
  if (!ed) { showToast('请先点击某一页的文字', 'warn'); return; }
  const row = ed.closest('.page-row');
  if (!row) return;
  const i = Number(row.dataset.i);
  const page = pages[i].page;
  const model = proofreadLlmModel || '';
  const btn = document.getElementById('prMenuReocr');
  if (btn) btn.disabled = true;
  try {
    const res = await fetchJSON('/api/reocr', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ page: page, model: model, html: ed.innerHTML }),
    });
    if (!res.ok) { showToast('重识别失败: ' + (res.error || '未知错误'), 'fail'); return; }
    proofreadOriginal[i] = ed.innerHTML;
    proofreadErrors[i] = Array.isArray(res.diff) ? res.diff : [];
    lastProofreadPage = pages[i].page;
    if (proofreadErrors[i].length) {
      renderProofread(i);
      scheduleRemeasure(i);
      setStatus('第 ' + page + ' 页重识别完成，标注 ' + proofreadErrors[i].length + ' 处差异');
      showToast('第 ' + page + ' 页重识别完成，标注 ' + proofreadErrors[i].length + ' 处差异', 'ok');
    } else {
      setStatus('第 ' + page + ' 页重识别完成，未发现差异');
      showToast('第 ' + page + ' 页重识别完成，未发现差异', 'ok');
    }
  } catch (e) {
    showToast('重识别失败: ' + e.message, 'fail');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// 子项3 清除：清除当前页的纠错标注（删除线 + 候选字）；已应用的文字/词句保留不动
function proofreadClearCurrent() {
  closeProofreadMenu();
  const ed = ctxTargetEditable(); // 右键菜单目标页优先（与光标位置解耦）
  if (!ed) { showToast('请先点击某一页的文字', 'warn'); return; }
  const row = ed.closest('.page-row');
  if (!row) return;
  const i = Number(row.dataset.i);
  if (!ed.querySelector('.ptoe-err, .ptoe-fix')) { showToast('当前页没有纠错标注', 'warn'); setStatus('当前页没有纠错标注'); return; }
  clearProofread(i);
  hideErrPopup();
  scheduleRemeasure(i);
  showToast('已清除当前页纠错标注', 'ok');
  setStatus('已清除当前页纠错标注');
}

// 子项2 应用：把当前页所有有候选的提示替换为 candidates[0]；无候选（增字）=删除 wrong（支持跨多文本节点）
function proofreadApplyCurrent() {
  closeProofreadMenu();
  const ed = ctxTargetEditable(); // 右键菜单目标页优先（与光标位置解耦）
  if (!ed) { showToast('请先点击某一页的文字', 'warn'); return; }
  const row = ed.closest('.page-row');
  if (!row) return;
  const i = Number(row.dataset.i);
  const errors = proofreadErrors[i];
  if (!errors || !errors.length) { showToast('当前页没有可应用的纠错提示', 'warn'); setStatus('当前页没有可应用的纠错提示'); return; }
  // D6: 改文本主体包进 histRun（入撤销栈）
  histRun('应用纠错', [i], function () {
    let applied = 0;
    const acceptItems = []; // 收集批量 accept 反馈
    const appliedShifts = []; // 收集 {start, delta, origStart} 用于 rebase 剩余标注
    // 倒序处理，避免 DOM 修改影响后续 data-err-i 索引
    for (let idx = errors.length - 1; idx >= 0; idx--) {
      const err = errors[idx];
      if (err._gone) continue; // F5: 已标记消失的条目跳过
      const sEls = ed.querySelectorAll('.ptoe-err[data-err-i="' + idx + '"]');
      if (!sEls.length) continue;
      const fixEl = ed.querySelector('.ptoe-fix[data-err-i="' + idx + '"]');
      if (fixEl) fixEl.parentNode.removeChild(fixEl);
      const delta = (err.candidates && err.candidates.length ? err.candidates[0].length : 0) - (err.wrong ? err.wrong.length : 0);
      if (err.candidates && err.candidates.length) {
        // 有候选：首段替换为 candidates[0]，其余段删除
        sEls[0].parentNode.replaceChild(document.createTextNode(err.candidates[0]), sEls[0]);
        for (let p = 1; p < sEls.length; p++) sEls[p].parentNode.removeChild(sEls[p]);
        acceptItems.push({ wrong: err.wrong, fixed: err.candidates[0] }); // 收集反馈
      } else {
        // 无候选（增字）：全部段删除
        for (const s of sEls) s.parentNode.removeChild(s);
      }
      // D8: 收集原始坐标用于 rebase
      appliedShifts.push({ start: err.start, delta: delta });
      errors.splice(idx, 1);
      applied++;
    }
    // D8: rebase 剩余标注——用原始坐标判定，end 用新 start+wrong 长度
    for (const e of errors) {
      if (e._gone) continue;
      let d = 0;
      for (const as of appliedShifts) {
        if (e.start >= as.start) d += as.delta;
      }
      const origLen = e.end - e.start;
      e.start = e.start + d;
      e.end = e.start + origLen;
    }
    syncContent(ed); // 页面文本同步入 pages[i].text
    // 重建剩余标注（避免手动 reindex 导致 fix 索引漂移）
    renderProofread(i);
    // 批量反馈：应用全部
    proofreadFeedbackAcceptBatch(acceptItems);
    if (applied > 0) {
      showToast('已应用 ' + applied + ' 处纠错提示', 'ok');
      setStatus('已应用 ' + applied + ' 处纠错提示');
    } else {
      showToast('当前页没有可应用的纠错提示', 'warn');
      setStatus('当前页没有可应用的纠错提示');
    }
  });
  scheduleRemeasure(i);
}

// 子项4 回退：彻底恢复当前页校正前的原始文本（已应用的修改一并撤回）
function proofreadRevertCurrent() {
  closeProofreadMenu();
  const ed = currentEditable();
  if (!ed) { showToast('请先点击某一页的文字', 'warn'); return; }
  const row = ed.closest('.page-row');
  if (!row) return;
  const i = Number(row.dataset.i);
  if (!proofreadOriginal[i]) { showToast('当前页没有可回退的纠错操作', 'warn'); setStatus('当前页没有可回退的纠错操作'); return; }
  // D6: 改文本主体包进 histRun（入撤销栈）
  histRun('回退纠错', [i], function () {
    ed.innerHTML = proofreadOriginal[i];
    ed.normalize();
    proofreadErrors[i] = [];
    if (proofreadDismissed[i]) proofreadDismissed[i] = new Set();
    delete proofreadOriginal[i];
  });
  scheduleRemeasure(i);
  hideErrPopup();
  showToast('已回退当前页的纠错操作', 'ok');
  setStatus('已回退当前页的纠错操作');
}

// 点击 .ptoe-err 弹悬浮窗
document.addEventListener('click', function (e) {
  const el = e.target.closest('.ptoe-err');
  if (!el) return;
  e.stopPropagation();
  e.preventDefault();
  const row = el.closest('.page-row');
  if (!row) return;
  const i = Number(row.dataset.i);
  const k = Number(el.dataset.errI);
  const errors = proofreadErrors[i];
  if (!errors || !errors[k]) return;
  if (errors[k]._gone) return; // F5: 已标记消失的条目不响应点击
  errKey = { i: i, k: k };
  suppressPopupUntil = performance.now() + 250;
  showErrPopup(el.getBoundingClientRect());
});
// 采纳：有候选=替换为 candidates[0]；无候选（增字）=删除 wrong 文本（支持跨多文本节点）
document.getElementById('errOk').addEventListener('click', function () {
  if (!errKey) { hideErrPopup(); return; }
  const i = errKey.i, k = errKey.k;
  const errors = proofreadErrors[i];
  if (!errors || !errors[k] || errors[k]._gone) { hideErrPopup(); return; }
  const err = errors[k];
  const row = [...host.children].find(function (r) { return Number(r.dataset.i) === i; });
  if (!row) { hideErrPopup(); return; }
  const ed = row.querySelector('.editable');
  const sEls = ed.querySelectorAll('.ptoe-err[data-err-i="' + k + '"]');
  if (!sEls.length) { hideErrPopup(); return; }
  // D6: 改文本主体包进 histRun（入撤销栈）
  hideErrPopup();
  histRun('采纳纠错', [i], function () {
    // 删 fixEl（若存在，只在首段）
    const fixEl2 = ed.querySelector('.ptoe-fix[data-err-i="' + k + '"]');
    if (fixEl2) fixEl2.parentNode.removeChild(fixEl2);
    // 计算 delta：整条 wrong 被替换为 candidates[0]（或删除）
    const delta = (err.candidates && err.candidates.length ? err.candidates[0].length : 0) - (err.wrong ? err.wrong.length : 0);
    if (err.candidates && err.candidates.length) {
      // 有候选：首段替换为 candidates[0]，其余段删除
      sEls[0].parentNode.replaceChild(document.createTextNode(err.candidates[0]), sEls[0]);
      for (let p = 1; p < sEls.length; p++) sEls[p].parentNode.removeChild(sEls[p]);
      // 反馈：采纳候选
      proofreadFeedbackAccept(err.wrong, err.candidates[0]);
    } else {
      // 无候选（增字）：全部段删除
      for (const s of sEls) s.parentNode.removeChild(s);
    }
    ed.normalize();
    errors.splice(k, 1);
    // rebase 剩余标注：非重叠，右侧整体平移 delta
    for (let e of errors) {
      if (!e._gone && e.start >= err.end) { e.start += delta; e.end += delta; }
    }
    syncContent(ed); // 页面文本同步入 pages[i].text
    // 重建剩余标注（避免手动 reindex 导致 fix 索引漂移）
    renderProofread(i);
  });
  scheduleRemeasure(i);
  advanceToNextError(i, k); // 自动跳转到下一处校正文本
});
// 忽略：移除标注恢复完整 wrong 文本，加入 dismissed（支持跨多文本节点）
document.getElementById('errNo').addEventListener('click', function () {
  if (!errKey) { hideErrPopup(); return; }
  const i = errKey.i, k = errKey.k;
  const errors = proofreadErrors[i];
  if (!errors || !errors[k] || errors[k]._gone) { hideErrPopup(); return; }
  const err = errors[k];
  const row = [...host.children].find(function (r) { return Number(r.dataset.i) === i; });
  if (!row) { hideErrPopup(); return; }
  const ed = row.querySelector('.editable');
  const sEls = ed.querySelectorAll('.ptoe-err[data-err-i="' + k + '"]');
  if (!sEls.length) { hideErrPopup(); return; }
  // D6: 改文本主体包进 histRun（入撤销栈）
  hideErrPopup();
  histRun('忽略纠错', [i], function () {
    // 反馈：忽略该词（在状态清除前捕获 wrong）
    proofreadFeedbackIgnore(err.wrong);
    // 删 fixEl（若存在）
    const fixEl2 = ed.querySelector('.ptoe-fix[data-err-i="' + k + '"]');
    if (fixEl2) fixEl2.parentNode.removeChild(fixEl2);
    // 解包：每段 .ptoe-err 替换为等文本的 textNode（wrong 文本保留原位，不重定位）
    for (const s of sEls) {
      const t = document.createTextNode(s.textContent);
      s.parentNode.replaceChild(t, s);
    }
    ed.normalize();
    if (!proofreadDismissed[i]) proofreadDismissed[i] = new Set();
    proofreadDismissed[i].add(err.start + ':' + err.wrong);
    errors.splice(k, 1);
    syncContent(ed); // 页面文本同步入 pages[i].text
    // 重建剩余标注（避免手动 reindex 导致 fix 索引漂移）
    renderProofread(i);
  });
  scheduleRemeasure(i);
  advanceToNextError(i, k); // 自动跳转到下一处校正文本
});
// 点击编辑区外 / 滚动 / 选区折叠 → 关闭悬浮窗
document.addEventListener('mousedown', function (e) {
  if (errKey && !e.target.closest('#errPopup')) hideErrPopup();
  // 点击菜单外（且不在按钮上）→ 关闭下拉菜单
  if (proofreadMenuOpen && !e.target.closest('#proofreadMenu') && !e.target.closest('#proofreadBtn')) {
    closeProofreadMenu();
  }
  // 点击图片弹窗外 → 关闭图片弹窗
  if (_imgKey && !e.target.closest('#imgPopup')) hideImgPopup();
});
// 点击编辑区内的图片 → 弹出图片设置弹窗（行内图片可能没有 <p> 包裹，pEl 允许为 null）
document.addEventListener('click', function (e) {
  const imgEl = e.target.closest('.editable img');
  if (!imgEl) return;
  const pEl = imgEl.closest('p') || null;
  const row = imgEl.closest('.page-row');
  if (!row) return;
  const i = Number(row.dataset.i);
  e.stopPropagation();
  e.preventDefault();
  _imgKey = { i: i, pEl: pEl, imgEl: imgEl };
  suppressPopupUntil = performance.now() + 300; // 抑制选中文字快捷菜单
  showImgPopup(imgEl.getBoundingClientRect());
});
// 图片弹窗按钮：大小/位置/删除
document.getElementById('imgPopup').addEventListener('click', function (e) {
  const btn = e.target.closest('.img-pop-btn');
  if (!btn || !_imgKey) return;
  const op = btn.dataset.imgOp;
  const val = btn.dataset.imgVal;
  const { i, pEl, imgEl } = _imgKey;
  const ed = [...host.children].find(function (r) { return Number(r.dataset.i) === i; }).querySelector('.editable');
  if (op === 'size') {
    // 设置图片大小：原尺寸=移除尺寸 class，其他=交换到对应 ptoe-img-w* class
    histRun('设置图片大小', [i], function () {
      _imgSizeClasses.forEach(function (c) { imgEl.classList.remove(c); });
      if (val !== 'original') imgEl.classList.add('ptoe-img-' + val);
      syncContent(ed);
    });
    scheduleRemeasure(i);
    // 大小操作保持弹窗打开，便于连续调整
  } else if (op === 'pos') {
    // 设置图片位置：行内图片 → vertical-align（顶/中/底）；
    // 块级图片 → 交换 ptoe-img-left/center/right class（p 的 text-align）
    const isInline = imgEl.classList.contains('ptoe-img-inline');
    histRun('设置图片位置', [i], function () {
      if (isInline) {
        _imgVAlignClasses.forEach(function (c) { imgEl.classList.remove(c); });
        imgEl.classList.add('ptoe-img-' + val);
      } else {
        _imgPosClasses.forEach(function (c) { pEl.classList.remove(c); });
        pEl.classList.add('ptoe-img-' + val);
      }
      syncContent(ed);
    });
    scheduleRemeasure(i);
    // 位置操作保持弹窗打开
  } else if (op === 'delete') {
    // 删除图片：行内图片只移除 <img> 本身（保留周围文字）；块级图片移除整个 <p> 包裹
    const isInline = imgEl.classList.contains('ptoe-img-inline');
    histRun('删除图片', [i], function () {
      if (isInline) {
        if (imgEl.parentNode) imgEl.parentNode.removeChild(imgEl);
      } else if (pEl && pEl.parentNode) {
        pEl.parentNode.removeChild(pEl);
      }
      syncContent(ed);
    });
    scheduleRemeasure(i);
    hideImgPopup();
  }
});
// Esc 关闭图片弹窗
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape' && _imgKey) { hideImgPopup(); }
});

// 字号下拉：仅调整编辑区显示字号（CSS 变量 --editor-font-size；视图偏好，不写入保存内容）
function applyFontSize(v) {
  document.documentElement.style.setProperty('--editor-font-size', (v || 14) + 'px');
  setStatus('编辑字号：' + (v || 14) + 'px');
}

// ---------- 格式规则（弹窗管理 + 条件列表/求值模式应用） ----------
const FORMAT_RULE_OPTS = [
  ['none','无（不对文本处理）'], ['bold','加粗'], ['no_bold','不加粗'], ['italic','斜体'], ['align_center','居中'], ['align_left','居左'],
  ['align_right','居右'], ['heading1','标题1'], ['heading2','标题2'], ['heading3','标题3'],
  ['heading4','标题4'], ['heading5','标题5'], ['heading6','标题6'],
  ['p','正文'], ['merge','合并段落'], ['note','注释'], ['citation','引用'], ['remove','清除格式'],
];
let formatRules = [];
let formatRuleEditingId = null;
let _frRange = null; // 打开格式规则弹窗时捕获的选区（应用前恢复，避免 selection 丢失）

// 格式冲突模型（2026-08-15）：块标签互斥（p/h1-6 同一块只能一个）、对齐互斥
// （align_left/center/right 同一块只能一个）；bold/italic/note 相互独立可共存；
// remove 与任何其他格式冲突（会清除全部格式）。
const FORMAT_OP_GROUPS = {
  block_tag: ['p','heading1','heading2','heading3','heading4','heading5','heading6'],
  align: ['align_left','align_center','align_right'],
  merge: ['merge'],
};
function opGroup(op) {
  for (const g in FORMAT_OP_GROUPS) if (FORMAT_OP_GROUPS[g].includes(op)) return g;
  return null;
}
function opsConflict(a, b) {
  if (a === b) return false;
  if (a === 'remove' || b === 'remove') return true;
  const ga = opGroup(a), gb = opGroup(b);
  if (ga === null || gb === null) return false;
  return ga === gb;
}
// 正则条件支持 /pattern/flags 语法（2026-08-15）：无斜杠包裹时按普通表达式处理
function parseRegexPattern(pattern) {
  const m = /^\/(.+)\/([a-z]*)$/.exec(pattern);
  return m ? { pattern: m[1], flags: m[2] } : { pattern: pattern, flags: '' };
}
// 统计正则表达式中的捕获组数量（跳过转义的 \( 和非捕获组 (?:...）
function _countCaptureGroups(pattern) {
  var rp = parseRegexPattern(pattern);
  var pat = rp.pattern;
  var count = 0;
  for (var i = 0; i < pat.length; i++) {
    if (pat[i] === '\\') { i++; continue; }
    if (pat[i] === '(' && pat[i + 1] !== '?') count++;
  }
  return count;
}
// 保存时冲突预警：两条规则存在相同条件（type+pattern+scope）且格式互斥时提示。
// 新模型下规则含 conditions 列表：返回该规则全部条件的键集合。
function ruleConditionKey(r) {
  const keys = new Set();
  for (const c of (r && r.conditions) || []) {
    keys.add((c.type || 'contains') + '|' + (c.pattern || '') + '|' + (c.scope || 'selection'));
  }
  return keys;
}
function rulesConflict(a, b) {
  const ka = ruleConditionKey(a), kb = ruleConditionKey(b);
  if (!ka.size || !kb.size) return false;
  let shared = false;
  for (const k of ka) { if (kb.has(k)) { shared = true; break; } }
  if (!shared) return false;
  // 格式取全部条件的并集（含 none，none 无冲突组，opsConflict 恒 false）
  const opsA = [], opsB = [];
  for (const c of (a && a.conditions) || []) opsA.push.apply(opsA, c.formats || []);
  for (const c of (b && b.conditions) || []) opsB.push.apply(opsB, c.formats || []);
  return opsA.some(function (x) { return opsB.some(function (y) { return opsConflict(x, y); }); });
}

function _escHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
function fmtSummary(fmts) {
  if (!fmts || !fmts.length) return '（无）';
  const names = {};
  for (const o of FORMAT_RULE_OPTS) names[o[0]] = o[1];
  return fmts.map(function (op) { return names[op] || op; }).join('、');
}
function condSummary(rule) {
  const conds = (rule && rule.conditions) || [];
  if (!conds.length) return '无条件';
  const tmap = { contains: '包含', prefix: '开头', suffix: '结尾', regex: '正则匹配' };
  return conds.map(function (c) {
    const t = tmap[c.type] || c.type;
    const scope = c.scope === 'paragraph' ? '整段' : (c.scope === 'page' ? '当前页' : '选中');
    const tgtMap = { before: '之前', after: '之后' };
    const tgtLabel = c.target === 'between' ? '之间「' + (c.between_end_pattern || '?') + '」' : (tgtMap[c.target] || '');
    const cond = c.pattern ? t + '「' + c.pattern + '」/' + scope + (tgtLabel ? '→' + tgtLabel : '') : '无条件';
    if (c.type === 'regex' && c.group_formats && c.group_formats.length) {
      var parts = [];
      for (var gi = 0; gi < c.group_formats.length; gi++) {
        var gf = c.group_formats[gi] || [];
        if (gf.length) parts.push('组' + (gi + 1) + ':' + fmtSummary(gf));
      }
      return cond + (parts.length ? ' → ' + parts.join('，') : ' → ' + fmtSummary(c.formats));
    }
    return cond + ' → ' + fmtSummary(c.formats);
  }).join('；');
}

async function openFormatRulesModal() {
  // 捕获当前选区（若在编辑区内），应用规则前恢复——弹窗打开会清空/改变 selection
  const sel = window.getSelection();
  if (sel && sel.rangeCount > 0) {
    const node = sel.getRangeAt(0).commonAncestorContainer;
    const el = node && node.nodeType === 3 ? node.parentNode : node;
    if (el && el.closest && el.closest('.editable')) _frRange = sel.getRangeAt(0).cloneRange();
  }
  try {
    const res = await fetchJSON('/api/format_rules');
    formatRules = (res && res.rules) || [];
  } catch (e) { formatRules = []; }
  formatRuleEditingId = null;
  document.getElementById('frRuleModalBg').style.display = 'none';
  document.getElementById('frFmtPopupBg').style.display = 'none';
  renderFormatRules();
  document.getElementById('formatRulesModalBg').style.display = 'flex';
}
function closeFormatRulesModal() {
  document.getElementById('formatRulesModalBg').style.display = 'none';
}
function restoreFrRange() {
  if (!_frRange) return;
  const sel = window.getSelection();
  if (!sel) return;
  sel.removeAllRanges();
  sel.addRange(_frRange);
}
function renderFormatRules() {
  const tbody = document.getElementById('formatRulesBody');
  tbody.innerHTML = '';
  if (!formatRules.length) {
    tbody.innerHTML = '<tr><td colspan="4" style="color:#9aa7b4;padding:10px 8px;">暂无规则，点击「新建规则」创建</td></tr>';
    return;
  }
  formatRules.forEach(function (rule, idx) {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td class="fr-order">' + (idx + 1) + '</td>' +
      '<td class="fr-name">' + _escHtml(rule.name) + '</td>' +
      '<td class="fr-sum">' + _escHtml(condSummary(rule)) + '</td>' +
      '<td style="white-space:nowrap;">' +
        '<button type="button" class="fr-up" title="上移"' + (idx === 0 ? ' disabled' : '') + '>↑</button> ' +
        '<button type="button" class="fr-down" title="下移"' + (idx === formatRules.length - 1 ? ' disabled' : '') + '>↓</button> ' +
        '<button type="button" class="fr-apply">应用</button> ' +
        '<button type="button" class="fr-edit">编辑</button> ' +
        '<button type="button" class="fr-del">删除</button>' +
      '</td>';
    tr.querySelector('.fr-up').addEventListener('click', function () { moveFormatRule(rule, -1); });
    tr.querySelector('.fr-down').addEventListener('click', function () { moveFormatRule(rule, 1); });
    tr.querySelector('.fr-apply').addEventListener('click', function () {
      if (applyFormatRule(rule)) closeFormatRulesModal();
    });
    tr.querySelector('.fr-edit').addEventListener('click', function () { editFormatRule(rule); });
    tr.querySelector('.fr-del').addEventListener('click', function () { deleteFormatRule(rule); });
    tbody.appendChild(tr);
  });
}
function moveFormatRule(rule, dir) {
  var idx = formatRules.findIndex(function (r) { return r.id === rule.id; });
  if (idx === -1) return;
  var j = idx + dir;
  if (j < 0 || j >= formatRules.length) return;
  var moved = formatRules[idx];
  formatRules.splice(idx, 1);
  formatRules.splice(j, 0, moved);
  renderFormatRules();
  persistFormatRules();
}
function renderFmtOptions(containerId) {
  const box = document.getElementById(containerId);
  box.innerHTML = '';
  FORMAT_RULE_OPTS.forEach(function (o) {
    const label = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.value = o[0];
    label.appendChild(cb);
    label.appendChild(document.createTextNode(o[1]));
    box.appendChild(label);
  });
}
function setFmtChecks(containerId, ops) {
  const set = {};
  (ops || []).forEach(function (op) { set[op] = true; });
  document.querySelectorAll('#' + containerId + ' input[type="checkbox"]').forEach(function (cb) { cb.checked = !!set[cb.value]; });
}
function collectFmtChecks(containerId) {
  const out = [];
  document.querySelectorAll('#' + containerId + ' input[type="checkbox"]:checked').forEach(function (cb) { out.push(cb.value); });
  return out;
}
// ---- 条件列表编辑（独立弹窗 #frRuleModalBg） ----
let _frConds = []; // 编辑中的条件列表（镜像 DOM；select/input 实时值经 syncCondsFromDom 读回）
let _frFmtIdx = -1; // 当前打开格式弹窗的条件下标
let _frFmtGroupIdx = -1; // 当前打开格式弹窗的 group_formats 下标（-1=条件级）
const _FR_COND_TYPES = [
  ['regex','正则匹配'], ['contains','包含文字'], ['prefix','以…开头'], ['suffix','以…结尾'],
];
const _FR_COND_TARGETS = [
  ['match','匹配对象'], ['before','条件之前'], ['after','条件之后'], ['between','两条件之间'],
];
// 把 DOM 中每行 select/input 的实时值同步回 _frConds（formats 保留 _frConds 中的）
function syncCondsFromDom() {
  const rows = document.querySelectorAll('#frConditions .fr-cond-row:not(.fr-group-row)');
  rows.forEach(function (row, idx) {
    const base = _frConds[idx] || { formats: [] };
    const tgtEl = row.querySelector('.frCondTarget');
    const endEl = row.querySelector('.frCondEndPattern');
    _frConds[idx] = {
      type: row.querySelector('.frCondType').value,
      pattern: row.querySelector('.frCondPattern').value,
      scope: row.querySelector('.frCondScope').value,
      formats: (base.formats || []).slice(),
      group_formats: (base.group_formats || []).map(function (g) { return (g || []).slice(); }),
      target: tgtEl ? tgtEl.value : (base.target || 'match'),
      between_end_pattern: endEl ? endEl.value : (base.between_end_pattern || ''),
    };
  });
}
function renderConditions(conds) {
  _frConds = conds;
  const box = document.getElementById('frConditions');
  box.innerHTML = '';
  if (!_frConds.length) {
    box.innerHTML = '<div style="color:#9aa7b4;font-size:12px;">暂无条件，点击「添加条件」创建</div>';
    return;
  }
  const names = {};
  for (const o of FORMAT_RULE_OPTS) names[o[0]] = o[1];
  _frConds.forEach(function (c, idx) {
    const row = document.createElement('div');
    row.className = 'fr-cond-row';
    const typeSel = document.createElement('select');
    typeSel.className = 'frCondType';
    _FR_COND_TYPES.forEach(function (t) {
      const opt = document.createElement('option');
      opt.value = t[0]; opt.textContent = t[1];
      if (t[0] === (c.type || 'contains')) opt.selected = true;
      typeSel.appendChild(opt);
    });
    const patInput = document.createElement('input');
    patInput.type = 'text'; patInput.className = 'frCondPattern';
    patInput.placeholder = '条件内容（正则填表达式，留空=无条件）';
    patInput.value = c.pattern || '';
    const scopeSel = document.createElement('select');
    scopeSel.className = 'frCondScope';
    [['selection','选中文字'], ['paragraph','光标所在段落'], ['page','当前页面']].forEach(function (t) {
      const opt = document.createElement('option');
      opt.value = t[0]; opt.textContent = t[1];
      if (t[0] === (c.scope || 'selection')) opt.selected = true;
      scopeSel.appendChild(opt);
    });
    // target 选择器：决定格式作用于匹配文本/之前/之后/两条件之间
    const targetSel = document.createElement('select');
    targetSel.className = 'frCondTarget';
    targetSel.style.cssText = 'margin-left:6px;';
    _FR_COND_TARGETS.forEach(function (t) {
      const opt = document.createElement('option');
      opt.value = t[0]; opt.textContent = t[1];
      if (t[0] === (c.target || 'match')) opt.selected = true;
      targetSel.appendChild(opt);
    });
    // between 模式的结束条件 pattern 输入
    const endPatInput = document.createElement('input');
    endPatInput.type = 'text'; endPatInput.className = 'frCondEndPattern';
    endPatInput.placeholder = '结束条件（正则/文字）';
    endPatInput.value = c.between_end_pattern || '';
    endPatInput.style.cssText = 'margin-left:6px;width:120px;display:' + ((c.target || 'match') === 'between' ? 'inline-block' : 'none') + ';';
    targetSel.addEventListener('change', function () {
      endPatInput.style.display = targetSel.value === 'between' ? 'inline-block' : 'none';
    });
    const fmtBtn = document.createElement('button');
    fmtBtn.type = 'button'; fmtBtn.className = 'frFmtBtn';
    fmtBtn.textContent = '格式';
    fmtBtn.title = '设置该条件的格式（含「无」= 不处理文本）';
    fmtBtn.addEventListener('click', function () { openFmtPopup(idx); });
    const tags = document.createElement('span');
    tags.className = 'fr-tags';
    const fmts = c.formats || [];
    if (fmts.length) {
      fmts.forEach(function (op) {
        const tag = document.createElement('span');
        tag.className = 'fr-tag' + (op === 'none' ? ' fr-tag-none' : '');
        tag.textContent = names[op] || op;
        tags.appendChild(tag);
      });
    } else {
      const empty = document.createElement('span');
      empty.className = 'fr-tags-empty';
      empty.textContent = '未设置格式';
      tags.appendChild(empty);
    }
    const upBtn = document.createElement('button');
    upBtn.type = 'button'; upBtn.className = 'frCondUp'; upBtn.textContent = '↑';
    upBtn.title = '上移'; upBtn.disabled = idx === 0;
    upBtn.addEventListener('click', function () { moveCondition(idx, -1); });
    const downBtn = document.createElement('button');
    downBtn.type = 'button'; downBtn.className = 'frCondDown'; downBtn.textContent = '↓';
    downBtn.title = '下移'; downBtn.disabled = idx === _frConds.length - 1;
    downBtn.addEventListener('click', function () { moveCondition(idx, 1); });
    const delBtn = document.createElement('button');
    delBtn.type = 'button'; delBtn.className = 'frCondDel'; delBtn.textContent = '✕';
    delBtn.title = '删除条件';
    delBtn.addEventListener('click', function () { removeCondition(idx); });
    row.appendChild(typeSel);
    row.appendChild(patInput);
    row.appendChild(scopeSel);
    row.appendChild(targetSel);
    row.appendChild(endPatInput);
    row.appendChild(fmtBtn);
    row.appendChild(tags);
    row.appendChild(upBtn);
    row.appendChild(downBtn);
    row.appendChild(delBtn);
    box.appendChild(row);
    // regex 条件：检测捕获组并显示 per-group 格式编辑行
    if (c.type === 'regex' && c.pattern) {
      var gCount = _countCaptureGroups(c.pattern);
      if (gCount > 0) {
        var gf = c.group_formats || [];
        for (var gi = 0; gi < gCount; gi++) {
          var grow = document.createElement('div');
          grow.className = 'fr-cond-row fr-group-row';
          grow.style.cssText = 'padding-left:2em;font-size:12px;opacity:.85;min-height:0;';
          var gLabel = document.createElement('span');
          gLabel.style.cssText = 'margin-right:4px;white-space:nowrap;';
          gLabel.textContent = '组' + (gi + 1) + '：';
          grow.appendChild(gLabel);
          var gFmtBtn = document.createElement('button');
          gFmtBtn.type = 'button'; gFmtBtn.className = 'frFmtBtn';
          gFmtBtn.textContent = '格式';
          gFmtBtn.title = '设置捕获组 ' + (gi + 1) + ' 的独立格式';
          (function(gIdx) {
            gFmtBtn.addEventListener('click', function () { openFmtPopup(idx, gIdx); });
          })(gi);
          grow.appendChild(gFmtBtn);
          var gTags = document.createElement('span');
          gTags.className = 'fr-tags';
          var gFmtList = gf[gi] || [];
          if (gFmtList.length) {
            gFmtList.forEach(function (op) {
              var tag = document.createElement('span');
              tag.className = 'fr-tag' + (op === 'none' ? ' fr-tag-none' : '');
              tag.textContent = names[op] || op;
              gTags.appendChild(tag);
            });
          } else {
            var gEmpty = document.createElement('span');
            gEmpty.className = 'fr-tags-empty';
            gEmpty.textContent = '未设置格式';
            gTags.appendChild(gEmpty);
          }
          grow.appendChild(gTags);
          box.appendChild(grow);
        }
      }
    }
  });
}
function addCondition() {
  syncCondsFromDom();
  _frConds.push({ type: 'contains', pattern: '', scope: 'page', formats: [], group_formats: [], target: 'match', between_end_pattern: '' });
  renderConditions(_frConds);
}
function removeCondition(idx) {
  syncCondsFromDom();
  _frConds.splice(idx, 1);
  renderConditions(_frConds);
}
function moveCondition(idx, dir) {
  syncCondsFromDom();
  const j = idx + dir;
  if (j < 0 || j >= _frConds.length) return;
  const c = _frConds[idx];
  _frConds.splice(idx, 1);
  _frConds.splice(j, 0, c);
  renderConditions(_frConds);
}
// 格式弹窗：勾选该条件的格式（含「无」= 不处理文本）；groupIdx>=0 时编辑分组格式
function openFmtPopup(idx, groupIdx) {
  syncCondsFromDom();
  _frFmtIdx = idx;
  _frFmtGroupIdx = (typeof groupIdx === 'number') ? groupIdx : -1;
  renderFmtOptions('frFmtOpts');
  if (_frFmtGroupIdx >= 0) {
    var gf = _frConds[idx].group_formats || [];
    setFmtChecks('frFmtOpts', gf[_frFmtGroupIdx] || []);
  } else {
    setFmtChecks('frFmtOpts', _frConds[idx].formats);
  }
  document.getElementById('frFmtPopupBg').style.display = 'flex';
}
function confirmFmtPopup() {
  syncCondsFromDom();
  if (_frFmtIdx < 0 || _frFmtIdx >= _frConds.length) { closeFmtPopup(); return; }
  var newFmt = collectFmtChecks('frFmtOpts');
  if (_frFmtGroupIdx >= 0) {
    var gf = _frConds[_frFmtIdx].group_formats || [];
    while (gf.length <= _frFmtGroupIdx) gf.push([]);
    gf[_frFmtGroupIdx] = newFmt;
    _frConds[_frFmtIdx].group_formats = gf;
  } else {
    _frConds[_frFmtIdx].formats = newFmt;
  }
  closeFmtPopup();
  renderConditions(_frConds);
}
function closeFmtPopup() {
  document.getElementById('frFmtPopupBg').style.display = 'none';
  _frFmtIdx = -1;
  _frFmtGroupIdx = -1;
}
function editFormatRule(rule) {
  formatRuleEditingId = rule.id || null;
  document.getElementById('frName').value = rule.name || '';
  document.getElementById('frMode').value = rule.mode || 'first';
  renderConditions((rule.conditions || []).map(function (c) {
    return { type: c.type, pattern: c.pattern, scope: c.scope, formats: (c.formats || []).slice(),
      group_formats: (c.group_formats || []).map(function (g) { return (g || []).slice(); }),
      target: c.target || 'match', between_end_pattern: c.between_end_pattern || '',
    };
  }));
  document.getElementById('frRuleModalBg').style.display = 'flex';
}
function newFormatRule() {
  formatRuleEditingId = null;
  document.getElementById('frName').value = '';
  document.getElementById('frMode').value = 'first';
  renderConditions([{ type: 'contains', pattern: '', scope: 'page', formats: [], target: 'match', between_end_pattern: '' }]);
  document.getElementById('frRuleModalBg').style.display = 'flex';
}
function closeRuleModal() {
  document.getElementById('frRuleModalBg').style.display = 'none';
  formatRuleEditingId = null;
}
async function persistFormatRules() {
  try {
    const res = await fetchJSON('/api/format_rules', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rules: formatRules }),
    });
    if (!res.ok) { showToast('保存失败: ' + (res.error || '未知错误'), 'fail'); return false; }
    formatRules = res.rules || formatRules;
    return true;
  } catch (e) { showToast('保存失败: ' + e, 'fail'); return false; }
}
async function saveFormatRule() {
  const name = document.getElementById('frName').value.trim();
  if (!name) { showToast('请填写规则名称', 'warn'); return; }
  syncCondsFromDom();
  if (!_frConds.length) { showToast('请至少添加一个条件', 'warn'); return; }
  // 校验每个条件的正则表达式
  for (const c of _frConds) {
    if (c.type === 'regex' && c.pattern) {
      try { new RegExp(c.pattern); } catch (e) { showToast('正则表达式无效: ' + e.message, 'fail'); return; }
    }
  }
  const rule = {
    name: name,
    mode: document.getElementById('frMode').value,
    conditions: _frConds.map(function (c) {
      var cond = { type: c.type, pattern: c.pattern, scope: c.scope, formats: (c.formats || []).slice() };
      if (c.type === 'regex' && c.group_formats && c.group_formats.length) {
        cond.group_formats = c.group_formats.map(function (g) { return (g || []).slice(); });
      }
      if (c.target && c.target !== 'match') cond.target = c.target;
      if (c.target === 'between' && c.between_end_pattern) cond.between_end_pattern = c.between_end_pattern;
      return cond;
    }),
  };
  if (formatRuleEditingId) rule.id = formatRuleEditingId;
  // 保存前冲突预警：与既有规则（排除正在编辑的）存在相同条件且格式互斥时提示
  const clash = formatRules.find(function (r) { return r.id !== formatRuleEditingId && rulesConflict(r, rule); });
  if (clash && !confirm('规则「' + rule.name + '」与「' + clash.name + '」存在相同条件且格式冲突（对齐/块标签互斥），执行时后者将被跳过。仍要保存？')) return;
  const idx = formatRules.findIndex(function (r) { return r.id === rule.id; });
  if (idx >= 0) formatRules[idx] = rule; else formatRules.push(rule);
  const ok = await persistFormatRules();
  if (!ok) return;
  closeRuleModal();
  renderFormatRules();
}
async function deleteFormatRule(rule) {
  if (!confirm('删除规则「' + rule.name + '」？')) return;
  formatRules = formatRules.filter(function (r) { return r.id !== rule.id; });
  const ok = await persistFormatRules();
  if (!ok) return;
  renderFormatRules();
}

// ---- 规则应用引擎：条件评估 → 求值模式 → 叠加应用 ----

// 构建文本节点偏移表：遍历 root 下所有文本节点，返回 [{node, start, end}]
function _buildTextNodeList(root) {
  var nodes = [];
  var offset = 0;
  var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
  var node;
  while ((node = walker.nextNode())) {
    var len = node.textContent.length;
    nodes.push({ node: node, start: offset, end: offset + len });
    offset += len;
  }
  return nodes;
}
// 将文本节点列表拼接为纯文本（与 _buildTextNodeList 偏移量对齐）
function _textFromNodes(textNodes) {
  var t = '';
  for (var i = 0; i < textNodes.length; i++) t += textNodes[i].node.textContent;
  return t;
}
// 根据字符偏移量创建 DOM Range（映射到文本节点列表）
function _rangeFromOffsets(textNodes, startOff, endOff) {
  if (startOff >= endOff || !textNodes.length) return null;
  var startNode = null, startIdx = 0, endNode = null, endIdx = 0;
  for (var i = 0; i < textNodes.length; i++) {
    var tn = textNodes[i];
    if (!startNode && tn.end > startOff) {
      startNode = tn.node;
      startIdx = startOff - tn.start;
    }
    if (tn.end >= endOff) {
      endNode = tn.node;
      endIdx = endOff - tn.start;
      break;
    }
  }
  if (!startNode || !endNode) return null;
  try {
    var range = document.createRange();
    range.setStart(startNode, Math.min(startIdx, startNode.textContent.length));
    range.setEnd(endNode, Math.min(endIdx, endNode.textContent.length));
    return range;
  } catch (e) { return null; }
}
// 正则捕获组独立格式应用：对 scope 文本运行正则，每个捕获组匹配独立应用格式
function applyRegexGroupFormats(cond, ed) {
  if (!cond || !cond.group_formats || !cond.group_formats.length) return;
  if (!cond.pattern) return;
  // 获取作用域文本——从 textNodes 拼接而非 innerText（innerText 含块间 \n，与 textNodes 偏移不对齐）
  var scopeRoot = ed;
  if (cond.scope === 'paragraph') {
    var sel = window.getSelection();
    var pnode = sel && sel.rangeCount ? sel.getRangeAt(0).startContainer : ed;
    if (pnode.nodeType === 3) pnode = pnode.parentElement;
    if (!pnode || !ed.contains(pnode)) pnode = ed;
    scopeRoot = pnode.closest ? pnode.closest('p,div,h1,h2,h3,h4,h5,h6') : ed;
  }
  var textNodes = _buildTextNodeList(scopeRoot);
  var text = _textFromNodes(textNodes);
  if (!text) return;
  var rp = parseRegexPattern(cond.pattern);
  var flags = rp.flags.indexOf('g') >= 0 ? rp.flags : rp.flags + 'g';
  var re;
  try { re = new RegExp(rp.pattern, flags); } catch (e) { return; }
  var match;
  var sel = window.getSelection();
  while ((match = re.exec(text)) !== null) {
    if (!match[0]) { re.lastIndex++; continue; } // 防零宽匹配死循环
    // 先正序计算所有捕获组的绝对偏移，存入数组
    var groupRanges = [];
    var offset = 0;
    for (var gi = 0; gi < cond.group_formats.length; gi++) {
      var fmts = cond.group_formats[gi] || [];
      if (!fmts.length || (fmts.length === 1 && fmts[0] === 'none')) {
        groupRanges.push(null);
        continue;
      }
      var groupText = match[gi + 1];
      if (!groupText) {
        groupRanges.push(null);
        continue;
      }
      var posInMatch = match[0].indexOf(groupText, offset);
      if (posInMatch < 0) {
        groupRanges.push(null);
        continue;
      }
      var groupStart = match.index + posInMatch;
      var groupEnd = groupStart + groupText.length;
      offset = posInMatch + groupText.length;
      groupRanges.push({ start: groupStart, end: groupEnd, fmts: fmts });
    }
    // 正序应用：每组应用后若含块级格式（heading/p/remove/merge/align/note），重建 textNodes 并重算剩余组偏移
    var blockLevelOps = ['heading', 'p', 'remove', 'merge', 'align_left', 'align_center', 'align_right', 'note'];
    function isBlockLevel(op) {
      if (op.indexOf('heading') === 0) return true;
      return blockLevelOps.indexOf(op) >= 0;
    }
    for (var gi = 0; gi < groupRanges.length; gi++) {
      var gr = groupRanges[gi];
      if (!gr) continue;
      var range = _rangeFromOffsets(textNodes, gr.start, gr.end);
      if (!range) continue;
      sel.removeAllRanges();
      sel.addRange(range);
      var appliedBlockLevel = false;
      for (var fi = 0; fi < gr.fmts.length; fi++) {
        var op = gr.fmts[fi];
        if (op === 'none') continue;
        // merge 特殊处理：仅合并捕获组所在块与其紧邻的下一个兄弟块，不使用全选区合并
        if (op === 'merge') {
          var block = range.startContainer.nodeType === 3 ? range.startContainer.parentElement : range.startContainer;
          block = block && block.closest ? block.closest('p,div,h1,h2,h3,h4,h5,h6') : null;
          if (block && block.parentNode) {
            var nextBlock = block.nextElementSibling;
            while (nextBlock && !/^(P|DIV|H[1-6])$/.test(nextBlock.tagName)) {
              nextBlock = nextBlock.nextElementSibling;
            }
            if (nextBlock) {
              var row = ed.closest('.page-row');
              var i = row ? Number(row.dataset.i) : -1;
              histRun('合并段落', [i], function () {
                var needSpace = block.innerHTML.length > 0 && nextBlock.innerHTML.length > 0;
                block.innerHTML += (needSpace ? ' ' : '') + nextBlock.innerHTML;
                nextBlock.parentNode.removeChild(nextBlock);
                syncContent(ed);
                if (row) { markDirty(i); scheduleRemeasure(i); }
              });
            }
          }
          appliedBlockLevel = true;
        } else {
          applySingleFormat(op, ed);
          if (isBlockLevel(op)) appliedBlockLevel = true;
        }
      }
      // 若应用了块级格式，DOM 结构变化，需重建 textNodes 并重算后续组偏移
      if (appliedBlockLevel) {
        textNodes = _buildTextNodeList(scopeRoot);
        text = _textFromNodes(textNodes);
        // 重算后续组偏移
        for (var gj = gi + 1; gj < groupRanges.length; gj++) {
          var gr2 = groupRanges[gj];
          if (!gr2) continue;
          var groupText2 = match[gj + 1];
          if (!groupText2) { groupRanges[gj] = null; continue; }
          var posInMatch2 = text.indexOf(groupText2, gr.start); // 从当前组结束位置搜索
          if (posInMatch2 < 0) { groupRanges[gj] = null; continue; }
          gr2.start = posInMatch2;
          gr2.end = posInMatch2 + groupText2.length;
        }
      }
    }
  }
}

// 正则按匹配对象独立格式应用（match_formats）：对同一正则的每次匹配可设置不同格式
function applyRegexMatchFormats(cond, ed) {
  if (!cond || !cond.match_formats || !cond.match_formats.length) return;
  if (!cond.pattern) return;
  // 从 textNodes 拼接文本（与偏移量对齐，不使用 innerText）
  var scopeRoot = ed;
  if (cond.scope === 'paragraph') {
    var sel = window.getSelection();
    var pnode = sel && sel.rangeCount ? sel.getRangeAt(0).startContainer : ed;
    if (pnode.nodeType === 3) pnode = pnode.parentElement;
    if (!pnode || !ed.contains(pnode)) pnode = ed;
    scopeRoot = pnode.closest ? pnode.closest('p,div,h1,h2,h3,h4,h5,h6') : ed;
  }
  var textNodes = _buildTextNodeList(scopeRoot);
  var text = _textFromNodes(textNodes);
  if (!text) return;
  var rp = parseRegexPattern(cond.pattern);
  var flags = rp.flags.indexOf('g') >= 0 ? rp.flags : rp.flags + 'g';
  var re;
  try { re = new RegExp(rp.pattern, flags); } catch (e) { return; }
  var match;
  var mi = 0;
  while ((match = re.exec(text)) !== null) {
    if (!match[0]) { re.lastIndex++; continue; }
    var fmts = cond.match_formats[mi] || [];
    mi++;
    if (!fmts.length || (fmts.length === 1 && fmts[0] === 'none')) continue;
    var matchStart = match.index;
    var matchEnd = matchStart + match[0].length;
    var range = _rangeFromOffsets(textNodes, matchStart, matchEnd);
    if (!range) continue;
    sel.removeAllRanges();
    sel.addRange(range);
    for (var fi2 = 0; fi2 < fmts.length; fi2++) {
      var op2 = fmts[fi2];
      if (op2 === 'none') continue;
      applySingleFormat(op2, ed);
    }
  }
}

// 目标格式应用：根据 cond.target 计算格式作用范围（before/after/between）并应用格式
function applyTargetFormats(cond, ed) {
  if (!cond || !cond.target || cond.target === 'match') return false;
  var fmts = (cond.formats || []).filter(function (op) { return op !== 'none'; });
  if (!fmts.length) return false;
  // 从 textNodes 拼接文本（与偏移量对齐，不使用 innerText/selection）
  var scopeRoot = ed;
  if (cond.scope === 'paragraph') {
    var sel = window.getSelection();
    var pnode = sel && sel.rangeCount ? sel.getRangeAt(0).startContainer : ed;
    if (pnode.nodeType === 3) pnode = pnode.parentElement;
    if (!pnode || !ed.contains(pnode)) pnode = ed;
    scopeRoot = pnode.closest ? pnode.closest('p,div,h1,h2,h3,h4,h5,h6') : ed;
  }
  var textNodes = _buildTextNodeList(scopeRoot);
  var text = _textFromNodes(textNodes);
  if (!text || !cond.pattern) return false;
  // 查找匹配位置
  var rp, re, match;
  try {
    rp = parseRegexPattern(cond.pattern);
    re = new RegExp(rp.pattern, rp.flags);
    match = re.exec(text);
  } catch (e) { return false; }
  if (!match) return false;
  var matchStart = match.index;
  var matchEnd = matchStart + match[0].length;
  var rangeStart = -1, rangeEnd = -1;
  if (cond.target === 'before') {
    rangeStart = 0; rangeEnd = matchStart;
  } else if (cond.target === 'after') {
    rangeStart = matchEnd; rangeEnd = text.length;
  } else if (cond.target === 'between') {
    if (!cond.between_end_pattern) return false;
    // 在第一个匹配之后查找结束条件
    var rp2, re2, match2;
    try {
      rp2 = parseRegexPattern(cond.between_end_pattern);
      re2 = new RegExp(rp2.pattern, rp2.flags);
      re2.lastIndex = matchEnd;
      match2 = re2.exec(text);
    } catch (e) { return false; }
    if (!match2) return false;
    rangeStart = matchEnd; rangeEnd = match2.index;
  }
  if (rangeStart < 0 || rangeEnd <= rangeStart) return false;
  var range = _rangeFromOffsets(textNodes, rangeStart, rangeEnd);
  if (!range) return false;
  var sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  for (var fi = 0; fi < fmts.length; fi++) {
    var op = fmts[fi];
    if (op === 'none') continue;
    applySingleFormat(op, ed);
  }
  return true;
}

// edArg 可选：右键菜单快速应用时传入右键目标页（此时跳过 restoreFrRange，
// 不恢复弹窗打开时捕获的选区——右键场景的选区应保持右键页的光标位置）。
function applyFormatRule(rule, edArg) {
  const ed = edArg || currentEditable();
  if (!ed) { showToast('请先选中文字或把光标放入段落', 'warn'); return false; }
  if (!edArg) restoreFrRange();
  const res = evalFormatRule(rule, ed);
  if ((!res.fmts || !res.fmts.length) && (!res.groupConds || !res.groupConds.length) && (!res.matchConds || !res.matchConds.length) && (!res.targetConds || !res.targetConds.length)) {
    showToast('该规则没有匹配的格式，未做修改', 'warn'); return false;
  }
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  histRun('格式规则', [i], function () {
    ed.focus();
    inDiscreteOp = true;
    try {
      withScrollStable(function () {
        // 捕获组独立格式
        if (res.groupConds && res.groupConds.length) {
          for (var k = 0; k < res.groupConds.length; k++) {
            applyRegexGroupFormats(res.groupConds[k], ed);
          }
        }
        // 匹配对象独立格式
        if (res.matchConds && res.matchConds.length) {
          for (var k2 = 0; k2 < res.matchConds.length; k2++) {
            applyRegexMatchFormats(res.matchConds[k2], ed);
          }
        }
        // 目标范围格式（before/after/between）
        if (res.targetConds && res.targetConds.length) {
          for (var k3 = 0; k3 < res.targetConds.length; k3++) {
            applyTargetFormats(res.targetConds[k3], ed);
          }
        }
        // 普通格式
        if (res.fmts && res.fmts.length) {
          var applied = [];
          var run = function () {
            for (var j = 0; j < res.fmts.length; j++) {
              var op = res.fmts[j];
              if (applied.some(function (a) { return opsConflict(a, op); })) continue;
              applySingleFormat(op, ed);
              applied.push(op);
            }
          };
          if (res.page) withPageSelection(ed, run); else run();
        }
      });
    } finally { inDiscreteOp = false; }
    syncContent(ed);
    if (row) { markDirty(i); scheduleRemeasure(i); }
  });
  showToast('已应用格式规则「' + rule.name + '」', 'ok');
  return true;
}

// 单条件评估：空 pattern = 无条件（恒匹配）；scope=selection 用选中文字，
// scope=paragraph 用光标所在段落文本；regex 支持 /pattern/flags 语法。
function evalCondition(cond, ed) {
  if (!cond || !cond.pattern) return true;
  const sel = window.getSelection();
  let text = sel && sel.rangeCount ? (sel.toString() || '') : '';
  if (cond.scope === 'page') {
    // 当前页面作用域：整页可见文本（innerText 与 UI 所见一致，含所有块）
    text = ed && ed.innerText ? ed.innerText : '';
  } else if (cond.scope === 'paragraph') {
    let node = sel && sel.rangeCount ? sel.getRangeAt(0).startContainer : ed;
    if (node.nodeType === 3) node = node.parentElement;
    if (!node || !ed.contains(node)) node = ed;
    const block = node.closest ? node.closest('p,div,h1,h2,h3,h4,h5,h6') : null;
    text = (block ? block.innerText : '') || '';
  }
  const t = text || '';
  switch (cond.type) {
    case 'regex':
      try {
        const rp = parseRegexPattern(cond.pattern);
        return new RegExp(rp.pattern, rp.flags).test(t);
      } catch (e) { return false; }
    case 'contains': return t.indexOf(cond.pattern) >= 0;
    case 'prefix': return t.indexOf(cond.pattern) === 0;
    case 'suffix': return t.length >= cond.pattern.length && t.endsWith(cond.pattern);
  }
  return false;
}

// 规则求值：first=首个匹配条件生效即停（none 过滤后即使为空也停，充当「此处不处理」守卫）；
// all=全部匹配条件的格式按序拼接（none 过滤）。none 绝不进入应用列表。
// 返回 { fmts: [...], page: bool, groupConds: [...], matchConds: [...], targetConds: [...] }——page=true 表示任一匹配条件为「当前页面」作用域，
// groupConds 为含 group_formats 的正则匹配条件（独立应用捕获组格式），
// matchConds 为含 match_formats 的正则匹配条件（逐次匹配独立应用），
// targetConds 为 target 非 match 的条件（before/after/between，独立应用目标范围格式）。
function evalFormatRule(rule, ed) {
  const out = [];
  const groupConds = [];
  const matchConds = [];
  const targetConds = [];
  let page = false;
  for (const c of (rule && rule.conditions) || []) {
    if (evalCondition(c, ed)) {
      if (c.scope === 'page') page = true;
      if (c.target && c.target !== 'match') {
        targetConds.push(c);
        if (rule.mode === 'first') break;
      } else if (c.type === 'regex' && c.group_formats && c.group_formats.length) {
        groupConds.push(c);
        if (rule.mode === 'first') break;
      } else if (c.type === 'regex' && c.match_formats && c.match_formats.length) {
        matchConds.push(c);
        if (rule.mode === 'first') break;
      } else {
        const fmts = (c.formats || []).filter(function (op) { return op !== 'none'; });
        if (rule.mode === 'first') {
          // first 模式：首个匹配条件生效即停，但需把此前收集的 group/match/target 一并返回
          return { fmts: fmts, page: page, groupConds: groupConds, matchConds: matchConds, targetConds: targetConds };
        }
        out.push.apply(out, fmts);
      }
    }
  }
  return { fmts: out, page: page, groupConds: groupConds, matchConds: matchConds, targetConds: targetConds };
}
// 页面级应用辅助：临时把选区扩展为整页（selectNodeContents(ed)）→ 执行 fn →
// 恢复原选区。applyToSelectedBlocks 在 startBlock===ed 时会置空从首块收集全部块，
// 因此整页选区下格式对每个块逐个生效。
function withPageSelection(ed, fn) {
  const sel = window.getSelection();
  const origRanges = [];
  if (sel) for (let i = 0; i < sel.rangeCount; i++) origRanges.push(sel.getRangeAt(i).cloneRange());
  try {
    const r = document.createRange();
    r.selectNodeContents(ed);
    if (sel) {
      sel.removeAllRanges();
      sel.addRange(r);
    }
    fn();
  } finally {
    if (sel) {
      sel.removeAllRanges();
      for (const rr of origRanges) sel.addRange(rr);
    }
  }
}
function applyFormatsList(fmts, ed, pageScope) {
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  const skipped = [];
  histRun('格式规则', [i], function () {
    ed.focus();
    inDiscreteOp = true;
    try {
      const applied = [];
      withScrollStable(function () {
        const run = function () {
          for (const op of fmts) {
            if (applied.some(function (a) { return opsConflict(a, op); })) { skipped.push(op); continue; }
            applySingleFormat(op, ed);
            applied.push(op);
          }
        };
        if (pageScope) withPageSelection(ed, run); else run();
      });
    } finally { inDiscreteOp = false; }
    syncContent(ed);
    if (row) { markDirty(i); scheduleRemeasure(i); }
  });
  return skipped;
}
function applyAllFormatRules() {
  const ed = currentEditable();
  if (!ed) { showToast('请先选中文字或把光标放入段落', 'warn'); return; }
  restoreFrRange();
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  const appliedOps = [];
  const skipped = [];
  let appliedRules = 0;
  histRun('格式规则（全部）', [i], function () {
    ed.focus();
    inDiscreteOp = true;
    try {
      withScrollStable(function () {
        for (const rule of formatRules) {
          restoreFrRange();
          const res = evalFormatRule(rule, ed);
          const hasFmt = res.fmts && res.fmts.length;
          const hasGrp = res.groupConds && res.groupConds.length;
          const hasTgt = res.targetConds && res.targetConds.length;
          const hasMtc = res.matchConds && res.matchConds.length;
          if (!hasFmt && !hasGrp && !hasTgt && !hasMtc) continue;
          let any = false;
          if (hasGrp) {
            for (var k = 0; k < res.groupConds.length; k++) {
              applyRegexGroupFormats(res.groupConds[k], ed);
              any = true;
            }
          }
          if (hasMtc) {
            for (var m = 0; m < res.matchConds.length; m++) {
              applyRegexMatchFormats(res.matchConds[m], ed);
              any = true;
            }
          }
          if (hasFmt) {
            const run = function () {
              for (const op of res.fmts) {
                if (appliedOps.some(function (a) { return opsConflict(a, op); })) { skipped.push(rule.name + ':' + op); continue; }
                applySingleFormat(op, ed);
                appliedOps.push(op);
                any = true;
              }
            };
            if (res.page) withPageSelection(ed, run); else run();
          }
          if (hasTgt) {
            for (var tg = 0; tg < res.targetConds.length; tg++) {
              applyTargetFormats(res.targetConds[tg], ed);
              any = true;
            }
          }
          if (any) appliedRules++;
        }
      });
    } finally { inDiscreteOp = false; }
    syncContent(ed);
    if (row) { markDirty(i); scheduleRemeasure(i); }
  });
  if (appliedRules === 0 && skipped.length === 0) { showToast('没有可应用的规则', 'warn'); return; }
  showToast('已应用 ' + appliedRules + ' 条规则' + (skipped.length ? '，跳过冲突格式 ' + skipped.length + ' 个' : ''), skipped.length ? 'warn' : 'ok');
  closeFormatRulesModal();
}
function _mergeSelectedBlocks(ed) {
  // 合并选区内所有块到第一个块（段落合并）
  if (typeof isComposing !== 'undefined' && isComposing) {
    _pendingOps.push(() => _mergeSelectedBlocks(ed));
    showToast('输入法中，已将操作排队，输入结束后自动应用', 'warn');
    return;
  }
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return;
  const range = sel.getRangeAt(0);
  const startNode = range.startContainer.nodeType === 3 ? range.startContainer.parentElement : range.startContainer;
  const endNode = range.endContainer.nodeType === 3 ? range.endContainer.parentElement : range.endContainer;
  let startBlock = (startNode && startNode.closest) ? (startNode.closest('p,div,h1,h2,h3,h4,h5,h6') || null) : null;
  let endBlock = (endNode && endNode.closest) ? (endNode.closest('p,div,h1,h2,h3,h4,h5,h6') || null) : null;
  if (startBlock === ed) startBlock = null;
  if (endBlock === ed) endBlock = null;
  const blocks = _blocksBetween(ed, startBlock, endBlock);
  if (blocks.length < 2) return;
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  histRun('合并段落', [i], function () {
    // 把后续块的内容追加到第一个块，再逐个移除
    const first = blocks[0];
    for (let k = 1; k < blocks.length; k++) {
      const b = blocks[k];
      if (!b || !b.parentNode) continue;
      // 在两段之间加一个空格避免中英混排粘连
      const needSpace = first.innerHTML.length > 0 && b.innerHTML.length > 0;
      first.innerHTML += (needSpace ? ' ' : '') + b.innerHTML;
      b.parentNode.removeChild(b);
    }
    syncContent(ed);
    if (row) { markDirty(i); scheduleRemeasure(i); }
    // 将光标置于合并后块的末尾
    try {
      const r2 = document.createRange();
      r2.selectNodeContents(first);
      r2.collapse(false);
      sel.removeAllRanges();
      sel.addRange(r2);
    } catch (e) {}
  });
}

function applySingleFormat(op, ed) {
  if (op === 'bold') { applyInlineFormat(ed, 'bold'); return; }
  if (op === 'no_bold') { applyToSelectedBlocks(ed, function (block) { block.style.fontWeight = 'normal'; }); return; }
  if (op === 'italic') { applyInlineFormat(ed, 'italic'); return; }
  if (op === 'remove') { applyToSelectedBlocks(ed, function () { document.execCommand('removeFormat'); }); return; }
  if (op === 'p') { applyToSelectedBlocks(ed, function (block) { _convertBlockTag(block, 'p'); }); return; }
  if (op === 'merge') { _mergeSelectedBlocks(ed); return; }
  if (op === 'note') { toggleNote(ed); return; }
  if (op === 'citation') { toggleCitation(ed); return; }
  if (op.indexOf('align_') === 0) { applyAlign(ed, op.slice(6)); return; }
  if (op.indexOf('heading') === 0) {
    const tag = 'h' + op.slice(7);
    applyToSelectedBlocks(ed, function (block) { _convertBlockTag(block, tag); });
  }
}

// Apply inline format (bold/italic) handling collapsed selection in paragraph scope
function applyInlineFormat(ed, format) {
  const sel = window.getSelection();
  const isCollapsed = !sel || sel.rangeCount === 0 || sel.getRangeAt(0).collapsed;
  
  if (isCollapsed) {
    // Collapsed selection: apply to entire block(s) by wrapping content
    applyToSelectedBlocks(ed, function(block) {
      if (format === 'bold') {
        // Wrap block content in <strong> if not already bold
        if (!block.querySelector('strong, b') && block.textContent.trim()) {
          const wrapper = document.createElement('strong');
          while (block.firstChild) wrapper.appendChild(block.firstChild);
          block.appendChild(wrapper);
        }
      } else if (format === 'italic') {
        // Wrap block content in <em> if not already italic
        if (!block.querySelector('em, i') && block.textContent.trim()) {
          const wrapper = document.createElement('em');
          while (block.firstChild) wrapper.appendChild(block.firstChild);
          block.appendChild(wrapper);
        }
      }
    });
  } else {
    // Normal selection: use execCommand
    applyToSelectedBlocks(ed, function () { document.execCommand(format); });
  }
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
  if (mods.length === 0 && !/^F\d{1,2}$/.test(key) && key !== 'Enter' && key !== 'Escape') return null; // 必须带修饰键或功能键（Enter/Escape 裸键放行）
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
  loadFontSettings();
  document.getElementById('editorFontSizeInput').value = parseInt(document.documentElement.style.getPropertyValue('--editor-font-size') || '14', 10);
  document.getElementById('modalBg').style.display = 'flex';
  // 默认激活快捷键标签
  document.querySelectorAll('.settings-tab').forEach(b => b.classList.remove('active'));
  document.querySelector('.settings-tab[data-tab="shortcuts"]').classList.add('active');
  document.querySelectorAll('.settings-panel').forEach(p => p.style.display = 'none');
  document.getElementById('panel-shortcuts').style.display = 'block';
}
function closeSettings() { capturingOp = null; document.getElementById('modalBg').style.display = 'none'; }

async function loadFontSettings() {
  try {
    const res = await fetchJSON('/api/config');
    if (res && res.fonts) {
      document.getElementById('fontBody').value = res.fonts.body || '';
      document.getElementById('fontHeading').value = res.fonts.heading || '';
      document.getElementById('fontNote').value = res.fonts.note || '';
      document.getElementById('fontCitation').value = res.fonts.citation || '';
      applyFontCSSVariables(res.fonts);
    }
    if (res && typeof res.citationItalicEnabled === 'boolean') {
      document.getElementById('citationItalicEnabled').checked = res.citationItalicEnabled;
    }
  } catch (e) { console.warn('loadFontSettings failed', e); }
}

async function saveFontSettings() {
  const fonts = {
    body: document.getElementById('fontBody').value.trim(),
    heading: document.getElementById('fontHeading').value.trim(),
    note: document.getElementById('fontNote').value.trim(),
    citation: document.getElementById('fontCitation').value.trim(),
  };
  const citationItalicEnabled = document.getElementById('citationItalicEnabled').checked;
  try {
    await fetchJSON('/api/config', { method: 'POST', body: JSON.stringify({ fonts, citationItalicEnabled }) });
    applyFontCSSVariables(fonts);
    setStatus('字体设置已保存');
  } catch (e) { setStatus('字体设置保存失败: ' + e); }
}

function applyFontCSSVariables(fonts) {
  const root = document.documentElement;
  if (fonts.body) root.style.setProperty('--font-body', fonts.body);
  if (fonts.heading) root.style.setProperty('--font-heading', fonts.heading);
  if (fonts.note) root.style.setProperty('--font-note', fonts.note);
  if (fonts.citation) root.style.setProperty('--font-citation', fonts.citation);
}

async function openHelp() {
  document.getElementById('helpModalBg').style.display = 'flex';
  try {
    const res = await fetch('/help.md');
    if (res.ok) {
      const md = await res.text();
      document.getElementById('helpContent').innerHTML = markedParse(md);
    } else {
      document.getElementById('helpContent').innerHTML = '<p style="color:#c0392b;">帮助文档加载失败</p>';
    }
  } catch (e) {
    document.getElementById('helpContent').innerHTML = '<p style="color:#c0392b;">帮助文档加载失败: ' + e + '</p>';
  }
}

// 简单的 Markdown 解析器（支持本帮助文档所需语法）
function markedParse(md) {
  return md
    .replace(/^### (.*$)/gim, '<h3>$1</h3>')
    .replace(/^## (.*$)/gim, '<h2>$1</h2>')
    .replace(/^# (.*$)/gim, '<h1>$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/__(.+?)__/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/_(.+?)_/g, '<em>$1</em>')
    .replace(/`(.+?)`/g, '<code>$1</code>')
    .replace(/\[(.+?)\]\((.+?)\)/g, '<a href="$2" target="_blank">$1</a>')
    .replace(/^\|(.+)\|$/gm, (m) => {
      const cells = m.split('|').slice(1, -1).map(c => c.trim());
      return '<tr>' + cells.map(c => '<td>' + c + '</td>').join('') + '</tr>';
    })
    .replace(/^---$/gm, '<hr/>')
    .replace(/\n\n/g, '</p><p>')
    .replace(/\n/g, '<br/>')
    .replace(/^<p><h([1-3])>/g, '<h$1>')
    .replace(/<\/h([1-3])><\/p>/g, '</h$1>')
    .replace(/^<p><table>/g, '<table>')
    .replace(/<\/table><\/p>/g, '</table>')
    .replace(/^<p><hr\/><\/p>/g, '<hr/>');
}

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

function histPush(before, after, errBefore, errAfter, label) {
  // D2: 撤销条目含 errBefore/errAfter（Map(i→errors 深拷贝)），只含相关页
  const entry = { before: before, after: after, label: label };
  if (errBefore && errBefore.size) entry.errBefore = errBefore;
  if (errAfter && errAfter.size) entry.errAfter = errAfter;
  undoStack.push(entry);
  if (undoStack.length > UNDO_LIMIT) undoStack.shift();
  redoStack.length = 0; // 新操作使重做历史失效
  histUpdateButtons();
}
function histCommitInput() {
  if (undoIdleTimer) { clearTimeout(undoIdleTimer); undoIdleTimer = null; }
  if (!currentUndo) return;
  const after = new Map();
  const errAfter = new Map();
  for (const i of currentUndo.pages) {
    after.set(i, pageSource(i));
    if (currentUndo.errBefore && currentUndo.errBefore.has(i)) {
      errAfter.set(i, _copyErrors(i));
    }
  }
  histPush(currentUndo.before, after, currentUndo.errBefore, errAfter, currentUndo.label);
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
  // D3: 同时捕获 errBefore（仅新建时捕获当时状态）
  const errBefore = new Map([[i, _copyErrors(i)]]);
  currentUndo = { before: before, pages: new Set([i]), errBefore: errBefore, label: '输入' };
}
function histTouchInput(i) {
  // input 事件（变更后）触发：只扩展进行中操作的页面集合并续期空闲计时；
  // 操作起点由「变更前」事件建立，这里不补建（否则快照已含本次变更）。
  if (i < 0 || inDiscreteOp) return;
  if (currentUndo) { currentUndo.pages.add(i); histScheduleIdle(); }
}
// 离散（同步）操作包装：先收掉进行中的输入操作，捕获 before，执行 fn，提交
let _histDepth = 0; // 嵌套 histRun 深度：>0 时内层直接执行（外层已建快照），避免双撤销条目（2026-08-15）
function histRun(label, pagesArr, fn) {
  if (_histDepth > 0) return fn(); // 嵌套 histRun：外层已建快照，直接执行
  _histDepth++;
  histCommitInput();
  const before = new Map();
  const errBefore = new Map();
  for (const i of (pagesArr || [])) {
    before.set(i, pageSource(i));
    errBefore.set(i, _copyErrors(i));
  }
  inDiscreteOp = true;
  let out;
  try { out = fn(); }
  finally { inDiscreteOp = false; _histDepth--; }
  histEnd(before, label, errBefore);
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
function histEnd(before, label, errBefore) {
  const after = new Map();
  const errAfter = new Map();
  for (const [i, src] of before) {
    const now = pageSource(i);
    if (now !== src) {
      after.set(i, now);
      if (errBefore && errBefore.has(i)) errAfter.set(i, _copyErrors(i));
    } else {
      before.delete(i);
    }
  }
  if (before.size) histPush(before, after, errBefore, errAfter, label);
}
function histClear() {
  if (undoIdleTimer) { clearTimeout(undoIdleTimer); undoIdleTimer = null; }
  currentUndo = null;
  undoStack = []; redoStack = [];
  histUpdateButtons();
}
function restoreHistorySnapshot(snap, errSnap) {
  // 恢复指定页的源（写入当前模式对应 map），重渲染已挂载行；若当前编辑页被
  // 恢复则重聚焦并置光标到末尾。恢复只写 map + innerHTML，不派发 beforeinput/
  // input，不会触发新的历史记录。
  // D5: 标注对账——恢复文本时同步恢复 proofreadErrors[i]
  for (const [i, src] of snap) {
    if (mdMode) mdSourceMap.set(i, src);
    else contentMap.set(i, src);
    // 标注对账：有 errSnap 则恢复，否则清空该页现有标注防错位
    if (errSnap && errSnap.has(i)) {
      proofreadErrors[i] = errSnap.get(i);
    } else if (proofreadErrors[i] && proofreadErrors[i].length) {
      proofreadErrors[i] = [];
    }
    const row = host.querySelector('.page-row[data-i="' + i + '"]');
    if (row) {
      const ed = row.querySelector('.editable');
      if (ed) { ed.innerHTML = displayHtml(i); _reapplyProofread(i); remeasure(i); }
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
  // D5: 兼容旧格式（无 errBefore 字段）——undefined errSnap → 只清空该页现有标注
  restoreHistorySnapshot(entry.before, entry.errBefore);
  redoStack.push(entry);
  if (redoStack.length > UNDO_LIMIT) redoStack.shift();
  histUpdateButtons();
  setStatus('已撤回：' + entry.label);
  return true;
}
function redoHistory() {
  const entry = redoStack.pop();
  if (!entry) { setStatus('没有可前进的操作'); return false; }
  restoreHistorySnapshot(entry.after, entry.errAfter);
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

// 纠错悬浮窗快捷键守卫：errKey 打开且焦点不在输入控件时才触发（返回 false = 未消费，交浏览器默认行为）
function _inField() { const t = document.activeElement; return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT'); }
function acceptErrShortcut() { if (errKey && !_inField()) { document.getElementById('errOk').click(); return true; } return false; }
function ignoreErrShortcut() { if (errKey && !_inField()) { document.getElementById('errNo').click(); return true; } return false; }

// 工具操作快捷键映射（op → 直接调用函数；格式/标记操作不在此表，走 applyOp）
const SHORTCUT_ACTIONS = {
  search: openSearchModal,
  clean: cleanAll,
  convert_t2s: () => convertAll('t2s'),
  convert_s2t: () => convertAll('s2t'),
  toggle_md: () => setMdMode(!mdMode),
  undo: undoHistory,
  redo: redoHistory,
  history: openHistory,
  export: openExportModal,
  save: save,
  stage: stage,
  finish: finish,
  jump: jumpToPage,
  help: () => { document.getElementById('helpModalBg').style.display = 'flex'; },
  settings: openSettings,
  proofread_correct: proofreadCorrect,
  proofread_reocr: runReocr,
  proofread_apply: proofreadApplyCurrent,
  proofread_clear: proofreadClearCurrent,
  proofread_revert: proofreadRevertCurrent,
  proofread_accept: acceptErrShortcut,
  proofread_ignore: ignoreErrShortcut,
};

// ---------- 全局事件（统一快捷键分发） ----------
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
  const act = SHORTCUT_ACTIONS[op];
  if (act) {
      // 撤销/重做在 INPUT/TEXTAREA/SELECT 聚焦时不触发（避免与输入框原生行为冲突）
      if (op === 'undo' || op === 'redo') {
        const t = e.target;
        if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return;
      }
      if (act() === false) return;   // act 返回 false：不 preventDefault、不进 applyOp，交浏览器默认行为（如 contenteditable 换行）
      e.preventDefault();
      return;
  }
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
  // proofread 设置：LLM 深度校对开关/模型 + 原有规则开关，均持久化在 config.json
  // （/api/proofread_settings）。随机端口下 localStorage 每次运行失效（2026-08-07 修复 → 迁移到服务端）。
  const prLlmEnableEl = document.getElementById('prLlmEnable');
  const prLlmModelEl = document.getElementById('prLlmModel');
  const prLegacyRulesEl = document.getElementById('prLegacyRules');
  async function loadProofreadLlm() {
    try {
      const res = await fetchJSON('/api/proofread_settings');
      if (!res.ok) throw new Error(res.error || '读取设置失败');
      proofreadLlmEnabled = !!res.enabled;
      proofreadLlmModel = res.model || '';
      proofreadLegacyRules = !!res.enable_legacy_rules;
      prLlmEnableEl.checked = proofreadLlmEnabled;
      if (prLegacyRulesEl) prLegacyRulesEl.checked = proofreadLegacyRules;
      prLlmModelEl.innerHTML = '';
      const opts = Array.isArray(res.available) ? res.available : [];
      opts.forEach(function (k) {
        const o = document.createElement('option');
        o.value = k; o.textContent = k;
        if (k === (proofreadLlmModel || res.selected)) o.selected = true;
        prLlmModelEl.appendChild(o);
      });
      if (!opts.length) {
        const o = document.createElement('option');
        o.value = ''; o.textContent = '（无可用模型）';
        prLlmModelEl.appendChild(o);
      }
    } catch (e) { console.warn('loadProofreadLlm failed: ' + e.message); }
  }
  // legacyToast=true 时提示语针对「原有规则」开关（由该勾选框的 change 触发）
  async function saveProofreadLlm(legacyToast) {
    proofreadLlmEnabled = prLlmEnableEl.checked;
    proofreadLlmModel = prLlmModelEl.value || '';
    proofreadLegacyRules = prLegacyRulesEl ? !!prLegacyRulesEl.checked : proofreadLegacyRules;
    try {
      const res = await fetchJSON('/api/proofread_settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: proofreadLlmEnabled,
          model: proofreadLlmModel,
          enable_legacy_rules: proofreadLegacyRules,
        }),
      });
      if (!res.ok) { showToast('保存校对设置失败: ' + (res.error || ''), 'fail'); return; }
      if (legacyToast === true) {
        showToast(proofreadLegacyRules ? '已启用原有规则（半角转全角/引号配对/混淆表/词典）' : '已关闭原有规则，校正只执行三条新规则', 'ok');
      } else {
        showToast(proofreadLlmEnabled ? '已启用 LLM 深度校对' : '已关闭 LLM 深度校对', 'ok');
      }
    } catch (e) {
      showToast('保存校对设置失败: ' + e.message, 'fail');
    }
  }
  // init
  loadProofreadLlm();
  prLlmEnableEl.addEventListener('change', function () { saveProofreadLlm(false); });
  prLlmModelEl.addEventListener('change', function () { saveProofreadLlm(false); });
  if (prLegacyRulesEl) prLegacyRulesEl.addEventListener('change', function () { saveProofreadLlm(true); });
  // llama-server 启停（句子校正：以纯文本模式启动，不附加图像投影）
  async function refreshLlmStatus() {
    const el = document.getElementById('prLlmStatus');
    if (!el) return;
    try {
      const res = await fetchJSON('/api/llm_status');
      if (!res.ok) { el.textContent = '服务状态: 未知（' + (res.error || '') + '）'; return; }
      // loading：进程存活但模型仍在加载（health 503），两个按钮都禁用避免重复启动/误停
      const loading = !res.running && !!res.loading;
      el.textContent = res.running
        ? ('服务状态: 运行中' + (res.mismatch
            ? '（其他模型，可停止后切换）'
            : (res.model ? '（' + res.model + '）' : '')))
        : (loading ? '服务状态: 启动中…' : '服务状态: 未运行');
      const startBtn = document.getElementById('prLlmStart');
      const stopBtn = document.getElementById('prLlmStop');
      if (startBtn) startBtn.disabled = !!res.running || loading;
      if (stopBtn) stopBtn.disabled = !res.running || loading;
    } catch (e) { el.textContent = '服务状态: 未知'; }
  }
  async function startLlm() {
    // 启动是阻塞请求（大模型加载可能数分钟），先给即时反馈避免界面看起来没反应
    const statusEl = document.getElementById('prLlmStatus');
    const startBtnNow = document.getElementById('prLlmStart');
    if (statusEl) statusEl.textContent = '服务状态: 启动中…';
    if (startBtnNow) startBtnNow.disabled = true;
    try {
      const modelEl = document.getElementById('prLlmModel');
      const res = await fetchJSON('/api/llm_start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: (modelEl && modelEl.value) || proofreadLlmModel || '' }),
      });
      if (!res.ok) { showToast('启动服务失败: ' + (res.error || ''), 'fail'); refreshLlmStatus(); return; }
      showToast(res.message || '服务已启动', res.running ? 'ok' : 'fail');
    } catch (e) { showToast('启动服务失败: ' + e.message, 'fail'); }
    refreshLlmStatus();
  }
  async function stopLlm() {
    try {
      const res = await fetchJSON('/api/llm_stop', { method: 'POST' });
      if (!res.ok) { showToast('停止服务失败: ' + (res.error || ''), 'fail'); refreshLlmStatus(); return; }
      showToast(res.message || '已停止服务', 'ok');
    } catch (e) { showToast('停止服务失败: ' + e.message, 'fail'); }
    refreshLlmStatus();
  }
  refreshLlmStatus();
  document.getElementById('prLlmStart').addEventListener('click', startLlm);
  document.getElementById('prLlmStop').addEventListener('click', stopLlm);

document.getElementById('cleanBtn').addEventListener('click', cleanAll);
document.getElementById('proofreadBtn').addEventListener('click', toggleProofreadMenu);
document.getElementById('prMenuCorrect').addEventListener('click', proofreadCorrect);
document.getElementById('prMenuReocr').addEventListener('click', runReocr);
document.getElementById('prMenuApply').addEventListener('click', proofreadApplyCurrent);
document.getElementById('prMenuClear').addEventListener('click', proofreadClearCurrent);
document.getElementById('prMenuRevert').addEventListener('click', proofreadRevertCurrent);
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
// 格式规则弹窗绑定
document.getElementById('formatRulesBtn').addEventListener('click', openFormatRulesModal);
document.getElementById('formatRulesCloseBtn').addEventListener('click', closeFormatRulesModal);
document.getElementById('formatRulesModalBg').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeFormatRulesModal(); });
document.getElementById('formatRuleNewBtn').addEventListener('click', newFormatRule);
document.getElementById('formatRulesApplyAllBtn').addEventListener('click', applyAllFormatRules);
document.getElementById('frSaveBtn').addEventListener('click', saveFormatRule);
document.getElementById('frCancelBtn').addEventListener('click', closeRuleModal);
document.getElementById('frRuleCloseBtn').addEventListener('click', closeRuleModal);
document.getElementById('frRuleModalBg').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeRuleModal(); });
document.getElementById('frAddCondBtn').addEventListener('click', addCondition);
document.getElementById('frFmtPopupCloseBtn').addEventListener('click', closeFmtPopup);
document.getElementById('frFmtCancelBtn').addEventListener('click', closeFmtPopup);
document.getElementById('frFmtOkBtn').addEventListener('click', confirmFmtPopup);
document.getElementById('frFmtPopupBg').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeFmtPopup(); });
// 格式规则快捷键 Ctrl+Shift+Q（独立注册，不依赖 bindings 体系）
document.addEventListener('keydown', (e) => {
  if (e.ctrlKey && e.shiftKey && (e.key === 'Q' || e.key === 'q')) {
    e.preventDefault();
    openFormatRulesModal();
  }
});
document.getElementById('closeSettings').addEventListener('click', closeSettings);
document.getElementById('modalBg').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeSettings(); });
// 设置面板标签切换
document.querySelectorAll('.settings-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.settings-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const tab = btn.dataset.tab;
    document.querySelectorAll('.settings-panel').forEach(p => p.style.display = 'none');
    document.getElementById('panel-' + tab).style.display = 'block';
  });
});
// 字体设置保存
['fontBody','fontHeading','fontNote','fontCitation'].forEach(id => {
  const el = document.getElementById(id);
  el.addEventListener('change', () => saveFontSettings());
});
document.getElementById('citationItalicEnabled').addEventListener('change', () => saveFontSettings());
// 编辑器字号设置
document.getElementById('editorFontSizeInput').addEventListener('change', (e) => {
  const v = parseInt(e.target.value, 10) || 14;
  applyFontSize(v);
});
// 暂存/保存/完成并转换/快捷键设置（2026-08-07 修复：四个绑定曾整块丢失 → 按钮点击无响应）
document.getElementById('saveBtn').addEventListener('click', save);
document.getElementById('stageBtn').addEventListener('click', stage);
document.getElementById('finishBtn').addEventListener('click', finish);
document.getElementById('settingsBtn').addEventListener('click', openSettings);
document.getElementById('mdToggleBtn').addEventListener('click', () => setMdMode(!mdMode));
document.getElementById('helpBtn').addEventListener('click', openHelp);
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
// ---------- 滚动驱动虚拟列表 ----------
// wheel/touchmove 置位「用户主动滚动」时间戳（供 withScrollStable 放弃还原）；
// scroll 事件置位任意滚动时间戳并 rAF 节流驱动 updateViewport 挂载视口附近行。
// 曾因重构丢失该块导致只挂载初始窗口、滚动后后续页空白（2026-08 修复后再次
// 丢失，2026-08-07 恢复）。
const markUserScroll = () => { lastUserScrollTs = Date.now(); lastAnyScrollTs = Date.now(); };
const markAnyScroll = () => { lastAnyScrollTs = Date.now(); };
let _viewportRaf = 0;
const scheduleViewport = () => {
  if (_viewportRaf) return;
  _viewportRaf = requestAnimationFrame(() => { _viewportRaf = 0; updateViewport(); });
};
window.addEventListener('wheel', markUserScroll, { passive: true });
window.addEventListener('touchmove', markUserScroll, { passive: true });
window.addEventListener('scroll', () => { markAnyScroll(); scheduleViewport(); hidePopup(); closeContextMenu(); }, { passive: true });
window.addEventListener('beforeunload', (e) => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });
// ---------- 浏览器存活监测 ----------
setInterval(() => { fetch('/api/heartbeat', { method: 'POST' }).catch(() => {}); }, 30000);
window.addEventListener('pagehide', () => { navigator.sendBeacon('/api/gone'); });
// IME composition guard and pending ops queue（顶层注册，避免依赖 setMdMode 调用）
window.isComposing = false;
window._pendingOps = [];
function _flushPendingOps() { while (window._pendingOps.length) { const f = window._pendingOps.shift(); try { f(); } catch (e) { console.error('pending op failed', e); } } }
document.addEventListener('compositionstart', () => { window.isComposing = true; });
document.addEventListener('compositionend', () => { window.isComposing = false; _flushPrPending(); setTimeout(_flushPendingOps, 0); });

// ---------- 初始化 ----------
(async function init() {
  try {
    pages = (await fetchJSON('/api/pages')).pages;
  } catch (e) { document.body.textContent = '加载失败: ' + e; return; }
  heights.length = pages.length; heights.fill(0);
  est = pages.length ? 420 : 420;
  loadBindingsFromServer();   // 服务端快捷键设置（异步覆盖，失败静默回退 localStorage/DEFAULTS）
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
  updateViewport();
  setStatus('已加载 ' + pages.length + ' 页');
})();
</script>
</body>
</html>
"""
