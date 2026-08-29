"""Quick test of exclude feature with actual pdf_to_epub call."""
import tempfile
import os
from pathlib import Path
import fitz

# Create a simple 3-page PDF
tmpdir = Path(tempfile.mkdtemp())
pdf_path = tmpdir / "test.pdf"
doc = fitz.open()
for i in range(3):
    page = doc.new_page()
    page.insert_text((72, 72), f"Page {i+1} content")
doc.save(pdf_path)
doc.close()

# Now test exclude feature
from mian import pdf_to_epub, parse_exclude_spec

# Test 1: exclude page 1
print("Test 1: exclude page 1")
result = pdf_to_epub(
    pdf_path,
    dpi=100,
    model_key="HY",
    max_workers=1,
    thinking=False,
    timeout=60,
    exclude="1",  # Exclude page 1
)

# Check that page 1 is empty in the result
print(f"Result: {result}")

# Cleanup
import shutil
shutil.rmtree(tmpdir)