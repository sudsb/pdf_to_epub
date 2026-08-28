#!/usr/bin/env node
try {
  require('fs').readFileSync('ui/app.js', 'utf8');
  // Simple syntax check - just verify it parses
  new Function('"use strict";' + document.getElementById('test').innerHTML);
  console.log('JS syntax OK');
} catch(e) {
  console.log('JS error:', e.message);
  process.exit(1);
}