"""ptoe entrypoint: PDF -> images -> OCR -> XHTML -> EPUB pipeline.

CLI:
  python mian.py [--version]
  python mian.py -e TEXT
  python mian.py epub <pdf> [--dpi 0] [--model HY] [--workers N] [--thinking]
                    [--title TITLE] [--author AUTHOR] [--lang zh-CN]
                    [--out-dir DIR] [--epub-path PATH] [--correct]
  python mian.py stop [--engine llama|vllm]   # 停止推理服务（llama-server / vLLM-Omni，释放端口）

--dpi 为档位（0-4）而非原始数值：0=100, 1=150, 2=200, 3=300, 4=600（默认 0）。
--correct 开启手动矫正：在浏览器中逐页对照原图与识别文字（默认关闭）。

无参数 + 交互终端（含打包 exe 双击启动）进入终端菜单 _run_menu：
PDF→EPUB 转换 / 手动矫正 / 配置 / 模型管理 / 退出。
非交互 stdin（管道/重定向）保持打印 "nothing to do"。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

from pdfmanage import split_pdf_to_images

# Defer importing llamamanage (requests dependency) to runtime paths that need OCR.
# Provide a safe fallback for REQUEST_TIMEOUT so CLI help and flags work.
REQUEST_TIMEOUT = 600

_PAGE_RE = re.compile(r"(\d+)(?:\.\w+)?$")


def parse_exclude_spec(spec) -> set[int]:
    """解析排除页码规格，返回页码集合（1-based）。

    支持格式：
    - 字符串 "1-15,17,20" -> {1..15, 17, 20}
    - 字符串 "1-15, 17, 20" （允许空白）
    - 列表 [1, 2, 5] -> {1, 2, 5}
    - 空/None/"" -> 空集合
    - 无效 token 会被忽略并打印警告（不崩溃）。
    """
    excluded: set[int] = set()
    if spec is None:
        return excluded
    if isinstance(spec, (list, tuple, set)):
        for v in spec:
            try:
                excluded.add(int(v))
            except (TypeError, ValueError):
                print(f"      ! 排除页码忽略无效项: {v!r}", file=sys.stderr)
        return excluded
    if isinstance(spec, str):
        s = spec.strip()
        if not s:
            return excluded
        for token in s.split(","):
            token = token.strip()
            if not token:
                continue
            if "-" in token:
                parts = token.split("-", 1)
                try:
                    start = int(parts[0].strip())
                    end = int(parts[1].strip())
                except (ValueError, IndexError):
                    print(f"      ! 排除页码忽略无效区间: {token!r}", file=sys.stderr)
                    continue
                if start > end:
                    start, end = end, start
                for n in range(start, end + 1):
                    if n >= 1:
                        excluded.add(n)
            else:
                try:
                    n = int(token)
                    if n >= 1:
                        excluded.add(n)
                except ValueError:
                    print(f"      ! 排除页码忽略无效项: {token!r}", file=sys.stderr)
        return excluded
    # 未知类型：尝试转为字符串再解析
    try:
        return parse_exclude_spec(str(spec))
    except Exception:
        return excluded

# 5 档 DPI：档位 -> 实际分辨率。档位越高图片 token 越多（约线性）、识别越精细但越慢。
DPI_LEVELS = {0: 100, 1: 150, 2: 200, 3: 300, 4: 600}

# 进度条宽度（字符）
_BAR_WIDTH = 24


def _read_meta() -> tuple[str, str]:
    """Return (name, version) from pyproject.toml or sensible defaults.

    PyInstaller 打包后 pyproject.toml 通过 --add-data 放入 sys._MEIPASS；
    __file__ 指向解包目录，直接 with_name() 找不到，需走 _MEIPASS。
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    else:
        base = Path(__file__).parent
    path = base / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        proj = data.get("project", {})
        return proj.get("name", "ptoe"), proj.get("version", "0.0.0")
    except Exception:
        return "ptoe", "0.0.0"


def _page_of(image) -> int:
    """Extract the 1-based page number from an image path, ImageItem, or filename.

    Accepts either a path-like (str/Path) or an object with a `.path` attribute
    (e.g. ImageItem). Uses the same _PAGE_RE regex over the stringified path.
    """
    candidate = getattr(image, "path", image)
    m = _PAGE_RE.search(str(candidate))
    return int(m.group(1)) if m else 0


def _pdf_title(pdf: Path) -> str:
    """从 PDF 元数据提取标题；失败或为空返回 ''。"""
    try:
        import fitz

        with fitz.open(pdf) as doc:
            meta = doc.metadata or {}
            return (meta.get("title") or "").strip()
    except Exception:
        return ""


def _ensure_server(model_key: str, workers: int | None = None) -> None:
    """Start the configured inference engine if it is not already serving the model.

    workers：调用方已知的并发数，传给 runserver 用于 --parallel 自适应
    （槽位不多于实际并发，避免 KV cache 浪费显存，见 llamamanage.runserver）。
    """
    from configmanage import get_config
    from llamamanage import _probe_server, runserver

    cfg = get_config(show_dialogs=False)
    model_name = (
        (cfg.get("model_choices") or {}).get(model_key, {}).get("name", model_key)
        if isinstance(cfg.get("model_choices"), dict)
        else model_key
    )
    if _probe_server(model_name) == "match":
        print("      server already running with the requested model")
        return
    print(f"      starting server (model='{model_key}') ...")
    # import runserver lazily to avoid pull-in of requests when not needed
    if not runserver(model_key, parallel=workers):
        raise RuntimeError(
            "server failed to start; check engine/llama_server/vllm_server in config.json"
        )


# ---- PaddleOCR 引擎覆盖（2026-08）：--engine paddle 仅作用于 PDF OCR 阶段 ----
_ENGINE_OVERRIDE: str | None = None


def _apply_engine_arg(engine: str | None) -> None:
    """应用 CLI --engine：paddle 走模块级覆盖（不进 llamamanage/config），llama/vllm 走 set_engine。"""
    global _ENGINE_OVERRIDE
    if not engine:
        return
    if engine == "paddle":
        _ENGINE_OVERRIDE = "paddle"
        return
    from llamamanage import set_engine

    set_engine(engine)


def _active_ocr_engine() -> str:
    """PDF OCR 阶段实际使用的引擎（paddle 覆盖优先于 config/llamamanage）。"""
    if _ENGINE_OVERRIDE == "paddle":
        return "paddle"
    from llamamanage import _active_engine

    return _active_engine()


def _apply_correction(
    structured: dict, corrected: list[dict[str, Any]], *, strict_markers: bool
) -> None:
    """把矫正后的 pages 写入 structured（body/paragraphs/articles）。

    strict_markers=True 时，标记处理失败（如注释标记与注释数量不匹配）抛异常
    （供浏览器端「完成并转换」回显提示）；False 时打印并跳过标记结构继续转换。
    """
    structured["pages"] = corrected
    structured["body"] = "\n\n".join(
        p["text"].strip() for p in corrected if (p.get("text") or "").strip()
    )
    structured["paragraphs"] = [
        {"page": p["page"], "text": p["text"]}
        for p in corrected
        if (p.get("text") or "").strip()
    ]
    # 标记 → 文章结构（全文=新文章/新页，段落=合并，注释=替换进正文）
    structured.pop("articles", None)
    if any(
        "data-ptoe-marker" in (p.get("text") or "")
        or "ptoe-note" in (p.get("text") or "")
        for p in corrected
    ):
        try:
            # import lazily to avoid pulling heavy GUI/IO deps into callers that
            # don't need the correction pipeline.
            from correctmanage import apply_markers

            articles = apply_markers(corrected)
        except ValueError as e:
            if strict_markers:
                raise
            print(f"      ! 标记处理失败：{e}", file=sys.stderr)
            articles = None

        if articles:
            structured["articles"] = articles


