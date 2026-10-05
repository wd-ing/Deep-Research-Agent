"""相关性闸门：判断一个来源是否在讲我们要研究的东西。

**为什么需要**：搜索后端只管返回 top-N，它不知道这次研究的对象是谁。
之前的实现把搜索结果直接当成"采纳"，于是出现了《捷变频信号参量提取技术》
这类完全无关的文档混进来源表。实测 bing 噪音率 0%、tavily 17%、360search 50%。

**为什么放在这里**：claims 此刻已经抽出来了，判断"这些 claims 是不是在讲
我要的东西"几乎零成本。再往后放就晚了，段落已经入库。

**三级判定，从便宜到贵**：
  rule     —— 拿探针阶段发现的实体和术语去匹配 claims 文本，命中即通过
  llm      —— 规则没命中但主题词本身存在时，交给 LLM 判断（只在模糊时花调用）
  fallback —— 探针阶段没给出任何实体和术语时，全部走 LLM

fallback 触发次数被记录成健康指标：如果一场研究里 fallback 频繁触发，
说明探针的轮廓提取失效了，闸门也就失去了依据。
"""

from __future__ import annotations

import re
from typing import Any

from ...llm.provider import LLMClient
from .. import prompts

LLM = LLMClient()

# 出现率低于这个值就判为「没提到」，走 LLM 或 fallback
MIN_MENTION_RATIO = 0.15

# 通用 AI 词汇在任何相关文章里都会出现，不能当判据。
# 这些词即使命中也不计入闸门。
WEAK_KEYWORDS = {
    "llm", "大模型", "ai", "人工智能", "agent", "智能体", "模型", "api",
    "置信度", "confidence", "路由", "routing", "工具调用", "tool calling",
    "类型安全", "type safety", "决策模型", "decision model", "准确率",
    "accuracy", "性能", "benchmark", "评测", "结构化输出", "json",
    "幻觉", "hallucination", "推理", "reasoning", "微调", "fine-tune",
    "训练", "training", "上下文", "context", "token", "提示词", "prompt",
}


def build_keywords(signals: dict, question: str) -> dict:
    """从探针轮廓取实体和术语，分强弱两档。

    返回 {"strong": [...], "weak": [...]}。
    strong 是产品、方法、组织这类「出现就基本能确定在讲这个主题」的名词；
    weak 是通用领域词、人名、地名——命中说明不了问题，不参与闸门判定。
    分档依据是探针给的实体类型，不是硬编码的词表。
    """
    strong: list[str] = []
    weak: list[str] = []

    def put(name: str) -> None:
        n = (name or "").strip()
        if not (2 <= len(n) <= 60):
            return
        (weak if n.lower() in WEAK_KEYWORDS else strong).append(n)

    # 实体按类型分档。探针本来就给每个实体打了 type，直接用数据分，
    # 不用硬编码人名地名词表。
    #
    # 为什么人名和地名不能当强信号：出现「张强」完全不说明这段在讲固态电池
    # （同名人到处都是）；出现「中国」更不说明问题（任何产业文章都会提）。
    # 把它们算成强信号的后果实测过：78 个 strong 里混了 14 个人名和 6 个国名地名，
    # 规则层拦下 134/177 段、最终保留率 91%——**闸门几乎成了摆设**。
    for e in signals.get("entities") or []:
        name = e.get("name")
        t = (e.get("type") or "").lower()
        if t in ("person", "place"):
            n = (name or "").strip()
            if 2 <= len(n) <= 60:
                weak.append(n)
        else:
            put(name)
    for j in signals.get("jargon") or []:
        put(j.get("term"))
        put(j.get("en_equivalent"))
    for m in re.findall(r"[A-Z][A-Za-z0-9.\-]{2,}", question or ""):
        if m.lower() not in {"the", "and", "for"}:
            put(m)

    def dedup(xs):
        seen, out = set(), []
        for x in xs:
            kk = x.lower()
            if kk not in seen:
                seen.add(kk)
                out.append(x)
        return out

    return {"strong": dedup(strong), "weak": dedup(weak)}


def _match(text: str, kws: list[str]) -> int:
    """返回命中次数。中文子串匹配，英文词边界匹配。"""
    if not text:
        return 0
    low = text.lower()
    hits = 0
    for k in kws:
        kl = k.lower()
        if re.search(r"[一-鿿]", kl):
            if kl in low:
                hits += 1
        elif re.search(rf"(?<![a-z0-9]){re.escape(kl)}(?![a-z0-9])", low):
            hits += 1
    return hits


def judge(item: dict, kw: dict, topic_label: str) -> dict:
    """判断单个来源是否与研究主题相关。

    规则：命中任一**专有名词**即通过（零成本）。
    没有专有名词时，如果通用词密度够高也放行；否则交给 LLM。
    item 需要含 title 与 claims。
    """
    strong = kw.get("strong") or []
    weak = kw.get("weak") or []
    blob = " ".join([
        item.get("title") or "",
        *[str(c.get("text") or "") for c in (item.get("claims") or [])[:20]],
    ])[:6000]

    # ---- fallback：探针没给出任何可用词 ----
    if not strong and not weak:
        return _llm_judge(item, topic_label, blob, method="fallback")

    hits = _match(blob, strong)
    if hits >= 1:
        return {"on_topic": True, "method": "rule", "hits": hits,
                "total": len(strong), "reason": f"命中专有名词 {hits}/{len(strong)}"}

    if weak and len(strong) == 0:
        wh = _match(blob, weak)
        if wh / len(weak) >= 0.4:
            return {"on_topic": True, "method": "rule_weak", "hits": wh,
                    "total": len(weak), "reason": f"仅有通用词密集出现 {wh}/{len(weak)}"}

    return _llm_judge(item, topic_label, blob, method="llm",
                      hits=hits, total=len(strong))


