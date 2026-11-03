"""analyze.py <ckpt_db> <thread_id> — per-channel serialized bytes in the biggest checkpoints + writes by channel."""
import sqlite3,sys,json
from collections import Counter,defaultdict
sys.path.insert(0,"/Users/wd/Documents/Deep Research Agent")
from langgraph.checkpoint.sqlite import SqliteSaver
db,tid=sys.argv[1],sys.argv[2]
conn=sqlite3.connect(f"file:{db}?mode=ro",uri=True,check_same_thread=False)
saver=SqliteSaver(conn)
cfg={"configurable":{"thread_id":tid}}
tuples=list(saver.list(cfg))
print("checkpoints in thread:",len(tuples))
sizes=[]
for t in tuples:
    cv=t.checkpoint.get("channel_values",{})
    tot=0; per={}
    for k,v in cv.items():
        n=len(saver.serde.dumps_typed(v)[1]); per[k]=n; tot+=n
    sizes.append((tot,t.checkpoint["id"],t.metadata.get("step"),per))
sizes.sort(key=lambda x:-x[0])
print("sum of channel bytes across all checkpoints: %.1f MB"%(sum(s[0] for s in sizes)/1e6))
print("largest checkpoint: %.2f MB (step %s)"%(sizes[0][0]/1e6,sizes[0][2]))
print("top channels in largest checkpoint:")
for k,n in sorted(sizes[0][3].items(),key=lambda x:-x[1])[:12]: print(f"   {k:<28}{n/1e3:>10.1f} KB")
agg=Counter()
for tot,_,_,per in sizes:
    for k,n in per.items(): agg[k]+=n
print("channels summed over ALL checkpoints (what the file actually stores):")
for k,n in agg.most_common(12): print(f"   {k:<28}{n/1e6:>8.2f} MB")
print("writes table by channel:")
for ch,c,b in conn.execute("select channel,count(*),sum(length(value)) from writes where thread_id=? group by 1 order by 3 desc limit 10",(tid,)): print(f"   {ch:<28}{c:>5} rows {b/1e6:>8.2f} MB")
