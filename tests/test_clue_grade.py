"""线索级来源机制守门（离线，2026-11-04）。
用法：.venv/bin/python tests/test_clue_grade.py"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _isolate  # noqa: F401, E402

from types import SimpleNamespace
from unittest.mock import MagicMock

from research_agent.search import quality
from research_agent.search.quality import clue_grade, CLUE_MARK, page_defect
from research_agent.graph.subgraphs import write as W
from research_agent.graph import prompts

FAILED = []
def check(l, c, d=""):
    print(("✅" if c else "❌"), l, (f"—— {d}" if d else ""))
    if not c: FAILED.append(l)

CLUE_TAIL = f" | {CLUE_MARK}（文库转存，其中的具体数字无法溯源，须另有来源印证）"

def main():
    # ① clue_grade / load_rules
    check("renrendoc.com 命中", clue_grade("renrendoc.com") == "renrendoc.com")
    check("www.renrendoc.com 命中", clue_grade("www.renrendoc.com") == "renrendoc.com")
    check("m.docin.com 命中", clue_grade("m.docin.com") == "docin.com")
    check("notrenrendoc.com 不命中", clue_grade("notrenrendoc.com") is None)
    check("sohu.com 不命中", clue_grade("sohu.com") is None)
    check("None 不命中", clue_grade(None) is None)
    check("'' 不命中", clue_grade("") is None)
    # YAML 缺失回落
    old_path, old_rules = quality.RULES_PATH, quality._rules
    try:
        quality.RULES_PATH = Path("/tmp/不存在.yaml"); quality._rules = None
        check("YAML 缺失 → clue 空集", quality.load_rules()["clue"] == set())
        check("YAML 缺失 → clue_grade None", clue_grade("renrendoc.com") is None)
    finally:
        quality.RULES_PATH, quality._rules = old_path, old_rules
    check("page_defect 对 renrendoc 仍 None（只标不丢）",
          page_defect("https://renrendoc.com/x", "t", "正文") is None)

    # ② _load_numbered_evidence
    srcs = [{"id":"a","title":"文库稿","domain":"renrendoc.com","url":"u1",
             "source_type":"报告","source_date":"2024","char_count":100,"source_nature":"第三方"},
            {"id":"b","title":"正常稿","domain":"sohu.com","url":"u2",
             "source_type":"新闻","source_date":"2024","char_count":200,"source_nature":"第三方"}]
    chunks = [{"id":"c1","source_id":"a","heading_path":"h","content":"x","summary":"s","token_count":1,"on_topic":1}]
    def q(sql, p):
        return srcs if "FROM source" in sql else chunks
    repo = SimpleNamespace(q=q)
    ev = W._load_numbered_evidence(repo, "sid") if hasattr(W,"_load_numbered_evidence") else None
    if ev is None:
        from research_agent.graph.nodes import _load_numbered_evidence
        ev = _load_numbered_evidence(repo, "sid")
    l1, l2 = ev["src_lines"]
    check("clue 行含标记", CLUE_MARK in l1 and l1.endswith(CLUE_TAIL[3:] if False else CLUE_TAIL))
    check("clue 行仍以 [S1] 开头", l1.startswith("[S1] "))
    expect2 = ("[S2] 正常稿 | sohu.com | 新闻 | 2024 | 第三方 | 正文 200 字")
    check("非 clue 行逐字不变", l2 == expect2, repr(l2))
    check("编号按 rowid", l1.startswith("[S1]") and l2.startswith("[S2]"))

    # ③ _clue_only_numeric_sentences
    sl = [f"[S1] t | renrendoc.com | x | d | n | 正文 1 字{CLUE_TAIL}",
          f"[S9] t | docin.com | x | d | n | 正文 1 字{CLUE_TAIL}",
          "[S2] t | sohu.com | x | d | n | 正文 1 字"]
    f = W._clue_only_numeric_sentences
    d = "<!-- task:t1 section:sec1 -->\n市场占比达 45%[S1·2]。"
    r = f(d, sl)
    check("③a 命中且 task/section/sentence/cited 正确",
          r == [{"task":"t1","section":"sec1","sentence":"市场占比达 45%[S1·2]","cited":["S1·2"]}], str(r))
    check("③a sentence 是 draft 逐字子串", r[0]["sentence"] in d)
    check("③b 并列非线索来源 → 不命中", f("占比 45%[S1·1][S2·1]。", sl) == [])
    check("③c 限定语 → 不命中", f("据线索级材料称占 30%[S1·1]（未获独立印证）。", sl) == [])
    check("③d 纯定性 → 不命中", f("增速很快[S1·1]。", sl) == [])
    check("③e 只年份 → 不命中", f("2023年发布[S1·1]。", sl) == [])
    check("③f 带空格 [ S9·1] 命中", len(f("占 50%[ S9·1]。", sl)) == 1)
    check("③g 无线索行 → []", f("占 50%[S2·1]。", sl[2:]) == [])
    big = "<!-- task:t section:s -->\n" + "".join(f"句{i}占 {i}%[S1·1]。\n" for i in range(20))
    r20 = f(big, sl, cap=12)
    check("③h cap=12 且按序", len(r20) == 12 and r20[0]["sentence"].startswith("句0"), str(len(r20)))
    rm = f("两项数据 10% 与 20%[S1·1][S9·2]。", sl)
    check("③i 全线索多来源命中 cited 全", rm and rm[0]["cited"] == ["S1·1","S9·2"], str(rm))

    # ④ node_audit payload
    captured = {}
    W.LLM = MagicMock()
    def fake(stage, prompt, name=None, schema=None, **kw):
        captured["prompt"] = prompt
        return {"rework_check":[],"need_rework":False,"reason":"ok","findings":[],"blocked":[]}
    W.LLM.complete_json_forced = fake
    st = {"question":"q","spec":{},"violations":{},"rework_rounds":0,
          "assembled":"<!-- task:t1 section:sec1 -->\n市场占 45%[S1·1]。","src_lines":sl,
          "tasks":[],"prev_findings":[]}
    try: W.node_audit(st)
    except Exception: pass
    got = "线索级来源独撑的含数字断言" in captured.get("prompt","")
    check("④ 有候选 → prompt 含键", got)
    st2 = dict(st); st2["assembled"] = "占 45%[S1·1][S2·1]。"
    captured.clear()
    try: W.node_audit(st2)
    except Exception: pass
    check("④ 无候选 → prompt 不含键",
          "线索级来源独撑的含数字断言" not in captured.get("prompt",""))

    # ④abc 回归守卫：线索级键不得挤掉「上一轮 findings/返修轮数」键
    orig_lhf, orig_max = W._last_high_findings, W.MAX_REWORK
    W._last_high_findings = lambda sid: [{"task":"t1","section":"sec1","problem":"p","fix":"f"}]
    W.MAX_REWORK = 2   # 默认 1：rounds=1 会走 skip_final 不调模型，钉 2 才到 payload
    try:
        # a) rounds=1 + 有线索候选 → 三键齐全
        sta = dict(st); sta["rework_rounds"] = 1; sta["session_id"] = "x"
        captured.clear()
        try: W.node_audit(sta)
        except Exception: pass
        pr = captured.get("prompt","")
        check("④a rounds=1+候选：三键齐全",
              all(k in pr for k in ("上一轮你提出的问题（逐条核对，现在还在吗）",
                                    "线索级来源独撑的含数字断言",
                                    "这是第几轮返修后的草稿")), pr[:80])
        # b) rounds=1 + 无线索来源 → 有上一轮键、无线索键
        stb = dict(sta); stb["src_lines"] = sl[2:]
        captured.clear()
        try: W.node_audit(stb)
        except Exception: pass
        pr = captured.get("prompt","")
        check("④b rounds=1+无线索：有上一轮键、无线索键",
              "上一轮你提出的问题（逐条核对，现在还在吗）" in pr
              and "线索级来源独撑的含数字断言" not in pr)
        # c) rounds=0 + 有候选 → 只有线索键
        captured.clear()
        try: W.node_audit(st)
        except Exception: pass
        pr = captured.get("prompt","")
        check("④c rounds=0+候选：只线索键无上一轮键",
              "线索级来源独撑的含数字断言" in pr
              and "上一轮你提出的问题" not in pr)
    finally:
        W._last_high_findings = orig_lhf; W.MAX_REWORK = orig_max

    # ⑤ 提示词
    check("⑤ WRITE_RULES 含线索级+8.", "线索级" in prompts.WRITE_RULES and "8." in prompts.WRITE_RULES)
    check("⑤ WRITE_AUDIT 含键", "线索级来源独撑的含数字断言" in prompts.WRITE_AUDIT)
    check("⑤ FAST_ANSWER_SYSTEM 含线索级", "线索级" in prompts.FAST_ANSWER_SYSTEM)

    # ⑥ _preload_evidence：S9 段的头部行带 ⚠线索级
    seg9 = {"ref": "S9·1", "content": "正文片段内容"}
    blk = W._preload_evidence({"src_lines": sl}, [seg9])
    head = [l for l in (blk or "").split("\n") if "S9" in l]
    check("⑥ _preload_evidence S9 段头含标记",
          blk is not None and any(CLUE_MARK in l for l in head),
          repr(head[:1]))
    print("\n" + ("全部通过" if not FAILED else f"失败 {FAILED}"))
    return 1 if FAILED else 0

if __name__ == "__main__":
    raise SystemExit(main())
