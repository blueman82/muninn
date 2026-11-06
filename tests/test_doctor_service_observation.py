"""Check macOS doctor observations without invoking native commands."""

from __future__ import annotations

import unittest
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest import mock

from muninn import obs

PRIVATE = b"PRIVATE_SENTINEL"
HEALTHY = b"state = running\n\tpid = 4242\n"
CASES: tuple[tuple[str, int | OSError, bytes, bool | None, str], ...] = (
    (
        "launchctl",
        PermissionError(1, "PRIVATE_SENTINEL"),
        PRIVATE,
        None,
        "launchctl_unavailable",
    ),
    (
        "launchctl",
        FileNotFoundError("PRIVATE_SENTINEL"),
        PRIVATE,
        None,
        "launchctl_unavailable",
    ),
    ("launchctl", 127, PRIVATE, None, "launchctl_unavailable"),
    ("launchctl", 124, PRIVATE, False, "launchctl_timeout"),
    ("launchctl", 113, PRIVATE, False, "launchctl_failed"),
    ("launchctl", 1, PRIVATE, False, "launchctl_failed"),
    ("launchctl", 0, b"", None, "launchctl_pid_unavailable"),
    ("launchctl", 0, PRIVATE, None, "launchctl_pid_unavailable"),
    ("launchctl", 0, b"pid = 0\n", None, "launchctl_pid_unavailable"),
    ("launchctl", 0, b"pid = -1\n", None, "launchctl_pid_unavailable"),
    (
        "launchctl",
        0,
        b"pid = 4242\npid = 4243\n",
        None,
        "launchctl_pid_unavailable",
    ),
    (
        "launchctl",
        0,
        b"pid = 4242\npid = 4242\n",
        None,
        "launchctl_pid_unavailable",
    ),
    (
        "ps",
        PermissionError(1, "PRIVATE_SENTINEL"),
        PRIVATE,
        None,
        "process_inspection_unavailable",
    ),
    (
        "ps",
        FileNotFoundError("PRIVATE_SENTINEL"),
        PRIVATE,
        None,
        "process_inspection_unavailable",
    ),
    ("ps", 127, PRIVATE, None, "process_inspection_unavailable"),
    ("ps", 127, b"4242\n", None, "process_inspection_unavailable"),
    ("ps", 124, PRIVATE, None, "process_inspection_timeout"),
    ("ps", 124, b"4242\n", None, "process_inspection_timeout"),
    ("ps", 1, PRIVATE, None, "process_inspection_failed"),
    ("ps", 1, b"4242\n", None, "process_inspection_failed"),
    ("ps", 0, b"", None, "process_pid_unverified"),
    ("ps", 0, b" \n\t", None, "process_pid_unverified"),
    ("ps", 0, b"4243\n", None, "process_pid_unverified"),
    ("ps", 0, b"4242\n4242\n", None, "process_pid_unverified"),
    ("ps", 0, b"4242\n4243\n", None, "process_pid_unverified"),
    ("ps", 0, PRIVATE, None, "process_pid_unverified"),
    ("ps", 0, b"4242 PRIVATE_SENTINEL", None, "process_pid_unverified"),
    ("ps", 0, b" 4242\n", True, "4242"),
)


class LaunchdObservationTests(unittest.TestCase):
    """Preserve unknown status and disclose only codes or verified PIDs."""

    def test_windows_does_not_inspect_launchd(self) -> None:
        with (
            mock.patch.object(obs, "run") as run,
            mock.patch.object(obs, "sys", SimpleNamespace(platform="win32")),
            mock.patch.object(obs, "os", SimpleNamespace()),
        ):
            result = obs._launchd_job()
        self.assertEqual(
            result,
            {
                "check": "launchd_job",
                "ok": False,
                "level": "error",
                "detail": "native scheduler pending",
            },
        )
        run.assert_not_called()

    def test_native_result_matrix(self) -> None:
        def uid() -> int:
            return 501

        for stage, status, output, ok, detail in CASES:
            with self.subTest(stage=stage, status=status, output=output):
                native = (
                    status
                    if isinstance(status, OSError)
                    else CompletedProcess([], status, output, PRIVATE)
                )
                responses = [native]
                if stage == "ps":
                    responses.insert(
                        0, CompletedProcess([], 0, HEALTHY, PRIVATE)
                    )
                with (
                    mock.patch.object(
                        obs, "run", side_effect=responses
                    ) as run,
                    mock.patch.object(
                        obs, "sys", SimpleNamespace(platform="darwin")
                    ),
                    mock.patch.object(obs, "os", SimpleNamespace(getuid=uid)),
                ):
                    result = obs._launchd_job()
                self.assertEqual(
                    result,
                    {
                        "check": "launchd_job",
                        "ok": ok,
                        "level": "error",
                        "detail": detail,
                    },
                )
                expected = [
                    mock.call(["launchctl", "print", "gui/501/com.muninn"])
                ]
                if stage == "ps":
                    expected.append(
                        mock.call(["ps", "-o", "pid=", "-p", "4242"])
                    )
                self.assertEqual(run.call_args_list, expected)
