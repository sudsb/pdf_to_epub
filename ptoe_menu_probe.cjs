'use strict';
// 探针：表格内右键 / 选中文字时，各浮层的显示状态（#contextMenu / #tblMenu / #popup）
// 2026-10 「表格内浮层菜单开关」行为矩阵：
//   默认（全关）：表格内右键无自定义菜单、选中文字无 popup；表格外不受影响
//   开启对应开关后：恢复弹出（三个开关独立）
// 注意：app.js 启动后会异步渲染 #pages（/api/pages），探针的 DOM 必须在启动稳定后再挂载，
// 否则挂载的节点被重渲染拆除，事件在游离树上冒泡到不了 document（本探针曾踩此坑）。
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => {
  console.log('jsdomError:', e.message);
  if (e.detail && e.detail.stack) console.log(e.detail.stack.split('\n').slice(0, 4).join('\n'));
});

const REPO = __dirname;
const appJs = fs.readFileSync(path.join(REPO, 'ui/app.js'), 'utf8');
const cm = fs.readFileSync(path.join(REPO, 'correctmanage.py'), 'utf8');
const m = cm.match(/_UI_HTML\s*=\s*r"""([\s\S]*?)"""/);
if (!m) throw new Error('未找到 correctmanage._UI_HTML');
const uiHtml = m[1];

