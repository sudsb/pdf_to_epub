# Smart Cleaning Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enhance the "智能清理" (smart cleaning) functionality to support: (1) global English punctuation/symbol to Chinese conversion, (2) configurable cleaning rules in settings, (3) removal of raw MD/HTML tags while preserving user-set rich text formatting.

**Architecture:** Extend the existing `clean_page_html` function in `correctmanage.py` with new cleaning options. Add configuration options to `ui_settings` in `configmanage.py`. Update the `/api/clean` endpoint in `epubeditmanage.py` to accept and pass the new options. The front-end (`ui/app.js`) will get new settings UI to control these options.

**Tech Stack:** Python 3.11+, stdlib only (re, html.parser), existing codebase patterns (atomic config writes, lazy imports, lock ordering).

**Spec:** This plan implements the user's requirements as described in the conversation.

## Global Constraints

- Python ≥3.11, Windows required (tested only on Win10/11)
- Dep manager: uv (`uv sync`, not pip)
- Test: stdlib unittest only; no pytest/coverage/lint by default
- Code style: PEP8, single-module patterns, avoid sprawling helpers
- Error Handling: Prefer explicit try/except with concise fallback. Never silence.
- Globals/Locks: Only `_CFG_LOCK`, `_SESSION`, and `_server_process` global. Lock ordering must be preserved (config → session → engine/image)
- Async/Concurrency: Multi-thread only in batch image/OCR/engine. No asyncio/event-loop—thread or process only.
- Lazy/Optional Deps: All heavy deps are lazy-imported (try/except fallback, Dummy for missing)
- Atomicity & Safety: Config and output writes use in-project atomic helpers (`configmanage._atomic_write_json`)

## Review Focus

1. **CJK-adjacent punctuation conversion**: Ensure half-width punctuation converts to full-width only when adjacent to Chinese characters, not when adjacent to Latin/digits (e.g., "test,OK" stays "test,OK" but "你好,世界" becomes "你好，世界")
2. **User rich text preservation**: Verify that `<strong>`, `<em>`, `<span class="ptoe-*">`, `<br/>`, `<img>` with allowed classes are preserved while raw/unexpected tags like `<div>`, `<font>`, `<a>`, markdown syntax are stripped
3. **Idempotency**: Running clean twice on already-cleaned content must produce identical output
4. **Configuration persistence**: New settings must persist across app restarts via config.json
5. **Backward compatibility**: Existing clean calls without new parameters must work identically

---

### Task 1: Add punctuation/symbol conversion maps and function in `stringmanage.py`

**Files:**
- Modify: `D:\code-project\python\PToEA\stringmanage.py` (add new conversion maps and function)
- Test: `D:\code-project\python\PToEA\test_stringmanage.py` (add tests)

**Interfaces:**
- Produces: `_normalize_punctuation_extended(text: str) -> str` function that converts English punctuation/symbols to Chinese equivalents globally

- [ ] **Step 1: Write failing tests for new punctuation conversion**

```python
# In test_stringmanage.py
class TestNormalizePunctuationExtended(unittest.TestCase):
    def test_quotes_conversion(self):
        # English double quotes to Chinese double quotes
        self.assertEqual(_normalize_punctuation_extended('他说 "hello"'), '他说 "hello"')
        # But preserve in English context
        self.assertEqual(_normalize_punctuation_extended('He said "hello"'), 'He said "hello"')
    
    def test_single_quotes_conversion(self):
        self.assertEqual(_normalize_punctuation_extended("它 '是' 什么"), "它 '是' 什么")
        self.assertEqual(_normalize_punctuation_extended("It 'is' what"), "It 'is' what")
    
    def test_brackets_conversion(self):
        self.assertEqual(_normalize_punctuation_extended("见[附录]"), "见【附录】")
        self.assertEqual(_normalize_punctuation_extended("See [appendix]"), "See [appendix]")
        self.assertEqual(_normalize_punctuation_extended("({test})"), "（{test}）")  # parens convert, braces don't
    
    def test_ellipsis_conversion(self):
        self.assertEqual(_normalize_punctuation_extended("等等..."), "等等……")
        self.assertEqual(_normalize_punctuation_extended("Wait..."), "Wait...")
    
    def test_semicolon_colon_conversion(self):
        self.assertEqual(_normalize_punctuation_extended("分号；冒号："), "分号；冒号：")
        self.assertEqual(_normalize_punctuation_extended("a;b c:d"), "a;b c:d")  # Latin context
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python test_stringmanage.py`
Expected: FAIL - function not defined

