# 分栏导出修复 + 连续分栏合并 + 导出时清理空白行（设计文档）

日期：2026-10-19
状态：已实现

## 意图与原因

用「分栏」排版（无边框表格：左批注 / 右正文）的段落，导出 EPUB 后**会出现表格框线**，
与编辑区所见不符；且**单元格内的多个段落会被并成一段**；一篇文章里**连续多个分栏**
导出后会各自成表（中间多出间距/断线），且无法用段落标记把跨分栏的两段拼回整段。
用户另外要求设置界面新增「导出时清理空白行」开关。

四个子问题的根因（均已实测复现）：

1. **框线**：`apply_markers`（导出链路）重建 `<table>` 开标签时只带 `_block_class_html`
   白名单（`ptoe-note`/对齐/题注…），既不含 `ptoe-noborder` 也不带 `style`，于是导出物里的
   `<table>` 丢掉无边框 class，EPUB 默认 `th,td{border:1px solid}` 生效。
2. **单元格段落合并**：`_Sanitizer` 遇单元格内 `p/div/hN` 时**只留行内文本、丢弃标签**，
   `<td><p>甲</p><p>乙</p></td>` → `<td>甲乙</td>`。
3. **表格不参与标记合并**：`apply_markers` 对表格块执行 `_MARKER_SPAN_RE.sub("", html)`，
   表内所有段落标记被直接丢弃。
4. 无「清理空白行」开关。

## 目标

1. 分栏（无边框表格）导出后**不显示框线**：class / `table-layout:fixed` 一路透传到 EPUB。
2. 单元格内多段**保留段界**，不再被并成一段。
3. 单元格内**段落标记生效**：标记前后两段合并为一段。
4. 一篇文章里**连续的分栏自动合并为一张表**；若两表之间存在段落标记，把标记前后两段
   合并为一段后再放进同一张分栏表。
5. 设置界面新增**「清理空白行」**开关（默认关）：开 = 导出时删纯空白段落、压缩连续空行；
   关 = 原样保留空白换行。

## 非目标

- **不改分栏的排版模型**（已评估并否决另两种方案）：
  - CSS 多列 `column-count`：多数阅读器忽略，且文本自由流动，无法固定「左批注/右正文」配对。
  - 固定版式 EPUB（fixed layout）：体积大、不可重排，超出必要。
  最终采用方案 (a)：**保留无边框表格，修好 class/style 透传**。
- **不改智能清理语义**：`clean_blank_lines` 只作用于**导出**，不改变 `/api/clean`、
  `clean_page_html` 的行为。
- **不改普通表格的导出**：只有带 `ptoe-noborder` 的分栏表参与连续合并。
- 不引入 asyncio / 新第三方依赖。

## 反目标（即使技术可行也算失败）

- 普通（有框线）表格被合并或丢边框。
- 单元格里的段界被再次压平（回归 b1 的旧行为）。
- 导出 HTML 里残留 `data-ptoe-marker` 标记。
- 关闭开关后，导出结果与改动前逐字节一致。
- 导入/存盘链路回归：`ptoe-noborder` 等 class 被白名单丢弃。

## 约束

- Python ≥3.11、Windows、stdlib-only（unittest）；无新依赖。
- 配置写盘走 `configmanage` 的原子写 + `_CFG_LOCK`；`clean_blank_lines` 落入
  `CLEAN_SETTINGS_DEFAULTS` + `DEFAULT_CONFIG["ui_settings"]`，由 `set_ui_settings`
  通用循环持久化，`get_clean_settings` 读取。
- 重依赖保持惰性；`export_clean_blank_lines_enabled()` 内部惰性 import `get_clean_settings`。
- 前端无 JS 单测：以 `node --check ui/app.js`、`python check_js.py`、两份 HTML 的
  id 同步断言（`test_table_ui.py`）为准。

## 已决定的做法

### 1. 表格级属性透传（单一事实来源）

新增 `correctmanage._table_open_attrs(d: dict) -> str` 与 `_table_open_attrs_str(attrs: str)`：
从表格开标签属性里只保留 class 白名单 `TBL_TABLE_CLASSES`（`ptoe-noborder`）
与 `rulemanage.normalize_table_style`（只认 `table-layout:fixed`）。

