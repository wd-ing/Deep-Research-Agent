"""探针子图。

存在的理由不是「流程好看」，而是三个具体的工程收益：
  1. 主循环看不到内部三次 LLM 调用与换词重试，状态不被污染
  2. 换词重试次数这个边界封在子图内部，不用泄漏到主图配置
  3. 可以脱离整套系统单独验收：给它 N 个研究对象，看它定位准不准、
     歧义判不判得出、内容农场认不认得出来
"""

from __future__ import annotations

import asyncio
import json

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ...ingest.extract import Fetcher
from ...llm.provider import LLMClient
from ...trace import traced
from ...search.base import SearchHit
from ...search.searxng import SearXNGProvider
from ...search.tavily import TavilyProvider
from .. import prompts
from ..state import ProbeState
# 与 write.py 共用同一套重试判据/入参归一（2026-10-20 抽出，见 _retry.py）。
from ._retry import (_norm_audit_list, _audit_retryable,
                     _transient_http, _wait_transient)

MAX_ATTEMPTS = 2          # 换词重试上限
LIGHT_PER_LANG = 3        # 轻扫每种语言几条宽查询
MAX_DEEP_PAGES = 3        # 深扫最多读几篇
MIN_DEEP_CHARS = 600      # 正文短于这个字数不值得读

SX = SearXNGProvider(script=".searxng/searxng.sh")
TV = TavilyProvider()
LLM = LLMClient()


# ---------------------------------------------------------------- 轻扫

def _default_queries(q: str) -> dict:
    """第一次的宽查询。刻意不用任何领域词，只用主题本身。"""
    return {
        "zh": [q, f"{q} 是什么", f"{q} 最新"],
        "en": [q, f"what is {q}", f"{q} latest"],
    }


def _fallback_queries(q: str) -> dict:
    """已删除。

    曾经用字符串拼「争议/最新/机构」这类固定词，在模型没给出换词建议时顶上。
    那是代码替模型发明了该问什么——既无实测依据，又掩盖了真正的失败原因
    （输出格式坏掉，而不是词选得不对）。判不出来时正确的做法是用同一批
    结果重新问一次，两次都不行就如实记 unjudged。
    """
    raise NotImplementedError


