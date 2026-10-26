"""HTTP 路径端到端基线驱动（2026-10-21，埋点轮）。

为什么必须有它：产品真跑的是 server.py（FastAPI + SSE + `get_agraph`
异步图），而 `tests/e2e_run.py` 走同步 `get_graph`——两条路的
checkpoint saver 不同，静默可能只在其中一条出现。基线必须走 HTTP。

用法：
    .venv/bin/python tests/e2e_http.py "研究问题" <db输出路径>

行为（模拟前端）：
  1. 子进程起 uvicorn（research_agent.server:app，端口 8011，
     RA_DB_PATH=<db输出路径>；.env 手动读进来——uvicorn 不读 .env）；
  2. POST /api/sessions 建会话 → GET /api/stream/{sid} 订 SSE；
  3. POST /api/sessions/{sid}/run?streaming=true 起跑；
  4. SSE 事件逐条带时间戳打印；审批点（轮询 /approval waiting=True）
     → POST /api/sessions/{sid}/approve（approved=true，不改大纲）；
  5. 等 done/error（超时 60 分钟），打印 timeline_report + cost_report；
  6. 停掉子进程。

⚠️ 端口只用 8011：8000/5180 是常驻服务，别碰。
⚠️ checkpoints.db 沿用默认 data/checkpoints.db（产品口径），
   只有 RA_DB_PATH（研究库）指向输出文件。
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8011"
TIMEOUT_S = 60 * 60

T0 = time.time()


def tick(msg: str) -> None:
    print(f"[{time.time() - T0:8.1f}s] {msg}", flush=True)


def _req(method: str, path: str, body: dict | None = None,
         timeout: float = 30) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _sse_loop(sid: str, stop: threading.Event, out_q: "queue.Queue") -> None:
    """订 SSE 并把每条事件丢给主线程。daemon 线程。"""
    req = urllib.request.Request(f"{BASE}/api/stream/{sid}")
    try:
        with urllib.request.urlopen(req, timeout=None) as r:
            for raw in r:
                if stop.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except Exception:
                    continue
                out_q.put(ev)
    except Exception as e:
        if not stop.is_set():
            out_q.put({"kind": "_sse_error", "error": f"{type(e).__name__}: {e}"})


def main() -> None:
    question = sys.argv[1]
    db = sys.argv[2] if len(sys.argv) > 2 else "/tmp/ra_e2e_http.db"
    Path(db).parent.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["RA_DB_PATH"] = db
    # uvicorn 不读 .env：手动注入（与 tests/e2e_run.py 同一个做法）
    envf = ROOT / ".env"
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "research_agent.server:app",
         "--host", "127.0.0.1", "--port", "8011"],
        cwd=str(ROOT), env=env)
    tick(f"uvicorn 子进程 pid={proc.pid}（8011，RA_DB_PATH={db}）")
    try:
        # 等服务起来
        # /api/health 会同步探 searxng(3s 超时)+tavily(网络)，慢网下 >2s，
        # 所以超时给 20s；循环耗尽必须报错，不能假装就绪。
        for i in range(30):
            try:
                urllib.request.urlopen(BASE + "/api/health", timeout=20)
                break
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError("uvicorn 启动失败（进程已退出）")
                time.sleep(1)
        else:
            raise RuntimeError("uvicorn 启动后 /api/health 始终无响应")
        tick("服务就绪")

        s = _req("POST", "/api/sessions", {"question": question})
        sid = s["session_id"]
        tick(f"session_id = {sid}")

        stop = threading.Event()
        evq: queue.Queue = queue.Queue()
        t = threading.Thread(target=_sse_loop, args=(sid, stop, evq),
                             daemon=True)
        t.start()

        _req("POST", f"/api/sessions/{sid}/run", timeout=10)
        tick("run 已发出")

        approved = False
        done = False
        deadline = time.time() + TIMEOUT_S
        last_approval_poll = 0.0
        while not done and time.time() < deadline:
            try:
                ev = evq.get(timeout=5)
            except queue.Empty:
                ev = None
            if ev is not None:
                tick(f"SSE {ev.get('kind')}: "
                     f"{json.dumps(ev, ensure_ascii=False)[:180]}")
                if ev.get("kind") == "done":
                    # ⚠️ interrupt（审批点）也会推 done——report_chars=0 的那条
                    # 不是终态，真 done 发生在 approve 续跑完之后。
                    if (ev.get("state") or {}).get("report_chars"):
                        done = True
                    else:
                        tick("interrupt 的 done（报告还没写），继续等审批点")
                elif ev.get("kind") in ("error", "_sse_error"):
                    tick(f"出错：{ev}")
                    done = True
            # 审批轮询：interrupt 时 SSE 不会再有动静，要主动去查
            if not approved and not done and time.time() - last_approval_poll > 3:
                last_approval_poll = time.time()
                try:
                    ap = _req("GET", f"/api/sessions/{sid}/approval", timeout=10)
                except Exception:
                    ap = {}
                if ap.get("waiting"):
                    tick("到审批点：approve（不改大纲、不带修订）")
                    _req("POST", f"/api/sessions/{sid}/approve",
                         {"approved": True}, timeout=10)
                    approved = True
        stop.set()
        if not done:
            tick("超时 60 分钟，放弃等待")
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        tick("uvicorn 已停")

    tick("==== timeline_report ====")
    subprocess.run([sys.executable, str(ROOT / "tests" / "timeline_report.py"),
                    db, sid], cwd=str(ROOT))
    tick("==== cost_report ====")
    cost = ROOT / "tests" / "cost_report.py"
    if cost.exists():
        subprocess.run([sys.executable, str(cost), db, sid], cwd=str(ROOT))
    else:
        print("tests/cost_report.py 不存在，跳过")


if __name__ == "__main__":
    main()
