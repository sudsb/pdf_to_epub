import re

with open('ui/app.js', 'r', encoding='utf-8') as f:
    js = f.read()

# Find all querySelectorAll calls
for m in re.finditer(r'querySelectorAll\(([^)]+)\)', js):
    print(m.group(1)[:120])
