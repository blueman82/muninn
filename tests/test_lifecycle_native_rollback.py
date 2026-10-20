"""Synthetic native current-store writes retain real transaction boundaries."""

from __future__ import annotations

import ast
import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import cast
from unittest.mock import patch

from install.context import Ctx, run_real
from muninn import store
from tests import lifecycle_native_rollback
from tests.lifecycle_native_rollback import (
    alternate_interpreter,
    mutation_source,
)
from tools.standards import check_source


def fixture_initializer() -> Callable[[sqlite3.Connection], None]:
    """Compile the checked fixture initializer against current contracts."""
    root = Path(__file__).resolve().parents[1]
    source = mutation_source((root / "muninn/store.py").read_text())
    function = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef)
        and node.name == "initialize_store"
    )
    code = ast.get_source_segment(source, function)
    assert code is not None
    namespace: dict[str, object] = {
        "sqlite3": sqlite3,
        "SCHEMA_VERSION": store.SCHEMA_VERSION,
        "SCHEMA_SQL": store.SCHEMA_SQL,
        "assert_store_format": store.assert_store_format,
    }
    exec(compile(code, "synthetic_native_write", "exec"), namespace)
    return cast(
        Callable[[sqlite3.Connection], None], namespace["initialize_store"]
    )


class NativeMutationTests(unittest.TestCase):
    """Execute the modified initializer on a real marked current store."""

    def test_write_commits_marker_without_changing_current_format(
        self,
    ) -> None:
        initialize = fixture_initializer()
        with closing(
            sqlite3.connect(":memory:", isolation_level=None)
        ) as conn:
            store.initialize_store(conn)
            initialize(conn)
            initialize(conn)
            store.assert_store_format(conn)
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM native_upgrade_marker"
                ).fetchall(),
                [(1,)],
            )
            self.assertFalse(conn.in_transaction)

    def test_failed_write_rolls_back_marker_and_preserves_current_store(
        self,
    ) -> None:
        initialize = fixture_initializer()

        def refuse(
            operation: int,
            table: str | None,
            column: str | None,
            database: str | None,
            trigger: str | None,
        ) -> int:
            return (
                sqlite3.SQLITE_DENY
                if operation == sqlite3.SQLITE_INSERT
                and table == "native_upgrade_marker"
                else sqlite3.SQLITE_OK
            )

        with closing(
            sqlite3.connect(":memory:", isolation_level=None)
        ) as conn:
            store.initialize_store(conn)
            before = "\n".join(conn.iterdump())
            conn.set_authorizer(refuse)
            with self.assertRaisesRegex(
                sqlite3.DatabaseError, "not authorized"
            ):
                initialize(conn)
            conn.set_authorizer(None)
            self.assertFalse(conn.in_transaction)
            self.assertEqual("\n".join(conn.iterdump()), before)
            store.assert_store_format(conn)
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_schema "
                    "WHERE name='native_upgrade_marker'"
                ).fetchone()
            )

    def test_unrecognized_source_shape_refuses_without_guessed_insertion(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "fixture_source_changed"):
            mutation_source("def unrelated(): pass\n")

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
        source = mutation_source((root / "muninn/store.py").read_text())
        self.assertEqual(check_source("muninn/store.py", source), [])
