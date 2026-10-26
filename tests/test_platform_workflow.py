"""Platform lanes retain all native checks on both Windows architectures."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github/workflows/platforms.yml"
)
PROOFS = {
    "Observe unqualified Windows boot-counter readiness",
    "Run real ordinary Windows lifecycle",
    "Provision pinned Codex native proof",
    "Run actual Codex and rendered provider routes",
    "Upload early native proof codes",
}
GATES = {"Check Windows native API typing", "Run mandatory native full gate"}
SETUP = {
    "Check out source",
    "Set up Python 3.13",
    "Verify interpreter and SQLite FTS5 readiness",
    "Create private native proof scratch",
    "Install native gate tools",
    "Provision trusted native interpreter tree",
}


def matrix_rows(source: str) -> list[dict[str, str]]:
    """Read the workflow's explicit scalar include rows without YAML deps."""
    matrix = source.split("        include:\n", 1)[1].split("    env:", 1)[0]
    return [
        dict(re.findall(r"(runner|architecture|lane): ([^\n]+)", row))
        for row in matrix.split("          - ")[1:]
    ]


def enabled(condition: str, row: dict[str, str]) -> bool:
    """Evaluate the finite conjunctions used by this workflow's steps."""
    os_name = (
        "Windows"
        if row["runner"].startswith("windows")
        else "macOS" if row["runner"].startswith("macos") else "Linux"
    )
    values = {
        "runner.os": os_name,
        "matrix.lane": row["lane"],
        "steps.provider_setup.outcome": "success",
    }
    for clause in condition.split(" && "):
        if clause in {"", "!cancelled()"}:
            continue
        match = re.fullmatch(r"([a-z_.]+) (==|!=) '([^']+)'", clause)
        if match is None:
            raise ValueError("unknown workflow condition")
        field, operator, expected = match.groups()
        if (values[field] == expected) != (operator == "=="):
            return False
    return True


def selected_steps(source: str, row: dict[str, str]) -> dict[str, str]:
    """Select complete step bodies according to a successful matrix lane."""
    result = {}
    for block in source.split("      - name: ")[1:]:
        name = block.splitlines()[0]
        condition = re.search(r"^        if: (.+)$", block, re.MULTILINE)
        if enabled(condition[1] if condition else "", row):
            result[name] = block
    return result


class PlatformWorkflowTests(unittest.TestCase):
    """Every architecture must pass independent proofs and the entire gate."""

    def test_every_windows_check_runs_exactly_once_on_each_free_runner(
        self,
    ) -> None:
        source = WORKFLOW.read_text()
        rows = matrix_rows(source)
        self.assertEqual(len(rows), 6)
        for runner, architecture in (
            ("windows-2022", "x64"),
            ("windows-11-arm", "arm64"),
        ):
            windows = [row for row in rows if row["runner"] == runner]
            self.assertEqual(
                {row["lane"] for row in windows}, {"proof", "gate"}
            )
            assigned = [selected_steps(source, row) for row in windows]
            for row, steps in zip(windows, assigned, strict=True):
                self.assertEqual(row["architecture"], architecture)
                self.assertEqual(
                    set(steps),
                    SETUP | (PROOFS if row["lane"] == "proof" else GATES),
                )
            for check in PROOFS | GATES:
                self.assertEqual(sum(check in steps for steps in assigned), 1)
            proof = selected_steps(
                source, next(row for row in windows if row["lane"] == "proof")
            )
            self.assertEqual(
                [name for name in proof if name in PROOFS],
                [
                    "Observe unqualified Windows boot-counter readiness",
                    "Run real ordinary Windows lifecycle",
                    "Provision pinned Codex native proof",
                    "Run actual Codex and rendered provider routes",
                    "Upload early native proof codes",
                ],
            )
            gate = selected_steps(
                source, next(row for row in windows if row["lane"] == "gate")
            )
            self.assertIn(
                "'-m', 'tools.check', '--full'",
                gate["Run mandatory native full gate"],
            )
            self.assertIn(
                "'--pythonplatform',", gate["Check Windows native API typing"]
            )
            self.assertIn(
                "'Windows', 'muninn'", gate["Check Windows native API typing"]
            )
        self.assertNotIn("needs:", source)
        self.assertNotIn("continue-on-error:", source)
        self.assertIn("timeout-minutes: 75", source)
        self.assertIn("fail-fast: false", source)
        self.assertIn("contents: read", source)
        self.assertIn("cancel-in-progress: true", source)
        self.assertIn("name: native-proof-codes-${{ matrix.runner }}", source)
        self.assertIn("if-no-files-found: error", source)
        self.assertIn("retention-days: 1", source)

    def test_unix_rows_keep_their_existing_checks(self) -> None:
        source = WORKFLOW.read_text()
        rows = [
            row
            for row in matrix_rows(source)
            if not row["runner"].startswith("windows")
        ]
        self.assertEqual(
            rows,
            [
                {"runner": "macos-15", "architecture": "arm64", "lane": "all"},
                {
                    "runner": "ubuntu-24.04",
                    "architecture": "x64",
                    "lane": "all",
                },
            ],
        )
        for row in rows:
            steps = selected_steps(source, row)
            self.assertIn("Run mandatory full gate", steps)
            self.assertIn(
                ".venv/bin/python3.13 -m tools.check --full",
                steps["Run mandatory full gate"],
            )
            self.assertNotIn("Run mandatory native full gate", steps)
            self.assertEqual(
                "Run real isolated Linux lifecycle" in steps,
                row["runner"].startswith("ubuntu"),
            )
            self.assertEqual(
                "Run actual Codex and rendered provider routes" in steps,
                row["runner"].startswith("ubuntu"),
            )