def _llm_judge(item: dict, topic_label: str, blob: str,
               method: str, hits: int = 0, total: int = 0) -> dict:
    try:
        d = LLM.complete_json(
            "relevance",
            f"研究对象：{topic_label}\n\n候选来源标题：{item.get('title','')}\n\n"
            f"从这篇材料里抽出的事实：\n{blob[:4000]}",
            system=prompts.RELEVANCE,
        )
        on = bool(d.get("on_topic"))
        reason = str(d.get("reason") or "")[:200]
    except Exception as e:
        # 判不了就放行。闸门宁可漏杀不可错杀——错杀的代价是报告里少了东西，
        # 漏杀的代价只是多了一个无关来源让模型自己判断。
        return {"on_topic": True, "method": method + "_degraded",
                "hits": hits, "total": total, "reason": f"判定失败，放行: {e}"}
    return {"on_topic": on, "method": method, "hits": hits, "total": total,
            "reason": reason}


def gate_all(results: list[dict], kws: list[str], topic_label: str) -> dict:
    """对一批抽取结果做相关性判定，就地写入 on_topic 等字段。

    返回统计：{"judged", "off_topic", "by_method": {...}}
    """
    stats = {"judged": 0, "off_topic": 0, "by_method": {}}
    for r in results:
        if r.get("kind") != "claims":
            continue
        v = judge(r, kws, topic_label)
        r["on_topic"] = v["on_topic"]
        r["gate_method"] = v["method"]
        r["topic_reason"] = v["reason"]
        stats["judged"] += 1
        stats["by_method"][v["method"]] = stats["by_method"].get(v["method"], 0) + 1
        if not v["on_topic"]:
            stats["off_topic"] += 1
    return stats


def gate_segments(segments: list[dict], kw: list[str], topic_label: str) -> dict:
    """段级相关性闸门。

    原先在来源级判，一页里「前半讲技术、后半讲融资」只能整篇进出。
    升到段级之后，这类页面只有相关的那几段会进报告，
    而「这页在东拉西扯」本身也变成可见的事实——段落概括会如实说。

    与 `gate_one` 同一条路径：命中专有名词即通过（零成本），
    都没有才叫 LLM，判不了就放行。宁可漏杀不可错杀。
    """
    strong = kw.get("strong") or []
    weak = kw.get("weak") or []
    keep, marks = [], []

    for seg in segments:
        # 概括缺失时退到正文开头，**不能**因为概括没生成就把这段漏掉——
        # 闸门才是判断相关性��地方，概括只是它的输入之一。
        # 之前这里只看概括，于是「概括批次失败 → 没概括 → 进不了闸门 →
        # 段直接消失」，本该被拦的垃圾页面绕过了闸门。
        blob = " ".join([
            seg.get("heading_path") or "", seg.get("title") or "",
            seg.get("summary") or "",
            "" if (seg.get("summary") or "").strip() else (seg.get("content") or "")[:600],
        ])[:3000]
        if not strong and not weak:
            r = _llm_judge_seg(seg, topic_label, blob, method="fallback")
        else:
            hits = _match(blob, strong)
            if hits >= 1:
                r = {"on_topic": True, "method": "rule", "hits": hits,
                     "total": len(strong),
                     "reason": f"命中专有名词 {hits}/{len(strong)}"}
            elif weak and len(strong) == 0 and _match(blob, weak) / len(weak) >= 0.4:
                r = {"on_topic": True, "method": "rule_weak",
                     "reason": "仅有通用词密集出现"}
            else:
                r = _llm_judge_seg(seg, topic_label, blob, method="llm",
                                   hits=hits, total=len(strong))
        marks.append((1 if r["on_topic"] else 0, r.get("reason", ""),
                      r.get("method", ""), seg["id"]))
        if r["on_topic"]:
            keep.append(seg)

    return {"keep": keep, "marks": marks,
            "stats": {"segments": len(segments), "on_topic": len(keep),
                      "off_topic": len(segments) - len(keep),
                      "by_method": _count(r[2] for r in marks)}}


def _llm_judge_seg(seg: dict, topic_label: str, blob: str,
                   method: str, hits: int = 0, total: int = 0) -> dict:
    try:
        d = LLM.complete_json(
            "relevance",
            f"研究对象：{topic_label}\n\n段落标题：{seg.get('heading_path') or '(无)'}\n\n"
            f"这段在讲：{seg.get('summary') or '(无)'}\n\n原文开头：\n{blob[:2000]}",
            system=prompts.RELEVANCE,
        )
        return {"on_topic": bool(d.get("on_topic")),
                "method": method, "hits": hits, "total": total,
                "reason": str(d.get("reason") or "")[:200]}
    except Exception as e:
        return {"on_topic": True, "method": method + "_degraded",
                "hits": hits, "total": total, "reason": f"判定失败，放行: {e}"}


def _count(xs) -> dict:
    out: dict[str, int] = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out
