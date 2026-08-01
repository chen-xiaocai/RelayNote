from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType


class AlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self._stream = None

    def __enter__(self) -> "InstanceLock":
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
        if self._stream is not None:
            fcntl.flock(self._stream, fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None
