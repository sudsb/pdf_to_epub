'use strict';
// 校正纠错标注「位置错位」回归测试（jsdom）
//
// 覆盖 2026-10-10 两处修复：
//   [1] 多页批处理第二页起错位：未挂载页的纠错基准必须与 displayHtml(k) 的
//       编辑区文本一致（displayHtml 经 _ensureBlockNewlines 在每个块开标签后插入
//       换行，未挂载页若用未插换行的原始串算偏移，挂载后整体错位）。
//   [2] 合并段落后错位：_mergeSelectedBlocks 直接改写 innerHTML、不派发 input
//       事件，绕过 _proofreadAutoDismiss；修复后在合并末尾显式调用它以重锚偏移。
//
// 说明：仓库内的 ui_full.html 夹具已过期（缺少当前 _UI_HTML 中的元素，app.js
// 顶层绑定会抛错）。本测试改为运行时从 correctmanage.py 提取最新 _UI_HTML。
//
// 运行：node ptoe_proofread_pos_test.cjs

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const appJs = fs.readFileSync(path.join(__dirname, 'ui/app.js'), 'utf8');
const py = fs.readFileSync(path.join(__dirname, 'correctmanage.py'), 'utf8');
const m = py.match(/_UI_HTML\s*=\s*r?"""([\s\S]*?)"""/);
if (!m) { console.error('无法从 correctmanage.py 提取 _UI_HTML'); process.exit(1); }
const uiHtml = m[1];

function jsonRes(obj) {
  return {
    ok: true, status: 200,
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
    window.fetch = async (url) => {
      const u = String(url);
      if (u.startsWith('/api/pages')) {
        return jsonRes({
          pages: [
            { page: 1, text: '<p>AAA</p>', w: 800, h: 1200 },
            { page: 2, text: '<p>BBB</p>', w: 800, h: 1200 },
            { page: 3, text: '<p>CCC</p>', w: 800, h: 1200 },
          ],
        });
      }
      if (u.startsWith('/api/')) return jsonRes({ ok: true });
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
    window.document.execCommand = () => false;
    window.document.caretRangeFromPoint = () => {
      const range = window.document.createRange();
      const ed = window.document.querySelector('.editable');
      if (ed) { range.selectNodeContents(ed); range.collapse(true); }
      return range;
    };
    try {
      window.URL.createObjectURL = () => 'blob:stub';
      window.URL.revokeObjectURL = () => {};
    } catch (e) {}
  },
});

const w = dom.window;
const doc = w.document;

w.eval(
  appJs +
  `
window.__PT = {
  _proofreadSourceAtIndex: _proofreadSourceAtIndex,
  _proofreadAutoDismiss: _proofreadAutoDismiss,
  _plainNoAnno: _plainNoAnno,
  displayHtml: displayHtml,
  syncContent: syncContent,
  renderProofread: renderProofread,
  get host() { return host; },
  get contentMap() { return contentMap; },
  get pages() { return pages; },
  get proofreadErrors() { return proofreadErrors; },
  set proofreadErrors(v) { proofreadErrors = v; },
  get _prTextBefore() { return _prTextBefore; }
};`
);

const PT = w.__PT;
const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

let failed = 0;
function check(name, cond, extra) {
  if (cond) console.log('  [OK ] ' + name);
  else { failed++; console.log('  [FAIL] ' + name + (extra ? '  -> ' + extra : '')); }
}
function plainOf(html) {
  const d = doc.createElement('div');
  d.innerHTML = html;
  return d.textContent;
}

(async function main() {
  await sleep(120); // 等 init() 渲染出页行

  const firstRow = PT.host.children && PT.host.children[0];
  check('夹具就绪：已渲染出可编辑页', !!firstRow && !!firstRow.querySelector('.editable'));
  if (!firstRow) { process.exit(1); }
  const ri = Number(firstRow.dataset.i);

  // ---- [1] 未挂载页纠错基准 == displayHtml 渲染文本（多页批处理错位根因） ----
  console.log('[1] 未挂载页纠错基准与 displayHtml 一致（第二页起错位修复）');
  const parent = firstRow.parentNode;
  const next = firstRow.nextSibling;
  parent.removeChild(firstRow); // 制造「未挂载」状态
  const raw = '<h1>标题</h1><p>第一段</p><p>第二段</p>';
  PT.contentMap.set(ri, raw);
  const src = PT._proofreadSourceAtIndex(ri);
  const disp = PT.displayHtml(ri);
  check('返回 html（未走 ed 分支）', src.ed === null, 'src=' + JSON.stringify(src));
  check('基准纯文本 == displayHtml 纯文本', plainOf(src.html) === plainOf(disp),
        JSON.stringify({ src: plainOf(src.html), disp: plainOf(disp) }));
  check('基准含块后换行（<h1>\\n 或 <p>\\n）',
        src.html.indexOf('<h1>\n') >= 0 || src.html.indexOf('<p>\n') >= 0,
        JSON.stringify(src.html));
  parent.insertBefore(firstRow, next); // 复原挂载

  // ---- [2] 合并段落后错误偏移重锚（错误建议位置错误修复） ----
  console.log('[2] 合并段落后错误偏移重锚（错误建议位置错误修复）');
  const ed = firstRow.querySelector('.editable');
  // 采用 displayHtml 的真实块形态：块内开标签后带换行（_ensureBlockNewlines）
  ed.innerHTML = '<p>\nAAA</p><p>\nBBB</p><p>\nCCC</p>';
  PT.syncContent(ed);
  const before = PT._plainNoAnno(ed);
  const idx = before.indexOf('CCC');
  check('初始基准含 CCC', idx >= 0, JSON.stringify(before));
  PT.proofreadErrors[ri] = [{ start: idx, end: idx + 3, wrong: 'CCC', candidates: ['ccc'], line: 1 }];
  PT.renderProofread(ri);
  check('渲染后错误标注已注入（.ptoe-err 存在）', !!ed.querySelector('.ptoe-err'));

  // 模拟 _mergeSelectedBlocks：把第 3 块并入第 2 块（承载 .ptoe-err/.ptoe-fix 标注），再移除第 3 块
  const ps = ed.querySelectorAll('p');
  ps[1].innerHTML += ' ' + ps[2].innerHTML;
  ps[2].parentNode.removeChild(ps[2]);
  PT.syncContent(ed);            // 合并路径同样会 syncContent
  PT._proofreadAutoDismiss(ed, ri); // 修复点：合并后显式重锚
  const after = PT._plainNoAnno(ed);
  const e0 = PT.proofreadErrors[ri][0];
  check('错误偏移已重锚到 CCC', after.slice(e0.start, e0.end) === 'CCC',
        JSON.stringify({ start: e0.start, end: e0.end, after }));
  check('错误未被误标 _gone', !e0._gone);
  check('重绘后标注位置正确（.ptoe-err 文本 == CCC）',
        (ed.querySelector('.ptoe-err') || {}).textContent === 'CCC',
        JSON.stringify((ed.querySelector('.ptoe-err') || {}).textContent));

  await sleep(20);
  console.log('');
  if (failed) { console.log('失败 ' + failed + ' 项'); process.exit(1); }
  console.log('全部通过');
  process.exit(0);
})();
