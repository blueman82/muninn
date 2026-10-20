"""A Windows stop binds the writer through at most one venv redirector."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock

from install import lifecycle, writer_chain
from install.context import Ctx, StepFailedError


class WindowsStopBindingTests(unittest.TestCase):
    """Resolve only the selected redirector and its task parent."""

    def test_direct_writer_queries_only_powershell_parent(self) -> None:
        """The selected writer image needs no redirector lookup."""
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "release"
            selected = (release, release / ".venv/Scripts/python.exe")
            queried: list[int] = []

            def run(
                argv: Sequence[str | Path],
                env: Mapping[str, str] | None = None,
                input: bytes | None = None,
            ) -> subprocess.CompletedProcess[bytes]:
                pid = int(str(argv[0]))
                queried.append(pid)
                record = {
                    "pid": 10,
                    "parent": 11,
                    "exe": "C:/Windows/powershell.exe",
                    "cmd": "powershell.exe --owned",
                    "created": "task-created",
                }
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps(record).encode(), b""
                )

            ctx = Ctx(Path(temporary), run, "test", platform="win32")
            writer: dict[str, object] = {
                "pid": 20,
                "parent": 10,
                "exe": str(selected[1]),
                "created": "writer-created",
            }
            with mock.patch.object(
                writer_chain,
                "process_command",
                side_effect=lambda pid: [str(pid)],
            ):
                launcher, redirector = lifecycle._windows_chain(
                    ctx, writer, selected
                )
            self.assertEqual(queried, [10])
            self.assertEqual(launcher["pid"], 10)
            self.assertIsNone(redirector)

    def test_redirector_chain_queries_exactly_two_parents(self) -> None:
        """A base writer links through selected venv process to PowerShell."""
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "release"
            selected = (release, release / ".venv/Scripts/python.exe")
            records = {
                15: {
                    "pid": 15,
                    "parent": 10,
                    "exe": str(selected[1]),
                    "cmd": f'"{selected[1]}" -I -m muninn serve',
                    "created": "redirector-created",
                },
                10: {
                    "pid": 10,
                    "parent": 11,
                    "exe": "C:/Windows/powershell.exe",
                    "cmd": "powershell.exe --owned",
                    "created": "task-created",
                },
            }
            queried: list[int] = []

            def run(
                argv: Sequence[str | Path],
                env: Mapping[str, str] | None = None,
                input: bytes | None = None,
            ) -> subprocess.CompletedProcess[bytes]:
                pid = int(str(argv[0]))
                queried.append(pid)
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps(records[pid]).encode(), b""
                )

            ctx = Ctx(Path(temporary), run, "test", platform="win32")
            writer: dict[str, object] = {
                "pid": 20,
                "parent": 15,
                "exe": "C:/Python/python.exe",
                "cmd": f'"{selected[1]}" -I -m muninn serve',
                "created": "writer-created",
            }
            with mock.patch.object(
                writer_chain,
                "process_command",
                side_effect=lambda pid: [str(pid)],
            ):
                launcher, redirector = lifecycle._windows_chain(
                    ctx, writer, selected
                )
            self.assertEqual(queried, [15, 10])
            self.assertEqual(launcher["pid"], 10)
            self.assertEqual(redirector, records[15])

    def test_bad_intermediate_parent_refuses_extra_lookup(self) -> None:
        """An invalid redirector PID never triggers an ancestor walk."""
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "release"
            selected = (release, release / ".venv/Scripts/python.exe")
            queried: list[int] = []

            def run(
                argv: Sequence[str | Path],
                env: Mapping[str, str] | None = None,
                input: bytes | None = None,
            ) -> subprocess.CompletedProcess[bytes]:
                pid = int(str(argv[0]))
                queried.append(pid)
                record = {
                    "pid": 15,
                    "parent": 0,
                    "exe": str(selected[1]),
                    "cmd": str(selected[1]),
                    "created": "redirector-created",
                }
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps(record).encode(), b""
                )

            ctx = Ctx(Path(temporary), run, "test", platform="win32")
            writer: dict[str, object] = {
                "pid": 20,
                "parent": 15,
                "created": "writer-created",
            }
            with (
                mock.patch.object(
                    writer_chain,
                    "process_command",
                    side_effect=lambda pid: [str(pid)],
                ),
                self.assertRaises(StepFailedError),
            ):
                lifecycle._windows_chain(ctx, writer, selected)
            self.assertEqual(queried, [15])

    def test_reused_pid_does_not_match_creation_identity(self) -> None:
        """A later process with the same PID cannot satisfy the stop wait."""
        with tempfile.TemporaryDirectory() as temporary:

            def run(
                argv: Sequence[str | Path],
                env: Mapping[str, str] | None = None,
                input: bytes | None = None,
            ) -> subprocess.CompletedProcess[bytes]:
                current = {
                    "pid": 20,
                    "parent": 15,
                    "exe": "C:/Python/python.exe",
                    "cmd": "python -m muninn serve",
                    "created": "replacement-created",
                }
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps(current).encode(), b""
                )

            ctx = Ctx(Path(temporary), run, "test", platform="win32")
            observed: dict[str, object] = {
                "pid": 20,
                "created": "original-created",
            }
            with mock.patch.object(
                writer_chain,
                "process_command",
                side_effect=lambda pid: [str(pid)],
            ):
                self.assertFalse(lifecycle._same_process(ctx, observed))
