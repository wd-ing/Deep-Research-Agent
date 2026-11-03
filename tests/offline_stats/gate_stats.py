import sqlite3,glob,os,re,sys
sys.path.insert(0,"/Users/wd/Documents/Deep Research Agent")
from research_agent.search.quality import page_defect
from collections import Counter
PRE=["ra_v18_baseline","ra_v19_batch1","ra_v20_batch2","ra_v21_batch3","ra_v22_final","ra_v23_final","ra_v24_final","ra_v25_cbam","ra_v25_glp1","ra_v25_rustgo"]
POST=["ra_v26_rustgo","ra_v26_glp1_fast","ra_v27_ark_std","ra_v28_A","ra_v28_B","ra_v28_C","ra_v29_D","ra_v29_E","ra_v29_F1","ra_v29_F2","ra_v30_std","ra_v30_fast"]
JUNK=re.compile(r"招聘|求职|立即投递|简历投递|薪资|五险一金|律师事务所.{0,6}咨询|免费咨询|\{\{|\$\{|<%")
def run(names,label):
    S=SO=C=CO=0; gm=Counter(); defects=[]; junk_on=[]; samples=[]
    for n in names:
        c=sqlite3.connect(f"file:/tmp/auth/snap/{n}.db?mode=ro",uri=True); c.row_factory=sqlite3.Row
        for s in c.execute("select id,url,title,domain,on_topic from source"):
            S+=1; SO+= (s["on_topic"]==0)
            head=c.execute("select content from chunk where source_id=? order by rowid limit 1",(s["id"],)).fetchone()
            d=page_defect(s["url"],s["title"],head["content"] if head else "")
            if d: defects.append((n,s["domain"],d))
            if s["on_topic"]==1 and JUNK.search((s["title"] or "")):
                junk_on.append((n,s["domain"],(s["title"] or "")[:40]))
        for r in c.execute("select gate_method,on_topic,topic_reason,source_id from chunk"):
            C+=1; CO+=(r["on_topic"]==0); gm[(r["gate_method"],r["on_topic"])]+=1
            if r["on_topic"]==0 and len(samples)<6: samples.append((n,(r["topic_reason"] or "")[:40]))
        # off-topic chunk content junk leakage among on_topic chunks
        k=c.execute("select count(*) from chunk where on_topic=1 and (content like '%{{%' or content like '%招聘%' or content like '%立即投递%')").fetchone()[0]
        if k: junk_on.append((n,"chunk-level on_topic with junk keyword",k))
    print(f"\n## {label} dbs={len(names)}: sources {S} (off-topic {SO}), chunks {C} (off-topic {CO})")
    print(" gate_method×on_topic:",dict(gm))
    print(" page_defect would-hit on stored sources:",len(defects),defects[:6])
    print(" on-topic junk-keyword hits:",junk_on[:10], "total",len(junk_on))
    print(" off-topic reason samples:",samples[:3])
run(PRE,"pre-batch7 (v18-v25)"); run(POST,"post-batch7 (v26-v30)")
