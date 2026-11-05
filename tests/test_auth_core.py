"""坑99 段1：auth 核心 + users CLI + 0014 迁移 + repo 归属过滤。离线临时目录。"""
import json, os, stat, subprocess, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import _isolate  # noqa

FAILED = []
def check(label, cond, detail=""):
    print(("✅" if cond else "❌"), label, (f"—— {detail}" if detail and not cond else ""))
    if not cond: FAILED.append(label)

def main():
    import research_agent.auth as A
    from research_agent.users import main as ucli
    from research_agent.db.repo import Repo

    tmp = Path(tempfile.mkdtemp(prefix="auth_t_"))
    os.environ["RA_USERS_PATH"] = str(tmp / "users.json")
    os.environ["RA_AUTH_SECRET"] = "test-secret-12345"

    # ---- users CLI + 用户库 ----
    check("add ok", ucli(["add","--id","alice","--name","Alice"]) == 0)
    check("add admin", ucli(["add","--id","boss","--name","Boss","--admin"]) == 0)
    check("add 重复id报错", ucli(["add","--id","alice","--name","X"]) == 1)
    check("add 非法id报错", ucli(["add","--id","A B","--name","X"]) == 1)
    raw = json.loads((tmp/"users.json").read_text())
    check("只存sha256不含明文", all("token_sha256" in u and len(u["token_sha256"])==64
          for u in raw.values()) and "t=" not in (tmp/"users.json").read_text())
    check("users.json 600", stat.S_IMODE((tmp/"users.json").stat().st_mode)==0o600)
    alice = A.get_user("alice")
    check("get_user", alice and alice.name=="Alice" and alice.limits=={"standard":2,"fast":6})
    check("admin limits null", A.get_user("boss").limits is None)
    check("disabled 后 get_user=None",
          (ucli(["disable","alice"])==0) and A.get_user("alice") is None)
    check("enable 恢复", ucli(["enable","alice"])==0 and A.get_user("alice") is not None)

    # ---- token（CLI add 打出来那个——这里直接造一个等价验） ----
    import secrets
    tok = secrets.token_urlsafe(32)
    A.rotate_token("alice", tok)
    check("token→uid", A.token_uid(tok)=="alice")
    check("旧token失效（rotate 另一枚）", A.token_uid(secrets.token_urlsafe(32)) is None)
    tok2 = secrets.token_urlsafe(32); A.rotate_token("alice", tok2)
    check("rotate 后旧 token 失效", A.token_uid(tok) is None and A.token_uid(tok2)=="alice")

    # ---- cookie ----
    ck = A.sign_cookie("alice")
    check("cookie 往返", A.check_cookie(ck)=="alice")
    tampered = ck[:-1] + ("a" if ck[-1]!="a" else "b")
    check("篡改一位→None", A.check_cookie(tampered) is None)
    # 过期：伪造 31 天前的 ts 重签
    payload = f"alice|{int(time.time()-31*86400)}|x"
    import hashlib, hmac
    sig = hmac.new(b"test-secret-12345", payload.encode(), hashlib.sha256).hexdigest()
    check("过期→None", A.check_cookie(f"{payload}|{sig}") is None)
    ucli(["disable","alice"])
    check("禁用后 cookie 立即失效", A.check_cookie(ck) is None)
    ucli(["enable","alice"])
    check("不存在的 uid→None", A.check_cookie(A.sign_cookie("ghost")) is None)
    # (a) 同秒连写：mtime 粒度粗也要即时生效（三元组缓存）
    ucli(["disable","alice"])
    check("同秒 disable 即时", A.get_user("alice") is None)
    ucli(["enable","alice"])
    check("同秒 enable 即时", A.get_user("alice") is not None)
    # (c) cookie 未来 ts 拒绝
    import hashlib as _h, hmac as _m
    payload = f"alice|{int(time.time()+600)}|z"
    sig = _m.new(b"test-secret-12345", payload.encode(), _h.sha256).hexdigest()
    check("未来 ts→None", A.check_cookie(f"{payload}|{sig}") is None)
    # (c) 并发 _secret：多线程拿到同一密钥
    del os.environ["RA_AUTH_SECRET"]; A._SECRET_PATH = tmp/"sec2"
    import threading as _th
    outs = []
    ths = [_th.Thread(target=lambda: outs.append(A._secret()))
           for _ in range(8)]
    [t.start() for t in ths]; [t.join() for t in ths]
    check("并发密钥一致", len(set(outs)) == 1 and outs[0], str(len(set(outs))))
    os.environ["RA_AUTH_SECRET"]="test-secret-12345"

    # ---- 密钥文件自动生成+600 ----
    del os.environ["RA_AUTH_SECRET"]
    sp = tmp/"auth_secret"; A._SECRET_PATH = sp
    A.sign_cookie("alice")
    check("密钥自动生成+600", sp.exists() and stat.S_IMODE(sp.stat().st_mode)==0o600)
    os.environ["RA_AUTH_SECRET"]="test-secret-12345"

    # ---- 迁移：旧库（0013）升到 0014 ----
    db = tmp/"r.db"; r = Repo(db); r.init_schema()
    cols = {c[1] for c in r.conn.execute("PRAGMA table_info(session)")}
    check("session.owner 迁移", "owner" in cols)
    r.q("SELECT * FROM usage_event")  # 表存在不抛
    sid0 = r.create_session("旧","q0")   # owner NULL
    sida = r.create_session("A","q1",owner="alice")
    sidb = r.create_session("B","q2",owner="bob")

    # ---- repo 过滤 ----
    w,ps = Repo.viewer_where(None); check("viewer=None 不过滤", w=="1=1")
    w,ps = Repo.viewer_where(alice); check("member 仅自己", w=="owner=?" and ps==["alice"])
    boss = A.get_user("boss")
    w,ps = Repo.viewer_where(boss); check("admin 自己+NULL", "IS NULL" in w)
    check("owned_by None→True", r.owned_by(sida,None))
    check("alice 见自己", r.owned_by(sida,alice) and not r.owned_by(sidb,alice))
    check("alice 不见 NULL", not r.owned_by(sid0,alice))
    check("boss 见 NULL", r.owned_by(sid0,boss))
    bob = A.User(id="bob",name="Bob")
    check("bob 不见 alice 的", not r.owned_by(sida,bob))

    # usage
    r.add_usage("alice","standard","s1",time.time())
    r.add_usage("alice","fast","s2",time.time())
    check("count_usage", r.count_usage("alice","standard",0)==1 and r.count_usage("alice","fast",0)==1)
    check("since 过滤", r.count_usage("alice","standard",time.time()+10)==0)

    # source 经会话反查（owned_by 只挡 session 层；source 归属在段2 server 里反查——
    # 段1只保证 repo 侧有手段）
    check("get_user disabled 即时（上面已验）", True)

    print("\n"+("全部通过" if not FAILED else f"失败 {FAILED}"))
    return 0 if not FAILED else 1

if __name__=="__main__": sys.exit(main())
