'use strict';
// 表格增强（2026-10）real-DOM jsdom harness
// 用法（cwd = 仓库根）：
//   $env:NODE_PATH="D:\code-project\python\PToEA\node_modules"; node ptoe_table_test.cjs
// 说明：jsdom 无布局引擎，故只验证「结构/尺寸数据」逻辑（grid/合并/拆分/插入/移动/
// 表头/列宽行高/均分/重置/垂直对齐/菜单项），拖拽与浮层几何无法在此断言（靠 CSS 静态断言）。
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const REPO = __dirname;
const appJs = fs.readFileSync(path.join(REPO, 'ui/app.js'), 'utf8');

function extractUiHtml() {
  if (process.env.PTOE_UI_HTML) {
    return fs.readFileSync(process.env.PTOE_UI_HTML, 'utf8');
  }
  const cm = fs.readFileSync(path.join(REPO, 'correctmanage.py'), 'utf8');
  const m = cm.match(/_UI_HTML\s*=\s*r"""([\s\S]*?)"""/);
  if (!m) throw new Error('未找到 correctmanage._UI_HTML');
  return m[1];
}
const uiHtml = extractUiHtml();

let pass = 0, fail = 0;
const fails = [];
function A(cond, msg) {
  if (cond) { pass++; console.log('  ok   ' + msg); }
  else { fail++; fails.push(msg); console.log('  FAIL ' + msg); }
}

