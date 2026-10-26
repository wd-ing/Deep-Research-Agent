"""抓取后的确定性质量过滤（2026-10-27，批 7 任务 2）。

规则来自 config/source_quality.yaml；模块级缓存一次。
YAML 缺失或损坏一律回落空规则——过滤是锦上添花，
绝不能因为一个配置文件让检索整体挂掉。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .normalize import domain_of

RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "source_quality.yaml"
_TEXT_HEAD = 300          # 只看正文前 300 字判未渲染模板

_rules: dict | None = None


def load_rules() -> dict:
    """{drop_domains: set, drop_title: [re], drop_text_head: [re]}。

    失败回 {domains:set(), title:[], head:[]}——空规则时
    `page_defect` 恒 None，行为与没装过滤器一致。
    """
    global _rules
    if _rules is not None:
        return _rules
    try:
        raw = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8")) or {}
        _rules = {
            "domains": {d.strip().lower() for d in
                        (raw.get("drop_domains") or []) if d},
            "title": [re.compile(p) for p in
                      (raw.get("drop_title_regex") or [])],
            "head": [re.compile(p) for p in
                     (raw.get("drop_text_head_regex") or [])],
        }
    except Exception:
        _rules = {"domains": set(), "title": [], "head": []}
    return _rules


def page_defect(url: str, title: str | None, text: str | None) -> str | None:
    """返回丢弃原因（`drop_domain:X`/`template_title`/`template_text_head`），
    不命中返回 None。只判「永远不是研究材料」的页——
    低权威但相关（weibo/雪球/门户转载）不在这里拦。
    """
    r = load_rules()
    dom = domain_of(url or "")
    # 后缀匹配含子域：a.liepin.com 命中 liepin.com；notliepin.com 不命中
    for d in r["domains"]:
        if dom == d or dom.endswith("." + d):
            return f"drop_domain:{d}"
    t = (title or "").strip()
    for pat in r["title"]:
        if pat.search(t):
            return "template_title"
    head = (text or "")[:_TEXT_HEAD]
    for pat in r["head"]:
        if pat.search(head):
            return "template_text_head"
    return None
