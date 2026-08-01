# Repository Guidelines

---

## Project Overview

**ptoe** is a Python utility for splitting PDFs into images, driving local OCR via llama.cpp, and converting OCR text to XHTML/EPUB. It provides a CLI (`mian.py`), PDF/fitz utilities (`pdfmanage.py`), llama.cpp server orchestration (`llamamanage.py`), config CRUD with tkinter dialog helpers (`configmanage.py`), zh-CN/zh-TW string conversion (`stringmanage.py`), and XHTML/EPUB output (`htmlmanage.py`, `epubmanage.py`). Packaged as flat Python scripts at repo root, not a Python package. **Not a git repo.** Windows-oriented (llama-server paths, tkinter dialogs). Inline comments/docstrings are predominantly zh-CN; match that style when editing.


## Architecture & Data Flow

- **Entrypoint:** `mian.py` — minimal CLI plus the wired PDF→EPUB pipeline. Top-level: `-e/--echo`, `--version`; with no args prints `"<name> <version> — nothing to do"` and exits 0. Subcommand `epub <pdf>` runs `pdf_to_epub()`: `split_pdf_to_images` → `batch_infer` (OCR) → `clean_and_structure_text` → `HTMLConverter.convert_document` (which packs the EPUB via `meta['package_epub']=True`). OCR results are sorted by page number (extracted from the image filename via `_page_of`) because `batch_infer` returns futures in completion order, not page order. Options: `--dpi` (level 0-4: 0=100, 1=150, 2=200, 3=300, 4=600; default 0=100 — CLI maps to the raw value before calling `pdf_to_epub`, whose `dpi` param stays a raw int), `--model`, `--workers` (default 3), `--thinking`, `--title`, `--author`, `--lang`, `--out-dir`, `--epub-path`. Reads name/version from pyproject.toml via tomllib. pyproject.toml currently declares name `ptoe`, version `0.1.0`.
- **PDF Pipeline:** `split_pdf_to_images(pdf_path, *, dpi=200, fmt="png")` (in `pdfmanage.py`; **`dpi`/`fmt` are keyword-only**) → creates `data/<pdf_stem>/` via PyMuPDF (`fitz`); page images are named `1.png`, `2.png`, ... (1-based); `fmt="jpg"` saves with `jpg_quality=100`. `createdic()` appends an incremental suffix (`_1`, `_2`, ...) if the target dir already exists. `is_pdf_file()` reads a 1 KB prefix and searches for `%PDF-` anywhere in it (tolerates BOM/whitespace/comment preamble — do NOT "simplify" to a first-5-bytes check). Password-protected PDFs raise `RuntimeError` (empty-password `authenticate` attempt, then error).
- **Image queue:** `pdfmanage.ImageItem` / `ImageQueue` — thread-safe FIFO for images; base64 is written to a temp file by default (`store_in_memory=True` keeps it in RAM; `get_base64()` materializes lazily from the temp file).
- **OCR Pipeline:** `llamamanage.py` starts llama-server.exe (path from config) with `-m <model> --mmproj <mmproj> --host 127.0.0.1 --port 8080 --temperature 0 --repeat-penalty 1.1 --parallel 4` and POSTs to `http://127.0.0.1:8080/v1/chat/completions` (OpenAI-compatible) via `requests`; health is polled at `/health` (120s timeout). `request_image` posts OpenAI-style multimodal messages: `content` is a list of `{"type": "text", "text": prompt}` and `{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}` blocks — **not** a native `images:` field (see `ocr_compare.txt`). The image request sends **no `stop` sequence** (a `stop: ["\n\n"]` used to truncate every multi-paragraph page at the first blank line; the model ends naturally on EOS). **Prompts always get `"按原文原格式输出"` appended unless `thinking=True`.** Per-request timeout is `REQUEST_TIMEOUT = 600` (a 300dpi page becomes ~8700 image tokens, 200dpi ~4600; the old 60s timeout killed every request under 4-way parallelism). `runserver` auto-detects GPU backends via `_detect_gpu` (`llama-server --list-devices`) and adds `--n-gpu-layers 999` when a CUDA/Vulkan/ROCm/Metal device is found, plus `--cache-type-k q8_0 --cache-type-v q8_0` to fit the 4-slot KV cache in VRAM (tested: RTX 3070 Laptop 8GB). Also `batch_infer` (ThreadPoolExecutor, `max_workers=3` by default, `timeout` threaded through, optional `on_progress(done, total)` callback invoked per completed page) logs per-page timings (`[OCR] <path> done in <dt>s (<chars> chars)`), and `stopserver` (terminate → 10s wait → kill). Module-global `_server_process` holds the subprocess handle. `mian._ensure_server` pre-checks `/health` (3s timeout) and only starts llama-server when it isn't already answering.
- **Configuration:** `configmanage.py` is the single config source. `get_config()` auto-creates/repairs `config.json` (thread-safe via `threading.Lock`, missing/invalid fields patched from `DEFAULT_CONFIG`). Also `update_config`, legacy aliases (`getconfig`, `creatconfig`, `saveconfig`), and tkinter file/dir dialogs. Config keys: `llama_server`, `models_dir`, `model_choices` (registry: `HY`, `QWEN.8`, `QWEN2`, `QWEN4`, `PD`, `ULQ8`, `ULQ4`), `selected_model` (default `HY`).
- **String Conversion:** `stringmanage.py` — `ttos` (Trad→Simp), `stot` (Simp→Trad), and `clean_and_structure_text` (strips `<think>` tags, normalizes whitespace, splits paragraphs, optional 繁简 conversion; returns `{pages, body, paragraphs}`). Honors per-page `to_simplified`/`to_traditional` flags; asserts you cannot request both conversions at once.
- **Output:** `htmlmanage.py` (`HTMLConverter`/`CSSManager`/`HTMLValidator` → OEBPS dir) feeds `epubmanage.py` (`EPUBPacker`, EPUB2/3, `mimetype` first and stored uncompressed via `ZIP_STORED`). `HTMLConverter.convert_document(structured_doc)` expects keys `pages`, `body`, `paragraphs`, optional `titles` and `meta` (`title`/`author`/`cover_image`/`language`/`package_epub`/`epub_path`/`epub_version`). It copies local `<img src=...>` files into `OEBPS/Images/` and rewrites srcs to relative paths; **the .epub is only packaged when `meta['package_epub']` is truthy** (result dict then gets an `epub` or `epub_error` key). `pack_from_oebps(root_dir, epub_path, metadata, epub_version='2.0')` builds the spine as cover.xhtml → nav.xhtml → content_*.xhtml (sorted); an NCX TOC is generated for EPUB2 only. Inside the .epub, `ResourceMapper` re-maps files into `OEBPS/Text/`, `OEBPS/Styles/`, `OEBPS/Images/` subfolders even though `htmlmanage` writes them flat under `OEBPS/` (`test_mian` asserts `OEBPS/Text/content_1.xhtml`).

