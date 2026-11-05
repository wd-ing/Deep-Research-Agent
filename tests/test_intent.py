"""意图识别离线测试（坑 98）。不触网：LLM 全程 monkeypatch 假实现。
运行：RA_DB_PATH=/tmp/x_t.db RA_CKPT_DB=/tmp/x_c.db .venv/bin/python tests/test_intent.py
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import _isolate  # noqa: F401  必须最先：隔离 DB 路径 + RA_INTENT 默认 0

FAILED = []


def check(label, cond, detail=""):
    print(("✅" if cond else "❌"), label, (f"—— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def main():
    from research_agent import intent as I
    from research_agent.llm.provider import LLMClient
    import intent_cases as TC

    # ① 预筛无违规（对全用例）
    v = TC.prefilter_violations(I.prefilter)
    check("prefilter_violations 为空", v == [], str(v[:5]))

    # ② 所有 research 用例预筛必须返回 None（不许被规则拦）
    bad = [c.q for c in TC.CASES if "research" in c.accept and I.prefilter(c.q) is not None]
    check("research 用例预筛全 None", bad == [], str(bad[:5]))

    # ③ normalize 边界
    cases = [("  Hi!  ", "hi"), ("你好呀~", "你好呀"), ("HELLO!!!", "hello"),
             ("固态电池 🤔", "固态电池"), ("thanks", "thanks")]
    for raw, want in cases:
        check(f"normalize {raw!r}→{want!r}", I.normalize(raw) == want, I.normalize(raw))
    check("normalize('   ')=''", I.normalize("   ") == "")
    check("normalize('嗯？')='嗯'", I.normalize("嗯？") == "嗯")

    # ③b _SYSTEM 与定稿文件逐字一致（坑 98：少了半句都不能算）
    ref = (ROOT / "tests/data/intent_system_prompt.txt").read_text("utf-8").strip()
    check("_SYSTEM 逐字=定稿文件", I._SYSTEM.strip() == ref)

    # ④ _parse：合法/多余文字/枚举外/空/截断/异常
    check("_parse 合法", I._parse('{"intent":"chitchat"}') == "chitchat")
    check("_parse 多余文字", I._parse('好的 {"intent":"off_scope"} 完了') == "off_scope")
    check("_parse 枚举外→None", I._parse('{"intent":"bogus"}') is None)
    check("_parse 空串→None", I._parse("") is None)
    check("_parse 截断→None", I._parse('{"intent":"rese') is None)
    check("_parse 非JSON→None", I._parse("就是随便说说") is None)

    # ⑤ 故障注入：抛错/垃圾/超时 → 全 research + fail_open（对非预筛输入）
    os.environ["RA_INTENT"] = "1"
    orig_complete = LLMClient.complete
    try:
        for name, fake in [
            ("抛错", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))),
            ("垃圾", lambda *a, **k: "这不是JSON"),
            ("超时", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("t"))),
        ]:
            LLMClient.complete = fake
            n_fail = 0
            for q in ["固态电池产业化进展", "Rust 与 Go 对比", "asdfgh"]:
                r = I.classify_intent(q)
                if not (r.intent == "research" and r.source == "fail_open"):
                    n_fail += 1
            check(f"故障注入[{name}] 全 fail_open", n_fail == 0)
    finally:
        LLMClient.complete = orig_complete

    # ⑥ RA_INTENT=0 → disabled 且不调 LLM
    called = [0]
    LLMClient.complete = lambda *a, **k: (called.__setitem__(0, called[0]+1), '{"intent":"research"}')[1]
    try:
        os.environ["RA_INTENT"] = "0"
        r = I.classify_intent("asdfgh")
        check("RA_INTENT=0 → disabled", r.intent == "research" and r.source == "disabled",
              f"{r.intent}/{r.source}")
        check("disabled 不调 LLM", called[0] == 0)
    finally:
        os.environ["RA_INTENT"] = "1"
        LLMClient.complete = orig_complete

    # ⑦ 超长输入只送头300+尾300
    seen = {}
    def cap(self, stage, prompt, system=None, json_mode=False, record=True):
        seen["prompt"] = prompt; seen["record"] = record; seen["system"] = system
        return '{"intent":"research"}'
    LLMClient.complete = cap
    try:
        long_q = "X" * 400 + "固态电池" + "Y" * 400
        I.classify_intent(long_q)
        inner = seen["prompt"]
        body = inner[len("<input>"):-len("</input>")]
        check("超长输入截 300+300", len(body) == 601, f"len={len(body)}")
        check("尾部在", body.endswith("Y" * 300))
        check("头部在", body.startswith("X" * 300))
        check("record=False 传给 complete", seen["record"] is False, str(seen.get("record")))
        check("system 用定稿提示词", seen["system"] == I._SYSTEM)
    finally:
        LLMClient.complete = orig_complete

    # ⑨ intent 调用不写 llm_call（record=False 成功+失败两路径）
    from research_agent.db.repo import get_repo
    repo = get_repo()
    n0 = repo.q1("SELECT COUNT(*) c FROM llm_call")["c"]
    def okfake(self, stage, prompt, system=None, json_mode=False, record=True):
        # 模拟 complete 成功路径——真实 complete 里 record=True 时 _record 会写行
        return '{"intent":"research"}'
    # 直接验证 provider 层：record=False 不记，缺省记
    client = LLMClient()
    orig_ark = client._opencode
    def fake_opencode(cfg, prompt, system, json_mode):
        return '{"intent":"research"}', {"input_tokens": 5, "output_tokens": 2}
    try:
        # 需要一个 ark provider 的 stage；intent stage 是 mini(ark_mini)？
        cfg = client.registry.stage("intent")
        prov_method = {"ark": client._opencode, "minimax": client._minimax,
                       "gemini": client._gemini}.get(cfg.provider)
        if cfg.provider in ("ark", "ark_mini") or cfg.provider in ("opencode","xj189","internai","ark_mini"):
            client._opencode = fake_opencode
        else:
            prov_method = None
        client.complete("intent", "<input>固态电池</input>", record=False)
        n1 = repo.q1("SELECT COUNT(*) c FROM llm_call")["c"]
        check("record=False 成功路径不写 llm_call", n1 == n0, f"{n0}→{n1}")
        # 失败路径
        def bad_opencode(cfg, prompt, system, json_mode):
            raise RuntimeError("boom")
        client._opencode = bad_opencode
        try:
            client.complete("intent", "<input>x</input>", record=False)
        except Exception:
            pass
        n2 = repo.q1("SELECT COUNT(*) c FROM llm_call")["c"]
        check("record=False 失败路径不写 llm_call", n2 == n0, f"{n0}→{n2}")
        # 缺省 record=True 行为不变：成功 1 行
        client._opencode = fake_opencode
        client.complete("intent", "<input>x</input>")
        n3 = repo.q1("SELECT COUNT(*) c FROM llm_call")["c"]
        check("缺省 record 成功写 1 行", n3 == n0 + 1, f"{n0}→{n3}")
        client._opencode = bad_opencode
        try:
            client.complete("intent", "<input>x</input>")
        except Exception:
            pass
        n4 = repo.q1("SELECT COUNT(*) c FROM llm_call")["c"]
        check("缺省 record 失败也写行（与改动前一致）", n4 == n0 + 2, f"{n3}→{n4}")
    finally:
        client._opencode = orig_ark

    # ⑧ TestClient 端点
    from fastapi.testclient import TestClient
    import research_agent.server as S
    c = TestClient(S.app)
    os.environ["RA_INTENT"] = "1"
    n_sess0 = repo.q1("SELECT COUNT(*) c FROM session")["c"]

    # 非 research → 409，会话表不变
    LLMClient.complete = lambda *a, **k: '{"intent":"chitchat"}'
    try:
        r = c.post("/api/sessions", json={"question": "给我讲个笑话", "mode": "fast"})
        check("非research POST /api/sessions → 409", r.status_code == 409,
              f"{r.status_code} {r.text[:120]}")
        n_sess1 = repo.q1("SELECT COUNT(*) c FROM session")["c"]
        check("409 不建会话", n_sess1 == n_sess0, f"{n_sess0}→{n_sess1}")
        d = r.json()["detail"]
        check("409 detail.code=not_research", d.get("code") == "not_research", str(d))
        check("409 带 reply+suggestions",
              bool(d.get("reply")) and len(d.get("suggestions") or []) == 3)

        # force_research → 建会话
        r2 = c.post("/api/sessions", json={"question": "给我讲个笑话",
                                           "mode": "fast", "force_research": True})
        check("force_research → 201/200", r2.status_code in (200, 201), r2.status_code)

        # /api/intent 判定 + /api/sessions 缓存命中：LLM 只调一次
        calls = [0]
        def count(self, stage, prompt, **kw):
            calls[0] += 1
            return '{"intent":"research"}'
        LLMClient.complete = count
        S._intent_cache.clear()
        c.post("/api/intent", json={"question": "固态电池"})
        c.post("/api/sessions", json={"question": "固态电池", "mode": "fast"})
        check("intent→sessions 缓存命中 LLM=1", calls[0] == 1, f"calls={calls[0]}")

        # TTL 过期重判（monkeypatch time.time 推 11 分钟）
        real_time = S.time.time
        S.time.time = lambda: real_time() + 700
        c.post("/api/sessions", json={"question": "固态电池", "mode": "fast"})
        check("TTL 过期重判 LLM+1", calls[0] == 2, f"calls={calls[0]}")
        S.time.time = real_time

        # 空白 question：/api/intent→unclear；/api/sessions→409
        r3 = c.post("/api/intent", json={"question": "   "})
        check("/api/intent 空白→unclear", r3.json().get("intent") == "unclear",
              r3.text[:100])
        r4 = c.post("/api/sessions", json={"question": "   ", "mode": "fast"})
        check("/api/sessions 空白→409", r4.status_code == 409, r4.status_code)
        # 预筛命中的 /api/intent 也返回模板（RA_INTENT 还开着）
        r = c.post("/api/intent", json={"question": "你好"})
        d = r.json()
        check("rule 命中返回 reply+suggestions",
              d["source"] == "rule" and d["intent"] == "chitchat"
              and bool(d["reply"]) and len(d["suggestions"]) == 3, str(d)[:140])
    finally:
        LLMClient.complete = orig_complete
        os.environ["RA_INTENT"] = "0"

    r = c.post("/api/intent", json={"question": "你好"})
    check("RA_INTENT=0 后 /api/intent → disabled",
          r.json()["source"] == "disabled")

    print("\n" + ("全部通过" if not FAILED else f"失败 {FAILED}"))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
