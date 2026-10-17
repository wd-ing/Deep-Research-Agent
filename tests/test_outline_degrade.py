"""`node_outline` 的**框架退化分支**（2026-10-18）。

## 这个 bug

全量 e2e 第一轮在 `outline` 崩了：

    UnboundLocalError: cannot access local variable 'warn'
      File "research_agent/graph/nodes.py", line 211, in node_outline
        warn.append("框架退化：" + "；".join(why))

`warn = []` 写在下面第 215 行，**晚于**第 211 行的 `warn.append(...)`。
Python 把它编译成局部变量，正常路径上从没赋值 → 一进退化分支就崩。

## 为什么平时没炸

因为**这条分支从来没被执行过**：框架正常时不碰它。
而它恰恰只在「探针降级 / sections 为空 / 小节太少」时才走 ——
也就是最需要它的那一次。

触发它的真实链条（2026-10-18 全量 e2e 实测）：

    probe_outline JSON 解析失败
      → node_probe_outline 返回**没有 signals 的降级态**
      → outline 输入只剩 364 token（对照正常 5,172）
      → 模型写不出 grounded_in，sections 为空
      → 框架退化分支 → 💥

**探针降级时报告已经不可信了，而告知读者「报告退化了」的那句话正好崩在这里。**

## 本测试要守住的

1. 退化分支**不许抛异常**（哪怕 `warn` 里只有一条）。
2. 退化时要**留下痕迹**（`events[].warnings` 里有「框架退化」）——
   静默退化正是这个项目反复消灭的东西。
3. 正常路径不许被这次挪动影响。

桩接在 `LLM.complete_json` 上（生产判定的入口），provider **钉死**，
不跟着 `defaults` 漂。

用法：.venv/bin/python tests/test_outline_degrade.py
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

# 临时库 + 真会话（outline 会 UPDATE session）
_TMP = Path(tempfile.gettempdir()) / "ra_outline_degrade.db"
if _TMP.exists():
    _TMP.unlink()
os.environ["RA_DB_PATH"] = str(_TMP)

import research_agent.db.repo as _repo_mod  # noqa: E402
_r = _repo_mod.get_repo()
_r.x("INSERT INTO session (id,title,question,status,created_at,updated_at) "
     "VALUES (?,?,?,?,?,?)",
     ("s_deg", "退化分支自检", "固态电池", "done",
      "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))

from research_agent.graph.nodes import node_outline  # noqa: E402
from research_agent.llm.provider import LLMClient  # noqa: E402

fails: list[str] = []


def check(tag: str, ok: bool, detail: str = "") -> None:
    print(f"{'✅' if ok else '❌'} {tag}" + (f"\n   {detail}" if detail else ""))
    if not ok:
        fails.append(tag)


GOOD_SECTIONS = [
    {"title": "定义与层级", "question": "怎么划分", "kind": "definition",
     "grounded_in": ["全固态电池指完全不含液态电解质"]},
    {"title": "材料路线", "question": "各路线差异", "kind": "comparison",
     "grounded_in": ["硫化物电导率高但遇水放出硫化氢"]},
    {"title": "企业格局", "question": "谁在做什么", "kind": "comparison",
     "grounded_in": ["某厂商宣称 2027 年量产半固态产品"]},
]

BASE_STATE = {
    "session_id": "s_deg",
    "question": "固态电池的产业化进展，以及各家技术路线的分歧点",
    "probe": {"signals": {"subtopics": [{"name": "材料路线"}],
                          "entities": [{"name": "硫化物电解质"}]}},
}


def run_with(plan: dict, state: dict) -> dict:
    """把 `LLM.complete_json` 桩成给定的返回，然后真跑一次 `node_outline`。"""
    orig = LLMClient.complete_json
    LLMClient.complete_json = lambda self, stage, prompt, system=None: dict(plan)
    try:
        return node_outline(state)
    finally:
        LLMClient.complete_json = orig


# ── ① 退化：探针降级 + sections 为空 ──
print("=== ① 探针降级 + sections 为空（e2e 实测触发的那一格）===")
state = {**BASE_STATE, "probe_degraded": True, "probe": {}}
try:
    out = run_with({"sections": [], "queries": {"zh": ["固态电池"], "en": []}}, state)
    warns = (out.get("events") or [{}])[0].get("warnings") or []
    check("① 退化分支不抛异常", True, f"返回 {len(out.get('outline') or [])} 个小节")
    check("① 退化留下了痕迹",
          any("框架退化" in w for w in warns), f"warnings={warns}")
    check("① 退回的极简框架至少有一节",
          len(out.get("outline") or []) >= 1, str(out.get("outline"))[:120])
except Exception as e:
    check("① 退化分支不抛异常", False,
          f"{type(e).__name__}: {str(e)[:140]}")
    check("① 退化留下了痕迹", False, "上一条已崩，后面都测不到")

# ── ② 退化：sections 非空但都没 grounded_in ──
print("\n=== ② sections 有内容但全缺 grounded_in ===")
try:
    out2 = run_with({"sections": [{"title": "凭空写的一节", "question": "?"}],
                     "queries": {"zh": ["固态电池"], "en": []}}, BASE_STATE)
    warns2 = (out2.get("events") or [{}])[0].get("warnings") or []
    check("② 缺 grounded_in 被丢弃且有告警",
          any("框架退化" in w for w in warns2), f"warnings={warns2}")
except Exception as e:
    check("② 缺 grounded_in 被丢弃且有告警", False,
          f"{type(e).__name__}: {str(e)[:140]}")

# ── ③ 正常路径不受影响 ──
print("\n=== ③ 正常路径（挪 `warn = []` 不能碰坏它）===")
try:
    out3 = run_with({"sections": GOOD_SECTIONS,
                     "queries": {"zh": ["固态电池"], "en": ["solid state battery"]}},
                    BASE_STATE)
    warns3 = (out3.get("events") or [{}])[0].get("warnings") or []
    check("③ 三个小节全保留", len(out3.get("outline") or []) == 3,
          str([s.get("title") for s in out3.get("outline") or []]))
    check("③ 正常路径没有框架退化告警",
          not any("框架退化" in w for w in warns3), f"warnings={warns3}")
    check("③ 查询词照常透出",
          (out3.get("plan_queries") or {}).get("zh") == ["固态电池"],
          str(out3.get("plan_queries")))
except Exception as e:
    check("③ 正常路径不受影响", False, f"{type(e).__name__}: {str(e)[:140]}")

print("\n" + "=" * 60)
print(f"失败 {len(fails)} 项" + ("：" + "、".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
