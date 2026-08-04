"""工作区路径校验；RelayNote 不再初始化或管理 Git 仓库。"""

from __future__ import annotations

from pathlib import Path


def validate_cwd(path: Path) -> Path:
    """校验并规范化工作目录，禁止使用文件系统根目录和用户主目录。"""
    resolved = path.expanduser().resolve()
    if resolved == Path("/") or resolved == Path.home().resolve():
        raise ValueError("workspace cannot be filesystem root or the user home")
    if not resolved.is_dir():
        raise NotADirectoryError(resolved)
    return resolved
