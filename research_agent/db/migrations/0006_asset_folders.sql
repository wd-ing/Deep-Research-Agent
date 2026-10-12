-- 产品化：文件夹式资产体系 + 软删除 + 会话摘要 + 无用来源标记
--
-- 背景（2026-10-12）：砍掉跨会话召回与上传语料后，聚焦「检索 → 写作」
-- 主链路的产品体验。核心诉求是**让界面有逻辑、历史信息方便溯源和回看**。
--
-- 这一批加的四样东西，各解决一个具体问题：
--
-- 1. folder 表 + session.folder_id
--    用户要求「按照层级来做归档」「自定义对话归档在哪个文件夹」，
--    所以 parent_id 保留嵌套能力（先做平级 UI，schema 先留口子）。
--    不建这个的话资产只能按时间排，几场之后就找不回来了。
--
-- 2. session.deleted_at
--    删除改成**软删除**：级联删报告/来源/段是不可逆的，而误删是最容易
--    发生的损失。改一个 UPDATE 而不是 DELETE，代价极小。
--    默认列表过滤掉，另给回收站视图可恢复。
--
-- 3. session.summary_json
--    资产列表要能「不点进去就知道大致内容」，所以写完报告时生成一份
--    结构化摘要（summary / key_points / gaps），列表直接显示。
--    gaps 与报告的「存疑与未完成」同源，不另编一套。
--
-- 4. ignored_source 表
--    实测库里混进了一条 VRPorn.com（与固态电池毫无关系）却通过了
--    relevance 闸门。闸门是 prompt 层的判断，调它风险大；
--    这里加产品层兜底：用户可标记排除，报告引用到时显示提示。

CREATE TABLE IF NOT EXISTS folder (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    parent_id  TEXT REFERENCES folder(id) ON DELETE SET NULL,  -- 留嵌套口子
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_folder_parent ON folder(parent_id, sort_order);

ALTER TABLE session ADD COLUMN deleted_at TEXT;     -- 非空 = 在回收站
ALTER TABLE session ADD COLUMN folder_id TEXT REFERENCES folder(id) ON DELETE SET NULL;
ALTER TABLE session ADD COLUMN summary_json TEXT;   -- {summary, key_points[], gaps[]}

CREATE INDEX IF NOT EXISTS idx_session_folder ON session(folder_id);
CREATE INDEX IF NOT EXISTS idx_session_deleted ON session(deleted_at);

-- 无用来源排除。source_id 主键 = 一个来源只能被标记一次（重复标记是幂等的）。
CREATE TABLE IF NOT EXISTS ignored_source (
    source_id  TEXT PRIMARY KEY REFERENCES source(id) ON DELETE CASCADE,
    reason     TEXT,                 -- 为什么排除（可选，用户自己写）
    ignored_at TEXT NOT NULL
);
