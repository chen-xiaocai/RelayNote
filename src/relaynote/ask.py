from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class Option:
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class Answer:
    option: int | None
    other: str | None
    timed_out: bool


@dataclass(slots=True)
class Question:
    prompt: str
    options: tuple[Option, ...]
    recommended: int
    todo_id: str | None = None
    run_id: str | None = None
    kind: str = "blocking"
    id: str = field(default_factory=lambda: str(uuid4()))
    shown_at: datetime | None = None
    deadline: datetime | None = None

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("question cannot be empty")
        if not 1 <= len(self.options) <= 3:
            raise ValueError("a question requires 1-3 options")
        if self.recommended not in range(len(self.options)):
            raise ValueError("recommended option is out of range")
        if any(not option.label.strip() or not option.description.strip() for option in self.options):
            raise ValueError("option label and description cannot be empty")


class Presenter(Protocol):
    async def show(self, question: Question) -> Answer: ...


class QuestionBroker:
    def __init__(
        self,
        presenter: Presenter,
        timeout_seconds: float = 120,
        store: Any | None = None,
        on_show: Callable[[Question], Awaitable[None]] | None = None,
        on_answer: Callable[[Question, Answer], Awaitable[None]] | None = None,
    ) -> None:
        self.presenter = presenter
        self.timeout_seconds = timeout_seconds
        self.store = store
        self.on_show = on_show
        self.on_answer = on_answer
        self._queue: asyncio.PriorityQueue[tuple[int, int, Question, asyncio.Future[Answer]]] = asyncio.PriorityQueue()
        self._sequence = 0
        self._worker: asyncio.Task[None] | None = None

    async def ask(self, question: Question) -> Answer:
        if self.store is not None:
            self.store.create_question(question)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Answer] = loop.create_future()
        self._sequence += 1
        priority = 0 if question.kind == "blocking" else 1
        await self._queue.put((priority, self._sequence, question, future))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())
        return await future

    async def _work(self) -> None:
        while not self._queue.empty():
            _, _, question, future = await self._queue.get()
            question.shown_at = datetime.now(UTC)
            question.deadline = question.shown_at + timedelta(seconds=self.timeout_seconds)
            if self.store is not None:
                self.store.mark_question_shown(question.id, question.shown_at, question.deadline)
            if self.on_show is not None:
                await self.on_show(question)
            try:
                answer = await asyncio.wait_for(self.presenter.show(question), self.timeout_seconds)
                if answer.option is None and not (answer.other and answer.other.strip()):
                    raise ValueError("Other requires non-empty text")
            except TimeoutError:
                answer = Answer(question.recommended, None, True)
            except Exception as error:  # noqa: BLE001 - deliver presenter failures to the blocked caller
                if not future.done():
                    future.set_exception(error)
                continue
            if self.store is not None:
                self.store.answer_question(question.id, answer)
            if self.on_answer is not None:
                await self.on_answer(question, answer)
            if not future.done():
                future.set_result(answer)

    async def close(self) -> None:
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass


class ConsolePresenter:
    async def show(self, question: Question) -> Answer:
        labels = " / ".join(f"{i + 1}:{o.label}" for i, o in enumerate(question.options))
        value = await asyncio.to_thread(input, f"{question.prompt} [{labels} / other] ")
        if value.isdigit() and 1 <= int(value) <= len(question.options):
            return Answer(int(value) - 1, None, False)
        return Answer(None, value, False)


class AskBrokerServer:
    """Authenticated local bridge shared by every task-specific Ask MCP process."""

    def __init__(self, path: Path, token: str, broker: QuestionBroker) -> None:
        self.path = path
        self.token = token
        self.broker = broker
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.server = await asyncio.start_unix_server(self._handle, self.path)
        os.chmod(self.path, 0o600)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            request = json.loads(raw)
            if request.get("token") != self.token:
                response: dict[str, Any] = {"error": "unauthorized"}
            else:
                arguments = request.get("arguments") or {}
                options = tuple(Option(item["label"], item["description"]) for item in arguments.get("options", []))
                question = Question(
                    prompt=arguments.get("question", ""),
                    options=options,
                    recommended=arguments.get("recommended", -1),
                    todo_id=request.get("todo_id"),
                    run_id=request.get("run_id"),
                )
                answer = await self.broker.ask(question)
                response = {"option": answer.option, "other": answer.other, "timed_out": answer.timed_out}
        except Exception as error:  # noqa: BLE001 - IPC must return a structured failure
            response = {"error_type": type(error).__name__, "error": str(error)}
        writer.write((json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