- [ ] **Step 3: Implement `_normalize_punctuation_extended` in `stringmanage.py`**

Add new conversion maps for:
- Double quotes: `"` `"` → `""` `""` (when adjacent to CJK)
- Single quotes: `'` `'` → `''` `''` (when adjacent to CJK)  
- Square brackets: `[` `]` → `【` `】` (when adjacent to CJK)
- Ellipsis: `...` → `……` (when adjacent to CJK)
- Extend existing `_normalize_punctuation` or create new function that builds on it

Implementation approach: Add new regex patterns similar to existing `_HALF_TO_FULL` / `_FULL_TO_HALF` but for additional symbols. Process after existing normalization.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python test_stringmanage.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add stringmanage.py test_stringmanage.py
git commit -m "feat: add extended punctuation/symbol conversion for smart cleaning"
```

---

### Task 2: Add clean settings to configmanage.py

**Files:**
- Modify: `D:\code-project\python\PToEA\configmanage.py` (add clean settings to DEFAULT_CONFIG and ui_settings validation)
- Test: `D:\code-project\python\PToEA\test_configmanage.py` (if exists, or test via existing)

**Interfaces:**
- Consumes: DEFAULT_CONFIG structure, ui_settings validation logic
- Produces: New ui_settings keys: `clean_punctuation`, `clean_symbols`, `clean_md_tags`, `clean_html_tags`, `clean_paragraphs`

- [ ] **Step 1: Write failing test for new config settings**

```python
# In test_configmanage.py or inline test
def test_clean_settings_in_ui_settings():
    from configmanage import get_config, set_ui_settings
    cfg = get_config()
    # Check new keys exist with defaults
    assert "clean_punctuation" in cfg["ui_settings"]
    assert "clean_symbols" in cfg["ui_settings"]
    assert "clean_md_tags" in cfg["ui_settings"]
    assert "clean_html_tags" in cfg["ui_settings"]
    assert "clean_paragraphs" in cfg["ui_settings"]
    # Test setting them
    set_ui_settings({"clean_punctuation": False, "clean_symbols": True})
    cfg = get_config()
    assert cfg["ui_settings"]["clean_punctuation"] == False
    assert cfg["ui_settings"]["clean_symbols"] == True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python test_configmanage.py`
Expected: FAIL - keys don't exist

- [ ] **Step 3: Add clean settings to DEFAULT_CONFIG and validation in `configmanage.py`**

In `DEFAULT_CONFIG["ui_settings"]` add:
```python
"clean_punctuation": True,    # 英文标点→中文标点（全局）
"clean_symbols": True,        # 英文符号→中文符号（引号、括号、省略号等）
"clean_md_tags": True,        # 移除残留 Markdown 语法（**粗体**、#标题等）
"clean_html_tags": True,      # 移除非白名单 HTML 标签（保留用户设置的富文本）
"clean_paragraphs": False,    # 合并相邻普通段落（OCR 拆段修复）
```

In `set_ui_settings`, add validation for each new boolean key (similar to `rule_all_pages_confirm`):
```python
if "clean_punctuation" in ui:
    v = ui["clean_punctuation"]
    clean["clean_punctuation"] = True if v is True else False
