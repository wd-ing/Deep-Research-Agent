"""坑99 段2：路由鉴权+归属。离线 TestClient，RA_AUTH=1 + 临时库。"""
import json, os, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import _isolate  # noqa

FAILED = []
def check(label, cond, detail=""):
    print(("✅" if cond else "❌"), label, (f"—— {detail}" if detail and not cond else ""))
    if not cond: FAILED.append(label)

def main():
    tmp = Path(tempfile.mkdtemp(prefix="auth_r_"))
    os.environ["RA_AUTH"] = "1"
    os.environ["RA_USERS_PATH"] = str(tmp / "users.json")
    os.environ["RA_AUTH_SECRET"] = "route-test-secret"
    os.environ["RA_DB_PATH"] = str(tmp / "r.db")
    os.environ["RA_CKPT_DB"] = str(tmp / "ck.db")

    import research_agent.auth as A
    A.add_user("alice", "Alice", "tok-a-not-real",
               limits={"standard": 99, "fast": 99})
    A.add_user("bob", "Bob", "tok-b-not-real",
               limits={"standard": 99, "fast": 99})
    A.add_user("admin1", "Boss", "tok-c-not-real", admin=True)

    import importlib
    import research_agent.server as S
    importlib.reload(S)          # 让 MODELS_YAML/依赖在临时库下重建
    from fastapi.testclient import TestClient

    # MODELS_YAML 必须指临时文件——绝不碰真 config/models.yaml
    fake_models = tmp / "models.yaml"
    fake_models.write_text("stages: {}\n")
    S.MODELS_YAML = fake_models

    c = TestClient(S.app, follow_redirects=False)
    ck_a, ck_b, ck_ad = (A.sign_cookie("alice"), A.sign_cookie("bob"),
                         A.sign_cookie("admin1"))
    repo = S.get_repo()

    def go(method, path, cookie=None, **kw):
        hdr = {"Cookie": f"{A.COOKIE_NAME}={cookie}"} if cookie else {}
        return getattr(c, method)(path, headers=hdr, **kw)

    # ============ ① 路由枚举守门：无 cookie 一律 401 ============
    WL = {"/api/auth/invite", "/api/health"}
    for r in S.app.routes:
        path = getattr(r, "path", "")
        if not path.startswith("/api/") or path in WL:
            continue
        p2 = (path.replace("{sid}", "s_x").replace("{fid}", "f_x")
                  .replace("{src_id}", "src_x").replace("{ref}", "x"))
        for m in (getattr(r, "methods", None) or ()):
            if m in ("HEAD", "OPTIONS"):
                continue
            resp = go(m.lower(), p2)
            check(f"401 {m} {path}", resp.status_code == 401,
                  f"→{resp.status_code}")

    # ============ ② 跨用户矩阵 ============
    sidA = repo.create_session("A会话", "q", owner="alice")
    fidA = repo.create_folder("A夹", None, owner="alice")
    srcA = repo.add_source(session_id=sidA, url="https://a.example/x",
                           title="A来源", domain="a.example", content="body",
                           url_normalized="https://a.example/x")
    sid0 = repo.create_session("遗留", "q")          # owner NULL
    sidB = repo.create_session("B会话", "q", owner="bob")
    fidB = repo.create_folder("B夹", None, owner="bob")

    PARAM = {"sid": sidA, "fid": fidA, "src_id": srcA, "ref": "x"}
    for r in S.app.routes:
        path = getattr(r, "path", "")
        if not path.startswith("/api/"):
            continue
        import re as _re
        names = _re.findall(r"\{(\w+)\}", path)
        if not names:
            continue
        p2 = path
        for n in names:
            p2 = p2.replace("{" + n + "}", PARAM.get(n, "x"))
        for m in (getattr(r, "methods", None) or ()):
            if m in ("HEAD", "OPTIONS"):
                continue
            resp = go(m.lower(), p2, cookie=ck_b)
            check(f"B→A 404 {m} {path}", resp.status_code == 404,
                  f"→{resp.status_code}")

    # 列表/搜索/回收站/来源/文件夹不泄露
    r = go("get", "/api/sessions", cookie=ck_b).json()
    check("B 列表无 A", all(x["id"] != sidA for x in r))
    check("B 列表无 NULL", all(x["id"] != sid0 for x in r))
    r = go("get", "/api/sessions", cookie=ck_ad).json()
    check("admin 见 NULL", any(x["id"] == sid0 for x in r))
    r = go("get", "/api/sessions/search?q=A会话", cookie=ck_b).json()
    check("search 不泄露", all(x["id"] != sidA for x in r["hits"]))
    r = go("get", "/api/trash", cookie=ck_b).json()
    check("trash 不泄露", all(x["id"] != sidA for x in r))
    r = go("get", "/api/sources", cookie=ck_b).json()
    check("sources 不泄露", all(x.get("session_id") != sidA
                                for x in r.get("sources", r if isinstance(r, list) else [])))
    r = go("get", "/api/folders", cookie=ck_b).json()
    check("folders 不泄露", all(x["id"] != fidA for x in r if x.get("id")))
    # B 把会话挪进 A 的文件夹 → 拒
    r = go("patch", f"/api/sessions/{sidB}", cookie=ck_b,
           json={"folder_id": fidA})
    check("B会话挪A夹被拒", r.status_code in (404, 400), f"→{r.status_code}")
    # NULL 会话：member 404，admin 200
    check("member NULL→404", go("get", f"/api/sessions/{sid0}",
          cookie=ck_b).status_code == 404)
    check("admin NULL→200", go("get", f"/api/sessions/{sid0}",
          cookie=ck_ad).status_code == 200)
    # owner 落库
    r = go("post", "/api/sessions", cookie=ck_b,
           json={"question": "x", "mode": "fast", "force_research": True})
    if r.status_code == 200:
        check("POST 写 owner",
              repo.q1("SELECT owner FROM session WHERE id=?",
                      (r.json()["session_id"],))["owner"] == "bob")
    else:
        check("POST 写 owner", False, f"→{r.status_code} {r.text[:80]}")

    # ============ ③ cookie/令牌 ============
    bad = ck_a[:-1] + ("x" if ck_a[-1] != "x" else "y")
    check("篡改 cookie→401", go("get", "/api/sessions",
          cookie=bad).status_code == 401)
    import hashlib, hmac
    payload = f"alice|{int(time.time()-31*86400)}|z"
    sig = hmac.new(b"route-test-secret", payload.encode(),
                   hashlib.sha256).hexdigest()
    check("过期 cookie→401", go("get", "/api/sessions",
          cookie=f"{payload}|{sig}").status_code == 401)
    A.set_disabled("alice", True)
    check("禁用即时 401", go("get", "/api/sessions",
          cookie=A.sign_cookie("alice")).status_code == 401)
    A.set_disabled("alice", False)
    # 错误 token：10 次 401，第 11 次 429（IP 限速）
    codes = [go("get", "/api/auth/invite?t=bad", follow_redirects=False)
             .status_code for _ in range(11)]
    check("错token 10×401+第11次429",
          codes[:10] == [401]*10 and codes[10] == 429, str(codes))
    # cookie 标志位（先清 IP 限速桶——前面 11 次错 token 已用满配额）
    S._rl.clear()
    import secrets
    tok = secrets.token_urlsafe(32); A.rotate_token("alice", tok)
    r = c.get(f"/api/auth/invite?t={tok}", follow_redirects=False)
    sc = r.headers.get("set-cookie", "")
    check("invite 302", r.status_code == 302 and r.headers.get("location") == "/")
    check("cookie 标志", all(x in sc for x in
          ("HttpOnly", "SameSite=lax", "Max-Age=2592000", "Path=/")), sc[:120])
    check("默认无 Secure", "Secure" not in sc)
    # X-Forwarded-Proto 只信 127.0.0.1
    try:
        c2 = TestClient(S.app, follow_redirects=False,
                        client=("1.2.3.4", 123))
        S._rl.clear()
        r2 = c2.get(f"/api/auth/invite?t={tok}",
                    headers={"X-Forwarded-Proto": "https"})
        check("非127.0.0.1的XFP不信", "Secure" not in
              r2.headers.get("set-cookie", ""), r2.headers.get("set-cookie","")[:90])
        c3 = TestClient(S.app, follow_redirects=False,
                        client=("127.0.0.1", 123))
        S._rl.clear()
        r3 = c3.get(f"/api/auth/invite?t={tok}",
                    headers={"X-Forwarded-Proto": "https"})
        check("127.0.0.1+https→Secure", "Secure" in
              r3.headers.get("set-cookie", ""))
    except TypeError:
        check("client IP 注入", False, "TestClient 不支持 client 参数——未测")

    # ============ ⑥ fail-closed / RA_AUTH=0 ============
    os.environ["RA_USERS_PATH"] = str(tmp / "nonexistent.json")
    try:
        with TestClient(S.app):
            check("无用户库 startup 报错", False, "启动成功了")
    except Exception as e:
        check("无用户库 startup 报错", "users add" in str(e), str(e)[:100])
    os.environ["RA_USERS_PATH"] = str(tmp / "users.json")
    os.environ["RA_AUTH"] = "0"
    r = go("get", "/api/sessions")
    check("RA_AUTH=0 通行", r.status_code == 200)
    check("local admin 见 NULL", any(x["id"] == sid0 for x in r.json()))
    os.environ["RA_AUTH"] = "1"

    # ============ ⑦ config/search admin + intent 限速 ============
    r = go("put", "/api/config", cookie=ck_b, json={"content": "x"})
    check("member PUT config→403", r.status_code == 403)
    check("models.yaml 字节不变", fake_models.read_text() == "stages: {}\n")
    check("member GET config→403",
          go("get", "/api/config", cookie=ck_b).status_code == 403)
    check("member GET search→403",
          go("get", "/api/search?q=x", cookie=ck_b).status_code == 403)
    check("admin GET config→200",
          go("get", "/api/config", cookie=ck_ad).status_code == 200)
    # health 分级（c 的 cookie jar 已被 invite 污染——另开干净客户端）
    c0 = TestClient(S.app)
    check("health 未登录只 ok",
          c0.get("/api/health").json() == {"ok": True})
    check("health 登录给详情",
          "searxng" in go("get", "/api/health", cookie=ck_a).json())
    # intent 限速 20/min
    S._rl.clear()
    codes = [go("post", "/api/intent", cookie=ck_b,
                json={"question": "x"}).status_code for _ in range(21)]
    check("intent 20/min 限速", codes[-1] == 429
          and codes[-1] != 401, str(codes[-3:]))

    print("\n" + ("全部通过" if not FAILED else f"失败 {FAILED}"))
    return 0 if not FAILED else 1

if __name__ == "__main__":
    sys.exit(main())
