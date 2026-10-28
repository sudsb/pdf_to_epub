import re

with open('ui/app.js', 'r', encoding='utf-8') as f:
    js = f.read()

# Check for specific IDs that were removed from the old "更多" dropdown
removed_ids = [
    'formatRulesBtn', 'tableConvertBtn', 'tableToTextBtn',
    'charWrapBtn', 'supSubBtn', 'imgModeSel', 'morePanelFormat',
    'morePanelRight', 'finishMoreBtn', 'finishMorePanel',
    'brushPanel', 'brushMixPanel', 'brushOpsList', 'brushMixList',
    'charWrapMenu', 'supSubMenu', 'dividerMenu', 'proofreadMenu'
]

for rid in removed_ids:
    # Check getElementById
    g = re.findall(rf'getElementById\(\'{rid}\'\)', js)
    # Check querySelector
    q = re.findall(rf'querySelector.*{re.escape(rid)}', js)
    # Check addEventListener on the ID
    e = re.findall(rf'{re.escape(rid)}\.addEventListener', js)
    # Check .onclick
    o = re.findall(rf'{re.escape(rid)}\.onclick', js)
    
    refs = []
    if g: refs.append(f'getElementById({len(g)})')
    if q: refs.append(f'querySelector({len(q)})')
    if e: refs.append(f'addEventListener({len(e)})')
    if o: refs.append(f'onclick({len(o)})')
    
    if refs:
        print(f'REFERENCED: {rid} -> {", ".join(refs)}')
    else:
        print(f'NOT referenced: {rid}')
