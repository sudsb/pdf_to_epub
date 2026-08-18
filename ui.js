
'use strict';
const BUFFER = 15, GAP = 12;
// 内存换平滑（2026-08）：滚动方向前方额外预挂载 PRELOAD 行，图片提前加载、行高提前稳定——
// 滚到那里时高度已测量，滚动中几乎不再触发补偿（减少跳变诱因）。代价：常驻 DOM/预览图内存略增（可接受）。
const PRELOAD = 15;
let _scrollDir = 1, _viewportY = 0, _lastLo = 0; // 最近一次滚动方向（1 向下 / -1 向上）、视口位置与上次窗口下界，供 updateViewport 动态预挂载/空白兜底
const OPS = [
  ['bold','粗体'], ['italic','斜体'], ['heading','标题'], ['p','正文'],
  ['remove','清除格式'], ['note','注释'],
  ['align_left','居左'], ['align_center','居中'], ['align_right','居右'],
  ['marker_full','全文标记'], ['marker_note','注释标记'], ['marker_join','段落标记'],
  ['marker_page','换页标记'],
  // 工具操作（无需 currentEditable，直接调用）
  ['search','搜索'], ['clean','智能清理'], ['convert_t2s','繁→简'], ['convert_s2t','简→繁'],
  ['toggle_md','Markdown模式'], ['undo','撤销'], ['redo','重做'], ['history','历史记录'],
  ['export','导出'], ['save','保存'], ['stage','暂存'], ['finish','完成并转换'],
  ['jump','跳转'], ['help','帮助'], ['settings','快捷键设置'],
  ['proofread_correct','校正'], ['proofread_reocr','重识别'], ['proofread_apply','应用'],
  ['proofread_clear','清除标注'], ['proofread_revert','回退'],
  ['proofread_accept', '采纳纠错'], ['proofread_ignore', '忽略纠错'],
];
const OP_ICON = {
  bold:'<span class="ic-b">B</span>', italic:'<span class="ic-i">I</span>',
  heading:'<span class="ic-h">标</span>', p:'<span class="ic-p">正</span>',
  remove:'<span class="ic-t">清</span>', note:'注',
  align_left:'左', align_center:'中', align_right:'右',
  marker_full:'篇', marker_note:'释', marker_join:'段', marker_page:'页'
};
const OP_TIP = {
  bold:'粗体', italic:'斜体', heading:'标题（循环 H1→H6→正文）', p:'正文',
  remove:'清除格式', note:'注释格式（整段小字）',
  align_left:'居左', align_center:'居中', align_right:'居右',
  marker_full:'全文标记（文章到此结束，开新页）',
  marker_note:'注释标记（由对应注释段落替换）',
  marker_join:'段落标记（段首合上段，段尾合下段）',
  marker_page:'换页标记（从此处之后的内容显示在新的一页）',
  proofread_accept: '采纳纠错（替换为候选字）', proofread_ignore: '忽略纠错（消除标注）',
};
const DEFAULTS = {
  bold:'Ctrl+B', italic:'Ctrl+I', heading:'Ctrl+1', p:'Ctrl+0',
  note:'Ctrl+Shift+N',
  align_left:'Ctrl+Shift+Left', align_center:'Ctrl+Shift+Up', align_right:'Ctrl+Shift+Right',
  marker_full:'Ctrl+Shift+F', marker_note:'Ctrl+Shift+M', marker_join:'Ctrl+Shift+J',
  marker_page:'Ctrl+Shift+P',
  // 工具操作默认快捷键
  search:'Ctrl+F', clean:'Ctrl+Shift+C', convert_t2s:'Ctrl+Shift+T', convert_s2t:'Ctrl+Shift+Y',
  toggle_md:'Ctrl+Shift+D', undo:'Ctrl+Z', redo:'Ctrl+Y', history:'Ctrl+H',
  export:'Ctrl+E', save:'Ctrl+S', stage:'Ctrl+Shift+S', finish:'Ctrl+Enter',
  jump:'Ctrl+G', help:'F1', settings:'Ctrl+Shift+O',
  proofread_correct:'Ctrl+K', proofread_reocr:'Ctrl+Shift+R', proofread_apply:'Ctrl+Shift+A',
  proofread_clear:'Ctrl+Shift+X', proofread_revert:'Ctrl+Shift+Z',
  proofread_accept: 'Enter', proofread_ignore: 'Escape',
};
let pages = [];
let contentMap = new Map();     // index -> 该行最近一次 innerHTML（虚拟列表离屏保留）
let editedSet = new Set();
let dirty = false;
let mdMode = false;             // Markdown 源码模式
let mdSourceMap = new Map();    // index -> markdown 源码（仅 md 模式使用）
let loadNonce = 0;              // 历史版本载入计数：图片 URL 加 ?v= 防换书后缓存错图
const imgAspect = {};           // page -> "W / H"（首帧加载后缓存，重挂载/换全幅图不再改变行高）
let loadedTitle = null;         // 当前打开的历史记录名（无文件模式下作为 EPUB 标题）
const heights = new Array(0);
let est = 420;
let bindings = loadBindings();
const host = document.getElementById('pages');
const popup = document.getElementById('popup');
let capturingOp = null;
let suppressPopupUntil = 0;  // 操作按钮点击后的抑制窗口：选中菜单不再自动弹出
const tipEl = document.getElementById('tip');   // 全局延迟提示（悬停超时后显示）
let tipTimer = null;                             // 提示显示计时器
let tipAnchor = null;                            // 当前悬停元素（供定时器回调定位）

// 提示延迟（毫秒）：悬停超过该时间才显示提示文字；0 = 立即显示（localStorage 可配置）
function tipDelay() { return loadInt('ptoe_tip_delay', 600); }
// 提示文字：操作说明 + 对应快捷键（若有绑定）
function tipTextFor(op) {
  const combo = bindings[op];
  if (combo) return OP_TIP[op] + '<span class="tip-key">(' + combo + ')</span>';
  return OP_TIP[op];
}
function positionTip(anchor) {
  const r = anchor.getBoundingClientRect();
  const tw = tipEl.offsetWidth, th = tipEl.offsetHeight;
  let x = r.left + r.width / 2 - tw / 2;
  x = Math.max(4, Math.min(x, window.innerWidth - tw - 4));  // 防左右溢出
  let y = r.bottom + 8;
  if (y + th > window.innerHeight - 4) y = r.top - th - 8;   // 下方放不下则显示在按钮上方
  tipEl.style.left = x + 'px';
  tipEl.style.top = y + 'px';
}
function scheduleTip(e) {
  const anchor = e.currentTarget;
  const op = anchor.dataset.op;
  if (!op || !OP_TIP[op]) return;
  clearTimeout(tipTimer);
  tipAnchor = anchor;
  tipTimer = setTimeout(function () {
    if (!tipAnchor) return;
    tipEl.innerHTML = tipTextFor(op);
    tipEl.style.display = 'block';
    positionTip(tipAnchor);
  }, tipDelay());
}
function hideTip() {
  clearTimeout(tipTimer);
  tipAnchor = null;
  tipEl.style.display = 'none';
}

function loadBindings() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem('ptoe_shortcuts') || '{}'); } catch (e) {}
  return Object.assign({}, DEFAULTS, saved);
}
// 服务端持久化（config.json shortcuts）：随机端口下 localStorage 每次运行失效，
// 故 init 时异步拉取服务端设置覆盖内存 bindings（服务端为准）；localStorage 仅作同步兜底。
async function loadBindingsFromServer() {
  try {
    const res = await fetchJSON('/api/shortcuts');
    if (!res || !res.ok) return;
    const sc = res.shortcuts;
    if (!sc || typeof sc !== 'object' || !Object.keys(sc).length) return;
    bindings = Object.assign({}, DEFAULTS, sc);
    try { localStorage.setItem('ptoe_shortcuts', JSON.stringify(bindings)); } catch (e) {}
    // 设置弹窗已打开时刷新表格（keydown 分发在派发时读 bindings 变量，无需额外处理）
    const bg = document.getElementById('modalBg');
    if (bg && bg.style.display === 'flex') renderShortcutTable();
  } catch (e) { console.warn('loadBindingsFromServer failed: ' + e.message); }
}
function saveBindings() {
  try { localStorage.setItem('ptoe_shortcuts', JSON.stringify(bindings)); } catch (e) {}
  // fire-and-forget 持久化到 config.json（失败静默，localStorage 仍生效）
  fetch('/api/shortcuts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ shortcuts: bindings }),
  }).catch(function () {});
}
function reverseBindings() { const m = {}; for (const op in bindings) if (bindings[op]) m[bindings[op]] = op; return m; }
function loadBool(key) { try { return localStorage.getItem(key) === '1'; } catch (e) { return false; } }
function saveStr(key, v) { try { localStorage.setItem(key, String(v)); } catch (e) {} }
function loadInt(key, def) { try { const v = parseInt(localStorage.getItem(key), 10); return isFinite(v) ? v : def; } catch (e) { return def; } }
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(url + ' -> ' + r.status);
  return r.json();
}

