"""Validate saved legacy operation selectors before recovery discovery."""

from __future__ import annotations

import dataclasses
import json
import math
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from install import legacy_baseline, legacy_state, record
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import platform_io
from tests.installer_support import Fake


class LegacyRecordContextTests(unittest.TestCase):
    """Exercise real private operation directories under a temporary home."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name).resolve() / "home"
        platform_io.ensure_private_dir(home)
        self.ctx = Ctx(
            home, Fake(home).run, "saved-operation", platform="darwin"
        )
        self.ctx.upgrade = True
        platform_io.ensure_private_dir(self.ctx.rdir)
        platform_io.ensure_private_dir(self.ctx.lib)
        self.rec: Record = {
            "ts": self.ctx.ts,
            "sha": "a" * 40,
            "home": str(self.ctx.home),
            "rdir": str(self.ctx.rdir),
            "fresh": False,
            "upgrade": True,
            "legacy": {
                "version": 1,
                "baseline": legacy_baseline.BASELINE,
                "platform": "darwin",
                "phase": "armed",
                "publication": "not_started",
                "record_directory": legacy_baseline.directory(
                    self.ctx.rdir, private=True
                ),
            },
        }

    def assert_refusal(self, expected: str = "legacy_record_invalid") -> None:
        """Require a stable code rather than coercion or an unchecked crash."""
        try:
            legacy_state.validate(self.ctx, self.rec)
        except Exception as error:
            self.assertIsInstance(error, StepFailedError)
            self.assertEqual(str(error), expected)
        else:
            self.fail("malformed recovery context was accepted")

    def test_boolean_version_cannot_select_a_version_one_operation(
        self,
    ) -> None:
        self.rec["legacy"]["version"] = True
        self.assert_refusal()

    def test_float_version_cannot_select_a_version_one_operation(self) -> None:
        self.rec["legacy"]["version"] = 1.0
        self.assert_refusal()

    def test_unhashable_phase_reports_a_stable_refusal(self) -> None:
        self.rec["legacy"]["phase"] = ["armed"]
        self.assert_refusal()

    def test_unhashable_publication_reports_a_stable_refusal(self) -> None:
        self.rec["legacy"]["publication"] = {"value": "complete"}
        self.assert_refusal()

    @unittest.skipIf(sys.platform == "win32", "POSIX selection link fixture")
    def test_valid_different_selected_release_is_not_a_context_mismatch(
        self,
    ) -> None:
        (self.ctx.lib / "current").symlink_to("f" * 40)
        legacy_state.validate(self.ctx, self.rec)
        self.assertEqual(self.rec["sha"], "a" * 40)
        self.assertEqual((self.ctx.lib / "current").readlink().name, "f" * 40)

    def test_native_directory_mismatch_keeps_its_existing_code(self) -> None:
        self.rec["legacy"]["record_directory"] = [0, 0, 0]
        self.assert_refusal("legacy_record_changed")

    def test_ignored_prepublication_binding_is_still_opaque(self) -> None:
        self.rec["legacy"]["publication_binding"] = ["retained", None]
        before = json.dumps(self.rec, sort_keys=True)
        legacy_state.validate(self.ctx, self.rec)
        self.assertEqual(json.dumps(self.rec, sort_keys=True), before)

    def test_unsupported_platform_precedes_malformed_record_validation(
        self,
    ) -> None:
        self.rec.clear()
        unsupported = dataclasses.replace(self.ctx, platform="win32")
        with self.assertRaisesRegex(
            StepFailedError, "legacy_boot_unsupported"
        ):
            legacy_state.validate(unsupported, self.rec)

    def test_historical_phases_and_publication_states_remain_readable(
        self,
    ) -> None:
        phases = (
            "intent",
            "prepared",
            "launcher_intent",
            "launcher",
            "runtime_intent",
            "runtime",
            "service_intent",
            "service",
            "armed",
            "verified",
            "recovery_required",
            "publication_possible",
            "published",
            "configuring",
            "configured",
            "launcher_possible",
            "activated",
            "starting",
            "complete",
        )
        for phase in phases:
            for publication in ("not_started", "possible", "complete"):
                with self.subTest(phase=phase, publication=publication):
                    self.rec["legacy"].update(
                        phase=phase,
                        publication=publication,
                        publication_binding={"future_binding": [1, 2]},
                    )
                    before = json.dumps(self.rec, sort_keys=True)
                    legacy_state.validate(self.ctx, self.rec)
                    self.assertEqual(
                        json.dumps(self.rec, sort_keys=True), before
                    )

    def test_wrong_operation_selectors_are_refused_without_coercion(
        self,
    ) -> None:
        original = deepcopy(self.rec)
        for field, value in (
            ("ts", None),
            ("ts", "../other"),
            ("ts", "x" * 81),
            ("sha", True),
            ("sha", "a" * 39),
            ("sha", "A" * 40),
            ("home", None),
            ("rdir", 12),
            ("fresh", 0),
            ("fresh", "false"),
            ("upgrade", 1),
            ("upgrade", "true"),
        ):
            with self.subTest(field=field, value=value):
                self.rec = deepcopy(original)
                self.rec[field] = value
                self.assert_refusal()

    def test_missing_required_context_fields_cannot_select_an_operation(
        self,
    ) -> None:
        original = deepcopy(self.rec)
        for field in (
            "ts",
            "sha",
            "home",
            "rdir",
            "fresh",
            "upgrade",
            "legacy",
        ):
            with self.subTest(field=field):
                self.rec = deepcopy(original)
                del self.rec[field]
                self.assert_refusal()

    def test_wrong_legacy_header_cannot_select_an_operation(self) -> None:
        original = deepcopy(self.rec)
        for field, value in (
            ("version", 2),
            ("baseline", "f" * 40),
            ("platform", "linux"),
            ("phase", "future_phase"),
            ("publication", "future_publication"),
        ):
            with self.subTest(field=field, value=value):
                self.rec = deepcopy(original)
                self.rec["legacy"][field] = value
                self.assert_refusal()

    def test_begun_publication_requires_a_dictionary_binding(self) -> None:
        for publication in ("possible", "complete"):
            for binding in (None, "unknown", ["binding"]):
                with self.subTest(publication=publication, binding=binding):
                    self.rec["legacy"].update(
                        publication=publication, publication_binding=binding
                    )
                    self.assert_refusal()

    def test_missing_native_directory_binding_is_still_changed(self) -> None:
        del self.rec["legacy"]["record_directory"]
        self.assert_refusal("legacy_record_changed")

    @unittest.skipIf(sys.platform == "win32", "POSIX owner-only mode fixture")
    def test_native_directory_safety_precedes_missing_binding(self) -> None:
        del self.rec["legacy"]["record_directory"]
        self.ctx.rdir.chmod(0o777)
        self.addCleanup(self.ctx.rdir.chmod, 0o700)
        self.assert_refusal("legacy_directory_unsafe")

    def test_context_location_mismatch_cannot_select_an_operation(
        self,
    ) -> None:
        original = deepcopy(self.rec)
        for field, value in (
            ("ts", "other-operation"),
            ("home", str(self.ctx.home / "other")),
            ("rdir", str(self.ctx.rdir / "other")),
        ):
            with self.subTest(field=field):
                self.rec = deepcopy(original)
                self.rec[field] = value
                self.assert_refusal()

    def test_pending_preserves_current_and_historical_opaque_extras(
        self,
    ) -> None:
        observed: list[Record] = []
        original_read = legacy_state.read

        def observe(source: Path) -> Record:
            loaded = original_read(source)
            observed.append(loaded)
            return loaded

        for current in (False, True):
            with self.subTest(current=current):
                if current:
                    self.rec["learning_scope"] = {
                        "mode": "all",
                        "enqueued": 0,
                        "project_id": None,
                        "excluded_spans": 0,
                    }
                    self.rec["policy_input"] = {
                        "status": "activation_pending",
                        "operation": self.ctx.ts,
                        "home": str(self.ctx.home),
                        "sha": self.rec["sha"],
                        "home_identity": legacy_baseline.directory(
                            self.ctx.home
                        ),
                        "enable_learning": False,
                    }
                self.rec["provider_before"] = {
                    "untouched": [None, {"value": float("nan")}],
                    "limit": float("inf"),
                }
                self.rec["legacy"]["future_metadata"] = {"opaque": [1, None]}
                record.save(self.ctx, self.rec)
                path = self.ctx.rdir / "rollback-record.json"
                before = path.read_bytes()
                observed.clear()
                later = dataclasses.replace(self.ctx, ts="new-invocation")
                with mock.patch.object(
                    legacy_state, "read", side_effect=observe
                ):
                    pending = legacy_state.pending(later)
                self.assertIsNotNone(pending)
                assert pending is not None
                active, retained = pending
                self.assertEqual(active.ts, "saved-operation")
                self.assertIs(retained, observed[0])
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(
                    retained["legacy"]["future_metadata"],
                    {"opaque": [1, None]},
                )
                self.assertTrue(
                    math.isnan(
                        retained["provider_before"]["untouched"][1]["value"]
                    )
                )
                self.assertEqual(
                    retained["provider_before"]["limit"], float("inf")
                )
                if current:
                    self.assertEqual(
                        retained["learning_scope"], self.rec["learning_scope"]
                    )

    def test_complete_and_nonlegacy_records_are_not_pending(self) -> None:
        self.rec["legacy"]["phase"] = "complete"
        record.save(self.ctx, self.rec)
        self.assertIsNone(legacy_state.pending(self.ctx))
        del self.rec["legacy"]
        record.save(self.ctx, self.rec)
        self.assertIsNone(legacy_state.pending(self.ctx))

    def test_pending_bad_selector_keeps_the_saved_record_unchanged(
        self,
    ) -> None:
        self.rec["legacy"]["phase"] = ["armed"]
        record.save(self.ctx, self.rec)
        path = self.ctx.rdir / "rollback-record.json"
        before = path.read_bytes()
        try:
            legacy_state.pending(self.ctx)
        except Exception as error:
            self.assertIsInstance(error, StepFailedError)
            self.assertEqual(str(error), "legacy_record_invalid")
        else:
            self.fail("malformed pending selector was accepted")
        self.assertEqual(path.read_bytes(), before)

    def test_windows_pending_discovery_does_not_read_posix_records(
        self,
    ) -> None:
        self.rec.clear()
        unsupported = dataclasses.replace(self.ctx, platform="win32")
        self.assertIsNone(legacy_state.pending(unsupported))
