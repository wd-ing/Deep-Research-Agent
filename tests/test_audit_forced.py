"""write_audit 迁 M3.1-Flash + 强制工具 submit_audit 的守门测试（离线）。

2026-10-19 加（坑 77）。验三件事的行为：
  · `_normalize_audit_input` 把 M3.1 偏离 schema 的工具入参归一回来
    （套键 / {"item":[...]} / need_rework 为 str 或带 "$text" 的 dict）；
  · `node_audit` 改走 `complete_json_forced` + 归一化失败重试一次；
  · M3.1 的配置护栏（关不掉思考的两个写法都被 validate 拦死）。

fixture 是真实样本：tests/data/audit_tool_inputs.json 里的三个
tool_input 抽自 /tmp/exp_audit3（MiniMax-M3.1-Flash{,-Preview} 实测）。
不依赖 /tmp——那份目录是会丢的。

⚠️ 桩挂在生产同一层：`W.LLM.complete_json_forced`（node_audit 调的就是它）、
`llm._minimax_once`（payload 定型处）；fixture 值是死数据不漂。

用法：
    .venv/bin/python tests/test_audit_forced.py
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MINIMAX_API_KEY", "stub-not-used")

from research_agent.llm import provider as P            # noqa: E402
from research_agent.graph.subgraphs import write as W   # noqa: E402
from research_agent.graph import prompts                 # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}" + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


FIXTURES = json.loads((ROOT / "tests/data/audit_tool_inputs.json")
                      .read_text(encoding="utf-8"))


# --------------------------------------------------------------- ① 归一化

def t_normalize():
    d = W._normalize_audit_input(FIXTURES["clean"])
    check("① clean 样本原样通过",
          d is not None and d["need_rework"] is True
          and len(d["findings"]) == 12 and len(d["blocked"]) == 5,
          "" if d else "None")

    d = W._normalize_audit_input(FIXTURES["wrapped_items"])
    check("① 套键解包 + {\"item\"} 展平 + \"true\"→True",
          d is not None and d["need_rework"] is True
          and len(d["findings"]) == 16 and len(d["blocked"]) == 5,
          "" if d else "None")

    # ⚠️ 这个样本的阻断列表键名漂成了 `blocker` 不是 `blocked`——
    # 归一化不设别名白名单（不猜模型会飘成什么键），缺 schema 键
    # 一律判无效让上层重试：宁可多花一次调用，也不能让整栏静默丢。
    check("① blocker≠blocked：判无效（重试兜住，不丢栏）",
          W._normalize_audit_input(FIXTURES["needrework_dict"]) is None)

    # 无效输入一律 None：不该被当成「审查通过」放行。
    for tag, raw in [
        ("{}", {}),
        ("{\"foo\":1}", {"foo": 1}),
        ("缺 findings", {"need_rework": False, "reason": "ok",
                         "blocked": []}),
        ("缺 blocked", {"need_rework": False, "reason": "ok",
                        "findings": []}),
        ("缺 need_rework", {"reason": "ok", "findings": [],
                            "blocked": []}),
        ("need_rework 无法转 bool", {"need_rework": {"x": 1},
                                    "findings": [], "blocked": []}),
        ("非 dict", [1, 2]),
        ("坏 JSON 字符串", "{不是 json"),
    ]:
        check(f"① 无效输入判 None：{tag}",
              W._normalize_audit_input(raw) is None,
              repr(raw)[:60])

    # blocked 的「形状歪但键在」仍合法：{"item":[...]} 展平、空串归 []。
    d = W._normalize_audit_input({"need_rework": False, "reason": "ok",
                                  "findings": [],
                                  "blocked": {"item": [{"task": "t1",
                                                        "kind": "no_material",
                                                        "issue": "x"}]}})
    check("① blocked={\"item\":[...]} 展平后合法",
          d is not None and len(d["blocked"]) == 1, str(d))
    d = W._normalize_audit_input({"need_rework": False, "reason": "ok",
                                  "findings": [], "blocked": ""})
    check("① blocked=\"\" 归 [] 仍合法",
          d is not None and d["blocked"] == [], str(d))

    d = W._normalize_audit_input({"need_rework": "false", "reason": "ok",
                                  "findings": [], "blocked": []})
    check("① \"false\" 字符串 → False，合法的「审查通过」放行",
          d is not None and d["need_rework"] is False, str(d))

    # 字符串输入（模型把整个 JSON 塞进字符串里）与双层套娃都解得开。
    d = W._normalize_audit_input(json.dumps(FIXTURES["clean"],
                                            ensure_ascii=False))
    check("① JSON 字符串输入可解",
          d is not None and len(d["findings"]) == 12)
    d = W._normalize_audit_input({"outer": {"audit": FIXTURES["clean"]}})
    check("① 双层包装可解",
          d is not None and len(d["findings"]) == 12, str(type(d)))


# --------------------------------------------------------------- ② node_audit

class StubAuditLLM:
    """挂在 `complete_json_forced` 层：按脚本返回 raw 入参或抛异常。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen: list[dict] = []

    def complete_json_forced(self, stage, prompt, name, schema,
                             system=None):
        self.seen.append({"stage": stage, "prompt": prompt,
                          "name": name, "schema": schema, "system": system})
        r = self.script[min(len(self.seen) - 1, len(self.script) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


def _audit_state() -> dict:
    # t1~t8 全挂上——样本 findings 的 high 都在这些 id 上，
    # 缺了 id 的 finding 不进 targets，「need_rework=True」就出不来。
    return {"question": "q", "spec": {"thesis": "T"},
            "assembled": "草稿正文", "violations": {},
            "session_id": None,          # _trace/_push_audit_history 见 None 直退
            "tasks": [{"id": f"t{i}", "covers": [f"sec{i}"],
                       "segments": [], "must_cover": []}
                      for i in range(1, 9)]}


def _run_audit(script):
    stub = StubAuditLLM(script)
    orig = W.LLM.complete_json_forced
    W.LLM.complete_json_forced = stub.complete_json_forced
    try:
        out = W.node_audit(_audit_state())
    finally:
        W.LLM.complete_json_forced = orig
    return stub, out


def t_node_audit():
    # (a) wrapped 样本一次成功：归一化后照常进下游。
    stub, out = _run_audit([FIXTURES["wrapped_items"]])
    ev = (out.get("events") or [{}])[0]
    check("② 一次成功：need_rework True 且 targets 非空",
          out["audit"].get("need_rework") is True
          and len(out["audit"].get("targets") or []) > 0,
          f"targets={len(out['audit'].get('targets') or [])}")
    check("② 事件 attempts==1",
          ev.get("kind") == "write_audit" and ev.get("attempts") == 1,
          str(ev)[:120])
    s0 = stub.seen[0]
    check("② 强制工具接线：name/schema/system/prompt",
          s0["name"] == "submit_audit" and s0["schema"] is W.AUDIT_SCHEMA
          and s0["system"] is prompts.WRITE_AUDIT
          and "submit_audit" in s0["prompt"],
          f"name={s0['name']} prompt尾={s0['prompt'][-40:]!r}")

    # (b) 第一次无效结构、第二次合法 → 重试一次救回。
    stub, out = _run_audit([{"foo": 1}, FIXTURES["clean"]])
    ev = (out.get("events") or [{}])[0]
    check("② 无效→重试成功：2 次调用、attempts==2",
          len(stub.seen) == 2 and ev.get("attempts") == 2
          and ev.get("kind") == "write_audit",
          f"{len(stub.seen)} {ev.get('attempts')}")

    # (c) 两次都无效 → 走 write_audit_error，不阻断。
    stub, out = _run_audit([{"foo": 1}, [1, 2]])
    ev = (out.get("events") or [{}])[0]
    check("② 两次无效→write_audit_error（带尝试次数）",
          len(stub.seen) == 2 and ev.get("kind") == "write_audit_error"
          and "2" in str(ev.get("error")),
          str(ev)[:140])
    check("② 失败时 audit 兜底形状不变",
          out["audit"].get("need_rework") is False
          and out["audit"].get("findings") == [])

    # (d) MiniMax 429/HTTP 类错误**不重试**（额度问题重发没意义）。
    stub, out = _run_audit([RuntimeError("MiniMax 429: Token Plan 速率限制")])
    ev = (out.get("events") or [{}])[0]
    check("② MiniMax 4xx 不重试（1 次调用→error）",
          len(stub.seen) == 1 and ev.get("kind") == "write_audit_error",
          f"{len(stub.seen)} {ev.get('kind')}")

    # (e) EmptyOutput（参数截断/空返回）值得重试。
    stub, out = _run_audit([P.EmptyOutput("参数被截断"),
                            FIXTURES["clean"]])
    ev = (out.get("events") or [{}])[0]
    check("② EmptyOutput→重试成功（2 次调用）",
          len(stub.seen) == 2 and ev.get("kind") == "write_audit"
          and ev.get("attempts") == 2, f"{len(stub.seen)} {ev.get('kind')}")

    # (f) MiniMax 529 过载 → 瞬时错误：等 _TRANSIENT_WAIT_S 后重试。
    #     （真实跑里二轮审查撞 529 被判不重试直接兜底、返修稿没复审
    #     就收工——服务端明说「请稍后重试」。）离线把等待置 0、sleep 桩掉。
    o_wait, o_sleep = W._TRANSIENT_WAIT_S, W.time.sleep
    slept: list[float] = []
    W._TRANSIENT_WAIT_S = 0
    W.time.sleep = lambda s: slept.append(s)
    try:
        stub, out = _run_audit([RuntimeError("MiniMax 529: overloaded_error"),
                                FIXTURES["clean"]])
        ev = (out.get("events") or [{}])[0]
        check("② 529→等后重试成功（2 次调用、睡 1 次）",
              len(stub.seen) == 2 and ev.get("kind") == "write_audit"
              and ev.get("attempts") == 2 and slept == [0],
              f"{len(stub.seen)} {ev.get('kind')} slept={slept}")

        # (g) 连续过载 → 最多 3 次尝试后兜底（不无限重发）。
        slept.clear()
        stub, out = _run_audit([RuntimeError("MiniMax 529: a"),
                                RuntimeError("MiniMax 529: b"),
                                RuntimeError("MiniMax 529: c")])
        ev = (out.get("events") or [{}])[0]
        check("② 连续 529 → 3 次尝试后兜底（睡 2 次）",
              len(stub.seen) == 3 and ev.get("kind") == "write_audit_error"
              and "3" in str(ev.get("error")) and slept == [0, 0],
              f"{len(stub.seen)} {str(ev.get('error'))[:60]} slept={slept}")
    finally:
        W._TRANSIENT_WAIT_S = o_wait
        W.time.sleep = o_sleep


def t_transient_http():
    check("_transient_http：529/503/500、带前缀的 reason 都命中",
          W._transient_http(RuntimeError("MiniMax 529: overloaded"))
          and W._transient_http("调用失败：MiniMax 503")
          and W._transient_http("MiniMax 500")
          and W._transient_http("MiniMax 504 超时"))
    check("_transient_http：429/4xx/ConfigError/EmptyOutput 不命中",
          not W._transient_http("MiniMax 429: rate limit")
          and not W._transient_http("MiniMax 400")
          and not W._transient_http(P.ConfigError("缺 key"))
          and not W._transient_http(P.EmptyOutput("截断"))
          and not W._transient_http(None))


# --------------------------------------------------------------- ③ 配置与 payload

def t_config_and_payload():
    reg = P.ModelRegistry()
    cfg = reg.stage("write_audit")
    check("③ write_audit 指向 M3.1-Flash / minimax 协议",
          cfg.provider == "minimax" and cfg.model == "MiniMax-M3.1-Flash",
          f"{cfg.provider}/{cfg.model}")
    check("③ effort 组合 = reasoning low + thinking_effort low",
          cfg.reasoning_effort == "low" and cfg.thinking_effort == "low",
          f"{cfg.reasoning_effort!r}/{cfg.thinking_effort!r}")
    check("③ max_output_tokens=24576、timeout 420",
          cfg.max_output_tokens == 24576 and cfg.timeout_s == 420.0,
          f"{cfg.max_output_tokens}/{cfg.timeout_s}")

    # 兄弟环节必须还在 M3 上——它们靠 forced_thinking: disabled 吃饭。
    for st in ("write_section", "write_revise"):
        check(f"③ {st} 没被误换，仍是 MiniMax-M3",
              reg.stage(st).model == "MiniMax-M3", reg.stage(st).model)

    # payload 定型：M3.1 的强制交卷发 adaptive + output_config.effort=low。
    llm = P.LLMClient()
    sent: list[dict] = []

    def fake_once(cfg, url, headers, payload):
        sent.append(payload)
        return 200, "", {"content": [{"type": "tool_use", "id": "u1",
                                      "name": "submit_audit",
                                      "input": FIXTURES["clean"]}],
                         "usage": {}}
    llm._minimax_once = fake_once
    try:
        llm._minimax_tools(
            cfg, "sys", [{"role": "user", "content": "p"}],
            [{"name": "submit_audit", "description": "d",
              "input_schema": W.AUDIT_SCHEMA}],
            tool_choice="submit_audit")
        p0 = sent[0]
        check("③ payload：model/max_tokens/stream",
              p0.get("model") == "MiniMax-M3.1-Flash"
              and p0.get("max_tokens") == 24576
              and p0.get("stream") is True,
              f"{p0.get('model')}/{p0.get('max_tokens')}")
        check("③ payload：adaptive + effort low（不是 disabled）",
              p0.get("thinking") == {"type": "adaptive"}
              and p0.get("output_config") == {"effort": "low"},
              f"{p0.get('thinking')} {p0.get('output_config')}")
        check("③ payload：tool_choice 指名 submit_audit",
              p0.get("tool_choice") == {"type": "tool",
                                       "name": "submit_audit"},
              str(p0.get("tool_choice")))
    finally:
        llm.close()


def t_validate_m31():
    """M3.1 的两个「关思考」写法必须被拦；M3 原行为不变。"""
    base = P.ModelRegistry().stage("write_audit")

    bad = copy.copy(base)
    bad.stage = "_m31a"
    bad.reasoning_effort = None        # = 发 disabled → M3.1 上 400
    try:
        bad.validate()
        check("③ M3.1 + reasoning_effort 缺省被拦", False, "没抛")
    except P.ConfigError:
        check("③ M3.1 + reasoning_effort 缺省被拦", True)

    bad = copy.copy(base)
    bad.stage = "_m31b"
    bad.forced_thinking = "disabled"   # 强制轮关思考 → 同样 400
    try:
        bad.validate()
        check("③ M3.1 + forced_thinking=disabled 被拦", False, "没抛")
    except P.ConfigError:
        check("③ M3.1 + forced_thinking=disabled 被拦", True)

    bad = copy.copy(base)
    bad.stage = "_m31c"
    bad.thinking_effort = "bogus"
    try:
        bad.validate()
        check("③ M3.1 + thinking_effort 非法被拦", False, "没抛")
    except P.ConfigError:
        check("③ M3.1 + thinking_effort 非法被拦", True)

    # M3（不带 .1）原行为一点不变：缺省 reasoning_effort 照常放行。
    ok = copy.copy(base)
    ok.stage = "_m3"
    ok.model = "MiniMax-M3"
    ok.reasoning_effort = None
    ok.thinking_effort = None
    try:
        ok.validate()
        check("③ M3 原行为不变（缺省照常放行）", True)
    except P.ConfigError as e:
        check("③ M3 原行为不变（缺省照常放行）", False, str(e)[:80])


def main() -> int:
    print("=== ① _normalize_audit_input ===")
    t_normalize()
    print("\n=== ② node_audit 强制工具 + 重试 ===")
    t_node_audit()
    t_transient_http()
    print("\n=== ③ 配置 / payload / M3.1 护栏 ===")
    t_config_and_payload()
    t_validate_m31()
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
