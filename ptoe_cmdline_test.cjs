'use strict';
// 命令行底栏 + 候选面板 UI/UX real-DOM jsdom harness（2026-10-28 美化/功能完善）
// 覆盖：
//   1) DOM 增强：输入字段容器 + ghost 补全层；候选浮层挂在底栏上方的头部信息条；
//   2) 打开态：底栏/候选面板/body 三处状态类、空输入只列「最近 + 内置命令」并分组；
//   3) 输入筛选：命中高亮 <mark>、类型徽标、hint 高亮态；
//   4) ghost：Tab 会补上的后缀（如 7,10me → rge）；
//   5) Tab 补全 / 「最近执行」整条填入；
//   6) 历史：持久化到 localStorage、空输入显示「上次：…」、Ctrl+P/N 翻历史；
//   7) 键位：Ctrl+U 清空、Ctrl+W 删词、Esc 先取消高亮再关闭、F2 隐藏候选；
//   8) 空结果示例 chips（点一下填入）、头部「自适应 / 收起」按钮。
// 运行：
//   python -c "import correctmanage,os;p=os.path.join(os.environ['TEMP'],'opencode','ui_current.html');os.makedirs(os.path.dirname(p),exist_ok=True);open(p,'w',encoding='utf-8').write(correctmanage._UI_HTML)"
//   node ptoe_cmdline_test.cjs
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const REPO = __dirname;
const appJs = fs.readFileSync(process.env.PTOE_APP_JS || path.join(REPO, 'ui/app.js'), 'utf8');
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

const PAGES = [{ page: 1, w: 800, h: 1200, text: '<p>第一行文字</p><p>第二行文字</p>' }];

const dom = new JSDOM(uiHtml, {
  url: 'http://localhost/',
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  beforeParse(window) {
    window.fetch = async (url) => {
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
    window.__store = store;
    const ls = {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
      clear: () => { Object.keys(store).forEach((k) => delete store[k]); },
      get length() { return Object.keys(store).length; },
      key: (i) => Object.keys(store)[i] || null,
    };
    // jsdom 的 localStorage 是原型上的访问器：直接赋值会被忽略，必须 defineProperty 覆盖
    try { Object.defineProperty(window, 'localStorage', { value: ls, configurable: true }); }
    catch (e) { window.localStorage = ls; }
    window.navigator.sendBeacon = () => true;
    window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
    window.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0);
    window.cancelAnimationFrame = (id) => clearTimeout(id);
    window.document.execCommand = () => true;
    window.document.queryCommandState = () => false;
    window.Element.prototype.scrollIntoView = function () {};
    window.__errors = [];
    window.addEventListener('error', (e) => { window.__errors.push(String(e.message)); });
  },
});
const w = dom.window;
const doc = w.document;
const EXPORTS = ['openCmdLine', 'closeCmdLine', '_cmdHistoryAdd', '_cmdRenderSuggest', '_cmdAcceptSelected'];
try {
  w.eval(appJs + '\n;[' + EXPORTS.map((n) => 'typeof ' + n + ' === "undefined" ? null : ' + n).join(',') +
    '].forEach(function(v, i) { if (v !== null && v !== undefined) window["__x_" + ' + JSON.stringify(EXPORTS) + '[i]] = v; });');
} catch (e) {
  console.error('[boot] FAILED:', e.message);
  process.exit(1);
}
const X = (n) => w['__x_' + n];
const store = w.__store;

const $ = (sel) => doc.querySelector(sel);
const $$ = (sel) => Array.prototype.slice.call(doc.querySelectorAll(sel));
function key(k, opts) {
  const ev = new w.KeyboardEvent('keydown', Object.assign({ key: k, bubbles: true, cancelable: true }, opts || {}));
  $('#cmdInput').dispatchEvent(ev);
  return ev;
}
function type(v) {
  const input = $('#cmdInput');
  input.value = v;
  input.dispatchEvent(new w.Event('input', { bubbles: true }));
}
function mousedown(el) {
  const ev = new w.MouseEvent('mousedown', { bubbles: true, cancelable: true });
  el.dispatchEvent(ev);
  return ev;
}
function items() { return $$('#cmdSuggestList .cs-item'); }
function itemTexts() { return items().map((el) => el.getAttribute('data-cmd')); }
function editableOf(i) {
  const row = doc.querySelector('.page-row[data-i="' + i + '"]');
  return row ? row.querySelector('.editable') : null;
}

