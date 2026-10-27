'use strict';
// 悬浮菜单鼠标键行为 + 鼠标侧键快捷键绑定（2026-10）jsdom 回归测试
// 用法：
//   $env:NODE_PATH="D:\code-project\python\PToEA\node_modules"; node ptoe_popup_mouse_button_test.cjs
//
// 覆盖：
//   1. 选中文字后的悬浮菜单（#popup）只响应左键(0)/右键(2)；中键(1)与侧键(3/4)忽略；
//   2. 中键在菜单上起手时，滑动手势仍能执行（不被菜单吞掉）；
//   3. 快捷键设置可绑定鼠标侧键（X1/X2），并能全局派发。
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const REPO = __dirname;
const appJs = fs.readFileSync(process.env.PTOE_APP_JS || path.join(REPO, 'ui/app.js'), 'utf8');
const uiHtmlPath = process.env.PTOE_UI_HTML || path.join(REPO, 'ui_full.html');
const uiHtml = fs.readFileSync(uiHtmlPath, 'utf8');

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
      if (u.pathname === '/api/pages') {
        return { ok: true, status: 200, json: async () => ({ pages: [] }), text: async () => '{"pages":[]}' };
      }
      if (u.pathname === '/api/format_rules') {
        return { ok: true, status: 200, json: async () => ({ rules: [] }), text: async () => '{}' };
      }
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

try {
  // app.js 在 eval 作用域内求值：内部标识符（applyOp/runOp/bindings…）只能在该作用域访问，
  // 故在同一段 eval 里完成打桩（记录 applyOp / runOp 调用）并把所需能力挂到 __SCOPE。
  w.eval(appJs + '\n;' +
    'window.__applyOpLog = []; window.__runOpLog = [];\n' +
    'var __origApplyOp = applyOp, __origRunOp = runOp;\n' +
    'applyOp = function (op) { window.__applyOpLog.push(op); };\n' +
    'runOp = function (op) { window.__runOpLog.push(op); return true; };\n' +
    'window.__SCOPE = {\n' +
    '  buildPopup: buildPopup,\n' +
    '  setRows: function (r1, r2) { uiSettings = Object.assign({}, UI_SETTINGS_DEFAULTS, { popup_row1: r1, popup_row2: r2 }); },\n' +
    '  clearSettings: function () { uiSettings = null; },\n' +
    '  bindings: function () { return bindings; },\n' +
    '  setBinding: function (op, v) { bindings[op] = v; },\n' +
    '  reverseBindings: reverseBindings,\n' +
    '  comboLabel: comboLabel,\n' +
    '  MOUSE_SIDE_BUTTONS: MOUSE_SIDE_BUTTONS,\n' +
    '  renderShortcutTable: renderShortcutTable,\n' +
    '  setCapturingOp: function (op) { capturingOp = op; },\n' +
    '  getCapturingOp: function () { return capturingOp; },\n' +
    '  getMidGesture: function () { return _midGesture; },\n' +
    '  restoreApplyOp: function () { applyOp = __origApplyOp; runOp = __origRunOp; },\n' +
    '};');
  console.log('[boot] app.js evaluated');
} catch (e) {
  console.error('[boot] FAILED:', e.message);
  process.exit(1);
}

const S = w.__SCOPE;
const popup = doc.getElementById('popup');

function applyLog() { return (w.__applyOpLog || []).slice(); }
function runLog() { return (w.__runOpLog || []).slice(); }
function clearLogs() { w.__applyOpLog = []; w.__runOpLog = []; }

// 派发 mousedown（带 button）
function mdown(el, button, opts) {
  const init = Object.assign({ bubbles: true, cancelable: true, button: button }, opts || {});
  const ev = new w.MouseEvent('mousedown', init);
  el.dispatchEvent(ev);
  return ev;
}
function mup(el, button, opts) {
  const init = Object.assign({ bubbles: true, cancelable: true, button: button }, opts || {});
  const ev = new w.MouseEvent('mouseup', init);
  el.dispatchEvent(ev);
  return ev;
}
function aux(el, button, opts) {
  const init = Object.assign({ bubbles: true, cancelable: true, button: button }, opts || {});
  const ev = new w.MouseEvent('auxclick', init);
  el.dispatchEvent(ev);
  return ev;
}

setTimeout(run, 60);