// ---------- Markdown 源码 <-> HTML ----------
function inlineToMd(t) {
  return String(t)
    .replace(/<strong>(.*?)<\/strong>/gi, function(m, x) { return '**' + x + '**'; })
    .replace(/<em>(.*?)<\/em>/gi, function(m, x) { return '*' + x + '*'; })
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'").replace(/&amp;/g, '&');
}
function htmlToMd(html) {
  // 已清洗 HTML（p/h1-6/strong/em/br/span）→ markdown 源码；标记 span 原样保留
  let s = String(html || '');
  s = s.replace(/<br\s*\/?>/gi, '\n');
  s = s.replace(/<h([1-6])([^>]*)>([\s\S]*?)<\/h\1>/gi, function(m, l, a, inner) {
    return new Array(Number(l) + 1).join('#') + ' ' + inlineToMd(inner).trim() + '\n\n';
  });
  s = s.replace(/<p([^>]*)>([\s\S]*?)<\/p>/gi, function(m, a, inner) { return inlineToMd(inner).trim() + '\n\n'; });
  s = inlineToMd(s);
  s = s.replace(/\n{3,}/g, '\n\n');
  return s.trim();
}
function inlineMd(t) {
  // 行内 Markdown → HTML：md 记号转标签，原样 HTML（标记 span）放行，
  // 其余文本转义（防止裸 < 破坏下游解析）
  const out = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*\s][^*]*\*)|(_[^_\s][^_]*_)|(\[[^\]]+\]\([^)]+\))|(<[^>]+>)/g;
  let last = 0, m;
  const s = String(t);
  while ((m = re.exec(s))) {
    if (m.index > last) out.push(esc(s.slice(last, m.index)));
    if (m[1]) out.push('<code>' + m[1].slice(1, -1) + '</code>');
    else if (m[2]) out.push('<strong>' + m[2].slice(2, -2) + '</strong>');
    else if (m[3]) out.push('<strong>' + m[3].slice(2, -2) + '</strong>');
    else if (m[4]) out.push('<em>' + m[4].slice(1, -1) + '</em>');
    else if (m[5]) out.push('<em>' + m[5].slice(1, -1) + '</em>');
    else if (m[6]) { const i2 = m[6].indexOf(']('); out.push('<a href="' + m[6].slice(i2 + 2, -1) + '">' + m[6].slice(1, i2) + '</a>'); }
    else if (m[7]) out.push(m[7]);
    last = m.index + m[0].length;
  }
  if (last < s.length) out.push(esc(s.slice(last)));
  return out.join('');
}
function mdToHtml(md) {
  // markdown 源码 → HTML（仅用现有白名单标签；列表/引用/代码块按段落输出）
  const lines = String(md || '').split('\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (/^```/.test(line)) {
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(esc(lines[i])); i++; }
      i++;
      out.push('<p>' + buf.join('<br/>') + '</p>');
      continue;
    }
    if (/^<(p|h[1-6]|div)(\s|>)/i.test(line)) { out.push(line); i++; continue; }
    let m = line.match(/^(#{1,6})\s+(.*)$/);
    if (m) { const lv = m[1].length; out.push('<h' + lv + '>' + inlineMd(m[2]) + '</h' + lv + '>'); i++; continue; }
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) { buf.push(inlineMd(lines[i].replace(/^>\s?/, ''))); i++; }
      out.push('<p>' + buf.join('<br/>') + '</p>');
      continue;
    }
    if (/^(\s*)([-*+]|\d+\.)\s+/.test(line)) {
      while (i < lines.length && /^(\s*)([-*+]|\d+\.)\s+/.test(lines[i])) {
        out.push('<p>' + inlineMd(lines[i].replace(/^(\s*)([-*+]|\d+\.)\s+/, '')) + '</p>');
        i++;
      }
      continue;
    }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^```/.test(lines[i]) && !/^#{1,6}\s/.test(lines[i]) && !/^>\s?/.test(lines[i]) && !/^(\s*)([-*+]|\d+\.)\s+/.test(lines[i]) && !/^<(p|h[1-6]|div)(\s|>)/i.test(lines[i])) {
      buf.push(inlineMd(lines[i]));
      i++;
    }
    out.push('<p>' + buf.join('<br/>') + '</p>');
  }
  return out.join('\n');
}
function editableSource(ed) {
  // 源码模式：取编辑区各子块/文本节点的纯文本（按行拼接）
  const lines = [];
  for (const c of ed.childNodes) {
    if (c.nodeType === 3) lines.push(c.textContent);
    else if (c.tagName === 'BR') lines.push('');
    else lines.push(c.textContent || '');
  }
  return lines.join('\n');
}
function displayHtml(i) {
  let base;
  if (!mdMode) base = contentMap.has(i) ? contentMap.get(i) : pages[i].text;
  else {
    const src = mdSourceMap.has(i) ? mdSourceMap.get(i) : htmlToMd(pages[i].text);
    base = String(src).split('\n').map(function(l) { return '<div>' + esc(l) + '</div>'; }).join('');
  }
  // If there's an active search highlight query, inject highlights into the
  // rendered HTML. Do NOT mutate underlying stored source (collect/pageSource
  // uses raw content). Regex validity already handled upstream; guard anyway.
  if (_searchHighlightQuery) {
    try {
      const re = searchRegexFor(_searchHighlightQuery);
      return _highlightInHtmlSource(base, re);
    } catch (e) {
      return base;
    }
  }
  return base;
}
function pageSource(i) {
  if (mdMode) return mdSourceMap.has(i) ? mdSourceMap.get(i) : htmlToMd(pages[i].text);
  return contentMap.has(i) ? contentMap.get(i) : pages[i].text;
}
// 保存兜底：剥掉残留的纠错标注（.ptoe-fix 整体删除、.ptoe-err 解包保留原文），
// 但保留 .ptoe-marker（标记是有意义的内容）。防止「清除后保存 → 载入历史版本
// 建议文字跟在原文后面复现」（2026-08-09 修复）。与 _plainNoAnno 不同：那个会连
// 标记一起剥掉，且返回纯文本。
function stripProofreadMarkup(html) {
  const s = String(html == null ? '' : html);
  if (s.indexOf('ptoe-err') < 0 && s.indexOf('ptoe-fix') < 0) return s; // 无标注快速返回
  const d = document.createElement('div');
  d.innerHTML = s;
  d.querySelectorAll('.ptoe-fix').forEach(function (el) { el.parentNode.removeChild(el); });
  d.querySelectorAll('.ptoe-err').forEach(function (el) {
    const t = document.createTextNode(el.textContent);
    el.parentNode.replaceChild(t, el);
  });
  d.normalize();
  return d.innerHTML;
}
function collect() {
  const out = [];
  for (let i = 0; i < pages.length; i++) {
    const src = pageSource(i);
    const html = mdMode ? mdToHtml(src) : stripProofreadMarkup(src);
    out.push({ page: pages[i].page, html: html });
  }
  return out;
}
function collectProofread() {
  // 收集非空纠错状态（key 均为 str(页码)）；dismissed Set → 数组
  const errors = {}, original = {}, dismissed = {};
  for (let i = 0; i < pages.length; i++) {
    const pageNo = pages[i].page;
    const errs = proofreadErrors[i];
    if (errs && errs.length) {
      const active = errs.filter(function (e) { return !e._gone; });
      if (active.length) errors[String(pageNo)] = JSON.parse(JSON.stringify(active));
    }
    if (proofreadOriginal[i]) original[String(pageNo)] = proofreadOriginal[i];
    if (proofreadDismissed[i] && proofreadDismissed[i].size) {
      dismissed[String(pageNo)] = Array.from(proofreadDismissed[i]);
    }
  }
  return { errors: errors, original: original, dismissed: dismissed };
}

// ---------- 虚拟列表 ----------
// P4：前缀高度数组 prefixH（prefixH[i] = 前 i 行累计高度），配合二分查找，
// 替代 O(n) 逐行累加 —— 滚动/布局每次 O(1)，千页级不卡顿。
const prefixH = [0];
function rebuildPrefix() {
  let s = 0;
  prefixH[0] = 0;
  for (let k = 0; k < pages.length; k++) {
    s += heights[k] || est;
    prefixH[k + 1] = s;
  }
}
function prefixTop(i) { return i <= 0 ? 0 : (prefixH[i] != null ? prefixH[i] : i * est); }
function totalHeight() { return prefixTop(pages.length); }

function updateStatus() {
  updatePrCount();
  let extra = '';
  const ed = currentEditable();
  if (ed) {
    const row = ed.closest('.page-row');
    if (row) {
      const i = Number(row.dataset.i);
      const t = (ed.textContent || '').trim();
      extra = ' ｜ 字符 ： ' + t.length;
    }
  }
  document.getElementById('status').textContent =
    '已编辑 ' + editedSet.size + '/' + pages.length + (dirty ? '（未保存）' : '') + extra;
}
function setStatus(s) { document.getElementById('status').textContent = s; }
function updatePrCount() {
  const el = document.getElementById('prCountNum');
  if (!el) return;
  const ed = currentEditable();
  if (!ed) { el.textContent = '0'; return; }
  const row = ed.closest('.page-row');
  if (!row) { el.textContent = '0'; return; }
  const i = Number(row.dataset.i);
  const errors = proofreadErrors[i];
  if (!errors || !errors.length) { el.textContent = '0'; return; }
  let n = 0;
  for (const e of errors) { if (!e._gone) n++; }
  el.textContent = String(n);
}
function markDirty(i) {
  if (i >= 0 && !editedSet.has(i)) editedSet.add(i);
  dirty = true; updateStatus();
}
function syncContent(ed) {
  const row = ed.closest('.page-row');
  if (!row) return;
  const i = Number(row.dataset.i);
  if (mdMode) mdSourceMap.set(i, editableSource(ed));
  else contentMap.set(i, ed.innerHTML);
}
function currentEditable() {
  const a = document.activeElement;
  if (a && a.classList && a.classList.contains('editable')) return a;
  const sel = window.getSelection();
  if (sel && sel.rangeCount) {
    let n = sel.anchorNode;
    if (n) {
      if (n.nodeType !== 1) n = n.parentElement;
      const ed = n && n.closest ? n.closest('.editable') : null;
      if (ed) return ed;
    }
  }
  return null;
}

function pageRow(p, i) {
  const row = document.createElement('div');
  row.className = 'page-row';
  row.dataset.i = i;
  row.innerHTML =
    '<div class="page-head">第 ' + p.page + ' 页</div>' +
    '<div class="img-panel"><span class="badge">预览</span>' +
    '<button type="button" class="img-insert" title="插入图片到右侧文字光标处（居中；显示模式见工具栏「图片」）">图</button>' +
    '<button type="button" class="img-crop" title="裁剪左侧图片后插入到右侧文字光标处">裁</button>' +
    '<img decoding="async" src="/preview/' + p.page + '?v=' + loadNonce + '" alt="第' + p.page + '页原图"></div>' +
    '<div class="editable" contenteditable="true" spellcheck="false" aria-label="第 ' + p.page + ' 页文字" role="textbox" aria-multiline="true"></div>';
  const ed = row.querySelector('.editable');
  ed.innerHTML = displayHtml(i);
  _reapplyProofread(i);
  ed.addEventListener('input', (ev) => { syncContent(ed); markDirty(i); scheduleRemeasure(i); histTouchInput(i); histScheduleIdle(); const composing = window.isComposing || (ev && (ev.isComposing || ev.inputType === 'insertCompositionText')); if (composing) { _proofreadAutoDismiss(ed, i, true); _prRenderPending[i] = true; } else { _proofreadAutoDismiss(ed, i); } });
  // 撤销/重做操作起点：beforeinput（现代浏览器，含 IME/粘贴/拖放/键盘）为主，
  // keydown（可打印键/退格/删除/回车）与 compositionstart/paste 作兼容兜底；
  // 均在 DOM 变更前触发，可捕获操作前快照。重复触发无副作用（幂等）。
  ed.addEventListener('beforeinput', () => { histBeginInput(i); });
  ed.addEventListener('keydown', (ev) => {
    if (ev.isComposing || ev.keyCode === 229) return;
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const k = ev.key;
    if (k === 'Backspace' || k === 'Delete' || k === 'Enter' || (k && k.length === 1)) histBeginInput(i);
  });
  ed.addEventListener('compositionstart', () => { histBeginInput(i); });
  ed.addEventListener('paste', () => { histBeginInput(i); });
  ed.addEventListener('focus', updateStatus);
  ed.addEventListener('blur', updateStatus);
  const insBtn = row.querySelector('.img-insert');
  insBtn.addEventListener('click', () => insertImage(row, i));
  const cropBtn = row.querySelector('.img-crop');
  cropBtn.addEventListener('click', () => openCrop(row, i));
  const img = row.querySelector('img');
  const v = loadNonce;
  // 已知宽高比时先占位：重挂载/预览↔原图切换不再改变行高（避免视口内行突然长高 → 下方内容下移 → 跳页）
  if (imgAspect[p.page]) img.style.aspectRatio = imgAspect[p.page];
  img.onload = () => {
    // 首帧加载后缓存宽高比并占位；随后批量测量（行高变化即时补偿 scrollY，视口保持贴附）
    if (img.naturalWidth > 0 && img.naturalHeight > 0) {
      const ar = img.naturalWidth + ' / ' + img.naturalHeight;
      imgAspect[p.page] = ar;
      img.style.aspectRatio = ar;
    }
    scheduleRemeasure(i);
  };
  // onerror 防循环：预览失败 → 尝试原图；原图也失败 → 显示「加载失败」不再请求
  img.onerror = () => {
    const badge = row.querySelector('.badge');
    if (!img.classList.contains('full')) {
      img.classList.add('full');
      img.src = '/full/' + p.page + '?v=' + v;
      if (badge) badge.textContent = '原图';
    } else if (badge) {
      badge.textContent = '加载失败';
    }
  };
  img.onclick = () => {
    const badge = row.querySelector('.badge');
    if (img.classList.contains('full')) {
      img.src = '/preview/' + p.page + '?v=' + v; img.classList.remove('full'); badge.textContent = '预览';
    } else {
      img.src = '/full/' + p.page + '?v=' + v; img.classList.add('full'); badge.textContent = '原图';
    }
  };
  return row;
}

function insertImage(row, i) {
  const img = row.querySelector('img');
  if (!img) return;
  fetch(img.src)
    .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.blob(); })
    .then((blob) => new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve({ dataUrl: fr.result, size: blob.size });
      fr.onerror = () => reject(new Error('读取图片失败'));
      fr.readAsDataURL(blob);
    }))
    .then(({ dataUrl, size }) => insertImageDataUrl(dataUrl, size, i))
    .catch((e) => showToast('插入图片失败：' + e.message, 'fail'));
}

// 把 dataUrl 图片插入到第 i 页文字光标处（整页图插入 / 外部插入 / 裁剪插入共用）。
// modeOverride 可选：显式指定插入模式（full/fit/inline），缺省读 imgModeSel 下拉框。
function insertImageDataUrl(dataUrl, size, i, modeOverride) {
  let ed = null;
  if (i != null) {
    const row = host.querySelector('.page-row[data-i="' + i + '"]');
    ed = row ? row.querySelector('.editable') : null;
  }
  if (!ed) ed = currentEditable();
  if (!ed) { showToast('未找到可插入的编辑区', 'fail'); return; }
  if (i == null) {
    const row = ed.closest('.page-row');
    i = row ? Number(row.dataset.i) : 0;
  }
  const mode = modeOverride || document.getElementById('imgModeSel').value;
  // 插入图片：mode 决定全画幅/局部/行内。
  // 全画幅=整块居中占满行宽（默认 w100）；局部=整块按原尺寸居中；
  // 行内=裸 <img> 嵌在文字光标处（50% 宽度），文字环绕。
  const isInline = mode === 'inline';
  let html;
  if (isInline) {
    html = '<img class="ptoe-img-inline ptoe-img-w50" src="' + dataUrl + '" alt="插图"/>';
  } else {
    const imgClass = mode === 'full' ? ' class="ptoe-img-w100"' : '';
    html = '<p class="ptoe-img-' + mode + ' ptoe-img-center"><img' + imgClass + ' src="' + dataUrl + '" alt="插图"/></p>';
  }
  const before = histBegin('插入图片', [i]);
  ed.focus();
  inDiscreteOp = true;
  try {
    withScrollStable(() => {
      // 恢复最近一次在 .editable 内的选区，使图片插入到光标处（而非末尾）
      if (_lastEditableRange) {
        const sel = window.getSelection();
        sel.removeAllRanges();
        try { sel.addRange(_lastEditableRange); } catch (e) { /* 选区已失效则回退到末尾 */ }
      }
      if (mdMode) {
        document.execCommand('insertText', false, html);
      } else if (!document.execCommand('insertHTML', false, html)) {
        ed.appendChild(document.createElement('div')).innerHTML = html;
      }
    });
  } finally { inDiscreteOp = false; }
  syncContent(ed); markDirty(i); scheduleRemeasure(i);
  histEnd(before, '插入图片');
  if (size >= 2 * 1024 * 1024) {
    showToast('已插入图片（图片较大，保存/打包可能变慢）', 'warn');
  } else if (isInline) {
    showToast('已插入图片（行内，50% 宽度，点击图片可调整大小/位置）', 'ok');
  } else {
    showToast('已插入图片（居中，' + (mode === 'full' ? '全画幅' : '局部') + '显示）', 'ok');
  }
}

// 左侧原图裁剪后插入：叠加裁剪层，拖拽选区，确认后 canvas 裁剪为 dataUrl 插入
function openCrop(row, i) {
  const panel = row.querySelector('.img-panel');
  const img = row.querySelector('img');
  if (!panel || !img) return;
  // 关闭其它行已打开的裁剪层
  document.querySelectorAll('.crop-layer').forEach((el) => el.remove());
  const imgW = img.clientWidth, imgH = img.clientHeight;
  if (!imgW || !imgH) { showToast('图片尚未加载完成', 'warn'); return; }
  const layer = document.createElement('div');
  layer.className = 'crop-layer';
  const box = document.createElement('div');
  box.className = 'crop-box';
  for (const h of ['tl', 'tr', 'bl', 'br']) {
    const el = document.createElement('div');
    el.className = 'crop-handle ' + h;
    box.appendChild(el);
  }
  const actions = document.createElement('div');
  actions.className = 'crop-actions';
  const okBtn = document.createElement('button');
  okBtn.type = 'button'; okBtn.textContent = '裁剪插入';
  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button'; cancelBtn.textContent = '取消';
  actions.appendChild(okBtn); actions.appendChild(cancelBtn);
  layer.appendChild(box); layer.appendChild(actions);
  panel.appendChild(layer);

  let cur = {
    x: Math.round(imgW * 0.1), y: Math.round(imgH * 0.1),
    w: Math.round(imgW * 0.8), h: Math.round(imgH * 0.8),
  };
  function renderBox() {
    box.style.left = cur.x + 'px';
    box.style.top = cur.y + 'px';
    box.style.width = cur.w + 'px';
    box.style.height = cur.h + 'px';
  }
  renderBox();
  let drag = null; // {type:'move'|'resize', h?, sx, sy, ox, oy, ow, oh}
  box.addEventListener('mousedown', (ev) => {
    ev.preventDefault(); ev.stopPropagation();
    drag = { type: 'move', sx: ev.clientX, sy: ev.clientY, ox: cur.x, oy: cur.y };
  });
  box.querySelectorAll('.crop-handle').forEach((hEl) => {
    hEl.addEventListener('mousedown', (ev) => {
      ev.preventDefault(); ev.stopPropagation();
      drag = {
        type: 'resize', h: hEl.classList[1],
        sx: ev.clientX, sy: ev.clientY,
        ox: cur.x, oy: cur.y, ow: cur.w, oh: cur.h,
      };
    });
  });
  function onMove(ev) {
    if (!drag) return;
    const dx = ev.clientX - drag.sx, dy = ev.clientY - drag.sy;
    if (drag.type === 'move') {
      cur.x = Math.min(Math.max(0, drag.ox + dx), Math.max(0, imgW - 20));
      cur.y = Math.min(Math.max(0, drag.oy + dy), Math.max(0, imgH - 20));
    } else {
      let nx = drag.ox, ny = drag.oy, nw = drag.ow, nh = drag.oh;
      const h = drag.h;
      if (h.indexOf('r') >= 0) nw = drag.ow + dx;
      if (h.indexOf('l') >= 0) { nw = drag.ow - dx; nx = drag.ox + dx; }
      if (h.indexOf('b') >= 0) nh = drag.oh + dy;
      if (h.indexOf('t') >= 0) { nh = drag.oh - dy; ny = drag.oy + dy; }
      if (nw < 20) { if (h.indexOf('l') >= 0) nx = drag.ox + (drag.ow - 20); nw = 20; }
      if (nh < 20) { if (h.indexOf('t') >= 0) ny = drag.oy + (drag.oh - 20); nh = 20; }
      if (nx < 0) { nw += nx; nx = 0; }
      if (ny < 0) { nh += ny; ny = 0; }
      if (nx + nw > imgW) nw = imgW - nx;
      if (ny + nh > imgH) nh = imgH - ny;
      cur = { x: nx, y: ny, w: nw, h: nh };
    }
    renderBox();
  }
  function onUp() { drag = null; }
  function closeCrop() {
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
    layer.remove();
  }
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
  cancelBtn.addEventListener('click', closeCrop);
  okBtn.addEventListener('click', () => {
    if (cur.w < 5 || cur.h < 5) { showToast('选区过小', 'warn'); return; }
    // 用原图（/full/）裁剪更清晰；坐标按显示尺寸比例换算到原图像素
    const fullSrc = img.src.indexOf('/full/') >= 0
      ? img.src
      : img.src.replace('/preview/', '/full/');
    const c = document.createElement('canvas');
    const full = new Image();
    full.onload = () => {
      // 关键：坐标换算必须以原图（/full/）的自然尺寸为基准，绝不能按预览图
      // （img.naturalWidth）换算再对原图 drawImage——预览是低 DPI 缩略版
      // （preview_dpi=110 vs _FULL_DPI=220/分割原图），按预览图换算会导致
      // 从原图裁出的区域只有选区的 1/4（面积），即「截图与插入图不一致」。
      const fw = full.naturalWidth, fh = full.naturalHeight;
      if (!fw || !fh) { showToast('裁剪失败：原图尺寸无效', 'fail'); return; }
      // 选区坐标相对裁剪层（img-panel 含 padding），img 相对 panel 有 4px 偏移，
      // 需先减掉再按显示尺寸比例映射到原图像素。
      const offX = img.offsetLeft || 0, offY = img.offsetTop || 0;
      c.width = Math.max(1, Math.round(cur.w * fw / imgW));
      c.height = Math.max(1, Math.round(cur.h * fh / imgH));
      const ctx = c.getContext('2d');
      const sx = (cur.x - offX) * fw / imgW;
      const sy = (cur.y - offY) * fh / imgH;
      const sw = cur.w * fw / imgW;
      const sh = cur.h * fh / imgH;
      try {
        ctx.drawImage(full, sx, sy, sw, sh, 0, 0, c.width, c.height);
        // 小图/透明场景用 PNG，大图用 JPEG 控制体积
        const small = c.width * c.height < 400 * 400;
        const dataUrl = c.toDataURL(small ? 'image/png' : 'image/jpeg', 0.92);
        closeCrop();
        // 截图插入固定为行内模式（嵌在文字光标处），不随 imgModeSel 下拉框变化
        insertImageDataUrl(dataUrl, dataUrl.length, i, 'inline');
      } catch (e) { showToast('裁剪失败：' + e.message, 'fail'); }
    };
    full.onerror = () => { showToast('裁剪失败：原图加载失败', 'fail'); };
    full.src = fullSrc + '?v=' + loadNonce;
  });
}

// 从外部文件插入图片（工具栏「图片」组）：选择本地图片 → dataUrl → 光标处插入
(function initExternalImageInsert() {
  const btn = document.getElementById('imgExternalBtn');
  const input = document.getElementById('imgExternalInput');
  if (!btn || !input) return;
  btn.addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    const file = input.files && input.files[0];
    input.value = '';
    if (!file) return;
    const fr = new FileReader();
    fr.onload = () => {
      const ed = currentEditable();
      const row = ed ? ed.closest('.page-row') : null;
      insertImageDataUrl(fr.result, file.size, row ? Number(row.dataset.i) : null);
    };
    fr.onerror = () => showToast('读取图片失败', 'fail');
    fr.readAsDataURL(file);
  });
})();

// ---------- 编辑器内图片拖拽移动（2026-08-15） ----------
// 原生 contenteditable 拖放 <img> 会把 base64 src 作为可见文本插入，或把裸 <img>
// 拖出 <p class="ptoe-img-full ptoe-img-center"> 包裹（丢失全画幅/局部 class）。
// 这里在文档级接管拖拽：dragstart 记录被拖块，drop 时按落点插入克隆并移除原块。
let _dragImgBlock = null; // 被拖的整块（带 class 的 <p> 包裹，无包裹时为裸 <img>）
let _dragImgEd = null;    // 被拖块所在的编辑区
document.addEventListener('dragstart', function (e) {
  // 每次拖拽先清空旧状态（上次拖拽被取消时 drop 不会触发）
  _dragImgBlock = null;
  _dragImgEd = null;
  const t = e.target;
  if (!t || t.tagName !== 'IMG' || !t.closest('.editable')) return;
  _dragImgBlock = t.closest('p.ptoe-img-full, p.ptoe-img-fit') || t;
  _dragImgEd = t.closest('.editable');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', t.src); // 非空数据才能启动拖拽（勿 preventDefault）
});
document.addEventListener('dragover', function (e) {
  if (_dragImgBlock) e.preventDefault(); // 允许放置
});
document.addEventListener('drop', function (e) {
  if (!_dragImgBlock) return;
  e.preventDefault(); // 阻止浏览器把 base64 src 作为文本插入
  const srcEd = _dragImgEd;
  const dstEd = e.target.closest('.editable');
  try {
    if (!dstEd) return; // 落点不在编辑区 → 放弃（finally 清状态）
    const srcRow = srcEd ? srcEd.closest('.page-row') : null;
    const dstRow = dstEd.closest('.page-row');
    const iSrc = srcRow ? Number(srcRow.dataset.i) : -1;
    const iDst = dstRow ? Number(dstRow.dataset.i) : -1;
    const pagesArr = (iSrc >= 0 && iDst >= 0 && iSrc !== iDst) ? [iSrc, iDst]
      : (iSrc >= 0 ? [iSrc] : (iDst >= 0 ? [iDst] : []));
    histRun('移动图片', pagesArr, function () {
      // 落点光标：caretRangeFromPoint 非标准但 Chrome/Edge 均支持
      let range = null;
      if (document.caretRangeFromPoint) range = document.caretRangeFromPoint(e.clientX, e.clientY);
      if (!range) return; // 无法定位落点 → 放弃（histEnd 无变化不入栈）
      const startNode = range.startContainer;
      // 落点在被拖块内部 → 视为未移动（no-op）
      if (startNode === _dragImgBlock || _dragImgBlock.contains(startNode)) return;
      const el = startNode.nodeType === 3 ? startNode.parentNode : startNode;
      const hostBlock = el && el.closest ? el.closest('p, h1, h2, h3, h4, h5, h6, div') : null;
      const clone = _dragImgBlock.cloneNode(true);
      if (hostBlock && hostBlock !== _dragImgBlock) {
        // 落点在块内 → 插到该块之后（避免 p 套 p 非法嵌套）
        hostBlock.parentNode.insertBefore(clone, hostBlock.nextSibling);
      } else {
        range.insertNode(clone);
      }
      // 移除原块
      if (_dragImgBlock.parentNode) _dragImgBlock.parentNode.removeChild(_dragImgBlock);
      // 光标移到克隆之后
      const sel = window.getSelection();
      const r = document.createRange();
      r.setStartAfter(clone); r.collapse(true);
      sel.removeAllRanges(); sel.addRange(r);
      syncContent(dstEd);
      if (srcEd && srcEd !== dstEd) syncContent(srcEd);
    });
    // 受影响页行：标记脏 + 重测行高
    const affected = new Set();
    if (iSrc >= 0) affected.add(iSrc);
    if (iDst >= 0) affected.add(iDst);
    for (const idx of affected) { markDirty(idx); scheduleRemeasure(idx); }
  } finally {
    _dragImgBlock = null;
    _dragImgEd = null;
  }
});
// 拖拽被取消（Esc/移出窗口）时清理状态，避免残留影响下一次拖拽
document.addEventListener('dragend', function () {
  _dragImgBlock = null;
  _dragImgEd = null;
});

const remeasurePending = new Map();
let _remeasureRaf = 0;
function scheduleRemeasure(i) {
  if (remeasurePending.has(i)) return;
  remeasurePending.set(i, true);
  if (_remeasureRaf) return;
  // 同一帧合并多行测量：全部处理后只重建/重排一次（原先每行一个 rAF + 每行 O(n) 重建）
  _remeasureRaf = requestAnimationFrame(() => {
    _remeasureRaf = 0;
    const items = [...remeasurePending.keys()];
    remeasurePending.clear();
    for (const idx of items) remeasure(idx, { deferLayout: true });
    rebuildPrefix();
    reposition();
  });
}

// 用户「有意」滚动的最后时间戳：wheel/touchmove 置位（手指/滚轮主动操作）。
// 程序性滚动还原（withScrollStable）只在用户未主动滚动时生效。
let lastUserScrollTs = 0;
// 任意滚动活动时间戳：再加 scroll 事件置位 —— 覆盖惯性滑动（touchmove 在
// 惯性期不触发）、滚动条拖拽、键盘翻页等。现仅服务 withScrollStable 还原判断。
let lastAnyScrollTs = 0;

// 滚动锚定：行高变化时，若该行整体位于视口上方，其高度变化会把下方
// 可见内容推下/拉上，需反向调整 scrollY，让视口内容保持贴附（不跳页）。
// （视口内的行自身高度变化由浏览器流式布局自然处理，无需补偿；
//   调用时机必须在 rebuildPrefix 之前——style.top/rect 仍是旧布局）
// 不做「滚动中不抢」门控：补偿与布局变化同帧原子生效（主线程顺序执行），
// 内容始终贴附视口——不会出现「滚动时滑走、停止后集中补偿」的累积-释放回弹。
// 判定必须用行当前的渲染位置（getBoundingClientRect，所见即所得），不能用
// 前缀估算：未测量行按 est 顶替会偏离真实布局（书内页高不均时偏差累积），
// 误判「位于视口上方」会对视口内/下方的行错误补偿 scrollY → 页面乱跳
// （向上滚时 180→190 / 196→180，2026-08 修复）。
function anchorScrollForHeightChange(i, oldH, newH) {
  if (oldH === newH) return;
  const row = host.querySelector('.page-row[data-i="' + i + '"]');
  if (!row) return;
  const rect = row.getBoundingClientRect();
  if (rect.bottom + 60 < 0) { // 行底部已整体滚出视口上方（留 60px 边距）
    window.scrollTo(0, Math.max(0, window.scrollY + (newH - oldH)));
  }
}

// 滚动稳定包装：execCommand 等操作可能触发浏览器自动滚动（跳到光标/
// 选中节点附近页），操作后把滚动位置还原（含 remeasure 之后二次修正）。
// 若操作期间用户已主动滚动，则不还原（避免把用户刚翻的页拉回来）。
function withScrollStable(fn) {
  const before = window.scrollY;
  const ts = lastUserScrollTs; // 操作开始时的用户滚动时间戳
  const restore = () => {
    if (lastUserScrollTs !== ts) return; // 期间用户滚动过：放弃还原
    const dy = window.scrollY - before;
    if (Math.abs(dy) > 2) window.scrollTo(0, Math.max(0, before));
  };
  try {
    const out = fn();
    requestAnimationFrame(restore);
    requestAnimationFrame(() => requestAnimationFrame(restore));
    return out;
  } catch (e) {
    requestAnimationFrame(restore);
    throw e;
  }
}

function measureRow(i) {
  const row = host.querySelector('.page-row[data-i="' + i + '"]');
  if (!row) return;
  const h = row.offsetHeight + GAP;
  // 无条件记录（含图片未就绪时的无图高度）：窗口内行高始终真实，不做 est
  // 顶替——否则未测量行按全局 est 估算，与已实测行错位（书内页高不均时
  // est 偏离累积），向上滚动时新挂载行与既有行重叠/间隙 → 视觉空白/乱跳
  // （2026-08 修复）。图片就绪后由 onload → scheduleRemeasure 修正高度并
  // 即时补偿 scrollY，视口保持贴附。
  if (h > 0) {
    heights[i] = h;
    est = Math.round((est * 3 + h) / 4);
  }
}
function attach(i, opts) {
  opts = opts || {};
  if (host.querySelector('.page-row[data-i="' + i + '"]')) return;
  const row = pageRow(pages[i], i);
  row.style.position = 'absolute';
  row.style.left = '14px'; row.style.right = '14px'; row.style.top = prefixTop(i) + 'px';
  row.style.margin = '0';
  host.appendChild(row);
  // 注意：不在 attach 里做滚动补偿——attach 发生在滚动驱动的 updateViewport 中，
  // 且此刻图片多为懒加载未就绪，测量高度偏小，补偿会与翻页手势互相打架。
  // 图片未就绪的行不记录「仅文字」高度：heights[i] 保持未设（走 est 估算），
  // 否则总高度被低估 → 浏览器滚动钳制把 scrollY 回拉（= 滚动中回滚）。
  if (opts.defer) return; // 批量路径：由 updateViewport 统一测量/重建前缀/重排（避免每行 O(n) 重建 + 强制 reflow）
  measureRow(i);
  rebuildPrefix();
  reposition();
}
function reposition() {
  for (const row of host.children) {
    const i = Number(row.dataset.i);
    const t = prefixTop(i);
    const cur = parseFloat(row.style.top) || 0; // 不读 offsetTop：避免逐行强制 reflow
    if (Math.abs(cur - t) > 1) row.style.top = t + 'px';
  }
  // 防滚动钳制：估算总高度被低估时，若小于当前滚动范围+视口，浏览器会把
  // scrollY 钳制回拉（表现=滚动中回滚）。下限设为滚动范围+缓冲，可滚动高度
  // 绝不在滚动过程中收缩；真实高度由 remeasure（图片加载/编辑）收敛后接管。
  host.style.height = Math.max(totalHeight(), window.scrollY + window.innerHeight + BUFFER * GAP) + 'px';
}
function remeasure(i, opts) {
  opts = opts || {};
  const row = host.querySelector('.page-row[data-i="' + i + '"]');
  if (!row) return;
  const h = row.offsetHeight + GAP;
  if (h <= 0) return;
  const old = heights[i] || est; // 未记录过（图片未就绪）时，此前按 est 估算
  heights[i] = h;
  est = Math.round((est * 3 + h) / 4);
  // 行高变化（编辑/撤销/图片加载）统一即时滚动补偿：仅当行整体位于视口上方时
  // 补偿，且与布局变化同帧原子生效 → 视口内容贴附，滚动/编辑都不再跳页。
  anchorScrollForHeightChange(i, old, h);
  if (!opts.deferLayout) { rebuildPrefix(); reposition(); } // 批量路径由调用方统一重建/重排
}
function updateViewport() {
  const sy = window.scrollY;
  if (sy !== _viewportY) { _scrollDir = sy > _viewportY ? 1 : -1; _viewportY = sy; } // 动态方向感知
  const hostTop = host.getBoundingClientRect().top + window.scrollY;
  const y = Math.max(0, window.scrollY - hostTop - 60);
  let lo = 0, hi = pages.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (prefixTop(mid) < y) lo = mid + 1; else hi = mid; }
  const viewRows = Math.ceil((window.innerHeight - 140) / (est || 420)) + 1;
  // 空白防护（2026-08）：scroll-lasso 允许 scrollY 超过内容总高（reposition 把
  // host 高度下限设为 scrollY+innerHeight+BUFFER*GAP）——用户滚过内容尾部进入
  // 空滚区时，二分 lo 钳到末尾、挂载的行渲染顶远在视口上方 → 视口空白且下方
  // 已挂载行会被 detach。此时回退上一窗口下界 _lastLo：保留用户刚看的窗口，
  // 绝不彻底空白；用户滚回内容区即恢复（二分回到真实位置）。正常滚动时
  // scrollY ≤ totalHeight()+innerHeight，不受影响。
  if (window.scrollY > totalHeight() + window.innerHeight) {
    lo = Math.min(lo, _lastLo);
  }
  const first = Math.max(0, lo - BUFFER - (_scrollDir < 0 ? PRELOAD : 0));
  let last = Math.min(pages.length, lo + viewRows + BUFFER + (_scrollDir > 0 ? PRELOAD : 0));
  const keep = new Set();
  const toAttach = [];
  for (let i = first; i < last; i++) {
    keep.add(i);
    if (!host.querySelector('.page-row[data-i="' + i + '"]')) toAttach.push(i);
  }
  for (const i of toAttach) attach(i, { defer: true });
  for (const i of toAttach) measureRow(i); // 全部挂载后再统一测量：一次布局，避免逐行强制 reflow
  for (const row of [...host.children]) {
    const i = Number(row.dataset.i);
    if (!keep.has(i)) {
      const ed = row.querySelector('.editable');
      if (ed) syncContent(ed);
      row.remove();
    }
  }
  _lastLo = lo;    // 记录本次窗口下界：下次无行在视口附近时兜底（防止彻底空白）
  rebuildPrefix(); // 批量：挂载/卸载结束后只重建一次前缀（原先每行都重建）
  reposition();    // 批量：只重排一次
}
// ---------- 多行/多块选择辅助与格式应用 ----------
function _blocksBetween(ed, startBlock, endBlock) {
  const blocks = [];
  let walker = document.createTreeWalker(ed, NodeFilter.SHOW_ELEMENT, {
    acceptNode: function(n) {
      const tag = n.tagName;
      return /^(P|DIV|H[1-6])$/.test(tag) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
    }
  });
  let cur = walker.nextNode();
  let started = false;
  // 选区起点/终点可能是 .editable 直属文本节点（startBlock/endBlock 回退为 ed）：
  // 此时 walker 遍历从 ed 的子节点开始，cur 永不 === ed，若仍以 startBlock===ed 判定
  // 会导致 blocks 恒为空、多块格式化全部失效——这里把 startBlock 置空改从首块收集。
  if (startBlock === ed) startBlock = null;
  while (cur) {
    if (!startBlock || cur === startBlock) started = true;
    if (started) blocks.push(cur);
    if (cur === endBlock) break;
    cur = walker.nextNode();
  }
  return blocks;
}

function applyToSelectedBlocks(ed, fn) {
  // If IME composition in progress, queue operation to run after compositionend
  if (typeof isComposing !== 'undefined' && isComposing) {
    _pendingOps.push(() => applyToSelectedBlocks(ed, fn));
    showToast('输入法中，已将操作排队，输入结束后自动应用', 'warn');
    return [];
  }
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) { return []; }
  const origRanges = [];
  for (let i = 0; i < sel.rangeCount; i++) origRanges.push(sel.getRangeAt(i).cloneRange());
  const range = sel.getRangeAt(0);
  const startNode = range.startContainer.nodeType === 3 ? range.startContainer.parentElement : range.startContainer;
  const endNode = range.endContainer.nodeType === 3 ? range.endContainer.parentElement : range.endContainer;
  const startBlock = (startNode && startNode.closest) ? (startNode.closest('p,div,h1,h2,h3,h4,h5,h6') || ed) : ed;
  const endBlock = (endNode && endNode.closest) ? (endNode.closest('p,div,h1,h2,h3,h4,h5,h6') || ed) : ed;
  const blocks = _blocksBetween(ed, startBlock, endBlock);
  for (const block of blocks) {
    try {
      const r = document.createRange();
      if (block === startBlock) r.setStart(range.startContainer, range.startOffset);
      else r.setStart(block, 0);
      if (block === endBlock) r.setEnd(range.endContainer, range.endOffset);
      else r.setEnd(block, block.childNodes.length);
      sel.removeAllRanges();
      sel.addRange(r);
      fn(block, r);
    } catch (e) {
      // best-effort: skip problematic block
      continue;
    }
  }
  // restore original selection
  sel.removeAllRanges();
  for (const rr of origRanges) sel.addRange(rr);
  return blocks;
}
// Capture basic inline/block formatting attributes from selection (for 格式刷)
let _formatBrush = null;
let _brushBefore = null; // aggregated history snapshot for persistent brush
function _convertBlockTag(block, newTag) {
  if (!block || !block.parentNode) return block;
  const newEl = document.createElement(newTag);
  // copy safe attributes: class/id/data-*/aria-*
  for (let i = 0; i < block.attributes.length; i++) {
    const a = block.attributes[i];
    const n = a.name.toLowerCase();
    if (n === 'class') {
      newEl.className = block.className; // preserve all classes
    } else if (n === 'id' || n.startsWith('data-') || n.startsWith('aria-')) {
      try { newEl.setAttribute(a.name, a.value); } catch (e) {}
    }
  }
  newEl.innerHTML = block.innerHTML;
  block.parentNode.replaceChild(newEl, block);
  return newEl;
}

function toggleNote(ed) {
  // Toggle ptoe-note on all blocks in selection
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  histRun('注释格式', [i], function () {
    applyToSelectedBlocks(ed, function(block) {
      block.classList.toggle('ptoe-note');
    });
    syncContent(ed);
    if (row) { markDirty(i); scheduleRemeasure(i); }
  });
}

function cycleHeading(ed) {
  // Apply heading level cycling per selected block
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  histRun('标题', [i], function () {
    applyToSelectedBlocks(ed, function(block) {
      const tag = block.tagName.toLowerCase();
      let next;
      if (tag === 'p' || tag === 'div') next = 'h1';
      else if (/^h[1-5]$/.test(tag)) next = 'h' + (parseInt(tag[1], 10) + 1);
      else next = 'p';
      _convertBlockTag(block, next);
    });
    syncContent(ed);
    if (row) { markDirty(i); scheduleRemeasure(i); }
  });
}
function applyAlign(ed, pos) {
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  histRun('对齐', [i], function () {
    applyToSelectedBlocks(ed, function(block) {
      block.classList.remove('ptoe-align-left', 'ptoe-align-center', 'ptoe-align-right');
      block.classList.add('ptoe-align-' + pos);
    });
    syncContent(ed);
    if (row) { markDirty(i); scheduleRemeasure(i); }
  });
}

function applyFormatBrushToSelection(format) {
  const ed = currentEditable();
  if (!ed || !format) return;
  // block classes
  applyToSelectedBlocks(ed, function(block, r) {
    if (format.blockClasses && format.blockClasses.length) {
      for (const c of ['ptoe-note','ptoe-align-left','ptoe-align-center','ptoe-align-right']) block.classList.remove(c);
      for (const c of format.blockClasses) block.classList.add(c);
    }
    // inline: bold/italic/color
    if (format.bold) withScrollStable(() => document.execCommand('bold'));
    if (format.italic) withScrollStable(() => document.execCommand('italic'));
    if (format.color) withScrollStable(() => document.execCommand('foreColor', false, format.color));
    const row = ed.closest('.page-row'); if (row) { markDirty(Number(row.dataset.i)); scheduleRemeasure(Number(row.dataset.i)); }
  });
}
function captureFormatFromSelection() {
  const ed = currentEditable();
  if (!ed) return null;
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  let node = range.commonAncestorContainer;
  if (node.nodeType === 3) node = node.parentElement;
  const block = (node && node.closest ? node.closest('p,div,h1,h2,h3,h4,h5,h6') : null) || ed;
  const fmt = { blockClasses: [], bold: false, italic: false, color: null };
  if (block && block.classList) {
    for (const c of ['ptoe-note','ptoe-align-left','ptoe-align-center','ptoe-align-right']) {
      if (block.classList.contains(c)) fmt.blockClasses.push(c);
    }
  }
  try { fmt.bold = document.queryCommandState('bold'); } catch (e) {}
  try { fmt.italic = document.queryCommandState('italic'); } catch (e) {}
  try { fmt.color = window.getComputedStyle(block).color; } catch (e) {}
  return fmt;
}
function applyOp(op) { const ed = currentEditable(); if (!ed) return;
  if (op.indexOf('marker_') === 0) { insertMarker(op); return; }
  if (op === 'note') { toggleNote(ed); return; }
  if (op === 'heading') { cycleHeading(ed); return; }
  if (op.indexOf('align_') === 0) { applyAlign(ed, op.slice(6)); return; }
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  histRun(OP_TIP[op] || op, [i], function () {
    // For multi-block selections, apply command per-block to guarantee
    // the change propagates to all selected lines/blocks.
    if (op === 'bold') applyToSelectedBlocks(ed, function() { withScrollStable(() => document.execCommand('bold')); });
    else if (op === 'italic') applyToSelectedBlocks(ed, function() { withScrollStable(() => document.execCommand('italic')); });
    else if (op === 'remove') applyToSelectedBlocks(ed, function() { withScrollStable(() => document.execCommand('removeFormat')); });
    else if (op === 'p') applyToSelectedBlocks(ed, function(block) { _convertBlockTag(block, 'p'); }); // 与 heading 一致逐块转换（execCommand formatBlock 对跨块选区只转起始块）
    else if (op === 'merge') _mergeSelectedBlocks(ed);
    syncContent(ed);
    if (row) { markDirty(i); scheduleRemeasure(i); }
  });
}
function insertMarkerAtCaret(block, span, range) {
  range.collapse(false);      // 只保留光标插入点
  range.insertNode(span);     // 终点在文本节点内时 insertNode 会自动切分文本节点
  range.setStartAfter(span);  // 光标移到标记之后，便于连续插入
  range.collapse(true);
}

function insertMarker(op) {
  const ed = currentEditable();
  if (!ed) return;
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  let type, label;
  if (op === 'marker_full') { type = 'full'; label = '全文'; }
  else if (op === 'marker_note') { type = 'note'; label = '注释'; }
  else if (op === 'marker_join') { type = 'join'; label = '段落'; }
  else if (op === 'marker_page') { type = 'page'; label = '换页'; }
  else return;
  if (mdMode) {
    // Markdown 源码模式：以行内 HTML 文本形式插入（md 转 html 时原样放行）
    histRun('标记', [i], function () {
      withScrollStable(() => document.execCommand('insertText', false, '<span data-ptoe-marker="' + type + '">' + label + '</span>'));
      syncContent(ed);
      markDirty(i);
      scheduleRemeasure(i);
    });
    return;
  }
  const sel = window.getSelection();
  const range = sel && sel.rangeCount ? sel.getRangeAt(0) : null;
  let node = range ? range.endContainer : ed;
  if (node.nodeType === 3) node = node.parentElement;
  let block = node && node.closest ? node.closest('p,div,h1,h2,h3,h4,h5,h6') : null;
  if (!block || !ed.contains(block)) block = ed;
  const span = document.createElement('span');
  span.className = 'ptoe-marker';
  span.dataset.ptoeMarker = type;
  span.textContent = label;
  histRun('标记', [i], function () {
    if (range && block.contains(range.endContainer)) {
      insertMarkerAtCaret(block, span, range);
    } else {
      block.appendChild(span);
    }
    ed.focus();
    syncContent(ed);
    markDirty(i);
    scheduleRemeasure(i);
  });
}

// ---------- 繁简转换 / Markdown 切换 / 字号 / 跳转 ----------
async function convertAll(mode) {
  const btn = mode === 's2t' ? document.getElementById('toTraditionBtn') : document.getElementById('toSimplifiedBtn');
  btn.disabled = true;
  try {
    const before = histBegin('繁简转换', null); // 全页快照；histEnd 只保留实际变化页
    const res = await fetchJSON('/api/convert', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: mode, pages: collect() })
    });
    const converted = res.pages || [];
    for (const it of converted) {
      let idx = -1;
      for (let i = 0; i < pages.length; i++) { if (pages[i].page === it.page) { idx = i; break; } }
      if (idx < 0) continue;
      if (mdMode) mdSourceMap.set(idx, htmlToMd(it.html));
      else contentMap.set(idx, it.html);
    }
    for (const row of [...host.children]) {
      const i = Number(row.dataset.i);
      const ed = row.querySelector('.editable');
      if (ed) { ed.innerHTML = displayHtml(i); _reapplyProofread(i); remeasure(i); }
    }
    histEnd(before, '繁简转换');
    for (let i = 0; i < pages.length; i++) editedSet.add(i);
    dirty = true; updateStatus();
    setStatus('已完成' + (mode === 's2t' ? '简体→繁体' : '繁体→简体') + '转换，请保存');
  } catch (e) { setStatus('转换失败: ' + e); }
  finally { btn.disabled = false; }
}
function setMdMode(on) {
  if (!!on === mdMode) return;
  if (on) {
    for (let i = 0; i < pages.length; i++) {
      mdSourceMap.set(i, htmlToMd(contentMap.has(i) ? contentMap.get(i) : pages[i].text));
    }
  } else {
    for (let i = 0; i < pages.length; i++) {
      contentMap.set(i, mdToHtml(mdSourceMap.has(i) ? mdSourceMap.get(i) : htmlToMd(pages[i].text)));
    }
    mdSourceMap.clear();
  }
  mdMode = !!on;
  histClear(); // 模式切换后快照源（md/富文本）不一致，撤销/重做历史失效
  saveStr('ptoe_md_mode', mdMode ? '1' : '0');
  const btn = document.getElementById('mdToggleBtn');
  btn.textContent = mdMode ? '富文本模式' : 'Markdown模式';
  btn.classList.toggle('active', mdMode);
  const keep = [...host.children].map(r => Number(r.dataset.i));
  host.innerHTML = '';
  for (const i of keep) attach(i);
  for (const i of keep) attach(i);
  setStatus(mdMode ? '已切换为 Markdown 源码模式（保存时按 Markdown 转 HTML）' : '已切换为富文本模式');
}
function jumpToPage() {
  const v = parseInt(document.getElementById('pageJump').value, 10);
  if (!v) { setStatus('请输入页码'); return; }
  let idx = -1;
  for (let i = 0; i < pages.length; i++) { if (pages[i].page === v) { idx = i; break; } }
  if (idx < 0) { setStatus('未找到第 ' + v + ' 页'); return; }
  const hostTop = host.getBoundingClientRect().top + window.scrollY;
  window.scrollTo({ top: Math.max(0, hostTop + prefixTop(idx) - 60), behavior: 'smooth' });
  hidePopup();
  setStatus('已跳转到第 ' + v + ' 页');
}

// ---------- 智能清理 / 搜索替换 ----------
async function cleanAll() {
  // 逐页调用 /api/clean：段首符号、中英文标点、残留 HTML 标签
  try {
    const before = histBegin('智能清理', null); // 全页快照；histEnd 只保留实际变化页
    const res = await fetchJSON('/api/clean', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pages: collect() })
    });
    const cleaned = res.pages || [];
    for (const it of cleaned) {
      let idx = -1;
      for (let i = 0; i < pages.length; i++) { if (pages[i].page === it.page) { idx = i; break; } }
      if (idx < 0) continue;
      if (mdMode) mdSourceMap.set(idx, htmlToMd(it.html));
      else contentMap.set(idx, it.html);
    }
    for (const row of [...host.children]) {
      const idx = Number(row.dataset.i);
      const ed = row.querySelector('.editable');
      if (ed) { ed.innerHTML = displayHtml(idx); _reapplyProofread(idx); scheduleRemeasure(idx); }
    }
    histEnd(before, '智能清理');
    for (let i = 0; i < pages.length; i++) editedSet.add(i);
    dirty = true;
    updateStatus();
    setStatus('已清理 ' + cleaned.length + ' 页');
    showToast('已清理 ' + cleaned.length + ' 页（段首符号 / 标点 / 标签）', 'ok');
  } catch (e) {
    showToast('清理失败: ' + e.message, 'fail');
  }
}

// 搜索结果状态：searchResults 为当前结果列表（上限 200 条），searchCurrent 为
// 当前选中序号（用于上一个/下一个跳转与「替换当前」）
let searchResults = [];
let searchCurrent = -1;

function pageText(i) {
  // 搜索用的纯文本：与 replaceAll/replaceCurrent 完全相同的 token 切分
  // （标签之间按原文，含实体），保证搜索序号与替换位置一一对应
  return (pageSource(i) || '').split(/(<[^>]+>)/).filter(function (t) { return t && t.charAt(0) !== '<'; }).join('');
}

function decodeEntities(s) {
  // 仅用于结果预览显示：把页面源码里的实体还原成可读文本
  return String(s).replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'");
}

function searchRegexFor(query) {
  const regexMode = document.getElementById('searchRegex').checked;
  const q = regexMode ? query : query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(q, regexMode ? 'gimu' : 'giu');
}

function updateSearchNav() {
  const pos = document.getElementById('searchPos');
  if (pos) pos.textContent = searchResults.length ? (searchCurrent + 1) + ' / ' + searchResults.length : '';
  const list = document.getElementById('searchList');
  if (list) {
    for (let k = 0; k < list.children.length; k++) list.children[k].classList.toggle('current', k === searchCurrent);
  }
}

function renderSearchResults(results, total, MAX) {
  const list = document.getElementById('searchList');
  const count = document.getElementById('srCount');
  count.textContent = '共 ' + total + ' 处匹配' + (total > MAX ? '，仅显示前 ' + MAX + ' 条' : '');
  list.innerHTML = '';
  if (!results.length) {
    list.innerHTML = '<div class="sr-empty">未找到匹配内容</div>';
    return;
  }
  results.forEach(function (r, k) {
    const item = document.createElement('div');
    item.className = 'sr-item';
    item.innerHTML = '<div class="sr-page">第 ' + r.page + ' 页</div><div class="sr-ctx">' + r.ctx + '</div>';
    item.addEventListener('click', function () {
      searchCurrent = k;
      updateSearchNav();
      scrollToIndex(r.i);
    });
    list.appendChild(item);
  });
}

function searchPages() {
  const query = document.getElementById('searchInput').value;
  const list = document.getElementById('searchList');
  if (!query) { showToast('请先输入搜索词', 'warn'); return; }
  let re;
  try { re = searchRegexFor(query); }
  catch (e) { showToast('正则表达式无效：' + e.message, 'fail'); return; }
  // 性能：一次遍历统计总数，超出 MAX 只存储前 MAX 条（列表仍显示真实总数）
  const CONTEXT = 40, MAX = 200;
  const results = [];
  let total = 0, pageStart = 0;
  for (let i = 0; i < pages.length; i++) {
    const text = pageText(i);
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      const withinPage = total - pageStart; // 本页内第几处（0 起）
      const pageOrd = total;                // 全局第几处（0 起）
      total++;
      if (results.length >= MAX) continue;
      const s = Math.max(0, m.index - CONTEXT);
      const e2 = Math.min(text.length, m.index + m[0].length + CONTEXT);
      results.push({
        i: i, page: pages[i].page, pageOrd: pageOrd, withinPage: withinPage,
        ctx: esc(decodeEntities(text.slice(s, m.index))) + '<mark>' + esc(decodeEntities(m[0])) + '</mark>' + esc(decodeEntities(text.slice(m.index + m[0].length, e2)))
      });
    }
    pageStart = total; // 下一页匹配的起点
  }
  searchResults = results;
  searchCurrent = results.length ? 0 : -1;
  renderSearchResults(results, total, MAX);
  updateSearchNav();
  // Highlight matches in visible pages for quick preview
  applySearchHighlights();
  openSearchModal();
  if (total === 0) showToast('未找到匹配内容', 'warn');
}
// ---------- 搜索高亮（在编辑区预览中高亮，输入为空时去高亮） ----------
let _searchHighlightQuery = '';
function debounce(fn, ms) {
  let t = null;
  return function(...args) {
    clearTimeout(t);
    t = setTimeout(() => fn.apply(this, args), ms);
  };
}

function _highlightInHtmlSource(html, re) {
  // Split by tags; only replace in text tokens to avoid touching attributes
  return String(html).split(/(<[^>]+>)/).map(function(tok) {
    if (!tok) return '';
    if (tok.charAt(0) === '<') return tok;
    return tok.replace(re, function(m) { return '<mark class="ptoe-search">' + esc(m) + '</mark>'; });
  }).join('');
}

function applySearchHighlights() {
  const q = (document.getElementById('searchInput').value || '').trim();
  if (!q) { clearSearchHighlights(); return; } // query 已在 clearSearchHighlights 内置空
  if (q === _searchHighlightQuery) return; // avoid redundant work
  let re;
  try { re = searchRegexFor(q); } catch (e) { return; }
  _searchHighlightQuery = q;
  // Only process attached rows (virtual list) for performance
  for (const row of host.children) {
    const idx = Number(row.dataset.i);
    const ed = row.querySelector('.editable');
    if (!ed) continue;
    // avoid replacing while user is editing to preserve caret
    if (ed === document.activeElement || ed.contains(document.activeElement)) continue;
    const src = displayHtml(idx);
    const highlighted = _highlightInHtmlSource(src, re);
    if (highlighted !== ed.innerHTML) {
      ed.innerHTML = highlighted;
      scheduleRemeasure(idx);
    }
  }
}

function clearSearchHighlights() {
  _searchHighlightQuery = '';   // 先置空，否则 displayHtml 还原时会用旧 query 回注标记
  for (const row of host.children) {
    const idx = Number(row.dataset.i);
    const ed = row.querySelector('.editable');
    if (!ed) continue;
    if (ed === document.activeElement || ed.contains(document.activeElement)) continue;
    const src = displayHtml(idx);
    if (ed.innerHTML.indexOf('ptoe-search') !== -1) {
      ed.innerHTML = src;
      scheduleRemeasure(idx);
    }
  }
}

// 标记只在点击「搜索」后应用（2026-08）；输入框清空时立即消除全部文字标记
document.getElementById('searchInput').addEventListener('input', function () {
  if (!(this.value || '').trim()) clearSearchHighlights();
});

// 清理搜索状态：清除全部文字标记 + 清空结果列表与计数 + 清空「x / y」位置显示（不动 #searchInput 的值）
function clearSearchState() {
  searchResults = [];
  searchCurrent = -1;
  clearSearchHighlights();          // 内部先置空 _searchHighlightQuery，还原不回注
  renderSearchResults([], 0, 200);  // 清空结果列表 + 计数
  updateSearchNav();                // 清空「x / y」位置显示
}
document.getElementById('searchClearBtn').addEventListener('click', clearSearchState);

async function exportFile(fmt) {
  try {
    // 兜底：先同步当前编辑框内容到 map，确保导出的是最新内容
    const ed = currentEditable();
    if (ed) syncContent(ed);
    const res = await fetchJSON('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ format: fmt, pages: collect() }),
    });
    if (res.cancelled) { setStatus('已取消导出'); return; }
    if (!res.ok) { showToast('导出失败：' + (res.error || '未知错误'), 'fail'); return; }
    showToast('导出成功：' + res.path, 'ok');
    setStatus('已导出：' + res.path);
  } catch (e) {
    showToast('导出失败：' + e.message, 'fail');
  }
}
function gotoMatch(dir) {
  if (!searchResults.length) return;
  searchCurrent = (searchCurrent + dir + searchResults.length) % searchResults.length;
  updateSearchNav();
  const cur = searchResults[searchCurrent];
  const list = document.getElementById('searchList');
  if (list.children[searchCurrent]) list.children[searchCurrent].scrollIntoView({ block: 'nearest' });
  scrollToIndex(cur.i);
}
function replaceCurrent() {
  // 只替换当前选中的那处匹配（第 searchCurrent 条），其余匹配保持不动
  if (!searchResults.length || searchCurrent < 0) { showToast('请先搜索', 'warn'); return; }
  const query = document.getElementById('searchInput').value;
  const repl = document.getElementById('replaceInput').value;
  let re;
  try { re = searchRegexFor(query); }
  catch (e) { showToast('正则表达式无效：' + e.message, 'fail'); return; }
  const cur = searchResults[searchCurrent];
  const i = cur.i;
  const target = cur.withinPage; // 该页内第 target 处（0 起）
  const before = histBegin('替换当前', [i]);
  const src = pageSource(i);
  let out, c = 0;
  if (mdMode) {
    out = src.replace(re, function (m) { const n = c++; return (n === target) ? repl : m; });
  } else {
    out = src.split(/(<[^>]+>)/).map(function (tok) {
      if (tok.charAt(0) === '<') return tok;
      return tok.replace(re, function (m) { const n = c++; return (n === target) ? repl : m; });
    }).join('');
  }
  if (c <= target) { showToast('该处匹配已变化，请重新搜索', 'warn'); return; }
  if (mdMode) mdSourceMap.set(i, out);
  else contentMap.set(i, out);
  const row = host.querySelector('.page-row[data-i="' + i + '"]');
  const ed = row && row.querySelector('.editable');
  if (ed) { ed.innerHTML = displayHtml(i); _reapplyProofread(i); scheduleRemeasure(i); }
  editedSet.add(i);
  dirty = true;
  updateStatus();
  histEnd(before, '替换当前');
  showToast('已替换当前匹配（第 ' + (searchCurrent + 1) + ' 条）', 'ok');
  searchPages(); // 替换后刷新结果列表与序号
}

function scrollToIndex(idx) {
  const hostTop = host.getBoundingClientRect().top + window.scrollY;
  window.scrollTo({ top: Math.max(0, hostTop + prefixTop(idx) - 60), behavior: 'smooth' });
  hidePopup();
}

function replaceAll() {
  const query = document.getElementById('searchInput').value;
  const repl = document.getElementById('replaceInput').value;
  if (!query) { showToast('请先输入搜索词', 'warn'); return; }
  let re;
  try { re = searchRegexFor(query); }
  catch (e) { showToast('正则表达式无效：' + e.message, 'fail'); return; }
  const changed = [];
  let count = 0;
  const before = histBegin('全部替换', null); // 全页快照；histEnd 只保留实际变化页
  for (let i = 0; i < pages.length; i++) {
    const src = pageSource(i);
    let out, c = 0;
    if (mdMode) {
      // Markdown 源码：直接整体替换
      out = src.replace(re, function () { c++; return repl; });
    } else {
      // 富文本：只替换标签之间的文本 token，不触碰标签/属性，避免破坏 HTML 结构
      out = src.split(/(<[^>]+>)/).map(function (tok) {
        if (tok.charAt(0) === '<') return tok;
        return tok.replace(re, function () { c++; return repl; });
      }).join('');
    }
    if (c > 0) changed.push({ i: i, out: out });
    count += c;
  }
  if (count === 0) { showToast('未找到匹配内容，未替换', 'warn'); return; }
  for (const ch of changed) {
    if (mdMode) mdSourceMap.set(ch.i, ch.out);
    else contentMap.set(ch.i, ch.out);
  }
  for (const row of [...host.children]) {
    const idx = Number(row.dataset.i);
    const ed = row.querySelector('.editable');
    if (ed) { ed.innerHTML = displayHtml(idx); _reapplyProofread(idx); scheduleRemeasure(idx); }
  }
  for (let i = 0; i < pages.length; i++) editedSet.add(i);
  dirty = true;
  updateStatus();
  histEnd(before, '全部替换');
  showToast('已替换 ' + count + ' 处', 'ok');
  searchPages(); // 替换后刷新结果列表（匹配数可能变化）
}

// ---------- 保存 / 完成 ----------
// U2：三色 toast（ok 成功 / fail 失败 / warn 警告），顶部居中，3s 自动消失
function showToast(msg, kind) {
  let wrap = document.getElementById('toast');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.id = 'toast';
    document.body.appendChild(wrap);
  }
  const t = document.createElement('div');
  t.className = 'toast' + (kind ? ' ' + kind : '');
  t.textContent = msg;
  wrap.appendChild(t);
  requestAnimationFrame(function () { t.classList.add('show'); });
  setTimeout(function () {
    t.classList.remove('show');
    setTimeout(function () { t.remove(); }, 250);
  }, 3000);
}
async function save() {
  const btn = document.getElementById('saveBtn');
  btn.disabled = true;
  try {
    const res = await fetchJSON('/api/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pages: collect(), proofread: collectProofread(), last_proofread_page: lastProofreadPage })
    });
    if (!res || res.ok === false) throw new Error((res && res.error) || '保存失败');
    dirty = false;
    setStatus('已保存 ' + res.saved + ' 页，' + new Date().toLocaleTimeString());
    showToast('已保存 ' + res.saved + ' 页', 'ok');
  } catch (e) {
    setStatus('保存失败: ' + e);
    showToast('保存失败：' + e, 'fail');
  }
  finally { btn.disabled = false; }
}
async function stage() {
  const btn = document.getElementById('stageBtn');
  btn.disabled = true;
  try {
    const res = await fetchJSON('/api/stage', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pages: collect(), proofread: collectProofread(), last_proofread_page: lastProofreadPage })
    });
    if (!res || res.ok === false) throw new Error((res && res.error) || '暂存失败');
    dirty = false;
    setStatus('已暂存 ' + res.saved + ' 页到本地历史，' + new Date().toLocaleTimeString());
    showToast('已暂存 ' + res.saved + ' 页', 'ok');
  } catch (e) {
    setStatus('暂存失败: ' + e);
    showToast('暂存失败：' + e, 'fail');
  }
  finally { btn.disabled = false; }
}
async function finish() {
  const btn = document.getElementById('finishBtn');
  btn.disabled = true;
  btn.classList.add('loading');
  setStatus('正在提交并生成 EPUB，请稍候 ...');
  let res = null, ok = false;
  try {
    res = await fetchJSON('/api/finish', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pages: collect(), name: loadedTitle || undefined, proofread: collectProofread(), last_proofread_page: lastProofreadPage })
    });
    ok = !!(res && res.ok);
  } catch (e) { /* 服务端异常；是否成功以响应为准 */ }
  btn.classList.remove('loading');
  const conv = res && res.converted;
  if (ok && conv && conv.ok) {
    setStatus('转换完成，等待确认');
    showToast('转换完成', 'ok');
    showFinishModal('done', conv.message);
  } else if (ok && conv && !conv.ok) {
    btn.disabled = false;
    setStatus('转换未完成：' + (conv.message || '请检查注释标记数量'));
    showFinishModal('fail', conv.message);
  } else if (ok) {
    setStatus('转换完成，等待确认');
    showToast('转换完成', 'ok');
    showFinishModal('done');
  } else if (res && res.converted && res.converted.ok) {
    // S4：历史缓存写入失败但转换成功 —— 提示警告，转换结果仍有效
    btn.disabled = false;
    setStatus('转换完成，但历史缓存写入失败（磁盘错误？）');
    showToast('转换完成，但历史缓存写入失败', 'warn');
    showFinishModal('done', res.converted.message);
  } else {
    btn.disabled = false;
    setStatus('提交失败，转换未完成（可重试）');
    showToast('提交失败：' + ((res && res.error) || '未知错误'), 'fail');
    showFinishModal('fail');
  }
}

// ---------- 完成/失败弹窗 ----------
let finishModalKind = null;
function showFinishModal(kind, msg) {
  finishModalKind = kind;
  const title = document.getElementById('finishTitle');
  const msgEl = document.getElementById('finishMsg');
  const closeBtn = document.getElementById('closePageBtn');
  const stayBtn = document.getElementById('stayPageBtn');
  if (kind === 'done') {
    title.textContent = '转换完成';
    msgEl.textContent = msg || '矫正内容已提交，EPUB 正在生成。是否关闭当前页面？';
    closeBtn.style.display = '';
    stayBtn.textContent = '留在本页';
  } else {
    title.textContent = '转换未完成';
    msgEl.textContent = msg || '提交失败（服务器可能已关闭），请点击「完成并转换」重试。';
    closeBtn.style.display = 'none';
    stayBtn.textContent = '知道了';
  }
  document.getElementById('finishModalBg').style.display = 'flex';
}
document.getElementById('closePageBtn').addEventListener('click', () => {
  document.getElementById('finishModalBg').style.display = 'none';
  window.close();
  setTimeout(() => { alert('浏览器不允许脚本自动关闭此标签页，请手动关闭。'); }, 300);
});
document.getElementById('stayPageBtn').addEventListener('click', () => {
  document.getElementById('finishModalBg').style.display = 'none';
  if (finishModalKind === 'done') setStatus('转换完成，可手动关闭此页面');
  finishModalKind = null;
});

// ---------- 历史记录弹窗（列表 / 单删 / 多选删 / 全部删 / 导出 / 导入） ----------
// 导出：把选中版本打包为 ZIP（每版本一个自包含 JSON，含预览图），可拷贝到其它电脑；
// 导入：读取导出的 JSON 或 ZIP 并落盘到本地历史缓存，供跨平台继续矫正。
async function exportHistoryVersion(id) {
  // 行内「导出」同样走 bulk ZIP 端点（单版本），保证导出格式统一为压缩包
  try {
    const res = await fetch('/api/history/export/bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: [id] }),
    });
    if (!res.ok) { let err = '导出失败'; try { const j = await res.json(); if (j && j.error) err = j.error; } catch (_) {} throw new Error(err); }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = id + '.zip';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast('已导出 ' + id + '.zip', 'ok');
  } catch (e) { showToast('导出失败：' + e, 'fail'); }
}
function _historyTimestamp() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds());
}
async function exportSelectedHistory() {
  try {
    const checks = document.querySelectorAll('.hist-check:checked');
    const ids = [...checks].map(c => c.dataset.id).filter(Boolean);
    if (!ids.length) { showToast('请先勾选要导出的历史记录', 'warn'); return; }
    const res = await fetch('/api/history/export/bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    });
    if (!res.ok) { let err = '导出失败'; try { const j = await res.json(); if (j && j.error) err = j.error; } catch (_) {} throw new Error(err); }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'ptoe_history_' + _historyTimestamp() + '.zip';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast('已导出 ' + ids.length + ' 个版本（ZIP）', 'ok');
  } catch (e) { showToast('导出失败：' + e, 'fail'); }
}
function importHistoryFile() { document.getElementById('historyImportFile').click(); }
function onHistoryImportFile(e) {
  const file = e.target && e.target.files && e.target.files[0];
  if (!file) return;
  const isZip = file.name.toLowerCase().endsWith('.zip');
  const finish = () => { document.getElementById('historyImportFile').value = ''; };
  if (isZip) {
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const bytes = new Uint8Array(reader.result);
        const CHUNK = 0x8000;
        let b64 = '';
        for (let i = 0; i < bytes.length; i += CHUNK) {
          b64 += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
        }
        const content_b64 = btoa(b64);
        const res = await fetch('/api/history/import', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: file.name, is_zip: true, content_b64 }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.ok === false) throw new Error(data && data.error ? data.error : '导入失败');
        const n = (data.ids || []).length;
        showToast('已导入 ' + n + ' 个版本' + (data.errors && data.errors.length ? '（' + data.errors.length + ' 个失败）' : ''), 'ok');
        loadHistory();
      } catch (err) { showToast('导入失败：' + err, 'fail'); }
      finally { finish(); }
    };
    reader.readAsArrayBuffer(file);
    return;
  }
  const fr = new FileReader();
  fr.onload = async () => {
    try {
      const content = JSON.parse(fr.result);
      const res = await fetchJSON('/api/history/import', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: file.name, content: content })
      });
      if (!res || res.ok === false) throw new Error((res && res.error) || '导入失败');
      showToast('已导入：' + (res.id || file.name), 'ok');
      loadHistory();
    } catch (err) { showToast('导入失败：' + err, 'fail'); }
    finally { finish(); }
  };
  fr.readAsText(file);
}
function historyRow(it) {
  const tr = document.createElement('tr');
  const tdCheck = document.createElement('td'); tdCheck.style.padding = '6px 8px';
  const cb = document.createElement('input');
  cb.type = 'checkbox'; cb.className = 'hist-check'; cb.dataset.id = it.id;
  tdCheck.appendChild(cb);
  const tdName = document.createElement('td'); tdName.style.padding = '6px 8px'; tdName.textContent = it.name;
  const tdPath = document.createElement('td'); tdPath.style.padding = '6px 8px'; tdPath.style.color = '#5a6b7c'; tdPath.textContent = it.path;
  const tdVer = document.createElement('td'); tdVer.style.padding = '6px 8px'; tdVer.textContent = 'v' + (it.version || 1);
  const tdTime = document.createElement('td'); tdTime.style.padding = '6px 8px'; tdTime.style.color = '#5a6b7c'; tdTime.textContent = it.updated;
  const tdProof = document.createElement('td'); tdProof.style.padding = '6px 8px'; tdProof.style.color = '#5a6b7c'; tdProof.textContent = it.last_proofread_page ? '校正至第 ' + it.last_proofread_page + ' 页' : '-';
  const tdOp = document.createElement('td'); tdOp.style.padding = '6px 8px';
  const btn = document.createElement('button');
  btn.type = 'button'; btn.textContent = '打开';
  btn.title = '把该版本的文本重新载入编辑器进行再次矫正（覆盖当前未保存的修改）';
  btn.addEventListener('click', () => loadHistoryVersion(it.id, it.name, it.version || 1));
  const btnExport = document.createElement('button');
  btnExport.type = 'button'; btnExport.textContent = '导出';
  btnExport.title = '把该版本导出为 ZIP 压缩包（含预览图），可在其他电脑通过「导入」继续矫正';
  btnExport.addEventListener('click', () => exportHistoryVersion(it.id));
  tdOp.appendChild(btn);
  tdOp.appendChild(btnExport);
  tr.append(tdCheck, tdName, tdPath, tdVer, tdTime, tdProof, tdOp);
  return tr;
}
async function loadHistory() {
  const tbody = document.querySelector('#historyTable tbody');
  tbody.innerHTML = '<tr><td colspan="6" style="padding:12px;color:#9aa7b4;">加载中 ...</td></tr>';
  document.getElementById('historyCheckAll').checked = false;
  try {
    const res = await fetchJSON('/api/history');
    const items = res.items || [];
    tbody.innerHTML = '';
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="6" style="padding:12px;color:#9aa7b4;">暂无历史记录</td></tr>';
      return;
    }
    for (const it of items) tbody.appendChild(historyRow(it));
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="5" style="padding:12px;color:#b3543a;">加载失败: ' + e + '</td></tr>';
  }
}
function openHistory() { loadHistory(); document.getElementById('historyModalBg').style.display = 'flex'; }
function closeHistory() { document.getElementById('historyModalBg').style.display = 'none'; }
// 搜索/导出模态框开关：与 historyModalBg 同一模式（CSS 默认 display:none）。
// 曾因重构丢失这四个函数导致加载期 ReferenceError，后续所有绑定（含工具栏）
// 全部失效——编辑后务必 node --check 并核对每个顶层绑定目标函数已定义。
function openSearchModal() { document.getElementById('searchModalBg').style.display = 'flex'; document.getElementById('searchInput').focus(); }
function closeSearchModal() { document.getElementById('searchModalBg').style.display = 'none'; }
function openExportModal() { document.getElementById('exportModalBg').style.display = 'flex'; }
function closeExportModal() { document.getElementById('exportModalBg').style.display = 'none'; }
async function loadHistoryVersion(id, name, ver) {
  const displayName = name + (ver ? ' v' + ver : '');
  if (!confirm('确定用该历史版本（' + displayName + '）替换当前编辑内容？未保存的修改将被覆盖。')) return;
  try {
    const res = await fetchJSON('/api/history/load', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id })
    });
    const loaded = res.pages || [];
    const map = {};
    for (const it of loaded) map[it.page] = it.html;
    // 旧内容按页码收集（未编辑页取初始 text）
    const oldByPage = {};
    for (let i = 0; i < pages.length; i++) {
      oldByPage[pages[i].page] = contentMap.has(i) ? contentMap.get(i) : pages[i].text;
    }
    const oldMdByPage = {};
    for (let i = 0; i < pages.length; i++) if (mdSourceMap.has(i)) oldMdByPage[pages[i].page] = mdSourceMap.get(i);
    // 页码取并集：历史版本可能包含当前会话没有的页（如无 PDF 启动后打开暂存）
    const pageSet = new Set();
    for (const p of pages) pageSet.add(p.page);
    for (const it of loaded) pageSet.add(it.page);
    const newPages = [...pageSet].sort((a, b) => a - b).map(function(p) { return { page: p, text: '' }; });
    const newContent = new Map();
    const newMd = new Map();
    for (let i = 0; i < newPages.length; i++) {
      const p = newPages[i].page;
      const cur = map[p] !== undefined ? map[p] : (oldByPage[p] !== undefined ? oldByPage[p] : '');
      newContent.set(i, cur);
      if (mdMode) newMd.set(i, oldMdByPage[p] !== undefined ? oldMdByPage[p] : htmlToMd(cur));
    }
    pages = newPages;
    contentMap = newContent;
    mdSourceMap = newMd;
    histClear(); // 整体替换内容，撤销/重做历史失效
    editedSet.clear();
    for (let i = 0; i < pages.length; i++) editedSet.add(i);
    heights.length = pages.length; heights.fill(0);
    est = pages.length ? 420 : 420;
    loadNonce++;  // 换书后图片 URL 加 ?v= 强制重新加载（缓存/来源已切换）
    // 恢复文字纠错状态（先重置再填充，防旧数据残留）
    proofreadErrors = {};
    proofreadOriginal = {};
    proofreadDismissed = {};
    lastProofreadPage = null;
    if (res.proofread) {
      try {
        // 页码→新索引映射（历史版本页数可能与当前会话不同）
        const pageIdx = new Map(newPages.map((p, idx) => [p.page, idx]));
        const remap = function (k) {
          const n = Number(k);
          if (pageIdx.has(n)) return pageIdx.get(n);
          // 旧格式兼容：key 为 pageIndex（整数且在新页数范围内）
          if (Number.isInteger(n) && n >= 0 && n < newPages.length) return n;
          return -1;
        };
        // errors：仅接受数组值，每项需有数字 start/end、字符串 wrong
        const srcErrors = res.proofread.errors || {};
        for (const k in srcErrors) {
          const idx = remap(k);
          if (idx < 0) continue;
          const arr = srcErrors[k];
          if (!Array.isArray(arr)) continue;
                const valid = arr.filter(function (e) {
                  return e && typeof e.start === 'number' && typeof e.end === 'number' && typeof e.wrong === 'string' && (!e.candidates || Array.isArray(e.candidates));
                });
          if (valid.length) proofreadErrors[idx] = JSON.parse(JSON.stringify(valid));
        }
        // original：仅接受字符串值
        const srcOrig = res.proofread.original || {};
        for (const k in srcOrig) {
          const idx = remap(k);
          if (idx < 0) continue;
          if (typeof srcOrig[k] === 'string') proofreadOriginal[idx] = srcOrig[k];
        }
        // dismissed：数组→Set
        const srcDismissed = res.proofread.dismissed || {};
        for (const k in srcDismissed) {
          const idx = remap(k);
          if (idx < 0) continue;
          const v = srcDismissed[k];
          proofreadDismissed[idx] = new Set(Array.isArray(v) ? v : []);
        }
      } catch (e) { /* 解析失败则保持清空 */ }
      lastProofreadPage = typeof res.last_proofread_page === 'number' ? res.last_proofread_page : null;
    }
    host.innerHTML = '';
    rebuildPrefix(); // heights 已重置，prefixH 需按 est 重建（旧累计值不可复用）
    host.style.height = totalHeight() + 'px';
    updateViewport();
    // 重注已挂载行的纠错标注
    for (const k in proofreadErrors) {
      if (proofreadErrors[k] && proofreadErrors[k].length) {
        const row = host.querySelector('.page-row[data-i="' + k + '"]');
        if (row) { const ed = row.querySelector('.editable'); if (ed) { ed.innerHTML = displayHtml(Number(k)); _reapplyProofread(Number(k)); } }
      }
    }
    dirty = true; updateStatus();
    closeHistory();
    loadedTitle = name.replace(/\.[^.\/\\]+$/, '');  // 去扩展名，无文件模式下作为 EPUB 标题
    setStatus('已从历史版本载入 ' + loaded.length + ' 页，可继续矫正（保存/完成将生成新版本）');
  } catch (e) { showToast('加载历史版本失败：' + (e && e.message ? e.message : e), 'fail'); }
}
async function deleteHistory(ids, all) {
  try {
    const res = await fetchJSON('/api/history/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: ids, all: !!all })
    });
    alert('已删除 ' + res.deleted + ' 条历史记录');
    loadHistory();
  } catch (e) { alert('删除失败: ' + e); }
}
document.getElementById('historyBtn').addEventListener('click', openHistory);
document.getElementById('historyCloseBtn').addEventListener('click', closeHistory);
document.getElementById('historyCheckAll').addEventListener('change', (e) => {
  document.querySelectorAll('.hist-check').forEach(c => { c.checked = e.target.checked; });
});
document.getElementById('historyDeleteBtn').addEventListener('click', () => {
  const ids = [...document.querySelectorAll('.hist-check:checked')].map(c => c.dataset.id);
  if (!ids.length) { alert('请先勾选要删除的历史记录'); return; }
  if (!confirm('确定删除选中的 ' + ids.length + ' 条历史记录？')) return;
  deleteHistory(ids, false);
});
document.getElementById('historyDeleteAllBtn').addEventListener('click', () => {
  if (!confirm('确定删除全部历史记录？此操作不可恢复。')) return;
  deleteHistory([], true);
});
document.getElementById('historyExportBtn').addEventListener('click', exportSelectedHistory);
document.getElementById('historyImportBtn').addEventListener('click', importHistoryFile);
document.getElementById('historyImportFile').addEventListener('change', onHistoryImportFile);

// ---------- 弹出快捷菜单（图标 + 悬停提示，置于选中文字正上方） ----------
function buildPopup() {
  popup.innerHTML = '';
  // 快捷菜单显示格式按钮（OPS 前 9 项：粗体/斜体/标题/正文/清除/注释/居左/居中/居右）；标记按钮已移至工具栏；规则按钮单独追加
  const ops = OPS.slice(0, 9);
  const groups = [ops.slice(0, 7), ops.slice(7)];
  groups.forEach((group, gi) => {
    if (gi > 0) { const d = document.createElement('div'); d.className = 'sep'; popup.appendChild(d); }
    for (const op of group.map(g => g[0])) {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'pop-btn'; b.dataset.op = op;
      b.innerHTML = OP_ICON[op] || op;
      b.setAttribute('aria-label', OP_TIP[op] || op);
      b.addEventListener('mouseenter', scheduleTip);
      b.addEventListener('mouseleave', hideTip);
      b.addEventListener('mousedown', (e) => {
        e.preventDefault();
        hideTip();
        suppressPopupUntil = performance.now() + 250;
        applyOp(op); hidePopup();
      });
      popup.appendChild(b);
    }
  });
  // 格式刷（单次模式）
  const paintBtn = document.createElement('button');
  paintBtn.type = 'button'; paintBtn.className = 'pop-btn'; paintBtn.id = 'popPaint';
  paintBtn.textContent = '刷';
  paintBtn.title = PAINT_TITLE;
  paintBtn.setAttribute('aria-label', '格式刷');
  if (paintActive) paintBtn.classList.add('active');
  paintBtn.addEventListener('mouseenter', scheduleTip);
  paintBtn.addEventListener('mouseleave', hideTip);
  paintBtn.addEventListener('mousedown', (e) => { e.preventDefault(); hideTip(); });
  paintBtn.addEventListener('click', () => {
    suppressPopupUntil = performance.now() + 250;
    if (paintActive) applyPaint(); else activatePaint();
  });
  popup.appendChild(paintBtn);
  // 标记按钮组（全文/段落/注释/换页）+ 删除按钮
  const markerOps = [['marker_full','全文标记'], ['marker_join','段落标记'], ['marker_note','注释标记'], ['marker_page','换页标记']];
  const markerSep = document.createElement('div'); markerSep.className = 'sep'; popup.appendChild(markerSep);
  markerOps.forEach(([op, label]) => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'pop-btn'; b.dataset.op = op;
    b.textContent = OP_ICON[op] || op;
    b.setAttribute('aria-label', label);
    b.title = label;
    b.addEventListener('mouseenter', scheduleTip);
    b.addEventListener('mouseleave', hideTip);
    b.addEventListener('mousedown', (e) => {
      e.preventDefault();
      hideTip();
      suppressPopupUntil = performance.now() + 250;
      applyOp(op); hidePopup();
    });
    popup.appendChild(b);
  });
  // 删除按钮（删除当前选区或光标所在块）
  const delBtn = document.createElement('button');
  delBtn.type = 'button'; delBtn.className = 'pop-btn';
  delBtn.textContent = '删'; delBtn.title = '删除'; delBtn.setAttribute('aria-label', '删除');
  delBtn.addEventListener('mouseenter', scheduleTip);
  delBtn.addEventListener('mouseleave', hideTip);
  delBtn.addEventListener('mousedown', (e) => {
    e.preventDefault();
    hideTip();
    suppressPopupUntil = performance.now() + 250;
    const ed = currentEditable();
    if (ed) {
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed) {
        document.execCommand('delete');
      } else {
        const row = ed.closest('.page-row');
        const i = row ? Number(row.dataset.i) : -1;
        histRun('删除', [i], function () {
          const block = ed.querySelector('p,div,h1,h2,h3,h4,h5,h6');
          if (block && block.parentNode) block.parentNode.removeChild(block);
          syncContent(ed);
          if (row) { markDirty(i); scheduleRemeasure(i); }
        });
      }
    }
    hidePopup();
  });
  popup.appendChild(delBtn);
  // 规则按钮 + 子菜单（改为点击展开，不再依赖 hover）
  const sep = document.createElement('div'); sep.className = 'sep'; popup.appendChild(sep);
  const ruleWrap = document.createElement('div');
  ruleWrap.className = 'pop-rule-wrap';
  const ruleBtn = document.createElement('button');
  ruleBtn.type = 'button'; ruleBtn.className = 'pop-btn pop-rule-btn';
  ruleBtn.textContent = '规'; ruleBtn.title = '应用格式规则';
  ruleBtn.setAttribute('aria-label', '应用格式规则');
  ruleWrap.appendChild(ruleBtn);
  const ruleSub = document.createElement('div');
  ruleSub.className = 'pop-rule-sub';
  ruleSub.innerHTML = '<div class="ctx-empty">加载中…</div>';
  ruleWrap.appendChild(ruleSub);
  let _ruleSubOpen = false;
  function _toggleRuleSub() {
    _ruleSubOpen = !_ruleSubOpen;
    ruleSub.style.display = _ruleSubOpen ? 'block' : 'none';
    if (_ruleSubOpen) _refreshPopRuleSub(ruleSub);
  }
  ruleBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _toggleRuleSub();
  });
  // 点击 popup 外部（或规则子菜单项）自动关闭子菜单
  ruleSub.addEventListener('mousedown', (e) => {
    if (e.target.closest('.ctx-item')) {
      _ruleSubOpen = false;
      ruleSub.style.display = 'none';
    }
  });
  popup.appendChild(ruleWrap);
}
function _refreshPopRuleSub(box) {
  if (box.dataset.loaded) return;
  box.innerHTML = '<div class="ctx-empty">加载中…</div>';
  fetchJSON('/api/format_rules').then(function (res) {
    const rules = (res && res.rules) || [];
    box.innerHTML = '';
    if (!rules.length) {
      const empty = document.createElement('div');
      empty.className = 'ctx-empty';
      empty.textContent = '暂无规则';
      box.appendChild(empty);
      return;
    }
    rules.forEach(function (rule) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ctx-item';
      btn.textContent = rule.name || '（未命名规则）';
      btn.title = '应用规则「' + (rule.name || '') + '」';
      btn.addEventListener('mousedown', (e) => {
        e.preventDefault();
        e.stopPropagation();
        suppressPopupUntil = performance.now() + 250;
        hidePopup();
        const ed = currentEditable();
        if (ed) applyFormatRule(rule, ed);
      });
      box.appendChild(btn);
    });
    box.dataset.loaded = '1';
  }).catch(function () {
    box.innerHTML = '<div class="ctx-empty">加载失败</div>';
  });
}
function hidePopup() { hideTip(); popup.style.display = 'none'; }
function showPopup(range) {
  buildPopup();
  popup.style.display = 'flex';
  const r = popup.getBoundingClientRect();
  const rect = range.getBoundingClientRect();
  let left = rect.left + rect.width / 2 - r.width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - r.width - 8));
  let top = rect.top - r.height - 8;   // 选中文字正上方，不遮盖选中内容
  if (top < 8) top = rect.bottom + 8;  // 上方空间不足 → 移到下方
  popup.style.left = left + 'px';
  popup.style.top = top + 'px';
}
// 选中文字 → 弹出快捷菜单。触发点：mouseup（鼠标框选）与 keyup（Shift+方向键
// 键盘选择）；Ctrl/Meta/Alt 组合键是快捷键操作，不弹菜单。点击操作按钮后
// suppressPopupUntil 窗口内不弹（避免格式操作后菜单反复弹出）。
function maybeShowPopup() {
  if (performance.now() < suppressPopupUntil) return;
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) { hidePopup(); return; }
  const range = sel.getRangeAt(0);
  let n = range.commonAncestorContainer;
  if (n && n.nodeType === 3) n = n.parentNode;
  const ed = n && n.closest ? n.closest('.editable') : null;
  if (!ed) { hidePopup(); return; }
  showPopup(range);
}
document.addEventListener('mouseup', maybeShowPopup);
document.addEventListener('keyup', (e) => { if (!e.isComposing && !e.ctrlKey && !e.metaKey && !e.altKey) maybeShowPopup(); });
// 保存最近一次在 .editable 内的选区，供 insertImage 在光标处插入（而非末尾）
let _lastEditableRange = null;
document.addEventListener('selectionchange', () => {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) hidePopup();
  hideErrPopup();
  // 记录选区（仅在 .editable 内且非折叠时）
  if (sel && sel.rangeCount > 0 && !sel.isCollapsed) {
    const node = sel.getRangeAt(0).commonAncestorContainer;
    const el = node && node.nodeType === 3 ? node.parentNode : node;
    if (el && el.closest && el.closest('.editable')) {
      _lastEditableRange = sel.getRangeAt(0).cloneRange();
    }
  }
});

// ---------- 右键上下文菜单（2026-08-08） ----------
// 编辑区内右键弹出自定义菜单（重识别/插入标记/导出/Markdown 提示/保存/暂存），
// 编辑区外保留浏览器默认右键菜单。打开时抑制选中文字快捷菜单（suppressPopupUntil），
// Esc / 滚动 / 点击外部均关闭；二级菜单 hover 展开、点击父项切换。
let ctxMenuOpen = false;
const ctxMenu = document.getElementById('contextMenu');
let _ctxEscCapture = null; // 菜单打开期间的临时 Esc 捕获监听（capture 阶段先于既有 bubble 快捷键分发）
// 2026-08-09：右键目标页/光标位置。右键打开菜单不改变焦点/选区，页级操作
// （清除/重识别/应用/插入标记）若用 currentEditable() 会取到光标所在页而非被右键页
// （曾致右键「清除」清不掉被右键页的纠错标注）。ctxRun 捕获-关闭-恢复-执行-清空，
// 使 fn 同步段内可读右键目标，工具栏直调（不经 ctxRun）不受影响。
let _ctxEditable = null; // 被右键的 .editable（右键目标页）
let _ctxRange = null;    // 右键位置 caretRangeFromPoint 的 range（标记插入精确定位，jsdom 无则 null）

function closeContextMenu() {
  ctxMenuOpen = false;
  ctxMenu.hidden = true;
  if (typeof _ctxCancelTimers === 'function') _ctxCancelTimers(); // 关菜单时取消 hover-intent 延时
  ctxMenu.querySelectorAll('.ctx-sub.open').forEach(el => el.classList.remove('open'));
  if (_ctxEscCapture) { document.removeEventListener('keydown', _ctxEscCapture, true); _ctxEscCapture = null; }
  _ctxEditable = null; // 防陈旧右键目标泄漏到后续工具栏操作
  _ctxRange = null;
}

function toggleCtxSub(parent) {
  const wasOpen = parent.classList.contains('open');
  ctxMenu.querySelectorAll('.ctx-sub.open').forEach(el => el.classList.remove('open'));
  if (!wasOpen) {
    parent.classList.add('open');
    orientCtxSubs(); // 展开后按实测尺寸定向（打开前 display:none 量不到）
  }
}

// 二级菜单展开方向：右缘不足向左展开、向左后左缘不足再翻回、下缘不足向上对齐。
// 打开时（hover / 点击 / 右键开菜单）都要重跑：display:none 状态量不到真实尺寸。
function orientCtxSubs() {
  ctxMenu.querySelectorAll('.ctx-sub').forEach(sub => {
    const sm = sub.querySelector('.ctx-submenu');
    if (!sm) return;
    const pr = sub.getBoundingClientRect();
    // 未展开时 offsetWidth/Height 为 0 → 用兜底估算值（与 CSS min-width 一致）
    const sw = sm.offsetWidth || 150, sh = sm.offsetHeight || 160;
    // 横向：默认向右；右侧放不下则向左；向左后左缘越界（左侧更挤）则翻回向右
    let left = pr.right + 4;
    if (left + sw > window.innerWidth) {
      if (pr.left - 4 - sw >= 0) sm.classList.add('ctx-left');
      else sm.classList.remove('ctx-left');
    } else {
      sm.classList.remove('ctx-left');
    }
    // 纵向：默认顶边与父项对齐（top:-5px）；下缘越界则改为底边对齐（.ctx-up）
    if (pr.top - 5 + sh > window.innerHeight && pr.bottom + 5 - sh >= 0) sm.classList.add('ctx-up');
    else sm.classList.remove('ctx-up');
  });
}

function openContextMenu(x, y) {
  hidePopup();
  hideErrPopup();
  closeProofreadMenu();
  refreshCtxRulesSub(); // 每次打开刷新「添加规则」二级菜单（异步填充规则名列表）
  suppressPopupUntil = performance.now() + 300; // 右键后的 mouseup 不弹选中菜单
  orientCtxSubs();
  ctxMenu.hidden = false;
  const w = ctxMenu.offsetWidth || 172, h = ctxMenu.offsetHeight || 240;
  const cx = Math.max(8, Math.min(x, window.innerWidth - w - 8)); // clamp 到视口 8px 边距
  const cy = Math.max(8, Math.min(y, window.innerHeight - h - 8));
  ctxMenu.style.left = cx + 'px';
  ctxMenu.style.top = cy + 'px';
  orientCtxSubs(); // 定位后按最终位置重算二级菜单方向（边缘裁切修复）
  ctxMenuOpen = true;
  if (!_ctxEscCapture) {
    _ctxEscCapture = (e) => {
      if (e.key === 'Escape' && ctxMenuOpen) {
        e.preventDefault();
        e.stopPropagation(); // 菜单打开期间 Esc 只关菜单：拦截既有快捷键分发（capture 先于 bubble）
        closeContextMenu();
      }
    };
    document.addEventListener('keydown', _ctxEscCapture, true);
  }
}

// 菜单项执行：先关菜单再执行，异常 toast。
// 捕获-关闭-恢复-执行-清空：closeContextMenu 会清掉 _ctxEditable/_ctxRange，
// 故先取出保存、关菜单后恢复，fn 同步段内可用 ctxTargetEditable() 取右键目标页；
// finally 清空防止泄漏（fn 内异步操作不应依赖右键目标）。
function ctxRun(fn) {
  const target = _ctxEditable;
  const range = _ctxRange;
  closeContextMenu();
  _ctxEditable = target;
  _ctxRange = range;
  suppressPopupUntil = performance.now() + 300;
  try { fn(); } catch (e) { showToast('操作失败：' + e.message, 'fail'); }
  finally { _ctxEditable = null; _ctxRange = null; }
}
// 右键菜单页级操作目标 = 被右键的页；无右键目标（工具栏直调）回退当前光标/选区页
function ctxTargetEditable() {
  return _ctxEditable || currentEditable();
}
// 插入标记依赖光标位置（insertMarker 用当前 selection）：优先用右键目标页；
// 若光标不在右键页，把光标移到右键位置（无 caretRangeFromPoint 时落到页首）
function ctxMarkerInsert(type) {
  ctxRun(() => {
    const ed = ctxTargetEditable();
    if (!ed) { showToast('请先点击某一页的文字', 'warn'); return; }
    if (_ctxEditable && ed !== currentEditable()) {
      ed.focus();
      const sel = window.getSelection();
      const range = (_ctxRange && ed.contains(_ctxRange.startContainer)) ? _ctxRange : (() => {
        const r = document.createRange();
        r.selectNodeContents(ed);
        r.collapse(true);
        return r;
      })();
      sel.removeAllRanges();
      sel.addRange(range);
    }
    insertMarker('marker_' + type);
  });
}
function ctxExportRun(fmt) { ctxRun(() => exportFile(fmt)); }
// 右键菜单「添加规则」二级菜单：列出已保存格式规则，点击即应用到右键目标页。
// 每次打开菜单时刷新（fetch /api/format_rules，fire-and-forget）；子菜单 hover
// 才展开，异步填充通常已就绪。空列表显示「暂无规则」。
let _ctxFormatRules = [];
function refreshCtxRulesSub() {
  const box = document.getElementById('ctxRulesSub');
  if (!box) return;
  box.innerHTML = '<div class="ctx-empty">加载中…</div>';
  fetchJSON('/api/format_rules').then(function (res) {
    const rules = (res && res.rules) || [];
    _ctxFormatRules = rules;
    box.innerHTML = '';
    if (!rules.length) {
      const empty = document.createElement('div');
      empty.className = 'ctx-empty';
      empty.textContent = '暂无规则';
      box.appendChild(empty);
      return;
    }
    rules.forEach(function (rule, idx) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ctx-item';
      btn.dataset.ctxRule = String(idx);
      btn.textContent = rule.name || '（未命名规则）';
      btn.title = '应用规则「' + (rule.name || '') + '」到当前页';
      box.appendChild(btn);
    });
  }).catch(function () {
    box.innerHTML = '<div class="ctx-empty">加载失败</div>';
  });
}
// 右键菜单快速应用规则到右键目标页：光标不在右键页时先移入（同 ctxMarkerInsert），
// 然后 applyFormatRule(rule, ed)（传入 edArg 跳过 restoreFrRange——右键场景不恢复
// 弹窗捕获的选区）。
function ctxApplyFormatRule(rule) {
  const ed = ctxTargetEditable();
  if (!ed) { showToast('请先点击某一页的文字', 'warn'); return; }
  if (_ctxEditable && ed !== currentEditable()) {
    ed.focus();
    const sel = window.getSelection();
    const range = (_ctxRange && ed.contains(_ctxRange.startContainer)) ? _ctxRange : (() => {
      const r = document.createRange();
      r.selectNodeContents(ed);
      r.collapse(true);
      return r;
    })();
    sel.removeAllRanges();
    sel.addRange(range);
  }
  applyFormatRule(rule, ed);
}

// 菜单点击分发（委托）
ctxMenu.addEventListener('click', (e) => {
  // 添加规则 二级菜单叶子项：点击规则名 → 快速应用到右键目标页
  const ruleBtn = e.target.closest('.ctx-submenu .ctx-item[data-ctx-rule]');
  if (ruleBtn) {
    e.stopPropagation();
    const idx = parseInt(ruleBtn.dataset.ctxRule, 10);
    const rule = _ctxFormatRules[idx];
    if (!rule) { showToast('规则不存在，可能已被删除', 'warn'); return; }
    ctxRun(() => ctxApplyFormatRule(rule));
    return;
  }
  // 二级菜单叶子项（插入标记 / 导出 子项）
  const subBtn = e.target.closest('.ctx-submenu .ctx-item');
  if (subBtn) {
    e.stopPropagation();
    const mk = subBtn.dataset.ctxMarker;
    if (mk) { ctxMarkerInsert(mk); return; }
    const ex = subBtn.dataset.ctxExport;
    if (ex) { ctxExportRun(ex); return; }
    return;
  }
  // 一级父项（插入标记 / 导出）：点击切换二级菜单展开
  const parent = e.target.closest('.ctx-item.ctx-sub');
  if (parent) { e.stopPropagation(); toggleCtxSub(parent); return; }
  const item = e.target.closest('.ctx-item[data-ctx]');
  if (!item) return;
  const kind = item.dataset.ctx;
  if (kind === 'reocr') ctxRun(runReocr);
  else if (kind === 'clear') ctxRun(proofreadClearCurrent);
  else if (kind === 'md') ctxRun(() => showToast('单页 Markdown 编辑暂未实现。当前可使用工具栏 Markdown 按钮切换全局模式（会影响全部页面）。建议后续按页记录 md 状态并随历史持久化。', 'warn'));
  else if (kind === 'save') ctxRun(save);
  else if (kind === 'stage') ctxRun(stage);
});

// 二级菜单 hover 展开（hover-intent，2026-08-09）：
// 移入父项 200ms 后才展开（划过不误开）；移出后 300ms 宽限再收起——鼠标斜穿
// 父项与子菜单之间的 4px 间隙（CSS ::before 桥 + 本延时）不会中途关闭。
// 移入另一父项时立即互斥收起旧的；relatedTarget 仍在本 .ctx-sub 内则不收起。
const CTX_HOVER_OPEN_MS = 200;
const CTX_HOVER_CLOSE_MS = 300;
let _ctxOpenTimer = null;
let _ctxCloseTimer = null;
function _ctxCancelTimers() {
  if (_ctxOpenTimer) { clearTimeout(_ctxOpenTimer); _ctxOpenTimer = null; }
  if (_ctxCloseTimer) { clearTimeout(_ctxCloseTimer); _ctxCloseTimer = null; }
}
function openCtxSub(sub) {
  ctxMenu.querySelectorAll('.ctx-sub.open').forEach(el => { if (el !== sub) el.classList.remove('open'); });
  sub.classList.add('open');
  orientCtxSubs(); // 展开即定向（边缘裁切修复）
}
ctxMenu.querySelectorAll('.ctx-sub').forEach(sub => {
  sub.addEventListener('mouseenter', () => {
    _ctxCancelTimers();
    if (sub.classList.contains('open')) return;
    _ctxOpenTimer = setTimeout(() => { _ctxOpenTimer = null; openCtxSub(sub); }, CTX_HOVER_OPEN_MS);
  });
  sub.addEventListener('mouseleave', (e) => {
    if (_ctxOpenTimer) { clearTimeout(_ctxOpenTimer); _ctxOpenTimer = null; }
    // 移向自己的子菜单（子孙节点）不关闭
    if (e && e.relatedTarget && sub.contains(e.relatedTarget)) return;
    if (_ctxCloseTimer) clearTimeout(_ctxCloseTimer);
    _ctxCloseTimer = setTimeout(() => { _ctxCloseTimer = null; sub.classList.remove('open'); }, CTX_HOVER_CLOSE_MS);
  });
  const sm = sub.querySelector('.ctx-submenu');
  if (sm) {
    // 宽限期内进入子菜单 → 取消收起
    sm.addEventListener('mouseenter', () => { _ctxCancelTimers(); sub.classList.add('open'); });
  }
});

// 编辑区内右键：阻止默认 + 收起选中菜单 + 抑制 mouseup 弹窗 + 打开自定义菜单；编辑区外不干预
document.addEventListener('contextmenu', (e) => {
  const ed = e.target.closest('.editable');
  if (ed) {
    e.preventDefault();
    hidePopup();
    suppressPopupUntil = performance.now() + 300;
    _ctxEditable = ed; // 记录右键目标页：页级操作（清除/重识别/应用/插入标记）与光标位置解耦
    _ctxRange = null;
    if (document.caretRangeFromPoint) {
      const r = document.caretRangeFromPoint(e.clientX, e.clientY);
      if (r && ed.contains(r.startContainer)) _ctxRange = r;
    }
    openContextMenu(e.clientX, e.clientY);
  }
});

// 菜单外 mousedown 关闭（再次右键别处：mousedown 先关 → contextmenu 在新位置重开）
document.addEventListener('mousedown', (e) => {
  if (ctxMenuOpen && !e.target.closest('#contextMenu')) closeContextMenu();
});
// 菜单内 mousedown：阻止默认（保持编辑区选区/光标，供标记插入使用）+ 抑制选中菜单弹出
ctxMenu.addEventListener('mousedown', (e) => {
  e.preventDefault();
  suppressPopupUntil = performance.now() + 300;
});

// ---------- 格式刷（单次模式，Word 风格） ----------
// 选中含格式的文本 → 点「刷」捕获格式 → 再选目标文字 → 再点「刷」应用（单次即止）
let paintActive = false;
let paintSource = null; // 格式描述对象 {bold, italic, underline, strike, sup, sub, note}
const PAINT_TITLE = '格式刷：复制所选文字的格式，再选择目标文字应用';

// 捕获当前选区起点/终点的格式：Text 节点取 parentElement，向父链上溯到最近的格式元素，
// 用 getComputedStyle 判定（起点/终点任一满足即 true）
function captureFormat() {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  const fmt = { bold: false, italic: false, underline: false, strike: false, sup: false, sub: false, note: false };
  const probe = (node) => {
    if (!node) return;
    if (node.nodeType === 3) node = node.parentElement;
    let el = node;
    while (el && el !== document.body && !(el.classList && el.classList.contains('editable'))) {
      const cs = window.getComputedStyle(el);
      if (!fmt.bold && (parseFloat(cs.fontWeight) >= 600 || el.closest('strong, b'))) fmt.bold = true;
      if (!fmt.italic && (cs.fontStyle === 'italic' || el.closest('em, i'))) fmt.italic = true;
      if (!fmt.underline && (cs.textDecorationLine.indexOf('underline') !== -1 || el.closest('u'))) fmt.underline = true;
      if (!fmt.strike && (cs.textDecorationLine.indexOf('line-through') !== -1 || el.closest('s, strike, del'))) fmt.strike = true;
      if (!fmt.sup && (cs.verticalAlign === 'super' || el.closest('sup'))) fmt.sup = true;
      if (!fmt.sub && (cs.verticalAlign === 'sub' || el.closest('sub'))) fmt.sub = true;
      if (!fmt.note && el.classList && el.classList.contains('ptoe-note')) fmt.note = true;
      el = el.parentElement;
    }
  };
  probe(range.startContainer);
  probe(range.endContainer);
  return fmt;
}

// 对目标 range 应用格式：行内格式走 execCommand（withScrollStable 防滚动跳页），
// 注释走既有 applyToSelectedBlocks 机制（与「注释」按钮同路径）
function applyFormat(fmt, range) {
  if (!fmt || !range) return;
  const sel = window.getSelection();
  if (!sel) return;
  sel.removeAllRanges();
  sel.addRange(range);
  if (fmt.bold) withScrollStable(() => document.execCommand('bold'));
  if (fmt.italic) withScrollStable(() => document.execCommand('italic'));
  if (fmt.underline) withScrollStable(() => document.execCommand('underline'));
  if (fmt.strike) withScrollStable(() => document.execCommand('strikeThrough'));
  if (fmt.sup) withScrollStable(() => document.execCommand('superscript'));
  if (fmt.sub) withScrollStable(() => document.execCommand('subscript'));
  if (fmt.note) {
    const ed = currentEditable();
    if (ed) applyToSelectedBlocks(ed, function(block) { block.classList.add('ptoe-note'); });
  }
}

function activatePaint() {
  const fmt = captureFormat();
  if (!fmt) { showToast('请先选中含有格式的文本以捕获格式', 'warn'); return; }
  paintSource = fmt;
  paintActive = true;
  const b = document.getElementById('popPaint');
  if (b) { b.classList.add('active'); b.title = '点击应用到所选文字'; }
  setStatus('格式刷已激活：请选择要应用格式的文字');
  hidePopup();
  suppressPopupUntil = performance.now() + 250; // 与既有抑制机制同时间基准（performance.now）
  document.body.classList.add('paint-mode');
}

function applyPaint() {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) { showToast('请先选择要应用格式的文字', 'warn'); return; }
  const range = sel.getRangeAt(0);
  applyFormat(paintSource, range);
  paintActive = false;
  paintSource = null;
  const b = document.getElementById('popPaint');
  if (b) { b.classList.remove('active'); b.title = PAINT_TITLE; }
  document.body.classList.remove('paint-mode');
  updateStatus(); // 恢复状态栏（保持选区不 collapse，用户可看到效果）
}

function cancelPaint() {
  paintActive = false;
  paintSource = null;
  const b = document.getElementById('popPaint');
  if (b) { b.classList.remove('active'); b.title = PAINT_TITLE; }
  document.body.classList.remove('paint-mode');
  updateStatus();
}

// Esc 取消格式刷（与既有 Escape 处理器并存，互不干扰；纠错悬浮窗可见时优先走 errNo）
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && paintActive && !errKey) cancelPaint();
});
// 点击编辑区外（且不在 popup 内）→ 取消格式刷
document.addEventListener('mousedown', (e) => {
  if (!paintActive) return;
  const t = e.target;
  if (t && t.closest && (t.closest('.editable') || t.closest('#popup'))) return;
  cancelPaint();
});

// ---------- 文字纠错（proofread） ----------
// 视图层叠加标注（仿搜索高亮机制），不进 undo 快照；服务端 proofread_page 检测错误。
let proofreadErrors = {};   // pageIndex → errors 数组
let proofreadDismissed = {}; // pageIndex → Set('start:wrong')
let proofreadOriginal = {};  // pageIndex → 校正前的 innerHTML 快照（用于回退）
let lastProofreadPage = null;  // 最后一次校正/重识别的真实页码（1-based，用于历史记录显示）
let errKey = null; // 当前悬浮窗对应的 {i, k}
let proofreadMenuOpen = false; // 下拉菜单是否展开
let proofreadLlmEnabled = false; // LLM 深度校对开关（服务端持久化 config.json，随机端口下 localStorage 每运行失效）
let proofreadLlmModel = '';      // 深度校对模型 key（'' = 跟随 selected_model）
let proofreadLegacyRules = false; // 原有规则开关（默认关：校正只跑三条新规则；服务端持久化）

// 纠错当前编辑行：调 /api/proofread → 叠加标注（不入 undo）
async function runProofread() {
  const ed = currentEditable();
  if (!ed) { showToast('请先点击某一页的文字', 'warn'); return; }
  const row = ed.closest('.page-row');
  if (!row) return;
  const i = Number(row.dataset.i);
  // 先清旧标注（clearProofread 会连带清掉旧快照/忽略集），再取本轮校正前快照——
  // 顺序不可颠倒：clearProofread 内 delete proofreadOriginal[i]（2026-08-09 清除 bug 修复）。
  // 快照取清理后的 HTML（不含 .ptoe-err/.ptoe-fix），「回退」恢复得到干净原文。
  if (proofreadErrors[i]) clearProofread(i, true);
  proofreadOriginal[i] = ed.innerHTML;
  try {
    const res = await fetchJSON('/api/proofread', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        html: ed.innerHTML,
        // 可选 LLM 增强：读自内存变量（服务端 /api/proofread_settings 持久化）
        use_llm: proofreadLlmEnabled,
        llm_model: proofreadLlmModel,
      }),
    });
    if (!res.ok) { showToast('纠错失败: ' + (res.error || '未知错误'), 'fail'); return; }
    let errors = res.errors || [];
    if (res.llm_error) showToast('深度校对失败: ' + res.llm_error, 'warn');
    // 过滤已忽略的条目
    if (proofreadDismissed[i]) {
      errors = errors.filter(function (e) { return !proofreadDismissed[i].has(e.start + ':' + e.wrong); });
    }
    proofreadErrors[i] = errors;
    lastProofreadPage = pages[i].page;
    if (errors.length) {
      renderProofread(i);
      scheduleRemeasure(i);
      setStatus('找到 ' + errors.length + ' 处疑似错误');
      showToast('找到 ' + errors.length + ' 处疑似错误', 'ok');
    } else {
      setStatus('未发现明显错误');
      showToast('未发现明显错误', 'ok');
    }
  } catch (e) {
    showToast('纠错失败: ' + e.message, 'fail');
  }
}

// 把错误标注叠加到指定行的文本节点上（删除线 + 候选字）
// 支持跨多个文本节点的 wrong：每段包成独立 sEl（同 data-err-i），fixEl 只在首段插一次
function renderProofread(i) {
  const row = host.children ? [...host.children].find(function (r) { return Number(r.dataset.i) === i; }) : null;
  if (!row) return;
  const ed = row.querySelector('.editable');
  if (!ed) return;
  const errors = proofreadErrors[i] || [];
  if (!errors.length) return;
  // P2: 保存光标（相对 ed 文本偏移，排除 .ptoe-fix/.ptoe-marker 的展示文本）——重建后恢复，防 IME 光标跳段首
  let savedSelOffset = -1;
  try {
    const sel = window.getSelection();
    if (sel && sel.rangeCount > 0 && ed.contains(sel.anchorNode)) {
      const acc = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT, {
        acceptNode: function (node) {
          if (node.parentElement && node.parentElement.closest('.ptoe-fix, .ptoe-marker')) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      let cum = 0, tn;
      while ((tn = acc.nextNode())) {
        if (tn === sel.anchorNode) { savedSelOffset = cum + sel.anchorOffset; break; }
        cum += tn.textContent.length;
      }
      if (savedSelOffset < 0) savedSelOffset = cum;
    }
  } catch (e) { savedSelOffset = -1; }
  // 幂等：先清除本页既有标注（与 clearProofread 同构），避免重复叠加
  const _fixes = ed.querySelectorAll('.ptoe-fix');
  for (const el of _fixes) el.parentNode.removeChild(el);
  const _errs = ed.querySelectorAll('.ptoe-err');
  for (const el of _errs) {
    const t = document.createTextNode(el.textContent);
    el.parentNode.replaceChild(t, el);
  }
  ed.normalize();
  // 倒序处理，避免 DOM 修改影响后续偏移
  for (let k = errors.length - 1; k >= 0; k--) {
    const err = errors[k];
    if (err._gone) continue; // F5: 已标记消失的条目跳过渲染
    // Phase 1: 收集与 [err.start, err.end) 相交的文本节点（不修改树，偏移稳定）
    const segs = [];
    {
      const walker2 = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT, {
        acceptNode: function (node) {
          if (node.parentElement && node.parentElement.closest('.ptoe-err, .ptoe-fix, .ptoe-marker')) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      let cum2 = 0, tn2;
      while ((tn2 = walker2.nextNode())) {
        const len = tn2.textContent.length;
        const segStart = Math.max(0, err.start - cum2);
        const segEnd = Math.min(len, err.end - cum2);
        if (segStart < segEnd) segs.push({ node: tn2, segStart: segStart, segEnd: segEnd });
        cum2 += len;
        if (cum2 >= err.end) break;
      }
    }
    // Phase 2: 逐节点包裹（收集期间树未改，偏移有效；wrong 文本跨节点时每段一个 sEl）
    let firstSeg = true;
    for (const s of segs) {
      const tn3 = s.node, segStart = s.segStart, segEnd = s.segEnd;
      const text = tn3.textContent;
      const before = text.slice(0, segStart);
      const segText = text.slice(segStart, segEnd);
      const after = text.slice(segEnd);
      const frag = document.createDocumentFragment();
      if (before) frag.appendChild(document.createTextNode(before));
      const sEl = document.createElement('s');
      sEl.className = 'ptoe-err';
      sEl.setAttribute('data-err-i', k);
      sEl.textContent = segText;
      frag.appendChild(sEl);
      // fixEl 只在首段插一次（candidates 必须是数组，否则安全跳过）
      if (firstSeg && Array.isArray(err.candidates) && err.candidates.length) {
        const fixEl = document.createElement('span');
        fixEl.className = 'ptoe-fix';
        fixEl.setAttribute('data-err-i', k);
        fixEl.textContent = err.candidates.join('/');
        frag.appendChild(fixEl);
      }
      firstSeg = false;
      if (after) frag.appendChild(document.createTextNode(after));
      tn3.parentNode.replaceChild(frag, tn3);
    }
  }
  // C: 重建后同步基准文本（供 _proofreadAutoDismiss delta-rebase 使用）
  _prTextBefore[i] = _plainNoAnno(ed);
  updatePrCount();
  // P2: 恢复光标（按保存的字符偏移定位文本节点）
  if (savedSelOffset >= 0) {
    try {
      const w2 = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT, {
        acceptNode: function (node) {
          if (node.parentElement && node.parentElement.closest('.ptoe-fix, .ptoe-marker')) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      let cum2 = 0, target = savedSelOffset, hit = null, off = 0, tn2;
      while ((tn2 = w2.nextNode())) {
        const len = tn2.textContent.length;
        if (cum2 + len >= target) { hit = tn2; off = target - cum2; break; }
        cum2 += len;
      }
      if (!hit) { hit = ed; off = ed.childNodes.length; }
      const r = document.createRange();
      r.setStart(hit, off);
      r.collapse(true);
      const s2 = window.getSelection();
      s2.removeAllRanges();
      s2.addRange(r);
    } catch (e) { /* 恢复失败不阻塞 */ }
  }
}

// 清除指定行的全部错误标注（wrong 文本保留原位）
// 2026-08-09 修复「清除后保存、载入历史版本建议复现」：除清空 proofreadErrors 外，
// 还需清掉 proofreadOriginal（否则 collectProofread 仍把陈旧快照写进历史）与
// proofreadDismissed，并 syncContent 把去标注后的 HTML 同步回 contentMap
// （否则 collect() 保存的仍是含 .ptoe-fix/.ptoe-err 的旧 HTML）。
// keepDismissed=true：仅供 runProofread 重新校正前内部清理使用（保留「已忽略」记忆，
// 否则用户点 ✗ 忽略过的条目会在下一轮校正中重新冒出来）。
function clearProofread(i, keepDismissed) {
  const row = host.children ? [...host.children].find(function (r) { return Number(r.dataset.i) === i; }) : null;
  if (!row) return;
  const ed = row.querySelector('.editable');
  if (!ed) return;
  const fixes = ed.querySelectorAll('.ptoe-fix');
  for (const el of fixes) el.parentNode.removeChild(el);
  const errs = ed.querySelectorAll('.ptoe-err');
  for (const el of errs) {
    const t = document.createTextNode(el.textContent);
    el.parentNode.replaceChild(t, el);
  }
  ed.normalize();
  proofreadErrors[i] = [];
  delete proofreadOriginal[i];
  if (!keepDismissed) delete proofreadDismissed[i];
  delete _prTextBefore[i];
  syncContent(ed);
}

// 反馈回写（fire-and-forget）：accept 上报采纳 / ignore 上报忽略；失败静默
function proofreadFeedbackAccept(wrong, fixed) {
  if (!wrong || !fixed || wrong === fixed) return;
  fetch('/api/proofread_feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'accept', wrong: wrong, fixed: fixed }),
  }).catch(function (e) { console.warn('proofread feedback accept failed', e); });
}
function proofreadFeedbackIgnore(wrong) {
  if (!wrong) return;
  fetch('/api/proofread_feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'ignore', wrong: wrong }),
  }).catch(function (e) { console.warn('proofread feedback ignore failed', e); });
}
// 批量 accept（proofreadApplyCurrent 用）
function proofreadFeedbackAcceptBatch(items) {
  if (!items || !items.length) return;
  fetch('/api/proofread_feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'accept', items: items }),
  }).catch(function (e) { console.warn('proofread feedback accept batch failed', e); });
}

// 重建行后若该页有标注数据，重新叠加（与搜索高亮回注同构）
const _displayHtmlOrig = displayHtml;
displayHtml = function (i) {
  const html = _displayHtmlOrig(i);
  return html;
};
function _reapplyProofread(i) {
  if (proofreadErrors[i] && proofreadErrors[i].length) {
    renderProofread(i);
  }
}

// 取去除标注后的纯文本（用于重锚定偏移）
// B: 同时剥离 .ptoe-marker（与渲染 walker 及服务端 _proofread_plain_text 偏移基准一致）
// 注意：.ptoe-err 内含 wrong 原文、.ptoe-fix 内含候选文本——均需解包保留原文，仅去掉标签
function _plainNoAnno(ed) {
  const c = ed.cloneNode(true);
  c.querySelectorAll('.ptoe-marker').forEach(function (el) { el.remove(); });
  c.querySelectorAll('.ptoe-fix').forEach(function (el) { el.parentNode.removeChild(el); });
  c.querySelectorAll('.ptoe-err').forEach(function (el) {
    const t = document.createTextNode(el.textContent);
    el.parentNode.replaceChild(t, el);
  });
  c.normalize();
  return c.textContent || '';
}

// C: 基准缓存——renderProofread 后同步，供 _proofreadAutoDismiss delta-rebase 使用
let _prTextBefore = {};
let _prRenderPending = {};   // IME composition 期间累积的待重建标记（skipRender=true 时置位，compositionend 后 _flushPrPending 统一重建）

// D1: 深拷贝 proofreadErrors[i]（条目为纯对象）
function _copyErrors(i) {
  return (typeof proofreadErrors[i] === 'undefined') ? undefined : JSON.parse(JSON.stringify(proofreadErrors[i]));
}

// C: delta-rebase 重锚定——基于编辑前后 diff 精确修正偏移，避免全局 indexOf 误锚
function _proofreadAutoDismiss(ed, i, skipRender) {
  const errors = proofreadErrors[i];
  if (!errors || !errors.length) return;
  const after = _plainNoAnno(ed);
  const before = _prTextBefore[i];
  let changed = false;
  if (typeof before === 'string' && before !== after) {
    // 编辑区间：公共前缀 p；公共后缀 → before 编辑区间 [p, be)、after 编辑区间 [p, ae)
    let p = 0;
    const n = Math.min(before.length, after.length);
    while (p < n && before[p] === after[p]) p++;
    let be = before.length, ae = after.length;
    while (be > p && ae > p && before[be - 1] === after[ae - 1]) { be--; ae--; }
    const delta = (ae - p) - (be - p);
    for (const err of errors) {
      if (err._gone) continue;
      if (err.end <= p) continue;                     // 编辑点之前：不动
      if (err.start >= be) {                          // 编辑点之后：整体平移
        err.start += delta; err.end += delta;
        // 平移后校验 wrong 仍完整（如 wrong 内部被插入字符则 slice 不匹配 → _gone）
        const w = err.wrong || '';
        if (err.start < 0 || err.end > after.length || after.slice(err.start, err.end) !== w) {
          err._gone = true;
        } else {
          err.line = 1 + (after.slice(0, err.start).match(/\n/g) || []).length;
        }
        changed = true;
      } else {
        // 编辑区间（[p, be)，before 坐标）与 err 区间重叠 = 用户直接改了 wrong 文本
        // （删除/替换/内部插入）→ 标注失效 _gone，绝不窗口搜索重锚（会误锚到相邻重复文本）
        err._gone = true;
        changed = true;
      }
    }
  } else if (typeof before !== 'string') {
    // 无基准（首次编辑前）→ 位置感知兜底：从 err.start 附近起搜，-1 回退全局；找不到 _gone
    errors.forEach(function (err) {
      if (err._gone) return;
      const wl = err.wrong ? err.wrong.length : 0;
      let idx = after.indexOf(err.wrong, Math.min(err.start, Math.max(0, after.length - wl)));
      if (idx < 0) idx = after.indexOf(err.wrong);
      if (idx < 0) { err._gone = true; }
      else if (err.start !== idx) { err.start = idx; err.end = idx + wl; }
      changed = true;
    });
  }
  _prTextBefore[i] = after;
  if (changed && !skipRender) renderProofread(i);   // 有变化才重建（性能：避免每次 input 全量重绘）；skipRender=true 时仅 rebase 不重建（IME composition 期间用）
}

// IME composition 期间累积的待重建统一 flush（compositionend 触发）——直接无条件 renderProofread，不走 changed 判断（composition 期间数据已 rebase、before==after 无 changed，会漏渲染）
function _flushPrPending() { for (const iStr in _prRenderPending) { if (!_prRenderPending[iStr]) continue; _prRenderPending[iStr] = false; const idx = Number(iStr); if (proofreadErrors[idx] && proofreadErrors[idx].length) renderProofread(idx); } }

// 纠错确认悬浮窗（恒显示 errOk：有候选=采纳候选，无候选=删除 wrong）
function showErrPopup(rect) {
  const pop = document.getElementById('errPopup');
  pop.style.display = 'flex';
  const r = pop.getBoundingClientRect();
  let left = rect.left + rect.width / 2 - r.width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - r.width - 8));
  let top = rect.top - r.height - 8;
  if (top < 8) top = rect.bottom + 8;
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';
}
function hideErrPopup() {
  document.getElementById('errPopup').style.display = 'none';
  errKey = null;
}

// ---------- 图片设置弹窗（点击编辑区内的图片弹出） ----------
let _imgKey = null; // { i, pEl, imgEl } 当前弹窗对应的图片
const _imgSizeClasses = ['ptoe-img-w25', 'ptoe-img-w50', 'ptoe-img-w75', 'ptoe-img-w100'];
const _imgPosClasses = ['ptoe-img-left', 'ptoe-img-center', 'ptoe-img-right'];
// 行内图片（ptoe-img-inline）用 vertical-align 控制上下位置；块级图片用 p 的 text-align
const _imgVAlignClasses = ['ptoe-img-vtop', 'ptoe-img-vmid', 'ptoe-img-vbot'];

function showImgPopup(rect) {
  const pop = document.getElementById('imgPopup');
  pop.style.display = 'flex';
  // 行内图片 → 显示「位置（行内）」顶/中/底行；块级图片 → 显示「位置」左/中/右行
  const isInline = !!( _imgKey && _imgKey.imgEl
    && _imgKey.imgEl.classList.contains('ptoe-img-inline'));
  document.getElementById('imgPosRow').style.display = isInline ? 'none' : 'flex';
  document.getElementById('imgVPosRow').style.display = isInline ? 'flex' : 'none';
  const r = pop.getBoundingClientRect();
  let left = rect.left + rect.width / 2 - r.width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - r.width - 8));
  let top = rect.top - r.height - 8;
  if (top < 8) top = rect.bottom + 8;
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';
}
function hideImgPopup() {
  document.getElementById('imgPopup').style.display = 'none';
  _imgKey = null;
}

// 自动跳转到下一处校正文本（按文档顺序）：采纳/忽略后调用，滚动到下一处并弹出采纳/忽略窗
// 用户点 采纳/忽略 后自动顺序推进，直至当前页无更多校正项。
function advanceToNextError(i, k) {
  const errors = proofreadErrors[i];
  if (!errors || !errors.length) { hideErrPopup(); return; }
  // 从 k 开始向后找第一个未 _gone 的条目（splice(k,1) 后原 k+1 移到 k）
  let nextK = -1;
  for (let idx = k; idx < errors.length; idx++) {
    if (errors[idx] && !errors[idx]._gone) { nextK = idx; break; }
  }
  if (nextK < 0) { hideErrPopup(); return; }
  const row = [...host.children].find(function (r) { return Number(r.dataset.i) === i; });
  if (!row) { hideErrPopup(); return; }
  const ed = row.querySelector('.editable');
  const el = ed.querySelector('.ptoe-err[data-err-i="' + nextK + '"]');
  if (!el) { hideErrPopup(); return; }
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  errKey = { i: i, k: nextK };
  showErrPopup(el.getBoundingClientRect());
}

// ---------- 文字纠错下拉菜单 ----------
function positionProofreadMenu() {
  const btn = document.getElementById('proofreadBtn');
  const menu = document.getElementById('proofreadMenu');
  if (!btn || !menu) return;
  const r = btn.getBoundingClientRect();
  let left = r.left;
  left = Math.max(8, Math.min(left, window.innerWidth - menu.offsetWidth - 8));
  menu.style.left = left + 'px';
  menu.style.top = (r.bottom + 2) + 'px';
}
function openProofreadMenu() {
  positionProofreadMenu();
  document.getElementById('proofreadMenu').style.display = 'block';
  proofreadMenuOpen = true;
  document.getElementById('proofreadBtn').classList.add('active');
}
function closeProofreadMenu() {
  document.getElementById('proofreadMenu').style.display = 'none';
  proofreadMenuOpen = false;
  document.getElementById('proofreadBtn').classList.remove('active');
}
function toggleProofreadMenu() {
  if (proofreadMenuOpen) closeProofreadMenu();
  else openProofreadMenu();
}

// 子项1 校正：对当前页执行纠错
function proofreadCorrect() {
  closeProofreadMenu();
  runProofread();
}

// 子项2 重识别：对当前页重新 OCR，差异以纠错标注叠加显示
async function runReocr() {
  closeProofreadMenu();
  const ed = ctxTargetEditable(); // 右键菜单目标页优先（与光标位置解耦）
  if (!ed) { showToast('请先点击某一页的文字', 'warn'); return; }
  const row = ed.closest('.page-row');
  if (!row) return;
  const i = Number(row.dataset.i);
  const page = pages[i].page;
  const model = proofreadLlmModel || '';
  const btn = document.getElementById('prMenuReocr');
  if (btn) btn.disabled = true;
  try {
    const res = await fetchJSON('/api/reocr', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ page: page, model: model, html: ed.innerHTML }),
    });
    if (!res.ok) { showToast('重识别失败: ' + (res.error || '未知错误'), 'fail'); return; }
    proofreadOriginal[i] = ed.innerHTML;
    proofreadErrors[i] = Array.isArray(res.diff) ? res.diff : [];
    lastProofreadPage = pages[i].page;
    if (proofreadErrors[i].length) {
      renderProofread(i);
      scheduleRemeasure(i);
      setStatus('第 ' + page + ' 页重识别完成，标注 ' + proofreadErrors[i].length + ' 处差异');
      showToast('第 ' + page + ' 页重识别完成，标注 ' + proofreadErrors[i].length + ' 处差异', 'ok');
    } else {
      setStatus('第 ' + page + ' 页重识别完成，未发现差异');
      showToast('第 ' + page + ' 页重识别完成，未发现差异', 'ok');
    }
  } catch (e) {
    showToast('重识别失败: ' + e.message, 'fail');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// 子项3 清除：清除当前页的纠错标注（删除线 + 候选字）；已应用的文字/词句保留不动
function proofreadClearCurrent() {
  closeProofreadMenu();
  const ed = ctxTargetEditable(); // 右键菜单目标页优先（与光标位置解耦）
  if (!ed) { showToast('请先点击某一页的文字', 'warn'); return; }
  const row = ed.closest('.page-row');
  if (!row) return;
  const i = Number(row.dataset.i);
  if (!ed.querySelector('.ptoe-err, .ptoe-fix')) { showToast('当前页没有纠错标注', 'warn'); setStatus('当前页没有纠错标注'); return; }
  clearProofread(i);
  hideErrPopup();
  scheduleRemeasure(i);
  showToast('已清除当前页纠错标注', 'ok');
  setStatus('已清除当前页纠错标注');
}

// 子项2 应用：把当前页所有有候选的提示替换为 candidates[0]；无候选（增字）=删除 wrong（支持跨多文本节点）
function proofreadApplyCurrent() {
  closeProofreadMenu();
  const ed = ctxTargetEditable(); // 右键菜单目标页优先（与光标位置解耦）
  if (!ed) { showToast('请先点击某一页的文字', 'warn'); return; }
  const row = ed.closest('.page-row');
  if (!row) return;
  const i = Number(row.dataset.i);
  const errors = proofreadErrors[i];
  if (!errors || !errors.length) { showToast('当前页没有可应用的纠错提示', 'warn'); setStatus('当前页没有可应用的纠错提示'); return; }
  // D6: 改文本主体包进 histRun（入撤销栈）
  histRun('应用纠错', [i], function () {
    let applied = 0;
    const acceptItems = []; // 收集批量 accept 反馈
    const appliedShifts = []; // 收集 {start, delta, origStart} 用于 rebase 剩余标注
    // 倒序处理，避免 DOM 修改影响后续 data-err-i 索引
    for (let idx = errors.length - 1; idx >= 0; idx--) {
      const err = errors[idx];
      if (err._gone) continue; // F5: 已标记消失的条目跳过
      const sEls = ed.querySelectorAll('.ptoe-err[data-err-i="' + idx + '"]');
      if (!sEls.length) continue;
      const fixEl = ed.querySelector('.ptoe-fix[data-err-i="' + idx + '"]');
      if (fixEl) fixEl.parentNode.removeChild(fixEl);
      const delta = (err.candidates && err.candidates.length ? err.candidates[0].length : 0) - (err.wrong ? err.wrong.length : 0);
      if (err.candidates && err.candidates.length) {
        // 有候选：首段替换为 candidates[0]，其余段删除
        sEls[0].parentNode.replaceChild(document.createTextNode(err.candidates[0]), sEls[0]);
        for (let p = 1; p < sEls.length; p++) sEls[p].parentNode.removeChild(sEls[p]);
        acceptItems.push({ wrong: err.wrong, fixed: err.candidates[0] }); // 收集反馈
      } else {
        // 无候选（增字）：全部段删除
        for (const s of sEls) s.parentNode.removeChild(s);
      }
      // D8: 收集原始坐标用于 rebase
      appliedShifts.push({ start: err.start, delta: delta });
      errors.splice(idx, 1);
      applied++;
    }
    // D8: rebase 剩余标注——用原始坐标判定，end 用新 start+wrong 长度
    for (const e of errors) {
      if (e._gone) continue;
      let d = 0;
      for (const as of appliedShifts) {
        if (e.start >= as.start) d += as.delta;
      }
      const origLen = e.end - e.start;
      e.start = e.start + d;
      e.end = e.start + origLen;
    }
    syncContent(ed); // 页面文本同步入 pages[i].text
    // 重建剩余标注（避免手动 reindex 导致 fix 索引漂移）
    renderProofread(i);
    // 批量反馈：应用全部
    proofreadFeedbackAcceptBatch(acceptItems);
    if (applied > 0) {
      showToast('已应用 ' + applied + ' 处纠错提示', 'ok');
      setStatus('已应用 ' + applied + ' 处纠错提示');
    } else {
      showToast('当前页没有可应用的纠错提示', 'warn');
      setStatus('当前页没有可应用的纠错提示');
    }
  });
  scheduleRemeasure(i);
}

// 子项4 回退：彻底恢复当前页校正前的原始文本（已应用的修改一并撤回）
function proofreadRevertCurrent() {
  closeProofreadMenu();
  const ed = currentEditable();
  if (!ed) { showToast('请先点击某一页的文字', 'warn'); return; }
  const row = ed.closest('.page-row');
  if (!row) return;
  const i = Number(row.dataset.i);
  if (!proofreadOriginal[i]) { showToast('当前页没有可回退的纠错操作', 'warn'); setStatus('当前页没有可回退的纠错操作'); return; }
  // D6: 改文本主体包进 histRun（入撤销栈）
  histRun('回退纠错', [i], function () {
    ed.innerHTML = proofreadOriginal[i];
    ed.normalize();
    proofreadErrors[i] = [];
    if (proofreadDismissed[i]) proofreadDismissed[i] = new Set();
    delete proofreadOriginal[i];
  });
  scheduleRemeasure(i);
  hideErrPopup();
  showToast('已回退当前页的纠错操作', 'ok');
  setStatus('已回退当前页的纠错操作');
}

// 点击 .ptoe-err 弹悬浮窗
document.addEventListener('click', function (e) {
  const el = e.target.closest('.ptoe-err');
  if (!el) return;
  e.stopPropagation();
  e.preventDefault();
  const row = el.closest('.page-row');
  if (!row) return;
  const i = Number(row.dataset.i);
  const k = Number(el.dataset.errI);
  const errors = proofreadErrors[i];
  if (!errors || !errors[k]) return;
  if (errors[k]._gone) return; // F5: 已标记消失的条目不响应点击
  errKey = { i: i, k: k };
  suppressPopupUntil = performance.now() + 250;
  showErrPopup(el.getBoundingClientRect());
});
// 采纳：有候选=替换为 candidates[0]；无候选（增字）=删除 wrong 文本（支持跨多文本节点）
document.getElementById('errOk').addEventListener('click', function () {
  if (!errKey) { hideErrPopup(); return; }
  const i = errKey.i, k = errKey.k;
  const errors = proofreadErrors[i];
  if (!errors || !errors[k] || errors[k]._gone) { hideErrPopup(); return; }
  const err = errors[k];
  const row = [...host.children].find(function (r) { return Number(r.dataset.i) === i; });
  if (!row) { hideErrPopup(); return; }
  const ed = row.querySelector('.editable');
  const sEls = ed.querySelectorAll('.ptoe-err[data-err-i="' + k + '"]');
  if (!sEls.length) { hideErrPopup(); return; }
  // D6: 改文本主体包进 histRun（入撤销栈）
  hideErrPopup();
  histRun('采纳纠错', [i], function () {
    // 删 fixEl（若存在，只在首段）
    const fixEl2 = ed.querySelector('.ptoe-fix[data-err-i="' + k + '"]');
    if (fixEl2) fixEl2.parentNode.removeChild(fixEl2);
    // 计算 delta：整条 wrong 被替换为 candidates[0]（或删除）
    const delta = (err.candidates && err.candidates.length ? err.candidates[0].length : 0) - (err.wrong ? err.wrong.length : 0);
    if (err.candidates && err.candidates.length) {
      // 有候选：首段替换为 candidates[0]，其余段删除
      sEls[0].parentNode.replaceChild(document.createTextNode(err.candidates[0]), sEls[0]);
      for (let p = 1; p < sEls.length; p++) sEls[p].parentNode.removeChild(sEls[p]);
      // 反馈：采纳候选
      proofreadFeedbackAccept(err.wrong, err.candidates[0]);
    } else {
      // 无候选（增字）：全部段删除
      for (const s of sEls) s.parentNode.removeChild(s);
    }
    ed.normalize();
    errors.splice(k, 1);
    // rebase 剩余标注：非重叠，右侧整体平移 delta
    for (let e of errors) {
      if (!e._gone && e.start >= err.end) { e.start += delta; e.end += delta; }
    }
    syncContent(ed); // 页面文本同步入 pages[i].text
    // 重建剩余标注（避免手动 reindex 导致 fix 索引漂移）
    renderProofread(i);
  });
  scheduleRemeasure(i);
  advanceToNextError(i, k); // 自动跳转到下一处校正文本
});
// 忽略：移除标注恢复完整 wrong 文本，加入 dismissed（支持跨多文本节点）
document.getElementById('errNo').addEventListener('click', function () {
  if (!errKey) { hideErrPopup(); return; }
  const i = errKey.i, k = errKey.k;
  const errors = proofreadErrors[i];
  if (!errors || !errors[k] || errors[k]._gone) { hideErrPopup(); return; }
  const err = errors[k];
  const row = [...host.children].find(function (r) { return Number(r.dataset.i) === i; });
  if (!row) { hideErrPopup(); return; }
  const ed = row.querySelector('.editable');
  const sEls = ed.querySelectorAll('.ptoe-err[data-err-i="' + k + '"]');
  if (!sEls.length) { hideErrPopup(); return; }
  // D6: 改文本主体包进 histRun（入撤销栈）
  hideErrPopup();
  histRun('忽略纠错', [i], function () {
    // 反馈：忽略该词（在状态清除前捕获 wrong）
    proofreadFeedbackIgnore(err.wrong);
    // 删 fixEl（若存在）
    const fixEl2 = ed.querySelector('.ptoe-fix[data-err-i="' + k + '"]');
    if (fixEl2) fixEl2.parentNode.removeChild(fixEl2);
    // 解包：每段 .ptoe-err 替换为等文本的 textNode（wrong 文本保留原位，不重定位）
    for (const s of sEls) {
      const t = document.createTextNode(s.textContent);
      s.parentNode.replaceChild(t, s);
    }
    ed.normalize();
    if (!proofreadDismissed[i]) proofreadDismissed[i] = new Set();
    proofreadDismissed[i].add(err.start + ':' + err.wrong);
    errors.splice(k, 1);
    syncContent(ed); // 页面文本同步入 pages[i].text
    // 重建剩余标注（避免手动 reindex 导致 fix 索引漂移）
    renderProofread(i);
  });
  scheduleRemeasure(i);
  advanceToNextError(i, k); // 自动跳转到下一处校正文本
});
// 点击编辑区外 / 滚动 / 选区折叠 → 关闭悬浮窗
document.addEventListener('mousedown', function (e) {
  if (errKey && !e.target.closest('#errPopup')) hideErrPopup();
  // 点击菜单外（且不在按钮上）→ 关闭下拉菜单
  if (proofreadMenuOpen && !e.target.closest('#proofreadMenu') && !e.target.closest('#proofreadBtn')) {
    closeProofreadMenu();
  }
  // 点击图片弹窗外 → 关闭图片弹窗
  if (_imgKey && !e.target.closest('#imgPopup')) hideImgPopup();
});
// 点击编辑区内的图片 → 弹出图片设置弹窗（行内图片可能没有 <p> 包裹，pEl 允许为 null）
document.addEventListener('click', function (e) {
  const imgEl = e.target.closest('.editable img');
  if (!imgEl) return;
  const pEl = imgEl.closest('p') || null;
  const row = imgEl.closest('.page-row');
  if (!row) return;
  const i = Number(row.dataset.i);
  e.stopPropagation();
  e.preventDefault();
  _imgKey = { i: i, pEl: pEl, imgEl: imgEl };
  suppressPopupUntil = performance.now() + 300; // 抑制选中文字快捷菜单
  showImgPopup(imgEl.getBoundingClientRect());
});
// 图片弹窗按钮：大小/位置/删除
document.getElementById('imgPopup').addEventListener('click', function (e) {
  const btn = e.target.closest('.img-pop-btn');
  if (!btn || !_imgKey) return;
  const op = btn.dataset.imgOp;
  const val = btn.dataset.imgVal;
  const { i, pEl, imgEl } = _imgKey;
  const ed = [...host.children].find(function (r) { return Number(r.dataset.i) === i; }).querySelector('.editable');
  if (op === 'size') {
    // 设置图片大小：原尺寸=移除尺寸 class，其他=交换到对应 ptoe-img-w* class
    histRun('设置图片大小', [i], function () {
      _imgSizeClasses.forEach(function (c) { imgEl.classList.remove(c); });
      if (val !== 'original') imgEl.classList.add('ptoe-img-' + val);
      syncContent(ed);
    });
    scheduleRemeasure(i);
    // 大小操作保持弹窗打开，便于连续调整
  } else if (op === 'pos') {
    // 设置图片位置：行内图片 → vertical-align（顶/中/底）；
    // 块级图片 → 交换 ptoe-img-left/center/right class（p 的 text-align）
    const isInline = imgEl.classList.contains('ptoe-img-inline');
    histRun('设置图片位置', [i], function () {
      if (isInline) {
        _imgVAlignClasses.forEach(function (c) { imgEl.classList.remove(c); });
        imgEl.classList.add('ptoe-img-' + val);
      } else {
        _imgPosClasses.forEach(function (c) { pEl.classList.remove(c); });
        pEl.classList.add('ptoe-img-' + val);
      }
      syncContent(ed);
    });
    scheduleRemeasure(i);
    // 位置操作保持弹窗打开
  } else if (op === 'delete') {
    // 删除图片：行内图片只移除 <img> 本身（保留周围文字）；块级图片移除整个 <p> 包裹
    const isInline = imgEl.classList.contains('ptoe-img-inline');
    histRun('删除图片', [i], function () {
      if (isInline) {
        if (imgEl.parentNode) imgEl.parentNode.removeChild(imgEl);
      } else if (pEl && pEl.parentNode) {
        pEl.parentNode.removeChild(pEl);
      }
      syncContent(ed);
    });
    scheduleRemeasure(i);
    hideImgPopup();
  }
});
// Esc 关闭图片弹窗
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape' && _imgKey) { hideImgPopup(); }
});

// 字号下拉：仅调整编辑区显示字号（CSS 变量 --editor-font-size；视图偏好，不写入保存内容）
function applyFontSize(v) {
  document.documentElement.style.setProperty('--editor-font-size', (v || 14) + 'px');
  setStatus('编辑字号：' + (v || 14) + 'px');
}

// ---------- 格式规则（弹窗管理 + 条件列表/求值模式应用） ----------
const FORMAT_RULE_OPTS = [
  ['none','无（不对文本处理）'], ['bold','加粗'], ['italic','斜体'], ['align_center','居中'], ['align_left','居左'],
  ['align_right','居右'], ['heading1','标题1'], ['heading2','标题2'], ['heading3','标题3'],
  ['heading4','标题4'], ['heading5','标题5'], ['heading6','标题6'],
  ['p','正文'], ['merge','合并段落'], ['note','注释'], ['remove','清除格式'],
];
let formatRules = [];
let formatRuleEditingId = null;
let _frRange = null; // 打开格式规则弹窗时捕获的选区（应用前恢复，避免 selection 丢失）

// 格式冲突模型（2026-08-15）：块标签互斥（p/h1-6 同一块只能一个）、对齐互斥
// （align_left/center/right 同一块只能一个）；bold/italic/note 相互独立可共存；
// remove 与任何其他格式冲突（会清除全部格式）。
const FORMAT_OP_GROUPS = {
  block_tag: ['p','heading1','heading2','heading3','heading4','heading5','heading6'],
  align: ['align_left','align_center','align_right'],
  merge: ['merge'],
};
function opGroup(op) {
  for (const g in FORMAT_OP_GROUPS) if (FORMAT_OP_GROUPS[g].includes(op)) return g;
  return null;
}
function opsConflict(a, b) {
  if (a === b) return false;
  if (a === 'remove' || b === 'remove') return true;
  const ga = opGroup(a), gb = opGroup(b);
  if (ga === null || gb === null) return false;
  return ga === gb;
}
// 正则条件支持 /pattern/flags 语法（2026-08-15）：无斜杠包裹时按普通表达式处理
function parseRegexPattern(pattern) {
  const m = /^\/(.+)\/([a-z]*)$/.exec(pattern);
  return m ? { pattern: m[1], flags: m[2] } : { pattern: pattern, flags: '' };
}
// 统计正则表达式中的捕获组数量（跳过转义的 \( 和非捕获组 (?:...）
function _countCaptureGroups(pattern) {
  var rp = parseRegexPattern(pattern);
  var pat = rp.pattern;
  var count = 0;
  for (var i = 0; i < pat.length; i++) {
    if (pat[i] === '\\') { i++; continue; }
    if (pat[i] === '(' && pat[i + 1] !== '?') count++;
  }
  return count;
}
// 保存时冲突预警：两条规则存在相同条件（type+pattern+scope）且格式互斥时提示。
// 新模型下规则含 conditions 列表：返回该规则全部条件的键集合。
function ruleConditionKey(r) {
  const keys = new Set();
  for (const c of (r && r.conditions) || []) {
    keys.add((c.type || 'contains') + '|' + (c.pattern || '') + '|' + (c.scope || 'selection'));
  }
  return keys;
}
function rulesConflict(a, b) {
  const ka = ruleConditionKey(a), kb = ruleConditionKey(b);
  if (!ka.size || !kb.size) return false;
  let shared = false;
  for (const k of ka) { if (kb.has(k)) { shared = true; break; } }
  if (!shared) return false;
  // 格式取全部条件的并集（含 none，none 无冲突组，opsConflict 恒 false）
  const opsA = [], opsB = [];
  for (const c of (a && a.conditions) || []) opsA.push.apply(opsA, c.formats || []);
  for (const c of (b && b.conditions) || []) opsB.push.apply(opsB, c.formats || []);
  return opsA.some(function (x) { return opsB.some(function (y) { return opsConflict(x, y); }); });
}

function _escHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
function fmtSummary(fmts) {
  if (!fmts || !fmts.length) return '（无）';
  const names = {};
  for (const o of FORMAT_RULE_OPTS) names[o[0]] = o[1];
  return fmts.map(function (op) { return names[op] || op; }).join('、');
}
function condSummary(rule) {
  const conds = (rule && rule.conditions) || [];
  if (!conds.length) return '无条件';
  const tmap = { contains: '包含', prefix: '开头', suffix: '结尾', regex: '正则匹配' };
  return conds.map(function (c) {
    const t = tmap[c.type] || c.type;
    const scope = c.scope === 'paragraph' ? '整段' : (c.scope === 'page' ? '当前页' : '选中');
    const tgtMap = { before: '之前', after: '之后' };
    const tgtLabel = c.target === 'between' ? '之间「' + (c.between_end_pattern || '?') + '」' : (tgtMap[c.target] || '');
    const cond = c.pattern ? t + '「' + c.pattern + '」/' + scope + (tgtLabel ? '→' + tgtLabel : '') : '无条件';
    if (c.type === 'regex' && c.group_formats && c.group_formats.length) {
      var parts = [];
      for (var gi = 0; gi < c.group_formats.length; gi++) {
        var gf = c.group_formats[gi] || [];
        if (gf.length) parts.push('组' + (gi + 1) + ':' + fmtSummary(gf));
      }
      return cond + (parts.length ? ' → ' + parts.join('，') : ' → ' + fmtSummary(c.formats));
    }
    return cond + ' → ' + fmtSummary(c.formats);
  }).join('；');
}

async function openFormatRulesModal() {
  // 捕获当前选区（若在编辑区内），应用规则前恢复——弹窗打开会清空/改变 selection
  const sel = window.getSelection();
  if (sel && sel.rangeCount > 0) {
    const node = sel.getRangeAt(0).commonAncestorContainer;
    const el = node && node.nodeType === 3 ? node.parentNode : node;
    if (el && el.closest && el.closest('.editable')) _frRange = sel.getRangeAt(0).cloneRange();
  }
  try {
    const res = await fetchJSON('/api/format_rules');
    formatRules = (res && res.rules) || [];
  } catch (e) { formatRules = []; }
  formatRuleEditingId = null;
  document.getElementById('frRuleModalBg').style.display = 'none';
  document.getElementById('frFmtPopupBg').style.display = 'none';
  renderFormatRules();
  document.getElementById('formatRulesModalBg').style.display = 'flex';
}
function closeFormatRulesModal() {
  document.getElementById('formatRulesModalBg').style.display = 'none';
}
function restoreFrRange() {
  if (!_frRange) return;
  const sel = window.getSelection();
  if (!sel) return;
  sel.removeAllRanges();
  sel.addRange(_frRange);
}
function renderFormatRules() {
  const tbody = document.getElementById('formatRulesBody');
  tbody.innerHTML = '';
  if (!formatRules.length) {
    tbody.innerHTML = '<tr><td colspan="4" style="color:#9aa7b4;padding:10px 8px;">暂无规则，点击「新建规则」创建</td></tr>';
    return;
  }
  formatRules.forEach(function (rule, idx) {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td class="fr-order">' + (idx + 1) + '</td>' +
      '<td class="fr-name">' + _escHtml(rule.name) + '</td>' +
      '<td class="fr-sum">' + _escHtml(condSummary(rule)) + '</td>' +
      '<td style="white-space:nowrap;">' +
        '<button type="button" class="fr-up" title="上移"' + (idx === 0 ? ' disabled' : '') + '>↑</button> ' +
        '<button type="button" class="fr-down" title="下移"' + (idx === formatRules.length - 1 ? ' disabled' : '') + '>↓</button> ' +
        '<button type="button" class="fr-apply">应用</button> ' +
        '<button type="button" class="fr-edit">编辑</button> ' +
        '<button type="button" class="fr-del">删除</button>' +
      '</td>';
    tr.querySelector('.fr-up').addEventListener('click', function () { moveFormatRule(rule, -1); });
    tr.querySelector('.fr-down').addEventListener('click', function () { moveFormatRule(rule, 1); });
    tr.querySelector('.fr-apply').addEventListener('click', function () {
      if (applyFormatRule(rule)) closeFormatRulesModal();
    });
    tr.querySelector('.fr-edit').addEventListener('click', function () { editFormatRule(rule); });
    tr.querySelector('.fr-del').addEventListener('click', function () { deleteFormatRule(rule); });
    tbody.appendChild(tr);
  });
}
function moveFormatRule(rule, dir) {
  var idx = formatRules.findIndex(function (r) { return r.id === rule.id; });
  if (idx === -1) return;
  var j = idx + dir;
  if (j < 0 || j >= formatRules.length) return;
  var moved = formatRules[idx];
  formatRules.splice(idx, 1);
  formatRules.splice(j, 0, moved);
  renderFormatRules();
  persistFormatRules();
}
function renderFmtOptions(containerId) {
  const box = document.getElementById(containerId);
  box.innerHTML = '';
  FORMAT_RULE_OPTS.forEach(function (o) {
    const label = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.value = o[0];
    label.appendChild(cb);
    label.appendChild(document.createTextNode(o[1]));
    box.appendChild(label);
  });
}
function setFmtChecks(containerId, ops) {
  const set = {};
  (ops || []).forEach(function (op) { set[op] = true; });
  document.querySelectorAll('#' + containerId + ' input[type="checkbox"]').forEach(function (cb) { cb.checked = !!set[cb.value]; });
}
function collectFmtChecks(containerId) {
  const out = [];
  document.querySelectorAll('#' + containerId + ' input[type="checkbox"]:checked').forEach(function (cb) { out.push(cb.value); });
  return out;
}
// ---- 条件列表编辑（独立弹窗 #frRuleModalBg） ----
let _frConds = []; // 编辑中的条件列表（镜像 DOM；select/input 实时值经 syncCondsFromDom 读回）
let _frFmtIdx = -1; // 当前打开格式弹窗的条件下标
let _frFmtGroupIdx = -1; // 当前打开格式弹窗的 group_formats 下标（-1=条件级）
const _FR_COND_TYPES = [
  ['regex','正则匹配'], ['contains','包含文字'], ['prefix','以…开头'], ['suffix','以…结尾'],
];
const _FR_COND_TARGETS = [
  ['match','匹配对象'], ['before','条件之前'], ['after','条件之后'], ['between','两条件之间'],
];
// 把 DOM 中每行 select/input 的实时值同步回 _frConds（formats 保留 _frConds 中的）
function syncCondsFromDom() {
  const rows = document.querySelectorAll('#frConditions .fr-cond-row');
  rows.forEach(function (row, idx) {
    const base = _frConds[idx] || { formats: [] };
    const tgtEl = row.querySelector('.frCondTarget');
    const endEl = row.querySelector('.frCondEndPattern');
    _frConds[idx] = {
      type: row.querySelector('.frCondType').value,
      pattern: row.querySelector('.frCondPattern').value,
      scope: row.querySelector('.frCondScope').value,
      formats: (base.formats || []).slice(),
      group_formats: (base.group_formats || []).map(function (g) { return (g || []).slice(); }),
      target: tgtEl ? tgtEl.value : (base.target || 'match'),
      between_end_pattern: endEl ? endEl.value : (base.between_end_pattern || ''),
    };
  });
}
function renderConditions(conds) {
  _frConds = conds;
  const box = document.getElementById('frConditions');
  box.innerHTML = '';
  if (!_frConds.length) {
    box.innerHTML = '<div style="color:#9aa7b4;font-size:12px;">暂无条件，点击「添加条件」创建</div>';
    return;
  }
  const names = {};
  for (const o of FORMAT_RULE_OPTS) names[o[0]] = o[1];
  _frConds.forEach(function (c, idx) {
    const row = document.createElement('div');
    row.className = 'fr-cond-row';
    const typeSel = document.createElement('select');
    typeSel.className = 'frCondType';
    _FR_COND_TYPES.forEach(function (t) {
      const opt = document.createElement('option');
      opt.value = t[0]; opt.textContent = t[1];
      if (t[0] === (c.type || 'contains')) opt.selected = true;
      typeSel.appendChild(opt);
    });
    const patInput = document.createElement('input');
    patInput.type = 'text'; patInput.className = 'frCondPattern';
    patInput.placeholder = '条件内容（正则填表达式，留空=无条件）';
    patInput.value = c.pattern || '';
    const scopeSel = document.createElement('select');
    scopeSel.className = 'frCondScope';
    [['selection','选中文字'], ['paragraph','光标所在段落'], ['page','当前页面']].forEach(function (t) {
      const opt = document.createElement('option');
      opt.value = t[0]; opt.textContent = t[1];
      if (t[0] === (c.scope || 'selection')) opt.selected = true;
      scopeSel.appendChild(opt);
    });
    // target 选择器：决定格式作用于匹配文本/之前/之后/两条件之间
    const targetSel = document.createElement('select');
    targetSel.className = 'frCondTarget';
    targetSel.style.cssText = 'margin-left:6px;';
    _FR_COND_TARGETS.forEach(function (t) {
      const opt = document.createElement('option');
      opt.value = t[0]; opt.textContent = t[1];
      if (t[0] === (c.target || 'match')) opt.selected = true;
      targetSel.appendChild(opt);
    });
    // between 模式的结束条件 pattern 输入
    const endPatInput = document.createElement('input');
    endPatInput.type = 'text'; endPatInput.className = 'frCondEndPattern';
    endPatInput.placeholder = '结束条件（正则/文字）';
    endPatInput.value = c.between_end_pattern || '';
    endPatInput.style.cssText = 'margin-left:6px;width:120px;display:' + ((c.target || 'match') === 'between' ? 'inline-block' : 'none') + ';';
    targetSel.addEventListener('change', function () {
      endPatInput.style.display = targetSel.value === 'between' ? 'inline-block' : 'none';
    });
    const fmtBtn = document.createElement('button');
    fmtBtn.type = 'button'; fmtBtn.className = 'frFmtBtn';
    fmtBtn.textContent = '格式';
    fmtBtn.title = '设置该条件的格式（含「无」= 不处理文本）';
    fmtBtn.addEventListener('click', function () { openFmtPopup(idx); });
    const tags = document.createElement('span');
    tags.className = 'fr-tags';
    const fmts = c.formats || [];
    if (fmts.length) {
      fmts.forEach(function (op) {
        const tag = document.createElement('span');
        tag.className = 'fr-tag' + (op === 'none' ? ' fr-tag-none' : '');
        tag.textContent = names[op] || op;
        tags.appendChild(tag);
      });
    } else {
      const empty = document.createElement('span');
      empty.className = 'fr-tags-empty';
      empty.textContent = '未设置格式';
      tags.appendChild(empty);
    }
    const upBtn = document.createElement('button');
    upBtn.type = 'button'; upBtn.className = 'frCondUp'; upBtn.textContent = '↑';
    upBtn.title = '上移'; upBtn.disabled = idx === 0;
    upBtn.addEventListener('click', function () { moveCondition(idx, -1); });
    const downBtn = document.createElement('button');
    downBtn.type = 'button'; downBtn.className = 'frCondDown'; downBtn.textContent = '↓';
    downBtn.title = '下移'; downBtn.disabled = idx === _frConds.length - 1;
    downBtn.addEventListener('click', function () { moveCondition(idx, 1); });
    const delBtn = document.createElement('button');
    delBtn.type = 'button'; delBtn.className = 'frCondDel'; delBtn.textContent = '✕';
    delBtn.title = '删除条件';
    delBtn.addEventListener('click', function () { removeCondition(idx); });
    row.appendChild(typeSel);
    row.appendChild(patInput);
    row.appendChild(scopeSel);
    row.appendChild(targetSel);
    row.appendChild(endPatInput);
    row.appendChild(fmtBtn);
    row.appendChild(tags);
    row.appendChild(upBtn);
    row.appendChild(downBtn);
    row.appendChild(delBtn);
    box.appendChild(row);
    // regex 条件：检测捕获组并显示 per-group 格式编辑行
    if (c.type === 'regex' && c.pattern) {
      var gCount = _countCaptureGroups(c.pattern);
      if (gCount > 0) {
        var gf = c.group_formats || [];
        for (var gi = 0; gi < gCount; gi++) {
          var grow = document.createElement('div');
          grow.className = 'fr-cond-row fr-group-row';
          grow.style.cssText = 'padding-left:2em;font-size:12px;opacity:.85;min-height:0;';
          var gLabel = document.createElement('span');
          gLabel.style.cssText = 'margin-right:4px;white-space:nowrap;';
          gLabel.textContent = '组' + (gi + 1) + '：';
          grow.appendChild(gLabel);
          var gFmtBtn = document.createElement('button');
          gFmtBtn.type = 'button'; gFmtBtn.className = 'frFmtBtn';
          gFmtBtn.textContent = '格式';
          gFmtBtn.title = '设置捕获组 ' + (gi + 1) + ' 的独立格式';
          (function(gIdx) {
            gFmtBtn.addEventListener('click', function () { openFmtPopup(idx, gIdx); });
          })(gi);
          grow.appendChild(gFmtBtn);
          var gTags = document.createElement('span');
          gTags.className = 'fr-tags';
          var gFmtList = gf[gi] || [];
          if (gFmtList.length) {
            gFmtList.forEach(function (op) {
              var tag = document.createElement('span');
              tag.className = 'fr-tag' + (op === 'none' ? ' fr-tag-none' : '');
              tag.textContent = names[op] || op;
              gTags.appendChild(tag);
            });
          } else {
            var gEmpty = document.createElement('span');
            gEmpty.className = 'fr-tags-empty';
            gEmpty.textContent = '未设置格式';
            gTags.appendChild(gEmpty);
          }
          grow.appendChild(gTags);
          box.appendChild(grow);
        }
      }
    }
  });
}
function addCondition() {
  syncCondsFromDom();
  _frConds.push({ type: 'contains', pattern: '', scope: 'page', formats: [], group_formats: [], target: 'match', between_end_pattern: '' });
  renderConditions(_frConds);
}
function removeCondition(idx) {
  syncCondsFromDom();
  _frConds.splice(idx, 1);
  renderConditions(_frConds);
}
function moveCondition(idx, dir) {
  syncCondsFromDom();
  const j = idx + dir;
  if (j < 0 || j >= _frConds.length) return;
  const c = _frConds[idx];
  _frConds.splice(idx, 1);
  _frConds.splice(j, 0, c);
  renderConditions(_frConds);
}
// 格式弹窗：勾选该条件的格式（含「无」= 不处理文本）；groupIdx>=0 时编辑分组格式
function openFmtPopup(idx, groupIdx) {
  syncCondsFromDom();
  _frFmtIdx = idx;
  _frFmtGroupIdx = (typeof groupIdx === 'number') ? groupIdx : -1;
  renderFmtOptions('frFmtOpts');
  if (_frFmtGroupIdx >= 0) {
    var gf = _frConds[idx].group_formats || [];
    setFmtChecks('frFmtOpts', gf[_frFmtGroupIdx] || []);
  } else {
    setFmtChecks('frFmtOpts', _frConds[idx].formats);
  }
  document.getElementById('frFmtPopupBg').style.display = 'flex';
}
function confirmFmtPopup() {
  syncCondsFromDom();
  if (_frFmtIdx < 0 || _frFmtIdx >= _frConds.length) { closeFmtPopup(); return; }
  var newFmt = collectFmtChecks('frFmtOpts');
  if (_frFmtGroupIdx >= 0) {
    var gf = _frConds[_frFmtIdx].group_formats || [];
    while (gf.length <= _frFmtGroupIdx) gf.push([]);
    gf[_frFmtGroupIdx] = newFmt;
    _frConds[_frFmtIdx].group_formats = gf;
  } else {
    _frConds[_frFmtIdx].formats = newFmt;
  }
  closeFmtPopup();
  renderConditions(_frConds);
}
function closeFmtPopup() {
  document.getElementById('frFmtPopupBg').style.display = 'none';
  _frFmtIdx = -1;
  _frFmtGroupIdx = -1;
}
function editFormatRule(rule) {
  formatRuleEditingId = rule.id || null;
  document.getElementById('frName').value = rule.name || '';
  document.getElementById('frMode').value = rule.mode || 'first';
  renderConditions((rule.conditions || []).map(function (c) {
    return { type: c.type, pattern: c.pattern, scope: c.scope, formats: (c.formats || []).slice(),
      group_formats: (c.group_formats || []).map(function (g) { return (g || []).slice(); }),
      target: c.target || 'match', between_end_pattern: c.between_end_pattern || '',
    };
  }));
  document.getElementById('frRuleModalBg').style.display = 'flex';
}
function newFormatRule() {
  formatRuleEditingId = null;
  document.getElementById('frName').value = '';
  document.getElementById('frMode').value = 'first';
  renderConditions([{ type: 'contains', pattern: '', scope: 'page', formats: [], target: 'match', between_end_pattern: '' }]);
  document.getElementById('frRuleModalBg').style.display = 'flex';
}
function closeRuleModal() {
  document.getElementById('frRuleModalBg').style.display = 'none';
  formatRuleEditingId = null;
}
async function persistFormatRules() {
  try {
    const res = await fetchJSON('/api/format_rules', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rules: formatRules }),
    });
    if (!res.ok) { showToast('保存失败: ' + (res.error || '未知错误'), 'fail'); return false; }
    formatRules = res.rules || formatRules;
    return true;
  } catch (e) { showToast('保存失败: ' + e, 'fail'); return false; }
}
async function saveFormatRule() {
  const name = document.getElementById('frName').value.trim();
  if (!name) { showToast('请填写规则名称', 'warn'); return; }
  syncCondsFromDom();
  if (!_frConds.length) { showToast('请至少添加一个条件', 'warn'); return; }
  // 校验每个条件的正则表达式
  for (const c of _frConds) {
    if (c.type === 'regex' && c.pattern) {
      try { new RegExp(c.pattern); } catch (e) { showToast('正则表达式无效: ' + e.message, 'fail'); return; }
    }
  }
  const rule = {
    name: name,
    mode: document.getElementById('frMode').value,
    conditions: _frConds.map(function (c) {
      var cond = { type: c.type, pattern: c.pattern, scope: c.scope, formats: (c.formats || []).slice() };
      if (c.type === 'regex' && c.group_formats && c.group_formats.length) {
        cond.group_formats = c.group_formats.map(function (g) { return (g || []).slice(); });
      }
      if (c.target && c.target !== 'match') cond.target = c.target;
      if (c.target === 'between' && c.between_end_pattern) cond.between_end_pattern = c.between_end_pattern;
      return cond;
    }),
  };
  if (formatRuleEditingId) rule.id = formatRuleEditingId;
  // 保存前冲突预警：与既有规则（排除正在编辑的）存在相同条件且格式互斥时提示
  const clash = formatRules.find(function (r) { return r.id !== formatRuleEditingId && rulesConflict(r, rule); });
  if (clash && !confirm('规则「' + rule.name + '」与「' + clash.name + '」存在相同条件且格式冲突（对齐/块标签互斥），执行时后者将被跳过。仍要保存？')) return;
  const idx = formatRules.findIndex(function (r) { return r.id === rule.id; });
  if (idx >= 0) formatRules[idx] = rule; else formatRules.push(rule);
  const ok = await persistFormatRules();
  if (!ok) return;
  closeRuleModal();
  renderFormatRules();
}
async function deleteFormatRule(rule) {
  if (!confirm('删除规则「' + rule.name + '」？')) return;
  formatRules = formatRules.filter(function (r) { return r.id !== rule.id; });
  const ok = await persistFormatRules();
  if (!ok) return;
  renderFormatRules();
}

// ---- 规则应用引擎：条件评估 → 求值模式 → 叠加应用 ----

// 构建文本节点偏移表：遍历 root 下所有文本节点，返回 [{node, start, end}]
function _buildTextNodeList(root) {
  var nodes = [];
  var offset = 0;
  var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
  var node;
  while ((node = walker.nextNode())) {
    var len = node.textContent.length;
    nodes.push({ node: node, start: offset, end: offset + len });
    offset += len;
  }
  return nodes;
}
// 根据字符偏移量创建 DOM Range（映射到文本节点列表）
function _rangeFromOffsets(textNodes, startOff, endOff) {
  if (startOff >= endOff || !textNodes.length) return null;
  var startNode = null, startIdx = 0, endNode = null, endIdx = 0;
  for (var i = 0; i < textNodes.length; i++) {
    var tn = textNodes[i];
    if (!startNode && tn.end > startOff) {
      startNode = tn.node;
      startIdx = startOff - tn.start;
    }
    if (tn.end >= endOff) {
      endNode = tn.node;
      endIdx = endOff - tn.start;
      break;
    }
  }
  if (!startNode || !endNode) return null;
  try {
    var range = document.createRange();
    range.setStart(startNode, Math.min(startIdx, startNode.textContent.length));
    range.setEnd(endNode, Math.min(endIdx, endNode.textContent.length));
    return range;
  } catch (e) { return null; }
}
// 正则捕获组独立格式应用：对 scope 文本运行正则，每个捕获组匹配独立应用格式
function applyRegexGroupFormats(cond, ed) {
  if (!cond || !cond.group_formats || !cond.group_formats.length) return;
  if (!cond.pattern) return;
  // 获取作用域文本
  var sel = window.getSelection();
  var text = '';
  if (cond.scope === 'page') {
    text = ed && ed.innerText ? ed.innerText : '';
  } else if (cond.scope === 'paragraph') {
    var pnode = sel && sel.rangeCount ? sel.getRangeAt(0).startContainer : ed;
    if (pnode.nodeType === 3) pnode = pnode.parentElement;
    if (!pnode || !ed.contains(pnode)) pnode = ed;
    var block = pnode.closest ? pnode.closest('p,div,h1,h2,h3,h4,h5,h6') : null;
    text = (block ? block.innerText : '') || '';
  } else {
    text = sel && sel.rangeCount ? (sel.toString() || '') : '';
  }
  if (!text) return;
  var rp = parseRegexPattern(cond.pattern);
  var flags = rp.flags.indexOf('g') >= 0 ? rp.flags : rp.flags + 'g';
  var re;
  try { re = new RegExp(rp.pattern, flags); } catch (e) { return; }
  var textNodes = _buildTextNodeList(ed);
  var match;
  while ((match = re.exec(text)) !== null) {
    if (!match[0]) { re.lastIndex++; continue; } // 防零宽匹配死循环
    var offset = 0;
    for (var gi = 0; gi < cond.group_formats.length; gi++) {
      var fmts = cond.group_formats[gi] || [];
      if (!fmts.length || (fmts.length === 1 && fmts[0] === 'none')) continue;
      var groupText = match[gi + 1];
      if (!groupText) continue;
      // 定位该捕获组在全匹配中的偏移
      var posInMatch = match[0].indexOf(groupText, offset);
      if (posInMatch < 0) continue;
      var groupStart = match.index + posInMatch;
      var groupEnd = groupStart + groupText.length;
      offset = posInMatch + groupText.length;
      var range = _rangeFromOffsets(textNodes, groupStart, groupEnd);
      if (!range) continue;
      sel.removeAllRanges();
      sel.addRange(range);
      for (var fi = 0; fi < fmts.length; fi++) {
        var op = fmts[fi];
        if (op === 'none') continue;
        applySingleFormat(op, ed);
      }
    }
  }
}

// 正则按匹配对象独立格式应用（match_formats）：对同一正则的每次匹配可设置不同格式
function applyRegexMatchFormats(cond, ed) {
  if (!cond || !cond.match_formats || !cond.match_formats.length) return;
  if (!cond.pattern) return;
  var sel = window.getSelection();
  var text = '';
  if (cond.scope === 'page') {
    text = ed && ed.innerText ? ed.innerText : '';
  } else if (cond.scope === 'paragraph') {
    var pnode = sel && sel.rangeCount ? sel.getRangeAt(0).startContainer : ed;
    if (pnode.nodeType === 3) pnode = pnode.parentElement;
    if (!pnode || !ed.contains(pnode)) pnode = ed;
    var block = pnode.closest ? pnode.closest('p,div,h1,h2,h3,h4,h5,h6') : null;
    text = (block ? block.innerText : '') || '';
  } else {
    text = sel && sel.rangeCount ? (sel.toString() || '') : '';
  }
  if (!text) return;
  var rp = parseRegexPattern(cond.pattern);
  var flags = rp.flags.indexOf('g') >= 0 ? rp.flags : rp.flags + 'g';
  var re;
  try { re = new RegExp(rp.pattern, flags); } catch (e) { return; }
  var textNodes = _buildTextNodeList(ed);
  var match;
  var mi = 0;
  while ((match = re.exec(text)) !== null) {
    if (!match[0]) { re.lastIndex++; continue; }
    var fmts = cond.match_formats[mi] || [];
    mi++;
    if (!fmts.length || (fmts.length === 1 && fmts[0] === 'none')) continue;
    var matchStart = match.index;
    var matchEnd = matchStart + match[0].length;
    var range = _rangeFromOffsets(textNodes, matchStart, matchEnd);
    if (!range) continue;
    sel.removeAllRanges();
    sel.addRange(range);
    for (var fi2 = 0; fi2 < fmts.length; fi2++) {
      var op2 = fmts[fi2];
      if (op2 === 'none') continue;
      applySingleFormat(op2, ed);
    }
  }
}

// 目标格式应用：根据 cond.target 计算格式作用范围（before/after/between）并应用格式
function applyTargetFormats(cond, ed) {
  if (!cond || !cond.target || cond.target === 'match') return false;
  var fmts = (cond.formats || []).filter(function (op) { return op !== 'none'; });
  if (!fmts.length) return false;
  // 获取作用域文本
  var sel = window.getSelection();
  var text = '';
  if (cond.scope === 'page') {
    text = ed && ed.innerText ? ed.innerText : '';
  } else if (cond.scope === 'paragraph') {
    var pnode = sel && sel.rangeCount ? sel.getRangeAt(0).startContainer : ed;
    if (pnode.nodeType === 3) pnode = pnode.parentElement;
    if (!pnode || !ed.contains(pnode)) pnode = ed;
    var block = pnode.closest ? pnode.closest('p,div,h1,h2,h3,h4,h5,h6') : null;
    text = (block ? block.innerText : '') || '';
  } else {
    text = sel && sel.rangeCount ? (sel.toString() || '') : '';
  }
  if (!text || !cond.pattern) return false;
  // 查找匹配位置
  var rp, re, match;
  try {
    rp = parseRegexPattern(cond.pattern);
    re = new RegExp(rp.pattern, rp.flags);
    match = re.exec(text);
  } catch (e) { return false; }
  if (!match) return false;
  var matchStart = match.index;
  var matchEnd = matchStart + match[0].length;
  var rangeStart = -1, rangeEnd = -1;
  if (cond.target === 'before') {
    rangeStart = 0; rangeEnd = matchStart;
  } else if (cond.target === 'after') {
    rangeStart = matchEnd; rangeEnd = text.length;
  } else if (cond.target === 'between') {
    if (!cond.between_end_pattern) return false;
    // 在第一个匹配之后查找结束条件
    var rp2, re2, match2;
    try {
      rp2 = parseRegexPattern(cond.between_end_pattern);
      re2 = new RegExp(rp2.pattern, rp2.flags);
      re2.lastIndex = matchEnd;
      match2 = re2.exec(text);
    } catch (e) { return false; }
    if (!match2) return false;
    rangeStart = matchEnd; rangeEnd = match2.index;
  }
  if (rangeStart < 0 || rangeEnd <= rangeStart) return false;
  var textNodes = _buildTextNodeList(ed);
  var range = _rangeFromOffsets(textNodes, rangeStart, rangeEnd);
  if (!range) return false;
  sel.removeAllRanges();
  sel.addRange(range);
  for (var fi = 0; fi < fmts.length; fi++) {
    var op = fmts[fi];
    if (op === 'none') continue;
    applySingleFormat(op, ed);
  }
  return true;
}

// edArg 可选：右键菜单快速应用时传入右键目标页（此时跳过 restoreFrRange，
// 不恢复弹窗打开时捕获的选区——右键场景的选区应保持右键页的光标位置）。
function applyFormatRule(rule, edArg) {
  const ed = edArg || currentEditable();
  if (!ed) { showToast('请先选中文字或把光标放入段落', 'warn'); return false; }
  if (!edArg) restoreFrRange();
  const res = evalFormatRule(rule, ed);
  if ((!res.fmts || !res.fmts.length) && (!res.groupConds || !res.groupConds.length) && (!res.matchConds || !res.matchConds.length) && (!res.targetConds || !res.targetConds.length)) {
    showToast('该规则没有匹配的格式，未做修改', 'warn'); return false;
  }
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  histRun('格式规则', [i], function () {
    ed.focus();
    inDiscreteOp = true;
    try {
      withScrollStable(function () {
        // 捕获组独立格式
        if (res.groupConds && res.groupConds.length) {
          for (var k = 0; k < res.groupConds.length; k++) {
            applyRegexGroupFormats(res.groupConds[k], ed);
          }
        }
        // 匹配对象独立格式
        if (res.matchConds && res.matchConds.length) {
          for (var k2 = 0; k2 < res.matchConds.length; k2++) {
            applyRegexMatchFormats(res.matchConds[k2], ed);
          }
        }
        // 目标范围格式（before/after/between）
        if (res.targetConds && res.targetConds.length) {
          for (var k3 = 0; k3 < res.targetConds.length; k3++) {
            applyTargetFormats(res.targetConds[k3], ed);
          }
        }
        // 普通格式
        if (res.fmts && res.fmts.length) {
          var applied = [];
          var run = function () {
            for (var j = 0; j < res.fmts.length; j++) {
              var op = res.fmts[j];
              if (applied.some(function (a) { return opsConflict(a, op); })) continue;
              applySingleFormat(op, ed);
              applied.push(op);
            }
          };
          if (res.page) withPageSelection(ed, run); else run();
        }
      });
    } finally { inDiscreteOp = false; }
    syncContent(ed);
    if (row) { markDirty(i); scheduleRemeasure(i); }
  });
  showToast('已应用格式规则「' + rule.name + '」', 'ok');
  return true;
}

// 单条件评估：空 pattern = 无条件（恒匹配）；scope=selection 用选中文字，
// scope=paragraph 用光标所在段落文本；regex 支持 /pattern/flags 语法。
function evalCondition(cond, ed) {
  if (!cond || !cond.pattern) return true;
  const sel = window.getSelection();
  let text = sel && sel.rangeCount ? (sel.toString() || '') : '';
  if (cond.scope === 'page') {
    // 当前页面作用域：整页可见文本（innerText 与 UI 所见一致，含所有块）
    text = ed && ed.innerText ? ed.innerText : '';
  } else if (cond.scope === 'paragraph') {
    let node = sel && sel.rangeCount ? sel.getRangeAt(0).startContainer : ed;
    if (node.nodeType === 3) node = node.parentElement;
    if (!node || !ed.contains(node)) node = ed;
    const block = node.closest ? node.closest('p,div,h1,h2,h3,h4,h5,h6') : null;
    text = (block ? block.innerText : '') || '';
  }
  const t = text || '';
  switch (cond.type) {
    case 'regex':
      try {
        const rp = parseRegexPattern(cond.pattern);
        return new RegExp(rp.pattern, rp.flags).test(t);
      } catch (e) { return false; }
    case 'contains': return t.indexOf(cond.pattern) >= 0;
    case 'prefix': return t.indexOf(cond.pattern) === 0;
    case 'suffix': return t.length >= cond.pattern.length && t.endsWith(cond.pattern);
  }
  return false;
}

// 规则求值：first=首个匹配条件生效即停（none 过滤后即使为空也停，充当「此处不处理」守卫）；
// all=全部匹配条件的格式按序拼接（none 过滤）。none 绝不进入应用列表。
// 返回 { fmts: [...], page: bool, groupConds: [...], matchConds: [...], targetConds: [...] }——page=true 表示任一匹配条件为「当前页面」作用域，
// groupConds 为含 group_formats 的正则匹配条件（独立应用捕获组格式），
// matchConds 为含 match_formats 的正则匹配条件（逐次匹配独立应用），
// targetConds 为 target 非 match 的条件（before/after/between，独立应用目标范围格式）。
function evalFormatRule(rule, ed) {
  const out = [];
  const groupConds = [];
  const matchConds = [];
  const targetConds = [];
  let page = false;
  for (const c of (rule && rule.conditions) || []) {
    if (evalCondition(c, ed)) {
      if (c.scope === 'page') page = true;
      if (c.target && c.target !== 'match') {
        targetConds.push(c);
        if (rule.mode === 'first') break;
      } else if (c.type === 'regex' && c.group_formats && c.group_formats.length) {
        groupConds.push(c);
        if (rule.mode === 'first') break;
      } else if (c.type === 'regex' && c.match_formats && c.match_formats.length) {
        matchConds.push(c);
        if (rule.mode === 'first') break;
      } else {
        const fmts = (c.formats || []).filter(function (op) { return op !== 'none'; });
        if (rule.mode === 'first') {
          // first 模式：首个匹配条件生效即停，但需把此前收集的 group/match/target 一并返回
          return { fmts: fmts, page: page, groupConds: groupConds, matchConds: matchConds, targetConds: targetConds };
        }
        out.push.apply(out, fmts);
      }
    }
  }
  return { fmts: out, page: page, groupConds: groupConds, matchConds: matchConds, targetConds: targetConds };
}
// 页面级应用辅助：临时把选区扩展为整页（selectNodeContents(ed)）→ 执行 fn →
// 恢复原选区。applyToSelectedBlocks 在 startBlock===ed 时会置空从首块收集全部块，
// 因此整页选区下格式对每个块逐个生效。
function withPageSelection(ed, fn) {
  const sel = window.getSelection();
  const origRanges = [];
  if (sel) for (let i = 0; i < sel.rangeCount; i++) origRanges.push(sel.getRangeAt(i).cloneRange());
  try {
    const r = document.createRange();
    r.selectNodeContents(ed);
    if (sel) {
      sel.removeAllRanges();
      sel.addRange(r);
    }
    fn();
  } finally {
    if (sel) {
      sel.removeAllRanges();
      for (const rr of origRanges) sel.addRange(rr);
    }
  }
}
function applyFormatsList(fmts, ed, pageScope) {
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  const skipped = [];
  histRun('格式规则', [i], function () {
    ed.focus();
    inDiscreteOp = true;
    try {
      const applied = [];
      withScrollStable(function () {
        const run = function () {
          for (const op of fmts) {
            if (applied.some(function (a) { return opsConflict(a, op); })) { skipped.push(op); continue; }
            applySingleFormat(op, ed);
            applied.push(op);
          }
        };
        if (pageScope) withPageSelection(ed, run); else run();
      });
    } finally { inDiscreteOp = false; }
    syncContent(ed);
    if (row) { markDirty(i); scheduleRemeasure(i); }
  });
  return skipped;
}
function applyAllFormatRules() {
  const ed = currentEditable();
  if (!ed) { showToast('请先选中文字或把光标放入段落', 'warn'); return; }
  restoreFrRange();
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  const appliedOps = [];
  const skipped = [];
  let appliedRules = 0;
  histRun('格式规则（全部）', [i], function () {
    ed.focus();
    inDiscreteOp = true;
    try {
      withScrollStable(function () {
        for (const rule of formatRules) {
          restoreFrRange();
          const res = evalFormatRule(rule, ed);
          const hasFmt = res.fmts && res.fmts.length;
          const hasGrp = res.groupConds && res.groupConds.length;
          const hasTgt = res.targetConds && res.targetConds.length;
          const hasMtc = res.matchConds && res.matchConds.length;
          if (!hasFmt && !hasGrp && !hasTgt && !hasMtc) continue;
          let any = false;
          if (hasGrp) {
            for (var k = 0; k < res.groupConds.length; k++) {
              applyRegexGroupFormats(res.groupConds[k], ed);
              any = true;
            }
          }
          if (hasMtc) {
            for (var m = 0; m < res.matchConds.length; m++) {
              applyRegexMatchFormats(res.matchConds[m], ed);
              any = true;
            }
          }
          if (hasFmt) {
            const run = function () {
              for (const op of res.fmts) {
                if (appliedOps.some(function (a) { return opsConflict(a, op); })) { skipped.push(rule.name + ':' + op); continue; }
                applySingleFormat(op, ed);
                appliedOps.push(op);
                any = true;
              }
            };
            if (res.page) withPageSelection(ed, run); else run();
          }
          if (hasTgt) {
            for (var tg = 0; tg < res.targetConds.length; tg++) {
              applyTargetFormats(res.targetConds[tg], ed);
              any = true;
            }
          }
          if (any) appliedRules++;
        }
      });
    } finally { inDiscreteOp = false; }
    syncContent(ed);
    if (row) { markDirty(i); scheduleRemeasure(i); }
  });
  if (appliedRules === 0 && skipped.length === 0) { showToast('没有可应用的规则', 'warn'); return; }
  showToast('已应用 ' + appliedRules + ' 条规则' + (skipped.length ? '，跳过冲突格式 ' + skipped.length + ' 个' : ''), skipped.length ? 'warn' : 'ok');
  closeFormatRulesModal();
}
function _mergeSelectedBlocks(ed) {
  // 合并选区内所有块到第一个块（段落合并）
  if (typeof isComposing !== 'undefined' && isComposing) {
    _pendingOps.push(() => _mergeSelectedBlocks(ed));
    showToast('输入法中，已将操作排队，输入结束后自动应用', 'warn');
    return;
  }
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return;
  const range = sel.getRangeAt(0);
  const startNode = range.startContainer.nodeType === 3 ? range.startContainer.parentElement : range.startContainer;
  const endNode = range.endContainer.nodeType === 3 ? range.endContainer.parentElement : range.endContainer;
  const startBlock = (startNode && startNode.closest) ? (startNode.closest('p,div,h1,h2,h3,h4,h5,h6') || null) : null;
  const endBlock = (endNode && endNode.closest) ? (endNode.closest('p,div,h1,h2,h3,h4,h5,h6') || null) : null;
  if (!startBlock || !endBlock || startBlock === endBlock) return;
  const blocks = _blocksBetween(ed, startBlock, endBlock);
  if (blocks.length < 2) return;
  const row = ed.closest('.page-row');
  const i = row ? Number(row.dataset.i) : -1;
  histRun('合并段落', [i], function () {
    // 把后续块的内容追加到第一个块，再逐个移除
    const first = blocks[0];
    for (let k = 1; k < blocks.length; k++) {
      const b = blocks[k];
      if (!b || !b.parentNode) continue;
      // 在两段之间加一个空格避免中英混排粘连
      const needSpace = first.innerHTML.length > 0 && b.innerHTML.length > 0;
      first.innerHTML += (needSpace ? ' ' : '') + b.innerHTML;
      b.parentNode.removeChild(b);
    }
    syncContent(ed);
    if (row) { markDirty(i); scheduleRemeasure(i); }
    // 将光标置于合并后块的末尾
    try {
      const r2 = document.createRange();
      r2.selectNodeContents(first);
      r2.collapse(false);
      sel.removeAllRanges();
      sel.addRange(r2);
    } catch (e) {}
  });
}

function applySingleFormat(op, ed) {
  if (op === 'bold') { applyToSelectedBlocks(ed, function () { document.execCommand('bold'); }); return; }
  if (op === 'italic') { applyToSelectedBlocks(ed, function () { document.execCommand('italic'); }); return; }
  if (op === 'remove') { applyToSelectedBlocks(ed, function () { document.execCommand('removeFormat'); }); return; }
  if (op === 'p') { applyToSelectedBlocks(ed, function (block) { _convertBlockTag(block, 'p'); }); return; }
  if (op === 'merge') { _mergeSelectedBlocks(ed); return; }
  if (op === 'note') { toggleNote(ed); return; }
  if (op.indexOf('align_') === 0) { applyAlign(ed, op.slice(6)); return; }
  if (op.indexOf('heading') === 0) {
    const tag = 'h' + op.slice(7);
    applyToSelectedBlocks(ed, function (block) { _convertBlockTag(block, tag); });
  }
}

// ---------- 快捷键绑定 ----------
function comboOf(e) {
  const mods = [];
  if (e.ctrlKey) mods.push('Ctrl');
  if (e.altKey) mods.push('Alt');
  if (e.shiftKey) mods.push('Shift');
  const k = e.key;
  if (k === 'Control' || k === 'Alt' || k === 'Shift' || k === 'Meta') return null;
  let key = k;
  if (/^[a-zA-Z]$/.test(key)) key = key.toUpperCase();
  if (key === ' ') key = 'Space';
  if (mods.length === 0 && !/^F\d{1,2}$/.test(key) && key !== 'Enter' && key !== 'Escape') return null; // 必须带修饰键或功能键（Enter/Escape 裸键放行）
  return [...mods, key].join('+');
}
function renderShortcutTable() {
  const tbody = document.getElementById('shortcutTable');
  tbody.innerHTML = '';
  for (const [op, label] of OPS) {
    const tr = document.createElement('tr');
    tr.dataset.op = op;
    const combo = bindings[op];
    tr.innerHTML = '<td>' + label + '</td><td>' + (combo ? '<kbd>' + combo.replace(/\+/g, '</kbd>+<kbd>') + '</kbd>' : '<span style="color:#9aa7b4">未绑定</span>') + '</td>';
    tr.addEventListener('click', () => {
      capturingOp = op;
      renderShortcutTable();
      const row = tbody.querySelector('tr[data-op="' + op + '"]');
      if (row) row.querySelector('td:nth-child(2)').textContent = '按下新组合键…（Esc 取消，Del 清除）';
    });
    tbody.appendChild(tr);
  }
}
function openSettings() {
  renderShortcutTable();
  document.getElementById('tipDelayInput').value = tipDelay();
  document.getElementById('modalBg').style.display = 'flex';
}
function closeSettings() { capturingOp = null; document.getElementById('modalBg').style.display = 'none'; }

// ---------- 撤销 / 重做 ----------
// 快照粒度为「操作」：一次连续输入（间隔 < UNDO_IDLE_MS）算一次操作；
// 格式按钮/标记/对齐/搜索替换/智能清理/繁简转换/插入图片等离散操作各算一次。
// undoStack/redoStack 各保留最近 UNDO_LIMIT（10）步，超出丢最旧；新操作清空重做。
// 快照只记录「源」（pageSource，即当前模式对应的 map 或回退到初始 text），
// 且 histEnd 只保留实际变化的页，避免全页操作把整本书复制 10 份。
const UNDO_LIMIT = 10;
const UNDO_IDLE_MS = 800; // 两次输入间隔超过此值 → 视为新操作起点
let undoStack = [];   // [{before: Map(i→源), after: Map(i→源), label}]
let redoStack = [];
let currentUndo = null;  // 进行中的输入操作 {before, pages:Set, label}；空闲超时后落栈
let undoIdleTimer = null;
let inDiscreteOp = false; // 离散操作正在改 DOM（抑制 beforeinput 误开输入操作）

function histPush(before, after, errBefore, errAfter, label) {
  // D2: 撤销条目含 errBefore/errAfter（Map(i→errors 深拷贝)），只含相关页
  const entry = { before: before, after: after, label: label };
  if (errBefore && errBefore.size) entry.errBefore = errBefore;
  if (errAfter && errAfter.size) entry.errAfter = errAfter;
  undoStack.push(entry);
  if (undoStack.length > UNDO_LIMIT) undoStack.shift();
  redoStack.length = 0; // 新操作使重做历史失效
  histUpdateButtons();
}
function histCommitInput() {
  if (undoIdleTimer) { clearTimeout(undoIdleTimer); undoIdleTimer = null; }
  if (!currentUndo) return;
  const after = new Map();
  const errAfter = new Map();
  for (const i of currentUndo.pages) {
    after.set(i, pageSource(i));
    if (currentUndo.errBefore && currentUndo.errBefore.has(i)) {
      errAfter.set(i, _copyErrors(i));
    }
  }
  histPush(currentUndo.before, after, currentUndo.errBefore, errAfter, currentUndo.label);
  currentUndo = null;
}
function histIdle() { undoIdleTimer = null; histCommitInput(); }
function histScheduleIdle() {
  if (undoIdleTimer) clearTimeout(undoIdleTimer);
  undoIdleTimer = setTimeout(histIdle, UNDO_IDLE_MS);
}
function histBeginInput(i) {
  // beforeinput/keydown/compositionstart/paste 均在 DOM 变更前触发 → 可捕获操作前快照。
  // 重复触发无副作用（幂等）；离散操作改 DOM 期间（execCommand 也会派发 beforeinput）
  // 忽略，防止把格式操作误记为「输入」。
  if (i < 0 || inDiscreteOp) return;
  if (currentUndo) { currentUndo.pages.add(i); return; }
  const before = new Map();
  before.set(i, pageSource(i));
  // D3: 同时捕获 errBefore（仅新建时捕获当时状态）
  const errBefore = new Map([[i, _copyErrors(i)]]);
  currentUndo = { before: before, pages: new Set([i]), errBefore: errBefore, label: '输入' };
}
function histTouchInput(i) {
  // input 事件（变更后）触发：只扩展进行中操作的页面集合并续期空闲计时；
  // 操作起点由「变更前」事件建立，这里不补建（否则快照已含本次变更）。
  if (i < 0 || inDiscreteOp) return;
  if (currentUndo) { currentUndo.pages.add(i); histScheduleIdle(); }
}
// 离散（同步）操作包装：先收掉进行中的输入操作，捕获 before，执行 fn，提交
let _histDepth = 0; // 嵌套 histRun 深度：>0 时内层直接执行（外层已建快照），避免双撤销条目（2026-08-15）
function histRun(label, pagesArr, fn) {
  if (_histDepth > 0) return fn(); // 嵌套 histRun：外层已建快照，直接执行
  _histDepth++;
  histCommitInput();
  const before = new Map();
  const errBefore = new Map();
  for (const i of (pagesArr || [])) {
    before.set(i, pageSource(i));
    errBefore.set(i, _copyErrors(i));
  }
  inDiscreteOp = true;
  let out;
  try { out = fn(); }
  finally { inDiscreteOp = false; _histDepth--; }
  histEnd(before, label, errBefore);
  return out;
}
// 离散（异步/多页）操作：histBegin 返回 before 快照，操作完成后 histEnd 提交；
// 只保留实际变化的页（before/after 均按变化页裁剪）。histBegin 之后若提前
// return（未发生变更），before 自然丢弃、不入栈。
function histBegin(label, pagesArr) {
  histCommitInput();
  const before = new Map();
  if (pagesArr === null || pagesArr === undefined) {
    for (let i = 0; i < pages.length; i++) before.set(i, pageSource(i));
  } else {
    for (const i of pagesArr) before.set(i, pageSource(i));
  }
  return before;
}
function histEnd(before, label, errBefore) {
  const after = new Map();
  const errAfter = new Map();
  for (const [i, src] of before) {
    const now = pageSource(i);
    if (now !== src) {
      after.set(i, now);
      if (errBefore && errBefore.has(i)) errAfter.set(i, _copyErrors(i));
    } else {
      before.delete(i);
    }
  }
  if (before.size) histPush(before, after, errBefore, errAfter, label);
}
function histClear() {
  if (undoIdleTimer) { clearTimeout(undoIdleTimer); undoIdleTimer = null; }
  currentUndo = null;
  undoStack = []; redoStack = [];
  histUpdateButtons();
}
function restoreHistorySnapshot(snap, errSnap) {
  // 恢复指定页的源（写入当前模式对应 map），重渲染已挂载行；若当前编辑页被
  // 恢复则重聚焦并置光标到末尾。恢复只写 map + innerHTML，不派发 beforeinput/
  // input，不会触发新的历史记录。
  // D5: 标注对账——恢复文本时同步恢复 proofreadErrors[i]
  for (const [i, src] of snap) {
    if (mdMode) mdSourceMap.set(i, src);
    else contentMap.set(i, src);
    // 标注对账：有 errSnap 则恢复，否则清空该页现有标注防错位
    if (errSnap && errSnap.has(i)) {
      proofreadErrors[i] = errSnap.get(i);
    } else if (proofreadErrors[i] && proofreadErrors[i].length) {
      proofreadErrors[i] = [];
    }
    const row = host.querySelector('.page-row[data-i="' + i + '"]');
    if (row) {
      const ed = row.querySelector('.editable');
      if (ed) { ed.innerHTML = displayHtml(i); _reapplyProofread(i); remeasure(i); }
    }
  }
  const ed = currentEditable();
  if (ed) {
    const row = ed.closest('.page-row');
    const i = row ? Number(row.dataset.i) : -1;
    if (snap.has(i)) {
      ed.focus();
      const r = document.createRange();
      r.selectNodeContents(ed); r.collapse(false);
      const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
    }
  }
}
function undoHistory() {
  histCommitInput(); // 先落栈进行中的输入操作，才能撤到它
  const entry = undoStack.pop();
  if (!entry) { setStatus('没有可撤回的操作'); return false; }
  // D5: 兼容旧格式（无 errBefore 字段）——undefined errSnap → 只清空该页现有标注
  restoreHistorySnapshot(entry.before, entry.errBefore);
  redoStack.push(entry);
  if (redoStack.length > UNDO_LIMIT) redoStack.shift();
  histUpdateButtons();
  setStatus('已撤回：' + entry.label);
  return true;
}
function redoHistory() {
  const entry = redoStack.pop();
  if (!entry) { setStatus('没有可前进的操作'); return false; }
  restoreHistorySnapshot(entry.after, entry.errAfter);
  undoStack.push(entry);
  if (undoStack.length > UNDO_LIMIT) undoStack.shift();
  histUpdateButtons();
  setStatus('已前进：' + entry.label);
  return true;
}
function histUpdateButtons() {
  const u = document.getElementById('undoBtn');
  const r = document.getElementById('redoBtn');
  if (u) u.disabled = !undoStack.length;
  if (r) r.disabled = !redoStack.length;
}
// 工具栏按钮（onmousedown preventDefault 保持编辑焦点不丢）
document.getElementById('undoBtn').addEventListener('click', () => { hideTip(); undoHistory(); });
document.getElementById('redoBtn').addEventListener('click', () => { hideTip(); redoHistory(); });

// 纠错悬浮窗快捷键守卫：errKey 打开且焦点不在输入控件时才触发（返回 false = 未消费，交浏览器默认行为）
function _inField() { const t = document.activeElement; return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT'); }
function acceptErrShortcut() { if (errKey && !_inField()) { document.getElementById('errOk').click(); return true; } return false; }
function ignoreErrShortcut() { if (errKey && !_inField()) { document.getElementById('errNo').click(); return true; } return false; }

// 工具操作快捷键映射（op → 直接调用函数；格式/标记操作不在此表，走 applyOp）
const SHORTCUT_ACTIONS = {
  search: openSearchModal,
  clean: cleanAll,
  convert_t2s: () => convertAll('t2s'),
  convert_s2t: () => convertAll('s2t'),
  toggle_md: () => setMdMode(!mdMode),
  undo: undoHistory,
  redo: redoHistory,
  history: openHistory,
  export: openExportModal,
  save: save,
  stage: stage,
  finish: finish,
  jump: jumpToPage,
  help: () => { document.getElementById('helpModalBg').style.display = 'flex'; },
  settings: openSettings,
  proofread_correct: proofreadCorrect,
  proofread_reocr: runReocr,
  proofread_apply: proofreadApplyCurrent,
  proofread_clear: proofreadClearCurrent,
  proofread_revert: proofreadRevertCurrent,
  proofread_accept: acceptErrShortcut,
  proofread_ignore: ignoreErrShortcut,
};

// ---------- 全局事件（统一快捷键分发） ----------
document.addEventListener('keydown', (e) => {
  if (capturingOp) {
    e.preventDefault(); e.stopPropagation();
    if (e.key === 'Escape') { capturingOp = null; renderShortcutTable(); return; }
    if (e.key === 'Delete' || e.key === 'Backspace') { bindings[capturingOp] = ''; saveBindings(); capturingOp = null; renderShortcutTable(); return; }
    const combo = comboOf(e);
    if (combo) { bindings[capturingOp] = combo; saveBindings(); capturingOp = null; renderShortcutTable(); }
    return;
  }
  const combo = comboOf(e);
  if (!combo) return;
  const op = reverseBindings()[combo];
  if (!op) return;
  const act = SHORTCUT_ACTIONS[op];
  if (act) {
      // 撤销/重做在 INPUT/TEXTAREA/SELECT 聚焦时不触发（避免与输入框原生行为冲突）
      if (op === 'undo' || op === 'redo') {
        const t = e.target;
        if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return;
      }
      if (act() === false) return;   // act 返回 false：不 preventDefault、不进 applyOp，交浏览器默认行为（如 contenteditable 换行）
      e.preventDefault();
      return;
  }
  if (!currentEditable()) return;
  applyOp(op);
  e.preventDefault();
  return;
});
  document.getElementById('searchInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') searchPages(); });
  // Color picker input (hidden). Append to body to avoid messing toolbar layout.
  const colorInput = document.createElement('input'); colorInput.type = 'color'; colorInput.id = 'colorInput'; colorInput.style.display = 'none'; document.body.appendChild(colorInput);
  const colorBtn = document.getElementById('colorBtn');
  if (colorBtn) {
    colorBtn.addEventListener('click', (e) => { e.preventDefault(); colorInput.click(); });
    colorInput.addEventListener('input', (e) => {
      const color = e.target.value;
      const ed = currentEditable(); if (!ed) return;
      const row = ed.closest('.page-row'); const i = row ? Number(row.dataset.i) : -1;
      histRun('颜色', [i], function() {
        applyToSelectedBlocks(ed, function() { withScrollStable(() => document.execCommand('foreColor', false, color)); });
        syncContent(ed);
        if (row) { markDirty(Number(row.dataset.i)); scheduleRemeasure(Number(row.dataset.i)); }
      });
    });
  }
  const brushBtn = document.getElementById('formatBrushBtn');
  if (brushBtn) {
    brushBtn.addEventListener('click', (e) => {
      e.preventDefault();
      if (!_formatBrush) {
        const fmt = captureFormatFromSelection();
        if (!fmt) { showToast('请先选中含有格式的文本以捕获格式', 'warn'); return; }
        _formatBrush = fmt;
        // start aggregated history entry
        _brushBefore = histBegin('格式刷', null);
        brushBtn.classList.add('active');
        setStatus('已捕获格式（持续模式）。在目标文本上点击或选区后格式将被应用；再次点击 格式刷 可提交并结束。');
      } else {
        // finish aggregated history entry and clear
        try { histEnd(_brushBefore, '格式刷'); } catch (err) { /* best-effort */ }
        _brushBefore = null;
        _formatBrush = null;
        brushBtn.classList.remove('active');
        setStatus('已提交格式刷并结束');
      }
    });
    host.addEventListener('mouseup', () => {
      if (_formatBrush) {
        const ed = currentEditable(); if (!ed) return;
        applyFormatBrushToSelection(_formatBrush);
        syncContent(ed);
        const row = ed.closest('.page-row'); if (row) { markDirty(Number(row.dataset.i)); scheduleRemeasure(Number(row.dataset.i)); }
        setStatus('已应用格式（持续模式）。要结束请再次点击 格式刷 或按 Esc。');
      }
    });
  }
  // proofread 设置：LLM 深度校对开关/模型 + 原有规则开关，均持久化在 config.json
  // （/api/proofread_settings）。随机端口下 localStorage 每次运行失效（2026-08-07 修复 → 迁移到服务端）。
  const prLlmEnableEl = document.getElementById('prLlmEnable');
  const prLlmModelEl = document.getElementById('prLlmModel');
  const prLegacyRulesEl = document.getElementById('prLegacyRules');
  async function loadProofreadLlm() {
    try {
      const res = await fetchJSON('/api/proofread_settings');
      if (!res.ok) throw new Error(res.error || '读取设置失败');
      proofreadLlmEnabled = !!res.enabled;
      proofreadLlmModel = res.model || '';
      proofreadLegacyRules = !!res.enable_legacy_rules;
      prLlmEnableEl.checked = proofreadLlmEnabled;
      if (prLegacyRulesEl) prLegacyRulesEl.checked = proofreadLegacyRules;
      prLlmModelEl.innerHTML = '';
      const opts = Array.isArray(res.available) ? res.available : [];
      opts.forEach(function (k) {
        const o = document.createElement('option');
        o.value = k; o.textContent = k;
        if (k === (proofreadLlmModel || res.selected)) o.selected = true;
        prLlmModelEl.appendChild(o);
      });
      if (!opts.length) {
        const o = document.createElement('option');
        o.value = ''; o.textContent = '（无可用模型）';
        prLlmModelEl.appendChild(o);
      }
    } catch (e) { console.warn('loadProofreadLlm failed: ' + e.message); }
  }
  // legacyToast=true 时提示语针对「原有规则」开关（由该勾选框的 change 触发）
  async function saveProofreadLlm(legacyToast) {
    proofreadLlmEnabled = prLlmEnableEl.checked;
    proofreadLlmModel = prLlmModelEl.value || '';
    proofreadLegacyRules = prLegacyRulesEl ? !!prLegacyRulesEl.checked : proofreadLegacyRules;
    try {
      const res = await fetchJSON('/api/proofread_settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: proofreadLlmEnabled,
          model: proofreadLlmModel,
          enable_legacy_rules: proofreadLegacyRules,
        }),
      });
      if (!res.ok) { showToast('保存校对设置失败: ' + (res.error || ''), 'fail'); return; }
      if (legacyToast === true) {
        showToast(proofreadLegacyRules ? '已启用原有规则（半角转全角/引号配对/混淆表/词典）' : '已关闭原有规则，校正只执行三条新规则', 'ok');
      } else {
        showToast(proofreadLlmEnabled ? '已启用 LLM 深度校对' : '已关闭 LLM 深度校对', 'ok');
      }
    } catch (e) {
      showToast('保存校对设置失败: ' + e.message, 'fail');
    }
  }
  // init
  loadProofreadLlm();
  prLlmEnableEl.addEventListener('change', function () { saveProofreadLlm(false); });
  prLlmModelEl.addEventListener('change', function () { saveProofreadLlm(false); });
  if (prLegacyRulesEl) prLegacyRulesEl.addEventListener('change', function () { saveProofreadLlm(true); });
  // llama-server 启停（句子校正：以纯文本模式启动，不附加图像投影）
  async function refreshLlmStatus() {
    const el = document.getElementById('prLlmStatus');
    if (!el) return;
    try {
      const res = await fetchJSON('/api/llm_status');
      if (!res.ok) { el.textContent = '服务状态: 未知（' + (res.error || '') + '）'; return; }
      // loading：进程存活但模型仍在加载（health 503），两个按钮都禁用避免重复启动/误停
      const loading = !res.running && !!res.loading;
      el.textContent = res.running
        ? ('服务状态: 运行中' + (res.mismatch
            ? '（其他模型，可停止后切换）'
            : (res.model ? '（' + res.model + '）' : '')))
        : (loading ? '服务状态: 启动中…' : '服务状态: 未运行');
      const startBtn = document.getElementById('prLlmStart');
      const stopBtn = document.getElementById('prLlmStop');
      if (startBtn) startBtn.disabled = !!res.running || loading;
      if (stopBtn) stopBtn.disabled = !res.running || loading;
    } catch (e) { el.textContent = '服务状态: 未知'; }
  }
  async function startLlm() {
    // 启动是阻塞请求（大模型加载可能数分钟），先给即时反馈避免界面看起来没反应
    const statusEl = document.getElementById('prLlmStatus');
    const startBtnNow = document.getElementById('prLlmStart');
    if (statusEl) statusEl.textContent = '服务状态: 启动中…';
    if (startBtnNow) startBtnNow.disabled = true;
    try {
      const modelEl = document.getElementById('prLlmModel');
      const res = await fetchJSON('/api/llm_start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: (modelEl && modelEl.value) || proofreadLlmModel || '' }),
      });
      if (!res.ok) { showToast('启动服务失败: ' + (res.error || ''), 'fail'); refreshLlmStatus(); return; }
      showToast(res.message || '服务已启动', res.running ? 'ok' : 'fail');
    } catch (e) { showToast('启动服务失败: ' + e.message, 'fail'); }
    refreshLlmStatus();
  }
  async function stopLlm() {
    try {
      const res = await fetchJSON('/api/llm_stop', { method: 'POST' });
      if (!res.ok) { showToast('停止服务失败: ' + (res.error || ''), 'fail'); refreshLlmStatus(); return; }
      showToast(res.message || '已停止服务', 'ok');
    } catch (e) { showToast('停止服务失败: ' + e.message, 'fail'); }
    refreshLlmStatus();
  }
  refreshLlmStatus();
  document.getElementById('prLlmStart').addEventListener('click', startLlm);
  document.getElementById('prLlmStop').addEventListener('click', stopLlm);

document.getElementById('cleanBtn').addEventListener('click', cleanAll);
document.getElementById('proofreadBtn').addEventListener('click', toggleProofreadMenu);
document.getElementById('prMenuCorrect').addEventListener('click', proofreadCorrect);
document.getElementById('prMenuReocr').addEventListener('click', runReocr);
document.getElementById('prMenuApply').addEventListener('click', proofreadApplyCurrent);
document.getElementById('prMenuClear').addEventListener('click', proofreadClearCurrent);
document.getElementById('prMenuRevert').addEventListener('click', proofreadRevertCurrent);
document.getElementById('searchOpenBtn').addEventListener('click', openSearchModal);
document.getElementById('searchBtn').addEventListener('click', searchPages);
document.getElementById('searchInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') searchPages(); });
document.getElementById('replaceBtn').addEventListener('click', replaceCurrent);
document.getElementById('replaceAllBtn').addEventListener('click', replaceAll);
document.getElementById('replaceInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') replaceCurrent(); });
document.getElementById('searchPrevBtn').addEventListener('click', () => gotoMatch(-1));
document.getElementById('searchNextBtn').addEventListener('click', () => gotoMatch(1));
document.getElementById('searchCloseBtn').addEventListener('click', closeSearchModal);
document.getElementById('searchModalBg').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeSearchModal(); });
document.getElementById('exportBtn').addEventListener('click', openExportModal);
document.getElementById('exportTxtBtn').addEventListener('click', () => exportFile('txt'));
document.getElementById('exportDocxBtn').addEventListener('click', () => exportFile('docx'));
document.getElementById('exportCloseBtn').addEventListener('click', closeExportModal);
document.getElementById('exportModalBg').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeExportModal(); });
document.getElementById('imgModeSel').addEventListener('change', (e) => { saveStr('ptoe_img_mode', e.target.value); });
// 格式规则弹窗绑定
document.getElementById('formatRulesBtn').addEventListener('click', openFormatRulesModal);
document.getElementById('formatRulesCloseBtn').addEventListener('click', closeFormatRulesModal);
document.getElementById('formatRulesModalBg').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeFormatRulesModal(); });
document.getElementById('formatRuleNewBtn').addEventListener('click', newFormatRule);
document.getElementById('formatRulesApplyAllBtn').addEventListener('click', applyAllFormatRules);
document.getElementById('frSaveBtn').addEventListener('click', saveFormatRule);
document.getElementById('frCancelBtn').addEventListener('click', closeRuleModal);
document.getElementById('frRuleCloseBtn').addEventListener('click', closeRuleModal);
document.getElementById('frRuleModalBg').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeRuleModal(); });
document.getElementById('frAddCondBtn').addEventListener('click', addCondition);
document.getElementById('frFmtPopupCloseBtn').addEventListener('click', closeFmtPopup);
document.getElementById('frFmtCancelBtn').addEventListener('click', closeFmtPopup);
document.getElementById('frFmtOkBtn').addEventListener('click', confirmFmtPopup);
document.getElementById('frFmtPopupBg').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeFmtPopup(); });
// 格式规则快捷键 Ctrl+Shift+Q（独立注册，不依赖 bindings 体系）
document.addEventListener('keydown', (e) => {
  if (e.ctrlKey && e.shiftKey && (e.key === 'Q' || e.key === 'q')) {
    e.preventDefault();
    openFormatRulesModal();
  }
});
document.getElementById('closeSettings').addEventListener('click', closeSettings);
document.getElementById('modalBg').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeSettings(); });
// 暂存/保存/完成并转换/快捷键设置（2026-08-07 修复：四个绑定曾整块丢失 → 按钮点击无响应）
document.getElementById('saveBtn').addEventListener('click', save);
document.getElementById('stageBtn').addEventListener('click', stage);
document.getElementById('finishBtn').addEventListener('click', finish);
document.getElementById('settingsBtn').addEventListener('click', openSettings);
document.getElementById('mdToggleBtn').addEventListener('click', () => setMdMode(!mdMode));
document.getElementById('helpBtn').addEventListener('click', () => { document.getElementById('helpModalBg').style.display = 'flex'; });
document.getElementById('closeHelp').addEventListener('click', () => { document.getElementById('helpModalBg').style.display = 'none'; });
document.getElementById('helpModalBg').addEventListener('click', (e) => { if (e.target === e.currentTarget) document.getElementById('helpModalBg').style.display = 'none'; });
document.getElementById('fontSizeSel').addEventListener('change', (e) => applyFontSize(parseInt(e.target.value, 10) || 14));
document.getElementById('jumpBtn').addEventListener('click', jumpToPage);
document.getElementById('pageJump').addEventListener('keydown', (e) => { if (e.key === 'Enter') jumpToPage(); });
document.getElementById('toTraditionBtn').addEventListener('click', () => convertAll('s2t'));
document.getElementById('toSimplifiedBtn').addEventListener('click', () => convertAll('t2s'));
document.querySelectorAll('#toolbar button[data-op]').forEach((b) => {
  b.addEventListener('mouseenter', scheduleTip);
  b.addEventListener('mouseleave', hideTip);
  b.addEventListener('click', () => { hideTip(); suppressPopupUntil = performance.now() + 250; applyOp(b.dataset.op); });
});
// ---------- 滚动驱动虚拟列表 ----------
// wheel/touchmove 置位「用户主动滚动」时间戳（供 withScrollStable 放弃还原）；
// scroll 事件置位任意滚动时间戳并 rAF 节流驱动 updateViewport 挂载视口附近行。
// 曾因重构丢失该块导致只挂载初始窗口、滚动后后续页空白（2026-08 修复后再次
// 丢失，2026-08-07 恢复）。
const markUserScroll = () => { lastUserScrollTs = Date.now(); lastAnyScrollTs = Date.now(); };
const markAnyScroll = () => { lastAnyScrollTs = Date.now(); };
let _viewportRaf = 0;
const scheduleViewport = () => {
  if (_viewportRaf) return;
  _viewportRaf = requestAnimationFrame(() => { _viewportRaf = 0; updateViewport(); });
};
window.addEventListener('wheel', markUserScroll, { passive: true });
window.addEventListener('touchmove', markUserScroll, { passive: true });
window.addEventListener('scroll', () => { markAnyScroll(); scheduleViewport(); hidePopup(); closeContextMenu(); }, { passive: true });
window.addEventListener('beforeunload', (e) => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });
// ---------- 浏览器存活监测 ----------
setInterval(() => { fetch('/api/heartbeat', { method: 'POST' }).catch(() => {}); }, 30000);
window.addEventListener('pagehide', () => { navigator.sendBeacon('/api/gone'); });
// IME composition guard and pending ops queue（顶层注册，避免依赖 setMdMode 调用）
window.isComposing = false;
window._pendingOps = [];
function _flushPendingOps() { while (window._pendingOps.length) { const f = window._pendingOps.shift(); try { f(); } catch (e) { console.error('pending op failed', e); } } }
document.addEventListener('compositionstart', () => { window.isComposing = true; });
document.addEventListener('compositionend', () => { window.isComposing = false; _flushPrPending(); setTimeout(_flushPendingOps, 0); });

// ---------- 初始化 ----------
(async function init() {
  try {
    pages = (await fetchJSON('/api/pages')).pages;
  } catch (e) { document.body.textContent = '加载失败: ' + e; return; }
  heights.length = pages.length; heights.fill(0);
  est = pages.length ? 420 : 420;
  loadBindingsFromServer();   // 服务端快捷键设置（异步覆盖，失败静默回退 localStorage/DEFAULTS）
  mdMode = loadBool('ptoe_md_mode');
  if (mdMode) {
    for (let i = 0; i < pages.length; i++) mdSourceMap.set(i, htmlToMd(pages[i].text));
    const btn = document.getElementById('mdToggleBtn');
    btn.textContent = '富文本模式';
    btn.classList.add('active');
  }
  const fs = loadInt('ptoe_font_size', 14);
  document.getElementById('fontSizeSel').value = fs;
  document.documentElement.style.setProperty('--editor-font-size', fs + 'px');
  updateViewport();
  setStatus('已加载 ' + pages.length + ' 页');
})();
