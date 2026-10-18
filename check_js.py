import re
import sys

# Windows 控制台默认 GBK：嵌入 JS 里的 ✕(U+2715)/全角符号会让 print 直接抛
# UnicodeEncodeError，诊断脚本自己崩掉、掩盖检查结果。统一按 UTF-8 输出，
# 不可编码字符替换为 ?，保证检查总能跑完（Python 3.7+ 的 TextIOWrapper 才有 reconfigure）。
if callable(_reconf := getattr(sys.stdout, 'reconfigure', None)):
    _reconf(encoding='utf-8', errors='replace')

with open('correctmanage.py', 'r', encoding='utf-8') as f:
    content = f.read()

match = re.search(r'<script>(.*?)</script>', content, re.DOTALL)
if match:
    js = match.group(1)

    # Check for unclosed template literals
    lines = js.split('\n')
    in_template = False
    template_start = 0
    for i, line in enumerate(lines):
        for j, c in enumerate(line):
            if c == '`':
                if not in_template:
                    in_template = True
                    template_start = i+1
                else:
                    in_template = False
    if in_template:
        print(f'Unclosed template literal starting at line {template_start}')
    else:
        print('All template literals closed')

    # Check for unclosed braces
    opens = js.count('{')
    closes = js.count('}')
    print(f'Open braces: {opens}, Close braces: {closes}')

    # Check for unclosed parens
    opens_p = js.count('(')
    closes_p = js.count(')')
    print(f'Open parens: {opens_p}, Close parens: {closes_p}')

    # Show last 30 lines
    print('\nLast 30 lines of JS:')
    for i in range(max(0, len(lines)-30), len(lines)):
        print(f'{i+1}: {lines[i]}')
