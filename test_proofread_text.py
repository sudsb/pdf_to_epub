import unittest
import dictionarymanage
import correctmanage

class TestProofreadText(unittest.TestCase):
    def test_english_mixed(self):
        s = '这是英文ABC混排测试'
        errs = correctmanage.proofread_page(s)
        # should flag the ABC fragment
        self.assertTrue(any(e['wrong'] == 'ABC' for e in errs), f'errs={errs}')

    def test_allowed_repeated_word(self):
        s = '好好学习，天天向上'
        errs = correctmanage.proofread_page(s)
        # 好好 and 天天 are in DIE_WORDS; shouldn't be flagged
        self.assertFalse(any('好好' in e['wrong'] or '天天' in e['wrong'] for e in errs), f'errs={errs}')

    def test_repeated_character_flagged(self):
        s = '啊啊啊，这个句子有叠字'
        errs = correctmanage.proofread_page(s)
        # expect the repeated run '啊啊啊' flagged
        self.assertTrue(any(e['wrong'].startswith('啊') for e in errs), f'errs={errs}')

    def test_half_to_full_quote(self):
        # 半角转全角属「原有规则」，2026-08-09 起默认关闭 → 显式开启
        s = '他说 "这是引用".'
        errs = correctmanage.proofread_page(s, enable_legacy_rules=True)
        # expect double-quote positions flagged and candidate includes a full-quote char
        quotes_candidates = [c for e in errs for c in e.get('candidates', []) if '\u201c' in c or '\u201d' in c or '“' in c or '”' in c]
        self.assertTrue(len(quotes_candidates) >= 1, f'errs={errs}')

    def test_cached_candidates_basic(self):
        # basic smoke: cached_candidates_for_token returns a list (may be empty) and is callable twice
        c1 = dictionarymanage.cached_candidates_for_token('苹', '吃', '')
        c2 = dictionarymanage.cached_candidates_for_token('苹', '吃', '')
        self.assertIsInstance(c1, list)
        self.assertIsInstance(c2, list)

if __name__ == '__main__':
    unittest.main()
