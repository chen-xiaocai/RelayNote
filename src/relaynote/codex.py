from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .jsonrpc import JsonRpcStream
from .workspace import validate_cwd


DEVELOPER_INSTRUCTIONS = """You are operating for RelayNote. Complete the todo in the provided absolute workspace. Ask blocking questions only through relaynote-ask. Preserve user changes, emit concise commentary, and finish with a complete report. Never use built-in question tools."""


@dataclass(slots=True)
class CodexProcess:
    process: asyncio.subprocess.Process
    rpc: JsonRpcStream
    endpoint: str

    async def start_thread(self, cwd: Path, prompt: str) -> Any:
        return await self.rpc.request("thread/start", {"cwd": str(validate_cwd(cwd)), "prompt": prompt, "developerInstructions": DEVELOPER_INSTRUCTIONS, "sandbox": "danger-full-access", "approvalPolicy": "never"})

    async def resume_thread(self, thread_id: str) -> Any:
        return await self.rpc.request("thread/resume", {"threadId": thread_id})

    async def steer(self, thread_id: str, turn_id: str, text: str) -> Any:
        return await self.rpc.request("turn/steer", {"threadId": thread_id, "expectedTurnId": turn_id, "input": [{"type": "text", "text": text}]})

    async def interrupt(self, thread_id: str, turn_id: str) -> Any:
        return await self.rpc.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def stop_gracefully(self, thread_id: str, turn_id: str) -> bool:
        await self.steer(thread_id, turn_id, "停止开始新工作，安全释放资源并立即汇报当前状态。")
        try:
            await asyncio.wait_for(self.process.wait(), 30)
            return True
        except TimeoutError:
            await self.interrupt(thread_id, turn_id)
            return False


async def spawn_app_server(settings: Settings, runtime_dir: Path, todo_id: str, run_id: str) -> CodexProcess:
    cwd = validate_cwd(runtime_dir)
    codex = settings.codex_path
    if not codex.is_file() and shutil.which(str(codex)) is None:
        raise FileNotFoundError(f"Codex executable not found: {codex}")
    socket = cwd / "codex.sock"
    try:
        socket.unlink()
    except FileNotFoundError:
        pass
    env = os.environ.copy()
    env.update(settings.network_env())
    env.update({"RELAYNOTE_TODO_ID": todo_id, "RELAYNOTE_RUN_ID": run_id, "RELAYNOTE_ASK_SOCKET": str(cwd / "ask.sock")})
    process = await asyncio.create_subprocess_exec(
        str(codex), "app-server", "--listen", f"unix://{socket}",
        "-c", 'sandbox="danger-full-access"', "-c", 'approval_policy="never"',
        "-c", 'mcp_servers.relaynote_ask.command="relaynote-ask"',
        "-c", 'mcp_servers.relaynote_ask.enabled=true', "-c", 'mcp_servers.relaynote_ask.required=true',
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    for _ in range(100):
        try:
            reader, writer = await asyncio.open_unix_connection(socket)
            return CodexProcess(process, JsonRpcStream(reader, writer), f"unix://{socket}")
        except (FileNotFoundError, ConnectionRefusedError):
            if process.returncode is not None:
                stderr = await process.stderr.read() if process.stderr else b""
                raise RuntimeError(stderr.decode(errors="replace"))
            await asyncio.sleep(0.05)
    process.terminate()
    raise TimeoutError("Codex app-server socket was not ready")
