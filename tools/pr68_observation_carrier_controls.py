"""Inspect the diagnostic carrier without applying or running its overlay."""

from __future__ import annotations

import ast
import hashlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools import pr68_observation_binding as binding
from tools import pr68_observation_overlay as overlay

ROOT = Path(__file__).resolve().parent.parent


class CarrierControls(unittest.TestCase):
    """Check one-shot topology and unchanged platform definitions."""

    def test_collector_is_stdlib_only_and_logical_runtime_copy_is_absent(
        self,
    ) -> None:
        """Keep release imports independent from the carrier tools package."""
        tree = ast.parse((ROOT / "tools/_cleanup_observation.py").read_text())
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(
                    alias.name.split(".")[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                modules.append((node.module or "").split(".")[0])
        self.assertTrue(
            set(modules) <= sys.stdlib_module_names | {"__future__"}
        )
        self.assertFalse((ROOT / "muninn/_cleanup_observation.py").exists())
        self.assertTrue((ROOT / "muninn/_cleanup_observation.pyi").is_file())

    def test_existing_registration_has_only_manual_one_ubuntu_job(
        self,
    ) -> None:
        """Require the selected existing path to define no automatic run."""
        text = (ROOT / ".github/workflows/worker-quiescence.yml").read_text()
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("pull_request:", text)
        self.assertNotIn("push:", text)
        self.assertEqual(text.count("runs-on: ubuntu-24.04"), 1)
        self.assertNotIn("matrix:", text)
        self.assertIn("timeout-minutes: 75", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertNotIn("tools.worker_quiescence_proof", text)

    def test_original_direct_full_suite_appears_exactly_once(self) -> None:
        """Require one direct suite after preparation and native setup."""
        text = (ROOT / ".github/workflows/worker-quiescence.yml").read_text()
        command = (
            'PATH="$PWD/.venv/bin:$PATH" '
            ".venv/bin/python3.13 -m tools.check --full"
        )
        self.assertEqual(text.count(command), 1)
        self.assertLess(
            text.index("tests.provider_native"), text.index(command)
        )
        self.assertLess(
            text.index("tools.pr68_observation_overlay"), text.index(command)
        )
        self.assertLess(
            text.index("tools.pr68_observation_binding"), text.index(command)
        )
        self.assertIn("if: always()", text)
        self.assertIn("retention-days: 30", text)
        self.assertIn("MUNINN_CLEANUP_OBSERVATION_WORKTREE:", text)
        self.assertEqual(
            text.count(
                "working-directory: "
                "${{ env.MUNINN_CLEANUP_OBSERVATION_WORKTREE }}"
            ),
            3,
        )

    def test_setup_failure_cannot_launch_the_suite(self) -> None:
        """Require successful overlay and binding before the sole suite."""
        text = (ROOT / ".github/workflows/worker-quiescence.yml").read_text()
        gate = text.split("      - name: Run mandatory full gate\n")[1]
        condition = gate.split("        shell:")[0]
        self.assertIn("success()", condition)
        self.assertIn(
            "steps.observation_overlay.outcome == 'success'", condition
        )
        self.assertIn(
            "steps.observation_binding.outcome == 'success'", condition
        )

    def test_platform_workflow_and_original_native_sources_are_unchanged(
        self,
    ) -> None:
        """Keep ordinary gate inputs separate from hosted payload bytes."""
        expected = {
            ".github/workflows/platforms.yml": (
                "ad8f001a51e09bf9b602041e077982861b7"
                "66b38e0c48cdc6edf651de6d5d781"
            ),
            "tools/test_process.py": (
                "e316ffb7b5b5c2e3d12341cbd3a2ad0642"
                "71f693a13db96eabbb95e2e161bfa7"
            ),
            "muninn/review_process_quiescence.py": (
                "a307fcb43088d059d30d665f223b261a8"
                "b26f5f07c76b5a5aeac26d7761becd9"
            ),
        }
        for name, digest in expected.items():
            self.assertEqual(
                hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), digest
            )

    def test_detached_setup_stages_only_the_private_source(self) -> None:
        """Inspect real setup arguments while replacing every IO operation."""
        destination = mock.MagicMock()
        destination.exists.return_value = False
        destination.parent.is_dir.return_value = True
        payloads: dict[str, str] = {}

        def git(*args: str) -> bytes:
            if args[0] == "status":
                return b""
            if args == ("rev-parse", "HEAD"):
                return b"carrier-head\n"
            if args[-2:] == ("rev-parse", "HEAD"):
                return overlay.BASE.encode()
            return b"bound-metadata"

        environment = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": (
                "refs/heads/diagnostic/pr68-cleanup-observation-one-shot"
            ),
            "MUNINN_CLEANUP_OBSERVATION_WORKTREE": "owned-private-source",
        }
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(overlay, "Path", return_value=destination),
            mock.patch.object(overlay, "ROOT", mock.MagicMock()),
            mock.patch.object(overlay, "PAYLOADS", payloads),
            mock.patch.object(overlay, "original_sources", return_value={}),
            mock.patch.object(
                overlay, "copy_overlay", return_value=[]
            ) as copy,
            mock.patch.object(
                overlay, "clear_overlay_caches", return_value=[]
            ),
            mock.patch.object(overlay, "git", side_effect=git) as commands,
            mock.patch.object(overlay, "write") as write,
        ):
            overlay.apply()
        commands.assert_any_call(
            "worktree", "add", "--detach", str(destination), overlay.BASE
        )
        commands.assert_any_call("-C", str(destination), "add", "--")
        commands.assert_any_call("-C", str(destination), "write-tree")
        copy.assert_called_once_with(destination)
        evidence = write.call_args.args[1]
        self.assertEqual(evidence["diagnostic_head"], overlay.BASE)
        self.assertEqual(evidence["carrier_head"], "carrier-head")
        self.assertEqual(evidence["child_loaded_closure"], "not_observed")

    def test_binding_refuses_a_different_head_before_gate_preparation(
        self,
    ) -> None:
        """Reject wrong Git identity without source hashing or native work."""
        with (
            mock.patch.dict(
                os.environ, {"MUNINN_CLEANUP_OBSERVATION_DIR": "x"}
            ),
            mock.patch.object(binding, "Path") as path,
            mock.patch.object(binding, "git", return_value=b"wrong-head"),
            mock.patch.object(binding, "digest") as digest,
            mock.patch.object(binding, "write") as write,
        ):
            artifact = path.return_value.__truediv__.return_value
            artifact.read_text.return_value = "{}"
            with self.assertRaisesRegex(
                ValueError, "diagnostic_identity_drift"
            ):
                binding.prepare()
        digest.assert_not_called()
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
