"""Tavily provider：英文检索主力。

注意：不要用 Tavily 的 /research 端点。research 端点自带编排且
4-250 credit/次，与我们的循环冲突，所以明确禁用。
"""

from __future__ import annotations

import os

import httpx

from .base import SearchHit, SearchProvider


class TavilyProvider(SearchProvider):
    name = "tavily"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.tavily.com",
        search_depth: str = "basic",
        max_results: int = 8,
        timeout_s: float | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.search_depth = search_depth
        self.max_results = max_results
        # 单次搜索上限（2026-10-25）：v18 基线测得 p50 3.2s / p90 5.7s /
        # max 49.3s——长尾查询把 retrieve R1 拖到 82s。B2-2 收紧到默认
        # 12s（p90 的两倍以上仍有富余），RA_SEARCH_TIMEOUT_S 可覆盖；
        # 超时按「该查询失败」处理。
        self.timeout_s = (timeout_s if timeout_s is not None else
                          float(os.environ.get("RA_SEARCH_TIMEOUT_S", "12")))

    def health(self) -> bool:
        if not self.api_key:
            return False
        try:
            r = httpx.get(
                f"{self.base_url}/usage",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=10.0,
            )
            return r.status_code == 200
        except Exception:
            return False

    def usage(self) -> dict:
        """读额度。usage 接口可能有延迟，用途是记账不是实时控制。"""
        if not self.api_key:
            return {}
        try:
            r = httpx.get(
                f"{self.base_url}/usage",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=10.0,
            )
            return r.json() if r.status_code == 200 else {}
        except Exception:
            return {}

    def search(
        self,
        query: str,
        language: str = "en",
        limit: int = 10,
    ) -> list[SearchHit]:
        if not self.api_key:
            return []
        n = min(limit, self.max_results)
        payload = {
            "query": query,
            "search_depth": self.search_depth,
            "max_results": n,
            "include_answer": False,
        }
        try:
            r = httpx.post(
                f"{self.base_url}/search",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                json=payload,
                timeout=self.timeout_s,
            )
            r.raise_for_status()
            data = r.json()
        except httpx.TimeoutException:
            # 超时要让人看见（retrieve 的 search span 记 timeout=True、
            # 事件里记 error）——吞成 [] 会把「慢」伪装成「没命中」。
            raise
        except Exception:
            return []

        hits: list[SearchHit] = []
        for item in data.get("results", []):
            url = item.get("url") or ""
            if not url:
                continue
            content = item.get("raw_content") or item.get("content") or ""
            hits.append(
                SearchHit(
                    url=url,
                    title=(item.get("title") or "").strip(),
                    snippet=(item.get("content") or "").strip(),
                    engine="tavily",
                    score=float(item.get("score") or 0.0),
                    content=content if len(content) > 200 else None,
                    meta={"published_date": item.get("published_date")},
                )
            )
            if len(hits) >= n:
                break
        return hits

    def extract(self, urls: list[str]) -> dict[str, str]:
        """批量抽取正文。Tavily 的 extract 端点按 5 个 URL 记 1 credit。"""
        if not self.api_key or not urls:
            return {}
        out: dict[str, str] = {}
        for i in range(0, len(urls), 5):
            batch = urls[i : i + 5]
            try:
                r = httpx.post(
                    f"{self.base_url}/extract",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    json={"urls": batch, "extract_depth": "basic"},
                    timeout=60.0,
                )
                r.raise_for_status()
                for item in r.json().get("results", []):
                    u = item.get("url")
                    if u and item.get("raw_content"):
                        out[u] = item["raw_content"]
            except Exception:
                continue
        return out
