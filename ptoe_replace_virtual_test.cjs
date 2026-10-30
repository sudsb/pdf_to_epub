'use strict';
// 替换规则「跨段虚拟文本路径」回归测试（jsdom）
//
// 覆盖 2026-10-30 修复：applyReplaceRule 对含 \n / \Z 等跨段写法的规则改走
// _rrApplyVirtual（整页虚拟文本匹配再映射回源码）。该路径原先直接按虚拟文本
// 坐标切源码，缺少 token 路径的实体/公式保护，会：
//   [1] 把 HTML 实体劈坏：<p>甲&#12288;乙</p> + (\d+)(?=;|\n|\Z) → 变成 &#;
//   [2] 改写 MathML 公式文本（<math>/<mi>/<mtext> 内的字符）
// 修复后：命中实体的匹配按 _regionsEntitySafe 原样保留且不计 replaced；
// 与公式区间相交的匹配直接跳过（seen 仍计数，便于提示「匹配均为…未替换」）。
//
// 说明：仓库内 ui_full.html 夹具可能过期，这里运行时从 correctmanage.py
// 提取最新 _UI_HTML（同 ptoe_proofread_pos_test.cjs）。
//
// 运行：node ptoe_replace_virtual_test.cjs

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const appJs = fs.readFileSync(path.join(__dirname, 'ui/app.js'), 'utf8');
const py = fs.readFileSync(path.join(__dirname, 'correctmanage.py'), 'utf8');
const m = py.match(/_UI_HTML\s*=\s*r?"""([\s\S]*?)"""/);
if (!m) { console.error('无法从 correctmanage.py 提取 _UI_HTML'); process.exit(1); }
const uiHtml = m[1];

const dom = new JSDOM(uiHtml, {
  url: 'http://localhost/',
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  beforeParse(window) {
    const jsonRes = (obj) => ({
      ok: true, status: 200,
      json: async () => obj,
      text: async () => JSON.stringify(obj),
      blob: async () => new Blob(),
    });
    window.fetch = async (url) => {
      const u = String(url);
      if (u.startsWith('/api/pages')) {
        return jsonRes({ pages: [{ page: 1, text: '<p>占位</p>', w: 800, h: 1200 }] });
      }
      if (u.startsWith('/api/')) return jsonRes({ ok: true });
      return jsonRes({});
    };
    const store = {};
    window.localStorage = {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
      clear: () => { Object.keys(store).forEach((k) => delete store[k]); },
      get length() { return Object.keys(store).length; },
      key: (i) => Object.keys(store)[i] || null,
    };
    window.navigator.sendBeacon = () => true;
    window.matchMedia = () => ({ matches: false, addListener: () => {}, removeListener: () => {} });
    window.requestAnimationFrame = (cb) => setTimeout(cb, 0);
    window.cancelAnimationFrame = (id) => clearTimeout(id);
    window.scrollTo = () => {};
  },
});

const w = dom.window;
w.eval(appJs + '\nwindow.__T = { apply: _rrApplyVirtual, compile: ptoeRegexCompile };');
const T = w.__T;

let pass = 0, fail = 0;
function A(label, got, want) {
  if (got === want) { pass++; console.log('  ok   ' + label); }
  else { fail++; console.log('  FAIL ' + label + '\n       got  ' + JSON.stringify(got) + '\n       want ' + JSON.stringify(want)); }
}
function apply(src, pattern, flags, repl) {
  return T.apply(src, T.compile(pattern, flags), new Set(), repl);
}

console.log('[1] 实体不被劈坏');
let r = apply('<p>甲&#12288;乙</p>', '(\\d+)(?=;|\\n|\\Z)', 'g', '');
A('源码保持不变', r.text, '<p>甲&#12288;乙</p>');
A('命中仍计数（用于提示）', r.seen, 1);
A('不计入 replaced', r.replaced, 0);

console.log('[2] 整段包含实体仍可替换');
r = apply('<p>a&#12288;(注)</p>', '(a&#12288;\\(注\\))(?=\\Z)', 'g', '');
A('整段删除', r.text, '<p></p>');
A('replaced=1', r.replaced, 1);

console.log('[3] MathML 公式内容不改写');
r = apply('<p><math><mtext>abc</mtext></math></p>', '(abc)(?=\\n|\\Z)', 'g', 'X');
A('公式保持原样', r.text, '<p><math><mtext>abc</mtext></math></p>');
A('replaced=0', r.replaced, 0);

console.log('[4] 公式外的文本正常替换');
r = apply('<p><math><mtext>abc</mtext></math> 123</p>', '(123)(?=\\n|\\Z)', 'g', 'X');
A('公式外替换', r.text, '<p><math><mtext>abc</mtext></math> X</p>');
A('replaced=1', r.replaced, 1);

console.log('[5] 段末数字跨段替换（\\n 路径）');
r = apply('<p>正文123</p><p>456</p>', '(\\d+)(?=;|\\n|\\Z)', 'g', '');
A('两段数字均删除', r.text, '<p>正文</p><p></p>');
A('replaced=2', r.replaced, 2);

console.log('[6] $1 展开');
r = apply('<p>甲乙\n丙</p>', '(乙)(?=\\n)', 'g', '[$1]');
A('组引用正确', r.text, '<p>甲[乙]\n丙</p>');

console.log('[7] <br> 视为行边界');
r = apply('<p>甲<br>乙</p>', '(甲)(?=\\n)', 'g', 'X');
A('段内换行前可替换', r.text, '<p>X<br>乙</p>');
A('replaced=1', r.replaced, 1);

console.log('[8] 跨标签之间的零宽匹配不改结构');
r = apply('<p>第一段</p><p>第二段</p>', '\\n+', 'g', '');
A('源码保持不变', r.text, '<p>第一段</p><p>第二段</p>');
A('不计入 replaced', r.replaced, 0);

console.log(fail ? ('\nFAILED: ' + fail + ' 项失败') : ('\n全部通过（' + pass + ' 项）'));
process.exit(fail ? 1 : 0);
