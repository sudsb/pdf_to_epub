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
  "selected_model": "HY",
  "ocr_prompt": "请逐行完整识别图片中的全部文字，逐字输出，不得遗漏任何内容、不得省略、不得总结、不得翻译",
  "llama_server_args": {
    "host": "127.0.0.1",
    "port": "8080",
    "temperature": "0",
    "repeat_penalty": "1.1",
    "parallel": "11",
    "cache_type_k": "q8_0",
    "cache_type_v": "q8_0",
    "log_verbosity": "0"
  }
}
```
检查方式：
```powershell
Test-Path E:/xox/Tools/llama-c/llama-server.exe
Test-Path E:/model
```
两者都要 `True`，且 `E:/model` 下有对应的 `.gguf` 模型文件和 `.mmproj` 多模态投影文件。

`ocr_prompt`：OCR 提示词（与 `llamamanage.OCR_PROMPT` 同值，可用下面的 `config set ocr_prompt` 修改）；`llama_server_args`：llama-server 启动参数（host/port/temperature/repeat_penalty/parallel/cache_type_k/cache_type_v/log_verbosity，缺失的键跳过、回退 llama-server 内置默认；可选 `n_gpu_layers` 键覆盖 GPU 自动检测）。**这两个键缺省时程序会在首次运行 `get_config()` 时自动补全并写入默认值**，无需手动添加。

**用命令行查看/修改配置（不用手改 JSON）：**
```powershell
uv run python mian.py config show                      # 查看当前配置（全部键：路径/模型/提示词/启动参数）
uv run python mian.py config set llama_server <path>   # 修改 llama-server.exe 路径
uv run python mian.py config set models_dir <path>     # 修改模型目录
uv run python mian.py config set selected_model <key>  # 切换默认模型（键必须是 model_choices 里已有的）
uv run python mian.py config set ocr_prompt <text>     # 修改 OCR 提示词
```
`config set` 接受 `llama_server` / `models_dir` / `selected_model` / `ocr_prompt` 四个顶层键，以及点分路径 `llama_server_args.<参数>`（如 `config set llama_server_args.parallel 11`，含可选的 `n_gpu_layers` GPU 覆盖）；`selected_model` 会校验必须存在于 `model_choices`。嵌套参数也可用 Python API 修改：
```python
from configmanage import set_llama_server_arg, set_ocr_prompt
set_llama_server_arg("parallel", "11")   # 修改嵌套键并原子写盘
set_ocr_prompt("自定义提示词")            # 修改 OCR 提示词（等价于 config set ocr_prompt）
```模型注册表（`model_choices` 的增删）用 `model` 子命令：
```powershell
uv run python mian.py model list                                       # 列出可用模型键与详情
uv run python mian.py model show                                       # 查看当前选中模型
uv run python mian.py model set <key>                                  # 设置默认模型（等价于 config set selected_model）
uv run python mian.py model add <key> --name <gguf> --mmproj <mmproj>  # 注册新模型（--force 覆盖已有键）
uv run python mian.py model remove <key>                               # 删除模型（若删的是当前选中模型会自动换回剩余模型）
```

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
| `--model` | config.json 的 `selected_model`（本机为 DOTS4） | config.json `model_choices` 里的模型键 |
| `--workers` | 3 | OCR 并发线程数，建议 ≤ 服务器 `--parallel`（config.json `llama_server_args.parallel`，默认 11）。并发留足 slot 余量，可降低 GPU 争抢与超时风险 |
| `--timeout` | 600 | 单次识别请求读超时（秒）。300dpi 下 4 并发总耗时可能超 1 分钟，别改小 || `--thinking` | 关 | 开启思维链模式（此时不附加"按原文原格式输出"指令） |
| `--correct` | 关 | 开启手动矫正：在浏览器中逐页对照原图与识别文字，可标记粗体/斜体/标题（详见第六节）。默认关闭，不改变既有流程 |
| `--correct-timeout` | 600 | （仅 `--correct` 生效）浏览器被关闭后自动继续后续流程的等待秒数，默认 600=10 分钟 |
| `--title` / `--author` | PDF 文件名 / 空 | EPUB 元数据 |
| `--lang` | zh-CN | EPUB 语言 |
| `--out-dir` | `data/<pdf名>/` | XHTML/OEBPS 输出目录 |
| `--epub-path` | `data/<pdf名>/<书名>.epub` | EPUB 输出路径 |

**模型输出自动处理**：`--model` 换成 PaddleOCR 系的 ULQ4/ULQ8 时，其输出格式为 `label [x,y,x,y] 文本`，ptoe 会自动解析——`title` 行转成标题（进 EPUB 目录/TOC）、`text` 行转成段落、`page_number`/`figure`/`image` 行丢弃；其余模型输出的纯文本中，页首/页尾的独立页码行（`第N页`、`- 4 -`、`— 5 —`、`6 / 12`、纯数字）也会被默认删除，页面中间的年份/数字不受影响。

顶层选项：`--version` 查版本；**无参数时若 stdin 为交互终端（含打包 exe 双击启动）会进入终端菜单**（PDF→EPUB 转换 / 手动矫正 / 配置 / 模型管理 / 帮助 / 退出）；非交互（管道/重定向）打印 `<name> <version> — nothing to do` 并退出。

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

## 六、手动矫正（可选，默认关闭）

OCR 完成后、生成 EPUB 前，可以在浏览器中逐页对照**原图**与**识别文字**，人工修正识别误差，并选中文字标记格式（粗体、斜体、一级/二级/三级标题等）。默认流程**不开启**；只有加 `--correct` 才进入矫正：

```powershell
uv run python mian.py epub "E:\MYBooks\books\毛泽东\主席与毛远新同志谈话纪要.pdf" --correct
```

1. 程序自动启动本地服务（127.0.0.1 随机端口）并在默认浏览器打开界面；
2. 界面**左侧为原图**（低分辨率预览，**点击图片**可切换为原图；没有分割图片时自动用 PDF 高分辨率渲染，保证原图始终可看），**右侧为可编辑的识别文字**，一页一图一文；文字按行显示，保留原始段落结构（不会挤成一整段）。1000+ 页的大书采用虚拟列表，只渲染屏幕附近的行，滚动流畅不卡顿（编辑后页面也不会跳动）；
3. 直接修改错字；**选中文字会自动弹出快捷菜单**，可设粗体/斜体/一级~六级标题；也可点「**快捷键**」把每个操作绑定到自己顺手的组合键（一个快捷键绑定一个操作，保存在浏览器里）；
4. **全文标记**（插入到光标处）：当前文章到此结束，之后的内容属于**新的一篇文章**，生成 EPUB 时自动**开新的一页**；
5. **段落标记**（插入到光标处）：OCR 把一个整段拆成两段时，在断口处放一个标记即可拼回整段；放在**段首** → 与上一段**合并**，放在**段尾** → 与下一段**合并**；
6. **注释格式**（整段转小字注释）+ **注释标记**（插入到光标处）：注释标记的位置由对应的注释段落替换，自动用**中文括号（ ）**括起（注释内部括号统一为中文括号；注释本身已带括号、视为已在正文中时只改字号不再加括号）；**一个注释段落对应一个注释标记**，数量不匹配时转换会提示原因；
7. 点「**暂存**」把当前修改**暂时保存到本地历史缓存**（`data/correction_history/`，按 PDF 哈希，**每次生成一个新版本**）；「**保存**」**不新建版本**，直接**覆盖当前缓存**（同一份内容反复保存只更新同一个文件，历史列表不会被保存刷屏）；
8. 点「**完成并转换**」**不会关闭界面**：每次点击都重新转换（弹窗提示**转换完成/未完成**并询问是否关闭当前页面，浏览器禁止脚本自动关闭时请手动关闭标签页），可留在页面继续修改后**再次点击**。

**更多工具（2026-08）：**
- **文本清理**（工具栏「清理」）：一键合并 OCR 拆散的小段落（**已设标题或带格式/标记/图片的段落不参与合并**；以句号、感叹号、问号或闭合括号/书名号 `（）】」』》〉` 结尾的完整段落也不合并）、清除段首 `#`/`*` 及残留的 `**` 加粗符号、归一化中英文标点（汉字旁的半角标点转全角，字母/数字间的全角标点转半角）、移除残留的 HTML 标签；
- **图片插入**：左栏「图」按钮把当前显示的原图插入到右侧文字光标处（居中显示）；工具栏「图片」选择**全画幅**（占满文字宽度）或**局部**（按原尺寸居中），EPUB 中图片自动提取进 `OEBPS/Images/`；
- **搜索替换**：工具栏「**搜**」按钮弹出搜索窗口：输入搜索词（支持正则，勾选「正则」）后点「搜索」，窗口列出所有匹配结果（匹配处前后约 40 字 + 所在页码 + 黄色高亮，最多 200 条），**点击结果直接跳转**到对应页；用「↑ / ↓」按钮在**上一个/下一个**匹配之间循环跳转；「**替换当前**」只替换当前选中的那处匹配（其余不动），「**全部替换**」把所有匹配一次替换完（只替换文字内容，不动格式标签）；
- **提示延迟**：「快捷键」设置里可调整悬停提示的响应时间（默认 600ms，0 为立即显示），提示会附带对应快捷键。
- **滚动稳定**：编辑/插入图片后页面不再跳动到附近页，大幅滚轮也不会被拉回正在处理的页面；滑动翻页（含触屏惯性滑动、拖滚动条、键盘翻页）过程中不会被反复拉回当前页。
- **撤回/前进**：工具栏「操作」组 ↶/↷ 按钮，或快捷键 **Ctrl+Z 撤回**、**Ctrl+Y / Ctrl+Shift+Z 前进**，可对最近 **10 次**以内的操作连续撤回和前进（连续打字合并为一步，格式/标记/对齐/搜索替换/智能清理/繁简转换/插入图片各算一步）；撤回后可再前进，产生新操作后前进记录清空。
- **导出 TXT/DOCX**：工具栏「**导出**」按钮弹出窗口选择格式（导出为 DOCX / 导出为 TXT），随后由**系统保存对话框**选择存放位置和文件名（对话框置顶显示，不会被浏览器窗口遮挡）；导出内容包含当前全部页面（**含尚未保存的修改**）；TXT 为带 BOM 的 UTF-8（Windows 记事本可直接打开），DOCX 中标题（1-6 级）为加粗加大并带大纲级别，正文中的换行保留。

