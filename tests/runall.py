"""全量回归：逐个跑 tests/test_*.py，各自 150 秒超时，打印退出码。

用法（仓库根目录）：.venv/bin/python tests/runall.py
测试是脚本式（main()+check()），pytest 收集不到；macOS 没有 `timeout` 命令，所以这里用 subprocess 超时。
库与 checkpoint 指到临时目录，不碰 data/。日志落在同一个临时目录，结束时打印位置。
"""
import glob
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tmp = tempfile.mkdtemp(prefix="ra_runall_")
env = dict(os.environ, RA_DB_PATH=os.path.join(tmp, "base.db"),
           RA_CKPT_DB=os.path.join(tmp, "ck.db"))
bad = []
for f in sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py"))):
    n = os.path.basename(f)[:-3]
    try:
        r = subprocess.run([sys.executable, f], capture_output=True, text=True,
                           timeout=150, env=env, cwd=ROOT)
        rc, out = r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        rc, out = "TIMEOUT", str(e.stdout)
    with open(os.path.join(tmp, f"{n}.log"), "w") as fh:
        fh.write(out)
    print(n, rc, flush=True)
    if rc != 0:
        bad.append(n)
print(f"\n{'全部通过' if not bad else '失败: ' + ', '.join(bad)}（日志目录 {tmp}）")
sys.exit(1 if bad else 0)
