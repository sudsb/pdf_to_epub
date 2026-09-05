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
  - **llama.cpp**: local llama-server.exe + multimodal GGUF model. 用于 OCR 与深度校对；程序可按 config/运行时自动拉起/复用服务。
  - **vLLM-Omni**: `vllm serve`（默认端口 8000），适合带 GPU 的环境。
  - **PaddleOCR (local)**: 本地 PaddleOCR 引擎。可通过 CLI 覆盖 `--engine paddle` 在 PDF→EPUB 的 OCR 阶段使用（运行时覆盖，只影响 OCR 阶段；不会写入 config.json）。文本矫正/深度校对仍走 llama-server/vLLM（大模型）。

### Install Dependencies

```powershell
cd D:\code-project\python\PToEA
uv sync
```

Dependencies: `pymupdf`, `requests`, `zhconv` only.

### Configure Paths

The program auto-creates/repairs `config.json` on first run, but **if `llama_server` or `models_dir` point to non-existent paths, a tkinter file dialog will block startup**. Configure first:

```powershell
# View config: open config.json directly, or run `gui` (the `config` command only has `set`)

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
  "gui_display": "pywebview",
  "tabs_position": "top",
  "window_maximized": true,
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

下面命令行帮助由源码生成（mian.py -h 及各子命令 -h），以保证与运行时参数一致。

```text
usage: ptoe [-h] [-e ECHO] [--version] command ...

ptoe: PDF -> OCR -> EPUB conversion tool

positional arguments:
  command
    epub           Convert a PDF to an EPUB file
    correct        直接启动手动矫正界面（不跑 OCR；可无文件启动）
    resume         继续/管理上次中断的 OCR 转换（断点续传）
    stop           停止推理服务（llama-server / vLLM-Omni）
    model          Model registry commands (list/show/set/add/remove)
    config         查看或修改配置（llama_server / models_dir / selected_model 等）
    gui            启动 HTML 配置操作界面（默认 pywebview 内置窗口，可切换浏览器）

options:
  -h, --help       show this help message and exit
  -e, --echo ECHO  Echo text to stdout
  --version        Print project version and exit
```

### epub

```text
usage: ptoe epub [-h] [--dpi {0,1,2,3,4}] [--model MODEL]
                 [--engine {llama,vllm,paddle}] [--workers WORKERS]
                 [--timeout TIMEOUT] [--thinking] [--title TITLE]
                 [--author AUTHOR] [--lang LANG] [--out-dir OUT_DIR]
                 [--epub-path EPUB_PATH] [--correct]
                 [--correct-timeout CORRECT_TIMEOUT] [--resume | --restart]
                 pdf

PDF -> images -> OCR -> XHTML -> EPUB

positional arguments:
  pdf                   Path to the source PDF

options:
  -h, --help            show this help message and exit
  --dpi {0,1,2,3,4}     DPI level 0-4: 0=100, 1=150, 2=200, 3=300, 4=600
                        (default: 0=100)
  --model MODEL         Model key in config.json model_choices (default: from
                        config.json)
  --engine {llama,vllm,paddle}
                        推理引擎：llama（llama.cpp，默认）或 vllm（vLLM-Omni）；缺省用
                        config.json 的 engine 键
  --workers WORKERS     OCR worker threads (default: 模型推荐并发
                        model_choices.<key>.workers，未配置时 3；视觉模型每张数千图像
                        token，并发过高会让 KV 缓存溢出到 CPU 反而变慢；显存充足可调大如 6)
  --timeout TIMEOUT     Per-request read timeout in seconds (default: 600)
  --thinking            Pass the prompt through without appending the
                        '按原文原格式输出' suffix
  --title TITLE         EPUB title (default: auto from PDF metadata)
  --author AUTHOR       EPUB author
  --lang LANG           EPUB language code (default: zh-CN)
  --out-dir OUT_DIR     Output directory for OEBPS/ and the EPUB (default:
                        data/<pdf stem>/)
  --epub-path EPUB_PATH
                        Explicit output path for the .epub file
  --correct             开启手动矫正：在浏览器中逐页对照原图与识别文字，可标记粗体/斜体/标题（默认关闭）
  --correct-timeout CORRECT_TIMEOUT
                        浏览器被关闭后自动继续后续流程的等待秒数（仅 --correct 生效；默认 600=10 分钟）
  --resume              继续上次中断的 OCR（跳过询问：只识别未完成页；OCR 已完成则直接转换）
  --restart             忽略已有 OCR 进度，重新识别全部页面（跳过询问）
