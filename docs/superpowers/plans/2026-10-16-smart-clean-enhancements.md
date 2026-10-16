# 智能清理增强（第二阶段）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为「智能清理」加入预览/一键还原、清理报告、设置内对照表与实时预览、可配置符号映射、更智能段落合并、批量并行清理。

**Architecture:** 统计在服务端计算（`clean_page_html(with_stats=True)`），预览/还原在前端（复用现有 undo 栈）。符号映射存入 `ui_settings.clean_symbol_map`，经既有 `/api/ui_settings` 读写。`/api/clean` 多页时用 `ThreadPoolExecutor` 并行。段落合并在 `_clean_blocks` 增加屏障判断。

**Tech Stack:** Python 3.11（stdlib：unittest、concurrent.futures、re）、原生 JS（`ui/app.js`，无框架）。

**Spec:** `docs/superpowers/specs/2026-10-16-smart-clean-enhancements-design.md`

## Global Constraints

- Python ≥3.11；无新第三方依赖；仅 stdlib unittest。
- `configmanage` 写盘走 `_CFG_LOCK` + `_atomic_write_json`。
- `clean_page_html` 默认**返回 str**（向后兼容）；仅 `with_stats=True` 时返回 `(str, dict)`。
- 保持 `clean(clean(x)) == clean(x)` 幂等。
- 表格仍是原子块：不统计、不合并、不处理。
- 前端无单测：以 `node --check ui/app.js` 通过 + Python 侧对 `_UI_HTML` 的字符串断言为准。
- 测试命令：`uv run python -m unittest test_correctmanage.<Class>`（PowerShell 直接跑 `test_correctmanage.py` 极慢，必须指定子类）。

## Review Focus

1. **空页/纯空 HTML**：`clean_page_html('')` 与 `('   ')` 必须返回稳定结果、统计不报错。
2. **幂等性**：任何开关组合下 `clean(clean(x)) == clean(x)`。
3. **英文/拉丁语境**：`test,OK` / `3.14` / `Wait...` 不被标点/符号规则改写。
4. **并发出错页**：并行清理中某页抛异常时，端点要么整体降级串行，要么返回该页错误的 `ok:false`，绝不静默丢页。
5. **符号表污染**：用户把映射配成危险值（空串、超长、重复源）时，`get_clean_symbol_map` 必须回退默认而非崩溃/死循环。

---

### Task 1: 服务端清理统计（F4 后端）

**Files:**
- Modify: `correctmanage.py`（`_clean_blocks` ~2907，`clean_page_html` ~3082；新增 `_char_delta`、`_count_tags`、常量 `_TAG_COUNT_RE`）
- Test: `test_correctmanage.py`（新增 `TestCleanStats`）

**Interfaces:**
- Produces:
  - `_clean_blocks(html, *, merge_paragraphs, strip_leading_symbols, normalize_punctuation, clean_symbols=False, clean_md_tags=True, symbol_pairs=None, stats=None) -> str`
  - `clean_page_html(raw, *, ..., symbol_map=None, with_stats=False)` → `str`，或 `with_stats=True` 时 `(str, dict)`
  - stats 键（int）：`punct` `symbols` `md` `merged` `tags` `chars`
  - `_char_delta(a: str, b: str) -> int` = 逐位不同计数 + 长度差
  - `_count_tags(s: str) -> int` = `len(_TAG_COUNT_RE.findall(s))`，`_TAG_COUNT_RE = re.compile(r"<[a-zA-Z/!]")`

- [ ] **Step 1: 写失败测试** `TestCleanStats`
```python
def test_stats_counts(self):
    raw = '<p>你好,世界</p><p>结束**重点**</p>'
    html, st = correctmanage.clean_page_html(raw, with_stats=True)
    self.assertEqual(st["punct"], 1)          # 半角逗号 → 全角
    self.assertEqual(st["md"], 1)             # ** 一对
    self.assertGreater(st["chars"], 0)
    self.assertEqual(set(st), {"punct","symbols","md","merged","tags","chars"})

def test_stats_merged_and_tags(self):
    raw = '<div>a</div><p>b</p><p>c</p>'
    html, st = correctmanage.clean_page_html(raw, clean_paragraphs=True, with_stats=True)
    self.assertEqual(st["merged"], 1)         # b 与 c 合并（b 末尾无句末标点）
    self.assertGreaterEqual(st["tags"], 1)    # <div> 被清理

def test_stats_empty(self):
    html, st = correctmanage.clean_page_html("   ", with_stats=True)
    self.assertEqual(st, {"punct":0,"symbols":0,"md":0,"merged":0,"tags":0,"chars":0})

def test_default_returns_str(self):
    self.assertIsInstance(correctmanage.clean_page_html("<p>x</p>"), str)
```

