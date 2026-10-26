-- ---------- 会话档位（2026-10-27，快速档 C1）----------
--
-- 为什么加：快速档 = 「检索 + 整理」，不要大纲审批、不做评估循环、
-- 不走写作子图。它与标准档共用同一条 graph/同一张 session 表，
-- 区分方式必须落库——`run_session` 靠 `session.mode` 决定初始
-- state 里放哪条路由键，列表/详情要能告诉用户这份报告是哪档。
--
-- 存字符串而不是布尔：`fast` 之后还会有第三档的可能（例如「只检索」），
-- TEXT 枚举值可扩展；缺省一律按 'standard'——旧库升级后行为不变。

ALTER TABLE session ADD COLUMN mode TEXT NOT NULL DEFAULT 'standard';
