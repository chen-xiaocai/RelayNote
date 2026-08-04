"""运行时测试：完成通知后的 followup 沿用原 thread。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from relaynote.ask import Answer, Question
from relaynote.config import Settings
from relaynote.model import TodoState
from relaynote.runtime import Runtime


class LaterPresenter:
    """始终选择推荐项的测试展示器。"""

    async def show(self, question: Question) -> Answer:
        return Answer(question.recommended, None, False)


class FakeCodex:
    """记录调用并模拟 Codex 会话对象的测试替身。"""

    def __init__(self) -> None:
        self.process = SimpleNamespace(pid=123)
        self.endpoint = "ws://127.0.0.1:1234"
        self.thread_id = None
        self.turn_id = None
        self.final_message = "完整完成汇报"
        self.turns = []

    async def start_thread(self, cwd: Path) -> str:
        """模拟启动新 thread。"""
        self.thread_id = "thread-original"
        return self.thread_id

    async def resume_thread(self, thread_id: str, cwd: Path) -> str:
        """模拟恢复已有 thread。"""
        self.thread_id = thread_id
        return thread_id

    async def start_turn(self, text: str) -> str:
        """记录输入并模拟启动 turn。"""
        self.turns.append(text)
        self.turn_id = f"turn-{len(self.turns)}"
        return self.turn_id

    async def steer(self, text: str) -> str:
        return self.turn_id


async def test_delayed_completion_followup_reuses_original_thread(monkeypatch, tmp_path: Path) -> None:
    """验证自动槽空闲时，验收反馈会沿用原 thread 继续。"""
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
    # 自动槽释放后再启动，应恢复原 thread 而不是新建。
    resumed = await runtime.start_todo(todo.id, pending.version)
    assert resumed["resumed"] is True
    assert resumed["thread_id"] == "thread-original"
    assert fake.turns == ["原始任务", "继续补充完整测试"]
