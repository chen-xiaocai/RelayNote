"""为待办准备隔离工作区，支持独立目录、非 Git 复制与 Git worktree。"""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class Workspace:
    """工作区描述：实际路径、Git 分支、是否 worktree 及原项目根目录。"""

    path: Path
    branch: str | None
    worktree: bool
    project_root: Path | None = None


async def _git(cwd: Path, *args: str) -> str:
    """在指定目录执行 Git 命令并返回 stdout。"""
    process = await asyncio.create_subprocess_exec("git", *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await process.communicate()
    if process.returncode:
        raise RuntimeError(err.decode(errors="replace"))
    return out.decode().strip()


def validate_cwd(path: Path) -> Path:
    """校验并规范化工作目录，禁止使用文件系统根目录和用户主目录。"""
    resolved = path.expanduser().resolve()
    if resolved == Path("/") or resolved == Path.home().resolve():
        raise ValueError("workspace cannot be filesystem root or the user home")
    if not resolved.is_dir():
        raise NotADirectoryError(resolved)
    return resolved


async def prepare_workspace(todo_id: str, managed_root: Path, project: Path | None = None, dirty_policy: str = "suspend") -> Workspace:
    """按待办准备工作区；已有目录复用，新项目初始化为 Git，脏仓库按策略处理。"""
    if not SAFE_ID.fullmatch(todo_id):
        raise ValueError("unsafe todo id")
    managed_root.mkdir(parents=True, exist_ok=True)
    target = managed_root / todo_id
    if target.exists():
        # 已有托管工作区时直接复用，避免重复初始化。
        return Workspace(validate_cwd(target), None, (target / ".git").is_file(), project)
    if project is None:
        # 无项目路径时创建全新的隔离目录并执行 git init。
        target.mkdir(exist_ok=False)
        await _git(target, "init")
        return Workspace(validate_cwd(target), None, False, None)
    project = validate_cwd(project)
    try:
        root = Path(await _git(project, "rev-parse", "--show-toplevel")).resolve()
    except RuntimeError:
        # 源目录不是 Git 仓库时，把内容复制到托管目录后再初始化。
        target.mkdir(exist_ok=False)
        for source in project.iterdir():
            destination = target / source.name
            if source.is_dir():
                shutil.copytree(source, destination, symlinks=True)
            else:
                shutil.copy2(source, destination, follow_symlinks=False)
        await _git(target, "init")
        return Workspace(validate_cwd(target), None, False, project)
    dirty = bool(await _git(root, "status", "--porcelain"))
    if dirty and dirty_policy == "suspend":
        raise RuntimeError("dirty_repository_requires_user_choice")
    if dirty_policy == "original":
        # original 策略直接使用原仓库，未提交改动也会被 Codex 看到。
        return Workspace(root, None, False, root)
    if dirty_policy not in {"suspend", "head"}:
        raise ValueError("dirty_policy must be suspend, head, or original")
    branch = f"relaynote/{todo_id}"
    # head 策略基于当前 HEAD 创建独立分支和 worktree，不携带未提交改动。
    await _git(root, "worktree", "add", "-b", branch, str(target), "HEAD")
    return Workspace(validate_cwd(target), branch, True, root)
