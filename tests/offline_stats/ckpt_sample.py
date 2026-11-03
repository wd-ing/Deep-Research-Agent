"""sample.py <ckpt_db> <out.csv> — every 5s: file size, #checkpoints, sum(blob), #writes, sum(writes) (read-only)."""
import sqlite3,sys,time,os
db,out=sys.argv[1],sys.argv[2]
f=open(out,"w"); f.write("t,file_bytes,wal_bytes,n_ckpt,ckpt_blob_bytes,n_writes,writes_bytes\n"); t0=time.time()
while True:
    try:
        c=sqlite3.connect(f"file:{db}?mode=ro",uri=True,timeout=5)
        a=c.execute("select count(*),ifnull(sum(length(checkpoint)),0) from checkpoints").fetchone()
        b=c.execute("select count(*),ifnull(sum(length(value)),0) from writes").fetchone()
        c.close()
        f.write(f"{time.time()-t0:.0f},{os.path.getsize(db)},{os.path.getsize(db+'-wal') if os.path.exists(db+'-wal') else 0},{a[0]},{a[1]},{b[0]},{b[1]}\n"); f.flush()
    except Exception as e:
        pass
    time.sleep(5)
