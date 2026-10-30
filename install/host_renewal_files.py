"""Check fixed erasure and supervisor files without reading lock bytes."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from install.context import StepFailedError
from muninn import platform_io, review_pause_state, store_fingerprint
from muninn.tombstone_intent import IntentRow, read_journal
from muninn.tombstone_key import (
    KEY_FILE,
    KEYED_PREFIX,
    TOMBSTONE_FILE,
    TombstoneKeyError,
    load_key,
)


@dataclass(frozen=True)
class Files:
    """Retain only fixed private identities, digests and the typed witness."""

    key: store_fingerprint.FileFingerprint | None
    journal: store_fingerprint.FileFingerprint | None
    witness: review_pause_state.Witness
    witness_file: review_pause_state.Fingerprint
    lock_identity: tuple[int, int]


def _optional(
    path: Path, check: Callable[[], None]
) -> store_fingerprint.FileFingerprint | None:
    """Bind private file absence or complete bytes with deadline checks."""
    check()
    result = (
        store_fingerprint.capture_file(path) if os.path.lexists(path) else None
    )
    check()
    return result


def lock_identity(home: Path, check: Callable[[], None]) -> tuple[int, int]:
    """Check descriptor identity without reading locked bytes."""
    check()
    path = home / review_pause_state.LOCK
    with platform_io.open_regular(
        path, root=home, expected=path.lstat()
    ) as handle:
        identity = review_pause_state.lock_identity(home, handle.fileno())
    check()
    return identity


def requires_key(row: IntentRow) -> bool:
    """Recognize a canonical keyed intent without retaining secret bytes."""
    return bool(row.line_sha256 and row.line_sha256.startswith(KEYED_PREFIX))


def _check_key(home: Path, check: Callable[[], None]) -> None:
    """Validate a required existing key without creating or retaining bytes."""
    check()
    try:
        load_key(home, create=False)
    except TombstoneKeyError as exc:
        check()
        raise StepFailedError("host_renewal_erasure_invalid") from exc
    check()


def capture_files(
    home: Path, check: Callable[[], None], *, require_key: bool = False
) -> Files:
    """Check complete fixed erasure files and exact witness/lock identities.

    Args:
        home: Existing private data directory owned by the caller's barrier.
        check: Caller check sharing its original absolute metadata deadline.
        require_key: Whether streamed table intent contains keyed tombstones.

    Returns:
        Finite frozen file observations, without key or journal bodies.

    Raises:
        StepFailedError: If fixed observations drift or the witness is invalid.
    """
    key = _optional(home / KEY_FILE, check)
    journal = _optional(home / TOMBSTONE_FILE, check)
    if key is not None or require_key:
        _check_key(home, check)
    with closing(read_journal(home)) as rows:
        check()
        for row in rows:
            check()
            if requires_key(row) and key is None:
                _check_key(home, check)
            check()
        check()
    raw, witness_file = review_pause_state.inspect(
        home, review_pause_state.WITNESS
    )
    check()
    witness = review_pause_state.parse(raw, review_pause_state.Witness)
    identity = lock_identity(home, check)
    if (
        witness is None
        or witness_file is None
        or witness.lock_identity != identity
    ):
        raise StepFailedError("host_renewal_state_changed")
    if (
        _optional(home / KEY_FILE, check) != key
        or _optional(home / TOMBSTONE_FILE, check) != journal
        or review_pause_state.inspect(home, review_pause_state.WITNESS)
        != (raw, witness_file)
        or lock_identity(home, check) != identity
    ):
        raise StepFailedError("host_renewal_state_changed")
    check()
    return Files(key, journal, witness, witness_file, identity)
