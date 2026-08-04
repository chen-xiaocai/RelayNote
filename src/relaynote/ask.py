"""全局问题队列、Ask Unix socket 桥与终端问题展示。"""

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

from .ipc import safe_unix_socket_path


@dataclass(frozen=True, slots=True)
class Option:
    """展示给用户的一个选项，包含标题和说明。"""

    label: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class Answer:
    """用户回答结果：选项索引、其他文本与是否超时。"""

    option: int | None
    other: str | None
    timed_out: bool


@dataclass(slots=True)
class Question:
    """一条待展示的问题；blocking 问题优先于 notification 通知。"""

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
        """校验问题文本、选项数量、推荐项和选项内容。"""
        if not self.prompt.strip():
            raise ValueError("question cannot be empty")
        if not 1 <= len(self.options) <= 3:
            raise ValueError("a question requires 1-3 options")
        if self.recommended not in range(len(self.options)):
            raise ValueError("recommended option is out of range")
        if any(not option.label.strip() for option in self.options):
            raise ValueError("option label cannot be empty")


class Presenter(Protocol):
    """问题展示器协议，实现方负责把问题呈现给用户。"""

    async def show(self, question: Question) -> Answer: ...


class QuestionBroker:
    """串行化问题展示、超时兜底、持久化和展示回调。"""

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
        """把问题加入优先级队列，并返回最终答案。"""
        if self.store is not None:
            self.store.create_question(question)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Answer] = loop.create_future()
        self._sequence += 1
        # blocking 问题优先，notification 通知排在后面。
        priority = 0 if question.kind == "blocking" else 1
        await self._queue.put((priority, self._sequence, question, future))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())
        return await future

    async def _work(self) -> None:
        """按队列顺序展示问题；超时自动选择推荐项。"""
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
                # 展示超时按推荐项继续，避免任务永久阻塞。
                answer = Answer(question.recommended, None, True)
            except Exception as error:  # noqa: BLE001 - 展示器失败需回传给被阻塞的调用方
                # 展示器内部错误直接回传给该问题的调用方。
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
        """取消正在运行的问题展示任务。"""
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass


class ConsolePresenter:
    """终端版问题展示器，读取一行用户输入。"""

    async def show(self, question: Question) -> Answer:
        """输出选项提示，并解析数字选项或自由文本。"""
        labels = " / ".join(f"{i + 1}:{o.label}" for i, o in enumerate(question.options))
        value = await asyncio.to_thread(input, f"{question.prompt} [{labels} / other] ")
        if value.isdigit() and 1 <= int(value) <= len(question.options):
            return Answer(int(value) - 1, None, False)
        return Answer(None, value, False)


class AskBrokerServer:
    """每个任务 Ask MCP 进程共用的本地认证桥。"""

    def __init__(self, path: Path, token: str, broker: QuestionBroker) -> None:
        self.path = safe_unix_socket_path(path, "ask")
        self.token = token
        self.broker = broker
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        """创建权限受限的 Unix socket，并开始接受 Ask 请求。"""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.server = await asyncio.start_unix_server(self._handle, self.path)
        os.chmod(self.path, 0o600)

    async def close(self) -> None:
        """关闭监听 socket 并删除临时路径。"""
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """处理单条 Ask 请求：校验 token、构造问题并写回答案。"""
        try:
            raw = await reader.readline()
            request = json.loads(raw)
            if request.get("token") != self.token:
                # token 不匹配时不暴露任何业务信息。
                response: dict[str, Any] = {"error": "unauthorized"}
            else:
                arguments = request.get("arguments") or {}
                questions = arguments.get("questions")
                answers = []
                for item in questions or []:
                    options = tuple(Option(option, "") for option in item.get("options", []))
                    question = Question(
                        prompt=item.get("question", ""),
                        options=options,
                        recommended=0,
                        todo_id=request.get("todo_id"),
                        run_id=request.get("run_id"),
                    )
                    answer = await self.broker.ask(question)
                    answers.append({"option": answer.option, "other": answer.other, "timed_out": answer.timed_out})
                response = {"answers": answers}
        except Exception as error:  # noqa: BLE001 - IPC 必须返回结构化失败
            response = {"error_type": type(error).__name__, "error": str(error)}
        writer.write((json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
