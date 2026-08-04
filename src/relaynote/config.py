"""环境配置、默认路径与网络代理设置。"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .ipc import safe_unix_socket_path


@dataclass(frozen=True, slots=True)
class Settings:
    """RelayNote 运行时配置，全部来自环境变量或默认值。"""

    data_dir: Path
    managed_root: Path
    codex_path: Path
    deepseek_api_key: str | None
    proxy_url: str = "http://127.0.0.1:7890"
    expected_codex_version: str = "codex-cli 0.146.0"
    ask_command: Path | None = None

    @classmethod
    def from_env(cls) -> Settings:
        """从环境变量构造配置，并解析默认的 Codex 与 Ask MCP 可执行文件。"""
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
        """返回 SQLite 数据库文件的完整路径。"""
        return self.data_dir / "relaynote.sqlite3"

    @property
    def runtime_dir(self) -> Path:
        """返回保存运行期临时文件与 Codex 会话文件的目录。"""
        return self.data_dir / "runtime"

    @property
    def ask_socket(self) -> Path:
        """返回 Ask MCP 本地 Unix socket 路径，并保证路径长度可被系统接受。"""
        return safe_unix_socket_path(self.runtime_dir / "ask.sock", "ask")

    def network_env(self) -> dict[str, str]:
        """返回子进程使用的代理环境变量，本地回环地址不经过代理。"""
        return {
            "HTTP_PROXY": self.proxy_url,
            "HTTPS_PROXY": self.proxy_url,
            "ALL_PROXY": self.proxy_url,
            "NO_PROXY": "localhost,127.0.0.1,::1",
        }
