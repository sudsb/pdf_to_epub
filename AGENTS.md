# Repository Guidelines

---

## Project Overview

**ptoe** is a Python utility for splitting PDFs into images, driving local OCR via llama.cpp (llama-server), and converting OCR text to XHTML/EPUB. It provides a CLI (`mian.py`), PDF/fitz utilities (`pdfmanage.py`), llama-server orchestration (`llamamanage.py`), config CRUD with tkinter dialog helpers (`configmanage.py`), zh-CN/zh-TW string conversion (`stringmanage.py`), an optional browser-based manual-correction stage (`correctmanage.py`), and XHTML/EPUB output (`htmlmanage.py`, `epubmanage.py`). Packaged as flat Python scripts at repo root, not a Python package. **Git repo** (single init commit). Windows-oriented (llama-server paths, tkinter dialogs). Inline comments/docstrings are predominantly zh-CN; match that style when editing.

## Architecture & Data Flow

- **Entrypoint:** `mian.py` — argparse CLI. Top-level: `-e/--echo`, `--version`; no args prints `"<name> <version> — nothing to do"` and exits 0. Subcommands: `epub <pdf>` (full OCR→EPUB pipeline) and `correct [<pdf>]` (direct correction UI without OCR). Name/version read from `pyproject.toml` via tomllib (`_read_meta()`; fallback `ptoe`/`0.0.0`). Exit codes: 0 success; epub exception or `epub_error` in result → stderr `Error: {e}` + exit 1.
- **PDF Pipeline:** `split_pdf_to_images` (`pdfmanage.py`; `dpi`/`fmt` are keyword-only) → `data/<pdf_stem>/` via PyMuPDF (`fitz`); page images 1-based (`1.png`, `2.png`…); **相同 PDF（sha256 一致）+ 相同 dpi/fmt 时复用已有分割图片** via `.ptoe_split.json` marker. `fitz` is lazily imported → `RuntimeError` if missing. Password-protected PDFs: tries empty-password auth, then raises. `createdic()` appends `_1`, `_2`… if dir exists (base = script dir, not cwd). `is_pdf_file()` searches for `%PDF-` anywhere in first 1 KB (not first-5-bytes).
- **Image queue:** `ImageItem` / `ImageQueue` (`pdfmanage.py`) — thread-safe FIFO. `ImageItem` base64 lazily materialized to temp file; `ImageQueue` supports `add/add_many/preload_all/get_next/peek/size/clear`.
- **OCR Pipeline:** `llamamanage.py` — module-global `_server_process`. `runserver()` starts llama-server.exe (config-driven) with GPU detection (`CUDA|Vulkan|ROCm|Metal` via `--list-devices`) and **`--log-verbosity 0`** (只输出 ERROR 级日志，屏蔽 find_slot/print_timing 刷屏；进度由 ptoe 侧打印). POSTs to OpenAI-compatible `/v1/chat/completions` via shared `requests.Session` (keep-alive). `_request_image_new` posts multimodal `content` blocks (text + image_url with base64 data URI) — **not** native `images:` field (see `ocr_compare.txt`). Payload includes **`max_tokens: MAX_TOKENS (=4096)`** and **`chat_template_kwargs: {"enable_thinking": thinking}`** — Qwen3 系模型默认隐藏思考链开启，OCR 时大部分 token 花在推理甚至死循环（实测单请求 4 万+ token、15s/页）；关思考后 100dpi 单页 ≈392 tokens、**~2-3s/页**（从 ~15s 提升）。非 Qwen 模板忽略该键，无副作用。**mian.py 默认 OCR prompt 是完整性提示词** `"请逐行完整识别图片中的全部文字，逐字输出，不得遗漏任何内容、不得省略、不得总结、不得翻译"`（0.8B 模型跳字靠它缓解，实测 +20% 字符）；**Prompts always get `"\n按原文原格式输出"` appended unless `thinking=True`**；`finish_reason=length`（触顶 max_tokens 截断）会打印 WARNING 提示检查该页。 `batch_infer` uses ThreadPoolExecutor; **config/model resolved once per batch** (avoids per-page `get_config()` lock contention + tkinter popups); **default max_workers=3** — visual models use ~4600-8700 image tokens/page; higher concurrency causes KV spill to CPU, slowing individual requests. Per-item failures captured in result dict (not raised). Server outlives the CLI; `stopserver()` to stop.
- **Configuration:** `configmanage.py` — `get_config()` auto-creates/repairs `config.json` (CWD-relative, thread-safe lock, **rewrites on every call**). Keys: `llama_server`, `models_dir`, `model_choices` (registry of `{name, mmproj}` objects: `HY`, `QWEN.8`, `QWEN2`, `QWEN4`, `PD`, `ULQ8`, `ULQ4`), `selected_model` (default `HY`). tkinter dialogs pop when paths are missing/invalid (headless → `TclError` swallowed, no prompt).
- **String Conversion:** `stringmanage.py` — `ttos`/`stot` (zhconv wrappers) and `clean_and_structure_text` (strips `<think>`, collapses whitespace, splits paragraphs, optional 繁简; returns `{pages, body, paragraphs}`). Asserts cannot request both simplified and traditional at once. Per-page auto-processing inside `clean_and_structure_text` (before whitespace normalization): **`convert_bbox_text`** — PaddleOCR/ULQ4/ULQ8 输出格式 `label [x,y,x,y] text` 自动转换（`title`→`<h2>`、其他→`<p>`、`page_number`/`figure`/`image` 丢弃、非 bbox 行保留原样；内容 html-escape）; **`strip_page_numbers`** — 默认删除页首/页尾的独立页码行（`第N页`、`- 4 -`、`— 5 —`、`6 / 12`、纯数字），页面中间的数字与 4 位年份保留（保守策略）。
- **Manual Correction (optional, `--correct` only):** `correctmanage.py` — `correct_pages()` starts a local `ThreadingHTTPServer` with embedded HTML UI (left=image preview from PDF, right=contenteditable text one `<div>` per line). Blocks until browser closes > `idle_timeout` (default 600s) or 完成并转换. **Browser-close monitor:** heartbeat every 30s + `pagehide` beacon; auto-continues pipeline once browser gone > `idle_timeout` (avoids infinite hang). `preload_history=False` skips history cache — `epub --correct` passes this (fresh OCR wins over staged content); `correct <pdf>` keeps default (re-correct saved content). Versioned history cache at `data/correction_history/` (20 versions per file, preloaded on start). Markers (`全文`, `段落`, `注释标记`) inserted as `<span data-ptoe-marker="...">` at caret; `apply_markers` converts them to `structured['articles']` for EPUB. `sanitize_html` whitelist: `<p>/<h1-6>/<strong>/<em>/<br/>/<span>` (with `data-ptoe-marker` + `class`), attributes stripped except `class="ptoe-note"`/`ptoe-align-*`. Also supports: Markdown source mode, 繁简 `/api/convert`, rebindable keyboard shortcuts, 虚拟列表 for 1000+ pages.
- **Output:** `htmlmanage.py` — `HTMLConverter.convert_document` creates `OEBPS/` + `OEBPS/Images/`, writes CSS, cover, content (merged or per-page chapters), and `nav.xhtml` TOC. **The .epub is only packaged when `meta['package_epub']` is truthy** via `pack_from_oebps`.
- **EPUB:** `epubmanage.py` — `EPUBPacker` builds `META-INF/` + `OEBPS/Text|Styles|Images`, zip writes `mimetype` FIRST with `ZIP_STORED`. `toc.ncx` for EPUB2 only. Spine order: `cover.xhtml` → `nav.xhtml` → `content_*.xhtml` sorted by `_natural_key` (number-aware).

