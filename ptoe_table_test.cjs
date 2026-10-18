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
    'syncGrow: _syncTblGrow, trsOf: _tblTrsOf,' +
    'showBar: showTblBar, hideBar: hideTblBar, reposBar: _tblRepositionBar,' +
    'hideLayer: _tblHideResizeLayer, showLayer: _tblShowResizeLayer,' +
    'layer: function () { return _tblResizeLayer; },' +
    'layerVisible: function () { return !!(_tblResizeLayer && _tblResizeLayer.style.display === "block"); },' +
    'barVisible: function () { return _tblBarVisible; },' +
    'setKey: function (k) { _tblKey = k; }, getKey: function () { return _tblKey; },' +
    'setSel: function (s) { _tblSel = s; }, getSel: function () { return _tblSel; },' +
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
  ['addrow', 'addcol', 'merge', 'split', 'colwidth', 'rowheight', 'alignleft', 'aligncenter', 'alignright', 'more', 'deltable']
    .forEach((op) => A(barOps.indexOf(op) >= 0, '#tblBar 含按钮 ' + op));
  A(/\.page-row\.ptoe-has-table\s+\.editable\s*\{[^}]*height:\s*auto/.test(uiHtml),
    'CSS：含表格的行放开撑行（.page-row.ptoe-has-table .editable{height:auto}）');
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
    'distribute', 'reset', 'valigntop', 'valignmiddle', 'valignbottom', 'header', 'deltable']
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

  A(w.__errors.length === 0, '全程无 JS 错误: ' + JSON.stringify(w.__errors));
  console.log('\n===== ' + pass + ' passed, ' + fail + ' failed =====');
  if (fail) { fails.forEach((f) => console.log('  - ' + f)); }
  process.exit(fail ? 1 : 0);
}
