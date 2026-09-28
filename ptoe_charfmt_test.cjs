'use strict';
// 逐字符格式 + 分隔线 real-DOM jsdom harness（2026-09-27）
// 用法：python %TEMP%\opencode\extract_ui_current.py   # 产出当前 _UI_HTML
//       $env:NODE_PATH="D:\code-project\python\PToEA\node_modules"; node ptoe_charfmt_test.cjs
// 完整 eval app.js（非片段提取）—— 片段式无模块级 const 上下文会漏真 bug。
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const REPO = __dirname;
const APP_JS = process.env.PTOE_APP_JS || path.join(REPO, 'ui/app.js');
const appJs = fs.readFileSync(APP_JS, 'utf8');
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

// 分隔线字形常量（码位构造，规避写文件时被改写；勿与下面 charStyleGet 的别名 G 冲突）
const GLY = { solid: '─', dashed: '╌', dotted: '┈', double: '═' };
const CP = { solid: 0x2500, dashed: 0x254C, dotted: 0x2508, double: 0x2550 };
const expDiv = (k) => '<p class="ptoe-divider ptoe-divider-' + k + '">' + GLY[k].repeat(8) + '</p>';

// 两页测试内容：第 1 页含分隔线块；第 2 页普通段落
const PAGES = [
  { page: 1, w: 800, h: 1200, text: '<p>第一段测试文字</p>' + expDiv('solid') + '<p>第二段测试文字</p>' },
  { page: 2, w: 800, h: 1200, text: '<p>第二页第一段</p><p>第二页第二段</p>' },
];

