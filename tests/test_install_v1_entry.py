"""Drive actual shell check and installer main over both frozen sources."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from contextlib import closing
from unittest import mock

from install import policy_preview
from install.context import StepFailedError
from muninn import store
from muninn.review_json import canonical_json
from tests.installer_support import ROOT, World
from tests.policy_preview_support import states
from tests.policy_preview_v1_entry import entry_environment, prepare_entry
from tests.policy_preview_v1_support import V1PreviewFixture
from tools import prepare_codex_policy


class V1EntryTests(V1PreviewFixture):
    """Keep HOME, checkout, configs, selection and logs byte-identical."""

    def test_changed_host_policy_still_refuses_both_old_shapes(self) -> None:
        for shape in ("base", "state"):
            for explicit in (False, True):
                with self.subTest(shape=shape, explicit=explicit):
                    self.old(shape, explicit=explicit)
                    terms = self.policy.experimental_codex
                    assert terms is not None
                    changed_host = (
                        "f" * 64
                        if terms.host_identity != "f" * 64
                        else "e" * 64
                    )
                    changed = self.policy.model_copy(
                        update={
                            "experimental_codex": terms.model_copy(
                                update={"host_identity": changed_host}
                            )
                        }
                    )
                    rows, files = self.rows(), states(self.home)
                    with self.assertRaisesRegex(
                        StepFailedError, "review_policy_transition_required"
                    ):
                        policy_preview.validate(self.ctx, changed)
                    self.assertEqual(self.rows(), rows)
                    self.assertEqual(states(self.home), files)

    def test_actual_wrapper_check_and_main_retain_both_old_shapes(
        self,
    ) -> None:
        for shape in ("base", "state"):
            for explicit in (False, True):
                with self.subTest(shape=shape, explicit=explicit):
                    self.doCleanups()
                    self.setUp()
                    world = World(self)
                    script, interpreter = prepare_entry(world)
                    self.home = world.home
                    self.ctx = world.ctx(upgrade=True, dry_run=True)
                    self.path = store.db_path(self.ctx.data)
                    self.lock = self.ctx.data / "writer.lock"
                    with (
                        store.writer_lock(self.ctx.data),
                        closing(store.connect_rw(self.path)),
                    ):
                        pass
                    actual_host = prepare_codex_policy.host_identity()
                    saved_host = (
                        "f" * 64 if actual_host != "f" * 64 else "e" * 64
                    )
                    with mock.patch.object(
                        prepare_codex_policy,
                        "host_identity",
                        return_value=saved_host,
                    ):
                        self.old(shape, explicit=explicit)
                    self.assertNotEqual(saved_host, actual_host)
                    self.ctx.plist.parent.mkdir(parents=True, exist_ok=True)
                    self.ctx.plist.write_bytes(b"fixture stopped service")
                    self.ctx.lib.mkdir(mode=0o700, parents=True)
                    (self.ctx.lib / "current").symlink_to(world.sha)
                    (self.home / "explicit-policy.json").write_text(
                        canonical_json(self.policy)
                    )
                    environment = entry_environment(
                        world, interpreter, self.policy, shape, explicit
                    )
                    files, checkout, rows = (
                        states(self.home),
                        states(world.repo),
                        self.rows(),
                    )
                    command = [str(script), "--check"]
                    if os.name == "nt":
                        shell = shutil.which("sh")
                        if shell is None:
                            raise RuntimeError(
                                "required wrapper shell unavailable"
                            )
                        command.insert(0, shell)
                    result = subprocess.run(
                        command,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertIn(
                        "OK to run: bin/muninn-install", result.stdout
                    )
                    self.assertIn("POLICY_ROUTE_OK", result.stdout)
                    self.assertEqual(states(self.home), files)
                    self.assertEqual(states(world.repo), checkout)
                    self.assertEqual(self.rows(), rows)
                    source_bytes = self.path.read_bytes()
                    environment["V1_REAL_PREFLIGHT"] = "1"
                    real = subprocess.run(
                        [
                            sys.executable,
                            "-B",
                            str(world.home.parent / "entry.py"),
                            str(ROOT),
                            "--repo",
                            str(world.repo),
                            "--sha",
                            world.sha,
                            "--upgrade",
                        ],
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                    self.assertEqual(
                        real.returncode, 0, real.stdout + real.stderr
                    )
                    self.assertIn("REAL_PREFLIGHT_OK", real.stdout)
                    self.assertEqual(self.path.read_bytes(), source_bytes)
                    self.assertEqual(self.rows(), rows)
                    self.assertEqual(states(world.repo), checkout)
