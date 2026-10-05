"""R3：实测 SearXNG 的 60 秒空闲自停是否真的工作。

设计上有个可疑之处：计时器是 `threading.Timer`，属于**当前进程**的守护线程。
脚本跑完进程一退出，计时器随之消失，而 SearXNG 还在跑。
如果是这样，「60 秒空闲自停」只在长驻服务里成立，对一次性脚本不成立。

本测试分三段：
  A. 进程内：一次 search 后什么都不做，等 65 秒，看是否自动关
  B. 进程退出后：主脚本立刻结束，隔 70 秒再看端口是否还被占
  C. 显式调用 shutdown_if_idle 是否正常
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SH = ROOT / ".searxng" / "searxng.sh"


def port_busy() -> str:
    out = subprocess.run(["lsof", "-nP", "-iTCP:8888", "-sTCP:LISTEN", "-t"],
                         capture_output=True, text=True)
    pids = [x for x in out.stdout.split() if x]
    if not pids:
        return ""
    ps = subprocess.run(["ps", "-o", "etime=", "-p", pids[0]],
                        capture_output=True, text=True)
    return f"占用中 PID {pids[0]}，已运行 {ps.stdout.strip()}"


def health() -> bool:
    import httpx
    try:
        r = httpx.get("http://127.0.0.1:8888/config", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


mode = sys.argv[1] if len(sys.argv) > 1 else "inproc"

if mode == "inproc":
    from research_agent.search.searxng import SearXNGProvider

    p = SearXNGProvider(script=SH)
    print(f"起始: {port_busy() or '端口空'}")
    print("发起一次检索…", flush=True)
    hits = p.search("大语言模型", language="zh-CN", limit=5)
    print(f"  返回 {len(hits)} 条，health={health()}")
    print(f"  计时器已启动: {p._idle_timer is not None}")
    print(f"  空闲阈值: {p.idle_shutdown_s} 秒")
    print("  静置 65 秒，其间不发任何请求…", flush=True)
    for i in range(13):
        time.sleep(5)
        print(f"    {5*(i+1):3d}s  health={health()}", flush=True)
    print(f"结束: {port_busy() or '端口空'}")

elif mode == "after-exit":
    print(f"脚本启动时: {port_busy() or '端口空'}")
    print("主脚本现在退出，不再有任何 Python 进程持有计时器。")
    sys.exit(0)

elif mode == "explicit":
    from research_agent.search.searxng import SearXNGProvider

    p = SearXNGProvider(script=SH)
    p.ensure_running()
    print(f"拉起后: {port_busy()}")
    t0 = time.time()
    p.shutdown_if_idle()
    print(f"显式关闭耗时 {time.time()-t0:.2f} 秒")
    print(f"关闭后: {port_busy() or '端口空'}  health={health()}")
