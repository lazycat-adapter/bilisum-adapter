BEGIN TRANSACTION;
CREATE TABLE knowledge_conversations (
                    conversation_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    messages_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
INSERT INTO "knowledge_conversations" VALUES('0ffe96bf6aab4b3e9de4d77dd0120a85','access-token:shared','shared-key history','[]','2026-11-27T04:04:44.812943+00:00','2026-11-27T04:04:44.812943+00:00');
INSERT INTO "knowledge_conversations" VALUES('7cbb0a934bf94cca9f9ef51b00392547','oidc:alice','alice','[{"id": "alice0", "role": "user", "content": "alice-user", "reasoning": "", "sources": [], "tools": [], "status": null}, {"id": "alice1", "role": "assistant", "content": "alice-assistant", "reasoning": "", "sources": [], "tools": [], "status": null}]','2026-11-27T04:04:44.889984+00:00','2026-11-27T04:04:44.905375+00:00');
INSERT INTO "knowledge_conversations" VALUES('03a82fda970747728c4ce7d88720622f','oidc:bob','bob','[{"id": "bob0", "role": "user", "content": "bob-user", "reasoning": "", "sources": [], "tools": [], "status": null}, {"id": "bob1", "role": "assistant", "content": "bob-assistant", "reasoning": "", "sources": [], "tools": [], "status": null}]','2026-11-27T04:04:44.970671+00:00','2026-11-27T04:04:44.976456+00:00');
CREATE TABLE knowledge_index (
                    chunk_id TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    indexed_content TEXT NOT NULL,
                    index_type TEXT NOT NULL DEFAULT 'video_summary',
                    segment_order INTEGER,
                    anchor_label TEXT,
                    anchor_seconds REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(video_id) REFERENCES video_assets(video_id)
                );
CREATE TABLE library_preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
CREATE TABLE task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );
CREATE TABLE task_results (
                    task_id TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );
CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY,
                    video_id TEXT,
                    status TEXT NOT NULL,
                    task_input_json TEXT NOT NULL,
                    page_number INTEGER,
                    page_title TEXT,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
CREATE TABLE video_assets (
                    video_id TEXT PRIMARY KEY,
                    canonical_id TEXT NOT NULL UNIQUE,
                    platform TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    cover_url TEXT,
                    duration REAL,
                    page_catalog_json TEXT NOT NULL DEFAULT '[]',
                    latest_task_id TEXT,
                    latest_status TEXT,
                    latest_stage TEXT,
                    latest_error_message TEXT,
                    is_favorite INTEGER NOT NULL DEFAULT 0,
                    favorite_updated_at TEXT,
                    folder_id TEXT,
                    global_order REAL NOT NULL DEFAULT 0,
                    folder_order REAL NOT NULL DEFAULT 0,
                    global_pinned INTEGER NOT NULL DEFAULT 0,
                    folder_pinned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
CREATE TABLE video_folder_memberships (
                    video_id TEXT NOT NULL,
                    folder_id TEXT NOT NULL,
                    folder_order REAL NOT NULL DEFAULT 0,
                    folder_pinned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (video_id, folder_id),
                    FOREIGN KEY(video_id) REFERENCES video_assets(video_id),
                    FOREIGN KEY(folder_id) REFERENCES video_folders(folder_id)
                );
CREATE TABLE video_folders (
                    folder_id TEXT PRIMARY KEY,
                    parent_id TEXT,
                    name TEXT NOT NULL,
                    position REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
CREATE TABLE video_tags (
                    video_id TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (video_id, tag),
                    FOREIGN KEY(video_id) REFERENCES video_assets(video_id)
                );
CREATE INDEX idx_knowledge_conversations_owner_updated
                ON knowledge_conversations (owner_id, updated_at DESC)
                ;
COMMIT;
