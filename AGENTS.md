# Repository Guidelines

## Project Overview

- Purpose: CLI tool (ptoe) to convert PDFs → images → OCR → structured XHTML → EPUB. Includes optional browser-based manual correction UI and a lightweight web config GUI.
- Entrypoint: `mian.py` (subcommands: `epub`, `correct`, `resume`, `model`, `config`, `gui`, `stop`).
- Target: Windows-first packaging (single-file PyInstaller exe); inference adapters support local and remote engines (llama.cpp, vLLM-Omni, PaddleOCR adapter).

## Architecture & Data Flow

- High-level pipeline (canonical):
  - mian.py → pdfmanage.split_pdf_to_images() → llamamanage/vllmmanage.batch_infer() → stringmanage.clean_and_structure_text() → htmlmanage.HTMLConverter → epubmanage.EPUBPacker
  - Optional manual correction: correctmanage.correct_pages() (ThreadingHTTPServer) → apply_markers() → repackage
  - Web config UI: guimanage.py serves small web endpoints for runtime configuration.
- Engine adapters share the same API shape (llamamanage.py ↔ vllmmanage.py). Paddle OCR lives in paddleocrmanage.py (optional heavy dependency).
- Outputs and state:
  - Per-PDF files under `data/` (images, correction_history, final .epub)
  - Dictionaries under `dicts/` (jieba, shapes, homophones)
  - Embedded UI JS/HTML kept in the server files and extracted by helper scripts when needed.

## Key Directories and files (paths)

- Top-level Python modules: `mian.py`, `pdfmanage.py`, `llamamanage.py`, `vllmmanage.py`, `configmanage.py`, `stringmanage.py`, `htmlmanage.py`, `epubmanage.py`, `correctmanage.py`, `guimanage.py`, `dictionarymanage.py`, `rulemanage.py`, `proofreadmanage.py`, `paddleocrmanage.py`.
- Data and assets: `data/`, `dicts/`, `ui/` (embedded UI assets when present).
- Build / packaging: `pack.ps1`, `ptoe.spec`, `build/`, `dist/`.
- Developer scripts: `check_js.py`, `extract_js.py`, `_extract_js.py`, `scripts/test_proofread_perf.py`.

## Development Commands (examples)

- Convert PDF to EPUB (basic):
  - python mian.py epub <file.pdf> --dpi 2 --model <MODEL_KEY> --workers 4 --title "Book Title"
- Launch correction UI for a PDF:
  - python mian.py correct <file.pdf>
- Start config GUI:
  - python mian.py gui
- Stop a running engine/server:
  - python mian.py stop --engine llama
- Unit tests (stdlib unittest):
  - python -m unittest discover -v
  - python -m unittest test_stringmanage
- Fast smoke tests after edits:
  - python -m unittest test_stringmanage
  - python -m unittest test_pdfmanage
  - python -m unittest test_llamamanage
  - python -m unittest test_vllmmanage
- Embedded-UI JS validation (after editing correctmanage.py):
  - python check_js.py
  - python extract_js.py && node --check extracted_ui.js
- Packaging (Windows):
  - powershell -ExecutionPolicy Bypass -File .\pack.ps1  # builds onefile PyInstaller exe

(Repository uses `uv run ...` wrappers in developer scripts; plain `python` commands work too.)

## Code Conventions & Common Patterns — what to follow as an assistant

- Flat script layout. Keep heavy imports lazy: import fitz/PyMuPDF, cv2, requests, zhconv, paddleocr, jieba inside functions to avoid expensive top-level imports.
- Atomic persistent writes: use the provided helper `configmanage._atomic_write_json(path, obj)` (tempfile + os.replace). Hold `configmanage._CFG_LOCK` when reading/writing config.
- Preserve module-global state and names. Common globals to not rename silently: `configmanage._CFG_LOCK`, `configmanage._CONFIG_PATH`, `configmanage._atomic_write_json`, `llamamanage._SESSION`, `llamamanage._server_process` (server lifecycle cache), `llamamanage._ARG_HELP_CACHE`, `llamamanage._BATCH_ENGINE`/`_ENGINE_CACHE`, `correctmanage._PREVIEW_WARM_STARTED`, `correctmanage._PREVIEW_WARMED_KEYS`. Use LSP-aware rename for cross-file symbol changes.
- Concurrency model:
  - ThreadPoolExecutor for batch inference and I/O parallelism (llamamanage.batch_infer).
  - ProcessPoolExecutor used for heavy rendering/preview warming in pdfmanage/correctmanage where present.
  - ThreadingHTTPServer for GUI/correction UIs (guimanage.py and correctmanage.py).
  - Protect shared mutable state with threading.Lock / RLock; lock order matters (config lock vs other locks).
