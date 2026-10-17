"""**单独**测 `write_plan_spec` 的思考量 —— 它至今零数据。

为什么必须单独测
----------------
2026-10-17 之前，`node_plan_spec` 和 `node_plan_tasks` **共用 stage 名
`"write_plan"`，共用一个 `max_output_tokens`**。后果有两个：

  1. 消融那 15 次跑的都是 **tasks 载荷**（`probe_plan_tasks_empty.build_prompt`），
     spec 的思考量**一次都没测过**；
  2. DB 里三处调用点全记成 `write_plan`，**历史数据无法归因到节点** ——
     上一轮 e2e 的 `write_plan ok 3` 根本分不清哪次是 spec、哪次是 tasks。

拆分之后 spec 第一次能被单独测。**载荷用真实数据回放**：
从 `/tmp/ra_internai_v1.db` 取那一轮真实的框架、轮廓、来源、109 段概括，
用生产同一个 `_brief()` 组装 —— 不自己手搓，避免「测的不是项目真正会发的」。

段号 `S{来源序号}·{来源内序号}` 按 session 内 rowid 排（与 `server.py:242` 同套）。

用法：.venv/bin/python tests/probe_plan_spec.py [次数] [预算]
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import httpx  # noqa: E402
import yaml  # noqa: E402

from research_agent.graph import prompts  # noqa: E402
from research_agent.graph.subgraphs.write import _brief, _clip, _dumps  # noqa: E402
from research_agent.llm.provider import _sse_lines  # noqa: E402

DB = "/tmp/ra_internai_v1.db"
SID = "s_efb71e9cbf66"


def load_real_state() -> dict:
    c = sqlite3.connect(DB)
    cur = c.cursor()
    cur.execute("SELECT question, outline_json, probe_json FROM session WHERE id=?", (SID,))
    question, outline_json, probe_json = cur.fetchone()
    outline = json.loads(outline_json).get("sections", [])

    # 来源按 rowid 排 → S1..Sn
    cur.execute("SELECT id, url, domain, title, source_type, source_date "
                "FROM source WHERE session_id=? ORDER BY rowid", (SID,))
    srcs = cur.fetchall()
    disp = {sid_: i for i, (sid_, *_) in enumerate(srcs, 1)}
    src_lines = [
        f"[S{i}] {title or '(无标题)'} | {domain or '?'} | {st or '?'} | "
        f"{(sd or '?')} | 正文 {n} 字"
        for i, (_id, _u, domain, title, st, sd, *_x) in enumerate(srcs, 1)
        for n in [0]
    ]
    # 真实来源行由 write_trace / 前端同一套渲染，这里用最小可辨识形式
    cur.execute("SELECT id, source_id, heading_path, summary, token_count, on_topic "
                "FROM chunk WHERE session_id=? AND summary IS NOT NULL AND summary!='' "
                "ORDER BY rowid", (SID,))
    segs, idx_in_src = [], {}
    for cid, sid_, head, summ, tok, on_topic in cur.fetchall():
        idx_in_src[sid_] = idx_in_src.get(sid_, 0) + 1
        segs.append({"ref": f"S{disp.get(sid_, 0)}·{idx_in_src[sid_]}",
                     "heading_path": head or "", "summary": summ,
                     "token_count": tok})
    on_topic = [g for g, o in zip(segs, cur.execute(
        "SELECT on_topic FROM chunk WHERE session_id=? AND summary IS NOT NULL "
        "AND summary!='' ORDER BY rowid", (SID,)).fetchall()) if o]
    return {"question": question, "outline": outline,
            "signals": json.loads(probe_json), "src_lines": src_lines,
            "segments": segs, "on_topic": on_topic}


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 65536

    cfg = yaml.safe_load((ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
    prov = cfg["defaults"]["provider"]
    p = cfg["providers"][prov]
    key = os.environ.get(p["api_key_env"], "")
    base = p["base_url"].rstrip("/")

    st = load_real_state()
    payload = _brief(st)
    body_text = _clip(_dumps(payload), 40000)
    sysmsg = prompts.WRITE_PLAN_SPEC

    print(f"=== write_plan_spec 思考量实测  {p['model']} @ {prov} ===")
    print(f"预算 {budget}｜effort high｜n={n}")
    print(f"真实载荷：{len(payload['证据段落'])} 段 / "
          f"{len(payload['已批准的框架'])} 节 / "
          f"提示词 {len(body_text)} 字符\n")

    res = []
    with httpx.Client(trust_env=False,
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"},
                      timeout=900.0) as c:
        for i in range(n):
            body = {"model": p["model"], "max_tokens": budget, "temperature": 0.0,
                    "reasoning_effort": "high",
                    "messages": [{"role": "system", "content": sysmsg},
                                 {"role": "user", "content": body_text}],
                    "stream": True, "stream_options": {"include_usage": True}}
            t0 = time.time()
            text = reason = fin = ""
            used = None
            try:
                with c.stream("POST", f"{base}/chat/completions", json=body) as r:
                    r.raise_for_status()
                    for obj in _sse_lines(r):
                        if obj.get("usage"):
                            used = obj["usage"]
                        for ch in obj.get("choices") or []:
                            d = ch.get("delta") or {}
                            text += d.get("content") or ""
                            reason += d.get("reasoning_content") or ""
                            fin = ch.get("finish_reason") or fin
            except Exception as e:
                print(f"  #{i+1} ⚠️  {type(e).__name__}: {str(e)[:80]}"
                      f"（平台故障，不计入）")
                time.sleep(5)
                continue
            rt = ((used or {}).get("completion_tokens_details") or {}).get("reasoning_tokens")
            ok = bool(text.strip())
            res.append({"ok": ok, "content": len(text), "reasoning_tokens": rt,
                        "finish": fin, "t": round(time.time() - t0, 1)})
            print(f"  #{i+1} {'✅' if ok else '❌'} content {len(text):>5} / "
                  f"思考token {rt} / finish={fin} / {time.time()-t0:6.1f}s / "
                  f"总token {(used or {}).get('completion_tokens')}")
            time.sleep(2)

    print(f"\n成功 {sum(1 for r in res if r['ok'])}/{len(res)}")
    if res:
        rts = [r["reasoning_tokens"] for r in res if r["reasoning_tokens"] is not None]
        ts = [r["t"] for r in res]
        print(f"思考 token 中位 {statistics.median(rts) if rts else '-'}｜"
              f"耗时中位 {statistics.median(ts):.1f}s")
        print(f"finish 分布：{[r['finish'] for r in res]}")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
