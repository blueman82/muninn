"""Bind wrapper process identity directly to its Python interpreter."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from muninn import review_launch_spec as launch
from tests import store_support
from tests.test_review_rounds import context


class WrapperPythonTests(unittest.TestCase):
    """Preserve the selected venv without introducing an intermediate PID."""

    def test_windows_venv_launches_base_image_with_selected_venv(self) -> None:
        selected, base = "C:/venv/Scripts/python.exe", "C:/Python/python.exe"
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.object(sys, "executable", selected),
            mock.patch.object(sys, "_base_executable", base),
        ):
            spec = launch.launch_spec(context(), Path.cwd(), {}, b"prompt")
        self.assertEqual(spec.wrapper_argv[0], base)
        self.assertEqual(spec.wrapper_env, {"__PYVENV_LAUNCHER__": selected})
        self.assertEqual(
            spec.wrapper_argv[1:5], ("-E", "-s", "-m", "muninn.review_worker")
        )

    def test_direct_windows_and_posix_launches_keep_empty_environment(
        self,
    ) -> None:
        for platform, selected, base in (
            ("win32", "C:/Python/python.exe", "C:/Python/python.exe"),
            ("linux", "/venv/bin/python", "/usr/bin/python"),
        ):
            with (
                self.subTest(platform=platform),
                mock.patch.object(sys, "platform", platform),
                mock.patch.object(sys, "executable", selected),
                mock.patch.object(sys, "_base_executable", base),
            ):
                spec = launch.launch_spec(context(), Path.cwd(), {}, b"prompt")
            self.assertEqual(spec.wrapper_argv[0], selected)
            self.assertEqual(spec.wrapper_env, {})

    @unittest.skipUnless(
        sys.platform == "win32", "requires Windows redirector"
    )
    def test_native_windows_pid_and_venv_dependencies_are_preserved(
        self,
    ) -> None:
        spec = launch.launch_spec(context(), Path.cwd(), {}, b"prompt")
        script = (
            "import json,os,pydantic,sys; "
            "print(json.dumps([os.getpid(),sys.prefix,pydantic.__file__]))"
        )
        with subprocess.Popen(
            [*spec.wrapper_argv[:3], "-c", script],
            env=spec.wrapper_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as child:
            output, errors = child.communicate(timeout=15)
        self.assertEqual(child.returncode, 0, errors)
        pid, prefix, dependency = json.loads(output)
        self.assertEqual(pid, child.pid)
        self.assertEqual(Path(prefix), Path(sys.prefix))
        self.assertTrue(Path(dependency).is_relative_to(Path(sys.prefix)))

    def test_windows_unavailable_base_refuses_before_launch(self) -> None:
        for base in (None, False, ""):
            with (
                self.subTest(base=base),
                mock.patch.object(sys, "platform", "win32"),
                mock.patch.object(sys, "_base_executable", base),
                self.assertRaisesRegex(
                    OSError, "wrapper_interpreter_unavailable"
                ),
            ):
                launch.launch_spec(context(), Path.cwd(), {}, b"prompt")

    def test_fixture_child_binds_direct_windows_python(self) -> None:
        selected, base = "C:/venv/Scripts/python.exe", "C:/Python/python.exe"
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.object(sys, "executable", selected),
            mock.patch.object(sys, "_base_executable", base),
            mock.patch("tests.store_support.subprocess.Popen") as start,
        ):
            store_support.Child(self, "pass")
        self.assertEqual(start.call_args.args[0][0], base)
        self.assertEqual(
            start.call_args.kwargs["env"]["__PYVENV_LAUNCHER__"], selected
        )