- Network client reuse: a shared requests.Session with mounted HTTPAdapter+Retry is used (llamamanage._SESSION). Preserve this pattern to keep connection reuse and retry semantics.
- EPUB rules: internal EPUB paths must use forward slashes ('/'); htmlmanage/epubmanage enforce this. Do not os.path.join EPUB internal paths.
- Format-rule engine: rulemanage implements a small DOM-like parser for rule application (conditions: contains/prefix/suffix/regex; modes: first/all). Tests exist — prefer using that engine rather than re-implementing client-side rule logic.
- Marker system: content markers use data attributes (e.g., data-ptoe-marker). When refactoring HTML conversion or marker application, update apply_markers() callsites.
- Frozen exe support: access bundled assets via sys._MEIPASS when reading pyproject/embedded assets. Packaging scripts expect these assets to be present.

## Important files (what you'll open/edit often)

- mian.py — CLI orchestration and subcommand wiring.
- configmanage.py — get_config(), validate_and_patch_config(), `_atomic_write_json()`, DEFAULT_CONFIG. Preserve `_CFG_LOCK` semantics.
- llamamanage.py — engine lifecycle, `_SESSION`, batch_infer(), server probe/start/stop helpers (probe uses `--help`).
- vllmmanage.py — vLLM adapter; keep API parity with llamamanage.
- pdfmanage.py — PDF→image splitting and preprocess, rendering helpers.
- stringmanage.py — text cleaning and structure heuristics (bbox→HTML, heading detection, brackets).
- htmlmanage.py / epubmanage.py — HTML→XHTML conversion and EPUB packaging (OPF/nav generation).
- correctmanage.py — browser correction server, preview warming, apply_markers(), embedded UI script.
- guimanage.py — config GUI endpoints and small management UI.
- rulemanage.py — server-side format-rule engine; tests: `test_rulemanage.py`.
- scripts: `check_js.py`, `extract_js.py`, `_extract_js.py`, `scripts/test_proofread_perf.py` (benchmarks).
- pack.ps1, ptoe.spec, pyproject.toml — packaging metadata and packaging script.

## Runtime / Tooling Preferences

- Python >= 3.11 recommended.
- Windows-first packaging via PyInstaller (pack.ps1). The released exe is single-file (onefile), no UPX.
- Node required only for static JS syntax checks (`node --check`).
- Tests primarily use the stdlib `unittest`; `pytest` is required only for `test_config_llama.py`.
- Avoid adding new heavy runtime dependencies; prefer optional imports guarded by try/except and conservative fallbacks.

## Testing & QA — verification checklist for edits

- Unit tests: `python -m unittest discover -v` (full run). Use targeted tests for fast feedback: `test_stringmanage`, `test_pdfmanage`, `test_llamamanage`, `test_vllmmanage`.
- After editing embedded UI in `correctmanage.py`:
  - Run `python check_js.py` (repo helper)
  - Extract JS: `python extract_js.py` (or `_extract_js.py` depending on area)
  - Syntax check: `node --check extracted_ui.js`
- After edits touching config behavior: run tests including `test_config_llama.py` (requires pytest) and verify `configmanage._atomic_write_json` still used by code paths that persist config.
- After server lifecycle or engine argument changes: verify server probe/start/stop flows and `mian.py stop` path.
- Packaging: if you change bundled assets (dicts/, pyproject.toml, ui files), run `powershell -ExecutionPolicy Bypass -File .\pack.ps1` and smoke the exe.

## Quick editing safety rules (short)

- Re-ground before edits: run the small smoke tests that exercise the module you change.
- Use lazy-import pattern already present; do not move heavy imports to module top-level.
- For cross-file symbol renames, use LSP `rename` to update callsites — do not sed/regex replace.
- Preserve `_SESSION`, `_CFG_LOCK`, `_server_process` semantics and lock order.
- If you change embedded UI, run check_js + node check before committing.

---

Paths and commands in this file were synthesized from repository source and developer scripts (see: `mian.py`, `configmanage.py`, `llamamanage.py`, `correctmanage.py`, `check_js.py`, `extract_js.py`, `pack.ps1`, `pyproject.toml`).

If you want, I will also: (a) add a one-line per-file quick reference table, or (b) produce a short `DEV_CHECKLIST.md` with the smoke-test commands and exact test set to run after edits. Which should I write next? (Recommended: 0)