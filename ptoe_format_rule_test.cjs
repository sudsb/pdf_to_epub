'use strict';
// 格式规则编辑器回归测试（jsdom）
//
// 覆盖：
//   1) 一条正则条件可添加多个「匹配N」独立格式行；
//   2) 给「匹配N」设置格式不会覆盖其它行（此前"设置格式只保留最后一个匹配"的隐患）；
//   3) 保存时 match_formats 完整提交给 /api/format_rules（服务端叠加应用，见
//      rulemanage.eval_format_rule：条件级 formats 作用于全部匹配，匹配/分组格式在其上追加）。
//
// 运行：node ptoe_format_rule_test.cjs

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const appJs = fs.readFileSync(path.join(__dirname, 'ui/app.js'), 'utf8');
const uiHtml = fs.readFileSync(path.join(__dirname, 'ui_full.html'), 'utf8');

const posts = [];
const previewPosts = [];
let previewReply = {
  ok: true,
  count: 2,
  shown: 2,
  matches: [
    { index: 1, text: '李克农〔1〕同志，并告金、彭〔2〕', groups: ['〔2〕'], start: 1, end: 18 },
    { index: 2, text: '一月二十日二十四时来电〔3〕及两组〔4〕', groups: ['〔4〕'], start: 20, end: 40 },
  ],
};
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
      if (opts && opts.method === 'POST' && String(url).startsWith('/api/format_rules/preview')) {
        previewPosts.push(JSON.parse(opts.body));
        return jsonRes(previewReply);
      }
      if (opts && opts.method === 'POST' && String(url).startsWith('/api/format_rules')) {
        posts.push(JSON.parse(opts.body));
        return jsonRes({ ok: true, rules: JSON.parse(opts.body).rules });
      }
      if (String(url).startsWith('/api/pages')) {
        return jsonRes({ pages: [{ page: 1, text: '<p>〔1〕甲〔2〕乙〔3〕丙〔4〕丁</p>', w: 800, h: 1200 }] });
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
    window.document.execCommand = () => false;
  },
});

const w = dom.window;
const doc = w.document;
w.eval(appJs + `
window.__FR = {
  renderConditions: renderConditions,
  getConds: function () { return _frConds; },
  saveFormatRule: saveFormatRule
};`);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failed = 0;
function check(name, cond, extra) {
  console.log((cond ? '  [OK ] ' : '  [FAIL] ') + name + (cond ? '' : '  -> ' + extra));
  if (!cond) failed++;
}

const COND = () => ({
  type: 'regex', pattern: '〔\\d+〕', scope: 'page', target: 'match',
  formats: [], group_formats: [], match_formats: [],
});

