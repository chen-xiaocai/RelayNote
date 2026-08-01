from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .ipc import safe_unix_socket_path


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    managed_root: Path
    codex_path: Path
    deepseek_api_key: str | None
    proxy_url: str = "http://127.0.0.1:7890"
    expected_codex_version: str = "codex-cli 0.146.0"
    ask_command: Path | None = None

    @classmethod
    def from_env(cls) -> Settings:
        data = Path(os.environ.get("RELAYNOTE_DATA_DIR", "~/Library/Application Support/RelayNote")).expanduser()
        managed = Path(os.environ.get("RELAYNOTE_WORKSPACES", "~/RelayNote/Workspaces")).expanduser()
        configured = os.environ.get("RELAYNOTE_CODEX_PATH")
        discovered = shutil.which("codex")
        codex = Path(configured or discovered or "/usr/local/bin/codex")
        ask = os.environ.get("RELAYNOTE_ASK_COMMAND") or shutil.which("relaynote-ask")
        if ask is None:
            executable_dir = Path(sys.executable).resolve().parent
            bundled_ask = next((candidate for candidate in (executable_dir / "ask_mcp", executable_dir / "ask_mcp.py") if candidate.is_file()), None)
            if bundled_ask is not None:
                ask = str(bundled_ask)
        return cls(
            data.resolve(), managed.resolve(), codex, os.environ.get("DEEPSEEK_API_KEY"),
            expected_codex_version=os.environ.get("RELAYNOTE_CODEX_VERSION", "codex-cli 0.146.0"),
            ask_command=Path(ask).resolve() if ask else None,
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / "relaynote.sqlite3"

    @property
    def runtime_dir(self) -> Path:
        return self.data_dir / "runtime"

    @property
    def ask_socket(self) -> Path:
        return safe_unix_socket_path(self.runtime_dir / "ask.sock", "ask")

    def network_env(self) -> dict[str, str]:
        return {
            "HTTP_PROXY": self.proxy_url,
            "HTTPS_PROXY": self.proxy_url,
            "ALL_PROXY": self.proxy_url,
            "NO_PROXY": "localhost,127.0.0.1,::1",
        }