- [ ] **Step 2: 运行确认失败**：`uv run python -m unittest test_correctmanage.TestCleanStats` → FAIL（`with_stats` 不支持）

- [ ] **Step 3: 实现**
  - 加 `_char_delta`、`_count_tags`、`_TAG_COUNT_RE`。
  - `_clean_blocks` 加 `symbol_pairs=None, stats=None`；stats 非 None 时累加：MD 用 `len(_MD_TAG_RE.findall(t2))`；标点用 `_char_delta`；符号归一含 `_BRACKET_DIGIT_RE` 前后 `_char_delta`；合并分支 `stats["merged"] += 1`。
  - `clean_page_html` 加 `symbol_map=None, with_stats=False`；建 `stats`，末尾 `stats["tags"]=max(0,_count_tags(raw)-_count_tags(out))`、`stats["chars"]=_char_delta(str(raw or ""), out)`；`with_stats` 决定返回。

- [ ] **Step 4: 运行确认通过** + 回归 `TestCleanPageHtml TestCleanPageHtmlExtended TestCleanBracketJunkHtml TestCleanBlocksTableBlock`

- [ ] **Step 5: 提交** `feat(clean): 服务端清理统计（punct/symbols/md/merged/tags/chars）`

---

### Task 2: 可配置符号映射（F3 后端）

**Files:**
- Modify: `stringmanage.py`（`_normalize_symbols_core` ~394、`_normalize_symbols_extended` ~416，加 `half_to_full=None`）
- Modify: `configmanage.py`（新增 `CLEAN_SYMBOL_MAP_DEFAULT`；`ui_settings` 默认加 `clean_symbol_map`；`set_ui_settings` 校验；新增 `get_clean_symbol_map()`）
- Modify: `correctmanage.py`（`_clean_blocks` 已含 `symbol_pairs`；`clean_page_html` 已含 `symbol_map`；`/api/clean` 传 `get_clean_symbol_map()`；`/api/ui_settings` GET/POST 加 `clean_symbol_map`）
- Test: `test_stringmanage.py`（`TestNormalizeSymbolsCustomMap`）、`test_correctmanage.py`（`TestCleanSymbolMap`、`TestSetUiSettingsConfig` 扩展）

**Interfaces:**
- Consumes: Task 1 的 `symbol_pairs` / `symbol_map` 形参。
- Produces:
  - `_normalize_symbols_core(text, half_to_full=None) -> str`（None → `_EXT_HALF_TO_FULL`）
  - `_normalize_symbols_extended(text, half_to_full=None) -> str`
  - `configmanage.CLEAN_SYMBOL_MAP_DEFAULT = [["[","【"],["]","】"],["--","——"],["~","～"]]`
  - `configmanage.get_clean_symbol_map() -> list[tuple[str,str]]`（读 `ui_settings.clean_symbol_map`；校验：每项长度为 2 的非空 str、无重复源、≤32 项；非法整体回退默认）
  - `set_ui_settings`：`clean_symbol_map` 存在时校验，非法 → `ValueError`

