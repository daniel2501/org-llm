-- [[file:../../org/20260425230731-org_llm.org::*Schema (SQL — canonical definition)][Schema (SQL — canonical definition):1]]
-- org-llm database schema

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    path        TEXT    NOT NULL UNIQUE,
    indexed_at  TEXT    NOT NULL,           -- ISO-8601
    node_count  INTEGER NOT NULL DEFAULT 0,
    mtime       REAL    NOT NULL            -- unix timestamp
);

CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    node_id     TEXT    UNIQUE,             -- org-roam :ID: property, nullable
    title       TEXT    NOT NULL,
    body        TEXT    NOT NULL DEFAULT '',
    tags        TEXT    NOT NULL DEFAULT '', -- space-separated
    mtime       REAL    NOT NULL,
    embedding   BLOB                        -- sqlite-vec float32[]
);

CREATE INDEX IF NOT EXISTS idx_nodes_file   ON nodes(file_id);
CREATE INDEX IF NOT EXISTS idx_nodes_title  ON nodes(title);
CREATE INDEX IF NOT EXISTS idx_nodes_tags   ON nodes(tags);

CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY,
    timestamp   TEXT    NOT NULL,
    command     TEXT    NOT NULL,
    query       TEXT    NOT NULL,
    response    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

INSERT OR IGNORE INTO config(key, value) VALUES
    ('org_dir',        '~/org'),
    ('ollama_url',     'http://localhost:11434'),
    ('embed_model',    'nomic-embed-text'),   -- semantic search embeddings
    ('chat_model',     'llama3.3'),           -- ask / general Q&A
    ('code_model',     'qwen2.5-coder'),      -- code generation
    ('reason_model',   'deepseek-r1'),        -- planning, complex reasoning
    ('fast_model',     'phi4'),               -- tagging, quick classification
    ('instruct_model', 'mistral-nemo'),       -- capture, instruction following
    ('text_model',     'gemma3'),             -- summarization, text analysis
    ('embed_dim',      '768'),
    ('db_version',     '1');
-- Schema (SQL — canonical definition):1 ends here
