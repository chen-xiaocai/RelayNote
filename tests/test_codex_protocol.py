"""Codex v2 JSON-RPC 协议：thread、turn 与完成消息事件测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from relaynote.codex import CodexProcess, spawn_app_server
from relaynote.config import Settings


class FakeProcess:
    """测试用子进程对象，只提供协议测试需要的字段。"""

    pid = 42
    returncode = 0


class FakeRpc:
    """记录请求并通过队列推送事件的伪 JSON-RPC 客户端。"""

    def __init__(self):
        self.requests = []
        self.notifications = []
        self.queue = asyncio.Queue()

    async def request(self, method, params):
        """按方法返回固定响应，同时记录收到的参数。"""
        self.requests.append((method, params))
        if method == "initialize":
            return {"userAgent": "test", "codexHome": "/tmp", "platformFamily": "unix", "platformOs": "linux"}
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"], "activeTurn": None}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "turn/steer":
            return {"turnId": params["expectedTurnId"]}
        return {}

    async def notify(self, method, params=None):
        """记录通知。"""
        self.notifications.append((method, params))

    async def messages(self):
        """从队列异步产出事件，收到 None 时结束。"""
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def close(self):
        await self.queue.put(None)


class FakeSpawnProcess:
    """spawn_app_server 测试使用的伪子进程。"""

    pid = 123
    returncode = None
    stdout = None
    stderr = None

    def terminate(self) -> None:
        self.returncode = 0

    async def wait(self) -> int:
        return 0


async def test_codex_v2_thread_turn_and_complete_message(tmp_path: Path) -> None:
    """验证线程启动、turn 启动、分片消息组装和完成事件。"""
    rpc = FakeRpc()
    events = []

    async def handler(message):
        events.append(message)

    process = CodexProcess(FakeProcess(), rpc, "unix:///tmp/codex.sock", handler)
    await process.initialize()
    await process.start_thread(tmp_path)
    turn_id = await process.start_turn("完整任务")
    # 模拟 Codex 推送消息分片、消息完成和 turn 完成三个事件。
    await rpc.queue.put({"method": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": "完整"}})
    await rpc.queue.put({"method": "item/completed", "params": {"item": {"id": "m1", "type": "agentMessage", "text": "完整结果"}}})
    await rpc.queue.put({"method": "turn/completed", "params": {"turn": {"id": turn_id, "status": "completed"}}})
    outcome = await asyncio.wait_for(process.wait_turn(turn_id), 1)
    assert outcome["final_message"] == "完整结果"
    assert rpc.requests[1][0] == "thread/start"
    assert "完整任务" not in str(rpc.requests[1][1])
    assert rpc.requests[2][0] == "turn/start"
    assert events[-1]["method"] == "turn/completed"
    await process.close(terminate=False)


async def test_spawn_app_server_keeps_existing_mcp_and_drops_proxy(monkeypatch, tmp_path: Path) -> None:
    """验证 Codex 启动命令只禁用 apps，并保留已有 MCP、不注入代理。"""
    codex = tmp_path / "codex"
    codex.write_text("", encoding="utf-8")
    settings = Settings(
        tmp_path / "data",
        tmp_path / "workspaces",
        codex,
        None,
        expected_codex_version="codex-cli 0.146.0",
    )
    captured = {}

    async def fake_version(path: Path) -> str:
        return settings.expected_codex_version

    async def fake_connect(endpoint: str, socket_path: Path | None = None):
        rpc = FakeRpc()
        rpc.queue.put_nowait(None)
        return rpc

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeSpawnProcess()

    monkeypatch.setattr("relaynote.codex.codex_version", fake_version)
    monkeypatch.setattr("relaynote.codex._connect", fake_connect)
    monkeypatch.setattr("relaynote.codex.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        "relaynote.codex.ask_mcp_command",
        lambda: ("/fake/ask_mcp", []),
    )
    process = await spawn_app_server(settings, tmp_path / "runtime", "todo", "run", "token")
    await process.close(terminate=False)
    command = " ".join(str(item) for item in captured["args"])
    assert "--disable apps" in command
    assert "--disable plugins" not in command
    assert "HTTP_PROXY" not in command
    assert "mcp_servers.tavily.enabled=false" not in command
    assert "mcp_servers.relaynote_ask.command" in command
