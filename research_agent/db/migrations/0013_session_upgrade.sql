-- ---------- 快速档一键补深度（2026-11-05）----------
--
-- 为什么加：升级 = 对已完成的快速档会话续跑标准档。
-- `upgraded_from` 记录「这行 session 是从哪档升上来的」（NULL=原生），
-- 一是防止重复升级（claim_upgrade 的条件之一），
-- 二是让列表/详情能把「补过深度的报告」标出来。
--
-- `report_version` 存升级前的产物快照：快速档简报被标准档报告
-- 覆盖前先把 markdown 留一份（kind='fast_brief'），升级后用户
-- 仍能回看原来的简报——没有这个表，升级=原报告被静默覆盖。
-- 级联删除：会话 purge 掉时快照跟着走（外键 ON DELETE CASCADE）。

ALTER TABLE session ADD COLUMN upgraded_from TEXT;

CREATE TABLE report_version (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,            -- 'fast_brief'
  markdown TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_report_version_session ON report_version(session_id);
