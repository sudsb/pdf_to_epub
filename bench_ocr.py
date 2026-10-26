"""OCR 基线测量脚本（Phase 0，只测量、不改生产逻辑）。

用途：在固定语料上量化「渲染 dpi × 检测最长边 × 空白裁剪」对
速度（det/rec/total 毫秒）与精度（相对 CER、检出行数、行高）的影响，
为后续是否落地 det/rec 分辨率解耦、默认 dpi、空白裁剪提供数据依据。

设计要点
- 不改 config.json、不改 paddleocrmanage / pdfmanage：矩阵全部通过
  ``predictor.predict(img, text_det_limit_side_len=..., text_det_limit_type="max")``
  与本地缩放/裁剪实现。
- 测量对象是已有的整页 PNG 语料（``data/*/.ptoe_split.json`` 记录了渲染 dpi）。
  用下采样模拟更低渲染 dpi；因原始图就是该 dpi 的栅格，等效于「扫描件信息上限」。
- 真值：默认取「最高 dpi + det 原生 + 不裁剪」作为伪真值（pseudo-GT），
  只衡量**相对**退化，不声称绝对准确率。

用法::

    uv run python bench_ocr.py --preset resolution --pages 6
    uv run python bench_ocr.py --preset det --pages 6
    uv run python bench_ocr.py --preset crop --pages 6
    uv run python bench_ocr.py --preset all --pages 4 --source <dir>

输出：``data/bench/results.json`` + ``data/bench/report.md`` + 终端表格。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from typing import Any, Callable, Iterable

try:  # numpy 在标准解释器里也有；测试依赖它
    import numpy as np
except Exception:  # pragma: no cover - 环境缺 numpy 时给出明确报错
    np = None  # type: ignore


# --------------------------------------------------------------------------
# 纯函数：文本归一化 / CER / 墨迹 bbox（numpy-only，便于单测）
# --------------------------------------------------------------------------

_WS_CHARS = " \t\r\n\u3000\u00a0"
_PUNCT = set("，。、；：？！“”‘’（）《》〈〉【】—…·,.;:?!\"'()[]{}<>~`@#$%^&*_+=|\\/-")


def normalize_text(text: str, drop_punct: bool = False) -> str:
    """归一化：去空白；可选去标点。CED/归一化口径需固定，便于跨配置比较。"""
    if not text:
        return ""
    out = []
    for ch in text:
        if ch in _WS_CHARS:
            continue
        if drop_punct and ch in _PUNCT:
            continue
        out.append(ch)
    return "".join(out)


def _to_codepoints(text: str) -> "np.ndarray":
    return np.frombuffer(text.encode("utf-32-le"), dtype="<u4").astype(np.int32)


def edit_distance(a: str, b: str) -> int:
    """向量化 Levenshtein（numpy）；空串短路。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if np is None:  # pragma: no cover
        raise RuntimeError("需要 numpy 才能计算编辑距离")
    aa = _to_codepoints(a)
    bb = _to_codepoints(b)
    n, m = aa.shape[0], bb.shape[0]
    prev = np.arange(m + 1, dtype=np.int32)
    idx = np.arange(m + 1, dtype=np.int32)
    for i in range(1, n + 1):
        cur = np.empty(m + 1, dtype=np.int32)
        cur[0] = i
        cost = (bb != aa[i - 1]).astype(np.int32)
        cur[1:] = np.minimum(prev[:-1] + cost, prev[1:] + 1)
        # 处理插入：cur[j] = min(cur[j], cur[j-1] + 1)
        np.minimum.accumulate(cur - idx, out=cur)
        cur += idx
        prev = cur
    return int(prev[m])


def cer(ref: str, hyp: str) -> float:
    """字符错误率 = 编辑距离 / 参考长度。参考为空时：假设非空则返回 1.0。"""
    ref = normalize_text(ref)
    hyp = normalize_text(hyp)
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / len(ref)


