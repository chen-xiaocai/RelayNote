from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class Workspace:
    path: Path
    branch: str | None
    worktree: bool
    project_root: Path | None = None


async def _git(cwd: Path, *args: str) -> str:
    process = await asyncio.create_subprocess_exec("git", *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await process.communicate()
    if process.returncode:
        raise RuntimeError(err.decode(errors="replace"))
    return out.decode().strip()


def validate_cwd(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == Path("/") or resolved == Path.home().resolve():
        raise ValueError("workspace cannot be filesystem root or the user home")
    if not resolved.is_dir():
        raise NotADirectoryError(resolved)
    return resolved


async def prepare_workspace(todo_id: str, managed_root: Path, project: Path | None = None, dirty_policy: str = "suspend") -> Workspace:
    if not SAFE_ID.fullmatch(todo_id):
        raise ValueError("unsafe todo id")
    managed_root.mkdir(parents=True, exist_ok=True)
    target = managed_root / todo_id
    if target.exists():
        return Workspace(validate_cwd(target), None, (target / ".git").is_file(), project)
    if project is None:
        target.mkdir(exist_ok=False)
        await _git(target, "init")
        return Workspace(validate_cwd(target), None, False, None)
    project = validate_cwd(project)
    try:
        root = Path(await _git(project, "rev-parse", "--show-toplevel")).resolve()
    except RuntimeError:
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
        return Workspace(root, None, False, root)
    if dirty_policy not in {"suspend", "head"}:
        raise ValueError("dirty_policy must be suspend, head, or original")
    branch = f"relaynote/{todo_id}"
    await _git(root, "worktree", "add", "-b", branch, str(target), "HEAD")
    return Workspace(validate_cwd(target), branch, True, root)
