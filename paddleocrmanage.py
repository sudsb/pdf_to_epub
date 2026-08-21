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
- batch_infer 返回形状与 llamamanage.batch_infer 完全一致：
  [{page, result, error}, ...]（按页码排序），失败不抛异常、逐页捕获，
  使 mian 的结构化/断点续传/进度文件逻辑零改动复用。
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable

__all__ = ["available", "detect_gpu", "get_predictor", "reset_predictor", "batch_infer"]

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
        lang="zh",
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
    """
    items = list(images or [])
    total = len(items)
    results: list[dict[str, Any]] = []

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
            texts: list[str] = []
            for res in outs or []:
                res_data = getattr(res, "res", None) or {}
                texts.extend(res_data.get("rec_texts") or [])
            entry["result"] = "\n".join(texts)
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
