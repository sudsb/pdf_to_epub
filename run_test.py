import sys
sys.path.insert(0, r"D:\code-project\python\PToEA")
import unittest
import test_correctmanage

# Run just the new test class
loader = unittest.TestLoader()
suite = loader.loadTestsFromTestCase(test_correctmanage.TestHistoryRenameEndpoint)
runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(suite)
print(f'\n\nFAILURES: {len(result.failures)}, ERRORS: {len(result.errors)}')
for test, trace in result.failures:
    print(f'FAIL: {test}')
for test, trace in result.errors:
    print(f'ERROR: {test}')