**关闭浏览器也能继续**：界面会每 30 秒向本地服务发一次心跳，关闭标签页/浏览器时发关闭信标；程序检测到浏览器已关闭超过 `--correct-timeout` 秒（默认 10 分钟）后，会自动继续后续流程（保留最后一次保存/暂存/完成的内容）。电脑休眠/恢复不会误判。

中途 Ctrl+C 可放弃矫正、按原识别结果继续。矫正提交的内容只放行白名单标签（段落/标题/粗体/斜体/注释），script、style、属性等一律被清洗。

### 直接启动手动矫正（不跑 OCR）

```powershell
uv run python mian.py correct "E:\MYBooks\books\毛泽东\主席与毛远新同志谈话纪要.pdf" [--title TITLE] [--author AUTHOR] [--correct-timeout 600]
uv run python mian.py correct   # 无文件直接启动：空白界面，用于历史记录管理/手动录入
```

直接打开矫正界面（不做 OCR）：页面文本**优先取本地历史缓存最新版本**（同一 PDF 上次矫正/暂存的内容），无历史则为空白页可手动录入；每次点「完成并转换」都会重新生成 EPUB，可反复修改反复转换。**不带 PDF 参数可无文件直接启动**（此时无内容可转换，仅用于历史记录管理）。

### 历史矫正记录

