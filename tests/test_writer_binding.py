"""The writer binding names the first check that fails."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from install.writer_binding import writer_mismatch
from muninn import platform_io, platform_windows
from muninn.obs_service import writer_command_matches, writer_image_matches

_SELECTED = (
    Path("C:/py/rel"),
    Path("C:/py/rel/.venv/Scripts/python.exe"),
)
_WRITER: dict[str, object] = {
    "exe": "c:/PY/rel/.venv/Scripts/python.exe",
    "parent": 10,
}
_WRITER_COMMAND = '"C:/py/rel/.venv/Scripts/python.exe" -I rel'
_LAUNCHER: dict[str, object] = {
    "exe": "C:/L/run.exe",
    "cmd": "run.exe --go rel",
    "pid": 10,
    "parent": 11,
}
_TASK = ("c:/l/RUN.exe", "--go")
_REDIRECTOR: dict[str, object] = {
    "exe": "C:/py/rel/.venv/Scripts/python.exe",
    "cmd": _WRITER_COMMAND,
    "pid": 15,
    "parent": 10,
}


class WriterMismatchTests(unittest.TestCase):
    """Each way the live processes can differ from the task is numbered."""

    def test_owned_processes_match(self) -> None:
        """Case differences and the right engine id are accepted."""
        self.assertIsNone(
            writer_mismatch(
                _SELECTED, _WRITER, _LAUNCHER, _WRITER_COMMAND, _TASK, {11}
            )
        )

    def test_unknown_selection_is_first(self) -> None:
        """Without a pinned release nothing else can be trusted."""
        self.assertEqual(
            writer_mismatch(
                None, _WRITER, _LAUNCHER, _WRITER_COMMAND, _TASK, {11}
            ),
            1,
        )

    def test_wrong_interpreter_release_args_and_engine(self) -> None:
        """Each remaining difference reports its own number."""
        other = {**_WRITER, "exe": "c:/other.exe"}
        wrong_launcher = {**_LAUNCHER, "exe": "C:/L/other.exe"}
        no_args = {**_LAUNCHER, "cmd": "run.exe"}
        cases = (
            ((_SELECTED, other, _LAUNCHER, _WRITER_COMMAND, _TASK, {11}), 3),
            ((_SELECTED, _WRITER, _LAUNCHER, "python x", _TASK, {11}), 4),
            (
                (
                    _SELECTED,
                    _WRITER,
                    wrong_launcher,
                    _WRITER_COMMAND,
                    _TASK,
                    {11},
                ),
                6,
            ),
            ((_SELECTED, _WRITER, no_args, _WRITER_COMMAND, _TASK, {11}), 8),
            ((_SELECTED, _WRITER, _LAUNCHER, _WRITER_COMMAND, _TASK, {99}), 9),
        )
        for arguments, number in cases:
            with self.subTest(number=number):
                self.assertEqual(writer_mismatch(*arguments), number)


class WindowsVenvImageTests(unittest.TestCase):
    """Bind a Windows redirector command to its private base interpreter."""

    def test_redirector_reports_base_image(self) -> None:
        """A genuine venv command can report its pyvenv base process image."""
        with TemporaryDirectory() as temporary:
            release = Path(temporary) / "release"
            venv = release / ".venv"
            launcher = venv / "Scripts/python.exe"
            base = Path(temporary) / "Python 3.13/python.exe"
            launcher.parent.mkdir(parents=True)
            base.parent.mkdir()
            launcher.write_bytes(b"launcher")
            base.write_bytes(b"base")
            prior = Path(temporary) / "prior/.venv/Scripts/python.exe"
            (venv / "pyvenv.cfg").write_text(
                f"home = {base.parent}\n"
                "include-system-site-packages = false\n"
                "version = 3.13.9\n"
                f"executable = {prior}\n"
                f"command = {base} -m venv {venv}\n"
            )
            command = f'"{launcher}" -I -B -m muninn serve'
            with (
                mock.patch.object(platform_io, "assert_private_fd"),
                mock.patch.object(
                    platform_windows, "assert_executable", create=True
                ),
            ):
                self.assertTrue(
                    writer_image_matches((release, launcher), str(base))
                )
                self.assertTrue(
                    writer_command_matches((release, launcher), command)
                )
                self.assertIsNone(
                    writer_mismatch(
                        (release, launcher),
                        {"exe": str(base), "parent": 15},
                        _LAUNCHER,
                        command,
                        _TASK,
                        {11},
                        redirector={
                            **_REDIRECTOR,
                            "exe": str(launcher),
                            "cmd": command,
                        },
                    )
                )
                valid_hop = {
                    **_REDIRECTOR,
                    "exe": str(launcher),
                    "cmd": command,
                }
                for changed in (
                    {"exe": str(base)},
                    {"cmd": f'"{base}" {command}'},
                    {"parent": 99},
                    {"pid": 99},
                ):
                    with self.subTest(changed=changed):
                        self.assertEqual(
                            writer_mismatch(
                                (release, launcher),
                                {"exe": str(base), "parent": 15},
                                _LAUNCHER,
                                command,
                                _TASK,
                                {11},
                                redirector={**valid_hop, **changed},
                            ),
                            10,
                        )

    def test_redirector_rejects_other_command_or_config(self) -> None:
        """An unrelated command or missing, changed cfg cannot borrow trust."""
        with TemporaryDirectory() as temporary:
            release = Path(temporary) / "release"
            venv = release / ".venv"
            launcher = venv / "Scripts/python.exe"
            base = Path(temporary) / "Python 3.13/python.exe"
            launcher.parent.mkdir(parents=True)
            base.parent.mkdir()
            launcher.write_bytes(b"launcher")
            base.write_bytes(b"base")
            cfg = venv / "pyvenv.cfg"
            cfg.write_text(f"home = {base.parent}\nexecutable = {base}\n")
            good = f'"{launcher}" -I -B -m muninn serve'
            with mock.patch.object(platform_io, "assert_private_fd"):
                self.assertFalse(
                    writer_command_matches(
                        (release, launcher), f'"{base}" {good}'
                    )
                )
                other = Path(temporary) / "other"
                cfg.write_text(f"home = {other}\nexecutable = {base}\n")
                self.assertFalse(
                    writer_image_matches((release, launcher), str(base))
                )
                cfg.unlink()
                self.assertFalse(
                    writer_image_matches((release, launcher), str(base))
                )
