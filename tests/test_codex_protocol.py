"""Codex v2 JSON-RPC 协议：thread、turn 与完成消息事件测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from relaynote.codex import CodexProcess


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