const dom = new JSDOM(uiHtml, {
  url: 'http://localhost/',
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  virtualConsole: vc,
  beforeParse(window) {
    window.fetch = async (url) => {
      const u = new URL(url, 'http://localhost/');
      if (u.pathname === '/api/pages') return { ok: true, status: 200, json: async () => ({ pages: [] }), text: async () => '{"pages":[]}' };
      if (u.pathname === '/api/format_rules') return { ok: true, status: 200, json: async () => ({ rules: [] }), text: async () => '{}' };
      if (u.pathname === '/api/ui_settings') return { ok: true, status: 200, json: async () => ({ ok: true, ui_settings: {} }), text: async () => '{}' };
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
    window.focus = () => {};
    // jsdom 的 Range 缺少 getClientRects/getClientRect（showPopup 定位用），补空实现
    if (window.Range && !window.Range.prototype.getClientRects) {
      window.Range.prototype.getClientRects = function () { return []; };
    }
    if (window.Range && !window.Range.prototype.getClientRect) {
      window.Range.prototype.getClientRect = function () { return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; };
    }
    if (window.Range && !window.Range.prototype.getBoundingClientRect) {
      window.Range.prototype.getBoundingClientRect = function () { return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; };
    }
  },
});
const w = dom.window;
const doc = w.document;
w.eval(appJs + '\n;window.__T = { buildTableHtml: buildTableHtml, uiBool: uiBool, touchesTable: _selectionTouchesTable };' +
  'window.__setUi = function (k, v) { if (!uiSettings) uiSettings = {}; uiSettings[k] = v; };' +
  'window.__isMd = function () { return !!mdMode; };');
const T = w.__T;
const host = doc.getElementById('pages');

let ed, td, p1, ctxMenu, tblMenu, popup, sel;
let failed = 0;
function A(cond, msg) {
  console.log((cond ? '  ok   ' : '  FAIL ') + msg);
  if (!cond) failed++;
}
function fire(el, type, init) {
  const e = new w.MouseEvent(type, Object.assign({ bubbles: true, cancelable: true, view: w }, init || {}));
  el.dispatchEvent(e);
}
function vis(el) {
  if (!el) return false;
  if (el.hidden) return false;
  const d = w.getComputedStyle(el).display;
  return d === 'block' || d === 'flex';
}
function resetSel() {
  sel.removeAllRanges();
  fire(doc.body, 'mousedown', { button: 0, clientX: 400, clientY: 400 });
}
// 挂载守卫：app.js 的轮询/异步流程会重渲染 #pages 并拆掉手动挂载的节点；
// 每个场景开始前检查，若被拆除则重新挂载并重取引用（事件在游离树上到不了 document）。
function ensureMounted() {
  if (ed && ed.isConnected && td && td.isConnected) return;
  host.innerHTML = '';
  const row = doc.createElement('div');
  row.className = 'page-row';
  row.dataset.i = '0';
  ed = doc.createElement('div');
  ed.className = 'editable';
  ed.innerHTML = '<p>段前</p>' + T.buildTableHtml(2, 2, true) + '<p>段后</p>';
  row.appendChild(ed);
  host.appendChild(row);
  td = ed.querySelector('td');
  p1 = ed.querySelector('p');
  ctxMenu = doc.getElementById('contextMenu');
  tblMenu = doc.getElementById('tblMenu');
  popup = doc.getElementById('popup');
  if (!td.isConnected) { console.log('FAIL: 重新挂载失败'); process.exit(1); }
}

setTimeout(() => {
  // 启动稳定后挂载（避免被启动渲染拆除）
  const row = doc.createElement('div');
  row.className = 'page-row';
  row.dataset.i = '0';
  ed = doc.createElement('div');
  ed.className = 'editable';
  ed.innerHTML = '<p>段前</p>' + T.buildTableHtml(2, 2, true) + '<p>段后</p>';
  row.appendChild(ed);
  host.appendChild(row);
  ctxMenu = doc.getElementById('contextMenu');
  tblMenu = doc.getElementById('tblMenu');
  popup = doc.getElementById('popup');
  td = ed.querySelector('td');
  p1 = ed.querySelector('p');
  sel = w.getSelection();
  if (!td.isConnected) { console.log('FAIL: td 未挂载'); process.exit(1); }

  // ---- 场景1：默认全关 ----
  fire(td, 'contextmenu', { button: 2, clientX: 100, clientY: 100 });
  A(!vis(ctxMenu), '默认全关：表格内右键 #contextMenu 不弹');
  A(!vis(tblMenu), '默认全关：表格内右键 #tblMenu 不弹');
  resetSel();
  const r = doc.createRange();
  r.selectNodeContents(td);
  sel.addRange(r);
  fire(td, 'mouseup', { button: 0, clientX: 100, clientY: 100 });
  A(popup.style.display !== 'flex', '默认全关：表格内选中文字 #popup 不弹');
  resetSel();
  fire(p1, 'contextmenu', { button: 2, clientX: 100, clientY: 100 });
  A(vis(ctxMenu), '默认全关：段落内右键 #contextMenu 照常弹（只影响表格）');
  resetSel();
  setTimeout(() => { // 跳出右键后的 suppressPopupUntil 窗口
    sel.removeAllRanges();
    const r2 = doc.createRange();
    r2.selectNodeContents(p1);
    sel.addRange(r2);
    fire(p1, 'mouseup', { button: 0, clientX: 100, clientY: 100 });
    A(popup.style.display === 'flex', '默认全关：段落内选中文字 #popup 照常弹（只影响表格）');
    resetSel();

    // ---- 场景2：三个开关逐一开启（独立控制） ----
    setTimeout(() => {
      ensureMounted();
      w.__setUi('tbl_tbl_menu', true);
      fire(td, 'contextmenu', { button: 2, clientX: 100, clientY: 100 });
      A(vis(tblMenu), '仅开表格菜单：表格内右键 #tblMenu 弹出');
      A(!vis(ctxMenu), '仅开表格菜单：#contextMenu 仍不弹（独立控制）');
      resetSel();

      setTimeout(() => {
        ensureMounted();
        w.__setUi('tbl_ctx_menu', true);
        fire(td, 'contextmenu', { button: 2, clientX: 100, clientY: 100 });
        A(vis(ctxMenu), '再开右键菜单：表格内右键 #contextMenu 弹出');
        A(vis(tblMenu), '再开右键菜单：#tblMenu 仍弹出（互不影响）');
        resetSel();

        setTimeout(() => {
          ensureMounted();
          w.__setUi('tbl_popup_menu', true);
          // 注：jsdom 对表格内选区有「addRange 后立即折叠」的怪癖（真实浏览器无此问题），
          // mouseup→maybeShowPopup 全链路无法在 jsdom 断言，改为直测门控的两个条件：
          // ① 选区触表判定 _selectionTouchesTable ② 开关读取 uiBool('tbl_popup_menu')。
          const rT = doc.createRange();
          rT.selectNodeContents(td);
          A(T.touchesTable(rT, null) === true, '门控判定：表格内选区识别为「触表」');
          const rP = doc.createRange();
          rP.selectNodeContents(p1);
          A(T.touchesTable(rP, null) === false, '门控判定：段落选区识别为「未触表」（不影响表格外）');
          A(T.uiBool('tbl_popup_menu', false) === true, '门控判定：tbl_popup_menu 开关可读为开');
          console.log(failed === 0 ? '===== probe all passed =====' : '===== probe FAILED: ' + failed + ' =====');
          process.exit(failed === 0 ? 0 : 1);
        }, 400);
      }, 400);
    }, 400);
  }, 400);
}, 600);
