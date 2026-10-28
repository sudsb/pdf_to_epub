'use strict';
// 多行文本对齐（alignLines）real-DOM jsdom harness（2026-10-20，2026-10-28 重写口径）
// 覆盖：
//   1) 基准宽度 = 选中行里最长的**真实渲染宽度**（像素量测），不是字符数/列数；
//   2) 含空白行：左段贴左、右段贴右，差额补在**最后一个空白段**（用户 2026-10-28 确认）；
//   3) 最长行不额外拉伸（只把分隔空白换成等宽 &nbsp;）；
//   4) 无空白行：行尾补不可见空白到基准宽度；
//   5) 行内格式（strong/em/span）保留——旧实现整体覆写 innerHTML 会丢；
//   6) 幂等：重复对齐结果不变；
//   7) 标记类 ptoe-align-lines 落在块上（导出时豁免空白符清理）；
//   8) 量测不可用（客户区宽度全 0，如 jsdom/隐藏页）→ 回退列宽算法（中文 2 列/其余 1 列）。
// 运行：
//   python -c "import correctmanage,os;p=os.path.join(os.environ['TEMP'],'opencode','ui_current.html');os.makedirs(os.path.dirname(p),exist_ok=True);open(p,'w',encoding='utf-8').write(correctmanage._UI_HTML)"
//   node ptoe_alignlines_test.cjs
const fs = require('fs');
const path = require('path');
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
// HTML 会把可折叠空白串折成一个空格（&nbsp; 不折叠）——量测模型必须先折叠
function collapse(s) {
  return String(s == null ? '' : s).replace(/[ \t\n\r\f\v]+/g, ' ');
}
// 模型 A：1 列 = 7px、&nbsp; = 1 列（与列宽算法等价，用来验证两条路径口径一致）
function pxCols(s) {
  return cols(collapse(s)) * 7;
}
// 模型 B：Windows Microsoft YaHei 14px 实测口径（中文 14px、空格/&nbsp; 4px、数字/字母 8px、'.' 3px）
// ——旧实现按「列宽」折算就是在这里崩掉的（补 1 列只补到 7px，实际 nbsp 只有 4px）
function pxYahei(s) {
  let w = 0;
  for (const ch of collapse(s)) {
    const c = ch.codePointAt(0);
    if (c === 0x20 || c === 0x00a0) w += 4;
    else if (ch === '.') w += 3;
    else if (c < 0x80) w += 8;
    else w += 14;
  }
  return w;
}
let MEASURE = pxCols;
// >0 时：量测宽度超过该值视为「已折成两个视觉行」（Range 返回两个不同 top 的矩形）
let WRAP_OVER = 0;

// 页面：一个编辑区里放四组互不干扰的段落（选区分组，基准只按选中的块算）
const PAGES = [
  {
    page: 1, w: 800, h: 1200,
    text:
      // A 组（列宽模型 / 兼容旧断言）
      '<p>第一章 总则</p>' +
      '<p>第一节 总则的适用范围</p>' +
      '<p>第二节 附则</p>' +
      '<p><strong>第三条 术语</strong></p>' +
      '<p>无空格行</p>' +
      // B 组（真实字体口径：视觉等长）
      '<p>第一章 总则</p>' +
      '<p>第一节 总则的适用范围</p>' +
      '<p>第二节 附则</p>' +
      // C 组（多个空白段 → 只放大最后一段）
      '<p>1. 第四节    多空白段的末列对齐</p>' +
      '<p>1. 第五节  短</p>' +
      // D 组（无量测环境 → 列宽兜底）
      '<p>第一章 总则</p>' +
      '<p>第一节 总则的适用范围</p>' +
      '<p>第二节 附则</p>' +
      // E 组（已折成多行的块不参与对齐）
      '<p>很长很长的一行超级长的文字内容用来模拟折行</p>' +
      '<p>短行 目标</p>',
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
    // 布局引擎替身：Range 的客户区宽度按 MEASURE 模型算（jsdom 本身没有布局）
    const rectList = function (text) {
      const w = MEASURE(text);
      if (!w) return [];
      if (WRAP_OVER && w > WRAP_OVER) {
        return [
          { width: w / 2, height: 14, left: 0, top: 0, right: w / 2, bottom: 14 },
          { width: w / 2, height: 14, left: 0, top: 20, right: w / 2, bottom: 34 },
        ];
      }
      return [{ width: w, height: 14, left: 0, top: 0, right: w, bottom: 14 }];
    };
    window.Range.prototype.getClientRects = function () {
      return rectList(this.toString());
    };
    window.Range.prototype.getBoundingClientRect = function () {
      const w = MEASURE(this.toString());
      return { width: w, height: 14, left: 0, top: 0, right: w, bottom: 14 };
    };
    // canvas getContext 替身：量 &nbsp; 宽度用
    window.HTMLCanvasElement.prototype.getContext = function () {
      return { font: '', measureText: (s) => ({ width: MEASURE(s) }) };
    };
    window.__errors = [];
    window.addEventListener('error', (e) => { window.__errors.push(String(e.message)); });
  },
});
const w = dom.window;
const doc = w.document;

