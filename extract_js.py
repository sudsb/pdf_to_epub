import re
with open('guimanage.py', 'r', encoding='utf-8') as f:
    content = f.read()
match = re.search(r'<script>(.*?)</script>', content, re.DOTALL)
if match:
    script = match.group(1)
    with open('/tmp/gui_ui.js', 'w', encoding='utf-8') as f:
        f.write(script)
    print('Script extracted, length:', len(script))
else:
    print('No script tag found')