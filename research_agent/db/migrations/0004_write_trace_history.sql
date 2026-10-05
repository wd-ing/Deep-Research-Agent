-- 审查结论原来每轮覆盖：`_trace(audit=...)` 走的是 upsert，
-- audit_json 只剩最后一轮。R5 那轮 27 次返修调用没收敛，
-- 第 1、2 轮的发现全部丢失，只能靠读代码反推根因。
--
-- 追加式留痕：保留原来的「最后一次」字段不动（下游读它），
-- 同时把每一轮的摘要按顺序堆起来，这样任何「对比两轮」的查询都查得出来。

ALTER TABLE write_trace ADD COLUMN audit_history_json TEXT;  -- [{round, need_rework, findings, high, rework_effective, reason}]
