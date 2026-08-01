from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
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
    kind: str = "blocking"
    id: str = field(default_factory=lambda: str(uuid4()))
    shown_at: datetime | None = None
    deadline: datetime | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.options) <= 3:
            raise ValueError("a question requires 1-3 options")
        if self.recommended not in range(len(self.options)):
            raise ValueError("recommended option is out of range")


class Presenter(Protocol):
    async def show(self, question: Question) -> Answer: ...


class QuestionBroker:
    def __init__(self, presenter: Presenter, timeout_seconds: float = 120) -> None:
        self.presenter = presenter
        self.timeout_seconds = timeout_seconds
        self._queue: asyncio.PriorityQueue[tuple[int, int, Question, asyncio.Future[Answer]]] = asyncio.PriorityQueue()
        self._sequence = 0
        self._worker: asyncio.Task[None] | None = None

    async def ask(self, question: Question) -> Answer:
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
            try:
                answer = await asyncio.wait_for(self.presenter.show(question), self.timeout_seconds)
                if answer.option is None and not (answer.other and answer.other.strip()):
                    raise ValueError("Other requires non-empty text")
            except TimeoutError:
                answer = Answer(question.recommended, None, True)
            except BaseException as error:
                if not future.done():
                    future.set_exception(error)
                continue
            if not future.done():
                future.set_result(answer)


class ConsolePresenter:
    async def show(self, question: Question) -> Answer:
        labels = " / ".join(f"{i + 1}:{o.label}" for i, o in enumerate(question.options))
        value = await asyncio.to_thread(input, f"{question.prompt} [{labels} / other] ")
        if value.isdigit() and 1 <= int(value) <= len(question.options):
            return Answer(int(value) - 1, None, False)
        return Answer(None, value, False)
