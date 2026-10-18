-- ---------- SSE 事件流落库（2026-10-18）----------
--
-- 为什么加：回放测试的 fixture 只能靠「跑的时候有人手工录」。fixture
-- 老化过一次——write 阶段的事件形状换了（gate/plan_spec/revise 全是新的），
-- 老 fixture 还在放旧形状，前端的新渲染分支等于从没被真实数据验过。
-- 现在每次真跑自动沉淀事件流，tests/dump_events.py 一条命令导出
-- 成 tests/fixtures/ 的格式。
--
-- kind 存外层信封类型（node/progress/thinking/draft/done/error），
-- node 存 progress 事件的来源节点（前端 buildPhases 靠它归类，
-- 丢了它写作阶段会把规划步骤全吞掉——那个 bug 的教训）。
-- seq 用 rowid 自增保证顺序，不用额外列。
--
-- ⚠️ 没有外键：这是日志表，宁可留下 session 已删的孤儿事件，
--   也不能因为外键失败把一次 emit 弄丢（emit 失败本来就静默）。

CREATE TABLE IF NOT EXISTS sse_event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    at         TEXT NOT NULL,            -- ISO 时间，与 session.created_at 同形
    kind       TEXT NOT NULL,
    node       TEXT,                     -- progress 事件才有
    event_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sse_event_session ON sse_event(session_id, id);