- `_Sanitizer` 表分支改用它（替换原先的内联实现）；
- `apply_markers` 块解析中 `tag == "table"` 时用它（替换 `_block_class_html`）。

### 2. 单元格段界保留（`_Sanitizer`）

新增 `self._cell_block_depth`。单元格内遇 `p/div/hN` 时不再丢弃，而是**统一降级为 `<p>`**：
`depth == 0` 时输出 `<p>`，`depth += 1`；`</…>` 对称 `depth -= 1`，归零时补 `</p>`；
`_tbl_finish_cell` 兜底补齐未闭合的 `</p>`。→ `<td><p>甲</p><p>乙</p></td>`。

### 3. `apply_markers` 表格感知

解析块流时进入 `<table>` 后置 `tbl_depth`，表内 token 全部进同一缓冲（表内 `<p>` 不再
被当块边界切碎），遇配对的 `</table>` 才产出 `{kind: "table", html: 表内层, attrs}`。
表块的标记改为 `_apply_markers_in_table(b["html"])`——逐单元格施加 join 语义，并剥净残留标记。

### 4. 连续分栏合并

- `_coalesce_column_tables(parsed)`（`apply_markers` 内、渲染之前）：把连续的无边框分栏表
  并为一张；中间若只隔一个「纯段落标记段」则视为**桥接**（`join_cells=True`），
  `_join_last_first_cell` 把前表末格末段与后表首格首段合并为一段，仍置于同一张表下。
- TXT/MD/DOCX 链路的等价实现：`_resolve_noborder_tables(html)`（在 `_html_to_rich_blocks`
  入口调用），先逐表施加表内标记，再合并连续无边框分栏表（`_merge_noborder_tables`）。
- 判定 `_is_column_table_item`：`kind == "table"` 且 `attrs` 含 `ptoe-noborder`。

### 5. 导出时清理空白行（默认关）

设置键 `clean_blank_lines`（`ui_settings`，默认 `False`）。

- `export_clean_blank_lines_enabled()`：读 `get_clean_settings()["clean_blank_lines"]`。
- **EPUB**：`_strip_blank_paragraphs(html)` 删表格外的纯空白 `<p>/<div>`
  （表内保留——分栏/批注里的空行可能是有意留白）。
- **TXT/MD/DOCX**：`_drop_blank_blocks(blocks)` 删纯空白段落块；
  TXT 另用 `_collapse_blank_lines` 把连续空行压成一个、去首尾空行。
- 护栏：`clean_blank_lines` 是导出专用键，`clean_page_html` 无此形参，
  `_clean_pages` 与 `clean_history_pages` 在 `**clean_kw` 解包前将其剔除。

### 6. 设置界面

- `ui/app.js`：`UI_SETTINGS_DEFAULTS.clean_blank_lines = false`；服务端回填
  （`loadUiSettingsFromServer` / 载入编辑器设置）、`saveUIMirror` 写
  `localStorage['ptoe_clean_blank_lines']`、重置默认、`change` 监听器。
  **不**加入 `CLEAN_RULE_CHECKS`（导出专用，`/api/clean` 请求体不含它）。
- `ui/ui_full.html` 与 `correctmanage._UI_HTML` 两份同步新增
  `<input type="checkbox" id="cleanBlankLinesChk">`。
- `/api/ui_settings` GET（默认字典 + merged 字典）与 POST 布尔白名单均加入该键。

## 验收（已实测）

- `test_correctmanage.py::TestColumnsExport`：`apply_markers` 保 class/style、普通表丢
  非白名单 class、单元格段界保留、单元格内 join、连续分栏合并、桥接并表、间隔正文不合并、
  普通表不合并、富文本路径单元格段界。
- `test_correctmanage.py::TestExportCleanBlankLines`：开关读取、表外空段删除、表内空段保留、
  连续空行压缩、空块删除。
- 更新 `test_sanitizer_keeps_cell_paragraphs_as_p`、
  `test_sanitize_table_block_tags_in_cell_kept_as_paragraphs`、
  `test_clean_settings_defaults_and_persist` 与三处 `/api/ui_settings` 期望字典。
- 冒烟：`test_stringmanage.py`、`test_pdfmanage.py`、`test_llamamanage.py`、
  `test_correctmanage.py`、`test_htmlmanage.py`、`test_table_ui.py`；
  `node --check ui/app.js`、`python check_js.py`。
