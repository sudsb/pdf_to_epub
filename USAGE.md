# ptoe 使用教程（PDF → OCR → EPUB）

## 一、环境准备

**前置要求：**
- Windows（llama-server 路径是 Windows 风格）
- Python ≥ 3.11 + [uv](https://docs.astral.sh/uv/)（项目用 `uv` 管理依赖）
- 推理引擎（可选）：
  - **llama.cpp**：`llama-server.exe` + 多模态 GGUF 模型。用于 OCR 与深度校对；程序可按 config/运行时自动拉起/复用服务。
  - **vLLM-Omni**：`vllm serve`（默认端口 8000），适合带 GPU 的环境。
  - **PaddleOCR（本地）**：本地 PaddleOCR 引擎。可通过命令行 `--engine paddle` 临时在 PDF→EPUB 的 OCR 阶段启用（运行时覆盖，仅影响 OCR 阶段；不会写入 config.json）。文本矫正/深度校对仍走 llama-server/vLLM（大模型）。

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
  "engine": "llama",                                          // 推理引擎：llama（默认）| vllm
  "llama_server": "E:/xox/Tools/llama-c/llama-server.exe",   // 必须存在
  "models_dir": "E:/model",                                   // 必须存在
  "model_choices": {
    "HY": "huiyuan/HunyuanOCR.BF16.gguf",                     // 模型名可带子目录前缀
    // ...
  },
  "llama_server_args": {
    "host": "127.0.0.1",
    "port": "8080",
    "temperature": "0",
    "repeat_penalty": "1.1",
    "parallel": "4",       // 服务端槽位数：KV cache 总量 ≈ ctx × parallel，默认 4；流程运行时会按实际并发自动取 min
    "cache_type_k": "q8_0",
    "cache_type_v": "q8_0",
    "log_verbosity": "0",
    "max_tokens": "8192",   // 单页输出/请求上限（默认 8192，程序将把此值传给模型并用于 HTTP 请求的 max_tokens）
    "ngram_size": "30",     // 防重复 ngram 大小（默认 30）
    "window_size": "90"     // 防重复滑动窗口（默认 90）
  },
  "vllm_server": "",                                          // vLLM-Omni 可执行文件路径；空 = 连接模式（不自动拉起）
  "vllm_server_args": {                                       // engine=vllm 时的启动参数
    "host": "127.0.0.1",
    "port": "8000",
    "max_model_len": "32768",
    "extra_args": ""
  }
}
```
检查方式：
```powershell
Test-Path E:/xox/Tools/llama-c/llama-server.exe
Test-Path E:/model
```
两者都要 `True`，且 `E:/model` 下有对应的 `.gguf` 模型文件和 `.mmproj` 多模态投影文件。

`ocr_prompt`：OCR 提示词（与 `llamamanage.OCR_PROMPT` 同值，可用下面的 `config set ocr_prompt` 修改）；`llama_server_args`：llama-server 启动参数（host/port/temperature/repeat_penalty/parallel/cache_type_k/cache_type_v/log_verbosity 等，新增支持 `max_tokens`/`ngram_size`/`window_size` 用于输出上限与防重复策略；缺失键跳过、回退 llama-server 内置默认；可选 `n_gpu_layers` 键覆盖 GPU 自动检测；可选 `flash_attn` 键：`"0"`/`false` 禁用、缺省自动、`"1"`/`"on"` 强制开启 GPU 下的 Flash Attention）。**这两个键缺省时程序会在首次运行 `get_config()` 时自动补全并写入默认值**，无需手动添加.

**各模型可配置推荐 OCR 并发**（`model_choices.<key>.workers`，可选）：未显式指定 `--workers` 时按此值运行，缺省 3。依据模型大小/量化选值——大模型（如 HY BF16）显存压力大，并发过高会让多槽位 KV 缓存溢出到 CPU、单张耗时反而大涨，建议 2-3；小模型（如 QWEN.8 0.8B）可 6+。GUI 设置页「模型管理」表格可直接调整（含「并发」列）。

**用命令行查看/修改配置（不用手改 JSON）：**
```powershell
uv run python mian.py config show                      # 查看当前配置（全部键：路径/模型/提示词/启动参数）
uv run python mian.py config set llama_server <path>   # 修改 llama-server.exe 路径
uv run python mian.py config set models_dir <path>     # 修改模型目录
uv run python mian.py config set selected_model <key>  # 切换默认模型（键必须是 model_choices 里已有的）
uv run python mian.py config set ocr_prompt <text>     # 修改 OCR 提示词
uv run python mian.py config set engine vllm           # 切换推理引擎（llama | vllm）
uv run python mian.py config set vllm_server <path>    # 设置 vLLM-Omni 可执行文件路径
uv run python mian.py config set vllm_server_args.port 8000  # 修改 vLLM-Omni 端口（嵌套参数）
```
注：若需临时使用 PaddleOCR，可在命令行加 `--engine paddle`（仅影响 OCR 阶段；config set engine 仍只接受 `llama|vllm`）。
`config set` 接受 `llama_server` / `models_dir` / `selected_model` / `ocr_prompt` / `engine` / `vllm_server` 顶层键，以及点分路径 `llama_server_args.<参数>`（如 `config set llama_server_args.parallel 11`，同样可设置 `max_tokens`/`ngram_size`/`window_size` 等）与 `vllm_server_args.<参数>`（如 `config set vllm_server_args.port 8000`）；`selected_model` 会校验必须存在于 `model_choices`，`engine` 仅接受 `llama` / `vllm`。嵌套参数也可用 Python API 修改：
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
[2/4] OCR via llama-server (model='HY', workers=auto) ...   → 自动启动服务器，并发按模型推荐（workers=2）
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
| `--engine` | config.json 的 `engine`（默认 llama） | 推理引擎：`llama`（llama.cpp）或 `vllm`（vLLM-Omni）。仅本次运行生效，不写入 config.json |
| `--workers` | 模型推荐（`model_choices.<key>.workers`，未配置时 3） | OCR 并发线程数。运行时服务端 `--parallel` 自动取 min(配置, 并发)——槽位不多于实际并发，避免 KV cache 按槽位预分配浪费显存（溢出到 CPU 反而变慢）。显存充足可显式调大（如 6） |
| `--timeout` | 600 | 单次识别请求读超时（秒）。300dpi 下 4 并发总耗时可能超 1 分钟，别改小 || `--thinking` | 关 | 开启思维链模式（此时不附加"按原文原格式输出"指令） |
| `--correct` | 关 | 开启手动矫正：在浏览器中逐页对照原图与识别文字，可标记粗体/斜体/标题（详见第六节）。默认关闭，不改变既有流程 |
| `--correct-timeout` | 600 | （仅 `--correct` 生效）浏览器被关闭后自动继续后续流程的等待秒数，默认 600=10 分钟 |
| `--title` / `--author` | PDF 文件名 / 空 | EPUB 元数据 |
| `--lang` | zh-CN | EPUB 语言 |
| `--out-dir` | `data/<pdf名>/` | XHTML/OEBPS 输出目录 |
| `--epub-path` | `data/<pdf名>/<书名>.epub` | EPUB 输出路径 |

**模型输出自动处理**：`--model` 换成 PaddleOCR 系的 ULQ4/ULQ8 时，其输出格式为 `label [x,y,x,y] 文本`，ptoe 会自动解析——`title` 行转成标题（进 EPUB 目录/TOC）、`text` 行转成段落、`page_number`/`figure`/`image` 行丢弃；其余模型输出的纯文本中，页首/页尾的独立页码行（`第N页`、`- 4 -`、`— 5 —`、`6 / 12`、纯数字）也会被默认删除，页面中间的年份/数字不受影响。

顶层选项：`--version` 查版本；**无参数时若 stdin 为交互终端（含打包 exe 双击启动）会进入终端菜单**（PDF→EPUB 转换 / 手动矫正 / 配置 / 模型管理 / 继续识别 / 帮助 / 停止推理服务 / 退出）；非交互（管道/重定向）打印 `<name> <version> — nothing to do` 并退出。

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
6. **注释格式**（整段转小字注释）+ **注释标记**（插入到光标处），两种用法：
   - **文中没有任何注释标记时**：注释段落**原位保留**，只套用注释格式（小字），**不会移动位置、也不会报错**——只想把某几段变成小字注释时，不必再逐条打标记；
   - **文中存在注释标记时**：注释标记与注释**一一对应**（**一段注释算一个注释标记**），标记所在位置由对应的注释段落替换；数量不匹配时转换会提示原因；
   - **因分页被折断的注释**：注释的后半段（通常在下一页开头）**放一个段落标记**即可与前半段**合并成一条**，再整体插入正文；
   - 插入正文的注释自动用**中文括号（ ）**包裹并使用**注释小字**（注释内部括号统一为中文括号；注释本身已带括号、视为已在正文中时只改字号不再加括号）；
7. 点「**暂存**」把当前修改**暂时保存到本地历史缓存**（`data/correction_history/`，按 PDF 哈希，**每次生成一个新版本**）；「**保存**」**不新建版本**，直接**覆盖当前缓存**（同一份内容反复保存只更新同一个文件，历史列表不会被保存刷屏）；
8. 点「**完成并转换**」**不会关闭界面**：每次点击都重新转换（弹窗提示**转换完成/未完成**并询问是否关闭当前页面，浏览器禁止脚本自动关闭时请手动关闭标签页），可留在页面继续修改后**再次点击**。

**更多工具（2026-08）：**
- **文本清理**（工具栏「清理」）：一键合并 OCR 拆散的小段落（**已设标题或带格式/标记/图片的段落不参与合并**；以句号、感叹号、问号或闭合括号/书名号 `（）】」』》〉` 结尾的完整段落也不合并）、清除段首 `#`/`*` 及残留的 `**` 加粗符号、归一化中英文标点（汉字旁的半角标点转全角，字母/数字间的全角标点转半角）、移除残留的 HTML 标签；
- **图片插入**：左栏「图」按钮把当前显示的原图插入到右侧文字光标处（居中显示）；工具栏「图片」选择**全画幅**（占满文字宽度）或**局部**（按原尺寸居中），EPUB 中图片自动提取进 `OEBPS/Images/`；
- **搜索替换**：工具栏「**搜**」按钮弹出搜索窗口：输入搜索词（支持正则，勾选「正则」）后点「搜索」，窗口列出所有匹配结果（匹配处前后约 40 字 + 所在页码 + 黄色高亮，最多 200 条），**点击结果直接跳转**到对应页；用「↑ / ↓」按钮在**上一个/下一个**匹配之间循环跳转；「**替换当前**」只替换当前选中的那处匹配（其余不动），「**全部替换**」把所有匹配一次替换完（只替换文字内容，不动格式标签）；
- **文字纠错**（工具栏「**校**」按钮，下拉菜单，**均只作用于当前页**）：自动检测半角标点、连续重复字、中文引号不配对、常见 OCR 混淆字（含词典未知词检测与中英混排），错字以**删除线（红）+ 候选字（绿）**标注，点击标注可**采纳**（替换为候选字并记入本地词表，下次不再误报）或**忽略**。下拉菜单四项：
  - **校正**：对当前页执行纠错并叠加标注；
  - **应用**：把当前页**全部有候选**的错误替换为候选首位（已应用的文字/词句保留）；
  - **清除**：清除当前页的纠错标注（删除线+候选字），**已应用的修改不在清除范围内**，原样保留；
  - **回退**：按校正前快照**彻底恢复**当前页文字，**已应用的修改一并撤回**；
