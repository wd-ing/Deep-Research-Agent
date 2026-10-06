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

HARD = dict(
    max_rounds=8,        # 单轮最多迭代几轮
    max_urls=50,         # 抓多少个 URL 就收
    # 实测（2026-10-06 接主图那轮）：17.7 分钟 / 421k token 就跑完了，
    # 而那是因为 assess 空转导致第 2 轮在重复检索。**修好 assess 后轮数会增加**，
    # 原来 20 分钟的墙会立刻变成主要失败原因，所以提到 30。
    timeout_min=30,
    # 同上：421k / 500k 已经贴脸，没有补检索的余量。
    max_tokens=800_000,
    # 修好之前它是个坏计数器（`produced` 取 claims 数量、恒为 0，
    # 于是 streak 每轮无条件 +1）。修好之后它才真的是「连续几轮没新东西」，
    # 2 轮是合理的停止阈值，所以**这个值不动**，变的是它的含义。
    no_new_streak=2,
)
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
            "used_queries": [], "last_round_segments": 0,
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


def _segments_for_assess(repo, sid: str) -> list[dict]:
    """assess 阶段能看到的证据段落。

    **单一真相**：跟 node_write 用的是同一张表、同一套过滤（on_topic + 有概括）。
    两边读的不一致，assess 就会对着空话说「材料不足」。
    """
    return [dict(r) for r in repo.q(
        "SELECT heading_path, summary, source_date FROM chunk "
        "WHERE session_id=? AND on_topic=1 AND summary IS NOT NULL "
        "AND summary!='' ORDER BY rowid", (sid,))]


def _section_terms(section: dict) -> list[str]:
    """从一小节的题面里抽「能拿去匹配材料的词」。

    不做分词——中文实体的子串匹配本来就准，而分词器会把
    「中科深蓝汇泽」切成碎片，反而匹配不上。
    按标点切开，取长度够的片段当实体。
    """
    raw = f"{section.get('title') or ''} {section.get('question') or ''}"
    parts = re.split(r"[、，,。；;：:？?！!（）()「」\"'／/\s]+", raw)
    out, seen = [], set()
    for p in parts:
        p = p.strip()
        if len(p) < 3:
            continue
        has_zh = bool(re.search(r"[\u4e00-\u9fff]", p))
        if has_zh and len(p) > 14:
            continue                       # 中文长片段多半是整句，不是实体
        if (not has_zh) and len(p) < 4:
            continue
        k = p.lower()
        if k in seen:
            continue
        seen.add(k); out.append(p)
    return out[:14]


def _match_for_section(section: dict, segs: list[dict], cap: int = 3) -> list[str]:
    """这节的题面词在材料里出现过几次，返回最有代表性的前 cap 条概括。"""
    terms = _section_terms(section)
    if not terms:
        return []
    hits = []
    for s in segs:
        blob = f"{s.get('heading_path') or ''} {s.get('summary') or ''}"
        n = sum(1 for t in terms if t in blob)
        if n:
            hits.append((n, (s.get("summary") or "").strip()))
    if not hits:
        return []
    hits.sort(key=lambda x: -x[0])
    return [h[1][:80] for h in hits[:cap]]


