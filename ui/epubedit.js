'use strict';
// EPUB 编辑器前端（浏览器端，由本地服务 epubeditmanage 提供 /api/book、/api/save、
// /api/convert{html,mode:t2s|s2t}、/api/clean{html}、/api/ping、/api/bye）。
// 虚拟列表复用矫正界面（correctmanage/ui-app.js）的成熟机制（BUFFER/GAP/PRELOAD 常量、
// heights/prefixH 前缀和、批量挂载+统一测量、reposition 高度下限防滚动钳制、空白兜底）。
// 每行 = 一篇文章（一级标题 + 正文），无图片、无纠错（有别于矫正界面）。
// 编辑能力（2026-09-13，对齐矫正界面）：玻璃工具栏（格式/对齐/文本/结构）、选中文字
// 快捷菜单、右键上下文菜单、撤销/重做（全书快照，上限 10 步）、搜索替换（当前章）、
// 繁简转换 / 智能清理（走 /api/convert、/api/clean）。标题一律不含 H1 —— 文章边界
// 即一级标题，正文内标题只允许 H2-H6（save 后按 H1 重新分章，正文内 H1 会拆章）。
(function () {
  const BUFFER = 15, GAP = 12, PRELOAD = 15; // 与矫正界面一致
  let articles = [];      // 影子数组（唯一数据源）：{title, text}；text 为富文本 HTML
  let heights = [];       // 每行实测高度（含 GAP）
  let prefixH = [];       // prefixH[i] = 第 i 行之前各行高度累计（未测量行按 est 估算）
  let est = 120;          // 行高估算（标题行 + 少量正文）；实测后滑动平均收敛
  let _scrollDir = 1, _viewportY = 0, _lastLo = 0; // 滚动方向 / 视口基准 / 上次窗口下界（空白兜底）
  let _viewportRaf = 0;   // rAF 节流句柄
  let _progJumpTs = 0;    // 程序性跳转时间戳（让跳转期间的参考行锚定让路）
  let _activeTocIdx = -1; // 当前激活的目录项索引
  let lastUserScrollTs = 0, lastAnyScrollTs = 0; // 用户滚动时间戳（锚定补偿守卫）

  const listEl = document.getElementById('list');
  const host = document.getElementById('host');
  const tocList = document.getElementById('tocList');
  const emptyState = document.getElementById('emptyState');
  const bookTitleEl = document.getElementById('bookTitle');
  const bookAuthorEl = document.getElementById('bookAuthor');
  const bookPathEl = document.getElementById('bookPath');
  const statusEl = document.getElementById('status');
  const toastWrap = document.getElementById('toast');
  const undoBtn = document.getElementById('undoBtn');
  const redoBtn = document.getElementById('redoBtn');
  const popup = document.getElementById('popup');
  const ctxMenu = document.getElementById('contextMenu');
  const toolbar = document.getElementById('toolbar');

  function setStatus(msg) { if (statusEl) statusEl.textContent = msg || ''; }

  function toast(msg, kind) {
    if (!toastWrap) return;
    const el = document.createElement('div');
    el.className = 'toast ' + (kind || '');
    el.textContent = msg;
    toastWrap.appendChild(el);
    setTimeout(function () { el.classList.add('show'); }, 10);
    setTimeout(function () {
      el.classList.remove('show');
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 250);
    }, 3000);
  }

  async function fetchJSON(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) {
      let msg = url + ' -> ' + r.status;
      try { const j = await r.json(); if (j && j.error) msg = j.error; } catch (e) { /* 保留状态码消息 */ }
      throw new Error(msg);
    }
    return r.json();
  }

  function updateEmptyState() {
    if (!emptyState) return;
    emptyState.style.display = articles.length ? 'none' : 'block';
  }

  // ---------- 前缀和 / 高度 ----------
  function rebuildPrefix() {
    let sum = 0;
    for (let i = 0; i < articles.length; i++) {
      prefixH[i] = sum;
      sum += (heights[i] > 0 ? heights[i] : est);
    }
    prefixH[articles.length] = sum;
  }
  function prefixTop(i) { return i <= 0 ? 0 : (prefixH[i] != null ? prefixH[i] : i * est); }
  function totalHeight() { return prefixTop(articles.length); }

  function measureRow(i) {
    const row = host.querySelector('.art-row[data-i="' + i + '"]');
    if (!row) return;
    const h = row.offsetHeight + GAP;
    if (h > 0) {
      heights[i] = h;
      est = Math.round((est * 3 + h) / 4);
    }
  }

  // 行内容变化（正文输入）后的高度修正：重测本行 + 重建前缀 + 重排下方行（rAF 合并）
  let _remeasureRaf = 0, _remeasureIdx = -1;
  function scheduleRemeasure(i) {
    _remeasureIdx = i;
    if (_remeasureRaf) return;
    _remeasureRaf = requestAnimationFrame(function () {
      _remeasureRaf = 0;
      const idx = _remeasureIdx; _remeasureIdx = -1;
      if (idx < 0 || idx >= articles.length) return;
      measureRow(idx);
      rebuildPrefix();
      reposition();
    });
  }

  // 卸载前把 DOM 上的最新编辑同步回影子数组（防丢失）；搜索标记属视图层，先剥离
  function syncRow(i) {
    if (i < 0 || i >= articles.length) return;
    const row = host.querySelector('.art-row[data-i="' + i + '"]');
    if (!row) return;
    const t = row.querySelector('.art-title');
    if (t) articles[i].title = t.textContent;
    const b = row.querySelector('.art-body');
    if (b) articles[i].text = stripSearchMarks(b.innerHTML);
  }

  function artRow(i) {
    const row = document.createElement('div');
    row.className = 'art-row';
    row.dataset.i = i;
    row.innerHTML =
      '<div class="art-head">' +
        '<span class="art-num"></span>' +
        '<div class="art-title" contenteditable="true" spellcheck="false" role="textbox" aria-multiline="false"></div>' +
        '<button type="button" class="art-remove" title="取消一级标题：本章内容并入上一章，本行消失">取消一级标题</button>' +
      '</div>' +
      '<div class="art-body" contenteditable="true" spellcheck="false" role="textbox" aria-multiline="true"></div>';
    row.querySelector('.art-num').textContent = '第' + (i + 1) + '章';
    const titleEl = row.querySelector('.art-title');
    const bodyEl = row.querySelector('.art-body');
    const rmBtn = row.querySelector('.art-remove');
    titleEl.textContent = articles[i].title || '';
    bodyEl.innerHTML = articles[i].text || '';
    if (i === 0) rmBtn.style.display = 'none'; // 第一篇文章无「上一章」，一级标题必须保留
    titleEl.addEventListener('beforeinput', function () { histBeginInput(i); });
    titleEl.addEventListener('input', function () {
      histTouchInput(i);
      articles[i].title = titleEl.textContent;
      updateTocEntry(i);
    });
    titleEl.addEventListener('blur', function () { articles[i].title = titleEl.textContent; });
    bodyEl.addEventListener('beforeinput', function () { histBeginInput(i); });
    bodyEl.addEventListener('input', function () {
      histTouchInput(i);
      articles[i].text = stripSearchMarks(bodyEl.innerHTML);
      scheduleRemeasure(i);
    });
    bodyEl.addEventListener('blur', function () {
      if (bodyEl.querySelector('mark.ptoe-search')) {
        _unwrapSearchMarks(bodyEl);
        scheduleRemeasure(i);
      }
    });
    rmBtn.addEventListener('click', function (e) {
      e.preventDefault();
      removeArticle(i);
    });
    return row;
  }

  function attach(i, opts) {
    opts = opts || {};
    if (host.querySelector('.art-row[data-i="' + i + '"]')) return;
    const row = artRow(i);
    row.style.position = 'absolute';
    row.style.left = '14px'; row.style.right = '14px'; row.style.top = prefixTop(i) + 'px';
    row.style.margin = '0';
    host.appendChild(row);
    // 搜索进行中：新挂载行补注高亮标记（视图层，不进影子数组）
    if (_searchHighlightQuery) {
      const ed = row.querySelector('.art-body');
      if (ed) {
        try {
          const re = searchRegexFor(_searchHighlightQuery);
          const h = _highlightInHtmlSource(articles[i].text || '', re);
          if (h !== ed.innerHTML) ed.innerHTML = h;
        } catch (e) { /* 正则失效则跳过补注 */ }
      }
    }
    if (opts.defer) return; // 批量路径：由 updateViewport 统一测量/重建前缀/重排
    measureRow(i);
    rebuildPrefix();
    reposition();
  }

  function reposition() {
    const rows = host.children;
    for (let k = 0; k < rows.length; k++) {
      const row = rows[k];
      const i = Number(row.dataset.i);
      const t = prefixTop(i);
      const cur = parseFloat(row.style.top) || 0; // 不读 offsetTop：避免逐行强制 reflow
      if (Math.abs(cur - t) > 1) row.style.top = t + 'px';
    }
    // 防滚动钳制 + 尾部空白封顶（同矫正界面）：可滚动高度绝不在滚动中收缩
    const _floorRaw = listEl.scrollTop + listEl.clientHeight + BUFFER * GAP;
    const _floorCap = totalHeight() + listEl.clientHeight + BUFFER * GAP;
    host.style.height = Math.max(totalHeight(), Math.min(_floorRaw, _floorCap)) + 'px';
  }

  // ---------- 滚动驱动 ----------
  function markUserScroll() { lastUserScrollTs = Date.now(); lastAnyScrollTs = Date.now(); }
  function markAnyScroll() { lastAnyScrollTs = Date.now(); }
  function scheduleViewport() {
    if (_viewportRaf) return;
    _viewportRaf = requestAnimationFrame(function () { _viewportRaf = 0; updateViewport(); });
  }

  function updateViewport() {
    const sy = listEl.scrollTop;
    const _nowTs = Date.now();
    const _userScrolling = (_nowTs - lastUserScrollTs) < 300;
    const _progJump = (_nowTs - _progJumpTs) < 300;
    const _isJump = _progJump ||
      (Math.abs(sy - _viewportY) > (listEl.clientHeight || 800) * 2 && !_userScrolling);
    if (sy !== _viewportY) { _scrollDir = sy > _viewportY ? 1 : -1; _viewportY = sy; }
    const y = Math.max(0, sy - 60); // 顶部留 60px 余量（同矫正界面）
    let lo = 0, hi = articles.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (prefixTop(mid) < y) lo = mid + 1; else hi = mid; }
    const viewRows = Math.ceil(((listEl.clientHeight || 800) - 140) / (est || 120)) + 1;
    // 空白兜底：进入空滚区（scrollY 超总高）时回退上一窗口下界，绝不彻底空白
    if (sy > totalHeight() + (listEl.clientHeight || 800)) { lo = Math.min(lo, _lastLo); }
    // 内容锚定（解析式）：向下锚顶行、向上锚底缘行，布局重建后把内容放回原偏移处
    let _anchorIdx = lo;
    if (!_isJump && _scrollDir < 0) {
      const yBot = y + (listEl.clientHeight || 800);
      let aLo = 0, aHi = articles.length;
      while (aLo < aHi) { const amid = (aLo + aHi) >> 1; if (prefixTop(amid) < yBot) aLo = amid + 1; else aHi = amid; }
      _anchorIdx = Math.min(aLo, articles.length);
    }
    const _anchorOff = _isJump ? null : (prefixTop(_anchorIdx) - sy);
    const first = Math.max(0, lo - BUFFER - (_scrollDir < 0 ? PRELOAD : 0));
    const last = Math.min(articles.length, lo + viewRows + BUFFER + (_scrollDir > 0 ? PRELOAD : 0));
    const keep = {};
    const toAttach = [];
    for (let i = first; i < last; i++) {
      keep[i] = true;
      if (!host.querySelector('.art-row[data-i="' + i + '"]')) toAttach.push(i);
    }
    try {
      for (const i of toAttach) attach(i, { defer: true });
      for (const i of toAttach) measureRow(i); // 全部挂载后再统一测量：一次布局
      for (const row of Array.prototype.slice.call(host.children)) {
        const ri = Number(row.dataset.i);
        if (!keep[ri]) {
          syncRow(ri); // 卸载前写回影子数组
          row.remove();
        }
      }
    } finally {
      _lastLo = lo;
      rebuildPrefix();
      reposition();
    }
    // 内容锚定补偿：布局重建后把锚定行放回原内容偏移处（同帧原子生效）
    if (_anchorOff !== null) {
      const d = (prefixTop(_anchorIdx) - listEl.scrollTop) - _anchorOff;
      if (Math.abs(d) > 1) {
        listEl.scrollTop = Math.max(0, listEl.scrollTop + d);
        _viewportY = listEl.scrollTop;
      }
    }
    setActiveToc(lo);
  }

  // ---------- 目录 ----------
  function setActiveToc(i) {
    if (i === _activeTocIdx) return;
    if (articles.length === 0) { _activeTocIdx = -1; return; }
    if (i < 0) i = 0; if (i >= articles.length) i = articles.length - 1;
    _activeTocIdx = i;
    const items = tocList.children;
    for (let k = 0; k < items.length; k++) {
      const it = items[k];
      if (Number(it.dataset.i) === i) it.classList.add('active');
      else it.classList.remove('active');
    }
    const rows = host.children;
    for (let r = 0; r < rows.length; r++) {
      const row = rows[r];
      if (Number(row.dataset.i) === i) row.classList.add('active');
      else row.classList.remove('active');
    }
    const act = tocList.querySelector('.toc-item.active');
    if (act && act.scrollIntoView) { try { act.scrollIntoView({ block: 'nearest' }); } catch (e) { /* jsdom 等环境可能未实现 */ } }
  }

  function updateTocEntry(i) {
    const item = tocList.querySelector('.toc-item[data-i="' + i + '"]');
    if (!item) return;
    const t = item.querySelector('.toc-title');
    if (t) t.textContent = articles[i].title || '（未命名章节）';
  }

  function rebuildToc() {
    tocList.innerHTML = '';
    for (let i = 0; i < articles.length; i++) {
      const item = document.createElement('div');
      item.className = 'toc-item';
      item.dataset.i = i;
      item.innerHTML = '<span class="toc-index"></span><span class="toc-title"></span>';
      item.querySelector('.toc-index').textContent = '第' + (i + 1) + '章';
      item.querySelector('.toc-title').textContent = articles[i].title || '（未命名章节）';
      item.addEventListener('click', function () { jumpTo(Number(this.dataset.i)); });
      tocList.appendChild(item);
    }
    _activeTocIdx = -1;
    if (articles.length) setActiveToc(0);
  }

  function jumpTo(i) {
    if (i < 0 || i >= articles.length) return;
    _progJumpTs = Date.now();
    listEl.scrollTop = prefixTop(i); // 未测量行按 est 估算，跳转瞬时定位
    updateViewport();
    setActiveToc(i);
  }

  // ---------- 撤销 / 重做（全书快照） ----------
  // 快照粒度为「操作」：一次连续输入（间隔 < UNDO_IDLE_MS）算一次操作；
  // 格式/对齐/标题/新增/取消一级标题/搜索替换/清理/繁简转换等离散操作各算一次。
  // undoStack/redoStack 各保留最近 UNDO_LIMIT（10）步；新操作清空重做栈。
  // 快照 = 全书 article 深拷贝（书籍规模小，简单可靠）。
  const UNDO_LIMIT = 10;
  const UNDO_IDLE_MS = 800;
  let undoStack = [];   // [{before, after, label}]
  let redoStack = [];
  let currentUndo = null; // 进行中的输入操作 {before, label}；空闲超时后落栈
  let undoIdleTimer = null;
  let inDiscreteOp = false; // 离散操作正在改 DOM（抑制 beforeinput 误开输入操作）
  let _histDepth = 0;       // 嵌套 histRun 深度：>0 时内层直接执行（外层已建快照）

  function snapAll() {
    return articles.map(function (a) { return { title: a.title, text: a.text }; });
  }
  function histUpdateButtons() {
    // 有进行中的输入操作也可撤（undo() 会先落栈）——输入即亮起，无需等 800ms 空闲
    if (undoBtn) undoBtn.disabled = !undoStack.length && !currentUndo;
    if (redoBtn) redoBtn.disabled = !redoStack.length;
  }
  function histPush(before, after, label) {
    undoStack.push({ before: before, after: after, label: label });
    if (undoStack.length > UNDO_LIMIT) undoStack.shift();
    redoStack.length = 0; // 新操作使重做历史失效
    histUpdateButtons();
  }
  function histCommitInput() {
    if (undoIdleTimer) { clearTimeout(undoIdleTimer); undoIdleTimer = null; }
    if (!currentUndo) return;
    histPush(currentUndo.before, snapAll(), currentUndo.label);
    currentUndo = null;
  }
  function histIdle() { undoIdleTimer = null; histCommitInput(); }
  function histScheduleIdle() {
    if (undoIdleTimer) clearTimeout(undoIdleTimer);
    undoIdleTimer = setTimeout(histIdle, UNDO_IDLE_MS);
  }
  function histBeginInput(i) {
    // beforeinput 在 DOM 变更前触发 → 可捕获操作前快照。重复触发幂等；
    // 离散操作改 DOM 期间（ensureBlockWrap 等）忽略，防止把格式操作误记为「输入」。
    if (i < 0 || inDiscreteOp) return;
    if (currentUndo) { histScheduleIdle(); return; }
    currentUndo = { before: snapAll(), label: '输入' };
    histScheduleIdle();
    histUpdateButtons();
  }
  function histTouchInput(i) {
    if (i < 0 || inDiscreteOp) return;
    if (currentUndo) histScheduleIdle();
  }
  // 离散（同步）操作包装：收掉进行中的输入操作，捕获 before，执行 fn，按变化提交
  function histRun(label, fn) {
    if (_histDepth > 0) return fn(); // 嵌套 histRun：外层已建快照，直接执行
    _histDepth++;
    histCommitInput();
    const before = snapAll();
    inDiscreteOp = true;
    let out;
    try { out = fn(); } finally { inDiscreteOp = false; _histDepth--; }
    const after = snapAll();
    if (JSON.stringify(after) !== JSON.stringify(before)) histPush(before, after, label);
    return out;
  }
  // 离散（异步）操作：histBegin 返回 before 快照，完成后 histEnd 提交（无变化不入栈）
  function histBegin(label) {
    histCommitInput();
    return snapAll();
  }
  function histEnd(before, label) {
    const after = snapAll();
    if (JSON.stringify(after) !== JSON.stringify(before)) histPush(before, after, label);
  }
  function restoreAll(snap) {
    // 记录当前编辑焦点（正文/标题），恢复后尽量保持位置与光标
    const active = document.activeElement;
    const focusedRow = active && active.closest ? active.closest('.art-row') : null;
    let focusIdx = focusedRow ? Number(focusedRow.dataset.i) : (_activeTocIdx >= 0 ? _activeTocIdx : 0);
    if (focusIdx < 0 || focusIdx >= snap.length) focusIdx = 0;
    const wasBody = !!(active && active.classList && active.classList.contains('art-body'));
    articles = snap.map(function (a) { return { title: a.title, text: a.text }; });
    rebuildAll();
    rebuildToc();
    if (snap.length) {
      jumpTo(focusIdx);
      const row = host.querySelector('.art-row[data-i="' + focusIdx + '"]');
      if (row && active && active.textContent !== undefined) {
        const ed = wasBody ? row.querySelector('.art-body') : row.querySelector('.art-title');
        if (ed) {
          ed.focus();
          try {
            const sel = window.getSelection();
            const r = document.createRange();
            r.selectNodeContents(ed);
            r.collapse(false); // 光标移到末尾
            sel.removeAllRanges();
            sel.addRange(r);
          } catch (e) { /* 部分环境（jsdom）可能缺少 */ }
        }
      }
    }
  }
  function undo() {
    histCommitInput(); // 收掉进行中的输入操作，避免与撤销交错（须先于空栈判断：输入操作未落栈时要能撤销）
    if (!undoStack.length) { setStatus('没有可撤回的操作'); return; }
    const entry = undoStack.pop();
    redoStack.push({ before: snapAll(), after: entry.after, label: entry.label });
    if (redoStack.length > UNDO_LIMIT) redoStack.shift();
    restoreAll(entry.before);
    histUpdateButtons();
    setStatus('已撤回：' + (entry.label || '操作'));
  }
  function redo() {
    if (!redoStack.length) { setStatus('没有可重做的操作'); return; }
    const entry = redoStack.pop();
    undoStack.push({ before: snapAll(), after: entry.after, label: entry.label });
    if (undoStack.length > UNDO_LIMIT) undoStack.shift();
    restoreAll(entry.after);
    histUpdateButtons();
    setStatus('已重做：' + (entry.label || '操作'));
  }
  // 恢复完成后清理历史（数据结构重建等场景；本编辑器暂无调用点，保留可测钩子）
  function histClear() {
    if (undoIdleTimer) { clearTimeout(undoIdleTimer); undoIdleTimer = null; }
    currentUndo = null;
    undoStack = []; redoStack = [];
    histUpdateButtons();
  }

  // ---------- 操作目标解析 ----------
  // 当前章索引：右键菜单打开期间优先 _ctxIdx（右键目标行）；否则取焦点行 →
  // 选区所在行 → 目录激活项 → 默认 0。
  let _ctxIdx = -1; // 被右键的文章索引（ctxRun 同步段内有效）
  function currentIdx() {
    if (_ctxIdx >= 0 && _ctxIdx < articles.length) return _ctxIdx;
    const active = document.activeElement;
    if (active && active.closest) {
      const row = active.closest('.art-row');
      if (row) return Number(row.dataset.i);
    }
    const sel = window.getSelection();
    if (sel && sel.rangeCount > 0) {
      for (let n = 0; n < 2; n++) {
        const node = n === 0 ? sel.getRangeAt(0).startContainer : sel.anchorNode;
        if (!node) continue;
        const el = node.nodeType === 3 ? node.parentNode : node;
        if (el && el.closest) {
          const row = el.closest('.art-row');
          if (row) return Number(row.dataset.i);
        }
      }
    }
    if (_activeTocIdx >= 0 && _activeTocIdx < articles.length) return _activeTocIdx;
    return 0;
  }
  function bodyFor(i) {
    const row = host.querySelector('.art-row[data-i="' + i + '"]');
    const b = row && row.querySelector('.art-body');
    if (b) return { body: b, detached: false };
    const div = document.createElement('div');
    div.className = 'art-body';
    div.innerHTML = articles[i].text || '';
    return { body: div, detached: true };
  }
  // 把（可能为挂载/分离的）正文 DOM 变更加载回影子数组并同步到挂载行
  function commitBody(i, body) {
    const html = stripSearchMarks(body.innerHTML);
    articles[i].text = html;
    const row = host.querySelector('.art-row[data-i="' + i + '"]');
    if (row) {
      const b = row.querySelector('.art-body');
      if (b && b !== body) b.innerHTML = html;
      scheduleRemeasure(i);
    }
  }
  // 服务端返回的 HTML 落盘并同步挂载行
  function applyHtmlToArticle(i, html) {
    const clean = stripSearchMarks(html || '');
    articles[i].text = clean;
    const row = host.querySelector('.art-row[data-i="' + i + '"]');
    if (row) {
      const b = row.querySelector('.art-body');
      if (b) b.innerHTML = clean;
      scheduleRemeasure(i);
    }
  }

  // ---------- 行内格式（8 类 span 类，与 EPUB 导出白名单一致的类名） ----------
  const INLINE_CLASSES = ['ptoe-underline', 'ptoe-underdot', 'ptoe-strike', 'ptoe-charbox', 'ptoe-shade', 'ptoe-highlight', 'ptoe-sup', 'ptoe-sub'];
  const INLINE_CLASS_LABEL = {
    underline: '下划线', underdot: '下加点', strike: '删除线', charbox: '字符边框',
    shade: '底纹', highlight: '突显', sup: '上标', sub: '下标'
  };
  // 上/下标互斥：同一区域不能同时是上标和下标
  const INLINE_MUTEX = {
    'ptoe-sup': ['ptoe-sup', 'ptoe-sub'],
    'ptoe-sub': ['ptoe-sup', 'ptoe-sub']
  };
  // 边界位置比较：(nodeA, offA) 与 (nodeB, offB) 的前后关系（-1/0/1）。
  // 不依赖 Range.compareBoundaryPoints——jsdom 对「元素容器边界 vs 其内文本节点
  // 边界」返回错误顺序（selectNodeContents 的 span 边界 vs 其中文本选区会判成
  // 不重叠），真实浏览器正确但 harness 无法跑（2026-09 修复）。
  function _posCmp(nodeA, offA, nodeB, offB) {
    if (nodeA === nodeB) {
      if (offA < offB) return -1;
      if (offA > offB) return 1;
      return 0;
    }
    // nodeA 是 nodeB 的子/后代：nodeA 位置由其最近子节点索引决定
    let cur = nodeA;
    while (cur && cur !== nodeB) cur = cur.parentNode;
    if (cur === nodeB) {
      let child = nodeA;
      while (child && child.parentNode !== nodeB) child = child.parentNode;
      const ci = Array.prototype.indexOf.call(nodeB.childNodes, child);
      return ci < offB ? -1 : 1; // ci == offB → (nodeA,offA) 恰在 (nodeB,offB) 处之后
    }
    cur = nodeB;
    while (cur && cur !== nodeA) cur = cur.parentNode;
    if (cur === nodeA) {
      let child = nodeB;
      while (child && child.parentNode !== nodeA) child = child.parentNode;
      const ci = Array.prototype.indexOf.call(nodeA.childNodes, child);
      return ci < offA ? 1 : -1;
    }
    // 分叉兄弟子树：沿父链找最近公共祖先前一个分叉子节点
    const pathA = []; let n = nodeA;
    while (n) { pathA.push(n); n = n.parentNode; }
    const pathB = []; let m = nodeB;
    while (m) { pathB.push(m); m = m.parentNode; }
    let i = pathA.length - 1, j = pathB.length - 1;
    while (i >= 0 && j >= 0 && pathA[i] === pathB[j]) { i--; j--; }
    const parent = pathA[i + 1];
    if (!parent) return 0;
    const idxA = Array.prototype.indexOf.call(parent.childNodes, pathA[i]);
    const idxB = Array.prototype.indexOf.call(parent.childNodes, pathB[j]);
    return idxA < idxB ? -1 : (idxA > idxB ? 1 : 0);
  }
  // 相交判定：range 与 other 有公共区间（含恰为全等/包含）。不能用「完全包含」式
  // 比较——选区恰好等于 span 内容时恒不命中，清除格式 / 上标互斥解包会漏掉
  // 精确选中的 span（2026-09 修复）。
  function _intersects(range, other) {
    // range 起点 ≥ other 终点 → range 完全在 other 之后
    if (_posCmp(range.startContainer, range.startOffset, other.endContainer, other.endOffset) >= 0) return false;
    // range 终点 ≤ other 起点 → range 完全在 other 之前
    if (_posCmp(range.endContainer, range.endOffset, other.startContainer, other.startOffset) <= 0) return false;
    return true;
  }

  // 解包与给定 range 相交的指定 class 的 span：把子节点移回父节点，保持顺序
  function unwrapSpans(block, cls, range) {
    const spans = block.querySelectorAll('span.' + cls);
    for (const span of Array.prototype.slice.call(spans)) {
      try {
        const spanRange = document.createRange();
        spanRange.selectNodeContents(span);
        if (_intersects(range, spanRange)) {
          const frag = document.createDocumentFragment();
          while (span.firstChild) frag.appendChild(span.firstChild);
          span.parentNode.replaceChild(frag, span);
        }
      } catch (e) { /* best-effort: 跳过异常 span */ }
    }
  }
  // 解包与 range 相交的 B/STRONG/I/EM 标签（清除格式用）
  function unwrapTags(block, range, tags) {
    const nodes = block.querySelectorAll(tags.join(','));
    for (const el of Array.prototype.slice.call(nodes)) {
      try {
        const spanRange = document.createRange();
        spanRange.selectNodeContents(el);
        if (_intersects(range, spanRange)) {
          const frag = document.createDocumentFragment();
          while (el.firstChild) frag.appendChild(el.firstChild);
          el.parentNode.replaceChild(frag, el);
        }
      } catch (e) { /* best-effort */ }
    }
  }
  // 把单个 block 的 range 包裹为 span.cls（整体 extractContents；异常回退逐文本节点）
  function wrapRange(block, range, cls) {
    try {
      const clone = range.cloneRange();
      if (clone.collapsed) return; // 空选区不插入空标签
      const frag = clone.extractContents();
      const span = document.createElement('span');
      span.className = cls;
      span.appendChild(frag);
      clone.insertNode(span);
      range.setStartBefore(span);
      range.setEndAfter(span);
      return;
    } catch (e) {
      const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, {
        acceptNode: function (node) {
          if (node.parentElement && node.parentElement.closest('span.' + cls)) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        }
      }, false);
      const textNodes = [];
      let node;
      while ((node = walker.nextNode())) {
        const nodeRange = document.createRange();
        nodeRange.selectNodeContents(node);
        if (_intersects(range, nodeRange)) {
          textNodes.push(node);
        }
      }
      for (const tn of textNodes) {
        if (tn.textContent === '') continue;
        const tnRange = document.createRange();
        tnRange.selectNodeContents(tn);
        const start = Math.max(0, range.startContainer === tn ? range.startOffset : 0);
        const end = range.endContainer === tn ? range.endOffset : tn.textContent.length;
        if (start >= end) continue;
        const segRange = document.createRange();
        segRange.setStart(tn, start);
        segRange.setEnd(tn, end);
        const segFrag = segRange.extractContents();
        const s = document.createElement('span');
        s.className = cls;
        s.appendChild(segFrag);
        segRange.insertNode(s);
      }
    }
  }
  // 加粗/斜体：包裹 <b>/<i>（EPUB 导出时归一 strong/em 的白名单标签）
  function wrapInline(block, range, tag) {
    try {
const clone = range.cloneRange();
      if (clone.collapsed) return; // 空选区不插入空标签
      const frag = clone.extractContents();
      const el = document.createElement(tag);
      el.appendChild(frag);
      clone.insertNode(el);
      range.setStartBefore(el);
      range.setEndAfter(el);
      return;
    } catch (e) {
      const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, {
        acceptNode: function (node) {
          if (node.parentElement && node.parentElement.closest(tag)) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        }
      }, false);
      const textNodes = [];
      let node;
      while ((node = walker.nextNode())) {
        const nodeRange = document.createRange();
        nodeRange.selectNodeContents(node);
        if (_intersects(range, nodeRange)) {
          textNodes.push(node);
        }
      }
      for (const tn of textNodes) {
        if (tn.textContent === '') continue;
        const tnRange = document.createRange();
        tnRange.selectNodeContents(tn);
        const start = Math.max(0, range.startContainer === tn ? range.startOffset : 0);
        const end = range.endContainer === tn ? range.endOffset : tn.textContent.length;
        if (start >= end) continue;
        const segRange = document.createRange();
        segRange.setStart(tn, start);
        segRange.setEnd(tn, end);
        const segFrag = segRange.extractContents();
        const s = document.createElement(tag);
        s.appendChild(segFrag);
        segRange.insertNode(s);
      }
    }
  }
  // 块标签转换（逐个移动子节点保留节点身份；无父节点（分离容器）时不动）
  function _convertBlockTag(block, newTag) {
    if (!block || !block.parentNode) return block;
    const newEl = document.createElement(newTag);
    for (let i = 0; i < block.attributes.length; i++) {
      const a = block.attributes[i];
      const n = a.name.toLowerCase();
      if (n === 'class') newEl.className = block.className;
      else if (n === 'id' || n.startsWith('data-') || n.startsWith('aria-')) {
        try { newEl.setAttribute(a.name, a.value); } catch (e) { }
      }
    }
    while (block.firstChild) newEl.appendChild(block.firstChild);
    block.parentNode.replaceChild(newEl, block);
    return newEl;
  }
  // 选区边界落在正文直属文本节点时的块解析：按方向吸附最近真实块；无法解析回退 ed
  function _boundaryBlockInRange(ed, node, isEnd) {
    const el = node.nodeType === 3 ? node.parentElement : node;
    const b = (el && el.closest) ? el.closest('p,div,h1,h2,h3,h4,h5,h6') : null;
    if (b && b !== ed && ed.contains(b)) return b;
    if (node.nodeType !== 3 || el !== ed) return ed;
    const w = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT, null, false);
    w.currentNode = node;
    let t = isEnd ? w.previousNode() : w.nextNode();
    while (t) {
      const p = t.parentElement;
      const bb = (p && p.closest) ? p.closest('p,div,h1,h2,h3,h4,h5,h6') : null;
      if (bb && bb !== ed && ed.contains(bb)) return bb;
      t = isEnd ? w.previousNode() : w.nextNode();
    }
    return ed;
  }
  function _blocksBetween(ed, startBlock, endBlock) {
    const blocks = [];
    const walker = document.createTreeWalker(ed, NodeFilter.SHOW_ELEMENT, {
      acceptNode: function (n) {
        const tag = n.tagName;
        return /^(P|DIV|H[1-6])$/.test(tag) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      }
    });
    let cur = walker.nextNode();
    let started = false;
    if (startBlock === ed) startBlock = null; // 起点为正文容器本身：从首块收集
    while (cur) {
      if (!startBlock || cur === startBlock) started = true;
      if (started) blocks.push(cur);
      if (cur === endBlock) break;
      cur = walker.nextNode();
    }
    return blocks;
  }
  // 对选区覆盖的多块统一执行 fn(block, range)；选区不在 ed 内时整篇应用（右键/分离行）
  function applyInBody(body, fn) {
    const selRange = selectionInBody(body);
    if (selRange) return applyToSelectedBlocks(body, fn);
    // 整篇应用：先用 <p> 包裹裸文本（无块结构时），再逐块执行
    ensureBlockWrap(body);
    const blocks = [];
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_ELEMENT, {
      acceptNode: function (n) {
        const tag = n.tagName;
        return /^(P|DIV|H[1-6])$/.test(tag) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      }
    });
    let cur;
    while ((cur = walker.nextNode())) blocks.push(cur);
    for (const blk of blocks) fn(blk, null);
    return blocks;
  }
  function selectionInBody(body) {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return null;
    const r = sel.getRangeAt(0);
    if (body.contains(r.startContainer) && body.contains(r.endContainer)) return r;
    return null;
  }
  function ensureBlockWrap(body) {
    // 正文为裸文本/行内节点（无任何块元素）时包裹为 <p>，保证块级操作有目标
    if (body.querySelector('p,div,h1,h2,h3,h4,h5,h6')) return;
    const p = document.createElement('p');
    while (body.firstChild) p.appendChild(body.firstChild);
    body.appendChild(p);
  }
  function wholeRange(block) {
    const r = document.createRange();
    r.selectNodeContents(block);
    return r;
  }
  function applyToSelectedBlocks(ed, fn) {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return [];
    const origRanges = [];
    for (let i = 0; i < sel.rangeCount; i++) origRanges.push(sel.getRangeAt(i).cloneRange());
    const range = sel.getRangeAt(0);
    const startBlock = _boundaryBlockInRange(ed, range.startContainer, false);
    const endBlock = _boundaryBlockInRange(ed, range.endContainer, true);
    const blocks = _blocksBetween(ed, startBlock, endBlock);
    let lastLiveRange = null; // 最后一个仍连通的块级 range（fn 可能把选区内容提取/包裹）
    for (const block of blocks) {
      try {
        const r = document.createRange();
        if (block === startBlock) r.setStart(range.startContainer, range.startOffset);
        else r.setStart(block, 0);
        if (block === endBlock) r.setEnd(range.endContainer, range.endOffset);
        else r.setEnd(block, block.childNodes.length);
        sel.removeAllRanges();
        sel.addRange(r);
        lastLiveRange = r;
        fn(block, r);
      } catch (e) { continue; } // best-effort: 跳过异常块
    }
    sel.removeAllRanges();
    // 恢复选区：原选区若已失效（内容被 extractContents 提取/包裹→容器脱离文档，
    // addRange 会把死选区钳制为折叠）则退回 fn 更新后的最后连通 range；
    // 否则重新选择新包裹元素，保证后续操作（如右键清除格式）拿到有效选区。
    for (let ri = 0; ri < origRanges.length; ri++) {
      const rr = origRanges[ri];
      if (rr.startContainer.isConnected && rr.endContainer.isConnected && !rr.collapsed) { sel.addRange(rr); continue; }
      if (lastLiveRange && lastLiveRange.startContainer.isConnected) { sel.addRange(lastLiveRange); lastLiveRange = null; continue; }
      if (blocks.length) {
        try {
          const fb = document.createRange();
          fb.selectNodeContents(blocks[0]);
          fb.collapse(false);
          sel.addRange(fb);
        } catch (e2) { /* 尽力而为 */ }
      }
    }
    return blocks;
  }
  // 滚动稳定包装：全程还原 listEl 滚动位置（底部居中的小菜单操作可能触发浏览器自动滚动）
  function withScrollStable(fn) {
    const before = listEl.scrollTop;
    const ts = lastUserScrollTs;
    const restore = function () {
      if (lastUserScrollTs !== ts) return; // 期间用户滚动过：放弃还原
      const dy = listEl.scrollTop - before;
      if (Math.abs(dy) > 2) listEl.scrollTop = Math.max(0, before);
    };
    try {
      const out = fn();
      requestAnimationFrame(restore);
      requestAnimationFrame(function () { requestAnimationFrame(restore); });
      return out;
    } catch (e) {
      requestAnimationFrame(restore);
      throw e;
    }
  }

  // ---------- 格式操作（applyOp 分发目标） ----------
  const HEAD_NEXT = { p: 'h2', div: 'h2', h2: 'h3', h3: 'h4', h4: 'h5', h5: 'h6', h6: 'p' }; // 正文内标题：H2-H6 循环，绝不含 H1
  function applyBoldItalic(body, i, tag) {
    histRun(tag === 'b' ? '加粗' : '斜体', function () {
      const selRange = selectionInBody(body);
      if (!selRange || selRange.collapsed) { toast('请先选中要加粗/斜体的文字', 'warn'); return; }
      withScrollStable(function () {
        applyToSelectedBlocks(body, function (block, r) { wrapInline(block, r, tag); });
      });
      commitBody(i, body);
    });
  }
  function applyHeading(body, i) {
    histRun('标题', function () {
      applyInBody(body, function (block) {
        const tag = block.tagName.toLowerCase();
        const next = HEAD_NEXT[tag] || 'h2';
        if (next !== tag) _convertBlockTag(block, next);
      });
      commitBody(i, body);
      setStatus('已应用标题（正文内标题，不含一级标题）');
    });
  }
  function applyPara(body, i) {
    histRun('正文', function () {
      applyInBody(body, function (block) {
        const tag = block.tagName.toLowerCase();
        if (tag !== 'p' && (tag === 'div' || /^h[2-6]$/.test(tag))) _convertBlockTag(block, 'p');
      });
      commitBody(i, body);
      setStatus('已转为正文');
    });
  }
  function applyNote(body, i) {
    histRun('注释', function () {
      applyInBody(body, function (block) { block.classList.toggle('ptoe-note'); });
      commitBody(i, body);
    });
  }
  function applyAlign(body, i, pos) {
    histRun('对齐', function () {
      applyInBody(body, function (block) {
        block.classList.remove('ptoe-align-left', 'ptoe-align-center', 'ptoe-align-right');
        block.classList.add('ptoe-align-' + pos);
      });
      commitBody(i, body);
    });
  }
  function applyInlineClass(body, i, cls) {
    const label = INLINE_CLASS_LABEL[cls.replace('ptoe-', '')] || cls;
    histRun(label, function () {
      applyInBody(body, function (block, r) {
        const range = r || wholeRange(block);
        // 先基于原 DOM 判断本 class 是否与 range 相交（互斥解包前）：
        // 互斥表含本 class 自身——若先解包再查「现有」，toggle 关闭会被判定为
        // 「无现有」而重新包裹（上标/下标点两次永远关不掉，2026-09 修复）。
        let hasCls = false;
        const existing = block.querySelectorAll('span.' + cls);
        for (const span of existing) {
          const spanRange = document.createRange();
          spanRange.selectNodeContents(span);
          if (_intersects(range, spanRange)) {
            hasCls = true;
            break;
          }
        }
        if (INLINE_MUTEX[cls]) {
          for (const mCls of INLINE_MUTEX[cls]) unwrapSpans(block, mCls, range);
        }
        if (!hasCls) wrapRange(block, range, cls); // toggle 语义：互斥解包已完成关闭；None 则包裹打开
      });
      commitBody(i, body);
    });
  }
  function applyRemove(body, i) {
    histRun('清除格式', function () {
      applyInBody(body, function (block) {
        // 清除格式按整块作用（右键清除格式的语义=清除该段全部格式，而非仅选中相交部分）
        const range = wholeRange(block);
        unwrapTags(block, range, ['b', 'strong', 'i', 'em']);
        for (const cls of INLINE_CLASSES) unwrapSpans(block, cls, range);
        const tag = block.tagName.toLowerCase();
        if (tag === 'div' || /^h[2-6]$/.test(tag)) _convertBlockTag(block, 'p');
        block.classList.remove('ptoe-note', 'ptoe-align-left', 'ptoe-align-center', 'ptoe-align-right');
      });
      commitBody(i, body);
      scheduleRemeasure(i);
      setStatus('已清除格式');
    });
  }

  // ---------- 操作分发（工具栏 / 弹出菜单 / 右键菜单 / 快捷键共用） ----------
  function applyOp(op) {
    if (op === 'undo') return undo();
    if (op === 'redo') return redo();
    if (op === 't2s') return convertText('t2s');
    if (op === 's2t') return convertText('s2t');
    if (op === 'clean') return cleanText();
    if (op === 'search') return openSearchModal(-1);
    if (op === 'addArticle') return addArticle();
    if (op === 'save') return saveEpub();
    const i = currentIdx();
    if (i < 0 || i >= articles.length) { toast('请先点击某一篇文章', 'warn'); return; }
    const b = bodyFor(i);
    const body = b.body;
    switch (op) {
      case 'bold': applyBoldItalic(body, i, 'b'); break;
      case 'italic': applyBoldItalic(body, i, 'i'); break;
      case 'heading': applyHeading(body, i); break;
      case 'p': applyPara(body, i); break;
      case 'remove': applyRemove(body, i); break;
      case 'note': applyNote(body, i); break;
      case 'align_left': applyAlign(body, i, 'left'); break;
      case 'align_center': applyAlign(body, i, 'center'); break;
      case 'align_right': applyAlign(body, i, 'right'); break;
      case 'sup': applyInlineClass(body, i, 'ptoe-sup'); break;
      case 'sub': applyInlineClass(body, i, 'ptoe-sub'); break;
      case 'highlight': applyInlineClass(body, i, 'ptoe-highlight'); break;
      case 'underdot': applyInlineClass(body, i, 'ptoe-underdot'); break;
      default: break;
    }
  }

  // ---------- 搜索 / 替换（当前章范围） ----------
  // searchResults 为当前结果列表（上限 200 条），searchCurrent 为当前选中序号。
  // 高亮为视图层（<mark class="ptoe-search">），不进影子数组——同步写回时剥离。
  let searchResults = [];
  let searchCurrent = -1;
  let _searchHighlightQuery = '';
  let _pendingSearchIdx = -1; // 从右键菜单打开时锁定的章（否则跟随当前章）
  let searchModalOpen = false;

  function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  function decodeEntities(s) {
    return String(s).replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'");
  }
  function pageText(i) {
    // 搜索用纯文本：与 replaceAll/replaceCurrent 完全相同的 token 切分（标签间原文含实体）
    return (articles[i].text || '').split(/(<[^>]+>)/).filter(function (t) { return t && t.charAt(0) !== '<'; }).join('');
  }
  function searchRegexFor(query) {
    const regexEl = document.getElementById('searchRegex');
    const regexMode = !!(regexEl && regexEl.checked);
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
    if (count) count.textContent = '共 ' + total + ' 处匹配' + (total > MAX ? '，仅显示前 ' + MAX + ' 条' : '');
    if (list) {
      list.innerHTML = '';
      if (!results.length) {
        list.innerHTML = '<div class="sr-empty">未找到匹配内容</div>';
        return;
      }
      results.forEach(function (r, k) {
        const item = document.createElement('div');
        item.className = 'sr-item';
        item.innerHTML = '<div class="sr-page">第 ' + r.page + ' 章</div><div class="sr-ctx">' + r.ctx + '</div>';
        item.addEventListener('click', function () {
          searchCurrent = k;
          updateSearchNav();
          scrollToIndex(r.i);
        });
        list.appendChild(item);
      });
    }
  }
  function openSearchModal(pinIdx) {
    searchModalOpen = true;
    // 打开时锁定搜索章（右键菜单显式锁定；工具栏打开则取「当前章」快照）。
    // 不能在 searchPages/replaceAll 时再解析 currentIdx()——打开后焦点已移到
    // searchInput，选区/焦点信号失效，会退回错章（2026-09 修复）。
    _pendingSearchIdx = (pinIdx >= 0 && pinIdx < articles.length) ? pinIdx : currentIdx();
    const bg = document.getElementById('searchModalBg');
    if (bg) { bg.style.display = 'flex'; bg.hidden = false; }
    const inp = document.getElementById('searchInput');
    if (inp) inp.focus();
    const idx = _pendingSearchIdx;
    setStatus('搜索范围：第 ' + (idx + 1) + ' 章');
  }
  function closeSearchModal() {
    searchModalOpen = false;
    _pendingSearchIdx = -1;
    const bg = document.getElementById('searchModalBg');
    if (bg) { bg.style.display = 'none'; bg.hidden = true; }
  }
  function searchPages() {
    const query = document.getElementById('searchInput').value;
    if (!query) { toast('请先输入搜索词', 'warn'); return; }
    let re;
    try { re = searchRegexFor(query); }
    catch (e) { toast('正则表达式无效：' + e.message, 'fail'); return; }
    const i = (_pendingSearchIdx >= 0 && _pendingSearchIdx < articles.length) ? _pendingSearchIdx : currentIdx();
    const text = pageText(i);
    const CONTEXT = 40, MAX = 200;
    const results = [];
    let total = 0;
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      const withinPage = total;
      total++;
      if (results.length >= MAX) continue;
      const s = Math.max(0, m.index - CONTEXT);
      const e2 = Math.min(text.length, m.index + m[0].length + CONTEXT);
      results.push({
        i: i, page: i + 1, withinPage: withinPage,
        ctx: esc(decodeEntities(text.slice(s, m.index))) + '<mark>' + esc(decodeEntities(m[0])) + '</mark>' + esc(decodeEntities(text.slice(m.index + m[0].length, e2)))
      });
    }
    searchResults = results;
    searchCurrent = results.length ? 0 : -1;
    renderSearchResults(results, total, MAX);
    updateSearchNav();
    applySearchHighlights();
    if (total === 0) { toast('未找到匹配内容', 'warn'); setStatus('未找到匹配内容'); }
    else setStatus('第 ' + (i + 1) + ' 章内找到 ' + total + ' 处');
  }
  function gotoMatch(dir) {
    if (!searchResults.length) return;
    searchCurrent = (searchCurrent + dir + searchResults.length) % searchResults.length;
    updateSearchNav();
    const cur = searchResults[searchCurrent];
    const list = document.getElementById('searchList');
    if (list && list.children[searchCurrent]) {
      try { list.children[searchCurrent].scrollIntoView({ block: 'nearest' }); } catch (e) { }
    }
    scrollToIndex(cur.i);
    setStatus('第 ' + (cur.i + 1) + ' 章，第 ' + (searchCurrent + 1) + ' / ' + searchResults.length + ' 处');
  }
  function scrollToIndex(idx) {
    if (idx < 0 || idx >= articles.length) return;
    _progJumpTs = Date.now();
    listEl.scrollTop = Math.max(0, prefixTop(idx) - 12);
    updateViewport();
    setActiveToc(idx);
    hidePopup();
  }
  function _highlightInHtmlSource(html, re) {
    return String(html).split(/(<[^>]+>)/).map(function (tok) {
      if (!tok) return '';
      if (tok.charAt(0) === '<') return tok;
      return tok.replace(re, function (m) { return '<mark class="ptoe-search">' + esc(m) + '</mark>'; });
    }).join('');
  }
  function _unwrapSearchMarks(root) {
    let changed = false;
    let found = true;
    while (found) {
      found = false;
      root.querySelectorAll('mark.ptoe-search').forEach(function (el) {
        found = true;
        changed = true;
        el.parentNode.replaceChild(document.createTextNode(el.textContent), el);
      });
      if (found) { try { root.normalize(); } catch (e) { } }
    }
    return changed;
  }
  // 剥离 HTML 字符串中的搜索标记（影子数组落盘/保存前调用；DOM 版保证结构完整）
  function stripSearchMarks(html) {
    if (!html || String(html).indexOf('ptoe-search') < 0) return String(html);
    const d = document.createElement('div');
    d.innerHTML = String(html);
    if (_unwrapSearchMarks(d)) return d.innerHTML;
    return String(html);
  }
  function applySearchHighlights() {
    const q = (document.getElementById('searchInput').value || '').trim();
    if (!q) { clearSearchHighlights(); return; }
    let re;
    try { re = searchRegexFor(q); } catch (e) { return; }
    _searchHighlightQuery = ''; // 先置空，避免回注旧标记/双重包裹
    for (const row of Array.prototype.slice.call(host.children)) {
      const idx = Number(row.dataset.i);
      const ed = row.querySelector('.art-body');
      if (!ed) continue;
      const isFocused = ed === document.activeElement || ed.contains(document.activeElement);
      _unwrapSearchMarks(ed);
      if (isFocused) continue; // 聚焦行跳过替换（input 写回时已剥离标记），blur 时清理
      const src = articles[idx].text;
      const highlighted = _highlightInHtmlSource(src, re);
      if (highlighted !== ed.innerHTML) {
        ed.innerHTML = highlighted;
        scheduleRemeasure(idx);
      }
    }
    _searchHighlightQuery = q;
  }
  function clearSearchHighlights() {
    _searchHighlightQuery = ''; // 先置空，避免还原时回注
    for (const row of Array.prototype.slice.call(host.children)) {
      const idx = Number(row.dataset.i);
      const ed = row.querySelector('.art-body');
      if (ed && _unwrapSearchMarks(ed)) scheduleRemeasure(idx);
    }
  }
  function clearSearchState() {
    searchResults = [];
    searchCurrent = -1;
    clearSearchHighlights();
    renderSearchResults([], 0, 200);
    updateSearchNav();
    setStatus('');
  }
  function replaceCurrent() {
    // 只替换当前选中的那处匹配（第 searchCurrent 条），其余匹配保持不动
    if (!searchResults.length || searchCurrent < 0) { toast('请先搜索', 'warn'); return; }
    const query = document.getElementById('searchInput').value;
    const repl = document.getElementById('replaceInput').value;
    let re;
    try { re = searchRegexFor(query); }
    catch (e) { toast('正则表达式无效：' + e.message, 'fail'); return; }
    const cur = searchResults[searchCurrent];
    const i = cur.i;
    const target = cur.withinPage; // 该章内第 target 处（0 起）
    const before = histBegin('替换当前');
    let out, c = 0;
    out = (articles[i].text || '').split(/(<[^>]+>)/).map(function (tok) {
      if (!tok) return '';
      if (tok.charAt(0) === '<') return tok;
      return tok.replace(re, function (m) { const n = c++; return (n === target) ? repl : m; });
    }).join('');
    if (c <= target) { toast('该处匹配已变化，请重新搜索', 'warn'); return; }
    applyHtmlToArticle(i, out);
    histEnd(before, '替换当前');
    toast('已替换当前匹配（第 ' + (searchCurrent + 1) + ' 条）', 'ok');
    searchPages(); // 替换后刷新结果列表与序号
  }
  function replaceAll() {
    const query = document.getElementById('searchInput').value;
    const repl = document.getElementById('replaceInput').value;
    if (!query) { toast('请先输入搜索词', 'warn'); return; }
    let re;
    try { re = searchRegexFor(query); }
    catch (e) { toast('正则表达式无效：' + e.message, 'fail'); return; }
    const i = (_pendingSearchIdx >= 0 && _pendingSearchIdx < articles.length) ? _pendingSearchIdx : currentIdx();
    const before = histBegin('全部替换');
    let out, c = 0;
    out = (articles[i].text || '').split(/(<[^>]+>)/).map(function (tok) {
      if (!tok) return '';
      if (tok.charAt(0) === '<') return tok;
      return tok.replace(re, function () { c++; return repl; });
    }).join('');
    if (c === 0) { toast('未找到匹配内容，未替换', 'warn'); return; }
    applyHtmlToArticle(i, out);
    histEnd(before, '全部替换');
    setStatus('第 ' + (i + 1) + ' 章已替换 ' + c + ' 处');
    toast('已替换 ' + c + ' 处', 'ok');
    searchPages(); // 替换后刷新结果列表（匹配数可能变化）
  }

  // ---------- 繁简转换 / 智能清理（服务端 /api/convert、/api/clean） ----------
  async function convertText(mode) {
    const i = currentIdx();
    const label = mode === 't2s' ? '繁转简' : '简转繁';
    if (i < 0 || i >= articles.length) { toast('请先点击某一篇文章', 'warn'); return; }
    const src = articles[i].text || '';
    if (!src.trim()) { toast('当前章正文为空', 'warn'); return; }
    const before = histBegin(label);
    try {
      const res = await fetchJSON('/api/convert', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ html: src, mode: mode })
      });
      if (!res || res.ok === false) throw new Error((res && res.error) || label + '失败');
      applyHtmlToArticle(i, res.html);
      histEnd(before, label);
      setStatus('第 ' + (i + 1) + ' 章已' + (mode === 't2s' ? '转为简体' : '转为繁体'));
      toast(label + '完成', 'ok');
    } catch (e) {
      toast(label + '失败: ' + e.message, 'fail');
    }
  }
  async function cleanText() {
    const i = currentIdx();
    if (i < 0 || i >= articles.length) { toast('请先点击某一篇文章', 'warn'); return; }
    const src = articles[i].text || '';
    if (!src.trim()) { toast('当前章正文为空', 'warn'); return; }
    const before = histBegin('智能清理');
    try {
      const res = await fetchJSON('/api/clean', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ html: src })
      });
      if (!res || res.ok === false) throw new Error((res && res.error) || '清理失败');
      applyHtmlToArticle(i, res.html);
      histEnd(before, '智能清理');
      setStatus('第 ' + (i + 1) + ' 章已清理');
      toast('智能清理完成', 'ok');
    } catch (e) {
      toast('清理失败: ' + e.message, 'fail');
    }
  }

  // ---------- 选中文字快捷菜单 ----------
  let _popupBuilt = false;
  let suppressPopupUntilTs = 0; // 操作按钮点击后窗口内不弹菜单（防误弹）
  function hidePopup() {
    if (popup) { popup.hidden = true; popup.style.display = 'none'; }
  }
  function buildPopup() {
    if (_popupBuilt || !popup) return;
    _popupBuilt = true;
    const rows = [
      [['bold', 'B', '加粗'], ['italic', 'I', '斜体'], ['heading', '标', '标题'], ['p', '正', '正文'],
       ['remove', '清', '清除格式'], ['note', '注', '注释'],
       ['align_left', '左', '左对齐'], ['align_center', '中', '居中'], ['align_right', '右', '右对齐']],
      [['sup', '上标', '上标'], ['sub', '下标', '下标']]
    ];
    for (const items of rows) {
      const r = document.createElement('div');
      r.className = 'pop-row';
      for (const it of items) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'pop-btn';
        b.textContent = it[1];
        b.title = it[2];
        b.setAttribute('aria-label', it[2]);
        b.addEventListener('mousedown', function (e) {
          e.preventDefault(); // 保持正文选区不丢
          suppressPopupUntilTs = performance.now() + 300;
          hidePopup();
          applyOp(it[0]);
        });
        r.appendChild(b);
      }
      popup.appendChild(r);
    }
  }
  function showPopup(range) {
    buildPopup();
    popup.hidden = false;
    popup.style.display = 'flex';
    const r = popup.getBoundingClientRect();
    const rects = (range.getClientRects) ? range.getClientRects() : null;
    const rect = (rects && rects.length > 0) ? rects[0] :
      ((range.getBoundingClientRect) ? range.getBoundingClientRect() : { left: 8, top: 8, width: 0, height: 0 });
    let left = rect.left + rect.width / 2 - r.width / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - r.width - 8));
    let top = rect.top - r.height - 8;
    if (top < 8) top = rect.bottom + 8;
    popup.style.left = left + 'px';
    popup.style.top = top + 'px';
  }
  function bodyFromSelection(range, sel) {
    // 选区所属 .art-body：优先 commonAncestorContainer，再尝试起止/锚点焦点节点
    const candidates = [];
    const add = function (node) {
      if (node) {
        const el = node.nodeType === 3 ? node.parentNode : node;
        if (el && el.closest) candidates.push(el.closest('.art-body'));
      }
    };
    add(range.commonAncestorContainer);
    add(range.startContainer);
    add(range.endContainer);
    if (sel) { add(sel.anchorNode); add(sel.focusNode); }
    for (const c of candidates) { if (c) return c; }
    return null;
  }
  function maybeShowPopup() {
    if (performance.now() < suppressPopupUntilTs) return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) { hidePopup(); return; }
    const range = sel.getRangeAt(0);
    const body = bodyFromSelection(range, sel);
    if (!body) { hidePopup(); return; }
    showPopup(range);
  }

  // ---------- 右键上下文菜单 ----------
  let ctxMenuOpen = false;
  let _ctxRange = null;   // 右键位置选中 range（jsdom 无 caretRangeFromPoint，仅存活选区）
  let _ctxEscCapture = null;
  function closeContextMenu() {
    ctxMenuOpen = false;
    if (ctxMenu) { ctxMenu.hidden = true; ctxMenu.style.display = 'none'; }
    if (ctxMenu) {
      ctxMenu.querySelectorAll('.ctx-sub.open').forEach(function (el) { el.classList.remove('open'); });
    }
    if (_ctxEscCapture) { document.removeEventListener('keydown', _ctxEscCapture, true); _ctxEscCapture = null; }
    _ctxIdx = -1; // 防陈旧右键目标泄漏到后续工具栏操作
    _ctxRange = null;
  }
  function orientCtxSubs() {
    if (!ctxMenu) return;
    ctxMenu.querySelectorAll('.ctx-sub').forEach(function (sub) {
      const sm = sub.querySelector('.ctx-submenu');
      if (!sm) return;
      const pr = sub.getBoundingClientRect();
      const sw = sm.offsetWidth || 150, sh = sm.offsetHeight || 160;
      const left = pr.right + 4;
      if (left + sw > window.innerWidth) {
        if (pr.left - 4 - sw >= 0) sm.classList.add('ctx-left');
        else sm.classList.remove('ctx-left');
      } else {
        sm.classList.remove('ctx-left');
      }
      if (pr.top - 5 + sh > window.innerHeight && pr.bottom + 5 - sh >= 0) sm.classList.add('ctx-up');
      else sm.classList.remove('ctx-up');
    });
  }
  function toggleCtxSub(parent) {
    if (!ctxMenu) return;
    const wasOpen = parent.classList.contains('open');
    ctxMenu.querySelectorAll('.ctx-sub.open').forEach(function (el) { el.classList.remove('open'); });
    if (!wasOpen) {
      parent.classList.add('open');
      orientCtxSubs();
    }
  }
  function openContextMenu(x, y) {
    if (!ctxMenu) return;
    hidePopup();
    suppressPopupUntilTs = performance.now() + 300; // 右键后的 mouseup 不弹选中菜单
    ctxMenu.hidden = false;
    try { ctxMenu.style.display = ''; } catch (e) { }
    const w = ctxMenu.offsetWidth || 172, h = ctxMenu.offsetHeight || 240;
    const cx = Math.max(8, Math.min(x, window.innerWidth - w - 8));
    const cy = Math.max(8, Math.min(y, window.innerHeight - h - 8));
    ctxMenu.style.left = cx + 'px';
    ctxMenu.style.top = cy + 'px';
    orientCtxSubs();
    ctxMenuOpen = true;
    if (!_ctxEscCapture) {
      _ctxEscCapture = function (e) {
        if (e.key === 'Escape' && ctxMenuOpen) {
          e.preventDefault();
          e.stopPropagation();
          closeContextMenu();
        }
      };
      document.addEventListener('keydown', _ctxEscCapture, true);
    }
  }
  // 菜单项执行：先关菜单再执行，异常 toast。
  // 捕获-关闭-恢复-执行-清空：closeContextMenu 会清掉 _ctxIdx/_ctxRange，故先取出保存、
  // 关菜单后恢复，fn 同步段内可用 currentIdx() 取右键目标章；finally 清空防泄漏
  function ctxRun(fn) {
    const idx = _ctxIdx;
    const range = _ctxRange;
    closeContextMenu();
    _ctxIdx = idx;
    _ctxRange = range;
    suppressPopupUntilTs = performance.now() + 300;
    try { fn(); } catch (e) { toast('操作失败：' + e.message, 'fail'); }
    finally { _ctxIdx = -1; _ctxRange = null; }
  }
  // 把光标/选区落回右键目标章正文（页级格式操作前保证选区目标正确）
  function ctxFocusBody(i) {
    const row = host.querySelector('.art-row[data-i="' + i + '"]');
    if (!row) return;
    const ed = row.querySelector('.art-body');
    if (!ed) return;
    ed.focus();
    const sel = window.getSelection();
    let r = (_ctxRange && ed.contains(_ctxRange.startContainer)) ? _ctxRange : null;
    if (!r) {
      r = document.createRange();
      r.selectNodeContents(ed);
      r.collapse(true);
    }
    try {
      sel.removeAllRanges();
      sel.addRange(r);
    } catch (e) { /* best-effort */ }
  }

  // ---------- 操作：取消一级标题 / 新增一级标题 / 保存 ----------
  function removeArticle(i) {
    if (i <= 0 || i >= articles.length) return; // 第一篇文章保留一级标题
    histRun('取消一级标题', function () {
      const prev = articles[i - 1];
      const cur = articles[i];
      // 标题文字保留为正文内标题（h2），防止内容丢失
      var curTitle = String(cur.title || '').trim();
      var curBody = cur.text || '';
      if (curTitle) {
        curBody = '<h2>' + esc(curTitle) + '</h2>' + (curBody ? '\n\n' + curBody : '');
      }
      var merged = ((prev.text || '') + '\n\n' + curBody)
        .replace(/\n{3,}/g, '\n\n')
        .replace(/^\s+|\s+$/g, '');
      prev.text = merged;
      articles.splice(i, 1);
      rebuildAll();
      rebuildToc();
    });
    setStatus('已取消一级标题，内容并入第 ' + i + ' 章');
  }

  function addArticle() {
    histRun('新增一级标题', function () {
      articles.push({ title: '新章节', text: '' });
      rebuildAll();
      rebuildToc();
    });
    setStatus('已新增章节（共 ' + articles.length + ' 章）');
    jumpTo(articles.length - 1); // 滚到新行
  }

  function rebuildAll() {
    const n = articles.length;
    heights = new Array(n).fill(0);
    prefixH = new Array(n + 1).fill(0);
    host.innerHTML = '';
    _lastLo = 0; _scrollDir = 1; _viewportY = listEl.scrollTop;
    _activeTocIdx = -1;
    rebuildPrefix();
    host.style.height = totalHeight() + 'px';
    updateEmptyState();
    updateViewport();
  }

  function collect() {
    // 影子数组是唯一数据源：编辑事件/撤销重做/undo 均会写回 articles，
    // 此处不再回读挂载行 DOM（否则会覆盖对 articles 的直接写入，2026-09 修复）。
    return {
      path: undefined,
      articles: articles.map(function (a) {
        return { title: String(a.title || '').trim() || '未命名章节', text: stripSearchMarks(String(a.text || '')) };
      })
    };
  }

  async function saveEpub() {
    if (saveBtn) { saveBtn.disabled = true; saveBtn.classList.add('loading'); }
    try {
      const res = await fetchJSON('/api/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(collect())
      });
      toast(res && res.path ? '已保存: ' + res.path : '已保存', 'ok');
      setStatus(res && res.path ? '已保存: ' + res.path : '已保存');
    } catch (e) {
      toast('保存失败: ' + e.message, 'fail');
      setStatus('保存失败: ' + e.message);
    } finally {
      if (saveBtn) { saveBtn.disabled = false; saveBtn.classList.remove('loading'); }
    }
  }

  // ---------- 事件接线：工具栏（data-op 委托） / 弹出菜单 / 右键菜单 / 搜索框 / 快捷键 ----------
  if (toolbar) {
    // mousedown preventDefault：保持正文选区不丢（按钮不抢焦点）
    toolbar.addEventListener('mousedown', function (e) {
      const btn = e.target && e.target.closest ? e.target.closest('[data-op]') : null;
      if (btn) {
        e.preventDefault();
        suppressPopupUntilTs = performance.now() + 250;
      }
    });
    toolbar.addEventListener('click', function (e) {
      const btn = e.target && e.target.closest ? e.target.closest('[data-op]') : null;
      if (!btn) return;
      applyOp(btn.dataset.op);
    });
  }
  // addBtn/saveBtn 已由 data-op 委托覆盖（removeArticle 的「取消一级标题」按钮仍行内直绑）

  // ----- 选中文字快捷菜单 -----
  document.addEventListener('mouseup', maybeShowPopup);
  document.addEventListener('keyup', function (e) {
    if (!e.isComposing && !e.ctrlKey && !e.metaKey && !e.altKey) maybeShowPopup();
  });
  document.addEventListener('selectionchange', function () {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) hidePopup();
  });

  // ----- 右键菜单 -----
  if (host) {
    host.addEventListener('contextmenu', function (e) {
      const row = e.target && e.target.closest ? e.target.closest('.art-row') : null;
      if (!row) return;
      e.preventDefault();
      const i = Number(row.dataset.i);
      _ctxIdx = i;
      _ctxRange = null;
      try {
        const sel = window.getSelection();
        if (sel && sel.rangeCount > 0) {
          const r = sel.getRangeAt(0);
          if (row.contains(r.startContainer) && row.contains(r.endContainer)) _ctxRange = r;
        }
      } catch (err) { /* best-effort */ }
      const rmBtn = document.getElementById('ctxRemoveBtn');
      if (rmBtn) rmBtn.disabled = (i <= 0);
      openContextMenu(e.clientX || 100, e.clientY || 100);
    });
  }
  if (ctxMenu) {
    ctxMenu.addEventListener('click', function (e) {
      // 二级菜单父项点击：切换展开
      const parent = e.target && e.target.closest ? e.target.closest('.ctx-sub') : null;
      if (parent && !(e.target.closest && e.target.closest('[data-ctx]'))) {
        toggleCtxSub(parent);
        return;
      }
      const btn = e.target && e.target.closest ? e.target.closest('.ctx-item[data-ctx]') : null;
      if (!btn) return;
      const opMap = { add: 'addArticle', removefmt: 'remove' };
      const op = opMap[btn.dataset.ctx] || btn.dataset.ctx;
      ctxRun(function () {
        // data-ctx="remove"（取消一级标题）与 data-ctx="removefmt"（清除格式）——删除按钮
        // 特判须按原始 data-ctx 区分：op 已被 opMap 归一，removefmt→'remove' 会误命中删除分支
        if (btn.dataset.ctx === 'remove') {
          const idx = currentIdx();
          if (idx <= 0) { toast('第一章无法取消一级标题', 'warn'); return; }
          removeArticle(idx);
          return;
        }
        if (op === 'search') { openSearchModal(currentIdx()); return; }
        applyOp(op);
      });
    });
    // hover 展开二级菜单
    ctxMenu.addEventListener('mouseover', function (e) {
      const sub = e.target && e.target.closest ? e.target.closest('.ctx-sub') : null;
      if (sub) toggleCtxSub(sub);
    });
  }
  // 点击菜单外关闭
  document.addEventListener('mousedown', function (e) {
    if (ctxMenuOpen && ctxMenu && !ctxMenu.contains(e.target)) closeContextMenu();
  });

  // ----- 搜索框 -----
  const searchBtn = document.getElementById('searchBtn');
  const searchInputEl = document.getElementById('searchInput');
  const replaceBtn = document.getElementById('replaceBtn');
  const replaceAllBtn = document.getElementById('replaceAllBtn');
  const replaceInputEl = document.getElementById('replaceInput');
  const searchPrevBtn = document.getElementById('searchPrevBtn');
  const searchNextBtn = document.getElementById('searchNextBtn');
  const searchCloseBtn = document.getElementById('searchCloseBtn');
  const searchClearBtn = document.getElementById('searchClearBtn');
  if (searchBtn) searchBtn.addEventListener('click', searchPages);
  if (searchInputEl) {
    searchInputEl.addEventListener('keydown', function (e) { if (e.key === 'Enter') searchPages(); });
    searchInputEl.addEventListener('input', function () {
      if (!(this.value || '').trim()) clearSearchHighlights();
    });
  }
  if (replaceBtn) replaceBtn.addEventListener('click', replaceCurrent);
  if (replaceAllBtn) replaceAllBtn.addEventListener('click', replaceAll);
  if (replaceInputEl) replaceInputEl.addEventListener('keydown', function (e) { if (e.key === 'Enter') replaceCurrent(); });
  if (searchPrevBtn) searchPrevBtn.addEventListener('click', function () { gotoMatch(-1); });
  if (searchNextBtn) searchNextBtn.addEventListener('click', function () { gotoMatch(1); });
  if (searchCloseBtn) searchCloseBtn.addEventListener('click', closeSearchModal);
  if (searchClearBtn) searchClearBtn.addEventListener('click', clearSearchState);
  _backdropClickClose('searchModalBg', closeSearchModal);

  // 遮罩点击关闭守卫（2026-09）：拖选输入框文字时 mouseup 落遮罩上会误关——
  // 仅当 mousedown 也起于遮罩（真正的「点遮罩关闭」）时才响应
  function _backdropClickClose(bgId, closeFn) {
    const bg = document.getElementById(bgId);
    if (!bg) return;
    let downOnBg = false;
    bg.addEventListener('mousedown', function (e) { downOnBg = (e.target === bg); });
    bg.addEventListener('click', function (e) { if (e.target === bg && downOnBg) closeFn(); });
  }

  // ----- 快捷键：Ctrl+Z / Ctrl+Y / Escape -----
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (ctxMenuOpen) { e.preventDefault(); closeContextMenu(); return; }
      if (popup && !popup.hidden) { e.preventDefault(); hidePopup(); return; }
      if (searchModalOpen) { e.preventDefault(); closeSearchModal(); return; }
      return;
    }
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && (e.key === 'z' || e.key === 'Z')) {
      e.preventDefault(); undo(); return;
    }
    if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'y' || e.key === 'Y')) {
      e.preventDefault(); redo(); return;
    }
  }, true); // capture：抢在 contenteditable 默认行为前

  // 滚动关闭弹出菜单
  listEl.addEventListener('scroll', function () { markAnyScroll(); hidePopup(); scheduleViewport(); }, { passive: true });
  document.addEventListener('scroll', function () { hidePopup(); }, true);
  window.addEventListener('scroll', function () { markAnyScroll(); hidePopup(); scheduleViewport(); }, { passive: true });
  window.addEventListener('wheel', markUserScroll, { passive: true });
  window.addEventListener('touchmove', markUserScroll, { passive: true });
  window.addEventListener('resize', scheduleViewport);
  setInterval(function () { fetch('/api/ping').catch(function () { }); }, 30000);
  window.addEventListener('pagehide', function () {
    try { navigator.sendBeacon('/api/bye'); } catch (e) { /* 环境不支持 sendBeacon */ }
  });

  // ---------- 初始化 ----------
  (async function init() {
    try {
      const res = await fetchJSON('/api/book');
      if (!res || res.ok === false) throw new Error((res && res.error) || 'book 接口返回失败');
      articles = (res.articles || []).map(function (a) {
        return { title: a && a.title ? String(a.title) : '', text: a && a.text ? String(a.text) : '' };
      });
      if (bookTitleEl) bookTitleEl.textContent = res.title || '';
      if (bookAuthorEl) bookAuthorEl.textContent = res.author || '';
      if (bookPathEl) bookPathEl.textContent = res.path || '';
    } catch (e) {
      setStatus('加载失败: ' + e.message);
      if (emptyState) { emptyState.textContent = '加载失败: ' + e.message; emptyState.style.display = 'block'; }
      return;
    }
    if (!articles.length) {
      rebuildToc();
      updateEmptyState(); // 触发空态
      setStatus('本书暂无章节');
      return;
    }
    rebuildAll();
    rebuildToc();
    setStatus('已加载 ' + articles.length + ' 章');
  })();

  // 测试/驱动钩子：harness 需要读取影子数组与调用各功能入口
  window.epubedit = {
    get articles() { return articles; },
    save: saveEpub,
    addArticle: addArticle,
    removeArticle: removeArticle,
    jumpTo: jumpTo,
    collect: collect,
    rebuildAll: rebuildAll,
    rebuildToc: rebuildToc,
    updateViewport: updateViewport,
    currentIdx: currentIdx,
    applyOp: applyOp,
    undo: undo,
    redo: redo,
    histClear: histClear,
    convert: convertText,
    clean: cleanText,
    search: searchPages,
    openSearchModal: openSearchModal,
    closeSearchModal: closeSearchModal,
    searchResults: function () { return searchResults; },
    searchCurrent: function () { return searchCurrent; }
  };
})();