import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_sum_service.repository import SqliteTaskRepository


# From the released Lazycat 1.19.2 service repository.initialize().
OLD_DDL = """
CREATE TABLE knowledge_conversations (
    conversation_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    title TEXT NOT NULL,
    messages_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_knowledge_conversations_owner_updated
ON knowledge_conversations (owner_id, updated_at DESC);
"""
# From the upstream 1.21.3 service.
NATIVE_DDL = """
CREATE TABLE knowledge_conversations (
    conversation_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_message_at TEXT
);
"""
DATE = "2026-06-27T12:00:00+00:00"
SOURCE = {
    "video_id": "v1", "title": "片段", "relevance_score": 0.73, "timestamp": "01:37",
    "video_title": "讲座", "page_title": "第二部分", "page_number": 2,
}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "video_sum.db"
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.repo = SqliteTaskRepository(self.db)

    def add_old(self, cid, actor, messages):
        self.db.execute(
            "INSERT INTO knowledge_conversations VALUES (?, ?, ?, ?, ?, ?)",
            (cid, actor, "我的记录", json.dumps(messages, ensure_ascii=False), DATE, DATE),
        )
        self.db.commit()

    def backups(self):
        return list(self.path.parent.glob("video_sum.db.pre-adapter-*.sqlite3"))

    def test_migrate_messages_ownership_duplicate_ids_and_restore_point(self):
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(OLD_DDL)
        answer = {
            "id": "local-id", "role": "assistant", "content": "回答", "reasoning": "分析",
            "status": "error", "sources": [SOURCE], "tools": [{"id": "search", "label": "检索"}],
        }
        question = {"id": "question", "role": "user", "content": "提问", "status": None}
        self.add_old("c1", "oidc:alice", [question, answer])
        self.add_old("c2", "oidc:bob", [dict(answer, content="Bob")])
        self.add_old("c3", "access-token:shared", [])

        self.repo.initialize()
        self.repo.initialize()

        alice = self.repo.list_knowledge_messages("c1")
        bob = self.repo.list_knowledge_messages("c2")
        self.assertEqual([m["sequence"] for m in alice], [1, 2])
        self.assertEqual([m["content"] for m in alice], ["提问", "回答"])
        self.assertEqual([m["status"] for m in alice], ["completed", "error"])
        self.assertEqual(alice[1]["reasoning"], answer["reasoning"])
        self.assertEqual(alice[1]["sources"], [SOURCE])
        self.assertEqual(alice[1]["tools"], answer["tools"])
        self.assertEqual(alice[1]["message_id"], "local-id")
        self.assertEqual(bob[0]["content"], "Bob")
        self.assertNotEqual(bob[0]["message_id"], alice[1]["message_id"])
        self.assertEqual(self.repo.get_knowledge_conversation("c1")["last_message_at"].isoformat(), DATE)
        self.assertIsNone(self.repo.get_knowledge_conversation("c3")["last_message_at"])
        self.assertEqual(
            [c["conversation_id"] for c in self.repo.list_knowledge_conversations(owner_id="oidc:bob")],
            ["c2"],
        )
        self.assertEqual(len(self.backups()), 1)
        with closing(sqlite3.connect(self.backups()[0])) as snapshot:
            self.assertEqual(snapshot.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            payload = snapshot.execute("SELECT messages_json FROM knowledge_conversations WHERE conversation_id='c1'").fetchone()[0]
            self.assertEqual(json.loads(payload), [question, answer])
            self.assertFalse(snapshot.execute("SELECT 1 FROM sqlite_schema WHERE name='knowledge_messages'").fetchone())

    def test_invalid_old_json_rolls_back_all_changes(self):
        self.db.executescript(OLD_DDL)
        self.add_old("a", "oidc:a", [{"id": "m", "role": "user", "content": "original"}])
        self.add_old("b", "oidc:b", [])
        self.db.execute("UPDATE knowledge_conversations SET messages_json='broken' WHERE conversation_id='b'")
        self.db.commit()
        before = list(self.db.iterdump())
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "conversation b"):
                self.repo.initialize()
            self.assertEqual(list(self.db.iterdump()), before)
            self.assertEqual(len(self.backups()), 1)

    def test_legacy_schema_signal_survives_empty_install_and_upgrade(self):
        from video_sum_service.history_compat import has_legacy_history

        self.assertFalse(has_legacy_history(self.path))
        self.assertFalse(has_legacy_history(self.path.parent / "missing"))
        self.db.executescript(OLD_DDL)
        self.assertTrue(has_legacy_history(self.path))
        self.repo.initialize()
        self.assertTrue(has_legacy_history(self.path))

    def test_native_unowned_rows_are_retained_and_private(self):
        self.db.executescript(NATIVE_DDL)
        self.db.execute("INSERT INTO knowledge_conversations VALUES ('native','original',?,?,NULL)", (DATE, DATE))
        self.db.commit()
        self.repo.initialize()
        self.assertEqual(self.repo.get_knowledge_conversation("native")["title"], "original")
        from video_sum_service.history_compat import has_legacy_history

        self.assertFalse(has_legacy_history(self.path))
        self.assertEqual(self.repo.list_knowledge_conversations(owner_id="oidc:a"), [])
        self.assertEqual(len(self.backups()), 1)

    def test_http_owner_scope_in_list_create_and_all_id_routes(self):
        from video_sum_service.routers import knowledge

        self.repo.initialize()
        app = FastAPI()
        app.state.task_repository = self.repo
        app.include_router(knowledge.router)
        # Select a trusted actor outside the request payload. Auth itself has separate tests.
        with patch.object(knowledge, "actor_for_request", lambda _request: app.state.actor), TestClient(app) as client:
            api = "/api/v1/knowledge"
            conv = api + "/conversations"
            app.state.actor = "oidc:alice"
            r = client.post(conv, json={"title": "Alice", "owner_id": "oidc:bob"})
            self.assertEqual(r.status_code, 201)
            cid = r.json()["conversation_id"]
            target = f"{conv}/{cid}"
            message = self.repo.create_knowledge_message(cid, "assistant", "private answer")
            job = self.repo.create_knowledge_job(cid, message["message_id"], "multi_video_summary", "completed", "query")
            job_url = f"{api}/jobs/{job['job_id']}"
            self.assertEqual(client.get(target).status_code, 200)
            self.assertEqual(client.get(target + "/messages").json()["messages"][0]["content"], "private answer")
            self.assertEqual(client.get(job_url).status_code, 200)
            app.state.actor = "oidc:bob"
            own = client.post(conv, json={"title": "Bob"}).json()["conversation_id"]
            for method, url, body in (
                ("GET", target, None), ("PATCH", target, {"title": "change"}), ("DELETE", target, None),
                ("GET", target + "/messages", None), ("POST", target + "/ask/stream", {"query": "hello"}),
                ("GET", job_url, None), ("POST", job_url + "/reaggregate", None),
            ):
                with self.subTest(method=method, url=url):
                    self.assertEqual(client.request(method, url, json=body).status_code, 404)
            self.assertEqual([c["conversation_id"] for c in client.get(conv).json()], [own])
            app.state.actor = "oidc:alice"
            self.assertEqual([c["conversation_id"] for c in client.get(conv + "?limit=1").json()], [cid])
            self.assertEqual(client.patch(target, json={"title": "Alice 2"}).json()["title"], "Alice 2")
            self.assertEqual(client.get(job_url).json()["status"], "completed")

            submitted = []
            app.state.knowledge_job_coordinator = SimpleNamespace(submit=submitted.append)
            self.assertEqual(client.post(job_url + "/reaggregate").json()["status"], "running")
            self.assertEqual(submitted, [job["job_id"]])

            class LocalRag:
                def ask_stream(self, *args, **kwargs):
                    yield "text_delta", {"delta": "本地回复"}
                    yield "done", {"answer": "本地回复", "sources": []}

            with (
                patch.object(knowledge, "_knowledge_enabled", return_value=True),
                patch.object(knowledge, "_get_services", return_value=(None, None, LocalRag())),
            ):
                result = client.post(target + "/ask/stream", json={"query": "问题"})
                self.assertEqual(result.status_code, 200)
                self.assertIn("event: done", result.text)
            self.assertEqual(client.get(target + "/messages").json()["messages"][-1]["content"], "本地回复")
            self.assertEqual(client.delete(target).status_code, 200)
