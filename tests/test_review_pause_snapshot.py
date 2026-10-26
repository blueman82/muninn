"""Refuse changed snapshots while permitting fresh bounded owner polling."""

from __future__ import annotations

import contextlib
import os
import unittest
from collections.abc import Callable, Generator
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, cast
from unittest import mock

from muninn import platform_io, review_pause_owner
from muninn import review_pause as pause
from muninn import review_pause_state as state
from tests import test_review_pause_polling as fixtures
from tests.store_support import public_read


class SnapshotTests(unittest.TestCase):
    """Keep discarded evidence separate from freshly validated authority."""

    def fixture(self) -> fixtures.WitnessPollingTests:
        """Create one disposable real owner without importing its tests."""
        case = fixtures.WitnessPollingTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def publication_phases(self) -> tuple[str, ...]:
        """Use publication windows supported by the native rename operation."""
        # MoveFileEx cannot replace the target while its old handle is open.
        return ("stat",) if os.name == "nt" else ("open", "read", "stat")

    @contextlib.contextmanager
    def changed(
        self,
        case: fixtures.WitnessPollingTests,
        phase: str,
        action: Callable[[], None],
        ready: Callable[[], bool] = lambda: True,
    ) -> Generator[None]:
        """Interleave one action with an actual strict witness inspection."""
        original_open, original_private = (
            platform_io.open_regular,
            state._private,
        )
        fired = False
        opened_once = False

        def act() -> None:
            nonlocal fired
            if ready() and not fired:
                fired = True
                action()

        def opened(
            path: Path,
            *,
            root: Path | None = None,
            expected: os.stat_result | None = None,
        ) -> BinaryIO:
            nonlocal opened_once
            handle = original_open(path, root=root, expected=expected)
            if path != case.home / state.WITNESS or not ready() or fired:
                return handle
            opened_once = True
            if phase == "open":
                with contextlib.ExitStack() as cleanup:
                    cleanup.callback(handle.close)
                    act()
                    cleanup.pop_all()
            if phase != "read":
                return handle
            proxy = mock.MagicMock(wraps=handle)
            proxy.__enter__.return_value = proxy
            proxy.__exit__.side_effect = handle.__exit__

            def read(size: int = -1) -> bytes:
                data = handle.read(size)
                act()
                return data

            proxy.read.side_effect = read
            return cast(BinaryIO, proxy)

        def private(path: Path, *, directory: bool = False) -> os.stat_result:
            if (
                path == case.home / state.WITNESS
                and opened_once
                and phase == "stat"
            ):
                act()
            return original_private(path, directory=directory)

        with (
            mock.patch.object(platform_io, "open_regular", side_effect=opened),
            mock.patch.object(state, "_private", side_effect=private),
        ):
            yield

    def test_actual_late_completion_is_discarded_without_returning_evidence(
        self,
    ) -> None:
        for phase in self.publication_phases():
            case = self.fixture()
            with (
                self.subTest(phase=phase),
                self.changed(case, phase, case.complete),
                self.assertRaises(state.PauseRefusedError) as caught,
            ):
                state.inspect(case.home, state.WITNESS)
            self.assertEqual(
                type(caught.exception).__name__, "SnapshotChangedError"
            )
            self.assertEqual(caught.exception.code, "pause_unverified")
            raw, fingerprint = state.inspect(case.home, state.WITNESS)
            self.assertIsNotNone(fingerprint)
            self.assertIn(b'"complete"', raw)

    def test_fresh_polling_preserves_selection_across_all_late_windows(
        self,
    ) -> None:
        for phase in self.publication_phases():
            for select_first in (False, True):
                self.check_polling(phase, select_first)

    def check_polling(self, phase: str, select_first: bool) -> None:
        """Check one publication interleave with a fresh disposable owner."""
        case = self.fixture()
        selected = [False]

        def stop(
            home: Path, pid: int, generation: str, *, deadline: float
        ) -> None:
            selected[0] = True
            self.assertEqual(generation, case.token)

        with (
            self.subTest(phase=phase, selected=select_first),
            self.changed(
                case,
                phase,
                case.complete,
                lambda: selected[0] or not select_first,
            ),
            mock.patch.object(
                pause.poller_stop, "request", side_effect=stop
            ) as request,
            pause.maintenance_owner(
                case.home, wait_s=1, status=pause.EraseStatus()
            ) as owner,
        ):
            self.assertEqual(owner.selected is not None, select_first)
            self.assertEqual(request.call_count, int(select_first))

    def test_late_replacement_cannot_change_selected_generation(self) -> None:
        case = self.fixture()
        path = case.home / state.WITNESS
        retained = case.home / "retained-witness"
        # Keep the inspected inode allocated across all owner publications.
        os.link(path, retained)
        self.addCleanup(retained.unlink, missing_ok=True)
        before, pinned = path.stat(), retained.stat()
        self.assertEqual(
            (before.st_dev, before.st_ino), (pinned.st_dev, pinned.st_ino)
        )
        selected = [False]

        def stop(
            home: Path, pid: int, generation: str, *, deadline: float
        ) -> None:
            selected[0] = True

        with (
            self.changed(
                case, "stat", lambda: case.complete(True), lambda: selected[0]
            ),
            mock.patch.object(
                pause.poller_stop, "request", side_effect=stop
            ) as request,
            self.assertRaisesRegex(ValueError, "owner_changed"),
            pause.maintenance_owner(
                case.home, wait_s=1, status=pause.EraseStatus()
            ),
        ):
            self.fail("replacement admitted")
        request.assert_called_once()
        after = path.stat()
        self.assertNotEqual(
            (before.st_dev, before.st_ino), (after.st_dev, after.st_ino)
        )

    def test_same_inode_and_replaced_old_descriptor_mutations_stay_generic(
        self,
    ) -> None:
        for replace in ((False,) if os.name == "nt" else (False, True)):
            self.check_mutation(replace)

    def check_mutation(self, replace: bool) -> None:
        """Mutate one opened inode with or without replacing its path."""
        case = self.fixture()
        fd = os.open(case.home / state.WITNESS, os.O_WRONLY | os.O_APPEND)
        self.addCleanup(os.close, fd)

        def mutate() -> None:
            if replace:
                case.complete()
            os.write(fd, b"changed")

        with (
            self.subTest(replace=replace),
            self.changed(case, "read", mutate),
            self.assertRaises(state.PauseRefusedError) as caught,
        ):
            state.inspect(case.home, state.WITNESS)
        self.assertIs(type(caught.exception), state.PauseRefusedError)

    def test_unsafe_replacement_is_not_a_retryable_snapshot(self) -> None:
        case = self.fixture()

        def unsafe() -> None:
            case.complete()
            self.enterContext(public_read(self, case.home / state.WITNESS))

        with (
            self.changed(case, "stat", unsafe),
            self.assertRaises(state.PauseRefusedError) as caught,
        ):
            state.inspect(case.home, state.WITNESS)
        self.assertIs(type(caught.exception), state.PauseRefusedError)

    def test_post_read_descriptor_privacy_failure_is_not_reclassified(
        self,
    ) -> None:
        case = self.fixture()
        checks = [0]
        error = PermissionError("descriptor privacy changed")
        original = platform_io.assert_private_fd

        def private(fd: int) -> None:
            checks[0] += 1
            if checks[0] == 2:
                raise error
            original(fd)

        with (
            mock.patch.object(
                platform_io, "assert_private_fd", side_effect=private
            ),
            self.assertRaises(PermissionError) as caught,
        ):
            state.inspect(case.home, state.WITNESS)
        self.assertIs(caught.exception, error)

    @unittest.skipIf(os.name == "nt", "POSIX mode; Windows uses ACL checks")
    def test_changed_old_descriptor_mode_is_not_reclassified(self) -> None:
        case = self.fixture()
        fd = os.open(case.home / state.WITNESS, os.O_RDONLY)
        self.addCleanup(os.close, fd)

        def change_mode() -> None:
            case.complete()
            os.fchmod(fd, 0o644)

        with (
            self.changed(case, "read", change_mode),
            self.assertRaises(state.PauseRefusedError) as caught,
        ):
            state.inspect(case.home, state.WITNESS)
        self.assertIs(type(caught.exception), state.PauseRefusedError)

    def test_typed_refusal_after_lock_acquisition_is_never_retried(
        self,
    ) -> None:
        case = self.fixture()
        selected = state.parse(
            state.inspect(case.home, state.WITNESS)[0], state.Witness
        )
        assert selected is not None
        case.complete()
        error = state.SnapshotChangedError()
        with (
            mock.patch.object(pause, "_latest", side_effect=error) as latest,
            mock.patch.object(
                pause,
                "_owner",
                return_value=(state.acquire(case.home), selected),
            ),
            self.assertRaises(state.PauseRefusedError) as caught,
            pause.maintenance_owner(
                case.home, wait_s=1, status=pause.EraseStatus()
            ),
        ):
            self.fail("acquired-lock uncertainty admitted")
        self.assertIs(caught.exception, error)
        latest.assert_called_once()

    def test_typed_polling_refusals_share_the_original_deadline(self) -> None:
        case = self.fixture()
        elapsed = [0.0]

        def sleep(seconds: float) -> None:
            elapsed[0] += seconds

        clock = SimpleNamespace(monotonic=lambda: elapsed[0], sleep=sleep)
        with (
            mock.patch.object(pause, "time", clock),
            mock.patch.object(review_pause_owner, "time", clock),
            mock.patch.object(
                review_pause_owner,
                "latest",
                side_effect=state.SnapshotChangedError(),
            ) as latest,
            mock.patch.object(pause.poller_stop, "request") as request,
            self.assertRaisesRegex(ValueError, "coordination_timeout"),
            pause.maintenance_owner(
                case.home, wait_s=0.03, status=pause.EraseStatus()
            ),
        ):
            self.fail("unstable snapshot admitted")
        self.assertEqual(latest.call_count, 2)
        self.assertAlmostEqual(elapsed[0], 0.03)
        request.assert_not_called()

    def test_symlink_replacement_remains_an_immediate_refusal(self) -> None:
        case = self.fixture()

        def link() -> None:
            case.complete()
            path = case.home / state.WITNESS
            target = case.home / "target"
            path.rename(target)
            path.symlink_to(target)

        with (
            self.changed(case, "stat", link),
            self.assertRaises(state.PauseRefusedError) as caught,
        ):
            state.inspect(case.home, state.WITNESS)
        self.assertIs(type(caught.exception), state.PauseRefusedError)

    def test_lock_change_after_retry_refuses_before_material_is_yielded(
        self,
    ) -> None:
        case = self.fixture()
        selected = [False]

        def stop(
            home: Path, pid: int, generation: str, *, deadline: float
        ) -> None:
            selected[0] = True

        def replace_lock() -> None:
            case.complete()
            (case.home / state.LOCK).rename(case.home / "old-lock")
            (case.home / state.LOCK).touch(mode=0o600)

        with (
            self.changed(case, "stat", replace_lock, lambda: selected[0]),
            mock.patch.object(
                pause.poller_stop, "request", side_effect=stop
            ) as request,
            self.assertRaises(state.PauseRefusedError) as caught,
            pause.erase_connection(
                case.home, wait_s=1, status=pause.EraseStatus()
            ),
        ):
            self.fail("changed lock admitted")
        self.assertIs(type(caught.exception), state.PauseRefusedError)
        request.assert_called_once()

    @unittest.skipUnless(
        os.name == "nt", "requires native Windows publication"
    )
    def test_windows_open_target_publication_keeps_active_owner(self) -> None:
        for phase in ("open", "read"):
            for select_first in (False, True):
                self.check_blocked_publication(phase, select_first)

    def check_blocked_publication(
        self, phase: str, select_first: bool
    ) -> None:
        """Retain the active witness and lock after native sharing refusal."""
        case = self.fixture()
        before = (case.home / state.WITNESS).read_bytes()
        selected = [False]
        status = pause.EraseStatus()

        def stop(
            home: Path, pid: int, generation: str, *, deadline: float
        ) -> None:
            selected[0] = True

        with (
            self.subTest(phase=phase, selected=select_first),
            self.changed(
                case,
                phase,
                case.complete,
                lambda: selected[0] or not select_first,
            ),
            mock.patch.object(
                pause.poller_stop, "request", side_effect=stop
            ) as request,
            self.assertRaises(PermissionError) as caught,
            pause.maintenance_owner(case.home, wait_s=3, status=status),
        ):
            self.fail("failed publication granted ownership")
        self.assertIn(getattr(caught.exception, "winerror", None), (5, 32))
        self.assertEqual((case.home / state.WITNESS).read_bytes(), before)
        self.assertEqual(status.stop_may_have_been_published, select_first)
        self.assertEqual(request.call_count, int(select_first))
        with self.assertRaises(BlockingIOError):
            state.acquire(case.home)


if __name__ == "__main__":
    unittest.main()
