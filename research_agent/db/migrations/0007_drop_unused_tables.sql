-- ---------- 删掉三张从未真正使用的表（2026-10-13）----------
--
-- 这三张表在 0001 里就建了，但配套代码从来没跑通过一次真实数据流：
--
--   contradiction / topic / session_topic
--       对应的 repo 方法（add_contradiction / set_topics / topics_of /
--       sessions_by_topic）**零调用点**，端点通着却从没被前端调用，
--       两个真实库里都是 0 行。`session_topic` 引用 `topic`，
--       所以先删子表再删父表（索引随表一起删，不用单独 DROP）。
--
--   ingested_doc
--       上传语料 2026-10-12 砍掉后，**连一行读写代码都没有**，
--       建表语句本身就是它唯一的引用。
--
-- 同时改了 0001_init.sql：新库从一开始就不建这三张表，
-- 迁移历史里不会出现「先建后删」的噪音。
-- `schema_migrations` 只记文件名、没有 checksum 校验，
-- 所以改 0001 对已经跑过它的库没有任何影响（不会重跑）。

DROP TABLE IF EXISTS session_topic;
DROP TABLE IF EXISTS topic;
DROP TABLE IF EXISTS contradiction;
DROP TABLE IF EXISTS ingested_doc;
