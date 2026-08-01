"""Print all lifecycle events for a local Codex session."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from samples.codex_session import get_lifecycle_events


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print all lifecycle events for a Codex session."
    )
    parser.add_argument("session_id", help="Full Codex session UUID")
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        help="Codex sessions directory (default: ~/.codex/sessions)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    lifecycle_events = get_lifecycle_events(args.session_id, args.sessions_dir)
    print(json.dumps(lifecycle_events, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
