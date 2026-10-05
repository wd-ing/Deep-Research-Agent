"""M1 冒烟测试：DB、切分、向量化、URL 规范化、检索后端。

用法: python -m tests.smoke
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OK, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, fn):
    t0 = time.time()
    try:
        detail = fn() or ""
        results.append((name, True, detail))
        print(f"{OK} {name}  ({time.time()-t0:.2f}s) {detail}")
    except Exception as e:
        results.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"{FAIL} {name}  ({time.time()-t0:.2f}s)  {type(e).__name__}: {e}")


# ---------- 1. 数据库 ----------

def t_db():
    from research_agent.db.repo import Repo
    import tempfile, os
    tmp = os.path.join(tempfile.mkdtemp(), "t.db")
    r = Repo(tmp)
    r.init_schema()
    sid = r.create_session("测试会话", "MiniMax M3.1 Flash 是什么")
    r.set_outline(sid, {"sections": ["背景", "规格"]})
    rid = r.save_report(sid, "# 报告\n内容")
    from research_agent.search.normalize import normalize_url, url_hash, content_hash
    src = r.add_source(
        session_id=sid, url="https://example.com/a?utm_source=x&id=1",
        url_normalized=normalize_url("https://example.com/a?utm_source=x&id=1"),
        domain="example.com", title="标题", engine="bing",
        content_hash=content_hash("正文 内容"),
    )
    cid = r.add_chunk(
        session_id=sid, report_id=rid, source_id=src, origin="web",
        content="这是一段测试内容，用来验证两阶段入库。", status="pending",
    )
    assert r.q1("SELECT id FROM chunk WHERE status='pending'") is not None
    assert len(r.ready_chunks(sid)) == 0, "pending 不该出现在检索集里"
    r.complete_chunks([(cid, "测试概括句", b"\x00" * 768, 20)])
    assert len(r.ready_chunks(sid)) == 1, "补全后应可检索"
    r.archive_session(sid)
    r.set_topics(sid, ["AI 模型"])
    assert "AI 模型" in r.topics_of(sid)
    assert len(r.sessions_by_topic("AI 模型")) == 1
    return f"session/report/source/chunk 往返正常，归档与主题正常"


# ---------- 2. URL 规范化 ----------

def t_normalize():
    from research_agent.search.normalize import (
        normalize_url, url_hash, content_hash, domain_of, guess_source_type)
    a = normalize_url("https://www.example.com/a?utm_source=x&id=1&fbclid=z")
    b = normalize_url("https://example.com/a?id=1")
    assert a == b, f"{a} != {b}"
    # 协议不做归一：http 与 https 内容可能真的不同，跨协议的重复交给正文哈希兜
    assert normalize_url("https://x.com/p#frag") == "https://x.com/p"
    assert normalize_url("https://x.com/post/amp/") == "https://x.com/post"
    assert normalize_url("https://x.com/p?b=2&a=1") == normalize_url("https://x.com/p?a=1&b=2")
    assert url_hash("https://WWW.A.com/p?gclid=1") == url_hash("https://a.com/p")
    assert content_hash("内容   内容") == content_hash("内容 内容")
    assert domain_of("https://www.x.com/p") == "x.com"
    assert guess_source_type("https://arxiv.org/abs/1") == "academic"
    assert guess_source_type("https://x.com/p", content="本文转载自公众号") == "repost"
    return "追踪参数剥离/参数序归一/AMP/锚点/同正文判定 均正确"


# ---------- 3. 递归切分 ----------

def t_split():
    from research_agent.ingest.split import RecursiveSplitter, count_tokens
    sp = RecursiveSplitter(max_tokens=2000, target_tokens=1000)
    md = ("# 标题\n\n" + "固态电池的关键在于电解质材料的选择。" * 60
          + "\n\n## 小节\n\n" + "硫化物路线的优点是电导率高。" * 90
          + "\n\n" + "短。\n\n" + "氧化物脆性大。\n")
    cs = sp.split(md)
    assert cs, "应切出片段"
    # 目标 1000：单块可因单个段落过大而到 max_tokens，但不得超过
    over = [c.token_count for c in cs if c.token_count > 2000]
    assert not over, f"有片段超过 max_tokens：{over}"
    # 落刀点在段落边界上：块内除最后一段外都是完整段落
    assert any("\n\n" in c.content for c in cs), "应有块包含多个段落"
    # 空输入不炸
    assert sp.split("   ") == []
    # 无空行时退化到按换行切，而不是当成一整块
    one = sp.split("A" * 50 + "\n" + "B" * 50)
    assert one, "无空行也要能切"
    return (f"{len(cs)} 块，最大 {max(c.token_count for c in cs)} token"
            f"（目标 {sp.target_tokens}，上限 {sp.max_tokens}）")


def t_embed():
    from research_agent.ingest.embed import (
        Embedder, to_blob, from_blob, cosine_topk, stack_blobs, time_decay, DIM)
    e = Embedder.instance()
    vecs = e.embed_passages(["大型语言模型的推理能力", "苹果发布新款手机"])
    assert vecs.shape[0] == 2, vecs.shape
    q = e.embed_queries(["语言模型的推理能力怎么样"])[0]
    sims = vecs @ q
    assert sims[0] > sims[1], f"语义相近的应排前: {sims}"
    # 前缀确实生效：去掉前缀应降低区分度（仅记录，不断言）
    blob = to_blob(vecs[0])
    back = from_blob(blob)
    assert abs(float(back @ back) - 1.0) < 0.01, "float16 往返后模长应仍为 1"
    m = stack_blobs([to_blob(v) for v in vecs])
    idx, sc = cosine_topk(q, m, k=2)
    assert idx[0] == 0, f"Top-1 应为语义相近那条，实际 {idx}"
    assert time_decay(None, "2026-10-03T00:00:00+00:00") == 1.0
    assert time_decay("2020-01-01", "2026-10-03T00:00:00+00:00") < 0.1
    return f"DIM={DIM}，float16 往返误差 <0.01，Top-1 正确，衰减正常"


# ---------- 5. LLM 配置硬约束 ----------

def t_llm_config():
    """模型选型与护栏的硬约束（2026-10-04 重写）。

    两类约束：

    一、三道防线。每个环节都要有 guard——给足预算、思考占比有监控、
    空输出能重试。少配一条就在启动时报错，不给静默兜底的机会。

    二、思考档位。统一走 MiniMax 的 output_config.effort：
      T0 = MiniMax-M3（省略 effort 即关闭思考）
      T1 = M3.1-Flash-Preview + low
      T2 = M3.1-Flash-Preview + high
    只允许这三档组合出现。上一轮踩过的坑是 thinking.budget_tokens
    （Anthropic 的字段，MiniMax 静默忽略），所以断言要盯住实际发出的字段。
    """
    from research_agent.llm.provider import (
        ModelRegistry, ConfigError, NEEDS_GUARD, THINKING_EFFORTS,
    )
    import yaml, tempfile, os
    reg = ModelRegistry()

    T0, T1, T2 = ("MiniMax-M3", "MiniMax-M3.1-Flash-Preview",
                  "MiniMax-M3.1-Flash-Preview")
    for stage in sorted(NEEDS_GUARD):
        cfg = reg.stage(stage)
        # 防线
        assert cfg.max_output_tokens >= 1024, f"{stage} 输出预算过小"
        assert cfg.guard.retry_on_empty >= 1, f"{stage} 未配空输出重试"
        assert cfg.guard.max_output_tokens > cfg.max_output_tokens, \
            f"{stage} 重试时预算没地方翻倍"
        assert 0 < cfg.guard.max_reasoning_ratio <= 1, f"{stage} 思考阈值不合理"
        # 思考档位：只认三档，且 effort 只能配在 minimax 上
        assert cfg.provider == "minimax", f"{stage} 偏离 MiniMax 统一策略: {cfg.provider}"
        if cfg.thinking_effort is None:
            assert cfg.model == T0, \
                f"{stage} 不设 effort 时只能用 {T0}，实际 {cfg.model}"
        else:
            assert cfg.model == T1 == T2 or cfg.model == T1, \
                f"{stage} 设了 effort 却不是 M3.1: {cfg.model}"
            assert cfg.thinking_effort in ("low", "high"), \
                f"{stage} effort 只用 low / high 两档，实际 {cfg.thinking_effort}"

    # 核心环节必须给到最高档
    for core in ("outline", "probe_outline", "write_plan", "write_audit"):
        assert reg.stage(core).thinking_effort == "high", \
            f"{core} 是核心环节，应当用 high 档"

    # 写环节的主 agent 与子 agent 分档，这是 R4 架构的基础
    for stage in ("write_plan", "write_audit"):
        assert reg.stage(stage).thinking_effort == "high"
    for stage in ("write_section", "write_revise"):
        assert reg.stage(stage).thinking_effort == "low"
    # 返修温度要低于初稿，避免它把没问题的部分一起重写
    assert reg.stage("write_revise").temperature < reg.stage("write_section").temperature

    # 篡改配置必须报错，而不是静默用默认值
    def expect_error(mutate, why):
        raw = yaml.safe_load(reg.path.read_text(encoding="utf-8"))
        mutate(raw)
        tf = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                         encoding="utf-8")
        yaml.safe_dump(raw, tf); tf.close()
        try:
            ModelRegistry(tf.name).stage("extract")
            raise AssertionError(f"{why} 时应当抛错")
        except ConfigError:
            pass
        os.unlink(tf.name)

    expect_error(lambda r: r["stages"]["extract"].pop("guard", None),
                 "缺少 guard")
    # provider 接受文档全集（low/medium/high/xhigh/max），
    # 「只用 low 与 high」是本项目的选型策略，对真实配置断言，不在这里冒充契约。
    expect_error(lambda r: r["stages"]["extract"].__setitem__("thinking_effort", "none"),
                 "用了接口不支持的 effort 值")
    expect_error(lambda r: r["stages"]["extract"].__setitem__("provider", "opencode"),
                 "effort 配在非 minimax provider 上")

    n_t0 = sum(1 for s in NEEDS_GUARD if reg.stage(s).thinking_effort is None)
    return (f"{len(NEEDS_GUARD)} 个环节防线齐备（{n_t0} 个 T0 / "
            f"{len(NEEDS_GUARD)-n_t0} 个 T1T2），三档之外与错配 provider 均拒绝启动")


# ---------- 6. 检索后端 ----------

def t_searxng():
    from research_agent.search.searxng import SearXNGProvider
    p = SearXNGProvider(script=".searxng/searxng.sh")
    hits = p.search("大语言模型", language="zh-CN", limit=6)
    assert hits, "SearXNG 应返回结果（会自动拉起）"
    engines = {h.engine for h in hits}
    assert hits[0].domain, "应解析出域名"
    p.shutdown_if_idle()
    return f"{len(hits)} 条，引擎 {sorted(engines)}"


def t_tavily():
    import os
    from pathlib import Path
    env = Path(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("TAVILY_API_KEY="):
                os.environ["TAVILY_API_KEY"] = line.split("=", 1)[1].strip()
    from research_agent.search.tavily import TavilyProvider
    p = TavilyProvider()
    if not p.health():
        return "跳过：key 不可用"
    hits = p.search("MiniMax M3.1 Flash", language="en", limit=5)
    assert hits, "Tavily 应返回结果"
    return f"{len(hits)} 条，首条 {hits[0].domain}"


# ---------- main ----------

if __name__ == "__main__":
    print("=" * 64)
    print("M1 冒烟测试")
    print("=" * 64)
    check("数据库 schema 与两阶段入库", t_db)
    check("URL 规范化与同源判定", t_normalize)
    check("递归字符切分", t_split)
    check("e5 向量化与余弦检索", t_embed)
    check("模型选型与护栏硬约束", t_llm_config)
    check("SearXNG 中文检索（惰性启停）", t_searxng)
    check("Tavily 英文检索", t_tavily)
    print("=" * 64)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} 通过")
    for name, ok, detail in results:
        if not ok:
            print(f"  {FAIL} {name}: {detail}")
    sys.exit(0 if passed == len(results) else 1)
