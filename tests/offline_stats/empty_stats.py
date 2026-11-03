import sqlite3,glob,os
from collections import Counter,defaultdict
rows=defaultdict(Counter)   # (era, stage) -> counter
calls=Counter()
def era(model):
    m=(model or "").lower()
    if "minimax" in m: return "minimax"
    if "glm" in m or "doubao" in m or "deepseek-v4-flash-ga" in m: return "ark"
    return "other:"+m[:24]
dbs=0
for p in sorted(glob.glob("/tmp/auth/snap/ra_*.db")):
    c=sqlite3.connect(f"file:{p}?mode=ro",uri=True)
    try: cur=c.execute("select stage,model,status,error from llm_call")
    except Exception as e: print("skip",p,e); continue
    dbs+=1
    for st,mo,sts,err in cur:
        e=era(mo); calls[(e,st)]+=1
        if sts!='ok':
            kind=(err or "")[:40].split(":")[0].split("(")[0].strip() or sts
            rows[(e,st)][f"{sts}|{kind}"]+=1
print("dbs",dbs)
print("== write_section / write_revise / write_patch calls & non-ok by era ==")
for e in ("minimax","ark"):
    for st in ("write_section","write_revise","write_patch"):
        print(e,st,"calls",calls[(e,st)],"non-ok",dict(rows[(e,st)]))
print("== any status containing empty/Empty across all stages ==")
for k,v in rows.items():
    for kind,n in v.items():
        if "mpty" in kind: print(k,kind,n)
print("== all eras: non-ok counts by era ==")
for e in ("minimax","ark"):
    tot=sum(n for (ee,s),n in calls.items() if ee==e); bad=sum(sum(v.values()) for (ee,s),v in rows.items() if ee==e)
    print(e,"calls",tot,"non-ok",bad)
