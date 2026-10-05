"""用**本项目的真实任务形态**测 Agnes 两个模型的速度。

为什么不用数学题
----------------
用户给的关键约束是免费额度 **RPM 10**（每分钟 10 次请求）。在这个额度下，
能不能用只看两件事：
  1. 单次调用多快（决定一次轻量环节要占多久配额槽）
  2. 开思考会不会烧光预算（决定 max_tokens 配多少，间接决定耗时）

前几轮探针用的是几何题，那是「长思考」场景，跟项目里的活不一样。
这里改成项目里真正在跑的四种任务形态，数字才有参考价值。

四种形态（都取自 /tmp/ra_r5_e2e.db 的真实数据）
--------------------------------------------
  A summarize  段落概括：给一个 ~700 token 段落，输出 38 字左右概括
  B probe_light 歧义判定：短问题 + 若干标题，输出 JSON
  C relevance  相关性判定：标题 + 开头 2500 字，输出 JSON
  D json        纯 JSON 短输出

每种都测 2.5 / 3.0 × 开/关思考。请求之间留 8 秒，避开 RPM 10 的限流。

用法：.venv/bin/python tests/probes/probe_agnes_speed.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import httpx  # noqa: E402

BASE = "https://apihub.agnes-ai.com/v1"
AUTH = {"Authorization": f"Bearer {os.environ['AGNES_API_KEY']}",
        "Content-Type": "application/json"}
DB = "/tmp/ra_r5_e2e.db"
GAP = 8.0          # RPM 10 → 至少 6 秒一个，留点余量


def load_real() -> dict:
    if not Path(DB).exists():
        sys.exit(f"缺真实数据 {DB}")
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    chunk = c.execute(
        "SELECT content, summary FROM chunk WHERE session_id='s_a9f5bc761016' "
        "AND summary IS NOT NULL AND length(content) > 2000 "
        "ORDER BY token_count DESC LIMIT 1").fetchone()
    titles = [r[0] for r in c.execute(
        "SELECT title FROM source WHERE session_id='s_a9f5bc761016' LIMIT 8")]
    return {"chunk": chunk["content"] if chunk else "",
            "summary": chunk["summary"] if chunk else "",
            "titles": [t for t in titles if t]}


D = load_real()


def call(model: str, system: str | None, user: str, max_tokens: int,
         thinking: bool) -> dict | None:
    payload: dict = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system else [])
                    + [{"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "chat_template_kwargs": {"enable_thinking": thinking},
    }
    t0 = time.time()
    try:
        r = httpx.post(f"{BASE}/chat/completions", json=payload,
                       headers=AUTH, timeout=180)
    except Exception as e:
        print(f"      ❌ {type(e).__name__} {str(e)[:50]}")
        time.sleep(GAP)
        return None
    dt = round(time.time() - t0, 2)
    time.sleep(GAP)
    if r.status_code != 200:
        print(f"      HTTP {r.status_code}  {str(r.json())[:70]}")
        return None
    d = r.json()
    ch = (d.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    u = d.get("usage") or {}
    det = u.get("completion_tokens_details") or {}
    return {"dt": dt, "out": msg.get("content") or "",
            "reason": det.get("reasoning_tokens", 0),
            "text": det.get("text_tokens", 0),
            "prompt": u.get("prompt_tokens", 0),
            "finish": ch.get("finish_reason"),
            "calls": u.get("cache_creation_tokens")}


TASKS = {
    "A summarize 段落概括": {
        "max": 1024,
        "system": "为报告的每个段落写一句话概括。概括句要**说清这段在讲什么**，"
                  "不是复述第一句。约 40 字。严格输出 JSON：{\"summary\": \"...\"}",
        "user": f"请为下面这一段写一句话概括。\n\n{D['chunk'][:4000]}",
        "json": True,
    },
    "B probe_light 歧义判定": {
        "max": 2048,
        "system": "判断这个问题是否需要澄清歧义。严格输出 JSON："
                  "{\"ambiguous\": true|false, \"reason\": \"...\"}",
        "user": "研究问题：固态电池的产业化进展，以及各家技术路线的分歧点。\n"
                f"检索到的标题：\n- " + "\n- ".join(D["titles"]),
        "json": True,
    },
    "C relevance 相关性判定": {
        "max": 2048,
        "system": "判断下面这段内容与研究问题是否相关。严格输出 JSON："
                  "{\"relevant\": true|false, \"why\": \"...\"}",
        "user": f"研究问题：固态电池的产业化进展，以及各家技术路线的分歧点。\n\n"
                f"内容：{D['chunk'][:2500]}",
        "json": True,
    },
    "D json 纯 JSON 输出": {
        "max": 1024,
        "system": "严格输出 JSON。",
        "user": '为「硫化物 氧化物 聚合物 卤化物」写一个术语表，'
                '格式 {"glossary": [{"canonical": "..."}]}',
        "json": True,
    },
}


def main() -> None:
    print(f"真实数据：段落 {len(D['chunk'])} 字、标题 {len(D['titles'])} 条")
    print(f"请求间隔 {GAP} 秒（RPM 10 的约束）\n")
    rows = []
    for name, t in TASKS.items():
        print("=" * 76)
        print(f"{name}   max_tokens={t['max']}  输入约 {len(t['user'])} 字")
        print("=" * 76)
        for model in ("agnes-3.0-flash", "agnes-2.5-flash"):
            for think in (False, True):
                r = call(model, t["system"], t["user"], t["max"], think)
                if not r:
                    continue
                ok = "—"
                if t["json"]:
                    try:
                        json.loads(r["out"])
                        ok = "JSON ✓"
                    except Exception:
                        ok = "JSON ✗"
                print(f"  {model:<18} think={think!s:<5} {r['dt']:>6}s  "
                      f"prompt {r['prompt']:>5}  reason {r['reason']:>5}  "
                      f"text {r['text']:>4}  {ok}  {r['finish']}")
                rows.append((name, model, think, r))
        print()

    print("=" * 76)
    print("汇总：单次耗时（秒）")
    print("=" * 76)
    print(f"  {'任务':<26}{'模型':<18}{'关思考':>9}{'开思考':>9}")
    for name in TASKS:
        for model in ("agnes-3.0-flash", "agnes-2.5-flash"):
            d = {(n, m): r["dt"] for n, m, th, r in rows if n == name and m == model
                 for th in [th]}
            off = [r["dt"] for n, m, th, r in rows
                   if n == name and m == model and th is False]
            on = [r["dt"] for n, m, th, r in rows
                  if n == name and m == model and th is True]
            print(f"  {name:<26}{model:<18}"
                  f"{off[0] if off else '—':>9}{on[0] if on else '—':>9}")
    print("\n  判读：")
    print("  · 轻量任务（summarize / 短 JSON）谁快，就把它分给谁")
    print("  · 重任务（probe_light / relevance 长输入）谁稳，就把它分给谁")
    print("  · 开思考只在「思考 token 占预算比」高时才值得——看 reason/text 的比值")


if __name__ == "__main__":
    main()
