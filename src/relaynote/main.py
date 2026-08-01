from __future__ import annotations

import argparse
import asyncio
import sys

from .config import Settings
from .db import Store
from .instance import InstanceLock


def _console(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.data_dir / "relaynote.sqlite3")
    parser = argparse.ArgumentParser(prog="relaynote")
    parser.add_argument("--add")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.add:
        print(store.create_todo(args.add).id)
    if args.list or not args.add:
        for todo in store.list_todos():
            print(f"{todo.id}\t{todo.state}\t{todo.body.splitlines()[0]}")


def main() -> None:
    settings = Settings.from_env()
    with InstanceLock(settings.data_dir / "relaynote.lock"):
        if sys.platform != "darwin":
            _console(settings)
            return
        from .macos_app import run_app
        run_app(settings)


if __name__ == "__main__":
    main()