- **提示延迟**：「快捷键」设置里可调整悬停提示的响应时间（默认 600ms，0 为立即显示），提示会附带对应快捷键。
- 校正改进（2026-08 更新）
## 七、格式规则（Format Rules）

矫正界面的“格式规则”是一套可配置、按条件自动或手动应用的文本格式化器。规则通过若干条件（单条件或多条件）匹配页面/段落/选区，命中时按照规则定义的格式操作（加粗、斜体、标题、对齐、注释、none 等）依序应用。规则既可通过界面 CRUD 管理，也可通过后端 API /api/format_rules 或直接写入 config.json 的 format_rules 字段（程序在读入时会做兼容迁移）。

字段概述（简洁）:
- id: 规则 id（后端生成/前端编辑器维护）
- name: 规则名称（必填）
- mode: 求值模式，"first"（遇到第一个匹配条件即停）或 "all"（所有匹配条件依序叠加）
- conditions: 条件列表（有序）。每个条件为 {type, pattern, scope, formats}：
  - type: contains | prefix | suffix | regex
  - pattern: 字符串或正则（支持 /pattern/flags 语法）
  - scope: selection | paragraph | page
  - formats: 要应用的格式操作列表（见下）

可用格式操作示例（非穷尽，前端/后端白名单校验）:
- bold / italic
- h1..h6  （将段落转为对应级别标题）
- align_left / align_center / align_right
- note   （注释样式，小字号并加括号）
- remove  （移除某些格式或标签）
- none   （占位：匹配但不做任何操作，常用于复合条件策略）

