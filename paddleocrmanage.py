# -*- coding: utf-8 -*-
"""PaddleOCR 识别引擎适配层（2026-08）。

功能范围：仅用于 PDF→EPUB 流程的 OCR 阶段（mian.py epub/resume，--engine paddle）。
文本矫正界面（correctmanage /api/reocr、深度校对）依旧走 llama-server/vLLM 大模型，
不经过本模块——保证矫正能力不受影响。

设计要点：
- 惰性导入 paddleocr/paddle：未安装时其余功能（转换、矫正）完全不受影响。
- GPU 自动识别：paddle.is_compiled_with_cuda() + device_count>0 → device="gpu:0"，
  否则回退 "cpu"（mkldnn 加速）。任何探测异常一律按 CPU 处理，绝不阻断。
- 单例预测器 + threading.Lock：PaddleOCR 实例初始化开销大（首次加载模型），
  进程内只建一次；官方指引明确不要跨线程共享实例做并发 predict，因此
  batch_infer 采用顺序逐页推理（GPU 本身串行；CPU 端 mkldnn 已多线程）。
- 文本行合并成段落（2026-09-28，配置键 config.json 的 paddle_merge_lines，默认开）：
  PaddleOCR 逐「文本行」输出，同一段落被版面切成多行，EPUB 里就变成一堆碎行。
  本模块按 rec_boxes 坐标把同一段落的相邻行合并：抽取 → 行归组排序 → 逐条规则
  判定合并 → 段内拼接（连接符按中英字符类型自适应）。段落之间仍用换行分隔。
  开关关闭、结果抽不到行、或合并过程抛任何异常时，逐字节退回旧的逐行行为
  （"\n".join(rec_texts)），绝不因该功能让 OCR 失败。
- batch_infer 返回形状与 llamamanage.batch_infer 完全一致：
  [{page, result, error}, ...]（按页码排序），失败不抛异常、逐页捕获，
  使 mian 的结构化/断点续传/进度文件逻辑零改动复用。
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "available",
    "detect_gpu",
    "get_predictor",
    "reset_predictor",
    "batch_infer",
    "extract_lines_from_res",
]


def _setup_model_cache_dir() -> None:
    """PaddlePaddle/PaddleX 模型默认下载位置 → 程序路径下（frozen 时为 exe 目录）。

    paddlex 在 import 时读取 PADDLE_PDX_CACHE_HOME 决定 CACHE_DIR（模型落在
    CACHE_DIR/official_models），因此必须在首次 import paddleocr/paddlex 之前
    设置本环境变量（本模块顶层调用，天然早于任何惰性 paddleocr 导入）。
    用户已显式设置该环境变量时尊重用户配置（即装即用）。
    """
    if os.environ.get("PADDLE_PDX_CACHE_HOME"):
        return
    try:
        from pdfmanage import app_base_dir

        base = app_base_dir()
    except Exception:
        base = Path(__file__).resolve().parent
    cache = base / "models" / "paddlex"
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        cache = base
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(cache)


_setup_model_cache_dir()

_LOCK = threading.Lock()
_PREDICTOR: Any = None
# 可注入的预测器工厂（测试用 monkeypatch 替换；None = 默认真实工厂）
_predictor_factory: Callable[[], Any] | None = None


def available() -> bool:
    """paddleocr 是否可导入（惰性，不触发模型下载）。"""
    try:
        import paddleocr  # noqa: F401

        return True
    except Exception:
        return False


def detect_gpu() -> bool:
    """检测当前 paddle 是否支持并可见 CUDA 设备。任何异常 → False（回退 CPU）。"""
    try:
        import paddle

        if not paddle.is_compiled_with_cuda():
            return False
        return int(paddle.device.cuda.device_count()) > 0
    except Exception:
        return False


def _default_factory() -> Any:
    """构建 PaddleOCR 预测器：关闭方向/矫正等非必需分支提速；GPU 自动识别。"""
    from paddleocr import PaddleOCR

    use_gpu = detect_gpu()
    # 国内网络加速模型下载（BOS 源）；已设置则尊重用户环境
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")
    return PaddleOCR(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        lang="ch",  # PaddleOCR 3.x 简体中文代码为 "ch"（"zh" 不在模型名映射表 → No models available）
        device="gpu:0" if use_gpu else "cpu",
        cpu_threads=10,
        enable_mkldnn=True,
    )


def get_predictor() -> Any:
    """进程内单例预测器（惰性创建，锁保护）。"""
    global _PREDICTOR
    if _PREDICTOR is not None:
        return _PREDICTOR
    with _LOCK:
        if _PREDICTOR is None:
            factory = _predictor_factory or _default_factory
            _PREDICTOR = factory()
        return _PREDICTOR


def reset_predictor() -> None:
    """丢弃已缓存预测器（测试/配置变更后使用）。"""
    global _PREDICTOR
    with _LOCK:
        _PREDICTOR = None


_MISSING_MSG = "未安装 paddleocr：请先安装（pip install paddleocr paddlepaddle）"

# ---------------------------------------------------------------------------
# 文本行合并成段落（paddle_merge_lines 开关，默认开）
# ---------------------------------------------------------------------------

# 中位行高兜底常量：坐标全缺/异常时按 20px 估（100dpi 下正文行高量级）。
_FALLBACK_LINE_HEIGHT = 20.0
# 布尔型配置值的真值表（大小写不敏感）：与 mian.py/guimanage.py 同表，
# 保持三个消费方对 paddle_merge_lines 的解释完全一致。
_BOOL_TRUE_WORDS = ("true", "1", "on", "yes")
_BOOL_FALSE_WORDS = ("false", "0", "off", "no")

# 句末标点：当前行 strip 后的末字符命中 → 判定为句子/段落结束，不与下一行合并
_SENTENCE_END = frozenset("。！？；：.!?;:)）】」』》〉\"'”")
# 新块起始字符：下一行 strip 后的首字符命中 → 判定为新段落/新条目（如编号、引用），
# 不与上一行合并。含数字、ASCII 字母、开括号/引号、中文逗号句读、第（第一章…）
_NEW_BLOCK_STARTERS = frozenset(
    "0123456789"
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "([{（【〔\"'“‘"
    "，、。：；！？"
    "第"
)
# CJK 汉字 + 中文标点（含全角/中文标点区）：用于判定拼接时是否需要空格
_CJK_CHARS_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef]")
# ASCII 字母/数字：任一侧为它时，拼接处补一个半角空格
_ASCII_ALNUM_RE = re.compile(r"[A-Za-z0-9]")


def _center_x(box) -> float:
    """框的水平中心。"""
    return (box[0] + box[2]) / 2.0


def _center_y(box) -> float:
    """框的垂直中心（行归组的锚点）。"""
    return (box[1] + box[3]) / 2.0


def _median(values: list[float], default: float = _FALLBACK_LINE_HEIGHT) -> float:
    """中位数；空列表/非正值 → default。"""
    nums = sorted(v for v in values if isinstance(v, (int, float)) and v > 0)
    if not nums:
        return default
    return float(nums[len(nums) // 2])


def _flatten_coords(obj) -> list[float] | None:
    """把一个坐标对象摊平为一串 float（保序）。

    鸭子类型兼容 numpy（模块内不 import numpy）：有 tolist() 就先转成嵌套
    list/tuple，再递归展开。任一元素无法转 float → None（调用方置 box=None，
    绝不因坐标异常丢掉那一行的文本）。
    """
    out: list[float] = []
    stack: list[Any] = [obj]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, (list, tuple)):
            stack = list(cur) + stack
            continue
        if isinstance(cur, (str, bytes)):
            return None
        try:
            out.append(float(cur))
        except (TypeError, ValueError):
            return None
    return out


def _box_from_raw(raw) -> tuple[float, float, float, float] | None:
    """rec_boxes 的一个原始坐标 → (x1, y1, x2, y2)；形状不认识 → None。

    支持两种形状：4 元素一维 xyxy（paddleocr 3.x 默认），以及 8 元素
    4×2 四点多边形（取外接矩形；2 行×4 点的行式写法也兼容）。
    """
    if raw is None:
        return None
    tolist = getattr(raw, "tolist", None)
    if callable(tolist):  # numpy 数组 → 嵌套 list（不 import numpy）
        try:
            raw = tolist()
        except Exception:
            return None
    nums = _flatten_coords(raw)
    if not nums:
        return None
    if len(nums) == 4:
        x1, y1, x2, y2 = nums
    elif len(nums) == 8:
        # 4 点的多边形：x/y 交替存放（折行成 2×4 时也仍是交替），求外接矩形
        xs, ys = nums[0::2], nums[1::2]
        x1, y2 = min(xs), max(ys)
        x2, y1 = max(xs), min(ys)
    else:
        return None
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    if x2 - x1 <= 0 or y2 - y1 <= 0:
        return None
    return (x1, y1, x2, y2)


def extract_lines_from_res(res) -> list[dict[str, Any]]:
    """从一条 PaddleOCR 结果抽成 [{text, score, box}]（box 为 (x1,y1,x2,y2) 或 None）。

    两种结果形状都兼容（2026-08 踩过的坑）：3.x predict() 返回顶层 dict，
    旧版/测试用 .res 包装。rec_scores 缺失 → 1.0；rec_boxes 缺失或形状不认识
    → 该行 box=None 但文本照常保留（strip 后为空的行才丢弃）。
    """
    res_data = getattr(res, "res", None)
    if res_data is None and isinstance(res, dict):
        res_data = res
    if not isinstance(res_data, dict):
        return []
    texts = res_data.get("rec_texts") or []
    scores = res_data.get("rec_scores") or []
    boxes = res_data.get("rec_boxes")
    if boxes is None:
        boxes = res_data.get("dt_polys") or res_data.get("rec_polys") or []
    lines: list[dict[str, Any]] = []
    for i, raw_text in enumerate(texts):
        try:
            text = str(raw_text if raw_text is not None else "").strip()
        except Exception:
            continue
        if not text:
            continue  # 空白行直接丢弃
        score = 1.0
        try:
            if scores is not None and i < len(scores):
                score = float(scores[i])
        except (TypeError, ValueError):
            score = 1.0
        box = None
        try:
            if boxes is not None and i < len(boxes):
                box = _box_from_raw(boxes[i])
        except Exception:
            box = None  # 坐标异常绝不能让这一行丢文本
        lines.append({"text": text, "score": score, "box": box})
    return lines


def _extract_page_lines(outs) -> list[dict[str, Any]] | None:
    """一页 predict() 的全部结果 → 行列表；任一 res 抽不出行 → None（退回旧行为）。"""
    lines: list[dict[str, Any]] = []
    for res in outs or []:
        got = extract_lines_from_res(res)
        if not got:
            return None
        lines.extend(got)
    return lines or None


def _plain_texts(outs) -> list[str]:
    """旧的纯文本抽取：只取 rec_texts（忽略置信度与坐标），逐行拼接。"""
    texts: list[str] = []
    for res in outs or []:
        # PaddleOCR 3.x predict() 返回顶层 dict，rec_texts 直接是结果键；
        # 兼容旧版/测试用的 .res 包装形状
        res_data = getattr(res, "res", None)
        if res_data is None and isinstance(res, dict):
            res_data = res
        if isinstance(res_data, dict):
            texts.extend(res_data.get("rec_texts") or [])
    return texts


def _split_row_cols(row: list[dict[str, Any]], median_h: float) -> list[list[dict[str, Any]]]:
    """行内分栏切分：相邻两框水平间隙 > 2.0 × 中位行高 → 在此切成独立视觉行。

    明显是左右两栏/表格列，避免左右两栏被粘成同一行（判定用「相邻」而非整行
    最左最右，因此一行里三列也能逐个切开）。

    已知边界（诚实说明）：真正的双栏书「先读完左栏再读右栏」的整页阅读顺序
    不在本函数职责内（属完整版面分析，另说）。这里只保证同一视觉行内的左右
    分栏不被粘成一行，整页顺序仍按行自上而下、同一行自左而右。
    """
    parts: list[list[dict[str, Any]]] = [[row[0]]]
    for cur, nxt in zip(row, row[1:]):
        if nxt["box"][0] - cur["box"][2] > 2.0 * median_h:
            parts.append([nxt])
        else:
            parts[-1].append(nxt)
    return parts


def _group_rows(lines: list[dict[str, Any]], median_h: float) -> list[list[dict[str, Any]]]:
    """把文本行聚成视觉行：行归组（中心 Y）→ 带内微调 → 行内分栏切分。

    对外表现与「Y 从上到下、X 从左到右」一致。无坐标的行无法参与几何聚类，
    各自成行并按原顺序置于末尾（真实 PaddleOCR 不会产生这种混合结果，
    仅作畸形结果的兜底）。
    """
    with_box = [ln for ln in lines if ln.get("box")]
    without_box = [ln for ln in lines if not ln.get("box")]
    # 1) 粗聚类：按中心 Y 升序遍历，带容差 = 0.6 × 中位行高
    bands: list[list[dict[str, Any]]] = []
    for ln in sorted(with_box, key=lambda l: _center_y(l["box"])):
        if bands and abs(_center_y(ln["box"]) - _center_y(bands[-1][-1]["box"])) <= 0.6 * median_h:
            bands[-1].append(ln)
        else:
            bands.append([ln])
    # 2) 带内微调：更小容差（0.35 × 中位行高）再聚一次，拆开被粗容差粘住的相邻行
    clusters: list[list[dict[str, Any]]] = []
    for band in bands:
        cur: list[dict[str, Any]] = []
        for ln in band:
            if cur and abs(_center_y(ln["box"]) - _center_y(cur[-1]["box"])) > 0.35 * median_h:
                clusters.append(cur)
                cur = [ln]
            else:
                cur.append(ln)
        if cur:
            clusters.append(cur)
    # 3) 行内按左边界 X 升序，再按水平大间隙切成独立视觉行
    rows: list[list[dict[str, Any]]] = []
    for cluster in clusters:
        cluster.sort(key=lambda l: l["box"][0])
        rows.extend(_split_row_cols(cluster, median_h))
    for ln in without_box:
        rows.append([ln])
    return rows


def _join_fragments(prev: str, nxt: str) -> str:
    """拼接同段的相邻两段文本，连接符按字符类型自适应。

    两侧 strip 后：都是 CJK 汉字/中文标点 → 直接连接不加空格（中文排版不插空格）；
    任一侧是 ASCII 字母/数字 → 中间补一个半角空格（中英混排需要分隔）。
    其它字符（纯符号等）按直接连接处理。
    """
    a = (prev or "").rstrip()
    b = (nxt or "").lstrip()
    if not a:
        return b
    if not b:
        return a
    tail, head = a[-1], b[0]
    if _CJK_CHARS_RE.match(tail) and _CJK_CHARS_RE.match(head):
        return a + b  # 汉字/中文标点相接：直接连接
    if _ASCII_ALNUM_RE.match(tail) or _ASCII_ALNUM_RE.match(head):
        return a + " " + b  # 中英相接：补一个半角空格
    return a + b


def _should_merge(cur: dict[str, Any], nxt: dict[str, Any], median_h: float) -> bool:
    """相邻两个视觉行是否属于同一段落：四条规则全部满足才合并。"""
    a, b = cur.get("box"), nxt.get("box")
    if a is None or b is None:
        # 坐标缺失：几何规则无法验证，视为通过（不因坐标缺失而放弃合并）
        pass
    else:
        # 水平间距：gap 为负（行间压叠）也允许
        if (b[1] - a[3]) >= 1.5 * median_h:
            return False
        # 对齐：左边界或水平中心对齐其一即可
        if abs(b[0] - a[0]) > 0.5 * median_h and abs(_center_x(b) - _center_x(a)) > 0.5 * median_h:
            return False
        # 水平重叠：交集宽 / min(两者宽) >= 0.3
        inter = min(a[2], b[2]) - max(a[0], b[0])
        denom = min(a[2] - a[0], b[2] - b[0])
        if denom > 0 and (inter / denom) < 0.3:
            return False
    ta = (cur.get("text") or "").rstrip()
    tb = (nxt.get("text") or "").lstrip()
    if not ta or not tb:
        return False
    if ta[-1] in _SENTENCE_END:  # 上一行以句末标点结束
        return False
    if tb[0] in _NEW_BLOCK_STARTERS:  # 下一行是新块起始
        return False
    return True


def _lines_to_paragraphs(lines: list[dict[str, Any]]) -> list[str]:
    """一页的文本行 → 段落文本列表（每个元素是一整个段落的合并结果）。"""
    if not lines:
        return []
    if not any(ln.get("box") for ln in lines):
        # 全页无坐标：没有任何几何信息可用 → 每行独立成段（等价旧的逐行行为）
        return [str(ln.get("text") or "") for ln in lines]
    heights: list[float] = []
    for ln in lines:
        b = ln.get("box")
        if b:
            heights.append(b[3] - b[1])
    median_h = _median(heights)
    rows = _group_rows(lines, median_h)
    paragraphs: list[str] = []
    acc = ""
    prev: dict[str, Any] | None = None
    for row in rows:
        row_text = str(row[0].get("text") or "")
        for ln in row[1:]:  # 同一视觉行内的多个框（靠得很近）也按同一规则拼接
            row_text = _join_fragments(row_text, str(ln.get("text") or ""))
        if prev is not None and acc and _should_merge(prev, row[0], median_h):
            acc = _join_fragments(acc, row_text)
        else:
            if acc:
                paragraphs.append(acc)
            acc = row_text
        prev = row[0]
    if acc:
        paragraphs.append(acc)
    return paragraphs


def _read_merge_lines_setting() -> bool:
    """读取 config.json 的 paddle_merge_lines（每批只读一次）。

    惰性导入 configmanage（模块顶层不 import，避免循环依赖）；走
    get_config(show_dialogs=False) 免 tkinter 弹窗；键缺失/非法值/读取异常
    一律回退 True（headless、损坏配置都不能让 OCR 挂掉）。
    """
    try:
        from configmanage import get_config

        cfg = get_config(show_dialogs=False)
    except Exception:
        return True
    if not isinstance(cfg, dict):
        return True
    raw = cfg.get("paddle_merge_lines", True)
    if isinstance(raw, bool):
        return raw
    v = str(raw).strip().lower()
    if v in _BOOL_TRUE_WORDS:
        return True
    if v in _BOOL_FALSE_WORDS:
        return False
    return True


def _page_text(outs, merge_lines: bool) -> str:
    """一页 predict() 输出 → 最终文本（merge_lines 开时走「文本行合并成段落」）。"""
    fallback = "\n".join(_plain_texts(outs))  # 旧的逐行行为，作为兜底基准
    if not merge_lines:
        return fallback
    try:
        lines = _extract_page_lines(outs)
        if not lines:
            return fallback
        return "\n".join(_lines_to_paragraphs(lines))
    except Exception:
        # 合并逻辑的任何异常都不该让 OCR 失败 → 退回逐行旧行为
        return fallback


def batch_infer(
    images,
    prompts=None,
    model_key: str = "",
    max_workers=None,
    thinking: bool = False,
    timeout: int = 600,
    on_progress: Callable[[int, int], None] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    *args,
    **kwargs,
) -> list[dict[str, Any]]:
    """对整批页面图片做 PaddleOCR 识别（顺序逐页）。

    参数与返回形状对齐 llamamanage.batch_infer：
    - images: [{page, path(或 get_base64/get_path), ...}] 页面项列表；
      兼容 dict 项（取 item["path"]）或纯路径字符串。
    - prompts/model_key/max_workers/thinking：为兼容调用方签名而保留，
      PaddleOCR 不需要提示词，忽略。
    - on_progress(done, total) 每页完成后回调；on_result(res_dict) 收到
      完整结果字典（含 img/page/result/error），供断点续传即时落盘。
    - 返回 [{'img': 图片路径, 'page': 页码, 'result': 文本|None,
      'error': 错误|None}, ...]，按页码升序；未安装 paddleocr 时每页
      返回中文错误，不抛异常。

    result 文本形态由 config.json 的 paddle_merge_lines（默认 True）决定：
    开=同一段落的相邻文本行按坐标合并成一段（段落间仍换行）；关/抽不到行/
    合并异常=退回逐行输出。
    """
    items = list(images or [])
    total = len(items)
    results: list[dict[str, Any]] = []
    # 配置每批只读一次：避免逐页 get_config() 锁竞争 + tkinter 弹窗（仓库约定）
    merge_lines = _read_merge_lines_setting()

    def _item_page(item: Any, idx: int) -> int:
        if isinstance(item, dict):
            try:
                return int(item.get("page", idx + 1))
            except (TypeError, ValueError):
                return idx + 1
        return idx + 1

    def _item_path(item: Any) -> str | None:
        if isinstance(item, dict):
            p = item.get("path") or item.get("image_path")
            if p is None and hasattr(item, "get_path"):
                try:
                    p = item.get_path()
                except Exception:
                    p = None
            return str(p) if p else None
        return str(item) if item else None

    if not available():
        for idx, item in enumerate(items):
            page = _item_page(item, idx)
            path = _item_path(item)
            entry = {"img": path, "page": page, "result": None, "error": _MISSING_MSG}
            results.append(entry)
            if on_result:
                try:
                    on_result(entry)
                except Exception:
                    pass
            if on_progress:
                try:
                    on_progress(idx + 1, total)
                except Exception:
                    pass
        return sorted(results, key=lambda r: r["page"])

    predictor = get_predictor()
    done = 0
    for idx, item in enumerate(items):
        page = _item_page(item, idx)
        path = _item_path(item)
        entry: dict[str, Any] = {"img": path, "page": page, "result": None, "error": None}
        try:
            if not path or not os.path.isfile(path):
                raise FileNotFoundError(f"页面图片不存在：{path}")
            outs = predictor.predict(path)
            entry["result"] = _page_text(outs, merge_lines)
        except Exception as exc:  # 逐页捕获：单页失败不中断整批
            entry["error"] = str(exc)
        results.append(entry)
        done += 1
        if on_result:
            try:
                on_result(entry)
            except Exception:
                pass
        if on_progress:
            try:
                on_progress(done, total)
            except Exception:
                pass
    return sorted(results, key=lambda r: r["page"])
