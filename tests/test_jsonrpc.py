from __future__ import annotations

import asyncio
import json

from relaynote.jsonrpc import JsonRpcStream


async def test_jsonrpc_accepts_split_input() -> None:
    received = []
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b'{"jsonrpc":"2.0","method":"progress","params":{"text":"full')
        await writer.drain()
        writer.write(' 内容"}}\n'.encode())
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    rpc = JsonRpcStream(reader, writer)
    async for item in rpc.messages():
        received.append(item)
    server.close()
    await server.wait_closed()
    assert received[0]["params"]["text"] == "full 内容"
