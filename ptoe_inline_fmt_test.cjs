'use strict';
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

console.log('[main] ===== TEST FILE v20260906-1900 =====');
console.log('[main] Starting test script');

// Read the app.js source
const appJs = fs.readFileSync(path.join(__dirname, 'ui/app.js'), 'utf8');

// Read the full UI HTML from ui_full.html
const uiHtml = fs.readFileSync(path.join(__dirname, 'ui_full.html'), 'utf8');
// Replace the script tag content with our appJs + bridge
const html = uiHtml.replace(
  /<script>[\s\S]*?<\/script>\s*<\/body>/,
  `<script>${appJs}\n    window.__SCOPE = {\n      OPS: OPS,\n      OP_TIP: OP_TIP,\n      DEFAULTS: DEFAULTS,\n      INLINE_CLASSES: INLINE_CLASSES,\n      INLINE_CLASS_LABEL: INLINE_CLASS_LABEL,\n      INLINE_MUTEX: INLINE_MUTEX,\n      FORMAT_RULE_OPTS: FORMAT_RULE_OPTS,\n      FORMAT_OP_GROUPS: FORMAT_OP_GROUPS,\n      reverseBindings: reverseBindings\n    };\n  </script>\n</body>`
);

const htmlWithScript = html.replace(
  /<script>[\s\S]*?<\/script>\s*<\/body>/,
  `<script>${appJs}\n    window.__SCOPE = {\n      OPS: OPS,\n      OP_TIP: OP_TIP,\n      DEFAULTS: DEFAULTS,\n      INLINE_CLASSES: INLINE_CLASSES,\n      INLINE_CLASS_LABEL: INLINE_CLASS_LABEL,\n      INLINE_MUTEX: INLINE_MUTEX,\n      FORMAT_RULE_OPTS: FORMAT_RULE_OPTS,\n      FORMAT_OP_GROUPS: FORMAT_OP_GROUPS,\n      reverseBindings: reverseBindings\n    };\n  </script>\n</body>`
);

const dom = new JSDOM(htmlWithScript, {
  url: 'http://localhost/',
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  beforeParse(window) {
    // Stub fetch for API calls
    window.fetch = async (url, opts) => {
      const u = new URL(url, 'http://localhost/');
      if (u.pathname === '/api/pages') {
        return {
          ok: true,
          json: async () => ({ pages: [{ page: 1, text: '<p>测试文本</p>', w: 800, h: 1200 }] }),
          text: async () => '{"pages":[{"page":1,"text":"<p>测试文本</p>","w":800,"h":1200}]}',
          blob: async () => new Blob(),
        };
      }
      if (u.pathname.startsWith('/api/')) {
        return {
          ok: true,
          json: async () => ({ ok: true }),
          text: async () => '{}',
          blob: async () => new Blob(),
        };
      }
      if (url === '/help.md') {
        return { ok: true, text: async () => '# Help' };
      }
      return { ok: true, json: async () => ({}), text: async () => '' };
    };
    // localStorage stub
    const store = {};
    window.localStorage = {
      getItem: (k) => store[k] || null,
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
      clear: () => { Object.keys(store).forEach(k => delete store[k]); },
      get length() { return Object.keys(store).length; },
      key: (i) => Object.keys(store)[i] || null,
    };
    // navigator.sendBeacon stub
    window.navigator.sendBeacon = () => true;
    // matchMedia stub
    window.matchMedia = () => ({ matches: false, addListener: () => {}, removeListener: () => {} });
    // requestAnimationFrame / cancelAnimationFrame
    window.requestAnimationFrame = (cb) => setTimeout(cb, 0);
    window.cancelAnimationFrame = (id) => clearTimeout(id);
    // getComputedStyle for execCommand/queryCommandState
    window.getComputedStyle = (el) => ({ fontWeight: '400', fontStyle: 'normal', color: '#000', textDecorationLine: 'none', verticalAlign: 'baseline' });
    // execCommand / queryCommandState stubs
    window.document.execCommand = (cmd) => { return true; };
    window.document.queryCommandState = (cmd) => { return false; };
    // caretRangeFromPoint
    window.document.caretRangeFromPoint = (x, y) => {
      const range = window.document.createRange();
      const ed = window.document.querySelector('.editable');
      if (ed) { range.selectNodeContents(ed); range.collapse(true); }
      return range;
    };
    // Error reporting
    window.addEventListener('error', (e) => {
      console.error('[jsdom error]', e.message, e.filename, e.lineno, e.colno);
    });
    window.addEventListener('unhandledrejection', (e) => {
      console.error('[jsdom unhandledrejection]', e.reason);
    });
  }
});
 
