# -*- coding: utf-8 -*-
"""智能清理「指定页面」回归护栏（2026-10-18）。

覆盖的不变量：
  C1 命令行「清理」支持页范围：无参数 = 当前页（光标页优先，回退视口页）；
     带参数复用 _cmdPageSpecIndices（N / N-M / +X / 当前 / 全部）。
  C2 工具栏「清理」按钮仍清全部页（cleanAll → cleanPages(null)，保持原行为）。
  C3 collectIndices(indices) 按页下标过滤；collect() 等价于 collectIndices(null)。
  C4 applyCleanPreview 只把真正变化的页标记为已编辑（范围清理时不虚高）。
  C5 _cmdPageSpecIndices 的解析语义：''→[base]、全部、+X、N、N-M、非法→null。

静态断言永远执行；行为断言（抽 _cmdPageSpecIndices 进 Node vm 跑真实逻辑）在有
node 时执行，无 node 自动跳过——与仓库「标准库 unittest + 手工 smoke」约定一致。
"""
import os
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


class CleanRangeStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = read_app()

    # ---------- C1：命令行「清理」页范围 ----------
    def test_cmd_clean_spec_uses_page_range(self):
        src = self.src
        self.assertIn('async function _cmdCleanSpec(', src)
        body = function_body(src, '_cmdCleanSpec')
        self.assertIn('_cmdPageSpecIndices(args, currentPageIndex(ed))', body,
                      '清理应复用页范围解析，基准页取当前页（光标页优先）')
        self.assertIn('await cleanPages(ps.indices);', body)
        self.assertIn('页范围无效', body, '非法范围要有提示')
        self.assertIn('未找到匹配的页', body, '空范围要有提示')

    def test_clean_builtin_registered(self):
        src = self.src
        self.assertIn("keys: ['清理', '智能清理', 'clean']", src,
                      '应把「清理」注册为内置命令（带用法提示）')
        i = src.index("keys: ['清理', '智能清理', 'clean']")
        entry = src[i:i + 600]
        self.assertIn('global: true', entry, '清理无需正文焦点（可在任意页执行）')
        self.assertIn('_cmdCleanSpec(ctx.ed, ctx.args)', entry)
        self.assertIn('无参数=当前页', entry, '用法说明应写明无参数=当前页')

    # ---------- C1b：命令行「清空」页范围 ----------
    def test_cmd_clear_spec_uses_page_range(self):
        src = self.src
        self.assertIn('function _cmdClearSpec(', src)
        body = function_body(src, '_cmdClearSpec')
        self.assertIn('_cmdPageSpecIndices(args, currentPageIndex(ed))', body,
                      '清空应复用页范围解析，基准页取当前页（光标页优先）')
        self.assertIn('_cmdClearPages(ps.indices);', body)
        self.assertIn('页范围无效', body, '非法范围要有提示')
        self.assertIn('未找到匹配的页', body, '空范围要有提示')

    def test_clear_builtin_registered(self):
        src = self.src
        self.assertIn("keys: ['clear', '清空', '清除', 'empty']", src)
        i = src.index("keys: ['clear', '清空', '清除', 'empty']")
        entry = src[i:i + 900]
        self.assertIn('global: true', entry, '清空无需正文焦点（默认当前选中页）')
        self.assertIn('_cmdClearSpec(ctx.ed, ctx.args)', entry)
        self.assertIn('ctx.spec.loc', entry, '行范围前缀语法应继续按行清空')
        self.assertIn('_cmdClearRange(ctx.ed, blocks, idxs)', entry)
        self.assertIn('无参数=当前页', entry, '用法说明应写明无参数=当前页')

    def test_clear_pages_updates_offscreen_sources(self):
        body = function_body(self.src, '_cmdClearPages')
        self.assertIn("histRun('清空页面', uniq", body, '多页清空应合并为一次撤销')
        self.assertIn("contentMap.set(i, '')", body, '离屏页也必须从 contentMap 清空')
        self.assertIn("mdSourceMap.set(i, '')", body, 'Markdown 模式源串也要清空')
        self.assertIn('syncContent(ed)', body, '已挂载页清空后同步回源串')
        self.assertIn('scheduleRemeasure(i)', body, '清空后重测行高')

    # ---------- C2：工具栏按钮仍清全部页 ----------
    def test_toolbar_clean_all_pages(self):
        src = self.src
        self.assertIn('async function cleanAll() { return cleanPages(null); }', src,
                      '工具栏/快捷键 cleanAll 必须清全部页（保持原行为）')
        self.assertIn("document.getElementById('cleanBtn').addEventListener('click', cleanAll)", src)
        self.assertIn('clean: cleanAll,', src, '快捷键动作仍指向 cleanAll')

    # ---------- C3：collectIndices 过滤 ----------
    def test_collect_indices_filters(self):
        src = self.src
        body = function_body(src, 'collectIndices')
        self.assertIn('new Set(indices)', body)
        self.assertIn('if (set && !set.has(i)) continue;', body)
        self.assertIn('function collect() { return collectIndices(null); }', src)
        cp = function_body(src, 'cleanPages')
        self.assertIn('pages: collectIndices(indices)', cp,
                      'cleanPages 只把目标页发给 /api/clean')

    # ---------- C4：只标记变化的页 ----------
    def test_apply_clean_preview_marks_changed_only(self):
        body = function_body(self.src, 'applyCleanPreview')
        self.assertIn('for (const c of changed) editedSet.add(c.idx);', body)
        self.assertNotIn('i++) editedSet.add(i)', body,
                         'applyCleanPreview 不得把所有页标成已编辑')

    # ---------- 语法门 ----------
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

