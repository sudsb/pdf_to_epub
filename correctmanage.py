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

# 格式规则服务端引擎（任务 A：rulemanage.py）
try:
    import rulemanage
except ImportError:
    rulemanage = None

# 加粗注释标签转换（htmlmanage.py）
try:
    from htmlmanage import transform_note_labels
except ImportError:
    transform_note_labels = None

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
# 行内对齐样式正则：style="text-align:left|center|right"（含空格容忍）
_ALIGN_STYLE_RE = re.compile(r"text-align\s*:\s*(left|center|right)\s*(?:;|$)")
# 行内格式类白名单：note/citation 等由规则引擎产生的行内 span 类
_INLINE_FORMAT_CLASSES = {"ptoe-note", "ptoe-citation"}

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
        or c == "ptoe-flush"
        or c == "ptoe-indent"
        or c == "ptoe-citation"
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
                    or c == "ptoe-flush"
                    or c == "ptoe-indent"
                    or c == "ptoe-citation"
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
        self.data_attrs: list[tuple[str, str]] = []  # 当前块保留的缩进/间距 data 属性
        self.skip: int = 0  # 非内容标签嵌套深度

    def _flush(self) -> None:
        if self.block is None:
            return
        closes = "".join(f"</{t}>" for t in reversed(self.stack))
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
                self.buf.append(f'<span data-ptoe-marker="{v}"{cls_html}>')
                self.stack.append("span")
            else:
                # 保留行内格式 span：对齐样式、注释类、引用类
                # 这些由规则引擎产生，需在 sanitize 后保留以便前端渲染
                style = attrs_d.get("style", "")
                cls_attr = attrs_d.get("class", "")
                keep = False
                keep_attrs = []
                if _ALIGN_STYLE_RE.search(style):
                    # 规则引擎用内联样式实现多对齐共存：style="text-align:center"
                    keep = True
                    keep_attrs.append(f'style="{_html.escape(style, quote=True)}"')
                cls_list = (cls_attr or "").split()
                for c in cls_list:
                    if c in _INLINE_FORMAT_CLASSES:
                        keep = True
                        keep_attrs.append(f'class="{_html.escape(c, quote=True)}"')
                        break  # 只保留第一个匹配的格式类
                if keep:
                    attrs_html = " ".join(keep_attrs)
                    self.buf.append(f"<span {attrs_html}>")
                    self.stack.append("span")
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
# 清理：[1]/【2】/［3］ 等方括号包裹的数字统一修正为全角六角括号 〔n〕（2026-08）
# x = 任意数字（半角/全角均可，内容原样保留）；已是 〔n〕 的不动
_BRACKET_DIGIT_RE = re.compile(r"[\[［【]\s*([0-9０-９]+)\s*[\]］】]")
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
_BRACKET_PAIR_RES = (
    re.compile(r"【([^【】]*)】"),
    re.compile(r"\[([^\[\]\n]{1,32})\]"),
    re.compile(r"［([^［］]*)］"),
    re.compile(r"（([^()]*?)）"),  # 补充全角圆括号（123）→ 〔123〕
)


def _clean_ulq_bracket_junk(text: str) -> str:
    """把 ULQ 输出的杂符包裹引注（如 （^{[1]】}、^{[2]}）折叠为〔n〕。"""
    return _ULQ_JUNK_BRACKET_RE.sub(r"〔\1〕", text)


def _normalize_bracket_pairs(text: str) -> str:
    """把成对的 【x】/[x]/［x］ 统一替换为 〔x〕（允许符号混用）。"""
    for pat in _BRACKET_PAIR_RES:
        text = pat.sub(r"〔\1〕", text)
    return text


def _normalize_brackets(text: str) -> str:
    """先清 ULQ 杂符包裹，再统一括号对。"""
    return _normalize_bracket_pairs(_clean_ulq_bracket_junk(text))


