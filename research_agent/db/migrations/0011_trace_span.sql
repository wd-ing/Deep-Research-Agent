-- ---------- trace_span：通用时间线埋点 + llm_call 细分耗时（2026-10-21）----------
--
-- 为什么要这张表：写作子图里有两段 45~50s 的「非 LLM 静默」，llm_call /
-- run_trace 都看不见它（一个只管 LLM 调用，一个只记主图节点毫秒数）。
-- 要查就得有「任意一段代码的起止绝对时间」——这张表就是那个通用容器：
-- 节点、子图节点、checkpoint 写入、工具循环轮次、检索子步骤、限流等待
-- 都落成带 epoch 起止的行，timeline_report 再离线对账。
--
-- **故意不加外键**：llm_call 的 session 外键曾经让孤儿记录被静默丢
-- （CONTEXT 地雷「测试库要建 session」）。span 是调试数据，
-- 一条记不上最多亏一行，不能因为 session 不存在就丢。
--
-- started_at/ended_at 是 epoch 秒（time.time()），不是 ISO 字符串——
-- 要对 llm_call 之类的绝对时间做减法，字符串每算一次都得先解析。

CREATE TABLE IF NOT EXISTS trace_span (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT,
    kind        TEXT NOT NULL,        -- node/subnode/checkpoint/toolround/retrieve/llm_wait
    name        TEXT NOT NULL,        -- 如 write.plan_spec / checkpoint.put / retrieve.fetch
    started_at  REAL NOT NULL,        -- epoch 秒
    ended_at    REAL NOT NULL,
    thread      TEXT,                 -- 写入线程名，并发诊断用
    detail_json TEXT                  -- bytes / llm_ms / tool_ms / url 等，按 kind 各记各的
);

CREATE INDEX IF NOT EXISTS idx_trace_span_session ON trace_span(session_id, started_at);

-- llm_call 三个细分列（同一次迁移里 ALTER）：
--   started_at  调用开始的 epoch（限流排队之前），用来和 trace_span 对轴
--   wait_ms     _throttle 限流器排队等待毫秒数——latency_ms 里一直含着它，
--               现在单独拆出来才能分辨「慢在平台」还是「慢在排队」
--   ttfb_ms     流式=请求发出到首个内容块；非流式=请求发出到响应头
ALTER TABLE llm_call ADD COLUMN started_at REAL;
ALTER TABLE llm_call ADD COLUMN wait_ms INTEGER;
ALTER TABLE llm_call ADD COLUMN ttfb_ms INTEGER;