def node_probe_light(state: ProbeState) -> dict:
    q = state["question"]
    queries = state.get("queries") or _default_queries(q)
    zh = (queries.get("zh") or [])[:LIGHT_PER_LANG]
    en = (queries.get("en") or [])[:LIGHT_PER_LANG]

    # 重试时复用上一轮的检索结果，不再搜一遍。
    # 轻扫失败的原因实测是**输出格式坏了**（一次尾逗号带崩了整场研究），
    # 不是词选得不对——换个词重搜解决不了格式问题，只是白白多花一次检索。
    # 所以重试的语义是「用同一批结果重新问一次」。
    cached = state.get("light_results") or []
    reused = bool(cached)

    async def call(prov, query, lang, lim):
        return await asyncio.to_thread(prov.search, query, language=lang, limit=lim)

    async def run():
        tasks = [call(SX, x, "zh-CN", 8) for x in zh]
        tasks += [call(TV, x, "en", 6) for x in en]
        res = await asyncio.gather(*tasks, return_exceptions=True)
        flat: list[SearchHit] = []
        for r in res:
            if isinstance(r, list):
                flat.extend(r)
        return flat

    if reused:
        light = cached
    else:
        hits = asyncio.run(run())
        # 只留标题与摘要，这是「轻扫」的定义
        light = [{
            "url": h.url, "title": h.title, "snippet": (h.snippet or "")[:300],
            "engine": h.engine,
        } for h in hits if h.url]

    events = [{
        "kind": "probe_light", "attempt": state.get("attempt", 0) + 1,
        "queries": {"zh": zh, "en": en}, "hits": len(light), "reused": reused,
    }]

    if not light:
        return {"attempt": state.get("attempt", 0) + 1, "queries": queries,
                "tried_queries": state.get("tried_queries", []) + zh + en,
                "light_results": [], "judged": False,
                "deep_urls": [], "quality": None, "events": events}

    # LLM① 一次调用同时判歧义与深度
    blob = "\n".join(
        f"[{i}] {r['title']}\n    {r['snippet'][:200]}\n    {r['url']}"
        for i, r in enumerate(light[:30])
    )
    try:
        data = LLM.complete_json(
            "probe_light",
            f"研究对象：{q}\n\n搜索结果（只有标题和摘要）：\n{blob}",
            system=prompts.PROBE_LIGHT,
        )
    except Exception as e:
        # 调用失败（超时、传输中断、JSON 彻底救不回）与「没给出判断」同样处理：
        # 复用这批结果重问一次，不发明新的查询词。
        return {"attempt": state.get("attempt", 0) + 1, "queries": queries,
                "tried_queries": state.get("tried_queries", []) + zh + en,
                "light_results": light, "judged": False,
                "deep_urls": [], "quality": None, "retry_queries": {},
                "events": events + [{"kind": "probe_error", "error": str(e)[:200]}]}

    amb = data.get("ambiguity") or {}
    depth = data.get("depth") or {}
    retry = data.get("retry_queries") or {}

    # 「模型没给出可用判断」和「判断为 thin」必须分开。
    # 早先这里写的是 depth.get("quality", "thin")，于是模型一次没按格式返回，
    # 就被当成「网上关于这个主题的信息稀薄」一路降级，报告开头直接写稀薄声明，
    # 大纲塌成一个把问题原样复述的小节，检索词也只剩问题本身。
    judged = depth.get("quality") in ("substantive", "thin", "farm")
    if not judged:
        events.append({
            "kind": "probe_unjudged",
            "why": "depth.quality 缺失或非法",
            "top_keys": list(data.keys())[:8],
            "hits": len(light),
        })
        # 用同一批结果重新问一次，不换词。
        # 这里曾经用字符串拼了「争议/最新/机构」三组兜底查询词——那是我替模型
        # 发明了该问什么，既没有实测依据，也会掩盖真正的失败原因。
        # 判不出来的实测原因是输出格式坏了，换词重搜不解决任何问题。
        return {
            "attempt": state.get("attempt", 0) + 1,
            "queries": queries,          # 保持不变，重试才拿得到同一批结果
            "tried_queries": state.get("tried_queries", []) + zh + en,
            "light_results": light,      # 复用，下一轮不再搜
            "ambiguity": amb,
            "judged": False,
            "retry_queries": {},          # 模型自己没给换词建议，不编
            "deep_urls": [],
            "quality": None,
            "events": events,
        }

    events.append({
        "kind": "probe_judged",
        "ambiguous": bool(amb.get("is_ambiguous")),
        "quality": depth.get("quality"),
        "will_read": len(depth.get("worth_reading_urls") or []),
    })

    return {
        "attempt": state.get("attempt", 0) + 1,
        "queries": queries,
        "tried_queries": state.get("tried_queries", []) + zh + en,
        "light_results": light,
        "ambiguity": amb,
        "judged": True,
        "retry_queries": retry,
        "deep_urls": (depth.get("worth_reading_urls") or [])[:MAX_DEEP_PAGES],
        "quality": depth["quality"],
        "events": events,
    }


# ---------------------------------------------------------------- 澄清

def node_clarify(state: ProbeState) -> dict:
    """歧义澄清。只在这个节点挂 ask_user 能力。"""
    amb = state.get("ambiguity") or {}
    if not amb.get("is_ambiguous"):
        return {"events": [{"kind": "clarify", "skipped": True}]}

    cands = amb.get("candidates") or []
    if len(cands) < 2:
        # 只有一个候选不算歧义
        return {"events": [{"kind": "clarify", "skipped": True,
                            "note": "候选不足 2 个，判定非歧义"}]}

    payload = {
        "kind": "need_clarification",
        "question": f"「{state['question']}」指向多个不同事物，你指的是哪个？",
        "reason": amb.get("reason", ""),
        "options": [{
            "label": c.get("label"),
            "summary": c.get("summary", ""),
            "evidence_titles": (c.get("evidence_titles") or [])[:3],
            "evidence_urls": (c.get("evidence_urls") or [])[:3],
        } for c in cands],
    }
    reply = interrupt(payload)

    if isinstance(reply, dict) and reply.get("selected"):
        chosen = reply["selected"]
    elif isinstance(reply, str) and reply:
        chosen = reply
    else:
        chosen = cands[0].get("label", "")

    return {
        "user_choice": chosen,
        "events": [{"kind": "clarify", "asked": True,
                    "candidates": [c.get("label") for c in cands],
                    "chosen": chosen}],
    }


