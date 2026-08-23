# Repository Guidelines

## Project Overview
- Purpose: CLI tool (ptoe) that converts PDF → images → OCR → structured XHTML → EPUB, with an optional browser-based manual correction UI.
- Entrypoint: `mian.py` (subcommands: `epub`, `correct`, `resume`, `model`, `config`, `gui`). See `mian.py` docstring / CLI help (mian.py:1-200).

## Architecture & Data Flow
- High level flow (implementor path): mian.py → pdfmanage.split_pdf_to_images() → llamamanage.batch_infer()/vllmmanage.batch_infer() → stringmanage.clean_and_structure_text() → htmlmanage.HTMLConverter → epubmanage.EPUBPacker.
  - OCR engines: default engine chosen by `config.json` `engine` key; runtime dispatch in `llamamanage._active_engine()` (llama vs vllm). See `llamamanage.py` (1-200).
  - Optional correction stage: `correctmanage.correct_pages()` serves a ThreadingHTTPServer and returns corrected pages, then `mian.py` `apply_markers()` merges markers into article structure (correctmanage.py:1-200).
- Concurrency:
  - Batch inference uses ThreadPoolExecutor (vllmmanage/llamamanage) and a shared requests.Session with retries (`llamamanage._SESSION`) to reuse HTTP keep-alive connections.
  - Correction and GUI services use ThreadingHTTPServer and module-level locks (`pages_lock`, config `_CFG_LOCK`). See `correctmanage.py`, `guimanage.py`, and `configmanage.py`.
- State & globals to be careful with during refactor: `_server_process`, `_SESSION` (llamamanage.py), `_CFG_LOCK` (configmanage.py), `_ENGINE_CACHE`/`_BATCH_ENGINE` (llamamanage.py). These names appear across modules and must be preserved or renamed with LSP-aware refactor.

## Key Directories
- Root scripts: `mian.py`, `pdfmanage.py`, `llamamanage.py`, `vllmmanage.py`, `configmanage.py`, `stringmanage.py`, `htmlmanage.py`, `epubmanage.py`, `correctmanage.py`, `guimanage.py`, `dictionarymanage.py`, `proofreadmanage.py`, `rulemanage.py`.
- data/: per-PDF output (images, correction_history, final .epub). (Referenced across pdfmanage.py/correctmanage.py.)
- dicts/: dictionaries bundled into exe (jieba, shapes, homophones). Lookups in `dictionarymanage.py`.
- scripts/: developer helpers (e.g., `check_js.py`, `test_proofread_perf.py`).
- build/ and dist/ (PyInstaller artifacts) — packaging flow anchored by `pack.ps1`.

## Development Commands
- Run CLI (local Python):
  - python mian.py epub <pdf> [--dpi 0..4] [--model KEY] [--workers N] [--engine llama|vllm] [--thinking]
  - python mian.py correct <pdf?> [--engine llama|vllm]
  - python mian.py gui [--host 127.0.0.1] [--port 0] [--no-browser]
- Unit tests (stdlib unittest):
  - python -m unittest discover -v
  - Targeted: python -m unittest test_stringmanage
  - Note: test_config_llama.py is pytest-style and may error under unittest discover if pytest not installed.
- Packaging (Windows):
  - powershell -ExecutionPolicy Bypass -File .\pack.ps1  # builds onefile exe via PyInstaller (mian.py entry)
- UI edit checks:
  - python check_js.py  # validates embedded UI script in correctmanage.py
  - node --check extracted_ui.js  # syntax check
  - (Optional) jsdom harness at %TEMP%/ptoe_ui_harness2.mjs for DOM tests (documented in repo).

## Code Conventions & Common Patterns
- Flat script layout: top-level .py files, lazy imports inside functions to avoid heavy deps during unrelated commands (mian.py, correctmanage.py import lazily).
- Atomic writes for config and small persistent files: `configmanage._atomic_write_json(path, obj)` uses tempfile + os.replace to guarantee atomic replacement; preserve this pattern for any persistent JSON writes.
- Module-global state: many modules expose module-level globals (e.g. `llamamanage._server_process`, `llamamanage._SESSION`, `configmanage._CFG_LOCK`, `correctmanage._preview_warm_started`). Do not rename/replace without using LSP rename across callsites.
- Concurrency:
  - Use ThreadPoolExecutor for CPU/IO parallelism in inference and ProcessPoolExecutor guarded for preview warming (see correctmanage._PREVIEW_POOL_CLS).
  - ThreadingHTTPServer is used for browser UIs; shutdown must call server.shutdown() before server.server_close() on Windows to avoid OSError 10038.
  - Use threading.Lock / RLock around shared mutable module state; `_CFG_LOCK` in configmanage protects reads/writes.
