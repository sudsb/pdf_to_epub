import ast
with open('correctmanage.py', 'r', encoding='utf-8') as f:
    code = f.read()
try:
    ast.parse(code)
    print('correctmanage.py: syntax OK')
except SyntaxError as e:
    print(f'Syntax error: {e}')