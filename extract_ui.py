import re
with open('correctmanage.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find _UI_HTML
match = re.search(r'_UI_HTML\s*=\s*([\"\'])(.*?)\1', content, re.DOTALL)
if match:
    quote = match.group(1)
    html = match.group(2)
    # Handle escaped quotes
    html = html.replace('\\' + quote, quote)
    with open('ui_extracted.html', 'w', encoding='utf-8') as f:
        f.write(html)
    print('Extracted', len(html), 'chars')
else:
    print('Not found')