-- 迭代过程的决策要能事后查，而不是只能翻 stdout。
--
-- 之前的问题（R7 实跑发现）：一轮跑完只知道「跑了 2 轮」，但
--   · 第 2 轮搜的词跟第 1 轮一字不差（重复检索，烧配额还产重复段落）
--   · `consecutive_no_new` 每轮无条件 +1，2 轮就强制停
--   · 报告里 7 个小节缺料，但没人提、没人补、也没人知道为什么停
-- 三个问题都只能从 harness 的 stdout 里翻，跑完就没了。
--
-- 每次 assess / retrieve / 停止决策落一行。跑完可以直接
--   SELECT * FROM run_trace WHERE session_id=? ORDER BY round;
-- 看清楚每一轮到底决定了什么。

CREATE TABLE IF NOT EXISTS run_trace (
    session_id  TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    round       INTEGER NOT NULL,
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,     -- assess / retrieve / stop
    detail_json TEXT,
    PRIMARY KEY (session_id, round, kind)
);

CREATE INDEX IF NOT EXISTS idx_run_trace_session ON run_trace(session_id, round);