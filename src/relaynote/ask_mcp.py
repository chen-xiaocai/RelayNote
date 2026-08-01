from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any


TOOL = {
    "name": "ask_user",
    "description": "Ask the RelayNote user one blocking multiple-choice question.",
    "inputSchema": {
        "type": "object",
        "required": ["question", "options", "recommended"],
        "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "object", "required": ["label", "description"], "properties": {"label": {"type": "string"}, "description": {"type": "string"}}}},
            "recommended": {"type": "integer", "minimum": 0, "maximum": 2},
        },
    },
}


async def _broker_call(arguments: Any) -> Any:
    socket = os.environ.get("RELAYNOTE_ASK_SOCKET")
    if not socket:
        raise RuntimeError("RELAYNOTE_ASK_SOCKET is missing")
    reader, writer = await asyncio.open_unix_connection(socket)
    writer.write((json.dumps({"todo_id": os.environ.get("RELAYNOTE_TODO_ID"), "run_id": os.environ.get("RELAYNOTE_RUN_ID"), "arguments": arguments}, ensure_ascii=False) + "\n").encode())
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    return response


async def run() -> None:
    while line := await asyncio.to_thread(sys.stdin.buffer.readline):
        request = json.loads(line)
        method, request_id = request.get("method"), request.get("id")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "relaynote-ask", "version": "0.1.0"}}
        elif method == "tools/list":
            result = {"tools": [TOOL]}
        elif method == "tools/call" and request.get("params", {}).get("name") == "ask_user":
            answer = await _broker_call(request["params"].get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(answer, ensure_ascii=False)}], "structuredContent": answer}
        elif method == "notifications/initialized":
            continue
        else:
            payload = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"unsupported method: {method}"}}
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}, ensure_ascii=False), flush=True)


def main() -> None:
    asyncio.run(run())
