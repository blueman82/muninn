"""Synthetic native upgrade migration performs actual transactional DDL."""

from __future__ import annotations

import ast
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from install.context import Ctx, run_real
from tests import lifecycle_native_rollback
from tests.lifecycle_native_rollback import (
    alternate_interpreter,
    migration_source,
)
from tools.standards import check_source


class NativeMigrationTests(unittest.TestCase):
    """Execute the modified current function on a real schema-three store."""

    def test_migration_creates_marker_and_commits_version_together(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        source = migration_source((root / "muninn/store.py").read_text())
        function = next(
            node
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_init_schema"
        )
        code = ast.get_source_segment(source, function)
        assert code is not None
        namespace: dict[str, object] = {
            "sqlite3": sqlite3,
            "Path": Path,
            "SCHEMA_VERSION": 4,
        }
        exec(compile(code, "synthetic_native_migration", "exec"), namespace)
        migrate = namespace["_init_schema"]
        assert callable(migrate)
        with closing(
            sqlite3.connect(":memory:", isolation_level=None)
        ) as conn:
            conn.execute("PRAGMA user_version=3")
            migrate(conn, Path("/unused"))
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 4
            )
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM native_upgrade_marker"
                ).fetchall(),
                [(1,)],
            )
            self.assertFalse(conn.in_transaction)

    def test_unrecognized_source_shape_refuses_without_guessed_insertion(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "fixture_source_changed"):
            migration_source("def unrelated(): pass\n")

    def test_existing_alternate_binary_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "python"
            source.write_bytes(b"trusted synthetic binary")
            target = source.parent / "python-muninn-native-upgrade"
            target.write_bytes(b"unknown prior binary")
            with self.assertRaises(FileExistsError):
                alternate_interpreter(source)
            self.assertEqual(target.read_bytes(), b"unknown prior binary")
            self.assertEqual(source.read_bytes(), b"trusted synthetic binary")

    def test_stale_healthy_pid_does_not_satisfy_replacement_readiness(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = Ctx(Path(temporary), run_real, "readiness", platform="linux")
            with patch.object(
                lifecycle_native_rollback.obs_linux_service,
                "inspect",
                side_effect=[(True, 10), (True, 11), (True, 11)],
            ):
                self.assertEqual(
                    lifecycle_native_rollback.wait_healthy(ctx, 10), 11
                )

    def test_fixture_source_meets_installer_standards(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = migration_source((root / "muninn/store.py").read_text())
        self.assertEqual(check_source("muninn/store.py", source), [])
