from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    managed_root: Path
    codex_path: Path
    deepseek_api_key: str | None
    proxy_url: str = "http://127.0.0.1:7890"

    @classmethod
    def from_env(cls) -> "Settings":
        data = Path(os.environ.get("RELAYNOTE_DATA_DIR", "~/Library/Application Support/RelayNote")).expanduser()
        managed = Path(os.environ.get("RELAYNOTE_WORKSPACES", "~/RelayNote/Workspaces")).expanduser()
        codex = Path(os.environ.get("RELAYNOTE_CODEX_PATH", "/usr/local/bin/codex"))
        return cls(data.resolve(), managed.resolve(), codex, os.environ.get("DEEPSEEK_API_KEY"))

    def network_env(self) -> dict[str, str]:
        return {
            "HTTP_PROXY": self.proxy_url,
            "HTTPS_PROXY": self.proxy_url,
            "ALL_PROXY": self.proxy_url,
            "NO_PROXY": "localhost,127.0.0.1,::1",
        }
