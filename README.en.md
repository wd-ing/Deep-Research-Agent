# Deep Research Agent

Turn a real question into a research report you can **trace, sentence by sentence**.

It searches in rounds, reads the originals, grades every source — and only starts writing **after you approve the outline**.
Every claim carries a paragraph citation you can open; anything uncertain gets a red "doubt" seal instead of being silently skipped.

[中文](README.md) · [Homepage](https://research.wding.site/homepage/) · [Film](https://research.wding.site/homepage/film.html) · [Hosted demo (invite-only)](https://research.wding.site/)

> Contributors: read [`CONTEXT.md`](CONTEXT.md) first (architecture, standing decisions, pitfalls, known gaps — written in Chinese), then [`TECH_DEBT.md`](TECH_DEBT.md).
> Development notes: [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md). Deployment: [`deploy/README.md`](deploy/README.md).

## Features

- Multi-round retrieval with a paragraph-level evidence gate (filters off-topic pages, templated pages, untraceable repost sites).
- Human-approved outline before any writing starts.
- Paragraph-level citations (`[S2·1]`) that open the original passage; sources are graded (first-party / media / aggregator / research / community).
- Cross-review and revision with a tracked-changes view; uncertain claims are sealed.
- Standard mode (~8–11 min incl. approval) and fast mode (~2–3 min brief, then one click to deepen into a full report, reusing the retrieval already done).
- Export to Markdown / PDF (browser print) / Word; every round and call is stored and replayable at zero token cost.
- Multi-user: invite-token login, per-user data isolation, daily quotas, a global concurrency cap, admin-only config endpoints.
- Input intent gate: greetings, "who are you", "write me a poem" are answered in place instead of burning a research run.

## Quick start (local)

Requires Python 3.12, Node 22, a [Volcengine Ark](https://www.volcengine.com/product/ark) API key (models) and a [Tavily](https://tavily.com) key (English search).
Chinese queries use a local SearXNG (optional; see [README.md](README.md#中文检索searxng可选) for the Docker command).

```bash
git clone https://github.com/wd-ing/Deep-Research-Agent.git
cd Deep-Research-Agent
python3.12 -m venv .venv
.venv/bin/pip install -e .          # editable install is required: the code locates config/ and data/ by file path
cp .env.example .env                # fill in ARK_API_KEY and TAVILY_API_KEY
set -a; . ./.env; set +a
RA_AUTH=0 .venv/bin/python -m uvicorn research_agent.server:app --port 8000

# another terminal
cd frontend && npm install && npm run dev      # → http://localhost:5180 (proxies /api to 127.0.0.1:8000)
```

Auth is **on by default** and the server refuses to start with no users; `RA_AUTH=0` is for single-user local development only.
**Real runs cost money**: roughly 300k tokens for a standard run, 60–110k for a fast one, plus search credits.

## Tests

```bash
.venv/bin/python tests/runall.py      # full regression: all offline, zero API cost
```

Tests are plain scripts (`main()` + `check()`), not pytest-collected. New test scripts must `import _isolate` before importing application modules.

## Deployment

See [`deploy/README.md`](deploy/README.md): systemd unit, Caddy snippet, SearXNG container, invite-token users (`python -m research_agent.users add|list|disable|enable|rotate`). Run with a single worker.

## Known limitations

- Chinese retrieval needs SearXNG. A standard run is ~300k tokens; fast→deepen is not cheaper than a fresh standard run.
- No "reject" on the approval page yet. Report quality is judged by process health (zero errors, citations in range, audit actually ran, no empty sections), not length.
- Single process, SQLite, built for small-group use — not a large multi-tenant service.

Full, evidence-backed lists live in [`CONTEXT.md`](CONTEXT.md) and [`TECH_DEBT.md`](TECH_DEBT.md).

## License

[MIT](LICENSE)
