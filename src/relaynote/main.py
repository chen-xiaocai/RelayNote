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
    parser = argparse.ArgumentParser(prog="relaynote")
    parser.add_argument("--add", metavar="TEXT")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--append", nargs=2, metavar=("TODO_ID", "TEXT"))
    parser.add_argument("--state", nargs=2, metavar=("TODO_ID", "STATE"))
    parser.add_argument("--start", metavar="TODO_ID")
    parser.add_argument("--project")
    parser.add_argument("--dirty-policy", choices=["suspend", "head", "original"], default="suspend")
    parser.add_argument("--run-tick", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    return parser


async def _async_command(settings: Settings, args: argparse.Namespace) -> None:
    from .runtime import Runtime

    runtime = Runtime(settings, ConsolePresenter())
    await runtime.start()
    try:
        if args.start:
            todo = runtime.store.get_todo(args.start)
            result = await runtime.start_todo(todo.id, todo.version, args.project, args.dirty_policy)
            print(json.dumps(result, ensure_ascii=False))
            outcome = await runtime.processes[result["run_id"]].wait_turn(result["turn_id"])
            print(json.dumps(outcome, ensure_ascii=False))
        if args.run_tick:
            result = await runtime.orchestrator.run(datetime.now(UTC))
            print(json.dumps(result, ensure_ascii=False))
            if runtime.processes:
                await asyncio.gather(*(process.wait_turn() for process in runtime.processes.values()))
    finally:
        await runtime.close()


def _console(settings: Settings, args: argparse.Namespace) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.database_path)
    if args.add:
        print(store.create_todo(args.add).id)
    if args.append:
        note = store.append_note(args.append[0], args.append[1])
        print(note.id)
    if args.state:
        todo = store.get_todo(args.state[0])
        updated = store.transition(todo.id, TodoState(args.state[1]), todo.version)
        print(f"{updated.id}\t{updated.state}\t{updated.version}")
    if args.doctor:
        try:
            version = asyncio.run(codex_version(settings.codex_path))
        except Exception as error:  # noqa: BLE001 - doctor reports every executable failure
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
        for todo in store.list_todos():
            print(f"{todo.id}\t{todo.state}\t{todo.title}")


def main() -> None:
    settings = Settings.from_env()
    args = _parser().parse_args()
    is_async = bool(args.start or args.run_tick)
    is_console = any((args.add, args.list, args.append, args.state, args.doctor, is_async))
    with InstanceLock(settings.data_dir / "relaynote.lock"):
        if is_async:
            asyncio.run(_async_command(settings, args))
        elif is_console or sys.platform != "darwin":
            _console(settings, args)
        else:
            from .macos_app import run_app
            run_app(settings)


if __name__ == "__main__":
    main()
