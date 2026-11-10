"""Keep predecessor runtimes usable until new witness support is selected."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Literal
from unittest import mock

from install import rollback
from install.context import Ctx
from install.record import Record
from install.recovery_guidance import recover_pause
from muninn import review_pause_state as state
from tests import test_review_completed_volume as fixtures
from tests.test_install_recovery import DoctorRunner


class LegacyWitness(state.StrictModel):
    """Preserve the predecessor's exact strict private-record contract."""

    version: state.SchemaVersion
    lock_identity: state.Identity
    owner: state.Owner | None
    state: Literal["active", "complete"]


@unittest.skipIf(os.name == "nt", "macOS migration uses POSIX locks")
class WitnessCompatibilityTests(unittest.TestCase):
    """Exercise recovery and rollback with the actual strict old format."""

    def fixture(self) -> fixtures.CompletedVolumeTests:
        """Create one disposable macOS recovery fixture without its tests."""
        case = fixtures.CompletedVolumeTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def context(self, home: Path) -> Ctx:
        """Select a predecessor runtime inside the temporary home."""
        ctx = Ctx(
            home,
            DoctorRunner(),
            "test",
            platform="darwin",
            upgrade=True,
            say=lambda _: None,
        )
        ctx.data = home
        ctx.lib.mkdir(parents=True)
        (ctx.lib / "previous").mkdir()
        (ctx.lib / "current").symlink_to("previous")
        return ctx

    def test_legacy_parser_rejects_uuid_field_without_silent_downgrade(
        self,
    ) -> None:
        case = self.fixture()
        raw = (case.home / state.WITNESS).read_bytes()
        self.assertIsNone(state.parse(raw, LegacyWitness))
        self.assertIsNotNone(state.parse(raw, state.Witness))

    def test_preselection_recovery_stays_readable_after_rollback(self) -> None:
        case = self.fixture()
        case.change_device(legacy=True)
        case.baseline()
        ctx = self.context(case.home)
        record = Record(upgrade=True, paused=True)
        with (
            mock.patch(
                "install.recovery_guidance.sys.stdin.isatty", return_value=True
            ),
            mock.patch("builtins.input", return_value="yes"),
        ):
            recover_pause(ctx, record)
        raw = (case.home / state.WITNESS).read_bytes()
        self.assertIsNotNone(state.parse(raw, LegacyWitness))
        self.assertFalse((case.home / state.BASELINE).exists())
        with (
            mock.patch("install.rollback.lifecycle.stop"),
            mock.patch("install.rollback.save"),
            mock.patch("install.rollback._undo_links") as undo,
        ):
            rollback.rollback_owned(ctx, record)
        undo.assert_not_called()
        self.assertEqual((ctx.lib / "current").readlink(), Path("previous"))
        self.assertEqual((case.home / state.WITNESS).read_bytes(), raw)

    def test_postselection_rollback_keeps_uuid_capable_runtime(self) -> None:
        case = self.fixture()
        ctx = self.context(case.home)
        (ctx.lib / "prepared").mkdir()
        current = ctx.lib / "current"
        current.unlink()
        current.symlink_to("prepared")
        fd, token = state.begin_owner(case.home)
        try:
            state.complete_owner(case.home, fd, token)
        finally:
            os.close(fd)
        raw = (case.home / state.WITNESS).read_bytes()
        self.assertIsNone(state.parse(raw, LegacyWitness))
        record = Record(upgrade=True, paused=True)
        with (
            mock.patch("install.rollback.lifecycle.stop") as stop,
            mock.patch("install.rollback.save"),
            mock.patch("install.rollback._undo_links") as undo,
            mock.patch("install.rollback.start_again") as start,
        ):
            rollback.rollback_owned(ctx, record)
        stop.assert_called_once()
        undo.assert_not_called()
        start.assert_not_called()
        self.assertEqual(current.readlink(), Path("prepared"))
        self.assertEqual((case.home / state.WITNESS).read_bytes(), raw)

    def test_initializer_can_defer_binding_until_runtime_selection(
        self,
    ) -> None:
        case = self.fixture()
        home = case.home / "fresh"
        home.mkdir(mode=0o700)
        fd = state.acquire(home)
        try:
            state.seed_new(home, fd, bind_volume=False)
        finally:
            os.close(fd)
        self.assertIsNotNone(
            state.parse((home / state.WITNESS).read_bytes(), LegacyWitness)
        )
