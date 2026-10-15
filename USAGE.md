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

`ocr_prompt`：OCR 提示词（与 `llamamanage.OCR_PROMPT` 同值，可用下面的 `config set ocr_prompt` 修改）；`llama_server_args`：llama-server 启动参数（host/port/temperature/repeat_penalty/parallel/cache_type_k/cache_type_v/log_verbosity 等，新增支持 `max_tokens`/`ngram_size`/`window_size` 用于输出上限与防重复策略；缺失键跳过、回退 llama-server 内置默认；可选 `n_gpu_layers` 键覆盖 GPU 自动检测；可选 `flash_attn` 键：`"0"`/`false` 禁用、缺省自动、`"1"`/`"on"` 强制开启 GPU 下的 Flash Attention）。**这两个键缺省时程序会在首次运行 `get_config()` 时自动补全并写入默认值**，无需手动添加. `gui_display`：界面加载方式，`pywebview`（默认，内置窗口）或 `browser`（系统浏览器，可用 `browser` 键指定具体浏览器路径）。窗口相关键：`window_maximized`（默认 `true`，pywebview 窗口启动即最大化）与 `tabs_position`（`top`/`bottom`，默认 `top`，多页标签栏的位置；在标签页上切换位置会立即生效并自动保存回配置）。**pywebview 模式下，若「配置中心」与「文字矫正」同时运行，二者会合并为同一个多标签窗口**：先启动者为**主窗口（owner）**，后启动者作为标签页并入；主窗口被关闭/退出后，剩余的应用会自动接管并重建窗口。`browser` 模式不合并，各应用各自在浏览器中打开。

**各模型可配置推荐 OCR 并发**（`model_choices.<key>.workers`，可选）：未显式指定 `--workers` 时按此值运行，缺省 3。依据模型大小/量化选值——大模型（如 HY BF16）显存压力大，并发过高会让多槽位 KV 缓存溢出到 CPU、单张耗时反而大涨，建议 2-3；小模型（如 QWEN.8 0.8B）可 6+。GUI 设置页「模型管理」表格可直接调整（含「并发」列）。

**用命令行查看/修改配置（不用手改 JSON）：**
```powershell
uv run python mian.py config set gui_display browser   # 界面加载方式：pywebview（默认，内置窗口）｜ browser（系统浏览器）
uv run python mian.py config set llama_server <path>   # 修改 llama-server.exe 路径
uv run python mian.py config set models_dir <path>     # 修改模型目录
uv run python mian.py config set selected_model <key>  # 切换默认模型（键必须是 model_choices 里已有的）
uv run python mian.py config set ocr_prompt <text>     # 修改 OCR 提示词
uv run python mian.py config set engine vllm           # 切换推理引擎（llama | vllm）
uv run python mian.py config set vllm_server <path>    # 设置 vLLM-Omni 可执行文件路径
uv run python mian.py config set vllm_server_args.port 8000  # 修改 vLLM-Omni 端口（嵌套参数）
uv run python mian.py config set tabs_position bottom      # 标签栏位置：top（顶栏，默认）｜ bottom（底栏）
uv run python mian.py config set window_maximized true     # pywebview 窗口默认最大化：true（默认）｜ false
```
注：若需临时使用 PaddleOCR，可在命令行加 `--engine paddle`（仅影响 OCR 阶段；config set engine 仍只接受 `llama|vllm`）。
`config set` 接受 `llama_server` / `models_dir` / `selected_model` / `ocr_prompt` / `engine` / `vllm_server` / `gui_display` / `tabs_position` / `window_maximized` 顶层键（`gui_display` 仅接受 `pywebview` / `browser`，控制 gui 与矫正界面的加载方式；`tabs_position` 仅接受 `top` / `bottom`，`window_maximized` 仅接受 `true` / `false`，后两者仅控制 pywebview 窗口行为），以及点分路径 `llama_server_args.<参数>`（如 `config set llama_server_args.parallel 11`，同样可设置 `max_tokens`/`ngram_size`/`window_size` 等）与 `vllm_server_args.<参数>`（如 `config set vllm_server_args.port 8000`）；`selected_model` 会校验必须存在于 `model_choices`，`engine` 仅接受 `llama` / `vllm`。嵌套参数也可用 Python API 修改：
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
| `--correct` | 关 | 开启手动矫正：在矫正界面中逐页对照原图与识别文字（默认 pywebview 内置窗口，gui_display=browser 时用浏览器），可标记粗体/斜体/标题（详见第六节）。默认关闭，不改变既有流程 |
| `--correct-timeout` | 600 | （仅 `--correct` 生效）界面（窗口/浏览器标签页）关闭后自动继续后续流程的等待秒数，默认 600=10 分钟 |
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

