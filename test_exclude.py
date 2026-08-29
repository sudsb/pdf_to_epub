from mian import parse_exclude_spec

# Test cases
assert parse_exclude_spec('1-15,17,20') == set(range(1, 16)) | {17, 20}, 'basic ranges'
assert parse_exclude_spec('1-15, 17, 20') == set(range(1, 16)) | {17, 20}, 'whitespace tolerance'
assert parse_exclude_spec('') == set(), 'empty string'
assert parse_exclude_spec(None) == set(), 'None'
assert parse_exclude_spec([]) == set(), 'empty list'
assert parse_exclude_spec([1, 2, 5]) == {1, 2, 5}, 'list of ints'
assert parse_exclude_spec('1-3,5,7-9') == {1,2,3,5,7,8,9}, 'mixed'
assert parse_exclude_spec('3-1') == {1,2,3}, 'reverse range'
assert parse_exclude_spec('1,invalid,3') == {1, 3}, 'invalid token skipped'
assert parse_exclude_spec('1-abc,5') == {5}, 'invalid range skipped'
print('All parse_exclude_spec tests passed!')