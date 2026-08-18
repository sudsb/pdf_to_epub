import re
with open('correctmanage.py', 'r', encoding='utf-8') as f:
    content = f.read()
match = re.search(r'<script>(.*?)</script>', content, re.DOTALL)
if not match:
    print('Script tags not found')
    exit(1)
js = match.group(1)
with open('ui_hist.js', 'w', encoding='utf-8') as f:
    f.write(js)
print('Extracted', len(js), 'bytes')