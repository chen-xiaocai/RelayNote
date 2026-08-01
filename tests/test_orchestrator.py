from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from relaynote.config import Settings
from relaynote.db import Store
from relaynote.logging import JsonlLogger
from relaynote.orchestrator import Orchestrator, ToolDefinition


class FakeResponse:
    def __init__(self, raw):
        self.raw = raw

    def model_dump(self):
        return self.raw


class FakeResponses:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return FakeResponse(self.outputs.pop(0))


async def test_stateless_tool_loop_reconstructs_context_and_drops_snapshots(tmp_path: Path) -> None:
    called = []

    async def inspect(value: str):
        called.append(value)
        return {"seen": value}

    responses = FakeResponses(
        [
            {"output": [{"type": "function_call", "call_id": "c1", "name": "inspect", "arguments": '{"value":"完整值"}'}], "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}},
            {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}], "usage": {"input_tokens": 15, "output_tokens": 1, "total_tokens": 16}},
        ]
    )
    client = SimpleNamespace(responses=responses)
    settings = Settings(tmp_path, tmp_path / "work", Path("/bin/false"), "key")
    store = Store(tmp_path / "db.sqlite3")
    store.create_todo("任务")
    tool = ToolDefinition("inspect", "inspect", {"type": "object", "required": ["value"], "properties": {"value": {"type": "string"}}, "additionalProperties": False}, inspect)
    orchestrator = Orchestrator(store, settings, JsonlLogger(tmp_path / "events.jsonl"), [tool], client=client)
    result = await orchestrator.run(datetime.now(UTC))
    assert called == ["完整值"]
    assert result["usage"]["total_tokens"] == 28
    assert len(responses.requests) == 2
    assert responses.requests[1]["input"][-1]["content"].startswith("LATEST_TODO_AND_SESSION_SNAPSHOT")
    _, history, _, _ = store.orchestrator_context()
    assert not any(isinstance(item, dict) and str(item.get("content", "")).startswith("LATEST_TODO_AND_SESSION_SNAPSHOT") for item in history)
    assert any(item.get("type") == "function_call_output" for item in history)


async def test_tool_calls_are_idempotent_within_a_run(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    settings = Settings(tmp_path, tmp_path / "work", Path("/bin/false"), "key")
    count = 0

    async def mutate():
        nonlocal count
        count += 1
        return {"count": count}

    orchestrator = Orchestrator(store, settings, JsonlLogger(tmp_path / "events.jsonl"), {"mutate": mutate}, client=SimpleNamespace())
    call = {"call_id": "same", "name": "mutate", "arguments": "{}"}
    first = await orchestrator._execute_call("run", call)
    second = await orchestrator._execute_call("run", call)
    assert first == second == {"count": 1}
    assert count == 1
