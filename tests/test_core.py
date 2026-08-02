from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from relaynote.ask import Answer, Option, Question, QuestionBroker
from relaynote.db import ConflictError, Store
from relaynote.instance import AlreadyRunningError, InstanceLock
from relaynote.logging import JsonlLogger
from relaynote.model import InvalidTransition, TodoState
from relaynote.scheduler import BoundaryScheduler, next_boundary
from relaynote.workspace import validate_cwd


def test_state_cas_and_auto_lease(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    first, second = store.create_todo("first"), store.create_todo("second")
    store.acquire_auto_lease(first.id, "run-1")
    running = store.transition(first.id, TodoState.RUNNING, first.version)
    with pytest.raises(ConflictError):
        store.acquire_auto_lease(second.id, "run-2")
    with pytest.raises(ConflictError):
        store.transition(first.id, TodoState.WAITING, first.version)
    taken = store.transition(running.id, TodoState.TAKEN_OVER, running.version)
    store.acquire_auto_lease(second.id, "run-2")
    assert taken.state is TodoState.TAKEN_OVER
    assert store.transition(second.id, TodoState.RUNNING, second.version).state is TodoState.RUNNING


def test_invalid_transition(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    todo = store.create_todo("x")
    with pytest.raises(InvalidTransition):
        store.transition(todo.id, TodoState.COMPLETED, todo.version)


def test_tool_call_idempotency(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    assert store.save_tool_result("run", "call", "bash", {"x": 1}, {"ok": True})
    assert not store.save_tool_result("run", "call", "bash", {"x": 2}, {"ok": False})


def test_boundary_math() -> None:
    assert next_boundary(datetime(2026, 8, 1, 12, 0, tzinfo=UTC)) == datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    assert next_boundary(datetime(2026, 8, 1, 12, 0, 1, tzinfo=UTC)) == datetime(2026, 8, 1, 12, 5, tzinfo=UTC)
    assert next_boundary(datetime(2026, 8, 1, 12, 58, tzinfo=UTC)) == datetime(2026, 8, 1, 13, 0, tzinfo=UTC)


async def test_scheduler_skips_overlap() -> None:
    gate = asyncio.Event()
    async def callback(_: datetime) -> None:
        await gate.wait()
    scheduler = BoundaryScheduler(callback)
    at = datetime.now(UTC)
    task = asyncio.create_task(scheduler.tick(at))
    await asyncio.sleep(0)
    assert not await scheduler.tick(at)
    gate.set()
    assert await task


async def test_scheduler_run_continues_after_tick_error(monkeypatch) -> None:
    failures = 0
    errors = []
    stop = asyncio.Event()

    async def callback(_: datetime) -> None:
        nonlocal failures
        failures += 1
        if failures == 1:
            raise RuntimeError("完整调度错误")
        stop.set()

    scheduler = BoundaryScheduler(callback, on_error=lambda at, error: errors.append((at, error)))

    async def immediate_timeout(coro, timeout):
        coro.close()
        raise TimeoutError

    monkeypatch.setattr("relaynote.scheduler.asyncio.wait_for", immediate_timeout)
    await scheduler.run(stop)
    assert failures == 2
    assert len(errors) == 1
    assert str(errors[0][1]) == "完整调度错误"


def test_py2app_config_includes_anyio_async_backend() -> None:
    setup = Path(__file__).resolve().parents[1] / "setup.py"
    assert "anyio._backends._asyncio" in setup.read_text()


class RecordingPresenter:
    def __init__(self) -> None:
        self.shown: list[str] = []
        self.gates: dict[str, asyncio.Future[Answer]] = {}

    async def show(self, question: Question) -> Answer:
        self.shown.append(question.prompt)
        future = asyncio.get_running_loop().create_future()
        self.gates[question.prompt] = future
        return await future


async def test_question_fifo_timeout_starts_when_shown() -> None:
    presenter = RecordingPresenter()
    broker = QuestionBroker(presenter, timeout_seconds=1)
    option = (Option("yes", "recommended"),)
    first = asyncio.create_task(broker.ask(Question("first", option, 0)))
    second = asyncio.create_task(broker.ask(Question("second", option, 0)))
    for _ in range(10):
        if presenter.shown:
            break
        await asyncio.sleep(0)
    assert presenter.shown == ["first"]
    presenter.gates["first"].set_result(Answer(0, None, False))
    assert await first == Answer(0, None, False)
    for _ in range(10):
        if len(presenter.shown) == 2:
            break
        await asyncio.sleep(0)
    assert presenter.shown == ["first", "second"]
    presenter.gates["second"].set_result(Answer(0, None, False))
    await second


def test_full_jsonl_roundtrip_and_secret_rejection(tmp_path: Path) -> None:
    logger = JsonlLogger(tmp_path / "events.jsonl")
    raw = {"unicode": "深层", "nested": {"items": ["x" * 200_000, {"stack": "line\n" * 1000}]}}
    logger.write("fixture", raw)
    assert json.loads(logger.path.read_text())["raw"] == raw
    with pytest.raises(ValueError):
        logger.write("bad", {"Authorization": "secret"})


def test_multi_megabyte_jsonl_value_is_not_shortened(tmp_path: Path) -> None:
    logger = JsonlLogger(tmp_path / "large.jsonl")
    value = "完整内容" * 600_000
    logger.write("large_fixture", {"value": value, "length": len(value)})
    record = json.loads(logger.path.read_text(encoding="utf-8"))
    assert record["raw"]["value"] == value
    assert record["raw"]["length"] == len(value)


def test_workspace_rejects_broad_paths() -> None:
    with pytest.raises(ValueError):
        validate_cwd(Path("/"))
    with pytest.raises(ValueError):
        validate_cwd(Path.home())


def test_single_instance_lock(tmp_path: Path) -> None:
    path = tmp_path / "relaynote.lock"
    with InstanceLock(path), pytest.raises(AlreadyRunningError), InstanceLock(path):
        pass
    with InstanceLock(path):
        assert path.read_text().isdigit()
