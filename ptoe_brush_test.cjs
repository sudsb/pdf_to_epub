'use strict';
const fs = require('fs'); const path = require('path'); const { JSDOM } = require('jsdom');
const appJs = fs.readFileSync(path.join(__dirname, 'ui/app.js'), 'utf8');
const py = fs.readFileSync(path.join(__dirname, 'correctmanage.py'), 'utf8');
const uiHtml = py.match(/_UI_HTML\s*=\s*r?"""([\s\S]*?)"""/)[1];
function jsonRes(o){return{ok:true,status:200,json:async()=>o,text:async()=>JSON.stringify(o),blob:async()=>new Blob()};}
const dom=new JSDOM(uiHtml,{url:'http://localhost/',runScripts:'outside-only',pretendToBeVisual:true,beforeParse(window){
  window.fetch=async(url)=>{const u=String(url);if(u.startsWith('/api/pages'))return jsonRes({pages:[{page:1,text:'<p>AAA</p>',w:800,h:1200},{page:2,text:'<p>BBB</p>',w:800,h:1200}]});return jsonRes({ok:true});};
  const store={};window.localStorage={getItem:(k)=>(k in store?store[k]:null),setItem:(k,v)=>{store[k]=String(v);},removeItem:()=>{},clear:()=>{},get length(){return 0;},key:()=>null};
  window.navigator.sendBeacon=()=>true;window.matchMedia=()=>({matches:false,addListener:()=>{},removeListener:()=>{}});
  window.requestAnimationFrame=(cb)=>setTimeout(cb,0);window.cancelAnimationFrame=(id)=>clearTimeout(id);window.scrollTo=()=>{};
  window.getComputedStyle=()=>({fontWeight:'400',fontStyle:'normal',color:'#000',textDecorationLine:'none',verticalAlign:'baseline'});
  window.document.queryCommandState=()=>false;window.document.execCommand=()=>false;window.HTMLElement.prototype.scrollIntoView=()=>{};
  window.document.caretRangeFromPoint=()=>{const r=window.document.createRange();const ed=window.document.querySelector('.editable');if(ed){r.selectNodeContents(ed);r.collapse(true);}return r;};
  try{window.URL.createObjectURL=()=>'blob:stub';window.URL.revokeObjectURL=()=>{};}catch(e){}
}});
const w=dom.window,doc=w.document;
w.eval(appJs+`
window.__PT={
  syncContent,
  currentEditable,
  _captureSelection,
  _brushApplyToBlock,
  _brushIsPureScope,
  _brushWatchSelection,
  _selectBlockRange,
  _selectionInBlock,
  _boundaryBlockInRange,
  get brushArmed(){return _brushArmed;},
  set brushArmed(v){_brushArmed=v;},
  get brushLiveSelection(){return _brushLiveSelection;},
  set brushLiveSelection(v){_brushLiveSelection=v;},
  get brushMergeTimer(){return _brushMergeTimer;},
  set brushMergeTimer(v){_brushMergeTimer=v;},
  brushActivateOps:_brushActivateOps,
  brushCancel:_brushCancel,
  brushMergeDelay,
  get uiSettings(){return uiSettings;},
  set uiSettings(v){uiSettings=v;},
};`);
const PT=w.__PT;const sleep=(ms)=>new Promise(r=>setTimeout(r,ms));
let pass=0,fail=0;
function chk(name,cond,extra){if(cond){pass++;console.log('[OK] '+name);}else{fail++;console.log('[FAIL] '+name+(extra?'  '+extra:''));}}

function selectRange(ed, startNode, startOff, endNode, endOff){
  const sel=w.getSelection(); const r=doc.createRange();
  r.setStart(startNode,startOff); r.setEnd(endNode,endOff);
  sel.removeAllRanges(); sel.addRange(r); return r;
}

