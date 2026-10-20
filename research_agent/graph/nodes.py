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
from ..ingest.extract import Fetcher
from ..ingest.split import RecursiveSplitter
from ..llm.provider import LLMClient, fire_progress, session_scope
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

# 边际收益阈值（2026-10-07，C1-d）：本轮新增段落 / 累计段落 低于它就收手。
# 定 20%：实测第 3、4 轮各只多 10 段（对累计 222 是 4.5%），边际收益早就是零，
# 但 assess 说了 7 轮「不够」。让模型自己发现这件事太慢，交给可算的硬判据。
MIN_ROUND_GAIN = 0.20
HARD = dict(
    # 从 8 收到 4。边际收益在第 3 轮就趋零，跑满 8 轮纯属烧配额。
    # 3–4 轮是「够用」与「别浪费」的平衡点。
    max_rounds=4,
    max_urls=50,         # 抓多少个 URL 就收
    # 实测（2026-10-06 接主图那轮）：17.7 分钟 / 421k token 就跑完了，
    # 而那是因为 assess 空转导致第 2 轮在重复检索。**修好 assess 后轮数会增加**，
    # 原来 20 分钟的墙会立刻变成主要失败原因，所以提到 30。
    timeout_min=30,
    # 原来这里还有一条 max_tokens=800_000，2026-10-08 删掉。理由两条：
    #
    # ① **它从来没生效过**。判据读 state["cost_tokens"]，而那个字段只在
    #    node_finalize 写——finalize 跑在检索循环**之后**，建 session 时也
    #    没进 INSERT，所以循环期间它恒为 0：`0 >= 800_000` 永远 False。
    #    它长得像在工作（有默认值、不报错、正常返回空串）。
    #
    # ② **就算修好链路也够不到**。实测检索期累计：跑满 7 轮那次 398,051，
    #    5 轮那次 344,272，2 轮那次 169,940。逐轮增量递减极快
    #    （7 轮那次第 3、4 轮各只多 11.8k / 13.5k）。只有「4 轮都重复
    #    首轮那种密集抓取」才可能到 832k，实测里从未发生。
    #
    # 剩下的四条测的都是**物理量**（轮数、URL 数、墙钟、连续无新增），
    # 不会像 token 那样因为缓存命中而漂移。真失控时它们先撞上。
    # 这是坑 #9 的第二次出现：设计授权了但下游没实现，外观像别的问题。
    # 修好之前它是个坏计数器（`produced` 取 claims 数量、恒为 0，
    # 于是 streak 每轮无条件 +1）。修好之后它才真的是「连续几轮没新东西」，
    # 2 轮是合理的停止阈值，所以**这个值不动**，变的是它的含义。
    no_new_streak=2,
)
MAX_QUERIES_PER_LANG = 5          # 单次每种语言的查询条数上限

SPLITTER = RecursiveSplitter(max_tokens=2000, target_tokens=1000)
LLM = LLMClient()
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


def _is_time_sensitive(q: str) -> bool:
    ql = q.lower()
    return any(m in ql for m in TIME_MARKERS)