# ---------------------------------------------------------------- 深扫

def node_probe_deep(state: ProbeState) -> dict:
    urls = state.get("deep_urls") or []
    urls = urls[:MAX_DEEP_PAGES]
    if not urls:
        return {"deep_pages": [],
                "events": [{"kind": "probe_deep", "read": 0,
                            "note": "没有可深读 URL，判定信息稀薄"}]}

    repo_paths = None
    try:
        from ...db.repo import get_repo
        repo_paths = get_repo()
    except Exception:
        pass

    fetcher = Fetcher(repo_paths) if repo_paths else _BareFetcher()
    pages = asyncio.run(fetcher.fetch_many(urls, bypass_cache=True))
    good = [p for p in pages if p.ok and len(p.text) >= MIN_DEEP_CHARS]

    return {
        "deep_pages": [{"url": p.url, "title": p.title, "text": p.text,
                        "published": p.published, "char_count": len(p.text)}
                       for p in good],
        "events": [{"kind": "probe_deep", "read": len(good), "tried": len(urls),
                    "chars": sum(len(p.text) for p in good)}],
    }


class _BareFetcher(Fetcher):  # 探针阶段可能还没初始化 DB
    def __init__(self):
        self.repo = None
        from ...ingest.extract import _DomainThrottle
        self.throttle = _DomainThrottle(0.3)
        import asyncio as _a
        self.sem = _a.Semaphore(3)
        self.timeout_s = 20.0
        self.cache_ttl_days = 7


# ---------------------------------------------------------------- 轮廓

# probe_outline 的强制交卷 schema（2026-10-20）：原来走 complete_json
# 手写长 JSON，中段写崩（未转义引号/漏逗号）没有任何重试、except 直接
# 降级 → 连续两个真实会话（v15/v16）大纲退化成 1 节。历史 3/23 同型
# 失败（v9 也有）。改成强制工具 `submit_outline`——模型只填
# `arguments` 对象，平台在解码层面保证它是合法 JSON（同 write_audit
# 的修法，坑 77/82）。
# 字段与 prompts.PROBE_OUTLINE 要求的输出一一对应；required 只列
# 真正缺了就没法用的三项，其余字段缺席不判失败。
OUTLINE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "subtopics": {"type": "array", "items": {
            "type": "object", "properties": {
                "name": {"type": "string"}, "what": {"type": "string"},
                "evidence_urls": {"type": "array",
                                  "items": {"type": "string"}}}}},
        "entities": {"type": "array", "items": {
            "type": "object", "properties": {
                "name": {"type": "string"},
                "type": {"type": "string",
                         "enum": ["person", "org", "product", "method",
                                  "place"]}}}},
        "jargon": {"type": "array", "items": {
            "type": "object", "properties": {
                "term": {"type": "string"},
                "en_equivalent": {"type": ["string", "null"]},
                "meaning": {"type": "string"}}}},
        "controversies": {"type": "array", "items": {
            "type": "object", "properties": {
                "topic": {"type": "string"}, "position_a": {"type": "string"},
                "position_b": {"type": "string"},
                "evidence_urls": {"type": "array",
                                  "items": {"type": "string"}}}}},
        "common_questions": {"type": "array", "items": {"type": "string"}},
        "info_quality": {"type": "string", "enum": ["high", "medium", "low"]},
        "one_line": {"type": "string"},
    },
    "required": ["subtopics", "info_quality", "one_line"],
}

_OUTLINE_KEYS = ("subtopics", "entities", "jargon", "controversies",
                 "common_questions", "info_quality", "one_line")


