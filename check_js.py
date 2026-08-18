import re

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
