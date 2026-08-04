"""Codex app-server 使用的 JSON-RPC 2.0 客户端传输层。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any


class JsonRpcError(RuntimeError):
    """服务端返回的 JSON-RPC error 对象。"""

    def __init__(self, error: Any) -> None:
        self.error = error
        super().__init__(json.dumps(error, ensure_ascii=False, separators=(",", ":")))


class JsonRpcConnection:
    """请求/响应配对、服务端通知分发与断线清理的通用 JSON-RPC 连接。"""

    def __init__(self, send_raw: Callable[[str], Awaitable[None]], receive_raw: Callable[[], Awaitable[str | None]]) -> None:
        self._send_raw = send_raw
        self._receive_raw = receive_raw
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """启动后台读取循环，仅在首次调用时创建任务。"""
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop())

    async def request(self, method: str, params: Any) -> Any:
        """发送带 id 的请求，并等待对应响应或错误。"""
        self.start()
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await future

    async def notify(self, method: str, params: Any | None = None) -> None:
        """发送不需要响应的通知。"""
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self.send(message)

    async def send(self, message: dict[str, Any]) -> None:
        """把消息序列化为紧凑 JSON 后交给底层传输。"""
        await self._send_raw(json.dumps(message, ensure_ascii=False, separators=(",", ":")))

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        """异步迭代服务端推送的事件，连接关闭时结束。"""
        self.start()
        while True:
            message = await self._events.get()
            if message is None:
                return
            yield message

    async def close(self) -> None:
        """取消读取循环，等待清理完成。"""
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

    async def _read_loop(self) -> None:
        """读取原始消息：匹配请求则唤醒等待者，否则放入事件队列。"""
        failure: BaseException | None = None
        try:
            while (raw := await self._receive_raw()) is not None:
                message = json.loads(raw)
                request_id = message.get("id")
                if request_id in self._pending and ("result" in message or "error" in message):
                    future = self._pending.pop(request_id)
                    if "error" in message:
                        future.set_exception(JsonRpcError(message["error"]))
                    else:
                        future.set_result(message["result"])
                else:
                    await self._events.put(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 传输失败要拒绝所有待处理请求
            failure = error
        finally:
            error = failure or ConnectionError("JSON-RPC connection closed")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()
            await self._events.put(None)


class JsonRpcStream(JsonRpcConnection):
    """基于 asyncio StreamReader/StreamWriter 的换行分隔 JSON-RPC 传输。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

        async def send_raw(raw: str) -> None:
            writer.write((raw + "\n").encode())
            await writer.drain()

        async def receive_raw() -> str | None:
            line = await reader.readline()
            return line.decode() if line else None

        super().__init__(send_raw, receive_raw)

    async def close(self) -> None:
        await super().close()
        self.writer.close()
        await self.writer.wait_closed()


class JsonRpcWebSocket(JsonRpcConnection):
    """基于 WebSocket 的 JSON-RPC 传输，兼容 Codex app-server。"""

    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket

        async def send_raw(raw: str) -> None:
            await websocket.send(raw)

        async def receive_raw() -> str | None:
            try:
                raw = await websocket.recv()
            except Exception as error:
                if type(error).__name__ in {"ConnectionClosed", "ConnectionClosedOK", "ConnectionClosedError"}:
                    return None
                raise
            if isinstance(raw, bytes):
                return raw.decode()
            return raw

        super().__init__(send_raw, receive_raw)

    async def close(self) -> None:
        await super().close()
        await self.websocket.close()