function run() {
  A(w.__errors.length === 0, '加载期无 JS 错误: ' + JSON.stringify(w.__errors));

  // ---- 1) DOM 增强 ----
  A(!!$('#cmdField'), '输入字段容器 #cmdField 已创建');
  EQ($('#cmdInput') && $('#cmdInput').parentNode.id, 'cmdField', '输入框被放进 #cmdField');
  A(!!$('#cmdGhost'), 'ghost 补全层 #cmdGhost 已创建');
  EQ($('#cmdGhost').getAttribute('aria-hidden'), 'true', 'ghost 对读屏隐藏');
  EQ($('#cmdSuggest') && $('#cmdSuggest').parentNode.id, 'cmdBar', '候选面板挂在底栏内部（贴底栏上方）');
  A(!!$('#cmdSuggestHead'), '候选面板头部信息条已创建');
  A(!!$('#cmdSuggestCount'), '头部有候选数量');
  EQ(Array.prototype.map.call($('#cmdSuggest').children, (el) => el.id).join(','),
    'cmdSuggestGrip,cmdSuggestHead,cmdSuggestList', '面板结构：拖拽条 → 信息条 → 候选列表');
  A($('#cmdSuggest').className.indexOf('cmd-open') < 0, '未打开时候选面板隐藏');

  // ---- 2) 打开态（空输入） ----
  const opened = X('openCmdLine')();
  A(opened === true, 'openCmdLine() 返回 true');
  A($('#cmdBar').classList.contains('cmd-open'), '底栏打开');
  A($('#cmdSuggest').classList.contains('cmd-open'), '候选面板打开');
  A(doc.body.classList.contains('cmd-active'), 'body 进入命令态');
  const cnt = $('#cmdSuggestCount').textContent;
  A(/^候选 \d+ 项$/.test(cnt), '头部显示候选数量: ' + cnt);
  A($$('#cmdSuggestList .cs-sec').length >= 1, '空输入按类型分组（有分组标题）');
  A(items().length > 0, '空输入有候选');
  A(items().every((el) => !!el.querySelector('.cs-kind')), '每个候选都带类型徽标');
  EQ(Array.prototype.map.call(items()[0].children, (el) => el.className).join(','),
    'cs-kind,cs-k,cs-d,cs-tab', '候选行结构：徽标 / 命令名 / 说明 / Tab 提示');
  const kinds = items().map((el) => el.querySelector('.cs-kind').textContent);
  A(kinds.indexOf('式') < 0 && kinds.indexOf('工') < 0, '空输入不铺开格式/工具（只列最近 + 内置命令）');
  A(items()[0].querySelector('.cs-k').textContent === 'merge', '无历史时首项是第一条内置命令');

  // ---- 3) 输入筛选 + 命中高亮 ----
  type('me');
  A(items().some((el) => el.querySelector('.cs-k').textContent === 'merge'), '输入 me 能筛到 merge');
  const mk = $('#cmdSuggestList .cs-k mark');
  A(!!mk && mk.textContent.toLowerCase() === 'me', '命中片段用 <mark> 高亮');
  A($('#cmdHint').classList.contains('cs-live'), '筛选中 hint 进入高亮态');

  // ---- 4) ghost 补全预览 ----
  type('7,10me');
  EQ($('#cmdGhost').textContent, 'rge', 'ghost 预览 Tab 会补上的后缀');
  type('');
  EQ($('#cmdGhost').textContent, '', '空输入不显示 ghost');

  // ---- 5) Tab 补全 + 最近执行整条填入 ----
  type('7,10me');
  key('Tab');
  EQ($('#cmdInput').value, '7,10merge', 'Tab 补全（保留范围前缀）');

  // ---- 6) 历史：持久化 / 上次提示 / Ctrl+P ----
  X('_cmdHistoryAdd')('页 12');
  A((store.ptoe_cmd_history || '').indexOf('页 12') >= 0, '历史写入 localStorage');
  EQ($('#cmdInput').value, '7,10merge', '记录历史不影响当前输入');
  type('');
  const hint = $('#cmdHint').textContent;
  A(hint.indexOf('上次：页 12') >= 0, '空输入提示上次命令: ' + hint);
  A($$('#cmdSuggestList .cs-sec').some((el) => el.textContent === '最近'), '空输入有「最近」分组');
  EQ(items()[0].querySelector('.cs-k').textContent, '页 12', '最近命令排在最前');
  key('Tab');
  EQ($('#cmdInput').value, '页 12', '「最近执行」候选 Tab 整条填入');
  type('');
  key('p', { ctrlKey: true });
  EQ($('#cmdInput').value, '页 12', 'Ctrl+P 翻到上一条历史');
  key('n', { ctrlKey: true });
  EQ($('#cmdInput').value, '', 'Ctrl+N 翻回当前输入');

  // ---- 7) 键位：Ctrl+U / Ctrl+W / Esc / F2 ----
  type('页 12');
  key('u', { ctrlKey: true });
  EQ($('#cmdInput').value, '', 'Ctrl+U 清空输入');
  type('页 12');
  key('w', { ctrlKey: true });
  A($('#cmdInput').value.indexOf('12') < 0, 'Ctrl+W 删除光标前一个词: ' + JSON.stringify($('#cmdInput').value));
  // Esc：先取消高亮，再关闭
  type('me');
  key('ArrowDown');
  A(items().some((el) => el.classList.contains('cs-on')), '↓ 高亮候选');
  key('Escape');
  A($('#cmdSuggest').classList.contains('cmd-open'), 'Esc 第一次只取消高亮，不关面板');
  A(!items().some((el) => el.classList.contains('cs-on')), '高亮已取消');
  type('7,10me');
  A($('#cmdGhost').textContent === 'rge', 'ghost 在取消高亮后仍可用');
  key('F2');
  A(!$('#cmdSuggest').classList.contains('cmd-open'), 'F2 隐藏候选面板');
  EQ($('#cmdGhost').textContent, '', '隐藏候选时 ghost 一并清空');
  key('F2');
  A($('#cmdSuggest').classList.contains('cmd-open'), 'F2 再次显示候选面板');
  key('Escape');
  A(!$('#cmdBar').classList.contains('cmd-open'), 'Esc 第二次关闭命令行');

  // ---- 8) 空结果 chips / 头部按钮 ----
  X('openCmdLine')();
  type('zzzzzz');
  EQ(items().length, 0, '无匹配时不渲染候选');
  const chips = $$('#cmdSuggestList .cs-chip');
  A(chips.length >= 4, '无匹配时给出示例 chips（' + chips.length + ' 个）');
  mousedown(chips[0]);
  EQ($('#cmdInput').value, chips[0].textContent, '点 chip 只填入输入框');
  type('me');
  const autoBtn = $('#cmdSuggestHead button[data-cs="auto"]');
  const hideBtn = $('#cmdSuggestHead button[data-cs="hide"]');
  A(!!autoBtn && !!hideBtn, '头部有「自适应 / 收起」按钮');
  $('#cmdSuggest').style.height = '260px';
  mousedown(autoBtn);
  EQ($('#cmdSuggest').style.height, '', '「自适应」清掉固定高度');
  A(!store.ptoe_cmd_suggest_h, '「自适应」同时清掉持久化高度');
  mousedown(hideBtn);
  A(!$('#cmdSuggest').classList.contains('cmd-open'), '「收起」隐藏候选面板');
  mousedown($('#cmdSuggestHead button[data-cs="hide"]'));
  X('closeCmdLine')();
  A(!$('#cmdBar').classList.contains('cmd-open'), '关闭后底栏收起');
}

function waitReady(cb) {
  const t0 = Date.now();
  (function poll() {
    if (editableOf(0) || Date.now() - t0 > 15000) return cb();
    setTimeout(poll, 50);
  })();
}

waitReady(() => {
  try { run(); } catch (e) { fail++; fails.push('异常: ' + e.message); console.log('  FAIL 异常: ' + e.stack); }
  console.log('\n通过 ' + pass + ' / 失败 ' + fail);
  if (fail) { console.log('失败项：\n  - ' + fails.join('\n  - ')); }
  try { w.close(); } catch (e) { /* ignore */ }
  process.exit(fail ? 1 : 0);
});
