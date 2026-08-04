"""RelayNote 运行时：串联用户操作、Codex 进程事件、Ask 问题和调度器。"""

from __future__ import annotations

import asyncio
import json
import secrets
import shlex
import shutil
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .ask import Answer, AskBrokerServer, Option, Presenter, Question, QuestionBroker
from .codex import CodexProcess, login_shell, spawn_app_server
from .config import Settings
from .db import ConflictError, Store
from .logging import JsonlLogger
from .model import Todo, TodoState
from .orchestrator import Orchestrator, ToolDefinition
from .scheduler import BoundaryScheduler
from .workspace import validate_cwd


class Runtime:
    """应用运行时对象，持有数据库、问题桥、Codex 进程与调度器。"""

    def __init__(self, settings: Settings, presenter: Presenter, on_change: Callable[[], None] | None = None) -> None:
        """初始化目录、数据库、Ask 服务、调度器和工具定义。"""
        self.settings = settings
        self.settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.settings.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = Store(settings.database_path)
        self.logger = JsonlLogger(settings.data_dir / "events.jsonl")
        self.ask_token = secrets.token_urlsafe(32)
        self.on_change = on_change
        self.broker = QuestionBroker(
            presenter,
            timeout_seconds=120,
            store=self.store,
            on_show=self._question_shown,
            on_answer=self._question_answered,
        )
        self.ask_server = AskBrokerServer(settings.ask_socket, self.ask_token, self.broker)
        self.processes: dict[str, CodexProcess] = {}
        self.stop_event = asyncio.Event()
        self.orchestrator = Orchestrator(self.store, settings, self.logger, self._tool_definitions())
        self.scheduler = BoundaryScheduler(self.orchestrator.run, on_error=self._scheduler_error)
        self.scheduler_task: asyncio.Task[None] | None = None
        self._notification_tasks: set[asyncio.Task[Any]] = set()
        self._process_tasks: set[asyncio.Task[Any]] = set()

    def _scheduler_error(self, at: datetime, error: BaseException) -> None:
        """把调度器异常完整写入 JSONL 日志。"""
        self.logger.write(
            "scheduler_error",
            {
                "tick": at.isoformat(),
                "error_type": type(error).__name__,
                "error": str(error),
                "stack": "".join(traceback.format_exception(type(error), error, error.__traceback__)),
            },
        )

    async def start(self) -> None:
        """恢复上次运行状态，启动 Ask socket 和调度器。"""
        self._reconcile_stale_lease()
        await self.ask_server.start()
        self.scheduler_task = asyncio.create_task(self.scheduler.run(self.stop_event))

    async def close(self) -> None:
        """停止调度器、挂起活动任务，并清理所有子进程与后台任务。"""
        self.stop_event.set()
        if self.scheduler_task is not None:
            await self.scheduler_task
        lease = self.store.automatic_lease()
        if lease is not None:
            todo = self.store.get_todo(lease[0])
            if todo.state in {TodoState.RUNNING, TodoState.WAITING}:
                # 退出前先安全停止自动任务，避免 Codex 进程残留。
                await self.suspend(todo.id)
        await self.ask_server.close()
        await self.broker.close()
        for process in list(self.processes.values()):
            await process.close()
        for task in self._process_tasks:
            if not task.done():
                task.cancel()
        for task in self._notification_tasks:
            if not task.done():
                task.cancel()

    def _reconcile_stale_lease(self) -> None:
        """处理上次进程退出后仍占用自动槽的待办。"""
        lease = self.store.automatic_lease()
        if lease is None:
            return
        todo = self.store.get_todo(lease[0])
        if todo.state is TodoState.STOPPING:
            # 停止流程中断时按停止前记录的目标状态恢复。
            target = TodoState(todo.pending_action or "suspended")
            self.store.transition(todo.id, target, todo.version, "RelayNote 重启时完成停止状态恢复")
        elif todo.state in {TodoState.RUNNING, TodoState.WAITING}:
            # 无法重连上次的 Codex app-server，标记为异常并等待人工重试。
            self.store.transition(todo.id, TodoState.ERROR, todo.version, "RelayNote 上次退出后未能重新连接 Codex app-server，请人工重试")

    def create_todo(self, body: str) -> Todo:
        """创建待办并通知 UI 刷新。"""
        todo = self.store.create_todo(body)
        self._changed()
        return todo

    async def append(self, todo_id: str, body: str) -> dict[str, Any]:
        """追加笔记；如果对应 turn 正在运行则立即 steer 给 Codex。"""
        todo = self.store.get_todo(todo_id)
        note = self.store.append_note(todo_id, body)
        delivery = "queued"
        run = self.store.latest_run(todo_id)
        if todo.state in {TodoState.RUNNING, TodoState.WAITING, TodoState.TAKEN_OVER} and run is not None:
            process = self.processes.get(run.run_id)
            if process is not None and process.turn_id is not None:
                # 只有进程还存活且已有 turn 时才投递，否则留在待投递队列。
                ack = await process.steer(body)
                self.store.mark_notes_delivered([note.id], ack)
                delivery = "steered"
        self._changed()
        return {"note_id": note.id, "delivery": delivery}

    async def start_todo(
        self,
        todo_id: str,
        workspace_dir: Path | str,
    ) -> dict[str, Any]:
        """启动待办：优先恢复已有 thread，否则在给定工作目录启动 Codex。"""
        existing = self.store.latest_run(todo_id)
        pending_notes = self.store.pending_delivery_notes(todo_id)
        if existing is not None and existing.thread_id and any(note.kind == "followup" for note in pending_notes):
            # 有 followup 且存在可恢复 thread 时沿用同一会话。
            return await self._start_existing_thread(todo_id, existing, pending_notes)
        workspace = validate_cwd(Path(workspace_dir))
        self.store.record_workspace(todo_id, workspace)
        todo = self.store.get_todo(todo_id)
        run_id = str(uuid4())
        self.store.bind_run(todo_id, todo.version, run_id, workspace)
        try:
            # 先连接 Codex，再绑定 thread 和 turn，失败时数据库标记 error。
            process = await spawn_app_server(
                self.settings,
                self.settings.runtime_dir / run_id,
                todo_id,
                run_id,
                self.ask_token,
                lambda message: self._codex_event(run_id, message),
            )
            self.processes[run_id] = process
            self._watch_process(run_id, process)
            self.store.update_run(run_id, pid=process.process.pid, endpoint=process.endpoint, status="connected")
            thread_id = await process.start_thread(workspace)
            self.store.update_run(run_id, thread_id=thread_id)
            todo = self.store.get_todo(todo_id)
            additions = self.store.pending_delivery_notes(todo_id)
            prompt = todo.body
            if additions:
                prompt += "\n\n用户追加：\n" + "\n".join(note.body for note in additions)
            turn_id = await process.start_turn(prompt)
            self.store.update_run(run_id, turn_id=turn_id, status="running")
            self.store.mark_notes_delivered([note.id for note in additions], turn_id)
        except Exception as error:
            # 启动失败要完整记录，并让待办进入异常状态。
            failure = {"error_type": type(error).__name__, "error": str(error), "stack": traceback.format_exc()}
            self.logger.write("codex_start_error", {"run_id": run_id, "todo_id": todo_id, "error": failure})
            self.store.update_run(run_id, status="error", error_json=failure)
            current = self.store.get_todo(todo_id)
            if current.state is TodoState.RUNNING:
                self.store.transition(todo_id, TodoState.ERROR, current.version, str(error))
            self._changed()
            raise
        self._changed()
        return {"run_id": run_id, "thread_id": thread_id, "turn_id": turn_id, "workspace": str(workspace), "endpoint": process.endpoint}

    async def suspend(self, todo_id: str) -> dict[str, Any]:
        """挂起待办；运行中的先停止并等待停止完成。"""
        return await self._stop_and_finalize(todo_id, TodoState.SUSPENDED)

    async def archive(self, todo_id: str) -> dict[str, Any]:
        """归档待办；运行中的先停止并等待停止完成。"""
        return await self._stop_and_finalize(todo_id, TodoState.ARCHIVED)

    async def complete(self, todo_id: str) -> dict[str, Any]:
        """把待办切换为已完成；运行中的先停止并等待停止完成。"""
        return await self._stop_and_finalize(todo_id, TodoState.COMPLETED)

    async def _stop_and_finalize(self, todo_id: str, target: TodoState) -> dict[str, Any]:
        """停止活动待办并执行最终状态转换。"""
        todo = self.store.get_todo(todo_id)
        if todo.state is TodoState.ARCHIVED:
            return {"state": TodoState.ARCHIVED, "clean_stop": True}
        if todo.state is target:
            return {"state": todo.state, "clean_stop": True}
        if todo.state not in {TodoState.RUNNING, TodoState.WAITING, TodoState.STOPPING}:
            result = self.store.transition(todo_id, target, todo.version)
            self._changed()
            return {"state": result.state, "clean_stop": True}

        run = self.store.latest_run(todo_id)
        process = self.processes.get(run.run_id) if run else None
        clean = True
        if todo.state in {TodoState.RUNNING, TodoState.WAITING}:
            self.store.transition(todo_id, TodoState.STOPPING, todo.version, pending_action=target.value)
        elif todo.state is TodoState.STOPPING and todo.pending_action != target.value:
            self.store.set_pending_action(todo_id, target.value, todo.version)
        if process is not None:
            clean = await process.stop_gracefully(30)

        current = self.store.get_todo(todo_id)
        if current.state is TodoState.STOPPING:
            result = self.store.transition(
                todo_id,
                target,
                current.version,
                "已安全停止" if clean else "已强制中断，可能需要检查残留资源",
            )
        elif current.state is not target:
            if current.state in {TodoState.RUNNING, TodoState.WAITING}:
                self.store.transition(todo_id, TodoState.STOPPING, current.version, pending_action=target.value)
                current = self.store.get_todo(todo_id)
            result = self.store.transition(todo_id, target, current.version)
        else:
            result = current
        if run:
            self.store.update_run(run.run_id, status=target.value, clean_stop=int(clean))
        self._changed()
        return {"state": result.state, "clean_stop": clean}

    async def takeover(self, todo_id: str) -> dict[str, Any]:
        """把待办交给人工接管：生成 resume 命令并打开 VS Code。"""
        todo = self.store.get_todo(todo_id)
        if todo.state not in {TodoState.PENDING, TodoState.SUSPENDED, TodoState.WAITING, TodoState.RUNNING}:
            raise ValueError(f"cannot take over todo in {todo.state}")
        claimed = self.store.transition(todo_id, TodoState.TAKEN_OVER, todo.version)
        run = self.store.latest_run(todo_id)
        workspace = Path(claimed.workspace) if claimed.workspace else None
        if workspace is None:
            raise ValueError("todo has no workspace; run orchestrator to create one first")
        workspace = validate_cwd(workspace)
        command = None
        if run and run.thread_id and run.endpoint:
            command = shlex.join([str(self.settings.codex_path), "resume", "--remote", run.endpoint, "-C", str(workspace), run.thread_id])
            # 命令写入剪贴板，用户可直接在终端恢复同一 Codex thread。
            await self._copy_to_clipboard(command)
        code = shutil.which("code")
        if code:
            await asyncio.create_subprocess_exec(code, "-n", str(workspace))
        self._changed()
        return {"state": TodoState.TAKEN_OVER, "workspace": str(workspace), "command": command, "copied": command is not None}

    def reset_completed(self, todo_id: str) -> Todo:
        """把已完成待办退回待完成状态。"""
        todo = self.store.get_todo(todo_id)
        result = self.store.transition(todo_id, TodoState.PENDING, todo.version)
        self._changed()
        return result

    async def retry_error(self, todo_id: str) -> dict[str, Any]:
        """把异常待办退回待完成并立即重新启动。"""
        todo = self.store.get_todo(todo_id)
        if not todo.workspace:
            raise ValueError("todo has no workspace")
        self.store.transition(todo_id, TodoState.PENDING, todo.version)
        return await self.start_todo(todo_id, todo.workspace)

    async def resume_completed(self, todo_id: str, instruction: str) -> dict[str, Any]:
        """接收验收反馈：加入 followup，并尽量沿用原 thread 继续执行。"""
        todo = self.store.get_todo(todo_id)
        if todo.state is not TodoState.COMPLETED:
            raise ValueError("todo is not completed")
        self.store.transition(todo_id, TodoState.PENDING, todo.version)
        self.store.append_note(todo_id, instruction, kind="followup")
        run = self.store.latest_run(todo_id)
        if self.store.automatic_lease() is not None or run is None or run.thread_id is None:
            # 自动槽被占用或没有可恢复 thread 时，先提高优先级排队等待。
            self.store.prioritize(todo_id)
            self._changed()
            return {"state": TodoState.PENDING, "delivery": "prioritized"}
        result = await self._start_existing_thread(todo_id, run, self.store.pending_delivery_notes(todo_id))
        result["delivery"] = "same_thread"
        return result

    async def _start_existing_thread(self, todo_id: str, run: Any, notes: list[Any]) -> dict[str, Any]:
        """恢复已有 Codex thread，并把待投递笔记作为新一轮输入。"""
        process = self.processes.get(run.run_id)
        if process is None:
            # 原 app-server 已退出时重新拉起同一运行目录。
            process = await spawn_app_server(
                self.settings,
                self.settings.runtime_dir / run.run_id,
                todo_id,
                run.run_id,
                self.ask_token,
                lambda message: self._codex_event(run.run_id, message),
            )
            await process.resume_thread(run.thread_id, Path(run.workspace))
            self.processes[run.run_id] = process
            self._watch_process(run.run_id, process)
            self.store.update_run(run.run_id, pid=process.process.pid, endpoint=process.endpoint)
        todo = self.store.get_todo(todo_id)
        self.store.resume_existing_run(todo_id, todo.version, run.run_id)
        prompt = "\n".join(note.body for note in notes)
        try:
            turn_id = await process.start_turn(prompt)
            self.store.mark_notes_delivered([note.id for note in notes], turn_id)
            self.store.update_run(run.run_id, turn_id=turn_id, status="running")
        except Exception as error:
            current = self.store.get_todo(todo_id)
            if current.state is TodoState.RUNNING:
                self.store.transition(todo_id, TodoState.ERROR, current.version, str(error))
            raise
        self._changed()
        return {"run_id": run.run_id, "thread_id": run.thread_id, "turn_id": turn_id, "workspace": run.workspace, "endpoint": process.endpoint, "resumed": True}

    def _watch_process(self, run_id: str, process: CodexProcess) -> None:
        """为 Codex 子进程创建退出监视任务。"""
        if not hasattr(process.process, "wait"):
            return
        task = asyncio.create_task(self._monitor_process(run_id, process))
        self._process_tasks.add(task)
        task.add_done_callback(self._process_tasks.discard)

    async def _monitor_process(self, run_id: str, process: CodexProcess) -> None:
        """进程意外退出时，把运行中的待办标记为异常或完成挂起。"""
        return_code = await process.process.wait()
        if self.stop_event.is_set():
            # 正常关闭流程不再修改状态。
            return
        try:
            run = self.store.get_run(run_id)
            todo = self.store.get_todo(run.todo_id)
            detail = f"Codex app-server exited with code {return_code}"
            if todo.state is TodoState.STOPPING:
                target = TodoState(todo.pending_action or "suspended")
                self.store.transition(todo.id, target, todo.version, detail)
                self.store.update_run(run_id, status=target.value, clean_stop=0)
            elif todo.state in {TodoState.RUNNING, TodoState.WAITING}:
                self.store.transition(todo.id, TodoState.ERROR, todo.version, detail)
                self.store.update_run(run_id, status="error", error_json={"return_code": return_code})
            self._changed()
        except (KeyError, ConflictError):
            return

    async def _question_shown(self, question: Question) -> None:
        """问题实际展示后，把运行中的待办切到等待用户。"""
        if not question.todo_id:
            return
        try:
            todo = self.store.get_todo(question.todo_id)
            if todo.state is TodoState.RUNNING:
                self.store.transition(todo.id, TodoState.WAITING, todo.version, question.prompt)
                self._changed()
        except (KeyError, ConflictError):
            return

    async def _question_answered(self, question: Question, answer: Answer) -> None:
        """问题得到回答后，把等待用户的待办切回运行中。"""
        if not question.todo_id:
            return
        try:
            todo = self.store.get_todo(question.todo_id)
            if todo.state is TodoState.WAITING:
                self.store.transition(todo.id, TodoState.RUNNING, todo.version)
                self._changed()
        except (KeyError, ConflictError):
            return

    async def _codex_event(self, run_id: str, message: dict[str, Any]) -> None:
        """处理 Codex 事件：持久化原始事件并更新待办和运行状态。"""
        method = message.get("method", "unknown")
        params = message.get("params") or {}
        detail = None
        if method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage":
                detail = item.get("text")
        elif method == "turn/plan/updated":
            detail = json.dumps(params.get("plan"), ensure_ascii=False, separators=(",", ":"))
        self.logger.write("codex_event", {"run_id": run_id, "message": message})
        self.store.record_codex_event(run_id, method, message, detail)
        run = self.store.get_run(run_id)
        if method == "turn/started":
            turn = params.get("turn") or {}
            if turn.get("id"):
                self.store.update_run(run_id, turn_id=turn["id"], status="running")
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            status = turn.get("status")
            process = self.processes.get(run_id)
            final = process.final_message if process else None
            todo = self.store.get_todo(run.todo_id)
            if todo.state is TodoState.STOPPING:
                # 停止后的目标状态由 pending_action 决定，不触发验收通知。
                target = TodoState(todo.pending_action or "suspended")
                self.store.transition(todo.id, target, todo.version)
                self.store.update_run(run_id, status=target.value, final_message=final)
            elif todo.state in {TodoState.RUNNING, TodoState.WAITING}:
                # 模型侧失败映射为 error，正常完成映射为 completed。
                target = TodoState.ERROR if status in {"failed", "error"} else TodoState.COMPLETED
                completed = self.store.transition(todo.id, target, todo.version, final)
                self.store.update_run(run_id, status=target, final_message=final)
                if completed.state is TodoState.COMPLETED:
                    # 完成后异步发送验收通知，不阻塞事件循环。
                    task = asyncio.create_task(self._completion_notice(completed))
                    self._notification_tasks.add(task)
                    task.add_done_callback(self._notification_tasks.discard)
            elif todo.state is TodoState.TAKEN_OVER:
                self.store.update_run(run_id, status="turn_completed", final_message=final)
        self._changed()

    async def _completion_notice(self, todo: Todo) -> None:
        """向用户发送验收通知，自由回答可作为 followup 继续执行。"""
        answer = await self.broker.ask(
            Question(
                prompt="Codex 已完成这项待办，请验收。",
                options=(Option("稍后验收", "保持已完成状态"),),
                recommended=0,
                todo_id=todo.id,
                kind="notification",
            )
        )
        if answer.other and answer.other.strip():
            await self.resume_completed(todo.id, answer.other)

    async def _copy_to_clipboard(self, text: str) -> None:
        """在 macOS 上通过 pbcopy 把接管命令复制到剪贴板。"""
        pbcopy = shutil.which("pbcopy")
        if not pbcopy:
            return
        process = await asyncio.create_subprocess_exec(pbcopy, stdin=asyncio.subprocess.PIPE)
        await process.communicate(text.encode())

    def _changed(self) -> None:
        """通知外部观察者（例如 GUI）数据已变化。"""
        if self.on_change is not None:
            self.on_change()

    def _tool_definitions(self) -> list[ToolDefinition]:
        """构建调度器模型可以调用的工具列表。"""
        no_args = {"type": "object", "properties": {}, "additionalProperties": False}
        return [
            ToolDefinition(
                "ask_user", "向用户显示问题或动作汇报；一次调用可包含多个问题，每题第一个选项是推荐项。",
                {
                    "type": "object",
                    "required": ["questions"],
                    "properties": {
                        "questions": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "required": ["question", "options"],
                                "properties": {
                                    "question": {"type": "string"},
                                    "options": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string"}},
                                },
                                "additionalProperties": False,
                            },
                        },
                        "todo_id": {"type": ["string", "null"]},
                    },
                    "additionalProperties": False,
                }, self._tool_ask,
            ),
            ToolDefinition("get_todo_list", "读取最新待办、自动槽与会话状态。", no_args, self._tool_get_todos, False),
            ToolDefinition(
                "set_todo_state", "设置待办语义状态 pending 或 suspended；把运行中/等待中的待办设为 suspended 时会先安全停止。",
                {"type": "object", "required": ["todo_id", "state"], "properties": {"todo_id": {"type": "string"}, "state": {"type": "string", "enum": ["pending", "suspended"]}}, "additionalProperties": False},
                self._tool_set_state,
            ),
            ToolDefinition(
                "bash", "在默认工作区根目录执行完整 bash 命令；请先用 cd 进入目标目录并 ls 确认。不得读取或输出凭据。",
                {"type": "object", "required": ["command"], "properties": {"command": {"type": "string"}}, "additionalProperties": False}, self._tool_bash,
            ),
            ToolDefinition(
                "bind_and_start_codex", "原子占用自动槽、绑定 Codex 会话并启动待办；工作目录必须已由 bash 创建。",
                {"type": "object", "required": ["todo_id", "workspace_dir"], "properties": {"todo_id": {"type": "string"}, "workspace_dir": {"type": "string"}}, "additionalProperties": False}, self.start_todo,
            ),
        ]

    async def _tool_ask(self, questions: list[dict[str, Any]], todo_id: str | None = None) -> dict[str, Any]:
        """ask_user 工具实现：顺序展示多个问题并返回全部答案。"""
        answers = []
        for item in questions:
            options = tuple(Option(option, "") for option in item["options"])
            answer = await self.broker.ask(Question(item["question"], options, 0, todo_id=todo_id))
            answers.append({"option": answer.option, "other": answer.other, "timed_out": answer.timed_out})
        return {"answers": answers}

    async def _tool_get_todos(self) -> dict[str, Any]:
        """get_todo_list 工具实现：返回当前最新快照。"""
        return self.orchestrator.snapshot()

    async def _tool_set_state(self, todo_id: str, state: str) -> dict[str, Any]:
        """set_todo_state 工具实现：suspended 走停止流程，pending 只处理非活动待办。"""
        if state == "suspended":
            return await self.suspend(todo_id)
        todo = self.store.get_todo(todo_id)
        if todo.state is TodoState.PENDING:
            return {"id": todo.id, "state": todo.state, "version": todo.version}
        if todo.state in {TodoState.SUSPENDED, TodoState.ERROR, TodoState.COMPLETED}:
            result = self.store.transition(todo_id, TodoState.PENDING, todo.version)
            self._changed()
            return {"id": result.id, "state": result.state, "version": result.version}
        raise ValueError("cannot set running/waiting/stopping todo to pending")

    async def _tool_bash(self, command: str) -> dict[str, Any]:
        """bash 工具实现：在默认工作区根目录执行命令并返回完整输出。"""
        self.settings.managed_root.mkdir(parents=True, exist_ok=True)
        workdir = validate_cwd(self.settings.managed_root)
        shell = login_shell(self.settings)
        prefixed = f"export RELAYNOTE_WORKSPACES={shlex.quote(str(workdir))}; " + command
        process = await asyncio.create_subprocess_exec(
            str(shell), "-lc", prefixed, cwd=workdir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return {"exit_code": process.returncode, "stdout": stdout.decode(errors="replace"), "stderr": stderr.decode(errors="replace")}