- [ ] **Step 1: 写失败测试**
```python
# test_stringmanage.py
def test_custom_map_overrides_brackets(self):
    from stringmanage import _normalize_symbols_core
    self.assertEqual(_normalize_symbols_core("看[注]", half_to_full=[["[","《"],["]","》"]]), "看《注》")

def test_default_map_unchanged(self):
    from stringmanage import _normalize_symbols_core
    self.assertEqual(_normalize_symbols_core("看[注]"), "看【注】")
```
```python
# test_correctmanage.py TestCleanSymbolMap
def test_custom_symbol_map_applied(self):
    out = correctmanage.clean_page_html("<p>看[注]</p>", symbol_map=[("x","y")])
    self.assertEqual(out, "<p>看[注]</p>")   # 默认 [ ] 映射被替换 → 不再转换

# configmanage get_clean_symbol_map 回退
def test_get_clean_symbol_map_default_and_fallback(self):
    self.assertEqual(self.cm.get_clean_symbol_map(), [("[","【"),("]","】"),("--","——"),("~","～")])
    self.cm.set_ui_settings({"clean_symbol_map": [["[","《"],["]","》"]]})
    self.assertEqual(self.cm.get_clean_symbol_map(), [("[","《"),("]","》")])
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**
  - `stringmanage`：`half_to_full = _EXT_HALF_TO_FULL if half_to_full is None else tuple(tuple(p) for p in half_to_full)`；反向由 `[(f,h) for h,f in half_to_full]` 派生（替换 `_EXT_FULL_TO_HALF` 的使用）。`_normalize_symbols_extended` 透传参数。
  - `configmanage`：加常量与默认键；`get_clean_symbol_map` 校验回退；`set_ui_settings` 校验（list、每项 2 个非空 str、源去重、≤32）。
  - `correctmanage`：`/api/clean` 的 `clean_kw` 加 `symbol_map=get_clean_symbol_map()`（body 可用 `clean_symbol_map` 覆盖）；`/api/ui_settings` GET 默认 + merged 加 `clean_symbol_map`；POST 加校验（非法 400）。

- [ ] **Step 4: 运行确认通过**（含 `TestSetUiSettingsConfig`、`TestUiSettingsEndpoint` 回归；更新其期望字典）

- [ ] **Step 5: 提交** `feat(clean): 符号映射表可配置（ui_settings.clean_symbol_map）`

---

### Task 3: 更智能段落合并（F6）

**Files:**
- Modify: `correctmanage.py`（`_clean_blocks` 合并分支 ~3019；新增 `_MERGE_BARRIER_RE`、`_MERGE_TAIL_INTRO_RE`）
- Test: `test_correctmanage.py`（`TestSmartMerge`）

**Interfaces:**
- Produces:
  - `_MERGE_BARRIER_RE = re.compile(r"^\s*(?:[•·▪◦*\-–—]\s|\d{1,3}\s*[.、)）]|[（(]\s*\d{1,3}\s*[）)]|　)")`
  - `_MERGE_TAIL_INTRO_RE = re.compile(r"[:：]\s*$")`
  - 合并仅当：双方皆普通 `<p>`、均非空、前块末尾不在 `_SENT_END_PUNCT`、**后块不匹配 `_MERGE_BARRIER_RE`**、**前块不匹配 `_MERGE_TAIL_INTRO_RE`**。

- [ ] **Step 1: 写失败测试** `TestSmartMerge`
```python
def test_no_merge_into_list_marker(self):
    self.assertEqual(
        clean_page_html("<p>步骤如下</p><p>1. 打开</p>"),
        "<p>步骤如下</p>\n<p>1. 打开</p>",
    )  # 前块末尾无句末标点，但后块是数字列表 → 不合并

def test_no_merge_into_bullet(self):
    self.assertEqual(
        clean_page_html("<p>清单如下</p><p>• 甲</p>"),
        "<p>清单如下</p>\n<p>• 甲</p>",
    )

def test_no_merge_after_colon(self):
    self.assertEqual(
        clean_page_html("<p>说明：</p><p>细节</p>"),
        "<p>说明：</p>\n<p>细节</p>",
    )

def test_no_merge_after_fullwidth_indent(self):
    self.assertEqual(
        clean_page_html("<p>你好</p><p>　　新段</p>"),
        "<p>你好</p>\n<p>　　新段</p>",
    )

def test_still_merges_plain_split(self):
    self.assertEqual(clean_page_html("<p>未完</p><p>待续</p>"), "<p>未完待续</p>")
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**：在合并条件中加入两个屏障判断（`not _MERGE_BARRIER_RE.match(next_txt) and not _MERGE_TAIL_INTRO_RE.search(prev_txt)`）。

- [ ] **Step 4: 运行确认通过** + 回归 clean 全套

- [ ] **Step 5: 提交** `feat(clean): 段落合并增加列表/缩进/冒号屏障`

---

### Task 4: 并行清理 + 端点返回统计（F7 + F4 端点）

**Files:**
- Modify: `correctmanage.py`（新增模块级 `_clean_pages(items, clean_kw) -> list[dict]`；`/api/clean` ~8527 改用它）
- Test: `test_correctmanage.py`（`TestCleanPagesParallel`、`TestCleanEndpoint` 扩展校验 stats）

