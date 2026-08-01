"""Tests for discovering Codex sessions by working directory."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from samples.get_codex_session_ids import get_codex_session_ids


class GetCodexSessionIdsTest(unittest.TestCase):
    def test_returns_only_sessions_for_the_requested_working_directory(self) -> None:
        matching_ids = [
            "019fbc39-f131-77d3-8699-f4ef726ba58e",
            "019fbc1b-61f9-7470-b7ac-8504979ee3ec",
        ]
        other_id = "019fbc36-4cba-7773-b9ae-d2a5afdf570f"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            working_directory = root / "project"
            other_directory = root / "other-project"
            sessions_directory = root / "sessions" / "2026" / "08" / "01"
            working_directory.mkdir()
            other_directory.mkdir()
            sessions_directory.mkdir(parents=True)

            for index, session_id in enumerate(matching_ids):
                self._write_session(
                    sessions_directory / f"matching-{index}.jsonl",
                    session_id,
                    working_directory,
                )
            self._write_session(
                sessions_directory / "other.jsonl", other_id, other_directory
            )
            (sessions_directory / "unrelated.jsonl").write_text(
                json.dumps({"type": "event_msg", "payload": {}}) + "\n",
                encoding="utf-8",
            )

            self.assertEqual(
                get_codex_session_ids(working_directory, root / "sessions"),
                sorted(matching_ids),
            )

    def test_rejects_a_missing_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(FileNotFoundError):
                get_codex_session_ids(
                    Path(temp_dir) / "missing", Path(temp_dir)
                )

    @staticmethod
    def _write_session(path: Path, session_id: str, cwd: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": session_id, "cwd": str(cwd)},
                }
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
