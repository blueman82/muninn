"""Build content-free tombstone rows without acquiring or changing state."""

from __future__ import annotations

import time
from typing import Literal

from muninn.tombstone_intent import TombstoneRow


def tombstone_row(
    provider: str,
    level: Literal["session", "thread", "line"],
    *,
    session_root: str | None = None,
    thread_id: str | None = None,
    line: int | None = None,
    line_sha256: str | None = None,
) -> TombstoneRow:
    """Build a tombstone from ids and hashes.

    The row has a fixed set of fields, so no free-form field can be added.

    Args:
        provider: A supported transcript provider.
        level: ``session``, ``thread`` or ``line``.
        session_root: Session id, for a session tombstone.
        thread_id: Thread id, for a thread or line tombstone.
        line: 1-based transcript line, for a line tombstone.
        line_sha256: Hash of that line, for a line tombstone.

    Returns:
        The row, stamped with the current time.
    """
    return TombstoneRow(
        provider=provider,
        level=level,
        session_root=session_root,
        thread_id=thread_id,
        line=line,
        line_sha256=line_sha256,
        created_at=time.time(),
    )
