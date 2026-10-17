"""主动限流 `SendRateLimiter` 的守门测试（离线，不需要网络）。

2026-10-17 加。限流器最坏的失败模式**不是抛异常，而是「装上了但不生效」**
——它只会让一切变慢，不报错。所以这个测试重点不是「有没有报错」，
而是「**窗口内的实际发送次数有没有真的被压住**」。

验的五件事：
  1. 限内不等待（不能给首批调用凭空加延迟）
  2. 超限会等，且等完确实能发出去（不能卡死）
  3. **线程共享**：并发线程不能各自拥有一个窗口（那等于没装）
  4. 按**端点**分桶，不是按 client 实例分
  5. 统计可读（限流是主动引入的等待，必须能被查出来）

用法：.venv/bin/python tests/test_rate_limiter.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research_agent.llm.provider import (  # noqa: E402
    LLMClient, SendRateLimiter, get_limiter, reset_limiters,
)

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {label}" + (f"  —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def t1_no_wait_under_limit():
    lim = SendRateLimiter(100)
    t0 = time.monotonic()
    for _ in range(50):
        lim.acquire()
    dt = time.monotonic() - t0
    check("① 限内零等待", dt < 0.05, f"50 次花了 {dt:.3f}s")
    check("① 没产生等待计数", lim.waits == 0, f"waits={lim.waits}")


def t2_blocks_past_limit():
    """超限必须真的被压住 —— 用 1 秒窗口的等价手法：把额度调到很小。"""
    lim = SendRateLimiter(2, window_s=0.4)
    lim.acquire()
    lim.acquire()
    t0 = time.monotonic()
    lim.acquire()          # 第 3 次应当被挡住
    dt = time.monotonic() - t0
    # 窗口是 60 秒，所以第 3 次要等到最老那条出窗——这里只断言「它确实在等」
    # 且统计被记上，不真的睡 60 秒（否则测试没法跑）。
    check("② 超限确实在等", dt > 0.01, f"等了 {dt:.3f}s")
    check("② 等待被计数", lim.waits == 1, f"waits={lim.waits}")
    lim._stamps.clear()


def t3_thread_shared():
    """🔴 并发线程必须共用一个窗口。

    这是限流器最容易假装的失败：不加锁的话每个 worker 各有一份窗口，
    N 个线程就能发 N×per_minute 个请求，而**一切正常、不报错**。
    """
    lim = SendRateLimiter(per_minute=5, window_s=0.4)
    got: list[float] = []
    lock = threading.Lock()

    def worker():
        w = lim.acquire()
        with lock:
            got.append(w)

    ths = [threading.Thread(target=worker) for _ in range(20)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    waited = [w for w in got if w > 0]
    # 5 个额度、20 个线程 ⇒ 至少 15 个必须等。共享窗口下这是必然的。
    check("③ 20 线程 / 5 额度 ⇒ 至少 15 个被挡", len(waited) >= 15,
          f"实际等待 {len(waited)} 个")
    check("③ 计数累计一致", lim.waits == len(waited),
          f"lim.waits={lim.waits} / 实际 {len(waited)}")
    check("③ 窗口内不超过额度", lim.stats()["in_window"] <= 5,
          f"in_window={lim.stats()['in_window']}")
    lim._stamps.clear()


def t4_endpoint_scoping():
    """限额按**端点**算，不按 client 实例算。"""
    reset_limiters()
    a = get_limiter("https://a.example.com/v1", 30)
    b = get_limiter("https://b.example.com/v1", 30)
    a2 = get_limiter("https://a.example.com/v1", 30)
    check("④ 同端点同实例", a is a2)
    check("④ 不同端点不同桶", a is not b)
    # 配置改了要跟着改，否则「改了配置没生效还不报错」
    a3 = get_limiter("https://a.example.com/v1", 10)
    check("④ 限额变更跟着生效", a3 is a and a3.per_minute == 10,
          f"per_minute={a3.per_minute}")


def t5_hook_wired():
    """🔴 限流必须真的挂到 httpx 上——**装上了但不生效是最坏的失败**。

    ⚠️ **测法本身踩过两次坑，两个都值得记**：

    1. 第一版桩的是 `client._client.stream`。那**完全绕过 httpx**，
       event hook 压根不会被触发 —— 于是表现成「限流没生效」，
       而其实是**测法不对**。教训与坑 50 同源：**桩要接在正确的层上**。
       正解是换**传输层**（`MockTransport`）：请求真的走 httpx 管线，
       hook 会跑，又不打网络。
    2. 中间那版在 `registry.stage()` **之前**设限额，被建 stage 时按配置
       覆写回 45，于是 4 次调用一次都不等，看着也像「没生效」。
       限额必须在 stage 之后设。
    """
    import httpx
    reset_limiters()
    c = LLMClient()
    sent = {"n": 0}

    import os
    import os
    # ⚠️ 密钥桩必须按**钉住之后**的 cfg 来给：先前是从 `write_plan_tasks`
    # 读的，而那时它解析到 minimax，于是给错了键名，报「未设置 INTERN_AI_API_KEY」。
    env_name = c.registry.stage("write_plan_tasks").api_key_env
    os.environ.setdefault(env_name or "X", "dummy")
    # ⚠️ **必须钉住 provider**：本用例验的是 **OpenAI 兼容路径**的流式发送
    # （`stream: true` ⇒ `.stream()`）。默认 provider 切成 minimax 之后
    # （2026-10-17），它会走 `_minimax_tools` —— **只实现非流式、用 `.post`**，
    # 于是 `.stream` 桩形同虚设、真的打了网络，报出来的是 401/404。
    # 症状与「限流钩子没生效」毫无关系，排查会被带偏很久。
    import copy
    _base = c.registry.stage("write_plan_tasks")
    _prof = (c.registry.raw["providers"])["internai"]
    _cfg = copy.copy(_base)
    _cfg.provider = "internai"
    _cfg.model = _prof["model"]
    _cfg.base_url = _prof["base_url"]
    _cfg.api_key_env = _prof["api_key_env"]
    _cfg.stream = True
    c.registry._cache["_t5"] = _cfg
    os.environ.setdefault(_cfg.api_key_env or "X", "dummy")
    # 限额必须在 `registry.stage()` 之后设：建 stage 会按配置把桶覆写成 45
    lim = get_limiter("https://discovery-api.intern-ai.org.cn/v1", 3)
    lim.window_s = 0.4   # 只为让测试跑得完，生产永远是 60s

    def handler(request: "httpx.Request") -> "httpx.Response":
        sent["n"] += 1
        return httpx.Response(200, content=(
            'data: {"choices":[{"delta":{"content":"ok"}}]}\n'
            'data: {"choices":[{"finish_reason":"stop"}],'
            '"usage":{"completion_tokens":1}}\n'
            "data: [DONE]\n").encode(),
            headers={"content-type": "text/event-stream"})

    c._client._transport = httpx.MockTransport(handler)
    try:
        from research_agent.graph.subgraphs import write as W
        for _ in range(4):
            c.complete_with_tools("_t5", "s",
                                  [{"role": "user", "content": "p"}],
                                  [W.SUBMIT_TASKS], tool_choice="submit_tasks")
    finally:
        c.close()

    check("⑤ 4 次调用全部真的发了", sent["n"] == 4, f"实际 {sent['n']}")
    check("⑤ 钩子确实记了等待（限到 3 却发了 4 次）", lim.waits >= 1,
          f"waits={lim.waits}")
    check("⑤ 限流统计可读", set(lim.stats()) >=
          {"per_minute", "waits", "waited_s", "in_window"}, str(lim.stats()))


def t6_config_value():
    """配置里写的限额必须真的读进去——「配置写了但没生效」也是沉默的。"""
    from research_agent.llm.provider import ModelRegistry
    reset_limiters()
    reg = ModelRegistry()
    prof = (reg.raw.get("providers") or {})
    rpm = (prof.get("internai") or {}).get("rate_limit_per_min")
    check("⑥ internai 配了限额", rpm == 45, f"rate_limit_per_min={rpm}")
    # ⚠️ **不能靠 `reg.stage("write_plan_tasks")` 去触发登记**：
    # 它跟随 `defaults.provider`，而默认已经切成 minimax（2026-10-17），
    # 于是登记的是 minimax 的桶（它没配限额），断言必然挂 ——
    # 而且挂的原因跟本用例要验的东西毫无关系。
    # 钉住要测的那个 provider。
    base = reg.stage("write_plan_tasks")
    import copy
    cfg = copy.copy(base)
    cfg.provider = "internai"
    cfg.base_url = "https://discovery-api.intern-ai.org.cn/v1"
    reg._cache["write_plan_tasks"] = cfg
    reg.stage("write_plan_tasks")
    lim = get_limiter("https://discovery-api.intern-ai.org.cn/v1", rpm or 0)
    check("⑥ 读 stage 后桶已登记", lim.per_minute == 45,
          f"per_minute={lim.per_minute}")


def main() -> int:
    for fn in (t1_no_wait_under_limit, t2_blocks_past_limit,
               t3_thread_shared, t4_endpoint_scoping,
               t5_hook_wired, t6_config_value):
        fn()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：")
        for f in FAILED:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
