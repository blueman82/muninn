"""Trust is derived only for a stable, successfully identified provider."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from install.context import Ctx
from install.provider_paths import codex_identity
from install.steps_config import _can_trust, _codex_observation, codex


class ProviderVersionTest(unittest.TestCase):
    """Platform proof and executable identity gate automatic trust."""

    def observe(
        self,
        version: bytes,
        *,
        platform: str = "win32",
        code: int = 0,
        identities: tuple[tuple[str, ...], ...] = (("same",), ("same",)),
    ) -> tuple[str, tuple[str, ...]]:
        """Observe a controlled version process and candidate identities."""
        done = subprocess.CompletedProcess(["synthetic"], code, version, b"")
        ctx = Ctx(
            Path("/synthetic"),
            mock.Mock(return_value=done),
            "test",
            platform=platform,
        )
        with (
            mock.patch(
                "install.steps_config.codex_argv", return_value=["synthetic"]
            ),
            mock.patch(
                "install.steps_config.codex_identity", side_effect=identities
            ),
        ):
            return _codex_observation(ctx)

    def test_verified_windows_version_requires_exit_zero(self) -> None:
        self.assertEqual(
            self.observe(b"codex-cli 0.159.2\n"),
            ("codex-cli 0.159.2", ("same",)),
        )
        self.assertEqual(
            self.observe(b"codex-cli 0.159.2\n", code=1), ("", ())
        )

    def test_unproved_windows_version_leaves_owner_step(self) -> None:
        self.assertEqual(self.observe(b"codex-cli 0.159.3\n"), ("", ()))
        self.assertEqual(self.observe(b"codex-cli 9.9.9\n"), ("", ()))

    def test_verified_posix_allowlist_is_preserved(self) -> None:
        self.assertEqual(
            self.observe(b"codex-cli 0.159.3\n", platform="darwin"),
            ("codex-cli 0.159.3", ("same",)),
        )

    def test_candidate_change_during_discovery_refuses_trust(self) -> None:
        self.assertEqual(
            self.observe(
                b"codex-cli 0.159.2\n", identities=(("old",), ("new",))
            ),
            ("", ()),
        )

    def test_candidate_or_version_change_before_write_refuses_trust(
        self,
    ) -> None:
        ctx = Ctx(Path("/synthetic"), mock.Mock(), "test")
        rec = {"codex_version": "codex-cli 0.159.2", "codex_identity": ["old"]}
        for observed in (
            ("codex-cli 0.159.2", ("new",)),
            ("codex-cli 0.159.3", ("old",)),
            ("", ()),
        ):
            with mock.patch(
                "install.steps_config._codex_observation",
                return_value=observed,
            ):
                self.assertFalse(_can_trust(ctx, rec))
        with mock.patch(
            "install.steps_config._codex_observation",
            return_value=("codex-cli 0.159.2", ("old",)),
        ):
            self.assertTrue(_can_trust(ctx, rec))

    def test_changed_candidate_never_applies_derived_trust_edit(self) -> None:
        messages: list[str] = []
        ctx = Ctx(
            Path("/synthetic"),
            mock.Mock(),
            "test",
            platform="darwin",
            say=messages.append,
        )
        rec = {
            "has_codex": True,
            "trust": "auto",
            "codex_version": "codex-cli 0.159.2",
            "codex_identity": ["old"],
        }
        with (
            mock.patch(
                "install.steps_config._codex_observation",
                return_value=("codex-cli 0.159.2", ("new",)),
            ),
            mock.patch(
                "install.steps_config._add_plugin",
                return_value=b"synthetic hooks",
            ),
            mock.patch("install.steps_config._edit_config") as edits,
        ):
            codex(ctx, rec)
        self.assertEqual(edits.call_count, 3)
        self.assertEqual(rec["trust"], "owner")
        self.assertIn("OWNER STEP", messages[-1])

    def test_identity_detects_byte_change_and_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic executable"
            path.write_bytes(b"first")
            ctx = Ctx(Path(tmp), mock.Mock(), "test")
            with mock.patch(
                "install.provider_paths.codex_argv", return_value=[str(path)]
            ):
                original = codex_identity(ctx)
                metadata = path.stat()
                path.write_bytes(b"other")
                os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
                changed = codex_identity(ctx)
                self.assertNotEqual(original, changed)
                replacement = Path(tmp) / "replacement"
                replacement.write_bytes(b"other")
                os.utime(
                    replacement,
                    ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
                )
                replacement.replace(path)
                self.assertNotEqual(changed, codex_identity(ctx))
