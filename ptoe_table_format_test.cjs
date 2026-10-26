'use strict';
// 表格内格式回归测试（2026-10-21）
// 背景：表格单元格里的裸文本没有 p/div 包裹，_boundaryBlockInRange 只查
// p/div/h1-h6 会命中 .editable 或 null，_blocksBetween 返回 []，导致工具栏
// 所有格式化 + 清除格式在单元格内静默失效；清除格式若把 td 当普通块还会
// 删掉列宽/垂直对齐/合并属性。
// 用法（cwd = 仓库根）：
//   $env:NODE_PATH="D:\code-project\python\PToEA\node_modules"; node ptoe_table_format_test.cjs
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const REPO = __dirname;
const appJs = fs.readFileSync(path.join(REPO, 'ui/app.js'), 'utf8');

function extractUiHtml() {
  const cm = fs.readFileSync(path.join(REPO, 'correctmanage.py'), 'utf8');
  const m = cm.match(/_UI_HTML\s*=\s*r"""([\s\S]*?)"""/);
  if (!m) throw new Error('未找到 correctmanage._UI_HTML');
  return m[1];
}

let pass = 0, fail = 0;
const fails = [];
function A(cond, msg) {
  if (cond) { pass++; console.log('  ok   ' + msg); }
  else { fail++; fails.push(msg); console.log('  FAIL ' + msg); }
}

const dom = new JSDOM(extractUiHtml(), {
  url: 'http://localhost/',
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  beforeParse(window) {
    window.fetch = async () => ({
      ok: true, status: 200,
      json: async () => ({ pages: [], rules: [] }),
      text: async () => '{}',
    });
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
    window.__errors = [];
    window.addEventListener('error', (e) => { window.__errors.push(String(e.message)); });
  },
});
const w = dom.window;
const doc = w.document;
// jsdom 未实现 execCommand：remove/bold 路径在真实浏览器可用，这里补 no-op，
// 以便继续验证其后的 DOM 清理逻辑（单元格布局保护）。
doc.execCommand = function () { return false; };
doc.queryCommandState = function () { return false; };

try {
  w.eval(appJs + '\n;window.__T = {' +
    'applyOp: applyOp, applySingleFormat: applySingleFormat,' +
    'applyToSelectedBlocks: applyToSelectedBlocks,' +
    '_boundaryBlockInRange: _boundaryBlockInRange, _blocksBetween: _blocksBetween,' +
    '_cellHasBlockChild: _cellHasBlockChild, _paragraphCarrier: _paragraphCarrier,' +
    'currentEditable: currentEditable, syncContent: syncContent,' +
    '_mergeSelectedBlocks: _mergeSelectedBlocks,' +
    '_brushMergeBlocks: _brushMergeBlocks,' +
    'setPages: function (p) { pages = p; } };');
} catch (e) {
  console.error('[boot] FAILED:', e.stack || e.message);
  process.exit(1);
}
const T = w.__T;
const host = doc.getElementById('pages');

function mount(html) {
  const row = doc.createElement('div');
  row.className = 'page-row';
  row.dataset.i = '0';
  const ed = doc.createElement('div');
  ed.className = 'editable';
  ed.innerHTML = html;
  row.appendChild(ed);
  host.appendChild(row);
  return { row, ed };
}
function unmount(m) { if (m.row.parentNode) m.row.parentNode.removeChild(m.row); }
function selectText(node, start, end) {
  const r = doc.createRange();
  r.setStart(node, start);
  r.setEnd(node, end === undefined ? node.data.length : end);
  const s = w.getSelection();
  s.removeAllRanges();
  s.addRange(r);
  return r;
}
function textNodeIn(el) {
  const w2 = doc.createTreeWalker(el, w.NodeFilter.SHOW_TEXT, null);
  return w2.nextNode();
}

// ---------- 1. 块解析 ----------
console.log('\n[1] 单元格块解析');
let m = mount('<p>before</p><table><tbody><tr><td>单元格文字内容</td><td>第二格</td></tr></tbody></table><p>after</p>');
let td = m.ed.querySelector('td');
let tn = td.firstChild;
let b = T._boundaryBlockInRange(m.ed, tn, false);
A(b === td, '裸文本单元格：块解析为 td（旧实现为 null）');
let blocks = T._blocksBetween(m.ed, b, T._boundaryBlockInRange(m.ed, tn, true));
A(blocks.length === 1 && blocks[0] === td, '裸文本单元格：块集合为 [td]，不会收集整页');