**Interfaces:**
- Consumes: Task 1 `clean_page_html(with_stats=True)`；Task 2 `get_clean_symbol_map()`。
- Produces:
  - `_clean_pages(items: list[dict], clean_kw: dict) -> list[dict]`：每项 `{"page": int, "html": str, "stats": dict}`；顺序与输入一致；单页同步；多页 `ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 4))`。
  - `/api/clean` 响应：`{"ok": True, "pages": [{"page": n, "html": str, "stats": {...}}, ...]}`。

- [ ] **Step 1: 写失败测试**
```python
def test_clean_pages_preserves_order_and_stats(self):
    items = [{"page": i, "html": f"<p>第{i}页,好</p>"} for i in range(8)]
    out = correctmanage._clean_pages(items, dict(clean_punctuation=True, clean_symbols=True,
             clean_md_tags=True, clean_html_tags=True, clean_paragraphs=False, symbol_map=None))
    self.assertEqual([o["page"] for o in out], list(range(8)))
    self.assertTrue(all("stats" in o and o["stats"]["punct"] == 1 for o in out))

def test_clean_pages_parallel_matches_serial(self):
    # 用纯函数验证：并行结果与逐页 clean_page_html 一致
    ...
```
（并给 `TestCleanEndpoint` 增加：响应含 `stats` 且 `punct>=0`。）

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**：`_clean_pages` 内定义 `one(it)`（int(page) 失败 → 跳过该页；`clean_page_html(html, with_stats=True, **clean_kw)`）；`len>1` 用线程池 `list(ex.map(one, valid))`；端点读取 `get_clean_symbol_map()` 放进 `clean_kw`，body 覆盖同类布尔键与 `clean_symbol_map`。

- [ ] **Step 4: 运行确认通过** + `TestCleanEndpoint` 回归

- [ ] **Step 5: 提交** `feat(clean): /api/clean 并行清理并返回每页统计`

---

### Task 5: 前端预览 / 报告 / 一键还原（F1 + F4 前端）

**Files:**
- Modify: `ui/app.js`（`cleanAll` ~3767 重写；`showToast` ~4482 加动作按钮参数）
- Modify: `correctmanage.py`（`_UI_HTML` 加预览模态框 `#cleanPreviewBg`）
- Test: 无 JS 单测；`node --check ui/app.js` + 手工核对（Python 侧断言 `"cleanPreviewBg" in correctmanage._UI_HTML` 可加入 `TestCleanStats`）

**Interfaces:**
- Produces:
  - `showToast(msg, kind, action)`：`action={label, onClick}` 时追加按钮
  - `cleanAll()`：请求 → 若无变化提示「无需清理」；否则弹预览 → 「应用清理」执行并 `histBegin/histEnd`（单条 undo），成功后 toast 带「撤销」按钮调用 `undoHistory()`

- [ ] **Step 1: 手工/脚本地基**：`node --check ui/app.js`（改前通过）
- [ ] **Step 2: 改 `showToast`**：`action` 为真时 `t.appendChild(button)`，按钮点击执行 `action.onClick()` 并移除 toast；`action.timeout` 默认 6000ms。
- [ ] **Step 3: 重写 `cleanAll`**：
  - `const before = histBegin('智能清理', null)` **推迟到「应用」时**（预览阶段只比对，不改内容）。预览需要「当前源」与「清理后结果」，直接发给 `/api/clean` 的 `collect()` 即可。
  - 构建 `changed = cleaned.filter(x => x.html !== 当前源)`；`changed.length===0` → `showToast('内容已是清理后状态','ok')`。
  - 汇总 stats；填充 `#cleanPreviewList`（每页 `第 N 页：标点 a / 符号 b / MD c / 标签 d / 合并 e`）与总计。
  - 「应用」：`histBegin` → 写 map → 重渲染行 → `histEnd` → dirty/editedSet → toast(带撤销)。「取消」：关闭模态框。
- [ ] **Step 4: `_UI_HTML` 加模态框**：`<div id="cleanPreviewBg" class="modal-bg" style="display:none;">` 内含标题、`#cleanPreviewSummary`、`#cleanPreviewList`、`#cleanPreviewApply`、`#cleanPreviewCancel`。复用既有 `.modal-bg`/`.modal` 样式（与 helpModalBg 同构）。
- [ ] **Step 5: 绑定**：`cleanPreviewCancel`/遮罩 → 关闭；`cleanPreviewApply` → 应用逻辑。
- [ ] **Step 6: 验证**：`node --check ui/app.js` 通过；`uv run python -c "import correctmanage; assert 'cleanPreviewBg' in correctmanage._UI_HTML"`。
- [ ] **Step 7: 提交** `feat(clean-ui): 清理预览模态框 + 报告 + 一键撤销`

