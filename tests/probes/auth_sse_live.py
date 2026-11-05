"""F4 真实 SSE 烟测：uvicorn 8012 + 真 cookie + /api/stream/{sid}。
TestClient 测不出 BaseHTTPMiddleware 对流式的影响，必须真起服务。
"""
import json, os, subprocess, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
tmp = Path(tempfile.mkdtemp(prefix="sse_live_"))
env = {**os.environ, "RA_AUTH": "1",
       "RA_USERS_PATH": str(tmp / "users.json"),
       "RA_AUTH_SECRET": "sse-live-secret",
       "RA_DB_PATH": str(tmp / "r.db"),
       "RA_CKPT_DB": str(tmp / "ck.db"),
       "RA_INTENT": "0", "RA_CKPT_SWEEP": "0",
       "no_proxy": "*", "NO_PROXY": "*"}
log = tmp / "uv.log"

FAILED = []
def check(label, cond, detail=""):
    print(("✅" if cond else "❌"), label, (f"—— {detail}" if detail and not cond else ""))
    if not cond: FAILED.append(label)

def main():
    import httpx
    # 先建用户再起服务——_auth_preflight 在零用户时会拒启动
    r = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "research_agent.users",
         "add", "--id", "live", "--name", "Live"],
        env=env, cwd=ROOT, capture_output=True, text=True)
    tok = [l for l in r.stdout.splitlines() if "?t=" in l][0].split("t=")[-1]
    proc = subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), "-m", "uvicorn",
         "research_agent.server:app", "--host", "127.0.0.1", "--port", "8012"],
         env=env, cwd=ROOT, stdout=open(log, "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(40):
            try:
                httpx.get("http://127.0.0.1:8012/api/health",
                          trust_env=False, timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        c = httpx.Client(trust_env=False, timeout=10,
                         base_url="http://127.0.0.1:8012",
                         follow_redirects=False)
        rv = c.get(f"/api/auth/invite?t={tok}")
        check("invite 302", rv.status_code == 302)
        # 建会话（不 /run）
        sid = c.post("/api/sessions",
                     json={"question": "q", "mode": "fast",
                           "force_research": True}).json()["session_id"]
        # 无 cookie → 401
        c2 = httpx.Client(trust_env=False, timeout=5,
                          base_url="http://127.0.0.1:8012")
        check("无 cookie stream→401",
              c2.get(f"/api/stream/{sid}").status_code == 401)
        # 有 cookie → connected + keepalive
        got = []
        with c.stream("GET", f"/api/stream/{sid}", timeout=8) as resp:
            check("stream 200", resp.status_code == 200)
            for line in resp.iter_lines():
                if line:
                    got.append(line)
                if len(got) >= 2:
                    break
        check("connected 事件", any("connected" in g for g in got), str(got[:2]))
        check("keepalive/事件流", len(got) >= 2)
        # 断开后服务仍响应
        check("断开后 me 正常",
              c.get("/api/auth/me").status_code == 200)
        # 5 并发 stream 全断开后服务正常
        def open_close(i):
            try:
                with c.stream("GET", f"/api/stream/{sid}", timeout=5) as r2:
                    for _ in r2.iter_lines():
                        break
            except Exception:
                pass
        ths = [threading.Thread(target=open_close, args=(i,))
               for i in range(5)]
        [t.start() for t in ths]; [t.join(timeout=10) for t in ths]
        check("5 并发断开后 me 正常",
              c.get("/api/auth/me").status_code == 200)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    print("\n" + ("全部通过" if not FAILED else f"失败 {FAILED}"))
    return 0 if not FAILED else 1

if __name__ == "__main__":
    sys.exit(main())