def correct_pdf(
    pdf_path: str | Path | None = None,
    *,
    title: str | None = None,
    author: str | None = None,
    language: str = "zh-CN",
    out_dir: str | Path | None = None,
    epub_path: str | Path | None = None,
    idle_timeout: int = 600,
) -> dict:
    """直接启动手动矫正界面（不跑 OCR）。

    pdf_path 可为 None（无文件直接启动）：界面从空白开始（0 页），主要用于
    历史记录管理与手动录入；此时「完成并转换」无可转换内容，会回提示。
    有 pdf 时：页面文本优先取本地历史缓存最新版本（data/correction_history/），
    无历史则为空白页；每次点「完成并转换」都会重新生成 EPUB（可留在页面继续
    修改后再次点击）。浏览器关闭超过 idle_timeout 秒后结束等待：已转换过用
    最近结果；否则有内容时补转一次，无内容则不转换。
    """
    # 惰性导入：correctmanage/htmlmanage 依赖（zhconv 等）仅在本命令用到时加载
    from correctmanage import correct_pages
    from htmlmanage import HTMLConverter

    if pdf_path is not None:
        pdf = Path(pdf_path)
        if not pdf.is_file():
            raise FileNotFoundError(f"PDF not found: {pdf}")
        try:
            import fitz

            with fitz.open(pdf) as doc:
                total = doc.page_count
        except Exception as e:
            raise RuntimeError(f"无法读取 PDF（需要 PyMuPDF 获取页数）：{e}") from e
        pdf_title = _pdf_title(pdf)
    else:
        pdf = None
        total = 0
        pdf_title = ""
    pages: list[dict[str, Any]] = [{"page": n, "text": ""} for n in range(1, total + 1)]
    structured: dict[str, Any] = {"pages": pages, "body": "", "paragraphs": []}
    structured["meta"] = {
        "title": title or pdf_title or (pdf.stem if pdf else "未命名"),
        "author": author or "",
        "language": language,
        "epub_version": "3.0",
        "package_epub": True,
        "epub_path": str(Path(epub_path).resolve()) if epub_path else None,
    }
    # 默认输出目录跟随程序所在目录（冻结时为 exe 目录），与分割图片一致
    from pdfmanage import app_base_dir

    root = (
        Path(out_dir)
        if out_dir
        else (
            app_base_dir() / "data" / pdf.stem if pdf else app_base_dir() / "data"
        )
    )
    root.mkdir(parents=True, exist_ok=True)

    last_convert: dict[str, Any] = {}

    def _convert_corrected(
        corrected: list[dict[str, Any]], name: str | None = None
    ) -> dict[str, Any]:
        """「完成并转换」回调：每次点击都转换，结果回给浏览器界面。

        无文件模式（pdf 为 None）同样支持转换：只要内容非空（打开历史记录
        或手动录入的文本）即可生成 EPUB。name 为浏览器传来的历史记录名
        （无文件模式下用作 EPUB 标题，除非命令行已指定 --title）。
        """
        if not corrected or not any((p.get("text") or "").strip() for p in corrected):
            return {
                "ok": False,
                "message": "没有可转换的内容（请先录入或打开历史记录）",
            }
        if name and not title:
            structured["meta"]["title"] = name
        try:
            _apply_correction(structured, corrected, strict_markers=True)
            result = HTMLConverter(
                output_dir=str(root), epub_version="3.0"
            ).convert_document(structured)
            last_convert["result"] = result
            return {"ok": True, "message": "转换完成", "epub": result.get("epub")}
        except Exception as e:  # 注释数量不匹配等 → 回给浏览器提示
            return {"ok": False, "message": str(e)}

    print("      矫正（直接启动：不跑 OCR；历史缓存优先，空白页可手动录入）")
    corrected = correct_pages(
        pages,
        pdf_path=pdf,
        img_dir=None,
        idle_timeout=idle_timeout,
        on_convert=_convert_corrected,
    )
    if last_convert.get("result") is not None:
        # 浏览器端已「完成并转换」过（可多次），直接用最近一次转换结果
        result = last_convert["result"]
    elif any((p.get("text") or "").strip() for p in corrected):
        # 浏览器被关闭且未点过完成并转换：有内容则按已保存内容补转一次
        _apply_correction(structured, corrected, strict_markers=False)
        result = HTMLConverter(
            output_dir=str(root), epub_version="3.0"
        ).convert_document(structured)
    else:
        print("      未产生转换（没有可转换的内容）")
        return {"content_files": []}

    if result.get("epub"):
        print(f"Done: {result['epub']}")
    elif result.get("epub_error"):
        print(f"EPUB packaging failed: {result['epub_error']}", file=sys.stderr)
    return result


# 各流程阶段的中文名（输出顺序 = 流水线顺序）；未执行的阶段不输出
_STAGE_LABELS: list[tuple[str, str]] = [
    ("split", "分割图片（含图片处理）"),
    ("model", "模型启动"),
    ("ocr", "文字识别"),
    ("structure", "文字整理"),
    ("correct", "矫正界面"),
    ("render", "EPUB 生成"),
    ("history", "历史记录保存"),
]


def _print_timing_summary(timings: dict[str, float], *, ocr_pages: int) -> None:
    """流程结束时输出各阶段总用时与总流程用时，并单独给出平均每页识别用时。

    timings 键 = _STAGE_LABELS 各阶段 + "total"（总流程）；ocr_pages 为本次
    实际识别的页数（断点续传时可能小于总页数，用于计算平均每页用时）。
    """
    print("\n—— 用时统计 ——")
    for key, label in _STAGE_LABELS:
        v = timings.get(key)
        if v is not None:
            print(f"  {label}：{v:.1f} 秒")
    total = timings.get("total")
    if total is not None:
        print(f"  总流程：{total:.1f} 秒")
    ocr = timings.get("ocr")
    if ocr is not None and ocr_pages > 0:
        print(
            f"  平均每页识别：{ocr / ocr_pages:.2f} 秒/页（本次共识别 {ocr_pages} 页）"
        )