冲突规则与行为:
- 块级标签互斥（p 与 h1-6）；对齐互斥（left/center/right）。当多个格式冲突时采用 "first-wins"（先应用的格式保留，后续冲突格式跳过并在结果中返回 skipped 信息）。
- 保存/应用时会做合法性校验（空名、非法正则、未授权格式都会导致该条条件或整条规则被丢弃或迁移）。

部署与 API:
- 在 GUI 中：工具栏”规“按钮打开规则管理弹窗，可 CRUD、调整顺序、手动「应用全部规则」。右键菜单也支持对当前页快速应用任意规则。
- 后端 API：GET/POST /api/format_rules（服务端会在写入时调用 _validate_format_rules 做校验并迁移旧格式）；也可通过 Python API 调用 configmanage.set_format_rules(rules_list) 写入 config.json。

使用案例（具体可直接粘入规则编辑器或写进 config.json）:

1) 单条件示例 — 把以“注：”开头的段落标记为注释：

```jsonc
{
  "id": "rule-anno-1",
  "name": "注释段落识别",
  "mode": "first",
  "conditions": [
    {"type":"prefix","pattern":"注：","scope":"paragraph","formats":["note"]}
  ]
}
```

用途：快速把 OCR 输出中常见的行内注释或脚注头标为注释样式，导出时自动缩小字号并加括号。

