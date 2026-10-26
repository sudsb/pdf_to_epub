from mian import parse_exclude_spec, resolve_exclude_spec

# Test cases
assert parse_exclude_spec('1-15,17,20') == set(range(1, 16)) | {17, 20}, 'basic ranges'
assert parse_exclude_spec('1-15, 17, 20') == set(range(1, 16)) | {17, 20}, 'whitespace tolerance'
assert parse_exclude_spec('') == set(), 'empty string'
assert parse_exclude_spec(None) == set(), 'None'
assert parse_exclude_spec([]) == set(), 'empty list'
assert parse_exclude_spec([1, 2, 5]) == {1, 2, 5}, 'list of ints'
assert parse_exclude_spec(['1-3', '5']) == {1, 2, 3, 5}, 'list with range string'
assert parse_exclude_spec(('7', '9-10')) == {7, 9, 10}, 'tuple with mixed strings'
assert parse_exclude_spec([['2-4'], 8]) == {2, 3, 4, 8}, 'nested list'
assert parse_exclude_spec([0, -2, 3]) == {3}, 'ignore non-positive ints'
assert parse_exclude_spec('1-3,5,7-9') == {1,2,3,5,7,8,9}, 'mixed'
assert parse_exclude_spec('3-1') == {1,2,3}, 'reverse range'
assert parse_exclude_spec('1,invalid,3') == {1, 3}, 'invalid token skipped'
assert parse_exclude_spec('1-abc,5') == {5}, 'invalid range skipped'

# --exclude 留空（None / 空白 / 空列表）应回退到配置文件，非空值优先
assert resolve_exclude_spec(None, ['2-4']) == ['2-4'], 'None falls back to config'
assert resolve_exclude_spec('', ['2-4']) == ['2-4'], 'blank string falls back to config'
assert resolve_exclude_spec('   ', ['2-4']) == ['2-4'], 'whitespace falls back to config'
assert resolve_exclude_spec([], ['2-4']) == ['2-4'], 'empty list falls back to config'
assert resolve_exclude_spec('9', ['2-4']) == '9', 'explicit CLI wins over config'
assert resolve_exclude_spec(['9'], ['2-4']) == ['9'], 'explicit list wins over config'
print('All parse_exclude_spec tests passed!')