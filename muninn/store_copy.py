"""Copy inventoried durable rows into an unpublished marked store."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import time
from collections.abc import Sequence

from muninn import knowledge_read, store
from muninn.knowledge_models import Scope
from muninn.knowledge_write import claims

BASE_TABLES = (
    "scope",
    "scope_path",
    "source",
    "event",
    "usage",
    "source_issue",
    "tombstone",
)
GOVERNED_TABLES = (
    "store_meta",
    "review_control",
    "review_source",
    "review_request",
    "review_attempt",
    "review_call",
    "review_evidence",
    "review_input",
    "knowledge",
    "knowledge_version",
    "knowledge_claim",
    "knowledge_support",
    "unresolved",
    "review_receipt",
)
# The marked current format keeps an exact DDL identity.
_SHAPES = {
    1: "535cd228a984e4d5cd6f98787a33c657359bf780753c73ec33885a2ed62ddc3c",
}
_LEGACY_TABLES = {
    "citation",
    "event",
    "knowledge",
    "knowledge_log",
    "scope",
    "scope_path",
    "source",
    "source_issue",
    "tombstone",
    "usage",
}
_LEGACY_VIRTUAL = {"event_fts", "knowledge_fts"}


def _shape(conn: sqlite3.Connection) -> int:
    """Require a supported legacy table set or marked current schema."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 3:
        tables = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA main.table_list")
            if row[0] == "main" and not row[1].startswith("sqlite_")
        }
        ordinary = {name for name, kind in tables.items() if kind == "table"}
        virtual = {name for name, kind in tables.items() if kind == "virtual"}
        shadows = {name for name, kind in tables.items() if kind == "shadow"}
        if (
            ordinary != _LEGACY_TABLES
            or virtual != _LEGACY_VIRTUAL
            or any(
                kind not in {"table", "virtual", "shadow"}
                for kind in tables.values()
            )
            or any(
                not name.startswith(("event_fts_", "knowledge_fts_"))
                for name in shadows
            )
        ):
            raise ValueError("cutover_source_mismatch")
        return version
    rows = [
        tuple(row)
        for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE sql IS NOT NULL ORDER BY type,name"
        )
    ]
    fingerprint = hashlib.sha256(
        json.dumps(rows, separators=(",", ":")).encode()
    ).hexdigest()
    if _SHAPES.get(version) != fingerprint:
        raise ValueError("cutover_source_mismatch")
    if version == 1:
        store.assert_store_format(conn)
    return version


def _columns(conn: sqlite3.Connection, table: str) -> str:
    """Quote the columns of an already inventoried program-selected table."""
    return ",".join(
        f'"{row[1]}"' for row in conn.execute(f'PRAGMA table_info("{table}")')
    )


def _digest(conn: sqlite3.Connection, table: str) -> tuple[int, str]:
    """Hash framed scalar types and values in stable column order."""
    columns = _columns(conn, table)
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY {columns}'):
        values = [
            (
                type(value).__name__,
                value.hex() if isinstance(value, bytes) else value,
            )
            for value in row
        ]
        encoded = json.dumps(
            values, separators=(",", ":"), ensure_ascii=True
        ).encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return count, digest.hexdigest()


def _empty(conn: sqlite3.Connection) -> None:
    """Accept only untouched current bootstrap rows and no governed content."""
    if _shape(conn) != 1:
        raise ValueError("cutover_target_mismatch")
    for table in BASE_TABLES + GOVERNED_TABLES:
        if table in ("store_meta", "review_control"):
            continue
        if conn.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
            raise ValueError("cutover_target_not_empty")
    control = conn.execute("SELECT * FROM review_control").fetchall()
    if [tuple(row) for row in control] != [(1, 0, 0, None, None, None)]:
        raise ValueError("cutover_target_not_empty")
    if conn.execute("SELECT 1 FROM knowledge_fts LIMIT 1").fetchone():
        raise ValueError("cutover_target_not_empty")


