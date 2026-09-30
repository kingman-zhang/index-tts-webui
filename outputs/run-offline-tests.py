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

try:
    import pytest as _pytest_probe  # noqa: F401
    HAS_PYTEST = True
except ImportError:
    HAS_PYTEST = False


def _is_pytest_style(path):
    """pytest 风格文件（顶层 import pytest）。

    为什么必须区分：本入口用 runpy 以 __main__ 跑文件，而 pytest 的用例函数只是
    **被定义**、不会执行 ⇒ returncode=0 却是**假通过**（2026-09-30 发现：
    tts-server/test_podcast_text_rules.py 的 40 项在旧的跑法下一项都没跑）。
    pytest 风格必须真交给 pytest。
    """
    try:
        return 'import pytest' in path.read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return False


import tempfile
with tempfile.TemporaryDirectory(prefix='all-offline-') as temp:
    for index, path in enumerate(files):
        env = {k: v for k, v in os.environ.items() if k in ('PATH','HOME','TMPDIR','LANG','SYSTEMROOT')}
        env['DATA_DIR'] = str(pathlib.Path(temp)/str(index))
        relative = str(path.relative_to(root))
        if _is_pytest_style(path):
            if not HAS_PYTEST:
                # 不装作通过：明确标注 SKIP（pytest 是可选的测试依赖）
                print(relative, 'SKIP(no pytest)', flush=True)
                results.append((relative, 0, 'SKIP：环境里没有 pytest，本文件的用例未执行'))
                continue
            cmd = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', str(path)]
        else:
            cmd = [sys.executable, '-c', bootstrap, str(path)]
        result = subprocess.run(cmd, env=env,
                                cwd=str(path.parent), capture_output=True, text=True, timeout=180)
        print(relative, result.returncode, flush=True)
        results.append((relative, result.returncode, result.stdout + result.stderr))
(root/'outputs/offline-tests.log').write_text('\n\n'.join(f'=== {p} exit={code} ===\n{log}' for p,code,log in results))
print('TOTAL',len(results),'PASS',sum(code==0 for _,code,_ in results))
sys.exit(any(code for _,code,_ in results))
