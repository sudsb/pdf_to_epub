import re

# Read app.js and find all getElementById calls
with open('ui/app.js', 'r', encoding='utf-8') as f:
    js = f.read()

ids = set(re.findall(r'getElementById\(\'([^\']+)\'\)', js))
print('=== getElementById IDs ===')
for id in sorted(ids):
    print(id)

# Read correctmanage.py and check which IDs exist in toolbar
with open('correctmanage.py', 'r', encoding='utf-8') as f:
    html = f.read()

toolbar_start = html.find('<div id="toolbar">')
hintbar_start = html.find('<div id="hintbar">')
toolbar = html[toolbar_start:hintbar_start]

print()
print('=== Check in toolbar HTML ===')
for id in sorted(ids):
    found = f'id="{id}"' in toolbar or f"id='{id}'" in toolbar
    status = 'OK' if found else 'MISSING'
    print(f'{status}: {id}')
