-- 0001 初始结构。这是数据库结构的**唯一来源**。
-- 之后所有结构变更都新增 migrations/00NN_*.sql，不要回头改这个文件。
-- 原因：曾经 schema.sql 与 migrations/ 两份描述同一个结构，会静默漂移。
-- 研究 Agent 数据库 schema
-- 约定：所有时间戳为 ISO8601 字符串（UTC）；所有日期字段可空，空值不参与时间衰减

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------- 会话 ----------
CREATE TABLE IF NOT EXISTS session (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    question      TEXT NOT NULL,
    outline_json  TEXT,                     -- agent 在 outline 环节自定的大纲
    probe_json    TEXT,                     -- 探针子图的产出。与大纲分开放，别互相覆盖
    status        TEXT NOT NULL DEFAULT 'running',  -- running/paused/done/archived/failed
    plan_json     TEXT,                     -- 已审批的研究计划
    cost_tokens   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    -- 只有显式归档的会话才进入历史检索，这是防污染第一道防线
    archived_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_status  ON session(status);
CREATE INDEX IF NOT EXISTS idx_session_archived ON session(archived_at);

-- ---------- 报告（与 session 一对一） ----------
CREATE TABLE IF NOT EXISTS report (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL UNIQUE REFERENCES session(id) ON DELETE CASCADE,
    markdown      TEXT,
    created_at    TEXT NOT NULL
);

-- ---------- 来源 ----------
CREATE TABLE IF NOT EXISTS source (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    url             TEXT NOT NULL,
    url_normalized  TEXT NOT NULL,           -- 规范化后用于判重
    domain          TEXT,
    title           TEXT,
    -- 信源类型标签：official/community/academic/blog/media/repost/unknown
    source_type     TEXT NOT NULL DEFAULT 'unknown',
    -- 来自哪个搜索引擎，native 检索时记录引擎名用于信源评级
    engine          TEXT,
    -- 内容自身时效，参与时间衰减；抽不到则 NULL 且不衰减
    source_date     TEXT,
    fetched_at      TEXT NOT NULL,
    content_hash    TEXT,                    -- 正文哈希，相同即同源转载
    char_count      INTEGER,
    http_status     INTEGER,
    -- 相关性闸门：与本次研究主题无关的来源留库但标记，不参与报告
    on_topic        INTEGER NOT NULL DEFAULT 1,
    topic_reason    TEXT,
    gate_method     TEXT,                   -- rule / llm / fallback
    -- 同源时指向被采纳的那条 source
    superseded_by   TEXT REFERENCES source(id)
);
CREATE INDEX IF NOT EXISTS idx_source_session ON source(session_id);
CREATE INDEX IF NOT EXISTS idx_source_normhash ON source(url_normalized);
CREATE INDEX IF NOT EXISTS idx_source_hash    ON source(content_hash);

-- ---------- 段落：最小储存单元 ----------
CREATE TABLE IF NOT EXISTS chunk (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    report_id     TEXT REFERENCES report(id) ON DELETE CASCADE,
    source_id     TEXT REFERENCES source(id) ON DELETE CASCADE,
    -- web=网络来源，upload=上传语料
    origin        TEXT NOT NULL,
    -- 一句话概括：召回确认与结果可读性都靠它
    summary       TEXT,
    content       TEXT NOT NULL,
    -- 递归切分时继承的原段标题，避免片段失去指代对象
    heading_path  TEXT,
    token_count   INTEGER,
    -- 概括句 + 正文拼接后向量化，float16 存储
    embedding     BLOB,
    -- pending 期间不参与检索（两阶段入库的中间态）
    status        TEXT NOT NULL DEFAULT 'pending',
    source_date   TEXT,
    ingested_at   TEXT NOT NULL,
    -- 显式取代指针，只在真打脸时写，不建边
    superseded_by TEXT REFERENCES chunk(id)
);
CREATE INDEX IF NOT EXISTS idx_chunk_status   ON chunk(status);
CREATE INDEX IF NOT EXISTS idx_chunk_session  ON chunk(session_id);
CREATE INDEX IF NOT EXISTS idx_chunk_source   ON chunk(source_id);

-- ---------- 主题：展示与归类用，不参与检索必需环节 ----------
CREATE TABLE IF NOT EXISTS topic (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    slug        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_topic (
    session_id  TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    topic_id    TEXT NOT NULL REFERENCES topic(id) ON DELETE CASCADE,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (session_id, topic_id)
);
CREATE INDEX IF NOT EXISTS idx_session_topic_topic ON session_topic(topic_id);

-- ---------- 冲突记录：矛盾结论并列，不静默覆盖 ----------
CREATE TABLE IF NOT EXISTS contradiction (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    topic         TEXT NOT NULL,
    claim_a       TEXT REFERENCES chunk(id),
    claim_b       TEXT REFERENCES chunk(id),
    resolution    TEXT,                      -- null 表示尚未裁决
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contradiction_session ON contradiction(session_id);

-- ---------- 调用记账：只记不告警 ----------
CREATE TABLE IF NOT EXISTS llm_call (
    id            TEXT PRIMARY KEY,
    session_id    TEXT REFERENCES session(id) ON DELETE CASCADE,
    stage         TEXT NOT NULL,             -- extract/dedupe/assess/write/summarize
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_call_session ON llm_call(session_id);

-- ---------- 上传文档 ----------
CREATE TABLE IF NOT EXISTS ingested_doc (
    id          TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    title       TEXT,
    doc_type    TEXT NOT NULL,               -- pdf/md
    source_date TEXT,
    parsed_at   TEXT,
    created_at  TEXT NOT NULL
);

-- ---------- 抓取缓存：正文与抓取时间，7 天有效期 ----------
CREATE TABLE IF NOT EXISTS fetch_cache (
    url_normalized  TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    content         TEXT NOT NULL,
    content_hash    TEXT,
    fetched_at      TEXT NOT NULL,
    http_status     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fetch_cache_time ON fetch_cache(fetched_at);

-- ---------- 全文索引：FTS5 内置，先粗筛再向量精排 ----------
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    summary,
    content,
    heading_path,
    content='chunk',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS chunk_ai AFTER INSERT ON chunk BEGIN
    INSERT INTO chunk_fts(rowid, summary, content, heading_path)
    VALUES (new.rowid, new.summary, new.content, new.heading_path);
END;
CREATE TRIGGER IF NOT EXISTS chunk_ad AFTER DELETE ON chunk BEGIN
    INSERT INTO chunk_fts(chunk_fts, rowid, summary, content, heading_path)
    VALUES ('delete', old.rowid, old.summary, old.content, old.heading_path);
END;
CREATE TRIGGER IF NOT EXISTS chunk_au AFTER UPDATE ON chunk BEGIN
    INSERT INTO chunk_fts(chunk_fts, rowid, summary, content, heading_path)
    VALUES ('delete', old.rowid, old.summary, old.content, old.heading_path);
    INSERT INTO chunk_fts(rowid, summary, content, heading_path)
    VALUES (new.rowid, new.summary, new.content, new.heading_path);
END;