def pdf_to_epub(
    pdf_path: str | Path,
    *,
    dpi: int = 100,
    model_key: str = "HY",
    max_workers: int | None = None,
    thinking: bool = False,
    timeout: int = REQUEST_TIMEOUT,
    title: str | None = None,
    author: str | None = None,
    language: str = "zh-CN",
    out_dir: str | Path | None = None,
    epub_path: str | Path | None = None,
    correct: bool = False,
    correct_idle_timeout: int = 600,
    resume: str | None = None,
    exclude=None,
) -> dict:
    """Convert a PDF to EPUB by chaining the pipeline modules.

    Steps: split_pdf_to_images -> batch_infer (llama-server OCR) ->
    clean_and_structure_text -> HTMLConverter.convert_document (XHTML + EPUB pack).
    OCR results are sorted by page number before structuring (batch_infer
    returns futures in completion order, not page order).
    correct=True 时在结构化之后、渲染之前插入手动矫正界面（浏览器逐页对照
    原图与识别文字，可标记粗体/斜体/标题），默认关闭、不改变既有流程。
    """
    pdf = Path(pdf_path)
    if not pdf.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf}")

    pdf_title = _pdf_title(pdf)
    t_start = time.perf_counter()
    timings: dict[str, float] = {}  # 各流程阶段用时（结束时统一输出）

    # 文件日志
    from logmanage import logger

    logger.info(f"pdf_to_epub start: pdf={pdf}, dpi={dpi}, model={model_key}, workers={max_workers}")

    print(f"[1/4] Splitting PDF to images (dpi={dpi}) ...", end="", flush=True)
    t0 = time.perf_counter()
    img_dir, img_paths = split_pdf_to_images(pdf, dpi=dpi, fmt="png")
    timings["split"] = time.perf_counter() - t0
    print(f" done in {timings['split']:.1f}s")
    print(f"      {len(img_paths)} page(s) -> {img_dir}")
    logger.info(f"split done: pages={len(img_paths)}, dir={img_dir}, elapsed={timings['split']:.1f}s")

    print(
        f"[2/4] OCR via llama-server (model='{model_key}', workers={max_workers if max_workers else 'auto'}) ...",
        end="",
        flush=True,
    )
    t0 = time.perf_counter()
    total_pages = len(img_paths)
    t_ocr = time.perf_counter()

    # 解析排除页码：CLI --exclude 优先，其次配置文件 exclude_pages
    from configmanage import get_config

    cfg = get_config(show_dialogs=False)
    exclude_spec = exclude if exclude is not None else cfg.get("exclude_pages")
    excluded_pages = parse_exclude_spec(exclude_spec)
    if excluded_pages:
        # 只保留在有效页码范围内的排除页
        excluded_pages = {p for p in excluded_pages if 1 <= p <= total_pages}
        print(f"      ! 排除 {len(excluded_pages)} 页 OCR：{sorted(excluded_pages)}")
        logger.info(f"exclude pages: {sorted(excluded_pages)} (count={len(excluded_pages)})")

    # --- OCR 断点续传：检查上次进度，决定 全新/继续/直接转换/重来 ------------------
    progress = _load_ocr_progress(img_dir)
    resume_mode = "new"  # new | resume | convert | restart | abort
    if (
        progress
        and str(progress.get("pdf") or "") == str(pdf.resolve())
        and int(progress.get("total") or -1) == total_pages
    ):
        if resume == "restart":
            resume_mode = "restart"
        elif resume == "resume":
            resume_mode = (
                "convert" if progress.get("status") == "ocr_done" else "resume"
            )
        else:
            resume_mode = _ask_ocr_resume(progress)
    elif resume == "resume":
        # resume 子命令但找不到进度：询问是否从头完整转换
        ans = _ask("未找到可恢复的 OCR 进度，是否从头完整转换？(y/N)：")
        if ans.lower() != "y":
            print("已取消。")
            return {"ok": False, "message": "cancelled", "epub_error": None}
        resume_mode = "new"

    if resume_mode == "abort":
        print("已取消。")
        return {"ok": False, "message": "cancelled", "epub_error": None}

    cached: dict[int, str] = {}  # 页码 -> 已识别文本（继续/转换时复用，不再请求）
    if resume_mode == "restart":
        _clear_ocr_progress(img_dir)
        progress = None
        resume_mode = "new"
    elif resume_mode in ("resume", "convert"):
        for k, v in (progress.get("pages") or {}).items():
            try:
                page_no = int(k)
            except (TypeError, ValueError):
                continue
            if v.get("status") == "done" and v.get("result"):
                cached[page_no] = v["result"]
        if resume_mode == "convert" and len(cached) < total_pages:
            print(
                f"      ! 有 {total_pages - len(cached)} 页无缓存结果，将一并重新识别"
            )

    todo_images = [p for p in img_paths if _page_of(p) not in cached and _page_of(p) not in excluded_pages]

    # 进度文件初始化：每页完成后写盘，中断不丢已完成页
    if progress is None:
        progress = {
            "pdf": str(pdf.resolve()),
            "dpi": dpi,
            "model_key": model_key,
            "total": total_pages,
            "status": "running",
            "pages": {},
        }
        _save_ocr_progress(img_dir, progress)
    elif progress.get("status") == "ocr_done":
        # 继续模式下有页需重识别（如失败页重试）→ 回到运行中状态
        progress["status"] = "running"

    # 并发数：--workers 未显式指定时按模型推荐（model_choices.<key>.workers，
    # 设置页可调；未配置则 3）。一次解析，_ensure_server（--parallel 自适应）
    # 与 batch_infer 共用同一值。
    from llamamanage import default_workers

    eff_workers = max_workers if (max_workers or 0) >= 1 else default_workers(model_key)
    if max_workers is None:
        print(f"      workers={eff_workers}（模型推荐）")

    # PaddleOCR 引擎（--engine paddle）：本地推理，无需启动 llama/vllm 服务
    use_paddle = _active_ocr_engine() == "paddle"

    if todo_images and not use_paddle:
        t_model = time.perf_counter()
        logger.info(f"starting server: model={model_key}, workers={eff_workers}")
        _ensure_server(model_key, workers=eff_workers)
        timings["model"] = time.perf_counter() - t_model
        logger.info(f"server ready: model={model_key}, elapsed={timings['model']:.1f}s")

    def _on_progress(done: int, total: int) -> None:
        """每完成一页 OCR 输出一行进度：已完成页数 + 百分比 + 已用时间（总 + 本步骤）。"""
        elapsed = time.perf_counter() - t_start
        pct = done / total * 100 if total else 100.0
        filled = round(_BAR_WIDTH * pct / 100)
        bar = "#" * filled + "-" * (_BAR_WIDTH - filled)
        print(
            f"      OCR {done}/{total} [{bar}] {pct:5.1f}% | "
            f"elapsed {elapsed:6.1f}s | step {time.perf_counter() - t_ocr:6.1f}s"
        )

    # 进度写盘节流（2026-08-17 性能调优）：每页完成都整文件原子写，文件随页数
    # 增长（含全部已识别文本），快模型下磁盘写会成为批次瓶颈。改为最多每 2 秒
    # 写一次，最后一页必写；中断最多丢失最近 2 秒内完成的页（续传时重新识别，
    # 正确性不受影响）。批次结束后 status=ocr_done 的那次写盘始终执行。
    _last_save_ts = 0.0
    _saved_count = 0

    def _on_ocr_result(res: dict) -> None:
        """每页 OCR 完成即写进度文件（断点续传核心：中断后已完成页不丢）。"""
        nonlocal _last_save_ts, _saved_count
        page_no = _page_of(res.get("img"))
        ok = not res.get("error")
        progress["pages"][str(page_no)] = {
            "status": "done" if ok else "error",
            "result": (res.get("result") or "") if ok else None,
            "error": res.get("error") if not ok else None,
        }
        if ok:
            text_len = len(res.get("result") or "")
            logger.debug(f"page {page_no} OCR ok: chars={text_len}")
        else:
            logger.error(f"page {page_no} OCR failed: {res.get('error')}")
        _saved_count += 1
        now = time.monotonic()
        if _saved_count >= len(todo_images) or now - _last_save_ts >= 2.0:
            _save_ocr_progress(img_dir, progress)
            _last_save_ts = now
            _saved_count = 0

    # Lazy imports used only when running the OCR pipeline — keep CLI (non-OCR)
    # commands working without optional deps like requests/zhconv.
    from configmanage import get_config
    from llamamanage import OCR_PROMPT, batch_infer

    # OCR 提示词优先取 config.json 的 ocr_prompt（用户可自定义），
    # 缺失/为空时回退到 llamamanage.OCR_PROMPT 默认值。
    prompt = get_config(show_dialogs=False).get("ocr_prompt") or OCR_PROMPT
    from htmlmanage import HTMLConverter
    from stringmanage import clean_and_structure_text

    if todo_images:
        t_batch = time.perf_counter()
        if use_paddle:
            # PaddleOCR 本地识别：无提示词/服务进程，接口形状与 llamamanage.batch_infer 一致
            import paddleocrmanage as _paddle

            results = _paddle.batch_infer(
                todo_images,
                [prompt] * len(todo_images),
                model_key=model_key,
                max_workers=eff_workers,
                thinking=thinking,
                timeout=timeout,
                on_progress=_on_progress,
                on_result=_on_ocr_result,
            )
        else:
            # Preload images into ImageQueue and pass ImageItem objects to batch_infer so
            # each worker can call img.get_base64() and skip repeated disk reads/encoding.
            try:
                from pdfmanage import ImageQueue
            except Exception:
                # Fallback: if pdfmanage unavailable, call batch_infer with paths as before
                results = batch_infer(
                    todo_images,
                    [prompt] * len(todo_images),
                    model_key=model_key,
                    max_workers=eff_workers,
                    thinking=thinking,
                    timeout=timeout,
                    on_progress=_on_progress,
                    on_result=_on_ocr_result,
                )
            else:
                q = ImageQueue(store_in_memory=False)
                items = [q.add(p, encode=False) for p in todo_images]
                # Pre-encode concurrently to temp files/in-memory to reduce per-worker I/O
                q.preload_all(max_workers=eff_workers or 4)
                results = batch_infer(
                    items,
                    [prompt] * len(items),
                    model_key=model_key,
                    max_workers=eff_workers,
                    thinking=thinking,
                    timeout=timeout,
                    on_progress=_on_progress,
                    on_result=_on_ocr_result,
                )

        timings["ocr"] = time.perf_counter() - t_batch
    else:
        results = []
    if total_pages == 0:
        print()

    # 合并缓存结果 + 本次识别结果（按页排序）
    merged: dict[int, dict] = {
        k: {"result": v, "error": None} for k, v in cached.items()
    }
    for r in results:
        merged[_page_of(r.get("img"))] = r
    # 排除页强制置空：即使缓存/本次识别有内容也覆盖为空，保持页码一致
    for p in excluded_pages:
        if 1 <= p <= total_pages:
            merged[p] = {"result": "", "error": None}
    print(f" done in {time.perf_counter() - t0:.1f}s")
    # OCR 全部完成：进度标记为 ocr_done 并保留文件（EPUB 生成成功后才删除）
    progress["status"] = "ocr_done"
    _save_ocr_progress(img_dir, progress)
    pages = []
    error_count = 0
    for page_no in sorted(merged):
        r = merged[page_no]
        if r.get("error"):
            print(f"      ! page {page_no} OCR failed: {r['error']}")
            error_count += 1
        pages.append({"page": page_no, "text": r.get("result") or ""})
    logger.info(f"ocr done: total_pages={total_pages}, todo={len(todo_images)}, excluded={len(excluded_pages)}, errors={error_count}, elapsed={timings.get('ocr', 0):.1f}s")

    print(f"[3/4] Structuring text ({len(pages)} page(s)) ...", end="", flush=True)
    t0 = time.perf_counter()
    structured = clean_and_structure_text(pages)
    structured["meta"] = {
        "title": title or pdf_title or pdf.stem,
        "author": author or "",
        "language": language,
        "epub_version": "3.0",
        "package_epub": True,
        "epub_path": str(Path(epub_path).resolve()) if epub_path else None,
    }
    timings["structure"] = time.perf_counter() - t0
    print(f" done in {timings['structure']:.1f}s")
    logger.info(f"structure done: elapsed={timings['structure']:.1f}s")

    root = Path(out_dir) if out_dir else img_dir

    def _post_correct(corrected, *, strict_markers: bool) -> dict:
        """矫正后的 pages → 结构化 → XHTML/EPUB；返回 convert_document 结果。"""
        _apply_correction(structured, corrected, strict_markers=strict_markers)
        t0 = time.perf_counter()
        result = HTMLConverter(
            output_dir=str(root), epub_version="3.0"
        ).convert_document(structured)
        timings["render"] = time.perf_counter() - t0
        return result

    if correct:
        print("      矫正（OCR 文字与原文对照；默认关闭，仅 --correct 时启用）")
        t0 = time.perf_counter()
        last_convert: dict[str, Any] = {}
        from correctmanage import correct_pages

        def _convert_corrected(
            corrected: list[dict[str, Any]], name: str | None = None
        ) -> dict[str, Any]:
            """「完成并转换」回调：每次点击都转换，结果回给浏览器界面。

            name（历史记录名）在此流水线中不使用——标题来自 PDF 元数据/--title。
            """
            try:
                result = _post_correct(corrected, strict_markers=True)

                last_convert["result"] = result
                return {"ok": True, "message": "转换完成", "epub": result.get("epub")}
            except Exception as e:  # 注释数量不匹配等 → 回给浏览器提示
                return {"ok": False, "message": str(e)}

        corrected = correct_pages(
            structured["pages"],
            pdf_path=pdf,
            img_dir=img_dir,
            idle_timeout=correct_idle_timeout,
            on_convert=_convert_corrected,
            # 重新识别后的新文本优先：不能用上一次暂存/保存的历史内容覆盖
            preload_history=False,
        )
        timings["correct"] = time.perf_counter() - t0
        print(f"      矫正完成 in {timings['correct']:.1f}s")
        logger.info(f"correct done: elapsed={timings['correct']:.1f}s")
        if last_convert.get("result") is not None:
            # 浏览器端已「完成并转换」过（可多次），直接用最近一次转换结果
            result = last_convert["result"]
        else:
            # 浏览器被关闭且未点过完成并转换：按已保存内容转换一次
            result = _post_correct(corrected, strict_markers=False)
    else:
        # 无矫正流程：把本次 OCR/结构化结果自动保存到历史记录（data/correction_history/）。
        # 之后可随时用 `mian.py correct [<pdf>]` 或矫正界面的「历史记录」打开该书矫正
        # （correct 命令默认 preload_history=True，自动加载同一 PDF 的最新版本）。
        t_hist = time.perf_counter()
        _save_ocr_history(pdf, structured)
        timings["history"] = time.perf_counter() - t_hist
        logger.info(f"history done: elapsed={timings['history']:.1f}s")
        print("[4/4] Rendering XHTML and packing EPUB ...", end="", flush=True)
        t0 = time.perf_counter()
        result = HTMLConverter(
            output_dir=str(root), epub_version="3.0"
        ).convert_document(structured)
        timings["render"] = time.perf_counter() - t0
        print(f" done in {timings['render']:.1f}s")
        logger.info(f"render done: elapsed={timings['render']:.1f}s")

    timings["total"] = time.perf_counter() - t_start
    _print_timing_summary(timings, ocr_pages=len(todo_images))

    # OCR 进度文件使命完成：EPUB 已生成，删除进度（避免下次误判为「未完成」）
    if result.get("epub") and not result.get("epub_error"):
        _clear_ocr_progress(img_dir)

    if result.get("epub"):
        print(f"Done: {result['epub']}")
        logger.info(f"pdf_to_epub success: epub={result['epub']}, total_elapsed={timings['total']:.1f}s")
    elif result.get("epub_error"):
        print(f"EPUB packaging failed: {result['epub_error']}", file=sys.stderr)
        logger.error(f"pdf_to_epub failed: epub_error={result['epub_error']}, total_elapsed={timings['total']:.1f}s")
    return result


