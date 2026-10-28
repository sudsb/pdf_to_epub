import re

with open('ui/app.js','r',encoding='utf-8') as f:
    js = f.read()

lines = js.split('\n')
for rid in ['formatRulesBtn','charWrapBtn','supSubBtn','imgModeSel','morePanelFormat']:
    for i, line in enumerate(lines):
        if f"getElementById('{rid}')" in line:
            start = max(0, i-5)
            end = min(len(lines), i+10)
            print(f'=== {rid} at line {i+1} ===')
            for j in range(start, end):
                marker = '>>> ' if j == i else '    '
                print(f'{marker}{j+1}: {lines[j][:200]}')
            print()