## Key Directories

- **Project Root:** All source and test files here (no src/ layout); also `config.json`, `pyproject.toml`, `uv.lock`, `USAGE.md`, `AGENTS.md`, `ocr_compare.txt`
- **data/** — Output dirs created by `createdic()` per PDF (e.g. `data/《毛泽东思想万岁》武汉钢二司编 第四卷/`). `1.png` 页图在相同输入下可跨运行复用（避免重复切图）；最终 .epub 也写入该目录 (e.g. `毛泽东思想万岁（1958—1960）.epub`, ~1.2 MB). `data/correction_history/<sha1>_<时间戳>_<随机>.json` holds versioned per-PDF corrected pages (one version per 保存/暂存/完成, capped 20/file, latest preloaded next run; managed via the 历史记录 dialog)
- **.venv/** — Local virtual environment (created by `uv`)
- **.opencode/**, **.crush/**, **.idea/**, **__pycache__/** — editor/tool state; ignore. `.opencode/memory/project.md` mirrors this file; keep them in sync
- **config.json** — Created/updated at runtime by `configmanage.py` (already present with the model registry)

## Development Commands

- **Sync deps:**
  ```sh
  uv sync
  ```
- **CLI:**
  ```sh
  uv run python mian.py [--version] [-e TEXT]
  uv run python mian.py epub <pdf> [--dpi 0] [--model HY] [--workers 3] [--timeout 600] [--title TITLE] [--author AUTHOR] [--lang zh-CN] [--out-dir DIR] [--epub-path PATH] [--thinking] [--correct] [--correct-timeout 600]
  uv run python mian.py correct [<pdf>] [--title TITLE] [--author AUTHOR] [--lang zh-CN] [--out-dir DIR] [--epub-path PATH] [--correct-timeout 600]
  uv run python mian.py config show                    # 查看当前配置
  uv run python mian.py config set llama_server <path> # 快捷修改 llama_server 路径
  uv run python mian.py config set models_dir <path>   # 快捷修改 models_dir 路径
  uv run python mian.py model list|show|set|add|remove  # 模型注册表管理
  ```
- **Run tests:**
  ```sh
  uv run python -m unittest test_pdfmanage   # passes: 9 tests
  uv run python -m unittest test_mian        # passes: pipeline wiring (mocked OCR)
  uv run python -m unittest test_correctmanage  # passes: sanitizer + markup rendering + --correct wiring
  uv run python -m unittest test_llamamanage  # passes: batch_infer config resolution + session reuse
  uv run python test_image_queue_request.py  # prints "test passed"; see Gotchas
  uv run python test_config_llama.py         # fails until `uv add --dev pytest` is run
  ```
- **Stop a leftover llama-server:**
  ```sh
  uv run python -c "from llamamanage import stopserver; stopserver()"
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
- **Exports:** `pdfmanage.py` uses `__all__` (`cpath`, `createdic`, `is_pdf_file`, `split_pdf_to_images`, `ImageItem`, `ImageQueue`) to mark its public API
- **PDF output:** All outputs go to `data/<name>/`
- **Config-driven paths:** `llamamanage.py` reads the llama-server.exe path and models dir from `config.json` (via `get_config()`); defaults point to `E:/xox/Tools/llama-c/` (Windows only)
- **Error handling:** Standard Python exceptions; error paths rarely tested. OCR functions return `{"result": None, "error": str(e)}` dicts instead of raising; `batch_infer` captures per-item failures rather than propagating
- **State:** No dependency injection — module-global `_server_process` in llamamanage.py; threading.Lock in configmanage.py
- **Concurrency:** threading (Lock, ThreadPoolExecutor), never asyncio
- **Comments:** Inline comments/docstrings are mostly zh-CN; match that when writing new code
- **CLI failure convention:** errors print `Error: {e}` to stderr, exit code 1

## Important Files

|File|Purpose|
|---|---|
|`mian.py`|CLI entrypoint: `-e/--echo`, `--version`, `epub <pdf>` + `correct <pdf>` subcommands; `pdf_to_epub()` pipeline, `correct_pdf()` direct correction UI, `_apply_correction()` shared post-correction structuring. Typo filename; do NOT rename|
|`pdfmanage.py`|PDF → image splitting + `ImageItem`/`ImageQueue`. Key: `cpath`, `createdic`, `is_pdf_file`, `split_pdf_to_images`. Exports via `__all__`|
|`llamamanage.py`|Manages llama-server.exe: `run`, `check`, `runserver`, `stopserver`, `request`, `request_image`, `batch_infer`|
|`configmanage.py`|Config read/write (`get_config`/`update_config`), tkinter dialogs, legacy aliases|
|`stringmanage.py`|zhconv wrapper — `ttos`, `stot`, `clean_and_structure_text`|
|`correctmanage.py`|可选手动矫正：本地 HTTP 服务 + 内嵌 HTML 界面（左图右文、虚拟列表、标记/标题/对齐/注释、繁简、Markdown 模式、历史缓存）。`correct_pages`/`sanitize_html`/`apply_markers`/`initial_html`。仅 `--correct`/`correct` 命令启用|
|`htmlmanage.py`|structured text → XHTML/OEBPS (HTMLConverter, CSSManager, HTMLValidator)；`_render_fragment` 白名单标记渲染 + 标题锚点 + 标题目录收集；`structured['articles']` → 每篇一个内容页|
|`epubmanage.py`|OEBPS → .epub (EPUBPacker, EPUBMetadata, ResourceMapper, `pack_from_oebps`)；`pack_from_oebps` 接受 htmlmanage 的标题目录（href 可带 #锚点，映射时保留 fragment）；EPUB2 `toc.ncx` 用同一目录|
|`USAGE.md`|zh-CN user tutorial for the epub pipeline (commands, flags, server lifecycle)|
|`ocr_compare.txt`|Records the OCR payload-format fix (see Gotchas)|
|`test_pdfmanage.py`|unittest suite covering all pdfmanage exports (9 tests, passing)|
|`test_mian.py`|unittest for the wired `pdf_to_epub` pipeline (mocked OCR)|
|`test_correctmanage.py`|unittest for correctmanage (sanitize_html, markup rendering, `--correct` wiring, browser-close monitor)|
|`test_llamamanage.py`|unittest for llamamanage (batch_infer config resolution + session reuse + single-failure handling)|
|`test_stringmanage.py`|unittest for stringmanage (bbox conversion, page-number stripping, clean_and_structure integration)|
|`test_image_queue_request.py`|Script-style check (plain asserts, monkeypatches `requests.post`)|
|`test_config_llama.py`|pytest-style; requires pytest (NOT in venv)|
|`pyproject.toml`|Metadata (name `ptoe`/version `0.1.0`), deps, Python ≥3.11, `[tool.pyrefly]`|
|`uv.lock`|Dependency lockfile (generated by uv; locks pymupdf 1.28.0, requests 2.34.2, zhconv 1.4.3)|
|`config.json`|Runtime config — auto-created/repaired by `configmanage.py`|

## Runtime/Tooling Preferences

- **Python version:** ≥3.11 (pyproject.toml)
- **Package manager:** `uv` (`pyproject.toml`, `uv.lock`)
- **Dependencies:** pymupdf, requests, zhconv — nothing else (pytest NOT included)
- **Platform:** llama-server paths are Windows-specific; tkinter dialogs on Windows desktop
- **No linter/formatter/typechecker**: Write idiomatic Python only — no CI will check. Pyrefly via editor LSP only

## Testing & QA

- **Framework:** stdlib `unittest` only; pytest is not installed. `test_image_queue_request.py` is a plain script, `test_config_llama.py` is pytest-style
- **test_pdfmanage.py** (4 classes / 9 tests, passing): covers `createdic`, `is_pdf_file`, `cpath`, `split_pdf_to_images`. Uses `tempfile.mkdtemp` & `shutil.rmtree`; honors `TEST_PDF_PATH` env var, falls back to a hardcoded `E:\MYBooks\books\毛泽东思想\主席与毛远新同志谈话纪要.pdf` if it exists, then to a generated 3-page fitz temp PDF named `test_split`; skips if fitz is missing; scrubs leftover `data/<name>` dirs in setUp
- **test_mian.py** (class `TestPdfToEpub`, 2 tests, passing): generates a temp PDF, monkeypatches `llamamanage.batch_infer` and `mian._ensure_server` (mian lazy-imports batch_infer), asserts a valid EPUB — mimetype first + `ZIP_STORED`, `META-INF/container.xml`, `OEBPS/content.opf`, single merged `OEBPS/Text/content_1.xhtml` with page order preserved, spine idrefs present in manifest, manifest hrefs exist in the zip; plus a `_natural_key` sort test. tearDown scrubs `data/sample*`
- **test_correctmanage.py** (tests, passing): `sanitize_html` whitelist (incl. marker span + `ptoe-note` class + `ptoe-align-*` alignment classes), `initial_html` line→`<div>` preservation, `apply_markers` (join merge across pages / 段首·段尾·段中 join / chapter `<h2>` / full article split / marker-only blocks / 注释替换·合并·括号归一·数量不匹配抛错 / alignment class 保留), `_render_fragment` markup + marker-strip + heading anchors + align classes, `convert_document` with `articles` → one content file per article, `convert_text_html`（繁简只转文本节点）+ `/api/convert` 端点（无状态、非法 mode 400）、`_history_pages_for_init`（preload_history 开关）、`pdf_to_epub(correct=True)` wiring, browser-close monitor (`_browser_gone` + heartbeat/gone endpoints + repeat-finish `on_convert` + 暂存写历史缓存 + 历史列表多版本/选中删除/全部删除). Run together: `uv run python -m unittest test_pdfmanage test_mian test_correctmanage test_llamamanage test_stringmanage` → 108 tests
- **test_llamamanage.py** (unittest, passing): covers `batch_infer` config resolution (model resolved once per batch, not per-page), `requests.Session` reuse across pages, and single-failure-doesn't-abort-batch behavior. Run: `uv run python -m unittest test_llamamanage`
- **test_stringmanage.py** (20 tests, passing): bbox conversion (title→`<h2>`, text→`<p>`, page_number/figure/image dropped, non-bbox lines kept, html-escape), page-number stripping (首/尾行 `第N页`/`- 4 -`/`— 5 —`/`6 / 12`/纯数字，中间与 4 位年份保留), clean_and_structure integration. Run: `uv run python -m unittest test_stringmanage`
- **test_image_queue_request.py**: script-style; fakes `requests.post` and asserts the OpenAI vision payload shape (`content[1]['image_url']['url'] == 'data:image/png;base64,...'`); run via `uv run python test_image_queue_request.py`; prints `test passed`
- **test_config_llama.py**: pytest-style (2 module-level test functions); fails with `ModuleNotFoundError: pytest` until `uv add --dev pytest`; also triggers `get_config()` (tkinter dialogs)
- **Coverage:** Core pdfmanage exports + wired pipeline covered; error paths and OCR request handling not unit-tested

## Gotchas

- **`request_image` is defined TWICE in `llamamanage.py`**: the implementation at ~line 239 is dead code (hardcodes `timeout=60`, `stop: ["\n\n"]`, assumes base64 input), shadowed at the bottom (line ~379) by `request_image = _request_image_new` (line ~334), a duck-typed version accepting any object with a callable `get_base64()` and threading `timeout` through. Edit the *new* function; do not delete the rebinding. `batch_infer` calls `_request_image_new` directly.
- **`ocr_compare.txt`** at repo root records the payload-format fix: the legacy `images:`-field request echoed back the prompt (garbage OCR), while the current `image_url` content-block variant works. Do not revert the payload format.
- **`config.json` diverges from `DEFAULT_CONFIG` on this machine**: `models_dir` is `E:/model` and `model_choices` names carry relative subdir prefixes (e.g. `huiyuan/HunyuanOCR.BF16.gguf`, `qwen3.5/...`, `paddleocr/...`, `Unlimited/...`); `check()`/`runserver()` join them via `os.path.join(models_dir, name)`.
- **`get_config()` pops interactive tkinter file/dir chooser dialogs** when `llama_server`/`models_dir` are missing or point at non-existent paths, and blocks until dismissed (headless environments swallow the TclError and skip). Every `llamamanage` call path (including `test_image_queue_request.py`) triggers `get_config()`, so on a desktop the script **hangs on the dialog** if the `E:/xox/...` paths don't exist — point `config.json` at valid paths first, or run headless.
- `llamamanage.py:46` — `check()` relies on a `'models_dir' in locals()` guard; if `llama` is passed in, `models_dir` is never bound and the fallback `os.path.dirname(llama)` is used.
- `createdic()` in `pdfmanage.py` uses incremental suffix (`_1`, `_2`) if the target dir exists; base dir is the script dir, not cwd
- OCR prompts always append `"按原文原格式输出"` unless `thinking=True`
- `stringmanage.clean_and_structure_text` asserts you can't request both simplified and traditional conversion at once
- The llama-server started by `runserver()` **outlives the CLI** — call `stopserver()` to stop it
- **`correctmanage.correct_pages` blocks the pipeline** until the user clicks 完成并转换 in the browser (or Ctrl+C). It starts a local server on 127.0.0.1 (ephemeral port) and opens the default browser; only `--correct` enters this stage. **Since the browser-close monitor, closing the tab/window no longer hangs the CLI**: the page heartbeats every 30s and sends a `pagehide` beacon; once gone > `idle_timeout` (default 600s, CLI `--correct-timeout`) the pipeline auto-continues with the last saved content. Preview images are rendered from the PDF (not the full-res PNGs) to avoid loading lag; if the PDF path is unavailable they fall back to the original page images.
- **History preload vs fresh OCR**: `correct_pages(preload_history=False)` skips loading the latest history cache — the `epub --correct` pipeline passes this so a re-recognized book shows the NEW OCR text, not the last staged/saved content (use `correct <pdf>` to re-correct saved content, which still preloads).
- `USAGE.md`'s `model_choices` example (flat string names) is stale vs the real `{name, mmproj}` objects
- No root `.gitignore` (only `.idea/`, `.venv/`, `.opencode/`, `.crush/` carry their own; `.crush/.gitignore` is just `*`)

---
