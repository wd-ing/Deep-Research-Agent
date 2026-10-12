"""会话摘要 stage 的真实数据探针（2026-10-12）。

拿 v5 那份真实报告（23,130 字 / 162 处引用）跑一遍 `session_summary`，
核对三件事：

1. **思考真的是关的**——`reasoning_content` 长度必须为 0。
   xj189 的约定是「省略 reasoning_effort = 关思考」（2026-10-09 实测 3/3），
   这里再验一次，因为摘要一旦开了思考就是纯浪费预算。
2. **输出形状可用**——`summary` / `key_points` / `gaps` 三个字段齐全且类型对。
3. **成本**——文档估的是约 8k 输入 token，相对 v5 的 466k 占比 1.7%。

跑法：
    set -a; . ./.env; set +a
    RA_DB_PATH=/tmp/ra_xj189_v5.db .venv/bin/python tests/probe_session_summary.py

默认**只读不写**：不碰 session 表。要落库加 `--write`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research_agent.db.repo import get_repo          # noqa: E402
from research_agent.graph.nodes import _clean_summary  # noqa: E402
from research_agent.llm.provider import LLMClient      # noqa: E402
from research_agent.graph import prompts               # noqa: E402

SID = "s_d047c6a91e56"


def measure_thinking(cfg, prompt: str, system: str) -> dict:
    """**绕开自己的流式代码**，直接打平台看它到底返不返 `reasoning_content`。

    为什么不用 delta 钩子量（2026-10-12 踩过的坑）：
    `reasoning_content` 只在 `SSEAccum`（流式路径）里被捕获，
    而 `session_summary` 没开 `stream` —— 钩子**压根不触发**，
    量出来永远是 0。那是**假阴性**，看起来像「思考关干净了」，
    实际什么都没测到。`llm_call` 表也只记 token，不记思考长度。

    所以这里按 `provider.py` 里 `complete_json` 的 payload 逐字段复刻一份，
    直连平台读原始响应——量的是平台行为，不是我们自己的封装。
    """
    import httpx
    import os as _os

    key = _os.environ[cfg.api_key_env]
    payload: dict = {
        "model": cfg.model,
        "messages": ([{"role": "system", "content": system}] if system else [])
                    + [{"role": "user", "content": prompt}],
        "max_tokens": cfg.max_output_tokens,
        "temperature": cfg.temperature,
        "response_format": {"type": "json_object"},
    }
    # 与 provider 一致：只有配了才发这一行
    if cfg.reasoning_effort:
        payload["reasoning_effort"] = cfg.reasoning_effort

    t0 = time.time()
    r = httpx.post(f"{cfg.base_url}/chat/completions",
                   headers={"Authorization": f"Bearer {key}"},
                   json=payload, timeout=cfg.timeout_s)
    r.raise_for_status()
    el = time.time() - t0
    msg = r.json()["choices"][0]["message"]
    usage = r.json().get("usage") or {}
    return {
        "elapsed_s": round(el, 1),
        # 平台把字段整个省掉 = 没有思考；有则是字符串
        "reasoning": len(msg.get("reasoning_content") or ""),
        "reasoning_key_present": "reasoning_content" in msg,
        "text": len(msg.get("content") or ""),
        "usage": usage,
        "sent_reasoning_effort": payload.get("reasoning_effort", "(未发送)"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sid", default=SID)
    ap.add_argument("--write", action="store_true", help="把摘要写回 session 表")
    ap.add_argument("--thinking", default="session_summary",
                    help="换一个 stage 跑，用来对照思考长度")
    args = ap.parse_args()

    repo = get_repo()
    rep = repo.q1("SELECT markdown FROM report WHERE session_id=?", (args.sid,))
    if not rep:
        print(f"✗ session {args.sid} 没有报告")
        return 1
    md = rep["markdown"] or ""
    q = repo.q1("SELECT question, summary_json FROM session WHERE id=?", (args.sid,))
    print(f"报告 {len(md):,} 字 · 问题：{(q['question'] or '')[:40]}")
    print(f"已存摘要：{'有' if q['summary_json'] else '无'}\n")

    from research_agent.llm.provider import ModelRegistry

    cfg = ModelRegistry().stage(args.thinking)
    prompt = f"研究问题：{q['question']}\n\n报告正文：\n{md}"

    print(f"--- 直连平台测思考（stage={args.thinking}）---")
    m = measure_thinking(cfg, prompt, prompts.SESSION_SUMMARY)
    print(f"  发出的 reasoning_effort : {m['sent_reasoning_effort']}")
    print(f"  平台返回 reasoning_content: {m['reasoning']} 字符"
          f"（字段{'存在' if m['reasoning_key_present'] else '不存在'}）")
    print(f"  正文长度                : {m['text']} 字符")
    print(f"  usage                   : {m['usage']}")
    print(f"  耗时                    : {m['elapsed_s']}s")
    if m["sent_reasoning_effort"] == "(未发送)":
        verdict = "✅ 符合预期（xj189：省略 = 关思考）" if m["reasoning"] == 0 \
            else "⚠️ 没发 reasoning_effort 但仍返回思考——平台行为已变，档案层注释要改"
    else:
        verdict = "（本次带了 reasoning_effort，用于对照）"
    print(f"  判定                    : {verdict}\n")

    llm = LLMClient()
    t0 = time.time()
    raw = llm.complete_json(args.thinking, prompt, system=prompts.SESSION_SUMMARY)
    el = time.time() - t0

    out = _clean_summary(raw)
    print(f"--- 经 LLMClient 的真实调用（{el:.1f}s）---")
    print(json.dumps(out, ensure_ascii=False, indent=2))

    ok = bool(out["summary"]) and len(out["key_points"]) >= 3
    print(f"\n{'✅' if ok else '❌'} 形状检查：summary={'有' if out['summary'] else '无'} "
          f"key_points={len(out['key_points'])} 条 gaps={len(out['gaps'])} 条")

    if args.write:
        repo.set_summary(args.sid, out)
        print(f"\n已写回 session {args.sid}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