# ... repeat for clean_symbols, clean_md_tags, clean_html_tags, clean_paragraphs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python test_configmanage.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add configmanage.py test_configmanage.py
git commit -m "feat: add smart clean settings to ui_settings config"
```

---

### Task 3: Extend clean_page_html in correctmanage.py with new options

**Files:**
- Modify: `D:\code-project\python\PToEA\correctmanage.py` (extend clean_page_html signature and logic)
- Test: `D:\code-project\python\PToEA\test_correctmanage.py` (add tests for new options)

**Interfaces:**
- Consumes: `_normalize_punctuation_extended` from stringmanage, new config options
- Produces: Extended `clean_page_html(raw: str, *, merge_paragraphs: bool = False, strip_leading_symbols: bool = True, normalize_punctuation: bool = True, strip_tags: bool = True, clean_punctuation: bool = True, clean_symbols: bool = True, clean_md_tags: bool = True, clean_html_tags: bool = True, clean_paragraphs: bool = False) -> str`

- [ ] **Step 1: Write failing tests for new clean_page_html options**

```python
# In test_correctmanage.py
class TestCleanPageHtmlExtended(unittest.TestCase):
    def test_clean_punctuation_false(self):
        # When disabled, English punctuation in Chinese context stays as-is
        src = '<p>你好,世界</p>'
        out = clean_page_html(src, clean_punctuation=False)
        self.assertEqual(out, '<p>你好,世界</p>')  # comma not converted
    
    def test_clean_symbols_false(self):
        src = '<p>他说 "hello"</p>'
        out = clean_page_html(src, clean_symbols=False)
        self.assertEqual(out, '<p>他说 "hello"</p>')  # quotes not converted
    
    def test_clean_md_tags_true(self):
        # Remove **bold** markdown syntax
        src = '<p>这是**重点**内容</p>'
        out = clean_page_html(src, clean_md_tags=True)
        self.assertEqual(out, '<p>这是重点内容</p>')
    
    def test_clean_html_tags_true(self):
        # Remove non-whitelisted tags but preserve rich text
        src = '<p>正文<div>脏标签</div><strong>粗体</strong></p>'
        out = clean_page_html(src, clean_html_tags=True)
        self.assertEqual(out, '<p>正文脏标签<strong>粗体</strong></p>')
    
    def test_clean_paragraphs_true(self):
        # Merge adjacent plain paragraphs
        src = '<p>第一段</p><p>续文</p>'
        out = clean_page_html(src, clean_paragraphs=True)
        self.assertEqual(out, '<p>第一段续文</p>')
    
    def test_idempotent_with_all_options(self):
        src = '<p>你好,世界**重点**</p>'
        once = clean_page_html(src, clean_punctuation=True, clean_symbols=True, clean_md_tags=True)
        twice = clean_page_html(once, clean_punctuation=True, clean_symbols=True, clean_md_tags=True)
        self.assertEqual(once, twice)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python test_correctmanage.py`
Expected: FAIL - new parameters not accepted

- [ ] **Step 3: Implement extended `clean_page_html` in `correctmanage.py`**

Modify the function signature to accept new parameters with defaults matching config defaults. In the function body:
1. If `clean_punctuation`: apply `_normalize_punctuation_extended` (import from stringmanage)
2. If `clean_symbols`: apply extended symbol conversion (quotes, brackets, ellipsis)
3. If `clean_md_tags`: strip markdown syntax like `**`, `# `, `* ` before sanitize
4. If `clean_html_tags`: use existing `_STRIP_TAG_RE` (already done via `strip_tags` parameter)
5. If `clean_paragraphs`: pass as `merge_paragraphs` to `_clean_blocks`

