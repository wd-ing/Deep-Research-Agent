-- ---------- 多人使用：归属 + 用量（坑 99·段 1）----------
-- owner：数据隔离的最小单位；NULL=本功能之前的遗留行，仅 admin 可见。
-- usage_event：每人每日场次记账（run/upgrade 认领时写，检查+写同事务）。

ALTER TABLE session ADD COLUMN owner TEXT;
ALTER TABLE folder ADD COLUMN owner TEXT;

CREATE TABLE usage_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner TEXT NOT NULL,
  kind TEXT NOT NULL,           -- 'standard'|'fast'
  session_id TEXT,
  ts REAL NOT NULL
);
CREATE INDEX idx_usage_owner_ts ON usage_event(owner, ts);
