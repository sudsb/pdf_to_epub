# 表格单元格段落格式丢失修复（设计文档）

日期：2026-10-19
状态：已实现

## 意图与原因

用户反馈（两处症状，格式为「引用」）：

1. 表格单元格里设置并显示的段落格式，导出到 EPUB 后**个别段落**不一致
   （界面显示引用、导出变正文）。
2. 把整段**拖进表格单元格**后，段落原本设的格式**丢失显示状态、显示成正文**。

四处已实测复现的根因：

1. **清洗链路丢单元格段落属性**：`_Sanitizer` 单元格内遇 `p/div/hN` 统一降级为
   `<p>`，但只输出裸 `<p>`——段落级 class（`ptoe-citation`/`ptoe-note`/对齐…）
   与「段落设置」`data-*` 全部被剥。保存/`智能清理`/历史载入后 DOM 被清洗结果
   覆盖 → 界面回落成正文。
2. **导出并段链路丢单元格段落属性**：`apply_markers` 的 `_apply_markers_in_cell`
   与跨表桥接 `_join_last_first_cell` 重建 `<p>` 时同样不带属性。
3. **EPUB 渲染不对单元格内 `<p>` 做表外等价处理**：`htmlmanage._render_fragment`
   把 `<table>` 整块原子透传，单元格内 `<p>` 的 `data-*`（缩进/行距）不转内联
   style、class 不走白名单 → 与界面显示不一致。
4. **原生拖放清属性**：整段拖进单元格时浏览器原生拖放会清掉块的 `class`/`data-*`。

## 目标

1. 单元格内段落的 class/`data-*` 在**保存、`智能清理`、历史载入、导出**全链路保留。
2. 单元格内段落（含 join 合并、跨表桥接合并的结果段）保留**首段**格式
   （与表外 `_merge_paragraph` 的「合并结果保留前一段对齐等属性」同口径）。
3. EPUB 导出时单元格内 `<p>` 与表外同口径重建开标签（class 白名单 +
   `data-*` → 内联 style）。
4. 整段拖进表格单元格时按原样插入（保留 class/`data-*`）；其余落点仍走原生拖放。

## 非目标

- 不改「单元格内块级标签降级为 `<p>`」的既有契约（标题级语义不保留）。
- 不放宽 class 白名单：单元格段落仍只保留 `ptoe-note`/`ptoe-citation`/对齐/
  换页/图片/题注/分隔线/flush/indent。
- 不改分栏（无边框表）合并语义与空白行清理开关（见既有设计文档）。
- 不引入 asyncio / 新依赖。

## 反目标

- 单元格里的非白名单 class 被放行。
- 表外段落渲染行为改变（回归）。
- 表格结构/单元格属性/单元格文本空白被改动。
- 单元格内多段被再次拍平成一段。

## 已决定的做法

### 1. `_Sanitizer` 单元格段落保留属性（correctmanage.py）

单元格内 `p/div/hN` 开标签（`_cell_block_depth == 0`）输出
`<p{_block_classes(attrs)}{_block_data_attrs(attrs)}>`；嵌套块只计深度不重复输出。

### 2. 单元格段落 token 携带属性

`_cell_para_tokens` 段首追加 `('attrs', 属性串)`（经 `_block_class_html` +
`_block_indent_attr_html` 过滤，与表外同一套 helper）。新增
`_cell_paragraph_items` 返回 `(attrs, html)`；`_cell_paragraph_texts` 委托它。

### 3. 并段保留首段格式

`_apply_markers_in_cell` / `_join_last_first_cell` 重建 `<p>` 时回填属性，合并时
取前一段属性。

### 4. EPUB 渲染单元格段落等价处理（htmlmanage.py）

`_render_fragment` 的表内分支跟踪 `in_cell`（`<td>/<th>` 开合），单元格内 `<p>`
开标签按 `_block_class_html` + `_indent_style_attrs` 重建；其余表内 token 仍原样
透传（空白/结构/属性不动）。

### 5. 整段拖进单元格前端接管（ui/app.js）

`dragstart`：非图片且选区整块覆盖 `p/div/h1-6` 时记录整块 `outerHTML`
（`_dragBlkHtml`）并写入 `text/html`。文档级 capture `drop`：落点在单元格内时
`preventDefault` 并按 `_dragBlkHtml` 在落点后插入、删除原块（`histRun('拖入表格')`）；
落点在编辑区外/非单元格 → 交回原生拖放。`dragstart`/`dragend`/drop 收尾均清状态。

## 验收（已实测）

- `test_correctmanage.py::TestColumnsExport`：清洗保留单元格 class/`data-*`、
  非白名单仍丢弃、单元格标题降级带类、apply_markers 保留、join 保留首段格式、
  跨表桥接保留格式。
- `test_htmlmanage.py::TestTableCellParagraphFormat`：单元格引用类保留、
  `data-*` → 内联 style、非白名单类丢弃、表结构/空白/单元格属性不变、表外段落不变。
- `test_table_ui.py::TestTableRefactor::test_drag_paragraph_into_cell_preserves_format`：
  前端接管逻辑静态不变量。
- 冒烟：`test_correctmanage.py`(930)、`test_htmlmanage.py`(56)、
  `test_stringmanage.py`(73)、`test_pdfmanage.py`(24)、`test_llamamanage.py`、
  `test_table_ui.py`(41)；`node --check ui/app.js`、`node ptoe_table_test.cjs`(177)。
