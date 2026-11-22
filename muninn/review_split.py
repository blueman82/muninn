"""Give the line and byte limits of one learning window.

A window holds at most `MAX_LINES` lines and at most `safe_limit` original
bytes of the policy read limit.
"""

from __future__ import annotations

MAX_LINES = 200


def safe_limit(max_input_bytes: int) -> int:
    """Return the charged-byte bound of one window under the policy limit.

    `_verify` charges a window about twice, and each pass may read up to
    8191 bytes more. Tiny limits fall back to a quarter of the limit.

    Args:
        max_input_bytes: The policy read limit.

    Returns:
        The largest byte count a window may charge.
    """
    return max((max_input_bytes - 2 * 8192) // 2, max_input_bytes // 4)
