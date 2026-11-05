"""实验：子 agent 写正文，「自由文本」vs「强制交卷工具 submit_section」。

要回答的问题（见 CONTEXT「写作正文污染」）：
  1. 现行做法（取最后一轮 text 当正文）在真实任务上污染率多少？
  2. 换成 final_tool=submit_section 之后：MiniMax 自愿交卷率、强制轮命中率、
     退回文本的比率、正文污染率、输出 token 变化。

输入全部取自 v13 的 checkpoint（真实任务、真实证据、真实审查意见），
不重跑检索/抽取。业务库用拷贝，llm_call 记账不会污染参考库。

用法：
    .venv/bin/python tests/experiments/exp_submit_section.py [n=5] [cases=t7,t5r,t2]
结果：stdout 汇总表 + /tmp/exp_submit/<variant>_<case>_<i>.json
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SID = "s_90b3c5d17069"
SRC_DB = ROOT / "data/refs/ra_v13_gate.db"
TMP_DB = Path("/tmp/ra_exp_submit.db")
shutil.copy(SRC_DB, TMP_DB)
os.environ["RA_DB_PATH"] = str(TMP_DB)

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer  # noqa: E402

from research_agent.db.repo import get_repo  # noqa: E402
from research_agent.graph import prompts  # noqa: E402
from research_agent.graph.subgraphs import write as W  # noqa: E402
from research_agent.llm.toolloop import run_tool_loop  # noqa: E402
from research_agent.tools.evidence import EvidenceTools  # noqa: E402

OUT = Path("/tmp/exp_submit")
OUT.mkdir(exist_ok=True)

SUBMIT_SECTION = {
    "name": "submit_section",
    "description": "正文写完后调用它交卷。正文只能放在 sections[].body 里；"
                   "对话文本里不要写正文、前言或自检。",
    "input_schema": {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "description": "你负责的小节正文，每节一项。",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "小节 id，如 sec7"},
                        "body": {"type": "string", "description":
                                 "该节的 Markdown 正文。不含节标题，不含前言、说明、"
                                 "自检或英文推理，不要用代码围栏包住。"},
                    },
                    "required": ["id", "body"],
                },
            },
            "notes": {"type": "string", "description":
                      "给审阅者的备注（引用核对、材料缺口等），不会进入报告。可省略。"},
        },
        "required": ["sections"],
    },
}

SUBMIT_ADDENDUM = (
    "\n\n**交卷方式（硬性）：** 读完材料、写完正文后，调用 `submit_section` 工具交卷。"
    "正文**只**放在 `sections[].body` 里，body 里只有可直接进入报告的 Markdown 正文："
    "不要前言（如「下面是…」）、不要自检或说明、不要英文推理、不要用代码围栏包住。"
    "对话文本里不要写正文。需要说明的事（引用核对、材料缺口）写在 `notes`。")

# ------------------------------------------------------------ 输入还原

_ser = JsonPlusSerializer()
_c = sqlite3.connect(ROOT / "data/checkpoints.db")
_row = _c.execute(
    "select type,checkpoint from checkpoints where thread_id=? and "
    "checkpoint_ns like 'write:%' order by checkpoint_id desc limit 1",
    (SID,)).fetchone()
STATE = dict(_ser.loads_typed(_row)["channel_values"])
_rows = _c.execute(
    "select type,value from writes where thread_id=? and channel='drafts' "
    "and checkpoint_ns like 'write:%'", (SID,)).fetchall()
FIRST_DRAFTS = [_ser.loads_typed(r) for r in _rows if _ser.loads_typed(r)][0]
TASKS = {t["id"]: t for t in STATE["tasks"]}


def _case(name: str) -> tuple[dict, dict | None]:
    """→ (task, revise)。t5r = t5 的返修（用审查的真实意见 + 当时带污染的旧稿）。"""
    if name == "t5r":
        f = next(x for x in STATE["audit"]["findings"] if x["task"] == "t5")
        asks = [{"小节": f["section"], "问题": f["problem"], "改法": f["fix"]}]
        task = {"id": "t5", "covers": [f["section"]], "mandate": "",
                "must_cover": [], "not_yours": [],
                "segments": TASKS["t5"]["segments"]}
        prev = FIRST_DRAFTS["t5"]["sec5"]
        return task, {"fix": W._dumps(asks), "problem": W._dumps(asks), "draft": prev}
    return TASKS[name], None


def capture(task: dict, revise: dict | None) -> tuple[str, list[dict]]:
    """跑真实的 `_run_task`，但把 run_tool_loop 换成抓取器，拿到它真正发出的
    (stage, messages)——实验的输入和生产构造逻辑一字不差。"""
    got = {}

    def fake(llm, stage, system, messages, provider, max_rounds=3, final_tool=None):
        got.update(stage=stage, messages=messages, system=system)
        return "", messages, []

    real = W.run_tool_loop
    W.run_tool_loop = fake
    try:
        W._run_task(STATE, task, revise)
    finally:
        W.run_tool_loop = real
    assert got["system"] == prompts.WRITE_RULES
    return got["stage"], got["messages"]


# ------------------------------------------------------------ 污染检测

_PRE = re.compile(
    r"^(下面|以下|现在|好的|我(先|来|已|看|将|重写|补|掌握|核对|把)|"
    r"Now|Let me|I'll|I've|I have|Here|OK|Okay|Sure|First)", re.I)
_META = re.compile(
    r"(Citation check|Constraints satisfied|Let me verify|self-check|"
    r"I've drafted|自检|核对清单|Other notable points)", re.I)
_PSEUDO = re.compile(r"minimax\]\[|\]<\]minimax|<tool_call|<invoke|read_segments")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def contam(body: str) -> list[str]:
    flags = []
    lines = [x for x in (body or "").split("\n") if x.strip()]
    if not lines:
        return ["empty"]
    if _PSEUDO.search(body):
        flags.append("pseudo_tool")
    first = next((x.strip() for x in lines if x.strip() not in ("---", "```markdown", "```")), "")
    if lines[0].strip().startswith("```"):
        flags.append("fence")
    if _PRE.match(first):
        flags.append("preamble")
    if re.match(r"^#{1,2}\s*(sec\d+|第[一二三四五六七八九十]+节)", first, re.I):
        flags.append("task_header")
    if _META.search(body):
        flags.append("trailing_meta")
    en = [x for x in lines if len(x) > 60 and not x.lstrip().startswith("|")
          and len(_CJK.findall(x)) < len(x) * 0.05]
    if len(en) >= 3:
        flags.append(f"english_block({len(en)})")
    if len(body) < 1500:
        flags.append("too_short")
    return flags


# ------------------------------------------------------------ 执行

class Proxy:
    """记录每次 complete_with_tools 调用的形状，其余属性透传。"""

    def __init__(self, llm):
        self._llm, self.calls = llm, []

    def __getattr__(self, n):
        return getattr(self._llm, n)

    def complete_with_tools(self, stage, system, messages, tools, tool_choice=None,
                            thinking=None):
        t0 = time.time()
        kw = {} if thinking is None else {"thinking": thinking}
        r = self._llm.complete_with_tools(stage, system, messages, tools,
                                          tool_choice=tool_choice, **kw)
        bl = r.get("blocks") or []
        self.calls.append({
            "tools": [t.get("name") for t in tools],
            "forced": tool_choice, "thinking": thinking,
            "blocks": [(b.get("type"), b.get("name")) for b in bl],
            "out_tokens": int((r.get("usage") or {}).get("output_tokens") or 0),
            "s": round(time.time() - t0, 1)})
        return r


VARIANTS = os.environ.get("VARIANTS", "AB")

# C：B + 工具路径输出上限抬到 16384（guard.max_output_tokens 本来就写着 16384，
#    但工具路径不走 guard）。D：B + 强制交卷那一轮关思考（payload 层改写）。
MODE = os.environ.get("EXP_MODE", "")
if MODE == "C":
    for _st in ("write_section", "write_revise"):
        _cfg = W.LLM.registry.stage(_st)
        _cfg.max_output_tokens *= 2
elif MODE == "D":
    _orig_once = W.LLM._minimax_once

    def _patched_once(cfg, url, headers, payload):
        if payload.get("tool_choice"):
            payload["thinking"] = {"type": "disabled"}
        return _orig_once(cfg, url, headers, payload)

    W.LLM._minimax_once = _patched_once


CAPTURED: dict[str, tuple[str, list[dict]]] = {}


def one(variant: str, case: str, i: int) -> dict:
    stage, messages = CAPTURED[case]
    px = Proxy(W.LLM)
    tools = EvidenceTools(get_repo(), SID)
    t0 = time.time()
    rec = {"variant": variant, "case": case, "i": i, "stage": stage}
    try:
        if variant == "A":
            text, _, _ = run_tool_loop(px, stage, prompts.WRITE_RULES, messages,
                                       tools, max_rounds=2)
            body, via = text, "text"
        else:
            text, _, _ = run_tool_loop(px, stage, prompts.WRITE_RULES + SUBMIT_ADDENDUM,
                                       messages, tools, max_rounds=2,
                                       final_tool=SUBMIT_SECTION)
            body, via = text, "?"
            try:
                d = json.loads(text)
                secs = d.get("sections") or []
                body = "\n\n".join(s.get("body", "") for s in secs)
                rec["notes_len"] = len(d.get("notes") or "")
                forced = any(c["forced"] and ("tool_use", "submit_section") in c["blocks"]
                             for c in px.calls)
                via = "forced" if forced else "voluntary"
            except Exception:
                via = "text_fallback"
    except Exception as e:
        rec.update(error=str(e)[:200], via="error", flags=["error"])
        return rec
    rec.update(via=via, chars=len(body), flags=contam(body),
               out_tokens=sum(c["out_tokens"] for c in px.calls),
               calls=px.calls, secs=round(time.time() - t0, 1), body=body)
    (OUT / f"{variant}_{case}_{i}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return rec


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    cases = (sys.argv[2] if len(sys.argv) > 2 else "t7,t5r,t2").split(",")
    for c in cases:          # 串行抓取：capture 会给模块级函数打补丁，不能并发
        CAPTURED[c] = capture(*_case(c))
    jobs = [(v, c, i) for c in cases for v in VARIANTS for i in range(n)]
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(lambda j: one(*j), jobs))
    print(f"\n{'case':5}{'var':4}{'n':3}{'clean':6}{'via':44}{'flags':60}{'out_tok':9}{'secs':6}")
    for c in cases:
        for v in VARIANTS:
            rs = [r for r in res if r["case"] == c and r["variant"] == v]
            clean = sum(1 for r in rs if not r.get("flags"))
            via, fl = {}, {}
            for r in rs:
                via[r["via"]] = via.get(r["via"], 0) + 1
                for f in r.get("flags") or []:
                    f = re.sub(r"\(\d+\)", "", f)
                    fl[f] = fl.get(f, 0) + 1
            ot = [r["out_tokens"] for r in rs if "out_tokens" in r]
            sc = [r["secs"] for r in rs if "secs" in r]
            print(f"{c:5}{v:4}{len(rs):<3}{clean:<6}{str(via):44}{str(fl):60}"
                  f"{(sum(ot) // len(ot) if ot else 0):<9}{(round(sum(sc) / len(sc)) if sc else 0):<6}")


if __name__ == "__main__":
    main()
