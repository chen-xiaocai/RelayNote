"""Git 工作区准备的扩展测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from relaynote.workspace import prepare_workspace


async def run_git(cwd: Path, *args: str) -> None:
    """在测试目录同步执行 Git 命令并断言成功。"""
    process = await asyncio.create_subprocess_exec("git", *args, cwd=cwd)
    assert await process.wait() == 0


async def test_non_git_project_is_copied_and_initialized(tmp_path: Path) -> None:
    """验证非 Git 项目会被完整复制到托管目录并初始化。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "完整.txt").write_text("原始内容", encoding="utf-8")
    workspace = await prepare_workspace("todo", tmp_path / "managed", source)
    assert workspace.path != source
    assert (workspace.path / "完整.txt").read_text(encoding="utf-8") == "原始内容"
    assert (workspace.path / ".git").is_dir()


async def test_dirty_git_requires_choice_and_head_creates_worktree(tmp_path: Path) -> None:
    """验证脏仓库默认要求选择，head 策略创建隔离 worktree。"""
    source = tmp_path / "source"
    source.mkdir()
    await run_git(source, "init")
    await run_git(source, "config", "user.email", "relaynote@example.invalid")
    await run_git(source, "config", "user.name", "RelayNote Test")
    (source / "file.txt").write_text("base", encoding="utf-8")
    await run_git(source, "add", "file.txt")
    await run_git(source, "commit", "-m", "base")
    (source / "file.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(RuntimeError, match="dirty_repository_requires_user_choice"):
        await prepare_workspace("todo-a", tmp_path / "managed", source)
    workspace = await prepare_workspace("todo-b", tmp_path / "managed", source, "head")
    assert workspace.worktree
    assert workspace.branch == "relaynote/todo-b"
    assert (workspace.path / "file.txt").read_text(encoding="utf-8") == "base"
