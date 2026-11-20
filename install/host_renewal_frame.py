"""Frame store rows for the host renewal and Codex interface hashes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def metadata_frame(value: object) -> bytes:
    """Length-frame scalar types and values with the existing copy encoding."""
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
    return len(raw).to_bytes(8, "big") + raw


def notice_meaning(row: Mapping[str, object]) -> dict[str, object]:
    """Keep the columns of one setup notice row without its time.

    Args:
        row: The columns of the `setup` row in table order.

    Returns:
        The same columns without `created_at`.
    """
    return {key: value for key, value in row.items() if key != "created_at"}


def notice_digest(row: Mapping[str, object]) -> str:
    """Digest the meaning of one setup notice row without its time.

    Args:
        row: The columns of the `setup` row in table order.

    Returns:
        The SHA-256 hex digest of the framed row without `created_at`.
    """
    return hashlib.sha256(metadata_frame(notice_meaning(row))).hexdigest()


def frame_row(row: dict[str, object]) -> bytes:
    """Frame exact columns, scalar types and values for one complete row."""
    return metadata_frame(
        [
            (
                key,
                type(value).__name__,
                value.hex() if isinstance(value, bytes) else value,
            )
            for key, value in row.items()
        ]
    )