m.ed.innerHTML = '<table><tbody><tr><td><p>段内文字</p></td><td>裸文本</td></tr></tbody></table>';
td = m.ed.querySelector('td');
let p = td.querySelector('p');
b = T._boundaryBlockInRange(m.ed, p.firstChild, false);
A(b === p, '单元格内已有 <p>：块解析为 <p>，不重复算单元格');
blocks = T._blocksBetween(m.ed, b, b);
A(blocks.length === 1 && blocks[0] === p, '单元格内 <p>：块集合只有 <p>');
let bareTd = m.ed.querySelectorAll('td')[1];
A(T._boundaryBlockInRange(m.ed, bareTd.firstChild, false) === bareTd, '同表内裸文本单元格仍可解析');
unmount(m);

// ---------- 2. 行内格式 ----------
console.log('\n[2] 单元格行内格式');
m = mount('<table><tbody><tr><td>单元格文字内容</td></tr></tbody></table>');
T.setPages([{ page: 1, text: m.ed.innerHTML }]);
td = m.ed.querySelector('td');
tn = td.firstChild;
selectText(tn, 2, 6);
T.applyOp('underline');
A(!!td.querySelector('span.ptoe-underline'), '下划线：单元格内生成 span.ptoe-underline');
A(td.querySelector('span.ptoe-underline').textContent === '格文字内', '下划线范围正确');

selectText(td.querySelector('span.ptoe-underline').firstChild, 0, 3);
T.applyOp('shade');
A(!!td.querySelector('span.ptoe-shade'), '底纹：单元格内生效');
unmount(m);

// ---------- 3. 清除格式保留表格布局 ----------
console.log('\n[3] 清除格式（单元格）');
m = mount('<table><tbody><tr><td class="ptoe-align-center ptoe-vtop" style="width:40%;vertical-align:top;color:red" colspan="2">' +
  '<span class="ptoe-underline" style="font-weight:700">单元格文字</span></td></tr></tbody></table>');
T.setPages([{ page: 1, text: m.ed.innerHTML }]);
td = m.ed.querySelector('td');
selectText(td.querySelector('span').firstChild, 0, 5);
T.applyOp('remove');
td = m.ed.querySelector('td');
A(!!td, '清除格式后 td 仍存在');
A(td.getAttribute('colspan') === '2', '保留 colspan');
A((td.getAttribute('style') || '').indexOf('width: 40%') >= 0 ||
  (td.getAttribute('style') || '').indexOf('width:40%') >= 0, '保留列宽 style');
A((td.getAttribute('style') || '').indexOf('vertical-align') >= 0, '保留垂直对齐 style');
A(!td.classList.contains('ptoe-align-center'), '清除文本对齐类');
A(td.classList.contains('ptoe-vtop'), '保留垂直对齐类');
A(!td.querySelector('span.ptoe-underline'), '解包行内下划线 span');
A((td.getAttribute('style') || '').indexOf('color') < 0, '清除文本颜色 style');
A(!td.querySelector('p'), '裸文本单元格不会被塞入 <p>（保持结构）');
unmount(m);

// ---------- 4. 段落级格式（载体 <p>） ----------
console.log('\n[4] 段落级格式（注释/缩进/标题/对齐）');
m = mount('<table><tbody><tr><td>单元格文字</td></tr></tbody></table>');
T.setPages([{ page: 1, text: m.ed.innerHTML }]);
td = m.ed.querySelector('td');
selectText(td.firstChild, 0, td.firstChild.data.length);
T.applyOp('note');
td = m.ed.querySelector('td');
A(!!td, '注释后 td 仍存在');
A(!!td.querySelector('p.ptoe-note'), '整格注释：落点包成 p.ptoe-note（保存不丢）');
A(td.querySelector('p.ptoe-note').textContent === '单元格文字', '注释载体保留文字');

selectText(td.querySelector('p.ptoe-note').firstChild, 0, 4);
T.applyOp('citation');
A(!!td.querySelector('span.ptoe-citation'), '部分选区引用：行内 span 生效');

m.ed.innerHTML = '<table><tbody><tr><td>缩进文字</td></tr></tbody></table>';
td = m.ed.querySelector('td');
selectText(td.firstChild, 0, td.firstChild.data.length);
T.applyOp('indent');
td = m.ed.querySelector('td');
A(!!td.querySelector('p.ptoe-indent'), '整格缩进：落到 p.ptoe-indent');
A(!!td, '缩进后 td 仍存在');

m.ed.innerHTML = '<table><tbody><tr><td>标题文字</td></tr></tbody></table>';
td = m.ed.querySelector('td');
selectText(td.firstChild, 0, td.firstChild.data.length);
T.applySingleFormat('heading2', m.ed);
td = m.ed.querySelector('td');
A(!!td && !!td.querySelector('h2'), '标题：单元格内生成 h2（td 不被替换）');
A(td.querySelector('h2').textContent === '标题文字', '标题文字保留');