def _timed_node(name: str):
    """给节点记墙钟耗时。

    `llm_call` 只记 LLM 调用的耗时，做成本优化时看不见两块：
    检索/抓取的网络 IO，和节点里不调模型的准备工作——常常才是大头。

    写进 `run_trace`（kind = `node:<名字>`）。节点名要进 kind 而不是 detail：
    run_trace 的主键是 (session_id, round, kind)，同一轮的多个节点不能重名。
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(state: dict):
            sid = state.get("session_id") or ""
            t0 = time.time()
            try:
                out = fn(state)
                # 累计**机器时间**，这是 `_predict_stop` 的超时判据唯一的数据来源。
                # ⚠️ 不用「`started_at` 到现在」的墙钟：`await_approval` 是
                # `interrupt()`，人去审批可能挂几个小时，那段不该算进预算。
                # 这里累加的是各节点自己的耗时，审批等待天然不在内（interrupt
                # 抛异常出去，走不到下面这行）。
                if isinstance(out, dict):
                    out["elapsed_s"] = round(
                        float(state.get("elapsed_s") or 0.0) + (time.time() - t0), 1)
                return out
            finally:
                if sid:
                    try:
                        get_repo().save_node_timing(
                            sid, name, (time.time() - t0) * 1000,
                            int(state.get("round", 0) or 0))
                    except Exception:
                        pass
        return wrapper
    return deco


# ================================================================ 探针

@scoped
@_timed_node("probe")
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
@_timed_node("outline")
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

    # 探针给不出轮廓时，模型就没有真实维度可锚，`grounded_in` 写不出来 →
    # sections 直接空 → 走上面的极简兜底。**整条链是静默的**：
    # 从「探针过载降级」到「报告只剩一节」，中间没有任何一句提示。
    # 实测（2026-10-06）：探针 529 过载 4 次全挂，outline 输入从 5,172 token
    # 掉到 382，产出一份 19,261 字、单节的报告，而事件里全是 "degraded: true"。
    #
    # 🔴 **2026-10-18 修：`warn = []` 原来写在下面第 215 行，晚于这里的
    # `warn.append(...)`。** 于是**框架退化这条分支一进就崩**
    # （`UnboundLocalError`）—— 它从来没被执行过，因为正常路径不碰它。
    # 探针降级恰恰是最需要这条分支的时候。
    # 教训：局部变量列表要在**所有**分支之前初始化，
    # 「先写正常路径、告警顺手写在后面」是最容易漏的写法。
    warn: list[str] = []
    probe_degraded = bool(state.get("probe_degraded")) or not sig
    raw_n = len(raw_sections)
    if probe_degraded or raw_n == 0 or len(outline) <= 2:
        why = []
        if probe_degraded:
            why.append("探针降级（过载或未产出轮廓）")
        if raw_n == 0:
            why.append("模型产出的 sections 为空数组")
        elif len(outline) <= 2:
            why.append(f"只有 {len(outline)} 个小节通过 grounded_in 校验（原产出 {raw_n} 个）")
        print(f"🚨 框架退化：{'；'.join(why)}。已退回极简框架，"
              f"检索目标会很窄、报告会明显偏薄。", flush=True)
        warn.append("框架退化：" + "；".join(why))

    qz = (plan.get("queries") or {}).get("zh") or []
    qe = (plan.get("queries") or {}).get("en") or []
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
    # 审批时把「这一节有多少材料撑着」一起给出去。
    # 大纲是在材料到位之前写的，所以用户要能一眼看出哪几节偏薄——
    # 否则他批的是一份未经检验的框架（详见 runner.py 文件头）。
    outline_view = []
    for s in (state.get("outline") or []):
        v = {"id": s.get("id"), "title": s.get("title"),
             "question": s.get("question"), "kind": s.get("kind")}
        note = str(s.get("材料支撑") or "").strip()
        if note:
            v["修订说明"] = note
        outline_view.append(v)
    payload = {
        "session_id": state["session_id"],
        "question": state["question"],
        "time_sensitive": state.get("time_sensitive", False),
        "probe": {k: pr.get(k) for k in
                  ("quality", "degraded", "headline", "ambiguity", "deep_urls_read")},
        "signals": (pr.get("signals") or {}),
        "outline": outline_view,
        "已修订": bool(state.get("outline_revision_log")),
        "修订记录": list(state.get("outline_revision_log") or [])[-3:],
        # ★ 这份框架是「材料撑得住的」还是「尽力而为的」——
        #   两者可信度完全不同，用户有权在批准之前知道。
        #   撞了轮数/URL/超时上限时，材料其实并不够。
        "框架可信度": {
            "种类": _stop_kind(state) or "仍在补充材料",
            "停止原因": state.get("stop_reason") or "",
            "说明": ("材料已经够了，这份框架的每一节都有材料支撑"
                     if _stop_kind(state) == "satisfied"
                     else "还没搜到够的材料就被上限拦下了，"
                          "这份框架只是尽力而为——请当作方向而非承诺"),
        },
        "当前材料": {
            "来源数": len(state.get("sources") or []) or state.get("url_count", 0),
            # ⚠️ 这里读的是 `segments` / `on_topic`，但它们是 **WriteState** 的字段
            # （state.py:160-161），**主图 ResearchState 里根本没有**——
            # 所以审批页一直显示「段落数 0、过闸门 0」，而实际有 50+ 段。
            # 2026-10-11 修：主图里正确的字段是 `all_evidence`（累积证据），
            # `last_round_segments` 是 assess 算过的可用段落数。
            # 前端会照实显示这个数，显示 0 比不显示更糟。
            "段落数": (len(state.get("all_evidence") or [])
                       or state.get("last_round_segments", 0)),
            "已抽取段数": state.get("last_round_segments", 0),
        },
        "queries": state.get("plan_queries", {}),
    }
    reply = interrupt(payload)
    # ⚠️ 原来这里还按 `approved_recalled_ids` 筛一遍 `recalled_chunks`
    # （历史召回）。召回 2026-10-12 砍掉后，`recalled_chunks` **从没有任何写入方**，
    # 这里恒为空转，报告里的「认知演进」一节也因 `if earlier:` 永不成立而永不出现。
    # 2026-10-13 连同那条链路一起删干净——空转的代码只会让人以为功能坏了。
    if isinstance(reply, dict):
        return {"plan_approved": bool(reply.get("approved", True))}
    return {"plan_approved": bool(reply)}


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


# 标点之外，中文里的虚词/连接词也是天然边界：按它们切出来的多半是短语，
# 而整句（8–12 字）在段落概括里原样出现的概率几乎是 0。
_TERM_SPLIT = re.compile(
    r"[、，,。；;：:？?！!（）()「」\"'／/\s—－\-_=&＆]+"
    r"|(?:以及|并且|如何|是否|分别|什么|哪些|怎样|为什么|多少|以及)"
    r"|(?:的|与|和|及|或|等|对|让|使|把|被|从|到|在|为|中)"
)


def _section_terms(section: dict) -> list[str]:
    """从一小节的题面里抽「能拿去匹配材料的词」。

    不做分词——中文实体的子串匹配本来就准，而分词器会把
    「中科深蓝汇泽」切成碎片，反而匹配不上。
    按标点与虚词切开，取长度够的片段当实体。

    **要二次切分**（2026-10-06 实测后加）：原先只按标点切，
    中文问句里标点之间的部分常常是 8–12 字的整句
    （「固—固界面失效机理」「固态电池安全性的路线依赖性」），
    这种串在段落概括里原样出现的机会几乎是 0——
    实测 4 个小节因此被误判成「命中 0」，而它们的材料其实都很充足
    （sec3 的段落里「界面」出现 26 次、sec7 的「安全」22 次）。
    """
    raw = f"{section.get('title') or ''} {section.get('question') or ''}"
    parts = _TERM_SPLIT.split(raw)
    out, seen = [], set()
    for p in parts:
        p = (p or "").strip()
        if len(p) < 2:
            continue
        has_zh = bool(re.search(r"[\u4e00-\u9fff]", p))
        if has_zh and len(p) > 10:
            continue                       # 中文长片段多半是整句，不是实体
        if (not has_zh) and len(p) < 4:
            continue
        k = p.lower()
        if k in seen:
            continue
        seen.add(k); out.append(p)
    return out[:16]


def _score_sections(section: dict, segs: list[dict]) -> list[tuple[int, str]]:
    """这节的题面词在每条段落里命中几个，按分数降序返回 [(分数, 概括)]。

    **要的是排序而不是 0/1 命中**（2026-10-06 实测后改）：
    题面和段落用词本来就会不一样（「固—固界面失效机理」vs「固固界面接触退化」），
    要求整串命中会全盘漏掉。打分排序能把相关段落捞上来，
    剩下的判断交给 LLM——它才是能做语义匹配的那个。
    """
    terms = _section_terms(section)
    if not terms:
        return []
    scored = []
    for s in segs:
        blob = f"{s.get('heading_path') or ''} {s.get('summary') or ''}"
        n = sum(1 for t in terms if t in blob)
        if n:
            scored.append((n, (s.get("summary") or "").strip()))
    scored.sort(key=lambda x: -x[0])
    return scored


def _match_for_section(section: dict, segs: list[dict], cap: int = 3) -> list[str]:
    """这节最相关的 cap 条段落概括（按命中词个数排序）。"""
    return [s[:80] for _, s in _score_sections(section, segs)[:cap]]


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


def _fetched_count(state: ResearchState) -> int:
    """**真正抓下来的**来源数，用来卡 max_urls 预算。

    别和 `visited_urls` 混为一谈（2026-10-06 修）：

    - `visited_urls` 记**尝试过**的 URL（含抓失败的），职责是去重，
      防止同一个 403/超时页面被反复重试
    - `url_count` 记**成功入库**的来源数，职责是预算

    原来两者是一个数（`len(visited) + len(hits)`），于是抓失败的
    30 个（too_short / 403 / captcha）也占额度——实测一轮搜到 50 个、
    只成功 20 个，却把 50 的上限一次用光，研究只能跑 1 轮。
    预算是为了限制「抓了多少内容」，不是「试了多少次」。
    """
    return len(state.get("sources") or [])


@_timed_node("retrieve")
def node_retrieve(state: ResearchState) -> dict:
    repo = get_repo()
    sid = state["session_id"]
    # 「本次是第几轮」——`state["round"]` 是**已完成**轮数，所以 +1。
    # ⚠️ 这和 `@_timed_node` 记进 run_trace 的那个 round 差 1（那里记的是
    # 「进入时已完成几轮」，本节点是唯一执行中就推进 round 的）。
    # 两个口径都对、都不能改，详见 `repo.save_node_timing` 的 docstring。
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

    # 第 1 轮把探针深读过的页面当种子放最前面（2026-10-20）：原来
    # `node_probe_deep` 抓完喂给轮廓模型就丢，页面里的事实进了轮廓/
    # 规划却被下游当成「无出处的知识」。先过 `_collect` 占进
    # `seen_norms`——搜索命中里的重复 URL 会自然去重，种子也因此
    # 记入 visited_urls，后续轮次不会再抓。之后走同一套抓取/哈希
    # 去重/guess_source_type/分段/extract/gate，不绕过任何环节。
    seeded: list[SearchHit] = []
    if rnd == 1:
        deep = ((state.get("probe") or {}).get("deep_urls_read")) or []
        seeded = _collect([SearchHit(url=u, engine="probe") for u in deep])

    all_q = [(q, "zh-CN", 8, SX) for q in queries["zh"]] + \
            [(q, "en", 6, TV) for q in queries["en"]]

    async def call(prov, q, lang, lim, i):
        ev = {"kind": "search_query", "i": i + 1, "of": len(all_q),
              "lang": lang, "query": q}
        try:
            res = await asyncio.to_thread(prov.search, q, language=lang, limit=lim)
            ev["hits"] = len(res)
            return res
        except Exception as e:
            ev["error"] = f"{type(e).__name__}: {e}"[:120]
            raise
        finally:
            # 环节内实时进度：查询一条条回来就一条条推，不等整轮跑完
            fire_progress("retrieve", ev)

    async def run():
        tasks = [call(prov, q, lang, lim, i)
                 for i, (q, lang, lim, prov) in enumerate(all_q)]
        res = await asyncio.gather(*tasks, return_exceptions=True)
        flat: list[SearchHit] = []
        for r in res:
            if isinstance(r, list):
                flat.extend(r)
        return _collect(flat)

    hits = seeded + asyncio.run(run())
    budget_left = max(0, HARD["max_urls"] - _fetched_count(state))
    if budget_left <= 0:
        # 额度用完就别再抓了——原来这里抓完再截，白白花掉一次网络往返
        events.append({"kind": "retrieve", "round": rnd, "queries": queries,
                       "hits": 0, "accepted": 0,
                       "note": f"URL 预算已用尽（{_fetched_count(state)}/"
                               f"{HARD['max_urls']}），本轮不再抓取",
                       "repeat_queries": 0, "skipped": [],
                       "seeded_from_probe": len(seeded)})
        return {"round": rnd, "events": events}
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

    # 本轮实际搜的词里，有几个是**更早的轮次已经搜过的**。
    # 重复检索烧配额、产重复段落、还会把 no_new_streak 推高导致提前停。
    #
    # ⚠️ 必须拿 `used_queries`（历史实际发过的词）比，**不能拿 plan_queries 比**：
    # `_collect_queries` 就是以 plan_queries 为 base 的，拿它比等于自己跟自己比，
    # 必然大量重合——第一版埋点就是这么写的，于是每轮都误报「重复 10 条」。
    used = {str(x).strip().lower() for x in (state.get("used_queries") or [])}
    cur_set = {str(x).strip().lower() for lang in ("zh", "en")
               for x in (queries.get(lang) or [])}
    repeat = sorted(used & cur_set)

    events.append({"kind": "retrieve", "round": rnd,
                   "queries": queries, "hits": len(hits),
                   "repeat_queries": 0 if rnd <= 1 else len(repeat),
                   "accepted": len(new_sources), "skipped": skipped[:20],
                   "seeded_from_probe": len(seeded)})
    used = list(state.get("used_queries") or [])
    for lang in ("zh", "en"):
        used += [str(x).strip() for x in (queries.get(lang) or [])]
    repo.save_run_trace(sid, rnd, "retrieve", {
        "queries": queries, "hits": len(hits), "accepted": len(new_sources),
        "repeat_queries": repeat if rnd > 1 else [],
        "fetched_total": len(state.get("sources") or []) + len(new_sources),
        "new_sources": len(new_sources), "skipped": skipped[:10],
        "seeded_from_probe": len(seeded),
    })
    return {"round": rnd, "visited_urls": list(seen_norms),
            "url_count": len(state.get("sources") or []) + len(new_sources),
            "sources": new_sources,
            "pending_extract": pending, "conflict_hashes": known_hashes,
            "used_queries": used,
            "events": events}


# ================================================================ 抽取

@scoped
@_timed_node("extract")
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

    # ⚠️ 送了但没判出性质的来源。**必须显式报出来**：性质只影响来源库分组和
    # 写作提示词里的「性质」字段，判不出来不影响流程，所以丢掉了也没人发现。
    # 2026-10-14 实测：`_run_nature_batch` 的下标拿全局值去比切片长度，
    # 第 2 批起整批静默丢弃，41 个来源丢了 21 个（占 51%），
    # 而来源库里那一组被标成「未判」——**看起来像模型判不出来，其实是代码丢的**。
    unjudged = out.get("unjudged_sources") or []
    if unjudged:
        print(f"⚠️  {len(unjudged)}/{len(items)} 个来源没判出性质，"
              f"来源库里会归到「未判」组：{unjudged[:5]}", flush=True)

    return {
        "pending_extract": [],
        "all_evidence": results,
        "events": (out.get("events") or []) + [{
            "kind": "extract", "round": state.get("round"),
            "sources": len(items), "ok": n_ok, "errors": n_err,
            "segments": n_seg, "summarized": n_sum,
            "summarize_failed_batches": out.get("failed"),
            "unjudged_sources": len(unjudged),
            "gate": stats,
        }],
    }


# ================================================================ 段级闸门

@scoped
@_timed_node("gate")
def node_gate(state: ResearchState) -> dict:
    """段级相关性闸门：每轮抽取之后、assess 之前。

    原先它在 `node_write` 里一次性跑完。两个后果（2026-10-18 实测 v12）：

    1. `assess` 一直在**未过滤的池子**上判断「材料够不够、要不要再搜
       一轮」——`_segments_for_assess` 的 `WHERE on_topic=1` 形同虚设
       （`on_topic` 默认 1，唯一标记点在 write），v12 的池子里 41% 是
       后来被闸门毙掉的段落。
    2. 192 次闸门调用挤在 write 头部，贴着 rate_limit 墙跑了 4.8 分钟，
       把真正的写作推迟了。

    挪到每轮抽取之后：`assess` 看到的就是过滤后的真池子，
    闸门调用也摊进各轮。每段只判一次 —— 靠 `gate_method IS NULL`
    挑本轮新入库的段，多轮检索不会重复判。
    """
    repo = get_repo()
    sid = state["session_id"]
    pr = state.get("probe") or {}
    kws = relevance.build_keywords((pr.get("signals") or {}), state["question"])

    # 闸门要看到**全部**新段落，包括没概括出来的。过滤发生在闸门之后，
    # 而且只在闸门判定相关之后——不能让「概括失败」把证据变成看不见。
    segs = [dict(r) for r in repo.q(
        "SELECT id, source_id, heading_path, content, summary, token_count "
        "FROM chunk WHERE session_id=? "
        "AND (gate_method IS NULL OR gate_method='') ORDER BY rowid", (sid,))]
    if not segs:
        return {"events": [{"kind": "gate", "round": state.get("round"),
                            "segments_total": 0, "segments_kept": 0}]}

    gate = relevance.gate_segments(segs, kws, state["question"][:80])
    repo.mark_chunk_topic(sid, gate["marks"])

    return {"events": [{
        "kind": "gate", "round": state.get("round"),
        "segments_total": gate["stats"]["segments"],
        "segments_kept": gate["stats"]["on_topic"],
        "gate": gate["stats"],
    }]}


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
    if state.get("stop_reason"):
        return str(state["stop_reason"])
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
    ]
    for hit, why in checks:
        if hit:
            return f"硬条件：{why}"
    return ""


def _count_urgency(gaps: list[dict]) -> dict:
    """按 urgency 分组统计缺口。用来判断「还有没有值得再搜的缺口」。

    `low` 的缺口是「网上确实没有」，它不该成为继续检索的理由——
    实测那 7 轮里每轮都填满 10 个 low/medium 缺口，于是永远说不够。
    """
    out: dict[str, int] = {}
    for g in gaps or []:
        k = str(g.get("urgency") or "?").lower()
        out[k] = out.get(k, 0) + 1
    return out


def _assess_history(repo, sid: str, rnd: int) -> dict:
    """给 assess 看「前面几轮发生了什么」。

    2026-10-07 实测：只告诉它「现在缺什么」，它就会永远说不够——
    三轮 `has_enough` 全 False，第 3 轮边际收益只剩 8% 却还有 6 个 high 缺口。
    原因是它做不了这个推理：「这个缺口我前面报过两轮了，每轮新增的材料里
    都没有它」⇒「那大概就是网上没有」。

    **把历史喂给它，这个推理才做得出来。**
    """
    if rnd <= 0 or not sid:
        return {}
    prev = None
    for e in reversed(repo.run_trace(sid)):
        if e.get("kind") == "assess" and e.get("round") == rnd - 1:
            prev = e
            break
    if not prev:
        return {}
    out: dict = {
        "这是第几轮检索": rnd + 1,
        "上一轮新增段落数": prev.get("new_segments"),
        "上一轮累计段落数": prev.get("segments_total"),
        "上一轮你的判断": ("证据已足够（已停工）" if prev.get("has_enough")
                           else f"仍不够，缺口分级 {prev.get('gaps_by_urgency')}"),
    }
    pg = prev.get("gaps") or []
    if pg:
        out["上一轮你报过的缺口"] = [
            {"section_id": g.get("section_id"), "urgency": g.get("urgency"),
             "what_missing": str(g.get("what_missing"))[:80]} for g in pg]
    return out


@scoped
@_timed_node("assess")
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
        scored = _score_sections(s, segs)
        best = scored[0][0] if scored else 0
        section_views.append({
            "id": s.get("id"), "title": s.get("title"),
            "question": (s.get("question") or "")[:120],
            "题面词": _section_terms(s)[:10],
            # 「有几条段落沾边」和「沾得多准」是两个数。
            # 实测 sec7 命中 68 段但最高分只有 2——材料很多却都不对题；
            # 只看命中数会给出「材料充足」的错误印象。
            "沾边段数": len(scored),
            "最高吻合度": best,
            "材料样例": [x[1][:80] for x in scored[:3]],
        })

    data = {}
    if section_views and n_seg:
        hist = _assess_history(repo, sid, rnd)
        # 框架还没获审批时，才让 assess 顺便评估「这节的要求材料撑不撑得住」。
        # 已审批的框架绝不该被程序改动——所以这里只**告诉**模型状态，
        # 由它决定给不给 outline_review（设计约定，不是硬门控）。
        approved = bool(state.get("plan_approved"))
        brief = json.dumps({"框架状态": "已获用户审批" if approved else "尚未审批",
                            "本轮可用段落数": n_seg,
                            "检索历史": hist or "这是第一轮，没有历史",
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

    # ---- 边际收益：本轮新增占累计的比例 ----
    # 提示词那层（`has_enough`）是判断题；这一层是**可算的数字**。
    # 实测（2026-10-07）：assess 连说 7 轮「不够」，而第 3、4 轮各只多 10 段、
    # 第 7 轮只多 4 段，边际收益早就是零，却一直搜到 URL 撞天花板。
    # 让模型自己发现这件事太慢，交给硬判据。
    gain = new_segs / n_seg if n_seg else 1.0
    low_gain = bool(n_seg and gain < MIN_ROUND_GAIN and rnd > 0)

    # 停止原因要**可归因**：是提示词让模型说了「够了」，
    # 还是机制层的硬判据拦下的。分不清就没法判断该改哪边。
    stop_would_be, stop_layer = "", ""
    if enough:
        stop_would_be, stop_layer = "assess 说够了", "prompt"
    elif streak >= HARD["no_new_streak"]:
        stop_would_be, stop_layer = f"连续 {streak} 轮无新增", "mechanism"
    elif low_gain:
        stop_would_be = (f"边际收益过低（本轮新增 {new_segs} 段 = {gain:.0%}，"
                         f"阈值 {MIN_ROUND_GAIN:.0%}）")
        stop_layer = "mechanism"
    elif rnd >= HARD["max_rounds"]:
        stop_would_be = f"轮数到顶（{rnd}/{HARD['max_rounds']}）"
        stop_layer = "mechanism"

    # 框架校准的产物。算一次、后面几处都用它，别重复调 `_to_revision`
    # （它有副作用式的过滤：题面过短的收窄会被丢掉）。
    _rev = _to_revision(data.get("outline_review"))

    out_state = {
        "plan_queries": queries or {},
        "consecutive_no_new": streak,
        "last_round_segments": n_seg,
        "stop_reason": stop_would_be,
        "stop_layer": stop_layer,      # 路由的 _stop_kind 要读它（区分「够了」和「撞顶」）
        "pending_revision": _rev,
        "_expect_revision": _rev["op"] == "revise",   # 给 apply_revision 对账用
        "events": [{"kind": "assess", "round": rnd,
                    "segments_total": n_seg, "new_segments": new_segs,
                    "round_gain": round(gain, 4),
                    "no_new_streak": streak, "has_enough": enough,
                    "thin_sections": [v["id"] for v in section_views
                                      if v["最高吻合度"] <= 1],
                    # 每节材料厚度明细（前端流程页的小节刻度）。thin_sections
                    # 只留 0/1 判薄，读不出「厚多少」——所以整列给出去。
                    "section_material": [{"id": v["id"],
                                          "hits": v["沾边段数"],
                                          "best": v["最高吻合度"]}
                                         for v in section_views],
                    "gaps": len(gaps),
                    "gaps_by_urgency": _count_urgency(gaps),
                    "queries_fresh": len(fresh), "queries_dropped_repeat": len(dropped),
                    "queries_fallback": fallback_used,
                    "stop_would_be": stop_would_be or "（会继续检索）",
                    "stop_layer": stop_layer or "—",
                    "outline_actions": _action_hist(data.get("outline_review")),
                    "outline_given": bool(data.get("outline_review")),
                    "gap_detail": [f"{g.get('section_id')}[{g.get('urgency')}]:"
                                   f"{str(g.get('what_missing'))[:60]}"
                                   for g in gaps][:8]}],
    }
    repo.save_run_trace(sid, rnd, "assess", {
        "segments_total": n_seg, "new_segments": new_segs, "streak": streak,
        "round_gain": round(gain, 4),
        "has_enough": enough,
        "stop_would_be": stop_would_be, "stop_layer": stop_layer,
        "gaps_by_urgency": _count_urgency(gaps),
        "sections": [{"id": v["id"], "hits": v["沾边段数"],
                      "top": v["最高吻合度"]} for v in section_views],
        "gaps": gaps[:10],
        "queries": queries, "dropped_repeat": dropped,
        "fallback_used": fallback_used,
        "stop_would_be": stop_would_be,
        "brief_chars": len(json.dumps(
            {"本轮可用段落数": n_seg, "小节": section_views},
            ensure_ascii=False)[:24000]),
        # ★ 框架校准的可观测性（2026-10-08 加）
        #
        # 上一轮现场跑完 0/14 节被校准，而同一份 brief 隔离重跑 4/4 都给了
        # outline_review——**现场没留下任何证据能区分这两种情况**：
        # run_trace 只存了 gaps，模型到底返没返回 outline_review 查不到。
        # 这几个字段就是为了下次能一眼看出是「模型没给」还是「给了没解析出来」。
        "rev_keys": sorted(data.keys()) if isinstance(data, dict) else [],
        "rev_given": bool(data.get("outline_review")) if isinstance(data, dict) else False,
        "rev_sections": len(((data.get("outline_review") or {}).get("sections") or []))
                         if isinstance(data, dict) else 0,
        "rev_actions": _action_hist(data.get("outline_review")),
        "rev_op": _rev["op"] if isinstance(data, dict) else "none",
    })
    return out_state


def _is_zh(s: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", s))


@scoped
def _to_revision(review: Any) -> dict:
    """把 assess 的 `outline_review` 翻成 `node_apply_revision` 认得的形状。

    **格式不对就当没给**，绝不因为模型改了输出结构就让流程报错——
    那一轮大纲原样保留，损失只是「没修正」，不是「跑不动」。
    """
    if not isinstance(review, dict):
        return {"op": "none"}
    secs = review.get("sections")
    if not isinstance(secs, list) or not secs:
        return {"op": "none"}

    narrow, remove, merge = [], [], []
    for s in secs:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "")
        if not sid:
            continue
        act = str(s.get("动作") or "").strip()
        if act == "收窄题面":
            q = str(s.get("收窄后question") or "").strip()
            # 收窄后必须仍是**一个能回答的问题**，不能是空或一截短语
            if len(q) >= 12:
                narrow.append({"id": sid, "question": q,
                               "reason": str(s.get("理由") or "")[:200]})
        elif act == "并入":
            into = str(s.get("并入") or "").strip()
            if into and into != sid:
                merge.append({"ids": [sid], "into": into,
                              "reason": str(s.get("理由") or "")[:200]})
        elif act == "删去":
            remove.append({"id": sid, "reason": str(s.get("理由") or "")[:200]})

    if not (narrow or remove or merge):
        return {"op": "none"}
    return {"op": "revise", "narrow": narrow, "remove": remove,
            "merge": merge, "reason": "按材料校准框架"}


def _action_hist(review: Any) -> dict:
    """埋点：四种动作各出现多少次。没有就返回空 dict，不占事件体积。"""
    if not isinstance(review, dict) or not isinstance(review.get("sections"), list):
        return {}
    hist: dict[str, int] = {}
    for s in review["sections"]:
        if isinstance(s, dict):
            a = str(s.get("动作") or "?").strip()
            hist[a] = hist.get(a, 0) + 1
    return hist


@_timed_node("apply_revision")
def node_apply_revision(state: ResearchState) -> dict:
    """把 LLM 自主提议的框架修订真正落到 outline 上。

    ⚠️ 开头这道自检是 2026-10-09 加的，理由是上一版**静默空转**：
    `ResearchState` 里没声明 `pending_revision`，LangGraph 把 assess 算出来的
    修订直接丢弃，本函数每次都读到 `{"op": "none"}` 然后返回空转——
    **不报错、不告警、run_trace 里还记着 op=revise**。
    「算出来了但没落地」光看返回值发现不了，必须比对。
    """
    rev = state.get("pending_revision") or {"op": "none"}
    outline = list(state.get("outline") or [])
    if rev.get("op") != "revise":
        # 自检①：state 说该改却没带修订 → 通道又断了。
        # 这个形状和「模型没给 outline_review」**症状完全一样**，
        # 所以必须能在这一层区分出来，否则下一个人还会猜错方向。
        if state.get("_expect_revision"):
            print("🚨 [apply_revision] 期待应用框架修订，但 state 里的 "
                  "pending_revision 是空的——检查 ResearchState 是否声明了它"
                  "（未声明的字段会被 LangGraph 静默丢弃）。", flush=True)
        return {"pending_revision": {"op": "none"}, "events": []}

    by_id = {s.get("id"): s for s in outline}
    removed = {r.get("id"): r.get("reason", "") for r in (rev.get("remove") or [])}
    merged = {tuple(m.get("ids") or []): m for m in (rev.get("merge") or [])}
    # 收窄题面：结构不动，只把要求换成材料撑得住的问法。
    # 这是四种动作里**最保守**的一个——不增不减小节，只改 question。
    narrowed = {r["id"]: r for r in (rev.get("narrow") or []) if r.get("id")}

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
        if sid_ in narrowed:
            # 复制一份再改，别就地改 state 里的对象
            s = dict(s)
            s["question"] = narrowed[sid_]["question"]
            s["材料支撑"] = (narrowed[sid_].get("reason") or "")[:120]
        out.append(s)

    # ⚠️ 全部小节被删/合并光 → 框架空了，后面 write 装配会炸。
    # 回退：保留原框架，不做这次修订。
    if not out:
        print(f"⚠️  [apply_revision] 本次修订会清空整个框架（删 {len(removed)}、"
              f"并 {len(merged)} 节），已放弃这次修订。", flush=True)
        return {"pending_revision": {"op": "none"}, "_expect_revision": False,
                "events": [{"kind": "outline_revision", "round": state.get("round"),
                            "skipped": True,
                            "reason": "修订后框架为空，原样保留"}]}

    # 自检②：说要改的 vs 实际改的，对不上就打警告。
    # 「模型给了修订但什么都没变」和「模型没给修订」在结果上一样，
    # 只有在这里比对才发现得了。
    promised = len(removed) + len(merged) + len(narrowed)
    applied = (len(removed)
               + sum(len(m.get("ids") or []) for m in (rev.get("merge") or []))
               + len([i for i in narrowed if i in {s.get("id") for s in out}]))
    if promised and applied != promised:
        print(f"🚨 [apply_revision] 说要改 {promised} 处、实际改了 {applied} 处——"
              f"有修订被静默丢弃（删 {list(removed)}、并 "
              f"{[m.get('ids') for m in (rev.get('merge') or [])]}、"
              f"收窄 {list(narrowed)}）", flush=True)

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
             "narrowed": [{"id": r["id"], "to": r["question"][:60]}
                          for r in (rev.get("narrow") or [])],
             "removed": list(removed.keys()),
             "merged": [f"{m.get('ids')}→{m.get('into')}"
                        for m in (rev.get("merge") or [])]}
    return {"outline": out, "pending_revision": {"op": "none"},
            "_expect_revision": False,
            "outline_revision_log": [entry],
            "events": [{"kind": "outline_revision", **entry}]}


def _stop_kind(state: ResearchState) -> str:
    """这次停下来是「够了」还是「撞顶了」。

    **审批前这两种情况必须分开**（2026-10-08）：

    - `satisfied` —— assess 说够了。框架是**材料撑得住**的，值得照着写。
    - `forced`    —— 撞了轮数 / URL / 超时 / 无新增上限。
      材料其实不够，框架只是**尽力而为**，用户审批时必须知道这一点，
      否则又变成「给未验证的承诺盖章」——只是换了个时间点。

    `prompt` 层（assess 判够）还是 `mechanism` 层（硬判据拦下），
    和 `stop_layer` 字段是同一件事，但那个记在 state 里、不参与路由；
    这里返回的是**路由要用的**，两者必须一致，所以都从 `stop_reason` 推。
    """
    if not state.get("stop_reason"):
        return ""
    # assess 算的时候把 stop_layer 一起写进了 state（路由要用，不能只留在事件里）。
    # 下面是**兜底**：stop_layer 缺失时按 stop_reason 反推。
    # 必须覆盖 assess 实际会写出的全部 5 种文案，否则会出现
    # 「边际收益过低」被当成 prompt（= 材料够了），而它其实是硬判据。
    layer = str(state.get("stop_layer") or "")
    if not layer:
        r = str(state["stop_reason"])
        prompt_markers = ("assess 说够了",)
        layer = "prompt" if r.startswith(prompt_markers) else "mechanism"
    return "satisfied" if layer == "prompt" else "forced"


def _should_stop(state: ResearchState) -> bool:
    """assess 之后该不该收工。**判据只有 `_predict_stop` 一份。**

    2026-10-08 踩过一次坑：挪审批点时我另写了一个同名逻辑的 `_should_stop`，
    结果 `_predict_stop` 变成**死代码**——只有 `assess_offline.py` 还在测它，
    而它已经不决定任何路由了。测试全绿，行为却可能不一致
    （坑 #6：自检覆盖不到出问题的地方）。

    所以这里**不重复任何判据**，只问 `_predict_stop` 有没有给出停的理由。
    """
    return bool(_predict_stop(state))


def route_after_assess(state: ResearchState) -> str:
    """assess 之后：没审批过就先校准框架，已审批就只决定继续还是收工。

    **审批点从 outline 之后挪到了第一次 assess 之后**：
    大纲是在只有探针轮廓的时候写的，材料到位之后才知道它撑不撑得住。
    让用户审批一个未经材料检验的框架，等于给未验证的承诺盖章
    （实测有一节要求「逐家核验四家企业完整台账」，而材料全是媒体转述，
    这个要求从一开始就做不到）。

    代价是审批推迟一轮检索 + 抽取（实测约 2 分钟），
    但那两步本来就要跑，只是不再跑在审批前面。
    """
    if not state.get("plan_approved"):
        # 先过一遍 apply_revision：材料撑不住的题面在这里被收窄，
        # 用户看到的就是**贴过材料**的框架。没有 outline_review 时是空转。
        return "revise"
    return "write" if _should_stop(state) else "retrieve"


def route_after_revision(state: ResearchState) -> str:
    """框架已按材料校准完：够了就给人看，不够就继续搜。"""
    if state.get("plan_approved"):
        return "write" if _should_stop(state) else "retrieve"
    # 审批前：够料与否都要过审批——审批的价值不只在于纠正大纲，
    # 还在于在烧掉几十万 token 之前让人能喊停。
    return "approve" if _should_stop(state) else "retrieve"


# ================================================================ 写报告

@scoped
@_timed_node("write")
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
        repo.x("UPDATE session SET degraded=? WHERE id=?",
               ("没有可用来源", sid))
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

    # 段级闸门已在 node_gate（每轮抽取后）跑完并落库——这里直接读
    # on_topic 标记。闸门不再占写作的时间窗口（v12 里它吃了 4.8 分钟）。
    pr = state.get("probe") or {}
    segs = [c for c in repo_segments if src_no.get(c["source_id"])]
    n_no_summary = sum(1 for c in segs if not (c.get("summary") or "").strip())
    keep = [c for c in segs if c.get("on_topic")]

    # 来源清单也从 src_no 反过来编号。两处各写一遍 enumerate 的话，
    # 改了一处忘了另一处，报告里每个 [Sn·m] 就会指向清单里下一行的来源——
    # 而且不会报任何错，只能靠肉眼看引用对不对。
    src_lines = [f"[S{src_no[s['id']]}] {s.get('title') or s['domain']} | {s['domain']} "
                 f"| {s.get('source_type')} | {s.get('source_date') or '日期未知'}"
                 f" | {s.get('source_nature') or '性质未判'}"
                 f" | 正文 {s.get('char_count') or 0} 字"
                 for s in all_sources]

    out = write_subgraph.run_write(
        session_id=sid, question=state["question"],
        outline=state.get("outline") or [],
        signals=(pr.get("signals") or {}),
        segments=segs, on_topic=keep,
        src_lines=src_lines,
        # 概括缺失的段数要一路传到报告里。静默丢证据比报错危险得多——
        # 报告看起来完整，实际上缺了料，而且从成品里永远看不出来。
        n_no_summary=n_no_summary)

    md = out.get("report_markdown") or ""
    if head:
        md = head + md
    repo.save_report(sid, md)
    # 降级产出要落进 session 表（C3）：报告正文那句声明是给人读的，
    # 列表和详情页要标「降级产出」得靠数据。两类降级都记：
    # 写作规约失败（plan_failed）与主题信息稀薄（probe_degraded）。
    deg = None
    if out.get("plan_failed"):
        deg = f"写作规约失败：{str(out['plan_failed'])[:120]}"
    elif head:
        deg = "探针判定：主题公开信息稀薄"
    if deg:
        repo.x("UPDATE session SET degraded=? WHERE id=?", (deg, sid))
    return {"report_markdown": md,
            "events": (out.get("events") or []) + [
                {"kind": "write", "round": state.get("round"),
                 "chars": len(md), "sources": len(src_lines),
                 "segments_total": len(segs),
                 # ⚠️ 不带 segments_kept：闸门结果现在由 node_gate 的
                 # `kind:"gate"` 事件按轮展示。前端 workflow.ts 判
                 # `segments_kept !== undefined` 就渲染「证据闸门」，
                 # 这里再带一遍会渲染出重复的闸门步骤。
                 "segments_without_summary": n_no_summary,
                 "degraded": bool(head)}]}


# ================================================================ 收尾

_RETRY_WAIT_RE = re.compile(r"\| wait ([\d.]+)s")


def _run_health(repo, sid: str, state: ResearchState) -> dict:
    """会话末运行健康度盘点（2026-10-20 加，平台拥堵期的可见性要求）。

    给开发者看的，不进报告。全部从 DB 现查（llm_call/chunk/source），
    不依赖内存状态——重放/续跑结果一致；`save_run_trace` 的主键
    (session_id, round, kind) upsert 使重入不产生重复行。

    `status='retry'` 行是 provider 层过载重试记账（token 0），
    单列 retries/retry_wait_s_total，不计入 calls_ok 也不计入 errors。
    """
    ev: dict = {"kind": "run_health"}

    by_stage: dict[str, dict] = {}
    for r in repo.q(
            "SELECT stage, status, error FROM llm_call WHERE session_id=?",
            (sid,)):
        st = by_stage.setdefault(
            r["stage"], {"calls_ok": 0, "errors": 0,
                         "retries": 0, "retry_wait_s_total": 0.0})
        if r["status"] == "retry":
            st["retries"] += 1
            m = _RETRY_WAIT_RE.search(r["error"] or "")
            if m:
                st["retry_wait_s_total"] += float(m.group(1))
        elif r["status"] == "error":
            st["errors"] += 1
        else:
            st["calls_ok"] += 1
    for st in by_stage.values():
        st["retry_wait_s_total"] = round(st["retry_wait_s_total"], 1)
    ev["llm_by_stage"] = by_stage

    nosum = repo.q1(
        "SELECT COUNT(*) n, COUNT(DISTINCT source_id) s FROM chunk "
        "WHERE session_id=? AND on_topic=1 "
        "AND (summary IS NULL OR summary='')", (sid,))
    total_on = repo.q1(
        "SELECT COUNT(*) n FROM chunk WHERE session_id=? AND on_topic=1",
        (sid,))
    ev["unsummarized_on_topic_chunks"] = {
        "missing": nosum["n"], "total": total_on["n"],
        "sources": nosum["s"]}
    ev["gate_degraded_chunks"] = repo.q1(
        "SELECT COUNT(*) n FROM chunk WHERE session_id=? "
        "AND gate_method LIKE '%degraded%'", (sid,))["n"]
    ev["unjudged_sources"] = repo.q1(
        "SELECT COUNT(*) n FROM source WHERE session_id=? "
        "AND (source_nature IS NULL OR source_nature='')", (sid,))["n"]

    # 写作失败任务：ResearchState 不直接带 WriteState.failed，从已累积的
    # 事件里取最后一条 kind='write' 的 `failed` 数（它来自子图收尾态，
    # 是 revise 清理之后的终值）。
    failed_tasks = 0
    for e in reversed(state.get("events") or []):
        if e.get("kind") == "write" and "failed" in e:
            failed_tasks = e["failed"] or 0
            break
    ev["failed_tasks"] = failed_tasks

    try:
        repo.save_run_trace(sid, int(state.get("round", 0) or 0),
                            "run_health", ev)
    except Exception:
        pass

    retries = sum(s["retries"] for s in by_stage.values())
    wait_total = round(sum(s["retry_wait_s_total"]
                           for s in by_stage.values()), 1)
    errors = sum(s["errors"] for s in by_stage.values())
    degraded_bits = (nosum["n"] + ev["gate_degraded_chunks"]
                     + ev["unjudged_sources"] + failed_tasks)
    if retries or errors or degraded_bits:
        print(f"⚠️  [run_health] 过载重试 {retries} 次（等 {wait_total}s）· "
              f"失败 {errors} 次 · 无概括段 {nosum['n']}/{total_on['n']} · "
              f"闸门降级 {ev['gate_degraded_chunks']} · "
              f"未判来源 {ev['unjudged_sources']} · "
              f"失败任务 {failed_tasks}", flush=True)
    else:
        print("✅ [run_health] 全部指标干净", flush=True)
    return ev


@scoped
@_timed_node("finalize")
def node_finalize(state: ResearchState) -> dict:
    """段落收尾 + 状态与成本回写。

    概括句已搬到 node_extract（见那里的说明），这里只负责把段落从
    pending 翻成 complete，以及写回成本与状态。
    """
    repo = get_repo()
    sid = state["session_id"]
    health = _run_health(repo, sid, state)
    rep = repo.q1("SELECT id FROM report WHERE session_id=?", (sid,))
    if not rep:
        return {"events": [health,
                           {"kind": "finalize", "skipped": "no report"}]}

    chunks = repo.q(
        "SELECT c.id, c.summary FROM chunk c "
        "WHERE c.session_id=? AND c.status='pending' ORDER BY c.ingested_at",
        (sid,),
    )
    if not chunks:
        return {"events": [health,
                           {"kind": "finalize",
                            "skipped": "no pending chunks"}]}

    # ---- 1. 段落收尾 ----
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
        updates.append((row["id"], summary))
    if updates:
        repo.complete_chunks(updates)

    # ---- 2. 状态与成本回写 ----
    cost = repo.session_cost(sid)
    repo.add_cost(sid, cost)
    repo.finish_session(sid, "done")

    remaining = len(chunks) - len(updates) - no_sum
    return {
        "cost_tokens": cost,
        "events": [health, {
            "kind": "finalize", "chunks": len(chunks), "finalized": len(updates),
            "no_summary": no_sum, "remaining_pending": max(0, remaining),
            "cost_tokens": cost,
        }],
    }


# ⚠️ node_finalize 原本的第一段是「向量化」，2026-10-13 整段删掉：
#     vec = EMB.embed_document(summary, row["content"])
#     updates.append((row["id"], summary, to_blob(vec), count_tokens(row["content"])))
# 理由不是「向量检索以后可能有用」，而是它**算出来就再没被读过**：
#   - 全库没有独立的向量库。`chunk.embedding` 只是一个 BLOB 列（384 维 float16）
#   - `cosine_topk()` 零生产调用；`chunk_fts` 是零 MATCH 查询；
#     `ready_chunks()` 零生产调用——「按相似度召回段落」这个环节从来没被实现过
#   - 所以 v5 那一轮 143 段花了 251 秒（整轮 1591s 的 15.8%）在写一列没人读的数据
# 连带删掉的：`EMB = Embedder.instance()` 单例、`to_blob` 导入、`count_tokens`
# 导入，以及那次 `count_tokens()` 调用本身——`complete_chunks` 的列表推导
# 用 `_` 把它丢了，从来没进过 SQL，而 chunk 插入时 `add_chunk` 已经写过
# token_count 了。上面那条 SELECT 里连带删掉的 content/heading_path/
# source_id 和 s.* 五列同理：它们只为了拼向量化文本而查出来。
#
# `embedding` 列**保留**：历史会话那 143 段向量还在（107 KB），将来真要做
# 库内检索时不必改 schema。代价是这列从此只会越来越空。
#
# ⚠️ 改这段最容易踩的坑：`complete_chunks` 里的 `status='complete'`
# 必须留着。它跟 `embedding=?` 原本在同一条 UPDATE 上——只删向量化而不
# 处理这个，段落会永久卡在 pending，而本函数正是靠 `WHERE status='pending'`
# 选段的，二次运行会把它们全部重跑一遍。


# ---- 会话摘要（产品化阶段 3）----


@_timed_node("summary")
def node_summary(state: ResearchState) -> dict:
    """报告写完后生成一份结构化摘要，存进 `session.summary_json`。

    **为什么是独立的图节点，而不是塞进 `node_finalize`**：
    finalize 是「向量化 + 回写成本」，职责是确定性的数据库操作；
    摘要是一次外部 LLM 调用，性质完全不同，混在一起会让 finalize
    既可能因 LLM 失败而整体失败，又没法单独给摘要配 stage。

    **摘要失败绝不能让整场研究失败**（`except` 兜住后只发一条事件）：
    到这一步报告已经落库、状态已经是 done，成果全在。
    因为一次限流或超时把整场 40 分钟的研究判失败，是不可接受的。

    **已经有摘要就跳过**：图可能被重入（approve 后续跑、状态重放），
    重跑一次要多花一次 8k 输入 token，而结果不会有变化。

    引用标记 `[S2·1]` **故意保留**：它让摘要里的数字可溯源，
    几百个标记对上下文长度的影响可以忽略。
    """
    repo = get_repo()
    sid = state["session_id"]

    # 从库里读而不是从 state 读：report_markdown 由 write 子图写入，
    # 这里直接读已落库的那份，和前端看到的完全一致。
    rep = repo.q1("SELECT markdown FROM report WHERE session_id=?", (sid,))
    if not rep or not (rep["markdown"] or "").strip():
        return {"events": [{"kind": "summary", "skipped": "no report"}]}

    done = repo.q1("SELECT summary_json FROM session WHERE id=?", (sid,))
    if done and done["summary_json"]:
        return {"events": [{"kind": "summary", "skipped": "already has summary"}]}

    question = state.get("question") or repo.q1(
        "SELECT question FROM session WHERE id=?", (sid,))["question"]
    body = rep["markdown"]

    t0 = time.time()
    try:
        data = LLM.complete_json(
            "session_summary",
            f"研究问题：{question}\n\n报告正文：\n{body}",
            system=prompts.SESSION_SUMMARY,
        )
    except Exception as e:
        # 见上面的说明：这里**不重试也不上抛**。摘要只是锦上添花。
        print(f"⚠️  [session_summary] 失败（不影响研究结果）: {str(e)[:160]}", flush=True)
        return {"events": [{"kind": "summary", "ok": False,
                            "error": f"{type(e).__name__}: {str(e)[:200]}"}]}

    out = _clean_summary(data)
    if not out["summary"]:
        return {"events": [{"kind": "summary", "ok": False, "error": "空摘要"}]}

    repo.set_summary(sid, out)
    ev = {"kind": "summary", "ok": True, "chars": len(out["summary"]),
          "points": len(out["key_points"]), "gaps": len(out["gaps"]),
          "elapsed_s": round(time.time() - t0, 1)}
    return {"events": [ev]}


def _clean_summary(data: Any) -> dict:
    """把模型返回整成固定形状。**字段不可信，逐个收窄。**

    模型可能少给字段、给成字符串、给成嵌套对象，或者 `key_points`
    里塞进一整段话。列表是直接渲染到 UI 上的，不收窄就会炸版面。
    """
    if not isinstance(data, dict):
        return {"summary": "", "key_points": [], "gaps": []}

    def _one_line(v: Any, limit: int) -> str:
        return re.sub(r"\s+", " ", str(v)).strip()[:limit]

    def _list(v: Any, limit: int, each: int) -> list[str]:
        if not isinstance(v, list):
            return []
        return [_one_line(x, each) for x in v[:limit] if isinstance(x, (str, int, float))]

    return {
        "summary": _one_line(data.get("summary") or "", 120),
        "key_points": _list(data.get("key_points"), 6, 160),
        "gaps": _list(data.get("gaps"), 5, 160),
    }
