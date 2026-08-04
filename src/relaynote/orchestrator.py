"""DeepSeek 调度器：无状态工具循环、持久上下文与历史压缩。"""

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

SYSTEM_PROMPT = """# Role Definition
你是 RelayNote 的调度器
RelayNote 是一个基于 Codex 的自动化待办管理系统
你每五分钟周期被唤醒一次, 负责调度 codex 完成待办
codex 是一个 agent, 负责完成各种任务, 每个待办至多绑定一个 codex threat 和一个 workspace

## Scheduling Rules

- 根据最新快照安排待办，优先级仅作为参考。
- 总计最多允许一个待办处于 `running`、`waiting_user` 或 `stopping` 状态。
- `claimed` 表示任务被用户人工接管, 不交给 codex
- 为一个待办分配 codex 时, 先创建一个工作区, 再为它绑定 codex threat

## Tool Usage

- 需要用户选择时调用 `ask_user`。
- 'asl_user' 是你与用户沟通的唯一途径, 不只是提问, 告知, 提醒等都使用这个 tool
- 执行任何改变后，查询最新列表确认结果。
- 不要假设工具调用成功。

## Safety

- 不要泄露环境变量或凭据。
"""
COMPACT_PROMPT = """把以下 RelayNote 调度历史压缩成可继续决策的完整状态总结。保留用户偏好、尚未完成的承诺、工具结果、失败原因、工作区决定与待办之间的依赖；不保留过期快照。只输出总结正文。"""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """调度器暴露给模型的一个本地工具定义。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    mutating: bool = True

    def spec(self) -> dict[str, Any]:
        """生成 OpenAI Responses API 使用的 strict 工具参数。"""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "strict": True,
        }


class Orchestrator:
    """每五分钟运行一次的调度器，通过工具调用读写待办状态。"""

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
        """初始化数据库、模型客户端、工具表与上下文压缩参数。"""
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
        """生成最新的待办、自动槽和运行状态快照。"""
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
        """返回待办最近一次 Codex 运行的关键字段。"""
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
        """执行一轮调度：压缩历史、调用模型、串行执行工具并持久化结果。"""
        if not self.settings.deepseek_api_key:
            # 未配置 API key 时跳过自动调度。
            return None
        compact_prompt, history, tokens, through_id = self.store.orchestrator_context()
        if tokens >= self.compaction_threshold and history:
            # 历史接近阈值时先压缩，后续只回放压缩总结和压缩后的新记录。
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
                # 最新快照只参与本轮输入，不写入持久历史，避免旧快照误导后续决策。
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
                    # 模型不再调用工具时结束本轮循环。
                    break
                call_outputs = []
                for call in calls:
                    # 工具按调用顺序串行执行，结果以 function_call_output 回传模型。
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
            # 任何模型或工具错误都完整记录，并结束本轮运行。
            failure = {"error_type": type(error).__name__, "error": str(error), "stack": traceback.format_exc()}
            self.logger.write("orchestrator_error", failure)
            self.store.finish_orchestrator_run(run_id, "error", failure, usage_totals)
            raise

    async def _execute_call(self, run_id: str, call: dict[str, Any]) -> Any:
        """执行单个工具调用，支持按 run_id + call_id 幂等。"""
        call_id, name = call["call_id"], call["name"]
        try:
            arguments = json.loads(call.get("arguments") or "{}")
        except json.JSONDecodeError as error:
            return {"error_type": type(error).__name__, "error": str(error)}
        existing = self.store.get_tool_call(run_id, call_id)
        if existing is not None:
            # 重试同一 call_id 时复用已完成结果；运行中的结果状态不确定则报错。
            if existing["status"] == "completed":
                return existing["result"]
            return {"error": "tool_call_result_uncertain", "status": existing["status"]}
        if not self.store.begin_tool_call(run_id, call_id, name, arguments):
            return {"error": "duplicate_tool_call"}
        definition = self.tools.get(name)
        if definition is None:
            # 未知工具也记录为完整结果，模型可据此修正。
            result: Any = {"error": f"unknown tool: {name}"}
            self.store.finish_tool_call(run_id, call_id, result, "completed")
            return result
        try:
            result = await definition.handler(**arguments)
            status = "completed"
        except Exception as error:  # noqa: BLE001 - 工具边界返回完整结构化失败
            result = {"error_type": type(error).__name__, "error": str(error), "stack": traceback.format_exc()}
            status = "failed"
        self.store.finish_tool_call(run_id, call_id, result, status)
        self.logger.write("tool_result", {"run_id": run_id, "call_id": call_id, "name": name, "arguments": arguments, "result": result, "status": status})
        return result

    async def _compact(self, previous: str | None, history: list[Any]) -> str:
        """调用模型把历史总结成可供后续决策的压缩正文。"""
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
        """返回所有工具的函数定义列表。"""
        return [definition.spec() for definition in self.tools.values()]
