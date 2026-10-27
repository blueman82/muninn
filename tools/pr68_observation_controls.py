"""Run pure diagnostic cancellation, bounded-storage and export controls."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import NoReturn
from unittest import mock

import muninn
from tools.pr68_observation_control_loader import load, native, process
from tools.pr68_observation_control_loader import observation as obs

ARGV = ("python", "-m", "unittest", "tests.test_install_legacy_record_context")


class StopControl(BaseException):
    """Represent cancellation outside the ordinary Exception hierarchy."""


class CollectorControls(unittest.TestCase):
    """Check bounded scalars and original-result preservation without IO."""

    def test_payloads_share_one_logical_collector_and_restore_aliases(
        self,
    ) -> None:
        """Restore prior import state after binding both fixture copies."""
        prior = ModuleType("prior_collector")
        self.assertIs(native.observation, obs)
        self.assertIs(process.observation, obs)
        with (
            mock.patch.dict(
                sys.modules, {"muninn._cleanup_observation": prior}
            ),
            mock.patch.object(
                muninn, "_cleanup_observation", prior, create=True
            ),
        ):
            collector, helper, caller = load()
            self.assertIs(helper.observation, collector)
            self.assertIs(caller.observation, collector)
            self.assertIs(sys.modules["muninn._cleanup_observation"], prior)
            self.assertIs(muninn.__dict__["_cleanup_observation"], prior)
            self.assertNotIn("_pr68_native_controls", sys.modules)
            self.assertNotIn("_pr68_process_controls", sys.modules)

    def test_first_eight_and_last_fifty_six_are_fixed(self) -> None:
        """Check first eight and last fifty six are fixed."""
        token = obs.begin()
        try:
            trace = token.trace
            trace.meta["pid"] = 10
            identities = tuple(id(row) for row in trace.records)
            for _ in range(100):
                obs.attempt(10)
                obs.finish(False)
            self.assertEqual(
                tuple(id(row) for row in trace.records), identities
            )
            document = json.loads(obs.document(trace))
            self.assertEqual(document["dropped_attempts"], 36)
            self.assertTrue(document["incomplete"])
            self.assertEqual(document["reason_counts"]["unknown"], 100)
            self.assertEqual(
                [row["attempt"] for row in document["attempts"]],
                [*range(1, 9), *range(45, 101)],
            )
        finally:
            obs.restore(token)

    def test_encoding_limit_remains_bounded_and_incomplete(self) -> None:
        """Check encoding limit remains bounded and incomplete."""
        token = obs.begin()
        try:
            token.trace.meta["stage"] = "x" * 100000
            encoded = obs.document(token.trace)
            self.assertLessEqual(len(encoded), 65536)
            self.assertEqual(json.loads(encoded)["reason"], "encoding_limit")
            self.assertTrue(json.loads(encoded)["incomplete"])
        finally:
            obs.restore(token)

    def test_export_failure_preserves_exact_uncapped_result(self) -> None:
        """Check export failure preserves exact uncapped result."""
        expected = (127, "captured" * 30000, False)
        token = obs.begin()
        try:
            with (
                mock.patch.object(
                    process, "_observed_execute", return_value=expected
                ),
                mock.patch.object(
                    obs, "export", side_effect=OSError(13, "refused")
                ),
                mock.patch.object(obs, "export_failure") as failed,
            ):
                actual = process.execute(
                    ARGV, Path(), {}, 600, threading.Event()
                )
            self.assertIs(actual, expected)
            self.assertIs(obs.current(), token.trace)
            failed.assert_called_once()
        finally:
            obs.restore(token)

    def test_original_cancellation_shapes_do_not_export(self) -> None:
        """Check original cancellation shapes do not export."""
        for initial in (False, True):
            with self.subTest(initial=initial):
                cancel = mock.Mock()
                cancel.is_set.return_value = True
                expected = (130, "cancelled-output", True)
                target = (
                    "_original_execute" if initial else "_observed_execute"
                )
                with (
                    mock.patch.object(process, target, return_value=expected),
                    mock.patch.object(obs, "export") as export,
                ):
                    if initial:
                        actual = process.execute(ARGV, Path(), {}, 600, cancel)
                        self.assertEqual(actual, (130, "", True))
                    else:
                        actual = process.execute(ARGV, Path(), {}, 600, cancel)
                        self.assertIs(actual, expected)
                        cancel.is_set.assert_not_called()
                export.assert_not_called()
                self.assertIsNone(obs.current())

    def test_pending_baseexception_restores_without_export(self) -> None:
        """Check pending baseexception restores without export."""
        for kind in (KeyboardInterrupt, SystemExit, StopControl):
            for seam in ("outer", "observer"):
                with self.subTest(kind=kind.__name__, seam=seam):
                    error = kind("cancel")
                    token = obs.begin()

                    def origin(
                        *args: object, failure: BaseException = error
                    ) -> NoReturn:
                        raise failure

                    def original(
                        *args: object,
                        location: str = seam,
                        raise_from: Callable[..., NoReturn] = origin,
                    ) -> tuple[int, str, bool]:
                        if location == "outer":
                            raise_from()
                        obs.mark("pid", 10)
                        obs.attempt(10)
                        with (
                            mock.patch.object(native.sys, "platform", "linux"),
                            mock.patch.object(
                                native.struct, "calcsize", return_value=8
                            ),
                            mock.patch.object(
                                native,
                                "_observed_linux",
                                side_effect=raise_from,
                            ),
                        ):
                            native.group_quiescent(10)
                        raise AssertionError("exception was converted")

                    try:
                        with (
                            mock.patch.object(
                                process,
                                "_observed_execute",
                                side_effect=original,
                            ),
                            mock.patch.object(obs, "export") as export,
                        ):
                            try:
                                process.execute(
                                    ARGV, Path(), {}, 600, threading.Event()
                                )
                            except BaseException as caught:
                                self.assertIs(caught, error)
                                traceback = caught.__traceback__
                                frames: list[str] = []
                                while traceback is not None:
                                    frames.append(
                                        traceback.tb_frame.f_code.co_name
                                    )
                                    traceback = traceback.tb_next
                                self.assertEqual(frames.count("execute"), 1)
                                self.assertEqual(frames[-1], "origin")
                            else:
                                self.fail(
                                    "original cancellation was swallowed"
                                )
                        self.assertIs(obs.current(), token.trace)
                        export.assert_not_called()
                    finally:
                        obs.restore(token)

    def test_new_export_cancellation_propagates(self) -> None:
        """Check new export cancellation propagates."""
        error = StopControl("export-cancel")
        with (
            mock.patch.object(
                process, "_observed_execute", return_value=(0, "ok", False)
            ),
            mock.patch.object(obs, "export", side_effect=error),
            mock.patch.object(obs, "export_failure") as failed,
            self.assertRaises(StopControl) as caught,
        ):
            process.execute(ARGV, Path(), {}, 600, threading.Event())
        self.assertIs(caught.exception, error)
        self.assertIsNone(obs.current())
        failed.assert_not_called()

    def test_original_reads_are_uncapped_and_not_repeated(self) -> None:
        """Check original reads are uncapped and not repeated."""
        token = obs.begin()
        stdout, stderr = mock.MagicMock(), mock.MagicMock()
        for stream in (stdout, stderr):
            stream.__enter__.return_value = stream
        stdout.read.return_value = b"a" * 200000
        stderr.read.return_value = b"b" * 200000
        temporary = mock.MagicMock()
        temporary.__enter__.return_value = "fake-home"
        paths = mock.MagicMock()
        paths.__truediv__.return_value.open.side_effect = [stdout, stderr]
        try:
            with (
                mock.patch.object(
                    process.tempfile,
                    "TemporaryDirectory",
                    return_value=temporary,
                ),
                mock.patch.object(process, "Path", return_value=paths),
                mock.patch.object(
                    process, "observed_posix", return_value=(0, False)
                ),
                mock.patch.object(process.os, "name", "posix"),
                mock.patch.object(
                    process.locale, "getencoding", return_value="utf-8"
                ),
                mock.patch.object(process.time, "monotonic", return_value=0.0),
                mock.patch.object(obs, "export") as exported,
            ):
                result = process.execute(
                    ARGV, Path(), {}, 600, mock.Mock(is_set=lambda: False)
                )
            self.assertEqual(result, (0, "a" * 200000 + "b" * 200000, False))
            stdout.read.assert_called_once_with()
            stderr.read.assert_called_once_with()
            trace = exported.call_args.args[0]
            self.assertIsInstance(trace, obs.Trace)
            self.assertEqual(len(trace.stdout), 200000)
            self.assertEqual(len(trace.stderr), 200000)
        finally:
            obs.restore(token)


if __name__ == "__main__":
    unittest.main()
