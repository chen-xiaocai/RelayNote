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


class SequentialPresenter:
    """记录展示顺序并选择每个问题的第一个选项。"""

    def __init__(self) -> None:
        self.shown: list[str] = []

    async def show(self, question: Question) -> Answer:
        self.shown.append(question.prompt)
        return Answer(0, None, False)


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
        "arguments": {"questions": [{"question": "怎么做？", "options": ["推荐"]}]},
    }
    writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
    await writer.drain()
    answer = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    await server.close()
    assert answer == {"answers": [{"option": None, "other": "完整的其他回答", "timed_out": False}]}
    assert store.timeline(todo.id)[-1]["answer"]["other"] == "完整的其他回答"


async def test_multiple_questions_are_shown_sequentially(tmp_path: Path) -> None:
    """验证一次 Ask 调用中的多个问题按顺序展示并返回全部答案。"""
    presenter = SequentialPresenter()
    broker = QuestionBroker(presenter)
    server = AskBrokerServer(tmp_path / "runtime" / "ask.sock", "token", broker)
    await server.start()
    reader, writer = await asyncio.open_unix_connection(server.path)
    request = {
        "token": "token",
        "todo_id": None,
        "run_id": "run",
        "arguments": {
            "questions": [
                {"question": "第一个问题", "options": ["推荐一", "其他一"]},
                {"question": "第二个问题", "options": ["推荐二"]},
            ]
        },
    }
    writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
    await writer.drain()
    answer = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    await server.close()
    assert presenter.shown == ["第一个问题", "第二个问题"]
    assert answer == {
        "answers": [
            {"option": 0, "other": None, "timed_out": False},
            {"option": 0, "other": None, "timed_out": False},
        ]
    }


async def test_mcp_bridge_falls_back_to_recommended_when_broker_is_unavailable(monkeypatch, tmp_path: Path) -> None:
    """验证桥不可用时 MCP 返回推荐项而不是抛错阻塞 Codex。"""
    monkeypatch.setenv("RELAYNOTE_ASK_SOCKET", str(tmp_path / "missing.sock"))
    monkeypatch.setenv("RELAYNOTE_ASK_TOKEN", "token")
    answer = await broker_call(
        {"questions": [{"question": "q1", "options": ["A", "B"]}, {"question": "q2", "options": ["C"]}]},
        unavailable_timeout=0.1,
    )
    assert answer == {"answers": [{"option": 0, "other": None, "timed_out": True}, {"option": 0, "other": None, "timed_out": True}]}


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
