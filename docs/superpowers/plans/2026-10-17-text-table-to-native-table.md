# 文字表格 → 程序原生表格 转换 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 OCR 识别文本里的文字表格（Markdown 管道表 / ASCII 画框表 / 手打的空格对齐列）转换成程序原生 `<table>`，自动 + 手动双入口，设置可开关（默认开）。

**Architecture:** 新增纯函数模块 `tableconvertmanage.py`，只依赖 `re`/`html`。它被三处调用：`stringmanage.clean_and_structure_text`（默认管线，自动）、`correctmanage.initial_html`（矫正编辑器载入，自动）、`correctmanage` 新增的 `/api/table_convert`（编辑器手动，进一步允许空格对齐）。开关为 `config.json` 顶层 `table_convert`，主设置页（`guimanage.py`）仿 `paddle_merge_lines` 暴露。

**Tech Stack:** Python ≥3.11（stdlib only）、`unittest`；前端原生 JS（`ui/app.js`）+ 内嵌 HTML（`correctmanage._UI_HTML`、`guimanage` 设置页）。

**Spec:** `docs/superpowers/specs/2026-10-17-text-table-to-native-table-design.md`

## Global Constraints

- Python ≥3.11、Windows；stdlib-only，**不新增第三方依赖**；测试用手写 TDD 循环（本计划先写测试再实现）。
- 转换器只用 `re` 与 `html`，**不做动态/字符串导入**（保 PyInstaller 从 `mian.py` 静态分析收录）。
- 配置写盘一律走 `configmanage._CFG_LOCK` + `configmanage._atomic_write_json`。
- 锁序不变：config → session → engine/image；本功能是纯函数，不触碰 session/engine。
- 不引入 asyncio；前端无 JS 单测，以 `node --check ui/app.js` + Python 侧静态断言为准。
- 新模块名固定为 `tableconvertmanage.py`；开关键固定为顶层 `table_convert`（bool，默认 `True`）。
- 转换器是纯函数：**同一进程内不得读写 config/session**；开关解析由调用方完成或经 `convert_tables` 参数传入。
- 幂等：`convert_text_tables(convert_text_tables(x)) == convert_text_tables(x)`。

## Review Focus

规格隐含、但最容易踩到的 5 类输入/情形（各自在所属任务里有对应测试）：

1. **表格在页面最首/最尾**（无前后正文）——不得吞掉首/末行，也不得把相邻正文行并进表格。
2. **表头与分隔行之间夹空行**（OCR 常见）——仍应识别为表格。
3. **单元格含 `<` `>` `&`**——必须 HTML 转义，不得破坏标签结构或被 sanitizer 吃掉。
4. **同页两个相邻表格**（中间仅一行说明文字）——不得被并成一个。
5. **迁移场景**：全角竖线 `｜`/`︱` 与半角混用、`\|` 转义出现在行首/行尾——列切分不得错位。

---

### Task 1: 转换器核心——扫描器 + Markdown 管道表（规格 A、D）

**Files:**
- Create: `tableconvertmanage.py`
- Create: `test_tableconvertmanage.py`

**Interfaces:**
- Consumes: 无（纯 stdlib）
- Produces: `convert_text_tables(text: str, *, allow_space_aligned: bool = False, require_separator: bool = True) -> str`
  - 返回值：把文本里的表格块替换为**单行** `<table><thead><tr><th>…</th></tr></thead><tbody><tr><td>…</td></tr></tbody></table>`；被提取的表题输出为它前面独立的 `<p>…</p>` 行；非表格行**原样保留**。无表格时原样返回。
  - `require_separator=True`（自动档）：管道表**必须有分隔行**才识别（安全锚点）。
  - `require_separator=False`（手动档）：无分隔行也识别，**首行当作表头**，其余为数据行（`W` 取各行最大单元格数）。
  - 单元格文本先做 `\|`→`|`，再 `html.escape(cell, quote=False)`。
  - 对齐类：分隔格 `:---`→`class="ptoe-align-left"`、`---:`→`ptoe-align-right`、`:---:`→`ptoe-align-center`、`---`→无 class；落在对应 `th`/`td` 上。分隔行按 `W` 归一后逐列决定，缺列为无 class。
  - 表格块识别：一段连续的「管道行或空行」，其中**有一行是分隔行**（`require_separator=False` 时改为「首行为管道行」），且分隔行之上最近的管道行是表头；分隔行之下至少 1 个管道行；空行在块内被容忍。

