"""单实例文件锁，防止多个 RelayNote 进程同时修改数据库。"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import Self


class AlreadyRunningError(RuntimeError):
    """另一个 RelayNote 进程已经持有实例锁。"""



class InstanceLock:
    """基于 fcntl 的进程级排他锁，并把当前 PID 写入锁文件。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self._stream = None

    def __enter__(self) -> Self:
        """以非阻塞方式获取锁；失败时说明已有实例在运行。"""
        self._stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._stream.close()
            self._stream = None
            raise AlreadyRunningError("RelayNote is already running") from error
        self._stream.seek(0)
        self._stream.truncate()
        self._stream.write(str(os.getpid()))
        self._stream.flush()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        """释放文件锁并关闭锁文件。"""
        if self._stream is not None:
            fcntl.flock(self._stream, fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None
