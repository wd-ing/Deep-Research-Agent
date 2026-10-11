"""审查环节的 `blocked` 判据探针（⚠️ 结论已修正，见文末）

背景
----
v5 跑出 `blocked=0`，两种解释都说得通：
  A. 材料确实够（41 来源 143 段）
  B. 审查缺判据，遇到材料不够只会默默处理，读者看不出来

⚠️ **本测试第一次跑出来的结论是错的，别照着它下判断。**

错在哪
------
这个探针的草稿是**我手写的**，里面「2 段材料写出 6 个批次数据」这个前提
是我自己造的，不是观察到的真实失败——然后我拿它去证明
「审查抓不到编造」。**先假定结论再验证。**

真实数据复核（2026-10-11，v5 / `s_d047c6a91e56`）
---------------------------------------------------
143 段证据、410,575 字正文，报告里 **17 个数值断言全部能在证据中找到出处，
零编造**；`tool_reads = {read_segments: 21}` 说明子 agent 确实调工具下挖过。

而且**子 agent 没有编造的条件**：它手上只有分配给它那几段的概括，
要写具体数字必须调 `read_segments` 取原文。
**「`blocked=0` = 材料确实够」这个最初推断是对的**，
被我自己的坏探针带偏了。

顺带记下一条架构判断（用户提出，我认为比当时那两个方案都更站得住）：
**审查的职责是跨任务一致性，不是逐条核证据。**
跨任务矛盾只有审查看得见（子 agent 互相看不到对方正文，是最小权限设计）；
单节内的证据忠实度是子 agent 自己的责任（它手上有证据、有工具）。
让审查代劳等于把输入撑爆，还越权。

所以这个探针现在只作为**回归基线**保留：材料稀缺且分派过贪时，
审查**有没有能力**判出 `bad_assignment`。真实稀缺场景还没测到。

跑法：.venv/bin/python tests/blocked_probe.py
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

os.environ.setdefault("RA_DB_PATH", "/tmp/ra_blocked_probe.db")

from research_agent.graph.subgraphs import write as W  # noqa: E402

SPEC = {
    "thesis": "该型号变频器存在严重的批次性能离散问题",
    "glossary": [{"canonical": "批次离散", "note": "同型号不同批次的实测参数差异"}],
    "units": ["kW", "Hz"],
    "cautions": ["不要用竞品数据代替本品实测"],
    "sections": [{"id": "sec1", "role": "给出结论与依据"},
                 {"id": "sec2", "role": "列出各批次实测差异"},
                 {"id": "sec3", "role": "分析差异成因"}],
    "spec_uncertainty": "本报告所依据的第三方实测数据仅有一份来源。",
}

# 三个任务：t2 只分到 2 段，却要「逐批次核验」——典型的 bad_assignment
TASKS = [
    {"id": "t1", "covers": ["sec1"], "segments": ["S1·1", "S1·2", "S1·3"],
     "must_cover": ["给出结论", "注明数据来源"], "not_yours": []},
    {"id": "t2", "covers": ["sec2"], "segments": ["S2·1", "S2·2"],
     "must_cover": ["逐批次列出 6 个批次的实测参数", "每个批次标注测试条件"],
     "not_yours": []},
    {"id": "t3", "covers": ["sec3"], "segments": ["S3·1"],
     "must_cover": ["分析器件批次差异的物理成因", "对比业界公认成因"],
     "not_yours": []},
]

# 草稿刻意「看起来完整」：t2 材料只有 2 段，却写了 6 个批次的结论。
# 这正是要抓的形状——**静默编造 / 静默写薄，读者看不出来**。
DRAFT = """<!-- task:t1 section:sec1 -->
### 1.1 结论
该型号变频器存在明显的批次性能离散，输出功率波动约 ±7% [S1·1]。
数据来自第三方实测报告 [S1·2]，测试条件为额定负载 [S1·3]。

<!-- task:t2 section:sec2 -->
### 2.1 各批次实测差异
批次 A：输出偏差 6.8%，噪声 42dB [S2·1]。
批次 B：输出偏差 7.2%，噪声 44dB [S2·1]。
批次 C：输出偏差 6.5%，噪声 41dB [S2·1]。
批次 D：输出偏差 7.4%，噪声 45dB [S2·2]。
批次 E：输出偏差 6.9%，噪声 43dB [S2·2]。
批次 F：输出偏差 7.1%，噪声 44dB [S2·2]。
以上六批次测试条件一致，差异显著。

<!-- task:t3 section:sec3 -->
### 3.1 成因分析
批次差异主要来自功率器件批次本身的参数漂移 [S3·1]，
这与业界公认的电介质厚度不均成因一致。
"""


def main() -> None:
    state = {
        "session_id": None,
        "question": "某工业变频器型号的批次性能离散问题严重吗？成因是什么？",
        "spec": SPEC,
        "outline": [{"id": "sec1", "title": "结论"},
                    {"id": "sec2", "title": "批次实测差异"},
                    {"id": "sec3", "title": "成因分析"}],
        "tasks": TASKS,
        "assembled": DRAFT,      # ⚠️ node_audit 读的是 assembled，不是 audited
        "violations": {},
        "rework_rounds": 0,
    }
    out = W.node_audit(state)
    a = out["audit"]
    print("=" * 70)
    print(f"findings {len(a['findings'])} 条 / high {len(a['targets'])} 条 / "
          f"blocked {len(a['blocked'])} 条")
    print(f"need_rework={a['need_rework']}")
    print(f"reason: {a['reason']}")
    print("-" * 70)
    for b in a["blocked"]:
        print(f"  [blocked/{b['kind']}] {b.get('task')}/{b.get('section')}")
        print(f"     {b['issue']}")
    if not a["blocked"]:
        print("  （没有 blocked —— 材料只有 2 段却写了 6 个批次的结论，"
              "审查竟然没喊出来）")
    print("-" * 70)
    print("remaining_issues（会写进报告存疑节）:")
    for r in a["remaining_issues"]:
        print(f"  - {r}")


if __name__ == "__main__":
    main()