No CI type checking, linting, async, DI, or explicit state management. `llamamanage.py` uses a module-global `_server_process`; threading (not async) in pdfmanage/llamamanage.


## Key Directories

- **Project Root:** All source and test files here (no src/ layout)
- **data/** — Output dirs created by `createdic()` for generated images per PDF (e.g. `data/主席与毛远新同志谈话纪要/`)
- **.venv/** — Local virtual environment (created by `uv`)
- **.opencode/**, **.crush/**, **.idea/** — editor/tool state; ignore. `.opencode/memory/project.md` mirrors this file; keep them in sync
- **config.json** — Created/updated at runtime by `configmanage.py` (already present with the model registry)


## Important Files
| File | Purpose |
|------|---------|
| `mian.py` | CLI entrypoint: `-e/--echo`, `--version`, `epub <pdf>` subcommand + `pdf_to_epub()` pipeline. Typo filename; do NOT rename |
| `pdfmanage.py` | PDF → image splitting + `ImageItem`/`ImageQueue`. Key: `cpath`, `createdic`, `is_pdf_file`, `split_pdf_to_images`. Exports via `__all__` |
| `llamamanage.py` | Manages llama-server.exe: `run`, `check`, `runserver`, `stopserver`, `request`, `request_image`, `batch_infer` |
| `configmanage.py` | Config read/write (`get_config`/`update_config`), tkinter dialogs |
| `stringmanage.py` | zhconv wrapper — `ttos`, `stot`, `clean_and_structure_text` |
| `htmlmanage.py` | structured text → XHTML/OEBPS (HTMLConverter, CSSManager, HTMLValidator) |
| `epubmanage.py` | OEBPS → .epub (EPUBPacker, EPUBMetadata, ResourceMapper, `pack_from_oebps`) |
| `test_pdfmanage.py` | unittest suite covering all pdfmanage exports (can also run directly) |
| `test_mian.py` | unittest for the wired `pdf_to_epub` pipeline: generates a temp PDF, monkeypatches `mian.batch_infer`, asserts a valid EPUB (mimetype first, ZIP_STORED, page order preserved) |
| `test_image_queue_request.py` | Script-style check (plain asserts, monkeypatches `requests.post`) — run via `python test_image_queue_request.py` |
| `test_config_llama.py` | pytest-style; requires pytest (NOT in venv) |
| `pyproject.toml` | Metadata (name/version/reqs), deps, Python ≥3.11, `[tool.pyrefly]` |
| `uv.lock` | Dependency lockfile (generated by uv) |
| `config.json` | Created/updated at runtime by `configmanage.py` (already present with the model registry) |


## Development Commands

- **Sync deps:**
  ```sh
  uv sync
  ```
- **Run CLI:**
  ```sh
  uv run python mian.py [--version] [-e TEXT]
  uv run python mian.py epub <pdf> [--dpi 0] [--model HY] [--workers 3] [--title TITLE]
  ```
- **Run tests:**
  ```sh
  uv run python -m unittest test_pdfmanage   # passes: 9 tests
  uv run python -m unittest test_mian        # passes: pipeline wiring (mocked OCR)
  uv run python test_image_queue_request.py  # prints "test passed"; see Gotchas
  uv run python test_config_llama.py         # fails until `uv add --dev pytest` is run
  ```

No build, lint, or type-check commands defined. No makefile or task runner present. No CI, pre-commit, or formatter config.

**Static checking:** `pyproject.toml` has a `[tool.pyrefly]` section (`project-includes` for `**/*.py*`, `**/*.ipynb`). Pyrefly runs in the editor LSP and reports **pre-existing** diagnostics — do not treat them as regressions from your changes:

- `llamamanage.py:46` — `models_dir may be uninitialized` (the `'models_dir' in locals()` guard in `check()` is a pyrefly-blind hack; `models_dir` is only bound inside the `if not llama:` branch)
- `configmanage.py:156/161` — values passed to `os.path.isfile`/`isdir` without narrowing (the dict-typed `models_dir` variant)
- `epubmanage.py` / `htmlmanage.py` — a handful of `LiteralString`/`None` assignability complaints (e.g. the `src` kwarg, result-dict keys `epub`/`epub_error`)
- `test_image_queue_request.py` — `json` param typed `None` in the fake `post`


## Code Conventions & Common Patterns

- **File layout:** Flat, all code at repo root, not installable as a package
- **Entrypoint:** Always use `mian.py` (deliberate typo, do NOT rename)
- **Exports:** `pdfmanage.py` uses `__all__` to mark its public API
- **PDF output:** All outputs go to `data/<name>/`
- **Config-driven paths:** `llamamanage.py` reads the llama-server.exe path and models dir from `config.json` (via `get_config()`); defaults point to `E:/xox/Tools/llama-c/` (Windows only)
- **Error handling:** Standard Python exceptions; error paths rarely tested. OCR functions return `{"result": None, "error": str(e)}` dicts instead of raising
- **State:** No dependency injection — module globals in llamamanage.py; threading.Lock in configmanage.py
- **Comments:** Inline comments/docstrings are mostly zh-CN; match that when writing new code

## Runtime/Tooling Preferences
- **Python version:** ≥3.11 (pyproject.toml)
- **Package manager:** `uv` (`pyproject.toml`, `uv.lock`)
- **Dependencies:** pymupdf, requests, zhconv — nothing else (pytest NOT included)
- **Platform:** llama-server paths are Windows-specific
- **No linter/formatter/typechecker**: Write idiomatic Python only — no CI will check.


## Testing & QA
- **Framework:** stdlib `unittest` only; pytest is not installed
- **Tests:** `test_pdfmanage.py` (all pdfmanage exports, **9 tests, currently passing**), `test_image_queue_request.py` (plain script, prints "test passed"), `test_config_llama.py` (pytest-style, will fail without pytest)
- **Utilities:** `test_pdfmanage.py` uses `tempfile` & `shutil.rmtree`; honors `TEST_PDF_PATH` env var, falls back to a hardcoded `E:\MYBooks\...` PDF, then to a generated 3-page PDF
- **Coverage:** Core pdfmanage exports covered; error paths not tested


## Gotchas

- **`request_image` is defined TWICE in `llamamanage.py`**: the implementation at ~line 196 is dead code, shadowed at the bottom by `request_image = _request_image_new` (line 319), a duck-typed version accepting any object with a callable `get_base64()`. Edit the *new* function; do not delete the rebinding.
- **`ocr_compare.txt`** at repo root records the payload-format fix: the legacy `images:`-field request echoed back garbage OCR text (`按原文原格式输出`), while the current `image_url` content-block variant (B) works. Do not revert the payload format.
- **`config.json` diverges from `DEFAULT_CONFIG` on this machine**: `models_dir` is `E:/model` and `model_choices` names carry relative subdir prefixes (e.g. `huiyuan/HunyuanOCR.BF16.gguf`); `check()`/`runserver()` join them via `os.path.join(models_dir, name)`.
- **`get_config()` pops interactive tkinter file/dir chooser dialogs** when `llama_server`/`models_dir` are missing or point at non-existent paths, and blocks until dismissed. Every `llamamanage` call path (including `test_image_queue_request.py`) triggers `get_config()`, so on a desktop the script **hangs on the dialog** if the `E:/xox/...` paths don't exist on the machine — point `config.json` at valid paths first, or run headless.
- `llamamanage.py:46` — `check()` relies on a `'models_dir' in locals()` guard; if `llama` is passed in, `models_dir` is never bound and the fallback `os.path.dirname(llama)` is used.
- `createdic()` in `pdfmanage.py` uses incremental suffix (`_1`, `_2`) if the target dir exists
- OCR prompts always append `"按原文原格式输出"` unless `thinking=True`
- `stringmanage.clean_and_structure_text` asserts you can't request both simplified and traditional conversion at once
- No root `.gitignore` (only `.idea/`, `.venv/`, `.opencode/` carry their own)

---

> This document is auto-generated; update if project structure or conventions change.
