"""RelayNote 配置：读取 ~/.relaynote/config.toml。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .ipc import safe_unix_socket_path

DEFAULT_CONFIG_PATH = Path("~/.relaynote/config.toml").expanduser()

DEFAULT_CONFIG = """\
[api]
key = ""
base_url = "https://api.deepseek.com"
model = "deepseek-v4-flash"

[paths]
data_dir = "~/Library/Application Support/RelayNote"
workspaces = "~/RelayNote/Workspaces"
codex_path = "codex"
expected_codex_version = "codex-cli 0.146.0"
shell = ""
"""


@dataclass(frozen=True, slots=True)
class Settings:
    """RelayNote 运行时配置，全部来自 config.toml 或默认值。"""

    data_dir: Path
    managed_root: Path
    codex_path: Path
    deepseek_api_key: str | None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    expected_codex_version: str = "codex-cli 0.146.0"
    shell_path: Path | None = None

    @classmethod
    def from_config(cls, path: Path | None = None) -> Settings:
        """读取配置文件；文件不存在时创建模板并返回默认设置。"""
        config_path = path or DEFAULT_CONFIG_PATH
        if not config_path.exists():
            config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            config_path.write_text(DEFAULT_CONFIG, encoding="utf-8")
            config_path.chmod(0o600)
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        api = raw.get("api", {})
        paths = raw.get("paths", {})

        def _path(value: str | None, default: str) -> Path:
            return Path(value or default).expanduser()

        shell_value = paths.get("shell")
        shell = Path(shell_value).expanduser() if shell_value else None
        return cls(
            data_dir=_path(paths.get("data_dir"), "~/Library/Application Support/RelayNote").resolve(),
            managed_root=_path(paths.get("workspaces"), "~/RelayNote/Workspaces").resolve(),
            codex_path=_path(paths.get("codex_path"), "codex"),
            deepseek_api_key=api.get("key") or None,
            deepseek_base_url=api.get("base_url", "https://api.deepseek.com"),
            deepseek_model=api.get("model", "deepseek-v4-flash"),
            expected_codex_version=paths.get("expected_codex_version", "codex-cli 0.146.0"),
            shell_path=shell.resolve() if shell else None,
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
