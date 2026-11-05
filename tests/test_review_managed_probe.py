"""Check bounded model-free negotiation and disposable observation caches."""

from __future__ import annotations

import time
from pathlib import Path
from unittest import mock

from muninn import review_managed_files as files
from muninn import review_managed_probe as probes
from tests.test_review_managed_files import ManagedFileTests


class ManagedProbeTests(ManagedFileTests):
    """Keep probes fixed and isolated from account or model calls."""

    def probe(
        self, observed: files.RuntimeIdentity, *, persist: bool = False
    ) -> files.RuntimeIdentity:
        """Negotiate an observed fixture using the real cache boundary."""
        return probes.negotiate(
            self.home,
            self.selected,
            observed,
            deadline=time.monotonic() + 10,
            persist=persist,
        )

    def run_probe(
        self,
        observed: files.RuntimeIdentity,
        args: tuple[str, ...],
        deadline: float,
    ) -> bytes:
        """Return only fixed help/version output without native activity."""
        self.assertGreater(deadline, time.monotonic())
        if args == ("--version",):
            return b"codex-cli 0.166.0\n"
        self.assertIn(args, (("--help",), ("exec", "--help")))
        return (
            b"--ephemeral --ignore-user-config --ignore-rules --disable "
            b"--sandbox --skip-git-repo-check --output-schema --json "
            b"--color --model --config --cd"
        )

    def test_preview_is_nonwriting_and_commands_are_fixed(self) -> None:
        observed = self.observe()
        before = sorted(self.home.rglob("*"))
        with mock.patch.object(
            probes, "run_probe", side_effect=self.run_probe
        ) as native:
            self.assertEqual(self.probe(observed), observed)
        self.assertEqual(
            [call.args[1] for call in native.call_args_list],
            [("--version",), ("--help",), ("exec", "--help")],
        )
        self.assertEqual(sorted(self.home.rglob("*")), before)

    def test_cache_reuses_only_exact_current_identity(self) -> None:
        observed = self.observe()
        with mock.patch.object(
            probes, "run_probe", side_effect=self.run_probe
        ) as native:
            self.probe(observed, persist=True)
            self.probe(observed, persist=True)
            self.assertEqual(native.call_count, 3)
        path = (
            self.home
            / "review-capabilities"
            / "managed-cache"
            / (observed.capability_key + ".json")
        )
        path.write_bytes(b'{"forged":true}')
        with mock.patch.object(
            probes, "run_probe", side_effect=self.run_probe
        ) as native:
            self.assertEqual(self.probe(observed), observed)
            self.assertEqual(native.call_count, 3)
        self.assertEqual(path.read_bytes(), b'{"forged":true}')
        with mock.patch.object(
            probes, "run_probe", side_effect=self.run_probe
        ) as native:
            self.probe(observed, persist=True)
            self.assertEqual(native.call_count, 3)
        self.assertNotEqual(path.read_bytes(), b'{"forged":true}')

    def test_unknown_help_and_probe_denial_refuse(self) -> None:
        observed = self.observe()
        with (
            mock.patch.object(probes, "run_probe", return_value=b"unknown"),
            self.assertRaisesRegex(
                ValueError, "runtime_interface_unsupported"
            ),
        ):
            self.probe(observed)
        with mock.patch(
            "subprocess.Popen", side_effect=PermissionError
        ) as native:
            with self.assertRaisesRegex(
                ValueError, "runtime_probe_unavailable"
            ):
                probes.run_probe(
                    observed, ("--version",), time.monotonic() + 10
                )
            self.assertEqual(native.call_count, 1)
            self.assertEqual(
                native.call_args.args[0][0], "/usr/bin/sandbox-exec"
            )
            self.assertNotIn("CODEX_HOME", native.call_args.kwargs["env"])
            self.assertEqual(
                native.call_args.kwargs["cwd"], Path("/var/empty")
            )

    def test_stale_snapshot_never_reuses_cache(self) -> None:
        observed = self.observe()
        (self.release / "codex-path/rg").write_bytes(b"changed")
        with mock.patch.object(probes, "run_probe") as native:
            with self.assertRaisesRegex(
                ValueError, "runtime_selection_unavailable"
            ):
                self.probe(observed)
            native.assert_not_called()

    def test_interrupted_cache_publication_leaves_no_partial_final_key(
        self,
    ) -> None:
        observed = self.observe()
        path = (
            self.home
            / "review-capabilities"
            / "managed-cache"
            / (observed.capability_key + ".json")
        )
        with (
            mock.patch.object(probes, "run_probe", side_effect=self.run_probe),
            mock.patch("muninn.file_sync.sync_fd", side_effect=OSError),
            self.assertRaises(OSError),
        ):
            self.probe(observed, persist=True)
        self.assertFalse(path.exists())

    def test_unsafe_cache_is_not_a_revalidation_miss(self) -> None:
        observed = self.observe()
        with mock.patch.object(
            probes, "run_probe", side_effect=self.run_probe
        ):
            self.probe(observed, persist=True)
        path = (
            self.home
            / "review-capabilities"
            / "managed-cache"
            / (observed.capability_key + ".json")
        )
        path.chmod(0o644)
        with mock.patch.object(probes, "run_probe") as native:
            with self.assertRaisesRegex(
                ValueError, "runtime_selection_unsafe"
            ):
                self.probe(observed)
            native.assert_not_called()

    def test_bad_cache_and_failed_revalidation_never_accept_or_write(
        self,
    ) -> None:
        observed = self.observe()
        with mock.patch.object(
            probes, "run_probe", side_effect=self.run_probe
        ):
            self.probe(observed, persist=True)
        path = (
            self.home
            / "review-capabilities"
            / "managed-cache"
            / (observed.capability_key + ".json")
        )
        path.write_bytes(b"truncated")
        with (
            mock.patch.object(
                probes,
                "run_probe",
                side_effect=ValueError("runtime_probe_unavailable"),
            ),
            self.assertRaisesRegex(ValueError, "runtime_probe_unavailable"),
        ):
            self.probe(observed, persist=True)
        self.assertEqual(path.read_bytes(), b"truncated")

    def test_cache_disappearing_during_read_is_unsafe_not_a_miss(self) -> None:
        observed = self.observe()
        with mock.patch.object(
            probes, "run_probe", side_effect=self.run_probe
        ):
            self.probe(observed, persist=True)
        original = probes.review_runner.read_private_file

        def disappear(path: Path, limit: int) -> bytes:
            raw = original(path, limit)
            path.unlink()
            return raw

        with (
            mock.patch.object(
                probes.review_runner,
                "read_private_file",
                side_effect=disappear,
            ),
            mock.patch.object(
                probes, "run_probe", side_effect=self.run_probe
            ) as native,
            self.assertRaisesRegex(ValueError, "runtime_selection_unsafe"),
        ):
            self.probe(observed)
        native.assert_not_called()