```

### resume

```text
usage: ptoe resume [-h] [--dpi {0,1,2,3,4}] [--model MODEL]
                   [--engine {llama,vllm,paddle}] [--workers WORKERS]
                   [--timeout TIMEOUT] [--thinking] [--title TITLE]
                   [--author AUTHOR] [--lang LANG] [--out-dir OUT_DIR]
                   [--epub-path EPUB_PATH] [--correct]
                   [--correct-timeout CORRECT_TIMEOUT] [--restart]
                   pdf

针对上次 OCR 中断/未完成的 PDF 继续处理：只识别未完成页（OCR 已全部完成则直接进入转换），交互询问或 --restart
强制重来。无进度时询问是否从头完整转换。

positional arguments:
  pdf                   Path to the source PDF

options:
  -h, --help            show this help message and exit
  --dpi {0,1,2,3,4}     DPI level 0-4: 0=100, 1=150, 2=200, 3=300, 4=600
                        (default: 0=100)
  --model MODEL         Model key in config.json model_choices (default: from
                        config.json)
  --engine {llama,vllm,paddle}
                        推理引擎：llama（llama.cpp，默认）或 vllm（vLLM-Omni）；缺省用
                        config.json 的 engine 键
  --workers WORKERS     OCR worker threads (default: 模型推荐并发
                        model_choices.<key>.workers，未配置时 3；显存充足可调大如 6)
  --timeout TIMEOUT     Per-request read timeout in seconds (default: 600)
  --thinking            Pass the prompt through without appending the
                        '按原文原格式输出' suffix
  --title TITLE         EPUB title (default: auto from PDF metadata)
  --author AUTHOR       EPUB author
  --lang LANG           EPUB language code (default: zh-CN)
  --out-dir OUT_DIR     Output directory for OEBPS/ and the EPUB (default:
                        data/<pdf stem>/)
  --epub-path EPUB_PATH
                        Explicit output path for the .epub file
  --correct             开启手动矫正（默认关闭；同 epub --correct）
  --correct-timeout CORRECT_TIMEOUT
                        浏览器被关闭后自动继续后续流程的等待秒数（仅 --correct 生效；默认 600=10 分钟）
  --restart             忽略已有进度，重新识别全部页面（跳过询问）
```

### correct

```text
usage: ptoe correct [-h] [--engine {llama,vllm,paddle}] [--title TITLE]
                    [--author AUTHOR] [--lang LANG] [--out-dir OUT_DIR]
                    [--epub-path EPUB_PATH]
                    [--correct-timeout CORRECT_TIMEOUT]
                    [pdf]

直接打开手动矫正界面：不运行 OCR；页面文本优先取本地历史缓存最新版本（同一 PDF 上次矫正/暂存的内容），无历史则为空白页。点「完成并转换」时生成
EPUB，可留在页面继续修改后再次点击。不带 PDF 参数时为无文件启动（空白界面，用于历史记录管理/手动录入）。

positional arguments:
  pdf                   Path to the source PDF（可省略：无文件直接启动，用于历史记录管理）