OCR 完成后、生成 EPUB 前，可以在矫正界面中逐页对照**原图**与**识别文字**（默认 pywebview 内置窗口，可在配置中改为浏览器），人工修正识别误差，并选中文字标记格式（粗体、斜体、一级/二级/三级标题等）。默认流程**不开启**；只有加 `--correct` 才进入矫正：

```powershell
uv run python mian.py epub "E:\MYBooks\books\毛泽东\主席与毛远新同志谈话纪要.pdf" --correct
```

1. 程序自动启动本地服务（127.0.0.1 随机端口）并打开矫正界面（默认 pywebview 内置窗口「ptoe 文字矫正」；config.json 的 `gui_display=browser` 时改用浏览器打开）；**若「配置中心」也已启动**，两者会合并为同一个多标签窗口（顶/底标签栏切换，见上文配置说明）；
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
8. 点「**完成并转换**」**不会关闭界面**：每次点击都重新转换（弹窗提示**转换完成/未完成**并询问是否关闭当前页面，浏览器禁止脚本自动关闭时请手动关闭标签页（pywebview 窗口模式直接关闭窗口即可）），可留在页面继续修改后**再次点击**。

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

### 编辑器命令行（Ctrl+;，2026-10）

**打开方式**：按 `Ctrl+;`（分号），或点工具栏「命令行」按钮。底部弹出半透明输入条（背景可调透明度、提示居中不贴边），Enter 执行、`↑`/`↓` 在**无候选**时翻阅**历史命令**（有候选时用于高亮候选，`Alt+↑`/`Alt+↓` 始终翻历史）、Tab 补全（**无高亮候选时默认插入第一个推荐命令**，`↑`/`↓` 高亮后则插入高亮项）、**Shift+Tab 在「命令行 ↔ 正文」之间切换焦点**（命令行保持打开，可先在正文里选词/定位再切回输入，插入/标记按正文最新光标定位）、Esc 关闭；输入 `help` 可看全部命令。**提示与补全按当前子命令段生效**：多命令下（`;` / `；` / 换行 / 段首括号分隔）只提示和补全光标所在的这一段，`插入`/`标记`/`包围` 等命令在**参数位**也会给出可选项，方便逐段补全。

**全局命令**：跳转/翻页/搜索等命令**无需先点击正文**（没有焦点页时也能执行），例如 `页 12`、`首页`、`下一页`、`搜索 关键词`；`保存`/`导出`/`撤销`/`重做`/`设置` 等工具操作同样可全局执行。

**基本语法**：`[范围]命令 [参数]`。范围是整个语法的基础（行号 = 当前页从上到下第 N 行，`A,B` 或 `A-B` 表示区间）：

| 命令 | 说明 |
|---|---|
| `7,10合并` | 把第 7~10 行合并成一段（至少两行） |
| `7,10删除` / `7,10清空` / `7替换 新文本` | 删除 / 清空（保留空行）/ 替换行内容 |
| `7,10format 加粗` | 对范围套用格式：加粗/斜体/标题1~6/居中/注释/引用/正文等；也可直接写 `7,10加粗` |
| `页 12` / `行 3` / `选 3 5 10` | 跳原书第 12 页 / 跳本页第 3 行 / 选中第 3 行第 5~10 字 |
| `重识别 12-15` / `校正 全部` / `重识别 +3` | 按原书页号重识别/校正（支持 `N`、`N-M`、`+X`=当前页起 X 页、`全部`） |
| `包围 书名号` / `包围 【】` | 用符号对包住选区/作用行（命名对：方括号/方头括号/六角括号/书名号/尖括号/圆括号/双引号/单引号/直角引号等） |
| `标记 全文` / `标记 段落` | 插入 全文/注释/段落/换页 标记（也可写 `插入 标记 段落`） |
| `复制` / `粘贴` / `复制文本` / `粘贴文本` | 剪贴板复制/粘贴（含格式 或 纯文本） |
| `插图 居中 60%` / `图格式 行内 50%` | 选图插入（布局+宽度一步到位）/ 修改当前选中图的布局与宽度 |
| `规则 1` / `规则 列表` | 按序号/名称套用已存格式规则；`规则 列表` 打开管理窗 |
| `首页` / `末页` / `上一页` / `下一页` / `页首` / `页尾` / `行首 3` / `行尾 3` | 各种跳转定位 |
| `清除格式` / `格式化` / `清空页` | 去格式 / 把块统一为正文 / 清空整页 |
| `采纳` / `忽略` / `应用校正` / `清除标注` / `回退` / `完成` / `题注` | 纠错与收尾的快捷别名 |