def ink_bbox(
    gray: "np.ndarray",
    threshold: int = 200,
    border_ratio: float = 0.01,
    pad_ratio: float = 0.015,
    min_keep_ratio: float = 0.25,
    noop_ratio: float = 0.97,
) -> tuple[int, int, int, int] | None:
    """返回内容包围盒 (x1, y1, x2, y2)；不该裁/空页返回 None。

    - 先忽略 ``border_ratio`` 的页边（避免扫描仪黑边/装订线误判）；
    - 用 0.5%/99.5% 分位抑制孤立噪点，再按 ``pad_ratio`` 外扩；
    - 包围盒已占 ``noop_ratio`` 以上面积 -> None（裁了没收益）；
    - 裁完只剩 ``min_keep_ratio`` 以下面积 -> None（过于激进，疑似误检）；
    """
    if np is None:  # pragma: no cover
        raise RuntimeError("需要 numpy 才能计算墨迹包围盒")
    h, w = gray.shape[:2]
    bx = max(1, int(w * border_ratio))
    by = max(1, int(h * border_ratio))
    roi = gray[by : h - by, bx : w - bx]
    if roi.size == 0:
        return None
    ys, xs = np.nonzero(roi < threshold)
    if xs.size == 0:
        return None
    x1 = int(np.percentile(xs, 0.5)) + bx
    x2 = int(np.percentile(xs, 99.5)) + bx
    y1 = int(np.percentile(ys, 0.5)) + by
    y2 = int(np.percentile(ys, 99.5)) + by
    px = int(w * pad_ratio)
    py = int(h * pad_ratio)
    x1, y1 = max(0, x1 - px), max(0, y1 - py)
    x2, y2 = min(w, x2 + px + 1), min(h, y2 + py + 1)
    area = (x2 - x1) * (y2 - y1)
    if area >= noop_ratio * w * h:
        return None
    if area < min_keep_ratio * w * h:
        return None
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    return x1, y1, x2, y2


def ink_ratio(gray: "np.ndarray", threshold: int = 200) -> float:
    """暗像素占比，用于空白页判断（独立于 bbox）。"""
    if np is None:  # pragma: no cover
        raise RuntimeError("需要 numpy")
    total = gray.size
    if not total:
        return 0.0
    return float((gray < threshold).sum()) / float(total)


# --------------------------------------------------------------------------
# 计时代理：包裹 det / rec 子预测器，捕获惰性生成器
# --------------------------------------------------------------------------


class TimedProxy:
    """把 ``predictor.text_det_model`` 这类可调用对象包一层计时。

    若返回值是生成器，则在迭代结束时累计耗时（PaddleX 部分子预测器是惰性的）。
    """

    def __init__(self, obj: Any, sink: dict[str, float], key: str) -> None:
        self._obj = obj
        self._sink = sink
        self._key = key

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        try:
            res = self._obj(*args, **kwargs)
        finally:
            self._sink[self._key] = self._sink.get(self._key, 0.0) + (
                time.perf_counter() - t0
            )
        if hasattr(res, "__next__"):
            sink, key = self._sink, self._key

            def _gen() -> Iterable[Any]:
                t = time.perf_counter()
                try:
                    for item in res:
                        yield item
                finally:
                    sink[key] = sink.get(key, 0.0) + (time.perf_counter() - t)

            return _gen()
        return res

    def rebind(self, sink: dict[str, float]) -> None:
        """换绑计时落点（同一 predictor 反复测量时复用代理）。"""
        self._sink = sink

    def __getattr__(self, name: str) -> Any:  # 透明转发其他属性
        return getattr(self._obj, name)


def _iter_pipelines(pipe: Any) -> Iterable[Any]:
    """展开 PaddleX 的并行包装层，拿到真正执行推理的 OCRPipeline。

    ``AutoParallelSimpleInferencePipeline`` 把真实实例放在 ``_pipeline`` /
    ``_pipelines`` 里，并在 ``__getattr__`` 中转发属性；直接给外层对象
    setattr 不会影响实际调用（实测计时恒为 0）。
    """
    inner = getattr(pipe, "_pipeline", None)
    if inner is not None and inner is not pipe:
        yield from _iter_pipelines(inner)
        return
    multi = getattr(pipe, "_pipelines", None)
    if multi:
        for p in multi:
            yield from _iter_pipelines(p)
        return
    yield pipe


def instrument(predictor: Any, sink: dict[str, float]) -> None:
    """把 predictor 内部 det/rec 子模型包上计时代理，结果累加进 ``sink``。

    代理只装一次；后续调用通过 ``rebind`` 指向新的 sink，避免计时串到上一页。
    """
    pipe = getattr(predictor, "paddlex_pipeline", None)
    if pipe is None:
        return
    for real in _iter_pipelines(pipe):
        for attr, key in (("text_det_model", "det"), ("text_rec_model", "rec")):
            model = getattr(real, attr, None)
            if model is None:
                continue
            if isinstance(model, TimedProxy):
                model.rebind(sink)
            else:
                setattr(real, attr, TimedProxy(model, sink, key))


# --------------------------------------------------------------------------
# 语料
# --------------------------------------------------------------------------


