"""Extract user and Codex text messages from a local Codex session."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Literal, TypedDict
from uuid import UUID


class ConversationMessage(TypedDict):
    """A user or Codex message stored in a session."""

    timestamp: str
    role: Literal["user", "assistant"]
    text: str


def _find_session_file(session_id: str, sessions_dir: Path | None) -> Path:
    normalized_id = str(UUID(session_id))
    root = sessions_dir or Path.home() / ".codex" / "sessions"
    matches = list(root.glob(f"**/*{normalized_id}.jsonl"))

    if not matches:
        raise FileNotFoundError(f"Codex session not found: {normalized_id}")
    if len(matches) > 1:
        paths = "\n".join(str(path) for path in matches)
        raise RuntimeError(
            f"Multiple Codex session files found for {normalized_id}:\n{paths}"
        )
    return matches[0]


def extract_conversation(
    session_id: str,
    sessions_dir: Path | None = None,
) -> list[ConversationMessage]:
    """Return user and assistant text messages in session-file order.

    User-authored input is read from ``user_message`` events so injected user-role
    context is excluded. Assistant text is read from canonical ``response_item``
    messages. Reasoning records, tool calls, and tool results are excluded.
    """
    session_file = _find_session_file(session_id, sessions_dir)
    messages: list[ConversationMessage] = []

    with session_file.open(encoding="utf-8") as lines:
        for line_number, line in enumerate(lines, start=1):
            try:
                event: Any = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Invalid JSON in {session_file} at line {line_number}"
                ) from error

            if not isinstance(event, dict):
                continue

            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue

            timestamp = event.get("timestamp")
            if not isinstance(timestamp, str):
                continue

            if event.get("type") == "event_msg" and payload.get("type") == "user_message":
                text = payload.get("message")
                if isinstance(text, str):
                    messages.append(
                        {"timestamp": timestamp, "role": "user", "text": text}
                    )
                continue

            if (
                event.get("type") != "response_item"
                or payload.get("type") != "message"
                or payload.get("role") != "assistant"
            ):
                continue

            content = payload.get("content")
            if not isinstance(content, list):
                continue
            text_parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "output_text" and isinstance(
                    item.get("text"), str
                ):
                    text_parts.append(item["text"])

            if text_parts:
                messages.append(
                    {
                        "timestamp": timestamp,
                        "role": "assistant",
                        "text": "\n".join(text_parts),
                    }
                )

    return messages


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract user and Codex text messages from a session."
    )
    parser.add_argument("session_id", help="Full Codex session UUID")
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        help="Codex sessions directory (default: ~/.codex/sessions)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    messages = extract_conversation(args.session_id, args.sessions_dir)
    print(json.dumps(messages, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
