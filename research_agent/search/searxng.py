"""SearXNG provider：中文检索主力。

惰性启停：首次 search 前调脚本拉起，空闲 60 秒后自动关。

**空闲回收不在本进程内做。** 早先用 `threading.Timer` 守护线程计时，
但 Python 进程一退出线程就没了——实测一次性脚本检索后退出，108 秒后
SearXNG 仍在跑、健康检查 200。这才是一次次留下孤儿进程的成因。

现在改为：每次检索 touch 一下 `.searxng/heartbeat`，回收由独立的
watchdog.sh 进程按文件新鲜度执行。规则与「谁发起的检索」「请求方是否
还活着」完全解耦，本进程不再持有任何计时器。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx

from .base import SearchHit, SearchProvider


class SearXNGProvider(SearchProvider):
    name = "searxng"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8888",
        script: str | Path = ".searxng/searxng.sh",
        timeout_s: float = 35.0,
        engines: list[str] | None = None,
        idle_shutdown_s: int = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.script = Path(script)
        self.timeout_s = timeout_s
        self.engines = engines or ["sogou", "360search", "quark", "bing"]
        # 保留这个字段只为兼容调用方；实际超时由 watchdog.sh 决定，
        # 见 _beat()。这里只用于决定「多久算过期」的自我提醒。
        self.idle_shutdown_s = idle_shutdown_s
        self._heartbeat = self.script.parent / "heartbeat"

    # ---------- 惰性启停 ----------

    def _beat(self) -> None:
        """写心跳。看门狗只看这个文件的 mtime，不看本进程状态。"""
        try:
            self._heartbeat.parent.mkdir(parents=True, exist_ok=True)
            self._heartbeat.touch()
        except OSError:
            pass

    def health(self) -> bool:
        try:
            r = httpx.get(
                f"{self.base_url}/config",
                headers={"Accept": "application/json"},
                timeout=3.0,
            )
            return r.status_code == 200
        except Exception:
            return False

    def ensure_running(self) -> bool:
        if self.health():
            self._beat()
            return True
        if not self.script.exists():
            return False
        try:
            subprocess.run(
                [str(self.script), "start"],
                cwd=str(self.script.parent.parent),
                capture_output=True,
                timeout=30,
            )
        except Exception:
            return False
        if self.health():
            self._beat()
            return True
        return False

    def shutdown_if_idle(self) -> None:
        """立即关闭。空闲超时的回收由 watchdog.sh 负责，不经过这里。"""
        if not self.script.exists():
            return
        try:
            subprocess.run(
                [str(self.script), "stop"],
                cwd=str(self.script.parent.parent),
                capture_output=True,
                timeout=15,
            )
        except Exception:
            pass

    # ---------- 检索 ----------

    def search(
        self,
        query: str,
        language: str = "zh-CN",
        limit: int = 10,
    ) -> list[SearchHit]:
        if not self.ensure_running():
            return []
        self._beat()
        params = {
            "q": query,
            "format": "json",
            "language": language,
        }
        if self.engines:
            params["engines"] = ",".join(self.engines)
        try:
            r = httpx.get(
                f"{self.base_url}/search",
                params=params,
                timeout=self.timeout_s,
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            return []

        hits: list[SearchHit] = []
        for item in data.get("results", []):
            url = item.get("url") or ""
            if not url:
                continue
            engines = item.get("engines") or []
            hits.append(
                SearchHit(
                    url=url,
                    title=(item.get("title") or "").strip(),
                    snippet=(item.get("content") or "").strip(),
                    engine=engines[0] if engines else "",
                    score=float(item.get("score") or 0.0),
                    content=item.get("content") if len(item.get("content") or "") > 200 else None,
                    meta={"engines": engines, "language": language},
                )
            )
            if len(hits) >= limit:
                break
        return hits
