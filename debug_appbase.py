import tempfile
import sys
import shutil
from pathlib import Path
import pdfmanage

print('--- debug_appbase start ---')

tmp = Path(tempfile.mkdtemp(prefix='test_appbase_'))
exe = tmp / 'ptoe.exe'
exe.write_bytes(b'MZ')
orig_frozen = getattr(sys, 'frozen', None)
orig_executable = sys.executable
sys.frozen = True
sys.executable = str(exe)
try:
    print('tmp:', tmp)
    print('tmp.exists():', tmp.exists())
    print('tmp.resolve():', tmp.resolve())
    print('tmp.as_posix():', tmp.as_posix())
    print('sys.executable:', sys.executable)
    print('pdfmanage.app_base_dir():', pdfmanage.app_base_dir())
    d = pdfmanage.createdic('frozen_probe')
    print('createdic parent:', d.parent)
    print('createdic parent resolved:', d.parent.resolve())
    print('tmp / data:', tmp / 'data')
    print('tmp / data resolved:', (tmp / 'data').resolve())
    print('str(tmp):', str(tmp))
    print('str(tmp.resolve()):', str(tmp.resolve()))
    print('str(d.parent):', str(d.parent))
    print('os.path.normcase of str(tmp):', __import__('os').path.normcase(str(tmp)))
    print('os.path.normcase of str(d.parent):', __import__('os').path.normcase(str(d.parent)))
finally:
    # cleanup
    sys.executable = orig_executable
    if orig_frozen is None:
        del sys.frozen
    else:
        sys.frozen = orig_frozen
    shutil.rmtree(tmp, ignore_errors=True)

print('--- debug_appbase end ---')
