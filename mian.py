"""ptoe entrypoint: PDF -> images -> OCR -> XHTML -> EPUB pipeline.

CLI:
  python mian.py [--version]
  python mian.py -e TEXT
  python mian.py epub <pdf> [--dpi 0] [--model HY] [--workers 3] [--thinking]
                    [--title TITLE] [--author AUTHOR] [--lang zh-CN]
                    [--out-dir DIR] [--epub-path PATH] [--correct]

--dpi 为档位（0-4）而非原始数值：0=100, 1=150, 2=200, 3=300, 4=600（默认 0）。
--correct 开启手动矫正：在浏览器中逐页对照原图与识别文字（默认关闭）。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import tomllib
from pathlib import Path
from typing import Any, Dict, List

from pdfmanage import split_pdf_to_images
# Defer importing llamamanage (requests dependency) to runtime paths that need OCR.
# Provide a safe fallback for REQUEST_TIMEOUT so CLI help and flags work.
REQUEST_TIMEOUT = 600

_PAGE_RE = re.compile(r"(\d+)(?:\.\w+)?$")

# 5 档 DPI：档位 -> 实际分辨率。档位越高图片 token 越多（约线性）、识别越精细但越慢。
DPI_LEVELS = {0: 100, 1: 150, 2: 200, 3: 300, 4: 600}

# 进度条宽度（字符）
_BAR_WIDTH = 24


def _read_meta() -> tuple[str, str]:
    """Return (name, version) from pyproject.toml or sensible defaults."""
    path = Path(__file__).with_name("pyproject.toml")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        proj = data.get("project", {})
        return proj.get("name", "ptoe"), proj.get("version", "0.0.0")
    except Exception:
        return "ptoe", "0.0.0"


def _page_of(image) -> int:
    """Extract the 1-based page number from an image path or filename."""
    m = _PAGE_RE.search(str(image))
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


def _ensure_server(model_key: str) -> None:
    """Start llama-server if it is not already answering on 127.0.0.1:8080."""
    import requests

    try:
        resp = requests.get("http://127.0.0.1:8080/health", timeout=3)
        if resp.status_code == 200:
            print("      llama-server already running")
            return
    except Exception:
        pass
    print(f"      starting llama-server (model='{model_key}') ...")
    # import runserver lazily to avoid pull-in of requests when not needed
    from llamamanage import runserver

    if not runserver(model_key):
        raise RuntimeError(
            "llama-server failed to start; check llama_server/models_dir in config.json"
        )


def _apply_correction(structured: dict, corrected: List[Dict[str, Any]], *, strict_markers: bool) -> None:
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
        "data-ptoe-marker" in (p.get("text") or "") or "ptoe-note" in (p.get("text") or "")
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
    pages: List[Dict[str, Any]] = [{"page": n, "text": ""} for n in range(1, total + 1)]
    structured: Dict[str, Any] = {"pages": pages, "body": "", "paragraphs": []}
    structured["meta"] = {
        "title": title or pdf_title or (pdf.stem if pdf else "未命名"),
        "author": author or "",
        "language": language,
        "epub_version": "3.0",
        "package_epub": True,
        "epub_path": str(Path(epub_path).resolve()) if epub_path else None,
    }
    root = Path(out_dir) if out_dir else (Path("data") / pdf.stem if pdf else Path("data"))
    root.mkdir(parents=True, exist_ok=True)

    last_convert: Dict[str, Any] = {}

    def _convert_corrected(
        corrected: List[Dict[str, Any]], name: str | None = None
    ) -> Dict[str, Any]:
        """「完成并转换」回调：每次点击都转换，结果回给浏览器界面。

        无文件模式（pdf 为 None）同样支持转换：只要内容非空（打开历史记录
        或手动录入的文本）即可生成 EPUB。name 为浏览器传来的历史记录名
        （无文件模式下用作 EPUB 标题，除非命令行已指定 --title）。
        """
        if not corrected or not any((p.get("text") or "").strip() for p in corrected):
            return {"ok": False, "message": "没有可转换的内容（请先录入或打开历史记录）"}
        if name and not title:
            structured["meta"]["title"] = name
        try:
            _apply_correction(structured, corrected, strict_markers=True)
            result = HTMLConverter(output_dir=str(root), epub_version="3.0").convert_document(structured)
            last_convert["result"] = result
            return {"ok": True, "message": "转换完成", "epub": result.get("epub")}
        except Exception as e:  # 注释数量不匹配等 → 回给浏览器提示
            return {"ok": False, "message": str(e)}

    print("      矫正（直接启动：不跑 OCR；历史缓存优先，空白页可手动录入）")
    corrected = correct_pages(
        pages, pdf_path=pdf, img_dir=None,
        idle_timeout=idle_timeout,
        on_convert=_convert_corrected,
    )
    if last_convert.get("result") is not None:
        # 浏览器端已「完成并转换」过（可多次），直接用最近一次转换结果
        result = last_convert["result"]
    elif any((p.get("text") or "").strip() for p in corrected):
        # 浏览器被关闭且未点过完成并转换：有内容则按已保存内容补转一次
        _apply_correction(structured, corrected, strict_markers=False)
        result = HTMLConverter(output_dir=str(root), epub_version="3.0").convert_document(structured)
    else:
        print("      未产生转换（没有可转换的内容）")
        return {"content_files": []}

    if result.get("epub"):
        print(f"Done: {result['epub']}")
    elif result.get("epub_error"):
        print(f"EPUB packaging failed: {result['epub_error']}", file=sys.stderr)
    return result


def pdf_to_epub(
    pdf_path: str | Path,
    *,
    dpi: int = 100,
    model_key: str = "HY",
    max_workers: int = 3,
    thinking: bool = False,
    timeout: int = REQUEST_TIMEOUT,
    title: str | None = None,
    author: str | None = None,
    language: str = "zh-CN",
    out_dir: str | Path | None = None,
    epub_path: str | Path | None = None,
    correct: bool = False,
    correct_idle_timeout: int = 600,
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

    print(f"[1/4] Splitting PDF to images (dpi={dpi}) ...", end="", flush=True)
    t0 = time.perf_counter()
    img_dir, img_paths = split_pdf_to_images(pdf, dpi=dpi, fmt="png")
    print(f" done in {time.perf_counter() - t0:.1f}s")
    print(f"      {len(img_paths)} page(s) -> {img_dir}")

    print(f"[2/4] OCR via llama-server (model='{model_key}', workers={max_workers}) ...", end="", flush=True)
    t0 = time.perf_counter()
    _ensure_server(model_key)
    total_pages = len(img_paths)
    t_ocr = time.perf_counter()

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

    # Lazy imports used only when running the OCR pipeline — keep CLI (non-OCR)
    # commands working without optional deps like requests/zhconv.
    from llamamanage import batch_infer, OCR_PROMPT
    prompts = [OCR_PROMPT] * len(img_paths)
    from stringmanage import clean_and_structure_text
    from htmlmanage import HTMLConverter

    results = batch_infer(
        img_paths,
        prompts,
        model_key=model_key,
        max_workers=max_workers,
        thinking=thinking,
        timeout=timeout,
        on_progress=_on_progress,
    )
    if total_pages == 0:
        print()
    print(f" done in {time.perf_counter() - t0:.1f}s")
    pages = []
    for r in sorted(results, key=lambda r: _page_of(r.get("img"))):
        page_no = _page_of(r.get("img"))
        if r.get("error"):
            print(f"      ! page {page_no} OCR failed: {r['error']}")
        pages.append({"page": page_no, "text": r.get("result") or ""})

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
    print(f" done in {time.perf_counter() - t0:.1f}s")

    root = Path(out_dir) if out_dir else img_dir

    def _post_correct(corrected, *, strict_markers: bool) -> dict:
        """矫正后的 pages → 结构化 → XHTML/EPUB；返回 convert_document 结果。"""
        _apply_correction(structured, corrected, strict_markers=strict_markers)
        return HTMLConverter(output_dir=str(root), epub_version="3.0").convert_document(structured)

    if correct:
        print("      矫正（OCR 文字与原文对照；默认关闭，仅 --correct 时启用）")
        t0 = time.perf_counter()
        last_convert: Dict[str, Any] = {}
        from correctmanage import correct_pages


        def _convert_corrected(
            corrected: List[Dict[str, Any]], name: str | None = None
        ) -> Dict[str, Any]:
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
            structured["pages"], pdf_path=pdf, img_dir=img_dir,
            idle_timeout=correct_idle_timeout,
            on_convert=_convert_corrected,
            # 重新识别后的新文本优先：不能用上一次暂存/保存的历史内容覆盖
            preload_history=False,
        )
        print(f"      矫正完成 in {time.perf_counter() - t0:.1f}s")
        if last_convert.get("result") is not None:
            # 浏览器端已「完成并转换」过（可多次），直接用最近一次转换结果
            result = last_convert["result"]
        else:
            # 浏览器被关闭且未点过完成并转换：按已保存内容转换一次
            result = _post_correct(corrected, strict_markers=False)
    else:
        print(f"[4/4] Rendering XHTML and packing EPUB ...", end="", flush=True)
        t0 = time.perf_counter()
        result = HTMLConverter(output_dir=str(root), epub_version="3.0").convert_document(structured)
        print(f" done in {time.perf_counter() - t0:.1f}s")

    print(f"Total: {time.perf_counter() - t_start:.1f}s")

    if result.get("epub"):
        print(f"Done: {result['epub']}")
    elif result.get("epub_error"):
        print(f"EPUB packaging failed: {result['epub_error']}", file=sys.stderr)
    return result


def main(argv: list[str] | None = None) -> int:
    name, version = _read_meta()
    # Load persistent config early so the CLI default for --model follows the
    # user's selected_model in config.json. get_config() is robust and will
    # auto-create/repair config.json if necessary (interactive prompts are
    # suppressed in headless environments).
    from configmanage import get_config, update_config
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
    epub_p.add_argument("--model", default=default_model, help="Model key in config.json model_choices (default: from config.json)")

    epub_p.add_argument(
        "--workers", type=int, default=3,
        help="OCR worker threads (default: 3；视觉模型每张数千图像 token，并发过高会让 KV 缓存溢出到 CPU 反而变慢；显存充足可调大如 6)",
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
    epub_p.add_argument("--title", default=None, help="EPUB title (default: auto from PDF metadata)")
    epub_p.add_argument("--author", default=None, help="EPUB author")
    epub_p.add_argument("--lang", default="zh-CN", help="EPUB language code (default: zh-CN)")
    epub_p.add_argument(
        "--out-dir",
        default=None,
        help="Output directory for OEBPS/ and the EPUB (default: data/<pdf stem>/)",
    )
    epub_p.add_argument("--epub-path", default=None, help="Explicit output path for the .epub file")
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

    correct_p = sub.add_parser(
        "correct",
        help="直接启动手动矫正界面（不跑 OCR；可无文件启动）",
        description="直接打开手动矫正界面：不运行 OCR；页面文本优先取本地历史缓存"
        "最新版本（同一 PDF 上次矫正/暂存的内容），无历史则为空白页。点「完成并转换」"
        "时生成 EPUB，可留在页面继续修改后再次点击。不带 PDF 参数时为无文件启动"
        "（空白界面，用于历史记录管理/手动录入）。",
    )
    correct_p.add_argument(
        "pdf", nargs="?", default=None,
        help="Path to the source PDF（可省略：无文件直接启动，用于历史记录管理）",
    )
    correct_p.add_argument("--title", default=None, help="EPUB title (default: auto from PDF metadata)")
    correct_p.add_argument("--author", default=None, help="EPUB author")
    correct_p.add_argument("--lang", default="zh-CN", help="EPUB language code (default: zh-CN)")
    correct_p.add_argument(
        "--out-dir",
        default=None,
        help="Output directory for OEBPS/ and the EPUB (default: data/<pdf stem>/)",
    )
    correct_p.add_argument("--epub-path", default=None, help="Explicit output path for the .epub file")
    correct_p.add_argument(
        "--correct-timeout",
        type=int,
        default=600,
        help="浏览器被关闭后自动继续后续流程的等待秒数（默认 600=10 分钟）",
    )

    # Model registry management: list / show / set the configured default model
    model_p = sub.add_parser(
        "model",
        help="Model registry commands (list/show/set/add/remove)",
        description="Manage available OCR model choices and the persistent selected model",
    )
    model_sub = model_p.add_subparsers(dest="model_cmd", metavar="action")

    model_list_p = model_sub.add_parser("list", help="List available model keys and details")
    model_show_p = model_sub.add_parser("show", help="Show current selected model key and detail")
    model_set_p = model_sub.add_parser("set", help="Set selected model key in config.json")
    model_set_p.add_argument("key", help="Model key to select (e.g. HY, QWEN.8)")

    model_add_p = model_sub.add_parser("add", help="Add a model choice (key + name + mmproj)")
    model_add_p.add_argument("key", help="Model key to add (e.g. MY)")
    model_add_p.add_argument("--name", required=True, help="Model file name (relative to models_dir or full path)")
    model_add_p.add_argument("--mmproj", required=True, help="mmproj file name (relative to models_dir or full path)")
    model_add_p.add_argument("--force", action="store_true", help="Overwrite existing key if present")

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

    config_show_p = config_sub.add_parser("show", help="显示当前配置")
    config_set_p = config_sub.add_parser("set", help="修改配置项（key=value）")
    config_set_p.add_argument("key", help="配置键名（如 llama_server, models_dir, selected_model）")
    config_set_p.add_argument("value", help="新的值")

    args = parser.parse_args(argv)

    if args.version:
        print(f"{name} {version}")
    # Handle model management commands (list/show/set)
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
            if key not in cfg.get("model_choices", {}):
                print(f"Error: unknown model key: {key}", file=sys.stderr)
                return 1
            cfg = update_config("selected_model", key)
            print(f"selected_model set to {key}")
            return 0
        if cmd == "add":
            key = getattr(args, "key", None)
            name = getattr(args, "name", None)
            mmproj = getattr(args, "mmproj", None)
            force = getattr(args, "force", False)
            choices = dict(cfg.get("model_choices", {}))
            if key in choices and not force:
                print(f"Error: model key already exists: {key} (use --force to overwrite)", file=sys.stderr)
                return 1
            choices[key] = {"name": name, "mmproj": mmproj}
            cfg2 = update_config("model_choices", choices)
            print(f"Model '{key}' added/updated.")
            return 0
        if cmd in ("remove", "rm"):
            key = getattr(args, "key", None)
            choices = dict(cfg.get("model_choices", {}))
            if key not in choices:
                print(f"Error: unknown model key: {key}", file=sys.stderr)
                return 1
            old_sel = cfg.get("selected_model")
            choices.pop(key)
            cfg2 = update_config("model_choices", choices)
            print(f"Model '{key}' removed.")
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
            for k in ("llama_server", "models_dir", "selected_model"):
                print(f"  {k}: {cfg.get(k, '')}")
            return 0
        if cmd == "set":
            key = getattr(args, "key", None)
            value = getattr(args, "value", None)
            if key not in ("llama_server", "models_dir", "selected_model"):
                print(f"Error: 可修改的键名仅限 llama_server / models_dir / selected_model", file=sys.stderr)
                return 1
            if key == "selected_model" and value not in cfg.get("model_choices", {}):
                print(f"Error: 未知的 model key: {value}（可用: {', '.join(cfg.get('model_choices', {}).keys())}）", file=sys.stderr)
                return 1
            update_config(key, value)
            print(f"{key} = {value}")
            return 0
        print("Unknown config action; use 'config show|set <key> <value>'")
        return 1

    if args.command == "epub":
        try:
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
            )
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        if result.get("epub_error"):
            return 1
        return 0
    if args.command == "correct":
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

    print(f"{name} {version} — nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