---

### Task 6: 设置内对照表 + 实时预览 + 符号表编辑（F2 + F3 UI）

**Files:**
- Modify: `correctmanage.py`（`_UI_HTML` 智能清理规则区加示例行、实时预览框、符号映射表 `#cleanSymbolMapEditor`）
- Modify: `ui/app.js`（`UI_SETTINGS_DEFAULTS` 加 `clean_symbol_map`；`saveUIMirror`/`loadUiSettingsFromServer`/`openSettings`/reset/change 处理；实时预览逻辑）
- Test: `node --check ui/app.js` + 手工核对

**Interfaces:**
- Consumes: Task 2 的 `clean_symbol_map`、Task 4 的 `/api/clean`（体 `{html, ...5 bool, clean_symbol_map}`）。
- Produces:
  - `UI_SETTINGS_DEFAULTS.clean_symbol_map = [['[','【'],[']','】'],['--','——'],['~','～']]`
  - `runCleanPreview()`：防抖 250ms；把 `#cleanPreviewInput` 文本转义包 `<p>`，连同当前 5 个 checkbox 状态与编辑器解析出的映射 POST `/api/clean`；输出到 `#cleanPreviewOut`。
  - 符号表编辑：每行 `from = to`；`parseSymbolMap(text)`、`formatSymbolMap(arr)`。

- [ ] **Step 1: 基线** `node --check ui/app.js`
- [ ] **Step 2: `_UI_HTML`**：每个 checkbox 下加灰色小字示例（如 `你好,世界 → 你好，世界`）；随后加：
  - 实时预览：`<textarea id="cleanPreviewInput">` + `<div id="cleanPreviewOut">`；
  - 符号映射表：`<textarea id="cleanSymbolMapEditor" rows="5">` + 说明。
- [ ] **Step 3: `ui/app.js`**
  - 默认值 + 保存/加载/打开/reset 同步 `clean_symbol_map`（localStorage 键 `ptoe_clean_symbol_map`，JSON）。
  - `parseSymbolMap`/`formatSymbolMap`；编辑器 `change` → 更新 `uiSettings.clean_symbol_map` + `saveUiSettings()` + `runCleanPreview()`。
  - 5 个 checkbox 的 `change` 处理追加 `runCleanPreview()`。
  - `runCleanPreview` 防抖 + 请求 + 渲染；空输入清空输出。
- [ ] **Step 4: 验证** `node --check ui/app.js`；`uv run python -c "import correctmanage; assert 'cleanPreviewInput' in correctmanage._UI_HTML and 'cleanSymbolMapEditor' in correctmanage._UI_HTML"`。
- [ ] **Step 5: 提交** `feat(clean-ui): 设置内对照表/实时预览/符号映射表编辑`

---

### Task 7: 收尾

- [ ] **Step 1: 全量验证**：`test_stringmanage`、`test_pdfmanage`、`python -m unittest test_llamamanage`、清理相关子类、`test_epubeditmanage`、`test_htmlmanage`、`test_rulemanage`、`test_ui_popup`、`node --check ui/app.js`、`python check_js.py`。
- [ ] **Step 2: 端到端手工验证**（临时脚本，不在仓库）：`clean_page_html(with_stats=True)`、自定义 `symbol_map`、并行 `_clean_pages` 顺序与一致性、幂等性。
- [ ] **Step 3: 更新 `docs/superpowers/plans/2026-10-16-smart-clean-enhancement.md` 交叉引用或新增变更说明**（如需）。
- [ ] **Step 4: 提交** `feat(clean): 第二阶段增强收尾（测试/文档）`

## Self-Review 记录

- **Spec 覆盖**：F1→T5；F2→T6；F3→T2+T6；F4→T1+T4+T5；F6→T3；F7→T4。全覆盖。
- **类型一致**：`symbol_pairs`（T1 形参）与 `symbol_map`（T2 传入）同名约定：`_clean_blocks` 用 `symbol_pairs`，`clean_page_html` 用 `symbol_map`，端点键 `clean_symbol_map`；T2 明确桥接。
- **Review Focus 测试**：空页→T1 `test_stats_empty`；幂等→T1/T4 回归；拉丁语境→T1/T2 回归；并发异常→T4 `_clean_pages` 跳非法页；符号表污染→T2 `get_clean_symbol_map` 回退测试。
