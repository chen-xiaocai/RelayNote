"""按墙钟边界触发的调度器，避免回调重叠并支持追赶跳过的 tick。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta


def next_boundary(now: datetime, minutes: int = 5) -> datetime:
    """返回下一个分钟边界；已在边界上且秒数为零时返回当前时刻。"""
    base = now.replace(second=0, microsecond=0)
    remainder = base.minute % minutes
    if remainder == 0 and now == base:
        return base
    return base + timedelta(minutes=minutes - remainder)


class BoundaryScheduler:
    """墙钟对齐调度器：同一时刻只运行一个 tick，醒来后最多补一次。"""

    def __init__(
        self,
        callback: Callable[[datetime], Awaitable[None]],
        interval: int = 5,
        on_error: Callable[[datetime, BaseException], None] | None = None,
    ) -> None:
        self.callback = callback
        self.interval = interval
        self.on_error = on_error
        self._running = False
        self.skipped_ticks: list[datetime] = []

    async def tick(self, at: datetime) -> bool:
        """执行一次回调；若已有回调在运行，则记录本次 tick 供稍后追赶。"""
        if self._running:
            self.skipped_ticks.append(at)
            return False
        self._running = True
        try:
            await self.callback(at)
            if self.skipped_ticks:
                catch_up = self.skipped_ticks[-1]
                self.skipped_ticks.clear()
                await self.callback(catch_up)
            return True
        finally:
            self._running = False

    async def run(self, stop: asyncio.Event) -> None:
        """等待下一个分钟边界并触发回调，直到 stop 事件被设置。"""
        while not stop.is_set():
            now = datetime.now().astimezone()
            target = next_boundary(now, self.interval)
            try:
                await asyncio.wait_for(stop.wait(), max(0.0, (target - now).total_seconds()))
            except TimeoutError:
                await self._run_tick(target)

    async def _run_tick(self, at: datetime) -> None:
        """运行回调并交给 on_error 处理异常，避免调度循环退出。"""
        try:
            await self.tick(at)
        except Exception as error:
            if self.on_error is None:
                raise
            self.on_error(at, error)