function run() {
  A(w.__errors.length === 0, '加载期无 JS 错误: ' + JSON.stringify(w.__errors));
  A(!!popup, '#popup 存在');

  console.log('\n== 1. 悬浮菜单：只有左键/右键触发操作 ==');
  S.setRows(['bold'], []);
  S.buildPopup();
  const boldBtn = popup.querySelector('button.pop-btn[data-op="bold"]');
  A(!!boldBtn, '渲染出加粗按钮');

  mdown(boldBtn, 1);                       // 中键
  A(applyLog().length === 0, '中键点击菜单按钮不触发操作 (' + JSON.stringify(applyLog()) + ')');
  mdown(boldBtn, 3);                       // 侧键1
  A(applyLog().length === 0, '侧键点击菜单按钮不触发操作 (' + JSON.stringify(applyLog()) + ')');
  mdown(boldBtn, 4);                       // 侧键2
  A(applyLog().length === 0, '侧键2点击菜单按钮不触发操作 (' + JSON.stringify(applyLog()) + ')');
  mdown(boldBtn, 2);                       // 右键
  A(applyLog().indexOf('bold') !== -1, '右键点击菜单按钮触发操作 (' + JSON.stringify(applyLog()) + ')');
  clearLogs();
  mdown(boldBtn, 0);                       // 左键
  A(applyLog().indexOf('bold') !== -1, '左键点击菜单按钮触发操作 (' + JSON.stringify(applyLog()) + ')');
  clearLogs();

  console.log('\n== 2. 格式刷按钮 / 规则按钮同样只响应左键右键 ==');
  S.setRows(['bold', 'paint'], []);
  S.buildPopup();
  const paint = popup.querySelector('#popPaint');
  A(!!paint, '格式刷按钮存在');
  if (paint) {
    mdown(paint, 1);
    A(applyLog().length === 0, '中键点击格式刷按钮不触发操作');
    mdown(paint, 3);
    A(applyLog().length === 0, '侧键点击格式刷按钮不触发操作');
  }
  const ruleBtn = popup.querySelector('.pop-rule-btn');
  A(!!ruleBtn, '规则按钮存在');
  if (ruleBtn) {
    mdown(ruleBtn, 1);
    A(applyLog().length === 0, '中键点击规则按钮不触发操作');
  }
  clearLogs();

  console.log('\n== 3. 中键在菜单上起手：滑动手势不被菜单吞掉 ==');
  // 构造一个编辑区 + 选区（currentEditable 依赖选区定位编辑区）
  const ed = doc.createElement('div');
  ed.className = 'editable';
  ed.setAttribute('contenteditable', 'true');
  ed.innerHTML = '<p>hello world</p>';
  doc.getElementById('pages').appendChild(ed);
  const p = ed.querySelector('p');
  const range = doc.createRange();
  range.selectNodeContents(p);
  const sel = w.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  A(!!sel.anchorNode, 'jsdom 选区建立成功');

  S.setRows(['bold'], []);
  S.buildPopup();
  const b2 = popup.querySelector('button.pop-btn[data-op="bold"]');
  mdown(b2, 1);
  A(applyLog().length === 0, '菜单上的中键按下不触发菜单操作');
  const mg = S.getMidGesture();
  A(!!mg, '菜单上的中键按下仍能起手中键手势');
  A(!!mg && mg.ed === ed, '中键手势目标回退到当前选区所在编辑区');
  clearLogs();

  console.log('\n== 4. 快捷键：绑定鼠标侧键 ==');
  const sb = S.MOUSE_SIDE_BUTTONS;
  A(sb.length === 2, '定义了两个侧键 (' + sb.length + ')');
  A(sb[0][2] === 3 && sb[1][2] === 4, '侧键 DOM button 编号为 3/4');
  A(S.comboLabel('Mouse4').indexOf('侧键') !== -1, 'comboLabel(Mouse4) 显示侧键名: ' + S.comboLabel('Mouse4'));
  A(S.comboLabel('Ctrl+B') === 'Ctrl+B', 'comboLabel(键盘组合键) 原样返回');

  // 捕获：设置里点击某行进入捕获态后按侧键 → 绑定
  S.setCapturingOp('italic');
  mdown(doc.body, 3);
  A(S.getCapturingOp() === null, '按侧键后退出捕获态');
  A(S.bindings()['italic'] === 'Mouse4', '侧键1 绑定到 italic (' + S.bindings()['italic'] + ')');
  S.setCapturingOp('underline');
  mdown(doc.body, 4);
  A(S.bindings()['underline'] === 'Mouse5', '侧键2 绑定到 underline (' + S.bindings()['underline'] + ')');
  A(runLog().length === 0, '捕获态下按侧键不派发操作');

  console.log('\n== 5. 快捷键：侧键派发 ==');
  const rev = S.reverseBindings();
  A(rev['Mouse4'] === 'italic', '反向表可由侧键1查到操作 (' + rev['Mouse4'] + ')');
  const evDown = mdown(doc.body, 3);
  A(runLog().indexOf('italic') !== -1, '按侧键1 派发绑定的操作 (' + JSON.stringify(runLog()) + ')');
  A(evDown.defaultPrevented, '已绑定侧键按下时拦截浏览器后退/前进');
  clearLogs();
  mup(doc.body, 3);
  A(runLog().length === 0, '同一按压的 mouseup 不重复派发 (' + JSON.stringify(runLog()) + ')');
  clearLogs();
  mdown(doc.body, 4);
  A(runLog().indexOf('underline') !== -1, '按侧键2 派发绑定的操作 (' + JSON.stringify(runLog()) + ')');
  clearLogs();

  console.log('\n== 5b. 侧键兜底：只上报 mouseup + 未绑定不拦截 ==');
  mup(doc.body, 4); // 结束上一段 mousedown 按压
  aux(doc.body, 4);
  clearLogs();
  // 某些内嵌浏览器只在 mouseup 阶段上报侧键：此时也必须派发一次
  mup(doc.body, 4);
  A(runLog().length === 1 && runLog()[0] === 'underline', 'mouseup-only 侧键派发一次 (' + JSON.stringify(runLog()) + ')');
  aux(doc.body, 4);
  A(runLog().length === 1, '紧随其后的 auxclick 不重复派发 (' + JSON.stringify(runLog()) + ')');
  clearLogs();
  // auxclick 已结束本次按压，下一次 mouseup-only 仍应生效（不能被 500ms 去重吃掉）
  mup(doc.body, 4);
  A(runLog().length === 1, '下一次 mouseup-only 点击仍生效 (' + JSON.stringify(runLog()) + ')');
  clearLogs();

  // 未绑定侧键：保留系统默认后退/前进，不 preventDefault
  S.setBinding('italic', '');
  const evUnbound = mdown(doc.body, 3);
  A(runLog().length === 0, '未绑定侧键不派发操作');
  A(!evUnbound.defaultPrevented, '未绑定侧键不拦截浏览器默认行为');
  const evUnboundUp = mup(doc.body, 3);
  A(!evUnboundUp.defaultPrevented, '未绑定侧键 mouseup 不拦截默认行为');
  S.setBinding('italic', 'Mouse4');
  clearLogs();

  // 长按侧键：mousedown 已派发后，超过 500ms 的 mouseup 也不能再次触发
  const realNow = Date.now;
  let fakeNow = realNow();
  Date.now = function () { return fakeNow; };
  try {
    mdown(doc.body, 3);
    A(runLog().length === 1, '长按侧键：mousedown 派发一次');
    fakeNow += 5000;
    mup(doc.body, 3);
    A(runLog().length === 1, '长按侧键：超时 mouseup 不重复派发 (' + JSON.stringify(runLog()) + ')');
    aux(doc.body, 3);
    A(runLog().length === 1, '长按侧键：超时 auxclick 不重复派发');
  } finally {
    Date.now = realNow;
  }
  clearLogs();

  console.log('\n== 6. 快捷键表渲染：侧键显示为中文名 ==');
  S.setBinding('italic', 'Mouse4');
  S.renderShortcutTable();
  const row = doc.querySelector('#shortcutTable tr[data-op="italic"]');
  A(!!row, '渲染出 italic 行');
  if (row) {
    const cell = row.querySelector('td:nth-child(2)').textContent;
    A(cell.indexOf('侧键') !== -1, '侧键绑定显示中文标签: ' + cell);
    A(cell.indexOf('Mouse4') === -1, '不再显示原始键名 Mouse4: ' + cell);
  }

  A(w.__errors.length === 0, '全流程无 JS 错误: ' + JSON.stringify(w.__errors));

  console.log('\n===== ' + pass + ' passed, ' + fail + ' failed =====');
  if (fail) { fails.forEach((f) => console.log('  - ' + f)); }
  process.exit(fail ? 1 : 0);
}