m.ed.innerHTML = '<table><tbody><tr><td>居中文字</td></tr></tbody></table>';
td = m.ed.querySelector('td');
selectText(td.firstChild, 0, td.firstChild.data.length);
T.applyOp('align_center');
A(!!m.ed.querySelector('td.ptoe-align-center'), '居中：类落在 td 上（清洗后保留）');
unmount(m);

// ---------- 5. 合并保护 ----------
console.log('\n[5] 合并跨单元格保护');
m = mount('<table><tbody><tr><td>第一格</td><td>第二格</td></tr></tbody></table>');
T.setPages([{ page: 1, text: m.ed.innerHTML }]);
const cells = m.ed.querySelectorAll('td');
let r = doc.createRange();
r.setStart(cells[0].firstChild, 0);
r.setEnd(cells[1].firstChild, 3);
let sel = w.getSelection();
sel.removeAllRanges();
sel.addRange(r);
T._mergeSelectedBlocks(m.ed);
A(m.ed.querySelectorAll('td').length === 2, '跨单元格合并被拒绝：两个 td 仍在');
A(m.ed.querySelectorAll('td')[0].textContent === '第一格' &&
  m.ed.querySelectorAll('td')[1].textContent === '第二格', '跨单元格文字未被拼接');
unmount(m);

// ---------- 5b. 合并段落自动清理空白 ----------
console.log('\n[5b] 合并段落自动清理空白');
m = mount('<p>关于同意发表《克服教条主义，</p><p> 投入反右派斗争》一文的批语〔1〕</p>');
T.setPages([{ page: 1, text: m.ed.innerHTML }]);
let ps = m.ed.querySelectorAll('p');
r = doc.createRange();
r.setStart(ps[0].firstChild, 0);
r.setEnd(ps[1].firstChild, ps[1].firstChild.data.length);
sel = w.getSelection();
sel.removeAllRanges();
sel.addRange(r);
T._mergeSelectedBlocks(m.ed);
A(m.ed.querySelectorAll('p').length === 1, '合并后只剩一个段落');
A(m.ed.querySelector('p').textContent ===
  '关于同意发表《克服教条主义，投入反右派斗争》一文的批语〔1〕',
  '合并后中文间空格/软换行被清理（got ' + m.ed.querySelector('p').textContent + '）');
unmount(m);

m = mount('<p>Hello  </p><p> \n world</p>');
T.setPages([{ page: 1, text: m.ed.innerHTML }]);
ps = m.ed.querySelectorAll('p');
r = doc.createRange();
r.setStart(ps[0].firstChild, 0);
r.setEnd(ps[1].firstChild, ps[1].firstChild.data.length);
sel.removeAllRanges();
sel.addRange(r);
T._mergeSelectedBlocks(m.ed);
A(m.ed.querySelector('p').textContent === 'Hello world',
  '合并后英文单词间只保留一个空格（got ' + m.ed.querySelector('p').textContent + '）');
unmount(m);

m = mount('<p><strong>Hello</strong> </p><p>world</p>');
T.setPages([{ page: 1, text: m.ed.innerHTML }]);
ps = m.ed.querySelectorAll('p');
r = doc.createRange();
r.setStart(ps[0].firstChild.firstChild, 0);
r.setEnd(ps[1].firstChild, ps[1].firstChild.data.length);
sel.removeAllRanges();
sel.addRange(r);
T._mergeSelectedBlocks(m.ed);
A(!!m.ed.querySelector('p strong'), '合并清理空白不破坏行内格式（strong 仍在）');
A(m.ed.querySelector('p').textContent === 'Hello world',
  '带行内格式时合并结果正确（got ' + m.ed.querySelector('p').textContent + '）');
unmount(m);

m = mount('<p>第一段 </p><p> 续文</p>');
T.setPages([{ page: 1, text: m.ed.innerHTML }]);
ps = m.ed.querySelectorAll('p');
A(T._brushMergeBlocks(m.ed, ps[0]) === true, '格式刷合并返回成功');
A(m.ed.querySelectorAll('p').length === 1, '格式刷合并后只剩一个段落');
A(m.ed.querySelector('p').textContent === '第一段续文',
  '格式刷合并同样自动清理空白（got ' + m.ed.querySelector('p').textContent + '）');
unmount(m);

// ---------- 6. 无运行时错误 ----------
console.log('\n[6] 运行时');
A((w.__errors || []).length === 0, '无未捕获 JS 错误');

console.log('\n' + (fail === 0 ? 'PASS' : 'FAIL') + '  ' + pass + ' passed, ' + fail + ' failed');
if (fails.length) console.log(fails.map((f) => ' - ' + f).join('\n'));
process.exit(fail === 0 ? 0 : 1);
