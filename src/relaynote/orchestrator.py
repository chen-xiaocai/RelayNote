from __future__ import annotations

import json
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from openai import AsyncOpenAI

from .config import Settings
from .db import Store
from .logging import JsonlLogger

SYSTEM_PROMPT = """你是 RelayNote 的本地待办调度器。你只在墙钟每五分钟运行一次。根据最下方最新快照安排待办，优先级仅作为参考。最多允许一个自动任务处于 running、waiting_user 或 stopping；claimed 任务不占自动槽且绝不能重新交给自动化。循环描述在 v0.1 中只执行一次。需要用户选择时调用 ask_user。执行任何改变后查询最新列表确认结果。不要假设工具调用成功，不要泄露环境变量或凭据。"""
COMPACT_PROMPT = """把以下 RelayNote 调度历史压缩成可继续决策的完整状态总结。保留用户偏好、尚未完成的承诺、工具结果、失败原因、工作区决定与待办之间的依赖；不保留过期快照。只输出总结正文。"""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    mutating: bool = True

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "strict": True,
        }


class Orchestrator:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        logger: JsonlLogger,
        tools: list[ToolDefinition] | dict[str, Callable[..., Awaitable[Any]]],
        client: Any | None = None,
        compaction_threshold: int = 128_000,
        max_tool_rounds: int = 12,
    ) -> None:
        self.store = store
        self.settings = settings
        self.logger = logger
        if isinstance(tools, dict):
            self.tools = {
                name: ToolDefinition(name, name.replace("_", " "), {"type": "object", "properties": {}, "additionalProperties": True}, handler)
                for name, handler in tools.items()
            }
        else:
            self.tools = {tool.name: tool for tool in tools}
        self.client = client or AsyncOpenAI(
            api_key=settings.deepseek_api_key or "missing",
            base_url="https://api.deepseek.com",
        )
        self.compaction_threshold = compaction_threshold
        self.max_tool_rounds = max_tool_rounds

    def snapshot(self) -> dict[str, Any]:
        lease = self.store.automatic_lease()
        return {
            "automatic_slot": {"todo_id": lease[0], "run_id": lease[1]} if lease else None,
            "todos": [
                {
                    "id": todo.id,
                    "body": todo.body,
                    "state": todo.state,
                    "priority": index,
                    "version": todo.version,
                    "workspace": todo.workspace,
                    "latest_detail": todo.latest_detail,
                    "latest_run": self._run_snapshot(todo.id),
                    "additions": [note.body for note in self.store.pending_additions(todo.id)],
                }
                for index, todo in enumerate(self.store.list_todos())
            ],
        }

    def _run_snapshot(self, todo_id: str) -> dict[str, Any] | None:
        run = self.store.latest_run(todo_id)
        if run is None:
            return None
        return {
            "run_id": run.run_id,
            "thread_id": run.thread_id,
            "turn_id": run.turn_id,
            "pid": run.pid,
            "endpoint": run.endpoint,
            "status": run.status,
        }

    async def run(self, tick: datetime) -> dict[str, Any] | None:
        if not self.settings.deepseek_api_key:
            return None
        compact_prompt, history, tokens, through_id = self.store.orchestrator_context()
        if tokens >= self.compaction_threshold and history:
            compact_prompt = await self._compact(compact_prompt, history)
            self.store.save_compaction(compact_prompt, through_id, 0)
            history = []
        run_id = str(uuid4())
        base: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if compact_prompt:
            base.append({"role": "system", "content": "HISTORY_SUMMARY\n" + compact_prompt})
        base.extend(history)
        self.store.start_orchestrator_run(run_id, tick, base, compact_prompt)
        current: list[dict[str, Any]] = []
        all_output: list[Any] = []
        usage_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        try:
            for _ in range(self.max_tool_rounds):
                snapshot = {"role": "user", "content": "LATEST_TODO_AND_SESSION_SNAPSHOT\n" + json.dumps(self.snapshot(), ensure_ascii=False, separators=(",", ":"))}
                input_items = [*base, *current, snapshot]
                request_record = {"run_id": run_id, "model": "deepseek-v4-flash", "input": input_items, "tools": self._tool_specs(), "reasoning": {"effort": "high"}}
                self.logger.write("deepseek_request", request_record)
                response = await self.client.responses.create(
                    model="deepseek-v4-flash",
                    input=input_items,
                    tools=self._tool_specs(),
                    reasoning={"effort": "high"},
                )
                raw = response.model_dump() if hasattr(response, "model_dump") else response
                self.logger.write("deepseek_response", raw)
                output = raw.get("output", [])
                usage = raw.get("usage") or {}
                for key in usage_totals:
                    usage_totals[key] += int(usage.get(key) or 0)
                current.extend(output)
                all_output.extend(output)
                self.store.append_orchestrator_items(run_id, output, int(usage.get("total_tokens") or 0))
                calls = [item for item in output if item.get("type") == "function_call"]
                if not calls:
                    break
                call_outputs = []
                for call in calls:
                    result = await self._execute_call(run_id, call)
                    call_outputs.append(
                        {
                            "type": "function_call_output",
                            "call_id": call["call_id"],
                            "output": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                        }
                    )
                current.extend(call_outputs)
                all_output.extend(call_outputs)
                self.store.append_orchestrator_items(run_id, call_outputs)
            else:
                raise RuntimeError("orchestrator exceeded maximum tool rounds")
            self.store.finish_orchestrator_run(run_id, "completed", all_output, usage_totals)
            return {"run_id": run_id, "output": all_output, "usage": usage_totals}
        except Exception as error:
            failure = {"error_type": type(error).__name__, "error": str(error), "stack": traceback.format_exc()}
            self.logger.write("orchestrator_error", failure)
            self.store.finish_orchestrator_run(run_id, "error", failure, usage_totals)
            raise

    async def _execute_call(self, run_id: str, call: dict[str, Any]) -> Any:
        call_id, name = call["call_id"], call["name"]
        try:
            arguments = json.loads(call.get("arguments") or "{}")
        except json.JSONDecodeError as error:
            return {"error_type": type(error).__name__, "error": str(error)}
        existing = self.store.get_tool_call(run_id, call_id)
        if existing is not None:
            if existing["status"] == "completed":
                return existing["result"]
            return {"error": "tool_call_result_uncertain", "status": existing["status"]}
        if not self.store.begin_tool_call(run_id, call_id, name, arguments):
            return {"error": "duplicate_tool_call"}
        definition = self.tools.get(name)
        if definition is None:
            result: Any = {"error": f"unknown tool: {name}"}
            self.store.finish_tool_call(run_id, call_id, result, "completed")
            return result
        try:
            result = await definition.handler(**arguments)
            status = "completed"
        except Exception as error:  # noqa: BLE001 - tool boundary returns complete structured failures
            result = {"error_type": type(error).__name__, "error": str(error), "stack": traceback.format_exc()}
            status = "failed"
        self.store.finish_tool_call(run_id, call_id, result, status)
        self.logger.write("tool_result", {"run_id": run_id, "call_id": call_id, "name": name, "arguments": arguments, "result": result, "status": status})
        return result

    async def _compact(self, previous: str | None, history: list[Any]) -> str:
        content: list[Any] = [{"role": "system", "content": COMPACT_PROMPT}]
        if previous:
            content.append({"role": "system", "content": previous})
        content.extend(history)
        response = await self.client.responses.create(model="deepseek-v4-flash", input=content)
        raw = response.model_dump() if hasattr(response, "model_dump") else response
        self.logger.write("deepseek_compaction", raw)
        text = raw.get("output_text")
        if text:
            return text
        return "\n".join(
            part.get("text", "")
            for item in raw.get("output", [])
            for part in item.get("content", [])
            if isinstance(part, dict) and part.get("type") in {"output_text", "text"}
        )

    def _tool_specs(self) -> list[dict[str, Any]]:
        return [definition.spec() for definition in self.tools.values()]
