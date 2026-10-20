"""`node_probe_outline` 强制工具提交的守门测试（2026-10-20，坑 82）。

背景：probe_outline 原来走 `complete_json` 手写长 JSON，解析失败
（中段 `Expecting ',' delimiter`——未转义引号/漏逗号）没有任何重试，
except 直接降级 → v15/v16 两个真实会话大纲退化成 1 节。现改走
`complete_json_forced` + `submit_outline` + `_normalize_outline_input`
+ 最多 2 次尝试（429/额度不重试，5xx/529 等后重试），与 write_audit
同形态（坑 77）。

桩挂 `LLMClient.complete_json_forced`（类级，覆盖 probe.LLM 实例），
与 test_outline_degrade / test_audit_forced 同层；不联网。

用法：
    .venv/bin/python tests/test_probe_outline_forced.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")
os.environ["RA_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.db")

from research_agent.graph.subgraphs import probe as PR  # noqa: E402
from research_agent.llm.provider import LLMClient       # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}"
          + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


GOOD = {
    "subtopics": [{"name": "材料路线", "what": "三大路线",
                   "evidence_urls": ["u://A"]},
                  {"name": "界面问题", "what": "瓶颈", "evidence_urls": []}],
    "entities": [{"name": "硫化物电解质", "type": "method"}],
    "jargon": [{"term": "固-固界面", "en_equivalent": "solid-solid interface",
                "meaning": "接触瓶颈"}],
    "controversies": [{"topic": "硫化物 vs 氧化物", "position_a": "导率高",
                       "position_b": "遇水产 H2S", "evidence_urls": ["u://A"]}],
    "common_questions": ["固态电池何时量产"],
    "info_quality": "high",
    "one_line": "三大路线并行，硫化物导率最高。",
}


def _state() -> dict:
    return {"question": "固态电池的产业化进展",
            "judged": True, "quality": "substantive",
            "light_results": [],
            "deep_pages": [{"url": "u://A", "title": "测试页",
                            "text": "正文" * 1000, "published": "",
                            "char_count": 2000}]}


class Stub:
    """挂到 LLMClient.complete_json_forced 上的捕获桩。"""

    def __init__(self, returns):
        self.returns = list(returns)   # 元素：dict 返回 或 Exception 抛
        self.calls: list[dict] = []

    def __call__(self, stage, prompt, name, schema, system=None):
        self.calls.append({"stage": stage, "prompt": prompt,
                           "name": name, "schema": schema, "system": system})
        r = self.returns.pop(0) if self.returns else {}
        if isinstance(r, Exception):
            raise r
        return r


def _run(returns) -> tuple[Stub, dict]:
    stub = Stub(returns)
    orig = LLMClient.complete_json_forced
    LLMClient.complete_json_forced = stub
    try:
        out = PR.node_probe_outline(_state())
    finally:
        LLMClient.complete_json_forced = orig
    return stub, out


def t1_happy():
    stub, out = _run([GOOD])
    sig = out.get("signals") or {}
    c = stub.calls[0]
    check("① 一次调用成功、signals 带上 subtopics",
          len(stub.calls) == 1 and len(sig.get("subtopics") or []) == 2,
          str(sig.get("subtopics"))[:80])
    check("① 工具名 submit_outline + schema 是 OUTLINE_SCHEMA",
          c["name"] == "submit_outline" and c["schema"] is PR.OUTLINE_SCHEMA)
    check("① prompt 末尾带点名工具的交卷话术",
          "调用 `submit_outline` 工具" in c["prompt"]
          and "不要在对话文本里写 JSON" in c["prompt"])
    check("① system 是 PROBE_OUTLINE", c["system"] is not None
          and "submit_outline" in c["system"])
    check("① info_quality 透传", sig.get("info_quality") == "high")


def t2_outer_wrap():
    stub, out = _run([{"outline": GOOD}])
    check("② 外层 {'outline': {...}} 解包",
          len((out.get("signals") or {}).get("subtopics") or []) == 2)
    check("② 双层包装也解",
          len((PR._normalize_outline_input(
              {"wrap": {"outline": {"subtopics": [], "info_quality": "high",
                                    "one_line": "x"}}}) or {})
              .get("entities") or []) == 0)


def t3_list_drift():
    raw = dict(GOOD)
    raw["entities"] = {"item": [GOOD["entities"][0]]}
    raw["common_questions"] = '["固态电池何时量产"]'
    raw["controversies"] = '{"item": []}'
    d = PR._normalize_outline_input(raw)
    check("③ {'item':[...]} 与 JSON 字符串数组归一",
          d is not None and len(d["entities"]) == 1
          and d["common_questions"] == ["固态电池何时量产"]
          and d["controversies"] == [])


def t4_empty_then_ok():
    bad = dict(GOOD, subtopics=[], one_line="")
    stub, out = _run([bad, GOOD])
    check("④ 空轮廓归一失败 → 重试 → 用第二次结果",
          len(stub.calls) == 2
          and len((out.get("signals") or {}).get("subtopics") or []) == 2)


def t5_both_fail_degrade():
    bad1 = {"subtopics": [], "info_quality": "high", "one_line": ""}
    bad2 = {"info_quality": "bogus", "subtopics": GOOD["subtopics"],
            "one_line": "x"}
    stub, out = _run([bad1, bad2])
    ev = [e for e in (out.get("events") or [])
          if e.get("kind") == "probe_outline"][0]
    check("⑤ 两次归一失败 → 降级 + attempts=2",
          out.get("degraded") is True and ev.get("attempts") == 2,
          f"{out.get('degraded')} {ev}")
    check("⑤ info_quality 非法值判无效（不静默回落）",
          PR._normalize_outline_input(bad2) is None)


def t6_429_no_retry():
    stub, out = _run([RuntimeError("MiniMax 429: rate limit")])
    ev = [e for e in (out.get("events") or [])
          if e.get("kind") == "probe_outline"][0]
    check("⑥ 429 不重试：1 次调用、attempts=1、降级",
          len(stub.calls) == 1 and ev.get("attempts") == 1
          and out.get("degraded") is True, str(ev))
    check("⑥ EmptyOutput 类可重试错误会重试一次",
          len(_run([RuntimeError("boom"), GOOD])[0].calls) == 2)


def t7_shape_compat():
    """归一后的 dict 与旧 complete_json 路径喂给 signals 的形状一致。"""
    stub, out = _run([GOOD])
    sig = out.get("signals") or {}
    keys = {"subtopics", "entities", "jargon", "controversies",
            "common_questions", "one_line", "info_quality"}
    check("⑦ signals 键集合与旧路径一致",
          set(sig.keys()) == keys, str(sorted(sig.keys())))
    check("⑦ 各字段类型",
          all(isinstance(sig[k], list) for k in
              ("subtopics", "entities", "jargon", "controversies",
               "common_questions"))
          and isinstance(sig["one_line"], str)
          and sig["info_quality"] in ("high", "medium", "low"))
    check("⑦ 输出契约键不变",
          {"signals", "degraded", "headline", "quality",
           "signals_from", "events"} <= set(out.keys()),
          str(sorted(out.keys())))


def main() -> int:
    print("=== probe_outline 强制工具提交 ===")
    for fn in (t1_happy, t2_outer_wrap, t3_list_drift, t4_empty_then_ok,
               t5_both_fail_degrade, t6_429_no_retry, t7_shape_compat):
        fn()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：")
        for f in FAILED:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