**字母简写（2026-10）**：每条命令都绑定了**唯一的字母简写**，简写之间两两不重复、也不与任何既有别名冲突（由 `ptoe_cmdshort_test.cjs` 校验）。同一个命令可以用「简写 / 英文名 / 中文名」任意一种写法执行，例如 `pg`、`page`、`页` 都是跳转原书页号。候选列表里输入字母即可看到带**（简写）**标记的候选，Tab 补全同样可用。常用简写速查：

| 分类 | 简写 → 命令 |
|---|---|
| 行编辑 | `mg`=合并、`del`=删除、`clr`=清空、`rp`=替换、`fmt`=套用格式 |
| 定位 | `pg`=页、`ln`=行、`se`=选中、`gt`=光标、`fd`=搜索、`h`=帮助 |
| 页面跳转 | `fp`=首页、`lp`=末页、`pp`=上一页、`np`=下一页、`pgs`/`pge`=页首/页尾、`lns`/`lne`=行首/行尾 |
| OCR | `ocr`=重识别、`pf`=校正 |
| 排版插入 | `wr`=包围、`mk`=标记、`ins`=插入、`nt`=注释、`qt`=引用、`tb`=表格、`im`=插图、`imf`=图格式、`cp`=清空页、`ru`=规则 |
| 剪贴板 | `cy`=复制、`ct`=复制文本、`pt`=粘贴、`px`=粘贴文本 |
| 格式（配合范围） | `b`=粗体、`i`=斜体、`hdr`=标题、`rm`=清除格式、`al`/`ac`/`ar`=居左/中/右、`cb`=居中加粗、`fl`/`id`=顶格/缩进、`ul`=下划线、`ud`=下加点、`sk`=删除线、`cx`=字框、`sd`=底纹、`hl`=突显、`sp`/`sb`=上/下标、`sw`=去空、`no`/`ci`=注释/引用格式 |
| 工具 | `sv`=保存、`ex`=导出、`st`=暂存、`u`=撤销、`rd`=重做、`hs`=历史、`cl`=智能清理、`t2`/`s2`=繁转简/简转繁、`set`=设置、`ok`=采纳、`ig`=忽略、`cm`=清除标注、`ap`=应用校正、`rv`=回退、`fn`=完成、`cap`=题注 |

**多命令脚本**：`;` 或 `；`（全角）或换行分隔多条命令**依次执行**；`（…）` 把一组命令合成一个作用单元（可嵌套），单元执行完**最终的选区/光标**会作为后续命令的默认范围：

```
（7,10合并）加粗        → 先合并 7-10 行，再把合并结果整块加粗
选 3 5; 包围 书名号      → 先选中第 3 行第 5 个字，再把它包上《》
（选 3 5 10）插入 注释    → 选中后直接插入剪贴板文字为注释（见下）
页尾; 插入 标记 段落     → 光标跳到当前页末尾，再插入段落标记
```

> 注意：只有命令**开头**的 `（`/`(` 才被当作分组符；`包围 ()` 里的括号是字面参数，请放心使用。

**插入命令与剪贴板（亮点）**：

`插入` 在光标处（或替换选中的文字）写入排版好的内容，支持 **注释 / 引用 / 正文 / 标题** 四种格式；注释与引用**默认用 `[]` 包裹**（如 `[本书为内部发行]`），可用 `包围 符号` 指定其它括号，或用 `包围 无` 取消包裹：

```
插入 注释 本书为内部发行          → 插入注释段「[本书为内部发行]」
插入 引用 一句名言                → 插入引用段「[一句名言]」
插入 注释 包围 【】 本书为内部发行 → 用【】包裹 →「【本书为内部发行】」
插入 注释 包围 无 订正说明         → 不加包裹 →「订正说明」
插入 正文 普通段落文字             → 插入普通段落（不包裹）
插入 标题 第一章                  → 插入一级标题段落
插入 标记 段落                    → 等价于「标记 段落」，插入段落标记
```