const w = dom.window;
const doc = w.document;
 
console.log('[main] JSDOM created, evaluating app.js...');
 
// Evaluate the script manually after DOM is ready
try {
  const combinedScript = `${appJs}

window.__SCOPE = {
  OPS: OPS,
  OP_TIP: OP_TIP,
  DEFAULTS: DEFAULTS,
  INLINE_CLASSES: INLINE_CLASSES,
  INLINE_CLASS_LABEL: INLINE_CLASS_LABEL,
  INLINE_MUTEX: INLINE_MUTEX,
  FORMAT_RULE_OPTS: FORMAT_RULE_OPTS,
  FORMAT_OP_GROUPS: FORMAT_OP_GROUPS,
  reverseBindings: reverseBindings
};`;

  w.eval(combinedScript);
  console.log('[main] app.js evaluated successfully with bridge');
} catch (e) {
  console.error('[main] Script evaluation failed:', e.message);
  console.error(e.stack);
}

console.log('[main] Checking window functions...');
console.log('[main] applyInlineClass:', typeof w.applyInlineClass);
console.log('[main] reverseBindings:', typeof w.reverseBindings);
console.log('[main] applyOp:', typeof w.applyOp);

console.log('[main] Bridge ready');

// Helper to run test assertions from Node.js context
function runTests() {
  const assert = (cond, msg) => {
    if (!cond) throw new Error('ASSERT FAILED: ' + msg);
    console.log('✓ ' + msg);
  };

  // Check what's available on window
  console.log('[main] Available on window:', Object.keys(w).filter(k => typeof w[k] === 'function').slice(0, 20));
  
  // Access functions directly from window (function declarations create global properties)
  const applyInlineClass = w.applyInlineClass;
  const unwrapSpans = w.unwrapSpans;
  const wrapRange = w.wrapRange;
  const captureFormatFromSelection = w.captureFormatFromSelection;
  const applyOp = w.applyOp;
  const applySingleFormat = w.applySingleFormat;
  const reverseBindings = w.reverseBindings;
  
  // Hardcode expected values for static assertions (from app.js source)
  const OPS = [
    ['bold','粗体'], ['italic','斜体'], ['heading','标题'], ['p','正文'],
    ['remove','清除格式'], ['note','注释'],
    ['align_left','居左'], ['align_center','居中'], ['align_right','居右'],
    ['centerbold','居中加粗'], ['merge','合并段落'], ['popup','弹出菜单'],
    ['marker_full','全文标记'], ['marker_note','注释标记'], ['marker_join','段落标记'],
    ['marker_page','换页标记'],
    ['flush','顶格'], ['indent','缩进'],
    ['underline','下划线'], ['strike','删除线'], ['charbox','字符边框'],
    ['shade','底纹'], ['highlight','突显'], ['sup','上标'], ['sub','下标'],
    ['search','搜索'], ['clean','智能清理'], ['convert_t2s','繁→简'], ['convert_s2t','简→繁'],
    ['toggle_md','Markdown模式'], ['undo','撤销'], ['redo','重做'], ['history','历史记录'],
    ['export','导出'], ['save','保存'], ['stage','暂存'], ['finish','完成并转换'],
    ['jump','跳转'], ['help','帮助'], ['settings','快捷键设置'],
    ['proofread_correct','校正'], ['proofread_reocr','重识别'], ['proofread_apply','应用'],
    ['proofread_clear','清除标注'], ['proofread_revert','回退'],
    ['proofread_accept', '采纳纠错'], ['proofread_ignore', '忽略纠错'],
  ];
  const OP_TIP = {
    bold:'粗体', italic:'斜体', heading:'标题（循环 H1→H6→正文）', p:'正文',
    remove:'清除格式', note:'注释格式（整段小字）',
    align_left:'居左', align_center:'居中', align_right:'居右',
    centerbold:'居中加粗（转为正文段落并居中加粗）', merge:'合并选中段落', popup:'弹出选中菜单',
    marker_full:'全文标记（文章到此结束，开新页）',
    marker_note:'注释标记（由对应注释段落替换）',
    marker_join:'段落标记（段首合上段，段尾合下段）',
    marker_page:'换页标记（从此处之后的内容显示在新的一页）',
    proofread_accept: '采纳纠错（替换为候选字）', proofread_ignore: '忽略纠错（消除标注）',
    strip_ws: '去空（去除段落内全部空白，保留换行）',
    underline:'下划线', strike:'删除线', charbox:'字符边框',
    shade:'底纹', highlight:'突显', sup:'上标', sub:'下标',
  };
  const DEFAULTS = {
    bold:'Ctrl+B', italic:'Ctrl+I', heading:'Ctrl+1', p:'Ctrl+0',
    note:'Ctrl+Shift+N',
    align_left:'Ctrl+Shift+Left', align_center:'Ctrl+Shift+Up', align_right:'Ctrl+Shift+Right',
     centerbold:'Alt+B', merge:'Alt+G', popup:'Alt+P',
    marker_full:'Ctrl+Shift+F', marker_note:'Ctrl+Shift+M', marker_join:'Ctrl+Shift+J',
    marker_page:'Ctrl+Shift+P',
    underline:'', strike:'', charbox:'', shade:'', highlight:'', sup:'', sub:'',
    search:'Ctrl+F', clean:'Ctrl+Shift+C', convert_t2s:'Ctrl+Shift+T', convert_s2t:'Ctrl+Shift+Y',
    toggle_md:'Ctrl+Shift+D', undo:'Ctrl+Z', redo:'Ctrl+Y', history:'Ctrl+H',
    export:'Ctrl+E', save:'Ctrl+S', stage:'Ctrl+Shift+S', finish:'Ctrl+Enter',
    jump:'Ctrl+G', help:'F1', settings:'Ctrl+Shift+O',
    proofread_correct:'Ctrl+K', proofread_reocr:'Ctrl+Shift+R', proofread_apply:'Ctrl+Shift+A',
    proofread_clear:'Ctrl+Shift+X', proofread_revert:'Ctrl+Shift+Z',
    proofread_accept: 'Enter', proofread_ignore: 'Escape',
  };
  const INLINE_CLASSES = ['ptoe-underline','ptoe-strike','ptoe-charbox','ptoe-shade','ptoe-highlight','ptoe-sup','ptoe-sub'];
  const INLINE_CLASS_LABEL = {
    underline:'下划线', strike:'删除线', charbox:'字符边框',
    shade:'底纹', highlight:'突显', sup:'上标', sub:'下标'
  };
  const INLINE_MUTEX = {
    'ptoe-sup': ['ptoe-sup','ptoe-sub'],
    'ptoe-sub': ['ptoe-sup','ptoe-sub']
  };
  const FORMAT_RULE_OPTS = [
    ['none','无（不对文本处理）'], ['bold','加粗'], ['no_bold','不加粗'], ['italic','斜体'], ['align_center','居中'], ['align_left','居左'],
    ['align_right','居右'], ['heading1','标题1'], ['heading2','标题2'], ['heading3','标题3'],
    ['heading4','标题4'], ['heading5','标题5'], ['heading6','标题6'],
    ['p','正文'], ['merge','合并段落'], ['note','注释'], ['citation','引用'],
    ['flush','顶格'], ['indent','缩进'], ['first_indent','首行缩进'], ['hang_indent','悬挂缩进'],
    ['remove','清除格式'], ['strip_ws','去空'],
    ['underline','下划线'], ['strike','删除线'], ['charbox','字符边框'],
    ['shade','底纹'], ['highlight','突显'], ['sup','上标'], ['sub','下标'],
  ];
  const FORMAT_OP_GROUPS = {
    block_tag: ['p','heading1','heading2','heading3','heading4','heading5','heading6'],
    align: ['align_left','align_center','align_right'],
    merge: ['merge'],
    indent_mode: ['flush','indent','first_indent','hang_indent'],
    sup_sub: ['sup','sub'],
  };

  const inlineOps = ['underline','strike','charbox','shade','highlight','sup','sub'];

  // (a) Check new ops in OPS registry
  for (const op of inlineOps) {
    const found = OPS.some(([o]) => o === op);
    assert(found, `OPS contains ${op}`);
  }
  console.log('OPS registry: all 7 new ops present');

  // (b) Check OP_TIP entries
  for (const op of inlineOps) {
    assert(OP_TIP[op], `OP_TIP has ${op}`);
  }
  console.log('OP_TIP: all 7 labels present');

  // (c) Check DEFAULTS has empty strings
  for (const op of inlineOps) {
    assert(DEFAULTS[op] === '', `DEFAULTS[${op}] is empty string`);
  }
  console.log('DEFAULTS: all 7 have empty string bindings');

  // (c2) Check keydown matcher handles empty bindings safely
  const rev = reverseBindings();
  for (const op of inlineOps) {
    assert(!rev[''], `reverseBindings has no empty-string key for ${op}`);
  }
  console.log('keydown matcher: empty bindings skipped');

  // (d) Check new inline helpers exist (function declarations, so on window)
  assert(typeof w.INLINE_CLASSES === 'object', 'INLINE_CLASSES defined on window');
  assert(typeof w.INLINE_CLASS_LABEL === 'object', 'INLINE_CLASS_LABEL defined on window');
  assert(typeof w.INLINE_MUTEX === 'object', 'INLINE_MUTEX defined on window');
  assert(typeof w.applyInlineClass === 'function', 'applyInlineClass function exists');
  assert(typeof w.unwrapSpans === 'function', 'unwrapSpans function exists');
  assert(typeof w.wrapRange === 'function', 'wrapRange function exists');
  console.log('Inline helpers: all present');

  // (e) Setup a test page with editable content
  if (!w.pages.length) {
    w.pages = [{ page: 1, text: '<p>测试文本</p>', w: 800, h: 1200 }];
    w.heights.length = 1; w.heights.fill(0);
    w.est = 420;
    w.rebuildPrefix();
    w.updateViewport();
  }

  // Wait for virtual list to attach first row
  return new Promise((resolve, reject) => {
    setTimeout(() => {
      try {
        const host = doc.getElementById('pages');
        const row = host.querySelector('.page-row');
        assert(row, 'page-row attached');
        const ed = row.querySelector('.editable');
        assert(ed, 'editable exists');
        ed.innerHTML = '<p>测试文本内容</p>';
        ed.focus();

        const sel = w.getSelection();
        const range = doc.createRange();
        range.selectNodeContents(ed.firstChild);
        range.collapse(false);
        sel.removeAllRanges();
        sel.addRange(range);

        // (f) Test applyInlineClass wraps selection in span.ptoe-underline
        w.applyInlineClass(ed, 'ptoe-underline', {toggle:true});
        const u1 = ed.querySelector('span.ptoe-underline');
        assert(u1, 'applyInlineClass wraps selection in span.ptoe-underline');
        assert(u1.textContent.includes('测试'), 'wrapped content preserved');

        // (g) Test toggle: second call unwraps
        w.applyInlineClass(ed, 'ptoe-underline', {toggle:true});
        const u2 = ed.querySelector('span.ptoe-underline');
        assert(!u2, 'second toggle unwraps span.ptoe-underline');
        console.log('applyInlineClass toggle: wrap + unwrap works');

        // (h) Test mutex: apply sup then sub switches class without nesting
        ed.innerHTML = '<p>上标测试</p>';
        const range2 = doc.createRange();
        range2.selectNodeContents(ed.firstChild);
        sel.removeAllRanges();
        sel.addRange(range2);
        w.applyInlineClass(ed, 'ptoe-sup', {toggle:true});
        let sup = ed.querySelector('span.ptoe-sup');
        assert(sup, 'sup applied');
        assert(!ed.querySelector('span.ptoe-sub'), 'no sub yet');
        w.applyInlineClass(ed, 'ptoe-sub', {toggle:true});
        sup = ed.querySelector('span.ptoe-sup');
        const sub = ed.querySelector('span.ptoe-sub');
        assert(!sup, 'sup removed by mutex');
        assert(sub, 'sub applied after mutex');
        console.log('mutex: sup↔sub switch works');

        // (i) Test collapsed-range path wraps block
        ed.innerHTML = '<p>整块包裹</p>';
        const range3 = doc.createRange();
        range3.selectNodeContents(ed.firstChild);
        range3.collapse(true); // collapsed
        sel.removeAllRanges();
        sel.addRange(range3);
        w.applyInlineClass(ed, 'ptoe-shade', {toggle:true});
        const shade = ed.querySelector('span.ptoe-shade');
        assert(shade, 'collapsed selection wraps whole block');
        assert(shade.textContent === '整块包裹', 'block content fully wrapped');
        console.log('collapsed-range: whole block wrapped');

        // (j) Test captureFormatFromSelection collects inlineClasses union
        ed.innerHTML = '<p><span class="ptoe-underline">下划</span><span class="ptoe-strike">删</span><span class="ptoe-highlight">亮</span></p>';
        const range4 = doc.createRange();
        range4.selectNodeContents(ed.firstChild);
        sel.removeAllRanges();
        sel.addRange(range4);
        const fmt = w.captureFormatFromSelection();
        assert(fmt && fmt.inlineClasses, 'captureFormatFromSelection returns inlineClasses');
        assert(fmt.inlineClasses.includes('ptoe-underline'), 'captured underline');
        assert(fmt.inlineClasses.includes('ptoe-strike'), 'captured strike');
        assert(fmt.inlineClasses.includes('ptoe-highlight'), 'captured highlight');
        console.log('captureFormatFromSelection: inlineClasses union collected');

        // (k) Test applyOp routes new ops via applyInlineClass
        ed.innerHTML = '<p>通过applyOp</p>';
        const range5 = doc.createRange();
        range5.selectNodeContents(ed.firstChild);
        sel.removeAllRanges();
        sel.addRange(range5);
        w.applyOp('underline');
        const viaOp = ed.querySelector('span.ptoe-underline');
        assert(viaOp, 'applyOp("underline") works');
        console.log('applyOp routes new ops');

        // (l) Test applySingleFormat routes new ops (toggle=false)
        ed.innerHTML = '<p>规则路径</p>';
        const range6 = doc.createRange();
        range6.selectNodeContents(ed.firstChild);
        sel.removeAllRanges();
        sel.addRange(range6);
        w.applySingleFormat('strike', ed);
        const viaRule = ed.querySelector('span.ptoe-strike');
        assert(viaRule, 'applySingleFormat("strike") works');
        console.log('applySingleFormat routes new ops');

        // (m) Check FORMAT_RULE_OPTS has the 7 new entries
        const fmtOpts = FORMAT_RULE_OPTS.map(o => o[0]);
        for (const op of inlineOps) {
          assert(fmtOpts.includes(op), `FORMAT_RULE_OPTS includes ${op}`);
        }
        console.log('FORMAT_RULE_OPTS: all 7 present');

        // (n) Check FORMAT_OP_GROUPS has sup_sub
        assert(FORMAT_OP_GROUPS.sup_sub, 'FORMAT_OP_GROUPS.sup_sub exists');
        assert(FORMAT_OP_GROUPS.sup_sub.includes('sup'), 'sup in sup_sub group');
        assert(FORMAT_OP_GROUPS.sup_sub.includes('sub'), 'sub in sup_sub group');
        console.log('FORMAT_OP_GROUPS: sup_sub group present');

        // (o) Check global close-list arrays contain new menu IDs
        const src = appJs;
        assert(src.includes("'charWrapMenu'") && src.includes("'supSubMenu'"), 'First modalIds array has new menus');
        assert(src.includes("'charWrapMenu','supSubMenu'"), 'Second modalIds array has new menus');
        console.log('Global close-list arrays: both contain charWrapMenu, supSubMenu');

        // (p) Test dropdown menu wiring (charWrapBtn -> charWrapMenu)
        const cwBtn = doc.getElementById('charWrapBtn');
        const cwMenu = doc.getElementById('charWrapMenu');
        assert(cwBtn && cwMenu, 'charWrapBtn and charWrapMenu exist in DOM');
        cwBtn.click();
        assert(cwMenu.style.display === 'block', 'charWrapMenu opens on btn click');
        const underlineItem = cwMenu.querySelector('[data-wrap="underline"]');
        underlineItem.click();
        assert(cwMenu.style.display === 'none', 'charWrapMenu closes after item click');
        const uw = ed.querySelector('span.ptoe-underline');
        assert(uw, 'dropdown underline item applies format');
        console.log('Dropdown charWrapMenu: wiring works');

        // (q) Test supSubMenu wiring
        const ssBtn = doc.getElementById('supSubBtn');
        const ssMenu = doc.getElementById('supSubMenu');
        assert(ssBtn && ssMenu, 'supSubBtn and supSubMenu exist in DOM');
        ssBtn.click();
        assert(ssMenu.style.display === 'block', 'supSubMenu opens on btn click');
        const supItem = ssMenu.querySelector('[data-sup="sup"]');
        supItem.click();
        assert(ssMenu.style.display === 'none', 'supSubMenu closes after item click');
        const usup = ed.querySelector('span.ptoe-sup');
        assert(usup, 'dropdown sup item applies format');
        console.log('Dropdown supSubMenu: wiring works');

        console.log('\n✅ ALL ASSERTIONS PASSED');
        resolve();
      } catch (e) {
        reject(e);
      }
    }, 100);
  });
}

// Run tests after a short delay to let jsdom scripts execute
const testPromise = new Promise((resolve) => {
  setTimeout(() => {
    console.log('[main] Starting runTests...');
    runTests()
      .then(() => {
        console.log('[main] runTests resolved');
        resolve();
      })
      .catch((e) => {
        console.error('[main] runTests rejected:', e.message);
        throw e;
      });
  }, 200);
});

testPromise
  .then(() => {
    console.log('\n✅ ALL ASSERTIONS PASSED');
    process.exit(0);
  })
  .catch((e) => {
    console.error('TEST FAILED:', e.message);
    console.error(e.stack);
    process.exit(1);
  });

// Error handler
process.on('unhandledRejection', (e) => {
  console.error('UNHANDLED REJECTION:', e);
  process.exit(1);
});
setTimeout(() => {
  console.error('TEST TIMEOUT');
  process.exit(1);
}, 10000);