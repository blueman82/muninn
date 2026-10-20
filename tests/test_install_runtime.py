"""Provision immutable release runtimes before changing active state."""

from __future__ import annotations

import ast
import dataclasses
import importlib.metadata
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock

from install import installer as co
from install.context import Ctx, StepFailedError, run_real
from tests.installer_support import World, git, snapshot
from tools.shape_rules import check_imports

ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = (
    "Scripts/python.exe" if sys.platform == "win32" else "bin/python3"
)


class InstallRuntimeTests(unittest.TestCase):
    """Exercise pinned installation, bootstrap and fail-closed activation."""

    def prepare(self, ctx: Ctx, release: Path) -> Path:
        """Require and invoke the runtime preparation interface."""
        self.assertTrue(hasattr(co, "prepare_runtime"), "missing preparation")
        return co.prepare_runtime(ctx, release)

    def verify(self, python: Path, release: Path) -> None:
        """Require and invoke the independent bootstrap interface."""
        self.assertTrue(hasattr(co, "verify_runtime"), "missing verification")
        co.verify_runtime(python, release)

    def test_prepare_uses_final_immutable_path(self) -> None:
        self.assertTrue(hasattr(co, "prepare_runtime"), "missing preparation")
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(Path(tmp), run_real, "runtime")
            release = ctx.lib / ("1" * 40)
            release.mkdir(parents=True)
            shutil.copyfile(
                ROOT / "requirements-runtime.lock",
                release / "requirements-runtime.lock",
            )
            with (
                mock.patch.object(Path, "rename", side_effect=AssertionError),
                mock.patch.object(Path, "replace", side_effect=AssertionError),
            ):
                python = self.prepare(ctx, release)
            self.assertEqual(python, release / ".venv" / VENV_PYTHON)
            self.assertTrue(python.is_file())
            self.verify(python, release)
            self.assertFalse((ctx.lib / "current").exists())

    def _failed_candidate(self, failure: str) -> None:
        """Preserve active state when candidate preparation fails."""
        self.assertTrue(hasattr(co, "prepare_runtime"), "missing preparation")
        world = World(self)
        ctx = world.ctx(fresh=True)
        co.install(ctx, world.repo, world.sha)
        original = snapshot(world.home)
        store = ctx.data / "muninn.sqlite"
        before_store = store.read_bytes()
        first = (ctx.lib / "current").readlink()
        lock = world.repo / "requirements-runtime.lock"
        if failure == "hash":
            lock.write_text(
                "pydantic==2.14.0 --hash=sha256:" + "0" * 64 + "\n"
            )
        else:
            lock.write_text("pydantic==0.0.0 --hash=sha256:" + "0" * 64)
        git(world.repo, "add", "requirements-runtime.lock")
        git(world.repo, "commit", "-qm", "invalid runtime")
        sha = git(world.repo, "rev-parse", "HEAD").decode().strip()

        def runner(
            argv: Sequence[str | Path],
            env: Mapping[str, str] | None = None,
            input: bytes | None = None,
        ) -> subprocess.CompletedProcess[bytes]:
            if "venv" in argv or "pip" in argv:
                return run_real(argv, env=env, input=input)
            return world.fake.run(argv, env=env, input=input)

        upgrade = dataclasses.replace(
            world.ctx(upgrade=True), ts="failed-runtime", run=runner
        )
        with self.assertRaises(StepFailedError):
            co.install(upgrade, world.repo, sha)
        self.assertEqual((ctx.lib / "current").readlink(), first)
        self.assertEqual(snapshot(world.home), original)
        self.assertEqual(store.read_bytes(), before_store)
        self.assertEqual(world.fake.loaded, "new")

    def test_hash_mismatch_leaves_active_release(self) -> None:
        self._failed_candidate("hash")

    def test_dependency_missing_fails_before_activation(self) -> None:
        self._failed_candidate("missing")

    def test_fresh_refuses_existing_store(self) -> None:
        world = World(self)
        ctx = world.ctx(fresh=True)
        ctx.data.mkdir(parents=True)
        store = ctx.data / "muninn.sqlite"
        store.write_bytes(b"existing store")
        configs = snapshot(world.home)
        with self.assertRaises(StepFailedError):
            co.install(ctx, world.repo, world.sha)
        self.assertEqual(store.read_bytes(), b"existing store")
        self.assertEqual(snapshot(world.home), configs)
        self.assertFalse(ctx.lib.exists())

    def test_check_writes_nothing(self) -> None:
        world = World(self)
        before = snapshot(world.home)
        ctx = world.ctx(fresh=True, dry_run=True)
        co.install(ctx, world.repo, world.sha)
        self.assertEqual(snapshot(world.home), before)
        self.assertFalse(ctx.lib.exists())
        self.assertFalse(ctx.rdir.exists())
        self.assertFalse(ctx.data.exists())
        self.assertFalse(any("pip" in call for call in world.fake.calls))

    def test_bootstrap_schema_check_has_no_future_model_dependency(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp)
            self.verify(Path(sys.executable), release)
            self.assertEqual(list(release.iterdir()), [])
        self.assertEqual(importlib.metadata.version("pydantic"), "2.14.0")

    def test_missing_dependency_fails_bootstrap(self) -> None:
        self.assertTrue(hasattr(co, "verify_runtime"), "missing verification")
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp)
            subprocess.run(
                [sys.executable, "-m", "venv", release / ".venv"], check=True
            )
            with self.assertRaises(StepFailedError):
                self.verify(release / ".venv" / VENV_PYTHON, release)

    def test_standards_allow_only_named_dependency_closure(self) -> None:
        allowed = (
            "pydantic",
            "pydantic_core",
            "annotated_types",
            "typing_extensions",
            "typing_inspection",
            "sqlite3",
            "muninn",
        )
        for path in ("muninn/example.py", "install/example.py"):
            for module in (*allowed, "requests", "pydantic_settings"):
                with self.subTest(path=path, module=module):
                    tree = ast.parse(
                        "from __future__ import annotations\n"
                        f"import {module}\n"
                    )
                    violations = check_imports(path, tree)
                    self.assertEqual(bool(violations), module not in allowed)

    def test_same_sha_reuses_runtime_without_provisioning_or_rename(
        self,
    ) -> None:
        self.assertTrue(hasattr(co, "prepare_runtime"), "missing preparation")
        world = World(self)
        first = world.ctx(fresh=True)
        co.install(first, world.repo, world.sha)
        before = (first.lib / world.sha / ".venv").stat().st_ino
        ctx = dataclasses.replace(world.ctx(upgrade=True), ts="same-sha")
        original_rename = Path.rename
        original_replace = Path.replace

        def guard(src: Path, dst: Path, *, replace: bool = False) -> Path:
            if not src.is_symlink() and (src / ".venv").is_dir():
                raise AssertionError("provisioned release moved")
            return (
                original_replace(src, dst)
                if replace
                else original_rename(src, dst)
            )

        with (
            mock.patch.object(Path, "rename", guard),
            mock.patch.object(
                Path, "replace", lambda src, dst: guard(src, dst, replace=True)
            ),
        ):
            co.install(ctx, world.repo, world.sha)
        self.assertEqual((ctx.lib / world.sha / ".venv").stat().st_ino, before)

    def test_invalid_cached_runtime_preserves_active_state(self) -> None:
        self.assertTrue(hasattr(co, "prepare_runtime"), "missing preparation")
        world = World(self)
        first = world.ctx(fresh=True)
        co.install(first, world.repo, world.sha)
        before = snapshot(world.home)
        store = (first.data / "muninn.sqlite").read_bytes()
        ctx = dataclasses.replace(world.ctx(upgrade=True), ts="invalid-cache")
        with (
            mock.patch(
                "install.runtime.verify_runtime", side_effect=StepFailedError
            ),
            self.assertRaises(StepFailedError),
        ):
            co.install(ctx, world.repo, world.sha)
        self.assertEqual((ctx.lib / "current").readlink(), Path(world.sha))
        self.assertEqual(snapshot(world.home), before)
        self.assertEqual((first.data / "muninn.sqlite").read_bytes(), store)
