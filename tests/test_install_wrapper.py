"""bin/muninn-install picks the install mode and passes the right flags."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.installer_support import ROOT, git

WRAPPER = ROOT / "bin/muninn-install"


class WrapperTest(unittest.TestCase):
    """Run the wrapper in a temp HOME with a stub in place of Python."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.log = Path(tmp.name) / "argv"
        self.stub = Path(tmp.name) / "python-stub"
        self.stub.write_text(
            '#!/bin/sh\nif [ "$1" = "-m" ] && [ "$2" = "venv" ]; then '
            'if [ "${VENV_DIR:-bin}" = "Scripts" ]; then '
            'mkdir -p "$3/Scripts"; cp "$0" "$3/Scripts/python.exe"; '
            'else mkdir -p "$3/bin"; cp "$0" "$3/bin/python3"; '
            'cp "$0" "$3/bin/python3.13"; fi; exit 0; fi\n'
            'if [ "$3" = "-c" ] && '
            '[ "$4" = "import sys; print(sys.platform)" ]; then '
            'echo "${PY_PLATFORM:-linux}"; exit 0; fi\n'
            'if [ "$3" = "-c" ]; then '
            'if [ "$0" = "$MUNINN_PYTHON" ] && '
            '[ "${IMPORT_RC:-0}" -ne 0 ]; then exit "$IMPORT_RC"; fi\n'
            'exit "${PINNED_VERSION_RC:-0}"; fi\n'
            f'echo "$@" >> {shlex.quote(self.log.as_posix())}\n'
            'exit "${STUB_RC:-0}"\n'
        )
        self.stub.chmod(0o755)
        self.sha = git(ROOT, "rev-parse", "HEAD").decode().strip()
        self.lib = self.home / ".local/lib/muninn"
        self.data = self.home / ".local/share/muninn"

    def run_wrapper(
        self,
        *args: str,
        rc: int = 0,
        script: Path = WRAPPER,
        input: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a ``bin`` script against the temp HOME.

        Args:
            *args: Command-line arguments.
            rc: Exit status the stub Python should return.
            script: The script to run, ``bin/muninn-install`` by default.
            input: Text sent to the wrapper's stdin.
            extra_env: Additional variables for the wrapper process.

        Returns:
            The finished process, with text output.
        """
        env = dict(
            os.environ,
            HOME=str(self.home),
            MUNINN_PYTHON=str(self.stub),
            STUB_RC=str(rc),
        )
        env.update(extra_env or {})
        command: list[str | Path] = [script, *args]
        if sys.platform == "win32":
            shell = shutil.which("sh")
            if shell is None:
                raise RuntimeError("Windows wrapper fixture needs required sh")
            command = [shell, script.as_posix(), *args]
            env.update(
                HOME=self.home.as_posix(), MUNINN_PYTHON=self.stub.as_posix()
            )
        return subprocess.run(
            command,
            env=env,
            input=input,
            capture_output=True,
            text=True,
        )

    def installed(self, sha: str) -> None:
        """Make the temp HOME look like a machine running release ``sha``."""
        (self.lib / sha).mkdir(parents=True)
        (self.lib / "current").symlink_to(sha)
        self.data.mkdir(parents=True)

    def argv(self) -> str:
        """Return what the stub Python was run with, or empty if never."""
        return self.log.read_text() if self.log.exists() else ""

    def hook_repo(self) -> Path:
        """Create a temporary checkout for Git-hook setup tests."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "bin").mkdir()
        script = root / "bin" / "muninn-install"
        shutil.copyfile(WRAPPER, script)
        script.chmod(0o755)
        shutil.copyfile(
            ROOT / "requirements-runtime.lock",
            root / "requirements-runtime.lock",
        )
        shutil.copyfile(
            ROOT / "requirements-dev.txt", root / "requirements-dev.txt"
        )
        git(root, "init", "-q")
        git(root, "commit", "--allow-empty", "-qm", "initial")
        return root

    def test_windows_facade_runs_the_same_script_with_required_sh(
        self,
    ) -> None:
        shell = shutil.which("sh")
        self.assertIsNotNone(shell)
        with (
            mock.patch(
                __name__ + ".sys",
                SimpleNamespace(platform="win32"),
                create=True,
            ),
            mock.patch.object(subprocess, "run", wraps=subprocess.run) as run,
        ):
            result = self.run_wrapper("--repo", "/x", "--sha", "f" * 40)
        self.assertEqual(
            run.call_args.args[0][:2], [shell, WRAPPER.as_posix()]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--repo /x --sha {'f' * 40}", self.argv())
        self.assertEqual(
            run.call_args.kwargs["env"]["HOME"], self.home.as_posix()
        )
        self.assertEqual(
            run.call_args.kwargs["env"]["MUNINN_PYTHON"], self.stub.as_posix()
        )

    def test_windows_facade_refuses_missing_required_shell(self) -> None:
        with (
            mock.patch(
                __name__ + ".sys",
                SimpleNamespace(platform="win32"),
                create=True,
            ),
            mock.patch.object(shutil, "which", return_value=None),
            mock.patch.object(subprocess, "run") as run,
            self.assertRaisesRegex(RuntimeError, "required sh"),
        ):
            self.run_wrapper()
        run.assert_not_called()

    def test_bare_on_an_empty_machine_runs_a_fresh_install(self) -> None:
        r = self.run_wrapper()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--fresh", self.argv())
        self.assertNotIn("--dry-run", self.argv())
        self.assertIn(f"--sha {self.sha}", self.argv())
        self.assertIn(f"installed {self.sha[:7]} (fresh)", r.stdout)

    def test_bare_on_an_installed_machine_upgrades(self) -> None:
        self.installed("a" * 40)
        r = self.run_wrapper()
        self.assertIn("--upgrade", self.argv())
        self.assertIn(f"installed {self.sha[:7]} (upgrade)", r.stdout)

    def test_the_summary_line_names_the_poller_pid(self) -> None:
        self.installed("a" * 40)
        (self.data / "status.json").write_text('{"passes": 1, "pid": 4242}')
        r = self.run_wrapper()
        self.assertIn("(upgrade); poller pid 4242; doctor ok;", r.stdout)

    def test_the_summary_line_survives_a_missing_status_file(self) -> None:
        r = self.run_wrapper()
        self.assertIn("poller pid unknown", r.stdout)

    def test_uninstall_passes_its_flags_to_the_uninstaller(self) -> None:
        r = self.run_wrapper("--uninstall", "--dry-run", "--purge-data")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            self.argv().split()[-4:],
            ["-m", "install.uninstall", "--dry-run", "--purge-data"],
        )

    def test_uninstall_runs_on_a_half_installed_machine(self) -> None:
        self.data.mkdir(parents=True)
        r = self.run_wrapper("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("install.uninstall", self.argv())

    def test_uninstall_needs_neither_git_nor_a_commit(self) -> None:
        bare = self.home.parent / "tarball/bin"
        bare.mkdir(parents=True)
        shutil.copy2(WRAPPER, bare / "muninn-install")
        r = self.run_wrapper("--uninstall", script=bare / "muninn-install")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-m install.uninstall", self.argv())

    def test_the_uninstall_command_is_the_uninstall_flag(self) -> None:
        r = self.run_wrapper("--dry-run", script=ROOT / "bin/muninn-uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-m install.uninstall --dry-run", self.argv())

    def test_help_describes_the_wrapper_and_writes_nothing(self) -> None:
        for flag in ("--help", "-h"):
            r = self.run_wrapper(flag)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("--check", r.stdout)
            self.assertIn("--status", r.stdout)
        self.assertEqual(self.argv(), "")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_a_failed_install_prints_no_success_line(self) -> None:
        r = self.run_wrapper(rc=1)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("installed", r.stdout)

    def test_half_an_install_is_refused_before_anything_runs(self) -> None:
        self.data.mkdir(parents=True)  # data without a current release
        r = self.run_wrapper()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("half-installed", r.stderr)
        self.assertEqual(self.argv(), "")

    def test_check_is_a_dry_run_that_ends_with_the_command_to_run(
        self,
    ) -> None:
        self.installed("a" * 40)
        r = self.run_wrapper("--check")
        self.assertIn("--dry-run", self.argv())
        self.assertIn("Upgrade: aaaaaaa", r.stdout)
        self.assertTrue(r.stdout.endswith("OK to run: bin/muninn-install\n"))

    def test_check_writes_nothing_to_the_home(self) -> None:
        self.run_wrapper("--check")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_a_failed_check_does_not_say_ok(self) -> None:
        r = self.run_wrapper("--check", rc=1)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("OK to run", r.stdout)

    def test_missing_runtime_dependencies_prompt_before_running_installer(
        self,
    ) -> None:
        root = self.hook_repo()
        result = self.run_wrapper(
            "--check",
            script=root / "bin/muninn-install",
            input="n\n",
            extra_env={"IMPORT_RC": "1"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Set up", result.stderr)
        self.assertIn("setup declined", result.stderr)
        self.assertEqual(self.argv(), "")

    def test_incompatible_pydantic_version_prompts_before_installing(
        self,
    ) -> None:
        root = self.hook_repo()
        result = self.run_wrapper(
            "--check",
            script=root / "bin/muninn-install",
            input="n\n",
            extra_env={"IMPORT_RC": "0", "PINNED_VERSION_RC": "1"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("incompatible", result.stderr)
        self.assertIn("setup declined", result.stderr)
        self.assertEqual(self.argv(), "")

    def test_ready_checkout_venv_skips_runtime_setup_prompt(self) -> None:
        root = self.hook_repo()
        self.installed("a" * 40)
        selected = self.lib / "python"
        selected.write_text(
            '#!/bin/sh\nif [ "$4" = "import sys; print(sys.platform)" ]; '
            "then echo linux; exit 0; fi\nexit 1\n"
        )
        selected.chmod(0o755)
        env_python = root / ".venv/bin/python3"
        env_python.parent.mkdir(parents=True)
        shutil.copyfile(self.stub, env_python)
        env_python.chmod(0o755)

        for args in (("--check",), ()):
            with self.subTest(args=args):
                result = self.run_wrapper(
                    *args,
                    script=root / "bin/muninn-install",
                    extra_env={"MUNINN_PYTHON": ""},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("Pinned runtime dependencies", result.stderr)
        self.assertIn("--upgrade --dry-run", self.argv())

    def test_installed_python_alias_uses_venv_target(self) -> None:
        root = self.hook_repo()
        sha = "a" * 40
        self.installed(sha)
        target = self.lib / sha / ".venv/bin/python3"
        target.parent.mkdir(parents=True)
        target.write_text(
            '#!/bin/sh\nif [ "$4" = "import sys; print(sys.platform)" ]; '
            "then echo linux; exit 0; fi\n"
            '[ "$0" = "$EXPECTED_VENV_PY" ] || exit 1\n'
            'if [ "$3" = "-c" ]; then exit 0; fi\n'
            f'echo "$@" >> {shlex.quote(self.log.as_posix())}\n'
        )
        target.chmod(0o755)
        (self.lib / "python").symlink_to(target)

        result = self.run_wrapper(
            "--check",
            script=root / "bin/muninn-install",
            extra_env={"MUNINN_PYTHON": "", "EXPECTED_VENV_PY": str(target)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Pinned runtime dependencies", result.stderr)
        self.assertIn("--upgrade --dry-run", self.argv())

    def test_accepting_git_hook_setup_installs_dev_tools_and_enables_hooks(
        self,
    ) -> None:
        root = self.hook_repo()
        result = self.run_wrapper(
            script=root / "bin/muninn-install", input="y\n"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        hook_path = git(root, "config", "--get", "core.hooksPath")
        self.assertEqual(hook_path.decode().strip(), ".githooks")
        self.assertTrue((root / ".venv" / "bin" / "python3.13").is_file())
        self.assertIn("-m pip install -r", self.argv())
        self.assertIn("requirements-dev.txt", self.argv())

    def test_windows_setup_uses_windows_venv_interpreter(self) -> None:
        root = self.hook_repo()
        result = self.run_wrapper(
            script=root / "bin/muninn-install",
            input="y\ny\n",
            extra_env={
                "IMPORT_RC": "1",
                "PY_PLATFORM": "win32",
                "VENV_DIR": "Scripts",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((root / ".venv" / "Scripts" / "python.exe").is_file())
        self.assertFalse((root / ".venv" / "bin").exists())
        self.assertEqual(
            git(root, "config", "--get", "core.hooksPath").decode().strip(),
            ".githooks",
        )

    def test_status_compares_the_installed_commit_with_head(self) -> None:
        self.installed("a" * 40)
        r = self.run_wrapper("--status")
        self.assertIn(f"installed aaaaaaa, checkout {self.sha[:7]}", r.stdout)
        self.assertIn("upgrade needed", r.stdout)
        self.assertEqual(self.argv(), "")

    def test_status_says_up_to_date_when_they_match(self) -> None:
        self.installed(self.sha)
        self.assertIn("up to date", self.run_wrapper("--status").stdout)

    def test_status_on_an_empty_machine_says_not_installed(self) -> None:
        self.assertIn("not installed", self.run_wrapper("--status").stdout)

    def test_other_arguments_go_to_the_installer_unchanged(self) -> None:
        self.run_wrapper("--repo", "/x", "--sha", "f" * 40, "--fresh")
        self.assertIn(f"--repo /x --sha {'f' * 40} --fresh", self.argv())


if __name__ == "__main__":
    unittest.main()
