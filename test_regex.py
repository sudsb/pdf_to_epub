import re
pattern = r'([\s\S]*)([\(（]\s*.+\s*年\s*.*?[日月]\s*[\）)])([\s\S]*?)(毛\s*泽\s*东[\s\S]*刊\s*印[\s\S]*?)?(注[\s]*释)([\s\S]*)'
text = '前言内容（2024年5月）正文毛泽东同志题写刊印。注释：注释放置'
m = re.search(pattern, text)
if m:
    for i, g in enumerate(m.groups()):
        print(f'Group {i+1}: {repr(g)}')
    print('Full match:', repr(m.group(0)))
    print('Regs:', m.regs)