def _clean_bracket_junk_html(html: str) -> str:
    """对 HTML 片段做杂符括号清理（token 级，只动文本节点）。

    部分大模型会把原文的 〔x〕 引注识别成 \\〔^{x〕}\\ 这类杂符包裹格式
    （\\ ^ { } 等无效字符夹着括号），这些字符进入矫正界面/对比前必须清除。
    逐 token 处理：`<...>` 与标记 span 原样保留（绝不能拆标签，否则会破坏
    img 属性/标记结构），仅对文本 token 做 _normalize_brackets
    （杂符清理 + 括号对统一，见 _ULQ_JUNK_BRACKET_RE）。
    """
    if not html or re.search(r"[\\^~`|·{}]", html) is None:
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
            # [1]/【2】等方括号数字统一修正为 〔n〕（全 token 处理）
            t2 = _BRACKET_DIGIT_RE.sub("〔\\1〕", t2)
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
        # 2026-08-30：历史/已存 HTML 内容 serve 前做杂符括号清理（token 级，
        # 只动文本节点）——历史版本可能保存过 \\〔^{x〕}\\ 之类大模型杂符包裹，
        # 不清理则界面正文与 reocr 的 current_text 基准不一致、diff 偏移错位。
        cleaned = _clean_bracket_junk_html(raw)
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
    """
    out = []
    for line in str(text).split("\n"):
        line = _normalize_brackets(line.strip())
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
    prev_note_joinable = False  # 上一渲染块是否为可合并的注释段落（无注释标记路径）
    note_join_prev = False  # 上一注释块的段尾是否带段落标记（join）
    for item in parsed:
        if item["note"]:
            if not note_markers:
                # 无注释标记：注释段落原位保留（仅套用注释格式，不移动位置）；
                # 段落标记（join）仍然生效——把前后两个相邻注释段落合并为一个 <p>
                html = "".join(h for h, _m in item["segments"]).strip()
                first_join = any(
                    t == "join" for _h, ms in item["segments"][:1] for t, _l in ms
                )
                if html:
                    deferred_join = False  # 防止并入正文段落，保持原位
                    if (
                        (note_join_prev or first_join)
                        and prev_note_joinable
                        and cur_article
                        and cur_article[-1].startswith("<p")
                        and cur_article[-1].endswith("</p>")
                    ):
                        cur_article[-1] = (
                            cur_article[-1][: -len("</p>")] + html + "</p>"
                        )
                    else:
                        push_content(item["kind"], html, item.get("attrs", ""))
                    prev_note_joinable = True
                note_join_prev = any(t == "join" for t, _l in item["trailing"])
            continue
        # 非注释块：重置注释段落合并状态（段落标记不跨非注释块生效）
        prev_note_joinable = False
        note_join_prev = False
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


def _write_server_info(port: int) -> None:
    """原子写矫正服务信息 sidecar：{"port", "pid", "started"}。

    失败仅打印警告，不抛出——矫正流程不应因 sidecar 写入失败而中断。
    """
    try:
        from configmanage import _atomic_write_json

        p = _server_info_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(str(p), {"port": int(port), "pid": os.getpid(), "started": time.time()})
    except Exception as e:  # noqa: BLE001
        print(f"[correctmanage] 写入矫正服务信息 sidecar 失败: {e}")


def _clear_server_info() -> None:
    """清除矫正服务信息 sidecar，仅当记录的 pid 匹配当前进程时删除。

    防止误删由更新实例写入的记录（竞态保护）。
    任何异常静默吞掉。
    """
    try:
        p = _server_info_path()
        if not p.exists():
            return
        with open(p, "r", encoding="utf-8") as f:
            info = json.load(f)
        if isinstance(info, dict) and info.get("pid") == os.getpid():
            p.unlink()
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


# 富文本块的行内标签集合（加粗/斜体/通用 span）
_RICH_INLINE_TAGS = ("span", "strong", "b", "em", "i")


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
      "runs": [...], "align": ..., "note": bool, "attrs": 属性串,
      "inner": 内嵌 HTML, "indent": _rich_parse_indent 形状}；
    - 图片块：{"kind": "img", "src", "alt", "cls"}（块内图片把周围文本
      拆成独立块，延续旧导出行为）。
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
            self.indent = _rich_parse_indent({})
            self.block_seen = False  # 是否已进入过块（孤立 img 不产生块）
            self.skip = 0  # >0 表示处于 script/style 等跳过区域
            self.stack: list[tuple[str, bool]] = []  # (行内标签, 是否标记 span)

        def _flags(self) -> tuple[bool, bool]:
            bold = any(t in ("strong", "b") for t, _ in self.stack)
            italic = any(t in ("em", "i") for t, _ in self.stack)
            return bold, italic

        def _push_text(self, txt: str) -> None:
            if not txt:
                return
            bold, italic = self._flags()
            if self.runs:
                last = self.runs[-1]
                if last["bold"] == bold and last["italic"] == italic:
                    last["text"] += txt
                    return
            self.runs.append({"text": txt, "bold": bold, "italic": italic})

        def _open_block(self, tag: str, attrs) -> None:
            self._flush()
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
            self.indent = _rich_parse_indent(d)
            self.block_seen = True

        def _flush(self) -> None:
            runs = self.runs
            self.runs = []
            inner = "".join(self.inner)
            self.inner = []
            # 去掉块首尾空白（与旧导出的整块 .strip() 等价）
            while runs and not runs[0]["text"].strip():
                runs.pop(0)
            while runs and not runs[-1]["text"].strip():
                runs.pop()
            if runs:
                runs[0]["text"] = runs[0]["text"].lstrip()
                runs[-1]["text"] = runs[-1]["text"].rstrip()
            text = "".join(r["text"] for r in runs)
            if text:
                self.blocks.append(
                    {
                        "kind": self.kind,
                        "tag": self.tag,
                        "text": text,
                        "runs": runs,
                        "align": _rich_align(self.attrs_str),
                        "note": self.note,
                        "attrs": self.attrs_str,
                        "inner": inner,
                        "indent": self.indent,
                    }
                )
            self.kind, self.tag, self.attrs_str = "p", "p", ""
            self.note = False
            self.indent = _rich_parse_indent({})

        def _emit_img(self, attrs) -> None:
            d = dict(attrs)
            self._flush()
            self.blocks.append(
                {
                    "kind": "img",
                    "src": d.get("src") or "",
                    "alt": d.get("alt") or "插图",
                    "cls": d.get("class") or "",
                }
            )

        def handle_starttag(self, tag, attrs) -> None:
            if tag in _SKIP_TAGS:
                self.skip += 1
                return
            if tag in ("p", "div") or (
                len(tag) == 2 and tag[0] == "h" and tag[1] in "123456"
            ):
                self._open_block(tag, attrs)
                return
            if self.skip:
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
                cls = (dict(attrs).get("class") or "").split()
                if "ptoe-note" in cls:
                    self.note = True
                self.stack.append((tag, "ptoe-marker" in cls))
                if "ptoe-marker" not in cls:
                    self.inner.append(self.get_starttag_text() or f"<{tag}>")
                return
            # 其他标签（白名单外，罕见）：不进 runs，仅透传进 inner
            self.inner.append(self.get_starttag_text() or f"<{tag}>")

        def handle_startendtag(self, tag, attrs) -> None:
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
                        if not any(mk for _, mk in seg):
                            self.inner.append(f"</{tag}>")
                        break

        def handle_data(self, data) -> None:
            if self.skip:
                return
            if any(mk for _, mk in self.stack):
                return  # 标记 span 内容整体剥除
            self._push_text(data)
            # inner 重转义（convert_charrefs 已解码实体；保持 HTML 形态供透传）
            self.inner.append(
                str(data).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )

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


def _norm_export_block(block: Any) -> dict:
    """兼容旧元组块与富文本字典块：统一归一为富文本块字典。

    - dict → 原样返回（_html_to_rich_blocks 产物）；
    - ('img', src, alt, cls) → 图片块字典；
    - ('p'|'hN', 文本) → 无格式信息的普通文本块（runs 单条）。
    """
    if isinstance(block, dict):
        return block
    if block[0] == "img":
        return {"kind": "img", "src": block[1], "alt": block[2], "cls": block[3]}
    kind, text = str(block[0]), str(block[1])
    return {
        "kind": kind,
        "tag": kind,
        "text": text,
        "runs": [{"text": text, "bold": False, "italic": False}],
        "align": "",
        "note": False,
        "attrs": "",
        "inner": "",
        "indent": {},
    }


def _docx_escape(text: str) -> str:
    """XML 文本转义（& < > "）。"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


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

        kind = block["kind"]
        runs = block.get("runs") or [
            {"text": block.get("text", ""), "bold": False, "italic": False}
        ]
        is_heading = kind.startswith("h") and len(kind) == 2 and kind[1].isdigit()
        lvl = int(kind[1]) if is_heading else 0
        align = block.get("align") or ""
        indent = block.get("indent") or {}
        note = bool(block.get("note"))

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
        if is_heading and not jc:
            jc = "center"  # 与 EPUB 一致：标题默认居中
        if jc:
            ppr_parts.append(f'<w:jc w:val="{jc}"/>')
        if is_heading:
            ppr_parts.append(f'<w:outlineLvl w:val="{lvl - 1}"/>')

        sz = _DOCX_HEADING_SZ.get(lvl, 24) if is_heading else None
        run_xml_parts: list[str] = []
        for r in runs:
            rpr = ""
            if is_heading or r.get("bold"):
                rpr += "<w:b/>"
            if r.get("italic") or note:
                rpr += "<w:i/>"
            if note:
                rpr += '<w:color w:val="808080"/>'
            if sz:
                rpr += f'<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/>'
            t = _docx_escape(r.get("text", "")).replace(
                "\n", '</w:t><w:br/><w:t xml:space="preserve">'
            )
            run_xml_parts.append(
                f"<w:r><w:rPr>{rpr}</w:rPr>"
                f'<w:t xml:space="preserve">{t}</w:t></w:r>'
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

    parts: list[str] = []
    for raw in blocks:
        b = _norm_export_block(raw)
        if b["kind"] == "img":
            parts.append(f"![{b.get('alt') or ''}]({b.get('src') or ''})")
            continue
        attrs = b.get("attrs") or ""
        if "class=" in attrs or "data-" in attrs:
            # 带属性块：原样透传（与前端 htmlToMd 规则一致）
            tag = b.get("tag") or b["kind"]
            parts.append(
                f"<{tag}{(' ' + attrs) if attrs else ''}>{b.get('inner') or ''}</{tag}>"
            )
            continue
        kind = b["kind"]
        if kind.startswith("h") and len(kind) == 2 and kind[1].isdigit():
            parts.append("#" * int(kind[1]) + " " + _inline(b["runs"]))
        else:
            parts.append(_inline(b["runs"]))
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

    def _config(self) -> None:
        """字体/界面配置：POST 写入 config.json fonts + citationItalicEnabled。

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
            if changed:
                update_config("fonts", cfg.get("fonts"))
                update_config(
                    "citationItalicEnabled", cfg.get("citationItalicEnabled")
                )
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
        if path == "/api/heartbeat":
            self._touch_heartbeat()
            self._send(204, b"", "text/plain")
            return
        if path == "/api/ping":
            # 轻量存活探测：仅返回 ok，不修改任何状态。供外部（如 GUI）
            # 发现并恢复已存活的矫正界面。
            self._send(200, self._json({"ok": True}), "application/json; charset=utf-8")
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
        if path == "/api/config":
            # 字体/界面配置：GET 读取 config.json fonts + citationItalicEnabled
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
                            "body": fonts.get("body", "serif"),
                            "heading": fonts.get("heading", "sans-serif"),
                            "note": fonts.get("note", "serif"),
                            "citation": fonts.get("citation", "cursive"),
                        },
                        "citationItalicEnabled": bool(
                            cfg.get("citationItalicEnabled", False)
                        ),
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
            # 重命名历史记录条目：更新 display_name，应用到同一 PDF 组的所有版本文件
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                vid = str(body.get("id") or "").strip()
                new_name = str(body.get("newName") or "").strip()
                if not vid:
                    self._send(
                        400,
                        self._json({"ok": False, "error": "缺少版本 ID"}),
                        "application/json; charset=utf-8",
                    )
                    return
                if not new_name:
                    self._send(
                        400,
                        self._json({"ok": False, "error": "新名称不能为空"}),
                        "application/json; charset=utf-8",
                    )
                    return
                # 验证：拒绝包含路径分隔符的名称
                if "\\" in new_name or "/" in new_name or ":" in new_name:
                    self._send(
                        400,
                        self._json({"ok": False, "error": "名称含非法字符（含 \\ / :）"}),
                        "application/json; charset=utf-8",
                    )
                    return
                # 长度限制（最多 100 个字符，与文件名惯例一致）
                if len(new_name) > 100:
                    self._send(
                        400,
                        self._json({"ok": False, "error": "名称过长（最多 100 字符）"}),
                        "application/json; charset=utf-8",
                    )
                    return
                prefix = _version_prefix(vid)  # 由版本文件名 stem 算出的 book 前缀
                if not prefix:
                    self._send(
                        404,
                        self._json({"ok": False, "error": "无法定位所属 PDF 组"}),
                        "application/json; charset=utf-8",
                    )
                    return
                d = _history_dir()
                # 遍历该 PDF 组的所有版本文件（前缀匹配），写入 display_name
                # 注意用 Path.glob（返回 Path 对象）；glob.glob 返回 str 无 .read_text
                matched = False
                for fp in d.glob(f"{prefix}_*.json"):
                    try:
                        data = json.loads(fp.read_text(encoding="utf-8"))
                        # 保留原有字段，仅更新/写入 display_name
                        data["display_name"] = new_name
                        # 使用原子写入：先写临时文件再 os.replace
                        import tempfile, os as _os
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
                        matched = True
                    except Exception:
                        continue
                # 更新内存中 _HISTORY_INDEX 以立即反映变更
                # 重新计算签名使下次 _history_entries 重读
                fps = sorted(fp for fp in d.glob("*.json") if not fp.name.endswith(".images.json"))
                sig = "|".join(
                    f"{fp.name}:{fp.stat().st_mtime_ns}:{fp.stat().st_size}" for fp in fps
                )
                # 若重命名的是当前编辑中的书（前缀一致），同步更新会话内
                # state.display_name，使随后的保存/暂存/完成沿用它而非回退到原名
                st = self.server.state
                if st.get("history_prefix") == prefix:
                    st["display_name"] = new_name
                # 只要取第一个版本的显示名作为组名变更的凭证
                first_items = _history_entries(prefix)
                first_display = first_items[0].get("display_name", first_items[0].get("name", "")) if first_items else new_name
                self._send(
                    200, self._json({"ok": True, "display_name": first_display}), "application/json; charset=utf-8"
                )
                return
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
        if path == "/api/format_rules/apply":
            # 应用格式规则到指定页 HTML（任务 B：服务端应用引擎）
            # body: {page:int, html:str, rule_id?:str, all?:bool, sel_start?:int, sel_end?:int}
            # 返回: {ok:bool, html:str} 或 {ok:false, error:str}
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

                # 校验参数
                page = body.get("page")
                html_text = body.get("html")
                rule_id = body.get("rule_id")
                all_rules = bool(body.get("all"))
                sel_start = body.get("sel_start")
                sel_end = body.get("sel_end")

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
        if path == "/api/proofread_settings":
            self._proofread_settings()
            return
        if path == "/api/shortcuts":
            self._shortcuts()
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
                # 快速切换支持（2026-08 修复）：若端口上运行的是其他模型，先停掉
                # 本进程管理的旧实例再启动新模型；仍被占用（外部进程）则给出明确提示，
                # 避免直接调 runserver 撞端口报出难懂的占用错误。
                from llamamanage import _probe_server as _probe_pre, stopserver

                pre_name = str(model_info.get("name") or model_key)
                if _probe_pre(pre_name) == "mismatch":
                    stopserver()  # 仅能停掉本进程启动的实例
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
                                }
                            ),
                            "application/json; charset=utf-8",
                        )
                        return
                # 矫正/重识别为单请求顺序处理（用户逐页点击，一次一个请求），并发
                # 槽位取 1 即可——llama-server 的 KV cache ≈ n_ctx × parallel，默认
                # 取 config 的 parallel(6)×max_tokens(8192) 会把 KV 预分配撑到数 GB，
                # 显著超过直接启动（--parallel 1）的显存占用（2026-09-01 修复）。
                running = bool(runserver(model_key, with_mmproj=has_mmproj, parallel=1))
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
                            ok = llamamanage.runserver(model_key, with_mmproj=True)
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
                # 2026-08-23/28：模型可能把图片页脚的页码一并识别进来，先剥掉末尾页码
                # （第 N 页 / 字符+数字：页123·P123·No.123 / 括号包裹 / 独立成行裸数字），
                # 避免被当成正文差异标注；仅清理返回文字最末尾的页码，正文不受影响。
                # 须在英文标点归一（_full_punct）之前执行，否则 "No.123" 的 "." 被转成
                # "。" 后无法匹配页码样式。
                new_text = _strip_trailing_page_number(new_text)
                # 2026-08-09：再将英文标点归一为中文标点，避免半角/全角差异被当成纠错项
                new_text = _full_punct(new_text)
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
                blocks: list[dict] = []
                for item in items:
                    # 应用加粗注释标签转换（注　　释：）
                    html_text = transform_note_labels(str(item.get("html") or ""))
                    blocks.extend(_html_to_rich_blocks(html_text))
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
                out = Path(out_path)
                out.parent.mkdir(parents=True, exist_ok=True)
                if fmt == "txt":

                    def _txt_line(b: dict) -> str:
                        # 图片块以 [图片] 占位符表示；首行缩进（data-ind=first）
                        # 以全角空格前缀近似（纯文本唯一能承载的版式信息）
                        if b["kind"] == "img":
                            return "[图片]"
                        line = "".join(r["text"] for r in b["runs"])
                        ind = b.get("indent") or {}
                        if ind.get("ind") == "first":
                            n = int(ind.get("indv") or 2)
                            line = "\u3000" * max(1, min(8, n)) + line
                        return line

                    text = "\n\n".join(_txt_line(b) for b in blocks) + "\n"
                    out.write_text(text, encoding=_TXT_ENCODING)
                elif fmt == "md":
                    # Markdown 导出：与前端 htmlToMd 规则一致（见 _build_md）
                    _build_md(blocks, str(out))
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
                        title = st.get("display_name") or (st.get("history_name") or "矫正导出")
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
        # 预渲染页数上限：可经 config.json 顶层键 prerender_max_pages 覆盖
        "prerender_max_pages": _resolve_prerender_max(),
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
    # 记录服务信息 sidecar，供 GUI 配置中心发现并恢复已存活的矫正界面
    _write_server_info(server.server_address[1])
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
        # 清除服务信息 sidecar（仅当前进程 pid 匹配时删除）
        _clear_server_info()
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
/* 预览图加载提示胶囊（底部居中）：加载中显示，完成后闪现「加载完成」 */
#loadHint{position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(8px);z-index:70;background:#334155;color:#fff;font-size:13px;padding:6px 14px;border-radius:16px;opacity:0;pointer-events:none;transition:opacity .18s ease,transform .18s ease;box-shadow:0 2px 10px rgba(0,0,0,.25);}
#loadHint.show{opacity:1;transform:translateX(-50%) translateY(0);}
#loadHint.done{background:#166534;}
/* 行级微光占位：预览图未就绪时填充图片区域，替代纯白 */
.ptoe-img-loading{background:linear-gradient(100deg,#eceff3 40%,#f7f9fb 50%,#eceff3 60%);background-size:200% 100%;animation:ptoeShimmer 1.1s linear infinite;}
@keyframes ptoeShimmer{to{background-position:-200% 0;}}
button.loading::after{content:'';display:inline-block;width:11px;height:11px;margin-left:8px;border:2px solid rgba(255,255,255,.45);border-top-color:#fff;border-radius:50%;animation:ptoe-spin .8s linear infinite;vertical-align:-2px;}
@keyframes ptoe-spin{to{transform:rotate(360deg);}}
/* U3：hintbar 可折叠（✕ 关闭，localStorage 记忆） */
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
.page-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;align-items:stretch;background:#fff;border:1px solid var(--border);border-radius:8px;padding:12px;}
.page-head{grid-column:1 / -1;font-size:12px;color:#5a6b7c;border-bottom:1px dashed var(--border);padding-bottom:6px;}
.img-panel{position:relative;min-width:0;overflow:hidden;background:#fff;border:1px solid var(--border);border-radius:4px;padding:4px;}
.img-panel img{width:100%;height:auto;display:block;background:#fff;cursor:zoom-in;}
.badge{position:absolute;top:8px;left:8px;background:rgba(0,0,0,.55);color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;pointer-events:none;}
.editable{height:0;min-height:100%;overflow-y:auto;padding:10px 14px;border:1px solid var(--border);border-radius:4px;line-height:1.7;font-size:var(--editor-font-size);outline:none;}
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
#popup{position:fixed;z-index:60;display:none;flex-wrap:wrap;gap:4px;max-width:360px;padding:6px 8px;background:#fff;border:1px solid var(--border);border-radius:8px;box-shadow:0 4px 18px rgba(0,0,0,.22);}
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
.pop-rule-wrap{position:relative;display:flex;flex-wrap:nowrap;gap:4px;align-items:center;min-width:224px;}
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
#indentModalBg{position:fixed;inset:0;z-index:66;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;}
.indent-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;margin:10px 0;font-size:13px;}
.indent-grid label{display:flex;align-items:center;gap:6px;white-space:nowrap;}
.indent-grid input[type="number"]{width:72px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;}
.indent-grid select{padding:4px 6px;border:1px solid var(--border);border-radius:4px;font:inherit;background:#fff;}
.indent-preview{border:1px dashed var(--border);border-radius:6px;padding:10px 12px;margin:8px 0 12px;min-height:56px;overflow:auto;background:#fafbfc;}
.indent-preview p{margin:0;}
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
  /* 窄屏单列布局：回退自然高度（图上文下纵向堆叠），文字窗不裁剪 */
  .page-row{grid-template-columns:1fr;}
  .img-panel img{width:100%;}
  .editable{font-size:calc(var(--editor-font-size) + 2px);height:auto;min-height:160px;overflow-y:visible;}
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
    <button type="button" class="ic-btn" data-op="strip_ws" onmousedown="event.preventDefault()" title="去空（去除段落内全部空白，保留换行）" aria-label="去空">去空</button>
    <button type="button" class="ic-btn" id="colorBtn" onmousedown="event.preventDefault()" title="文本颜色" aria-label="文本颜色">色</button>
    <button type="button" class="ic-btn" id="formatBrushBtn" onmousedown="event.preventDefault()" title="格式刷" aria-label="格式刷">刷</button>
    <button type="button" class="ic-btn" id="formatRulesBtn" onmousedown="event.preventDefault()" title="格式规则：对选中文字一键应用自定义规则（可多条叠加 / 条件分支；Ctrl+Shift+Q）" aria-label="格式规则">规</button>
  </div>
<div class="tb-group" role="group" aria-label="对齐">
     <button type="button" class="ic-btn" data-op="align_left" onmousedown="event.preventDefault()" title="居左" aria-label="居左">左</button>
     <button type="button" class="ic-btn" data-op="align_center" onmousedown="event.preventDefault()" title="居中" aria-label="居中">中</button>
     <button type="button" class="ic-btn" data-op="align_right" onmousedown="event.preventDefault()" title="居右" aria-label="居右">右</button>
     <button type="button" class="ic-btn" data-op="flush" onmousedown="event.preventDefault()" title="顶格" aria-label="顶格">顶格</button>
     <button type="button" class="ic-btn" data-op="indent" onmousedown="event.preventDefault()" title="缩进" aria-label="缩进">缩进</button>
     <button type="button" class="ic-btn" id="indentDlgBtn" onmousedown="event.preventDefault()" title="段落设置：左/右缩进、首行/悬挂缩进、段前段后与行距（导出 EPUB 生效）" aria-label="段落设置">¶</button>
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
    <select id="imgModeSel" hidden title="插入图片的显示模式：全画幅=占满文字宽度，局部=按原尺寸居中，行内=嵌在文字中间（50% 宽度）">
      <option value="full">全画幅</option>
      <option value="fit">局部</option>
      <option value="inline">行内</option>
    </select>
    <button type="button" id="imgExternalBtn" onmousedown="event.preventDefault()" title="从本地文件选择图片，插入到文字光标处">外部</button>
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
      <button type="button" class="ctx-item" data-ctx-marker="join">段落标记</button>
      <button type="button" class="ctx-item" data-ctx-marker="page">换页标记</button>
      <button type="button" class="ctx-item" data-ctx-marker="full">全文标记</button>
      <button type="button" class="ctx-item" data-ctx-marker="note">注释标记</button>
    </div>
  </div>
  <div class="ctx-item ctx-sub" data-ctx="export">导出 <span class="ctx-arrow">▸</span>
    <div class="ctx-submenu" id="ctxExportSub">
      <button type="button" class="ctx-item" data-ctx-export="txt">txt格式</button>
      <button type="button" class="ctx-item" data-ctx-export="docx">docx格式</button>
      <button type="button" class="ctx-item" data-ctx-export="md">md格式</button>
      <button type="button" class="ctx-item" data-ctx-export="epub">epub格式</button>
    </div>
  </div>
  <div class="ctx-item ctx-sub" data-ctx="rules">添加规则 <span class="ctx-arrow">▸</span>
    <div class="ctx-submenu" id="ctxRulesSub"></div>
  </div>
  <button type="button" class="ctx-item" data-ctx="clearpage">清空</button>
  <button type="button" class="ctx-item" data-ctx="fmtall">格式化</button>
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
      <button type="button" id="prLlmSwitch" style="flex:1;" title="用当前所选模型重启服务：自动停止旧模型并加载新模型（无需先手动停止）">切换模型</button>
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
  <p class="export-desc">把全部页面的文字（含未保存的修改）导出为文件；点击下方按钮后弹出窗口选择保存位置。DOCX 中标题自动加粗加大并居中，带章节大纲；对齐、缩进等段落格式同步导出；Markdown 与界面规则保持一致。</p>
  <div class="export-actions">
    <button type="button" id="exportDocxBtn" title="导出为 Word 文档（.docx）">导出为 DOCX</button>
    <button type="button" id="exportMdBtn" title="导出为 Markdown 文件（.md）">导出为 MD</button>
    <button type="button" id="exportTxtBtn" class="primary" title="导出为纯文本文件（.txt）">导出为 TXT</button>
  </div>
</div></div>
<div id="indentModalBg"><div class="modal search-modal">
  <div class="search-head"><h3>段落设置</h3><button type="button" id="indCloseBtn" class="x-btn" title="关闭段落设置" aria-label="关闭段落设置">✕</button></div>
  <p style="font-size:12px;color:#5a6b7c;margin:4px 0;">作用于当前选中/光标所在段落（可跨多段）。缩进单位为字符，间距单位为行；设置随内容保存并在导出 EPUB 时生效。</p>
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
    <button type="button" id="indClearBtn" title="清除所选段落的全部缩进与间距设置">清除格式</button>
    <button type="button" id="indOkBtn" class="primary" title="把设置应用到所选段落">确定</button>
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
<script src="/ui/app.js"></script>
</body>
</html>
"""
