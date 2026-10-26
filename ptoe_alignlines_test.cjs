'use strict';
// 多行文本对齐（alignLines）real-DOM jsdom harness（2026-10-20）
// 覆盖：
//   1) 基准宽度按**显示列宽**（CJK 占 2 列）算——按字符数补齐会让中文行左右错位；
//   2) 含空格的行：左段靠左、右段靠右（两端对齐），中间以 &nbsp; 填充；
//   3) 最长行不补齐（只把分隔空白统一成 &nbsp;，列宽不变）；
//   4) 无空格的行：右侧补齐到基准宽度；
//   5) 行内格式（strong/em/span）保留——旧实现整体覆写 innerHTML 会丢；
//   6) 幂等：重复对齐结果不变；
//   7) 标记类 ptoe-align-lines 落在块上（导出时豁免空白符清理）。
// 运行：node ptoe_alignlines_test.cjs
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { JSDOM } = require('jsdom');

const REPO = __dirname;
const APP_JS = process.env.PTOE_APP_JS || path.join(REPO, 'ui/app.js');
const appJs = fs.readFileSync(APP_JS, 'utf8');
const uiHtml = fs.readFileSync(process.env.PTOE_UI_HTML ||
  path.join(process.env.TEMP, 'opencode', 'ui_current.html'), 'utf8');

let pass = 0, fail = 0;
const fails = [];
function A(cond, msg) {
  if (cond) { pass++; console.log('  ok   ' + msg); }
  else { fail++; fails.push(msg); console.log('  FAIL ' + msg); }
}
function EQ(got, want, msg) {
  A(got === want, msg + '  (got ' + JSON.stringify(got) + ', want ' + JSON.stringify(want) + ')');
}

const NB = '\u00a0';
// 与前端同一套列宽口径（CJK/全角 2 列，其余 1 列）
function cols(s) {
  let w = 0;
  for (const ch of String(s)) {
    const c = ch.codePointAt(0);
    const wide = (c >= 0x1100 && c <= 0x115f) || (c >= 0x2e80 && c <= 0x303e) ||
      (c >= 0x3041 && c <= 0x33ff) || (c >= 0x3400 && c <= 0x4dbf) ||
      (c >= 0x4e00 && c <= 0x9fff) || (c >= 0xa000 && c <= 0xa4cf) ||
      (c >= 0xac00 && c <= 0xd7a3) || (c >= 0xf900 && c <= 0xfaff) ||
      (c >= 0xfe10 && c <= 0xfe19) || (c >= 0xfe30 && c <= 0xfe6f) ||
      (c >= 0xff00 && c <= 0xff60) || (c >= 0xffe0 && c <= 0xffe6) ||
      (c >= 0x20000 && c <= 0x2fffd) || (c >= 0x30000 && c <= 0x3fffd);
    w += wide ? 2 : 1;
  }
  return w;
}

const PAGES = [
  {
    page: 1, w: 800, h: 1200,
    text: '<p>第一章 总则</p>' +
      '<p>第一节 总则的适用范围</p>' +
      '<p>第二节 附则</p>' +
      '<p><strong>第三条 术语</strong></p>' +
      '<p>无空格行</p>',
  },
];

const dom = new JSDOM(uiHtml, {
  url: 'http://localhost/',
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  beforeParse(window) {
    window.fetch = async (url, opts) => {
      const u = new URL(url, 'http://localhost/');
      if (u.pathname === '/api/pages') {
        return { ok: true, status: 200,
          json: async () => ({ pages: PAGES }),
          text: async () => JSON.stringify({ pages: PAGES }) };
      }
      if (u.pathname === '/help.md') return { ok: true, text: async () => '# Help' };
      return { ok: true, status: 200, json: async () => ({ ok: true }), text: async () => '{}' };
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
    window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
    window.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0);
    window.cancelAnimationFrame = (id) => clearTimeout(id);
    window.document.execCommand = () => true;
    window.document.queryCommandState = () => false;
    window.__errors = [];
    window.addEventListener('error', (e) => { window.__errors.push(String(e.message)); });
  },
});
const w = dom.window;
const doc = w.document;

const EXPORTS = ['alignLines', '_textCols', '_blockTextNodes', '_replaceBlockRange', 'ALIGN_LINES_CLASS'];
try {
  w.eval(appJs + '\n;[' + EXPORTS.map((n) => 'typeof ' + n + ' === "undefined" ? null : ' + n).join(',') +
    '].forEach(function(v, i) { if (v !== null && v !== undefined) window["__x_" + ' + JSON.stringify(EXPORTS) + '[i]] = v; });');
} catch (e) {
  console.error('[boot] FAILED:', e.message);
  process.exit(1);
}
for (let i = 0; i < EXPORTS.length; i++) {
  const key = '__x_' + EXPORTS[i];
  if (w[key] === undefined || w[key] === null) continue;
  try { w[EXPORTS[i]] = w[key]; } catch (e) { /* const 不可写，忽略 */ }
}
const X = (n) => w['__x_' + n];