(async function main() {
  await sleep(60);
  const FR = w.__FR;
  const rows = () => doc.querySelectorAll('#frConditions .fr-group-row');
  const opt = (v) => Array.from(doc.querySelectorAll('#frFmtOpts input[type=checkbox]')).find((cb) => cb.value === v);

  console.log('[1] 建立 4 个匹配行');
  FR.renderConditions([COND()]);
  for (let i = 0; i < 4; i++) doc.querySelector('#frConditions .frMatchAdd').click();
  check('match_formats 有 4 行', FR.getConds()[0].match_formats.length === 4, JSON.stringify(FR.getConds()[0].match_formats));

  console.log('[2] 设置「匹配1」= 加粗');
  rows()[0].querySelector('.frFmtBtn').click();
  check('格式弹窗已打开', doc.getElementById('frFmtPopupBg').style.display === 'flex');
  opt('bold').checked = true;
  doc.getElementById('frFmtOkBtn').click();
  let mf = FR.getConds()[0].match_formats;
  check('匹配1 = [bold]', JSON.stringify(mf[0]) === '["bold"]', JSON.stringify(mf));
  check('其余行为空', mf.slice(1).every((m) => !m.length), JSON.stringify(mf));

  console.log('[3] 再设置「匹配4」= 下划线（不得覆盖匹配1）');
  rows()[3].querySelector('.frFmtBtn').click();
  opt('underline').checked = true;
  doc.getElementById('frFmtOkBtn').click();
  mf = FR.getConds()[0].match_formats;
  check('匹配4 = [underline]', JSON.stringify(mf[3]) === '["underline"]', JSON.stringify(mf));
  check('匹配1 未被覆盖', JSON.stringify(mf[0]) === '["bold"]', JSON.stringify(mf));
  check('行数仍为 4', mf.length === 4, JSON.stringify(mf));

  console.log('[4] 保存后提交给服务端的 match_formats 完整');
  doc.getElementById('frName').value = '测试规则';
  doc.getElementById('frMode').value = 'all';
  await FR.saveFormatRule();
  await sleep(20);
  const body = posts[posts.length - 1];
  const saved = body && body.rules && body.rules[body.rules.length - 1];
  check('已提交规则', !!saved, JSON.stringify(posts));
  if (saved) {
    check(
      'match_formats = [["bold"],[],[],["underline"]]',
      JSON.stringify(saved.conditions[0].match_formats) === '[["bold"],[],[],["underline"]]',
      JSON.stringify(saved.conditions[0]),
    );
  }

  console.log('[5] 反序操作（先匹配4 再匹配1）同样不丢');
  FR.renderConditions([COND()]);
  for (let i = 0; i < 4; i++) doc.querySelector('#frConditions .frMatchAdd').click();
  rows()[3].querySelector('.frFmtBtn').click();
  opt('underline').checked = true;
  doc.getElementById('frFmtOkBtn').click();
  rows()[0].querySelector('.frFmtBtn').click();
  opt('bold').checked = true;
  doc.getElementById('frFmtOkBtn').click();
  mf = FR.getConds()[0].match_formats;
  check('两行都在', JSON.stringify(mf) === '[["bold"],[],[],["underline"]]', JSON.stringify(mf));

  console.log('[6] 匹配预览：贪婪前缀只匹配 2 处，界面摊开真实匹配数与分组');
  FR.renderConditions([{
    type: 'regex', pattern: '[\\t\\S]+(〔\\d+〕)', scope: 'page', target: 'match',
    formats: [], group_formats: [['sup']], match_formats: [],
  }]);
  const prevBtn = doc.querySelector('#frConditions .frPreviewBtn');
  check('存在「预览」按钮', !!prevBtn);
  prevBtn.click();
  await sleep(40);
  const panel = doc.querySelector('.fr-preview');
  check('已渲染预览面板', !!panel);
  const ptxt = panel ? panel.textContent : '';
  check('显示真实匹配数「2 处」', ptxt.indexOf('本页匹配到 2 处') >= 0, ptxt);
  check('列出匹配1 文本', ptxt.indexOf('李克农〔1〕同志，并告金、彭〔2〕') >= 0, ptxt);
  check('列出「组1 = 〔2〕」并标明已设格式', ptxt.indexOf('组1 = 〔2〕') >= 0 && ptxt.indexOf('上标') >= 0, ptxt);
  check('给出贪婪量词提示', ptxt.indexOf('贪婪量词') >= 0, ptxt);
  check(
    '预览请求带上 html/pattern',
    previewPosts.length === 1 && previewPosts[0].pattern === '[\\t\\S]+(〔\\d+〕)'
      && typeof previewPosts[0].html === 'string',
    JSON.stringify(previewPosts),
  );

  console.log('[7] 无匹配时给出提示');
  previewReply = { ok: true, count: 0, shown: 0, matches: [] };
  FR.renderConditions([{
    type: 'regex', pattern: '〔\\d+〕', scope: 'page', target: 'match',
    formats: [], group_formats: [], match_formats: [],
  }]);
  doc.querySelector('#frConditions .frPreviewBtn').click();
  await sleep(40);
  const panel2 = doc.querySelector('.fr-preview');
  check('提示未匹配到内容', !!panel2 && panel2.textContent.indexOf('没有匹配到内容') >= 0,
    panel2 ? panel2.textContent : '(无面板)');

  await sleep(20);
  console.log('');
  console.log(failed ? '失败 ' + failed + ' 项' : '全部通过');
  process.exit(failed ? 1 : 0);
})();