- [ ] **Step 1: 写失败测试**

在 `test_tableconvertmanage.py` 建 `class TestMdPipeTable(unittest.TestCase)`，导入 `from tableconvertmanage import convert_text_tables`。断言如下（`\n` 为真实换行）：

```python
BASIC_IN = "| A | B |\n| --- | --- |\n| C | D |"
BASIC_OUT = ("<table><thead><tr><th>A</th><th>B</th></tr></thead>"
             "<tbody><tr><td>C</td><td>D</td></tr></tbody></table>")

def test_md_basic(self):
    self.assertEqual(convert_text_tables(BASIC_IN), BASIC_OUT)

def test_no_leading_trailing_pipes(self):
    self.assertEqual(convert_text_tables("A | B\n--- | ---\nC | D"), BASIC_OUT)

def test_align_classes(self):
    out = convert_text_tables("| A | B | C |\n| :- | -: | :-: |\n| 1 | 2 | 3 |")
    self.assertIn('<th class="ptoe-align-left">A</th>', out)
    self.assertIn('<th class="ptoe-align-right">B</th>', out)
    self.assertIn('<th class="ptoe-align-center">C</th>', out)
    self.assertIn('<td class="ptoe-align-left">1</td>', out)

def test_escaped_pipe_in_cell(self):
    out = convert_text_tables("| A \\| B | C |\n| --- | --- |\n| x | y |")
    self.assertIn("<th>A | B</th>", out)

def test_fullwidth_pipe(self):
    out = convert_text_tables("｜ A ｜ B ｜\n｜ --- ｜ --- ｜\n｜ C ｜ D ｜")
    self.assertEqual(out, BASIC_OUT)
```

再补这些（每条一两句断言，全部 `assertEqual`/`assertIn`）：

- `test_caption_via_separator_count`：用**用户真实例子**（表题行 8 个单元、分隔行 7 格、数据行 5 格）：
  ```python
  CAP_IN = ("1951年各省区市高级农业生产合作社组织情况 | | 社数 | 户数 | 每社平均户数 | 占总农户的% | 耕地面积（亩） | 占总耕地面积% |\n"
            "|---|---|---|---|---|---|---|\n"
            "| 全国总计〔1〕30 | 30。00 | 0。00003 | 1152 | 0。00007 |")
  ```
  断言：`out.split("\n")[0] == "<p>1951年各省区市高级农业生产合作社组织情况</p>"`；表头首格为空 `<th></th>`（真实空列保留）；数据行不足 7 格补空 → 结尾为 `<td>0。00007</td><td></td><td></td>`。
- `test_no_caption_when_counts_match`：`"社数 | 户数\n--- | ---\n1 | 2"` → 首 `<th>社数</th>`，**不**产生 `<p>`。
- `test_overflow_merged_into_last_cell`：2 列表头、3 格数据行 `| a | b | c |` → `<td>a</td><td>bc</td>`。
- `test_no_separator_unchanged`（Review Focus 自动档安全）：`"A | B\nC | D"` → 原样返回。
- `test_manual_no_separator_first_row_header`：`convert_text_tables("| A | B |\n| C | D |", require_separator=False)` → `<th>A</th><th>B</th>` + `<td>C</td><td>D</td>`。
- `test_prose_with_pipe_unchanged`：`"他说 | 然后走了"` → 原样返回。
- `test_existing_table_untouched`（规格 D）：`"<table><tbody><tr><td>a</td></tr></tbody></table>"` → 原样返回。
- `test_idempotent`：`convert_text_tables(BASIC_OUT) == BASIC_OUT`，且 `convert_text_tables(BASIC_IN)` 再跑一次不变。
- `test_blank_line_between_header_and_separator`（Review Focus 2）：`"| A | B |\n\n| --- | --- |\n| C | D |"` → 等于 `BASIC_OUT`。
- `test_table_at_page_start_and_end`（Review Focus 1）：`BASIC_IN + "\n正文一"` → 表格后跟 `\n正文一`；`"正文一\n" + BASIC_IN` → 前面保留 `正文一`。
- `test_surrounding_prose_not_swallowed`：`"前言\n" + BASIC_IN + "\n后记"` → 首行 `前言`、末行 `后记` 都在。
- `test_two_adjacent_tables_not_merged`（Review Focus 4）：`BASIC_IN + "\n说明\n" + BASIC_IN` → 结果含**两个** `<table`，且中间有 `说明`。
- `test_cell_html_escaped`（Review Focus 3）：`"| a<b>&c | d |\n| --- | --- |\n| 1 | 2 |"` → `<th>a&lt;b&gt;&amp;c</th>`。
- `test_escaped_pipe_at_line_edges`（Review Focus 5）：`"| \\|a | b |\n| --- | --- |\n| 1 | 2 |"` → 首 `<th>|a</th>`。