options:
  -h, --help            show this help message and exit
  --engine {llama,vllm,paddle}
                        推理引擎：llama（llama.cpp，默认）或 vllm（vLLM-Omni）；缺省用
                        config.json 的 engine 键
  --title TITLE         EPUB title (default: auto from PDF metadata)
  --author AUTHOR       EPUB author
  --lang LANG           EPUB language code (default: zh-CN)
  --out-dir OUT_DIR     Output directory for OEBPS/ and the EPUB (default:
                        data/<pdf stem>/)
  --epub-path EPUB_PATH
                        Explicit output path for the .epub file
  --correct-timeout CORRECT_TIMEOUT
                        浏览器被关闭后自动继续后续流程的等待秒数（默认 600=10 分钟）
```

### model

```text
usage: ptoe model [-h] action ...

Manage available OCR model choices and the persistent selected model

positional arguments:
  action
    list      List available model keys and details
    show      Show current selected model key and detail
    set       Set selected model key in config.json
    add       Add a model choice (key + name + mmproj)
    remove    Remove a model choice
    rm        Alias for remove

options:
  -h, --help  show this help message and exit
```

### config

```text
usage: ptoe config [-h] action ...

快捷查看或修改 config.json 中的配置项

positional arguments:
  action
    set       修改配置项（key=value）

options:
  -h, --help  show this help message and exit
```

### gui

```text
usage: ptoe gui [-h] [--host HOST] [--port PORT] [--no-browser]
                [--idle-timeout IDLE_TIMEOUT]

启动本地 HTTP 服务并在 pywebview 内置窗口中打开配置操作界面（config.json 的 gui_display=browser 时改用浏览器打开）：查看/修改配置、启动/停止推理服务、选择文件路径等。界面关闭超过 idle-timeout
秒后自动退出；pywebview 窗口直接关闭即退出。pywebview 不可用时自动回退浏览器。pywebview 窗口默认**最大化**（config `window_maximized=false` 可关闭）；若「文字矫正」界面（`correct`/`epub --correct`）也已启动，两者合并为**同一个多标签窗口**（顶/底紧凑标签栏，`config set tabs_position bottom` 切换并自动保存；先启动者为主窗口，主窗口关闭后其余应用自动接管重建窗口）。

options:
  -h, --help            show this help message and exit
  --host HOST           监听地址（默认 127.0.0.1）
  --port PORT           监听端口（默认 0=自动分配）
  --no-browser          不自动打开界面（窗口/浏览器均不打开）
  --idle-timeout IDLE_TIMEOUT
                        界面关闭后自动退出的等待秒数（默认 120）
```

### stop

```text
usage: ptoe stop [-h] [--engine {llama,vllm,paddle}]

关闭正在运行的推理服务进程并释放端口：本进程启动的实例直接终止，上次运行遗留/外部启动的实例按配置端口兜底关闭（Windows
netstat+taskkill）。

options:
  -h, --help            show this help message and exit
  --engine {llama,vllm,paddle}
                        推理引擎：llama（llama.cpp，默认）或 vllm（vLLM-Omni）；缺省用
                        config.json 的 engine 键
```

Note: To keep documentation authoritative, re-run the capture step and paste the updated help blocks when the CLI changes.
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

When enabled, a local HTTP server starts and opens the correction UI (a pywebview window by default; `gui_display=browser` uses the system browser):

- **Left**: Page image preview (click to toggle full-res)
- **Right**: Editable OCR text (one `<div>` per line)
- **Toolbar**: Format (bold, italic, headings h1–h6, alignment, notes), markers (article break, paragraph merge), search/replace, proofreading, image insert, export TXT/DOCX/EPUB
- **Virtual List**: Handles 1000+ pages smoothly
- **Undo/Redo**: Ctrl+Z / Ctrl+Y (10 steps)
- **History**: Versioned cache at `data/correction_history/` (20 versions per PDF)
- **Auto-continue**: Closing the window/tab continues the pipeline after `--correct-timeout` (default 600s)

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

pywebview-window interface by default (config `gui_display=browser` switches to the system browser) for:
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
