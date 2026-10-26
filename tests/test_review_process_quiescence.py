"""Refuse incomplete or active native process group observations."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from muninn import review_process_quiescence as quiescence


def stat(pid: int = 10, state: str = "Z", threads: int = 1) -> bytes:
    """Render relevant native fields with a deliberately awkward command."""
    fields = [state, "1", "10", "10"] + ["0"] * 16
    fields[17] = str(threads)
    fields[19] = "123"
    return f"{pid} (a ) tricky) {' '.join(fields)}".encode()


class GroupObservationTests(unittest.TestCase):
    """Keep unknown native state from becoming cleanup authority."""

    def test_parse_terminal_identity_and_thread_count(self) -> None:
        self.assertEqual(quiescence._parse(stat(), 10), (10, 10, "Z", 1, 123))
        self.assertEqual(
            quiescence._parse(stat(state="R", threads=2), 10),
            (10, 10, "R", 2, 123),
        )

    def test_malformed_stat_refused(self) -> None:
        for raw in (
            b"",
            b"10 (a) Z",
            stat().replace(b" (a ) tricky", b""),
            stat(pid=11),
            stat(threads=0),
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                quiescence._parse(raw, 10)

    def test_linux_requires_matching_terminal_members_with_leader(
        self,
    ) -> None:
        terminal = {10: ("Z", 1, 123), 11: ("X", 1, 124)}
        cases = (
            (terminal, terminal, True),
            ({10: ("Z", 2, 123)}, {10: ("Z", 2, 123)}, False),
            ({10: ("R", 1, 123)}, {10: ("R", 1, 123)}, False),
            ({11: ("Z", 1, 123)}, {11: ("Z", 1, 123)}, False),
            (terminal, {10: ("Z", 1, 123)}, False),
            ({10: ("Z", 1, 123)}, {10: ("Z", 1, 125)}, False),
            ({}, {}, False),
        )
        for first, second, expected in cases:
            with (
                self.subTest(first=first, second=second),
                mock.patch.object(quiescence.sys, "platform", "linux"),
                mock.patch.object(
                    quiescence.struct, "calcsize", return_value=8
                ),
                mock.patch.object(quiescence, "_visible", return_value=True),
                mock.patch.object(
                    quiescence, "_leader_terminal", return_value=True
                ),
                mock.patch.object(
                    quiescence, "_counter", return_value=(1, 100)
                ),
                mock.patch.object(
                    quiescence, "_members", side_effect=[first, second]
                ),
            ):
                self.assertEqual(quiescence.group_quiescent(10), expected)

    def test_unknown_platform_and_observation_errors_refused(self) -> None:
        with mock.patch.object(quiescence.sys, "platform", "unknown"):
            self.assertFalse(quiescence.group_quiescent(10))
        for error in (PermissionError(), ValueError(), TimeoutError()):
            with (
                self.subTest(error=error),
                mock.patch.object(quiescence.sys, "platform", "linux"),
                mock.patch.object(
                    quiescence.struct, "calcsize", return_value=8
                ),
                mock.patch.object(quiescence, "_visible", side_effect=error),
            ):
                self.assertFalse(quiescence.group_quiescent(10))

    def test_mount_visibility_rejects_hiding_and_numeric_masks(self) -> None:
        root = "1 0 0:1 / /proc rw - proc proc rw"
        cases = (
            (root, True),
            (root + "\n2 1 0:2 / /møunt rw - tmpfs x rw", True),
            (root + ",hidepid=0", True),
            (root + ",hidepid=2", False),
            (root + ",hidepid=invisible", False),
            (root.replace("- proc", "- tmpfs"), False),
            (root + "\n2 1 0:2 / /proc/10 rw - tmpfs x rw", False),
            (root + "\n2 1 0:2 / /proc/self/stat rw - tmpfs x rw", False),
            (root + "\n" + root, False),
            ("malformed", False),
        )
        for mountinfo, expected in cases:
            with self.subTest(mountinfo=mountinfo):
                self.assertEqual(
                    quiescence._mount_visible(mountinfo), expected
                )

    def test_fork_during_observation_refuses_terminal_snapshot(self) -> None:
        terminal = {10: ("Z", 1, 123)}
        with (
            mock.patch.object(quiescence.sys, "platform", "linux"),
            mock.patch.object(quiescence.struct, "calcsize", return_value=8),
            mock.patch.object(quiescence, "_visible", return_value=True),
            mock.patch.object(
                quiescence, "_leader_terminal", return_value=True
            ),
            mock.patch.object(quiescence, "_members", return_value=terminal),
            mock.patch.object(
                quiescence, "_counter", side_effect=[(1, 100), (1, 101)]
            ),
        ):
            self.assertFalse(quiescence.group_quiescent(10))

    @unittest.skipIf(os.name == "nt", "requires native WNOWAIT")
    def test_leader_observation_never_reaps(self) -> None:
        if os.name == "nt":
            raise unittest.SkipTest("requires WNOWAIT")
        result = mock.Mock(si_pid=10, si_code=os.CLD_KILLED)
        with mock.patch.object(
            quiescence.os, "waitid", return_value=result
        ) as wait:
            self.assertTrue(quiescence._leader_terminal(10))
        wait.assert_called_once_with(
            os.P_PID, 10, os.WEXITED | os.WNOHANG | os.WNOWAIT
        )
        with mock.patch.object(quiescence.os, "waitid", return_value=None):
            self.assertFalse(quiescence._leader_terminal(10))

    def test_counter_requires_exact_fresh_nonnegative_fields(self) -> None:
        cases = (
            (b"btime 10\nprocesses 100\n", (10, 100)),
            (b"btime 10\nprocesses -1\n", None),
            (b"btime 10\nprocesses 100\nprocesses 101\n", None),
            (b"btime 10\n", None),
        )
        for raw, expected in cases:
            with (
                self.subTest(raw=raw),
                mock.patch.object(quiescence, "_read", return_value=raw),
            ):
                if expected is None:
                    with self.assertRaises((ValueError, KeyError)):
                        quiescence._counter()
                else:
                    self.assertEqual(quiescence._counter(), expected)

    def test_kernel_thread_zero_session_is_valid_non_target(self) -> None:
        raw = stat(state="I").split(b") ")
        fields = raw[-1].split()
        fields[2:4] = [b"0", b"0"]
        native = b") ".join(raw[:-1]) + b") " + b" ".join(fields)
        self.assertEqual(quiescence._parse(native, 10), (0, 0, "I", 1, 123))

    def test_visible_requires_unique_single_namespace_pid(self) -> None:
        if os.name == "nt":
            raise unittest.SkipTest("requires POSIX namespace APIs")
        mounts = (
            "1 0 0:1 / /proc rw - proc proc rw\n"
            "2 1 0:2 / /møunt rw - tmpfs x rw\n"
        )
        cases = (
            (b"NSpid:\t42\n", True),
            (b"NSpid:\t42 1\n", False),
            (b"NSpid:\t42\nNSpid:\t42\n", False),
            (b"NSpid:\tbad\n", False),
            (b"NSpid:\t0\n", False),
            (b"Pid:\t42\n", False),
        )
        for status, expected in cases:
            with (
                self.subTest(status=status),
                mock.patch.object(quiescence.os, "getpid", return_value=42),
                mock.patch.object(quiescence.os, "getpgrp", return_value=10),
                mock.patch.object(quiescence.os, "getsid", return_value=10),
                mock.patch.object(
                    quiescence.Path,
                    "readlink",
                    return_value=quiescence.Path("42"),
                ),
                mock.patch.object(
                    quiescence,
                    "_read",
                    side_effect=[mounts.encode(), status, stat(pid=42)],
                ),
            ):
                self.assertEqual(quiescence._visible(), expected)

    def test_member_observation_permission_error_refuses(self) -> None:
        with (
            mock.patch.object(quiescence.sys, "platform", "linux"),
            mock.patch.object(quiescence.struct, "calcsize", return_value=8),
            mock.patch.object(quiescence, "_visible", return_value=True),
            mock.patch.object(
                quiescence, "_leader_terminal", return_value=True
            ),
            mock.patch.object(quiescence, "_counter", return_value=(1, 100)),
            mock.patch.object(
                quiescence, "_members", side_effect=PermissionError()
            ),
        ):
            self.assertFalse(quiescence.group_quiescent(10))