2) 正则单条件示例 — 把章节标题识别为一级标题（支持标志）:

```jsonc
{
  "id": "rule-chap-head",
  "name": "章节标题识别",
  "mode": "first",
  "conditions": [
    {"type":"regex","pattern":"/^第\\d+章\\b/u","scope":"paragraph","formats":["h1","align_center"]}
  ]
}
```

说明：pattern 可以使用 `/pattern/flags` 语法（例子里用 u 标志以支持 Unicode）。匹配整段即转为 h1 并居中。

3) 多条件示例（mode=all）— 当段落同时包含“目录”关键字且匹配页内特定编号格式时，叠加格式：

```jsonc
{
  "id": "rule-toc-block",
  "name": "目录条目美化",
  "mode": "all",
  "conditions": [
    {"type":"contains","pattern":"目录","scope":"paragraph","formats":["h3"]},
    {"type":"regex","pattern":"/^[0-9]{1,2}\\s+.+/","scope":"paragraph","formats":["align_left"]}
  ]
}
```

用途：先把含“目录”的段落标为小标题，再对符合“数字 + 空格 + 文本”的行左对齐（多个条件按序生效，冲突时跳过被覆盖的 op）。

4) 复杂策略示例（使用 none 占位、page 作用域）— 对当前页整体判断，若页中含“奉旨敕令”关键字则把整页标题化并居中：

```jsonc
{
  "id": "rule-proclaim-page",
  "name": "敕令页样式",
  "mode": "first",
  "conditions": [
    {"type":"contains","pattern":"奉旨敕令","scope":"page","formats":["h2","align_center"]}
  ]
}
```

说明：scope=page 时规则评估以整页文本为单位，可在 GUI 中选择“当前页面”作用域并一键应用。

操作建议与注意事项:
- 先在少量样本页上验证规则，再批量应用（GUI 支持「应用全部规则」并给出冲突/跳过统计）。
- 模式建议：当希望按顺序尝试多种匹配并在第一个匹配处终止时用 mode="first"；希望组合多条件格式叠加时用 mode="all"。
- 正则语法要小心转义（在界面编辑器里直接写 `/pattern/flags`；若通过 JSON 写入需保证字符串转义正确）。
- 规则变更后历史记录不会自动回溯已保存的 correction_history；对历史批量重写请导出后再做批量替换。

示例：通过 Python 写入 config.json（简单演示）

