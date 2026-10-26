"""Preview leaves checkout, HOME and interpreter unchanged."""

from __future__ import annotations

import shutil
import subprocess
import sys
import venv
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.install_wrapper_support import WrapperFixture, git


def snapshot(root: Path) -> dict[str, tuple[int, int, bytes | str | None]]:
    """Capture contents and metadata, including directories and symlinks."""
    result: dict[str, tuple[int, int, bytes | str | None]] = {}
    for path in (root, *root.rglob("*")):
        state = path.lstat()
        content = (
            str(path.readlink())
            if path.is_symlink()
            else path.read_bytes() if path.is_file() else None
        )
        result[str(path.relative_to(root))] = (
            state.st_mode,
            state.st_mtime_ns,
            content,
        )
    return result


class CheckTest(WrapperFixture):
    """Exercise the preview boundary with controlled and real interpreters."""

    def test_check_reuses_ready_checkout_without_hook_setup(self) -> None:
        root = self.hook_repo()
        interpreter = root / ".venv/bin/python3"
        interpreter.parent.mkdir(parents=True)
        shutil.copy2(self.stub, interpreter)
        before = snapshot(root), snapshot(self.home)
        result = self.run_wrapper(
            "--check",
            script=root / "bin/muninn-install",
            input="y\ny\n",
            extra_env={"IMPORT_RC": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--fresh --dry-run", self.argv())
        self.assertNotIn("[y/N]", result.stderr)
        self.assertEqual((snapshot(root), snapshot(self.home)), before)

    def test_check_refuses_half_install_without_setup(self) -> None:
        root = self.hook_repo()
        self.data.mkdir(parents=True)
        before = snapshot(root), snapshot(self.home)
        result = self.run_wrapper(
            "--check",
            script=root / "bin/muninn-install",
            input="y\ny\n",
            extra_env={"IMPORT_RC": "1"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("half-installed", result.stderr)
        self.assertEqual(self.argv(), "")
        self.assertEqual((snapshot(root), snapshot(self.home)), before)

    def test_real_install_preserves_custom_hooks_without_setup(self) -> None:
        root = self.hook_repo()
        git(root, "config", "core.hooksPath", "custom-hooks")
        before = snapshot(root), snapshot(self.home)
        result = self.run_wrapper(
            script=root / "bin/muninn-install", input="y\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--dry-run", self.argv())
        self.assertNotIn("pip install", self.argv())
        self.assertEqual((snapshot(root), snapshot(self.home)), before)

    def test_check_refuses_missing_or_incompatible_runtime_without_setup(
        self,
    ) -> None:
        for failure in ({"IMPORT_RC": "1"}, {"PINNED_VERSION_RC": "1"}):
            with self.subTest(failure=failure):
                self.log.unlink(missing_ok=True)
                root = self.hook_repo()
                before = snapshot(root), snapshot(self.home)
                result = self.run_wrapper(
                    "--check",
                    script=root / "bin/muninn-install",
                    input="y\ny\n",
                    extra_env=failure,
                )
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual((snapshot(root), snapshot(self.home)), before)
                self.assertNotIn("[y/N]", result.stderr)
                self.assertIn("requirements-runtime.lock", result.stderr)
                self.assertNotIn("OK to run", result.stdout)
                self.assertEqual(self.argv(), "")

    def test_check_never_enables_missing_or_custom_git_hooks(self) -> None:
        for hook_path in (None, "custom-hooks"):
            with self.subTest(hook_path=hook_path):
                self.log.unlink(missing_ok=True)
                root = self.hook_repo()
                if hook_path is not None:
                    git(root, "config", "core.hooksPath", hook_path)
                before = snapshot(root), snapshot(self.home)
                result = self.run_wrapper(
                    "--check", script=root / "bin/muninn-install", input="y\n"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((snapshot(root), snapshot(self.home)), before)
                self.assertNotIn("[y/N]", result.stderr)
                self.assertNotIn("pip install", self.argv())
                self.assertIn("--fresh --dry-run", self.argv())

    def test_real_dependency_probe_and_preview_create_no_bytecode(
        self,
    ) -> None:
        root = self.hook_repo()
        runtime = self.home.parent / "runtime"
        venv.EnvBuilder(with_pip=False).create(runtime)
        interpreter = runtime / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python3"
        )
        purelib = subprocess.check_output(
            [
                interpreter,
                "-B",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            text=True,
        ).strip()
        (Path(purelib) / "pydantic.py").write_text('__version__ = "2.14.0"\n')
        package = root / "install"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "installer.py").write_text(
            'import sys\nprint(" ".join(sys.argv[1:]))\n'
        )
        git(root, "config", "core.hooksPath", "custom-hooks")
        before = snapshot(root), snapshot(self.home), snapshot(runtime)
        result = self.run_wrapper(
            "--check",
            script=root / "bin/muninn-install",
            extra_env={"MUNINN_PYTHON": interpreter.as_posix()},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--fresh --dry-run", result.stdout)
        self.assertEqual(
            (snapshot(root), snapshot(self.home), snapshot(runtime)), before
        )
        self.assertEqual(list(runtime.rglob("pydantic*.pyc")), [])
        with mock.patch(
            "tests.install_wrapper_support.sys",
            SimpleNamespace(platform="win32"),
        ):
            facade = self.run_wrapper(
                "--check",
                script=root / "bin/muninn-install",
                extra_env={"MUNINN_PYTHON": interpreter.as_posix()},
            )
        self.assertEqual(facade.returncode, 0, facade.stderr)
        self.assertIn("--fresh --dry-run", facade.stdout)
        self.assertEqual(
            (snapshot(root), snapshot(self.home), snapshot(runtime)), before
        )
