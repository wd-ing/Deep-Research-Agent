"""正文抓取与抽取。

抓取礼貌性（P7 确认）：
  - 同域名请求间隔 1 秒
  - 403/429/503 直接跳过，绝不重试、绝不换 IP
  - 时间敏感问题绕过 7 天缓存
  - 识别验证码页并跳过
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from dataclasses import dataclass

import httpx

from .split import RecursiveSplitter, count_tokens
from ..trace import record_span

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

BLOCK_STATUS = {403, 429, 503}
CAPTCHA_HINTS = re.compile(
    r"(安全验证|请输入验证码|滑动验证|人机验证|access denied|captcha|"
    r"are you a robot|checking your browser|请完成验证)",
    re.I,
)


def _decode(r) -> str:
    """按真实编码解码 HTML。

    httpx 只认响应头里声明的 charset，缺省就当 utf-8。国内站点里相当一部分
    是 GB2312/GBK 且不声明或声明错，整篇正文会变成「������」，
    抽出来的断言自然是废的——中文语料占我们来源的一半，这一步不能省。
    """
    body = r.content
    enc = (r.encoding or "").lower().replace("_", "-")
    if enc and enc not in ("utf-8", "utf8", "ascii"):
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            pass
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(body).best()
        if best is not None:
            return str(best)
    except Exception:
        pass
    return body.decode("utf-8", errors="replace")


class _DomainThrottle:
    """同域名最小请求间隔。"""

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    async def wait(self, domain: str) -> None:
        while True:
            with self._lock:
                now = time.time()
                prev = self._last.get(domain, 0.0)
                gap = self.interval - (now - prev)
                if gap <= 0:
                    self._last[domain] = now
                    return
            await asyncio.sleep(gap)


@dataclass
class Page:
    url: str
    ok: bool
    text: str = ""
    status: int = 0
    reason: str = ""
    title: str = ""
    published: str | None = None


class Fetcher:
    def __init__(
        self,
        repo,
        throttle_s: float = 1.0,
        concurrency: int = 8,
        timeout_s: float = 20.0,
        cache_ttl_days: int = 7,
    ) -> None:
        self.repo = repo
        self.throttle = _DomainThrottle(throttle_s)
        self.sem = asyncio.Semaphore(concurrency)
        self.timeout_s = timeout_s
        self.cache_ttl_days = cache_ttl_days

    async def fetch_many(
        self,
        urls: list[str],
        bypass_cache: bool = False,
    ) -> list[Page]:
        from ..search.normalize import content_hash, domain_of, normalize_url

        async with httpx.AsyncClient(
            timeout=self.timeout_s,
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        ) as client:
            async def one(url: str) -> Page:
                # 每页一个 span：检索节点里「抓」占多少、缓存命中多少，
                # 靠这些行和 retrieve.fetch_all 对出来。
                t0 = time.time()

                def _done(page: Page) -> Page:
                    record_span("retrieve", "fetch", t0, time.time(), {
                        "url": url, "ok": page.ok, "status": page.status,
                        "cached": page.reason == "cache",
                        "bytes": len(page.text or ""),
                        "reason": page.reason or None})
                    return page

                norm = normalize_url(url)
                if not bypass_cache:
                    cached = self.repo.cache_get(norm, self.cache_ttl_days)
                    if cached:
                        return _done(Page(url=url, ok=True, text=cached,
                                          status=200, reason="cache"))
                async with self.sem:
                    await self.throttle.wait(domain_of(url))
                    try:
                        r = await client.get(url)
                    except Exception as e:
                        return _done(Page(url=url, ok=False,
                                          reason=f"{type(e).__name__}"))
                if r.status_code in BLOCK_STATUS:
                    # 明确不重试、不换 IP。记录下来供 UI 展示
                    return _done(Page(url=url, ok=False, status=r.status_code,
                                      reason=f"blocked:{r.status_code}"))
                if r.status_code >= 400:
                    return _done(Page(url=url, ok=False, status=r.status_code,
                                      reason=f"http:{r.status_code}"))
                html = _decode(r)
                if CAPTCHA_HINTS.search(html[:8000]):
                    return _done(Page(url=url, ok=False, status=r.status_code,
                                      reason="captcha"))
                page = self._extract(html, url, r.status_code)
                if page.ok and not bypass_cache:
                    self.repo.cache_put(norm, url, page.text,
                                         content_hash(page.text), r.status_code)
                return _done(page)

            return await asyncio.gather(*(one(u) for u in urls))

    def _extract(self, html: str, url: str, status: int) -> Page:
        try:
            import trafilatura
            from trafilatura.metadata import extract_metadata
        except Exception as e:
            return Page(url=url, ok=False, status=status, reason=f"trafilatura:{e}")

        try:
            text = trafilatura.extract(
                html,
                url=url,
                include_comments=False,
                include_tables=False,   # v1 不抽表格，已确认
                favor_precision=True,
                # 段落结构只能靠它带回来。不开时整篇文章压成一行、零空行
                # （25 篇实测 25 个「原始段落」），切分的最高优先级分隔符
                # \n\n 永远不触发，只能退到句子边界切。多出来的字符全是
                # #/##/### 标题标记与 **加粗**，去掉标记后没有任何一行是
                # 原文没有的——不是噪声，是结构线索，所以不清洗。
                include_formatting=True,
            )
        except Exception as e:
            return Page(url=url, ok=False, status=status, reason=f"extract:{e}")

        if not text or len(text) < 200:
            return Page(url=url, ok=False, status=status, reason="too_short")

        title, published = "", None
        try:
            md = extract_metadata(html, default_url=url)
            if md:
                title = md.title or ""
                published = md.date
        except Exception:
            pass

        return Page(url=url, ok=True, text=text, status=status,
                    title=title, published=published)


