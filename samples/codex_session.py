"""本地 Codex session 生命周期事件检查工具。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict
from uuid import UUID

LIFECYCLE_MARKERS = frozenset({"task_started", "task_complete", "turn_aborted"})


class LifecycleMarker(TypedDict):
    """Codex session 中记录的最新生周期标记。"""

    marker: str
    timestamp: str


def get_lifecycle_marker(
    session_id: str,
    sessions_dir: Path | None = None,
) -> LifecycleMarker | None:
    """返回指定 session 的最新生命周期标记和原始时间戳。"""
    events = get_lifecycle_events(session_id, sessions_dir)
    return events[-1] if events else None


def get_lifecycle_events(
    session_id: str,
    sessions_dir: Path | None = None,
) -> list[LifecycleMarker]:
    """按文件顺序返回 session 的全部生命周期标记。"""
    normalized_id = str(UUID(session_id))
    root = sessions_dir or Path.home() / ".codex" / "sessions"
    # 文件名中包含 UUID，直接递归搜索即可定位唯一 session 文件。
    matches = list(root.glob(f"**/*{normalized_id}.jsonl"))

    if not matches:
        raise FileNotFoundError(f"Codex session not found: {normalized_id}")
    if len(matches) > 1:
        # 同一 ID 匹配多个文件时全部列出，避免猜测错误文件。
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
                # 只关心事件消息，响应记录不在生命周期标记范围内。
                continue

            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue

            marker = payload.get("type")
            timestamp = event.get("timestamp")
            if marker in LIFECYCLE_MARKERS and isinstance(timestamp, str):
                events.append({"marker": marker, "timestamp": timestamp})

    return events
