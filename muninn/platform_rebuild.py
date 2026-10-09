"""Windows store publication with a durable private recovery copy."""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from muninn import platform_io, platform_windows, store
from muninn.file_sync import sync_fd

if sys.platform == "win32":

    def _copy(source: Path, target: Path) -> None:
        """Publish a synced private copy without replacing a sibling."""
        platform_windows.assert_private(source)
        fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".recovery-")
        try:
            with os.fdopen(fd, "wb") as output:
                platform_io.assert_private_fd(output.fileno())
                with platform_io.open_regular(
                    source, expected=source.stat()
                ) as input_file:
                    shutil.copyfileobj(input_file, output)
                output.flush()
                sync_fd(output.fileno())
            platform_windows.publish(Path(temp), target, replace=False)
        finally:
            Path(temp).unlink(missing_ok=True)

    def publish_store(
        home: Path, db: Path, new: Path, *, readable: bool
    ) -> tuple[bool, str | None, bool]:
        """Retain recoverable old data before any native replacement attempt.

        Args:
            home: Private data directory, under the writer lock.
            db: Existing database pathname.
            new: Complete synced replacement database.
            readable: Whether the old store's ledger was copied.

        Returns:
            Publication success, retained recovery filename, and whether a
            failed publication was followed by a confirmed restoration.
        """
        prior = None
        try:
            if db.exists():
                prefix = (
                    store.RECOVERY_PREFIX
                    if readable
                    else store.UNREADABLE_PREFIX
                )
                prior = home / f"{prefix}{uuid.uuid4().hex}"
                _copy(db, prior)
            platform_windows.publish(new, db, replace=True)
        except OSError:
            restored = False
            if prior is not None and prior.exists():
                restore = home / f".restore-{uuid.uuid4().hex}"
                try:
                    _copy(prior, restore)
                    platform_windows.publish(restore, db, replace=True)
                    restored = True
                except OSError:
                    pass  # the durable prior copy remains available
                finally:
                    with contextlib.suppress(OSError):
                        restore.unlink(missing_ok=True)
            kept = prior.name if prior is not None and prior.exists() else None
            return False, kept, restored
        if prior is not None and readable:
            try:
                prior.unlink()
            except OSError:
                return True, prior.name, False
            prior = None
        return True, prior.name if prior is not None else None, False
