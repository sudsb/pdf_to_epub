# -*- coding: utf-8 -*-
"""快捷键绑定 / 设置持久化回归护栏（2026-10）。

覆盖的不变量（括号内是本次修复的缺陷）：
  S1 comboOf 的键名归一必须与 DEFAULTS 的写法一致——否则默认快捷键静默失效
     （此前方向键被归一成 ArrowUp 而 DEFAULTS 写 Up，三个对齐快捷键全是死键）。
  S2 绑定写入必须走 assignBinding：同一组合键只能属于一个操作，且要顶掉旧操作
     （否则 reverseBindings 后写者胜，旧操作静默失效、表格两行显示同一个键）。
  S3 历史绑定值 'Ctrl+Shift+ArrowUp' 必须迁移成 'Ctrl+Shift+Up'。
  S4 图片布局模式值域前后端一致：后端 POST 校验只认 3 种、前端 5 种时，选「绕排」
     会让整批 ui_settings 被 400 拒绝 → 所有界面设置都不落盘。
  S5 设置保存失败必须让用户看见（此前 fire-and-forget，完全静默）。
  S6 格式规则快捷键必须走 bindings 体系（此前硬编码监听，捕获时会被同时触发）。
  S7 符号映射表解析按「源」去重（后端拒绝重复源，重复即整批 400）。
  S8 重做按钮的 tooltip 不得宣传已被「回退原文」占用的 Ctrl+Shift+Z。

静态断言永远执行；行为断言（把 ui/app.js 的真函数抽进 Node vm 跑）在有 node 时执行。
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_JS = os.path.join(ROOT, 'ui', 'app.js')
CM_PY = os.path.join(ROOT, 'correctmanage.py')


def read(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def defaults_map(src):
    """从 app.js 抽出 DEFAULTS 对象里的字符串键值（op -> 组合键）。"""
    m = re.search(r'const DEFAULTS = \{(.*?)\n\};', src, re.S)
    assert m, '找不到 DEFAULTS'
    return dict(re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*'([^']*)'", m.group(1)))


def node_bin():
    return shutil.which('node')


def function_body(src, name):
    """截取 `function name(` 起、到下一个顶层 `function ` 为止的源码（粗粒度，够静态断言用）。"""
    i = src.index('function %s(' % name)
    j = src.find('\nfunction ', i + 1)
    return src[i:] if j < 0 else src[i:j]


HARNESS = r'''
const fs = require('fs'), vm = require('vm');
const app = fs.readFileSync(process.argv[2], 'utf8');
const defaults = JSON.parse(process.argv[3]);

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
function extractDecl(re) {
  const m = app.match(re);
  if (!m) throw new Error('找不到声明: ' + re);
  return m[0];
}

const failures = [];
function ok(cond, msg) { if (!cond) failures.push(msg); }

const script = [
  extractDecl(/const SHIFT_DIGIT_SYMBOL = \{[^\n]*\};/),
  extractDecl(/const KEY_ALIAS = \{[^\n]*\};/),
  'let bindings = {};',
  'function setBindings(o) { bindings = o; }',
  extract('comboOf'),
  extract('normalizeCombo'),
  extract('assignBinding'),
  extract('reverseBindings'),
  extract('parseSymbolMap'),
  'module.exports = { comboOf, normalizeCombo, assignBinding, reverseBindings, parseSymbolMap, setBindings };'
].join('\n');

const mod = { exports: {} };
vm.runInNewContext(script, { module: mod, console });
const F = mod.exports;

// ---- S1：每个默认组合键都必须能被 comboOf 重新生成 ----
const KEY_EVENT = function (k) {
  // 组合键里的键名 → 真实 KeyboardEvent 的 key/code
  const map = { Up: 'ArrowUp', Down: 'ArrowDown', Left: 'ArrowLeft', Right: 'ArrowRight' };
  const ev = { ctrlKey: false, altKey: false, shiftKey: false, key: k, code: '' };
  if (map[k]) ev.key = map[k];
  else if (/^[A-Z]$/.test(k)) ev.key = k.toLowerCase();
  else if (/^[0-9]$/.test(k)) { ev.key = k; ev.code = 'Digit' + k; }
  else if (k === 'Space') ev.key = ' ';
  return ev;
};
for (const op in defaults) {
  const combo = defaults[op];
  if (!combo) continue;
  const parts = combo.split('+');
  const key = parts[parts.length - 1];
  const ev = KEY_EVENT(key);
  ev.ctrlKey = parts.indexOf('Ctrl') >= 0;
  ev.altKey = parts.indexOf('Alt') >= 0;
  ev.shiftKey = parts.indexOf('Shift') >= 0;
  const got = F.comboOf(ev);
  ok(got === combo, 'S1 默认快捷键无法触发：' + op + ' 期望 ' + combo + '，comboOf 实际返回 ' + got);
}

// ---- S3：历史方向键写法迁移 ----
ok(F.normalizeCombo('Ctrl+Shift+ArrowUp') === 'Ctrl+Shift+Up', 'S3 normalizeCombo 未把 ArrowUp 迁移为 Up');
ok(F.normalizeCombo('Ctrl+Shift+Left') === 'Ctrl+Shift+Left', 'S3 normalizeCombo 误改已是短名的组合键');
ok(F.normalizeCombo('') === '', 'S3 normalizeCombo 空值应原样返回');

// ---- S2：同键冲突必须顶掉旧操作并报告 ----
F.setBindings({ bold: 'Ctrl+B', proofread_correct: 'Ctrl+K', italic: '' });
const displaced = F.assignBinding('bold', 'Ctrl+K');
ok(displaced === 'proofread_correct', 'S2 冲突时应返回被顶掉的操作，实际 ' + displaced);
const rev = F.reverseBindings();
ok(rev['Ctrl+K'] === 'bold', 'S2 冲突后新操作应独占该组合键');
ok(!rev['Ctrl+B'], 'S2 旧组合键应被清空');
F.setBindings({ bold: 'Ctrl+B' });
ok(F.assignBinding('bold', 'Ctrl+B') === '', 'S2 绑定自身已有组合键不应算冲突');

// ---- S7：符号映射表按源去重（后者覆盖前者）----
const pairs = F.parseSymbolMap('a → b\na → c\nx->y\n\n');
ok(JSON.stringify(pairs) === JSON.stringify([['a', 'c'], ['x', 'y']]),
   'S7 重复源未去重（后端不接受重复源）：' + JSON.stringify(pairs));

if (failures.length) {
  console.log('FAIL');
  failures.forEach(function (f) { console.log(' - ' + f); });
  process.exit(1);
}
console.log('OK');
'''


class ShortcutBehaviorTest(unittest.TestCase):
    """行为断言：把 ui/app.js 的真函数抽进 Node vm 跑。"""

    @classmethod
    def setUpClass(cls):
        cls.node = node_bin()

    def test_shortcut_runtime_invariants(self):
        if not self.node:
            self.skipTest('未安装 node，跳过行为测试')
        src = read(APP_JS)
        defaults = defaults_map(src)
        with tempfile.TemporaryDirectory() as td:
            script = os.path.join(td, 'harness.js')
            with open(script, 'w', encoding='utf-8') as f:
                f.write(textwrap.dedent(HARNESS))
            r = subprocess.run([self.node, script, APP_JS, json.dumps(defaults)],
                               capture_output=True, text=True,
                               encoding='utf-8', errors='replace')
        out = (r.stdout or '') + (r.stderr or '')
        self.assertEqual(r.returncode, 0, '快捷键行为断言未通过：\n%s' % out)
        self.assertIn('OK', out)


class ShortcutStaticTest(unittest.TestCase):
    """静态不变量：关键接线不许回退。"""

    @classmethod
    def setUpClass(cls):
        cls.src = read(APP_JS)
        cls.cm = read(CM_PY)

    # S1：DEFAULTS 里不得出现 comboOf 生成不出来的写法
    def test_defaults_use_canonical_key_names(self):
        for op, combo in defaults_map(self.src).items():
            if not combo:
                continue
            key = combo.split('+')[-1]
            self.assertNotIn(key, ('ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'),
                             'DEFAULTS 里 %s 用了 Arrow* 写法，comboOf 已归一为 Up/Down/…' % op)

    # S2：所有绑定写入点都必须经过 assignBinding（除清空/侧键下拉）
    def test_binding_writes_go_through_assign_binding(self):
        self.assertIn('function assignBinding(op, combo)', self.src)
        # 捕获分支：不再直接写 bindings[capturingOp] = combo
        self.assertNotIn('bindings[capturingOp] = combo', self.src,
                         '键盘捕获必须走 assignBinding（否则同键冲突静默覆盖）')
        self.assertNotIn('bindings[capturingOp] = sb.name', self.src,
                         '侧键捕获必须走 assignBinding')

    # S2b：捕获期间点别处要退出捕获（否则后续按键被吞掉）
    def test_capture_cancelled_on_outside_click(self):
        self.assertIn("closest('#shortcutTable')", self.src,
                      '点击快捷键表以外区域应取消键盘捕获')

    # S4：图片布局模式前后端值域一致
    def test_img_mode_domain_matches_backend(self):
        m = re.search(r"const IMG_MODES = \[([^\]]*)\];", self.src)
        self.assertIsNotNone(m, '找不到 IMG_MODES')
        front = set(re.findall(r"'([^']*)'", m.group(1)))
        import correctmanage
        back = set(getattr(correctmanage, 'IMG_MODE_CHOICES', ()))
        self.assertTrue(back, 'correctmanage 缺少 IMG_MODE_CHOICES（前后端值域必须来自同一处）')
        self.assertTrue(front <= back,
                        '前端可写入后端不接受的图片模式 → 整批 ui_settings 会 400：%s' % (front - back))

    # S5：保存失败要可见
    def test_settings_save_reports_server_error(self):
        for fn in ('saveUiSettings', 'saveBindings', 'saveMouseBindings'):
            body = function_body(self.src, fn)
            self.assertIn('_postJson(', body,
                          '%s 必须走统一 POST 助手（不能 fire-and-forget 静默失败）' % fn)
        helper = function_body(self.src, '_postJson')
        self.assertIn('res.ok', helper, '_postJson 必须检查 HTTP 状态码')
        self.assertIn('_reportPersistFailure', helper, '服务端拒绝时必须提示用户')
        self.assertIn('showToast', function_body(self.src, '_reportPersistFailure'),
                      '保存失败必须冒到界面上')

    # S6：格式规则快捷键纳入 bindings
    def test_format_rules_is_a_bindable_op(self):
        self.assertRegex(self.src, r"format_rules\s*:\s*'Ctrl\+Shift\+Q'",
                         'DEFAULTS 缺少 format_rules')
        self.assertIn("['format_rules'", self.src, 'OPS 缺少 format_rules 行')
        self.assertIn('format_rules: openFormatRulesModal', self.src,
                      'SHORTCUT_ACTIONS 缺少 format_rules')
        self.assertNotIn("e.key === 'Q'", self.src,
                         '不得再用硬编码 keydown 监听抢 Ctrl+Shift+Q（捕获时会双触发）')

    # S9：弹窗打开时不派发会改动正文的快捷键（与侧键分支同一守卫）
    def test_shortcuts_blocked_while_modal_open(self):
        self.assertIn('const MODAL_SAFE_OPS', self.src, '缺少弹窗白名单常量')
        self.assertIn('_multiSelAnyModalOpen() && MODAL_SAFE_OPS.indexOf(op) === -1', self.src,
                      '统一快捷键分发缺少弹窗守卫（会在弹窗里改到背后正文）')

    # S8：重做 tooltip 不得宣传 Ctrl+Shift+Z（已被「回退原文」占用）
    def test_redo_tooltip_matches_real_binding(self):
        m = re.search(r'id="redoBtn"[^>]*title="([^"]*)"', self.cm)
        self.assertIsNotNone(m)
        title = m.group(1)
        self.assertNotIn('Ctrl+Shift+Z', title,
                         'Ctrl+Shift+Z 已绑给「回退原文」，tooltip 不得再宣传为重做')


if __name__ == '__main__':
    unittest.main(verbosity=2)
