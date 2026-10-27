"""Preview leaves checkout, HOME and interpreter unchanged."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.install_wrapper_support import WrapperFixture, git


def diagnostic_entry(
    entry: tuple[int, int, bytes | str | None] | None,
) -> object:
    """Describe snapshot state without disclosing captured content."""
    if entry is None:
        return None
    mode, mtime, content = entry
    encoded = content.encode() if isinstance(content, str) else content
    return {
        "mode": mode,
        "mtime_ns": mtime,
        "content": (
            None
            if encoded is None
            else {
                "size": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        ),
    }


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

    def assert_snapshots(
        self,
        after: tuple[dict[str, tuple[int, int, bytes | str | None]], ...],
        before: tuple[dict[str, tuple[int, int, bytes | str | None]], ...],
    ) -> None:
        """Compare every captured path, content byte and metadata field."""
        differences = [
            {
                "part": part,
                "path": key,
                "before": diagnostic_entry(old.get(key)),
                "after": diagnostic_entry(new.get(key)),
            }
            for part, (new, old) in enumerate(zip(after, before, strict=True))
            for key in sorted(set(new) | set(old))
            if new.get(key) != old.get(key)
        ]
        with mock.patch.object(self, "longMessage", False):
            self.assertEqual(
                after, before, json.dumps(differences, sort_keys=True)
            )

    def test_snapshot_failure_identifies_mutations_without_content(
        self,
    ) -> None:
        root = self.home / "diagnostic"
        root.mkdir()
        path = root / "private"
        path.write_bytes(b"first-secret")
        before = (snapshot(root),)
        original = path.stat()
        path.write_bytes(b"other-secret")
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaises(AssertionError) as caught:
            self.assert_snapshots((snapshot(root),), before)
        self.assertIn('"path": "private"', str(caught.exception))
        self.assertIn('"sha256":', str(caught.exception))
        self.assertNotIn("first-secret", str(caught.exception))
        self.assertNotIn("other-secret", str(caught.exception))
        entry = json.loads(str(caught.exception))[0]
        self.assertEqual(entry["before"]["mode"], entry["after"]["mode"])
        self.assertEqual(
            entry["before"]["mtime_ns"], entry["after"]["mtime_ns"]
        )
        self.assertNotEqual(
            entry["before"]["content"], entry["after"]["content"]
        )
        for change in ("metadata", "added", "removed"):
            with self.subTest(change=change):
                before = (snapshot(root),)
                state = path.stat()
                if change == "metadata":
                    os.utime(
                        path,
                        ns=(state.st_atime_ns, state.st_mtime_ns + 1000000000),
                    )
                elif change == "added":
                    (root / "added").write_bytes(b"another-secret")
                else:
                    path.unlink()
                with self.assertRaises(AssertionError) as caught:
                    self.assert_snapshots((snapshot(root),), before)
                entries = {
                    item["path"]: item
                    for item in json.loads(str(caught.exception))
                }
                selected = entries["added" if change == "added" else "private"]
                if change == "added":
                    self.assertIsNone(selected["before"])
                    self.assertIsNotNone(selected["after"])
                elif change == "removed":
                    self.assertIsNotNone(selected["before"])
                    self.assertIsNone(selected["after"])
                else:
                    self.assertEqual(
                        selected["before"]["mtime_ns"], state.st_mtime_ns
                    )
                    self.assertEqual(
                        selected["after"]["mtime_ns"],
                        state.st_mtime_ns + 1000000000,
                    )
                    self.assertEqual(
                        selected["before"]["content"],
                        selected["after"]["content"],
                    )
                self.assertNotIn("another-secret", str(caught.exception))

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
        self.assert_snapshots((snapshot(root), snapshot(self.home)), before)

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
        self.assert_snapshots((snapshot(root), snapshot(self.home)), before)

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
        self.assert_snapshots((snapshot(root), snapshot(self.home)), before)

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
                self.assert_snapshots(
                    (snapshot(root), snapshot(self.home)), before
                )
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
                self.assert_snapshots(
                    (snapshot(root), snapshot(self.home)), before
                )
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
        self.assert_snapshots(
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
        self.assert_snapshots(
            (snapshot(root), snapshot(self.home), snapshot(runtime)), before
        )
