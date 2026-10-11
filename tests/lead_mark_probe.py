"""验证子 agent 会不会照约定给引言标题加 `<!-- lead -->` 标记。

方案 C 的前提：模型愿意按约定输出标记，程序才能靠标记删除引言标题。
这个测试只验证「遵守率」，不验证内容质量。

跑法：.venv/bin/python tests/lead_mark_probe.py
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from research_agent.llm.provider import LLMClient  # noqa: E402

LEAD_RE = re.compile(r"<!--\s*lead\s*-->")

RULE = """写你负责的这一块。

**你的正文会被装到大纲小节标题下面去，节标题由系统加上，你不用重复它。**

**开头那句概括本节的小标题，请这样写**：先单独写一行 `<!-- lead -->`，
紧接着写那个标题。**加上这行标记，它就会被识别成「引言」而不是子标题**——
引言不进目录、不编号，正文里当普通段落读。
不带标记的小标题会被当成真正的子标题，编进目录。

**不要写 `##`。** 内部层次一律用 `###` 和更深的级别，且**不要自己编号**
（不要写「一、」「1.1」「sec3」这类前缀），编号由系统统一补。"""

CASES = [
    ("sec1", "固态电池技术路线分歧：硫化物、氧化物、聚合物与卤化物",
     "这四条路线各自的性能上限与工程化难度如何取舍",
     ["硫化物电解质离子电导率高但遇水产生硫化氢，量产环境要求极严",
      "氧化物热稳定性好、易与现有产线衔接，但界面阻抗高",
      "聚合物柔韧性好、界面接触好，室温电导率偏低",
      "卤化物理论上兼顾高电导与可加工性，尚处实验室阶段"]),
    ("sec2", "固-固界面问题：全固态电池最核心的技术瓶颈",
     "固-固界面的阻抗来源与主流缓解手段",
     ["固-固接触面积小，界面阻抗远高于液-液体系",
      "脱溶剂化不充分导致界面副反应持续",
      "循环中体积变化使接触面失配、孔隙累积",
      "表面修饰与缓冲层可显著降低界面阻抗"]),
    ("sec3", "成本困局：硫化锂价格与全固态电池商业化",
     "硫化锂价格占硫化物电解质成本的比重及其下降路径",
     ["硫化锂原料价格高，硫化物电解质成本远高于氧化物",
      "国产化替代正在推进，但产能释放仍需时间",
      "工艺改进与规模化生产是中期降本的主要来源",
      "各来源给出的价格数据差异较大，缺乏独立验证"]),
]


def main() -> None:
    llm = LLMClient()
    ok = 0
    for sid, title, mandate, segs in CASES:
        body = "\n".join(f"[S{i+1}·1] {s}" for i, s in enumerate(segs))
        prompt = (
            f"研究问题：固态电池的产业化进展\n\n"
            f"【你的任务】\n对应小节={sid}\n"
            f"小节标题：{title}\n"
            f"要回答的问题：{mandate}\n\n"
            f"【你的证据段落】\n{body}\n"
        )
        try:
            out = llm.complete("write_section", prompt, system=RULE)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  {sid} 调用失败：{e}")
            continue
        first = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
        is_lead_marked = bool(LEAD_RE.search(out))
        has_heading = first.startswith("#")
        numbered = bool(re.match(r"^#{2,6}\s+(?:[一二三四五六七八九十]+、|\d|sec\d)", first))
        if is_lead_marked:
            ok += 1
        print(f"\n--- {sid} ---")
        print(f"  首行           : {first[:70]}")
        print(f"  含 lead 标记   : {'是' if is_lead_marked else '否'}")
        print(f"  首行是标题     : {'是' if has_heading else '否'}")
        print(f"  自造了编号     : {'是（不该出现）' if numbered else '否'}")
    print(f"\nlead 标记遵守率：{ok}/{len(CASES)}")


if __name__ == "__main__":
    main()
