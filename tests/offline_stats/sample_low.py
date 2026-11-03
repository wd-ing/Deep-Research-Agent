import sqlite3,re,sys,random
random.seed(7)
DB,DOM,N=sys.argv[1],sys.argv[2],int(sys.argv[3])
c=sqlite3.connect(f"file:/tmp/auth/snap/{DB}.db?mode=ro",uri=True); c.row_factory=sqlite3.Row
sid=c.execute("select id from session").fetchone()[0]
md=c.execute("select markdown from report").fetchone()[0]
cut=md.rfind("\n---\n\n### 来源\n"); md=md[:cut] if cut>=0 else md
srcs=[dict(r) for r in c.execute("select id,domain,title,url,source_nature,source_type,char_count,source_date from source where session_id=? order by rowid",(sid,))]
no={s["id"]:i for i,s in enumerate(srcs,1)}
chunks={}
for r in c.execute("select source_id,content,heading_path from chunk where session_id=? order by rowid",(sid,)):
    chunks.setdefault(r["source_id"],[]).append(r)
dom_nos={i for i,s in enumerate(srcs,1) if DOM in (s["domain"] or "")}
print(f"== {DB} {DOM}: sources", [(i,srcs[i-1]['title'][:36],srcs[i-1]['source_nature'],srcs[i-1]['char_count'],srcs[i-1]['source_date']) for i in sorted(dom_nos)])
sents=re.split(r"(?<=[。！？\n])",md)
hits=[]
for st in sents:
    for m in re.finditer(r"S(\d+)·(\d+)",st):
        n=int(m.group(1))
        if n in dom_nos: hits.append((st.strip(),n,int(m.group(2)))); break
print("sentences citing this domain:",len(hits),"of",len([s for s in sents if 'S' in s and '·' in s]))
for st,n,k in random.sample(hits,min(N,len(hits))):
    s=srcs[n-1]; ch=chunks.get(s["id"],[])
    body=ch[k-1]["content"] if 0<k<=len(ch) else "(chunk not found)"
    print("---");print("句:",st[:230]);print(f"源[S{n}]:",s["title"][:50],"|",s["url"][:80]);print("段原文:",re.sub(r"\s+"," ",body)[:230])
