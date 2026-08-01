from __future__ import annotations

import asyncio
import json
import os
import secrets
import shlex
import shutil
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from .ask import Answer, AskBrokerServer, Option, Presenter, Question, QuestionBroker
from .codex import CodexProcess, spawn_app_server
from .config import Settings
from .db import ConflictError, Store
from .logging import JsonlLogger
from .model import Todo, TodoState
from .orchestrator import Orchestrator, ToolDefinition
from .scheduler import BoundaryScheduler
from .workspace import prepare_workspace, validate_cwd


class Runtime:
    def __init__(self, settings: Settings, presenter: Presenter, on_change: Callable[[], None] | None = None) -> None:
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
        self.scheduler = BoundaryScheduler(self.orchestrator.run)
        self.scheduler_task: asyncio.Task[None] | None = None
        self._notification_tasks: set[asyncio.Task[Any]] = set()
        self._process_tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        self._reconcile_stale_lease()
        await self.ask_server.start()
        self.scheduler_task = asyncio.create_task(self.scheduler.run(self.stop_event))

    async def close(self) -> None:
        self.stop_event.set()
        if self.scheduler_task is not None:
            await self.scheduler_task
        lease = self.store.automatic_lease()
        if lease is not None:
            todo = self.store.get_todo(lease[0])
            if todo.state in {TodoState.RUNNING, TodoState.WAITING}:
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
        lease = self.store.automatic_lease()
        if lease is None:
            return
        todo = self.store.get_todo(lease[0])
        if todo.state is TodoState.STOPPING:
            self.store.transition(todo.id, TodoState.SUSPENDED, todo.version, "RelayNote 重启时完成停止状态恢复")
        elif todo.state in {TodoState.RUNNING, TodoState.WAITING}:
            self.store.transition(todo.id, TodoState.ERROR, todo.version, "RelayNote 上次退出后未能重新连接 Codex app-server，请人工重试")

    def create_todo(self, body: str) -> Todo:
        todo = self.store.create_todo(body)
        self._changed()
        return todo

    async def append(self, todo_id: str, body: str) -> dict[str, Any]:
        todo = self.store.get_todo(todo_id)
        note = self.store.append_note(todo_id, body)
        delivery = "queued"
        run = self.store.latest_run(todo_id)
        if todo.state in {TodoState.RUNNING, TodoState.WAITING, TodoState.TAKEN_OVER} and run is not None:
            process = self.processes.get(run.run_id)
            if process is not None and process.turn_id is not None:
                ack = await process.steer(body)
                self.store.mark_notes_delivered([note.id], ack)
                delivery = "steered"
        self._changed()
        return {"note_id": note.id, "delivery": delivery}

    async def start_todo(
        self,
        todo_id: str,
        expected_version: int,
        project: str | None = None,
        dirty_policy: str = "suspend",
    ) -> dict[str, Any]:
        existing = self.store.latest_run(todo_id)
        pending_notes = self.store.pending_delivery_notes(todo_id)
        if existing is not None and existing.thread_id and any(note.kind == "followup" for note in pending_notes):
            return await self._start_existing_thread(todo_id, expected_version, existing, pending_notes)
        workspace = await prepare_workspace(
            todo_id,
            self.settings.managed_root,
            Path(project) if project else None,
            dirty_policy,
        )
        self.store.record_workspace(todo_id, workspace.path, workspace.project_root, workspace.branch, workspace.worktree, dirty_policy)
        run_id = str(uuid4())
        self.store.bind_run(todo_id, expected_version, run_id, workspace.path)
        try:
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
            thread_id = await process.start_thread(workspace.path)
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
            failure = {"error_type": type(error).__name__, "error": str(error), "stack": traceback.format_exc()}
            self.logger.write("codex_start_error", {"run_id": run_id, "todo_id": todo_id, "error": failure})
            self.store.update_run(run_id, status="error", error_json=failure)
            current = self.store.get_todo(todo_id)
            if current.state is TodoState.RUNNING:
                self.store.transition(todo_id, TodoState.ERROR, current.version, str(error))
            self._changed()
            raise
        self._changed()
        return {"run_id": run_id, "thread_id": thread_id, "turn_id": turn_id, "workspace": str(workspace.path), "endpoint": process.endpoint}

    async def suspend(self, todo_id: str) -> dict[str, Any]:
        todo = self.store.get_todo(todo_id)
        if todo.state is TodoState.PENDING:
            result = self.store.transition(todo_id, TodoState.SUSPENDED, todo.version)
            self._changed()
            return {"state": result.state, "clean_stop": True}
        if todo.state is TodoState.WAITING or todo.state is TodoState.RUNNING:
            target = self.store.transition(todo_id, TodoState.STOPPING, todo.version)
        else:
            raise ValueError(f"cannot suspend todo in {todo.state}")
        run = self.store.latest_run(todo_id)
        process = self.processes.get(run.run_id) if run else None
        clean = True
        if process is not None:
            clean = await process.stop_gracefully(30)
        current = self.store.get_todo(todo_id)
        if current.state is TodoState.STOPPING:
            self.store.transition(todo_id, TodoState.SUSPENDED, current.version, "已安全停止" if clean else "已强制中断，可能需要检查残留资源")
        if run:
            self.store.update_run(run.run_id, status="suspended", clean_stop=int(clean))
        self._changed()
        return {"state": target.state, "clean_stop": clean}

    async def takeover(self, todo_id: str) -> dict[str, Any]:
        todo = self.store.get_todo(todo_id)
        if todo.state not in {TodoState.PENDING, TodoState.SUSPENDED, TodoState.WAITING, TodoState.RUNNING}:
            raise ValueError(f"cannot take over todo in {todo.state}")
        claimed = self.store.transition(todo_id, TodoState.TAKEN_OVER, todo.version)
        run = self.store.latest_run(todo_id)
        workspace = Path(claimed.workspace) if claimed.workspace else None
        if workspace is None:
            prepared = await prepare_workspace(todo_id, self.settings.managed_root)
            workspace = prepared.path
            self.store.record_workspace(todo_id, workspace, prepared.project_root, prepared.branch, prepared.worktree, None)
        command = None
        if run and run.thread_id and run.endpoint:
            command = shlex.join([str(self.settings.codex_path), "resume", "--remote", run.endpoint, "-C", str(workspace), run.thread_id])
            await self._copy_to_clipboard(command)
        code = shutil.which("code")
        if code:
            await asyncio.create_subprocess_exec(code, "-n", str(workspace))
        self._changed()
        return {"state": TodoState.TAKEN_OVER, "workspace": str(workspace), "command": command, "copied": command is not None}

    def archive(self, todo_id: str) -> Todo:
        todo = self.store.get_todo(todo_id)
        result = self.store.transition(todo_id, TodoState.ARCHIVED, todo.version)
        self._changed()
        return result

    def reset_completed(self, todo_id: str) -> Todo:
        todo = self.store.get_todo(todo_id)
        result = self.store.transition(todo_id, TodoState.PENDING, todo.version)
        self._changed()
        return result

    async def retry_error(self, todo_id: str) -> dict[str, Any]:
        todo = self.store.get_todo(todo_id)
        pending = self.store.transition(todo_id, TodoState.PENDING, todo.version)
        return await self.start_todo(todo_id, pending.version, project=todo.workspace, dirty_policy="original")

    async def resume_completed(self, todo_id: str, instruction: str) -> dict[str, Any]:
        todo = self.store.get_todo(todo_id)
        if todo.state is not TodoState.COMPLETED:
            raise ValueError("todo is not completed")
        self.store.transition(todo_id, TodoState.PENDING, todo.version)
        self.store.append_note(todo_id, instruction, kind="followup")
        run = self.store.latest_run(todo_id)
        if self.store.automatic_lease() is not None or run is None or run.thread_id is None:
            self.store.prioritize(todo_id)
            self._changed()
            return {"state": TodoState.PENDING, "delivery": "prioritized"}
        result = await self._start_existing_thread(
            todo_id,
            self.store.get_todo(todo_id).version,
            run,
            self.store.pending_delivery_notes(todo_id),
        )
        result["delivery"] = "same_thread"
        return result

    async def _start_existing_thread(self, todo_id: str, expected_version: int, run: Any, notes: list[Any]) -> dict[str, Any]:
        process = self.processes.get(run.run_id)
        if process is None:
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
        self.store.resume_existing_run(todo_id, expected_version, run.run_id)
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
        if not hasattr(process.process, "wait"):
            return
        task = asyncio.create_task(self._monitor_process(run_id, process))
        self._process_tasks.add(task)
        task.add_done_callback(self._process_tasks.discard)

    async def _monitor_process(self, run_id: str, process: CodexProcess) -> None:
        return_code = await process.process.wait()
        if self.stop_event.is_set():
            return
        try:
            run = self.store.get_run(run_id)
            todo = self.store.get_todo(run.todo_id)
            detail = f"Codex app-server exited with code {return_code}"
            if todo.state is TodoState.STOPPING:
                self.store.transition(todo.id, TodoState.SUSPENDED, todo.version, detail)
                self.store.update_run(run_id, status="suspended", clean_stop=0)
            elif todo.state in {TodoState.RUNNING, TodoState.WAITING}:
                self.store.transition(todo.id, TodoState.ERROR, todo.version, detail)
                self.store.update_run(run_id, status="error", error_json={"return_code": return_code})
            self._changed()
        except (KeyError, ConflictError):
            return

    async def _question_shown(self, question: Question) -> None:
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
                self.store.transition(todo.id, TodoState.SUSPENDED, todo.version)
                self.store.update_run(run_id, status="suspended", final_message=final)
            elif todo.state in {TodoState.RUNNING, TodoState.WAITING}:
                target = TodoState.ERROR if status in {"failed", "error"} else TodoState.COMPLETED
                completed = self.store.transition(todo.id, target, todo.version, final)
                self.store.update_run(run_id, status=target, final_message=final)
                if completed.state is TodoState.COMPLETED:
                    task = asyncio.create_task(self._completion_notice(completed))
                    self._notification_tasks.add(task)
                    task.add_done_callback(self._notification_tasks.discard)
            elif todo.state is TodoState.TAKEN_OVER:
                self.store.update_run(run_id, status="turn_completed", final_message=final)
        self._changed()

    async def _completion_notice(self, todo: Todo) -> None:
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
        pbcopy = shutil.which("pbcopy")
        if not pbcopy:
            return
        process = await asyncio.create_subprocess_exec(pbcopy, stdin=asyncio.subprocess.PIPE)
        await process.communicate(text.encode())

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def _tool_definitions(self) -> list[ToolDefinition]:
        no_args = {"type": "object", "properties": {}, "additionalProperties": False}
        return [
            ToolDefinition(
                "ask_user", "向用户显示一个问题或动作汇报；每次只问一个问题。",
                {
                    "type": "object",
                    "required": ["question", "options", "recommended"],
                    "properties": {
                        "question": {"type": "string"},
                        "options": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "object", "required": ["label", "description"], "properties": {"label": {"type": "string"}, "description": {"type": "string"}}, "additionalProperties": False}},
                        "recommended": {"type": "integer"},
                        "todo_id": {"type": ["string", "null"]},
                    },
                    "additionalProperties": False,
                }, self._tool_ask,
            ),
            ToolDefinition("get_todo_list", "读取最新待办、自动槽与会话状态。", no_args, self._tool_get_todos, False),
            ToolDefinition(
                "set_todo_state", "只设置语义状态 pending 或 suspended；运行状态由真实 Codex 事件维护。",
                {"type": "object", "required": ["todo_id", "state", "expected_version"], "properties": {"todo_id": {"type": "string"}, "state": {"type": "string", "enum": ["pending", "suspended"]}, "expected_version": {"type": "integer"}}, "additionalProperties": False},
                self._tool_set_state,
            ),
            ToolDefinition("suspend_active_todo", "安全收尾并挂起当前自动任务。", no_args, self._tool_suspend_active),
            ToolDefinition(
                "bash", "在明确的绝对工作目录执行完整 bash 命令。不得读取或输出凭据。",
                {"type": "object", "required": ["command", "cwd"], "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}}, "additionalProperties": False}, self._tool_bash,
            ),
            ToolDefinition(
                "prepare_workspace", "为待办准备隔离工作区；脏仓库必须依据用户选择指定策略。",
                {"type": "object", "required": ["todo_id", "project", "dirty_policy"], "properties": {"todo_id": {"type": "string"}, "project": {"type": ["string", "null"]}, "dirty_policy": {"type": "string", "enum": ["suspend", "head", "original"]}}, "additionalProperties": False}, self._tool_prepare_workspace,
            ),
            ToolDefinition(
                "bind_and_start_codex", "原子占用自动槽、绑定 Codex 会话并启动待办。",
                {"type": "object", "required": ["todo_id", "expected_version", "project", "dirty_policy"], "properties": {"todo_id": {"type": "string"}, "expected_version": {"type": "integer"}, "project": {"type": ["string", "null"]}, "dirty_policy": {"type": "string", "enum": ["suspend", "head", "original"]}}, "additionalProperties": False}, self.start_todo,
            ),
        ]

    async def _tool_ask(self, question: str, options: list[dict[str, str]], recommended: int, todo_id: str | None = None) -> dict[str, Any]:
        answer = await self.broker.ask(Question(question, tuple(Option(item["label"], item["description"]) for item in options), recommended, todo_id=todo_id))
        return {"option": answer.option, "other": answer.other, "timed_out": answer.timed_out}

    async def _tool_get_todos(self) -> dict[str, Any]:
        return self.orchestrator.snapshot()

    async def _tool_set_state(self, todo_id: str, state: str, expected_version: int) -> dict[str, Any]:
        todo = self.store.transition(todo_id, TodoState(state), expected_version)
        self._changed()
        return {"id": todo.id, "state": todo.state, "version": todo.version}

    async def _tool_suspend_active(self) -> dict[str, Any]:
        lease = self.store.automatic_lease()
        if lease is None:
            return {"state": "idle"}
        return await self.suspend(lease[0])

    async def _tool_bash(self, command: str, cwd: str) -> dict[str, Any]:
        workdir = validate_cwd(Path(cwd))
        env = os.environ.copy()
        env.update(self.settings.network_env())
        process = await asyncio.create_subprocess_exec(
            "/bin/bash", "-lc", command, cwd=workdir, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return {"exit_code": process.returncode, "stdout": stdout.decode(errors="replace"), "stderr": stderr.decode(errors="replace")}

    async def _tool_prepare_workspace(self, todo_id: str, project: str | None, dirty_policy: str) -> dict[str, Any]:
        workspace = await prepare_workspace(todo_id, self.settings.managed_root, Path(project) if project else None, dirty_policy)
        self.store.record_workspace(todo_id, workspace.path, workspace.project_root, workspace.branch, workspace.worktree, dirty_policy)
        return {"path": str(workspace.path), "branch": workspace.branch, "worktree": workspace.worktree, "project_root": str(workspace.project_root) if workspace.project_root else None}
