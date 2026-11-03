"""Ark（火山方舟）真实预检：迁移后 17 环节逐项实测（联网，烧真 token）。

用法：set -a && source .env && set +a && RA_DB_PATH=/tmp/ark_preflight.db \
      .venv/bin/python tests/preflight_ark.py
密钥从 ARK_API_KEY 读，不打印。预算 ≤15 万 token（实测远低于）。
"""
from __future__ import annotations
import os, sys, tempfile, time, threading, statistics
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RA_DB_PATH",
                      str(Path(tempfile.mkdtemp()) / "ark_preflight.db"))

from research_agent.llm.provider import LLMClient, ModelRegistry, session_scope  # noqa
from research_agent.db.repo import get_repo  # noqa

GLM = ["probe_outline","outline","assess","write_plan_spec","write_plan_tasks","write_audit"]
MINI = ["probe_light","probe_degraded","extract","relevance","dedupe","summarize",
        "session_summary","fast_answer","write_section","write_revise","write_patch"]
SID = "pf_ark"


def calls_for(tag=""):
    repo = get_repo()
    q = ("SELECT stage, model, input_tokens, output_tokens, status, "
         "cache_read_tokens, cache_creation_tokens, latency_ms "
         "FROM llm_call WHERE session_id=? ORDER BY created_at")
    return [dict(r) for r in repo.q(q, (SID,))]


def main() -> int:
    if not os.environ.get("ARK_API_KEY"):
        print("❌ 未设置 ARK_API_KEY"); return 1
    reg = ModelRegistry()
    client = LLMClient(registry=reg)
    # 所有调用挂同一个临时会话，记账集中可查；
    # log_llm_call 的 INSERT 依赖 session 行存在——先建出来。
    global SID
    SID = get_repo().create_session("pf_ark", "q")
    with session_scope(SID):
        print("== (a) 每环节 complete('回复ok') ==")
        for st in GLM + MINI:
            t0 = time.time()
            try:
                out = client.complete(st, "回复ok")
                ms = int((time.time()-t0)*1000)
                print(f"  ✅ {st:18s} {ms:6d}ms  out={out[:30]!r}")
            except Exception as e:
                print(f"  ❌ {st:18s} {type(e).__name__}: {str(e)[:200]}")

        print("== (b) complete_json_forced 小 schema ==")
        for st, name in (("summarize","emit_summaries"),
                         ("write_plan_tasks","submit_tasks")):
            t0 = time.time()
            try:
                out = client.complete_json_forced(
                    st, "把『猫会爬树』概括成一条", name,
                    {"type": "object",
                     "properties": {"items": {"type": "array",
                                              "items": {"type": "string"}}},
                     "required": ["items"]})
                print(f"  ✅ {st:18s} {int((time.time()-t0)*1000):6d}ms -> {str(out)[:120]}")
            except Exception as e:
                print(f"  ❌ {st:18s} {type(e).__name__}: {str(e)[:200]}")

        print("== (c) 工具循环冒烟 write_section(mini) ==")
        tools = [{"name": "read_segment", "description": "读一段证据原文",
                  "input_schema": {"type": "object",
                                   "properties": {"ref": {"type": "string"}},
                                   "required": ["ref"]}}]
        msgs = [{"role": "user",
                 "content": "用 read_segment 读 ref='S1·1' 的证据"}]
        try:
            r1 = client.complete_with_tools("write_section", "按指示调用工具",
                                            list(msgs), tools)
            tu = next((b for b in r1["blocks"] if b.get("type") == "tool_use"), None)
            print(f"  第1轮 tool_use: {tu is not None} {tu and tu.get('name')}")
            if tu:
                # 喂 tool_result 再要最终文本（OpenAI 形状 tool 消息）
                msgs2 = msgs + [
                    {"role": "assistant", "content": None,
                     "tool_calls": [{"id": tu.get("id") or "c1",
                                     "type": "function",
                                     "function": {"name": tu["name"],
                                                  "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": tu.get("id") or "c1",
                     "content": "证据原文：猫确实会爬树。"}]
                r2 = client.complete_with_tools("write_section", "按指示调用工具",
                                                msgs2, tools)
                txt = "".join(b.get("text","") for b in r2["blocks"]
                              if b.get("type") == "text")
                print(f"  第2轮 text: {txt[:60]!r}")
        except Exception as e:
            print(f"  ❌ {type(e).__name__}: {str(e)[:200]}")

        print("== (d) 流式: write_section(mini) + outline(glm) ==")
        for st in ("write_section", "outline"):
            t0 = time.time()
            try:
                out = client.complete(st, "回复ok")
                print(f"  ✅ {st:14s} {int((time.time()-t0)*1000):6d}ms len={len(out)}")
            except Exception as e:
                print(f"  ❌ {st:14s} {type(e).__name__}: {str(e)[:200]}")

        print("== (e) 记账核对 llm_call ==")
        rows = calls_for()
        bad = [r for r in rows
               if not (r["input_tokens"] or 0) > 0
               or not (r["output_tokens"] or 0) > 0]
        print(f"  共 {len(rows)} 行；token<=0 的行: {len(bad)}")
        for r in rows[-8:]:
            print(f"    {r['stage']:16s} {r['model']:28s} "
                  f"in={r['input_tokens']} out={r['output_tokens']} "
                  f"cache={r['cache_read_tokens']}/{r['cache_creation_tokens']} "
                  f"{r['status']} {r['latency_ms']}ms")

        print("== (f) 缓存探测 ~3000token 前缀 ×2（mini+glm，仅报告） ==")
        prefix = "重复前缀:" + "研究证据。" * 600
        for st in ("relevance", "outline"):
            for i in (1, 2):
                t0 = time.time()
                try:
                    client.complete(st, prefix + "回复ok")
                except Exception as e:
                    print(f"  ❌ {st}#{i} {type(e).__name__}: {str(e)[:120]}")
                    continue
                rows2 = calls_for()
                last = rows2[-1]
                print(f"  {st}#{i} {int((time.time()-t0)*1000)}ms "
                      f"in={last['input_tokens']} "
                      f"cache_read={last['cache_read_tokens']} "
                      f"cache_create={last['cache_creation_tokens']}")
                if i == 1:
                    time.sleep(3)

        print("== (g) 并发冒烟（mini×20 / glm×6，仅报告） ==")
        for st, n in (("relevance", 20), ("outline", 6)):
            lat, errs, lock = [], {}, threading.Lock()
            def go():
                t0 = time.time()
                try:
                    client.complete(st, "回复ok")
                    with lock: lat.append(int((time.time()-t0)*1000))
                except Exception as e:
                    s = str(e)
                    k = s[s.find(' ') + 1:s.find(':')] if ' ' in s and ':' in s else type(e).__name__
                    with lock: errs[k] = errs.get(k, 0) + 1
            ths = [threading.Thread(target=go) for _ in range(n)]
            t0 = time.time()
            for t in ths: t.start()
            for t in ths: t.join()
            lat.sort()
            p50 = lat[len(lat)//2] if lat else 0
            p95 = lat[int(len(lat)*0.95)] if lat else 0
            print(f"  {st:10s} n={n} ok={len(lat)} err={errs} "
                  f"p50={p50}ms p95={p95}ms wall={int((time.time()-t0)*1000)}ms")
    client.close()
    print(f"\n总 llm_call 行数: {len(calls_for())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
