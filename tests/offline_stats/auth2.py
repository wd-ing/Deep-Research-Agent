import glob,os,sqlite3
from collections import Counter,defaultdict
from auth_stats import analyze, tier
MM={"ra_v18_baseline","ra_v19_batch1","ra_v20_batch2","ra_v21_batch3","ra_v22_final","ra_v23_final","ra_v24_final","ra_v25_cbam","ra_v25_glp1","ra_v25_rustgo","ra_v26_rustgo"}
ARK={"ra_v28_B","ra_v28_C","ra_v29_D","ra_v29_E","ra_v30_std"}
FAST={"ra_fast_cbam","ra_fast_glp1","ra_fast_rustgo","ra_fast_smoke","ra_fast_solid","ra_v26_glp1_fast","ra_v29_F2","ra_v30_fast"}
def nat_table(names,label):
    ns=defaultdict(lambda:[0,0]); lowdom=Counter(); tot=0; ntot=0
    per=[]
    for n in sorted(names):
        a=analyze(f"/tmp/auth/snap/{n}.db")
        c=sum(v for k,v in a["cites"].items() if k in a["by_no"]); tot+=c
        d=Counter()
        for s in a["srcs"]:
            k=s["source_nature"] or "unjudged"; ns[k][0]+=1; ns[k][1]+=a["cites"].get(s["no"],0)
            if s["tier"]=="low": d[s["domain"]]+=a["cites"].get(s["no"],0)
        lowdom.update(d)
        if d:
            dom,cn=d.most_common(1)[0]; per.append((n,dom,cn,round(cn/c,3)))
    S=sum(v[0] for v in ns.values())
    print(f"\n## {label} (n={len(names)})")
    print("nature: "+", ".join(f"{k} src {v[0]/S:.0%}/cite {v[1]/tot:.0%}" for k,v in sorted(ns.items(),key=lambda x:-x[1][1])))
    print("low-tier top domains by cites:",[(d,n,f"{n/tot:.1%}") for d,n in lowdom.most_common(6)])
    print("per-report max single low domain share:",[(p[0][3:],p[1],p[3]) for p in per if p[3]>=0.08])
nat_table(MM,"MiniMax std"); nat_table(ARK,"Ark std"); nat_table(FAST,"fast")
