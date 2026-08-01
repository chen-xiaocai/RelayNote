from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from relaynote.ask import Answer, Question
from relaynote.config import Settings
from relaynote.model import TodoState
from relaynote.runtime import Runtime


class LaterPresenter:
    async def show(self, question: Question) -> Answer:
        return Answer(question.recommended, None, False)


class FakeCodex:
    def __init__(self) -> None:
        self.process = SimpleNamespace(pid=123)
        self.endpoint = "ws://127.0.0.1:1234"
        self.thread_id = None
        self.turn_id = None
        self.final_message = "完整完成汇报"
        self.turns = []

    async def start_thread(self, cwd: Path) -> str:
        self.thread_id = "thread-original"
        return self.thread_id

    async def resume_thread(self, thread_id: str, cwd: Path) -> str:
        self.thread_id = thread_id
        return thread_id

    async def start_turn(self, text: str) -> str:
        self.turns.append(text)
        self.turn_id = f"turn-{len(self.turns)}"
        return self.turn_id

    async def steer(self, text: str) -> str:
        return self.turn_id


async def test_delayed_completion_followup_reuses_original_thread(monkeypatch, tmp_path: Path) -> None:
    fake = FakeCodex()

    async def spawn(*args, **kwargs):
        return fake

    monkeypatch.setattr("relaynote.runtime.spawn_app_server", spawn)
    settings = Settings(tmp_path / "data", tmp_path / "work", Path("/bin/false"), None, ask_command=Path("/bin/false"))
    runtime = Runtime(settings, LaterPresenter())
    todo = runtime.create_todo("原始任务")
    started = await runtime.start_todo(todo.id, todo.version)
    await runtime._codex_event(started["run_id"], {"method": "turn/completed", "params": {"turn": {"id": started["turn_id"], "status": "completed"}}})
    assert runtime.store.get_todo(todo.id).state is TodoState.COMPLETED
    blocker = runtime.create_todo("占用自动槽")
    runtime.store.acquire_auto_lease(blocker.id, "blocking-run")
    queued = await runtime.resume_completed(todo.id, "继续补充完整测试")
    assert queued["delivery"] == "prioritized"
    runtime.store.release_auto_lease(blocker.id)
    pending = runtime.store.get_todo(todo.id)
    resumed = await runtime.start_todo(todo.id, pending.version)
    assert resumed["resumed"] is True
    assert resumed["thread_id"] == "thread-original"
    assert fake.turns == ["原始任务", "继续补充完整测试"]