- [ ] **Step 2: 跑测试确认失败**

Run: `python test_tableconvertmanage.py`
Expected: FAIL（`ModuleNotFoundError: No module named 'tableconvertmanage'` / 函数不存在）

- [ ] **Step 3: 实现 `tableconvertmanage.py`**

```python
def convert_text_tables(text: str, *, allow_space_aligned: bool = False) -> str
```
实现要点（算法，签名与测试未完全确定的部分）：
- `_normalize_pipes(line)`：全角 `｜`/`︱` → `|`。
- `_split_cells(line)`：按未转义 `|` 切分（先按 `\|` 保护），再**仅**去掉行首一个/行尾一个空单元（依据整行是否以 `|` 开头/结尾）。
- `_is_separator(line)`：切分后每格匹配 `^\s*:?[-—－]+:?\s*$` 且 ≥1 格。
- `_is_pipe_row(line)`：含 ≥1 个未转义 `|`。
- 主循环逐行扫描：遇到分隔行，向上回看最近的非空管道行作为表头（允许中间空行），向下收集连续管道行；块边界 = 第一个既非管道行也非空行的行。识别失败则不生成表格、原样保留。
- `_md_block_to_html(header_line, sep_line, body_lines)`：`W = len(_split_cells(sep_line))`；表题按规格（`W+1` 判据）提取；各行按 `W` 归一（补空 / 多并入末格）；输出见 Interfaces。
- 逐行替换，非表格行原样 append；用 `"\n".join(...)` 还原。

- [ ] **Step 4: 跑测试确认通过**

Run: `python test_tableconvertmanage.py`
Expected: OK（全部 `TestMdPipeTable` 用例通过）

- [ ] **Step 5: 提交**

```bash
git add tableconvertmanage.py test_tableconvertmanage.py
git commit -m "feat: text table (markdown pipe) to native <table> converter"
```

---

### Task 2: ASCII 画框表（规格 B）

**Files:**
- Modify: `tableconvertmanage.py`
- Modify: `test_tableconvertmanage.py`

**Interfaces:**
- Consumes: `convert_text_tables`（Task 1）
- Produces: 无新公开签名；`convert_text_tables` 现在也识别画框表，输出同一 `<table>` 结构（首行 = 表头）。

- [ ] **Step 1: 写失败测试**

新增 `class TestBoxTable(unittest.TestCase)`：

```python
def test_unicode_box(self):
    src = "┌───┬───┐\n│ A │ B │\n├───┼───┤\n│ C │ D │\n└───┴───┘"
    out = convert_text_tables(src)
    self.assertIn("<th>A</th><th>B</th>", out)
    self.assertIn("<td>C</td><td>D</td>", out)

def test_ascii_plus(self):
    src = "+---+---+\n| A | B |\n+---+---+\n| C | D |\n+---+---+"
    out = convert_text_tables(src)
    self.assertIn("<th>A</th><th>B</th>", out)

def test_box_not_mistaken_for_prose(self):
    self.assertEqual(convert_text_tables("普通一行──没有竖边框"), "普通一行──没有竖边框")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python test_tableconvertmanage.py`
Expected: FAIL（画框输入未被识别）

- [ ] **Step 3: 实现**