矫正内容按 PDF 自动缓存到 `data/correction_history/`：**每个文件保留多个版本**（暂存/完成时各生成一个新版本，文件名 `<PDF哈希>_<时间戳>_<随机>.json`，每文件最多保留最近 20 个版本；「保存」不新建版本、直接覆盖当前最新版本）。下次对同一 PDF 运行 `--correct` 或 `correct` 命令，自动加载**最新版本**，**支持对已修改过的内容再次手动矫正**。

矫正界面工具栏新增「**历史记录**」按钮：弹窗列出所有历史缓存，**文件名与路径分列显示**（同名不同路径的文件可区分），同一文件的多个版本按时间编号（v1=最新）；支持**勾选单个/多个删除**，也可**全部删除**。

### EPUB 目录与标题

正文中的标题（一级~六级，含矫正时标记的小标题）会自动加锚点并**一一对应**进 EPUB 目录（EPUB3 `nav.xhtml` 与 EPUB2 `toc.ncx`），支持正常目录跳转；小标题**居中显示**（正文不再重复插入书名的多个一级标题，分卷用「书名（第N部分）」）。

## 七、服务器与并发说明

- 服务器由程序自动拉起（先探测 `127.0.0.1:8080/health`，已运行则复用），监听 `127.0.0.1:8080`。启动参数从 config.json 的 `llama_server_args` 读取（默认 `--temperature 0 --repeat-penalty 1.1 --parallel 11 --cache-type-k q8_0 --cache-type-v q8_0 --log-verbosity 0`；日志只输出 ERROR 级，`find_slot`/`print_timing` 等刷屏信息已屏蔽，进度由 ptoe 打印）。检测到 GPU 时自动追加 `--n-gpu-layers 999`，配置里显式给出 `n_gpu_layers` 键则覆盖自动检测。
- 页面间共享 KV cache：同一批页面重跑时，缓存命中的页几乎瞬时完成。
- **服务端进程独立于 CLI 存活**。不想让它常驻可手动终止，或调用：
  ```powershell
  uv run python -c "from llamamanage import stopserver; stopserver()"
  ```