Import `_normalize_punctuation_extended` from stringmanage at function level (lazy import).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python test_correctmanage.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add correctmanage.py test_correctmanage.py
git commit -m "feat: extend clean_page_html with configurable cleaning options"
```

---

### Task 4: Update /api/clean endpoint to pass config settings

**Files:**
- Modify: `D:\code-project\python\PToEA\epubeditmanage.py` (update _api_clean to read config and pass options)
- Test: `D:\code-project\python\PToEA\test_epubeditmanage.py` (add test)

**Interfaces:**
- Consumes: configmanage.get_config() to read ui_settings
- Produces: Updated _api_clean that passes clean settings to clean_page_html

- [ ] **Step 1: Write failing test for API endpoint with config**

```python
# In test_epubeditmanage.py
def test_api_clean_uses_config():
    # Mock config with clean settings
    # Call _api_clean with HTML
    # Verify clean_page_html called with config values
    pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python test_epubeditmanage.py`
Expected: FAIL

- [ ] **Step 3: Update `_api_clean` in `epubeditmanage.py`**

In `_api_clean`:
1. Call `get_config()` to read current config
2. Extract `ui_settings` clean options
3. Pass them to `clean_page_html(html_text, **clean_options)`
4. Handle missing config gracefully (use defaults)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python test_epubeditmanage.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add epubeditmanage.py test_epubeditmanage.py
git commit -m "feat: update /api/clean to use config settings for smart cleaning"
```

---

### Task 5: Add UI settings panel for clean options in ui/app.js

**Files:**
- Modify: `D:\code-project\python\PToEA\ui\app.js` (add clean settings to settings modal)
- Test: Manual verification via browser

**Interfaces:**
- Consumes: uiSettings object, saveUiSettings function
- Produces: New checkboxes in settings modal under "清理" or "界面" section

- [ ] **Step 1: Add clean settings to UI_SETTINGS_DEFAULTS in app.js**

```javascript
const UI_SETTINGS_DEFAULTS = {
    // ... existing ...
    clean_punctuation: true,
    clean_symbols: true,
    clean_md_tags: true,
    clean_html_tags: true,
    clean_paragraphs: false,
};
```

- [ ] **Step 2: Add UI elements in settings modal (in correctmanage.py _UI_HTML or wherever settings UI is defined)**

Actually, the settings modal HTML is in `correctmanage.py` in the `_UI_HTML` constant. Need to add checkboxes there.

- [ ] **Step 3: Add save/load logic for new settings in app.js**

In `saveUIMirror` and `loadUiSettingsFromServer`, handle the new keys.

- [ ] **Step 4: Test manually in browser**

Start the GUI, open settings, verify new checkboxes appear and work.

- [ ] **Step 5: Commit**

```bash
git add ui/app.js correctmanage.py
git commit -m "feat: add smart clean settings UI in settings modal"
```

---

### Task 6: Run full test suite and verify integration

**Files:**
- Run all existing tests

**Interfaces:**
- All existing tests must pass
- New tests must pass

- [ ] **Step 1: Run stringmanage tests**

Run: `uv run python test_stringmanage.py`
Expected: PASS

- [ ] **Step 2: Run configmanage tests**

Run: `uv run python test_configmanage.py` (if exists)
Expected: PASS

- [ ] **Step 3: Run correctmanage tests**

Run: `uv run python test_correctmanage.py`
Expected: PASS

- [ ] **Step 4: Run epubeditmanage tests**

Run: `uv run python test_epubeditmanage.py`
Expected: PASS

- [ ] **Step 5: Run all tests**

Run: `uv run python -m unittest discover -s . -p "test_*.py"`
Expected: PASS (all tests pass)

- [ ] **Step 6: Manual integration test**

Start the app, open a document, click "智能清理" button, verify:
1. English punctuation converts to Chinese in Chinese context
2. English symbols (quotes, brackets, ellipsis) convert in Chinese context
3. Markdown syntax (**bold**, # heading) is removed
4. Non-whitelisted HTML tags are stripped but rich text (bold, italic, markers) preserved
5. Settings checkboxes control each behavior
6. Settings persist after restart

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: complete smart cleaning enhancement with config and UI"
```