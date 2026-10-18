'use strict';
// 智能清理规则开关 → 工具栏「清理」按钮链路 real-DOM jsdom harness（2026-10）
// 用法：
//   python -c "import correctmanage,os;p=os.path.join(os.environ['TEMP'],'opencode','ui_current.html');open(p,'w',encoding='utf-8').write(correctmanage._UI_HTML)"
//   $env:NODE_PATH="D:\code-project\python\PToEA\node_modules"; node ptoe_clean_rules_test.cjs
//
// 回归目标（2026-10 修复：点工具栏「清理」后已启用的智能清理规则不生效）：
//   A. 打开设置面板 → 勾选/取消勾选规则 → 点 #cleanBtn：
//      POST /api/clean 的 body 必须携带当前开关与符号映射表（与实时预览同口径），
//      不能只发 pages（否则全靠 config.json 侧信道，保存失败/竞态时规则不生效）；
//   B. 从未打开设置面板（沿用上会话已启用规则）→ 点 #cleanBtn：
//      body 携带 uiBool（服务端/localStorage）口径的开关；
//   C. 两种情况下预览弹窗正常弹出、无 JS 错误。
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
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

const cleanRespPages = [{
  page: 1,
  html: '<p>你好，世界。</p>',
  stats: { punct: 2, symbols: 0, md: 0, merged: 1, tags: 0, chars: 5 },
}];