const failures = [];
function ok(cond, msg) { if (!cond) failures.push(msg); }
function eq(a, b, msg) { ok(JSON.stringify(a) === JSON.stringify(b), msg + '（实际 ' + JSON.stringify(a) + '，期望 ' + JSON.stringify(b) + '）'); }

const stub = `
let pages = [];
function SETPAGES(p) { pages = p; }
`;

const body = [
  stub,
  extract('_cmdPageSpecIndices'),
  'module.exports = { _cmdPageSpecIndices, SETPAGES };'
].join('\n');

const mod = { exports: {} };
vm.runInNewContext(body, { module: mod, console });
const F = mod.exports;

// 模拟 20 页，原书页号 1..20（page === index + 1）
const P = [];
for (let k = 1; k <= 20; k++) P.push({ page: k });
F.SETPAGES(P);

eq(F._cmdPageSpecIndices('', 5), { indices: [5] }, '无参数 = 当前页（基准页）');
eq(F._cmdPageSpecIndices('   ', 5), { indices: [5] }, '空白参数 = 当前页');
eq(F._cmdPageSpecIndices('当前', 0), { indices: [0] }, '当前 = 基准页（0 也有效，不落回 0）');
eq(F._cmdPageSpecIndices('.', 5), { indices: [5] }, '. = 当前页');
eq(F._cmdPageSpecIndices('全部', 5).indices.length, 20, '全部 = 整本');
eq(F._cmdPageSpecIndices('+10', 5), { indices: [5,6,7,8,9,10,11,12,13,14] }, '+X = 当前页起 X 页');
eq(F._cmdPageSpecIndices('+3', 5), { indices: [5,6,7] }, '清空 +3 = 当前页起共 3 页');
eq(F._cmdPageSpecIndices('+10', 17).indices, [17,18,19], '+X 触底截断');
eq(F._cmdPageSpecIndices('17', 0), { indices: [16] }, 'N = 原书页号优先匹配');
eq(F._cmdPageSpecIndices('2-8', 0).indices, [1,2,3,4,5,6,7], '清空 2-8 = 原书第 2~8 页');
eq(F._cmdPageSpecIndices('17-100', 0).indices, [16,17,18,19], 'N-M = 页号区间（触底截断）');
eq(F._cmdPageSpecIndices('3~5', 0).indices, [2,3,4], 'N~M 区间');
eq(F._cmdPageSpecIndices('5-3', 0).indices, [2,3,4], '反序区间自动交换');
eq(F._cmdPageSpecIndices('abc', 0), null, '非法范围 = null');
eq(F._cmdPageSpecIndices('99', 0).indices, [], '超出范围的页号 = 空');

if (failures.length) {
  console.log('FAIL');
  failures.forEach(function (f) { console.log(' - ' + f); });
  process.exit(1);
}
console.log('OK');
'''


class CleanRangeBehaviorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')

    def test_page_spec_parsing(self):
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
