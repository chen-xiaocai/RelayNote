"""SQLite 持久化层：待办状态机、运行记录、问题、调度上下文与工具幂等。"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .model import (
    AUTO_SLOT_STATES,
    CodexRun,
    Todo,
    TodoNote,
    TodoState,
    validate_transition,
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta VALUES ('schema_version', '2');
CREATE TABLE IF NOT EXISTS todos (
 id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL, sort_key INTEGER NOT NULL,
 version INTEGER NOT NULL DEFAULT 0, latest_detail TEXT, workspace TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT
);
CREATE TABLE IF NOT EXISTS todo_additions (
 id INTEGER PRIMARY KEY, todo_id TEXT NOT NULL REFERENCES todos(id), body TEXT NOT NULL,
 created_at TEXT NOT NULL, delivered_at TEXT, ack_id TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS todo_notes (
 id INTEGER PRIMARY KEY, todo_id TEXT NOT NULL REFERENCES todos(id), kind TEXT NOT NULL,
 body TEXT NOT NULL, created_at TEXT NOT NULL, delivered_at TEXT, ack_id TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS transitions (
 id INTEGER PRIMARY KEY, todo_id TEXT NOT NULL REFERENCES todos(id), from_state TEXT,
 to_state TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspaces (
 todo_id TEXT PRIMARY KEY REFERENCES todos(id), path TEXT NOT NULL, project_root TEXT,
 branch TEXT, is_worktree INTEGER NOT NULL, dirty_policy TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS codex_runs (
 run_id TEXT PRIMARY KEY, todo_id TEXT NOT NULL REFERENCES todos(id), thread_id TEXT,
 turn_id TEXT, pid INTEGER, endpoint TEXT, workspace TEXT NOT NULL, status TEXT NOT NULL,
 final_message TEXT, clean_stop INTEGER, error_json TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS codex_events (
 id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES codex_runs(run_id),
 method TEXT NOT NULL, raw_json TEXT NOT NULL, created_at TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS orchestrator_items (
 id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, item_json TEXT NOT NULL,
 token_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orchestrator_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), compact_prompt TEXT,
 compacted_through INTEGER NOT NULL DEFAULT 0, token_count INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_calls (
 run_id TEXT NOT NULL, call_id TEXT NOT NULL, name TEXT NOT NULL, arguments_json TEXT NOT NULL,
 result_json TEXT, status TEXT NOT NULL, PRIMARY KEY(run_id, call_id)
);
CREATE TABLE IF NOT EXISTS auto_lease (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), todo_id TEXT UNIQUE REFERENCES todos(id),
 run_id TEXT UNIQUE, acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS todos_order ON todos(state, sort_key);
CREATE INDEX IF NOT EXISTS notes_todo ON todo_notes(todo_id, id);
CREATE INDEX IF NOT EXISTS events_run ON codex_events(run_id, id);
CREATE INDEX IF NOT EXISTS questions_state ON questions(state, created_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ConflictError(RuntimeError):
    """并发写入或自动槽占用导致的乐观锁冲突。"""



class Store:
    """数据库访问入口，统一管理连接、事务、迁移与业务读写。"""

    def __init__(self, path: Path) -> None:
        """初始化数据库目录，执行建表语句和旧版本迁移。"""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self._lock = threading.RLock()
        with self.connect() as db:
            db.executescript(SCHEMA)
            self._migrate(db)

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """补齐 codex_runs 新列，并把旧版 todo_additions 合并进 todo_notes。"""
        columns = {row[1] for row in db.execute("PRAGMA table_info(codex_runs)")}
        for name, declaration in (
            ("final_message", "TEXT"),
            ("clean_stop", "INTEGER"),
            ("error_json", "TEXT"),
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE codex_runs ADD COLUMN {name} {declaration}")
        db.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        # 老版本待办追加表统一迁移到 todo_notes，迁移过程保持幂等。
        db.execute(
            "INSERT INTO todo_notes(todo_id,kind,body,created_at,delivered_at,ack_id) "
            "SELECT a.todo_id,'addition',a.body,a.created_at,a.delivered_at,a.ack_id FROM todo_additions a "
            "WHERE NOT EXISTS (SELECT 1 FROM todo_notes n WHERE n.todo_id=a.todo_id AND n.kind='addition' AND n.created_at=a.created_at AND n.body=a.body)"
        )
        db.execute(
            "INSERT INTO todo_notes(todo_id,kind,body,created_at) "
            "SELECT t.id,'original',t.body,t.created_at FROM todos t "
            "WHERE NOT EXISTS (SELECT 1 FROM todo_notes n WHERE n.todo_id=t.id AND n.kind='original')"
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """打开一个带行映射和 WAL 参数的新连接。"""
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """提供立即开始、成功提交、异常回滚的事务上下文。"""
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    @staticmethod
    def _todo(row: sqlite3.Row) -> Todo:
        """把数据库行转换为 Todo 数据对象。"""
        return Todo(
            row["id"], row["body"], TodoState(row["state"]), row["sort_key"],
            row["version"], row["latest_detail"], row["workspace"],
            _date(row["created_at"]), _date(row["updated_at"]),
        )

    def create_todo(self, body: str) -> Todo:
        """创建待办，写入初始状态、转换记录和原始正文笔记。"""
        if not body.strip():
            raise ValueError("todo body cannot be empty")
        todo_id, now = str(uuid4()), _now()
        with self.transaction() as db:
            sort_key = db.execute("SELECT COALESCE(MAX(sort_key), 0) + 1024 FROM todos").fetchone()[0]
            db.execute(
                "INSERT INTO todos(id,body,state,sort_key,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (todo_id, body, TodoState.PENDING, sort_key, now, now),
            )
            db.execute(
                "INSERT INTO transitions(todo_id,to_state,created_at) VALUES(?,?,?)",
                (todo_id, TodoState.PENDING, now),
            )
            db.execute(
                "INSERT INTO todo_notes(todo_id,kind,body,created_at) VALUES(?,?,?,?)",
                (todo_id, "original", body, now),
            )
        return self.get_todo(todo_id)

    def get_todo(self, todo_id: str) -> Todo:
        """按 ID 读取待办，不存在时抛出 KeyError。"""
        with self.connect() as db:
            row = db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        if row is None:
            raise KeyError(todo_id)
        return self._todo(row)

    def list_todos(self, include_archived: bool = False) -> list[Todo]:
        """按自动任务优先、再按排序键返回待办列表。"""
        where = "" if include_archived else " WHERE state != 'archived'"
        query = (
            "SELECT * FROM todos" + where
            + " ORDER BY CASE WHEN state IN ('running','waiting_user','stopping') THEN 0 ELSE 1 END, sort_key, created_at"
        )
        with self.connect() as db:
            rows = db.execute(query).fetchall()
        return [self._todo(row) for row in rows]

    def reorder(self, ordered_ids: list[str]) -> None:
        """重新排列可移动待办；自动槽中的任务不允许参与排序。"""
        if len(ordered_ids) != len(set(ordered_ids)):
            raise ValueError("todo order contains duplicate ids")
        with self.transaction() as db:
            rows = db.execute(
                "SELECT id,state FROM todos WHERE archived_at IS NULL"
            ).fetchall()
            movable = {row["id"] for row in rows if TodoState(row["state"]) not in AUTO_SLOT_STATES}
            supplied = [todo_id for todo_id in ordered_ids if todo_id in movable]
            if set(supplied) != movable:
                raise ValueError("todo order must include every movable todo exactly once")
            now = _now()
            for index, todo_id in enumerate(supplied, 1):
                db.execute(
                    "UPDATE todos SET sort_key=?,version=version+1,updated_at=? WHERE id=?",
                    (index * 1024, now, todo_id),
                )

    def transition(
        self,
        todo_id: str,
        target: TodoState,
        expected_version: int,
        detail: str | None = None,
    ) -> Todo:
        """在版本一致的乐观锁条件下转换待办状态。"""
        now = _now()
        with self.transaction() as db:
            self._transition(db, todo_id, target, expected_version, detail, now)
        return self.get_todo(todo_id)

    def _transition(
        self,
        db: sqlite3.Connection,
        todo_id: str,
        target: TodoState,
        expected_version: int,
        detail: str | None,
        now: str,
    ) -> None:
        """事务内执行状态转换，包括自动槽校验和转换历史写入。"""
        row = db.execute("SELECT state,version FROM todos WHERE id=?", (todo_id,)).fetchone()
        if row is None:
            raise KeyError(todo_id)
        if row["version"] != expected_version:
            raise ConflictError(f"todo {todo_id} version is {row['version']}, expected {expected_version}")
        current = TodoState(row["state"])
        validate_transition(current, target)
        if target in AUTO_SLOT_STATES:
            # 全局只能有一个自动任务处于运行/等待/停止状态。
            lease = db.execute("SELECT todo_id FROM auto_lease WHERE singleton=1").fetchone()
            if lease is not None and lease["todo_id"] != todo_id:
                raise ConflictError(f"automatic slot held by {lease['todo_id']}")
        archived = now if target is TodoState.ARCHIVED else None
        changed = db.execute(
            "UPDATE todos SET state=?,latest_detail=COALESCE(?,latest_detail),version=version+1,"
            "updated_at=?,archived_at=COALESCE(?,archived_at) WHERE id=? AND version=?",
            (target, detail, now, archived, todo_id, expected_version),
        )
        if changed.rowcount != 1:
            raise ConflictError(todo_id)
        db.execute(
            "INSERT INTO transitions(todo_id,from_state,to_state,detail,created_at) VALUES(?,?,?,?,?)",
            (todo_id, current, target, detail, now),
        )
        if detail:
            db.execute(
                "INSERT INTO todo_notes(todo_id,kind,body,created_at) VALUES(?,?,?,?)",
                (todo_id, "transition", detail, now),
            )
        if target not in AUTO_SLOT_STATES:
            # 离开自动槽后必须释放全局 lease，让其他待办可以接手。
            db.execute("DELETE FROM auto_lease WHERE todo_id=?", (todo_id,))

    def acquire_auto_lease(self, todo_id: str, run_id: str) -> None:
        """原子占用全局自动任务槽，已占用时抛出 ConflictError。"""
        with self.transaction() as db:
            try:
                db.execute("INSERT INTO auto_lease VALUES(1,?,?,?)", (todo_id, run_id, _now()))
            except sqlite3.IntegrityError as error:
                raise ConflictError("automatic slot already occupied") from error

    def release_auto_lease(self, todo_id: str) -> None:
        """释放指定待办占用的自动任务槽。"""
        with self.transaction() as db:
            db.execute("DELETE FROM auto_lease WHERE todo_id=?", (todo_id,))

    def automatic_lease(self) -> tuple[str, str] | None:
        """返回当前自动槽的 todo_id 和 run_id。"""
        with self.connect() as db:
            row = db.execute("SELECT todo_id,run_id FROM auto_lease WHERE singleton=1").fetchone()
        return (row["todo_id"], row["run_id"]) if row else None

    def bind_run(self, todo_id: str, expected_version: int, run_id: str, workspace: Path) -> CodexRun:
        """原子绑定运行记录、占用自动槽并把待办切换为运行中。"""
        now = _now()
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM auto_lease WHERE singleton=1").fetchone():
                raise ConflictError("automatic slot already occupied")
            db.execute("INSERT INTO auto_lease VALUES(1,?,?,?)", (todo_id, run_id, now))
            self._transition(db, todo_id, TodoState.RUNNING, expected_version, None, now)
            db.execute(
                "UPDATE todos SET workspace=? WHERE id=?",
                (str(workspace), todo_id),
            )
            db.execute(
                "INSERT INTO codex_runs(run_id,todo_id,workspace,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (run_id, todo_id, str(workspace), "starting", now, now),
            )
        return self.get_run(run_id)

    def resume_existing_run(self, todo_id: str, expected_version: int, run_id: str) -> Todo:
        """恢复已有 thread 时重新占用自动槽，并把待办切回运行中。"""
        now = _now()
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM auto_lease WHERE singleton=1").fetchone():
                raise ConflictError("automatic slot already occupied")
            db.execute("INSERT INTO auto_lease VALUES(1,?,?,?)", (todo_id, run_id, now))
            self._transition(db, todo_id, TodoState.RUNNING, expected_version, None, now)
            db.execute("UPDATE codex_runs SET status='running',updated_at=? WHERE run_id=?", (now, run_id))
        return self.get_todo(todo_id)

    def prioritize(self, todo_id: str) -> None:
        """把待办移到可移动列表最前面。"""
        with self.transaction() as db:
            minimum = db.execute("SELECT COALESCE(MIN(sort_key),1024) FROM todos WHERE archived_at IS NULL").fetchone()[0]
            db.execute("UPDATE todos SET sort_key=?,version=version+1,updated_at=? WHERE id=?", (minimum - 1024, _now(), todo_id))

    def create_taken_over_run(self, todo_id: str, workspace: Path, run_id: str | None = None) -> CodexRun:
        """为人工接管创建 claimed 运行记录，但不占用自动槽。"""
        run_id, now = run_id or str(uuid4()), _now()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO codex_runs(run_id,todo_id,workspace,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (run_id, todo_id, str(workspace), "claimed", now, now),
            )
            db.execute("UPDATE todos SET workspace=? WHERE id=?", (str(workspace), todo_id))
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> CodexRun:
        """按 run_id 读取运行记录。"""
        with self.connect() as db:
            row = db.execute("SELECT * FROM codex_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run(row)

    def latest_run(self, todo_id: str) -> CodexRun | None:
        """返回待办最近一次运行记录。"""
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM codex_runs WHERE todo_id=? ORDER BY created_at DESC LIMIT 1",
                (todo_id,),
            ).fetchone()
        return self._run(row) if row else None

    @staticmethod
    def _run(row: sqlite3.Row) -> CodexRun:
        """把数据库行转换为 CodexRun 数据对象。"""
        return CodexRun(
            row["run_id"], row["todo_id"], row["thread_id"], row["turn_id"],
            row["pid"], row["endpoint"], row["workspace"], row["status"], row["final_message"],
        )

    def update_run(self, run_id: str, **fields: Any) -> CodexRun:
        """更新运行记录中允许修改的字段。"""
        allowed = {"thread_id", "turn_id", "pid", "endpoint", "status", "final_message", "clean_stop", "error_json"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("invalid codex run update")
        values = {key: (_json(value) if key == "error_json" and value is not None else value) for key, value in fields.items()}
        values["updated_at"] = _now()
        clause = ",".join(f"{key}=?" for key in values)
        with self.transaction() as db:
            changed = db.execute(
                f"UPDATE codex_runs SET {clause} WHERE run_id=?",
                (*values.values(), run_id),
            )
            if changed.rowcount != 1:
                raise KeyError(run_id)
        return self.get_run(run_id)

    def append_note(self, todo_id: str, body: str, kind: str = "addition", ack_id: str | None = None) -> TodoNote:
        """追加不可变笔记；相同 ack_id 重复写入时返回原记录。"""
        if not body.strip():
            raise ValueError("note body cannot be empty")
        now = _now()
        with self.transaction() as db:
            try:
                cursor = db.execute(
                    "INSERT INTO todo_notes(todo_id,kind,body,created_at,ack_id) VALUES(?,?,?,?,?)",
                    (todo_id, kind, body, now, ack_id),
                )
            except sqlite3.IntegrityError:
                if ack_id is None:
                    raise
                row = db.execute("SELECT id FROM todo_notes WHERE ack_id=?", (ack_id,)).fetchone()
                return self.get_note(row["id"])
            db.execute("UPDATE todos SET version=version+1,updated_at=? WHERE id=?", (now, todo_id))
            note_id = cursor.lastrowid
        return self.get_note(note_id)

    def get_note(self, note_id: int) -> TodoNote:
        """按笔记 ID 读取记录。"""
        with self.connect() as db:
            row = db.execute("SELECT * FROM todo_notes WHERE id=?", (note_id,)).fetchone()
        if row is None:
            raise KeyError(note_id)
        return TodoNote(row["id"], row["todo_id"], row["kind"], row["body"], _date(row["created_at"]), _date(row["delivered_at"]), row["ack_id"])

    def list_notes(self, todo_id: str) -> list[TodoNote]:
        """按创建顺序返回待办的全部笔记。"""
        with self.connect() as db:
            rows = db.execute("SELECT * FROM todo_notes WHERE todo_id=? ORDER BY created_at,id", (todo_id,)).fetchall()
        return [TodoNote(row["id"], row["todo_id"], row["kind"], row["body"], _date(row["created_at"]), _date(row["delivered_at"]), row["ack_id"]) for row in rows]

    def pending_additions(self, todo_id: str) -> list[TodoNote]:
        """返回尚未投递给 Codex 的普通追加笔记。"""
        return [note for note in self.pending_delivery_notes(todo_id) if note.kind == "addition"]

    def pending_delivery_notes(self, todo_id: str) -> list[TodoNote]:
        """返回尚未投递的追加和 followup 笔记。"""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM todo_notes WHERE todo_id=? AND kind IN ('addition','followup') AND delivered_at IS NULL ORDER BY id",
                (todo_id,),
            ).fetchall()
        return [TodoNote(row["id"], row["todo_id"], row["kind"], row["body"], _date(row["created_at"]), None, row["ack_id"]) for row in rows]

    def mark_notes_delivered(self, note_ids: list[int], ack_id: str) -> None:
        """记录笔记已随某 turn 投递，ack_id 用于去重。"""
        if not note_ids:
            return
        placeholders = ",".join("?" for _ in note_ids)
        with self.transaction() as db:
            db.execute(
                f"UPDATE todo_notes SET delivered_at=?,ack_id=COALESCE(ack_id,?) WHERE id IN ({placeholders})",
                (_now(), ack_id, *note_ids),
            )

    def record_workspace(self, todo_id: str, path: Path, project_root: Path | None, branch: str | None, is_worktree: bool, dirty_policy: str | None) -> None:
        """保存或更新待办工作区信息。"""
        with self.transaction() as db:
            db.execute(
                "INSERT INTO workspaces(todo_id,path,project_root,branch,is_worktree,dirty_policy,created_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(todo_id) DO UPDATE SET path=excluded.path,project_root=excluded.project_root,branch=excluded.branch,is_worktree=excluded.is_worktree,dirty_policy=excluded.dirty_policy",
                (todo_id, str(path), str(project_root) if project_root else None, branch, int(is_worktree), dirty_policy, _now()),
            )

    def record_codex_event(self, run_id: str, method: str, raw: Any, detail: str | None = None) -> int:
        """记录完整 Codex 事件，并可选更新待办的最近进度。"""
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT INTO codex_events(run_id,method,raw_json,created_at) VALUES(?,?,?,?)",
                (run_id, method, _json(raw), _now()),
            )
            if detail is not None:
                db.execute(
                    "UPDATE todos SET latest_detail=?,updated_at=? WHERE id=(SELECT todo_id FROM codex_runs WHERE run_id=?)",
                    (detail, _now(), run_id),
                )
        return int(cursor.lastrowid)

    def codex_events(self, todo_id: str) -> list[dict[str, Any]]:
        """按时间顺序返回某待办的全部 Codex 事件。"""
        with self.connect() as db:
            rows = db.execute(
                "SELECT e.* FROM codex_events e JOIN codex_runs r ON r.run_id=e.run_id WHERE r.todo_id=? ORDER BY e.id",
                (todo_id,),
            ).fetchall()
        return [{"id": row["id"], "run_id": row["run_id"], "method": row["method"], "raw": json.loads(row["raw_json"]), "created_at": row["created_at"]} for row in rows]

    def create_question(self, question: Any) -> None:
        """在展示前持久化问题，保证重启后状态可追踪。"""
        now = _now()
        options = [{"label": option.label, "description": option.description} for option in question.options]
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO questions(id,todo_id,run_id,kind,prompt,options_json,recommended,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (question.id, question.todo_id, getattr(question, "run_id", None), question.kind, question.prompt, _json(options), question.recommended, "queued", now),
            )

    def mark_question_shown(self, question_id: str, shown_at: datetime, deadline: datetime) -> None:
        """记录问题实际展示时间和超时截止时间。"""
        with self.transaction() as db:
            db.execute(
                "UPDATE questions SET state='shown',shown_at=?,deadline_at=? WHERE id=?",
                (shown_at.isoformat(), deadline.isoformat(), question_id),
            )

    def answer_question(self, question_id: str, answer: Any) -> None:
        """保存用户对问题的回答。"""
        with self.transaction() as db:
            db.execute(
                "UPDATE questions SET state='answered',answered_at=?,answer_json=? WHERE id=?",
                (_now(), _json({"option": answer.option, "other": answer.other, "timed_out": answer.timed_out}), question_id),
            )

    def timeline(self, todo_id: str) -> list[dict[str, Any]]:
        """合并笔记、问题与转换详情，返回待办时间线。"""
        items: list[dict[str, Any]] = []
        with self.connect() as db:
            for row in db.execute("SELECT * FROM todo_notes WHERE todo_id=?", (todo_id,)):
                items.append({"at": row["created_at"], "kind": row["kind"], "body": row["body"]})
            for row in db.execute("SELECT * FROM questions WHERE todo_id=?", (todo_id,)):
                items.append({"at": row["created_at"], "kind": "question", "body": row["prompt"], "answer": json.loads(row["answer_json"]) if row["answer_json"] else None})
            for row in db.execute("SELECT * FROM transitions WHERE todo_id=? AND detail IS NOT NULL", (todo_id,)):
                items.append({"at": row["created_at"], "kind": "transition", "body": row["detail"]})
        return sorted(items, key=lambda item: item["at"])

    def start_orchestrator_run(self, run_id: str, tick_at: datetime, input_items: Any, compact_prompt: str | None) -> None:
        """记录一次调度器运行的开始状态和完整输入。"""
        now = _now()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO orchestrator_runs(run_id,tick_at,status,compact_prompt,input_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (run_id, tick_at.isoformat(), "running", compact_prompt, _json(input_items), now, now),
            )

    def finish_orchestrator_run(self, run_id: str, status: str, output: Any, usage: Any) -> None:
        """保存调度器运行结果与 token 用量。"""
        with self.transaction() as db:
            db.execute(
                "UPDATE orchestrator_runs SET status=?,output_json=?,usage_json=?,updated_at=? WHERE run_id=?",
                (status, _json(output), _json(usage), _now(), run_id),
            )

    def append_orchestrator_items(self, run_id: str, items: list[Any], token_count: int = 0) -> None:
        """追加调度器输出项，token 只记在第一条以简化统计。"""
        if not items:
            return
        with self.transaction() as db:
            for index, item in enumerate(items):
                db.execute(
                    "INSERT INTO orchestrator_items(run_id,item_json,token_count,created_at) VALUES(?,?,?,?)",
                    (run_id, _json(item), token_count if index == 0 else 0, _now()),
                )

    def orchestrator_context(self) -> tuple[str | None, list[Any], int, int]:
        """返回压缩总结、未压缩历史、估算 token 数和最后记录 ID。"""
        with self.connect() as db:
            state = db.execute("SELECT * FROM orchestrator_state WHERE singleton=1").fetchone()
            through = state["compacted_through"] if state else 0
            rows = db.execute("SELECT id,item_json,token_count FROM orchestrator_items WHERE id>? ORDER BY id", (through,)).fetchall()
        prompt = state["compact_prompt"] if state else None
        tokens = (state["token_count"] if state else 0) + sum(row["token_count"] for row in rows)
        last_id = rows[-1]["id"] if rows else through
        return prompt, [json.loads(row["item_json"]) for row in rows], tokens, last_id

    def save_compaction(self, compact_prompt: str, through_id: int, token_count: int) -> None:
        """保存压缩后的历史总结和压缩截止位置。"""
        with self.transaction() as db:
            db.execute(
                "INSERT INTO orchestrator_state(singleton,compact_prompt,compacted_through,token_count,updated_at) VALUES(1,?,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET compact_prompt=excluded.compact_prompt,compacted_through=excluded.compacted_through,token_count=excluded.token_count,updated_at=excluded.updated_at",
                (compact_prompt, through_id, token_count, _now()),
            )

    def get_tool_call(self, run_id: str, call_id: str) -> dict[str, Any] | None:
        """读取已有工具调用记录，供重试时复用结果。"""
        with self.connect() as db:
            row = db.execute("SELECT * FROM tool_calls WHERE run_id=? AND call_id=?", (run_id, call_id)).fetchone()
        if row is None:
            return None
        return {"name": row["name"], "arguments": json.loads(row["arguments_json"]), "result": json.loads(row["result_json"]) if row["result_json"] else None, "status": row["status"]}

    def begin_tool_call(self, run_id: str, call_id: str, name: str, arguments: Any) -> bool:
        """标记工具调用开始，重复 call_id 时返回 False。"""
        with self.transaction() as db:
            try:
                db.execute(
                    "INSERT INTO tool_calls(run_id,call_id,name,arguments_json,status) VALUES(?,?,?,?,?)",
                    (run_id, call_id, name, _json(arguments), "running"),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def finish_tool_call(self, run_id: str, call_id: str, result: Any, status: str = "completed") -> None:
        """保存工具调用的完整结果和状态。"""
        with self.transaction() as db:
            db.execute(
                "UPDATE tool_calls SET result_json=?,status=? WHERE run_id=? AND call_id=?",
                (_json(result), status, run_id, call_id),
            )

    def save_tool_result(self, run_id: str, call_id: str, name: str, arguments: Any, result: Any) -> bool:
        """一次性写入工具结果；已有记录时不覆盖。"""
        if not self.begin_tool_call(run_id, call_id, name, arguments):
            return False
        self.finish_tool_call(run_id, call_id, result)
        return True

    def get_setting(self, key: str, default: Any = None) -> Any:
        """读取 JSON 编码的设置项。"""
        with self.connect() as db:
            row = db.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        """写入 JSON 编码的设置项，已存在时覆盖。"""
        with self.transaction() as db:
            db.execute(
                "INSERT INTO settings(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (key, _json(value)),
            )
