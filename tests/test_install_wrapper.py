"""bin/muninn-install picks the install mode and passes the right flags."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.install_wrapper_support import ROOT, WRAPPER, WrapperFixture, git


class WrapperTest(WrapperFixture):
    """Verify automatic mode selection and explicitly accepted local setup."""

    def test_windows_facade_runs_the_same_script_with_required_sh(
        self,
    ) -> None:
        shell = shutil.which("sh")
        self.assertIsNotNone(shell)
        with (
            mock.patch(
                "tests.install_wrapper_support.sys",
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
                "tests.install_wrapper_support.sys",
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
            '#!/bin/sh\nwhile [ "${1:-}" = -E ] || [ "${1:-}" = -s ] || '
            '[ "${1:-}" = -B ]; do shift; done\n'
            'if [ "$2" = "import sys; print(sys.platform)" ]; '
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
            '#!/bin/sh\nwhile [ "${1:-}" = -E ] || [ "${1:-}" = -s ] || '
            '[ "${1:-}" = -B ]; do shift; done\n'
            'if [ "$2" = "import sys; print(sys.platform)" ]; '
            "then echo linux; exit 0; fi\n"
            '[ "$0" = "$EXPECTED_VENV_PY" ] || { '
            'printf "alias target mismatch: actual=%s expected=%s\\n" '
            f'"$0" "$EXPECTED_VENV_PY" >> {shlex.quote(self.log.as_posix())}; '
            "exit 1; }\n"
            'if [ "$1" = "-c" ]; then exit 0; fi\n'
            f'echo "$@" >> {shlex.quote(self.log.as_posix())}\n'
        )
        target.chmod(0o755)
        (self.lib / "python").symlink_to(target)

        result = self.run_wrapper(
            "--check",
            script=root / "bin/muninn-install",
            extra_env={
                "MUNINN_PYTHON": "",
                "EXPECTED_VENV_PY": target.as_posix(),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr + self.argv())
        self.assertNotIn("Pinned runtime dependencies", result.stderr)
        self.assertIn("--upgrade --dry-run", self.argv())

        self.log.unlink()
        refused = self.run_wrapper(
            "--check",
            script=root / "bin/muninn-install",
            extra_env={
                "MUNINN_PYTHON": "",
                "EXPECTED_VENV_PY": "wrong-target",
            },
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("alias target mismatch: actual=", self.argv())
        self.assertIn("expected=wrong-target", self.argv())

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
        self.assertRegex(
            self.argv(),
            r"(?m)^-m pip install --require-hashes -r .+[/\\]"
            r"requirements-dev\.txt$",
        )

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
        self.assertIn("versions differ; ancestry unknown", r.stdout)
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
