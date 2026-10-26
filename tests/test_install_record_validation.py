"""Malformed activation records fail before installer side effects."""

from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from install import (
    legacy_state,
    policy_records,
    record,
    review_policy,
    review_policy_default,
)
from install.context import Ctx, StepFailedError
from tests.test_review_shared_models import policy_data


class PolicyRecordValidationTests(unittest.TestCase):
    """Exercise saved JSON and recovery consumers with private fixtures."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name).resolve()
        self.ctx = Ctx(
            home=home,
            fresh=True,
            platform="darwin",
            ts="record-test",
            run=mock.Mock(side_effect=AssertionError("external effect")),
        )
        self.sha = "a" * 40
        self.ctx.rdir.mkdir(parents=True, mode=0o700)
        self.path = self.ctx.rdir / "rollback-record.json"
        self.ctx.review_policy = self.ctx.home / "policy.json"
        data = policy_data()
        data["projection_version"] = "permitted-fields-v2"
        self.ctx.review_policy.write_text(json.dumps(data))
        self.snapshot = review_policy.capture(self.ctx, self.sha)
        self.rec: record.Record = {
            "sha": self.sha,
            "policy_input": self.snapshot,
        }

    def write(self, value: object) -> None:
        """Write private malformed JSON without production save checks."""
        self.path.write_text(json.dumps(value))
        self.path.chmod(0o600)

    def test_wrong_snapshot_container_refuses_with_stable_error(self) -> None:
        for value in (None, [], "policy", 7, False):
            with self.subTest(value=value):
                self.rec["policy_input"] = value
                with self.assertRaisesRegex(
                    StepFailedError, "review_policy_snapshot_changed"
                ):
                    review_policy.frozen(self.ctx, self.rec)

    def test_snapshot_strict_identity_and_unknown_fields_refuse(self) -> None:
        for field, value in (
            ("home_identity", [True, 2, 448]),
            ("home_identity", ["1", 2, 448]),
            ("home_identity", [1, 2]),
            ("status", "complete"),
            ("raw", "\ud800"),
            ("unknown_authority", "discarding this is unsafe"),
        ):
            with self.subTest(field=field, value=value):
                self.rec["policy_input"] = self.snapshot | {field: value}
                with self.assertRaisesRegex(
                    StepFailedError, "review_policy_snapshot_changed"
                ):
                    review_policy.frozen(self.ctx, self.rec)

    def test_invalid_activation_load_and_save_do_not_replace_record(
        self,
    ) -> None:
        record.save(self.ctx, self.rec)
        before = self.path.read_bytes()
        for value in (
            [],
            {"phase": "intent"},
            {"phase": "complete", "intent": {"control": {}}},
            {"phase": "unknown", "intent": {}},
        ):
            with self.subTest(value=value):
                malformed = self.rec | {"policy_activation": value}
                with self.assertRaisesRegex(
                    StepFailedError, "review_activation_record_changed"
                ):
                    record.save(self.ctx, malformed)
                self.assertEqual(self.path.read_bytes(), before)
                self.write(malformed)
                for load in (record.load_record, legacy_state.read):
                    with self.assertRaisesRegex(
                        StepFailedError, "review_activation_record_changed"
                    ):
                        load(self.path)
                self.path.write_bytes(before)

    def test_malformed_json_and_non_object_have_safe_error(self) -> None:
        for raw in (
            "{",
            "[]",
            "null",
            '"text"',
            "\ufffd",
            '{"sha":"old","sha":"new"}',
        ):
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                self.path.chmod(0o600)
                for load in (record.load_record, legacy_state.read):
                    with self.assertRaisesRegex(
                        StepFailedError, "legacy_record_invalid"
                    ):
                        load(self.path)

    def test_parser_recursion_refuses_with_safe_error(self) -> None:
        self.write(self.rec)
        with mock.patch(
            "install.policy_records.json.loads", side_effect=RecursionError
        ):
            for load in (record.load_record, legacy_state.read):
                with self.assertRaisesRegex(
                    StepFailedError, "legacy_record_invalid"
                ):
                    load(self.path)

    def test_historical_and_no_policy_records_round_trip(self) -> None:
        self.ctx.review_policy = None
        values: list[record.Record] = [
            {"historical": {"arbitrary_provider_value": [1, None]}},
            {
                "sha": self.sha,
                "policy_input": review_policy.capture(self.ctx, self.sha),
            },
            self.rec,
        ]
        for value in values:
            with self.subTest(value=value):
                record.save(self.ctx, value)
                self.assertEqual(record.load_record(self.path), value)
                self.assertEqual(legacy_state.read(self.path), value)
        self.assertIsNone(review_policy.frozen(self.ctx, values[1]))

    def test_historical_nonfinite_provider_preimages_remain_readable(
        self,
    ) -> None:
        value = {"provider_before": [float("nan"), float("inf")]}
        record.save(self.ctx, value)
        for load in (record.load_record, legacy_state.read):
            loaded = load(self.path)["provider_before"]
            self.assertTrue(math.isnan(loaded[0]))
            self.assertEqual(loaded[1], float("inf"))

    def test_earlier_default_snapshots_remain_readable_without_new_bindings(
        self,
    ) -> None:
        original: record.Record = dict(self.snapshot) | {
            "policy_origin": "repository_default",
            "default_executable": "/original/codex",
            "default_executable_digest": "b" * 64,
            "default_auth_home": str(self.ctx.home),
            "default_auth_identity": [1, 2, 448],
            "default_profile_digest": "c" * 64,
        }
        for additions in (
            {},
            {"default_profile": "{}"},
            {"default_executable_target": "/original/target"},
        ):
            with self.subTest(additions=additions):
                rec = self.rec | {"policy_input": original | additions}
                record.save(self.ctx, rec)
                self.assertEqual(legacy_state.read(self.path), rec)
                self.assertIsNotNone(review_policy.frozen(self.ctx, rec))
                with self.assertRaisesRegex(
                    StepFailedError, "review_policy_default_changed"
                ):
                    policy_records.default_source(original | additions)

    def test_missing_ready_bindings_refuse_before_file_observation(
        self,
    ) -> None:
        for value in (
            [],
            {"runtime": {}, "files": {}, "selection": {}},
        ):
            with (
                self.subTest(value=value),
                mock.patch(
                    "install.review_policy.baseline.tree_state"
                ) as observe,
                self.assertRaisesRegex(
                    StepFailedError, "review_activation_runtime_changed"
                ),
            ):
                review_policy.verify_ready(
                    self.ctx, self.rec | {"policy_ready": value}
                )
            observe.assert_not_called()

    def test_missing_default_path_refuses_before_materialization(self) -> None:
        selected = review_policy.frozen(self.ctx, self.rec)
        assert selected is not None
        source: record.Record = copy.deepcopy(dict(self.snapshot))
        source["policy_origin"] = "repository_default"
        source["default_profile"] = "{}"
        with (
            mock.patch.object(
                review_policy_default.prepare_codex_policy, "prepare"
            ) as prepare,
            self.assertRaisesRegex(
                StepFailedError, "review_policy_default_changed"
            ),
        ):
            review_policy_default.prepare(
                self.ctx, self.rec | {"policy_input": source}, selected
            )
        prepare.assert_not_called()

    def test_private_reader_still_refuses_linked_record(self) -> None:
        target = self.ctx.home / "target.json"
        target.write_text(json.dumps(self.rec))
        target.chmod(0o600)
        self.path.symlink_to(target)
        with self.assertRaises(StepFailedError):
            legacy_state.read(self.path)

    def activation(self) -> record.Record:
        """Build the original JSON shape of a saved post-bootstrap intent."""
        return {
            "phase": "intent",
            "intent": {
                "store": [1, 2],
                "boundaries": "a" * 64,
                "control": {
                    "singleton": 1,
                    "generation": 0,
                    "admission": 1,
                    "policy_json": self.snapshot.get("raw"),
                    "policy_digest": "a" * 64,
                    "activated_at": 1.0,
                },
            },
        }

    def test_nested_control_fields_are_strict_and_finite(self) -> None:
        value = self.activation()
        for field, wrong in (
            ("singleton", True),
            ("generation", "1"),
            ("generation", -1),
            ("admission", True),
            ("admission", 2),
            ("policy_digest", "bad"),
            ("activated_at", 1),
            ("activated_at", float("nan")),
            ("activated_at", float("inf")),
            ("activated_at", -1.0),
        ):
            with self.subTest(field=field, value=wrong):
                bad = copy.deepcopy(value)
                bad["intent"]["control"][field] = wrong
                with self.assertRaisesRegex(
                    StepFailedError, "review_activation_record_changed"
                ):
                    record.save(
                        self.ctx, self.rec | {"policy_activation": bad}
                    )
                self.assertFalse(self.path.exists())

    def test_valid_activation_json_retains_arrays_and_integer_flags(
        self,
    ) -> None:
        for admission in (0, 1):
            for phase in ("intent", "complete"):
                value = self.activation()
                value["phase"] = phase
                value["intent"]["control"]["admission"] = admission
                original = self.rec | {"policy_activation": value}
                record.save(self.ctx, original)
                loaded = legacy_state.read(self.path)
                self.assertEqual(loaded, original)
                self.assertIsInstance(
                    loaded["policy_activation"]["intent"]["store"], list
                )

    def test_pending_envelope_refuses_before_context_derivation(self) -> None:
        base: record.Record = {
            "ts": self.ctx.ts,
            "fresh": True,
            "upgrade": False,
            "sha": self.sha,
            "home": str(self.ctx.home),
            "rdir": str(self.ctx.rdir),
            "repo": str(self.ctx.home),
            "policy_activation": self.activation(),
        }
        for field, wrong in (
            ("ts", "../escape"),
            ("ts", []),
            ("fresh", "true"),
            ("upgrade", True),
            ("install_complete", "yes"),
            ("superseded_by", True),
        ):
            with self.subTest(field=field, value=wrong):
                self.write(base | {field: wrong})
                with self.assertRaisesRegex(
                    StepFailedError, "review_activation_record_changed"
                ):
                    review_policy.pending(self.ctx)

    def test_ready_cannot_omit_managed_configuration_or_selection(
        self,
    ) -> None:
        runtime = {"directories": {".": [1, 2, 448]}, "files": {}}
        value = {
            "runtime": runtime,
            "files": {
                str(p): None
                for p in (
                    self.ctx.settings,
                    self.ctx.config,
                    self.ctx.cursor_settings,
                )
            },
            "selection": {"links": {}, "cache": None},
        }
        with self.assertRaisesRegex(
            StepFailedError, "review_activation_runtime_changed"
        ):
            policy_records.ready(value, self.ctx)
        value["files"].pop(str(self.ctx.config))
        with self.assertRaisesRegex(
            StepFailedError, "review_activation_configuration_changed"
        ):
            policy_records.ready(value, self.ctx)