function editableOf(pageIdx) {
  const row = doc.querySelector('.page-row[data-i="' + pageIdx + '"]');
  return row ? row.querySelector('.editable') : null;
}
function blocksOf(ed) {
  return Array.prototype.slice.call(
    ed.querySelectorAll(':scope > p, :scope > div, :scope > h1, :scope > h2'));
}
// 块内可见文本（去掉块开标签后补的 \n 与首尾半角空白；尾部 &nbsp; 填充保留）
function visible(b) {
  return (b.textContent || '').replace(/^\s+/, '').replace(/[^\S\u00a0]+$/, '');
}

function selectBlocks(bs) {
  const r = doc.createRange();
  r.setStart(bs[0], 0);
  r.setEnd(bs[bs.length - 1], bs[bs.length - 1].childNodes.length);
  const sel = w.getSelection();
  sel.removeAllRanges();
  sel.addRange(r);
  return r;
}

function run() {
  const align = X('alignLines');
  const textCols = X('_textCols');
  A(typeof align === 'function', 'alignLines 可用');
  A(typeof textCols === 'function', '_textCols 可用');
  if (!editableOf(0)) { A(false, 'init 挂载了第 1 页'); return; }
  A(w.__errors.length === 0, '加载期无 JS 错误: ' + JSON.stringify(w.__errors));

  const ed = editableOf(0);
  const bs = blocksOf(ed);
  EQ(bs.length, 5, '第 1 页 5 个块');

  // ---- 1) 列宽口径：CJK 2 列 ----
  EQ(textCols('第一章'), 6, '中文 2 字 = 6 列');
  EQ(textCols('abc'), 3, 'ASCII 3 字符 = 3 列');

  // ---- 2) 三行对齐（含空格的两端对齐 + 最长行不补齐）----
  selectBlocks(bs.slice(0, 3));
  align(ed);
  const t0 = visible(bs[0]), t1 = visible(bs[1]), t2 = visible(bs[2]);
  EQ(t0, '第一章' + NB.repeat(11) + '总则', '第 1 行：左段靠左 + 中间 &nbsp; 填充');
  EQ(t1, '第一节' + NB + '总则的适用范围', '第 2 行（最长行）：不补齐');
  EQ(t2, '第二节' + NB.repeat(11) + '附则', '第 3 行：同基准宽度');
  A(cols(t0) === cols(t1) && cols(t1) === cols(t2),
    '三行列宽一致（' + cols(t0) + '/' + cols(t1) + '/' + cols(t2) + '）');
  A(/^第一章(\u00a0)+总则$/.test(t0), '填充用 &nbsp;（HTML 折叠不掉）');
  A(!/ /.test(t0) && !/ /.test(t1) && !/ /.test(t2), '段内不留半角空格（导出清理不会吃掉对齐）');
  // 左段靠左（同一列起）+ 右段靠右（右边缘同列）
  A(t0.startsWith('第一章') && t1.startsWith('第一节') && t2.startsWith('第二节'), '左段靠左（同一列起）');
  A(cols(t0) === cols(t1) && cols(t1) === cols(t2), '右段靠右（右边缘同列）');

  // ---- 3) 标记类 ----
  EQ(X('ALIGN_LINES_CLASS'), 'ptoe-align-lines', '标记类名常量');
  bs.slice(0, 3).forEach((b, i) => {
    A(b.classList.contains('ptoe-align-lines'), '第 ' + (i + 1) + ' 行带 ptoe-align-lines 标记类');
  });

  // ---- 4) 行内格式保留 ----
  const b3 = bs[3];
  A(b3.querySelector('strong') !== null, '加粗 span 仍在');
  selectBlocks([b3, bs[4]]);
  align(ed);
  const t3 = visible(b3), t4 = visible(bs[4]);
  EQ(t3, '第三条' + NB + '术语', '最长行只把分隔空白统一成 &nbsp;（列宽不变）');
  EQ(t4, '无空格行' + NB.repeat(3), '无空格行：右侧补齐到基准宽度');
  A(b3.querySelector('strong') !== null, '对齐后加粗 span 未被整体覆写丢掉');
  A(cols(t4) === cols(t3), '无空格行补齐后与基准同宽');

  // ---- 5) 幂等 ----
  const before = visible(bs[0]);
  selectBlocks(bs.slice(0, 3));
  align(ed);
  EQ(visible(bs[0]), before, '重复对齐结果不变（幂等）');
}

function waitReady(cb) {
  const t0 = Date.now();
  (function poll() {
    if (editableOf(0) || Date.now() - t0 > 15000) return cb();
    setTimeout(poll, 50);
  })();
}

waitReady(() => {
  try { run(); } catch (e) { fail++; fails.push('异常: ' + e.message); console.log('  FAIL 异常: ' + e.message); }
  console.log('\n通过 ' + pass + ' / 失败 ' + fail);
  if (fail) { console.log('失败项：\n  - ' + fails.join('\n  - ')); }
  try { w.close(); } catch (e) { /* ignore */ }
  process.exit(fail ? 1 : 0);
});
