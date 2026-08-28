import ast
import sys

with open('correctmanage.py', 'r', encoding='utf-8') as f:
    code = f.read()

try:
    ast.parse(code)
    print('correctmanage.py: syntax OK')
    sys.exit(0)
except SyntaxError as e:
    print(f'Syntax error: {e}')
    sys.exit(1)