```python
from configmanage import set_format_rules
rules = [
    # 上面列举的规则对象
]
set_format_rules(rules)
```

更多细节与迁移兼容性见项目内文档（若遇旧规则样式被迁移，程序会在读入时自动转换为新结构）。

--

（注：本节为格式规则功能使用说明与示例；若需要我可将若干常用规则直接写入当前项目的 config.json 示例或生成一组可导入的规则模板文件。）



  - 文本级校正现在优先使用词典与保守规则（不默认接入视觉/大模型）以减少误报并提升性能。
  - 优化点：
    - 候选生成引入了按首字索引与小型线程安全缓存（cached_candidates_for_token），避免对大词表重复扫描。
    - 只有在替换后本地窗口的已知词数量增加或候选本身为词表词时，才建议替换（降低单字误替换导致语义不通问题）。
    - 返回项中新增 `candidate_scores`（与 `candidates` 一一对应）；当最高置信分 >= 0.85 时后端会标记 `auto_fixable: true`，前端可据此默认自动应用（或作为提示）
  - 快速运行/验证：
    - 单元测试：python -m unittest test_proofread_text
    - 基准测试（性能）：python scripts/test_proofread_perf.py  # 修改脚本中的 N 值以增加样本量
    - 产物字段：/api/proofread 返回 JSON 中含 `errors` 列表，每项可能含 `candidates`、`candidate_scores`、`auto_fixable`。
  - 调参：阈值（相似度、最小得分、缓存大小）在 dictionarymanage.py 顶部可调整（_SIMILARITY_MIN / _SCORE_MIN / _MAX_CAND_CACHE / _MAX_REPLACEMENT_COMBINATIONS），后续可通过 config.json 外置到运行时配置。

- **滚动稳定**：编辑/插入图片后页面不再跳动到附近页，大幅滚轮也不会被拉回正在处理的页面；滑动翻页（含触屏惯性滑动、拖滚动条、键盘翻页）过程中不会被反复拉回当前页。
- **撤回/前进**：工具栏「操作」组 ↶/↷ 按钮，或快捷键 **Ctrl+Z 撤回**、**Ctrl+Y / Ctrl+Shift+Z 前进**，可对最近 **10 次**以内的操作连续撤回和前进（连续打字合并为一步，格式/标记/对齐/搜索替换/智能清理/繁简转换/插入图片各算一步）；撤回后可再前进，产生新操作后前进记录清空。
- **导出 TXT/DOCX**：工具栏「**导出**」按钮弹出窗口选择格式（导出为 DOCX / 导出为 TXT），随后由**系统保存对话框**选择存放位置和文件名（对话框置顶显示，不会被浏览器窗口遮挡）；导出内容包含当前全部页面（**含尚未保存的修改**）；TXT 为带 BOM 的 UTF-8（Windows 记事本可直接打开），DOCX 中标题（1-6 级）为加粗加大并带大纲级别，正文中的换行保留。

**关闭浏览器也能继续**：界面会每 30 秒向本地服务发一次心跳，关闭标签页/浏览器时发关闭信标；程序检测到浏览器已关闭超过 `--correct-timeout` 秒（默认 10 分钟）后，会自动继续后续流程（保留最后一次保存/暂存/完成的内容）。电脑休眠/恢复不会误判。

中途 Ctrl+C 可放弃矫正、按原识别结果继续。矫正提交的内容只放行白名单标签（段落/标题/粗体/斜体/注释），script、style、属性等一律被清洗。

### 直接启动手动矫正（不跑 OCR）

```powershell
uv run python mian.py correct "E:\MYBooks\books\毛泽东\主席与毛远新同志谈话纪要.pdf" [--title TITLE] [--author AUTHOR] [--correct-timeout 600] [--engine llama|vllm]
uv run python mian.py correct   # 无文件直接启动：空白界面，用于历史记录管理/手动录入
```

直接打开矫正界面（不做 OCR）：页面文本**优先取本地历史缓存最新版本**（同一 PDF 上次矫正/暂存的内容），无历史则为空白页可手动录入；每次点「完成并转换」都会重新生成 EPUB，可反复修改反复转换。**不带 PDF 参数可无文件直接启动**（此时无内容可转换，仅用于历史记录管理）。

### 历史矫正记录