def discover_sources(root: str = "data") -> list[dict[str, Any]]:
    """扫描 ``data/*/.ptoe_split.json``，返回 [{dir, dpi, pages}]，按 dpi 降序。"""
    found: list[dict[str, Any]] = []
    if not os.path.isdir(root):
        return found
    for name in os.listdir(root):
        d = os.path.join(root, name)
        meta = os.path.join(d, ".ptoe_split.json")
        if not os.path.isdir(d) or not os.path.isfile(meta):
            continue
        try:
            with open(meta, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            dpi = int(data.get("dpi") or 0)
        except Exception:
            continue
        pages = [p for p in os.listdir(d) if p.lower().endswith(".png")]
        if len(pages) < 2 or dpi <= 0:
            continue
        found.append({"dir": d, "dpi": dpi, "pages": len(pages)})
    found.sort(key=lambda x: (-x["dpi"], -x["pages"]))
    return found


def pick_pages(source_dir: str, count: int) -> list[str]:
    """按页码数字排序后均匀抽样，返回图片绝对/相对路径列表。"""

    def key(p: str) -> tuple[int, str]:
        stem = os.path.splitext(os.path.basename(p))[0]
        return (int(stem) if stem.isdigit() else 10**9, stem)

    pages = sorted(
        (os.path.join(source_dir, p) for p in os.listdir(source_dir) if p.lower().endswith(".png")),
        key=key,
    )
    if count >= len(pages):
        return pages
    step = len(pages) / float(count)
    picked = [pages[min(len(pages) - 1, int(i * step))] for i in range(count)]
    # 去重保序
    seen: set[str] = set()
    out = []
    for p in picked:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# --------------------------------------------------------------------------
# 单页测量
# --------------------------------------------------------------------------


def load_gray(path: str) -> "np.ndarray":
    """读灰度图；走 fromfile+imdecode，规避 Windows 下 cv2.imread 不支持中文路径。"""
    import cv2

    try:
        buf = np.fromfile(path, dtype=np.uint8)
    except OSError as exc:
        raise FileNotFoundError(f"无法读取图片：{path}") from exc
    if buf.size == 0:
        raise FileNotFoundError(f"无法读取图片：{path}")
    img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"无法解码图片：{path}")
    return img


