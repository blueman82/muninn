"""Reject late fake-clock results without launching or observing processes."""

from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest import mock

from tools import darwin_exit_watch as watch


class DeadlineTests(unittest.TestCase):
    """Exercise real deadline decisions with all native operations replaced."""

    def setUp(self) -> None:
        fixtures: dict[str, object] = {
            "KQ_FILTER_PROC": -5,
            "KQ_NOTE_EXIT": 2147483648,
            "KQ_EV_ADD": 1,
            "KQ_EV_ENABLE": 4,
            "KQ_EV_ONESHOT": 16,
            "KQ_EV_ERROR": 16384,
            "kqueue": mock.Mock(),
            "kevent": mock.Mock(),
        }
        for name, value in fixtures.items():
            patcher = mock.patch.object(watch.select, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_selector_oversleep_performs_no_read_or_write(self) -> None:
        for operation in ("read", "write"):
            with self.subTest(operation=operation):
                clock = [0.0]
                selector = mock.MagicMock()
                selector.__enter__.return_value = selector

                def oversleep(
                    timeout: float, state: list[float] = clock
                ) -> list[tuple[int, int]]:
                    state[0] = 2.0
                    return [(1, 1)]

                def now(state: list[float] = clock) -> float:
                    return state[0]

                selector.select.side_effect = oversleep
                with (
                    mock.patch.object(watch.time, "monotonic", now),
                    mock.patch.object(
                        watch.selectors,
                        "DefaultSelector",
                        return_value=selector,
                    ),
                    mock.patch.object(
                        watch.os, "read", return_value=b"OK\n"
                    ) as read,
                    mock.patch.object(
                        watch.os, "write", return_value=3
                    ) as write,
                ):
                    with self.assertRaises(TimeoutError):
                        if operation == "read":
                            watch._frame(17, 1.0)
                        else:
                            watch._send(17, b"OK", 1.0)
                    read.assert_not_called()
                    write.assert_not_called()

    def test_read_finishing_late_cannot_supply_an_accepted_frame(self) -> None:
        clock = [0.0]
        selector = mock.MagicMock()
        selector.__enter__.return_value = selector
        selector.select.return_value = [(1, 1)]

        def late_read(descriptor: int, limit: int) -> bytes:
            clock[0] = 2.0
            return b"OK\n"

        with (
            mock.patch.object(watch.time, "monotonic", lambda: clock[0]),
            mock.patch.object(
                watch.selectors, "DefaultSelector", return_value=selector
            ),
            mock.patch.object(watch.os, "read", side_effect=late_read),
            self.assertRaises(TimeoutError),
        ):
            watch._frame(17, 1.0)

    def test_late_event_retains_observed_fact_without_qualification(
        self,
    ) -> None:
        clock = [0.0]
        child = mock.Mock(pid=22, returncode=None)
        child.wait.return_value = 0
        queue = mock.Mock()
        event = SimpleNamespace(
            ident=22,
            filter=watch._KQ.KQ_FILTER_PROC,
            flags=0,
            fflags=watch._KQ.KQ_NOTE_EXIT,
            data=0,
            udata=7,
        )

        def control(
            changes: object, count: int, timeout: float
        ) -> list[SimpleNamespace]:
            if changes is not None:
                return []
            clock[0] = 4.0
            return [event]

        queue.control.side_effect = control
        result: dict[str, object] = {"exit_event_observed": False}
        with (
            mock.patch.object(watch.time, "monotonic", lambda: clock[0]),
            mock.patch.object(watch, "_send"),
            mock.patch.object(watch, "_frame", return_value=b"LIVE nonce"),
            mock.patch.object(watch.select, "kevent"),
            self.assertRaises(TimeoutError),
        ):
            watch._watch(
                child,
                queue,
                (17, 18, b"nonce"),
                7,
                (12.0, 1.0),
                result,
                io.StringIO(),
            )
        self.assertTrue(result["exit_event_observed"])
        self.assertNotIn("qualification", result)
        child.wait.assert_not_called()

    def test_late_actual_wait_retains_reap_and_prevents_fallback_signal(
        self,
    ) -> None:
        clock = [0.0]
        child = mock.Mock(pid=22, returncode=None)
        queue = mock.Mock()
        event = SimpleNamespace(
            ident=22,
            filter=watch._KQ.KQ_FILTER_PROC,
            flags=0,
            fflags=watch._KQ.KQ_NOTE_EXIT,
            data=0,
            udata=7,
        )
        queue.control.side_effect = [[], [event]]

        def late_wait(timeout: float) -> int:
            clock[0] = 2.0
            child.returncode = 0
            return 0

        child.wait.side_effect = late_wait
        with (
            mock.patch.object(watch.time, "monotonic", lambda: clock[0]),
            mock.patch.object(
                watch, "_pipe", side_effect=[(17, 18), (19, 20)]
            ),
            mock.patch.object(watch, "_ready"),
            mock.patch.object(watch, "_send"),
            mock.patch.object(watch, "_frame", return_value=b"LIVE nonce"),
            mock.patch.object(watch.os, "close"),
            mock.patch.object(watch.os, "killpg", create=True) as signal_group,
            mock.patch.object(watch.subprocess, "Popen", return_value=child),
            mock.patch.object(watch.select, "kqueue", return_value=queue),
            mock.patch.object(watch.select, "kevent"),
            mock.patch.object(
                watch.secrets, "token_hex", return_value="nonce"
            ),
            mock.patch.object(watch.secrets, "randbelow", return_value=6),
            mock.patch.object(watch, "_fallback") as fallback,
            mock.patch.object(watch, "_RETAINED_CHILDREN", []),
        ):
            result = watch._experiment(io.StringIO())
        self.assertTrue(result["exit_event_observed"])
        self.assertTrue(result["direct_child_reaped"])
        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(result["normal_exit_status"], 0)
        self.assertEqual(result["qualification"], "inconclusive")
        fallback.assert_not_called()
        signal_group.assert_not_called()


if __name__ == "__main__":
    unittest.main()
