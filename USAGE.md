# ptoe 使用教程（PDF → OCR → EPUB）

## 一、环境准备

**前置要求：**
- Windows（llama-server 路径是 Windows 风格）
- Python ≥ 3.11 + [uv](https://docs.astral.sh/uv/)（项目用 `uv` 管理依赖）
- llama.cpp 服务端 `llama-server.exe` + 一个多模态 OCR 模型（本项目用 **HunyuanOCR** 的 GGUF + mmproj）

**同步依赖（首次或换机后）：**
```powershell
cd D:\code-project\python\PToEA
uv sync
```
依赖仅 3 个：`pymupdf`（PDF 分页）、`requests`（HTTP）、`zhconv`（繁简转换）。

## 二、配置 config.json（关键步骤）

程序运行时会自动创建/修复 `config.json`，但**如果 `llama_server` / `models_dir` 指向的路径不存在，启动时会弹出 tkinter 文件选择对话框并阻塞等待**。所以先手动确认：

```jsonc
// config.json（本机已配置好的样子）
{
  "llama_server": "E:/xox/Tools/llama-c/llama-server.exe",   // 必须存在
  "models_dir": "E:/model",                                   // 必须存在
  "model_choices": {
    "HY": "huiyuan/HunyuanOCR.BF16.gguf",                     // 模型名可带子目录前缀
    // ...
  },
  "selected_model": "HY"
}
```
检查方式：
```powershell
Test-Path E:/xox/Tools/llama-c/llama-server.exe
Test-Path E:/model
```
两者都要 `True`，且 `E:/model` 下有对应的 `.gguf` 模型文件和 `.mmproj` 多模态投影文件。

## 三、基本用法（一条命令跑通）

```powershell
uv run python mian.py epub "E:\MYBooks\books\毛泽东\主席与毛远新同志谈话纪要.pdf" --title "主席与毛远新同志谈话纪要"
```

程序自动完成 4 步并打印进度：
```
[1/4] Splitting PDF to images (dpi=200) ...      → 12 page(s) -> data/主席与毛远新同志谈话纪要/
[2/4] OCR via llama-server (model='HY', workers=3) ...   → 自动启动服务器，3 路并发识别
[3/4] Structuring text (12 page(s)) ...
[4/4] Rendering XHTML and packing EPUB ...
Done: D:\code-project\python\PToEA\data\主席与毛远新同志谈话纪要\主席与毛远新同志谈话纪要.epub
```

## 四、参数详解

| 参数 | 默认 | 说明 |
|------|------|------|
| `pdf` | — | 源 PDF 路径（必填） |
| `--dpi` | 0 | 分页渲染分辨率档位（0-4）：0=100、1=150、2=200、3=300、4=600。档位越高图片 token 越多（0 档 ≈ 1000 图像 token/页，2 档 ≈ 4600，4 档 ≈ 8700），识别越精细但越慢。实测 HY 模型 0 档≈3s/张、1 档≈5s/张、2 档≈10s/张、3 档≈35s/张，字符数几乎不降 |
| `--model` | HY | config.json `model_choices` 里的模型键 |
| `--workers` | 3 | OCR 并发线程数，建议 ≤ 服务器 `--parallel`（默认 4）。3 并发留 1 slot 余量，可降低 GPU 争抢与超时风险 |
| `--timeout` | 600 | 单次识别请求读超时（秒）。300dpi 下 4 并发总耗时可能超 1 分钟，别改小 || `--thinking` | 关 | 开启思维链模式（此时不附加"按原文原格式输出"指令） |
| `--title` / `--author` | PDF 文件名 / 空 | EPUB 元数据 |
| `--lang` | zh-CN | EPUB 语言 |
| `--out-dir` | `data/<pdf名>/` | XHTML/OEBPS 输出目录 |
| `--epub-path` | `data/<pdf名>/<书名>.epub` | EPUB 输出路径 |

顶层选项：`--version` 查版本；无参数时打印 `<name> <version> — nothing to do` 并退出。

## 五、完整实战流程

```powershell
# 1. 确认配置路径有效（见第二节）
Test-Path E:/xox/Tools/llama-c/llama-server.exe

# 2. 执行转换（12 页实测约几分钟，视显卡而定）
uv run python mian.py epub "E:\MYBooks\books\毛泽东\主席与毛远新同志谈话纪要.pdf" `
  --title "主席与毛远新同志谈话纪要" --author "毛泽东" --dpi 0 --workers 3

# 3. 检查产物
Get-ChildItem "data/主席与毛远新同志谈话纪要"   # 含 1.png...12.png + OEBPS/ + .epub
```

**输出的 EPUB 结构**（zip 内）：`mimetype`（首项、无压缩）、`META-INF/container.xml`、`OEBPS/content.opf`、`OEBPS/nav.xhtml`、`OEBPS/Text/content_1.xhtml...`、`OEBPS/Styles/style.css`、`OEBPS/Images/`。标准 EPUB3，电子书阅读器/Calibre 可直接打开。

## 六、服务器与并发说明

- 服务器由程序自动拉起（先探测 `127.0.0.1:8080/health`，已运行则复用），监听 `127.0.0.1:8080`，参数 `--temperature 0 --repeat-penalty 1.1 --parallel 4`。
- 页面间共享 KV cache：同一批页面重跑时，缓存命中的页几乎瞬时完成。
- **服务端进程独立于 CLI 存活**。不想让它常驻可手动终止，或调用：
  ```powershell
  uv run python -c "from llamamanage import stopserver; stopserver()"
  ```

## 七、常见问题

| 现象 | 原因与对策 |
|------|-----------|
| 启动卡住、弹出文件选择框 | `config.json` 路径不存在 → 按第二节把 `llama_server`/`models_dir` 指到真实路径 |
| 全部页面报 `Read timed out` | 并发多/图大导致单请求超时 → 默认 600s；仍超时则加 `--timeout 900` 或减 `--workers` |
| 加密 PDF 报 `RuntimeError` | 有密码的 PDF 打不开 → 先解密再转换 |
| 输出目录重名 | 自动加后缀：`data/书名_1/` |
| 识别慢 / 页数越多单页越慢 | 确认已启用 GPU（`--n-gpu-layers 999` 自动检测）；长时间满载笔记本 GPU 会热降频，页多时更明显 → 用默认 0 档（约 3s/张）或降到 1 档即可，字符数几乎不降 |
| 个别页面超时 | 每页有独立 `--timeout`（默认 600s）；2 档内容多时单页可达 50-130s，若并发排队累积可能超时 → 降档位或减 `--workers`。KV cache 按 slot 预分配、请求结束即释放，**不会**因历史页数累积占显存，无需清理 |

## 八、测试

```powershell
uv run python -m unittest test_pdfmanage test_mian   # 10 tests，全部通过
uv run python test_image_queue_request.py            # 队列+请求脚本测试，打印 test passed
# test_config_llama.py 是 pytest 风格，需要先 uv add --dev pytest
```

## 九、API 方式调用（跳过 CLI）

```python
from mian import pdf_to_epub
result = pdf_to_epub(
    "E:/MYBooks/books/毛泽东/主席与毛远新同志谈话纪要.pdf",
    dpi=100, model_key="HY", max_workers=3, title="主席与毛远新同志谈话纪要",
)
print(result["epub"])   # EPUB 路径
```

或分步用：`pdfmanage.split_pdf_to_images` → `llamamanage.batch_infer` → `stringmanage.clean_and_structure_text` → `htmlmanage.HTMLConverter`。
