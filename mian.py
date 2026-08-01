"""ptoe entrypoint: PDF -> images -> OCR -> XHTML -> EPUB pipeline.

CLI:
  python mian.py [--version]
  python mian.py -e TEXT
  python mian.py epub <pdf> [--dpi 0] [--model HY] [--workers 3] [--thinking]
                    [--title TITLE] [--author AUTHOR] [--lang zh-CN]
                    [--out-dir DIR] [--epub-path PATH]

--dpi 为档位（0-4）而非原始数值：0=100, 1=150, 2=200, 3=300, 4=600（默认 0）。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import tomllib
from pathlib import Path

from pdfmanage import split_pdf_to_images
from llamamanage import batch_infer, runserver, REQUEST_TIMEOUT
from stringmanage import clean_and_structure_text
from htmlmanage import HTMLConverter

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
    if not runserver(model_key):
        raise RuntimeError(
            "llama-server failed to start; check llama_server/models_dir in config.json"
        )


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
) -> dict:
    """Convert a PDF to EPUB by chaining the pipeline modules.

    Steps: split_pdf_to_images -> batch_infer (llama-server OCR) ->
    clean_and_structure_text -> HTMLConverter.convert_document (XHTML + EPUB pack).
    OCR results are sorted by page number before structuring (batch_infer
    returns futures in completion order, not page order).
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
    prompts = ["请识别图片中的文字内容"] * len(img_paths)
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

    print(f"[4/4] Rendering XHTML and packing EPUB ...", end="", flush=True)
    t0 = time.perf_counter()
    root = Path(out_dir) if out_dir else img_dir
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
        "--model", default="HY", help="Model key in config.json model_choices (default: HY)"
    )
    epub_p.add_argument("--workers", type=int, default=3, help="OCR worker threads (default: 3)")
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

    args = parser.parse_args(argv)

    if args.version:
        print(f"{name} {version}")
        return 0
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
