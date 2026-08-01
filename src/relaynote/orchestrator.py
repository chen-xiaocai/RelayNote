from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from openai import AsyncOpenAI

from .config import Settings
from .db import Store
from .logging import JsonlLogger


SYSTEM_PROMPT = """你是 RelayNote 的本地待办调度器。每轮审视最新待办快照，必要时调用工具。最多只有一个自动 Codex 任务。挂起任务不得自动接手。循环待办不受支持。不要假设工具成功，必须依据工具结果继续。"""


class Orchestrator:
    def __init__(self, store: Store, settings: Settings, logger: JsonlLogger, tools: dict[str, Callable[..., Awaitable[Any]]]) -> None:
        self.store, self.settings, self.logger, self.tools = store, settings, logger, tools
        self.client = AsyncOpenAI(api_key=settings.deepseek_api_key or "missing", base_url="https://api.deepseek.com")
        self.history: list[dict[str, Any]] = []
        self.compact_prompt: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {"todos": [{"id": t.id, "body": t.body, "state": t.state, "version": t.version, "workspace": t.workspace} for t in self.store.list_todos()]}

    async def run(self, tick: datetime) -> None:
        if not self.settings.deepseek_api_key:
            return
        run_id = str(uuid4())
        snapshot_item = {"role": "user", "content": "LATEST_SNAPSHOT\n" + json.dumps(self.snapshot(), ensure_ascii=False)}
        input_items: list[Any] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if self.compact_prompt:
            input_items.append({"role": "system", "content": self.compact_prompt})
        input_items.extend(self.history)
        input_items.append(snapshot_item)
        self.logger.write("deepseek_request", {"run_id": run_id, "model": "deepseek-v4-flash", "input": input_items})
        response = await self.client.responses.create(model="deepseek-v4-flash", input=input_items, tools=self._tool_specs())
        raw = response.model_dump()
        self.logger.write("deepseek_response", raw)
        round_items = raw.get("output", [])
        self.history.extend(round_items)
        for item in round_items:
            if item.get("type") != "function_call":
                continue
            call_id, name = item["call_id"], item["name"]
            arguments = json.loads(item.get("arguments") or "{}")
            if name not in self.tools:
                result: Any = {"error": f"unknown tool: {name}"}
            else:
                try:
                    result = await self.tools[name](**arguments)
                except BaseException as error:
                    result = {"error_type": type(error).__name__, "error": str(error)}
            if self.store.save_tool_result(run_id, call_id, name, arguments, result):
                output = {"type": "function_call_output", "call_id": call_id, "output": json.dumps(result, ensure_ascii=False)}
                self.history.append(output)
                self.logger.write("tool_result", output)

    def _tool_specs(self) -> list[dict[str, Any]]:
        return [{"type": "function", "name": name, "description": name.replace("_", " "), "parameters": {"type": "object", "additionalProperties": True}} for name in self.tools]