const dom = new JSDOM(uiHtml, {
  url: 'http://localhost/',
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  beforeParse(window) {
    window.fetch = async (url) => {
      const u = new URL(url, 'http://localhost/');
      if (u.pathname === '/api/pages') return { ok: true, status: 200, json: async () => ({ pages: [] }), text: async () => '{"pages":[]}' };
      if (u.pathname === '/api/format_rules') return { ok: true, status: 200, json: async () => ({ rules: [] }), text: async () => '{}' };
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
    window.__errors = [];
    window.addEventListener('error', (e) => { window.__errors.push(String(e.message)); });
  },
});
const w = dom.window;
const doc = w.document;

// 暴露内部函数（顶层 function 声明本已是 window 属性；此处统一经 __T 取用，
// 并暴露 _tblKey/_tblSel 这类 let 变量的读写器，jsdom 无法直接碰词法全局）
try {
  w.eval(appJs + '\n;window.__T = {' +
    'grid: _tblGrid, cellPos: _tblCellPos, spanOf: _tblSpanOf, gridWidth: _tblGridWidth,' +
    'gridWidthAll: _tblGridWidthAll, repair: _tblRepairCells,' +
    'snapshotColPcts: _tblSnapshotColPcts, applyColPcts: _tblApplyColPcts, rowHeights: _tblRowHeightsPx,' +
    'merge: _tblMerge, split: _tblSplit, splitCell: _tblSplitCell,' +
    'insertRow: _tblInsertRow, delRow: _tblDelRow, moveRow: _tblMoveRow,' +
    'insertCol: _tblInsertCol, delCol: _tblDelCol, moveCol: _tblMoveCol,' +
    'toggleHeader: _tblToggleHeader, align: _tblAlign,' +
    'setColW: _tblSetColWidthPct, setRowH: _tblSetRowHeight, distribute: _tblDistributeCols,' +
    'resetSizes: _tblResetSizes, vAlign: _tblVAlign, ensureFixed: _tblEnsureFixed,' +
    'menuOpen: _tblMenuOpen, menuItems: TBL_MENU_ITEMS, buildTableHtml: buildTableHtml,' +
    'toggleNoborder: _tblToggleNoborder, hasNoborder: _tblHasNoborder,' +
    'ensureAlignGrid: _ensureTblAlignGrid, alignGridOpen: _tblAlignGridOpen,' +
    'alignGridClose: _tblAlignGridClose, alignGridActive: _tblSyncAlignGridActive,' +
    'targetCells: _tblTargetCells, vAlignOf: _tblVAlignOf,' +
    'buildColumnsTable: buildColumnsTable, tableToParagraphs: _tblTableToParagraphs,' +
    'touchesTable: _selectionTouchesTable,' +
    'syncGrow: _syncTblGrow, trsOf: _tblTrsOf,' +
    'showBar: showTblBar, hideBar: hideTblBar, reposBar: _tblRepositionBar,' +
    'hideLayer: _tblHideResizeLayer, showLayer: _tblShowResizeLayer,' +
    'layer: function () { return _tblResizeLayer; },' +
    'layerVisible: function () { return !!(_tblResizeLayer && _tblResizeLayer.style.display === "block"); },' +
    'barVisible: function () { return _tblBarVisible; },' +
    'setKey: function (k) { _tblKey = k; }, getKey: function () { return _tblKey; },' +
    'setSel: function (s) { _tblSel = s; }, getSel: function () { return _tblSel; },' +
    'setPages: function (p) { pages = p; },' +
    'isMd: function () { return mdMode; } };');
  console.log('[boot] app.js evaluated');
} catch (e) {
  console.error('[boot] FAILED:', e.stack || e.message);
  process.exit(1);
}
const T = w.__T;
const host = doc.getElementById('pages');

// ---- 构造一个挂在 #pages 下的可编辑页，内含给定 table ----
function mountTable(tableHtml, i) {
  i = i || 0;
  const row = doc.createElement('div');
  row.className = 'page-row';
  row.dataset.i = String(i);
  const ed = doc.createElement('div');
  ed.className = 'editable';
  ed.innerHTML = tableHtml;
  row.appendChild(ed);
  host.appendChild(row);
  const table = ed.querySelector('table');
  return { row: row, ed: ed, table: table, i: i };
}
function unmount(m) { if (m && m.row && m.row.parentNode) m.row.parentNode.removeChild(m.row); }
function key(m, cellRef) {
  // cellRef: CSS 选择器（相对 table）或元素
  const cell = typeof cellRef === 'string' ? m.table.querySelector(cellRef) : cellRef;
  const k = { i: m.i, cell: cell, table: m.table, ed: m.ed };
  T.setKey(k);
  return k;
}
function cellCount(table) { return table.querySelectorAll('td,th').length; }
function rowCellCounts(table) {
  return Array.from(table.querySelectorAll('tr')).map((tr) => tr.children.length);
}

setTimeout(run, 60);

function run() {
  A(w.__errors.length === 0, '加载期无 JS 错误: ' + JSON.stringify(w.__errors));

  // ---------- 0. 静态契约（HTML / CSS 落位） ----------
  console.log('\n== 0. 内嵌 HTML/CSS 契约 ==');
  A(!!doc.getElementById('tblBar'), '#tblBar 存在');
  A(!!doc.getElementById('tblMenu'), '#tblMenu 存在');
  A(!!doc.getElementById('tblResizeLayer'), '#tblResizeLayer 存在');
  A(!!doc.getElementById('tblSizeModalBg'), '#tblSizeModalBg 存在');
  A(!!doc.getElementById('tblSizeColWidth') && !!doc.getElementById('tblSizeRowHeight') && !!doc.getElementById('tblSizeRowUnit'),
    '尺寸弹窗输入控件存在');
  const barOps = Array.from(doc.querySelectorAll('#tblBar button')).map((b) => b.dataset.tblOp);
  ['addrow', 'addcol', 'merge', 'split', 'colwidth', 'rowheight', 'alignleft', 'aligncenter', 'alignright', 'aligngrid', 'more', 'deltable']
    .forEach((op) => A(barOps.indexOf(op) >= 0, '#tblBar 含按钮 ' + op));
  A(!/\.page-row\.ptoe-has-table\s+\.editable\s*\{/.test(uiHtml),
    'CSS：含表格的行不再撑高（页高与图片等高、内容内部滚动；旧 height:auto 规则已移除）');
  A(/#tblAlignGrid\s*\{[^}]*position:\s*fixed/.test(uiHtml), 'CSS：#tblAlignGrid 固定定位');
  A(/\.editable\s*>\s*table\s+tr\s*\{[^}]*counter-increment:\s*ptoe-ln/.test(uiHtml),
    'CSS：表格行参与行号计数（tr counter-increment）');
  A(/#tblResizeLayer\s*\{[^}]*position:\s*fixed/.test(uiHtml), 'CSS：#tblResizeLayer 固定定位');

  // ---------- 1. 网格化 ----------
  console.log('\n== 1. 网格化（colspan/rowspan）==');
  const m1 = mountTable(
    '<table><tbody>' +
    '<tr><td colspan="2">A</td><td>B</td></tr>' +
    '<tr><td rowspan="2">C</td><td>D</td><td>E</td></tr>' +
    '<tr><td>F</td><td>G</td></tr>' +
    '</tbody></table>', 0);
  A(!!m1.table, '表格挂载成功');
  A(T.gridWidth(T.grid(m1.table)) === 3, 'grid 宽度为 3（got ' + T.gridWidth(T.grid(m1.table)) + '）');
  const g1 = T.grid(m1.table);
  const cellA = m1.table.querySelector('td');
  A(T.cellPos(g1, cellA).rs === 1 && T.cellPos(g1, cellA).cs === 2, 'A 的 span = 1x2');
  const cellC = m1.table.querySelectorAll('tr')[1].querySelector('td');
  A(T.cellPos(g1, cellC).rs === 2 && T.cellPos(g1, cellC).cs === 1, 'C 的 span = 2x1');
  A(T.cellPos(g1, cellC).r === 1 && T.cellPos(g1, cellC).c === 0, 'C 位于 grid (1,0)');
  unmount(m1);

  // ---------- 2. 合并 / 拆分 ----------
  console.log('\n== 2. 合并 / 拆分 ==');
  const m2 = mountTable('<table><tbody><tr><td>a</td><td>b</td><td>c</td></tr><tr><td>d</td><td>e</td><td>f</td></tr><tr><td>g</td><td>h</td><td>i</td></tr></tbody></table>', 0);
  key(m2, 'td');
  T.setSel({ table: m2.table, anchor: { r: 0, c: 0 }, focus: { r: 1, c: 1 } });
  T.merge();
  const merged = m2.table.querySelector('td');
  A(merged.getAttribute('colspan') === '2', '合并后左上格 colspan=2（got ' + merged.getAttribute('colspan') + '）');
  A(merged.getAttribute('rowspan') === '2', '合并后左上格 rowspan=2（got ' + merged.getAttribute('rowspan') + '）');
  A(JSON.stringify(rowCellCounts(m2.table)) === JSON.stringify([2, 1, 3]),
    '合并后各行子节点数 [2,1,3]（左上格 colspan=2，got ' + JSON.stringify(rowCellCounts(m2.table)) + '）');
  A(T.gridWidth(T.grid(m2.table)) === 3, '合并后网格宽度仍为 3');
  A(/a/.test(merged.textContent) && /b/.test(merged.textContent) && /d/.test(merged.textContent) && /e/.test(merged.textContent),
    '被并入的文本 a/b/d/e 都保留在左上格');
  A(cellCount(m2.table) === 6, '合并后剩余 6 个单元格（got ' + cellCount(m2.table) + '）');
  // 拆分
  key(m2, merged);
  T.setSel(null);
  T.split();
  A(!merged.getAttribute('colspan') && !merged.getAttribute('rowspan'), '拆分后左上格 span 属性被移除');
  A(cellCount(m2.table) === 9, '拆分后恢复 9 个单元格（got ' + cellCount(m2.table) + '）');
  A(JSON.stringify(rowCellCounts(m2.table)) === JSON.stringify([3, 3, 3]), '拆分后每行 3 格');
  unmount(m2);

  // ---------- 3. 插入 / 删除 / 移动 行 ----------
  console.log('\n== 3. 行操作 ==');
  const m3 = mountTable('<table><tbody><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></tbody></table>', 0);
  key(m3, m3.table.querySelectorAll('tr')[0].querySelector('td'));
  T.insertRow('below');
  A(T.trsOf(m3.table).length === 3, '下方插入行后共 3 行（got ' + T.trsOf(m3.table).length + '）');
  A(m3.table.querySelectorAll('tr')[1].querySelectorAll('td').length === 2, '新行为 2 格');
  T.insertRow('above');
  A(T.trsOf(m3.table).length === 4, '上方插入行后共 4 行');
  T.delRow();
  A(T.trsOf(m3.table).length === 3, '删除行后共 3 行');
  unmount(m3);

  const m3b = mountTable('<table><tbody><tr><td>r1</td><td>x</td></tr><tr><td>r2</td><td>y</td></tr></tbody></table>', 0);
  const tr1 = T.trsOf(m3b.table)[0];
  key(m3b, tr1.querySelector('td'));
  T.moveRow(1);
  A(T.trsOf(m3b.table)[0].textContent.indexOf('r2') >= 0 && T.trsOf(m3b.table)[1].textContent.indexOf('r1') >= 0,
    '下移本行后 r2 在 r1 之前');
  T.moveRow(-1);
  A(T.trsOf(m3b.table)[0].textContent.indexOf('r1') >= 0, '上移本行还原顺序');
  T.moveRow(-1);
  A(T.trsOf(m3b.table)[0].textContent.indexOf('r1') >= 0, '已在最上行时上移为空操作（不越界）');
  unmount(m3b);

  // ---------- 4. 插入 / 删除 / 移动 列 ----------
  console.log('\n== 4. 列操作 ==');
  const m4 = mountTable('<table><tbody><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></tbody></table>', 0);
  key(m4, m4.table.querySelectorAll('tr')[0].querySelectorAll('td')[0]);
  T.insertCol('right');
  A(rowCellCounts(m4.table).join(',') === '3,3', '右侧插入列后每行 3 格（got ' + rowCellCounts(m4.table).join(',') + '）');
  T.insertCol('left');
  A(rowCellCounts(m4.table).join(',') === '4,4', '左侧插入列后每行 4 格');
  T.delCol();
  A(rowCellCounts(m4.table).join(',') === '3,3', '删除列后每行 3 格');
  unmount(m4);

  const m4b = mountTable('<table><tbody><tr><td>a</td><td>b</td><td>c</td></tr></tbody></table>', 0);
  key(m4b, m4b.table.querySelectorAll('td')[0]);
  T.moveCol(1);
  A(m4b.table.querySelectorAll('tr')[0].children[0].textContent === 'b' &&
    m4b.table.querySelectorAll('tr')[0].children[1].textContent === 'a', '右移本列后 b 在 a 之前');
  T.moveCol(-1);
  A(m4b.table.querySelectorAll('tr')[0].children[0].textContent === 'a', '左移本列还原');
  unmount(m4b);

  // ---------- 5. 表头切换 ----------
  console.log('\n== 5. 表头切换 ==');
  const m5 = mountTable('<table><tbody><tr><td>H1</td><td>H2</td></tr><tr><td>a</td><td>b</td></tr></tbody></table>', 0);
  key(m5, m5.table.querySelector('td'));
  T.toggleHeader();
  A(!!m5.table.querySelector('thead th'), '首行升为 thead/th');
  A(m5.table.querySelectorAll('thead th').length === 2, 'thead 含 2 个 th');
  const th0 = m5.table.querySelector('thead th');
  key(m5, th0);
  T.toggleHeader();
  A(!m5.table.querySelector('thead'), '再次切换后 thead 被移除');
  A(!m5.table.querySelector('th'), 'th 全部还原为 td');
  A(m5.table.textContent.indexOf('H1') >= 0 && m5.table.textContent.indexOf('H2') >= 0, '表头文本保留');
  unmount(m5);

  // ---------- 6. 尺寸：列宽 / 行高 / 均分 / 重置 ----------
  console.log('\n== 6. 尺寸 ==');
  const m6 = mountTable('<table><tbody><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></tbody></table>', 0);
  key(m6, m6.table.querySelector('td'));
  A(T.setColW(m6.table, 0, 30) === true, '设置第 0 列列宽=30% 成功');
  A(m6.table.querySelectorAll('tr')[0].cells[0].style.width === '30%', '首行第 0 列 style.width=30%');
  A(m6.table.querySelectorAll('tr')[1].cells[0].style.width === '30%', '第 1 行同列同步 30%');
  A(/table-layout:\s*fixed/.test(m6.table.getAttribute('style') || ''), '设置列宽后表格落 table-layout:fixed');
  A(T.setRowH(m6.table, 0, 2.5, 'em') === true, '设置第 0 行行高=2.5em 成功');
  A(m6.table.querySelectorAll('tr')[0].cells[0].style.height === '2.5em', '第 0 行格 style.height=2.5em');
  A(T.setColW(m6.table, 0, 999) === true, '越界列宽被钳制（不抛错）');
  A(/^100%$/.test(m6.table.querySelectorAll('tr')[0].cells[0].style.width), '列宽上限钳到 100%');
  T.distribute(m6.table);
  const sum = Array.from(m6.table.querySelectorAll('tr')[0].cells).reduce((s, c) => s + parseFloat(c.style.width), 0);
  A(Math.abs(sum - 100) < 0.6, '均分列宽后各列宽度合计≈100%（got ' + sum + '）');
  T.resetSizes(m6.table);
  A(!/table-layout/.test(m6.table.getAttribute('style') || ''), '重置尺寸后清除 table-layout:fixed');
  A(!m6.table.querySelector('td').getAttribute('style'), '重置尺寸后单元格无残留 style');
  unmount(m6);

  // ---------- 7. 垂直对齐 / 水平对齐 ----------
  console.log('\n== 7. 对齐 ==');
  const m7 = mountTable('<table><tbody><tr><td>a</td><td>b</td></tr></tbody></table>', 0);
  const c7 = m7.table.querySelector('td');
  key(m7, c7);
  T.vAlign('middle');
  A(c7.classList.contains('ptoe-vmid'), '垂直居中 → ptoe-vmid');
  T.vAlign('middle');
  A(!c7.classList.contains('ptoe-vmid'), '再次点垂直居中 → 取消');
  T.vAlign('bottom');
  A(c7.classList.contains('ptoe-vbot') && !c7.classList.contains('ptoe-vmid'), '切到底部 → 仅 ptoe-vbot');
  T.align('center');
  A(c7.classList.contains('ptoe-align-center') && c7.classList.contains('ptoe-vbot'), '水/垂直对齐互不干扰');
  unmount(m7);

  // ---------- 8. 「更多」菜单项 ----------
  console.log('\n== 8. 菜单项 ==');
  const m8 = mountTable('<table><tbody><tr><td>a</td><td>b</td></tr></tbody></table>', 0);
  key(m8, m8.table.querySelector('td'));
  T.menuOpen(10, 10);
  const menu = doc.getElementById('tblMenu');
  const menuOps = Array.from(menu.querySelectorAll('button')).map((b) => b.dataset.tblOp);
  ['merge', 'split', 'colwidth', 'rowheight', 'insertrowabove', 'insertrowbelow', 'moverowup',
    'moverowdown', 'insertcolleft', 'insertcolright', 'movecolleft', 'movecolright', 'delrow', 'delcol',
    'distribute', 'reset', 'valigntop', 'valignmiddle', 'valignbottom', 'noborder', 'header', 'uncolumn', 'deltable']
    .forEach((op) => A(menuOps.indexOf(op) >= 0, '菜单含 ' + op));
  A(menu.style.display === 'block', '菜单打开 (display=' + menu.style.display + ')');
  A(w.__T.menuItems.length >= 20, '菜单项数据结构完整');
  unmount(m8);

  // ---------- 9. 撑行（表格太小的修复路径） ----------
  console.log('\n== 9. 含表格的行撑行 ==');
  const m9 = mountTable('<table><tbody><tr><td>a</td></tr></tbody></table>', 0);
  T.syncGrow(m9.ed, 0);
  A(m9.row.classList.contains('ptoe-has-table'), '含表格的行加上 ptoe-has-table');
  m9.ed.innerHTML = '<p>纯文本</p>';
  T.syncGrow(m9.ed, 0);
  A(!m9.row.classList.contains('ptoe-has-table'), '表格移除后去掉 ptoe-has-table');
  unmount(m9);

  // ---------- 10. buildTableHtml 结构 ----------
  console.log('\n== 10. buildTableHtml ==');
  const h = T.buildTableHtml(3, 2, true);
  A(/<table>/.test(h) && /<thead><tr><th><\/th><th><\/th><\/tr><\/thead>/.test(h), 'header 表结构正确: ' + h.slice(0, 60));
  A(T.buildTableHtml(999, 999, false).match(/<tr>/g).length === 20, '行列上限 20');
  const m10 = mountTable(T.buildTableHtml(2, 2, false), 0);
  A(T.trsOf(m10.table).length === 2 && T.gridWidth(T.grid(m10.table)) === 2, '插入的表格可被网格化解析');
  unmount(m10);

  // ---------- 10b. 修复缺失单元格 + 尺寸快照（2026-10 重构） ----------
  console.log('\n== 10b. 修复残表 / 尺寸快照 ==');
  const m10b = mountTable(
    '<table><tbody>' +
    '<tr><td>a</td><td rowspan="3">b</td><td>c</td></tr>' +
    '<tr><td>d</td><td>e</td></tr>' +
    '</tbody></table>', 0);
  // 残表特征：rowspan=3 但只有 2 行 → 缺 1 行；补出的行只应填「未被 rowspan 占用」的列
  const addN = T.repair(m10b.table);
  A(addN > 0, 'repair 修复残表并返回补入数量（got ' + addN + '）');
  A(T.gridWidthAll(T.grid(m10b.table)) === 3, '修复后全表列数稳定为 3（补行未多出一列）');
  A(T.repair(m10b.table) === 0, '结构完整后 repair 幂等返回 0');
  unmount(m10b);

  const m10c = mountTable(
    '<table><tbody>' +
    '<tr><td>a</td><td>b</td><td>c</td></tr>' +
    '<tr><td>d</td><td>e</td></tr>' +
    '</tbody></table>', 0);
  A(rowCellCounts(m10c.table)[1] === 2, '前置：第 2 行仅 2 格');
  A(T.repair(m10c.table) === 1, 'repair 为缺格行补 1 格');
  A(rowCellCounts(m10c.table).every((n) => n === 3), '修复后各行均 3 格');
  unmount(m10c);

  // 尺寸快照：jsdom 无布局引擎（getBoundingClientRect 全 0），仅验证结构契约与写入
  const m10d = mountTable('<table><tbody><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></tbody></table>', 0);
  const snap = T.snapshotColPcts(m10d.table);
  A(Array.isArray(snap) && snap.length === 2, 'snapshotColPcts 返回列数长度的数组');
  A(Math.abs(snap.reduce((s, p) => s + p, 0) - 100) < 0.01, '快照列宽合计≈100%');
  A(T.rowHeights(m10d.table).length === 2, 'rowHeights 返回每行一个高度');
  T.applyColPcts(m10d.table, [25, 75]);
  const r0c = m10d.table.querySelectorAll('tr')[0].cells;
  A(r0c[0].style.width === '25%' && r0c[1].style.width === '75%', 'applyColPcts 落到各列 style.width');
  A(/table-layout:\s*fixed/.test(m10d.table.getAttribute('style') || ''), 'applyColPcts 落 table-layout:fixed');
  unmount(m10d);

  // ---------- 11. 浮层收起 / 无残留（「滑页后非表格位置出现表格框线」回归） ----------
  console.log('\n== 11. 浮层收起（滚动不残留）==');
  const m11 = mountTable('<table><tbody><tr><td>a</td><td>b</td></tr></tbody></table>', 0);
  key(m11, m11.table.querySelector('td'));
  T.showBar();
  const L11 = T.layer();
  A(!!L11, '手柄层元素已初始化');
  // jsdom 无布局（getBoundingClientRect 全 0）→ 无法由几何触发显示，手工伪造「已显示 + 有手柄」
  L11.style.display = 'block';
  L11.appendChild(doc.createElement('div')).className = 'tbl-col-h';
  A(L11.children.length > 0, '前置：手柄层含残留手柄');
  T.hideBar(); // 模拟滚动：应用滚动处理器调用 hideTblBar()
  A(L11.style.display === 'none', 'hideTblBar 后手柄层隐藏（回归：原先不收 → 残留浮层）');
  A(L11.children.length === 0, 'hideTblBar 清空手柄层 DOM');

  key(m11, m11.table.querySelector('td'));
  T.showBar();
  T.menuOpen(10, 10);
  A(doc.getElementById('tblMenu').style.display === 'block', '前置：「更多」菜单已打开');
  T.hideBar();
  A(doc.getElementById('tblMenu').style.display === 'none', 'hideTblBar 同时收起「更多」菜单');

  key(m11, m11.table.querySelector('td'));
  T.showBar();
  const L11b = T.layer();
  L11b.style.display = 'block';
  m11.table.parentNode.removeChild(m11.table); // 表格脱离 DOM
  T.reposBar();
  A(L11b.style.display === 'none', '目标表格失效后手柄层收起');
  A(!T.barVisible(), '目标表格失效后操作条标记不可见');
  unmount(m11);

  const compactUi = uiHtml.replace(/\s+/g, ' ');
  A(!/#tblResizeLayer:hover \.tbl-col-h/.test(compactUi), 'CSS：无「整层 hover 显影全部手柄」规则(col)');
  A(!/#tblResizeLayer:hover \.tbl-row-h/.test(compactUi), 'CSS：无「整层 hover 显影全部手柄」规则(row)');
  A(/#tblResizeLayer \.tbl-col-h:hover::after/.test(compactUi), 'CSS：保留单手柄 hover 显影');
  A(/addEventListener\('scroll',\s*\(\)\s*=>\s*\{[^}]*hideTblBar\(\)/.test(appJs), 'app.js：window scroll 处理器收起浮层');

  // ---------- 12. 无边框表格 + 分栏 + 表格内浮层菜单开关（2026-10） ----------
  console.log('\n== 12. 无边框 / 分栏 / 菜单开关 ==');
  // 12a. 无边框：切换 class + 菜单激活态
  const m12 = mountTable('<table><tbody><tr><td>a</td><td>b</td></tr></tbody></table>', 0);
  A(!T.hasNoborder(m12.table), '前置：默认有边框（无 ptoe-noborder）');
  T.toggleNoborder(m12.table);
  A(T.hasNoborder(m12.table), 'toggle 后加 ptoe-noborder');
  T.toggleNoborder(m12.table);
  A(!T.hasNoborder(m12.table), '再 toggle 恢复边框（幂等切换）');
  key(m12, m12.table.querySelector('td'));
  T.toggleNoborder(m12.table);
  T.menuOpen(10, 10);
  const nb12 = doc.getElementById('tblMenu').querySelector('button[data-tbl-op="noborder"]');
  A(nb12 && nb12.classList.contains('active'), '表格菜单「不显示边框」按当前状态高亮');
  unmount(m12);
  // 编辑区 CSS：无边框规则存在（与 htmlmanage EPUB CSS 同契约）
  A(/\.editable table\.ptoe-noborder th,\.editable table\.ptoe-noborder td\{border:none;\}/.test(compactUi),
    'CSS：编辑区无边框规则（th/td border:none）');
  A(/\.editable table\.ptoe-noborder th\{background:transparent;\}/.test(compactUi),
    'CSS：编辑区无边框表头去底色');
  // 保存白名单：_Sanitizer 表格级 class 白名单常量
  A(/TBL_TABLE_CLASSES\s*=\s*\("ptoe-noborder",\)/.test(
    fs.readFileSync(path.join(REPO, 'correctmanage.py'), 'utf8')), '后端：表格级 class 白名单仅放行 ptoe-noborder');

  // 12b. 分栏：buildColumnsTable 结构（无边框 + fixed + 左窄右宽 + 左栏垂直居中）
  const cols12 = T.buildColumnsTable([
    { left: '批注一', right: '正文一' },
    { left: '', right: '正文二' },
  ]);
  A(cols12.tagName === 'TABLE' && cols12.classList.contains('ptoe-noborder'), '分栏表带 ptoe-noborder');
  A(/fixed/i.test(cols12.style.tableLayout || cols12.getAttribute('style') || ''), '分栏表 table-layout:fixed');
  const trs12 = cols12.querySelectorAll('tr');
  A(trs12.length === 2, '每对段落一行（2 行）');
  A(trs12[0].cells[0].style.width === '25%' && trs12[0].cells[0].classList.contains('ptoe-vmid'),
    '左栏 25% 宽 + 垂直居中（ptoe-vmid class）');
  // 2026-10 修复：左栏曾用 inline vertical-align:middle —— inline 压过 class，
  // 顶端/底端对齐永远不生效，且九宫格只认 class → 恒显左上。
  A(!/vertical-align/i.test(trs12[0].cells[0].getAttribute('style') || ''),
    '左栏不带 inline vertical-align（改由 ptoe-vmid class 表达）');
  A(trs12[0].cells[1].style.width === '75%', '右栏 75% 宽');
  A(trs12[0].cells[0].innerHTML === '批注一' && trs12[0].cells[1].innerHTML === '正文一', '第一行左右内容就位');
  A(trs12[1].cells[0].innerHTML === '' && trs12[1].cells[1].innerHTML === '正文二', '落单段放右栏、左栏留空');
  A(cols12.querySelectorAll('thead').length === 0, '分栏表无表头');

  // 12b-2. 对齐生效/识别契约（2026-10 修复）：
  //  ① 编辑区 CSS 必须有 .editable td/th 级对齐规则——否则被 .editable td{text-align:left}
  //    （特异性 0,1,1）压过 .ptoe-align-*（0,1,0），单元格对齐「设置后不生效」。
  A(/\.editable\s+td\.ptoe-align-center/.test(compactUi) && /\.editable\s+th\.ptoe-align-center/.test(compactUi),
    'CSS：单元格水平对齐类有 .editable td/th 级规则（压过默认 text-align:left）');
  A(/\.editable\s+td\.ptoe-align-left/.test(compactUi) && /\.editable\s+td\.ptoe-align-right/.test(compactUi),
    'CSS：单元格左/右对齐同为 td 级规则');
  //  ② 旧内容（分栏左栏）用 inline vertical-align 表达垂直对齐：九宫格要能识别，
  //    设置新方向后要清除 inline（否则 class 压不过 inline）。
  const leg12 = mountTable('<table><tbody><tr><td style="vertical-align:middle">旧批注</td></tr></tbody></table>', 0);
  A(T.vAlignOf(leg12.table.querySelector('td')) === 'middle', '旧 inline vertical-align:middle → 识别为「中」');
  T.setKey({ i: 0, cell: leg12.table.querySelector('td'), table: leg12.table, ed: leg12.ed });
  T.vAlign('bottom');
  const legCell = leg12.table.querySelector('td');
  A(legCell.classList.contains('ptoe-vbot') && legCell.style.verticalAlign !== 'middle',
    '设置垂直对齐后清除 inline vertical-align（否则 class 不生效）');
  unmount(leg12);
  // ③ 「再点同向 = 取消」只认 class：旧 inline 中间单元格点一次「中」应转为 class 且仍居中，
  //    不能因识别到 inline=中间 就判成取消（否则反而跳到顶端）。
  const leg12b = mountTable('<table><tbody><tr><td style="vertical-align:middle">旧批注二</td></tr></tbody></table>', 0);
  T.setKey({ i: 0, cell: leg12b.table.querySelector('td'), table: leg12b.table, ed: leg12b.ed });
  T.vAlign('middle');
  const legCell2 = leg12b.table.querySelector('td');
  A(legCell2.classList.contains('ptoe-vmid') && T.vAlignOf(legCell2) === 'middle'
    && legCell2.style.verticalAlign !== 'middle',
    '旧 inline 中间单元格点「中」→ 转 class 且保持居中（不误判取消）');
  unmount(leg12b);

  // 12c. 表格转段落（分栏还原）：逐行逐格 → 段落，空格跳过
  const m12c = mountTable('<table><tbody>' +
    '<tr><td>左一</td><td>右一</td></tr>' +
    '<tr><td></td><td>右二</td></tr>' +
    '</tbody></table>', 0);
  T.setKey({ i: 0, cell: m12c.table.querySelector('td'), table: m12c.table, ed: m12c.ed });
  T.tableToParagraphs();
  A(!m12c.ed.querySelector('table'), '还原后表格移除');
  const ps12 = Array.from(m12c.ed.querySelectorAll(':scope > p')).map((p) => p.innerHTML);
  A(JSON.stringify(ps12) === JSON.stringify(['左一', '右一', '右二']),
    '逐行逐格还原成段落且跳过空格（got ' + JSON.stringify(ps12) + '）');
  unmount(m12c);

  // 12d. 选区触表判定（表格内浮层菜单开关的门控条件）
  const m12d = mountTable('<table><tbody><tr><td>单元</td></tr></tbody></table>', 0);
  const rInTd = doc.createRange();
  rInTd.selectNodeContents(m12d.table.querySelector('td'));
  A(T.touchesTable(rInTd, null) === true, '选区在单元格内 → 触表');
  const pOut = doc.createElement('p');
  pOut.textContent = '表外段落';
  m12d.ed.appendChild(pOut);
  const rOut = doc.createRange();
  rOut.selectNodeContents(pOut);
  A(T.touchesTable(rOut, null) === false, '选区在表外段落 → 不触表');
  unmount(m12d);

  // 12e. 静态契约：设置弹窗三个复选框 + 默认值 + 触发点门控 + 工具栏分栏按钮
  A(/id="tblPopupMenuChk"/.test(compactUi) && /id="tblCtxMenuChk"/.test(compactUi) && /id="tblTblMenuChk"/.test(compactUi),
    '设置弹窗含表格内浮层菜单三个复选框');
  A(/tbl_popup_menu:\s*false/.test(appJs) && /tbl_ctx_menu:\s*false/.test(appJs) && /tbl_tbl_menu:\s*false/.test(appJs),
    'app.js 默认值：三个开关默认关');
  A(/_selectionTouchesTable\(range, sel\) && !uiBool\('tbl_popup_menu', false\)/.test(appJs),
    'maybeShowPopup 按开关门控（只影响表格）');
  A(/uiBool\('tbl_tbl_menu', false\)/.test(appJs) && /uiBool\('tbl_ctx_menu', false\)/.test(appJs),
    '两个右键菜单监听按各自开关门控');
  A(/id="colsBtn"[^>]*data-op="columns"/.test(compactUi), '工具栏「分栏」按钮（data-op=columns）');
  A(/if \(op === 'columns'\) \{ convertSelectionToColumns\(\); return; \}/.test(appJs), 'applyOp 分派 columns');
  A(/"tbl_tbl_menu": False/.test(
    fs.readFileSync(path.join(REPO, 'configmanage.py'), 'utf8')), 'configmanage 默认值：三个开关默认关');

  // ---------- 13. 单元格对齐九宫格（2026-10） ----------
  console.log('\n== 13. 对齐九宫格 ==');
  const m13 = mountTable('<table><tbody>' +
    '<tr><td>a</td><td>b</td></tr>' +
    '<tr><td>c</td><td>d</td></tr>' +
    '</tbody></table>', 0);
  T.ensureAlignGrid();
  const ag13 = doc.getElementById('tblAlignGrid');
  A(!!ag13, '九宫格节点已构建');
  A(ag13.querySelectorAll('[data-tbl-ag-h]').length === 9, '九宫格含 9 个按钮');
  const a13 = m13.table.querySelector('td');
  key(m13, a13);
  T.alignGridOpen(0, 0);
  A(ag13.style.display === 'block', '九宫格可打开（display=block）');
  A(ag13.querySelectorAll('.tbl-ag-cell.active').length === 1, '九宫格高亮当前组合（默认左/上）');
  // 单格：水平 + 垂直一起设置
  T.align('center'); T.vAlign('middle');
  A(a13.classList.contains('ptoe-align-center') && a13.classList.contains('ptoe-vmid'),
    '单格同时应用水平 + 垂直对齐');
  // 多格选区：整体应用
  T.setSel({ table: m13.table, anchor: { r: 0, c: 0 }, focus: { r: 1, c: 1 } });
  T.align('right'); T.vAlign('bottom');
  A((m13.table.innerHTML.match(/ptoe-align-right/g) || []).length === 4, '多格选区 4 格全设为右对齐');
  A((m13.table.innerHTML.match(/ptoe-vbot/g) || []).length === 4, '多格选区 4 格全设为底端对齐');
  A(T.targetCells().length === 4, '选区命中 4 个单元格');
  // 取消：单格再点同一方向 = 移除
  T.setSel(null);
  T.align('right'); T.vAlign('bottom');
  A(!a13.classList.contains('ptoe-align-right') && !a13.classList.contains('ptoe-vbot'),
    '单格重复点同向 = 取消该对齐');
  T.alignGridClose();
  A(ag13.style.display === 'none', '九宫格可收起');
  unmount(m13);

  // 13b. 九宫格点击链路回归（2026-10 修复）：点选项前必须先走 mousedown，
  // 否则 document 上那个「点击浮层外 → hideTblBar」的处理器会先把 _tblKey 清空，
  // 随后的 click 在 _tblDoOp 入口守卫处静默返回 —— 9 个位置全部「点了没反应」。
  // 该处理器原先只认 #tblBar/#tblMenu/#popup，漏了 #tblAlignGrid（新浮层）。
  console.log('\n== 13b. 九宫格点击链路（mousedown → mouseup → click） ==');
  const m13b = mountTable('<table><tbody>' +
    '<tr><td>x</td><td>y</td></tr>' +
    '<tr><td>z</td><td>w</td></tr>' +
    '</tbody></table>', 0);
  const x13 = m13b.table.querySelector('td');
  // _tblOperate → histBegin → pageSource(i) 需要 pages[i].text（撤销快照来源），
  // 空数组会抛错 —— 与真实环境（页面总是已载入）保持一致。
  T.setPages([{ page: 1, text: '' }, { page: 2, text: '' }]);
  key(m13b, x13);
  T.setSel(null);
  T.ensureAlignGrid();
  const ag13b = doc.getElementById('tblAlignGrid');
  // 落在按钮内部的 <i> 上（与真实点击落在图形子元素一致），closest 仍需能爬到按钮
  function fireClickSeq(el, type) {
    const ev = doc.createEvent('MouseEvents');
    ev.initMouseEvent(type, true, true, w, 0, 0, 0, 0, 0, false, false, false, false, 0, null);
    el.dispatchEvent(ev);
  }
  [['center', 'middle'], ['right', 'bottom'], ['left', 'top']].forEach(function (p) {
    x13.setAttribute('class', '');
    x13.style.verticalAlign = '';
    const btn = ag13b.querySelector('[data-tbl-ag-h="' + p[0] + '"][data-tbl-ag-v="' + p[1] + '"]');
    A(!!btn, '九宫格存在 ' + p[0] + '/' + p[1] + ' 按钮');
    T.alignGridOpen(0, 0);
    fireClickSeq(btn.querySelector('i') || btn, 'mousedown');
    fireClickSeq(btn.querySelector('i') || btn, 'mouseup');
    fireClickSeq(btn.querySelector('i') || btn, 'click');
    A(!!T.getKey(), '点击 ' + p[0] + '/' + p[1] + ' 后 _tblKey 未丢（mousedown 未误关操作条）');
    A(x13.classList.contains('ptoe-align-' + p[0]) && x13.classList.contains('ptoe-v' +
      (p[1] === 'top' ? 'top' : p[1] === 'middle' ? 'mid' : 'bot')),
      '点击 ' + p[0] + '/' + p[1] + ' 后水平 + 垂直 class 均落到单元格');
  });
  // 再点同一个位置 = 取消（单格语义不能因链路变化而失效）
  T.alignGridOpen(0, 0);
  const sameBtn = ag13b.querySelector('[data-tbl-ag-h="left"][data-tbl-ag-v="top"]');
  fireClickSeq(sameBtn.querySelector('i') || sameBtn, 'mousedown');
  fireClickSeq(sameBtn.querySelector('i') || sameBtn, 'mouseup');
  fireClickSeq(sameBtn.querySelector('i') || sameBtn, 'click');
  A(!x13.classList.contains('ptoe-align-left') && !x13.classList.contains('ptoe-vtop'),
    '点击链路下单格再点同向仍可取消对齐');
  // 点九宫格不应清掉多格矩形选区（_tblTargetCells 的来源）
  T.setSel({ table: m13b.table, anchor: { r: 0, c: 0 }, focus: { r: 1, c: 1 } });
  T.alignGridOpen(0, 0);
  const midBtn = ag13b.querySelector('[data-tbl-ag-h="center"][data-tbl-ag-v="middle"]');
  fireClickSeq(midBtn.querySelector('i') || midBtn, 'mousedown');
  fireClickSeq(midBtn.querySelector('i') || midBtn, 'click');
  A(T.getSel() && T.getSel().table === m13b.table, '点九宫格后多格选区保留（对齐仍作用于整个选区）');
  A((m13b.table.innerHTML.match(/ptoe-align-center/g) || []).length === 4,
    '多格选区下点九宫格 4 格全设为居中');
  // 反向保证：点编辑区外仍要收起操作条（不能把守卫改松成永远不关）
  T.setSel(null);
  const bodyHit = doc.createElement('div');
  doc.body.appendChild(bodyHit);
  fireClickSeq(bodyHit, 'mousedown');
  A(!T.getKey() && !T.barVisible(), '点击页面空白处仍收起表格操作条');
  doc.body.removeChild(bodyHit);
  unmount(m13b);

  A(w.__errors.length === 0, '全程无 JS 错误: ' + JSON.stringify(w.__errors));
  console.log('\n===== ' + pass + ' passed, ' + fail + ' failed =====');
  if (fail) { fails.forEach((f) => console.log('  - ' + f)); }
  process.exit(fail ? 1 : 0);
}
