"""Shared limits, scopes and the violation record for the standards checks.

Every limit lives in this module and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MAX_FILE_LINES = 400
MAX_FUNCTION_LINES = 100
SCOPES = ("muninn", "install", "tools", "tests")


@dataclass(frozen=True)
class Violation:
    """One rule broken at one place.

    Attributes:
        path: Repository-relative file path.
        line: 1-based line number.
        rule: Rule id, ``S1`` to ``S8``.
        message: What is wrong and what to do.
    """

    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        """Render as ``path:line: rule message`` for editors and logs."""
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


def python_files(root: Path) -> list[Path]:
    """List every Python file under the checked scopes.

    Args:
        root: Repository root.

    Returns:
        Sorted paths of ``*.py`` files, skipping bytecode caches.
    """
    found = (p for s in SCOPES for p in (root / s).rglob("*.py"))
    return sorted(p for p in found if "__pycache__" not in p.parts)


def is_test(rel: str) -> bool:
    """Say whether a repository-relative path is test code.

    Args:
        rel: Path relative to the repository root, with ``/`` separators.

    Returns:
        True for files under ``tests/``.
    """
    return rel.startswith("tests/")
