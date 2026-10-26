# -*- coding: utf-8 -*-
"""「按格式查找 / 把一种格式替换成另一种格式」回归护栏（2026-10-21 新增功能）。

覆盖的不变量：
  F1 查找命中的元素是**按格式**匹配的，不是按文字：
     下划线类 / 元素级（粗体·斜体）/ 字符样式（颜色·字号·字体）三类都要认。
  F2 替换要同时改写**闭标签**（<strong> → <span class="ptoe-underline"> 时
     </strong> 必须变 </span>，否则产出非法 HTML）。
  F3 替换只动目标格式，其余 class / style 声明必须保留。
  F4 字符样式走 normalizeCharStyle/charStyleString —— 与后端
     rulemanage.normalize_char_style 同一契约（顺序 text-align;font-size;font-family;color）。
  F5 公式（MathML）不受影响：<math> 及其属性不能被格式替换改坏。
  F6 查找/替换的数据源是**页面源码**（contentMap），不是 DOM ——
     页面是虚拟滚动的，只扫 DOM 会漏掉屏幕外的页。

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


class FormatFindReplaceStaticTest(unittest.TestCase):
    """静态不变量：ui/app.js 的关键代码形状不许回退。"""

    @classmethod
    def setUpClass(cls):
        cls.src = read_app()

    def test_format_specs_table_exists(self):
        src = self.src
        self.assertIn('const FORMAT_SPECS = [', src, '缺少格式目标表 FORMAT_SPECS')
        for spec in ('ptoe-underline', 'ptoe-wavy', 'ptoe-sup', 'ptoe-sub',
                     'ptoe-highlight-green', 'ptoe-ruby'):
            self.assertIn("'%s'" % spec, src, '格式表缺少行内格式类: ' + spec)
        self.assertIn("kind:'tag'", src, '格式表缺少元素级格式（粗体/斜体）')
        self.assertIn("kind:'style'", src, '格式表缺少字符样式格式（颜色/字号/字体）')

    def test_ui_controls_exist(self):
        """搜索弹窗里要有「按格式」一组的控件。"""
        import correctmanage

        html = correctmanage._UI_HTML
        for eid in ('fmtFindSel', 'fmtFindBtn', 'fmtReplaceSel', 'fmtReplaceBtn',
                    'fmtValueInput', 'fmtFontSel'):
            self.assertIn('id="%s"' % eid, html, '搜索弹窗缺少控件: ' + eid)

    def test_find_and_replace_use_source_not_dom(self):
        """查找/替换必须扫页面源码（contentMap），不能只扫已挂载的 DOM。"""
        fb = function_body(self.src, 'findByFormat')
        rb = function_body(self.src, 'replaceFormat')
        self.assertIn('pageSource(i)', fb, '按格式查找应扫页面源码')
        self.assertIn('pageSource(i)', rb, '按格式替换应扫页面源码')
        self.assertIn('contentMap.set(i, rr.text)', rb, '替换结果要写回 contentMap')

    def test_replace_records_history_and_dirty(self):
        rb = function_body(self.src, 'replaceFormat')
        self.assertIn("histBegin('替换格式'", rb, '按格式替换要进撤销栈')
        self.assertIn('histEnd(before,', rb, '按格式替换要结束历史事务')
        self.assertIn('editedSet.add(i)', rb, '改动的页要标记为已编辑')
        self.assertIn('dirty = true', rb)

    def test_char_style_uses_backend_contract(self):
        """字符样式改写必须走 normalizeCharStyle/charStyleString（与后端同契约）。"""
        ab = function_body(self.src, '_fmtApplySpec')
        self.assertIn('charStyleDeclMap(', ab)
        self.assertIn('charStyleString(', ab)

    def test_formula_not_broken_by_format_replace(self):
        """格式替换不得改坏公式：<math> 不在行内类/元素级/字符样式三类里。"""
        src = self.src
        mb = function_body(src, '_fmtMatch')
        self.assertIn("spec.kind === 'class'", mb)
        self.assertIn("name !== 'span'", mb, '行内类/字符样式只认 span')
        tb = function_body(src, '_fmtTagRe') if '_fmtTagRe' in src else None
        self.assertIsNone(tb)  # 占位：没有独立的标签表时由 FORMAT_SPECS 承担

    def test_app_js_syntax(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('未安装 node，跳过语法检查')
        r = subprocess.run([node, '--check', APP_JS], capture_output=True, text=True)
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
  extractDecl(/const CHAR_STYLE_PROPS = \[[^\]]*\];/),
  extractDecl(/const CHAR_FONT_STACKS = \[[^\]]*\];/),
  extractDecl(/const CHAR_SIZE_MIN = \d+, CHAR_SIZE_MAX = \d+;/),
  extractDecl(/const CHAR_ALIGN_VALUES = \[[^\]]*\];/),
  extractDecl(/const _CHAR_COLOR_RE = [^;]+;/),
  extractDecl(/const _CHAR_FONT_SIZE_RE = [^;]+;/),
  extractDecl(/const _CHAR_FONT_QUOTE_RE = [^;]+;/),
  extractDecl(/const _CHAR_FONT_WS_RE = [^;]+;/),
  extractDecl(/const FORMAT_SPECS = \[[\s\S]*?\n\];/),
].join('\n');

const body = [
  decl,
  extract('_roundHalfEven'),
  extract('_canonCharDecl'),
  extract('normalizeCharStyle'),
  extract('charStyleGet'),
  extract('charStyleString'),
  extract('charStyleDeclMap'),
  extract('decodeEntities'),
  extract('_fmtSpecById'),
  extract('_fmtAttrGet'),
  extract('_fmtAttrSet'),
  extract('_fmtClassList'),
  extract('_fmtMatch'),
  extract('_fmtTransform'),
  extract('_fmtApplySpec'),
  extract('_fmtFindIn'),
  'module.exports = { FORMAT_SPECS, _fmtSpecById, _fmtApplySpec, _fmtFindIn, normalizeCharStyle, charStyleGet };'
].join('\n');

const mod = { exports: {} };
vm.runInNewContext(body, { module: mod, console });
const F = mod.exports;
const spec = function (id) { return F._fmtSpecById(id); };

// ---- F1a：行内类（下划线）查找 ----
let src = '<p>甲<span class="ptoe-underline">乙</span>丙</p>';
eq(F._fmtFindIn(src, spec('ptoe-underline')).length, 1, '下划线应命中 1 处');
eq(F._fmtFindIn(src, spec('ptoe-underline'))[0].text, '乙', '命中的文本片段');
eq(F._fmtFindIn(src, spec('ptoe-wavy')).length, 0, '波浪线不应命中');

// ---- F1b：元素级（粗体/斜体）查找 ----
src = '<p>甲<strong>乙</strong><em>丙</em></p>';
eq(F._fmtFindIn(src, spec('bold')).length, 1, '粗体应命中 1 处');
eq(F._fmtFindIn(src, spec('italic')).length, 1, '斜体应命中 1 处');
eq(F._fmtFindIn(src, spec('bold'))[0].text, '乙', '粗体文本片段');

// ---- F1c：字符样式（颜色）查找 ----
src = '<p><span style="color:#ff0000">甲</span><span style="font-size:20px">乙</span></p>';
eq(F._fmtFindIn(src, spec('color')).length, 1, '红色应命中 1 处');
eq(F._fmtFindIn(src, spec('font-size')).length, 1, '20px 字号应命中 1 处');
eq(F._fmtFindIn(src, spec('color'))[0].text, '甲', '颜色命中的文本');

// ---- F2：替换必须同时改写闭标签 ----
src = '<p>甲<strong>乙</strong>丙</p>';
let r = F._fmtApplySpec(src, spec('bold'), spec('ptoe-underline'), '');
eq(r.count, 1, '粗体 → 下划线应命中 1 处');
eq(r.text, '<p>甲<span class="ptoe-underline">乙</span>丙</p>', '闭标签必须一并改成 </span>');

src = '<p>甲<span class="ptoe-underline">乙</span>丙</p>';
r = F._fmtApplySpec(src, spec('ptoe-underline'), spec('bold'), '');
eq(r.text, '<p>甲<strong>乙</strong>丙</p>', 'span → strong 时闭标签必须改成 </strong>');

// ---- F3：只动目标格式，其余 class 保留 ----
src = '<p><span class="ptoe-note ptoe-underline">乙</span></p>';
r = F._fmtApplySpec(src, spec('ptoe-underline'), spec('ptoe-strike'), '');
eq(r.text, '<p><span class="ptoe-note ptoe-strike">乙</span></p>', '其余 class 必须保留');

// ---- F4：字符样式走后端同契约（声明顺序固定） ----
src = '<p><span style="font-size:20px">乙</span></p>';
r = F._fmtApplySpec(src, spec('font-size'), spec('color'), '#ff0000');
eq(r.text, '<p><span style="color:#ff0000">乙</span></p>', '字号 → 颜色：旧声明应被摘掉');
src = '<p><span style="color:#ff0000">乙</span></p>';
r = F._fmtApplySpec(src, spec('color'), spec('font-size'), '24px');
eq(r.text, '<p><span style="font-size:24px">乙</span></p>', '颜色 → 字号');

// ---- F4b：样式声明顺序恒定（text-align;font-size;font-family;color） ----
src = '<p><span style="color:#ff0000">乙</span></p>';
r = F._fmtApplySpec(src, spec('color'), spec('font-size'), '24px');
eq(F.charStyleGet('font-size:24px;color:#ff0000', 'color'), '#ff0000', 'charStyleGet 取值');
eq(F.normalizeCharStyle('color:#ff0000;font-size:24px'), 'font-size:24px;color:#ff0000', '声明顺序恒定');

// ---- F5：公式不受格式替换影响 ----
src = '<p><math xmlns="http://www.w3.org/1998/Math/MathML" alttext="E=mc^2"><semantics><mrow><mi>E</mi></mrow></semantics></math><span class="ptoe-underline">乙</span></p>';
r = F._fmtApplySpec(src, spec('ptoe-underline'), spec('ptoe-wavy'), '');
ok(r.text.indexOf('<math xmlns="http://www.w3.org/1998/Math/MathML" alttext="E=mc^2">') >= 0, '公式元素不得被改坏');
ok(r.text.indexOf('ptoe-wavy') >= 0, '目标格式应被替换');
eq(F._fmtFindIn(src, spec('color')).length, 0, '公式不带颜色格式');

// ---- F6：多处命中全部替换 ----
src = '<p><span class="ptoe-sup">x</span>和<span class="ptoe-sup">y</span>与<span class="ptoe-sup">z</span></p>';
r = F._fmtApplySpec(src, spec('ptoe-sup'), spec('ptoe-sub'), '');
eq(r.count, 3, '三处上标应全部命中');
eq(r.text.indexOf('ptoe-sup'), -1, '不应残留上标类');
eq((r.text.match(/ptoe-sub/g) || []).length, 3, '应有三处下标类');

if (failures.length) {
  console.log('FAIL');
  failures.forEach(function (f) { console.log(' - ' + f); });
  process.exit(1);
}
console.log('OK');
'''


class FormatFindReplaceBehaviorTest(unittest.TestCase):
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
            r = subprocess.run([self.node, script, APP_JS], capture_output=True, text=True)
        out = (r.stdout or '') + (r.stderr or '')
        self.assertEqual(r.returncode, 0, '行为断言未通过：\n%s' % out)
        self.assertIn('OK', out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
