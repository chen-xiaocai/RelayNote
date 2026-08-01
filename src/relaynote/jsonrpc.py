from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any


class JsonRpcStream:
    """Newline JSON-RPC transport retaining incomplete input across reads."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}

    async def request(self, method: str, params: Any) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await future

    async def send(self, message: dict[str, Any]) -> None:
        self.writer.write((json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        await self.writer.drain()

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        while line := await self.reader.readline():
            message = json.loads(line)
            request_id = message.get("id")
            if request_id in self._pending and ("result" in message or "error" in message):
                future = self._pending.pop(request_id)
                if "error" in message:
                    future.set_exception(RuntimeError(json.dumps(message["error"], ensure_ascii=False)))
                else:
                    future.set_result(message["result"])
            else:
                yield message