def resize_scale(gray: "np.ndarray", scale: float) -> "np.ndarray":
    if abs(scale - 1.0) < 1e-6:
        return gray
    import cv2

    h, w = gray.shape[:2]
    nw, nh = max(8, int(round(w * scale))), max(8, int(round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
    return cv2.resize(gray, (nw, nh), interpolation=interp)


def measure_page(
    predictor: Any,
    page_path: str,
    native_dpi: int,
    target_dpi: int,
    det_limit: int | None,
    crop: bool,
    merge_lines: bool = True,
) -> dict[str, Any]:
    """对单页跑一次配置，返回文本/行数/行高/分阶段耗时。"""
    import cv2
    import paddleocrmanage as pm

    sink: dict[str, float] = {}
    instrument(predictor, sink)

    t0 = time.perf_counter()
    gray = load_gray(page_path)
    t_load = time.perf_counter() - t0

    scale = target_dpi / float(native_dpi)
    t0 = time.perf_counter()
    img = resize_scale(gray, scale) if scale < 1.0 else gray
    cropped = False
    blank = False
    if crop:
        ratio = ink_ratio(img)
        if ratio < 1e-4:
            blank = True
        else:
            box = ink_bbox(img)
            if box is not None:
                x1, y1, x2, y2 = box
                img = img[y1:y2, x1:x2]
                cropped = True
    t_pre = time.perf_counter() - t0

    # PaddleOCR 3.x 的输入校验按 HxWxC 解包，灰度图需扩成 3 通道（与生产读 PNG 后一致）
    inp = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    kwargs: dict[str, Any] = {}
    if det_limit is not None:
        kwargs["text_det_limit_side_len"] = int(det_limit)
        kwargs["text_det_limit_type"] = "max"

    sink.clear()
    t0 = time.perf_counter()
    outs = list(predictor.predict(inp, **kwargs)) if not blank else []
    t_total = time.perf_counter() - t0

    lines: list[dict[str, Any]] = []
    if outs:
        try:
            res = outs[0]
            lines = pm.extract_lines_from_res(res) or []
        except Exception:
            lines = []
    heights = []
    for ln in lines:
        box = ln.get("box")
        if box:
            heights.append(float(box[3] - box[1]))
    text_lines = "".join((ln.get("text") or "") for ln in lines)
    try:
        text_merged = pm._page_text(outs, merge_lines) if outs else ""
    except Exception:
        text_merged = text_lines

    return {
        "page": os.path.basename(page_path),
        "blank": blank,
        "cropped": cropped,
        "img_w": int(img.shape[1]),
        "img_h": int(img.shape[0]),
        "n_lines": len(lines),
        "median_line_h": round(statistics.median(heights), 2) if heights else 0.0,
        "ink_ratio": round(ink_ratio(gray), 6),
        "ms_load": round(t_load * 1000, 1),
        "ms_pre": round(t_pre * 1000, 1),
        "ms_det": round(sink.get("det", 0.0) * 1000, 1),
        "ms_rec": round(sink.get("rec", 0.0) * 1000, 1),
        "ms_total": round(t_total * 1000, 1),
        "text_lines": text_lines,
        "text_merged": text_merged,
    }


# --------------------------------------------------------------------------
# 配置矩阵
# --------------------------------------------------------------------------


def build_matrix(
    preset: str, native_dpi: int, dpi_list: list[int] | None = None
) -> list[dict[str, Any]]:
    """返回 [{name, dpi, det, crop}]；dpi 会按 native_dpi 截断。"""
    dpis = [d for d in (dpi_list or [native_dpi, 200, 150, 120, 100]) if 0 < d <= native_dpi]
    # 去重保序
    seen: set[int] = set()
    dpis = [d for d in dpis if not (d in seen or seen.add(d))]

    def cfg(name: str, dpi: int, det: int | None, crop: bool) -> dict[str, Any]:
        return {"name": name, "dpi": dpi, "det": det, "crop": crop}

    if preset == "resolution":
        return [cfg(f"dpi{d}_det-native", d, None, False) for d in dpis]
    if preset == "det":
        dets = [None, 1600, 1280, 960]
        base = [d for d in dpis if d in (native_dpi, 200)] or dpis[:1]
        return [cfg(f"dpi{d}_det{dl or 'native'}", d, dl, False) for d in base for dl in dets]
    if preset == "crop":
        base = [d for d in dpis if d in (native_dpi, 200)] or dpis[:1]
        return [cfg(f"dpi{d}_det1280_crop{c}", d, 1280, c) for d in base for c in (False, True)]
    if preset == "all":
        dets = [None, 1280]
        return [
            cfg(f"dpi{d}_det{dl or 'native'}_crop{c}", d, dl, c)
            for d in dpis
            for dl in dets
            for c in (False, True)
        ]
    raise ValueError(f"未知 preset: {preset}")


# --------------------------------------------------------------------------
# 运行与报告
# --------------------------------------------------------------------------


def run(
    source: str,
    native_dpi: int,
    preset: str,
    n_pages: int,
    out_dir: str,
    dpi_list: list[int] | None = None,
    merge_lines: bool = True,
    warmup: bool = True,
) -> dict[str, Any]:
    import paddleocrmanage as pm

    pages = pick_pages(source, n_pages)
    predictor = pm.get_predictor()
    matrix = build_matrix(preset, native_dpi, dpi_list)

    if warmup and pages:
        print("[bench] 预热中（首次推理含 cuDNN autotune）…", flush=True)
        measure_page(predictor, pages[0], native_dpi, min(150, native_dpi), 1280, True, merge_lines)

    results: list[dict[str, Any]] = []
    for i, conf in enumerate(matrix, 1):
        print(f"[bench] ({i}/{len(matrix)}) {conf['name']}", flush=True)
        per_page = []
        for p in pages:
            rec = measure_page(
                predictor, p, native_dpi, conf["dpi"], conf["det"], conf["crop"], merge_lines
            )
            per_page.append(rec)
            print(
                f"    {rec['page']}: {rec['img_w']}x{rec['img_h']} "
                f"lines={rec['n_lines']} h={rec['median_line_h']}px "
                f"det={rec['ms_det']}ms rec={rec['ms_rec']}ms total={rec['ms_total']}ms",
                flush=True,
            )
        results.append({**conf, "pages": per_page})

    ref_name = matrix[0]["name"] if matrix else ""
    ref_entry = next((r for r in results if r["name"] == ref_name), None)
    ref_text = "".join(p["text_lines"] for p in ref_entry["pages"]) if ref_entry else ""

    summary = []
    for r in results:
        pages_r = r["pages"]
        text = "".join(p["text_lines"] for p in pages_r)
        summary.append(
            {
                "name": r["name"],
                "dpi": r["dpi"],
                "det": r["det"],
                "crop": r["crop"],
                "mean_ms_det": round(statistics.mean(p["ms_det"] for p in pages_r), 1),
                "mean_ms_rec": round(statistics.mean(p["ms_rec"] for p in pages_r), 1),
                "mean_ms_total": round(statistics.mean(p["ms_total"] for p in pages_r), 1),
                "mean_ms_pre": round(statistics.mean(p["ms_pre"] for p in pages_r), 1),
                "mean_ms_load": round(statistics.mean(p["ms_load"] for p in pages_r), 1),
                "lines": sum(p["n_lines"] for p in pages_r),
                "median_line_h": round(
                    statistics.median([p["median_line_h"] for p in pages_r if p["median_line_h"]] or [0]),
                    2,
                ),
                "chars": len(normalize_text(text)),
                "cer_vs_ref": round(cer(ref_text, text), 5),
            }
        )

    payload = {
        "source": source,
        "native_dpi": native_dpi,
        "preset": preset,
        "merge_lines": merge_lines,
        "pages": pages,
        "results": results,
        "summary": summary,
    }
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    _write_report(payload, os.path.join(out_dir, "report.md"))
    return payload


def format_table(summary: list[dict[str, Any]]) -> str:
    head = (
        "| 配置 | dpi | det | crop | det(ms) | rec(ms) | total(ms) | 行数 | 行高(px) | 字符 | CER |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    rows = []
    for s in summary:
        rows.append(
            f"| {s['name']} | {s['dpi']} | {s['det'] or 'native'} | "
            f"{'on' if s['crop'] else 'off'} | {s['mean_ms_det']} | {s['mean_ms_rec']} | "
            f"{s['mean_ms_total']} | {s['lines']} | {s['median_line_h']} | "
            f"{s['chars']} | {s['cer_vs_ref']} |"
        )
    return head + "\n".join(rows) + "\n"


def _write_report(payload: dict[str, Any], path: str) -> None:
    lines = [
        "# OCR 基线测量报告（自动生成）",
        "",
        f"- 语料：`{payload['source']}`（原生 dpi={payload['native_dpi']}）",
        f"- 预设：`{payload['preset']}`，页数：{len(payload['pages'])}",
        f"- 伪真值：矩阵第一个配置（最高 dpi + det 原生 + 不裁剪）",
        "- 说明：CER 为相对退化指标；真值本身也来自 OCR，不代表绝对准确率。",
        "",
        format_table(payload["summary"]),
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def find_source(sources: list[dict[str, Any]], path: str) -> dict[str, Any] | None:
    """按绝对路径匹配语料；``--source`` 常传绝对路径，不能直接比字符串。"""
    target = os.path.normcase(os.path.abspath(path))
    for s in sources:
        if os.path.normcase(os.path.abspath(s["dir"])) == target:
            return s
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="OCR 基线测量（Phase 0）")
    ap.add_argument("--source", default="", help="整页 PNG 目录（默认自动选最高 dpi 语料）")
    ap.add_argument("--preset", default="resolution",
                    choices=["resolution", "det", "crop", "all"])
    ap.add_argument("--pages", type=int, default=6)
    ap.add_argument("--dpi", default="", help="逗号分隔的目标 dpi 列表，覆盖预设默认")
    ap.add_argument("--out", default=os.path.join("data", "bench"))
    ap.add_argument("--no-merge", action="store_true", help="关闭段落合并（逐行输出）")
    ap.add_argument("--list-sources", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sources = discover_sources("data")
    if args.list_sources:
        for s in sources:
            print(f"{s['dpi']:>4}dpi  {s['pages']:>4}页  {s['dir']}")
        return 0
    if not sources:
        print("未找到语料（data/*/.ptoe_split.json）", file=sys.stderr)
        return 2
    if args.source:
        src = find_source(sources, args.source)
        if src is None:
            print(
                f"警告：{args.source} 未在 data/*/.ptoe_split.json 中登记，按原生 300dpi 处理",
                file=sys.stderr,
            )
            src = {"dir": args.source, "dpi": 300, "pages": 0}
        else:
            print(f"[bench] 指定语料：{src['dir']}（{src['dpi']}dpi，{src['pages']}页）")
    else:
        src = sources[0]
        print(f"[bench] 自动选择语料：{src['dir']}（{src['dpi']}dpi，{src['pages']}页）")

    dpi_list = [int(x) for x in args.dpi.split(",") if x.strip()] if args.dpi else None
    payload = run(
        source=src["dir"],
        native_dpi=int(src["dpi"]),
        preset=args.preset,
        n_pages=args.pages,
        out_dir=args.out,
        dpi_list=dpi_list,
        merge_lines=not args.no_merge,
    )
    print()
    print(format_table(payload["summary"]))
    print(f"结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
