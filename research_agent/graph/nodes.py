"""主图节点。

结构原则（2026-10-04 决定）：**改结构，不打规则补丁。**
探针与批量抽取各自成为子图；框架有 grounded_in 硬约束；框架可被 LLM 自主修订。
来源质量的分级、配额、去重这些「规则补丁」暂不实现——分级信号已经由
extract_batch 免费产出，留到后面按实测效果决定是否加规则。
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
import time
from typing import Any

from ..db.repo import get_repo
from ..ingest.embed import Embedder, to_blob
from ..ingest.extract import Fetcher
from ..ingest.split import RecursiveSplitter, count_tokens
from ..llm.provider import LLMClient, session_scope
from ..search.base import SearchHit
from ..search.normalize import content_hash, domain_of, guess_source_type, normalize_url
from ..search.searxng import SearXNGProvider
from ..search.tavily import TavilyProvider
from . import prompts
from .state import ResearchState
from .subgraphs import extract_batch, probe, relevance
from .subgraphs import write as write_subgraph

TIME_MARKERS = ["最新", "近况", "现在", "目前", "近期", "现状", "最近", "today",
                "latest", "recent", "current", "now"]

HARD = dict(max_rounds=8, max_urls=50, timeout_min=20, max_tokens=500_000, no_new_streak=2)
MAX_QUERIES_PER_LANG = 5          # 单次每种语言的查询条数上限

SPLITTER = RecursiveSplitter(max_tokens=2000, target_tokens=1000)
LLM = LLMClient()
EMB = Embedder.instance()
SX = SearXNGProvider(script=".searxng/searxng.sh")
TV = TavilyProvider()


def scoped(fn):
    """把节点包进本场研究的记账作用域。

    没有它，llm_call 的 session_id 全是 NULL，session_cost 恒为 0，
    成本可观测（P7）就是空的。子图与并发协程在同上下文里自动继承。
    """

    @functools.wraps(fn)
    def wrapper(state):
        with session_scope((state or {}).get("session_id")):
            return fn(state)

    return wrapper

_KEEP_UPPER = re.compile(r"^(Accept|Charset|Cookie|Encoding|Referer|User-Agent|TE|"
                         r"Transfer-Encoding|Connection|Via)", re.I)
_PROXY_HDRS = {"host", "x-forwarded-for", "x-forwarded-proto", "x-real-ip",
               "x-forwarded-host", "cf-connecting-ip", "x-forwarded-port"}


def _is_time_sensitive(q: str) -> bool:
    ql = q.lower()
    return any(m in ql for m in TIME_MARKERS)


# ================================================================ 探针

@scoped
def node_probe(state: ResearchState) -> dict:
    """探针子图封装。子图内部三次 LLM 调用与换词重试主循环都看不到。"""
    q = state["question"]
    ts = _is_time_sensitive(q)
    result = probe.probe(q, session_id=state.get("session_id", ""),
                         time_sensitive=ts)
    repo = get_repo()
    repo.x("UPDATE session SET probe_json=? WHERE id=?",
           (json.dumps(result, ensure_ascii=False), state["session_id"]))
    return {
        "probe": result,
        "probe_degraded": result.get("degraded", False),
        "probe_headline": result.get("headline", ""),
        "time_sensitive": ts,
        "events": result.get("events", []),
    }


# ================================================================ 框架

@scoped
def node_outline(state: ResearchState) -> dict:
    """基于探针轮廓定框架。grounded_in 是唯一的硬约束。"""
    pr = state.get("probe") or {}
    sig = pr.get("signals") or {}

    plan = LLM.complete_json(
        "outline",
        f"研究问题：{state['question']}\n"
        + (f"用户已澄清指向：{pr.get('ambiguity', {}).get('user_answer')}\n"
           if pr.get("ambiguity", {}).get("user_answer") else "")
        + f"\n探针阶段实际观察到的领域轮廓：\n"
        + json.dumps(sig, ensure_ascii=False)[:12000],
        system=prompts.PLAN_OUTLINE,
    )

    # ---- grounded_in 校验：缺这一项的节直接丢弃 ----
    raw_sections = plan.get("sections") or []
    outline, dropped = [], []
    for i, s in enumerate(raw_sections):
        gi = s.get("grounded_in") or []
        if not isinstance(gi, list) or not gi:
            dropped.append({"title": s.get("title", f"sec{i+1}"),
                            "reason": "缺 grounded_in，无法确认这一维度是真实观测到的"})
            continue
        s.setdefault("id", f"sec{i + 1}")
        outline.append(s)

    if dropped:
        print(f"⚠️  {len(dropped)} 个小节因缺 grounded_in 被丢弃："
              f"{[d['title'] for d in dropped]}", flush=True)

    if not outline:
        # 全部被丢：退回极简框架，只留问题本身，不靠 LLM 先验撑
        outline = [{
            "id": "sec1", "title": state["question"][:40],
            "question": state["question"],
            "grounded_in": ["(degraded)"],
            "kind": "definition",
        }]

    qz = (plan.get("queries") or {}).get("zh") or []
    qe = (plan.get("queries") or {}).get("en") or []
    warn = []
    if len(qz) > MAX_QUERIES_PER_LANG or len(qe) > MAX_QUERIES_PER_LANG:
        warn.append(f"查询超限：zh {len(qz)} 条、en {len(qe)} 条，将截断至 {MAX_QUERIES_PER_LANG} 条")

    # 兜底：模型没给或被截断掉了，就从问题、轮廓信号和小节标题里生成。
    # 没有这一步，整场研究会跑满 8 轮空转然后产出一份 48 字符的废报告。
    if not qz and not qe:
        queries = _queries_from_outline(state["question"], outline, sig)
        warn.append("模型未产出 queries，已按轮廓信号自动生成")
    else:
        queries = {"zh": qz[:MAX_QUERIES_PER_LANG],
                   "en": qe[:MAX_QUERIES_PER_LANG]}

    repo = get_repo()
    repo.x("UPDATE session SET outline_json=?, plan_json=? WHERE id=?",
           (json.dumps({"sections": outline, "queries": queries},
                       ensure_ascii=False),
            json.dumps(plan, ensure_ascii=False),
            state["session_id"]))   # 只写 outline_json，不碰 probe_json

    ev = [{"kind": "outline", "sections": len(outline), "dropped": dropped,
           "queries": {"zh": len(queries["zh"]), "en": len(queries["en"])},
           "warnings": warn}]
    return {"outline": outline, "plan_queries": queries, "events": ev,
            "round": 0, "visited_urls": [], "url_count": 0,
            "consecutive_no_new": 0, "all_evidence": [], "sources": [],
            "outline_revision_log": []}


def _queries_from_outline(question: str, outline: list, signals: dict) -> dict:
    """从问题、大纲小节标题、探针观察到的实体推导查询词。

    与「编造查询词」有本质区别：这里用的每一个词都来自**实际观测到的材料**
    （框架里的小节标题、探针归纳出的实体），不是代码里写死的关键词模板。
    没有这一步，模型一旦没给出 queries，整场研究会空转到硬上限、
    最后产出一份几十字的废报告。

    探针里那个同名的兜底函数（拼「争议/最新/机构」）已删除，两者不要混为一谈。
    """
    import re as _re

    ent = signals.get("entities") or []
    zh_ent = [e.get("name") for e in ent
              if e.get("name") and _re.search(r"[\u4e00-\u9fff]", e.get("name", ""))]
    en_ent = [e.get("name") for e in ent
              if e.get("name") and not _re.search(r"[\u4e00-\u9fff]", e.get("name", ""))]
    titles = [str(s.get("title") or "") for s in outline if s.get("title")]

    zh = [question] + [t for t in titles[:3]] + [e for e in zh_ent[:2]]
    en = [question] + [t for t in titles[:2]] + [e for e in en_ent[:3]]
    for q in (signals.get("common_questions") or [])[:2]:
        en.append(str(q))
    return {"zh": _dedup_zh(zh)[:MAX_QUERIES_PER_LANG],
            "en": _dedup_zh(en)[:MAX_QUERIES_PER_LANG]}


@scoped
def node_await_approval(state: ResearchState) -> dict:
    from langgraph.types import interrupt

    pr = state.get("probe") or {}
    payload = {
        "session_id": state["session_id"],
        "question": state["question"],
        "time_sensitive": state.get("time_sensitive", False),
        "probe": {k: pr.get(k) for k in
                  ("quality", "degraded", "headline", "ambiguity", "deep_urls_read")},
        "signals": (pr.get("signals") or {}),
        "outline": state.get("outline", []),
        "queries": state.get("plan_queries", {}),
        "recalled": [
            {"id": c["id"], "summary": c.get("summary"),
             "heading": c.get("heading_path"), "session_id": c.get("session_id"),
             "source_date": c.get("source_date")}
            for c in (state.get("recalled_chunks") or [])[:40]
        ],
    }
    reply = interrupt(payload)
    if isinstance(reply, dict):
        keep = set(reply.get("approved_recalled_ids") or [])
        return {
            "plan_approved": bool(reply.get("approved", True)),
            "recalled_approved": [c for c in (state.get("recalled_chunks") or [])
                                  if c.get("id") in keep],
            "recalled_rejected": [c for c in (state.get("recalled_chunks") or [])
                                  if c.get("id") not in keep],
        }
    return {"plan_approved": bool(reply), "recalled_approved": [],
            "recalled_rejected": []}


# ================================================================ 检索

def _collect_queries(state: ResearchState) -> dict:
    """三路查询合并，每种语言不超过 5 条。"""
    base = state.get("plan_queries") or {}
    zh = list(base.get("zh") or [])
    en = list(base.get("en") or [])

    # 第二轮起补上从证据里抽到的实体（缺口驱动之外的自主扩展）
    if state.get("round", 0) >= 1:
        zh += _entities_from(state, zh_only=True)
        en += _entities_from(state, zh_only=False)

    # 去重 + 截断。截断要留痕，不能像旧代码那样静默丢掉
    zh = _dedup_zh(zh)[:MAX_QUERIES_PER_LANG]
    en = _dedup_zh(en)[:MAX_QUERIES_PER_LANG]
    return {"zh": zh, "en": en}


def _dedup_zh(xs: list[str]) -> list[str]:
    seen, out = set(), []
    for x in xs:
        k = x.strip().lower()
        if k and k not in seen:
            seen.add(k); out.append(x.strip())
    return out


def _entities_from(state: ResearchState, zh_only: bool) -> list[str]:
    ev = state.get("all_evidence") or []
    if not ev:
        return []
    cands: list[str] = []
    for e in ev[-40:]:
        txt = (e.get("title") or "") + " " + " ".join(
            c.get("text", "")[:200] for c in (e.get("claims") or [])[:5]
        )
        for pat in (r"([A-Z][A-Za-z0-9]{2,}(?:\s+[A-Z][A-Za-z0-9]{2,}){0,2})",
                    r"([\u4e00-\u9fff]{2,8}(?:公司|团队|实验室|大学|研究院|模型))"):
            cands += re.findall(pat, txt)
    out, seen = [], set()
    for c in cands:
        if len(c) < 3 or (bool(re.search(r"[\u4e00-\u9fff]", c)) != zh_only):
            continue
        if c.lower() in seen:
            continue
        seen.add(c.lower()); out.append(c)
        if len(out) >= 3:
            break
    return out


@scoped
def node_retrieve(state: ResearchState) -> dict:
    repo = get_repo()
    sid = state["session_id"]
    rnd = state.get("round", 0) + 1
    queries = _collect_queries(state)

    visited = set(state.get("visited_urls") or [])
    seen_norms = set(visited)
    known_hashes = state.get("conflict_hashes") or repo.known_content_hashes()

    def _collect(hits: list[SearchHit]) -> list[SearchHit]:
        out = []
        for h in hits:
            n = normalize_url(h.url)
            if n in seen_norms:
                continue
            seen_norms.add(n)
            out.append(h)
        return out

    async def call(prov, q, lang, lim):
        return await asyncio.to_thread(prov.search, q, language=lang, limit=lim)

    async def run():
        tasks = [call(SX, q, "zh-CN", 8) for q in queries["zh"]]
        tasks += [call(TV, q, "en", 6) for q in queries["en"]]
        res = await asyncio.gather(*tasks, return_exceptions=True)
        flat: list[SearchHit] = []
        for r in res:
            if isinstance(r, list):
                flat.extend(r)
        return _collect(flat)

    hits = asyncio.run(run())
    budget_left = max(0, HARD["max_urls"] - len(visited))
    hits = hits[:budget_left]

    bypass = bool(state.get("time_sensitive"))
    pages = asyncio.run(Fetcher(repo).fetch_many([h.url for h in hits],
                                               bypass_cache=bypass))
    page_by_url = {p.url: p for p in pages}

    new_sources, pending, events, skipped = [], [], [], []

    for h in hits:
        p = page_by_url.get(h.url)
        if p is None or not p.ok:
            skipped.append({"url": h.url, "reason": p.reason if p else "no_page"})
            continue
        ch = content_hash(p.text)
        if ch in known_hashes:
            repo.x("UPDATE source SET superseded_by=? WHERE session_id=? AND url=?",
                   (known_hashes[ch], sid, h.url))
            skipped.append({"url": h.url, "reason": f"dup:{known_hashes[ch]}"})
            continue
        stype = guess_source_type(h.url, p.title or h.title, p.text)
        if stype == "repost":
            skipped.append({"url": h.url, "reason": "repost"})
            continue

        src_id = repo.add_source(
            session_id=sid, url=h.url, url_normalized=normalize_url(h.url),
            domain=domain_of(h.url), title=p.title or h.title,
            source_type=stype, engine=h.engine, source_date=p.published,
            content_hash=ch, char_count=len(p.text), http_status=p.status,
        )
        known_hashes[ch] = src_id
        new_sources.append({"id": src_id, "url": h.url, "title": p.title or h.title,
                            "domain": domain_of(h.url), "source_type": stype,
                            "engine": h.engine, "source_date": p.published,
                            "char_count": len(p.text)})
        pending.append({"source_id": src_id, "title": p.title or h.title,
                        "url": h.url, "published": p.published, "text": p.text})

    # 段落先落库（status=pending），extract 阶段补概括句与来源性质，
    # finalize 阶段只补向量。概括句从 finalize 搬到 extract 之后，
    # 写作阶段才看得到它——原先它在报告写完之后才生成，只进召回索引。
    for it in pending:
        segments = []
        for c in SPLITTER.split(it["text"]):
            cid = repo.add_chunk(
                session_id=sid, source_id=it["source_id"], origin="web",
                content=c.content, heading_path=c.heading_path,
                token_count=c.token_count, source_date=it["published"],
            )
            segments.append({"chunk_id": cid, "text": c.content,
                             "heading_path": c.heading_path,
                             "token_count": c.token_count})
        it["segments"] = segments

    events.append({"kind": "retrieve", "round": rnd,
                   "queries": queries, "hits": len(hits),
                   "accepted": len(new_sources), "skipped": skipped[:20]})
    return {"round": rnd, "visited_urls": list(seen_norms),
            "url_count": len(visited) + len(hits), "sources": new_sources,
            "pending_extract": pending, "conflict_hashes": known_hashes,
            "events": events}


# ================================================================ 抽取

@scoped
def node_extract(state: ResearchState) -> dict:
    """调用抽取子图。批次细节与并发都在子图内部。"""
    items = state.get("pending_extract") or []
    if not items:
        return {"pending_extract": [], "all_evidence": [],
                "events": [{"kind": "extract", "round": state.get("round"),
                            "sources": 0}]}

    out = extract_batch.extract_all(items)
    results = out.get("results") or []

    # 相关性闸门：claims 已经抽出来了，此刻判断几乎零成本
    pr = state.get("probe") or {}
    sig = pr.get("signals") or {}
    kws = relevance.build_keywords(sig, state["question"])
    label = state["question"][:80]
    stats = relevance.gate_all(results, kws, label)

    # 落库回写：来源性质与段落概括
    repo = get_repo()

    # 来源性质：原先只活在 state 里，进程退出就丢，写作阶段只能靠主 agent
    # 在规约里转述「S1 是雪球帖、S2 是博客」这类判断
    for r in results:
        if r.get("kind") == "claims" and r.get("source_nature"):
            repo.set_source_nature(r["source_id"], r["source_nature"],
                                   r.get("nature_reason") or "")

    # 段落概括：写作阶段的证据入口。
    # 注意：批次失败的那些段不会有概括，进而在 node_write 里被
    # 「要有概括才进证据池」滤掉——这是**静默丢证据**。
    # 所以要把差额显式记进事件，不能让它悄悄消失。
    summaries = out.get("summaries") or {}
    if summaries:
        repo.mark_summaries(summaries)
    n_seg = len(items) and sum(len(i.get("segments") or []) for i in items)
    n_sum = len(summaries)
    if n_seg and n_sum < n_seg:
        print(f"⚠️  {n_seg - n_sum}/{n_seg} 个段落没有概括，"
              f"写作阶段将看不到它们（概括批次失败）", flush=True)

    # 落库回写：无关来源留库但标记，不参与报告
    for r in results:
        if r.get("kind") != "claims":
            continue
        repo.x("UPDATE source SET on_topic=?, topic_reason=?, gate_method=? WHERE id=?",
               (1 if r.get("on_topic") else 0, r.get("topic_reason"),
                r.get("gate_method"), r.get("source_id")))

    n_claims = sum(len(r.get("claims") or []) for r in results
                   if r.get("kind") == "claims")
    n_ok = len([r for r in results if r.get("kind") == "claims"])
    n_err = len([r for r in results if r.get("kind") == "extract_error"])

    return {
        "pending_extract": [],
        "all_evidence": results,
        "events": (out.get("events") or []) + [{
            "kind": "extract", "round": state.get("round"),
            "sources": len(items), "ok": n_ok, "errors": n_err,
            "segments": n_seg, "summarized": n_sum,
            "summarize_failed_batches": out.get("failed"),
            "gate": stats,
        }],
    }


# ================================================================ 缺口评估

@scoped
def node_assess(state: ResearchState) -> dict:
    rnd = state.get("round", 0)
    ev = state.get("all_evidence") or []
    claims = [c for e in ev if e.get("kind") == "claims" for c in (e.get("claims") or [])]
    outline = state.get("outline") or []

    if not claims:
        return {"plan_queries": state.get("plan_queries") or {},
                "events": [{"kind": "assess", "round": rnd, "note": "no claims"}]}

    brief = json.dumps({
        "sections": [{"id": s.get("id"), "title": s.get("title")} for s in outline],
        "claims": [{"t": c.get("text"), "e": (c.get("entities") or [])[:3]}
                   for c in claims[-150:]],
    }, ensure_ascii=False)[:40000]

    try:
        data = LLM.complete_json("assess", brief, system=prompts.ASSESS)
    except Exception:
        data = {}

    nq = data.get("next_queries") or {}
    queries = {
        "zh": (nq.get("zh") or [])[:MAX_QUERIES_PER_LANG],
        "en": (nq.get("en") or [])[:MAX_QUERIES_PER_LANG],
    }
    if not queries["zh"] and not queries["en"]:
        queries = state.get("plan_queries") or {}

    # 本轮是否真有新增
    new_now = len([e for e in (state.get("pending_extract") or []) if e])
    produced = len([c for c in claims])
    prev_round_claims = state.get("last_round_claims")
    new_claims = produced - (prev_round_claims or 0) if prev_round_claims else produced
    streak = state.get("consecutive_no_new", 0) + 1 if new_claims <= 0 else 0

    return {
        "plan_queries": queries,
        "consecutive_no_new": streak,
        "last_round_claims": produced,
        "stop_reason": "satisfied" if data.get("has_enough") else "",
        "pending_revision": data.get("outline_revision") or {"op": "none"},
        "events": [{"kind": "assess", "round": rnd,
                    "has_enough": bool(data.get("has_enough")),
                    "covered": data.get("covered", []),
                    "gaps": data.get("gaps", []),
                    "new_claims": new_claims, "no_new_streak": streak}],
    }


@scoped
def node_apply_revision(state: ResearchState) -> dict:
    """把 LLM 自主提议的框架修订真正落到 outline 上。"""
    rev = state.get("pending_revision") or {"op": "none"}
    outline = list(state.get("outline") or [])
    if rev.get("op") != "revise":
        return {"pending_revision": {"op": "none"}, "events": []}

    by_id = {s.get("id"): s for s in outline}
    removed = {r.get("id"): r.get("reason", "") for r in (rev.get("remove") or [])}
    merged = {tuple(m.get("ids") or []): m for m in (rev.get("merge") or [])}

    dropped_ids = set()
    out = []
    for s in outline:
        sid_ = s.get("id")
        if sid_ in removed:
            dropped_ids.add(sid_); continue
        if sid_ in dropped_ids:
            continue
        if any(sid_ in (m.get("ids") or []) for m in (rev.get("merge") or [])):
            continue  # 合并目标保留，被合并的丢弃
        out.append(s)

    added = []
    nxt = len(out)
    for a in (rev.get("add") or []):
        gi = a.get("grounded_in") or []
        if not gi:
            continue  # 新增的小节同样必须有 grounded_in
        nxt += 1
        ns = {"id": f"sec{nxt}", "title": a.get("title"),
              "question": a.get("question"), "grounded_in": gi,
              "kind": a.get("kind", "definition")}
        out.append(ns); added.append(ns["title"])

    repo = get_repo()
    repo.x("UPDATE session SET outline_json=? WHERE id=?",
           (json.dumps({"sections": out, "queries": state.get("plan_queries")},
                       ensure_ascii=False), state["session_id"]))

    entry = {"round": state.get("round"), "reason": rev.get("reason", ""),
             "added": added,
             "removed": list(removed.keys()),
             "merged": [m.get("into") for m in (rev.get("merge") or [])]}
    return {"outline": out, "pending_revision": {"op": "none"},
            "outline_revision_log": [entry],
            "events": [{"kind": "outline_revision", **entry}]}


def route_after_assess(state: ResearchState) -> str:
    """先应用修订，再决定继续还是收工。"""
    return "apply_revision"


def route_after_revision(state: ResearchState) -> str:
    rnd = state.get("round", 0)
    if state.get("stop_reason") == "satisfied":
        return "write"
    q = state.get("plan_queries") or {}
    if not q.get("zh") and not q.get("en"):
        return "write"      # 没有任何查询可用，继续跑也是空转
    if state.get("consecutive_no_new", 0) >= HARD["no_new_streak"]:
        return "write"
    if rnd >= HARD["max_rounds"]:
        return "write"
    if state.get("url_count", 0) >= HARD["max_urls"]:
        return "write"
    if state.get("elapsed_s", 0) >= HARD["timeout_min"] * 60:
        return "write"
    if state.get("cost_tokens", 0) >= HARD["max_tokens"]:
        return "write"
    return "retrieve"


# ================================================================ 写报告

@scoped
def node_write(state: ResearchState) -> dict:
    """写报告。真正的活儿在 subgraphs/write.py，这里只做取数与落库。

    架构已重设计：原来是一节一次调用、各节互不可见、且写完没人检查；
    现在是主 agent 先出规约与任务列表，子 agent 分块写，主 agent 通读审查
    后按需定点返修。详见 DESIGN_R4_WRITE.md。
    """
    repo = get_repo()
    sid = state["session_id"]

    # 来源与编号一律以库里的 rowid 为准——EvidenceTools 也是这么编的。
    # 早先这里用的是 state["sources"]（检索轮次累积的顺序），两者一旦不一致，
    # 子 agent 拿到的段号就和工具返回的对不上，引用会指错。单一真相在数据库。
    all_sources = [dict(r) for r in repo.q(
        "SELECT id, title, domain, url, source_type, source_date, char_count, "
        "       source_nature FROM source WHERE session_id=? ORDER BY rowid", (sid,))]

    head = ""
    if state.get("probe_degraded"):
        head = prompts.DEGRADED_HEADER.format(
            quality=(state.get("probe") or {}).get("quality", "thin"),
            notice=state.get("probe_headline", "该主题的公开信息稀薄。"))

    if not all_sources:
        md = (head + "\n# 检索结果\n\n本轮没有可用来源。"
              "可尝试调整检索词或换检索后端。\n")
        repo.save_report(sid, md)
        return {"report_markdown": md,
                "events": [{"kind": "write", "round": state.get("round"),
                            "note": "empty", "sections_written": 0}]}

    # 段号方案必须与 EvidenceTools 一致：来源按入库顺序（rowid）编号，
    # 段按来源内顺序编号，写成 S3·5。引用粒度因此是「哪一段」而不是「哪一篇」。
    src_no: dict[str, int] = {s["id"]: i for i, s in enumerate(all_sources, 1)}
    # Row 是只读的，要挂 ref 必须先转 dict
    repo_segments = [dict(r) for r in repo.q(
        "SELECT id, source_id, heading_path, content, summary, token_count, on_topic "
        "FROM chunk WHERE session_id=? ORDER BY rowid", (sid,))]
    seg_no: dict[str, int] = {}
    for c in repo_segments:
        n = seg_no.get(c["source_id"], 0) + 1
        seg_no[c["source_id"]] = n
        c["ref"] = f"S{src_no.get(c['source_id'], 0)}·{n}"

    # 段级闸门：整篇一起进出太粗，一半讲技术一半讲融资的页面会整篇进来
    pr = state.get("probe") or {}
    kws = relevance.build_keywords((pr.get("signals") or {}), state["question"])
    segs = [c for c in repo_segments
            if src_no.get(c["source_id"]) and (c["summary"] or "").strip()]
    gate = relevance.gate_segments(segs, kws, state["question"][:80])
    repo.mark_chunk_topic(sid, gate["marks"])

    src_lines = [f"[S{i}] {s.get('title') or s['domain']} | {s['domain']} "
                 f"| {s.get('source_type')} | {s.get('source_date') or '日期未知'}"
                 f" | {s.get('source_nature') or '性质未判'}"
                 f" | 正文 {s.get('char_count') or 0} 字"
                 for i, s in enumerate(all_sources, 1)]

    keep = state.get("recalled_approved") or []
    earlier = [{"summary": c.get("summary"), "date": c.get("source_date")}
               for c in keep[:30]] if keep else []

    out = write_subgraph.run_write(
        session_id=sid, question=state["question"],
        outline=state.get("outline") or [],
        signals=(pr.get("signals") or {}),
        segments=segs, on_topic=gate["keep"], earlier=earlier,
        src_lines=src_lines)

    md = out.get("report_markdown") or ""
    if head:
        md = head + md
    repo.save_report(sid, md)
    return {"report_markdown": md,
            "events": (out.get("events") or []) + [
                {"kind": "write", "round": state.get("round"),
                 "chars": len(md), "sources": len(src_lines),
                 "degraded": bool(head)}]}


# ================================================================ 收尾

@scoped
def node_finalize(state: ResearchState) -> dict:
    """向量化 + 状态与成本回写。

    概括句已搬到 node_extract（见那里的说明），这里只负责把
    「概括句 + 正文」拼起来向量化，以及写回成本与状态。
    """
    repo = get_repo()
    sid = state["session_id"]
    rep = repo.q1("SELECT id FROM report WHERE session_id=?", (sid,))
    if not rep:
        return {"events": [{"kind": "finalize", "skipped": "no report"}]}

    chunks = repo.q(
        "SELECT c.id, c.content, c.heading_path, c.source_id, c.summary, "
        "       s.url, s.domain, s.title, s.source_type, s.source_date, s.engine "
        "FROM chunk c LEFT JOIN source s ON s.id=c.source_id "
        "WHERE c.session_id=? AND c.status='pending' ORDER BY c.ingested_at",
        (sid,),
    )
    if not chunks:
        return {"events": [{"kind": "finalize", "skipped": "no pending chunks"}]}

    # ---- 1. 向量化 ----
    # 概括句已经不在这里生成了：它从 node_finalize 搬到 node_extract。
    # 原先报告写完之后才概括，产出只进跨会话召回的索引，写作阶段压根看不到；
    # 写作只能对着抽取出来的原子断言工作，而断言的父级是整篇文章，
    # 没法展开回去看上下文。搬到抽取阶段后，段落概括成了写作的证据入口。
    updates = []
    no_sum = 0
    for row in chunks:
        summary = (row["summary"] or "").strip()
        if not summary:
            no_sum += 1
            continue
        vec = EMB.embed_document(summary, row["content"])
        updates.append((row["id"], summary, to_blob(vec),
                        count_tokens(row["content"])))
    if updates:
        repo.complete_chunks(updates)

    # ---- 3. 状态与成本回写 ----
    cost = repo.session_cost(sid)
    repo.add_cost(sid, cost)
    repo.finish_session(sid, "done")

    remaining = len(chunks) - len(updates) - no_sum
    return {
        "embedded_chunks": len(updates),
        "cost_tokens": cost,
        "events": [{
            "kind": "finalize", "chunks": len(chunks), "embedded": len(updates),
            "no_summary": no_sum, "remaining_pending": max(0, remaining),
            "cost_tokens": cost,
        }],
    }
