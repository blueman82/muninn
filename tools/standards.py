"""Run every mechanical standards rule over the repository.

Stdlib only, so the checks run on any machine that can run the tests.
``tests/test_standards.py`` and ``tools/check.py`` both call ``check_repo``.

Rules:
    S1: a file has at most ``MAX_FILE_LINES`` non-blank lines.
    S2: a function has at most ``MAX_FUNCTION_LINES`` lines.
    S3: modules, classes and functions carry docstrings (tests are exempt
        for ``test_*`` methods and fixture hooks).
    S4: docstrings use the Google layout: a one-line summary ending in
        punctuation, a blank line, then only Google section names.
    S5: no comment or docstring cites a design, spec or work-unit label.
    S6: imports are explicit, top-level and runtime; no ``TYPE_CHECKING``.
    S7: every parameter and return value is annotated.
    S8: no mutable default arguments.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tools.doc_rules import check_comments, check_docstrings
from tools.model import Violation, python_files
from tools.shape_rules import (
    check_function_shape,
    check_imports,
    check_length,
)


def check_source(rel: str, source: str) -> list[Violation]:
    """Run every rule over one file's text.

    Args:
        rel: Repository-relative path with ``/`` separators.
        source: File text.

    Returns:
        All violations, ordered by line.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Violation(rel, exc.lineno or 1, "S0", "does not parse")]
    found = [
        *check_length(rel, source),
        *check_function_shape(rel, tree),
        *check_imports(rel, tree),
        *check_docstrings(rel, tree),
        *check_comments(rel, source),
    ]
    return sorted(found, key=lambda v: (v.line, v.rule))


def check_repo(root: Path) -> list[Violation]:
    """Run every rule over every Python file in the checked scopes.

    Args:
        root: Repository root.

    Returns:
        All violations, ordered by file then line.
    """
    found: list[Violation] = []
    for path in python_files(root):
        rel = path.relative_to(root).as_posix()
        found += check_source(rel, path.read_text(encoding="utf-8"))
    return found
