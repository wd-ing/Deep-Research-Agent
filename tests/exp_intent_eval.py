"""意图识别真实调用评测（坑 98）。3 轮 × CASES，并发≤4，轮间不共享缓存。
运行：.venv/bin/python tests/exp_intent_eval.py（联网、烧 mini 额度）
"""
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import _isolate  # noqa: F401
os.environ["RA_INTENT"] = "1"

FAILED = []


def main():
    set(_ for _ in [])  # noqa
    import intent_cases as TC
    from research_agent.intent import classify_intent
    from research_agent.llm.provider import LLMClient

    # 额度/连通性探测：一次极小调用，失败就停
    try:
        r = LLMClient().complete("probe_light", "只回复OK", record=False)
        print("探测:", repr(r[:10]))
    except Exception as e:
        print("探测失败:", type(e).__name__, e)
        return 1

    stamp = time.strftime("%H%M%S")
    out = Path(f"/tmp/intent_eval_{stamp}.json")

    # ---- 串行延迟轮（并发 1，口径=单线程延迟，含重试等待的墙钟） ----
    print("=== 串行延迟轮（并发 1）===")
    serial_lat, serial_src = [], Counter()
    for c in TC.CASES:
        t = time.time()
        r = classify_intent(c.q)
        serial_lat.append((time.time() - t) * 1000)
        serial_src[r.source] += 1
    sl = sorted(serial_lat)
    print(f"串行 n={len(sl)} mean={sum(sl)/len(sl):.0f}ms "
          f"p50={sl[len(sl)//2]:.0f} p95={sl[int(len(sl)*.95)]:.0f} "
          f"max={sl[-1]:.0f} src={dict(serial_src)}")

    # ---- 3 轮准确率（并发 2，轮间歇 5s） ----
    runs, meta = [], []
    for rnd in range(3):
        if rnd:
            time.sleep(5)
        t0 = time.time()
        preds, lat = {}, []
        def one(q):
            s = time.time()
            r = classify_intent(q)
            return q, r.intent, r.source, (time.time() - s) * 1000
        with ThreadPoolExecutor(max_workers=2) as ex:
            for q, pred, src, ms in ex.map(one, [c.q for c in TC.CASES]):
                preds[q] = pred
                lat.append(ms)
        runs.append(preds)
        meta.append({"round": rnd, "secs": round(time.time() - t0, 1),
                     "latency_ms": sorted(lat)})
        print(f"--- 第 {rnd+1} 轮 {time.time()-t0:.0f}s ---")

    rep, fails = TC.score(runs)
    all_src = [m for m in meta]
    print(json.dumps({k: v for k, v in rep.items() if k != "unstable_cases"},
                     ensure_ascii=False, indent=1))
    print("unstable:", len(rep["unstable_cases"]), rep["unstable_cases"][:8])
    print("failures:", len(fails), fails[:12])
    lat_all = sorted(x for m in meta for x in m["latency_ms"])
    n = len(lat_all)
    print(f"并发延迟 mean={sum(lat_all)/n:.0f}ms p95={lat_all[int(n*.95)]:.0f}ms")
    out.write_text(json.dumps({"report": rep, "failures": fails,
                               "runs": runs, "meta": meta,
                               "serial_latency_ms": serial_lat,
                               "serial_source": dict(serial_src)},
                              ensure_ascii=False), encoding="utf-8")
    print("落盘:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