矫正内容按 PDF 自动缓存到 `data/correction_history/`：**每个文件保留多个版本**（暂存/完成时各生成一个新版本，文件名 `<PDF哈希>_<时间戳>_<随机>.json`，每文件最多保留最近 20 个版本；「保存」不新建版本、直接覆盖当前最新版本）。下次对同一 PDF 运行 `--correct` 或 `correct` 命令，自动加载**最新版本**，**支持对已修改过的内容再次手动矫正**。

矫正界面工具栏新增「**历史记录**」按钮：弹窗列出所有历史缓存，**文件名与路径分列显示**（同名不同路径的文件可区分），同一文件的多个版本按时间编号（v1=最新）；支持**勾选单个/多个删除**，也可**全部删除**。

### 历史记录跨设备共享（内嵌预览图）

矫正历史缓存的 JSON 文件中自动内嵌每页的预览图（JPEG base64 编码，110 DPI，quality=50），用于解决**换电脑后 PDF 路径不一致导致原图无法显示**的问题。

**典型场景：** 在电脑 A 上矫正完一本书，把 `data/correction_history/` 下的 JSON 文件拷贝到电脑 B，用 `correct` 命令打开——即使电脑 B 上 PDF 的路径不同（或根本没有 PDF），预览图仍然可以从历史记录中加载，不影响对比矫正。

**工作原理：**
- 每次暂存/保存/完成时，程序自动将当前 PDF 每页渲染为低分辨率 JPEG 并编码为 base64，写入 JSON 的 `images` 字段
- 当 PDF 文件不可用时（路径不存在、未配置等），预览图自动回退到历史缓存中的内嵌图——前端无需任何改动，`/preview/<N>` 和 `/full/<N>` 端点透明地从内嵌数据返回图片
- 内嵌图的分辨率低于原始 PNG（110 DPI vs 220 DPI），足以用于文字对比但不适合精细查看

**注意事项：**
- 内嵌预览图会增加历史 JSON 文件的大小（约 30-60KB/页 × 总页数），100 页的书约 3-6MB
- 旧版本的历史记录（不带 `images` 字段）仍可正常加载，只是 PDF 不可用时无法显示预览图
- 如果历史记录中保存了来自电脑 A 的内嵌图，用电脑 B 打开后 PDF 路径不一致，会自动使用内嵌图作为预览；当 PDF 路径一致且可用时，仍优先使用原始高清图

### EPUB 目录与标题

正文中的标题（一级~六级，含矫正时标记的小标题）会自动加锚点并**一一对应**进 EPUB 目录（EPUB3 `nav.xhtml` 与 EPUB2 `toc.ncx`），支持正常目录跳转；小标题**居中显示**（正文不再重复插入书名的多个一级标题，分卷用「书名（第N部分）」）。

## 七、服务器与并发说明

