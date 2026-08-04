"""Codex app-server 进程、v2 JSON-RPC 会话与事件消费管理。"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import socket
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets

from .config import Settings
from .ipc import safe_unix_socket_path
from .jsonrpc import JsonRpcConnection, JsonRpcWebSocket
from .workspace import validate_cwd

DEVELOPER_INSTRUCTIONS = """You are operating one RelayNote todo. Work only in the provided absolute workspace. Preserve all existing user changes. Ask blocking questions exclusively through the relaynote_ask.ask_user MCP tool; one call may contain multiple questions, and each question's first option is the recommended option. Never invoke a built-in question tool. Treat appended instructions as part of this todo. Send concise commentary while working and finish with a complete outcome report."""


class CodexVersionError(RuntimeError):
    """本机 Codex CLI 版本与 RelayNote 要求的版本不一致。"""



EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class CodexProcess:
    """包装一个 Codex app-server 子进程及其 JSON-RPC 会话状态。"""

    process: asyncio.subprocess.Process
    rpc: JsonRpcConnection
    endpoint: str
    event_handler: EventHandler | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    final_message: str | None = None
    _events_task: asyncio.Task[None] | None = None
    _stream_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _turn_finished: dict[str, asyncio.Event] = field(default_factory=dict)
    _message_parts: dict[str, list[str]] = field(default_factory=dict)

    async def initialize(self) -> dict[str, Any]:
        """完成 JSON-RPC initialize 握手并启动事件消费任务。"""
        result = await self.rpc.request(
            "initialize",
            {
                "clientInfo": {"name": "RelayNote", "title": "RelayNote", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.rpc.notify("initialized")
        self._events_task = asyncio.create_task(self._consume_events())
        return result

    async def start_thread(self, cwd: Path) -> str:
        """为待办启动一个新的 Codex thread，返回 thread id。"""
        result = await self.rpc.request(
            "thread/start",
            {
                "cwd": str(validate_cwd(cwd)),
                "developerInstructions": DEVELOPER_INSTRUCTIONS,
                "sandbox": "danger-full-access",
                "approvalPolicy": "never",
                "ephemeral": False,
                "serviceName": "RelayNote",
            },
        )
        self.thread_id = result["thread"]["id"]
        return self.thread_id

    async def resume_thread(self, thread_id: str, cwd: Path | None = None) -> str:
        """恢复已有 thread，并读取当前活动 turn 作为上下文。"""
        params: dict[str, Any] = {
            "threadId": thread_id,
            "sandbox": "danger-full-access",
            "approvalPolicy": "never",
            "developerInstructions": DEVELOPER_INSTRUCTIONS,
            "excludeTurns": True,
        }
        if cwd is not None:
            params["cwd"] = str(validate_cwd(cwd))
        result = await self.rpc.request("thread/resume", params)
        self.thread_id = result["thread"]["id"]
        active_turn = result["thread"].get("activeTurn")
        if active_turn:
            self.turn_id = active_turn.get("id")
        return self.thread_id

    async def start_turn(self, text: str) -> str:
        """在当前 thread 中启动一轮处理，返回 turn id。"""
        if self.thread_id is None:
            raise RuntimeError("thread is not started")
        result = await self.rpc.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": text}],
                "approvalPolicy": "never",
            },
        )
        self.turn_id = result["turn"]["id"]
        self._turn_finished[self.turn_id] = asyncio.Event()
        return self.turn_id

    async def steer(self, text: str, expected_turn_id: str | None = None) -> str:
        """向运行中的 turn 注入新指令并返回最新 turn id。"""
        thread_id = self.thread_id
        turn_id = expected_turn_id or self.turn_id
        if thread_id is None or turn_id is None:
            raise RuntimeError("there is no active turn")
        result = await self.rpc.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": text}],
            },
        )
        return result["turnId"]

    async def interrupt(self, turn_id: str | None = None) -> None:
        """请求 Codex 立即中断当前 turn。"""
        thread_id = self.thread_id
        target = turn_id or self.turn_id
        if thread_id is None or target is None:
            raise RuntimeError("there is no active turn")
        await self.rpc.request("turn/interrupt", {"threadId": thread_id, "turnId": target})

    async def stop_gracefully(self, timeout: float = 30) -> bool:
        """先要求 Codex 安全收尾；超时后再强制中断。"""
        target = self.turn_id
        if target is None:
            return True
        await self.steer("停止开始任何新工作；安全释放你启动的资源，然后立即汇报当前状态并结束本轮。", target)
        event = self._turn_finished.setdefault(target, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except TimeoutError:
            await self.interrupt(target)
            return False

    async def wait_turn(self, turn_id: str | None = None) -> dict[str, Any]:
        """等待指定 turn 完成，返回 thread、turn 和最终消息。"""
        target = turn_id or self.turn_id
        if target is None:
            raise RuntimeError("there is no active turn")
        await self._turn_finished.setdefault(target, asyncio.Event()).wait()
        return {"thread_id": self.thread_id, "turn_id": target, "final_message": self.final_message}

    async def close(self, terminate: bool = True) -> None:
        """关闭 RPC、取消后台任务，并在需要时终止子进程。"""
        await self.rpc.close()
        if self._events_task is not None and not self._events_task.done():
            self._events_task.cancel()
        if terminate and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in self._stream_tasks:
            if not task.done():
                task.cancel()

    async def _consume_events(self) -> None:
        """消费服务端事件，更新 turn 状态并组装最终 agent 消息。"""
        async for message in self.rpc.messages():
            method = message.get("method")
            params = message.get("params") or {}
            if method == "turn/started":
                # turn 可能由服务端自行启动，因此事件也要登记完成事件。
                turn = params.get("turn") or {}
                self.turn_id = turn.get("id", self.turn_id)
                if self.turn_id:
                    self._turn_finished.setdefault(self.turn_id, asyncio.Event())
            elif method == "item/agentMessage/delta":
                # 消息分片先暂存，等 item 完成时再拼成完整文本。
                item_id = params.get("itemId")
                if item_id:
                    self._message_parts.setdefault(item_id, []).append(params.get("delta", ""))
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage":
                    text = item.get("text") or "".join(self._message_parts.pop(item.get("id"), []))
                    if text:
                        self.final_message = text
            elif method == "turn/completed":
                # 设置完成事件，唤醒等待该 turn 的调用方。
                turn = params.get("turn") or {}
                completed_id = turn.get("id") or self.turn_id
                if completed_id:
                    self._turn_finished.setdefault(completed_id, asyncio.Event()).set()
            if self.event_handler is not None:
                await self.event_handler(message)

    async def drain_process_stream(self, name: str, stream: asyncio.StreamReader) -> None:
        """把子进程 stdout/stderr 原样转发为事件，供日志与诊断使用。"""
        while chunk := await stream.readline():
            if self.event_handler is not None:
                await self.event_handler(
                    {"method": f"process/{name}", "params": {"raw": chunk.decode(errors="replace")}}
                )


async def codex_version(path: Path) -> str:
    """运行 codex --version 并返回版本字符串。"""
    process = await asyncio.create_subprocess_exec(
        str(path), "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        raise RuntimeError(stderr.decode(errors="replace"))
    return stdout.decode().strip()


def login_shell(settings: Settings) -> Path:
    """返回用于加载用户全局环境的登录 shell。"""
    if settings.shell_path is not None:
        return settings.shell_path
    configured = os.environ.get("SHELL")
    if configured:
        return Path(configured)
    return Path("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")


def ask_mcp_command() -> tuple[str, list[str]]:
    """返回项目自带 Ask MCP 的命令和参数，绝不使用外部配置。"""
    executable_dir = Path(sys.executable).resolve().parent
    for candidate in ("ask_mcp", "ask_mcp.py"):
        path = executable_dir / candidate
        if path.is_file():
            return str(path), []
    script = shutil.which("relaynote-ask")
    if script:
        return script, []
    return str(Path(sys.executable).resolve()), ["-m", "relaynote.ask_mcp"]


def _loopback_port() -> int:
    """绑定临时端口获取空闲 loopback 端口号。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return int(port)


async def _connect(endpoint: str, socket_path: Path | None = None) -> JsonRpcWebSocket:
    """按端点类型连接 Unix socket 或 loopback WebSocket。"""
    if socket_path is not None:
        websocket = await websockets.unix_connect(str(socket_path), uri="ws://localhost", proxy=None, max_size=None)
    else:
        websocket = await websockets.connect(endpoint, proxy=None, max_size=None)
    return JsonRpcWebSocket(websocket)


async def spawn_app_server(
    settings: Settings,
    runtime_dir: Path,
    todo_id: str,
    run_id: str,
    ask_token: str,
    event_handler: EventHandler | None = None,
    prefer_unix: bool = True,
) -> CodexProcess:
    """启动一个与待办绑定的 Codex app-server，并完成初始化握手。"""
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cwd = validate_cwd(runtime_dir)
    codex = settings.codex_path
    executable = str(codex) if codex.is_file() else shutil.which(str(codex))
    if executable is None:
        raise FileNotFoundError(f"Codex executable not found: {codex}")
    actual_version = await codex_version(Path(executable))
    if actual_version != settings.expected_codex_version:
        raise CodexVersionError(f"Codex version is {actual_version}; RelayNote requires {settings.expected_codex_version}")
    ask_command, ask_args = ask_mcp_command()
    common = [
        executable,
        "app-server",
        "--disable", "apps",
        "-c", f"mcp_servers.relaynote_ask.command={json.dumps(str(ask_command))}",
        "-c", f"mcp_servers.relaynote_ask.args={json.dumps(ask_args)}",
        "-c", 'mcp_servers.relaynote_ask.enabled=true',
        "-c", 'mcp_servers.relaynote_ask.required=true',
        "-c", 'mcp_servers.relaynote_ask.enabled_tools=["ask_user"]',
        "-c", 'mcp_servers.relaynote_ask.env_vars=["RELAYNOTE_TODO_ID","RELAYNOTE_RUN_ID","RELAYNOTE_ASK_SOCKET","RELAYNOTE_ASK_TOKEN"]',
        "-c", "mcp_servers.relaynote_ask.startup_timeout_sec=30",
        "-c", "mcp_servers.relaynote_ask.tool_timeout_sec=86400",
    ]

    socket_path: Path | None
    if prefer_unix:
        # Unix socket 路径短、更安全；Linux/macOS 上优先使用。
        socket_path = safe_unix_socket_path(cwd / "codex.sock", f"codex-{run_id}")
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        endpoint = f"unix://{socket_path}"
    else:
        # loopback WebSocket 作为兼容备选，端口随机选择。
        socket_path = None
        endpoint = f"ws://127.0.0.1:{_loopback_port()}"
    shell = login_shell(settings)
    relaynote_env = {
        "RELAYNOTE_TODO_ID": todo_id,
        "RELAYNOTE_RUN_ID": run_id,
        "RELAYNOTE_ASK_SOCKET": str(settings.ask_socket),
        "RELAYNOTE_ASK_TOKEN": ask_token,
    }
    exports = " ".join(f"export {name}={shlex.quote(value)};" for name, value in relaynote_env.items())
    command = exports + " exec " + shlex.join(common) + " --listen " + shlex.quote(endpoint)
    process = await asyncio.create_subprocess_exec(
        str(shell), "-lc", command, cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    rpc: JsonRpcWebSocket | None = None
    for _ in range(200):
        try:
            rpc = await _connect(endpoint, socket_path)
            break
        except (OSError, TimeoutError, websockets.exceptions.WebSocketException):
            if process.returncode is not None:
                # 进程提前退出时把完整输出作为错误返回，方便定位启动失败原因。
                stdout = await process.stdout.read() if process.stdout else b""
                stderr = await process.stderr.read() if process.stderr else b""
                raise RuntimeError(json.dumps({"stdout": stdout.decode(errors="replace"), "stderr": stderr.decode(errors="replace")}, ensure_ascii=False))
            await asyncio.sleep(0.05)
    if rpc is None:
        process.terminate()
        await process.wait()
        if prefer_unix:
            # Unix 连接超时可能是平台限制，回退到 loopback 再试一次。
            return await spawn_app_server(settings, runtime_dir, todo_id, run_id, ask_token, event_handler, prefer_unix=False)
        raise TimeoutError("Codex app-server endpoint was not ready")
    instance = CodexProcess(process, rpc, endpoint, event_handler)
    if process.stdout is not None:
        # 后台消费子进程输出，避免管道缓冲区占满阻塞 Codex。
        instance._stream_tasks.append(asyncio.create_task(instance.drain_process_stream("stdout", process.stdout)))
    if process.stderr is not None:
        instance._stream_tasks.append(asyncio.create_task(instance.drain_process_stream("stderr", process.stderr)))
    try:
        await instance.initialize()
    except BaseException:
        # 握手失败时清理已启动的进程和 socket，不留孤儿进程。
        await instance.close()
        raise
    return instance
