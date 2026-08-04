"""完整原始 JSONL 日志记录，并在写入前拒绝敏感字段。"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORBIDDEN_KEYS = frozenset({"authorization", "api_key", "ipc_token", "password", "secret"})


def _secret_key(key: Any) -> bool:
    """判断字段名是否可能携带凭据，命中时禁止写入日志。"""
    normalized = str(key).lower()
    return normalized in FORBIDDEN_KEYS or normalized.endswith(("_api_key", "_access_token", "_auth_token"))


def _reject_secrets(value: Any, path: str = "$") -> None:
    """递归检查嵌套结构，发现敏感字段时直接抛出异常。"""
    if isinstance(value, dict):
        for key, child in value.items():
            if _secret_key(key):
                raise ValueError(f"secret-bearing field cannot be logged: {path}.{key}")
            _reject_secrets(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secrets(child, f"{path}[{index}]")


class JsonlLogger:
    """追加完整原始记录；字符串、数组和嵌套对象一律不截断、不提炼。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self._lock = threading.Lock()

    def write(self, category: str, raw: Any) -> None:
        """把带时间戳的完整原始数据写成一行 JSON，并同步刷盘。"""
        _reject_secrets(raw)
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "category": category,
            "raw": raw,
        }
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
