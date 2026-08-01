"""Tests for extracting user and Codex messages from a session."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from samples.extract_codex_conversation import extract_conversation


class ExtractConversationTest(unittest.TestCase):
    SESSION_ID = "019fbc1b-61f9-7470-b7ac-8504979ee3ec"

    def test_extracts_messages_and_excludes_tools_and_event_mirrors(self) -> None:
        records = [
            {
                "timestamp": "2026-08-01T00:59:59Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "注入上下文"}],
                },
            },
            {
                "timestamp": "2026-08-01T01:00:00Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "你好"},
            },
            {
                "timestamp": "2026-08-01T01:00:02Z",
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "example"},
            },
            {
                "timestamp": "2026-08-01T01:00:03Z",
                "type": "response_item",
                "payload": {"type": "custom_tool_call_output", "output": "工具结果"},
            },
            self._message(
                "2026-08-01T01:00:04Z", "assistant", "output_text", "完整回答"
            ),
            self._message("2026-08-01T01:00:05Z", "developer", "input_text", "开发者消息"),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / f"rollout-{self.SESSION_ID}.jsonl"
            session_path.write_text(
                "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
                encoding="utf-8",
            )

            self.assertEqual(
                extract_conversation(self.SESSION_ID, Path(temp_dir)),
                [
                    {
                        "timestamp": "2026-08-01T01:00:00Z",
                        "role": "user",
                        "text": "你好",
                    },
                    {
                        "timestamp": "2026-08-01T01:00:04Z",
                        "role": "assistant",
                        "text": "完整回答",
                    },
                ],
            )

    @staticmethod
    def _message(timestamp: str, role: str, content_type: str, text: str) -> dict:
        return {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": role,
                "content": [{"type": content_type, "text": text}],
            },
        }


if __name__ == "__main__":
    unittest.main()
