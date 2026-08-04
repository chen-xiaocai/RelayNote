"""Ask socket 桥、MCP 兜底与问题超时持久化的测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from relaynote.ask import Answer, AskBrokerServer, Option, Question, QuestionBroker
from relaynote.ask_mcp import broker_call
from relaynote.db import Store


class ImmediatePresenter:
    """始终立即返回自由文本回答的测试展示器。"""

    async def show(self, question: Question) -> Answer:
        return Answer(None, "完整的其他回答", False)


async def test_authenticated_ask_socket_roundtrip(tmp_path: Path) -> None:
    """验证带 token 的 Ask socket 能完整往返并持久化回答。"""
    store = Store(tmp_path / "db.sqlite3")
    todo = store.create_todo("任务")
    broker = QuestionBroker(ImmediatePresenter(), store=store)
    server = AskBrokerServer(tmp_path / "runtime" / "ask.sock", "token", broker)
    await server.start()
    reader, writer = await asyncio.open_unix_connection(server.path)
    request = {
        "token": "token",
        "todo_id": todo.id,
        "run_id": "run",
        "arguments": {"question": "怎么做？", "options": [{"label": "A", "description": "推荐"}], "recommended": 0},
    }
    writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
    await writer.drain()
    answer = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    await server.close()
    assert answer == {"option": None, "other": "完整的其他回答", "timed_out": False}
    assert store.timeline(todo.id)[-1]["answer"]["other"] == "完整的其他回答"


async def test_mcp_bridge_falls_back_to_recommended_when_broker_is_unavailable(monkeypatch, tmp_path: Path) -> None:
    """验证桥不可用时 MCP 返回推荐项而不是抛错阻塞 Codex。"""
    monkeypatch.setenv("RELAYNOTE_ASK_SOCKET", str(tmp_path / "missing.sock"))
    monkeypatch.setenv("RELAYNOTE_ASK_TOKEN", "token")
    answer = await broker_call(
        {"question": "q", "options": [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}], "recommended": 1},
        unavailable_timeout=0.1,
    )
    assert answer == {"option": 1, "other": None, "timed_out": True}


async def test_timeout_is_measured_after_display_and_persisted(tmp_path: Path) -> None:
    """验证超时从展示后开始计算，并持久化展示时间和截止时间。"""
    class NeverPresenter:
        """永不返回答案的测试展示器。"""

        async def show(self, question: Question) -> Answer:
            await asyncio.Future()

    store = Store(tmp_path / "db.sqlite3")
    broker = QuestionBroker(NeverPresenter(), timeout_seconds=0.01, store=store)
    question = Question("问题", (Option("推荐", "默认动作"),), 0)
    answer = await broker.ask(question)
    assert answer == Answer(0, None, True)
    with store.connect() as db:
        row = db.execute("SELECT * FROM questions WHERE id=?", (question.id,)).fetchone()
    assert row["shown_at"] is not None
    assert row["deadline_at"] is not None
    assert json.loads(row["answer_json"])["timed_out"] is True
