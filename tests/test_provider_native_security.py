"""Native security probe diagnostics are finite and contain no ACL bytes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from install.config_windows import security_parts
from tests.native_diagnostics import write
from tests.provider_native_security import probe, sample_fields
from tests.test_config_windows_metadata import descriptor


class ProbeFieldsTest(unittest.TestCase):
    """Only fixed variant IDs and numeric component results are exposed."""

    def test_samples_exact_control_and_component_equality(self) -> None:
        before = security_parts(descriptor(flags=0x8004))
        after = security_parts(descriptor())
        self.assertEqual(
            sample_fields(1, "initial", before, after),
            {
                "probe_v1_initial_control": 0x8404,
                "probe_v1_initial_owner_equal": 1,
                "probe_v1_initial_group_equal": 1,
                "probe_v1_initial_dacl_equal": 1,
            },
        )
        with self.assertRaises(ValueError):
            sample_fields(4, "initial", before, after)
        with self.assertRaises(ValueError):
            sample_fields(1, "raw_descriptor", before, after)

    def test_probe_fields_survive_later_provider_failure(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {"RUNNER_TEMP": tmp}),
        ):
            codes = {"probe_original_control": 0x8004}
            original = security_parts(descriptor(flags=0x8004))
            current = security_parts(descriptor())
            for variant in (1, 2, 3):
                for stage in ("initial", "final"):
                    codes.update(
                        sample_fields(variant, stage, original, current)
                    )
                codes[f"probe_v{variant}_error"] = 0
            write("provider", "codex_render")
            write(
                "provider",
                None,
                error=PermissionError("not exported"),
                metrics=codes,
            )
            data = json.loads(
                (
                    Path(tmp)
                    / "muninn-native-diagnostics"
                    / "muninn-provider-proof.json"
                ).read_text()
            )
            for key, value in codes.items():
                self.assertEqual(data[key], value)
            self.assertEqual(data["phase"], "codex_render")
            with self.assertRaises(ValueError):
                write(
                    "provider", "security_probe", metrics={"probe_v4_error": 0}
                )

    def test_non_windows_probe_creates_nothing(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch(
                "tests.provider_native_security.sys.platform", "darwin"
            ),
        ):
            path = Path(tmp) / "not-created.json"
            self.assertEqual(probe(path), {})
            self.assertFalse(path.exists())
