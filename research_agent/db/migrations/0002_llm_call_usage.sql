-- 记账要能支撑测试期的质量分析，光有 token 不够。
--
-- 1. 原来 input_tokens / output_tokens 是用 _rough_tokens() 在本地估算的，
--    不是 API 报上来的真实值。估算值可以用来做大致的成本感知，但没法用来
--    判断缓存到底命中了多少——那正是 write 环节最需要看的数据。
-- 2. latency_ms 字段建了却从来没写过，34 次调用全是 NULL。
-- 3. write 子图的规约与任务列表是判断实现质量的关键中间产物，
--    散在事件日志里看不出全貌，需要单独落一份可查。

ALTER TABLE llm_call ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE llm_call ADD COLUMN cache_creation_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE llm_call ADD COLUMN est_input_tokens INTEGER;   -- 本地估算值，保留以便对比偏差

-- write 子图的中间产物。plan 每跑一次 upsert 一行，audit 每次更新。
CREATE TABLE IF NOT EXISTS write_trace (
    session_id   TEXT PRIMARY KEY REFERENCES session(id) ON DELETE CASCADE,
    spec_json    TEXT,      -- 写作规约：thesis/glossary/units/cautions/spec_uncertainty
    tasks_json   TEXT,      -- 任务列表
    audit_json   TEXT,      -- 最近一次审查结论
    stats_json   TEXT,      -- 任务数/返修轮数/引用问题等
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
