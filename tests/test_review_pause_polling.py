"""Exercise strict witness reads across concurrent owner publication."""

from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO
from unittest import mock

from muninn import platform_io, review_pause_owner
from muninn import review_pause as pause
from muninn import review_pause_state as state
from tests.review_pause_support import initialize


class WitnessPollingTests(unittest.TestCase):
    """Retain deadline and owner continuity through strict fresh reads."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        initialize(self.home)
        self.fd, self.token = state.begin_owner(self.home)
        self.addCleanup(self.close_owner)

    def close_owner(self) -> None:
        """Close a fixture descriptor once without touching another owner."""
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def complete(self, replacement: bool = False) -> None:
        """Publish actual completion while retaining the lifetime lock."""
        state.complete_owner(self.home, self.fd, self.token)
        self.close_owner()
        if replacement:
            fd, token = state.begin_owner(self.home)
            try:
                state.complete_owner(self.home, fd, token)
            finally:
                os.close(fd)

    def interleave(
        self, ready: Callable[[], bool], *, replacement: bool = False
    ) -> Callable[..., BinaryIO]:
        """Complete the real owner between one strict stat and open."""
        original = platform_io.open_regular
        fired = False

        def opened(
            path: Path,
            *,
            root: Path | None = None,
            expected: os.stat_result | None = None,
        ) -> BinaryIO:
            nonlocal fired
            if path == self.home / state.WITNESS and ready() and not fired:
                fired = True
                self.complete(replacement)
                assert expected is not None
                self.assertNotEqual(expected.st_ino, path.stat().st_ino)
            return original(path, root=root, expected=expected)

        return opened

    def test_completion_before_selection_restarts_with_a_fresh_read(
        self,
    ) -> None:
        with (
            mock.patch.object(
                platform_io,
                "open_regular",
                side_effect=self.interleave(lambda: True),
            ),
            mock.patch.object(pause.poller_stop, "request") as request,
            pause.maintenance_owner(
                self.home, wait_s=1, status=pause.EraseStatus()
            ) as owner,
        ):
            self.assertIsNone(owner.selected)
            request.assert_not_called()
        witness = state.parse(
            state.inspect(self.home, state.WITNESS)[0], state.Witness
        )
        assert witness is not None
        self.assertEqual(witness.state, "complete")

    def test_selected_owner_completion_is_rechecked_after_publication(
        self,
    ) -> None:
        selected = False

        def stop(
            home: Path, pid: int, generation: str, *, deadline: float
        ) -> None:
            nonlocal selected
            selected = True
            self.assertEqual(
                (home, pid, generation), (self.home, os.getpid(), self.token)
            )

        with (
            mock.patch.object(
                pause.poller_stop, "request", side_effect=stop
            ) as request,
            mock.patch.object(
                platform_io,
                "open_regular",
                side_effect=self.interleave(lambda: selected),
            ),
            pause.maintenance_owner(
                self.home, wait_s=1, status=pause.EraseStatus()
            ) as owner,
        ):
            assert owner.selected is not None
            self.assertEqual(
                owner.selected.owner,
                state.Owner(pid=os.getpid(), generation=self.token),
            )
            request.assert_called_once()

    def test_changed_owner_after_transient_read_is_still_refused(self) -> None:
        path = self.home / state.WITNESS
        retained = self.home / "retained-witness"
        # Keep the discovered inode allocated across all owner publications.
        os.link(path, retained)
        self.addCleanup(retained.unlink, missing_ok=True)
        before, pinned = path.stat(), retained.stat()
        self.assertEqual(
            (before.st_dev, before.st_ino), (pinned.st_dev, pinned.st_ino)
        )
        selected = False

        def stop(
            home: Path, pid: int, generation: str, *, deadline: float
        ) -> None:
            nonlocal selected
            selected = True

        with (
            mock.patch.object(
                pause.poller_stop, "request", side_effect=stop
            ) as request,
            mock.patch.object(
                platform_io,
                "open_regular",
                side_effect=self.interleave(
                    lambda: selected, replacement=True
                ),
            ),
            self.assertRaisesRegex(ValueError, "owner_changed"),
            pause.maintenance_owner(
                self.home, wait_s=1, status=pause.EraseStatus()
            ),
        ):
            self.fail("replacement owner admitted")
        request.assert_called_once()
        after = path.stat()
        self.assertNotEqual(
            (before.st_dev, before.st_ino), (after.st_dev, after.st_ino)
        )

    def test_repeated_identity_changes_exhaust_the_original_deadline(
        self,
    ) -> None:
        elapsed = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            elapsed[0] += seconds

        clock = SimpleNamespace(monotonic=lambda: elapsed[0], sleep=sleep)
        with (
            mock.patch.object(pause, "time", clock),
            mock.patch.object(review_pause_owner, "time", clock),
            mock.patch.object(
                platform_io,
                "open_regular",
                side_effect=OSError("source changed identity"),
            ) as opened,
            mock.patch.object(pause.poller_stop, "request") as request,
            self.assertRaisesRegex(ValueError, "coordination_timeout"),
            pause.maintenance_owner(
                self.home, wait_s=0.03, status=pause.EraseStatus()
            ),
        ):
            self.fail("unstable witness admitted")
        self.assertGreaterEqual(opened.call_count, 2)
        self.assertAlmostEqual(sum(sleeps), 0.03)
        self.assertAlmostEqual(elapsed[0], 0.03)
        request.assert_not_called()

    def test_unsafe_and_unexpected_errors_are_never_retried(self) -> None:
        for error in (
            PermissionError("source changed identity"),
            OSError("source is outside its root"),
            OSError("unrelated I/O failure"),
            state.PauseRefusedError("pause_unverified"),
        ):
            with (
                self.subTest(error=repr(error)),
                mock.patch.object(
                    platform_io, "open_regular", side_effect=error
                ) as opened,
                mock.patch.object(pause.poller_stop, "request") as request,
                self.assertRaises(type(error)) as caught,
                pause.maintenance_owner(
                    self.home, wait_s=1, status=pause.EraseStatus()
                ),
            ):
                self.fail("unsafe witness admitted")
            self.assertIs(caught.exception, error)
            opened.assert_called_once()
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
