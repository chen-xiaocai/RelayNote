"""JSON-RPC 换行传输的测试。"""

from __future__ import annotations

import asyncio

from relaynote.jsonrpc import JsonRpcStream


async def test_jsonrpc_accepts_split_input() -> None:
    """验证一条 JSON 消息被拆成多次写入时仍能完整解析。"""
    received = []
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """分两次写入同一行 JSON，模拟网络分片。"""
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
