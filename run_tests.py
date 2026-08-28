import subprocess
import sys

result = subprocess.run(
    [sys.executable, '-m', 'unittest', 'test_correctmanage'],
    capture_output=True, text=True, cwd='D:\code-project\python\PToEA'
)
print('STDOUT:', result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout)
print('STDERR:', result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr)
print('Return code:', result.returncode)