- **推理引擎**由 config.json 的 `engine` 键选择（`llama` 默认 | `vllm`；CLI `--engine` 可临时覆盖、不写盘）：`llama` 走 llama.cpp（见下条），`vllm` 走 vLLM-Omni——`runserver` 拼 `vllm serve <模型>` 命令，启动参数从 `vllm_server_args` 读取（键自动转 `--kebab-case` 标志，默认监听 `127.0.0.1:8000`）；`vllm_server` 为空时**不自动拉起**，直接连接已运行的 vLLM-Omni 服务。矫正界面的「启动/停止服务」与深度校对提示均按当前引擎显示对应服务名与端口。
- 服务器由程序自动拉起（先探测 `127.0.0.1:8080/health`，已运行则复用），监听 `127.0.0.1:8080`。启动参数从 config.json 的 `llama_server_args` 读取（默认 `--temperature 0 --repeat-penalty 1.1 --parallel 4 --cache-type-k q8_0 --cache-type-v q8_0 --log-verbosity 0`；日志只输出 ERROR 级，`find_slot`/`print_timing` 等刷屏信息已屏蔽，进度由 ptoe 打印）。检测到 GPU 时自动追加 `--n-gpu-layers 999`，配置里显式给出 `n_gpu_layers` 键则覆盖自动检测。
- **多并行调优（2026-08-17）**：llama.cpp 的 KV cache 总量 ≈ ctx × parallel（每槽位独占一份上下文缓存），槽位多于实际并发只会浪费显存、溢出到 CPU 时单张反而变慢——`--parallel` 默认 4，且转换流程运行时按实际并发自动取 min(配置, workers)。CUDA/Vulkan 下自动启用 Flash Attention（长上下文解码显著加速）：老构建附加裸 `--flash-attn`，新构建（`--flash-attn [on|off|auto]` 值形式）利用其 `auto` 默认——CUDA 支持时自动开启、不支持时安全回退，避免裸标志导致 llama-server 启动失败（llama13 实测）；`llama_server_args.flash_attn = "0"` 禁用、`"1"`/`"on"` 强制开启。各模型推荐并发见 `model_choices.<key>.workers`（GUI 设置页「模型管理」可调）。
- **GUI 转换弹窗询问（2026-08-17）**：GUI 转换页的转换子进程无控制台，遇到需要用户决策的提示（如「检测到上次未完成的 OCR 进度」）时不再依赖终端 stdin，而是在浏览器弹出选择窗口（继续识别 / 重新识别全部 / 取消），选择写回子进程后流程继续；终端 CLI 仍是原 stdin 交互。子进程等待弹窗期间不会卡死启动流程，EOF/异常回退默认选择。转换完成判定不依赖 stdout EOF——转换子进程会拉起常驻的 llama-server 并继承其 stdout 管道（永不 EOF），监控线程改为「读线程 + 主进程退出即收尾」，避免转换完成后按钮停在「转换中」、停止按钮失效（2026-08-17 修复）。
- 页面间共享 KV cache：同一批页面重跑时，缓存命中的页几乎瞬时完成。
- **服务端进程独立于 CLI 存活**。不需要时用 `stop` 命令一键关闭——**模型与服务器程序一起停止**，且**上次运行遗留/外部启动的实例也会按端口兜底关闭**（Windows 下自动定位占用端口的进程并终止，无需手动去任务管理器找）：
  ```powershell
  uv run python mian.py stop [--engine llama|vllm]   # 停止推理服务（缺省按 config.json 的 engine；--engine 可指定要停的引擎）
  uv run python -c "from llamamanage import stopserver; stopserver()"   # 等价底层调用
  ```
- **矫正界面的「停止服务」按钮同样适用**：即使正在运行的模型与需要启动的校正模型不一致（状态栏显示「运行中（其他模型，可停止后切换）」），也能一键停止旧服务并释放端口，随后即可重新选择目标模型启动深度校对/重识别（2026-08-13 修复：此前上次运行遗留的 llama-server 无法被关闭，导致端口被旧模型占用、无法切换模型）。

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
uv run python -m unittest test_pdfmanage test_mian test_correctmanage test_llamamanage test_stringmanage test_vllmmanage   # 258 tests pass
uv run python test_image_queue_request.py            # 队列+请求脚本测试，打印 test passed
# test_config_llama.py 是 pytest 风格，需要先 uv add --dev pytest
```

## 十、API 方式调用（跳过 CLI）

```python
from mian import pdf_to_epub

result = pdf_to_epub(
    "E:/MYBooks/books/毛泽东/主席与毛远新同志谈话纪要.pdf",
    dpi=100,
    model_key="HY",
    max_workers=3,
    title="主席与毛远新同志谈话纪要",
)
print(result["epub"])  # EPUB 路径

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
  5) 继续识别上次中断的转换（resume）
  6) 帮助（CLI 用法）
  7) 停止推理服务（llama-server / vLLM）
  0) 退出
  ```
  退出时停在「按回车键继续…」，控制台窗口不会一闪而过。
- **命令行用法与原 CLI 一致**：`ptoe.exe epub <pdf> [--dpi 0] [--model <key>] ...`、`ptoe.exe correct [<pdf>]`、`ptoe.exe config show`、`ptoe.exe stop [--engine llama|vllm]` 等；无参数但 stdin 非交互（管道）时仍打印 `nothing to do`。
- **首次运行**：保证 exe 所在目录（双击时 CWD 即 exe 目录）有 `config.json`，且 `llama_server` / `models_dir` 指向有效路径（见第二节）。
- 依赖说明：脚本用 `uv run --with pyinstaller`，首次构建自动拉取 pyinstaller，**不写入** `pyproject.toml` / `uv.lock`。
