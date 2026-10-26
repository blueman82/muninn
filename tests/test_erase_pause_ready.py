"""Bound publication waits and preserve original fixture startup errors."""

from __future__ import annotations

import errno
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

from tests import erase_pause_ready as ready
from tests import test_erase_pause as erasure


class ReadyTests(unittest.TestCase):
    """Exercise transient and permanent fixture startup failures."""

    def setUp(self) -> None:
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        """Advance only the programmed fixture-read clock."""
        self.sleeps.append(seconds)
        self.elapsed += seconds

    def clock(self) -> SimpleNamespace:
        """Keep the original deadline independent of test scheduling."""
        return SimpleNamespace(
            monotonic=lambda: self.elapsed, sleep=self.sleep
        )

    def test_transient_creation_and_access_finish_with_fresh_contents(
        self,
    ) -> None:
        with (
            mock.patch.object(ready, "time", self.clock()),
            mock.patch.object(
                Path,
                "read_text",
                side_effect=[
                    FileNotFoundError(errno.ENOENT, "not published"),
                    PermissionError(errno.EACCES, "publication in progress"),
                    '{"ready": true}',
                ],
            ) as read,
        ):
            self.assertEqual(
                ready.read_ready_text(Path("ready.json")), '{"ready": true}'
            )
        self.assertEqual(read.call_count, 3)
        self.assertAlmostEqual(self.elapsed, 0.02)

    def test_permanent_access_failure_keeps_original_error_and_cause(
        self,
    ) -> None:
        original = PermissionError(errno.EACCES, "not readable")
        cause = LookupError("native publication cause")
        original.__cause__ = cause
        with (
            mock.patch.object(ready, "time", self.clock()),
            mock.patch.object(Path, "read_text", side_effect=original),
            self.assertRaises(PermissionError) as caught,
        ):
            ready.read_ready_text(Path("ready.json"))
        self.assertIs(caught.exception, original)
        self.assertIs(caught.exception.__cause__, cause)
        self.assertAlmostEqual(self.elapsed, 8)

    def test_other_read_errors_propagate_without_waiting(self) -> None:
        for original in (
            OSError(errno.EIO, "unexpected I/O"),
            PermissionError(errno.EPERM, "unexpected permission failure"),
        ):
            with (
                self.subTest(error=repr(original)),
                mock.patch.object(ready, "time", self.clock()),
                mock.patch.object(
                    Path, "read_text", side_effect=original
                ) as read,
                self.assertRaises(type(original)) as caught,
            ):
                ready.read_ready_text(Path("ready.json"))
            self.assertIs(caught.exception, original)
            read.assert_called_once()
        self.assertEqual(self.sleeps, [])

    def test_absent_marker_keeps_original_error_at_the_deadline(self) -> None:
        original = FileNotFoundError(errno.ENOENT, "marker never published")
        with (
            mock.patch.object(ready, "time", self.clock()),
            mock.patch.object(Path, "read_text", side_effect=original),
            self.assertRaises(FileNotFoundError) as caught,
        ):
            ready.read_ready_text(Path("ready.json"))
        self.assertIs(caught.exception, original)
        self.assertAlmostEqual(self.elapsed, 8)

    def test_oversleep_refuses_before_a_late_successful_retry(self) -> None:
        original = PermissionError(errno.EACCES, "not readable yet")

        def oversleep(seconds: float) -> None:
            self.elapsed = 9

        with (
            mock.patch.object(
                ready,
                "time",
                SimpleNamespace(
                    monotonic=lambda: self.elapsed, sleep=oversleep
                ),
            ),
            mock.patch.object(
                Path, "read_text", side_effect=[original, "late"]
            ) as read,
            self.assertRaises(PermissionError) as caught,
        ):
            ready.read_ready_text(Path("ready.json"))
        self.assertIs(caught.exception, original)
        read.assert_called_once()

    def test_late_read_preserves_the_previous_native_error(self) -> None:
        original = PermissionError(errno.EACCES, "not readable yet")
        attempts = [0]

        def read(*args: object, **kwargs: object) -> str:
            attempts[0] += 1
            if attempts[0] == 1:
                raise original
            self.elapsed = 9
            return "late"

        with (
            mock.patch.object(ready, "time", self.clock()),
            mock.patch.object(Path, "read_text", side_effect=read),
            self.assertRaises(PermissionError) as caught,
        ):
            ready.read_ready_text(Path("ready.json"))
        self.assertIs(caught.exception, original)

    def test_first_read_finishing_after_deadline_cannot_start_the_owner(
        self,
    ) -> None:
        def read(*args: object, **kwargs: object) -> str:
            self.elapsed = 9
            return "late"

        with (
            mock.patch.object(ready, "time", self.clock()),
            mock.patch.object(Path, "read_text", side_effect=read),
            self.assertRaises(TimeoutError),
        ):
            ready.read_ready_text(Path("ready.json"))

    def test_cleanup_requires_success_only_after_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "release"
            child = mock.Mock(spec=subprocess.Popen)
            child.returncode = 1
            child.communicate.return_value = (
                b"",
                b"no startup acknowledgement",
            )
            ready.finish_owner(
                cast(subprocess.Popen[bytes], child), release, started=False
            )
            self.assertTrue(release.exists())
            child.communicate.assert_called_once_with(timeout=10)
            child.kill.assert_not_called()
            with self.assertRaises(AssertionError):
                ready.finish_owner(
                    cast(subprocess.Popen[bytes], child), release, started=True
                )

    def test_cleanup_retains_the_existing_bounded_kill_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            child = mock.Mock(spec=subprocess.Popen)
            child.returncode = 0
            child.communicate.side_effect = [
                subprocess.TimeoutExpired("fixture", 10),
                (b"", b""),
            ]
            ready.finish_owner(
                cast(subprocess.Popen[bytes], child),
                Path(temporary) / "release",
                started=True,
            )
            child.kill.assert_called_once()
            self.assertEqual(
                child.communicate.call_args_list,
                [mock.call(timeout=10), mock.call(timeout=5)],
            )

    def test_real_startup_read_failure_preserves_error_and_finishes_cleanup(
        self,
    ) -> None:
        case = erasure.ErasePauseTests()
        case.setUp()
        original = PermissionError(errno.EACCES, "fixture marker unavailable")

        def fail_ready(path: Path) -> str:
            ready.read_ready_text(path)
            raise original

        try:
            with (
                mock.patch.object(
                    erasure, "read_ready_text", side_effect=fail_ready
                ),
                self.assertRaises(PermissionError) as caught,
            ):
                case.start_owner()
            self.assertIs(caught.exception, original)
            self.assertTrue(case.doCleanups())
        finally:
            case.doCleanups()


if __name__ == "__main__":
    unittest.main()
