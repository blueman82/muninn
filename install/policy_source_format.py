"""Validate exact marked sources without opening or changing state."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal

from install import snapshot_io
from install.context import StepFailedError
from muninn import platform_io, store, store_copy, store_fingerprint


def legacy_source(path: Path) -> bool:
    """Recognize validated settled historical stores before lock admission.

    A header identifies only a possible older source. The complete legacy
    validator and stable private file checks must confirm it. Marked sources
    still require their normal existing writer ownership.

    Args:
        path: Existing store in its private data directory.

    Returns:
        Whether the unchanged source has the supported historical format.

    Raises:
        StepFailedError: If a historical source is unsafe or unsupported.
    """
    try:
        with snapshot_io.validated(path) as check:
            expected = path.lstat()
            with platform_io.open_regular(
                path, root=path.parent, expected=expected
            ) as handle:
                platform_io.assert_private_fd(handle.fileno())
                header = handle.read(100)
            check()
            if (
                len(header) != 100
                or header[:16] != b"SQLite format 3\x00"
                or int.from_bytes(header[60:64], "big") != 3
            ):
                return False
            before = store_fingerprint.capture_file(path)
            if (
                before.identity != (expected.st_dev, expected.st_ino)
                or before.size != expected.st_size
                or before.mtime_ns != expected.st_mtime_ns
                or before.ctime_ns != expected.st_ctime_ns
            ):
                raise StepFailedError("cutover_source_mismatch")

            def settled() -> None:
                check()
                if header[18:20] != b"\x01\x01" or any(
                    os.path.lexists(f"{path}{suffix}")
                    for suffix in ("-journal", "-wal", "-shm")
                ):
                    raise StepFailedError("cutover_source_mismatch")
                if store_fingerprint.capture_file(path) != before:
                    raise StepFailedError("cutover_source_mismatch")

            settled()
            with closing(
                sqlite3.connect(
                    path.absolute().as_uri() + "?mode=ro&immutable=1",
                    uri=True,
                )
            ) as conn:
                settled()
                if store_copy.validate_source(conn) != 3:
                    raise StepFailedError("cutover_source_mismatch")
                settled()
            settled()
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise StepFailedError("cutover_source_mismatch") from exc
    return True


def validate_policy_source(conn: sqlite3.Connection) -> Literal[1, 2]:
    """Require inventoried marked DDL, matching marker, FKs and integrity.

    Args:
        conn: Readable source under the caller's existing identity guards.

    Returns:
        The exact marked source version, one or two.

    Raises:
        StepFailedError: If the source is unsupported or damaged.
    """
    try:
        version = store_copy.validate_source(conn)
        if version in (1, 2):
            return 1 if version == 1 else 2
    except (ValueError, sqlite3.Error, store.StoreUnavailableError):
        pass
    raise StepFailedError("review_policy_state_invalid")
