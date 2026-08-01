"""List Codex session IDs associated with a working directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import UUID


def _session_meta_from_file(path: Path) -> tuple[str, Path] | None:
    """Return the validated session ID and working directory from a session file."""
    try:
        with path.open(encoding="utf-8") as session_file:
            for line in session_file:
                try:
                    event: Any = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not isinstance(event, dict) or event.get("type") != "session_meta":
                    continue

                payload = event.get("payload")
                if not isinstance(payload, dict):
                    return None

                session_id = payload.get("id")
                cwd = payload.get("cwd")
                if not isinstance(session_id, str) or not isinstance(cwd, str):
                    return None

                try:
                    return str(UUID(session_id)), Path(cwd).expanduser().resolve()
                except ValueError:
                    return None
    except (OSError, UnicodeError):
        return None

    return None


def get_codex_session_ids(
    working_directory: Path | str,
    sessions_directory: Path | str | None = None,
) -> list[str]:
    """Return session IDs whose recorded working directory matches the input.

    Codex session files are read recursively from ``sessions_directory``, which
    defaults to ``~/.codex/sessions``. Files without valid session metadata are
    ignored.

    Raises:
        FileNotFoundError: If either directory does not exist.
        NotADirectoryError: If either input is not a directory.
    """
    target = Path(working_directory).expanduser()
    sessions_root = (
        Path(sessions_directory).expanduser()
        if sessions_directory is not None
        else Path.home() / ".codex" / "sessions"
    )

    for directory in (target, sessions_root):
        if not directory.exists():
            raise FileNotFoundError(f"Directory does not exist: {directory}")
        if not directory.is_dir():
            raise NotADirectoryError(f"Not a directory: {directory}")

    resolved_target = target.resolve()
    session_ids: set[str] = set()
    for candidate in sessions_root.rglob("*.jsonl"):
        metadata = _session_meta_from_file(candidate)
        if metadata is not None and metadata[1] == resolved_target:
            session_ids.add(metadata[0])

    return sorted(session_ids)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print Codex session IDs for a working directory."
    )
    parser.add_argument("working_directory", type=Path, help="Codex working directory")
    parser.add_argument(
        "--sessions-directory",
        type=Path,
        help="Codex sessions root (default: ~/.codex/sessions)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    for session_id in get_codex_session_ids(
        args.working_directory, args.sessions_directory
    ):
        print(session_id)


if __name__ == "__main__":
    main()