const dom = new JSDOM(uiHtml, {
  url: 'http://localhost/',
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  beforeParse(window) {
    window.fetch = async (url, opts) => {
      const u = new URL(url, 'http://localhost/');
      if (u.pathname === '/api/pages') {
        return { ok: true, status: 200,
          json: async () => ({ pages: PAGES }),
          text: async () => JSON.stringify({ pages: PAGES }) };
      }
      if (u.pathname === '/help.md') return { ok: true, text: async () => '# Help' };
      if (u.pathname === '/api/auto_save') {
        return { ok: true, json: async () => ({ auto_save: { enabled: false, interval_minutes: 5, new_backup: true } }) };
      }
      if (u.pathname.startsWith('/api/')) {
        return { ok: true, status: 200, json: async () => ({ ok: true }), text: async () => '{}' };
      }
      return { ok: true, json: async () => ({}), text: async () => '' };
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
    // insertText 记录器：Markdown 模式必须走 execCommand('insertText')，此处验证
    window.__inserted = [];
    window.document.execCommand = (cmd, ui, val) => {
      if (cmd === 'insertText') {
        window.__inserted.push(String(val));
        // 真实浏览器行为：把文本插入到当前选区（覆盖选区）
        const sel = window.getSelection();
        if (sel && sel.rangeCount && !sel.isCollapsed) {
          const r = sel.getRangeAt(0);
          r.deleteContents();
          r.insertNode(window.document.createTextNode(String(val)));
        }
        return true;
      }
      return true;
    };
    window.document.queryCommandState = () => false;
    window.__errors = [];
    window.addEventListener('error', (e) => { window.__errors.push(String(e.message)); });
  },
});
const w = dom.window;
const doc = w.document;

const EXPORTS = [
  'normalizeCharStyle', 'charStyleGet', 'charStyleString', 'charStyleDeclMap', '_canonCharDecl',
  'CHAR_FONT_STACKS', 'CHAR_STYLE_PROPS',
  'DIVIDER_CLASS', 'DIVIDER_STYLES', 'DIVIDER_GLYPHS', 'buildDividerHtml', 'dividerGlyph',
  'isDividerBlock', 'isDividerHtmlLine',
  'applyCharStyleToBlock', 'insertDividerAtCaret', '_insertDividerMd',
  '_charTargetRange', '_snapOffsets', '_rangeFromOffsets', '_textExtentIn', '_splitStyledSpans',
  'openCharFormatDialog', 'closeCharFormatDialog', 'applyCharFormat', 'clearCharFormatQuick',
  'updateCharFormatPreview', 'charFormatMode', 'unwrapStyledSpans', 'applyOp',
  '_captureCharFormatSelection', '_restoreCharFormatSelection', '_cfTargetEd', '_charFormatDecls',
  '_paintColorSwatch', 'viewportPage', 'currentEditable',
  'pages', 'contentMap', 'mdSourceMap', 'pageSource', 'displayHtml', 'undoHistory',
  'undoStack', 'redoStack', 'mdMode', 'toggleDropMenu', 'closeDropMenu', 'setMdMode',
];
try {
  w.eval(appJs + '\n;[' + EXPORTS.map((n) => 'typeof ' + n + ' === "undefined" ? null : ' + n).join(',') +
    '].forEach(function(v, i) { if (v !== null && v !== undefined) window["__x_" + ' + JSON.stringify(EXPORTS) + '[i]] = v; });');
  console.log('[boot] app.js evaluated');
} catch (e) {
  console.error('[boot] FAILED:', e.message);
  console.error(e.stack);
  process.exit(1);
}
// pages 等是 let/const（严格模式下 eval 不挂 window）—— 通过桥接取回
for (let i = 0; i < EXPORTS.length; i++) {
  const key = '__x_' + EXPORTS[i];
  if (w[key] === undefined || w[key] === null) continue;
  try { w[EXPORTS[i]] = w[key]; } catch (e) { /* const 不可写，忽略 */ }
}
const X = (n) => w['__x_' + n];

// ---------- 1. 归一化契约（纯函数） ----------
console.log('\n== 1. normalizeCharStyle 契约 ==');
const N = X('normalizeCharStyle');
EQ(N('font-size:20px'), 'font-size:20px', '单声明原样');
EQ(N('FONT-SIZE:20PX'), 'font-size:20px', '属性名/单位大小写归一');
EQ(N('font-size:20.4px'), 'font-size:20px', '字号四舍五入');
// 后端 int(round(float(x))) 是银行家舍入（.5 取偶），Math.round 是 .5 进位 —— 两者必须一致
EQ(N('font-size:20.5px'), 'font-size:20px', '20.5px 按半数取偶 → 20px（后端 round 语义）');
EQ(N('font-size:21.5px'), 'font-size:22px', '21.5px 按半数取偶 → 22px');
EQ(N('font-size:22.5px'), 'font-size:22px', '22.5px 按半数取偶 → 22px');
EQ(N('font-size:20.6px'), 'font-size:21px', '20.6px 四舍五入 → 21px');
EQ(N('font-size:3px'), 'font-size:8px', '字号下限钳到 8');
EQ(N('font-size:999px'), 'font-size:72px', '字号上限钳到 72');
EQ(N('font-size:abc'), '', '非法字号丢弃');
EQ(N('font-size:20'), '', '无单位字号丢弃');
EQ(N('color:#ABCDEF'), 'color:#abcdef', '颜色小写化');
EQ(N('color:#12345'), 'color:#12345', '5 位十六进制合法');
EQ(N('color:#123456789'), '', '9 位十六进制非法');
EQ(N('color:red'), 'color:red', '颜色名透传');
// 后端 CHAR_COLOR_RE 与 correctmanage._COLOR_VALUE_RE 刻意同形（#3-8 十六进制 或 3-20 字母名），
// 收窄会让 <font color> 转换与 position 过滤出现分叉 —— 前端必须照抄，不得自创更严规则。
EQ(N('color:reddishbluexx'), 'color:reddishbluexx', '3-20 位字母名按后端同形规则透传');
EQ(N('color:a'.repeat(21)), '', '21 位字母名超长丢弃');
EQ(N('color:rgb(1,2,3)'), '', 'rgb() 函数丢弃');
EQ(N("font-family:'宋体', SimSun, serif"), 'font-family:宋体, SimSun, serif', '字体去引号+折叠空白');
EQ(N('font-family:Arial, sans-serif'), '', '白名单外字体丢弃');
EQ(N('font-family:serif'), 'font-family:serif', 'generic 字体合法');
EQ(N('text-align:CENTER'), 'text-align:center', '对齐值小写');
EQ(N('text-align:justify'), '', '非法对齐丢弃');
EQ(N('position:absolute;top:0;font-size:12px'), 'font-size:12px', '白名单外属性丢弃');
EQ(N('color:red;color:#00ff00'), 'color:#00ff00', '同名后者覆盖');
EQ(N(';;;font-size:12px;;;'), 'font-size:12px', '多余分号不炸');
EQ(N('nocolon;font-size:12px'), 'font-size:12px', '无冒号声明丢弃');
EQ(N('color:red;font-family:serif;font-size:12px;text-align:right'),
   'text-align:right;font-size:12px;font-family:serif;color:red', '序列化顺序固定');
EQ(N(''), '', '空串 → 空');
EQ(N(null), '', 'null → 空');
EQ(N(undefined), '', 'undefined → 空');
// 幂等 + 反复往返不 churn
let s = 'color:#AABBCC;font-size:20.6px;position:fixed;font-family:黑体, SimHei, sans-serif';
const once = N(s);
A(N(once) === once, '幂等 normalizeCharStyle(normalizeCharStyle(x)) === normalizeCharStyle(x)');
EQ(N(N(s)), once, '重复归一不改变结果');
// 10 项字体栈全部往返无损
const stacks = X('CHAR_FONT_STACKS');
A(stacks.length === 10, 'CHAR_FONT_STACKS 共 10 项');
for (const st of stacks) A(N('font-family:' + st) === 'font-family:' + st, '字体栈往返无损: ' + st);
// charStyleGet
const G = X('charStyleGet');
EQ(G('color:red;font-size:20px', 'font-size'), '20px', 'charStyleGet 取字号');
EQ(G('color:red', 'font-family'), '', 'charStyleGet 缺失项返回空');
EQ(G('color:red', 'bogus'), '', 'charStyleGet 未知属性返回空');
EQ(G('color:#FF0000', 'COLOR'), '#ff0000', 'charStyleGet 属性名不区分大小写');
// charStyleString / charStyleDeclMap 往返
const CS = X('charStyleString');
const CD = X('charStyleDeclMap');
EQ(CS({ 'font-size': '20px', color: 'red' }), 'font-size:20px;color:red', 'charStyleString 按序输出');
EQ(CS({ 'font-size': '999px' }), 'font-size:72px', 'charStyleString 过值钳制');
EQ(JSON.stringify(CD('color:red;font-size:20px')), JSON.stringify({ 'font-size': '20px', color: 'red' }), 'charStyleDeclMap 往返');

console.log('\n== 2. 分隔线标记契约 ==');
const BD = X('buildDividerHtml');
const DG = X('dividerGlyph');
// 期望串一律由码位构造：本文件经 PowerShell/工具写入时裸 U+2500 系字形可能被改写，
// 用码位可让「实现写了几个字、哪个码位」成为真正的断言而不是运气。
EQ(BD('bogus'), BD('solid'), '未知样式回退 solid');
for (const k of ['solid', 'dashed', 'dotted', 'double']) {
  EQ(BD(k), expDiv(k), k + ' 标记精确（含 class 与字形）');
  EQ(DG(k).length, 8, k + ' 字形 8 字符');
  EQ(DG(k).charCodeAt(0), CP[k], k + ' 字形码位 = U+' + CP[k].toString(16).toUpperCase());
  A(new Set(DG(k).split('')).size === 1, k + ' 字形同码位');
  A(BD(k).indexOf('class="ptoe-divider ptoe-divider-' + k + '"') >= 0, k + ' class 为基类+后缀两个类');
}
const idb = doc.createElement('div');
idb.innerHTML = expDiv('double') + '<p>' + GLY.solid.repeat(8) + '</p>';
const IDB = X('isDividerBlock');
A(IDB(idb.children[0]), 'isDividerBlock 识别基类+后缀');
A(!IDB(idb.children[1]), 'isDividerBlock 不误判普通段');
const bare = doc.createElement('p');
bare.className = 'ptoe-divider';
A(IDB(bare), 'isDividerBlock 识别裸基类');
const IHBL = X('isDividerHtmlLine');
A(IHBL(expDiv('solid')), 'isDividerHtmlLine 识别 <p> 分隔线');
A(IHBL('<div class="ptoe-divider ptoe-divider-dotted">' + GLY.dotted.repeat(8) + '</div>'),
  'isDividerHtmlLine 识别 md 行内 <div> 分隔线');
A(!IHBL('<p>普通段落</p>'), 'isDividerHtmlLine 不误判普通行');

console.log('\n== 3. 工具栏 / 弹窗 DOM 契约 ==');
EQ(doc.querySelectorAll('#toolbar [data-op]').length, 18, '工具栏 data-op 仍为 18 个');
for (const id of ['charFormatBtn', 'charFmtClearBtn', 'dividerBtn', 'dividerMenu',
                  'charFormatModalBg', 'cfSizeSel', 'cfSizeNum', 'cfFontSel', 'cfColorBtn',
                  'cfColorClearBtn', 'cfPreview', 'cfOkBtn', 'cfClearBtn', 'cfCancelBtn', 'cfCloseBtn',
                  'cfModeSel', 'cfModePage', 'cfModeAll']) {
  A(!!doc.getElementById(id), 'DOM 存在 #' + id);
}
EQ(doc.querySelectorAll('#toolbar .tb-group').length, 8, '工具栏分组 8 个（与改动前一致，字符格式并入文本工具组）');
EQ(doc.querySelectorAll('#dividerMenu [data-div]').length, 4, '分隔线菜单 4 项');
A(doc.getElementById('dividerBtn').closest('#toolbar') !== null, '分隔线按钮在工具栏内');
A(doc.getElementById('dividerBtn').getAttribute('onmousedown') === 'event.preventDefault()',
  '分隔线按钮 onmousedown 防抢选区');
A(doc.getElementById('charFormatBtn').getAttribute('onmousedown') === 'event.preventDefault()',
  '字符格式按钮 onmousedown 防抢选区');
EQ(doc.getElementById('cfFontSel').options.length, 11, '字体下拉 10 项 + 不修改');
// 弹窗注册在 4 张表里
A(appJs.indexOf("'charFormatModalBg'") !== -1, 'charFormatModalBg 在注册表中');
A((appJs.match(/'charFormatModalBg'/g) || []).length >= 5, 'charFormatModalBg 出现在全部 4 张表 + 绑定');
A((appJs.match(/'dividerMenu'/g) || []).length >= 5, 'dividerMenu 出现在关闭列表 + 绑定');

// 防回归（2026-09-27）：#dividerMenu 必须与另外三个下拉菜单共用那三条 CSS 规则的选择器。
// openDropMenu 只写 left/top + display:block，缺 position:fixed 时坐标被浏览器忽略，
// 菜单退回 static 块又被 sticky 的 #toolbar（z-index:20）盖住 → 点了像没反应。
// 负控：从任一条选择器里去掉 `#dividerMenu, ` 即必须变红。
// 锚点用 #proofreadMenu（整份 _UI_HTML 里只出现在这 3 条规则的选择器里）；
// 不能用三元组字面量——第 2/3 条是 `#proofreadMenu button, …`，三元组不连续。
{
  const rules = uiHtml.match(/#proofreadMenu[^{}]*\{[^{}]*\}/g) || [];
  const selHasDivider = (r) => !!r && /#dividerMenu\b/.test(r.slice(0, r.indexOf('{')));
  const LABELS = ['菜单本体', '菜单按钮', '按钮 hover'];
  EQ(rules.length, 3, '下拉菜单共用 CSS 规则仍为 3 条（本体 / 按钮 / 按钮:hover）');
  A(rules.length === 3 && rules.filter(selHasDivider).length === rules.length,
    '3 条规则的选择器都含 #dividerMenu（实得 ' + rules.filter(selHasDivider).length + '）');
  for (let k = 0; k < 3; k++) {
    A(selHasDivider(rules[k]), '第 ' + (k + 1) + ' 条规则（' + LABELS[k] + '）选择器含 #dividerMenu');
  }
  A(rules.length > 0 && /position:fixed/.test(rules[0].slice(rules[0].indexOf('{'))),
    '菜单本体规则仍带 position:fixed');
}
A(appJs.includes("_backdropClickClose('charFormatModalBg', closeCharFormatDialog)"), '遮罩点击关闭已绑定');
// 未新增快捷键
A(!/char_format|divider/.test(appJs.split('DEFAULTS = {')[1].split('};')[0]), 'DEFAULTS 未新增快捷键');
A(!/char_format|divider/.test(appJs.split('SHORTCUT_ACTIONS = {')[1].split('\n};')[0]), 'SHORTCUT_ACTIONS 未新增快捷键');

setTimeout(run, 120);

function blockOf(pageIdx, blockIdx) {
  const row = doc.querySelector('.page-row[data-i="' + pageIdx + '"]');
  return row ? row.querySelectorAll('.editable > p, .editable > div, .editable > h1, .editable > h2')[blockIdx] : null;
}
function editableOf(pageIdx) {
  const row = doc.querySelector('.page-row[data-i="' + pageIdx + '"]');
  return row ? row.querySelector('.editable') : null;
}
// 用**块内文本偏移**造选区（与生产代码同一套 _rangeFromOffsets 口径）。
// 不能假定 block.firstChild 是文本节点：施加格式后它可能是 span，且块内首个文本节点
// 带前导 \n（块间换行约定，2026-09-15），按 firstChild+offset 设界会抛 IndexSizeError。
function selectIn(block, from, to) {
  const r = X('_rangeFromOffsets')(block, { start: from, end: to });
  const sel = w.getSelection();
  sel.removeAllRanges();
  sel.addRange(r);
  return r;
}
    // 把值塞进弹窗。两处易踩的静默失效：
    //  1) cfSizeSel 的 option value 是**裸数字**（8/9/…/72），塞 '18px' 会让
    //     select.value 退化成 ''（无匹配项）；
    //  2) cfSizeNum 是 <input type="number">，赋 '18px' 会被**值净化算法**清空
    //     （真实 Chrome 与 jsdom 同款行为），只接受裸数字。
    // 所以统一剥掉 px 再赋值。color 传 null 时点「清除颜色」，把 _cfColor 真正归零。
    function setDecls(size, color) {
      const sel = doc.getElementById('cfSizeSel');
      const num = doc.getElementById('cfSizeNum');
      const v = String(size == null ? '' : size).replace(/px$/i, '');
      const has = !!(sel && Array.prototype.some.call(sel.options, (o) => o.value === v));
      if (has) { sel.value = v; num.value = ''; }
      else { sel.value = ''; num.value = v; }
      if (color) {
        const inp = doc.getElementById('cfColorInput');
        inp.value = color;
        inp.dispatchEvent(new w.Event('input', { bubbles: true }));
      } else {
        doc.getElementById('cfColorClearBtn').click();
      }
      return { 'font-size': v === '' ? '' : v + 'px', 'font-family': '', 'color': color || '' };
    }
    function _setDeclsDbg(size, color) {
      const r = setDecls(size, color);
      if (process.env.PTOE_DEBUG_DIV) console.log('    [dbgSD] size=' + JSON.stringify(size) +
        ' color=' + JSON.stringify(color) + ' -> num=' + JSON.stringify(doc.getElementById('cfSizeNum').value) +
        ' sel=' + JSON.stringify(doc.getElementById('cfSizeSel').value) +
        ' decls=' + JSON.stringify(X('_charFormatDecls')()));
      return r;
    }
    void _setDeclsDbg;   // 保留为排查用开关（设 PTOE_DEBUG_DIV=1 生效），默认不走

function run() {
  console.log('\n== 4. 行挂载前置断言 ==');
  A(!!editableOf(0), 'init 挂载了第 1 页（/api/pages 契约）');
  A(w.__errors.length === 0, '加载期无 JS 错误: ' + JSON.stringify(w.__errors));

  const ACS = X('applyCharStyleToBlock');
  const whole = (b) => { const r = doc.createRange(); r.selectNodeContents(b); return r; };

  console.log('\n== 5. 选区级字符格式 ==');
  {
    const b = blockOf(0, 0);
    const before = b.textContent;
    selectIn(b, 0, 3);
    const changed = ACS(b, w.getSelection().getRangeAt(0), { 'font-size': '20px', color: '#ff0000' });
    A(changed, '部分选区应用返回「已变化」');
    EQ(b.textContent, before, '文字内容不变');
    const sp = b.querySelectorAll('span[style]');
    EQ(sp.length, 1, '只生成 1 个 span（不嵌套）');
    EQ(sp[0].getAttribute('style'), 'font-size:20px;color:#ff0000', 'span style 规范化且有序');
    EQ(sp[0].textContent, before.slice(0, 3), 'span 覆盖且仅覆盖选中文字');
    // 幂等：同一区间重复施加相同声明 → innerHTML 逐字节不变
    const html1 = b.innerHTML;
    selectIn(b, 0, 3);
    ACS(b, w.getSelection().getRangeAt(0), { 'font-size': '20px', color: '#ff0000' });
    A(b.innerHTML === html1, '同区间重复施加同一格式不产生 churn');
    // 整块施加是**更大**的区间，理应扩展到未选中的文字（不是 churn）
    ACS(b, whole(b), { 'font-size': '20px' });
    A(b.textContent === before, '整块施加只改格式不改文字');
    A(b.querySelectorAll('span[style]').length === 1, '整块施加后合并为 1 个 span');
  }
  {
    const b = blockOf(0, 0);
    // 整块 + 已有 span → 合并而非嵌套
    ACS(b, whole(b), { 'font-family': '宋体, SimSun, serif' });
    const sp = b.querySelectorAll('span[style]');
    A(sp.length >= 1, '整块应用后有 span');
    for (const s of sp) A(!s.querySelector('span[style]'), 'span 内无嵌套 span');
    A(sp[0].getAttribute('style').indexOf('font-family:宋体, SimSun, serif') >= 0, '整块应用后字体保留');
    A(sp[0].getAttribute('style').indexOf('font-size:20px') >= 0, '整块应用保留原有字号（合并而非覆盖）');
  }
  {
    // 跨界选区跨越已有 span 边界 → 应切开而不是把外侧一起包进去
    const b = blockOf(0, 0);
    b.innerHTML = '<span style="color:red">甲乙</span>丙丁戊';
    const r = selectIn(b, 1, 4);                      // 「乙丙丁」
    ACS(b, r, { 'font-size': '30px' });
    const sp = b.querySelectorAll('span[style]');
    A(sp.length >= 2, '跨界选区产生多个 span');
    A(!sp[0].querySelector('span'), '无嵌套');
    // 只看**被本次施加格式**的 span：外侧原有的 color:red span 不该算进来
    const hit = [...sp].filter((s) => (s.getAttribute('style') || '').indexOf('font-size:30px') >= 0);
    EQ(hit.length, 1, '恰好 1 个 span 带新字号');
    EQ(hit[0].textContent, '乙丙丁', '仅选中文字被加字号（乙丙丁）');
    A([...sp].some((s) => s.textContent === '甲' && (s.getAttribute('style') || '') === 'color:red'),
      '外侧原有 color 未丢失且未被加字号');
    A(b.textContent === '甲乙丙丁戊', '切开不丢/不重文字');
  }
  {
    // 清除只删指定属性，保留 text-align
    const b = blockOf(0, 0);
    b.innerHTML = '<span style="text-align:right;font-size:20px;color:red">清我</span>';
    const r = whole(b);
    ACS(b, r, { 'font-size': '' });
    const s = b.querySelector('span[style]');
    EQ(s.getAttribute('style'), 'text-align:right;color:red', '清除字号保留对齐与颜色');
    ACS(b, r, { color: '', 'font-family': '' });
    EQ(b.querySelector('span[style]').getAttribute('style'), 'text-align:right', '再清颜色/字体后仅留对齐');
    ACS(b, r, { 'text-align': '' });
    A(b.querySelector('span[style]') === null, '无存活声明时 span 被解包（清除彻底）');
  }
  {
    // 分隔线块不参与字符格式
    const div = blockOf(0, 1);
    const before = div.innerHTML;
    A(ACS(div, whole(div), { 'font-size': '20px' }) === false, '分隔线块被跳过（返回 false）');
    EQ(div.innerHTML, before, '分隔线块 HTML 逐字节不变');
  }
  {
    // 空块 / 无文字块不改
    const b = doc.createElement('p');
    b.innerHTML = '<img src="data:image/png;base64,AA" alt="图">';
    A(ACS(b, whole(b), { 'font-size': '20px' }) === false, '纯图片块不参与字符格式');
  }

  console.log('\n== 6. 清除格式不破坏分隔线 ==');
  {
    const AP = X('applyOp');
    const div = blockOf(0, 1);
    const before = div.innerHTML;
    selectIn(div, 0, 8);
    try { AP('remove'); } catch (e) { A(false, 'applyOp(remove) 抛错: ' + e.message); }
    EQ(div.innerHTML, before, '清除格式后分隔线块逐字节不变（未被退化成方框字符）');
    EQ(div.textContent.replace(/\n/g, ''), GLY.solid.repeat(8), '分隔线字形仍为 8 个 ' + GLY.solid);
  }
  {
    // 清除格式仍应解包字符格式 span
    const AP = X('applyOp');
    const b = blockOf(0, 0);
    b.innerHTML = '<span style="font-size:20px">带字号的文字</span>';
    selectIn(b, 0, 6);
    try { AP('remove'); } catch (e) { A(false, 'applyOp(remove) 抛错: ' + e.message); }
    A(b.querySelector('span[style]') === null, '清除格式解包了字符格式 span');
  }

  console.log('\n== 7. 弹窗选中内容流程 ==');
  {
    const b = blockOf(0, 0);
    b.innerHTML = '<p>'.slice(0, 0) || '';
    b.innerHTML = '甲乙丙丁戊己';
    const OCF = X('openCharFormatDialog');
    const ACF = X('applyCharFormat');
    selectIn(b, 0, 3);
    OCF();
    EQ(doc.getElementById('charFormatModalBg').style.display, 'flex', '弹窗已打开');
    // 模拟弹窗内交互销毁选区（聚焦输入框）
    setDecls('24px', '#0000ff');
    const selNow = w.getSelection();
    selNow.removeAllRanges();
    const t = doc.createTextNode('别的页面的光标');
    doc.getElementById('cfSizeNum').appendChild(t);  // 占位，防空块
    t.remove();
    const ghost = doc.createRange();
    ghost.setStart(blockOf(0, 2).firstChild, 0);
    ghost.setEnd(blockOf(0, 2).firstChild, 0);
    selNow.addRange(ghost);   // 选区已跑到别的段
    ACF(false);
    EQ(doc.getElementById('charFormatModalBg').style.display, 'none', '确定后弹窗关闭');
    const sp = b.querySelectorAll('span[style]');
    A(sp.length >= 1, '弹窗交互后仍作用于原选区（选区快照生效）');
    let styled = '';
    sp.forEach((s) => { styled += s.textContent; });
    EQ(styled, '甲乙丙', '只作用于原选中文字（未退化成整块/整页）');
    EQ(b.textContent, '甲乙丙丁戊己', '全文未被截断');
  }
  {
    // 预览：style 串规范化后贴到示例段。钳制只能用自由数字框测——<select> 只有
    // 合法 option，赋非法值时 .value 会退化成 ''，测不到钳制分支。
    const UCF = X('updateCharFormatPreview');
    const num = doc.getElementById('cfSizeNum');
    const color = doc.getElementById('cfColorInput');
    // 清颜色走「清除颜色」按钮：_cfColor 是模块级变量，只有该按钮 / input 事件会同步它
    doc.getElementById('cfColorClearBtn').click();
    num.value = '999';
    doc.getElementById('cfSizeSel').value = '';
    UCF();
    const p = doc.getElementById('cfPreview').querySelector('p');
    EQ(p.getAttribute('style'), 'font-size:72px', '预览按钳制值显示（999 → 72px）');
    // 「不修改」：字号与颜色都清空才应完全不写 style
    num.value = '';
    UCF();
    A(p.getAttribute('style') === null, '「不修改」时预览不写 style 属性');
    // 只设颜色 → 只写 color
    color.value = '#00ff00';
    color.dispatchEvent(new w.Event('input', { bubbles: true }));
    UCF();
    EQ(p.getAttribute('style'), 'color:#00ff00', '仅颜色时预览只写 color');
    doc.getElementById('cfColorClearBtn').click();
    UCF();
  }
  {
    // 快速清除按钮 = 选中内容语义清除
    const CCFQ = X('clearCharFormatQuick');
    const b = blockOf(0, 0);
    b.innerHTML = '<span style="text-align:right;font-size:20px;color:red">要清干净</span>';
    const r = doc.createRange();
    r.selectNodeContents(b);
    const sel = w.getSelection(); sel.removeAllRanges(); sel.addRange(r);
    doc.getElementById('cfModeSel').checked = true;
    CCFQ();
    A(b.querySelector('span[style]') === null || b.querySelector('span[style]').getAttribute('style') === 'text-align:right',
      '快速清除移除字号/颜色但保留对齐');
  }

  console.log('\n== 8. 当前页 / 全部范围 ==');
  {
    const ACF = X('applyCharFormat');
    const b0 = blockOf(0, 0), b2 = blockOf(0, 2);
    b0.innerHTML = '页内甲'; b2.innerHTML = '页内丙';
    doc.getElementById('cfModePage').checked = true;
    setDecls('18', null);
    if (process.env.PTOE_DEBUG_DIV) {
      const bg = document.getElementById('charFormatModalBg');
      const te = X('_cfTargetEd')();
      console.log('    [dbg8] mode=' + X('charFormatMode')() + ' modalDisplay=' + JSON.stringify(bg && bg.style.display) +
        ' targetEd=' + (te ? (te.closest('.page-row') && te.closest('.page-row').dataset.i) : 'null') +
        ' viewportPage=' + X('viewportPage')() + ' lastFocused=' + (X('currentEditable')() ? 'yes' : 'null'));
    }
    ACF(false);
    A(b0.querySelector('span[style]') !== null, '当前页：块 1 已应用');
    A(b2.querySelector('span[style]') !== null, '当前页：块 3 已应用（隔过分隔线）');
    const div = blockOf(0, 1);
    A(div.querySelector('span[style]') === null, '当前页：分隔线块未被应用');
    const other = blockOf(1, 0);
    A(other.querySelector('span[style]') === null, '当前页模式不越界到第 2 页');
  }
  {
    // 全部：确认 + 一次撤销
    const ACF = X('applyCharFormat');
    const UH = X('undoHistory');
    const stack = X('undoStack');          // 是数组本身，不是函数
    const before = stack.length;
    doc.getElementById('cfModeAll').checked = true;
    setDecls('30', null);        // 30 不在下拉 option 里 → 走自由数字框
    const origConfirm = w.confirm;
    w.confirm = () => true;
    ACF(false);
    w.confirm = origConfirm;
    A(blockOf(1, 0).querySelector('span[style]') !== null, '全部：第 2 页也应用了');
    A(blockOf(1, 1).querySelector('span[style]') !== null, '全部：第 2 页第二个块也应用了');
    EQ(stack.length, before + 1, '全部范围只压入 1 步撤销');
    UH();
    A(blockOf(1, 0).querySelector('span[style]') === null, '一次撤销回退全部范围');
    // 第 1 页块 1 在本用例之前已被「当前页」用例上过 18px —— 撤销只回到 histBegin
    // 那一刻的快照，18px 理应还在；要断言的是 30px 被撤掉。
    A(!(blockOf(0, 0).querySelector('span[style*="font-size:30px"]')), '一次撤销同时回退第 1 页');
  }
  {
    // 全部范围需确认；取消则零改动
    const ACF = X('applyCharFormat');
    const b = blockOf(0, 0);
    b.innerHTML = '取消测试';
    doc.getElementById('cfModeAll').checked = true;
    setDecls('40', null);
    const origConfirm = w.confirm;
    w.confirm = () => false;
    ACF(false);
    w.confirm = origConfirm;
    A(b.querySelector('span[style]') === null, '全部范围取消确认后零改动');
    doc.getElementById('cfModeSel').checked = true;
  }

  console.log('\n== 9. 插入分隔线 ==');
  {
    const IDC = X('insertDividerAtCaret');
    const ed = editableOf(0);
    const target = blockOf(0, 0);
    target.innerHTML = '插前测试';
    ed.focus();
    const sel = w.getSelection();
    const r = doc.createRange();
    r.setStart(target.firstChild, 0);   // 段首
    r.collapse(true);
    sel.removeAllRanges(); sel.addRange(r);
    const made = IDC('dashed');
    A(!!made, '插入返回新分隔线块');
    EQ(made.className, 'ptoe-divider ptoe-divider-dashed', '插入的分隔线 class 正确');
    EQ(made.textContent, GLY.dashed.repeat(8), '插入的分隔线字形正确');
    A(made.previousElementSibling === null || made.previousElementSibling !== target, '段首插入时位于该段之前');
    A(target.nextElementSibling === made || made.nextElementSibling === target, '分隔线与原段相邻');
  }
  {
    const IDC = X('insertDividerAtCaret');
    const ed = editableOf(1);
    const target = blockOf(1, 0);
    target.innerHTML = '段中测试';
    ed.focus();        // currentEditable() 里 activeElement 优先，真实浏览器下
                       // 选区所在编辑区必然也是焦点所在，必须对齐这个前提
    const sel = w.getSelection();
    const r = doc.createRange();
    r.setStart(target.firstChild, 2);   // 段中
    r.collapse(true);
    sel.removeAllRanges(); sel.addRange(r);
    const made = IDC('double');
    if (process.env.PTOE_DEBUG_DIV) console.log('    [dbg] ed=' + [...ed.children].map((e) => e.tagName + '.' + e.className + ':' + JSON.stringify(e.textContent)).join(' | ') +
      '\n    [dbg] target=' + JSON.stringify(target.outerHTML) +
      '\n    [dbg] made=' + (made && made.outerHTML) +
      '\n    [dbg] madeInEd=' + (made ? ed.contains(made) : 'null'));
    A(!!made, '段中插入成功');
    A(made.previousElementSibling === target || target.nextElementSibling === made, '段中插入紧随该段之后');
    EQ(made.textContent, GLY.double.repeat(8), '双线字形正确');
  }
  {
    // 插入后内容可保存（syncContent 已跑）
    const PS = X('pageSource');
    const i = 0;
    const src = PS(i);
    A(src.indexOf('ptoe-divider') >= 0, '插入的分隔线进入 pageSource（可保存）');
  }

  // 真·负控：对 ui/app.js 源码做变异后重跑本 harness，断言**必须失败**。
  // 变异源用临时副本，绝不碰仓库文件；PTOE_NEG_MUTATE 防止子进程递归。
  if (!process.env.PTOE_NEG_MUTATE) runNegativeControls();
  else console.log('\n== 10. 负控子进程（变异源，预期失败）==\n===== ' + pass + ' passed, ' + fail + ' failed =====');

  console.log('\n===== ' + pass + ' passed, ' + fail + ' failed =====');
  if (fail) { console.log('FAILED:'); fails.forEach((f) => console.log('  - ' + f)); }
  // 全绿时也要显式退出：jsdom 的 rAF/timer 会挂住事件循环，
  // 负控子进程会被父进程按 timeout 杀掉（exit null），白白耗掉几分钟。
  process.exit(fail ? 1 : 0);
}

// 真·负控：把 ui/app.js 里的某个片段替换成错误实现，重跑整套断言，断言退出码非 0。
// 目的：证明本 harness 的断言真的会因为实现被改坏而变红，而不是恒真。
function runNegativeControls() {
  const { spawnSync } = require('child_process');
  const orig = fs.readFileSync(APP_JS, 'utf8');
  const MUTANTS = [
    ['NEG1 分隔线字形 8 → 7', 'new Array(9).join(ch)', 'new Array(8).join(ch)'],
    ['NEG2 字号声明被整体丢弃', "d['font-size'] = px === ''", "d['__probe'] = px === ''"],
    // 注意：NEG3 要连**两个**分支一起废掉——只废选区快照那条，光标快照仍会把
    // 整段选区恢复回去，行为不变（假负控）。直接废掉判据函数最彻底。
    ['NEG3 选区快照恢复整体失效（退化成整块）',
      'return _cfSnapshotAlive(snap) && snap.ed === ed;', 'return false;'],
    // NEG4 必须打 applyCharStyleToBlock 内部那道闸（:2451），只废调用点的检查
    // 会被内部守卫兜住 → 行为不变。
    ['NEG4 分隔线不跳过（去掉 applyCharStyleToBlock 内部守卫）',
      'if (!block || isDividerBlock(block)) return false;', 'if (!block) return false;'],
    ['NEG5 字号钳制失效（999 不钳到 72）', 'Math.min(CHAR_SIZE_MAX, px)', 'Math.min(9999, px)'],
  ];
  console.log('\n== 10. 真·负控（变异 ui/app.js 后本 harness 必须失败）==');
  const tmpDir = path.join(process.env.TEMP || '.', 'opencode', 'negmut');
  fs.mkdirSync(tmpDir, { recursive: true });
  MUTANTS.forEach(([name, from, to], k) => {
    if (orig.indexOf(from) < 0) { A(false, name + ' — 变异锚点未在源码中找到（源码已变，负控失效）'); return; }
    const f = path.join(tmpDir, 'mut' + k + '.js');
    fs.writeFileSync(f, orig.split(from).join(to), 'utf8');
    const r = spawnSync(process.execPath, [__filename], {
      encoding: 'utf8',
      env: Object.assign({}, process.env, { PTOE_APP_JS: f, PTOE_NEG_MUTATE: '1' }),
      timeout: 240000,
    });
    const out = (r.stdout || '') + (r.stderr || '');
    // 子进程输出形如 "  FAIL …"（两个前导空格），要按 /^\s*FAIL / 匹配
    const got = out.split('\n').filter((l) => /^\s*FAIL /.test(l)).map((l) => l.trim().slice(0, 96));
    // 必须因**断言失败**而红，而不是 TypeError 崩掉/超时被杀——崩掉证明不了断言有效
    A(r.status === 1 && got.length > 0, name + ' → 变异后至少 1 条断言 FAIL（exit ' + r.status + '，' + got.length + ' 条）');
    console.log('      变异后失败断言 ' + got.length + ' 条，例如：' + (got[0] || '(无，exit ' + r.status + '：' + out.split('\n').filter((l) => /Error|timed out/i.test(l))[0] + ')'));
    fs.unlinkSync(f);
  });
}
