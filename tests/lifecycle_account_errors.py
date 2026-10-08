"""Classify bounded pre-report probe output without exporting its contents."""

from __future__ import annotations

import re

_COMPILE = re.compile(rb"\berror CS(\d{4})\b")
_LIMIT = 16384


def failure_codes(returncode: int, stderr: bytes) -> dict[str, int]:
    """Return fixed numeric compiler/category evidence, preserving unknowns."""
    codes = {
        "outer_returncode": returncode,
        "outer_compile_code": 0,
        "outer_parser_error": 0,
    }
    if len(stderr) > _LIMIT:
        return codes
    matches = set(_COMPILE.findall(stderr))
    if len(matches) == 1:
        codes["outer_compile_code"] = int(matches.pop())
    codes["outer_parser_error"] = int(b"ParserError" in stderr)
    return codes
