
const fs = require('fs');
const { JSDOM } = require('jsdom');
const script = fs.readFileSync('correctmanage.py','utf8');
const m = script.match(/<script>([\s\S]*)<\/script>/);
if(!m){ console.error('no script found'); process.exit(2); }
const ui = m[1];

const dom = new JSDOM(`<!doctype html><html><body><div class="editable" id="page0" contenteditable="true"></div><div id="toast"></div>`, { runScripts: 'outside-only', resources: 'usable', url: 'http://127.0.0.1/' });
const window = dom.window;
const document = window.document;
// inject basic globals used by the script
window.fetch = async function(){ return { ok:true, json: async ()=> ({pages: [{text: '<div>foo 123 foo 456 foo</div>'}]}) }; };
window.navigator.sendBeacon = function(){ return true; };
window.requestAnimationFrame = function(cb){ return setTimeout(cb,0); };

// Evaluate UI script in the JSDOM context
window.eval(ui);

// Prepare a rule: regex matching "foo \d+" twice; match_formats: first italic, second align_center
const rule = {
  id: 'r1', name:'mtest', mode:'first', conditions:[{
    type:'regex', pattern:'foo\s(\d+)', scope:'selection', formats:[], match_formats:[['italic'], ['align_center']]
  }]
};

// set editable content
const ed = document.getElementById('page0');
ed.innerText = 'foo 123 foo 456 foo';

// set selection to cover whole editable
const range = document.createRange(); range.selectNodeContents(ed);
const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);

// call applyFormatRule (should be defined)
if(typeof window.applyFormatRule !== 'function') { console.error('applyFormatRule not found'); process.exit(3); }
const res = window.applyFormatRule(rule);

setTimeout(()=>{
  console.log('after apply innerHTML:', ed.innerHTML);
  process.exit(0);
}, 200);