const EXPORTS = ['alignLines', '_textCols', '_blockTextNodes', '_replaceBlockRange', 'ALIGN_LINES_CLASS',
  '_alignLineInfo', '_lastWsRun', '_alignLineMetrics', '_blockTextSpan', '_nbspPx'];
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
// 一行按真实字体渲染后的宽度（px，含尾部 &nbsp; 填充）
function edge(block) {
  return MEASURE(collapse(visible(block)));
}

function run() {
  const align = X('alignLines');
  const textCols = X('_textCols');
  A(typeof align === 'function', 'alignLines 可用');
  A(typeof textCols === 'function', '_textCols 可用');
  A(typeof X('_alignLineMetrics') === 'function', '_alignLineMetrics 可用（像素量测入口）');
  if (!editableOf(0)) { A(false, 'init 挂载了第 1 页'); return; }
  A(w.__errors.length === 0, '加载期无 JS 错误: ' + JSON.stringify(w.__errors));

  const ed = editableOf(0);
  const bs = blocksOf(ed);
  EQ(bs.length, 15, '第 1 页 15 个块（五组用例）');

  // ---- 1) 列宽口径：CJK 2 列 ----
  EQ(textCols('第一章'), 6, '中文 2 字 = 6 列');
  EQ(textCols('abc'), 3, 'ASCII 3 字符 = 3 列');
  // 最后一个空白段解析
  EQ(X('_lastWsRun')('1. 第一节    总则').index, 6, '分界取最后一个空白段（不是第一个，第一个在 index 2）');
  EQ(X('_lastWsRun')('无空白'), null, '无空白行没有分界');

  // ==== A 组：列宽模型（量测口径与列宽口径一致） ====
  MEASURE = pxCols;
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
  A(t0.startsWith('第一章') && t1.startsWith('第一节') && t2.startsWith('第二节'), '左段靠左（同一列起）');

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

  // ==== B 组：真实字体口径（Microsoft YaHei 14px）—— 视觉等长 ====
  MEASURE = pxYahei;
  const b1 = bs.slice(5, 8);
  selectBlocks(b1);
  align(ed);
  const e1 = b1.map(edge);
  const nbCount = (visible(b1[0]).match(/\u00a0/g) || []).length;
  A(nbCount > 11, '补白按真实 &nbsp; 宽度算（' + nbCount + ' 个，旧算法只补 11 个）');
  A(Math.max.apply(null, e1) - Math.min.apply(null, e1) <= 4.01,
    '三行右侧对齐到像素级（' + e1.join(' / ') + ' px）');
  A(Math.abs(e1[1] - pxYahei('第一节 总则的适用范围')) < 0.01, '最长行宽度不变（' + e1[1] + ' px）');
  A(visible(b1[0]).startsWith('第一章') && visible(b1[2]).startsWith('第二节'), '左段仍贴左');
  // 导出线索：填充只能是 &nbsp;（半角空格会被折叠/清理）
  A(!/ /.test(visible(b1[0])) && !/ /.test(visible(b1[2])), '补白不含半角空格');

  // ==== C 组：多个空白段 → 只放大最后一个空白段 ====
  const c1 = bs.slice(8, 10);
  selectBlocks(c1);
  align(ed);
  const ce = c1.map(edge);
  A(Math.max.apply(null, ce) - Math.min.apply(null, ce) <= 4.01,
    '多空白段行也等长（' + ce.join(' / ') + ' px）');
  A(visible(c1[0]).indexOf('1. 第四节') === 0 && visible(c1[1]).indexOf('1. 第五节') === 0,
    '前缀空白段保持原样（补白落在最后一段）');
  A(/^1\. 第五节(\u00a0)+短$/.test(visible(c1[1])), '最后一列右对齐（页码/末列场景）');

  // ==== D 组：量测不可用 → 回退列宽算法 ====
  MEASURE = () => 0;
  const d1 = bs.slice(10, 13);
  selectBlocks(d1);
  align(ed);
  EQ(visible(d1[0]), '第一章' + NB.repeat(11) + '总则', '无量测环境回退列宽算法（第 1 行）');
  EQ(visible(d1[2]), '第二节' + NB.repeat(11) + '附则', '无量测环境回退列宽算法（第 3 行）');
  MEASURE = pxYahei;

  // ==== E 组：已折成多个视觉行的块不参与（没有单一右边缘可比） ====
  WRAP_OVER = 200;
  const e2 = bs.slice(13, 15);
  const beforeLong = visible(e2[0]);
  selectBlocks(e2);
  align(ed);
  EQ(visible(e2[0]), beforeLong, '折成两行的块不参与对齐（文本不变）');
  A(!e2[0].classList.contains('ptoe-align-lines'), '折行块不加对齐标记类');
  A(Math.abs(edge(e2[1]) - pxYahei('短行 目标')) <= 4.01, '同一选区里的单行仍按自身宽度收口');
  WRAP_OVER = 0;
  MEASURE = pxCols;
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
