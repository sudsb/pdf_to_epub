# Repository Guidelines

## Project Overview

PToEA is a Windows-first CLI tool (plus optional local GUI) for converting PDF files to EPUB via OCR, model-based correction, and browser-based manual annotation. Core features: PDF-to-image splitting, OCR (llama.cpp and vLLM-Omni backends, PaddleOCR optional), robust EPUB output, interruption resume, and self-contained PyInstaller packs. Designed for portability and easy deployment (minimal deps, atomic writes, lazy imports, regular smoke testing). Key asset: flexible text normalization and correction pipeline, with both automated and manual flows.

## Architecture & Data Flow

- **Core pipeline**: `pdfmanage.py` splits PDF→images, `paddleocrmanage.py`/`llamamanage.py`/`vllmmanage.py` run OCR (engine selected via config/CLI), output is normalized (`stringmanage.py`), structured, and XHMTL-wrapped for EPUB creation (`epubmanage.py`).
- **Manual correction path**: Served via `guimanage.py`, using embedded UI (`ui/app.js`, `ui/epubedit.html`/`.js`). Users can proof, re-token, and export corrections interactively; supports undo/history.
- **Config/session/dependency management**: `configmanage.py` ensures atomic config writes, session mutices, guarded lazy import of heavy/optional deps (`zhconv`, `requests`, etc.).
- **Locking**: `_CFG_LOCK`, per-session mutex, and explicit module global guards for concurrency safety—order: config, session, PDF/image, model engine, HTML.
- **Batch/parallel hotspots**: Batch inference (`llamamanage.py`), PDF-to-image bulk ops, and EPUB post-processing are lock-coordinated and performance-sensitive.
- **Frozen EXE**: Paths, config, and runtime tests handle 8.3/long-form mismatches and minimize frozen build bugs.

## Key Directories

- Project Root: all primary modules, tests, `config.json` for persistent state.
- `ui/`: JS/HTML embedded frontend for correction GUI and EPUB editing.

## Development Commands

- **Dependency sync**: `uv sync`
- **Run CLI**: `uv run python mian.py [args]`
- **Set config**: `uv run python mian.py config set KEY VALUE`
- **Tests**: `python test_stringmanage.py`, `python test_pdfmanage.py`, `python test_llamamanage.py` (smoke: always run after changes)
- **Browser/GUI**: `uv run python mian.py gui`
- **Package to exe**: `powershell -ExecutionPolicy Bypass -File .\0pack.ps1 [-NoPaddle][-OneFile]`
- **Lint UI JS**: `node --check ui/app.js`; also `python check_js.py` for embedded/extracted JS

## Code Conventions & Common Patterns

- **Formatting**: No enforced linter; PEP8 and single-module patterns, avoid sprawling helpers.
- **Naming**: Module scope is flat, one responsibility/module; test files `test_*.py` mirror module names at root.
- **Error Handling**: Prefer explicit try/except with concise fallback. Never silence.
- **Globals/Locks**: Only `_CFG_LOCK`, `_SESSION`, and `_server_process` global. Lock ordering must be preserved (config → session → engine/image), see in-source comments for hack doors.
- **Async/Concurrency**: Multi-thread only in batch image/OCR/engine. No asyncio/event-loop—thread or process only.
- **Lazy/Optional Deps**: All heavy deps (`zhconv`, `requests`, PaddleOCR, opencv) are lazy-imported (try/except fallback, Dummy for missing); code still imports if missing, always test with and without deps installed.
- **State/DI**: No IoC framework; state passed explicitly or via config/session, not via DI.
- **Atomicity & Safety**: Config and output writes use in-project atomic helpers (`configmanage._atomic_write_json`).
- **Dangerous Paths**: File ops manipulate both image and epub in a single run—tmp/partial files cleaned after finish. Be careful: overwrites are explicit, not autosafe.
- **Test Isolation**: Most tests monkeypatch config/params; no cross-test shared state (except manual override for session).

## Important Files

- **Entrypoint**: `mian.py` (CLI), `guimanage.py` (server/GUI)
- **Key modules**: `configmanage.py`, `pdfmanage.py`, `llamamanage.py`, `correctmanage.py`, `epubmanage.py`, `stringmanage.py`, `vllmmanage.py`, `paddleocrmanage.py`
- **Frontend**: `ui/app.js`, `ui/epubedit.html`, `ui/tabhost.html`, `ui/epubedit.js`
- **Config**: `config.json` (auto-managed but can be edited for advanced setup)
- **Pack, build scripts**: `0pack.ps1`, `0pack.bat`, `0_copy_ptoe_to_pte.ps1`
- **Tests**: `test_*.py` (all at repo root)
- **Docs**: `README.md`, `USAGE.md` (Chinese quickstart), `pyproject.toml` (dependencies/tooling)

## Runtime/Tooling Preferences

- **Interpreter**: Python ≥3.11, Windows required (tested only on Win10/11)
- **Dep manager**: [uv](https://docs.astral.sh/uv/) (`uv sync`, not pip)
- **Build**: PyInstaller (via PowerShell), optional PaddleOCR support (default packs exclude for space)
- **UI**: Node.js (or Bun) only needed for JS lint/validation
- **Test**: stdlib unittest only; no pytest/coverage/lint by default
- **Embedded UI**: JS/HTML in `ui/`, extracted/checked via `python extract_js.py` + `node --check`

## Testing & QA

- **Framework**: stdlib `unittest` across 20+ `test_*.py` in project root
- **How to run**:
  - Individual: `python test_xxx.py`
  - Minimal smoke (always run after changes):
    - `python test_stringmanage.py`
    - `python test_pdfmanage.py`
    - `python test_llamamanage.py`
  - UI/JS: `node --check ui/app.js`, `python check_js.py`
- **Quality**: No type/lint/coverage gate—manual smoke tests are required
- **Patterns**: Use atomic write helpers, lock ordering, and guarded lazy imports for all tests
- **Pitfalls**: Be watchful of 8.3/long-path mismatches (especially in frozen exe), session/config race bugs, global state, and missed dependency mocks in test runs

---
_See [README.md](README.md), [USAGE.md](USAGE.md), and comments in `0pack.ps1` for deeper specifics. Update this guideline if major architecture or conventions change._
