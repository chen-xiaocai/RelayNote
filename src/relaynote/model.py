from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TodoState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting_user"
    STOPPING = "stopping"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    ERROR = "error"
    TAKEN_OVER = "taken_over"
    ARCHIVED = "archived"


AUTO_SLOT_STATES = frozenset(
    {TodoState.RUNNING, TodoState.WAITING, TodoState.STOPPING}
)

ALLOWED_TRANSITIONS: dict[TodoState, frozenset[TodoState]] = {
    TodoState.PENDING: frozenset({TodoState.RUNNING, TodoState.SUSPENDED, TodoState.TAKEN_OVER}),
    TodoState.RUNNING: frozenset({TodoState.WAITING, TodoState.STOPPING, TodoState.COMPLETED, TodoState.ERROR, TodoState.TAKEN_OVER}),
    TodoState.WAITING: frozenset({TodoState.RUNNING, TodoState.STOPPING, TodoState.ERROR, TodoState.TAKEN_OVER}),
    TodoState.STOPPING: frozenset({TodoState.SUSPENDED, TodoState.ERROR, TodoState.TAKEN_OVER}),
    TodoState.SUSPENDED: frozenset({TodoState.PENDING, TodoState.TAKEN_OVER}),
    TodoState.COMPLETED: frozenset({TodoState.PENDING, TodoState.ARCHIVED, TodoState.TAKEN_OVER}),
    TodoState.ERROR: frozenset({TodoState.PENDING, TodoState.SUSPENDED, TodoState.TAKEN_OVER}),
    TodoState.TAKEN_OVER: frozenset({TodoState.COMPLETED, TodoState.ARCHIVED}),
    TodoState.ARCHIVED: frozenset(),
}


class InvalidTransition(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Todo:
    id: str
    body: str
    state: TodoState
    sort_key: int
    version: int
    latest_detail: str | None
    workspace: str | None


def validate_transition(current: TodoState, target: TodoState) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"invalid todo transition: {current} -> {target}")