**文本缺省时自动读取系统剪贴板**——先复制、后插入的典型工作流：

| 命令 | 效果 |
|---|---|
| `插入 注释` | 把**剪贴板文字**插成注释段并用 `[]` 包裹 |
| `插入 引用` | 把剪贴板文字插成引用段（`[]` 包裹） |
| `插入 注释 包围 【】` | 剪贴板文字插成注释段并用 `【】` 包裹 |
| `插入 正文`（或直接 `插入`） | 把剪贴板文字插成普通段落 |
| `插入 标记 段落` | 插入段落标记（内容非剪贴板） |
| `注释 剪贴板` / `引用 剪贴板` | 注释/引用命令同样支持剪贴板来源 |

细节说明：
- 取的是剪贴板**纯文本**（text/plain）；剪贴板为空会提示「剪贴板为空，请先复制」；
- 关键字：`剪贴板` / `粘贴板` / `系统剪贴板` / `clipboard` / `自动`（大小写均可）；
- 包裹：注释/引用默认 `[]`；`包围 符号`（如 `包围 【】`、`包围 方括号`）可换符号，`包围 无`（或 `包裹 无` / `wrap none`）取消包裹；正文/标题默认不包裹；
- **插入位置 = 焦点位置**。要插入到指定位置：先用鼠标点该行，或用 `行首 N` / `行尾 N` / `选 N A B` / `页首` / `页尾` 把光标定位好，再执行 `插入`；也可在命令行打开时按 **Shift+Tab** 切到正文点好位置、按 **Shift+Tab** 切回再执行；
- 有选区时**替换整个选区**；无选区时在光标所在行后**新建一段**（注释/引用/正文为 `<p>`，标题为 `<h1>`）；
- 配合 `;`/`；` 与 `（…）` 可把「定位 + 插入」合成一条命令：如 `（行首 5）插入 引用`、`页尾; 插入 标记 段落`。

**典型使用案例**：
```
① 在浏览器复制一段引文 → 回矫正界面按 Ctrl+; 输入「插入 引用」回车
   → 引文自动成为引用段（[] 包裹），光标后紧跟正文可继续输入。
② 把某段要标注的文字先 选 3 8 20，再输入 「插入 注释」
   → 选中文字被替换为注释段，无需手动打字。
③ 整段搬移：源位置 复制文本 → 目标位置 行尾 3; 插入 正文
   → 剪贴板文字以其原顺序插入目标行之后。
④ 页尾插段落标记：输入「页尾; 插入 标记 段落」
   → 光标先跳到当前页末尾，再插入段落标记。
```

### 界面新增项（近期）

- **编辑器命令行（Ctrl+;）**：底部命令输入条，支持行范围操作、`;`/`；` 多命令脚本与 `（…）` 作用域分组；**每条命令都有唯一的字母简写**（如 `pg`=`页`、`mg`=合并，见上方「字母简写」速查表）；Tab 无高亮时**默认插入第一个推荐命令**；`Shift+Tab` 可在**命令行 ↔ 正文**之间切换焦点；`插入` 命令文本缺省时**自动读取剪贴板**，注释/引用**默认用 `[]` 包裹**（可用 `包围 符号` 调整、`包围 无` 取消），并支持 `插入 标记 段落`（见上方「编辑器命令行」章节）。
- **操作动画（2026-10，纯视觉）**：段落合并后结果段播放一次高亮脉冲，拖放文字/图片的落点轻微闪烁提示新位置；为 CSS 动画，不写入内容、不进历史，系统开启「减少动态效果」时自动禁用。
- **行号与正文对齐（设置）**：设置面板可开关**行号栏**并选择其**尺寸（小/中/大）**，行号栏与正文之间留白加宽、更易读；**正文两端对齐**默认开启，可在设置中关闭。均为纯视图层，不影响保存与导出。
- **替换规则（Replace Rules）**：可预设常用文本替换，批量应用到当前编辑内容。
- **自动保存（Auto Save）**：在校正过程中自动保存临时状态，减少意外中断丢失。
- **分隔线与表格**：工具栏新增插入 `<hr>`、表格，支持直接在编辑器中排版表格。
- **图片宽度（%）**：图片属性支持自定义百分比宽度，配合版式更灵活。
- **历史步数**：历史最大步数由配置项 `history_max_steps` 控制，可按需调大/调小。
- **导出增强**：EPUB 支持图文混合**浮动绕排**与**题注**，表格导出结构更完整。
- **批处理优化**：可限制**批处理页数**并在批处理后自动内存回收，适合长文处理。
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

