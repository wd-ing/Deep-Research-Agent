-- 段级相关性 + 来源性质。证据树从「源 → 断言」变成「源 → 段 → 段概括」。

-- 1. 闸门从源级升到段级。检索回来的网页常常一半讲技术一半讲融资，
--    整篇一起进出太粗；段级判定让「这个来源其实没在说我们问的事」变得可见。
ALTER TABLE chunk ADD COLUMN on_topic INTEGER NOT NULL DEFAULT 1;
ALTER TABLE chunk ADD COLUMN topic_reason TEXT;
ALTER TABLE chunk ADD COLUMN gate_method TEXT;   -- rule / rule_weak / llm / fallback

-- 2. 来源性质由 extract 阶段判定并落库。原先它只活在 state 里，
--    进程退出就丢，write 环节只能靠主 agent 写进规约去转述。
ALTER TABLE source ADD COLUMN source_nature TEXT;      -- first_party/research/media/community/aggregator/unknown
ALTER TABLE source ADD COLUMN nature_reason TEXT;

-- 3. 段落概括由 extract 阶段写（原先是报告写完后在 finalize 写的，
--    只进检索索引，写作阶段用不上）。
--    chunk.summary 字段本来就存在，这里只是补一条迁移说明它的写入时机。