在 `convert_text_tables` 的扫描里增加画框分支：检测到 `^[┌├└┏┠┗+][─━\-+┬┼┴┯┿┷]+[…┐┤┘┓┫┛+]$` 形态的边框行时，把随后到闭合边框之间的 `│`/`|` 行切列输出；首行内容行 = 表头。`allow_space_aligned` 不影响画框表（画框是强信号，自动档也识别）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python test_tableconvertmanage.py`
Expected: OK

- [ ] **Step 5: 提交**

```bash
git add tableconvertmanage.py test_tableconvertmanage.py
git commit -m "feat: recognize ascii/unicode box-drawing text tables"
```

---

### Task 3: 空格/制表符对齐列（规格 C，仅手动）

**Files:**
- Modify: `tableconvertmanage.py`
- Modify: `test_tableconvertmanage.py`

**Interfaces:**
- Consumes: `convert_text_tables`（Task 1）
- Produces: 无新签名；`allow_space_aligned=True` 时额外识别无竖线的空格/`\t` 对齐列（`W = 各行最大单元格数`，首行 = 表头）；`False` 时**永不**触发该分支。

- [ ] **Step 1: 写失败测试**

新增 `class TestSpaceAligned(unittest.TestCase)`：

```python
SRC = "省份    数值    数值\n河北    1       2\n山西    3       4"

def test_off_by_default(self):
    self.assertEqual(convert_text_tables(SRC), SRC)

def test_on_when_enabled(self):
    out = convert_text_tables(SRC, allow_space_aligned=True)
    self.assertIn("<th>省份</th><th>数值</th><th>数值</th>", out)
    self.assertIn("<td>河北</td><td>1</td><td>2</td>", out)

def test_single_space_not_a_table(self):
    src = "一行 只有 单空格"
    self.assertEqual(convert_text_tables(src, allow_space_aligned=True), src)

def test_tab_separated(self):
    out = convert_text_tables("A\tB\n1\t2", allow_space_aligned=True)
    self.assertIn("<th>A</th><th>B</th>", out)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python test_tableconvertmanage.py`
Expected: FAIL

- [ ] **Step 3: 实现**

新增分支（仅在 `allow_space_aligned=True` 时进入）：把连续 ≥2 行的块，按每行「≥2 连续空格或 `\t`」切分；要求**≥2 行**且**列数一致**（或取最大值并允许补空），否则不识别。单空格、单行一律不识别。

- [ ] **Step 4: 跑测试确认通过**

Run: `python test_tableconvertmanage.py`
Expected: OK（Task 1–3 全部通过）

- [ ] **Step 5: 提交**

```bash
git add tableconvertmanage.py test_tableconvertmanage.py
git commit -m "feat: space/tab aligned columns (manual mode only)"
```

---

### Task 4: 接入默认管线（`stringmanage.clean_and_structure_text`）

**Files:**
- Modify: `stringmanage.py`（`clean_and_structure_text`，函数定义在 ~line 204）
- Modify: `test_stringmanage.py`

**Interfaces:**
- Consumes: `tableconvertmanage.convert_text_tables`
- Produces: `clean_and_structure_text(pages, ..., convert_tables: bool | None = None)`。
  - `convert_tables is None` → 懒读 `configmanage.get_config(show_dialogs=False)` 的顶层 `table_convert`（`try/except` 回退 `True`）；`True/False` 直接生效。
  - 开关开启时，在 `t = normalize_brackets(t)` 之后、`t = detect_headings(t)` 之前执行 `t = convert_text_tables(t)`（`allow_space_aligned=False`）。

- [ ] **Step 1: 写失败测试**

在 `test_stringmanage.py` 里测试 `clean_and_structure_text` 的既有测试类中新增：

```python
def test_clean_and_structure_converts_md_table(self):
    pages = [{"page": 1, "text": "| A | B |\n| --- | --- |\n| C | D |"}]
    out = clean_and_structure_text(pages, convert_tables=True)
    self.assertIn("<table", out["pages"][0]["text"])