- IO robustness:
  - Avoid synchronous heavy imports at module top; lazy import in function scope.
  - When spawning external servers, probe `--help` support first (`llamamanage._server_supports_arg`) to avoid process-exit due to unsupported flags.
- Text and EPUB rules:
  - EPUB internal paths must use forward slashes ('/') — htmlmanage/epubmanage enforce this.
  - Embedded UI HTML/JS is large; after edits run `check_js.py` + `node --check` before committing.

## Important Files
- mian.py — CLI entry and orchestration; key functions: `pdf_to_epub`, `correct_pdf`. (mian.py:1-200)
- llamamanage.py — engine dispatch, server lifecycle, shared HTTP session `_SESSION`, batch_infer, runserver/stopserver. (llamamanage.py:1-200)
- vllmmanage.py — vLLM-Omni adapter (same API shape as llamamanage). (vllmmanage.py)
- configmanage.py — `get_config()`, `_atomic_write_json()`, DEFAULT_CONFIG, set_format_rules. (configmanage.py:1-200)
- correctmanage.py — correction UI server, `correct_pages()`, `apply_markers()`, HTML sanitizer, format rules CRUD/validation (`/api/format_rules`). (correctmanage.py:1-200)
- guimanage.py — GUI server endpoints (`/api/convert/*`, `/api/server/*`, `/api/pick`), `_UI_HTML` placeholder. (guimanage.py:1-220)
- htmlmanage.py / epubmanage.py — HTML conversion and EPUB packaging; EPUBPacker and HTMLConverter classes.
- dictionarymanage.py — tokenization and candidate generation used by proofreader.
- rulemanage.py — server-side format rules application engine (pure stdlib html.parser mini DOM): condition evaluation (contains/prefix/suffix/regex with `/pattern/flags`), rule modes (first/all), match/group/target formats, conflict resolution (first-wins). Served via POST `/api/format_rules/apply` ({page, html, rule_id|all, sel_start, sel_end} → sanitized new HTML); the browser only renders — the old client-side JS engine (evalCondition/evalFormatRule/applyRegexMatchFormats/applyRegexGroupFormats/applyTargetFormats/applyFormatsList) was removed. Tests: `test_rulemanage.py`.

## Runtime / Tooling Preferences
- Python >= 3.11 recommended (typing features used). Tests / runner expect stdlib unittest; pytest only for one file.
- Package runner: `uv` recommended in repo docs but standard `python` runner works.
- Node (for `node --check`) is required to validate inlined UI JS edits. jsdom optional for DOM-level checks.
- Windows-first packaging: `pack.ps1` targets PyInstaller and produces a single exe (mian.py entry). vLLM-Omni typically Linux-only; vllm_server is often used in connect-only mode on Windows.

## Testing & QA
- Test organization: many test_*.py files at repo root. Unit tests use stdlib unittest; run discover for full suite.
- Fast smoke tests suggested after edits:
  - python -m unittest test_stringmanage
  - python -m unittest test_pdfmanage
  - python -m unittest test_llamamanage
- GUI & packaging tests require dependencies (PyMuPDF/requests) and may spawn subprocesses; run them only when environment has those deps.
- After any edit to correctmanage.py's embedded UI script:
  1. python check_js.py
  2. python _extract_js.py to extract JS
  3. node --check <extracted.js>
  4. (Optional) run jsdom harness at %TEMP%/ptoe_ui_harness2.mjs with url option to validate DOM usage.

## Practical notes for AI-assisted edits
- For cross-file renames and exported symbol changes, use LSP rename (symbol-aware) rather than regex/text replace to avoid missing callsites. Relevant symbols: `_server_process`, `_SESSION`, `_CFG_LOCK`, `apply_markers`, `correct_pages`.
- Preserve atomic-write helpers and lock order (e.g., do not call get_config() while holding _CFG_LOCK).
- When changing server start/stop logic, ensure probe/start/stop functions keep the same external behavior: runserver(model_key, with_mmproj=True) and stopserver() semantics.


-- End of AGENTS.md draft
