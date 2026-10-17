"""`summarize_segments` 的**有界重试**（2026-10-18）。

## 为什么要

一次批次失败 = **12 段证据永远到不了写作阶段**，而报告照样写完、
`degraded: false`、不报错。MiniMax 全量实测（`/tmp/ra_v12_minimax.db`）：

    summarize ok 20 · error 12（37.5%）
    ⇒ 246 个分块里 98 个仍 pending、148 段里 98 段无概括

## 本文件守四件事

① **失败会重试**，而且**只重试失败的批次**（不重跑成功的）
② **重试有界**（`SUMM_RETRY=1`）—— 无界重试会把「模型就是交不出来」
   放大成时间与 token 的黑洞
③ **「返回了但条数不够」也算失败** —— 下标错位/空 summary 会被
   「模型给错下标就跳过，不猜」悄悄丢掉，只重试抛异常会漏掉这一半
④ **两次都不成的照旧计入 `failed`**，事件里如实记
   `retried` / `recovered` / `still_incomplete`

桩接在 `LLM.complete_json_forced` 上（生产判定的入口），
不碰网络。

用法：.venv/bin/python tests/test_summarize_retry.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
os.environ["RA_DB_PATH"] = str(Path(tempfile.gettempdir()) / "ra_summ_retry.db")

from research_agent.graph.subgraphs import extract_batch as EB  # noqa: E402
from research_agent.llm.provider import LLMClient  # noqa: E402

fails: list[str] = []


def check(tag: str, ok: bool, detail: str = "") -> None:
    print(f"{'✅' if ok else '❌'} {tag}" + (f"\n   {detail}" if detail else ""))
    if not ok:
        fails.append(tag)


def make_items(n: int) -> list[dict]:
    """n 个段落，每段文本短小 ⇒ 全部落进同一批（可控批次数）。"""
    return [{"chunk_id": f"c{i}", "text": f"段落{i}讲的是某个具体的技术判断。",
             "heading_path": f"H{i}"} for i in range(n)]


def stub(script: list, n_batches: int):
    """把 `complete_json_forced` 桩成按脚本依次返回。

    脚本元素可以是 dict（成功，条数由 `n` 决定）或 None（抛异常）。
    """
    calls: list[str] = []
    it = iter(script)

    def fake(self, stage, prompt, name=None, schema=None, system=None):
        calls.append(prompt[:40])
        step = next(it, {"ok": True, "n": None})
        if step is None:
            raise RuntimeError("EmptyOutput: 模型返回了文本而不是工具调用")
        n = step.get("n")
        if n is None:      # 默认：把批次里每段都概括到
            n = prompt.count("[") and len(
                [1 for ln in prompt.splitlines() if ln.startswith("[")])
        return {"summaries": [{"index": i, "summary": f"s{i}"} for i in range(n)]}

    return fake, calls


def run(script, n_items=12):
    fake, calls = stub(script, 1)
    orig = LLMClient.complete_json_forced
    LLMClient.complete_json_forced = fake
    try:
        out = EB.summarize_segments(make_items(n_items))
    finally:
        LLMClient.complete_json_forced = orig
    return out, calls


# 确认这批用例真的只切出 1 个批次（否则下面按「第几批」的假设会错）
_probe, _ = run([{"ok": True}] * 40, 12)
print(f"（12 个小段落 ⇒ 切成 "
      f"{_probe['events'][0]['batches']} 批）")
if _probe["events"][0]["batches"] != 1:
    check("用例前置条件：12 个小段落只切出 1 批", False,
          f"实际 {_probe['events'][0]['batches']} 批，下面断言的批次编号会错位")
    print("\n失败 " + str(len(fails)) + " 项")
    sys.exit(1)
check("用例前置条件：12 个小段落只切出 1 批", True)

print("\n=== ① 失败会重试 ===")
out, calls = run([None, {"ok": True}])          # 第 1 次抛，第 2 次成
ev = out["events"][0]
check("① 重试后拿到全部 12 段", len(out["summaries"]) == 12,
      f"拿到 {len(out['summaries'])} 段")
check("① 确实发起了第二次调用", len(calls) == 2, f"实际 {len(calls)} 次")
check("① retried=1 / recovered=1 / failed=0",
      (ev["retried"], ev["recovered"], ev["failed"]) == (1, 1, 0),
      f"retried={ev['retried']} recovered={ev['recovered']} failed={ev['failed']}")

print("\n=== ② 两次都不成：照旧算失败，不无限重试 ===")
out, calls = run([None, None])
ev = out["events"][0]
check("② 只发两次（不无限重试）", len(calls) == 2, f"实际 {len(calls)} 次")
check("② failed=1 且一段都没拿到", ev["failed"] == 1 and not out["summaries"],
      f"failed={ev['failed']} summaries={len(out['summaries'])}")
check("② still_incomplete 如实记下是第几批", ev["still_incomplete"] == [0],
      str(ev["still_incomplete"]))

print("\n=== ③ 「返回了但条数不够」也要重试 ===")
out, calls = run([{"n": 5}, {"ok": True}])      # 第 1 次只给 5/12
ev = out["events"][0]
check("③ 条数不够会触发重试", len(calls) == 2, f"实际 {len(calls)} 次")
check("③ 重试后补齐 12 段", len(out["summaries"]) == 12,
      f"拿到 {len(out['summaries'])} 段")
check("③ recovered=1", ev["recovered"] == 1, f"recovered={ev['recovered']}")

print("\n=== ④ 成功批次不重跑 ===")
out, calls = run([{"ok": True}])
ev = out["events"][0]
check("④ 一次成功就不重试", len(calls) == 1, f"实际 {len(calls)} 次")
check("④ retried=0 / recovered=0", (ev["retried"], ev["recovered"]) == (0, 0),
      f"retried={ev['retried']} recovered={ev['recovered']}")

print("\n=== ⑤ 开关：SUMM_RETRY=0 即回到旧行为 ===")
_orig_retry = EB.SUMM_RETRY
try:
    EB.SUMM_RETRY = 0
    out, calls = run([None, {"ok": True}])
    ev = out["events"][0]
    check("⑤ 关掉重试 ⇒ 只发一次、如实记 failed",
          len(calls) == 1 and ev["failed"] == 1 and ev["retried"] == 0,
          f"calls={len(calls)} failed={ev['failed']} retried={ev['retried']}")
finally:
    EB.SUMM_RETRY = _orig_retry

print("\n" + "=" * 60)
print(f"失败 {len(fails)} 项" + ("：" + "、".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
