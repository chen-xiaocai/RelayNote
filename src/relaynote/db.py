from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .model import AUTO_SLOT_STATES, Todo, TodoState, validate_transition


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta VALUES ('schema_version', '1');
CREATE TABLE IF NOT EXISTS todos (
 id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL, sort_key INTEGER NOT NULL,
 version INTEGER NOT NULL DEFAULT 0, latest_detail TEXT, workspace TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT
);
CREATE TABLE IF NOT EXISTS todo_additions (
 id INTEGER PRIMARY KEY, todo_id TEXT NOT NULL REFERENCES todos(id), body TEXT NOT NULL,
 created_at TEXT NOT NULL, delivered_at TEXT, ack_id TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS transitions (
 id INTEGER PRIMARY KEY, todo_id TEXT NOT NULL REFERENCES todos(id), from_state TEXT,
 to_state TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS codex_runs (
 run_id TEXT PRIMARY KEY, todo_id TEXT NOT NULL REFERENCES todos(id), thread_id TEXT,
 turn_id TEXT, pid INTEGER, endpoint TEXT, workspace TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
 id TEXT PRIMARY KEY, todo_id TEXT REFERENCES todos(id), run_id TEXT, kind TEXT NOT NULL,
 prompt TEXT NOT NULL, options_json TEXT NOT NULL, recommended INTEGER NOT NULL,
 state TEXT NOT NULL, created_at TEXT NOT NULL, shown_at TEXT, deadline_at TEXT,
 answered_at TEXT, answer_json TEXT
);
CREATE TABLE IF NOT EXISTS orchestrator_runs (
 run_id TEXT PRIMARY KEY, tick_at TEXT NOT NULL, status TEXT NOT NULL,
 compact_prompt TEXT, input_json TEXT NOT NULL, output_json TEXT, usage_json TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_calls (
 run_id TEXT NOT NULL, call_id TEXT NOT NULL, name TEXT NOT NULL, arguments_json TEXT NOT NULL,
 result_json TEXT, status TEXT NOT NULL, PRIMARY KEY(run_id, call_id)
);
CREATE TABLE IF NOT EXISTS auto_lease (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), todo_id TEXT UNIQUE REFERENCES todos(id),
 run_id TEXT UNIQUE, acquired_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS todos_order ON todos(state, sort_key);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ConflictError(RuntimeError):
    pass


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self._lock = threading.RLock()
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def create_todo(self, body: str) -> Todo:
        if not body.strip():
            raise ValueError("todo body cannot be empty")
        todo_id, now = str(uuid4()), _now()
        with self.transaction() as db:
            sort_key = db.execute("SELECT COALESCE(MAX(sort_key), 0) + 1024 FROM todos").fetchone()[0]
            db.execute("INSERT INTO todos(id,body,state,sort_key,created_at,updated_at) VALUES(?,?,?,?,?,?)", (todo_id, body, TodoState.PENDING, sort_key, now, now))
            db.execute("INSERT INTO transitions(todo_id,to_state,created_at) VALUES(?,?,?)", (todo_id, TodoState.PENDING, now))
        return self.get_todo(todo_id)

    def get_todo(self, todo_id: str) -> Todo:
        with self.connect() as db:
            row = db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        if row is None:
            raise KeyError(todo_id)
        return Todo(row["id"], row["body"], TodoState(row["state"]), row["sort_key"], row["version"], row["latest_detail"], row["workspace"])

    def list_todos(self, include_archived: bool = False) -> list[Todo]:
        query = "SELECT * FROM todos" + ("" if include_archived else " WHERE state != 'archived'") + " ORDER BY CASE WHEN state IN ('running','waiting_user','stopping') THEN 0 ELSE 1 END, sort_key"
        with self.connect() as db:
            rows = db.execute(query).fetchall()
        return [Todo(r["id"], r["body"], TodoState(r["state"]), r["sort_key"], r["version"], r["latest_detail"], r["workspace"]) for r in rows]

    def transition(self, todo_id: str, target: TodoState, expected_version: int, detail: str | None = None) -> Todo:
        now = _now()
        with self.transaction() as db:
            row = db.execute("SELECT state,version FROM todos WHERE id=?", (todo_id,)).fetchone()
            if row is None:
                raise KeyError(todo_id)
            if row["version"] != expected_version:
                raise ConflictError(f"todo {todo_id} version is {row['version']}, expected {expected_version}")
            current = TodoState(row["state"])
            validate_transition(current, target)
            if target in AUTO_SLOT_STATES:
                lease = db.execute("SELECT todo_id FROM auto_lease WHERE singleton=1").fetchone()
                if lease is not None and lease["todo_id"] != todo_id:
                    raise ConflictError(f"automatic slot held by {lease['todo_id']}")
            archived = now if target is TodoState.ARCHIVED else None
            changed = db.execute("UPDATE todos SET state=?, latest_detail=COALESCE(?,latest_detail), version=version+1, updated_at=?, archived_at=COALESCE(?,archived_at) WHERE id=? AND version=?", (target, detail, now, archived, todo_id, expected_version))
            if changed.rowcount != 1:
                raise ConflictError(todo_id)
            db.execute("INSERT INTO transitions(todo_id,from_state,to_state,detail,created_at) VALUES(?,?,?,?,?)", (todo_id, current, target, detail, now))
            if target not in AUTO_SLOT_STATES:
                db.execute("DELETE FROM auto_lease WHERE todo_id=?", (todo_id,))
        return self.get_todo(todo_id)

    def acquire_auto_lease(self, todo_id: str, run_id: str) -> None:
        with self.transaction() as db:
            try:
                db.execute("INSERT INTO auto_lease VALUES(1,?,?,?)", (todo_id, run_id, _now()))
            except sqlite3.IntegrityError as error:
                raise ConflictError("automatic slot already occupied") from error

    def release_auto_lease(self, todo_id: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM auto_lease WHERE todo_id=?", (todo_id,))

    def save_tool_result(self, run_id: str, call_id: str, name: str, arguments: Any, result: Any) -> bool:
        with self.transaction() as db:
            existing = db.execute("SELECT 1 FROM tool_calls WHERE run_id=? AND call_id=?", (run_id, call_id)).fetchone()
            if existing:
                return False
            db.execute("INSERT INTO tool_calls VALUES(?,?,?,?,?,?)", (run_id, call_id, name, json.dumps(arguments, ensure_ascii=False), json.dumps(result, ensure_ascii=False), "completed"))
            return True
