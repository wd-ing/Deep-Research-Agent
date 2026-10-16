"""新平台的 reasoning_effort 语义实测，n≥3。

**为什么必须重测而不是沿用 xj189 的结论**：两个平台的语义是**相反**的。

| | xj189 | 信通院（预检 n=1） |
|---|---|---|
| 省略该字段 | **关思考**（`[0,0,0]`） | **开思考**（52 字） |
| `none` | **开思考**（140–399 字） | **关思考**（0 字） |

这直接决定配置怎么写：当前 14 个环节里有 **8 个故意省略 effort**
（靠「省略 = 关思考」来关掉思考），换平台后它们会**全部被反向打开** ——
而这 8 个正是调用量最大、输出最短的一档。

⚠️ **n=1 不能定配置**：预检只在「27×31」这种不用思考的题上跑，
思考长度全是几十字，分不出档位差别。这里换成真需要推理的题 + n=3。

用法：
    .venv/bin/python tests/probe_intern_ai_effort.py
    .venv/bin/python tests/probe_intern_ai_effort.py --model deepseek-v4-flash-0731
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

BASE = "https://discovery-api.intern-ai.org.cn/v1"

# 真需要推理的题：短答案是偷懒，长思考才是真在工作。
# 用「数到二十里 7 的倍数」这种要真算的，比「27×31」有区分度。
PROMPT = ("找出 1 到 30 之间所有 7 的倍数，按升序列出，"
          "然后说明你是怎么检查的。只输出答案和简短说明。")

LEVELS = ["<省略>", "none", "low", "medium", "high"]


def load_env() -> None:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def one(client: httpx.Client, model: str, effort: str) -> dict:
    payload = {"model": model, "max_tokens": 4096,
               "messages": [{"role": "user", "content": PROMPT}]}
    if effort != "<省略>":
        payload["reasoning_effort"] = effort
    t0 = time.time()
    r = client.post(f"{BASE}/chat/completions", json=payload, timeout=300)
    r.raise_for_status()
    d = r.json()
    msg = (d.get("choices") or [{}])[0].get("message") or {}
    usage = d.get("usage") or {}
    rc = msg.get("reasoning_content")
    return {
        "t": round(time.time() - t0, 1),
        "rc_chars": len(rc) if isinstance(rc, str) else 0,
        "rc_field": "有" if isinstance(rc, str) and rc else "无",
        "text_chars": len(msg.get("content") or ""),
        "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        "finish": (d.get("choices") or [{}])[0].get("finish_reason"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek-v4-flash-vision")
    ap.add_argument("--n", type=int, default=3)
    args = ap.parse_args()

    load_env()
    h = {"Authorization": "Bearer " + os.environ["INTERN_AI_API_KEY"],
         "Content-Type": "application/json"}
    print(f"=== reasoning_effort 语义实测  n={args.n}  {args.model} ===")
    print(f"题：{PROMPT}\n")

    rows = {}
    with httpx.Client(trust_env=False, headers=h,
                      timeout=httpx.Timeout(300.0, connect=20.0)) as c:
        for eff in LEVELS:
            rows[eff] = []
            for i in range(args.n):
                try:
                    r = one(c, args.model, eff)
                    rows[eff].append(r)
                    print(f"  {eff:<10} #{i+1} {r['t']:>5.1f}s "
                          f"思考 {r['rc_chars']:>5} 字 (tokens={r['reasoning_tokens']}) "
                          f"正文 {r['text_chars']:>4} 字 finish={r['finish']}")
                except Exception as e:
                    print(f"  {eff:<10} #{i+1} ❌ {type(e).__name__}: {str(e)[:120]}")

    print("\n--- 汇总（先看组内方差，再谈档位差别）---")
    print(f"{'档位':<10}{'思考字数':<22}{'思考 tokens':<18}{'耗时':<16}rc字段")
    for eff in LEVELS:
        rs = rows[eff]
        if not rs:
            print(f"{eff:<10}(全失败)")
            continue
        chars = [r["rc_chars"] for r in rs]
        toks = [r["reasoning_tokens"] for r in rs if r["reasoning_tokens"] is not None]
        ts = [r["t"] for r in rs]
        cstr = (f"{statistics.median(chars):.0f} "
                f"[{min(chars)}~{max(chars)}]" + (" ← 组内差很大" if max(chars) - min(chars) > 30 else ""))
        tstr = f"{statistics.median(ts):.1f}s [{min(ts)}~{max(ts)}]"
        tokstr = f"{statistics.median(toks):.0f}" if toks else "-"
        print(f"{eff:<10}{cstr:<22}{tokstr:<18}{tstr:<16}{rs[0]['rc_field']}")

    print(json.dumps(rows, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