def _copy(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    tables: Sequence[str],
) -> dict[str, int]:
    """Copy each complete row and check independently read target checksums."""
    result: dict[str, int] = {}
    for table in tables:
        columns = _columns(source, table)
        if columns != _columns(target, table):
            raise ValueError("cutover_source_mismatch")
        if table in ("store_meta", "review_control"):
            target.execute(f'DELETE FROM "{table}"')
        marks = ",".join("?" for _ in columns.split(","))
        target.executemany(
            f'INSERT INTO "{table}" VALUES({marks})',
            source.execute(f'SELECT * FROM "{table}"'),
        )
        before = _digest(source, table)
        if before != _digest(target, table):
            raise ValueError("cutover_checksum_mismatch")
        result[table] = before[0]
    return result


def rebuild_indexes(conn: sqlite3.Connection) -> None:
    """Derive FTS only from current eligible normalized content.

    Args:
        conn: Caller-owned marked connection inside the copy transaction.
    """
    conn.execute("INSERT INTO event_fts(event_fts) VALUES('rebuild')")
    conn.execute("DELETE FROM knowledge_fts")
    factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        scopes = conn.execute(
            "SELECT DISTINCT v.scope_json FROM knowledge k "
            "JOIN knowledge_version v ON v.knowledge_id=k.id "
            "AND v.revision=k.current_revision WHERE k.state='active'"
        )
        now = time.time()
        indexed: set[str] = set()
        for row in scopes:
            scope = Scope.model_validate_json(row[0])
            for view in knowledge_read.current_knowledge(conn, scope, now):
                if view.body is not None and view.id not in indexed:
                    indexed.add(view.id)
                    text = "\n".join(claim[1] for claim in claims(view.body))
                    conn.execute(
                        "INSERT INTO knowledge_fts VALUES(?,?,?)",
                        (view.id, view.revision, text),
                    )
    finally:
        conn.row_factory = factory


def _validate(conn: sqlite3.Connection) -> None:
    """Require actual FK and physical integrity checks on the copied store."""
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("cutover_foreign_keys")
    if [tuple(row) for row in conn.execute("PRAGMA quick_check")] != [("ok",)]:
        raise ValueError("cutover_integrity")


def validate_source(conn: sqlite3.Connection) -> int:
    """Read-check supported legacy or marked sources without acquiring state.

    The caller owns the connection, snapshot and maintenance barriers. This
    operation opens, initializes, writes and commits nothing. Validation
    applies only to the caller's current snapshot; copying validates again.

    Args:
        conn: Caller-owned readable connection to a supported source.

    Returns:
        Three for the legacy table contract or one for marked current format.

    Raises:
        ValueError: If source shape, foreign keys or integrity are invalid.
        store.StoreUnavailableError: If the current format marker is absent.
        sqlite3.Error: If SQLite cannot inspect the source.
    """
    version = _shape(conn)
    _validate(conn)
    return version


def copy_governed_state(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
) -> dict[str, int]:
    """Copy supported durable state atomically without acquiring resources.

    The caller owns both connections and maintenance barriers. The target
    must be an unpublished, newly initialized marked store. Source and
    target must have no active transaction. The copy owns only its read
    snapshot and target transaction, never their connections or pathnames.
    Replay current erasure intent in autocommit after this call, then
    validate and synchronize before publication. Legacy knowledge is kept
    solely in the caller's retained source archive.

    Args:
        source: Connection to the exact legacy or marked current source.
        target: Empty marked-schema connection, never an exposed store.

    Returns:
        Verified row counts for every copied durable table.

    Raises:
        ValueError: If transaction, format, emptiness or validation fails.
        sqlite3.Error: If SQLite cannot copy or commit the complete state.
    """
    if source is target or source.in_transaction or target.in_transaction:
        raise ValueError("cutover_transaction")
    source.execute("BEGIN")
    try:
        version = validate_source(source)
        _empty(target)
        target.execute("BEGIN IMMEDIATE")
        try:
            target.execute("PRAGMA defer_foreign_keys=ON")
            tables = BASE_TABLES + (GOVERNED_TABLES if version == 1 else ())
            result = _copy(source, target, tables)
            rebuild_indexes(target)
            _validate(target)
            target.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                target.execute("ROLLBACK")
            raise
    except BaseException:
        with contextlib.suppress(BaseException):
            source.execute("ROLLBACK")
        raise
    else:
        source.execute("ROLLBACK")
        return result
