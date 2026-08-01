from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from relaynote.db import ConflictError, Store
from relaynote.model import TodoState


def test_notes_are_immutable_idempotent_and_ordered(tmp_path: Path) -> None:
    store = Store(tmp_path / "relaynote.sqlite3")
    todo = store.create_todo("第一行\n完整正文")
    first = store.append_note(todo.id, "追加一", ack_id="append-1")
    duplicate = store.append_note(todo.id, "不会覆盖", ack_id="append-1")
    second = store.append_note(todo.id, "追加二")
    assert first.id == duplicate.id
    assert [note.body for note in store.list_notes(todo.id)] == ["第一行\n完整正文", "追加一", "追加二"]
    store.mark_notes_delivered([first.id, second.id], "turn-1")
    assert store.pending_additions(todo.id) == []


def test_bind_run_is_atomic_and_completion_releases_slot(tmp_path: Path) -> None:
    store = Store(tmp_path / "relaynote.sqlite3")
    first = store.create_todo("first")
    second = store.create_todo("second")
    run = store.bind_run(first.id, first.version, "run-1", tmp_path)
    assert run.todo_id == first.id
    assert store.automatic_lease() == (first.id, "run-1")
    with pytest.raises(ConflictError):
        store.bind_run(second.id, second.version, "run-2", tmp_path)
    running = store.get_todo(first.id)
    store.transition(first.id, TodoState.COMPLETED, running.version, "全部结果")
    assert store.automatic_lease() is None
    assert store.timeline(first.id)[-1]["body"] == "全部结果"


def test_reorder_excludes_pinned_automatic_task(tmp_path: Path) -> None:
    store = Store(tmp_path / "relaynote.sqlite3")
    first, second, third = (store.create_todo(value) for value in ("first", "second", "third"))
    store.bind_run(first.id, first.version, "run", tmp_path)
    store.reorder([third.id, second.id])
    assert [todo.id for todo in store.list_todos()] == [first.id, third.id, second.id]


def test_v1_additions_are_migrated_without_duplication(tmp_path: Path) -> None:
    path = tmp_path / "relaynote.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE todos(id TEXT PRIMARY KEY,body TEXT,state TEXT,sort_key INTEGER,version INTEGER,latest_detail TEXT,workspace TEXT,created_at TEXT,updated_at TEXT,archived_at TEXT);
        CREATE TABLE todo_additions(id INTEGER PRIMARY KEY,todo_id TEXT,body TEXT,created_at TEXT,delivered_at TEXT,ack_id TEXT UNIQUE);
        INSERT INTO todos VALUES('t','todo','pending',1024,0,NULL,NULL,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',NULL);
        INSERT INTO todo_additions VALUES(1,'t','legacy','2026-01-01T00:00:01+00:00',NULL,'legacy-1');
        """
    )
    db.commit()
    db.close()
    store = Store(path)
    assert [(note.kind, note.body) for note in store.list_notes("t")] == [("original", "todo"), ("addition", "legacy")]
    Store(path)
    assert [(note.kind, note.body) for note in store.list_notes("t")] == [("original", "todo"), ("addition", "legacy")]