## 八、常见问题

| 现象 | 原因与对策 |
|------|-----------|
| 启动卡住、弹出文件选择框 | `config.json` 路径不存在 → 按第二节用 `config set llama_server/models_dir` 指到真实路径 |
| 全部页面报 `Read timed out` | 并发多/图大导致单请求超时 → 默认 600s；仍超时则加 `--timeout 900` 或减 `--workers` |
| 加密 PDF 报 `RuntimeError` | 有密码的 PDF 打不开 → 先解密再转换 |
| 输出目录重名 | 自动加后缀：`data/书名_1/` |
| 识别慢 / 页数越多单页越慢 | 确认已启用 GPU（`--n-gpu-layers 999` 自动检测）；长时间满载笔记本 GPU 会热降频，页多时更明显 → 用默认 0 档（约 3s/张）或降到 1 档即可，字符数几乎不降 |
| 个别页面超时 | 每页有独立 `--timeout`（默认 600s）；2 档内容多时单页可达 50-130s，若并发排队累积可能超时 → 降档位或减 `--workers`。KV cache 按 slot 预分配、请求结束即释放，**不会**因历史页数累积占显存，无需清理 |

## 九、测试

```powershell
uv run python -m unittest test_pdfmanage test_mian test_correctmanage test_llamamanage test_stringmanage   # 41 tests pass + 1 import error（test_correctmanage 缺 convert_text_html，重构中）
uv run python test_image_queue_request.py            # 队列+请求脚本测试，打印 test passed
# test_config_llama.py 是 pytest 风格，需要先 uv add --dev pytest
```

## 十、API 方式调用（跳过 CLI）

```python
from mian import pdf_to_epub
result = pdf_to_epub(
    "E:/MYBooks/books/毛泽东/主席与毛远新同志谈话纪要.pdf",
    dpi=100, model_key="HY", max_workers=3, title="主席与毛远新同志谈话纪要",
)
print(result["epub"])   # EPUB 路径

# 手动矫正：correct=True（会阻塞等待浏览器操作完成）
result = pdf_to_epub("xxx.pdf", correct=True)
```

或分步用：`pdfmanage.split_pdf_to_images` → `llamamanage.batch_infer` → `stringmanage.clean_and_structure_text` → `htmlmanage.HTMLConverter`。

## 十一、打包为独立 exe 与终端菜单

**`pack.ps1`**（项目根目录）用 PyInstaller 把 `mian.py` 打包成单个独立 exe（`dist\ptoe.exe`，onefile + console，无需 Python 环境）：

```powershell
powershell -ExecutionPolicy Bypass -File .\pack.ps1
```

打包要点：
- **onefile + console**：单文件、终端程序；`pyproject.toml` 一并打入，`--version` 显示正确版本号；`pymupdf`/`requests`/`zhconv`/`tkinter` 全部内置；`--noupx` 避免杀软误报。
- **双击 `ptoe.exe` 启动终端菜单**（无参数 + 交互终端即进入）：
  ```
  1) PDF → EPUB 转换（OCR 全流程）   ← 交互式填写 PDF 路径/模型/DPI/并发/是否矫正
  2) 手动矫正（correct，不跑 OCR）
  3) 查看/修改配置（config）
  4) 模型管理（model）
  5) 帮助（CLI 用法）
  0) 退出
  ```
  退出时停在「按回车键继续…」，控制台窗口不会一闪而过。
- **命令行用法与原 CLI 一致**：`ptoe.exe epub <pdf> [--dpi 0] [--model <key>] ...`、`ptoe.exe correct [<pdf>]`、`ptoe.exe config show` 等；无参数但 stdin 非交互（管道）时仍打印 `nothing to do`。
- **首次运行**：保证 exe 所在目录（双击时 CWD 即 exe 目录）有 `config.json`，且 `llama_server` / `models_dir` 指向有效路径（见第二节）。
- 依赖说明：脚本用 `uv run --with pyinstaller`，首次构建自动拉取 pyinstaller，**不写入** `pyproject.toml` / `uv.lock`。
