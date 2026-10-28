# -*- coding: utf-8 -*-
"""搜索/替换回归护栏（2026-10：修复「当前页替换失效」等缺陷后补的测试）。

覆盖的不变量（括号内是本次修复的缺陷编号，见各注释）：
  A1 viewportPage 判定「当前页」必须与 jumpToPage/scrollToIndex 的落点用同一条线
     （hostTop + prefixTop(idx) - toolbarOffset()，即吸顶工具栏实测高度的下沿）——
     否则跳转后目标页 rect.top=工具栏高度，判定取到被工具栏盖住的上一页，「当前页」范围
     打错页 → 替换失效，工具栏页码也少一页。
     （2026-10：工具栏改为 Ribbon 三段结构，高度不再是定值 60px，故判定线由常量改为实测。）
  A2 replaceAll 必须用 _replaceScopeIndexes()（沿用搜索时锁定的 _scopeSearchPage），
     替换时不能重新解析 _searchScopeIndexes()（视口变了就打到别的页）。
  A3 搜索「当前页」= currentPageIndex()：光标/选区所在页优先，取不到再回退视口页
     （2026-10-18：只用视口页时，正在编辑但不在视口顶部的那一页会被判成上一页 →
      对着该页搜「当前页」找不到匹配，而全局搜索正常）。
  B1 editedSet 只标记真正变化的页；B2 pageText 有 mdMode 分支（与整页替换同源）；
  B3 搜索/替换共用 _searchTokens 切分（命中不跨标签）；B4 decodeEntities 的 &amp; 最后解；
  B5 searchPages 不无条件 openSearchModal() 抢焦点；B6 高亮有 _highlightScopeOk 门控；
  B7 实体安全 _matchEntitySafe 贯穿搜索/高亮/替换；B8 replaceCurrent 空词守卫；
  B9 输入框清空走 clearSearchState()（连结果/锁定页一起清）。

静态断言永远执行；行为断言（抽函数进 Node vm 跑真实逻辑）在有 node 时执行，
无 node 自动跳过——与仓库「标准库 unittest + 手工 smoke」约定一致。
"""
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_JS = os.path.join(ROOT, 'ui', 'app.js')


def read_app():
    with open(APP_JS, 'r', encoding='utf-8') as f:
        return f.read()


def function_body(src, name):
    """截取 `function name(` 起、到下一个顶层 `function ` 为止的源码（粗粒度）。"""
    i = src.index('function %s(' % name)
    j = src.find('\nfunction ', i + 1)
    return src[i:] if j < 0 else src[i:j]