def step_script(name: str) -> str:
    """Extract one executable workflow step without a YAML dependency."""
    block = WORKFLOW.read_text().split(f"      - name: {name}\n", 1)[1]
    block = block.split("      - name: ", 1)[0]
    return textwrap.dedent(block.split("        run: |\n", 1)[1])


class RuntimeWorkflowTests(unittest.TestCase):
    """Exercise dependency ordering and selected-interpreter gate commands."""

    def test_windows_installs_hashed_runtime_before_developer_tools(
        self,
    ) -> None:
        calls: list[list[str]] = []

        def record(argv: list[str], **kwargs: object) -> None:
            calls.append(argv)

        script = step_script("Install native gate tools").split("url = ", 1)[0]
        with mock.patch.object(subprocess, "run", record):
            exec(script, {})
        self.assertEqual(calls[0], [sys.executable, "-m", "venv", ".venv"])
        self.assertEqual(
            [args[1:] for args in calls[1:]],
            [
                [
                    "-m",
                    "pip",
                    "install",
                    "--require-hashes",
                    "-r",
                    "requirements-runtime.lock",
                ],
                ["-m", "pip", "install", "-r", "requirements-dev.txt"],
            ],
        )
        self.assertTrue(
            all(
                Path(args[0]) == Path(".venv/Scripts/python.exe")
                for args in calls[1:]
            )
        )

    def test_windows_gate_selects_venv_for_itself_and_child_commands(
        self,
    ) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def record(argv: list[str], **kwargs: object) -> None:
            calls.append((argv, kwargs))

        with mock.patch.object(subprocess, "run", record):
            exec(step_script("Run mandatory native full gate"), {})
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        python = Path(".venv/Scripts/python.exe").resolve()
        self.assertEqual(argv, [str(python), "-m", "tools.check", "--full"])
        env = kwargs.get("env")
        assert isinstance(env, dict)
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(python.parent))

    def test_windows_typing_uses_the_dependency_environment(self) -> None:
        calls: list[list[str]] = []

        def record(argv: list[str], **kwargs: object) -> None:
            calls.append(argv)

        with mock.patch.object(subprocess, "run", record):
            exec(step_script("Check Windows native API typing"), {})
        self.assertEqual(len(calls), 1)
        command = calls[0]
        self.assertIn("--pythonpath", command)
        self.assertEqual(
            command[command.index("--pythonpath") + 1], command[0]
        )

    def test_windows_copy_installs_hashed_runtime_before_readiness(
        self,
    ) -> None:
        calls: list[list[str]] = []

        def record(argv: list[str], **kwargs: object) -> None:
            calls.append(argv)

        script = step_script("Provision trusted native interpreter tree")
        script = "copied = " + script.split("copied = ", 1)[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "GITHUB_PATH": str(root / "path"),
                        "GITHUB_ENV": str(root / "env"),
                    },
                ),
                mock.patch.object(subprocess, "run", record),
            ):
                exec(
                    script,
                    {
                        "destination": root,
                        "original": Path(sys.executable),
                        "Path": Path,
                        "subprocess": subprocess,
                        "os": os,
                    },
                )
            copied = str(root / Path(sys.executable).name)
            self.assertEqual(
                calls[0],
                [
                    copied,
                    "-m",
                    "pip",
                    "install",
                    "--require-hashes",
                    "-r",
                    "requirements-runtime.lock",
                ],
            )
            self.assertEqual(
                calls[1][:6], [copied, "-I", "-B", "-X", "utf8", "-c"]
            )
            self.assertEqual((root / "path").read_text().strip(), str(root))


if sys.platform != "win32":

    class UnixRuntimeWorkflowTests(unittest.TestCase):
        """Run the POSIX setup commands against an owned command fixture."""

        def test_unix_installs_hashed_runtime_before_developer_tools(
            self,
        ) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                interpreter = root / "python3.13"
                interpreter.write_text(
                    '#!/bin/sh\nif [ "$2" = venv ]; then\n'
                    "  mkdir -p .venv/bin\n"
                    '  cp "$0" .venv/bin/python\n'
                    '  cp "$0" .venv/bin/python3.13\nfi\n'
                    'printf "%s\\n" "$*" >> calls\n'
                )
                interpreter.chmod(0o700)
                script = step_script("Install full gate tools").split(
                    "if ! command", 1
                )[0]
                subprocess.run(
                    ["/bin/sh", "-eu", "-c", script],
                    cwd=root,
                    env=os.environ
                    | {"PATH": f"{root}{os.pathsep}{os.environ['PATH']}"},
                    check=True,
                    capture_output=True,
                )
                self.assertEqual(
                    (root / "calls").read_text().splitlines(),
                    [
                        "-m venv .venv",
                        "-m pip install --require-hashes "
                        "-r requirements-runtime.lock",
                        "-m pip install -r requirements-dev.txt",
                    ],
                )
