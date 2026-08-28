import unittest
import sys

# Run specific test classes mentioned in the failures
loader = unittest.TestLoader()
all_tests = []

# Add the specific test classes
test_classes = [
    'test_correctmanage.TestImagesSidecar',
    'test_correctmanage.TestHistoryImportExport',
    'test_correctmanage.TestHistoryLoadEndpoint',
    'test_correctmanage.TestProofreadHistory',
]

for tc in test_classes:
    try:
        cls = getattr(unittest, tc.split('.')[-1]) if '.' in tc else __import__(tc.rsplit('.', 1)[1]).__dict__[tc.rsplit('.', 1)[1]]
        # Actually let's just use the standard approach
        suite = loader.loadTestsFromName(tc)
        all_tests.extend(suite)
    except Exception as e:
        print(f"Could not load {tc}: {e}")

# Also load all tests from test_correctmanage
try:
    all_tests = loader.loadTestsFromName('test_correctmanage')
except Exception as e:
    print(f"Could not load all tests: {e}")

runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(unittest.TestSuite(all_tests))
sys.exit(0 if result.wasSuccessful() else 1)