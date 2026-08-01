from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any


class JsonRpcError(RuntimeError):
    def __init__(self, error: Any) -> None:
        self.error = error
        super().__init__(json.dumps(error, ensure_ascii=False, separators=(",", ":")))


class JsonRpcConnection:
    def __init__(self, send_raw: Callable[[str], Awaitable[None]], receive_raw: Callable[[], Awaitable[str | None]]) -> None:
        self._send_raw = send_raw
        self._receive_raw = receive_raw
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop())

    async def request(self, method: str, params: Any) -> Any:
        self.start()
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await future

    async def notify(self, method: str, params: Any | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self.send(message)

    async def send(self, message: dict[str, Any]) -> None:
        await self._send_raw(json.dumps(message, ensure_ascii=False, separators=(",", ":")))

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        self.start()
        while True:
            message = await self._events.get()
            if message is None:
                return
            yield message

    async def close(self) -> None:
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

    async def _read_loop(self) -> None:
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
        except Exception as error:  # noqa: BLE001 - transport failures reject every pending request
            failure = error
        finally:
            error = failure or ConnectionError("JSON-RPC connection closed")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()
            await self._events.put(None)


class JsonRpcStream(JsonRpcConnection):
    """Newline JSON-RPC transport retaining incomplete input across reads."""

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
