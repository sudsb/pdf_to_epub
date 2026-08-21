# ptoe

**PDF → OCR → EPUB** pipeline using local inference (llama.cpp / vLLM-Omni).

A Windows-oriented CLI tool that converts PDFs to EPUB via local OCR and local model servers. Supports both llama.cpp (`llama-server`) and vLLM-Omni backends.

## Features

- **PDF → Images**: Split PDF into page images using PyMuPDF (fitz)
- **OCR**: Run OCR against local llama-server (OpenAI-compatible `/v1/chat/completions`) or vLLM-Omni
- **Text Processing**: Clean, structure, and normalize OCR text (bbox conversion, page-number stripping, heading detection, zh-CN/zh-TW conversion)
- **EPUB Generation**: Convert structured text to XHTML and package as standard EPUB3
- **Optional Manual Correction**: Browser-based UI for page-by-page correction with formatting, markers, search/replace, proofreading, and history
- **Resume Support**: Resume interrupted OCR jobs from checkpoint
- **Packaging**: Single-file Windows executable via PyInstaller

## Quick Start

### Prerequisites

- Windows (llama-server paths are Windows-style)
- Python ≥ 3.11 + [uv](https://docs.astral.sh/uv/)
- Inference backend (choose one):
  - **llama.cpp**: `llama-server.exe` + multimodal OCR model (GGUF + mmproj), e.g., HunyuanOCR
  - **vLLM-Omni**: `vllm serve` (default port 8000), suitable for GPU environments

### Install Dependencies

```powershell
cd D:\code-project\python\PToEA
uv sync
```

Dependencies: `pymupdf`, `requests`, `zhconv` only.

### Configure Paths

The program auto-creates/repairs `config.json` on first run, but **if `llama_server` or `models_dir` point to non-existent paths, a tkinter file dialog will block startup**. Configure first:

```powershell
# Check current config
uv run python mian.py config show

# Set llama-server path
uv run python mian.py config set llama_server "E:/xox/Tools/llama-c/llama-server.exe"

# Set models directory
uv run python mian.py config set models_dir "E:/model"

# Set default model (must exist in model_choices)
uv run python mian.py config set selected_model HY

# Switch engine (llama | vllm)
uv run python mian.py config set engine llama
```

Or edit `config.json` directly. Key structure:

```jsonc
{
  "engine": "llama",
  "llama_server": "E:/xox/Tools/llama-c/llama-server.exe",
  "models_dir": "E:/model",
  "model_choices": {
    "HY": "huiyuan/HunyuanOCR.BF16.gguf",
    "QWEN.8": "qwen3.5/qwen2.5-0.8b-instruct.gguf"
  },
  "selected_model": "HY",
  "llama_server_args": {
    "host": "127.0.0.1",
    "port": "8080",
    "temperature": "0",
    "repeat_penalty": "1.1",
    "parallel": "4",
    "cache_type_k": "q8_0",
    "cache_type_v": "q8_0",
    "log_verbosity": "0",
    "max_tokens": "8192"
  },
  "vllm_server": "",
  "vllm_server_args": {
    "host": "127.0.0.1",
    "port": "8000",
    "max_model_len": "32768"
  }
}
```

### Run Conversion

```powershell
uv run python mian.py epub "E:\Books\example.pdf" --title "Example Book"
```

Output:
```
[1/4] Splitting PDF to images (dpi=200) ...      → 12 page(s) -> data/Example Book/
[2/4] OCR via llama-server (model='HY', workers=auto) ...
[3/4] Structuring text (12 page(s)) ...
[4/4] Rendering XHTML and packing EPUB ...
Done: D:\code-project\python\PToEA\data\Example Book\Example Book.epub
```

## CLI Reference

### Main Commands

| Command | Description |
|---------|-------------|
| `epub <pdf>` | Full OCR → EPUB pipeline |
| `correct [<pdf>]` | Direct correction UI (no OCR) |
| `resume <pdf>` | Resume interrupted OCR |
| `stop [--engine llama\|vllm]` | Stop inference server |
| `config show\|set <key> <value>` | View/modify config |
| `model list\|show\|set\|add\|remove` | Manage model registry |
| `gui [--host 127.0.0.1] [--port 0] [--no-browser] [--idle-timeout 120]` | Web-based config UI |

### `epub` Options

| Option | Default | Description |
|--------|---------|-------------|
| `--dpi` | 0 | Render DPI level: 0=100, 1=150, 2=200, 3=300, 4=600 |
| `--model` | config `selected_model` | Model key from `model_choices` |
| `--engine` | config `engine` | `llama` or `vllm` (runtime only) |
| `--workers` | model's `workers` or 3 | OCR concurrency |
| `--timeout` | 600 | Per-request timeout (seconds) |
| `--thinking` | off | Enable thinking mode (Qwen3 hidden CoT) |
| `--correct` | off | Enable manual correction UI |
| `--correct-timeout` | 600 | Browser-close auto-continue delay (seconds) |
| `--title` | PDF filename | EPUB title metadata |
| `--author` | (empty) | EPUB author metadata |
| `--lang` | zh-CN | EPUB language |
| `--out-dir` | `data/<pdf_stem>/` | OEBPS output directory |
| `--epub-path` | `data/<pdf_stem>/<title>.epub` | Final EPUB path |

### DPI Levels

| Level | DPI | Image Tokens/Page (approx) | Speed (HY model) |
|-------|-----|---------------------------|------------------|
| 0 | 100 | ~1,000 | ~3s/page |
| 1 | 150 | ~2,500 | ~5s/page |
| 2 | 200 | ~4,600 | ~10s/page |
| 3 | 300 | ~7,000 | ~35s/page |
| 4 | 600 | ~8,700 | slower |

Higher DPI = more accurate but slower. Level 0 is recommended for most cases.

## Manual Correction (`--correct`)

When enabled, a local HTTP server starts and opens a browser UI:

- **Left**: Page image preview (click to toggle full-res)
- **Right**: Editable OCR text (one `<div>` per line)
- **Toolbar**: Format (bold, italic, headings h1–h6, alignment, notes), markers (article break, paragraph merge), search/replace, proofreading, image insert, export TXT/DOCX/EPUB
- **Virtual List**: Handles 1000+ pages smoothly
- **Undo/Redo**: Ctrl+Z / Ctrl+Y (10 steps)
- **History**: Versioned cache at `data/correction_history/` (20 versions per PDF)
- **Auto-continue**: Closing browser tab continues pipeline after `--correct-timeout` (default 600s)

### Direct Correction (No OCR)

```powershell
uv run python mian.py correct "E:\Books\example.pdf"
# Or without PDF for history management:
uv run python mian.py correct
```

Loads latest history cache for the PDF (or blank for new entry).

## Resume Interrupted OCR

```powershell
uv run python mian.py resume "E:\Books\example.pdf" [--restart]
```

- Detects `data/<pdf_stem>/.ocr_progress.json`
- Prompts: continue unfinished pages / convert directly / restart / cancel
- `--resume` / `--restart` flags skip prompt

## Model Management

```powershell
uv run python mian.py model list                    # List registered models
uv run python mian.py model show                    # Show current model
uv run python mian.py model set HY                  # Set default model
uv run python mian.py model add NEW --name model.gguf --mmproj mmproj.gguf [--force]
uv run python mian.py model remove OLD              # Remove model
```

## Web Config UI (`gui`)

```powershell
uv run python mian.py gui [--host 127.0.0.1] [--port 0] [--no-browser] [--idle-timeout 120]
```

Browser-based interface for:
- View/edit config & launch parameters
- Start/stop inference server
- Launch conversion with progress monitoring
- File/directory pickers via tkinter (main thread)

## Packaging (Windows Executable)

```powershell
powershell -ExecutionPolicy Bypass -File .\pack.ps1
```

Produces `dist\ptoe.exe` (onefile + console). Double-click to enter terminal menu:

```
1) PDF → EPUB 转换（OCR 全流程）
2) 手动矫正（correct，不跑 OCR）
3) 查看/修改配置（config）
4) 模型管理（model）
5) 继续识别上次中断的转换（resume）
6) 帮助（CLI 用法）
7) 停止推理服务（llama-server / vLLM）
0) 退出
```

CLI usage identical: `ptoe.exe epub <pdf> ...`, `ptoe.exe correct`, etc.

## Architecture

```
mian.py (CLI entry)
  ├─ pdfmanage.py          → split_pdf_to_images(), ImageItem, ImageQueue
  ├─ llamamanage.py        → llama.cpp engine: runserver, batch_infer, request_image
  ├─ vllmmanage.py         → vLLM-Omni engine (same API)
  ├─ configmanage.py       → config.json read/write (atomic, locked)
  ├─ stringmanage.py       → clean_and_structure_text, bbox conversion, heading detection
  ├─ htmlmanage.py         → HTMLConverter, XHTML/OEBPS generation
  ├─ epubmanage.py         → EPUBPacker, mimetype-first ZIP_STORED
  ├─ correctmanage.py      → Correction UI (ThreadingHTTPServer + embedded HTML/JS)
  ├─ dictionarymanage.py   → Tokenization, wordlist, proofreading helpers
  ├─ proofreadmanage.py    → Visual proofreading via VLM (not wired by default)
  └─ guimanage.py          → Web config UI (added 2026-08-17)
```

### Data Flow

1. `split_pdf_to_images(pdf, dpi, fmt)` → `data/<pdf_stem>/1.png, 2.png...`
2. `batch_infer` posts images to local server → per-page text
3. `clean_and_structure_text` → structured pages (bbox→HTML, strip page numbers, detect headings)
4. `HTMLConverter` → `OEBPS/Text/content_N.xhtml`, `Styles/`, `Images/`
5. `EPUBPacker` → `.epub` (mimetype first, ZIP_STORED, spine ordered, nav.xhtml with `epub:type="toc"`)

## Output Structure

```
data/<pdf_stem>/
├── 1.png, 2.png, ...           # Page images (reused across runs)
├── .ocr_progress.json          # Resume checkpoint
├── OEBPS/
│   ├── Text/content_1.xhtml    # Chapters (split by h1)
│   ├── Styles/style.css
│   ├── Images/                 # Extracted images
│   └── nav.xhtml               # TOC (EPUB3 nav)
├── mimetype
├── META-INF/container.xml
├── content.opf
└── <title>.epub                # Final EPUB
```

## Testing

```powershell
# Unit tests (stdlib unittest)
uv run python -m unittest test_pdfmanage test_mian test_correctmanage test_llamamanage test_stringmanage test_vllmmanage

# Queue + request script test
uv run python test_image_queue_request.py

# Proofread performance benchmark
uv run python scripts/test_proofread_perf.py
```

Note: `test_config_llama.py` is pytest-style; requires `uv add --dev pytest`.

## Stopping Stray Servers

```powershell
uv run python mian.py stop
# Or directly:
uv run python -c "from llamamanage import stopserver; stopserver()"
```

## Key Implementation Details

- **Flat script layout**: All `.py` files at repo root (not a package)
- **Lazy imports**: Heavy deps (fitz, requests) imported inside functions
- **Atomic config writes**: tempfile + os.replace under module-level lock
- **Threading only**: ThreadPoolExecutor, ThreadingHTTPServer; no asyncio
- **Module globals**: `_server_process` in llamamanage, shared `requests.Session`
- **EPUB internal paths**: Always forward slashes (`/`), never `os.path.join`
- **Frozen exe support**: `sys._MEIPASS` for bundled assets (dicts/, pyproject.toml)
- **OCR prompt**: Appends `"\n按原文原格式输出"` unless `--thinking` enabled
- **Thinking mode**: Qwen3 hidden CoT slows OCR ~7x; default off

## License

MIT (or specify your license)

---

*See [USAGE.md](USAGE.md) for detailed Chinese tutorial and [AGENTS.md](AGENTS.md) for development guidelines.*
