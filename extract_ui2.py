import re
with open('correctmanage.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find _UI_HTML = r"""..."""
match = re.search(r'_UI_HTML\s*=\s*r?"""([\s\S]*?)"""', content)
if match:
    html = match.group(1)
    with open('ui_full.html', 'w', encoding='utf-8') as f:
        f.write(html)
    print('Extracted', len(html), 'chars to ui_full.html')
else:
    print('Not found')
    # Try alternative pattern
    match2 = re.search(r'_UI_HTML\s*=\s*["\']{3}([\s\S]*?)["\']{3}', content)
    if match2:
        print('Found with alt pattern:', len(match2.group(1)))