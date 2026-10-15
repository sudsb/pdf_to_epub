#!/usr/bin/env node
// 命令行字母简写校验（2026-10）：
//  1) _CMD_SHORT_ALIASES 内部两两不重复（源码层面无同名键覆盖）；
//  2) 简写不与既有别名冲突：_CMD_BUILTINS keys、_CMD_TOOL_ALIAS keys、OPS 英文名/中文标签；
//  3) 每个简写的值都指向三类表中的既有键（否则命令无法解析）；
//  4) 简写不破坏 nvim 语法：不得为 's'，也不得以「s+符号」开头（s/旧/新/ 替换特判）；
//  5) 解析链路可执行断言：pg/page/页 命中同一命令；范围+简写（7,10mg）正常；
//     工具/格式简写目标可达；s2 不误入 s/ 替换特判。
// 运行：node ptoe_cmdshort_test.cjs
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, 'ui', 'app.js'), 'utf8');

function cut(startMarker, endMarker) {
  const i = src.indexOf(startMarker);
  if (i < 0) throw new Error('未找到起始标记 ' + startMarker);
  const j = src.indexOf(endMarker, i);
  if (j < 0) throw new Error('未找到结束标记 ' + endMarker);
  return src.slice(i, j + endMarker.length);
}
function cutFn(name) { return cut('function ' + name + '(', '\n}'); }
function block(name) { return cut('const ' + name + ' = {', '};'); }
function parsePairs(text) {
  const out = []; const re = /['"]([^'"]+)['"]\s*:/g; let m;
  while ((m = re.exec(text)) !== null) out.push(m[1]);
  return out;
}

// ---- 1) 简写表自身唯一（含源码层面重复键检测） ----
const shortBlock = block('_CMD_SHORT_ALIASES');
const shorts = parsePairs(shortBlock);
const shortTargets = {};
const kv = /['"]([^'"]+)['"]\s*:\s*['"]([^'"]+)['"]/g; let m;
while ((m = kv.exec(shortBlock)) !== null) shortTargets[m[1]] = m[2];
const dupShort = shorts.filter((k, i) => shorts.indexOf(k) !== i);
assert.strictEqual(dupShort.length, 0, '简写重复定义: ' + JSON.stringify(dupShort));

// ---- 2) 既有别名全集，检查冲突 ----
const builtinsBlock = cut('const _CMD_BUILTINS = [', '\n];');
const builtinKeys = [];
const kre = /keys:\s*\[([^\]]*)\]/g;
while ((m = kre.exec(builtinsBlock)) !== null) {
  for (const k of m[1].split(',')) { const t = k.trim().replace(/^['"]|['"]$/g, ''); if (t) builtinKeys.push(t); }
}
const toolKeys = parsePairs(block('_CMD_TOOL_ALIAS'));
const opsBlock = cut('const OPS = [', '\n];');
const opsNames = [], opsLabels = [];
const ore = /\['([^']+)',\s*'([^']+)'\]/g;
while ((m = ore.exec(opsBlock)) !== null) { opsNames.push(m[1]); opsLabels.push(m[2]); }
const existing = new Set([...builtinKeys, ...toolKeys, ...opsNames, ...opsLabels]);
const clashes = shorts.filter(k => existing.has(k));
assert.strictEqual(clashes.length, 0, '简写与既有别名冲突: ' + JSON.stringify(clashes));

// ---- 3) 简写目标必须存在 ----
const allKeys = new Set([...builtinKeys, ...toolKeys, ...opsNames]);
const badTargets = Object.keys(shortTargets).filter(k => !allKeys.has(shortTargets[k]));
assert.strictEqual(badTargets.length, 0, '简写目标不存在: ' + JSON.stringify(badTargets));

// ---- 4) nvim s/ 语法安全 ----
assert.ok(!shorts.includes('s'), "简写 's' 与 s/旧/新/ 冲突");
const sBad = shorts.filter(k => /^s[^a-z0-9\u4e00-\u9fa5]/i.test(k));
assert.strictEqual(sBad.length, 0, '简写触发 s/ 特判: ' + JSON.stringify(sBad));

// ---- 5) 解析链路可执行断言（提取纯逻辑段，沙盒执行） ----
const code = [
  cut('const OPS = [', '\n];'),
  cut('const _CMD_FORMAT_OPS = new Set([', '\n]);'),
  cut('const _CMD_BUILTINS = [', '\n];'),
  block('_CMD_SHORT_ALIASES'),
  block('_CMD_TOOL_ALIAS'),
  cutFn('_cmdOpAliasMap'),
  cutFn('_cmdBuiltinByVerb'),
  cutFn('_parseLocator'),
  cutFn('_parseCmdLine'),
  'let _cmdAliasCache = null;',
  'return { OPS, _CMD_FORMAT_OPS, _CMD_BUILTINS, _CMD_SHORT_ALIASES, _CMD_TOOL_ALIAS,',
  '  _cmdOpAliasMap, _cmdBuiltinByVerb, _parseLocator, _parseCmdLine };',
].join('\n');
const api = new Function(code)();

// pg / page / 页 命中同一命令对象
const byPage = api._cmdBuiltinByVerb('页'), byEn = api._cmdBuiltinByVerb('page'), byShort = api._cmdBuiltinByVerb('pg');
assert.ok(byPage && byPage === byEn && byEn === byShort, 'pg/page/页 应命中同一命令');
assert.strictEqual(byPage.global, true, '页跳转应为 global 命令');

// 范围 + 简写：7,10mg → merge
const p1 = api._parseCmdLine('7,10mg');
assert.strictEqual(p1.verb, 'mg', '7,10mg 动词应为 mg');
assert.ok(api._cmdBuiltinByVerb('mg') === api._cmdBuiltinByVerb('merge'), 'mg 应命中 merge');
assert.strictEqual(p1.loc.line, 7, '范围解析 7');

// 简写小写化：PG 大写也能命中（exec 前 toLowerCase）
assert.ok(api._cmdBuiltinByVerb('PG'.toLowerCase()) === byPage, 'pg 大写小写化后命中');

// 工具/格式简写目标可达
assert.strictEqual(api._CMD_TOOL_ALIAS[api._CMD_SHORT_ALIASES['sv']], 'save', 'sv→save→save op');
assert.ok(api._CMD_FORMAT_OPS.has(api._CMD_SHORT_ALIASES['b']) && api._cmdOpAliasMap()[api._CMD_SHORT_ALIASES['b']] === 'bold', 'b→bold 格式可达');
assert.ok(api._CMD_FORMAT_OPS.has(api._CMD_SHORT_ALIASES['ac']), 'ac→align_center 格式可达');

// s2 不误入 s/ 替换特判；s/旧/新/ 行为不变
assert.strictEqual(api._parseCmdLine('s2 简体').verb, 's2', 's2 应为普通动词');
assert.strictEqual(api._parseCmdLine('s/旧/新/').verb, 's', 's/旧/新/ 特判不变');
assert.strictEqual(api._parseCmdLine('/关键词').kind, 'search', '/搜索 不变');

// 简写候选进 suggest（简写项指向目标 desc）
const suggestSrc = cut('function _cmdSuggestItems()', '\n}');
assert.ok(/_CMD_SHORT_ALIASES/.test(suggestSrc), '候选表应包含简写追加逻辑');

console.log('简写共 ' + shorts.length + ' 条：无重复、无冲突、目标全部存在、s/ 语法安全 ✓');
console.log('解析链路：pg/page/页 同命令、范围+简写、大小写、工具/格式简写、s2/s 特判 ✓');
