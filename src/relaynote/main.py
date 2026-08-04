"""RelayNote 的命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

from .ask import ConsolePresenter
from .codex import codex_version
from .config import Settings
from .db import Store
from .instance import InstanceLock
from .model import TodoState


def _parser() -> argparse.ArgumentParser:
    """构建包含所有支持子命令的 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(prog="relaynote")
    parser.add_argument("--add", metavar="TEXT")  # 创建一条新待办，参数是待办文本。
    parser.add_argument("--list", action="store_true")  # 展示待办清单，不需要额外参数。
    parser.add_argument("--append", nargs=2, metavar=("TODO_ID", "TEXT"))  # 给指定待办追加笔记，参数为待办 ID 和追加文本。
    parser.add_argument("--state", nargs=2, metavar=("TODO_ID", "STATE"))  # 将指定待办切换到目标状态，参数为待办 ID 和目标状态。
    parser.add_argument("--start", metavar="TODO_ID")  # 启动一条待办，参数为待办 ID。
    parser.add_argument("--project")  # 指定 --start 使用的 Git 项目路径，不传时创建独立工作区。
    parser.add_argument("--dirty-policy", choices=["suspend", "head", "original"], default="suspend")  # 项目有未提交改动时的策略：suspend 中止等待选择，head 基于 HEAD 建 worktree，original 直接用原目录。
    parser.add_argument("--run-tick", action="store_true")  # 手动推进一次调度器 tick。
    parser.add_argument("--doctor", action="store_true")  # 输出环境诊断信息，便于排查问题。
    return parser


async def _async_command(settings: Settings, args: argparse.Namespace) -> None:
    """执行需要异步运行时和进程生命周期的命令。"""
    from .runtime import Runtime

    runtime = Runtime(settings, ConsolePresenter())
    await runtime.start()
    try:
        if args.start:
            # 启动一条待办，打印运行结果，并等待其第一轮处理完成。
            todo = runtime.store.get_todo(args.start)
            result = await runtime.start_todo(todo.id, todo.version, args.project, args.dirty_policy)
            print(json.dumps(result, ensure_ascii=False))
            outcome = await runtime.processes[result["run_id"]].wait_turn(result["turn_id"])
            print(json.dumps(outcome, ensure_ascii=False))
        if args.run_tick:
            # 推进一次调度器，并等待本次产生的所有进程工作完成。
            result = await runtime.orchestrator.run(datetime.now(UTC))
            print(json.dumps(result, ensure_ascii=False))
            if runtime.processes:
                await asyncio.gather(*(process.wait_turn() for process in runtime.processes.values()))
    finally:
        await runtime.close()


def _console(settings: Settings, args: argparse.Namespace) -> None:
    """执行来自 CLI 的直接同步数据库操作。"""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.database_path)
    if args.add:
        # 创建新待办，并将生成的 ID 作为命令结果输出。
        print(store.create_todo(args.add).id)
    if args.append:
        note = store.append_note(args.append[0], args.append[1])
        print(note.id)
    if args.state:
        # 仅在当前版本一致时执行待办状态转换。
        todo = store.get_todo(args.state[0])
        updated = store.transition(todo.id, TodoState(args.state[1]), todo.version)
        print(f"{updated.id}\t{updated.state}\t{updated.version}")
    if args.doctor:
        # 输出环境信息，便于排查问题，无需另行搜索。
        try:
            version = asyncio.run(codex_version(settings.codex_path))
        except Exception as error:  # noqa: BLE001 - doctor 需要报告每个可执行文件失败
            version = f"error: {error}"
        report = {
            "data_dir": str(settings.data_dir),
            "managed_root": str(settings.managed_root),
            "codex_path": str(settings.codex_path),
            "codex_version": version,
            "expected_codex_version": settings.expected_codex_version,
            "ask_command": str(settings.ask_command) if settings.ask_command else None,
            "deepseek_enabled": settings.deepseek_api_key is not None,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.list or not any((args.add, args.append, args.state, args.doctor)):
        # 未指定控制台命令时，默认展示待办清单。
        for todo in store.list_todos():
            print(f"{todo.id}\t{todo.state}\t{todo.title}")


def main() -> None:
    """解析 CLI 参数、获取实例锁，并分派命令。"""
    settings = Settings.from_env()
    args = _parser().parse_args()
    is_async = bool(args.start or args.run_tick)
    is_console = any((args.add, args.list, args.append, args.state, args.doctor, is_async))
    with InstanceLock(settings.data_dir / "relaynote.lock"):
        if is_async:
            asyncio.run(_async_command(settings, args))
        elif is_console or sys.platform != "darwin":
            # 控制台命令适用于所有平台；仅在 macOS 上才回退到 GUI 模式。
            _console(settings, args)
        else:
            from .macos_app import run_app
            run_app(settings)


if __name__ == "__main__":
    main()
