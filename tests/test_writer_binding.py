"""The writer binding names the first check that fails."""

from __future__ import annotations

import unittest
from pathlib import Path

from install.writer_binding import writer_mismatch

_SELECTED = (Path("rel"), Path("C:/py/python.exe"))
_WRITER: dict[str, object] = {"exe": "c:/PY/python.exe"}
_LAUNCHER: dict[str, object] = {
    "exe": "C:/L/run.exe",
    "cmd": "run.exe --go rel",
    "pid": 10,
    "parent": 11,
}
_TASK = ("c:/l/RUN.exe", "--go")


class WriterMismatchTests(unittest.TestCase):
    """Each way the live processes can differ from the task is numbered."""

    def test_owned_processes_match(self) -> None:
        """Case differences and the right engine id are accepted."""
        self.assertIsNone(
            writer_mismatch(
                _SELECTED, _WRITER, _LAUNCHER, "python rel", _TASK, {11}
            )
        )

    def test_unknown_selection_is_first(self) -> None:
        """Without a pinned release nothing else can be trusted."""
        self.assertEqual(
            writer_mismatch(
                None, _WRITER, _LAUNCHER, "python rel", _TASK, {11}
            ),
            1,
        )

    def test_wrong_interpreter_release_args_and_engine(self) -> None:
        """Each remaining difference reports its own number."""
        other = {**_WRITER, "exe": "c:/other.exe"}
        wrong_launcher = {**_LAUNCHER, "exe": "C:/L/other.exe"}
        no_args = {**_LAUNCHER, "cmd": "run.exe"}
        cases = (
            ((_SELECTED, other, _LAUNCHER, "python rel", _TASK, {11}), 3),
            ((_SELECTED, _WRITER, _LAUNCHER, "python x", _TASK, {11}), 4),
            (
                (
                    _SELECTED,
                    _WRITER,
                    wrong_launcher,
                    "python rel",
                    _TASK,
                    {11},
                ),
                6,
            ),
            ((_SELECTED, _WRITER, no_args, "python rel", _TASK, {11}), 8),
            ((_SELECTED, _WRITER, _LAUNCHER, "python rel", _TASK, {99}), 9),
        )
        for arguments, number in cases:
            with self.subTest(number=number):
                self.assertEqual(writer_mismatch(*arguments), number)
