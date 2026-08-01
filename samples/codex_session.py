"""Utilities for inspecting local Codex session lifecycle events."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict
from uuid import UUID


LIFECYCLE_MARKERS = frozenset({"task_started", "task_complete", "turn_aborted"})


class LifecycleMarker(TypedDict):
    """The latest lifecycle marker recorded for a Codex session."""

    marker: str
    timestamp: str


def get_lifecycle_marker(
    session_id: str,
    sessions_dir: Path | None = None,
) -> LifecycleMarker | None:
    """Return the latest lifecycle marker and timestamp for ``session_id``.

    Args:
        session_id: Full Codex session UUID.
        sessions_dir: Codex sessions root. Defaults to ``~/.codex/sessions``.

    Returns:
        A dictionary containing the marker and its original ISO 8601 timestamp,
        or ``None`` when the session exists but has no lifecycle marker.

    Raises:
        ValueError: If ``session_id`` is not a valid UUID.
        FileNotFoundError: If no matching session file exists.
        RuntimeError: If more than one session file matches the same ID.
    """
    events = get_lifecycle_events(session_id, sessions_dir)
    return events[-1] if events else None


def get_lifecycle_events(
    session_id: str,
    sessions_dir: Path | None = None,
) -> list[LifecycleMarker]:
    """Return all lifecycle markers recorded for ``session_id``.

    Events are returned in the same order in which they appear in the session
    file. Each item contains the marker and its original ISO 8601 timestamp.

    Args:
        session_id: Full Codex session UUID.
        sessions_dir: Codex sessions root. Defaults to ``~/.codex/sessions``.

    Raises:
        ValueError: If ``session_id`` is not a valid UUID or the file contains
            invalid JSON.
        FileNotFoundError: If no matching session file exists.
        RuntimeError: If more than one session file matches the same ID.
    """
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

    events: list[LifecycleMarker] = []
    with matches[0].open(encoding="utf-8") as session_file:
        for line_number, line in enumerate(session_file, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Invalid JSON in {matches[0]} at line {line_number}"
                ) from error

            if event.get("type") != "event_msg":
                continue

            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue

            marker = payload.get("type")
            timestamp = event.get("timestamp")
            if marker in LIFECYCLE_MARKERS and isinstance(timestamp, str):
                events.append({"marker": marker, "timestamp": timestamp})

    return events
