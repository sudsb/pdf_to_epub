'use strict';
// 右键菜单「复制 / 粘贴」回归测试（jsdom）
//
// 覆盖 2026-09-13 重写的三条通路：
//   1) copy 事件覆盖法（无需剪贴板权限）
//   2) Clipboard API（有权限时）
//   3) 服务端 /api/clipboard（浏览器 API 被拒时的兜底）
// 以及粘贴插入的降级链（insertHTML → insertText → Range.insertNode）。
//
// 运行：node ptoe_clipboard_test.cjs

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const appJs = fs.readFileSync(path.join(__dirname, 'ui/app.js'), 'utf8');
const uiHtml = fs.readFileSync(path.join(__dirname, 'ui_full.html'), 'utf8');

const fetchCalls = [];
const copied = {};

function jsonRes(obj) {
  return {
    ok: true,
    status: 200,
    json: async () => obj,
    text: async () => JSON.stringify(obj),
    blob: async () => new Blob(),
  };
}

const dom = new JSDOM(uiHtml, {
  url: 'http://localhost/',
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  beforeParse(window) {
    window.fetch = async (url, opts) => {
      const method = (opts && opts.method) || 'GET';
      fetchCalls.push({ url: String(url), method, body: opts && opts.body });
      if (String(url).startsWith('/api/clipboard')) {
        if (method === 'POST') return jsonRes({ ok: true });
        return jsonRes({ ok: true, text: '服务端文本', html: '<i>服务端HTML</i>' });
      }
      if (String(url).startsWith('/api/pages')) {
        return jsonRes({ pages: [{ page: 1, text: '<p>abc</p>', w: 800, h: 1200 }] });
      }
      if (String(url).startsWith('/api/')) return jsonRes({ ok: true });
      return jsonRes({});
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
    window.matchMedia = () => ({ matches: false, addListener: () => {}, removeListener: () => {} });
    window.requestAnimationFrame = (cb) => setTimeout(cb, 0);
    window.cancelAnimationFrame = (id) => clearTimeout(id);
    window.scrollTo = () => {};
    window.getComputedStyle = () => ({
      fontWeight: '400', fontStyle: 'normal', color: '#000',
      textDecorationLine: 'none', verticalAlign: 'baseline',
    });
    window.document.queryCommandState = () => false;
    // 默认：execCommand 全部失败（模拟受限环境）——正好验证降级链路
    window.document.execCommand = () => false;
    window.document.caretRangeFromPoint = (x, y) => {
      const range = window.document.createRange();
      const ed = window.document.querySelector('.editable');
      if (ed) { range.selectNodeContents(ed); range.collapse(true); }
      return range;
    };
  },
});

const w = dom.window;
const doc = w.document;

w.eval(
  appJs +
  `
window.__CLIP = {
  _copyViaEvent: _copyViaEvent,
  _clipboardWrite: _clipboardWrite,
  _clipboardRead: _clipboardRead,
  _insertAtCaret: _insertAtCaret,
  _ctxSelectionText: _ctxSelectionText,
  ctxPasteText: ctxPasteText,
  setCtxClipText: function (v) { _ctxClipText = v; },
  setCtxRange: function (r) { _ctxRange = r; },
  setCtxEditable: function (e) { _ctxEditable = e; }
};`
);

const C = w.__CLIP;
const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

let failed = 0;
function check(name, cond, extra) {
  if (cond) {
    console.log('  [OK ] ' + name);
  } else {
    failed++;
    console.log('  [FAIL] ' + name + (extra ? '  -> ' + extra : ''));
  }
}

(async function main() {
  await sleep(60); // 等 app.js 的 init() 渲染出页行

  const ed = doc.querySelector('.page-row .editable');
  const row = ed && ed.closest('.page-row');
  const ri = row ? Number(row.dataset.i) : 0;
  check('已渲染出可编辑页（夹具就绪）', !!ed, 'editable=' + !!ed);
  if (!ed) { process.exit(1); }

  function caretToEnd() {
    const r = doc.createRange();
    r.selectNodeContents(ed);
    r.collapse(false);
    const sel = w.getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
    return r;
  }

  console.log('[1] copy 事件覆盖法（无剪贴板权限也应成功）');
  doc.execCommand = (cmd) => {
    if (cmd !== 'copy') return false;
    const ev = new w.Event('copy', { bubbles: true, cancelable: true });
    ev.clipboardData = { setData: (k, v) => { copied[k] = v; } };
    doc.dispatchEvent(ev);
    return true;
  };
  check('_copyViaEvent 返回 true', C._copyViaEvent('<b>粗体</b>', '粗体') === true);
  check('text/plain 已写入', copied['text/plain'] === '粗体', JSON.stringify(copied));
  check('text/html 已写入', copied['text/html'] === '<b>粗体</b>', JSON.stringify(copied));

  console.log('[2] 复制降级到服务端 /api/clipboard（execCommand 与 Clipboard API 都不可用）');
  doc.execCommand = () => false;
  fetchCalls.length = 0;
  check('_clipboardWrite 返回 true（服务端兜底成功）', (await C._clipboardWrite('<b>H</b>', 'T')) === true);
  const post = fetchCalls.find((c) => c.method === 'POST' && c.url.startsWith('/api/clipboard'));
  check('已 POST /api/clipboard', !!post, JSON.stringify(fetchCalls));
  check(
    'POST 载荷含 text/html',
    !!post && JSON.parse(post.body).text === 'T' && JSON.parse(post.body).html === '<b>H</b>',
    post && post.body
  );

  console.log('[3] 粘贴读剪贴板：Clipboard API 被拒 → 服务端系统剪贴板');
  w.navigator.clipboard = {
    read: async () => { throw new Error('NotAllowedError: clipboard-read'); },
    readText: async () => { throw new Error('NotAllowedError: clipboard-read'); },
  };
  fetchCalls.length = 0;
  const clip = await C._clipboardRead(true);
  check('读取到服务端内容', !!clip && clip.text === '服务端文本' && clip.html === '<i>服务端HTML</i>', JSON.stringify(clip));
  check(
    'GET /api/clipboard?rich=1',
    fetchCalls.some((c) => c.method === 'GET' && c.url === '/api/clipboard?rich=1'),
    JSON.stringify(fetchCalls)
  );

  console.log('[4] 粘贴插入降级链：insertHTML 失败 → Range.insertNode 兜底');
  doc.execCommand = () => false;
  ed.innerHTML = '<p>abc</p>';
  const r = caretToEnd();
  C.setCtxRange(r);
  C.setCtxEditable(ed);
  check('_insertAtCaret 返回 true', C._insertAtCaret('', 'XYZ') === true);
  check('文本已插入到光标处', /abcXYZ\s*$/.test(ed.textContent), JSON.stringify(ed.textContent));

  console.log('[5] 纯文本粘贴端到端（服务端读取 → histRun 插入）');
  fetchCalls.length = 0;
  ed.innerHTML = '<p>abc</p>';
  await C.ctxPasteText(ed, ri, caretToEnd());
  check('服务端文本已插入', ed.textContent.indexOf('服务端文本') >= 0, JSON.stringify(ed.textContent));

  console.log('[6] 选区文本取值：选区折叠时用右键快照兜底');
  const sel = w.getSelection();
  sel.removeAllRanges();
  sel.collapse(ed, 0);
  C.setCtxClipText('快照文本');
  check('_ctxSelectionText 回退到快照', C._ctxSelectionText() === '快照文本', C._ctxSelectionText());

  await sleep(20);
  console.log('');
  if (failed) {
    console.log('失败 ' + failed + ' 项');
    process.exit(1);
  }
  console.log('全部通过');
})();
