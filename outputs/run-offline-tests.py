"""离线回归入口：独立进程、临时数据、屏蔽 dotenv 和外网 socket。"""
import os
import pathlib
import subprocess
import sys

root = pathlib.Path(__file__).resolve().parents[1]
bootstrap = '''import pathlib,runpy,sys,socket
from unittest.mock import patch
original=pathlib.Path.exists
connect=socket.socket.connect
def offline_connect(sock,address):
 if isinstance(address,tuple) and address[0]=="127.0.0.1" and address[1]==35325 and pathlib.Path(sys.argv[0]).name=="test_email_register.py":
  return connect(sock,address)
 raise RuntimeError("离线测试禁止外网连接")
socket.socket.connect=offline_connect
sys.argv=[sys.argv[1]]
with patch.object(pathlib.Path,"exists",lambda p: False if p.name==".env" else original(p)):
 runpy.run_path(sys.argv[0],run_name="__main__")
'''
files = sorted((root/'webui-backend/tests').glob('test_*.py'))
files += sorted(root.glob('tts-server*/test_*.py'))
results = []
import tempfile
with tempfile.TemporaryDirectory(prefix='all-offline-') as temp:
    for index, path in enumerate(files):
        env = {k: v for k, v in os.environ.items() if k in ('PATH','HOME','TMPDIR','LANG','SYSTEMROOT')}
        env['DATA_DIR'] = str(pathlib.Path(temp)/str(index))
        result = subprocess.run([sys.executable, '-c', bootstrap, str(path)], env=env,
                                cwd=str(path.parent), capture_output=True, text=True, timeout=180)
        relative = str(path.relative_to(root))
        print(relative, result.returncode, flush=True)
        results.append((relative, result.returncode, result.stdout + result.stderr))
(root/'outputs/offline-tests.log').write_text('\n\n'.join(f'=== {p} exit={code} ===\n{log}' for p,code,log in results))
print('TOTAL',len(results),'PASS',sum(code==0 for _,code,_ in results))
sys.exit(any(code for _,code,_ in results))
