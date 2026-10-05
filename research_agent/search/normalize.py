"""URL 规范化与内容判重。

三层，从便宜到贵：
  1. URL 规范化后 hash —— 消掉 utm/fbclid 追踪参数造成的表面重复
  2. 正文 content hash —— 不同 URL 相同正文即同源转载，纯 hash 比对不花 LLM
  3. 近似同源（相似度 >= 阈值）—— 只有这一层才调 LLM 判定
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 常见追踪参数
TRACKING_PARAMS = re.compile(
    r"^(utm_[a-z_]+|fbclid|gclid|gbraid|wbraid|msclkid|mc_cid|mc_eid|"
    r"igshid|ref_src|ref_url|spm|scm|from_source|share_source|"
    r"scene|clickid|ttclid|sr_share|_hsenc|_hsmi|yclid)$",
    re.I,
)

# 常见 AMP / 移动版后缀
AMP_SUFFIXES = ("/amp", "/amp/", ".amp")


def normalize_url(url: str) -> str:
    """规范化 URL 用于判重。保留语义参数，剥离追踪参数。"""
    if not url:
        return ""
    url = url.strip()
    parts = urlsplit(url)

    scheme = parts.scheme.lower() or "https"
    host = parts.netloc.lower()
    # 去掉 www.
    if host.startswith("www."):
        host = host[4:]
    # 去掉默认端口
    if host.endswith(":80") and scheme == "http":
        host = host[:-3]
    elif host.endswith(":443") and scheme == "https":
        host = host[:-4]

    path = parts.path or "/"
    for suf in AMP_SUFFIXES:
        if path.endswith(suf) and path != suf:
            path = path[: -len(suf)] or "/"
            break

    # 过滤查询参数；顺序归一以便同参不同序视为相同
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not TRACKING_PARAMS.match(k)]
    kept.sort()
    query = urlencode(kept)

    # 去掉 fragment
    return urlunsplit((scheme, host, path, query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()[:32]


def content_hash(text: str) -> str:
    """正文哈希。归一化空白后计算，避免排版差异导致同源漏判。"""
    if not text:
        return ""
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


def domain_of(url: str) -> str:
    try:
        host = urlsplit(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


# ---------- 来源类型推断（供信源评级用）----------

DOMAIN_HINTS: list[tuple[str, str]] = [
    (r"(^|\.)arxiv\.org$", "academic"),
    (r"(^|\.)acm\.org$", "academic"),
    (r"(^|\.)ieee\.org$", "academic"),
    (r"(^|\.)springer\.com$", "academic"),
    (r"(^|\.)sciencedirect\.com$", "academic"),
    (r"(^|\.)doi\.org$", "academic"),
    (r"(^|\.)nature\.com$", "academic"),
    (r"(^|\.)openai\.com$", "official"),
    (r"(^|\.)anthropic\.com$", "official"),
    (r"(^|\.)google\.(com|ai)$", "official"),
    (r"(^|\.)minimax\.(io|ai)$", "official"),
    (r"(^|\.)deepseek\.com$", "official"),
    (r"(^|\.)github\.com$", "community"),
    (r"(^|\.)news\.qq\.com$", "media"),
    (r"(^|\.)zhihu\.com$", "community"),
    (r"(^|\.)juejin\.cn$", "community"),
    (r"(^|\.)36kr\.com$", "media"),
    (r"(^|\.)wikipedia\.org$", "community"),
]

REPOST_HINTS = re.compile(r"(转载|转自| republished|originally published)", re.I)


def guess_source_type(url: str, title: str = "", content: str = "") -> str:
    """粗判信源类型。official/community/academic/blog/media/repost/unknown"""
    d = domain_of(url)
    for pat, kind in DOMAIN_HINTS:
        if re.search(pat, d):
            return kind
    blob = f"{title}\n{content[:500]}"
    if REPOST_HINTS.search(blob):
        return "repost"
    return "unknown"
