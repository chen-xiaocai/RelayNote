from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

MAX_UNIX_SOCKET_PATH = 96


def safe_unix_socket_path(path: Path, namespace: str) -> Path:
    resolved = path.expanduser().resolve()
    if len(os.fsencode(resolved)) <= MAX_UNIX_SOCKET_PATH:
        return resolved
    digest = hashlib.sha256(os.fsencode(resolved)).hexdigest()[:16]
    parent = Path(tempfile.gettempdir()) / f"relaynote-{os.getuid()}-{namespace}-{digest}"
    if parent.exists():
        metadata = parent.lstat()
        if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise PermissionError(f"unsafe RelayNote IPC directory: {parent}")
    else:
        parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    return parent / resolved.name
