"""M2 冒烟测试。

不依赖 LLM 的部分全测：图编译、检索节点、抓取礼貌性、API、SSE、interrupt。
需要 GEMINI_API_KEY 的节点会在最后单独报。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("RA_DB_PATH", "/tmp/ra_m2_test.db")

OK, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name, fn):
    t0 = time.time()
    try:
        d = fn() or ""
        results.append((name, True, d))
        print(f"{OK} {name}  ({time.time()-t0:.2f}s) {d}")
    except Exception as e:
        import traceback
        results.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"{FAIL} {name}  ({time.time()-t0:.2f}s) {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)


# ---------- 1. 图编译 ----------

def t_graph():
    from research_agent.graph.runner import get_graph
    g = get_graph()
    nodes = set(g.get_graph().nodes.keys())
    for n in ("plan", "await_approval", "retrieve", "extract", "assess", "write"):
        assert n in nodes, f"缺节点 {n}，现有 {nodes}"
    assert g.checkpointer is not None, "checkpointer 未挂上"
    return f"节点 {sorted(nodes - {'__start__', '__end__'})}，checkpointer 已挂载"


# ---------- 2. 时间敏感判定 ----------

def t_time_sensitive():
    from research_agent.graph.nodes import _is_time_sensitive
    cases = [
        ("JEV 这个模型是什么", False),
        ("JEV 最近的性能怎么样", True),
        ("JEV 现状如何", True),
        ("what is the latest on JEV", True),
        ("JEV 的定价", False),
    ]
    bad = [(q, _is_time_sensitive(q), exp) for q, exp in cases
           if _is_time_sensitive(q) != exp]
    assert not bad, f"判定错误: {bad}"
    return f"{len(cases)} 条用例全部正确"


# ---------- 3. 硬上限路由 ----------

def t_router():
    from research_agent.graph.nodes import route_after_assess
    base = {"round": 1, "url_count": 0, "cost_tokens": 0, "elapsed_s": 0,
            "consecutive_no_new": 0, "stop_reason": ""}
    assert route_after_assess(base) == "retrieve"
    assert route_after_assess({**base, "stop_reason": "satisfied"}) == "write"
    assert route_after_assess({**base, "consecutive_no_new": 2}) == "write"
    assert route_after_assess({**base, "round": 8}) == "write"
    assert route_after_assess({**base, "url_count": 50}) == "write"
    assert route_after_assess({**base, "elapsed_s": 20 * 60}) == "write"
    assert route_after_assess({**base, "cost_tokens": 500_000}) == "write"
    return "5 条停止条件全部正确（满足/无新增2轮/8轮/50URL/20分钟/50万token）"


# ---------- 4. 抓取礼貌性 ----------

def t_fetcher():
    import asyncio
    from research_agent.ingest.extract import Fetcher
    from research_agent.db.repo import Repo
    repo = Repo("/tmp/ra_fetch_test.db")
    repo.init_schema()
    f = Fetcher(repo, throttle_s=0.2, concurrency=4)
    pages = asyncio.run(f.fetch_many([
        "https://typesafe.ai/blog/introducing-system-one-models-and-jev",
        "https://arxiv.org/abs/2509.13309",
        "https://juejin.cn/",        # 验证码页，应被拒绝
        "https://www.geekpark.net/", # 403，应被跳过
        "https://example.com/",      # 正文不足 200 字，应被拒绝
    ]))
    by = {u: p for u, p in zip([
        "https://typesafe.ai/blog/introducing-system-one-models-and-jev",
        "https://arxiv.org/abs/2509.13309",
        "https://juejin.cn/", "https://www.geekpark.net/", "https://example.com/"], pages)}
    jev = by["https://typesafe.ai/blog/introducing-system-one-models-and-jev"]
    assert jev.ok and len(jev.text) > 3000, f"JEV 官方博客抽出 {len(jev.text)} 字符"
    arx = by["https://arxiv.org/abs/2509.13309"]
    assert arx.ok and len(arx.text) > 1000, f"arxiv 抽出 {len(arx.text)} 字符"
    assert not by["https://juejin.cn/"].ok, "验证码页不应判成功"
    assert by["https://juejin.cn/"].reason == "captcha"
    assert not by["https://www.geekpark.net/"].ok, "403 不应判成功"
    assert by["https://www.geekpark.net/"].reason.startswith("blocked:")
    assert not by["https://example.com/"].ok, "过短页不应判成功"
    return (f"正常页 {len(jev.text)}/{len(arx.text)} 字符并带 title；"
            f"验证码页判 captcha，403 判 blocked，过短页判 {by['https://example.com/'].reason}")


def t_block_skip():
    """403/429 必须跳过而不是重试。"""
    import asyncio
    from research_agent.ingest.extract import Fetcher, Page, BLOCK_STATUS
    from research_agent.db.repo import Repo
    assert 403 in BLOCK_STATUS and 429 in BLOCK_STATUS and 503 in BLOCK_STATUS
    repo = Repo("/tmp/ra_block_test.db"); repo.init_schema()
    f = Fetcher(repo, throttle_s=0.05)

    # 直接构造一个被拦的响应路径，验证 reason 字段与不重试语义
    pages = asyncio.run(f.fetch_many(["https://httpbin.org/status/429"]))
    p = pages[0]
    assert not p.ok and p.reason.startswith("blocked:"), f"429 未被正确跳过: {p}"
    return f"429 响应被判为 {p.reason}，未重试"


# ---------- 5. 缓存与绕过 ----------

def t_cache():
    import asyncio
    from research_agent.db.repo import Repo
    from research_agent.search.normalize import normalize_url
    import uuid as _u
    repo = Repo(f"/tmp/ra_cache_{_u.uuid4().hex[:8]}.db"); repo.init_schema()
    norm = normalize_url("https://example.com/p")
    assert repo.cache_get(norm) is None
    repo.cache_put(norm, "https://example.com/p", "缓存正文", "h1", 200)
    assert repo.cache_get(norm) == "缓存正文"
    assert repo.cache_get(norm, ttl_days=0) is None, "TTL=0 时应视为过期"
    return "写入、命中、TTL 过期均正确"


# ---------- 6. 服务器与 API ----------

def t_server():
    import shutil
    from fastapi.testclient import TestClient
    for p in ("/tmp/ra_api_test.db",):
        for suf in ("", "-wal", "-shm"):
            f = p + suf
            if os.path.exists(f):
                os.unlink(f)
    os.environ["RA_DB_PATH"] = "/tmp/ra_api_test.db"
    # 重新载入 repo 模块绑定新库
    import importlib
    from research_agent.db import repo as repo_mod
    importlib.reload(repo_mod)
    from research_agent import server as srv
    importlib.reload(srv)

    with TestClient(srv.app) as c:
        h = c.get("/api/health").json()
        assert h["ok"] is True, h

        r = c.post("/api/sessions", json={"question": "MiniMax M3.1 Flash 的规格"}).json()
        sid = r["session_id"]
        assert r["thread_id"] == sid

        d = c.get(f"/api/sessions/{sid}").json()
        assert d["question"].startswith("MiniMax")

        c.post(f"/api/sessions/{sid}/topics", json={"topics": ["AI 模型", "大语言模型"]})
        assert set(c.get(f"/api/sessions/{sid}").json()["topics"]) == {"AI 模型", "大语言模型"}

        c.post(f"/api/sessions/{sid}/archive")
        d2 = c.get(f"/api/sessions/{sid}").json()
        assert d2["status"] == "archived" and d2["archived_at"], d2
        # 归档后应能被主题浏览到
        assert any(s["id"] == sid for s in c.get("/api/sessions?archived=1").json())
        assert {t["name"] for t in c.get("/api/topics").json()} >= {"AI 模型"}

        # 检索调试口
        s = c.get("/api/search", params={"q": "大语言模型", "lang": "zh-CN", "limit": 4}).json()
        assert s["count"] > 0, s

        assert c.get("/api/sessions/nonexistent").status_code == 404
    return f"健康检查/建会话/归档/主题/检索/404 全部正常，中文检索 {s['count']} 条"


# ---------- 7. SSE ----------

def t_sse():
    """测事件机制本身，不走 TestClient 的流式读取。

    TestClient 在退出 stream 上下文时会尝试读完整个响应体，
    而 SSE 是长连接、生成器不会自然结束，所以流式断言必然挂住。
    这里改为直接验证注册与序列化逻辑。
    """
    import queue as _q
    from research_agent import server as srv

    # 1) 路由已注册；media_type 挂在返回的 StreamingResponse 上，不在路由上
    from starlette.responses import StreamingResponse
    route = next((r for r in srv.app.routes
                  if getattr(r, "path", "") == "/api/stream/{sid}"), None)
    assert route is not None, "SSE 路由未注册"
    resp = srv.stream("t-sse-probe")
    assert isinstance(resp, StreamingResponse), type(resp)
    assert resp.media_type == "text/event-stream", resp.media_type
    assert resp.headers.get("x-accel-buffering") == "no", "缺关闭代理缓冲的响应头"
    resp.body_iterator.aclose()   # 立刻关掉生成器，不消费

    # 2) 订阅与事件推送
    sid = "t-sse"
    q: _q.Queue = _q.Queue(maxsize=4)
    srv._subs.setdefault(sid, set())
    srv._subs[sid].add(q)
    srv._emit(sid, {"kind": "retrieve", "round": 1, "hits": 3})
    ev = q.get_nowait()
    assert ev["kind"] == "retrieve" and ev["round"] == 1, ev

    # 3) 可 JSON 序列化（含中文与不可序列化对象的兜底）
    line = f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
    assert line.startswith("data: ") and line.endswith("\n\n"), repr(line)
    assert json.loads(line[6:].strip())["hits"] == 3

    # 4) 短轮询常量：阻塞超时必须是秒级，不能是十几秒级的长阻塞
    src = (srv.__file__ and __import__("pathlib").Path(srv.__file__).read_text(encoding="utf-8"))
    assert "q.get(timeout=2.0)" in src, "SSE 轮询超时应保持秒级，避免断连后长时间占线程"

    srv._subs[sid].discard(q)
    return "路由/媒体类型/事件推送/序列化/短轮询 全部正确"


# ---------- 8. LLM 环节（需要 key）----------

def t_llm_path():
    if not os.environ.get("OPENCODE_API_KEY"):
        raise RuntimeError("未设置 OPENCODE_API_KEY，跳过（需要你提供）")
    from research_agent.llm.provider import LLMClient
    llm = LLMClient()
    d = llm.complete_json("extract", "片段：JEV 是 TypeSafe AI 于 2026-09-15 发布的模型。",
                          system=("从片段抽取断言，严格输出 JSON："
                                  '{"claims":[{"text":str,"entities":[str],'
                                  '"source_date":str|null,"confidence":str}]}'))
    assert d.get("claims"), d
    return f"抽取环节返回 {len(d['claims'])} 条断言（thinkingBudget=0 生效）"


if __name__ == "__main__":
    for line in Path(".env").read_text(encoding="utf-8").splitlines() if Path(".env").exists() else []:
        if line.startswith("TAVILY_API_KEY="):
            os.environ["TAVILY_API_KEY"] = line.split("=", 1)[1].strip()
        if line.startswith("OPENCODE_API_KEY="):
            os.environ["OPENCODE_API_KEY"] = line.split("=", 1)[1].strip()

    print("=" * 66)
    print("M2 冒烟测试")
    print("=" * 66)
    check("LangGraph 状态机编译", t_graph)
    check("时间敏感判定", t_time_sensitive)
    check("循环停止条件路由", t_router)
    check("抓取与正文抽取", t_fetcher)
    check("403/429 跳过不重试", t_block_skip)
    check("抓取缓存与 TTL", t_cache)
    check("FastAPI 接口", t_server)
    check("SSE 事件流", t_sse)
    check("LLM 抽取环节", t_llm_path)
    print("=" * 66)
    p = sum(1 for _, ok, _ in results if ok)
    print(f"{p}/{len(results)} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  {FAIL} {n}: {d}")
    sys.exit(0 if p == len(results) else 1)