// 启动一个独立的校正界面实例（各自独立 window/fetch 记录）
function boot(serverUiSettings) {
  const requests = [];
  const dom = new JSDOM(uiHtml, {
    url: 'http://localhost/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
    beforeParse(window) {
      window.fetch = async (url, opts) => {
        const u = new URL(url, 'http://localhost/');
        let body = null;
        if (opts && opts.body) { try { body = JSON.parse(opts.body); } catch (e) { body = opts.body; } }
        requests.push({ path: u.pathname, method: (opts && opts.method) || 'GET', body: body });
        const json = (obj) => ({ ok: true, status: 200, json: async () => obj, text: async () => JSON.stringify(obj) });
        if (u.pathname === '/api/pages') {
          return json({ pages: [{ page: 1, w: 100, h: 100, text: '<p>你好,世界</p>' }] });
        }
        if (u.pathname === '/api/ui_settings' && (!opts || !opts.method || opts.method === 'GET')) {
          return json({ ok: true, ui_settings: serverUiSettings || {} });
        }
        if (u.pathname === '/api/clean') return json({ ok: true, pages: cleanRespPages });
        return json({ ok: true });
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
  w.eval(appJs + '\n;window.__SCOPE = { cleanAll: cleanAll, applyCleanPreview: applyCleanPreview };');
  const doc = w.document;
  return {
    w, doc, requests,
    S: w.__SCOPE,
    post: (p) => requests.filter(r => r.path === p && r.method === 'POST'),
    toggle(id, checked) {
      const el = doc.getElementById(id);
      if (!el) return false;
      el.checked = checked;
      el.dispatchEvent(new w.Event('change', { bubbles: true }));
      return true;
    },
    async clickClean() {
      const btn = doc.getElementById('cleanBtn');
      if (!btn) return false;
      btn.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
      await sleep(120);
      return true;
    },
  };
}

(async function run() {
  // ================= 用例 A：打开设置面板并勾选规则后点「清理」 =================
  console.log('\n== 用例 A：设置面板勾选规则 → 工具栏清理按钮 ==');
  const A1 = boot({ clean_paragraphs: false, clean_punctuation: true }); // 服务端是默认值
  await sleep(150); // 等 init + loadUiSettingsFromServer
  A(A1.w.__errors.length === 0, 'A: 加载期无 JS 错误 ' + JSON.stringify(A1.w.__errors));

  const settingsBtn = A1.doc.getElementById('settingsBtn');
  A(!!settingsBtn, 'A: #settingsBtn 存在');
  if (settingsBtn) settingsBtn.dispatchEvent(new A1.w.MouseEvent('click', { bubbles: true }));
  await sleep(30);

  A(A1.toggle('cleanParagraphsChk', true), 'A: 勾选段落合并');
  A(A1.toggle('cleanPunctuationChk', false), 'A: 取消标点归一');
  await sleep(80);
  const saves = A1.post('/api/ui_settings');
  A(saves.length >= 1, 'A: 发出 POST /api/ui_settings (' + saves.length + ')');

  await A1.clickClean();
  const cleansA = A1.post('/api/clean');
  A(cleansA.length === 1, 'A: 发出 POST /api/clean (' + cleansA.length + ')');
  const bodyA = cleansA.length ? cleansA[cleansA.length - 1].body : null;
  A(!!bodyA, 'A: /api/clean 请求体已捕获');
  if (bodyA) {
    A(bodyA.clean_paragraphs === true,
      'A: body.clean_paragraphs === true（启用的规则带上）: ' + JSON.stringify(bodyA.clean_paragraphs));
    A(bodyA.clean_punctuation === false,
      'A: body.clean_punctuation === false（关闭的规则带上）: ' + JSON.stringify(bodyA.clean_punctuation));
    A(bodyA.clean_symbols === true && bodyA.clean_md_tags === true && bodyA.clean_html_tags === true,
      'A: 其余开关按当前口径下发: ' + JSON.stringify({ s: bodyA.clean_symbols, m: bodyA.clean_md_tags, h: bodyA.clean_html_tags }));
    A(Array.isArray(bodyA.clean_symbol_map) && bodyA.clean_symbol_map.length > 0,
      'A: body.clean_symbol_map 随请求发送 (' + (bodyA.clean_symbol_map || []).length + ' 项)');
    A(Array.isArray(bodyA.pages) && bodyA.pages.length > 0, 'A: body.pages 携带待清理页');
  }
  await sleep(60);
  const bgA = A1.doc.getElementById('cleanPreviewBg');
  A(bgA && bgA.style.display === 'flex', 'A: 清理预览弹窗已弹出');
  A(A1.w.__errors.length === 0, 'A: 全程无 JS 错误 ' + JSON.stringify(A1.w.__errors));
  A1.S.applyCleanPreview();
  await sleep(30);
  A(A1.w.__errors.length === 0, 'A: 应用清理无 JS 错误 ' + JSON.stringify(A1.w.__errors));

  // ============ 用例 B：不打开设置面板（沿用上会话已启用规则）============
  console.log('\n== 用例 B：未打开设置面板（服务端已启用规则）→ 工具栏清理按钮 ==');
  const B1 = boot({ clean_paragraphs: true, clean_punctuation: false }); // 上会话已启用段落合并、关闭标点
  await sleep(150);
  A(B1.w.__errors.length === 0, 'B: 加载期无 JS 错误 ' + JSON.stringify(B1.w.__errors));

  await B1.clickClean();
  const cleansB = B1.post('/api/clean');
  A(cleansB.length === 1, 'B: 发出 POST /api/clean (' + cleansB.length + ')');
  const bodyB = cleansB.length ? cleansB[cleansB.length - 1].body : null;
  A(!!bodyB, 'B: /api/clean 请求体已捕获');
  if (bodyB) {
    A(bodyB.clean_paragraphs === true,
      'B: body.clean_paragraphs === true（服务端已启用规则带上）: ' + JSON.stringify(bodyB.clean_paragraphs));
    A(bodyB.clean_punctuation === false,
      'B: body.clean_punctuation === false（服务端已关闭规则带上）: ' + JSON.stringify(bodyB.clean_punctuation));
    A(B1.post('/api/ui_settings').length === 0, 'B: 未改动设置时不应 POST /api/ui_settings');
  }
  A(B1.w.__errors.length === 0, 'B: 全程无 JS 错误 ' + JSON.stringify(B1.w.__errors));

  console.log('\n----------------------------------');
  console.log('pass=' + pass + ' fail=' + fail);
  if (fail) { console.log('失败项:\n  - ' + fails.join('\n  - ')); process.exit(1); }
  process.exit(0);
})();
