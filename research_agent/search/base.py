"""检索层：统一的 SearchProvider 接口 + 两个实现。

分工（P5 确认）：
  Tavily   → 英文
  SearXNG  → 中文
两者接口一致，主程序按 query 语言选择，可随时替换。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""
    engine: str = ""          # 来自哪个引擎/后端
    score: float = 0.0
    content: str | None = None  # 部分后端可直接给正文，省一次抓取
    meta: dict = field(default_factory=dict)

    @property
    def domain(self) -> str:
        from .normalize import domain_of

        return domain_of(self.url)


class SearchProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def search(
        self,
        query: str,
        language: str = "zh-CN",
        limit: int = 10,
    ) -> list[SearchHit]:
        ...

    def health(self) -> bool:
        """后端是否可用。惰性启停流程会先查这个。"""
        return True
