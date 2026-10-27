"""Compare diagnostic native-call ordering with pure original-call fakes."""

from __future__ import annotations

import subprocess
import unittest
from collections.abc import Iterator
from unittest import mock

from muninn import review_process_quiescence as original
from tools import test_process as baseline
from tools.pr68_observation_control_loader import native as observed
from tools.pr68_observation_control_loader import observation as obs
from tools.pr68_observation_control_loader import process


class CleanupPathControls(unittest.TestCase):
    """Exercise decisions while mocking every native process operation."""

    def test_four_timeout_paths_keep_calls_and_actual_reap(self) -> None:
        """Check four timeout paths keep calls and actual reap."""
        cases = (
            ("before_observer", [0.0, 5.0], False, False),
            ("observer_false", [0.0, 1.0, 5.0], False, False),
            ("late_observer_true", [0.0, 1.0, 5.0], True, False),
            ("late_wait_reaped", [0.0, 1.0, 1.2, 5.0], True, True),
        )
        for reason, samples, result, reaped in cases:
            with self.subTest(reason=reason):
                expected_calls: list[tuple[str, object]] = []
                actual_calls: list[tuple[str, object]] = []
                for module, calls in (
                    (baseline, expected_calls),
                    (process, actual_calls),
                ):
                    child = mock.Mock(pid=10, args=("fake",), returncode=None)

                    def signal_group(
                        pgid: int,
                        sig: int,
                        record: list[tuple[str, object]] = calls,
                    ) -> None:
                        record.append(("killpg", (pgid, sig)))

                    def verify(
                        pgid: int,
                        record: list[tuple[str, object]] = calls,
                        decision: bool = result,
                    ) -> bool:
                        record.append(("observe", pgid))
                        return decision

                    def wait(
                        timeout: float,
                        record: list[tuple[str, object]] = calls,
                        selected: mock.Mock = child,
                    ) -> int:
                        record.append(("wait", timeout))
                        selected.returncode = 0
                        return 0

                    child.wait.side_effect = wait
                    token = obs.begin()
                    token.trace.meta["pid"] = 10
                    try:
                        with (
                            mock.patch.object(module.os, "name", "posix"),
                            mock.patch.object(
                                module.os,
                                "killpg",
                                side_effect=signal_group,
                                create=True,
                            ),
                            mock.patch.object(
                                module, "group_quiescent", side_effect=verify
                            ),
                            mock.patch.object(
                                module.time, "monotonic", side_effect=samples
                            ),
                            mock.patch.object(
                                module.time,
                                "sleep",
                                side_effect=AssertionError("extra sleep"),
                            ),
                            self.assertRaises(
                                subprocess.TimeoutExpired
                            ) as caught,
                        ):
                            module._contain(child)
                        if module is process:
                            obs.outer_error(caught.exception)
                            self.assertEqual(
                                token.trace.meta["signal"], "returned"
                            )
                            self.assertEqual(
                                token.trace.meta["timeout_path"], reason
                            )
                            self.assertEqual(
                                token.trace.meta["actual_reaped"], reaped
                            )
                            self.assertEqual(
                                child.returncode is not None, reaped
                            )
                    finally:
                        obs.restore(token)
                self.assertEqual(actual_calls, expected_calls)

    def test_counter_refusal_preserves_native_order_and_short_circuit(
        self,
    ) -> None:
        """Check counter refusal preserves native order and short."""
        expected: list[tuple[str, object]] = []
        actual: list[tuple[str, object]] = []
        for module, calls in ((original, expected), (observed, actual)):
            counter_values = iter(((1, 100), (1, 101)))

            def counter(
                record: list[tuple[str, object]] = calls,
                values: Iterator[tuple[int, int]] = counter_values,
            ) -> tuple[int, int]:
                record.append(("counter", ()))
                return next(values)

            def terminal(
                pgid: int, record: list[tuple[str, object]] = calls
            ) -> bool:
                record.append(("terminal", pgid))
                return True

            def members(
                pgid: int,
                deadline: float,
                record: list[tuple[str, object]] = calls,
            ) -> dict[int, tuple[str, int, int]]:
                record.append(("members", (pgid, deadline)))
                return {10: ("Z", 1, 20)}

            def visible(record: list[tuple[str, object]] = calls) -> bool:
                record.append(("visible", ()))
                return True

            token = obs.begin()
            token.trace.meta["pid"] = 10
            obs.attempt(10)
            try:
                with (
                    mock.patch.object(module.sys, "platform", "linux"),
                    mock.patch.object(
                        module.struct, "calcsize", return_value=8
                    ),
                    mock.patch.object(module, "_visible", side_effect=visible),
                    mock.patch.object(module, "_counter", side_effect=counter),
                    mock.patch.object(
                        module, "_leader_terminal", side_effect=terminal
                    ),
                    mock.patch.object(module, "_members", side_effect=members),
                    mock.patch.object(
                        module.time, "monotonic", side_effect=[0.0, 0.01]
                    ),
                ):
                    self.assertFalse(module.group_quiescent(10))
                if module is observed:
                    row = token.trace.records[0]
                    self.assertFalse(row["counter_equal"])
                    self.assertEqual(row["leader_present"], "not_evaluated")
                    self.assertEqual(row["maps_equal"], "not_evaluated")
            finally:
                obs.restore(token)
        self.assertEqual(actual, expected)
        self.assertEqual(
            actual,
            [
                ("visible", ()),
                ("counter", ()),
                ("terminal", 10),
                ("members", (10, 0.1)),
                ("members", (10, 0.1)),
                ("terminal", 10),
                ("counter", ()),
            ],
        )

    def test_visibility_and_error_refusals_leave_later_operands_unvisited(
        self,
    ) -> None:
        """Check visibility and error refusals leave later."""
        for failure in (False, ValueError("private unexportable details")):
            with self.subTest(failure=type(failure).__name__):
                token = obs.begin()
                token.trace.meta["pid"] = 10
                obs.attempt(10)
                try:
                    with (
                        mock.patch.object(observed.sys, "platform", "linux"),
                        mock.patch.object(
                            observed.struct, "calcsize", return_value=8
                        ),
                        mock.patch.object(
                            observed,
                            "_visible",
                            side_effect=(
                                failure
                                if isinstance(failure, Exception)
                                else None
                            ),
                            return_value=False,
                        ),
                        mock.patch.object(
                            observed,
                            "_counter",
                            side_effect=AssertionError("extra counter"),
                        ),
                        mock.patch.object(
                            observed.time, "monotonic", return_value=0.0
                        ),
                    ):
                        self.assertFalse(observed.group_quiescent(10))
                    row = token.trace.records[0]
                    self.assertEqual(row["counter_equal"], obs.MISSING)
                    self.assertEqual(row["terminal_a"], obs.MISSING)
                    self.assertNotIn(
                        b"private unexportable", obs.document(token.trace)
                    )
                    self.assertEqual(
                        row["reason"],
                        (
                            "exception"
                            if isinstance(failure, Exception)
                            else "visible"
                        ),
                    )
                finally:
                    obs.restore(token)

    def test_terminal_iteration_stops_at_first_bad_member(self) -> None:
        """Check terminal iteration stops at first bad member."""
        token = obs.begin()
        token.trace.meta["pid"] = 10
        obs.attempt(10)
        members = {10: ("Z", 1, 1), 11: ("R", 99, 2), 12: ("Z", 1, 3)}
        try:
            with (
                mock.patch.object(observed.sys, "platform", "linux"),
                mock.patch.object(observed.struct, "calcsize", return_value=8),
                mock.patch.object(observed, "_visible", return_value=True),
                mock.patch.object(observed, "_counter", return_value=(1, 100)),
                mock.patch.object(
                    observed, "_leader_terminal", return_value=True
                ),
                mock.patch.object(observed, "_members", return_value=members),
                mock.patch.object(
                    observed.time, "monotonic", side_effect=[0.0, 0.01]
                ),
            ):
                self.assertFalse(observed.group_quiescent(10))
            self.assertEqual(token.trace.records[0]["member_checked"], 2)
            self.assertEqual(
                token.trace.records[0]["offender_state"], "nonterminal"
            )
        finally:
            obs.restore(token)


if __name__ == "__main__":
    unittest.main()