(async function main(){
  await sleep(120);
  const row=PT.host ? PT.host.children[0] : doc.querySelector('.page-row');
  const ed=row.querySelector('.editable');
  console.log('ed found:', !!ed);

  // ---- T1: pure note brush with a partial (word) captured selection ----
  ed.innerHTML='<p>这是测试文本内容</p>';
  PT.syncContent(ed);
  const p=ed.querySelector('p');
  const tn=p.firstChild; // text node
  // select chars 2..4 ("测试")
  selectRange(ed, tn, 2, tn, 4);
  const cap = PT._captureSelection();
  console.log('T1 captured:', JSON.stringify(cap && {s:cap.startOffset,e:cap.endOffset}));
  PT.brushArmed = { ops:['note'], selection: cap };
  console.log('T1 isPureScope:', PT._brushIsPureScope(['note']));
  console.log('T1 selectionInBlock:', PT._selectionInBlock(cap, p));
  PT._brushApplyToBlock(ed, p);
  const spanNote = ed.querySelector('span.ptoe-note');
  chk('T1 note partial selection wraps inline span', !!spanNote, 'html='+ed.innerHTML);
  chk('T1 note does NOT set block class', !p.classList.contains('ptoe-note'), 'class='+p.className);

  // ---- T2: pure citation brush with partial selection ----
  ed.innerHTML='<p>这是测试文本内容</p>';
  PT.syncContent(ed);
  const p2=ed.querySelector('p');
  const tn2=p2.firstChild;
  selectRange(ed, tn2, 2, tn2, 4);
  PT.brushArmed = { ops:['citation'], selection: PT._captureSelection() };
  PT._brushApplyToBlock(ed, p2);
  const spanCit = ed.querySelector('span.ptoe-citation');
  chk('T2 citation partial selection wraps inline span', !!spanCit, 'html='+ed.innerHTML);
  chk('T2 citation does NOT set block class', !p2.classList.contains('ptoe-citation'), 'class='+p2.className);

  // ---- T3: pure p brush with partial selection (should NOT convert whole block?) ----
  ed.innerHTML='<h2>这是测试文本内容</h2>';
  PT.syncContent(ed);
  const h2=ed.querySelector('h2');
  const tn3=h2.firstChild;
  selectRange(ed, tn3, 2, tn3, 4);
  PT.brushArmed = { ops:['p'], selection: PT._captureSelection() };
  PT._brushApplyToBlock(ed, h2);
  const tag3 = ed.firstChild.tagName;
  chk('T3 p partial selection keeps H2 (no whole-block convert)', tag3==='H2', 'tag='+tag3+' html='+ed.innerHTML);

  // ---- T3b: p partial selection unwraps inline note span in the selection only ----
  ed.innerHTML='<p>前缀<span class="ptoe-note">注释词</span>后缀</p>';
  PT.syncContent(ed);
  const p3b=ed.querySelector('p');
  const noteSpan=p3b.querySelector('span.ptoe-note');
  const nTex=noteSpan.firstChild;
  selectRange(ed, nTex, 0, nTex, nTex.length); // select inside the note span
  PT.brushLiveSelection = null;
  PT.brushArmed = { ops:['p'], selection: PT._captureSelection() };
  PT._brushApplyToBlock(ed, p3b);
  chk('T3b p partial unwraps inline note span', !ed.querySelector('span.ptoe-note'), 'html='+ed.innerHTML);
  chk('T3b p partial keeps block tag <p> and text', ed.firstChild.tagName==='P' && ed.textContent.indexOf('注释词')>=0, 'html='+ed.innerHTML);

  // ---- T4: no selection -> whole paragraph ----
  ed.innerHTML='<p>这是测试文本内容</p>';
  PT.syncContent(ed);
  const p4=ed.querySelector('p');
  selectRange(ed, p4, 0, p4, 0); // collapsed
  PT.brushArmed = { ops:['note'], selection: null };
  PT._brushApplyToBlock(ed, p4);
  chk('T4 note no selection -> block class', p4.classList.contains('ptoe-note'), 'class='+p4.className+' html='+ed.innerHTML);

  // ---- T5: mixed (note+heading) with selection -> whole block ----
  ed.innerHTML='<p>这是测试文本内容</p>';
  PT.syncContent(ed);
  const p5=ed.querySelector('p');
  const tn5=p5.firstChild;
  selectRange(ed, tn5, 2, tn5, 4);
  PT.brushArmed = { ops:['note','heading'], selection: PT._captureSelection() };
  console.log('T5 isPureScope:', PT._brushIsPureScope(['note','heading']));
  PT._brushApplyToBlock(ed, p5);
  chk('T5 mixed -> whole block class', p5.classList.contains('ptoe-note'), 'class='+p5.className+' html='+ed.innerHTML);

  // ---- T6: merge delay is configurable ----
  const src = appJs;
  chk('T6 merge delay read from setting (brush_merge_delay)', src.indexOf('brush_merge_delay') >= 0, 'no brush_merge_delay in app.js');
  chk('T6 no hardcoded 150ms merge timer', src.indexOf('}, 150); // 150ms') < 0, 'old 150ms literal still present');
  PT.uiSettings = { brush_merge_delay: 30 };
  chk('T6 brushMergeDelay() honours setting', PT.brushMergeDelay() === 30, 'got='+PT.brushMergeDelay());
  PT.uiSettings = { brush_merge_delay: 999999 };
  chk('T6 brushMergeDelay() clamps to 5000', PT.brushMergeDelay() === 5000, 'got='+PT.brushMergeDelay());

  // ---- T7: arm first (no selection), then select word, then click -> scope applies ----
  ed.innerHTML='<p>这是测试文本内容</p>';
  PT.syncContent(ed);
  const p7=ed.querySelector('p');
  const tn7=p7.firstChild;
  PT.brushLiveSelection = null;
  PT.brushArmed = { ops:['note'], selection: null }; // armed with NO selection (arm-first flow)
  selectRange(ed, tn7, 2, tn7, 4); // user then selects "测试"
  doc.dispatchEvent(new w.Event('selectionchange')); // live tracking fires
  chk('T7 live selection tracked while armed', !!PT.brushLiveSelection, 'live=null');
  PT._brushApplyToBlock(ed, p7);
  chk('T7 arm-then-select note wraps only selection', !!ed.querySelector('span.ptoe-note'), 'html='+ed.innerHTML);
  chk('T7 arm-then-select does NOT set block class', !p7.classList.contains('ptoe-note'), 'class='+p7.className);
  const noteTxt = ed.querySelector('span.ptoe-note');
  chk('T7 wrapped text is only the selected word', noteTxt && noteTxt.textContent === '测试', 'text='+(noteTxt&&noteTxt.textContent));

  // ---- T7b: mixed brush ignores live selection -> whole block ----
  ed.innerHTML='<p>这是测试文本内容</p>';
  PT.syncContent(ed);
  const p7b=ed.querySelector('p');
  const tn7b=p7b.firstChild;
  PT.brushLiveSelection = null;
  PT.brushArmed = { ops:['note','heading'], selection: null };
  selectRange(ed, tn7b, 2, tn7b, 4);
  doc.dispatchEvent(new w.Event('selectionchange'));
  chk('T7b mixed brush does NOT track live selection', !PT.brushLiveSelection, 'live='+JSON.stringify(PT.brushLiveSelection));
  PT._brushApplyToBlock(ed, p7b);
  chk('T7b mixed -> whole block class', p7b.classList.contains('ptoe-note'), 'class='+p7b.className+' html='+ed.innerHTML);

  // ---- T8: merge刷 debounce (delay configurable; resets on each selectionchange) ----
  ed.innerHTML='<p>第一段文字</p><p>第二段文字</p>';
  PT.syncContent(ed);
  const pA=ed.children[0], pB=ed.children[1];
  const tnB=pB.firstChild;
  selectRange(ed, pA.firstChild, 0, tnB, tnB.length);
  PT.uiSettings = { brush_merge_delay: 60 };
  PT.brushArmed = { ops:['merge'], selection: null };
  PT.brushMergeTimer = null;
  doc.dispatchEvent(new w.Event('selectionchange')); // t=0 -> timer set to 60ms
  await sleep(20);
  chk('T8 no merge before delay', ed.children.length === 2, 'children='+ed.children.length);
  doc.dispatchEvent(new w.Event('selectionchange')); // t=20 -> debounce resets timer
  await sleep(20);
  chk('T8 debounce reset keeps blocks unmerged', ed.children.length === 2, 'children='+ed.children.length);
  await sleep(80); // past the reset delay -> single merge
  chk('T8 merge fires once after settle', ed.children.length === 1, 'children='+ed.children.length);
  chk('T8 merged text preserved', /第一段文字[\s\S]*第二段文字/.test(ed.textContent), 'text='+ed.textContent);
  PT.brushCancel();

  console.log('\n'+(fail?'FAILED '+fail:'ALL PASS')+'  (pass='+pass+' fail='+fail+')');
  process.exit(fail?1:0);
})().catch(e=>{console.error(e); process.exit(2);});
