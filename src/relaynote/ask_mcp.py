"""RelayNote Ask MCP 服务，通过本地 Unix socket 桥接全局问题队列。"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

TOOL_SCHEMA = {
    "type": "object",
    "required": ["question", "options", "recommended"],
    "properties": {
        "question": {"type": "string", "minLength": 1},
        "options": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "required": ["label", "description"],
                "additionalProperties": False,
                "properties": {
                    "label": {"type": "string", "minLength": 1},
                    "description": {"type": "string", "minLength": 1},
                },
            },
        },
        "recommended": {"type": "integer", "minimum": 0, "maximum": 2},
    },
    "additionalProperties": False,
}


async def broker_call(arguments: dict[str, Any], unavailable_timeout: float = 5) -> dict[str, Any]:
    """通过环境变量定位 Ask socket，调用桥接服务并在失败时返回推荐项兜底。"""
    socket = os.environ.get("RELAYNOTE_ASK_SOCKET")
    token = os.environ.get("RELAYNOTE_ASK_TOKEN")
    recommended = arguments.get("recommended")
    fallback = {"option": recommended, "other": None, "timed_out": True}
    if not socket or not token:
        # 缺少 socket 或 token 时不能阻塞 Codex，直接返回推荐项。
        return fallback

    async def exchange() -> dict[str, Any]:
        """建立一次短连接，发送问题并读取 JSON 答案。"""
        reader, writer = await asyncio.open_unix_connection(socket)
        request = {
            "token": token,
            "todo_id": os.environ.get("RELAYNOTE_TODO_ID"),
            "run_id": os.environ.get("RELAYNOTE_RUN_ID"),
            "arguments": arguments,
        }
        writer.write((json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        await writer.drain()
        raw = await reader.readline()
        writer.close()
        await writer.wait_closed()
        response = json.loads(raw)
        if "error" in response:
            raise RuntimeError(response["error"])
        return response

    try:
        return await asyncio.wait_for(exchange(), unavailable_timeout)
    except (TimeoutError, OSError, RuntimeError, json.JSONDecodeError):
        # 桥不可用或超时都属于可恢复的展示故障，按推荐项继续。
        return fallback


def create_server() -> Server[Any]:
    """创建只暴露 ask_user 工具的 MCP server。"""
    async def list_tools(context: Any, params: Any) -> types.ListToolsResult:
        """向客户端声明唯一的 ask_user 工具。"""
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="ask_user",
                    description="向 RelayNote 用户提出一个阻塞式问题。每次只问一个问题，必须给 1-3 个选项并指定推荐项；用户也可以输入其他回答。",
                    inputSchema=TOOL_SCHEMA,
                )
            ]
        )

    async def call_tool(context: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        """校验推荐项索引，然后调用本地桥并返回结构化结果。"""
        if params.name != "ask_user":
            return types.CallToolResult(
                content=[types.TextContent(text=f"unsupported tool: {params.name}")],
                isError=True,
            )
        arguments = params.arguments or {}
        options = arguments.get("options") or []
        recommended = arguments.get("recommended")
        if not isinstance(recommended, int) or recommended not in range(len(options)):
            # 推荐项必须指向实际提供的选项之一。
            return types.CallToolResult(
                content=[types.TextContent(text="recommended must identify one supplied option")],
                isError=True,
            )
        result = await broker_call(arguments)
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        return types.CallToolResult(
            content=[types.TextContent(text=encoded)],
            structuredContent=result,
        )

    return Server("relaynote-ask", version="0.1.0", on_list_tools=list_tools, on_call_tool=call_tool)


async def run() -> None:
    """通过 stdio 启动 MCP server 并保持运行。"""
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """MCP 命令行入口。"""
    asyncio.run(run())
