-- ---------- llm_call 记录失败调用（2026-10-13）----------
--
-- 为什么加：`_record()` 的三个调用点全在 `return` 之前的**成功路径**上，
-- 抛异常时根本不会执行。所以 504 / 429 / 解析失败这些情况，
-- `llm_call` 里**查不到任何痕迹**——真出问题时无从下手，只能靠猜。
--
-- `status` 默认 'ok' 而不是 NULL：现有历史行**确实全是成功的调用**
-- （失败的从来没被记过），所以给 'ok' 是对既有事实的还原，不是编造。
-- 迁移后 `COUNT(*) WHERE status='error'` 就直接是失败次数。
--
-- `error` 存 `类型: 消息`，限长 500 字符：平台报错偶尔带很长的
-- HTML 错误页，不截断会把表撑大，而前 500 字符足够定位问题。

ALTER TABLE llm_call ADD COLUMN status TEXT NOT NULL DEFAULT 'ok';
ALTER TABLE llm_call ADD COLUMN error TEXT;

-- 查失败：SELECT * FROM llm_call WHERE status='error' ORDER BY created_at DESC
CREATE INDEX IF NOT EXISTS idx_llm_call_status ON llm_call(status);
