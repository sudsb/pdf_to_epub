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
window.__PT={_plainNoAnno,syncContent,renderProofread,
 get errKey(){return errKey;},set errKey(v){errKey=v;},get host(){return host;},get contentMap(){return contentMap;},get proofreadErrors(){return proofreadErrors;},set proofreadErrors(v){proofreadErrors=v;}};`);
const PT=w.__PT;const sleep=(ms)=>new Promise(r=>setTimeout(r,ms));
let pass=0,fail=0;
function chk(name,cond,extra){if(cond){pass++;console.log('[OK] '+name);}else{fail++;console.log('[FAIL] '+name+(extra?'  '+extra:''));}}
(async function main(){
 await sleep(120);
 const row=PT.host.children[0],ri=Number(row.dataset.i),ed=row.querySelector('.editable');
 function accept(k){PT.errKey={i:ri,k};doc.getElementById('errOk').click();}
 function spanText(k){const a=ed.querySelectorAll('.ptoe-err[data-err-i="'+k+'"]');let s='';a.forEach(e=>s+=e.textContent);return s;}
 function sliceErr(e){return PT._plainNoAnno(ed).slice(e.start,e.end);}

 // --- T1 (C1): block-boundary error -> apply must not shift subsequent marker ---
 ed.innerHTML='<p>\nA\nB</p><p>\nBBB</p>';PT.syncContent(ed);
 const t=PT._plainNoAnno(ed); console.log('T1 plain='+JSON.stringify(t));
 const iA=t.indexOf('A\nB'); // 1
 const iB=t.indexOf('BBB');   // 5
 PT.proofreadErrors[ri]=[
   {start:iA,end:iA+3,wrong:'AB',candidates:['X'],line:1},   // drift: end-start(3) != wrong.len(2)
   {start:iB,end:iB+3,wrong:'BBB',line:1},
 ];
 PT.renderProofread(ri);
 chk('T1 initial err0 span text == "A\\nB"', spanText(0)==='A\nB', JSON.stringify(spanText(0)));
 accept(0);
 const after=PT._plainNoAnno(ed);
 const e1=PT.proofreadErrors[ri][0];
 chk('T1 subsequent marker rebased correctly', after.slice(e1.start,e1.end)==='BBB',
     'after='+JSON.stringify(after)+' err1=['+e1.start+','+e1.end+') slice='+JSON.stringify(after.slice(e1.start,e1.end)));
 chk('T1 rendered span text == "BBB"', spanText(0)==='BBB', JSON.stringify(spanText(0)));

 // --- T2 (C2): unsorted errors array must render both fully ---
 ed.innerHTML='<p>\nAAA</p><p>\nBBB</p><p>\nCCC</p>';PT.syncContent(ed);
 const t2=PT._plainNoAnno(ed);
 const a=t2.indexOf('AAA'), b=t2.indexOf('BBB'), c=t2.indexOf('CCC');
 PT.proofreadErrors[ri]=[
   {start:c,end:c+3,wrong:'CCC',line:1},   // out of order
   {start:a,end:a+3,wrong:'AAA',line:1},
   {start:b,end:b+3,wrong:'BBB',line:1},
 ];
 PT.renderProofread(ri);
 const sorted=PT.proofreadErrors[ri].map(e=>e.start);
 chk('T2 array sorted ascending', sorted[0]<=sorted[1]&&sorted[1]<=sorted[2], JSON.stringify(sorted));
 // indices now correspond to sorted order: 0=AAA,1=BBB,2=CCC
 chk('T2 err0 (AAA) fully rendered', spanText(0)==='AAA', JSON.stringify(spanText(0)));
 chk('T2 err1 (BBB) fully rendered', spanText(1)==='BBB', JSON.stringify(spanText(1)));
 chk('T2 err2 (CCC) fully rendered', spanText(2)==='CCC', JSON.stringify(spanText(2)));
 // apply leftmost among sorted array (index0=AAA)
 accept(0);
 const eC=PT.proofreadErrors[ri].find(e=>e.wrong==='CCC');
 chk('T2 after apply(AAA), CCC marker still correct', sliceErr(eC)==='CCC', JSON.stringify(sliceErr(eC)));

 console.log('\n'+(fail?'FAILED '+fail:'全部通过')+'  (pass='+pass+' fail='+fail+')');
 process.exit(fail?1:0);
})();
