from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets

from .config import Settings
from .jsonrpc import JsonRpcConnection, JsonRpcWebSocket
from .workspace import validate_cwd

DEVELOPER_INSTRUCTIONS = """You are operating one RelayNote todo. Work only in the provided absolute workspace. Preserve all existing user changes. Ask blocking questions exclusively through the relaynote_ask.ask_user MCP tool, one question per call, with one recommended option. Never invoke a built-in question tool. Treat appended instructions as part of this todo. Send concise commentary while working and finish with a complete outcome report."""
SAFE_MCP_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class CodexVersionError(RuntimeError):
    pass


EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class CodexProcess:
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
        thread_id = self.thread_id
        target = turn_id or self.turn_id
        if thread_id is None or target is None:
            raise RuntimeError("there is no active turn")
        await self.rpc.request("turn/interrupt", {"threadId": thread_id, "turnId": target})

    async def stop_gracefully(self, timeout: float = 30) -> bool:
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
        target = turn_id or self.turn_id
        if target is None:
            raise RuntimeError("there is no active turn")
        await self._turn_finished.setdefault(target, asyncio.Event()).wait()
        return {"thread_id": self.thread_id, "turn_id": target, "final_message": self.final_message}

    async def close(self, terminate: bool = True) -> None:
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
        async for message in self.rpc.messages():
            method = message.get("method")
            params = message.get("params") or {}
            if method == "turn/started":
                turn = params.get("turn") or {}
                self.turn_id = turn.get("id", self.turn_id)
                if self.turn_id:
                    self._turn_finished.setdefault(self.turn_id, asyncio.Event())
            elif method == "item/agentMessage/delta":
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
                turn = params.get("turn") or {}
                completed_id = turn.get("id") or self.turn_id
                if completed_id:
                    self._turn_finished.setdefault(completed_id, asyncio.Event()).set()
            if self.event_handler is not None:
                await self.event_handler(message)

    async def drain_process_stream(self, name: str, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.readline():
            if self.event_handler is not None:
                await self.event_handler(
                    {"method": f"process/{name}", "params": {"raw": chunk.decode(errors="replace")}}
                )


async def codex_version(path: Path) -> str:
    process = await asyncio.create_subprocess_exec(
        str(path), "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        raise RuntimeError(stderr.decode(errors="replace"))
    return stdout.decode().strip()


async def _configured_mcp_names(codex: Path, env: dict[str, str]) -> list[str]:
    process = await asyncio.create_subprocess_exec(
        str(codex), "mcp", "list", "--json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    stdout, _ = await process.communicate()
    if process.returncode:
        return []
    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    entries = raw if isinstance(raw, list) else raw.get("mcp_servers", raw.get("servers", []))
    return [item.get("name") for item in entries if isinstance(item, dict) and SAFE_MCP_NAME.fullmatch(item.get("name", ""))]


def _loopback_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return int(port)


async def _connect(endpoint: str, socket_path: Path | None = None) -> JsonRpcWebSocket:
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
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cwd = validate_cwd(runtime_dir)
    codex = settings.codex_path
    executable = str(codex) if codex.is_file() else shutil.which(str(codex))
    if executable is None:
        raise FileNotFoundError(f"Codex executable not found: {codex}")
    actual_version = await codex_version(Path(executable))
    if actual_version != settings.expected_codex_version:
        raise CodexVersionError(f"Codex version is {actual_version}; RelayNote requires {settings.expected_codex_version}")
    ask_command = settings.ask_command or Path(shutil.which("relaynote-ask") or "")
    if not ask_command.is_file():
        raise FileNotFoundError("relaynote-ask executable not found")

    env = os.environ.copy()
    env.update(settings.network_env())
    env.update(
        {
            "RELAYNOTE_TODO_ID": todo_id,
            "RELAYNOTE_RUN_ID": run_id,
            "RELAYNOTE_ASK_SOCKET": str(settings.ask_socket),
            "RELAYNOTE_ASK_TOKEN": ask_token,
        }
    )
    disabled = await _configured_mcp_names(Path(executable), env)
    common = [
        executable,
        "app-server",
        "--disable", "apps",
        "--disable", "plugins",
        "-c", 'sandbox_mode="danger-full-access"',
        "-c", 'approval_policy="never"',
        "-c", f"mcp_servers.relaynote_ask.command={json.dumps(str(ask_command))}",
        "-c", 'mcp_servers.relaynote_ask.enabled=true',
        "-c", 'mcp_servers.relaynote_ask.required=true',
        "-c", 'mcp_servers.relaynote_ask.enabled_tools=["ask_user"]',
        "-c", 'mcp_servers.relaynote_ask.env_vars=["RELAYNOTE_TODO_ID","RELAYNOTE_RUN_ID","RELAYNOTE_ASK_SOCKET","RELAYNOTE_ASK_TOKEN"]',
        "-c", "mcp_servers.relaynote_ask.startup_timeout_sec=30",
        "-c", "mcp_servers.relaynote_ask.tool_timeout_sec=86400",
    ]
    for name in disabled:
        if name != "relaynote_ask":
            common.extend(("-c", f"mcp_servers.{name}.enabled=false"))

    socket_path: Path | None
    if prefer_unix:
        socket_path = cwd / "codex.sock"
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        endpoint = f"unix://{socket_path}"
    else:
        socket_path = None
        endpoint = f"ws://127.0.0.1:{_loopback_port()}"
    process = await asyncio.create_subprocess_exec(
        *common, "--listen", endpoint,
        env=env,
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
                stdout = await process.stdout.read() if process.stdout else b""
                stderr = await process.stderr.read() if process.stderr else b""
                raise RuntimeError(json.dumps({"stdout": stdout.decode(errors="replace"), "stderr": stderr.decode(errors="replace")}, ensure_ascii=False))
            await asyncio.sleep(0.05)
    if rpc is None:
        process.terminate()
        await process.wait()
        if prefer_unix:
            return await spawn_app_server(settings, runtime_dir, todo_id, run_id, ask_token, event_handler, prefer_unix=False)
        raise TimeoutError("Codex app-server endpoint was not ready")
    instance = CodexProcess(process, rpc, endpoint, event_handler)
    if process.stdout is not None:
        instance._stream_tasks.append(asyncio.create_task(instance.drain_process_stream("stdout", process.stdout)))
    if process.stderr is not None:
        instance._stream_tasks.append(asyncio.create_task(instance.drain_process_stream("stderr", process.stderr)))
    try:
        await instance.initialize()
    except BaseException:
        await instance.close()
        raise
    return instance