def _entities_from(state: ResearchState, zh_only: bool) -> list[str]:
    """第二轮起的自主扩展检索词：从**段落概括**里抽实体。

    原先读的是 `claims`，而 claims 从 R5 起恒为空数组（R7 实测发现的
    三个空转点之一），所以这个机制也是死的。改成读段落概括。
    """
    repo = get_repo()
    try:
        segs = _segments_for_assess(repo, state["session_id"])
    except Exception:
        return []
    if not segs:
        return []
    cands: list[str] = []
    for s in segs[-120:]:
        txt = f"{s.get('heading_path') or ''} {s.get('summary') or ''}"
        for pat in (r"([A-Z][A-Za-z0-9]{2,}(?:\s+[A-Z][A-Za-z0-9]{2,}){0,2})",
                    r"([\u4e00-\u9fff]{2,8}(?:公司|集团|团队|实验室|大学|研究院|工厂|产线))"):
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

    # 与上一轮的查询词重合多少——重复检索烧配额、产重复段落、
    # 还会把 no_new_streak 推高导致提前停。这个数必须一眼可见。
    prev_q = state.get("plan_queries") or {}
    prev_set = {str(x).strip().lower() for lang in ("zh", "en")
                for x in (prev_q.get(lang) or [])}
    cur_set = {str(x).strip().lower() for lang in ("zh", "en")
               for x in (queries.get(lang) or [])}
    repeat = sorted(prev_set & cur_set)

    events.append({"kind": "retrieve", "round": rnd,
                   "queries": queries, "hits": len(hits),
                   "repeat_queries": len(repeat),
                   "accepted": len(new_sources), "skipped": skipped[:20]})
    used = list(state.get("used_queries") or [])
    for lang in ("zh", "en"):
        used += [str(x).strip() for x in (queries.get(lang) or [])]
    repo.save_run_trace(sid, rnd, "retrieve", {
        "queries": queries, "hits": len(hits), "accepted": len(new_sources),
        "repeat_queries": repeat, "new_sources": len(new_sources),
        "skipped": skipped[:10],
    })
    return {"round": rnd, "visited_urls": list(seen_norms),
            "url_count": len(visited) + len(hits), "sources": new_sources,
            "pending_extract": pending, "conflict_hashes": known_hashes,
            "used_queries": used,
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

def _unused_queries(state: ResearchState) -> set[str]:
    """已经搜过的词。第 2 轮绝不能原样再搜一遍。"""
    used = set(state.get("used_queries") or [])
    q = state.get("plan_queries") or {}
    for lang in ("zh", "en"):
        for x in (q.get(lang) or []):
            used.add(str(x).strip().lower())
    return used


def _predict_stop(state: ResearchState) -> str:
    """按当前状态，停在这里的话是哪个条件会拦下。纯预测，不改路由。

    之前跑完只知道「停了」，不知道是被哪个条件停的——而这轮恰恰是
    `no_new_streak`（一个坏计数器）把研究按在 2 轮上。
    """
    if state.get("stop_reason") == "satisfied":
        return "assess 判定证据已足够"
    q = state.get("plan_queries") or {}
    if not q.get("zh") and not q.get("en"):
        return "没有可用的查询词"
    checks = [
        (state.get("consecutive_no_new", 0) >= HARD["no_new_streak"],
         f"连续 {state.get('consecutive_no_new', 0)} 轮无新增（阈值 {HARD['no_new_streak']}）"),
        (state.get("round", 0) >= HARD["max_rounds"],
         f"轮数到顶（{state.get('round', 0)}/{HARD['max_rounds']}）"),
        (state.get("url_count", 0) >= HARD["max_urls"],
         f"URL 数到顶（{state.get('url_count', 0)}/{HARD['max_urls']}）"),
        (state.get("elapsed_s", 0) >= HARD["timeout_min"] * 60,
         f"超时（{state.get('elapsed_s', 0)/60:.0f}/{HARD['timeout_min']} 分钟）"),
        (state.get("cost_tokens", 0) >= HARD["max_tokens"],
         f"token 到顶（{state.get('cost_tokens', 0)}/{HARD['max_tokens']}）"),
    ]
    for hit, why in checks:
        if hit:
            return f"硬条件：{why}"
    return ""


@scoped
def node_assess(state: ResearchState) -> dict:
    """判断每节证据够不够、下一轮搜什么。

    **2026-10-06 重写**：原先它读 `all_evidence[].claims`，而 claims 从 R5
    证据重构起就是**硬编码的空数组**（`extract_batch.py:113`），
    于是每轮都走「no claims」早退。这个空转打中了三处——
    assess 本身、`new_claims` 的计算（恒为 0，导致 `consecutive_no_new`
    每轮无条件 +1）、`_entities_from`（第 2 轮自主扩展）。
    后果是第 2 轮检索用了和第 1 轮一字不差的查询词。

    现在改为读**段落**：按小节把「题面命中的段落」喂进去，
    输入从 150 条平铺 claim 降到每节 3 条概括。
    """
    repo = get_repo()
    sid = state.get("session_id") or ""
    rnd = state.get("round", 0)
    outline = state.get("outline") or []

    segs = _segments_for_assess(repo, sid) if sid else []
    n_seg = len(segs)

    # ---- 本轮是否真有新增：按**段落数**算，不是按 claims（那个恒为 0）----
    prev_segs = state.get("last_round_segments")
    new_segs = (n_seg - prev_segs) if prev_segs else n_seg
    streak = state.get("consecutive_no_new", 0) + 1 if new_segs <= 0 else 0

    # ---- 按小节组装输入 ----
    section_views = []
    for s in outline:
        hits = _match_for_section(s, segs)
        section_views.append({
            "id": s.get("id"), "title": s.get("title"),
            "question": (s.get("question") or "")[:120],
            "题面词": _section_terms(s)[:8],
            "命中段落数": len(_all_hits(s, segs)),
            "材料样例": hits,
        })

    data = {}
    if section_views and n_seg:
        brief = json.dumps({"本轮可用段落数": n_seg,
                            "小节": section_views}, ensure_ascii=False)[:24000]
        try:
            data = LLM.complete_json("assess", brief, system=prompts.ASSESS)
        except Exception as e:
            print(f"⚠️  [assess] 失败: {str(e)[:120]}", flush=True)

    enough = bool(data.get("has_enough"))
    gaps = [g for g in (data.get("gaps") or []) if isinstance(g, dict)]

    # ---- 下一轮搜什么：**不能沿用已经搜过的词** ----
    nq = data.get("next_queries") or {}
    used = _unused_queries(state)
    raw = ([str(x).strip() for x in (nq.get("zh") or []) if str(x).strip()]
           + [str(x).strip() for x in (nq.get("en") or []) if str(x).strip()])
    fresh = [q for q in raw if q.lower() not in used]
    queries = {
        "zh": [q for q in fresh if _is_zh(q)][:MAX_QUERIES_PER_LANG],
        "en": [q for q in fresh if not _is_zh(q)][:MAX_QUERIES_PER_LANG],
    }
    dropped = [q for q in raw if q.lower() in used]
    fallback_used = False
    if not queries["zh"] and not queries["en"]:
        # 兜底：**绝不原样再搜一遍**。去旧词 + 自主扩展都拿不到东西时，
        # 才允许沿用旧查询词——但那说明该收工了，不该继续烧配额。
        if gaps or not enough:
            queries = {"zh": _dedup_zh(_entities_from(state, True)),
                       "en": _dedup_zh(_entities_from(state, False))}
            fallback_used = True
        if not queries["zh"] and not queries["en"]:
            queries = {}

    # 预测「如果现在就收工，是哪个条件拦下的」——纯观测，不改路由。
    # 之前跑完只知道「停了」，不知道被谁停的，而那轮恰恰是被一个
    # 坏掉的计数器（no_new_streak）按在 2 轮上的。
    probe_state = dict(state)
    probe_state.update({"plan_queries": queries or {},
                        "consecutive_no_new": streak,
                        "stop_reason": "satisfied" if enough else ""})
    stop_would_be = _predict_stop(probe_state)

    out_state = {
        "plan_queries": queries or {},
        "consecutive_no_new": streak,
        "last_round_segments": n_seg,
        "stop_reason": "satisfied" if enough else "",
        "pending_revision": {"op": "none"},   # 大纲已获用户审批，assess 不再擅自改
        "events": [{"kind": "assess", "round": rnd,
                    "segments_total": n_seg, "new_segments": new_segs,
                    "no_new_streak": streak, "has_enough": enough,
                    "thin_sections": [v["id"] for v in section_views
                                      if v["命中段落数"] == 0],
                    "gaps": len(gaps),
                    "queries_fresh": len(fresh), "queries_dropped_repeat": len(dropped),
                    "queries_fallback": fallback_used,
                    "stop_would_be": stop_would_be,
                    "gap_detail": [f"{g.get('section_id')}:{str(g.get('what_missing'))[:70]}"
                                   for g in gaps][:8]}],
    }
    repo.save_run_trace(sid, rnd, "assess", {
        "segments_total": n_seg, "new_segments": new_segs, "streak": streak,
        "has_enough": enough,
        "sections": [{"id": v["id"], "hits": v["命中段落数"]} for v in section_views],
        "gaps": gaps[:10],
        "queries": queries, "dropped_repeat": dropped,
        "fallback_used": fallback_used,
        "stop_would_be": stop_would_be,
        "brief_chars": len(json.dumps(
            {"本轮可用段落数": n_seg, "小节": section_views},
            ensure_ascii=False)[:24000]),
    })
    return out_state


def _all_hits(section: dict, segs: list[dict]) -> list[str]:
    terms = _section_terms(section)
    out = []
    for s in segs:
        blob = f"{s.get('heading_path') or ''} {s.get('summary') or ''}"
        if any(t in blob for t in terms):
            out.append(s.get("summary") or "")
    return out


def _is_zh(s: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", s))


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
    # 闸门要看到**全部**段落，包括没概括出来的。过滤发生在闸门之后，
    # 而且只在闸门判定相关之后——不能让「概括失败」把证据变成看不见。
    segs = [c for c in repo_segments if src_no.get(c["source_id"])]
    n_no_summary = sum(1 for c in segs if not (c.get("summary") or "").strip())
    gate = relevance.gate_segments(segs, kws, state["question"][:80])
    repo.mark_chunk_topic(sid, gate["marks"])

    # 来源清单也从 src_no 反过来编号。两处各写一遍 enumerate 的话，
    # 改了一处忘了另一处，报告里每个 [Sn·m] 就会指向清单里下一行的来源——
    # 而且不会报任何错，只能靠肉眼看引用对不对。
    src_lines = [f"[S{src_no[s['id']]}] {s.get('title') or s['domain']} | {s['domain']} "
                 f"| {s.get('source_type')} | {s.get('source_date') or '日期未知'}"
                 f" | {s.get('source_nature') or '性质未判'}"
                 f" | 正文 {s.get('char_count') or 0} 字"
                 for s in all_sources]

    keep = state.get("recalled_approved") or []
    earlier = [{"summary": c.get("summary"), "date": c.get("source_date")}
               for c in keep[:30]] if keep else []

    out = write_subgraph.run_write(
        session_id=sid, question=state["question"],
        outline=state.get("outline") or [],
        signals=(pr.get("signals") or {}),
        segments=segs, on_topic=gate["keep"], earlier=earlier,
        src_lines=src_lines,
        # 概括缺失的段数要一路传到报告里。静默丢证据比报错危险得多——
        # 报告看起来完整，实际上缺了料，而且从成品里永远看不出来。
        n_no_summary=n_no_summary)

    md = out.get("report_markdown") or ""
    if head:
        md = head + md
    repo.save_report(sid, md)
    return {"report_markdown": md,
            "events": (out.get("events") or []) + [
                {"kind": "write", "round": state.get("round"),
                 "chars": len(md), "sources": len(src_lines),
                 "segments_total": len(segs), "segments_kept": len(gate["keep"]),
                 "segments_without_summary": n_no_summary,
                 "gate": gate["stats"],
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
