import unittest
import sys
import os

os.chdir('D:\code-project\python\PToEA')

# Run just one test to see output
suite = unittest.TestLoader().loadTestsNamePattern('test_correctmanage.TestImagesSidecar.test_save_stage_payload_has_no_images_and_sidecar_written')
print(f"Tests loaded: {suite.countTestCases()}")

# Use a different approach
from test_correctmanage import TestImagesSidecar
suite = unittest.TestLoader().loadTestsFromTestCase(TestImagesSidecar)
print(f"Test case has {suite.countTestCases()} tests")

# Run the specific test
runner = unittest.TextTestRunner(verbosity=2)
# Just run the first test method
test_methods = [m for m in dir(TestImagesSidecar) if m.startswith('test_')]
print(f"Test methods: {test_methods[:5]}")