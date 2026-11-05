#!/usr/bin/env python
"""探针：验证「失败调用也留痕」（产品化 #2）。

背景：`llm_call` 原来只在成功路径上记账，所以 504 / 429 / JSON 解析失败
在表里查不到任何东西——真出问题时只能猜。2026-10-13 加了 `status`/`error`
字段并补上三条失败路径的记账。

这个探针**不碰真实库、不发任何网络请求**（monkeypatch 掉底层调用），
只验证记账行为，可以随时重跑：

    RA_DB_PATH=/tmp/fail_probe.db python tests/probes/probe_call_failure.py

三条路径各自应该记成什么样：
  ① provider 抛异常          → 只有 1 行 error（不记 ok）
  ② JSON 解析失败            → ok + error 两行，**token 不重复**
  ③ 模型没按要求调工具        → 只有 1 行 error（不记 ok）
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 用临时库，别碰任何真实数据
_tmp = tempfile.NamedTemporaryFile(prefix="fail_probe_", suffix=".db", delete=False)
os.close(_tmp.fileno())
os.environ["RA_DB_PATH"] = _tmp.name

from research_agent.db.repo import get_repo            # noqa: E402
from research_agent.llm.provider import LLMClient      # noqa: E402


def main() -> int:
    repo = get_repo()
    llm = LLMClient()

    # ① provider 调用失败——模拟 504 / 连接超时
    def boom(*a, **k):
        raise RuntimeError("upstream 504 Gateway Timeout")
    llm._opencode = boom
    try:
        llm.complete("assess", "测试提示词，用于 provider 失败路径" * 5)
        print("❌ ① 应该抛异常却没抛")
        return 1
    except Exception as e:
        print(f"  ① complete() → {type(e).__name__}")

    # ② JSON 解析失败——token 花了但业务上没成
    llm._opencode = lambda *a, **k: ("这不是 JSON，是一段散文。",
                                     {"input_tokens": 123, "output_tokens": 45})
    try:
        llm.complete_json("assess", "测试提示词，用于 JSON 解析失败路径" * 5)
        print("❌ ② 应该抛异常却没抛")
        return 1
    except Exception as e:
        print(f"  ② complete_json() → {type(e).__name__}")

    # ③ 模型返回文本而不是工具调用
    def no_tool(cfg_, system, prompt, tool):
        return ([{"type": "text", "text": "我不调工具"}],
                {"input_tokens": 10, "output_tokens": 5})
    llm._openai_compat_forced = no_tool
    try:
        llm.complete_json_forced("summarize", "测试提示词", name="emit_summaries",
                                 schema={"type": "object"})
        print("❌ ③ 应该抛 EmptyOutput 却没抛")
        return 1
    except Exception as e:
        print(f"  ③ complete_json_forced() → {type(e).__name__}")

    print("\nllm_call 实际记录：")
    rows = repo.q("SELECT stage,status,input_tokens,output_tokens,"
                   "est_input_tokens,error FROM llm_call ORDER BY rowid")
    for r in rows:
        print(f"  {r['stage']:10} status={r['status']:6} in={r['input_tokens']:4} "
              f"out={r['output_tokens']:3} est={(r['est_input_tokens'] or 0):4} "
              f"err={(r['error'] or '')[:44]}")

    n_ok = repo.q1("SELECT COUNT(*) c FROM llm_call WHERE status='ok'")["c"]
    n_err = repo.q1("SELECT COUNT(*) c FROM llm_call WHERE status='error'")["c"]

    # 断言：三条失败路径各留了痕迹，且 error 行不污染 token 统计
    fails = []
    if n_err != 3:
        fails.append(f"应有 3 行 error，实际 {n_err}")
    if any(r["input_tokens"] for r in rows if r["status"] == "error"):
        fails.append("error 行的 input_tokens 必须为 0，否则花费统计会被污染")
    forced = [r for r in rows if r["stage"] == "summarize"]
    if len(forced) != 1 or forced[0]["status"] != "error":
        fails.append(f"EmptyOutput 应只记 1 行 error，实际 {forced}")
    parse = [r for r in rows if r["error"] and "JSONDecodeError" in r["error"]]
    if len(parse) != 1:
        fails.append(f"JSON 解析失败应补记 1 行 error，实际 {len(parse)}")

    os.unlink(_tmp.name)
    print(f"\nok={n_ok}  error={n_err}")
    if fails:
        for f in fails:
            print(f"  ❌ {f}")
        return 1
    print("  ✓ 失败调用全部留痕，且 error 行不污染 token 统计")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