class SearchReplaceStaticTest(unittest.TestCase):
    """静态不变量：ui/app.js 的关键代码形状不许回退。"""

    @classmethod
    def setUpClass(cls):
        cls.src = read_app()

    # ---------- A1：视口页判定线与跳转落点一致 ----------
    def test_viewport_page_uses_jump_landing_line(self):
        src = self.src
        # 2026-10：吸顶工具栏改为 Ribbon 三段结构（选项卡行 + 面板行 + 状态行，面板还会换行），
        # 高度不再是 60px 定值 → 判定线/落点统一改为 toolbarOffset() 实测。
        # 不变量仍是：判定线与跳转落点必须取同一个高度。
        self.assertEqual(src.count('function toolbarOffset()'), 1,
                         'toolbarOffset 应只声明一次')
        self.assertNotIn('const VIEW_TOP_OFFSET = 60;', src,
                         '不得回退到写死 60px 的旧实现（Ribbon 工具栏高度会变化）')
        self.assertIn('const topOff = toolbarOffset();', src)
        self.assertIn('rect.top <= topOff', src,
                      'viewportPage 应按工具栏下沿线判定（原 rect.top<=0 取到上一页）')
        self.assertIn('const y = Math.max(0, window.scrollY - hostTop + topOff)', src,
                      'viewportPage 兜底二分必须用同一条线')
        self.assertNotIn('const viewportTop = window.scrollY;', src,
                         '旧的「窗口顶 y=0」判定不得回归')
        # 跳转三处落点必须引用同一来源（而非散落字面量 60）
        self.assertGreaterEqual(src.count('hostTop + prefixTop(idx) - toolbarOffset()'), 3,
                                'jumpToPage/jumpToPageIndex/scrollToIndex 落点应引用 toolbarOffset()')

    # ---------- A2：替换沿用搜索锁定页 ----------
    def test_replace_uses_locked_scope_page(self):
        src = self.src
        rb = function_body(src, 'replaceAll')
        self.assertIn('_replaceScopeIndexes()', rb,
                      'replaceAll 必须用替换专用范围（沿用搜索锁定页）')
        self.assertNotIn('_searchScopeIndexes()', rb,
                         'replaceAll 不得在替换时重新解析视口页')
        self.assertIn('_scopeSearchPage = (scopePage && scopeIdx.length)', src,
                      'searchPages 必须锁定本次搜索实际作用的当前页')
        self.assertIn('let _scopeSearchPage = -1;', src)
        self.assertIn('_scopeSearchPage = -1;', function_body(src, 'clearSearchState'),
                      '清空搜索状态时应解除锁定')

    # ---------- A3：搜索「当前页」优先光标页，回退视口页 ----------
    def test_search_scope_prefers_caret_page(self):
        src = self.src
        self.assertIn('function currentPageIndex(', src,
                      '应定义 currentPageIndex（光标页优先，回退视口页）')
        cp = function_body(src, 'currentPageIndex')
        self.assertIn('currentEditable()', cp, 'currentPageIndex 应优先取当前光标/编辑页')
        self.assertIn('return viewportPage();', cp, '取不到光标页时应回退视口页')
        sb = function_body(src, '_searchScopeIndexes')
        self.assertIn('return [currentPageIndex()];', sb,
                      '搜索「当前页」必须走 currentPageIndex()（光标页优先，回退视口页）')
        self.assertNotIn('return [viewportPage()];', sb,
                         '搜索「当前页」不得只用视口页（会造成「找不到匹配对象」）')

    # ---------- B1：只标记真正变化的页 ----------
    def test_replace_all_marks_only_changed_pages(self):
        rb = function_body(self.src, 'replaceAll')
        self.assertIn('editedSet.add(ch.i)', rb)
        self.assertNotIn('i++) editedSet.add(i)', rb,
                         'replaceAll 不得无条件把所有页标成已编辑')
        self.assertIn('if (changed.length) dirty = true;', rb,
                      'dirty 只能置位（直接 dirty=true 会盖掉无变化场景的语义）')

    # ---------- B2/B3：同一份 token 切分 ----------
    def test_search_and_replace_share_tokenizer(self):
        src = self.src
        self.assertIn("if (mdMode) return pageSource(i) || '';", src,
                      'mdMode 下 pageText 必须返回整页源码（替换也是整页 replace）')
        self.assertIn('const toks = _searchTokens(i)', function_body(src, 'searchPages'))
        self.assertIn('_searchTokens(i)', function_body(src, 'replaceCurrent'))
        self.assertIn('_searchTokens(i)', function_body(src, 'replaceAll'))
        self.assertIn("if (!mdMode && tok.charAt(0) === '<')",
                      function_body(src, '_partialReplaceTokens'),
                      'mdMode 整页 token 可能以 < 开头，不能当标签跳过')

    # ---------- B4：实体解码顺序 ----------
    def test_decode_entities_amp_last(self):
        body = function_body(self.src, 'decodeEntities')
        self.assertLess(body.index("replace(/&lt;/g, '<')"), body.index("replace(/&amp;/g, '&')"),
                        '&amp; 必须最后解，否则 "&amp;lt;" 被双重解码')

    # ---------- B5：替换后不抢焦点 ----------
    def test_search_does_not_steal_focus_when_open(self):
        body = function_body(self.src, 'searchPages')
        self.assertIn("style.display !== 'flex') openSearchModal()", body,
                      '弹窗已打开时不得重复 open/focus（否则替换后按 Enter 变重搜）')
        self.assertNotIn('\n  openSearchModal();', body)

    # ---------- B6：高亮范围门控 ----------
    def test_highlight_gated_by_scope(self):
        src = self.src
        self.assertGreaterEqual(src.count('_highlightScopeOk('), 3,
                                '_highlightScopeOk 应被定义并在 displayHtml/applySearchHighlights 调用')
        self.assertIn('if (_searchHighlightQuery && _highlightScopeOk(i))', src)
        self.assertIn('if (!_highlightScopeOk(idx)) continue;', src)

    # ---------- B7：实体安全贯穿 ----------
    def test_entity_safety_used_everywhere(self):
        src = self.src
        for fn in ('searchPages', '_highlightInHtmlSource', '_partialApply'):
            self.assertIn('_matchEntitySafe(', function_body(src, fn),
                          '%s 应做实体安全判定' % fn)
        self.assertIn('_regionsEntitySafe(', function_body(src, '_partialApply'))
        rb = function_body(src, 'replaceAll')
        self.assertIn('_matchEntitySafe(', rb)
        rc = function_body(src, 'replaceCurrent')
        self.assertIn('_matchEntitySafe(', rc)
        self.assertIn('_replaceOffset(arguments)', rb + rc,
                      'String.replace 回调要用 _replaceOffset 取匹配起点')

    # ---------- B8/B9：空词守卫与清空语义 ----------
    def test_empty_query_guard_and_clear(self):
        src = self.src
        self.assertGreaterEqual(src.count('请先输入搜索词'), 3,
                                'searchPages/replaceCurrent/replaceAll 都要有空搜索词守卫')
        self.assertIn("if (!(this.value || '').trim()) clearSearchState();", src,
                      '输入框清空应连结果列表一起清（只清高亮会留下可点的旧结果）')

    # ---------- 语法门 ----------
    def test_app_js_syntax(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('未安装 node，跳过语法检查')
        # 显式 utf-8：Windows 默认按 GBK 解码 node 输出，含中文报错时测试自己先抛 UnicodeDecodeError
        r = subprocess.run([node, '--check', APP_JS], capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        self.assertEqual(r.returncode, 0, 'node --check 失败：\n%s' % (r.stderr or r.stdout))


HARNESS = r'''
const fs = require('fs'), vm = require('vm');
const app = fs.readFileSync(process.argv[2], 'utf8');

function extract(name) {
  const key = 'function ' + name + '(';
  const i = app.indexOf(key);
  if (i < 0) throw new Error('找不到函数: ' + name);
  let depth = 0, started = false, inStr = null, inLC = false, inBC = false;
  for (let j = i; j < app.length; j++) {
    const c = app[j], n = app[j + 1];
    if (inLC) { if (c === '\n') inLC = false; continue; }
    if (inBC) { if (c === '*' && n === '/') { inBC = false; j++; } continue; }
    if (inStr) { if (c === '\\') { j++; continue; } if (c === inStr) inStr = null; continue; }
    if (c === '/' && n === '/') { inLC = true; j++; continue; }
    if (c === '/' && n === '*') { inBC = true; j++; continue; }
    if (c === '"' || c === "'" || c === '`') { inStr = c; continue; }
    if (c === '{') { depth++; started = true; }
    else if (c === '}') { depth--; if (started && depth === 0) return app.slice(i, j + 1); }
  }
  throw new Error('括号不平衡: ' + name);
}
function extractDecl(pattern) {
  const m = app.match(pattern);
  if (!m) throw new Error('找不到声明: ' + pattern);
  return m[0];
}

const failures = [];
function ok(cond, msg) { if (!cond) failures.push(msg); }
function eq(a, b, msg) { ok(JSON.stringify(a) === JSON.stringify(b), msg + '（实际 ' + JSON.stringify(a) + '，期望 ' + JSON.stringify(b) + '）'); }

const decl = [
  extractDecl(/const _ENTITY_RE = [^\n]+;/)
].join('\n');

const stub = `
let ROWS = [];
let SCROLL_Y = 0;
let HOST_TOP = 0;
let PREFIX = [];
let pages = [];
let searchScope = 'all';
let _scopeSearchPage = -1;
let mdMode = false;
let PAGE_SRC = '';
let CARET_PAGE = -1;
const host = {
  querySelectorAll: function () { return ROWS; },
  getBoundingClientRect: function () { return { top: HOST_TOP }; }
};
const window = { get scrollY() { return SCROLL_Y; } };
// 工具栏遮挡高度桩：行为断言按 60px 这一固定线验证（真实实现读 #toolbar 实测高度）
function toolbarOffset() { return 60; }
function prefixTop(i) { return PREFIX[i]; }
function pageSource(i) { return PAGE_SRC; }
// 光标/选区所在页桩：CARET_PAGE>=0 时返回带 .page-row[data-i] 的编辑区
function currentEditable() {
  return CARET_PAGE >= 0 ? { isConnected: true, closest: function () { return { dataset: { i: String(CARET_PAGE) } }; } } : null;
}
function SET(o) {
  if (o.rows !== undefined) ROWS = o.rows;
  if (o.scrollY !== undefined) SCROLL_Y = o.scrollY;
  if (o.hostTop !== undefined) HOST_TOP = o.hostTop;
  if (o.prefix !== undefined) PREFIX = o.prefix;
  if (o.pages !== undefined) pages = o.pages;
  if (o.scope !== undefined) searchScope = o.scope;
  if (o.locked !== undefined) _scopeSearchPage = o.locked;
  if (o.md !== undefined) mdMode = !!o.md;
  if (o.src !== undefined) PAGE_SRC = o.src;
  if (o.caret !== undefined) CARET_PAGE = o.caret;
}
function row(i, top) { return { dataset: { i: String(i) }, getBoundingClientRect: function () { return { top: top }; } }; }
`;

const body = [
  decl,
  stub,
  extract('viewportPage'),
  extract('currentPageIndex'),
  extract('_searchScopeIndexes'),
  extract('_replaceScopeIndexes'),
  extract('_searchTokens'),
  extract('pageText'),
  extract('decodeEntities'),
  extract('_entitySpans'),
  extract('_matchEntitySafe'),
  extract('_regionsEntitySafe'),
  'module.exports = { viewportPage, currentPageIndex, _searchScopeIndexes, _replaceScopeIndexes, _searchTokens, pageText, decodeEntities, _entitySpans, _matchEntitySafe, _regionsEntitySafe, SET, row };'
].join('\n');

const mod = { exports: {} };
vm.runInNewContext(body, { module: mod, console });
const F = mod.exports;

// ---- A1a：跳转落点后（目标页 top=60），当前页 = 目标页（旧实现返回上一页） ----
F.SET({ scope: 'all', locked: -1, rows: [F.row(1, -340), F.row(2, 60)], scrollY: 0, hostTop: 0, pages: [0, 1, 2], prefix: [0, 400, 800] });
eq(F.viewportPage(), 2, '跳转落点(目标页 top=60) 时应返回目标页');

// ---- A1b：自然滚动（上一页顶边 -100，当前页顶边 400）→ 返回顶边≤60 的那页 ----
F.SET({ rows: [F.row(1, -100), F.row(2, 400)], scrollY: 0, hostTop: 0 });
eq(F.viewportPage(), 1, '自然滚动时应返回工具栏下沿处的那一页');

// ---- A1c：兜底二分（无挂载行）同一条线：scrollY=350、host 顶边在文档 0 处，每页 400px
//      → 判定线 y = 350 + 60 = 410 → 第 1 页（旧实现 y=350 → 第 0 页）
F.SET({ rows: [], scrollY: 350, hostTop: -350, pages: [0, 1, 2], prefix: [0, 400, 800] });
eq(F.viewportPage(), 1, '兜底二分应取包含工具栏下沿(350+60=410)的页');

// ---- A2：替换范围沿用锁定页 ----
F.SET({ scope: 'page', locked: 7, caret: 5, pages: [0,1,2,3,4,5,6,7,8], prefix: [0,400,800,1200,1600,2000,2400,2800,3200], scrollY: 0, rows: [F.row(1, -100)] });
eq(F._replaceScopeIndexes(), [7], '当前页范围：替换应沿用搜索锁定页');
eq(F._searchScopeIndexes(), [5], '当前页范围：搜索取光标页（正在编辑的那页）');
// ---- A3：无光标页 → 回退视口页（视口顶部那一页）----
F.SET({ caret: -1, rows: [F.row(1, -100)] });
eq(F._searchScopeIndexes(), [1], '无光标页时回退视口页');
F.SET({ scope: 'page', locked: -1, caret: -1 });
eq(F._replaceScopeIndexes(), F._searchScopeIndexes(), '无锁定页时回退到视口页');
F.SET({ scope: 'all', locked: 7 });
eq(F._replaceScopeIndexes().length, 9, '全部页范围不受锁定影响');

// ---- B7：实体安全规则（'a &amp; b' → 实体跨度 [2,7]；裸 & 不算实体）----
const sp = F._entitySpans('a &amp; b');
eq(sp, [[2, 7]], '实体跨度定位');
eq(F._matchEntitySafe(2, 7, sp), true, '整体包含实体=安全');
eq(F._matchEntitySafe(0, 1, sp), true, '完全不相交=安全');
eq(F._matchEntitySafe(4, 9, sp), false, '劈开实体=不安全');
eq(F._matchEntitySafe(3, 5, sp), false, '落在实体内部=不安全');
eq(F._matchEntitySafe(7, 9, sp), true, '实体之后=安全');
eq(F._entitySpans('Tom & co'), [], '裸 & 不是实体');
eq(F._entitySpans('Tom & Jerry &amp; co'), [[12, 17]], '多实体/裸 & 混排的跨度');
eq(F._regionsEntitySafe([{ s: 0, e: 2 }, { s: 2, e: 5 }], [[1, 4]]), false, '区域边界劈开实体=不安全');
eq(F._regionsEntitySafe([{ s: 0, e: 5 }, { s: 5, e: 9 }], [[1, 4]]), true, '实体完整落在一个区域内=安全');

// ---- B2：mdMode 的 pageText / token 切分与整页替换同源 ----
F.SET({ md: true, src: 'a <b>x</b> c' });
eq(F.pageText(0), 'a <b>x</b> c', 'mdMode 下 pageText 应返回整页源码');
eq(F._searchTokens(0).length, 1, 'mdMode 只有一个 token');
F.SET({ md: false });
eq(F.pageText(0), 'a x c', '富文本 pageText 去掉标签');
eq(F._searchTokens(0).length, 5, '富文本按标签切分');

// ---- B4：解码顺序 ----
eq(F.decodeEntities('&amp;lt;'), '&lt;', '&amp; 必须最后解，不能双重解码');
eq(F.decodeEntities('a &amp; b'), 'a & b', '普通实体照常解码');

if (failures.length) {
  console.log('FAIL');
  failures.forEach(function (f) { console.log(' - ' + f); });
  process.exit(1);
}
console.log('OK');
'''


class SearchReplaceBehaviorTest(unittest.TestCase):
    """行为断言：把 ui/app.js 里的真函数抽进 Node vm，跑真实逻辑。"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')

    def test_functions_behave_as_fixed(self):
        if not self.node:
            self.skipTest('未安装 node，跳过行为测试')
        with tempfile.TemporaryDirectory() as td:
            script = os.path.join(td, 'harness.js')
            with open(script, 'w', encoding='utf-8') as f:
                f.write(textwrap.dedent(HARNESS))
            r = subprocess.run([self.node, script, APP_JS], capture_output=True, text=True,
                               encoding='utf-8', errors='replace')
        out = (r.stdout or '') + (r.stderr or '')
        self.assertEqual(r.returncode, 0, '行为断言未通过：\n%s' % out)
        self.assertIn('OK', out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
