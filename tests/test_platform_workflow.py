"""Platform lanes retain all native checks on both Windows architectures."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github/workflows/platforms.yml"
)
PROOFS = {
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
        self.assertIn("fail-fast: true", source)
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
                "run: python3.13 -m tools.check --full",
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
