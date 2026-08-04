"""待办数据模型、状态标签与合法状态转换表。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TodoState(StrEnum):
    """待办生命周期中的全部状态。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting_user"
    STOPPING = "stopping"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    ERROR = "error"
    TAKEN_OVER = "taken_over"
    ARCHIVED = "archived"

    @property
    def label(self) -> str:
        """返回用户界面使用的中文状态标签。"""
        return STATE_LABELS[self]


AUTO_SLOT_STATES = frozenset(
    {TodoState.RUNNING, TodoState.WAITING, TodoState.STOPPING}
)

ALLOWED_TRANSITIONS: dict[TodoState, frozenset[TodoState]] = {
    TodoState.PENDING: frozenset({TodoState.RUNNING, TodoState.SUSPENDED, TodoState.COMPLETED, TodoState.ARCHIVED, TodoState.TAKEN_OVER}),
    TodoState.RUNNING: frozenset({TodoState.WAITING, TodoState.STOPPING, TodoState.COMPLETED, TodoState.ERROR, TodoState.TAKEN_OVER}),
    TodoState.WAITING: frozenset({TodoState.RUNNING, TodoState.STOPPING, TodoState.COMPLETED, TodoState.ERROR, TodoState.TAKEN_OVER}),
    TodoState.STOPPING: frozenset({TodoState.SUSPENDED, TodoState.COMPLETED, TodoState.ARCHIVED, TodoState.ERROR, TodoState.TAKEN_OVER}),
    TodoState.SUSPENDED: frozenset({TodoState.PENDING, TodoState.COMPLETED, TodoState.ARCHIVED, TodoState.TAKEN_OVER}),
    TodoState.COMPLETED: frozenset({TodoState.PENDING, TodoState.ARCHIVED, TodoState.TAKEN_OVER}),
    TodoState.ERROR: frozenset({TodoState.PENDING, TodoState.SUSPENDED, TodoState.COMPLETED, TodoState.ARCHIVED, TodoState.TAKEN_OVER}),
    TodoState.TAKEN_OVER: frozenset({TodoState.COMPLETED, TodoState.ARCHIVED}),
    TodoState.ARCHIVED: frozenset(),
}

STATE_LABELS = {
    TodoState.PENDING: "待完成",
    TodoState.RUNNING: "进行中",
    TodoState.WAITING: "等待你",
    TodoState.STOPPING: "正在停止",
    TodoState.SUSPENDED: "挂起",
    TodoState.COMPLETED: "已完成",
    TodoState.ERROR: "异常",
    TodoState.TAKEN_OVER: "已接管",
    TodoState.ARCHIVED: "已归档",
}


class InvalidTransition(ValueError):
    """状态机不允许当前状态到目标状态的转换。"""



@dataclass(frozen=True, slots=True)
class Todo:
    """一条待办的核心记录，version 用于乐观并发控制。"""

    id: str
    body: str
    state: TodoState
    sort_key: int
    version: int
    latest_detail: str | None
    workspace: str | None
    pending_action: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def title(self) -> str:
        """从正文第一行生成列表标题。"""
        return next((line.strip() for line in self.body.splitlines() if line.strip()), "无标题待办")


@dataclass(frozen=True, slots=True)
class TodoNote:
    """追加给待办的不可变笔记，包含投递确认信息。"""

    id: int
    todo_id: str
    kind: str
    body: str
    created_at: datetime
    delivered_at: datetime | None
    ack_id: str | None


@dataclass(frozen=True, slots=True)
class CodexRun:
    """一次 Codex app-server 运行在数据库中的记录。"""

    run_id: str
    todo_id: str
    thread_id: str | None
    turn_id: str | None
    pid: int | None
    endpoint: str | None
    workspace: str
    status: str
    final_message: str | None


def validate_transition(current: TodoState, target: TodoState) -> None:
    """校验状态转换是否合法，非法时抛出 InvalidTransition。"""
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"invalid todo transition: {current} -> {target}")