# ---------------------------------------------------------------------------
# OCR 断点续传：进度文件读写 + 交互询问（继续识别 / 重新识别 / 直接转换）
# ---------------------------------------------------------------------------


def _ocr_progress_path(img_dir) -> Path:
    """OCR 进度文件位置：与分割图片同目录（data/<pdf_stem>/.ocr_progress.json）。"""
    return Path(img_dir) / ".ocr_progress.json"


def _load_ocr_progress(img_dir) -> dict | None:
    """读取 OCR 进度；文件不存在/损坏返回 None。"""
    import json

    fp = _ocr_progress_path(img_dir)
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _save_ocr_progress(img_dir, progress: dict) -> None:
    """原子写入 OCR 进度文件（tempfile + os.replace，中断不损坏文件）。"""
    import json
    import os
    import tempfile

    progress["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    fp = _ocr_progress_path(img_dir)
    fd, tmp = tempfile.mkstemp(dir=str(fp.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
        os.replace(tmp, fp)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _clear_ocr_progress(img_dir) -> None:
    """删除 OCR 进度文件（重新识别 / 转换成功时调用）。"""

    try:
        _ocr_progress_path(img_dir).unlink(missing_ok=True)
    except OSError:
        pass


def _save_ocr_history(pdf: Path, structured: dict) -> None:
    """无矫正流程：把结构化后的页面文本写入历史记录（失败不阻断转换）。

    复用 correctmanage 的历史写入函数（同文件名/载荷格式），使后续
    correct 命令/矫正界面能通过历史记录打开并矫正这本书。
    """
    import threading

    try:
        from correctmanage import (
            _history_prefix,
            _page_text,
            _write_history_version,
        )

        state = {
            "pdf_path": str(pdf),
            "pages": {
                # 与矫正界面 /api/pages 一致（2026-08-15）：所有传入矫正界面的文本
                # 一律为正文——结构化产生的 <h1>-<h6> 标题经 _page_text 归一为 <p>
                # （标题由用户在矫正界面手动标记）；纯文本才按行转 <div>（initial_html）。
                p["page"]: _page_text(p.get("text") or "")
                for p in (structured.get("pages") or [])
            },
            "history_prefix": _history_prefix(str(pdf)),
            "history_lock": threading.Lock(),
            "history_name": pdf.name,
            "proofread": {"errors": {}, "original": {}, "dismissed": {}},
            "last_proofread_page": None,
        }
        if _write_history_version(state):
            print("      已保存到历史记录（可随时用 correct 命令打开矫正）")
        else:
            print("      ! 历史记录写入失败（不影响本次转换）")
    except Exception as e:  # noqa: BLE001
        print(f"      ! 历史记录保存失败: {e}（不影响本次转换）")


def _ask_ocr_resume(progress: dict) -> str:
    """询问用户如何继续上次 OCR 进度。

    返回 'resume'（继续识别未完成页）| 'convert'（直接转换）|
    'restart'（重新识别全部）| 'abort'（取消）。
    GUI 转换子进程（PTOE_UI_PROMPT=1）下改走浏览器弹窗（_ask_ui_choice），
    不再依赖不可见的 stdin——此前子进程无控制台时该提示会卡死/取消，
    llama-server 无法启动（2026-08-17 修复）。
    """
    total = int(progress.get("total") or 0)
    pages = progress.get("pages") or {}
    done = sum(1 for v in pages.values() if v.get("status") == "done")
    failed = sum(1 for v in pages.values() if v.get("status") == "error")
    if progress.get("status") == "ocr_done":
        if _gui_prompt_mode():
            return _ask_ui_choice(
                f"检测到上次 OCR 已全部完成（{done}/{total} 页），尚未生成 EPUB。",
                [
                    ("convert", "直接继续转换"),
                    ("restart", "重新识别全部"),
                    ("abort", "取消"),
                ],
                default="convert",
            )
        print(f"\n检测到上次 OCR 已全部完成（{done}/{total} 页），尚未生成 EPUB。")
        choice = (
            _ask("选择操作：1) 直接继续转换  2) 重新识别全部  3) 取消 [1]：") or "1"
        )
        if choice == "2":
            return "restart"
        if choice == "3":
            return "abort"
        return "convert"
    extra = f"，{failed} 页失败" if failed else ""
    if _gui_prompt_mode():
        return _ask_ui_choice(
            f"检测到上次未完成的 OCR 进度（{done}/{total} 页完成{extra}）。",
            [
                ("resume", "继续识别（只处理未完成页）"),
                ("restart", "重新识别全部"),
                ("abort", "取消"),
            ],
            default="resume",
        )
    print(f"\n检测到上次未完成的 OCR 进度（{done}/{total} 页完成{extra}）。")
    choice = (
        _ask("选择操作：1) 继续识别（只处理未完成页）  2) 重新识别全部  3) 取消 [1]：")
        or "1"
    )
    if choice == "2":
        return "restart"
    if choice == "3":
        return "abort"
    return "resume"


# GUI 转换子进程的弹窗询问协议（2026-08-17）：guimanage 以 Popen + stdin=PIPE
# 启动 `mian.py epub` 并置 PTOE_UI_PROMPT=1。流程中需要用户决策（如 OCR 断点
# 续传选择）时打印 __PTOE_PROMPT__ 标记行（JSON 载荷）并读 stdin 一行；
# guimanage 监控线程截获标记后在浏览器弹窗，用户选择后写回子进程 stdin。
# EOF/非法选择回退 default，保证任何情况下不卡死。
_PROMPT_MARKER = "__PTOE_PROMPT__"
_UI_PROMPT_ENV = "PTOE_UI_PROMPT"


def _gui_prompt_mode() -> bool:
    """是否 GUI 转换子进程模式（guimanage 启动时置 PTOE_UI_PROMPT=1）。"""
    import os

    return os.environ.get(_UI_PROMPT_ENV) == "1"


def _ask_ui_choice(question: str, options: list, default: str) -> str:
    """询问用户选择：GUI 子进程模式经浏览器弹窗，终端模式走 stdin。

    options: [(value, label), ...]；default 为默认 value。
    GUI 模式：打印 __PTOE_PROMPT__ 标记行（单行 JSON，guimanage 据此弹窗），
    阻塞读 stdin 一行（GUI 写回选择）；EOF/非法选择回退 default。
    终端模式：等价原 _ask 交互（序号 1..n 对应 options，回车用默认）。
    """
    values = [v for v, _ in options]
    if not _gui_prompt_mode():
        choices = "  ".join(f"{i + 1}) {label}" for i, (_, label) in enumerate(options))
        idx = _ask(f"{question} 选择操作：{choices} [{values.index(default) + 1}]：")
        if idx.isdigit() and 1 <= int(idx) <= len(values):
            return values[int(idx) - 1]
        return default
    import json
    import sys as _sys

    payload = {
        "id": "ocr_resume",
        "question": question,
        "options": [{"value": v, "label": label} for v, label in options],
        "default": default,
    }
    print(f"\n{_PROMPT_MARKER} {json.dumps(payload, ensure_ascii=False)}", flush=True)
    try:
        line = _sys.stdin.readline()
    except Exception:
        line = ""
    choice = line.strip() if line else ""
    return choice if choice in values else default


def _ask(prompt: str) -> str:
    """打印提示并从 stdin 读一行；EOF/读取失败返回空串。"""
    try:
        print(prompt, end="", flush=True)
        line = sys.stdin.readline()
        return line.strip() if line else ""
    except Exception:
        return ""


def _pause() -> None:
    """交互模式下等待回车（打包 exe 双击启动时窗口不会立刻消失）。"""
    try:
        if getattr(sys.stdin, "isatty", lambda: False)():
            input("按回车键继续...")
    except Exception:
        pass


def _menu_epub(cfg: dict) -> None:
    """菜单项 1：交互式 PDF → EPUB 转换。"""
    pdf = _ask("PDF 路径：")
    if not pdf:
        print("已取消（未输入路径）。")
        return
    if not Path(pdf).is_file():
        print(f"错误：文件不存在 {pdf}")
        return
    model = _ask(f"模型键（默认 {cfg.get('selected_model', 'HY')}，回车用默认）：")
    if not model:
        model = cfg.get("selected_model", "HY")
    dpi = 0
    dpi_in = _ask("DPI 档位 0-4（默认 0=100，回车用默认）：")
    if dpi_in.isdigit() and int(dpi_in) in DPI_LEVELS:
        dpi = int(dpi_in)
    rec_workers = int(
        (cfg.get("model_choices") or {}).get(model, {}).get("workers") or 3
    )
    workers = rec_workers
    workers_in = _ask(f"OCR 并发数（默认 {rec_workers}（模型推荐），回车用默认）：")
    if workers_in.isdigit():
        workers = int(workers_in)
    correct = _ask("开启手动矫正（浏览器对照原图修字）？(y/N)：").lower() == "y"
    print(
        f"\n开始转换：{pdf}\n  模型={model}，dpi={DPI_LEVELS[dpi]}，并发={workers}，矫正={'开' if correct else '关'}"
    )
    try:
        result = pdf_to_epub(
            pdf,
            dpi=DPI_LEVELS[dpi],
            model_key=model,
            max_workers=workers,
            correct=correct,
        )
        if result.get("epub_error"):
            print(f"错误：{result['epub_error']}")
        else:
            print(f"完成：{result.get('epub')}")
    except Exception as e:
        print(f"错误：{e}")
    _pause()


def _menu_resume() -> None:
    """菜单项 5：继续上次中断的 OCR 转换（断点续传）。"""
    pdf = _ask("PDF 路径：")
    if not pdf:
        print("已取消（未输入路径）。")
        return
    if not Path(pdf).is_file():
        print(f"错误：文件不存在 {pdf}")
        return
    try:
        result = pdf_to_epub(pdf, resume="resume")
        if result.get("epub_error"):
            print(f"错误：{result['epub_error']}")
        elif result.get("ok") is False:
            print(f"已取消：{result.get('message') or '未转换'}")
        else:
            print(f"完成：{result.get('epub')}")
    except Exception as e:
        print(f"错误：{e}")
    _pause()


def _menu_stop() -> None:
    """菜单项 7：停止推理服务（llama-server / vLLM）。"""
    from llamamanage import _active_engine, stopserver

    eng = _active_engine()
    eng_label = "vLLM-Omni" if eng == "vllm" else "llama-server"
    stopserver()
    print(f"{eng_label} 已停止")
    _pause()


def _menu_gui() -> None:
    """菜单项 8：启动 HTML 配置操作界面（GUI）。"""
    try:
        from guimanage import gui_serve

        gui_serve()
    except Exception as e:
        print(f"错误：{e}")
    _pause()


def _menu_correct() -> None:
    """菜单项 2：直接启动手动矫正界面（不跑 OCR）。"""
    pdf = _ask("PDF 路径（留空=无文件启动，用于历史记录管理）：")
    try:
        result = correct_pdf(pdf or None)
        if result.get("epub_error"):
            print(f"错误：{result['epub_error']}")
        else:
            print(f"完成：{result.get('epub')}")
    except Exception as e:
        print(f"错误：{e}")
    _pause()


def _menu_config() -> None:
    """菜单项 3：查看/修改配置（engine / llama_server / models_dir / selected_model / browser）。"""
    from configmanage import get_config, update_config

    cfg = get_config()
    for k in ("engine", "llama_server", "models_dir", "selected_model", "browser"):
        print(f"  {k}: {cfg.get(k, '')}")
    key = _ask(
        "要修改的键（engine/llama_server/models_dir/selected_model/browser，留空跳过）："
    )
    if key not in ("engine", "llama_server", "models_dir", "selected_model", "browser"):
        print("已跳过（键名无效或为空）。")
        return
    if key == "selected_model":
        value = _ask(
            f"{key} 的新值（可选：{', '.join(cfg.get('model_choices', {}).keys())}）："
        )
    elif key == "engine":
        value = _ask(f"{key} 的新值（llama / vllm）：")
    else:
        value = _ask(f"{key} 的新值：")
    if not value:
        print("已跳过（未输入新值）。")
        return
    if key == "selected_model" and value not in cfg.get("model_choices", {}):
        print(
            f"错误：未知模型键 {value}；可用：{', '.join(cfg.get('model_choices', {}).keys())}"
        )
        return
    if key == "engine" and value not in ("llama", "vllm"):
        print("错误：engine 仅支持 llama / vllm")
        return
    update_config(key, value)
    print(f"{key} = {value}")
    _pause()


def _menu_model() -> None:
    """菜单项 4：模型管理（列出并切换默认模型）。"""
    from configmanage import get_config, update_config

    cfg = get_config()
    choices = cfg.get("model_choices", {})
    sel = cfg.get("selected_model")
    for k, v in choices.items():
        mark = "*" if k == sel else " "
        print(f"{mark} {k}: name={v.get('name')}, mmproj={v.get('mmproj')}")
    key = _ask("要切换的模型键（留空跳过）：")
    if not key:
        print("已跳过。")
        return
    if key not in choices:
        print(f"错误：未知模型键 {key}")
        return
    update_config("selected_model", key)
    print(f"selected_model 已切换为 {key}")
    _pause()


def _run_menu(name: str, version: str) -> int:
    """无参数 + 交互终端时的终端菜单（打包 exe 双击启动即进入此界面）。"""
    print(f"\n{name} {version} — PDF → OCR → EPUB 工具")
    from configmanage import get_config

    while True:
        print("\n请选择操作：")
        print("  1) PDF → EPUB 转换")
        print("  2) 矫正界面")
        print("  3) 配置信息")
        print("  4) 模型管理")
        print("  5) 中断重试 ")
        print("  6) 帮助信息")
        print("  7) 关闭引擎")
        print("  8) 配置界面")
        print("  0) 退出")
        choice = _ask("请输入序号 [0-8]：")
        if not choice:
            # EOF（管道关闭/控制台关闭）或空输入：安全退出，避免死循环
            print("已退出。")
            break
        if choice == "0":
            break
        if choice == "1":
            _menu_epub(get_config())
        elif choice == "2":
            _menu_correct()
        elif choice == "3":
            _menu_config()
        elif choice == "4":
            _menu_model()
        elif choice == "5":
            _menu_resume()
        elif choice == "6":
            print("  命令行用法（功能与菜单相同）：")
            print(
                "    mian.py epub <pdf> [--dpi 0-4] [--model KEY] [--workers N] [--thinking] [--correct] [--resume|--restart]"
            )
            print("    mian.py resume <pdf> [--restart]")
            print("    mian.py correct [<pdf>]")
            print("    mian.py config show|set <key> <value>")
            print("    mian.py model list|show|set|add|remove")
            print("    mian.py stop [--engine llama|vllm]")
            print("  详见 USAGE.md 或 mian.py <子命令> --help")
        elif choice == "7":
            _menu_stop()
        elif choice == "8":
            _menu_gui()
        else:
            print("无效输入，请输入 0-8。")
    if getattr(sys, "frozen", False):
        # 打包后的 exe 双击启动：退出前暂停，避免控制台窗口一闪而过
        _pause()
    return 0


def main(argv: list[str] | None = None) -> int:
    # S6：打包 exe 在非交互管道下 stdout/stderr 可能是 GBK 编码，
    # 遇到无法编码的字符（如 emoji、特殊符号）会抛 UnicodeEncodeError 直接崩溃；
    # errors="replace" 保证任何输出都不会因编码问题中断。
    # 2026-08-25：实测 PyInstaller 冻结子进程忽略 PYTHONIOENCODING/PYTHONUTF8
    # 环境变量（GUI 父进程注入无效），管道下 stdout 落到系统 ANSI（GBK），
    # GUI 父进程按 UTF-8 解码即乱码；且管道下 stdout 全缓冲，日志成块延迟到达
    # （界面显示不完整）。故非 tty（管道/重定向）时强制 UTF-8 + 行缓冲；
    # tty（双击 exe 的控制台菜单）保持系统编码，避免控制台代码页不匹配。
    for _s in (sys.stdout, sys.stderr):
        try:
            if not _s.isatty():
                _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            else:
                _s.reconfigure(errors="replace")
        except Exception:
            pass

    # 初始化文件日志（仅文件，不影响控制台 print）
    from logmanage import setup_logging, logger

    setup_logging()
    logger.info(f"=== ptoe start: {sys.argv} ===")

    name, version = _read_meta()
    # Load persistent config early so the CLI default for --model follows the
    # user's selected_model in config.json. get_config() is robust and will
    # auto-create/repair config.json if necessary (interactive prompts are
    # suppressed in headless environments).
    from configmanage import get_config, set_llama_server_arg, update_config

    cfg = get_config()
    default_model = cfg.get("selected_model", "HY")

    parser = argparse.ArgumentParser(
        prog=name,
        description="ptoe: PDF -> OCR -> EPUB conversion tool",
    )
    parser.add_argument("-e", "--echo", help="Echo text to stdout", default=None)
    parser.add_argument(
        "--version", action="store_true", help="Print project version and exit"
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    epub_p = sub.add_parser(
        "epub",
        help="Convert a PDF to an EPUB file",
        description="PDF -> images -> OCR -> XHTML -> EPUB",
    )
    epub_p.add_argument("pdf", help="Path to the source PDF")
    epub_p.add_argument(
        "--dpi",
        type=int,
        choices=sorted(DPI_LEVELS),
        default=0,
        help="DPI level 0-4: 0=100, 1=150, 2=200, 3=300, 4=600 (default: 0=100)",
    )
    epub_p.add_argument(
        "--model",
        default=default_model,
        help="Model key in config.json model_choices (default: from config.json)",
    )
    epub_p.add_argument(
        "--engine",
        choices=("llama", "vllm", "paddle"),
        default=None,
        help="推理引擎：llama（llama.cpp，默认）或 vllm（vLLM-Omni）；缺省用 config.json 的 engine 键",
    )

    epub_p.add_argument(
        "--workers",
        type=int,
        default=None,
        help="OCR worker threads (default: 模型推荐并发 model_choices.<key>.workers，未配置时 3；视觉模型每张数千图像 token，并发过高会让 KV 缓存溢出到 CPU 反而变慢；显存充足可调大如 6)",
    )
    epub_p.add_argument(
        "--timeout",
        type=int,
        default=REQUEST_TIMEOUT,
        help="Per-request read timeout in seconds (default: 600)",
    )
    epub_p.add_argument(
        "--thinking",
        action="store_true",
        help="Pass the prompt through without appending the '按原文原格式输出' suffix",
    )
    epub_p.add_argument(
        "--title", default=None, help="EPUB title (default: auto from PDF metadata)"
    )
    epub_p.add_argument("--author", default=None, help="EPUB author")
    epub_p.add_argument(
        "--lang", default="zh-CN", help="EPUB language code (default: zh-CN)"
    )
    epub_p.add_argument(
        "--out-dir",
        default=None,
        help="Output directory for OEBPS/ and the EPUB (default: data/<pdf stem>/)",
    )
    epub_p.add_argument(
        "--epub-path", default=None, help="Explicit output path for the .epub file"
    )
    epub_p.add_argument(
        "--correct",
        action="store_true",
        help="开启手动矫正：在浏览器中逐页对照原图与识别文字，可标记粗体/斜体/标题（默认关闭）",
    )
    epub_p.add_argument(
        "--correct-timeout",
        type=int,
        default=600,
        help="浏览器被关闭后自动继续后续流程的等待秒数（仅 --correct 生效；默认 600=10 分钟）",
    )
    epub_p.add_argument(
        "--exclude",
        default=None,
        help="跳过指定页码的 OCR 识别（如 1-15,17,20），优先于配置文件 exclude_pages",
    )
    _resume_group = epub_p.add_mutually_exclusive_group()
    _resume_group.add_argument(
        "--resume",
        action="store_true",
        help="继续上次中断的 OCR（跳过询问：只识别未完成页；OCR 已完成则直接转换）",
    )
    _resume_group.add_argument(
        "--restart",
        action="store_true",
        help="忽略已有 OCR 进度，重新识别全部页面（跳过询问）",
    )

    correct_p = sub.add_parser(
        "correct",
        help="直接启动手动矫正界面（不跑 OCR；可无文件启动）",
        description="直接打开手动矫正界面：不运行 OCR；页面文本优先取本地历史缓存"
        "最新版本（同一 PDF 上次矫正/暂存的内容），无历史则为空白页。点「完成并转换」"
        "时生成 EPUB，可留在页面继续修改后再次点击。不带 PDF 参数时为无文件启动"
        "（空白界面，用于历史记录管理/手动录入）。",
    )
    correct_p.add_argument(
        "pdf",
        nargs="?",
        default=None,
        help="Path to the source PDF（可省略：无文件直接启动，用于历史记录管理）",
    )
    correct_p.add_argument(
        "--engine",
        choices=("llama", "vllm", "paddle"),
        default=None,
        help="推理引擎：llama（llama.cpp，默认）或 vllm（vLLM-Omni）；缺省用 config.json 的 engine 键",
    )
    correct_p.add_argument(
        "--title", default=None, help="EPUB title (default: auto from PDF metadata)"
    )
    correct_p.add_argument("--author", default=None, help="EPUB author")
    correct_p.add_argument(
        "--lang", default="zh-CN", help="EPUB language code (default: zh-CN)"
    )
    correct_p.add_argument(
        "--out-dir",
        default=None,
        help="Output directory for OEBPS/ and the EPUB (default: data/<pdf stem>/)",
    )
    correct_p.add_argument(
        "--epub-path", default=None, help="Explicit output path for the .epub file"
    )
    correct_p.add_argument(
        "--correct-timeout",
        type=int,
        default=600,
        help="浏览器被关闭后自动继续后续流程的等待秒数（默认 600=10 分钟）",
    )

    resume_p = sub.add_parser(
        "resume",
        help="继续/管理上次中断的 OCR 转换（断点续传）",
        description="针对上次 OCR 中断/未完成的 PDF 继续处理：只识别未完成页"
        "（OCR 已全部完成则直接进入转换），交互询问或 --restart 强制重来。"
        "无进度时询问是否从头完整转换。",
    )
    resume_p.add_argument("pdf", help="Path to the source PDF")
    resume_p.add_argument(
        "--dpi",
        type=int,
        choices=sorted(DPI_LEVELS),
        default=0,
        help="DPI level 0-4: 0=100, 1=150, 2=200, 3=300, 4=600 (default: 0=100)",
    )
    resume_p.add_argument(
        "--model",
        default=default_model,
        help="Model key in config.json model_choices (default: from config.json)",
    )
    resume_p.add_argument(
        "--engine",
        choices=("llama", "vllm", "paddle"),
        default=None,
        help="推理引擎：llama（llama.cpp，默认）或 vllm（vLLM-Omni）；缺省用 config.json 的 engine 键",
    )
    resume_p.add_argument(
        "--workers",
        type=int,
        default=None,
        help="OCR worker threads (default: 模型推荐并发 model_choices.<key>.workers，未配置时 3；显存充足可调大如 6)",
    )
    resume_p.add_argument(
        "--timeout",
        type=int,
        default=REQUEST_TIMEOUT,
        help="Per-request read timeout in seconds (default: 600)",
    )
    resume_p.add_argument(
        "--thinking",
        action="store_true",
        help="Pass the prompt through without appending the '按原文原格式输出' suffix",
    )
    resume_p.add_argument(
        "--title", default=None, help="EPUB title (default: auto from PDF metadata)"
    )
    resume_p.add_argument("--author", default=None, help="EPUB author")
    resume_p.add_argument(
        "--lang", default="zh-CN", help="EPUB language code (default: zh-CN)"
    )
    resume_p.add_argument(
        "--out-dir",
        default=None,
        help="Output directory for OEBPS/ and the EPUB (default: data/<pdf stem>/)",
    )
    resume_p.add_argument(
        "--epub-path", default=None, help="Explicit output path for the .epub file"
    )
    resume_p.add_argument(
        "--correct",
        action="store_true",
        help="开启手动矫正（默认关闭；同 epub --correct）",
    )
    resume_p.add_argument(
        "--correct-timeout",
        type=int,
        default=600,
        help="浏览器被关闭后自动继续后续流程的等待秒数（仅 --correct 生效；默认 600=10 分钟）",
    )
    resume_p.add_argument(
        "--exclude",
        default=None,
        help="跳过指定页码的 OCR 识别（如 1-15,17,20），优先于配置文件 exclude_pages",
    )
    resume_p.add_argument(
        "--restart",
        action="store_true",
        help="忽略已有进度，重新识别全部页面（跳过询问）",
    )

    stop_p = sub.add_parser(
        "stop",
        help="停止推理服务（llama-server / vLLM-Omni）",
        description="关闭正在运行的推理服务进程并释放端口：本进程启动的实例直接终止，"
        "上次运行遗留/外部启动的实例按配置端口兜底关闭（Windows netstat+taskkill）。",
    )
    stop_p.add_argument(
        "--engine",
        choices=("llama", "vllm", "paddle"),
        default=None,
        help="推理引擎：llama（llama.cpp，默认）或 vllm（vLLM-Omni）；缺省用 config.json 的 engine 键",
    )

    # Model registry management: list / show / set the configured default model
    model_p = sub.add_parser(
        "model",
        help="Model registry commands (list/show/set/add/remove)",
        description="Manage available OCR model choices and the persistent selected model",
    )
    model_sub = model_p.add_subparsers(dest="model_cmd", metavar="action")

    model_list_p = model_sub.add_parser(
        "list", help="List available model keys and details"
    )
    model_show_p = model_sub.add_parser(
        "show", help="Show current selected model key and detail"
    )
    model_set_p = model_sub.add_parser(
        "set", help="Set selected model key in config.json"
    )
    model_set_p.add_argument("key", help="Model key to select (e.g. HY, QWEN.8)")

    model_add_p = model_sub.add_parser(
        "add", help="Add a model choice (key + name + mmproj)"
    )
    model_add_p.add_argument("key", help="Model key to add (e.g. MY)")
    model_add_p.add_argument(
        "--name",
        required=True,
        help="Model file name (relative to models_dir or full path)",
    )
    model_add_p.add_argument(
        "--mmproj",
        required=True,
        help="mmproj file name (relative to models_dir or full path)",
    )
    model_add_p.add_argument(
        "--force", action="store_true", help="Overwrite existing key if present"
    )

    model_remove_p = model_sub.add_parser("remove", help="Remove a model choice")
    model_remove_p.add_argument("key", help="Model key to remove")
    # alias: rm
    model_rm_p = model_sub.add_parser("rm", help="Alias for remove")
    model_rm_p.add_argument("key", help="Model key to remove")

    # ---- config 子命令：快捷查看/修改配置路径 ----
    config_p = sub.add_parser(
        "config",
        help="查看或修改配置（llama_server / models_dir / selected_model 等）",
        description="快捷查看或修改 config.json 中的配置项",
    )
    config_sub = config_p.add_subparsers(dest="config_cmd", metavar="action")

    config_set_p = config_sub.add_parser("set", help="修改配置项（key=value）")
    config_set_p.add_argument(
        "key",
        help="配置键名（llama_server / models_dir / selected_model / ocr_prompt / engine / vllm_server / browser / llama_server_args.<参数> / vllm_server_args.<参数> / proofread.<param>）",
    )
    config_set_p.add_argument("value", help="配置值")

    gui_p = sub.add_parser(
        "gui",
        help="启动 HTML 配置操作界面（浏览器）",
        description="启动本地 HTTP 服务并在浏览器中打开配置操作界面：查看/修改配置、"
        "启动/停止推理服务、选择文件路径等。浏览器关闭超过 idle-timeout 秒后自动退出。",
    )
    gui_p.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    gui_p.add_argument(
        "--port", type=int, default=0, help="监听端口（默认 0=自动分配）"
    )
    gui_p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    gui_p.add_argument(
        "--idle-timeout",
        type=int,
        default=120,
        help="浏览器关闭后自动退出的等待秒数（默认 120）",
    )
    args = parser.parse_args(argv)

    def _find_model_key(choices: dict | None, key: str) -> str | None:
        """Return canonical key from choices by case-insensitive match.

        Rules:
        - If `key` exactly exists in choices -> return it.
        - Else look for case-insensitive matches (k.lower() == key.lower()).
          - If exactly one match -> return that canonical key.
          - If multiple matches -> ambiguous -> return None (caller treats as unknown/ambiguous).
        - If no match -> return None.
        """
        if not isinstance(choices, dict) or not isinstance(key, str) or not key:
            return None
        if key in choices:
            return key
        key_l = key.lower()
        matches = [
            k for k in choices.keys() if isinstance(k, str) and k.lower() == key_l
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    if args.command == "model":
        cmd = getattr(args, "model_cmd", None)
        # read fresh config for each model action so changes are immediately visible
        cfg = get_config()
        if cmd == "list":
            choices = cfg.get("model_choices", {})
            sel = cfg.get("selected_model")
            for k, v in choices.items():
                mark = "*" if k == sel else " "
                name = v.get("name")
                mmproj = v.get("mmproj")
                print(f"{mark} {k}: name={name}, mmproj={mmproj}")
            return 0
        if cmd == "show":
            sel = cfg.get("selected_model")
            if not sel:
                print("No selected model configured")
                return 1
            v = cfg.get("model_choices", {}).get(sel, {})
            print(f"selected_model: {sel}")
            print(f"  name: {v.get('name')}")
            print(f"  mmproj: {v.get('mmproj')}")
            return 0
        if cmd == "set":
            key = getattr(args, "key", None)
            choices = dict(cfg.get("model_choices", {}))
            canonical = _find_model_key(choices, key)
            if canonical is None:
                # ambiguous or unknown
                matches = [
                    k
                    for k in choices.keys()
                    if isinstance(k, str)
                    and isinstance(key, str)
                    and k.lower() == (key.lower() if isinstance(key, str) else "")
                ]
                if len(matches) > 1:
                    print(
                        f"Error: ambiguous model key: '{key}' matches {matches} - use exact key or remove duplicates",
                        file=sys.stderr,
                    )
                else:
                    print(f"Error: unknown model key: {key}", file=sys.stderr)
                return 1
            cfg = update_config("selected_model", canonical)
            print(f"selected_model set to {canonical}")
            return 0

        if cmd == "add":
            key = getattr(args, "key", None)
            name = getattr(args, "name", None)
            mmproj = getattr(args, "mmproj", None)
            force = getattr(args, "force", False)
            choices = dict(cfg.get("model_choices", {}))
            if not isinstance(key, str) or not key:
                print(f"Error: invalid model key: {key}", file=sys.stderr)
                return 1
            matches = [
                k for k in choices if isinstance(k, str) and k.lower() == key.lower()
            ]
            if matches and not force:
                print(
                    f"Error: model key already exists: {matches[0]} (case-insensitive match) (use --force to overwrite)",
                    file=sys.stderr,
                )
                return 1
            if matches:
                existing_key = next((k for k in matches if k == key), matches[0])
                choices[existing_key] = {"name": name, "mmproj": mmproj}
                out_key = existing_key
            else:
                choices[key] = {"name": name, "mmproj": mmproj}
                out_key = key
            cfg2 = update_config("model_choices", choices)
            print(f"Model '{out_key}' added/updated.")
            return 0

        if cmd in ("remove", "rm"):
            key = getattr(args, "key", None)
            choices = dict(cfg.get("model_choices", {}))
            canonical = _find_model_key(choices, key)
            if canonical is None:
                matches = [
                    k
                    for k in choices.keys()
                    if isinstance(k, str)
                    and isinstance(key, str)
                    and k.lower() == key.lower()
                ]
                if len(matches) > 1:
                    print(
                        f"Error: ambiguous model key: '{key}' matches {matches} - use exact key or remove duplicates",
                        file=sys.stderr,
                    )
                else:
                    print(f"Error: unknown model key: {key}", file=sys.stderr)
                return 1
            old_sel = cfg.get("selected_model")
            choices.pop(canonical, None)
            cfg2 = update_config("model_choices", choices)
            print(f"Model '{canonical}' removed.")
            new_sel = cfg2.get("selected_model")
            if old_sel != new_sel:
                print(f"selected_model changed from {old_sel} to {new_sel}")
            return 0

        # unknown subcommand
        print("Unknown model action; use 'model list|show|set|add|remove <args>'")
        return 1

    if args.command == "config":
        cmd = getattr(args, "config_cmd", None)
        cfg = get_config()
        if cmd == "show":
            for k in ("engine", "llama_server", "models_dir", "selected_model"):
                print(f"  {k}: {cfg.get(k, '')}")
            print(f"  vllm_server: {cfg.get('vllm_server', '')}")
            print(f"  browser: {cfg.get('browser', '')}")
            sel = cfg.get("selected_model")
            print("  model_choices:")
            for mk, mv in (cfg.get("model_choices", {}) or {}).items():
                mark = "*" if mk == sel else " "
                print(
                    f"    {mark}{mk}: name={mv.get('name')}, mmproj={mv.get('mmproj')}"
                )
            print(f"  ocr_prompt: {cfg.get('ocr_prompt', '')}")
            sargs = cfg.get("llama_server_args", {}) or {}
            print("  llama_server_args:")
            for ak, av in sargs.items():
                print(f"    {ak}: {av}")
            vsargs = cfg.get("vllm_server_args", {}) or {}
            print("  vllm_server_args:")
            for ak, av in vsargs.items():
                print(f"    {ak}: {av}")
            return 0
        if cmd == "set":
            key = getattr(args, "key", None) or ""
            value = getattr(args, "value", None)
            if key.startswith("llama_server_args."):
                # 嵌套参数（如 llama_server_args.parallel）走 set_llama_server_arg
                nested = key.split(".", 1)[1]
                set_llama_server_arg(nested, value)
                print(f"{key} = {value}")
                return 0
            if key.startswith("vllm_server_args."):
                # vllm 启动参数（如 vllm_server_args.port）走 set_vllm_server_arg
                nested = key.split(".", 1)[1]
                from configmanage import set_vllm_server_arg

                set_vllm_server_arg(nested, value)
                print(f"{key} = {value}")
                return 0
            if key.startswith("proofread."):
                # proofread.<param> 专门路由到 set_proofread_param
                sub = key.split(".", 1)[1]
                from configmanage import set_proofread_param

                set_proofread_param(sub, value)
                print(f"{key} = {value}")
                return 0
            if key not in (
                "llama_server",
                "models_dir",
                "selected_model",
                "ocr_prompt",
                "engine",
                "vllm_server",
                "browser",
            ):
                print(
                    "Error: 可修改的键名仅限 llama_server / models_dir / selected_model / ocr_prompt / engine / vllm_server / browser / llama_server_args.<参数名> / vllm_server_args.<参数名> / proofread.<param>",
                    file=sys.stderr,
                )
                return 1
            if key == "selected_model" and value not in cfg.get("model_choices", {}):
                print(
                    f"Error: 未知的 model key: {value}（可用: {', '.join(cfg.get('model_choices', {}).keys())}）",
                    file=sys.stderr,
                )
                return 1
            if key == "engine" and value not in ("llama", "vllm"):
                print("Error: engine 仅支持 llama / vllm", file=sys.stderr)
                return 1
            update_config(key, value)
            print(f"{key} = {value}")
            return 0
        print("Unknown config action; use 'config show|set <key> <value>'")
        return 1

    if args.command == "gui":
        # 惰性导入：guimanage 仅在本命令用到时加载（不引入额外依赖）
        from guimanage import gui_serve

        gui_serve(
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
            idle_timeout=args.idle_timeout,
        )
        return 0

    if args.command == "stop":
        if args.engine == "paddle":
            # PaddleOCR 为本地推理引擎（无外部服务进程），无需停止
            print("PaddleOCR 为本地推理引擎，无需停止服务")
            return 0
        try:
            from llamamanage import _active_engine, set_engine, stopserver

            if args.engine:
                set_engine(args.engine)
            eng = _active_engine()
            eng_label = "vLLM-Omni" if eng == "vllm" else "llama-server"
            stopserver()
            print(f"{eng_label} 已停止")
            return 0
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    if args.command == "epub":
        try:
            _apply_engine_arg(args.engine)
            result = pdf_to_epub(
                args.pdf,
                dpi=DPI_LEVELS[args.dpi],
                model_key=args.model,
                max_workers=args.workers,
                thinking=args.thinking,
                timeout=args.timeout,
                title=args.title,
                author=args.author,
                language=args.lang,
                out_dir=args.out_dir,
                epub_path=args.epub_path,
                correct=args.correct,
                correct_idle_timeout=args.correct_timeout,
                resume=(
                    "resume" if args.resume else ("restart" if args.restart else None)
                ),
                exclude=args.exclude,
            )
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        if result.get("epub_error"):
            return 1
        return 0
    if args.command == "resume":
        try:
            _apply_engine_arg(args.engine)
            result = pdf_to_epub(
                args.pdf,
                dpi=DPI_LEVELS[args.dpi],
                model_key=args.model,
                max_workers=args.workers,
                thinking=args.thinking,
                timeout=args.timeout,
                title=args.title,
                author=args.author,
                language=args.lang,
                out_dir=args.out_dir,
                epub_path=args.epub_path,
                correct=args.correct,
                correct_idle_timeout=args.correct_timeout,
                resume="restart" if args.restart else "resume",
                exclude=args.exclude,
            )
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        if result.get("epub_error"):
            return 1
        return 0
    if args.command == "correct":
        if args.engine == "paddle":
            # PaddleOCR 仅用于 PDF 识别流程；文本矫正仍使用大模型引擎
            print(
                "PaddleOCR 仅用于 PDF 识别流程；文本矫正仍使用大模型引擎，已忽略 --engine paddle"
            )
        else:
            _apply_engine_arg(args.engine)
        try:
            result = correct_pdf(
                args.pdf,
                title=args.title,
                author=args.author,
                language=args.lang,
                out_dir=args.out_dir,
                epub_path=args.epub_path,
                idle_timeout=args.correct_timeout,
            )
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        if result.get("epub_error"):
            return 1
        return 0
    if args.echo is not None:
        print(args.echo)
        return 0

    # 无参数：交互终端（含打包 exe 双击启动）进入终端菜单；非交互保持原行为
    if getattr(sys.stdin, "isatty", lambda: False)():
        return _run_menu(name, version)
    print(f"{name} {version} — nothing to do")
    return 0


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
