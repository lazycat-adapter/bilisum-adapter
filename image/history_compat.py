"""Migrate Lazycat 1.19.2 conversation history at repository startup."""

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

logger = logging.getLogger(__name__)


def _columns(db):
    return {row[1] for row in db.execute("PRAGMA table_info(knowledge_conversations)")}


def has_legacy_history(db_path: Path) -> bool:
    """Identify upgraded installations even if no account has created a conversation."""
    path = Path(db_path)
    if not path.exists():
        return False
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return {"owner_id", "messages_json"} <= _columns(db)


def _backup_database(cursor):
    # BEGIN IMMEDIATE excludes competing writers. A second connection reads the
    # last committed database, including WAL pages, before upstream runs its DDL.
    filename = cursor.execute("PRAGMA database_list").fetchone()[2]
    if not filename:  # SQLite in-memory databases have no persistent volume.
        return
    path = Path(filename)
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as temporary:
        staging = Path(temporary.name)
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(staging)) as target:
                source.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Conversation migration backup failed SQLite quick_check")
        with staging.open("rb") as snapshot:
            digest = hashlib.file_digest(snapshot, "sha256").hexdigest()
            os.fsync(snapshot.fileno())
        backup = path.with_name(f"{path.name}.pre-adapter-{digest[:20]}.sqlite3")
        # Retrying the same failed migration keeps a single verified snapshot.
        staging.replace(backup)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        logger.info("Database backup before history migration: %s", backup)
    finally:
        staging.unlink(missing_ok=True)


def prepare_history_migration(cursor: sqlite3.Cursor) -> bool:
    """Called inside upstream sqlite_cursor before the first initialization statement."""
    cursor.execute("BEGIN IMMEDIATE")
    columns = _columns(cursor)
    if not columns:
        return False
    if "messages_json" in columns:
        if "owner_id" not in columns:
            raise ValueError("Legacy conversation table lacks owner_id")
        completed = cursor.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='lzc_history_migration_v1'"
        ).fetchone()
        if completed:
            return False
        _backup_database(cursor)
        if "last_message_at" not in columns:
            cursor.execute("ALTER TABLE knowledge_conversations ADD COLUMN last_message_at TEXT")
        return True
    if "owner_id" not in columns:
        # Native upstream records have no evidence of ownership. Keep them
        # available to the repository without assigning them to a signed-in actor.
        _backup_database(cursor)
        cursor.execute("ALTER TABLE knowledge_conversations ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
    return False


def migrate_legacy_messages(cursor: sqlite3.Cursor):
    from .schemas import KnowledgeMessageResponse

    # Iterate with a separate cursor to avoid loading all accounts' histories into memory.
    with closing(cursor.connection.cursor()) as reader:
        rows = reader.execute(
            "SELECT conversation_id, messages_json, updated_at FROM knowledge_conversations ORDER BY conversation_id"
        )
        for conversation_id, payload, updated in rows:
            try:
                messages = json.loads(payload)
                if not isinstance(messages, list):
                    raise ValueError("expected a message list")
                for sequence, old in enumerate(messages, 1):
                    if not isinstance(old, dict):
                        raise ValueError("expected a message object")
                    # Old clients chose their own IDs independently for each chat.
                    message_id = old.get("id")
                    if not message_id or cursor.execute(
                        "SELECT 1 FROM knowledge_messages WHERE message_id = ?", (message_id,)
                    ).fetchone():
                        message_id = uuid4().hex
                    # Legacy messages had no individual timestamps. Use the save time
                    # for display and the original array order for sequencing.
                    message = KnowledgeMessageResponse(
                        message_id=message_id, conversation_id=conversation_id, sequence=sequence,
                        role=old["role"], content=old["content"], reasoning=old.get("reasoning", ""),
                        status=old.get("status") or "completed", sources=old.get("sources", []),
                        tools=old.get("tools", []), created_at=updated, updated_at=updated,
                    )
                    cursor.execute(
                        """INSERT INTO knowledge_messages
                        (message_id, conversation_id, sequence, role, content, reasoning,
                         status, sources_json, tools_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (message.message_id, conversation_id, sequence, message.role, message.content,
                         message.reasoning, message.status, json.dumps(old.get("sources", []), ensure_ascii=False),
                         json.dumps(message.tools, ensure_ascii=False), updated, updated),
                    )
            except (ValueError, KeyError, TypeError, sqlite3.IntegrityError, sqlite3.ProgrammingError):
                # Pydantic errors can include private message text. Report only the affected ID.
                raise ValueError(f"Cannot migrate legacy messages in conversation {conversation_id}") from None
            if messages:
                cursor.execute(
                    "UPDATE knowledge_conversations SET last_message_at = ? WHERE conversation_id = ?",
                    (updated, conversation_id),
                )
    # This table and the inserts above commit together with the upstream schema changes.
    cursor.execute("CREATE TABLE lzc_history_migration_v1 (completed_at TEXT NOT NULL)")
    cursor.execute("INSERT INTO lzc_history_migration_v1 VALUES (CURRENT_TIMESTAMP)")