def _normalize_outline_input(raw) -> dict | None:
    """把 `submit_outline` 的工具入参归一成下游读的 dict；归一不了返回 None。

    容忍的漂移与 `_normalize_audit_input` 同款（M3.1/M3 实测形态）：
    外层单键 dict 包装（最多解两层）、数组包成 `{"item": [...]}`、
    数组回成 JSON 字符串。**严格**：`info_quality` 非法值不静默回落，
    `subtopics` 为空且 `one_line` 也为空视为没交出轮廓——都判无效，
    让上层重试，而不是拿半残结构往下游走。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    d = raw
    for _ in range(2):
        if any(k in d for k in _OUTLINE_KEYS):
            break
        dict_vals = [v for v in d.values() if isinstance(v, dict)]
        if len(dict_vals) == 1:
            d = dict_vals[0]
        else:
            break
    iq = d.get("info_quality")
    if iq not in ("high", "medium", "low"):
        return None
    out = dict(d)
    for k in ("subtopics", "entities", "jargon", "controversies",
              "common_questions"):
        out[k] = _norm_audit_list(out.get(k))
    if not isinstance(out.get("one_line"), str):
        out["one_line"] = ("" if out.get("one_line") is None
                           else str(out["one_line"]))
    if not out["subtopics"] and not out["one_line"].strip():
        return None
    out["info_quality"] = iq
    return out


def node_probe_outline(state: ProbeState) -> dict:
    """生成领域轮廓。

    **深读页为空时不能直接放弃**（2026-10-06 修）。
    原先 `if not pages: data = {}` —— 深读一失败，整个探针就作废了。
    可轻扫阶段已经拿到几十条带标题和摘要的命中（实测 42 条），
    那些信息足够生成一个**粗但可用**的轮廓。丢掉它是白白浪费。

    实测代价：那一轮深读没跑起来 → 轮廓全空 → `node_outline` 的输入
    从 5,172 token 掉到 382 → 模型写不出 `grounded_in` → 大纲退化成 1 节
    → 报告只有 19,261 字（对照正常的 78,462 字）。
    """
    pages = state.get("deep_pages") or []
    light = state.get("light_results") or []
    fallback_used = False

    if pages:
        blob = "\n\n".join(
            f"### 来源{i + 1}：{p['title']}\nURL：{p['url']}\n\n{p['text'][:6000]}"
            for i, p in enumerate(pages)
        )
        src = f"\n读到的正文：\n{blob[:20000]}"
    elif light:
        # 回退：只用轻扫的标题 + 摘要。比没有强得多，而且要说明材料只有摘要。
        blob = "\n".join(
            f"[{i}] {r.get('title') or ''}\n    {(r.get('snippet') or '')[:200]}"
            for i, r in enumerate(light[:40]) if r.get("title")
        )
        src = ("\n**深读没有完成，以下只有搜索结果的标题与摘要，没有正文。**"
               "据此能判断这个题目大致覆盖哪些维度，但无法核实细节。\n"
               f"\n搜索结果：\n{blob[:14000]}")
        fallback_used = True
    else:
        return {"signals": {}, "degraded": True, "quality": "thin",
                "headline": "轻扫与深读都没有产出任何材料，领域轮廓无法生成。",
                "events": [{"kind": "probe_outline", "degraded": True,
                            "note": "既无深读页也无轻扫结果"}]}

    # 2026-10-20 起结论走强制工具 `submit_outline`（坑 82：手写长 JSON
    # 中段解析失败无重试 → 连续两个真实会话大纲退化成 1 节）。工具
    # 入参仍可能偏离 schema → `_normalize_outline_input` 归一，归一
    # 不了当失败重试一次（429/额度/ConfigError 不重试，与 write_audit
    # 同一套判据）。点名工具的话术与 submit_audit 同款。
    prompt = (f"研究对象：{state['question']}\n"
              + (f"用户已澄清指向：{state.get('user_choice')}\n"
                 if state.get("user_choice") else "")
              + src
              + "\n\n读完后**调用 `submit_outline` 工具**提交结论，"
                "结论只放在工具参数里，不要在对话文本里写 JSON。")
    data: dict | None = None
    err: object = ""
    attempts = 0
    for _attempt in (1, 2):
        attempts = _attempt
        try:
            raw = LLM.complete_json_forced(
                "probe_outline", prompt, "submit_outline", OUTLINE_SCHEMA,
                system=prompts.PROBE_OUTLINE)
        except Exception as e:
            err = e
            if _audit_retryable(e) and _attempt < 2:
                if _transient_http(e):
                    _wait_transient("probe_outline", e)
                else:
                    print(f"⚠️  [probe_outline] 调用失败"
                          f"（{type(e).__name__}: {str(e)[:80]}），重试一次",
                          flush=True)
                continue
            break
        data = _normalize_outline_input(raw)
        if data is None:
            err = "submit_outline 入参偏离 schema，无法归一化"
            if _attempt < 2:
                print("⚠️  [probe_outline] 返回结构无法归一化，重试一次",
                      flush=True)
                continue
        break

    if data is None:
        return {"degraded": True,
                "events": [{"kind": "probe_outline", "error": str(err)[:200],
                            "attempts": attempts,
                            "fallback_to_titles": fallback_used}],
                "quality": "thin"}

    judged = state.get("judged", False)
    degraded = (not judged) or state.get("quality") in ("thin", "farm") \
        or not data.get("subtopics")
    headline = ""
    if degraded:
        # 「判不出来」和「判出来是稀薄」在报告开头要说不同的话，
        # 否则一次格式失败会让报告凭空多出一句关于信息稀薄的结论。
        # 而且必须把「搜索其实成功了」讲清楚——实测模型看到「读到的页面数：0」
        # 就自行推论成「未能成功抓取任何公开页面」，那是假的：
        # 轻扫拿到了几十条结果，只是没能读正文、也没判出质量。
        light_n = len(state.get("light_results") or [])
        if not judged:
            situation = (
                f"检索本身是成功的：轻扫拿到了 {light_n} 条搜索结果"
                f"（标题与摘要）。失败发生在两个环节，且都不是「网上没东西」：\n"
                f"  1. 轻扫没能对结果做出质量判断（模型未按格式返回）\n"
                f"  2. 因此没有进入深读，实际读过的正文页数：{len(pages)}\n"
                f"honest_headline 必须反映这一点：**是判断环节失灵，不是信息稀薄**。"
                f"不要写成「未能抓到内容」或「没有公开信息」。"
            )
        else:
            situation = (
                f"探针判断信息质量为 {state.get('quality')}，"
                f"实际读过的正文页数：{len(pages)}"
            )
        try:
            d = LLM.complete_json(
                "probe_degraded",
                f"研究对象：{state['question']}\n"
                f"探针尝试过的查询：{state.get('tried_queries')}\n"
                f"{situation}\n"
                f"已观察到的内容：{str(data)[:2000]}",
                system=prompts.PROBE_DEGRADED,
            )
            headline = d.get("honest_headline", "")
        except Exception:
            headline = ("该主题的公开信息稀薄，报告结论可信度有限。" if judged
                        else "探针未能完成质量判断，报告基于未经评估的材料。")

    return {
        "signals": {
            "subtopics": data.get("subtopics", []),
            "entities": data.get("entities", []),
            "jargon": data.get("jargon", []),
            "controversies": data.get("controversies", []),
            "common_questions": data.get("common_questions", []),
            "one_line": data.get("one_line", ""),
            "info_quality": data.get("info_quality", "low"),
        },
        "degraded": degraded,
        "headline": headline,
        "quality": "substantive" if not degraded else (state.get("quality") or "unjudged"),
        "signals_from": "titles" if fallback_used else "deep_pages",
        "events": [{"kind": "probe_outline",
                    "subtopics": len(data.get("subtopics", [])),
                    "entities": len(data.get("entities", [])),
                    "controversies": len(data.get("controversies", [])),
                    "degraded": degraded,
                    "fallback_to_titles": fallback_used}],
    }


# ---------------------------------------------------------------- 路由

def route_after_light(state: ProbeState) -> str:
    amb = state.get("ambiguity") or {}
    if amb.get("is_ambiguous") and len(amb.get("candidates") or []) >= 2:
        return "clarify"
    return "check_quality"


def node_check_quality(state: ProbeState) -> dict:
    """占位：质量判断已在轻扫完成，这里只做事件记录。"""
    return {"events": [{"kind": "probe_quality", "quality": state.get("quality"),
                        "judged": state.get("judged", False),
                        "deep_urls": len(state.get("deep_urls") or [])}]}


def route_after_quality(state: ProbeState) -> str:
    # 还没拿到判断就不许谈稀薄。两次都没判出来，才承认「判不了」并降级。
    if not state.get("judged", False) and state.get("attempt", 0) < MAX_ATTEMPTS:
        return "retry"
    if state.get("judged") and state.get("quality") == "substantive" \
            and state.get("deep_urls"):
        return "deep"
    attempt = state.get("attempt", 0)
    retry = state.get("retry_queries") or {}
    if attempt < MAX_ATTEMPTS and (retry.get("zh") or retry.get("en")):
        return "retry"
    return "outline"


def node_retry(state: ProbeState) -> dict:
    retry = state.get("retry_queries") or {}
    zh = (retry.get("zh") or [])[:LIGHT_PER_LANG]
    en = (retry.get("en") or [])[:LIGHT_PER_LANG]
    if not zh and not en:
        # 判不出来时 retry_queries 是空的（模型没给，代码也不编）。
        # 这时要沿用上一轮的词，否则会带着空查询进入下一轮——
        # 虽然 light 节点会复用已有结果，但事件日志会显示成「没查任何词」。
        cur = state.get("queries") or {}
        zh = (cur.get("zh") or [])[:LIGHT_PER_LANG]
        en = (cur.get("en") or [])[:LIGHT_PER_LANG]
    return {
        "queries": {"zh": zh, "en": en},
        "events": [{"kind": "probe_retry", "attempt": state.get("attempt", 0) + 1,
                    "new_queries": {"zh": zh, "en": en}}],
    }


# ---------------------------------------------------------------- 组装

def build_probe_graph():
    g = StateGraph(ProbeState)
    # traced()：子图节点的绝对起止进 trace_span（主图只有 node:probe 总耗时）。
    g.add_node("light", traced("probe.light", node_probe_light))
    g.add_node("clarify", traced("probe.clarify", node_clarify))
    g.add_node("check_quality", traced("probe.check_quality", node_check_quality))
    g.add_node("retry", traced("probe.retry", node_retry))
    g.add_node("deep", traced("probe.deep", node_probe_deep))
    g.add_node("outline", traced("probe.outline", node_probe_outline))

    g.add_edge(START, "light")
    g.add_conditional_edges("light", route_after_light,
                             {"clarify": "clarify", "check_quality": "check_quality"})
    g.add_edge("clarify", "check_quality")
    g.add_conditional_edges("check_quality", route_after_quality,
                             {"deep": "deep", "retry": "retry", "outline": "outline"})
    g.add_edge("retry", "light")
    g.add_edge("deep", "outline")
    g.add_edge("outline", END)
    return g


def probe(question: str, session_id: str = "", time_sensitive: bool = False,
          checkpointer=None) -> dict:
    """跑一次探针。返回子图出口契约。"""
    g = build_probe_graph()
    if checkpointer is not None:
        g = g.compile(checkpointer=checkpointer)
    else:
        g = g.compile()
    out = g.invoke({
        "question": question, "session_id": session_id,
        "time_sensitive": time_sensitive, "attempt": 0,
    })
    return {
        "signals": out.get("signals", {}),
        "quality": out.get("quality", "thin"),
        "ambiguity": {
            "is_ambiguous": bool((out.get("ambiguity") or {}).get("is_ambiguous")),
            "candidates": (out.get("ambiguity") or {}).get("candidates", []),
            "user_answer": out.get("user_choice", ""),
        },
        "probes_used": out.get("attempt", 0),
        "deep_urls_read": [p["url"] for p in (out.get("deep_pages") or [])],
        "degraded": out.get("degraded", False),
        "headline": out.get("headline", ""),
        "events": out.get("events", []),
    }