def test_clean_and_structure_table_convert_disabled(self):
    pages = [{"page": 1, "text": "| A | B |\n| --- | --- |\n| C | D |"}]
    out = clean_and_structure_text(pages, convert_tables=False)
    self.assertNotIn("<table", out["pages"][0]["text"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python test_stringmanage.py`
Expected: FAIL（`TypeError: clean_and_structure_text() got an unexpected keyword argument 'convert_tables'`）

- [ ] **Step 3: 实现**

改签名加 `convert_tables: bool | None = None`；在模块级加私有 helper `_tables_enabled(flag=None)`：`flag` 非 None 直接返回 `bool(flag)`，None 时 `try: from configmanage import get_config; return bool(get_config(show_dialogs=False).get("table_convert", True)) except Exception: return True`。在循环里按上述位置插入 `if _tables_enabled(convert_tables): t = convert_text_tables(t)`。

（`mian.py` 的默认管线调用不传该参数，走 `None → 读 config` 的路径，与同函数上游已读 `ocr_prompt` 的模式一致；测试与需要确定性时显式传参。）

- [ ] **Step 4: 跑测试确认通过**

Run: `python test_stringmanage.py`
Expected: OK（新增 2 条 + 既有全部通过）

- [ ] **Step 5: 提交**

```bash
git add stringmanage.py test_stringmanage.py
git commit -m "feat: auto-convert text tables in clean_and_structure_text"
```

---

### Task 5: 配置键 `table_convert`（默认开）

**Files:**
- Modify: `configmanage.py`（`DEFAULT_CONFIG` 内，`paddle_merge_lines` 附近 ~line 271）
- Modify: `guimanage.py`（设置页 HTML ~line 267；`renderBasic` ~line 582；`collectConfig` ~line 613；`/api/config` POST 校验 ~line 1695）
- Modify: `mian.py`（config set 白名单 ~line 2065、bool 解析 ~line 2105、help ~line 1833、`config show` ~line 2019）
- Test: `test_guimanage.py`（新增 `class TestGuiTableConvert`，仿既有 `TestGuiPaddleMergeLines`）

**Interfaces:**
- Consumes: 无
- Produces: `config.json` 顶层键 `table_convert: bool`（默认 `True`）；`mian.py config set table_convert false` 可写；guimanage 设置页出现「识别表格转换为表格」复选框。

- [ ] **Step 1: 写失败测试**

在 `test_guimanage.py` 加 `class TestGuiTableConvert(GuiServerTestBase)`（照抄 `TestGuiPaddleMergeLines` 的结构：`_get("/api/config")` 取默认、`_post("/api/config", {...})` 写盘、`_on_disk()` 复读）：

```python
def test_default_true(self):
    from configmanage import DEFAULT_CONFIG
    self.assertIs(DEFAULT_CONFIG["table_convert"], True)

def test_get_default_and_post_false(self):
    # GET /api/config → config.table_convert is True
    # POST {"table_convert": False} → 200；磁盘上 False；GET 复读 False
    ...

def test_post_invalid_rejected(self):
    # POST {"table_convert": "maybe"} → 400，error 含 "table_convert"
    ...
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python test_guimanage.py`
Expected: FAIL（`KeyError: 'table_convert'` / POST 未校验）

- [ ] **Step 3: 实现**

- `configmanage.DEFAULT_CONFIG` 加 `"table_convert": True,`（带一行注释说明默认开=自动把识别到的文字表格转成原生表格）。
- `guimanage.py`：在 `cfgPaddleMergeLines` 那个 `form-row` 附近加 `<input type="checkbox" id="cfgTableConvert">`（同结构、同英文 id 命名风格）；`renderBasic()` 里 `document.getElementById("cfgTableConvert").checked = cfg.table_convert !== false;`；`collectConfig()` 里 `cfg.table_convert = !!document.getElementById("cfgTableConvert").checked;`；`/api/config` POST 加一个与 `paddle_merge_lines` 同款的真值归一（`_parse_bool_arg`）校验块。
- `mian.py`：`config set` 白名单加 `table_convert`，加同 `paddle_merge_lines` 的 bool 解析分支与 help 文本；`config show` 打印 `table_convert`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python test_guimanage.py`
Expected: OK

- [ ] **Step 5: 提交**

```bash
git add configmanage.py guimanage.py mian.py test_guimanage.py
git commit -m "feat: table_convert config key + settings checkbox (default on)"
```

---

### Task 6: 矫正编辑器载入接线（`_page_text` / `initial_html`）

**Files:**
- Modify: `correctmanage.py`（`_page_text` 的探测正则 ~line 3574；`initial_html` ~line 3588）
- Modify: `test_correctmanage.py`

**Interfaces:**
- Consumes: `tableconvertmanage.convert_text_tables`、config 的 `table_convert`
- Produces: `initial_html(text)` 现在会（开关开启时）先把文本里的表格转成 `<table>`，并把**整行即为**转换器产出的单行块（`<table>…</table>` 或 `<p>…</p>`）**原样放行**（不转义）；其余行仍 `_html.escape` + 包 `<div>`。`_page_text` 的 HTML 探测正则纳入 table 系标签。

- [ ] **Step 1: 写失败测试**

在 `test_correctmanage.py` 加：

```python
def test_page_text_bare_table_not_escaped(self):
    html = '<table><tbody><tr><td>a</td></tr></tbody></table>'
    from correctmanage import _page_text
    out = _page_text(html)
    self.assertIn("<table", out)
    self.assertNotIn("&lt;table", out)

def test_initial_html_converts_md_table(self):
    from correctmanage import initial_html
    out = initial_html("| A | B |\n| --- | --- |\n| C | D |")
    self.assertIn("<table", out)
    self.assertIn("<th>A</th>", out)

def test_alignment_class_survives_sanitize(self):
    from correctmanage import sanitize_html
    out = sanitize_html('<table><tbody><tr><td class="ptoe-align-center">a</td></tr></tbody></table>')
    self.assertIn("ptoe-align-center", out)
    self.assertIn("<table", out)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python test_correctmanage.py`
Expected: FAIL（`_page_text` 把 `<table>` 当原始文本转义 → 出现 `&lt;table`；`initial_html` 不产出 `<table>`）

- [ ] **Step 3: 实现**

- `_page_text` 探测正则改为 `r"</?(?:p|div|h[1-6]|span|table|thead|tbody|tfoot|tr|th|td)([^>]*)>"`。
- `initial_html`：先 `if _tables_enabled(): text = convert_text_tables(text)`（在 `correctmanage` 内加私有 `_tables_enabled()`，读同一 config 键 `table_convert`、`try/except` 回退 True）；逐行时，若 `line.strip()` 完整匹配 `^<(?:table|p)\b[^>]*>.*</(?:table|p)>$` 则 **原样 append**，否则走原 `_html.escape` + `<div>` 分支。

- [ ] **Step 4: 跑测试确认通过**

Run: `python test_correctmanage.py`
Expected: OK

- [ ] **Step 5: 提交**

```bash
git add correctmanage.py test_correctmanage.py
git commit -m "feat: editor load converts text tables + recognizes bare <table> pages"
```

---

### Task 7: 手动入口后端 `POST /api/table_convert`

**Files:**
- Modify: `correctmanage.py`（HTTP 路由，紧邻 `/api/clean` ~line 9030）
- Modify: `test_correctmanage.py`

**Interfaces:**
- Consumes: `tableconvertmanage.convert_text_tables`
- Produces: `POST /api/table_convert`，请求体 `{"text": str}` → `200 {"ok": true, "html": str}`；`text` 缺失/空 → `400 {"ok": false, "error": "..."}`。内部用 `allow_space_aligned=True, require_separator=False`（手动档：无分隔行也认，首行当表头）。

- [ ] **Step 1: 写失败测试**

参照 `test_correctmanage.py` 现有 handler 测试（如 `test_reocr_*`）的构造方式，加：

```python
def test_table_convert_endpoint(self):
    # POST /api/table_convert {"text": "| A | B |\n| --- | --- |\n| C | D |"}
    # 断言 status==200 且 body["html"] 含 "<table"
    ...
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python test_correctmanage.py`
Expected: FAIL（404 / 无该路由）

- [ ] **Step 3: 实现**

仿 `/api/clean` 的 handler 写法（读 `Content-Length`、`json.loads`、`self._send`）加路由；懒导入 `from tableconvertmanage import convert_text_tables`；调用 `convert_text_tables(text, allow_space_aligned=True, require_separator=False)`；`text` 为空时 400。

- [ ] **Step 4: 跑测试确认通过**

Run: `python test_correctmanage.py`
Expected: OK

- [ ] **Step 5: 提交**

```bash
git add correctmanage.py test_correctmanage.py
git commit -m "feat: /api/table_convert endpoint (manual convert)"
```

---

### Task 8: 前端手动「转成表格」菜单项

**Files:**
- Modify: `configmanage.py`（`POPUP_ELIGIBLE_OPS` ~line 100）
- Modify: `ui/app.js`（`OPS` ~line 11；`OP_ICON` ~line 63；`OP_TIP` ~line 75；`POPUP_ELIGIBLE_OPS` ~line 189；`applyOp` ~line 4500；新增 `convertSelectionToTable`，紧邻 `insertTable` ~line 1698）
- Modify: `test_table_ui.py`（静态断言）

**Interfaces:**
- Consumes: `POST /api/table_convert`（Task 7）
- Produces: 弹出菜单新 op `table`（中文名「转成表格」，字形「表」）。点击时取当前选区文本 → 调 `/api/table_convert` → 用返回 `<table>` 替换选区（走 `histBegin/histEnd` 撤销栈 + `syncContent` + `markDirty`）。无选区 → `showToast('请先选中要转换的文字', 'warn')`。

- [ ] **Step 1: 写失败测试**

在 `test_table_ui.py` 加（复用该文件既有 `read(rel)` helper）：

```python
def test_table_convert_op_present(self):
    app = read("ui/app.js")
    self.assertRegex(app, r"POPUP_ELIGIBLE_OPS\s*=\s*\[[^\]]*'table'")
    self.assertIn("convertSelectionToTable", app)
    cfg = read("configmanage.py")
    self.assertRegex(cfg, r"POPUP_ELIGIBLE_OPS\s*=\s*\[[^\]]*\"table\"")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python test_table_ui.py`
Expected: FAIL

- [ ] **Step 3: 实现**

- `configmanage.POPUP_ELIGIBLE_OPS` 加 `"table"`；`ui/app.js` 的 `POPUP_ELIGIBLE_OPS` 同步加 `'table'`（注释已声明两者必须同步）。
- `ui/app.js`：`OPS` 加 `['table','转成表格']`；`OP_ICON` 加 `table:'表'`；`OP_TIP` 加 `table:'转成表格（把选中文字转换为表格）'`。
- `applyOp(op)` 顶部（`charformat` 分支附近）加 `if (op === 'table') { convertSelectionToTable(ed); return; }`。
- 新增 `convertSelectionToTable(ed)`：取选区纯文本（`window.getSelection().toString()`；空则 toast 返回）；`fetch('/api/table_convert', {method:'POST', ...})`；拿到 `html` 后，仿 `insertTable` 的插入路径（`histBegin` → `withScrollStable` → `execCommand('insertHTML', false, html)`，失败回退）替换选区；`syncContent(ed)` + `markDirty(i)` + `scheduleRemeasure(i)` + `showToast('已转换为表格', 'ok')`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python test_table_ui.py` 且 `node --check ui/app.js`
Expected: OK / 无输出（语法通过）

- [ ] **Step 5: 提交**

```bash
git add configmanage.py ui/app.js test_table_ui.py
git commit -m "feat: popup menu 转成表格 op (manual conversion)"
```

---

### Task 9: 端到端回归与冒烟

**Files:**
- Modify: 仅在有回归时改动既有测试或实现

**Interfaces:**
- Consumes: Task 1–8 的全部产出
- Produces: 无新接口；确认既有行为未回归。

- [ ] **Step 1: 跑项目冒烟测试**

Run（逐条执行，全部须通过）：
`python test_tableconvertmanage.py`、`python test_stringmanage.py`、`python test_pdfmanage.py`、`python test_llamamanage.py`、`python test_correctmanage.py`、`python test_table_ui.py`、`python test_guimanage.py`
Expected: 全 OK

- [ ] **Step 2: 前端 JS 校验**

Run: `node --check ui/app.js`
Expected: 无输出（通过）

- [ ] **Step 3: 手工验证（一次真实闭环）**

启动 `uv run python mian.py correct` 或对含表格的 PDF 跑 `uv run python mian.py <pdf>`：
- 自动：识别文本里的 Markdown 表格在编辑器/导出 EPUB 中呈现为原生表格（表头、空列保留、列数补齐）。
- 手动：在编辑器选中一段空格对齐文字 → 弹出菜单「转成表格」→ 就地变表格。
- 开关：主设置页取消勾选「识别表格转换为表格」→ 重新跑，文字保持原文不转换。

- [ ] **Step 4: 提交（如 Step 1–3 有修正）**

```bash
git add -A
git commit -m "test: regression pass for text-table conversion"
```