**关闭界面也能继续**：pywebview 窗口模式下关闭窗口即视为结束；浏览器模式下界面每 30 秒向本地服务发一次心跳，关闭标签页/浏览器时发关闭信标。程序检测到界面已关闭超过 `--correct-timeout` 秒（默认 10 分钟）后，会自动继续后续流程（保留最后一次保存/暂存/完成的内容）。电脑休眠/恢复不会误判。

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

**`0pack.ps1`**（项目根目录）用 PyInstaller 把 `mian.py` 打包成自包含目录（`dist\ptoe\`，onedir + console，无需 Python 环境）：

```powershell
powershell -ExecutionPolicy Bypass -File .\0pack.ps1                  # 默认 onedir，约 2.5 GB（含 GPU PaddleOCR）
powershell -ExecutionPolicy Bypass -File .\0pack.ps1 -NoPaddle        # 不打包 PaddleOCR 引擎，约 250 MB
powershell -ExecutionPolicy Bypass -File .\0pack.ps1 -OneFile         # 单文件 exe（体积小、首启慢，默认不用）
powershell -ExecutionPolicy Bypass -File .\0pack.ps1 -KeepCudnnAdv    # 保留 cudnn_adv64_9.dll（+233 MB）
```

打包要点：
- **onedir + console**：目录式分发（`dist\ptoe\ptoe.exe` + `_internal\` 依赖）；`pyproject.toml` 一并打入，`--version` 显示正确版本号；`pymupdf`/`requests`/`zhconv`/`tkinter` 全部内置；`--noupx` 避免杀软误报。**因采用 onedir，启动时不需解包到临时目录，首次启动无 onefile 式卡顿**。
- **体积（2026-09 优化）**：`ptoe.spec` 现在是唯一的打包定义（脚本只负责设开关 + 调它），裁剪在 **Analysis 之后、复制之前** 完成，因此不再"先复制上千 MB 再删"。裸 collect-all 5.56 GB → 优化后约 2.52 GB（-55%）；相对旧脚本产物（约 4.6 GB，根目录残留孤儿 CUDA DLL）为 -45%。每一项裁剪/保留都有实测依据，见 `ptoe.spec` 头部注释（含"移除即失败"的必需清单：`cublasLt64_12.dll`、`cudnn_*_engines*`、`cudnn_ops/cnn/graph`、`mklml.dll`、`mkldnn.dll`、`phi.dll`）。
- **-NoPaddle**：PaddleOCR 只是可选引擎（见 `pyproject.toml` 的 optional-dependencies），不打包它时 `--engine paddle` 不可用，其余功能（llama/vLLM OCR、手动矫正、配置界面、EPUB 打包）完全不受影响。
- **双击 `dist\ptoe\ptoe.exe` 启动终端菜单**（无参数 + 交互终端即进入）：
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
- **命令行用法与原 CLI 一致**：`ptoe.exe epub <pdf> [--dpi 0] [--model <key>] ...`、`ptoe.exe correct [<pdf>]`、`ptoe.exe config set engine llama`、`ptoe.exe stop [--engine llama|vllm]` 等；无参数但 stdin 非交互（管道）时仍打印 `nothing to do`。
- **首次运行**：保证 exe 所在目录（双击时 CWD 即 exe 目录）有 `config.json`，且 `llama_server` / `models_dir` 指向有效路径（见第二节）。
- 依赖说明：脚本用 `uv run --with pyinstaller`，首次构建自动拉取 pyinstaller，**不写入** `pyproject.toml` / `uv.lock`。


- 硬件：RTX 3070 Laptop 8GB + i7-11800H（8C/16T）+ 32GB RAM
- .venv 里 paddleocr 已装且 detect_gpu()=True（我先前用系统 Python 3.13 检查得出"未安装"是误报，请忽略）
- 当前配置：engine=paddle、selected_model=DOTS8、dpi=100、parallel=2、ctx_size=16384、max_tokens=8192、cache_type_k/v=q8_0、flash_attn=0、image_preprocess.workers=8、paddle_merge_lines=true
- 模型实际存在：E:/model/dots/dots.ocr.Q8_0.gguf(1.76GB) + dots.ocr.mmproj-Q8_0.gguf(1.25GB)
两个必须先懂的机制（决定参数怎么配）
1. --workers 对 PaddleOCR 无效。 paddleocrmanage.batch_infer 注释明确写着"顺序逐页"，max_workers 被忽略。Paddle 路径下真正影响速度的只有 dpi（分辨率）和 image_preprocess.workers（CPU 预处理并行）。
2. llama 路径下 parallel 和 ctx_size 是绑定的一对。 每槽上下文 = ctx_size // parallel；视觉页约占 3100 token，代码要求每槽 ≥ 8192 才不截断。所以 parallel=2 + ctx=16384（8192/槽）✅，parallel=4 + ctx=16384（4096/槽）❌ 会截断。且 runserver 取 min(config的parallel, workers)，workers 应 = parallel，多出来的客户端只会排队。
推荐参数（三种取向）
参数	速度优先	均衡（推荐）	质量优先
engine	paddle	paddle 或 llama	llama
dpi	100	150	200
workers	—（paddle 无效）	2（llama）/ —（paddle）	1
parallel	2	2	1
ctx_size	16384	16384	16384（或 24576）
batch_size	4096	4096	4096
ubatch_size	1024	512	1024
flash_attn	1	1	1
cache_type_k/v	q8_0	q8_0	q8_0
max_tokens	3072	4096	8192
ocr_batch_size	2	2	1
ocr_group_by_size	true	true	true
temperature	0	0	0
cache_reuse	0	0	0
image_preprocess	gray+sharpen, workers 8	同左	+denoise=true
关键理由：
- dpi 是最大杠杆（图像 token 随面积增长）：150 是质量/速度拐点；paddle 的检测器会把长边缩放到 limit_side_len（默认约 960–1280px），A4 上 150dpi≈1240px 已基本够，再往上多半被缩小、白费时间；只有小字密集页才值得 200。
- flash_attn=1：RTX 3070 是 Ampere（算力 8.6），支持 FA2，长上下文解码明显提速。你当前是 0（关）。若开启后 llama-server 启动失败就改回 0。
- batch 4096 / ubatch 512：-b 4096 让一页约 3100 图像 token 一次 llama_decode 算完（默认 2048 要拆两次）；-ub 512 压低显存峰值，8GB 稳。显存宽裕可提到 1024。
- cache_reuse=0：每页图片都不同，公共前缀只有约 30 token 系统提示，命中收益 <1%，开了反而略增开销。
- ocr_batch_size = workers：显式分波、避免框架自动合并不同尺寸页；ocr_group_by_size=true 让同尺寸页同波提交，减少服务端 padding。
应用命令（均衡方案）
若继续用 PaddleOCR（当前引擎，无需起服务）：
uv run python mian.py config set dpi 150
uv run python mian.py config set ocr_batch_size 2
uv run python mian.py config set ocr_group_by_size true
# image_preprocess.workers 已是 8，可不动
uv run python mian.py epub "你的书.pdf" --resume
（paddle 路径想"最快"就把 dpi 降到 100；想"质量"就 200 + image_preprocess.denoise=true。）
若切到 llama + dots.ocr（能真正并行，通常更快）：
uv run python mian.py config set engine llama
uv run python mian.py config set selected_model DOTS8
uv run python mian.py config set dpi 150
uv run python mian.py config set ocr_batch_size 2
uv run python mian.py config set ocr_group_by_size true
uv run python mian.py config set llama_server_args.parallel 2
uv run python mian.py config set llama_server_args.ctx_size 16384
uv run python mian.py config set llama_server_args.batch_size 4096
uv run python mian.py config set llama_server_args.ubatch_size 512
uv run python mian.py config set llama_server_args.flash_attn 1
uv run python mian.py config set llama_server_args.max_tokens 4096
uv run python mian.py stop          # 改启动参数必须重启服务才生效
uv run python mian.py epub "你的书.pdf" --model DOTS8 --workers 2 --resume
调优时盯显存：
nvidia-smi --query-gpu=memory.used,memory.total --format=csv -l 2
若显存接近打满（KV 溢出到 CPU 会反而变慢），就把 parallel 降到 1、ubatch_size 降到 256。
说明：config set engine 只接受 llama/vllm；你 config.json 里的 engine:"paddle" 是别处写入的，切回 llama 用上面的命令即可，切回 paddle 需在 GUI 转换页选引擎。
要我直接把这些参数写进 config.json（均衡方案）吗？告诉我用 paddle 还是 llama 就行。
