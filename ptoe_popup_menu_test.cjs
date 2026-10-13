'use strict';
// 选中文字快捷菜单（#popup）按钮字形 real-DOM jsdom harness（2026-10）
// 用法：
//   python extract_ui.py 的替代（triple-quoted _UI_HTML）见 test_ui_popup.py 说明；
//   或手动产出当前 _UI_HTML 到 %TEMP%\opencode\ui_current.html
//   $env:NODE_PATH="D:\code-project\python\PToEA\node_modules"; node ptoe_popup_menu_test.cjs
//
// 目的：在真实 DOM 中渲染弹出菜单，证明：
//   - 任何配置下都不会出现英文按钮（op 名 / B / I）；
//   - 所有按钮名称两两不重复；
//   - 候选池新增操作（underdot / strip_ws 等）都能渲染出中文单字。
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const REPO = __dirname;
const appJs = fs.readFileSync(process.env.PTOE_APP_JS || path.join(REPO, 'ui/app.js'), 'utf8');
const uiHtmlPath = process.env.PTOE_UI_HTML ||
  path.join(process.env.TEMP || '.', 'opencode', 'ui_current.html');
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
  // 注意：buildPopup() 的行内容优先取 uiSettings（服务端设置），localStorage 仅作加载前兜底。
  // 故这里注入 setRows() 直接改 uiSettings，才能真实模拟「自定义弹出行」。
  w.eval(appJs + '\n;window.__SCOPE = {' +
    'OP_ICON: OP_ICON, POPUP_ELIGIBLE_OPS: POPUP_ELIGIBLE_OPS, buildPopup: buildPopup, ' +
    'popupOpIcon: popupOpIcon, popupOpLabel: popupOpLabel, UI_SETTINGS_DEFAULTS: UI_SETTINGS_DEFAULTS, ' +
    'setRows: function (r1, r2) { uiSettings = Object.assign({}, UI_SETTINGS_DEFAULTS, { popup_row1: r1, popup_row2: r2 }); }, ' +
    'clearSettings: function () { uiSettings = null; } };');
  console.log('[boot] app.js evaluated');
} catch (e) {
  console.error('[boot] FAILED:', e.message);
  process.exit(1);
}

const S = w.__SCOPE;
const popup = doc.getElementById('popup');

function iconTexts() {
  // 只取有 data-op 的操作按钮（排除 #popPaint 与「规」规则按钮，它们单独断言）
  return Array.from(popup.querySelectorAll('button.pop-btn[data-op]'))
    .map((b) => (b.textContent || '').trim());
}

setTimeout(run, 60);

function run() {
  A(w.__errors.length === 0, '加载期无 JS 错误: ' + JSON.stringify(w.__errors));
  A(!!popup, '#popup 存在');

  console.log('\n== 1. 默认配置（两行默认按钮）==');
  w.localStorage.removeItem('ptoe_popup_row1');
  w.localStorage.removeItem('ptoe_popup_row2');
  S.clearSettings();
  S.buildPopup();
  let texts = iconTexts();
  A(texts.length >= 10, '默认配置渲染出操作按钮 (got ' + texts.length + ')');
  A(texts.every((t) => t.length > 0), '默认按钮均非空');
  A(texts.every((t) => !/[A-Za-z]/.test(t)), '默认按钮无英文: ' + JSON.stringify(texts));
  A(new Set(texts).size === texts.length, '默认按钮名称不重复: ' + JSON.stringify(texts));
  const paint = popup.querySelector('#popPaint');
  A(!!paint && (paint.textContent || '').trim() === '刷', '格式刷按钮存在且为「刷」');
  const allDefault = texts.concat([paint ? paint.textContent.trim() : '', '规']);
  A(new Set(allDefault).size === allDefault.length, '含格式刷/规则按钮后仍不重复');

  console.log('\n== 2. 自定义行：新增功能（下加点 / 去空）==');
  S.setRows(['underline', 'underdot', 'strike', 'strip_ws'],
            ['indent', 'charbox', 'shade', 'highlight']);
  S.buildPopup();
  texts = iconTexts();
  A(texts.includes('线') && texts.includes('点') && texts.includes('空'),
    '新增功能渲染为中文单字: ' + JSON.stringify(texts));
  A(texts.every((t) => !/[A-Za-z]/.test(t)), '自定义行按钮无英文: ' + JSON.stringify(texts));
  A(new Set(texts).size === texts.length, '自定义行按钮名称不重复: ' + JSON.stringify(texts));

  console.log('\n== 3. 候选池全量：每个操作都有唯一中文单字 ==');
  const all = S.POPUP_ELIGIBLE_OPS.filter((o) => o !== 'paint');
  S.setRows(all.slice(0, 20), all.slice(20));
  S.buildPopup();
  const seen = [];
  all.forEach((op) => {
    const b = popup.querySelector('button.pop-btn[data-op="' + op + '"]');
    A(!!b, '候选操作有按钮: ' + op);
    if (b) {
      const t = (b.textContent || '').trim();
      A(t.length > 0 && !/[A-Za-z]/.test(t), '候选操作中文非空字形: ' + op + '=' + t);
      seen.push(t);
    }
  });
  A(new Set(seen).size === seen.length, '全部候选操作字形两两不重复 (' + seen.length + ' 个)');
  A(w.__errors.length === 0, '渲染全量候选池后无 JS 错误: ' + JSON.stringify(w.__errors));

  console.log('\n== 4. 字符格式按钮点击打开字号/颜色弹窗 ==');
  S.setRows(['charformat'], []);
  S.buildPopup();
  const cfBtn = popup.querySelector('button.pop-btn[data-op="charformat"]');
  A(!!cfBtn, '候选池「字符格式」渲染出按钮');
  A(!!cfBtn && (cfBtn.textContent || '').trim() === '格', '字符格式字形为「格」');
  const cfBg = doc.getElementById('charFormatModalBg');
  A(!!cfBg, '字符格式弹窗 DOM 存在');
  if (cfBg) cfBg.style.display = 'none';
  if (cfBtn) {
    // 生产代码在 mousedown 上触发（防抢选区），click() 不派发 mousedown
    cfBtn.dispatchEvent(new w.MouseEvent('mousedown', { bubbles: true, cancelable: true }));
  }
  A(cfBg && cfBg.style.display === 'flex', '点击字符格式按钮后弹窗打开 (display=' + (cfBg && cfBg.style.display) + ')');
  A(w.__errors.length === 0, '点击字符格式后无 JS 错误: ' + JSON.stringify(w.__errors));

  console.log('\n===== ' + pass + ' passed, ' + fail + ' failed =====');
  if (fail) { fails.forEach((f) => console.log('  - ' + f)); }
  process.exit(fail ? 1 : 0);
}
