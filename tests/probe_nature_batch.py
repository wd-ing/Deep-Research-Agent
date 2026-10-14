"""探针（回归守卫）：`_run_nature_batch` 不许再整批丢来源。

**它守的是什么**：2026-10-14 发现来源性质判定**每轮只有第 0 批被判**，
其余整批静默丢弃——v5 实测 41 个来源丢 21 个（占 51%）。根因是
`_render_nature` 渲染给模型看的下标是**全局**下标（`items` 里的位置），
`_run_nature_batch` 却拿它去比 `len(batch)`（**切片**长度），
于是第 2 批起必然越界、被 `continue` 掉。

后果不只是来源库里多一个「未判」组：这些来源的 `nature` 也会喂进
写作提示词（写作时看到的是「性质未判」），而它们恰好是段数最多的一批。
界面上完全看不出来——不抛异常、不计数、不进 `failed_batches`。

**怎么守的**：把 `LLM.complete_json` 换成一个**完美桩**——批次里每一条
都原样还回去，`source_index` 全对、`source_nature` 全合法。这样模型侧
不可能出任何错；此时「未判定 > 0」就一定是代码自己的锅。

另有一段故意让桩只回一半，验证缺口确实被**如实报告**（返回 + 未判定 == 送进）。

跑：.venv/bin/python tests/probe_nature_batch.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research_agent.graph.subgraphs import extract_batch as eb


def make_items(n: int) -> list[dict]:
    return [
        {
            "source_id": f"src_{i:03d}",
            "title": f"标题 {i}",
            "url": f"https://example.com/{i}",
            "text": f"正文 {i}",
            "segments": [{"summary": f"概括 {i}"}],
        }
        for i in range(n)
    ]


class PerfectLLM:
    """完美桩：把批次里每一条都原样还回去，下标全对。"""

    def __init__(self):
        self.calls = 0

    def complete_json(self, _stage, _user, system=None):
        self.calls += 1
        # 从渲染出的 blob 里读回模型实际看到的下标——不能自己编，
        # 否则验的就不是「模型看到什么」而是「我以为模型看到什么」。
        import re
        idx = [int(m) for m in re.findall(r"===== 来源 (\d+) =====", _user)]
        return {"sources": [{"source_index": i, "source_nature": "media",
                             "nature_reason": "桩"} for i in idx]}


def run(n_sources: int) -> None:
    eb.LLM = PerfectLLM()
    items = make_items(n_sources)
    nature_items = eb._make_nature_items(items)
    batches = eb.make_batches(nature_items)

    print(f"\n=== {n_sources} 个来源，NATURE_BATCH={eb.NATURE_BATCH} "
          f"→ {len(batches)} 批 ===")
    total_in = total_out = total_miss = 0
    for bi, batch in enumerate(batches):
        out, missing = eb._run_nature_batch(batch)
        total_in += len(batch)
        total_out += len(out)
        total_miss += len(missing)
        flag = "  ⚠️ 整批丢失" if missing == [b["source_id"] for b in batch] else ""
        print(f"  批 {bi}: 送进去 {len(batch)} 条 → 返回 {len(out)} 条"
              f"，未判定 {len(missing)} 条{flag}")
    print(f"  合计：送进 {total_in}，判到 {total_out}，"
          f"未判定 {total_miss}"
          f"（{100*total_miss/total_in:.0f}%）")
    # 回归守卫：完美桩下不该有任何缺口。这条断言就是当初那个 bug 的守门人。
    assert total_miss == 0, (
        f"完美桩下仍丢了 {total_miss} 条——下标体系又分叉了。"
        f"检查 _render_nature 渲染的下标与 _run_nature_batch 校验的下标"
        f"是不是同一套")


class DroppingLLM:
    """故意漏掉一半 + 给一个越界下标。模拟模型输出不完整。"""

    def __init__(self):
        self.calls = 0

    def complete_json(self, _stage, _user, system=None):
        self.calls += 1
        import re
        idx = [int(m) for m in re.findall(r"===== 来源 (\d+) =====", _user)]
        keep = [i for k, i in enumerate(idx) if k % 2 == 0]
        keep.append(999)                      # 越界下标，必须被丢掉而不是崩
        return {"sources": [{"source_index": i, "source_nature": "media",
                             "nature_reason": "桩"} for i in keep]}


def check_shortfall() -> None:
    """模型只回一半时，缺口必须**报出来**，而不是静默。"""
    print("\n=== 模型只回一半 + 一个越界下标 ===")
    eb.LLM = DroppingLLM()
    items = make_items(9)
    batches = eb.make_batches(eb._make_nature_items(items))
    out, missing = eb._run_nature_batch(batches[0])
    sent = [b["source_id"] for b in batches[0]]
    print(f"  送进 {len(sent)} 条 → 返回 {len(out)} 条，报告未判定 {len(missing)} 条")
    print(f"  返回的 source_id : {[o['source_id'] for o in out]}")
    print(f"  报告的未判定    : {missing}")
    ok = len(out) + len(missing) == len(sent) and not (set(out and
         [o['source_id'] for o in out]) & set(missing))
    print(f"  {'✅ 守恒：返回 + 未判定 == 送进，且两者不重叠' if ok else '❌ 对不上'}")
    assert ok, "缺口没被如实报告"
    print("  越界下标 999 被跳过、没有 KeyError ✅")


def main() -> None:
    print(__doc__.strip().splitlines()[0])
    print("\n用完美桩（模型下标全对、性质全合法）跑当前代码——")
    print("此时还能丢的只有本函数自己的校验，所以「未判定 > 0」就一定是代码的锅。")
    for n in (8, 20, 24, 41):
        run(n)
    check_shortfall()
    print("\n结论：")
    print("  三处下标（渲染给模型看的、校验的、batch[i] 取值的）现在是同一套，")
    print("  所以第 2 批起不再丢。原先是拿**全局**下标去比**切片**长度，")
    print("  第 2 批起必然越界 → `continue` 静默丢弃，")
    print("  不抛异常、不计数、不进 failed_batches。")
    print("  缺口现在由 `missing` 显式带出去 → 事件 `unjudged` → `nodes.py`")
    print("  的 `unjudged_sources` → 工作流视图的 ⚠️ 明细。下次再发生不会是静默的。")


if __name__ == "__main__":
    main()
