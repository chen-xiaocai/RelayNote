from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta


def next_boundary(now: datetime, minutes: int = 5) -> datetime:
    base = now.replace(second=0, microsecond=0)
    remainder = base.minute % minutes
    if remainder == 0 and now == base:
        return base
    return base + timedelta(minutes=minutes - remainder)


class BoundaryScheduler:
    """Wall-clock aligned scheduler with no overlap and one wake catch-up."""

    def __init__(self, callback: Callable[[datetime], Awaitable[None]], interval: int = 5) -> None:
        self.callback = callback
        self.interval = interval
        self._running = False
        self.skipped_ticks: list[datetime] = []

    async def tick(self, at: datetime) -> bool:
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
        while not stop.is_set():
            now = datetime.now().astimezone()
            target = next_boundary(now, self.interval)
            try:
                await asyncio.wait_for(stop.wait(), max(0.0, (target - now).total_seconds()))
            except TimeoutError:
                await self.tick(target)
