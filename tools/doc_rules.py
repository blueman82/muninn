"""Documentation rules S3, S4 and S5: docstrings, Google layout, comments."""

from __future__ import annotations

import ast
import re

from tools.model import Violation, is_test
from tools.shape_rules import FunctionNode

FIXTURE_HOOKS = frozenset(
    {"setUp", "tearDown", "setUpClass", "tearDownClass", "setUpModule"}
)
GOOGLE_SECTIONS = frozenset(
    {
        "Args",
        "Attributes",
        "Example",
        "Examples",
        "Note",
        "Notes",
        "Raises",
        "Returns",
        "Rules",
        "See Also",
        "Todo",
        "Warning",
        "Yields",
    }
)
# A label that only means something inside the conversation that produced it.
CITATION = re.compile(
    r"\bdesign \d|\bspec [A-Z]\d|\b(?:WU|AM)\d+\b"
    r"|\(\s*[AOIDET]\d{1,2}[a-z]?\s*\)|\b[AOID]\d{1,2}[a-z]?[:;)]"
    r"|\bO\d[a-z]?,\s"
)
SECTION_LINE = re.compile(r"^\s{0,4}([A-Z][A-Za-z ]{1,20}):\s*$")
SUMMARY_END = (".", "?", "!")

Documentable = ast.Module | ast.ClassDef | FunctionNode


def check_docstrings(
    rel: str, tree: ast.Module
) -> list[Violation]:
    """Apply S3 and S4 to a module, its classes and its functions.

    Args:
        rel: Repository-relative path.
        tree: Parsed module.

    Returns:
        Violations for missing or malformed docstrings.
    """
    out: list[Violation] = []
    for node, nested in _documentables(tree):
        doc = ast.get_docstring(node, clean=False)
        line = getattr(node, "lineno", 1)
        name = getattr(node, "name", "module")
        if doc is None:
            if _needs_docstring(rel, node, nested):
                out.append(Violation(rel, line, "S3", f"{name}: no docstring"))
            continue
        out += [Violation(rel, line, "S4", f"{name}: {m}") for m in _layout(doc)]
    return out


def _documentables(
    tree: ast.Module,
) -> list[tuple[Documentable, bool]]:
    """List every node that can carry a docstring.

    Args:
        tree: Parsed module.

    Returns:
        ``(node, nested)`` pairs; ``nested`` marks a function defined inside
        another function (a closure).
    """
    found: list[tuple[Documentable, bool]] = [(tree, False)]

    def visit(node: ast.AST, inside_function: bool) -> None:
        """Walk children, tracking whether we are inside a function body."""
        for child in ast.iter_child_nodes(node):
            is_fn = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            if is_fn or isinstance(child, ast.ClassDef):
                found.append((child, is_fn and inside_function))
            visit(child, inside_function or is_fn)

    visit(tree, False)
    return found


def _needs_docstring(rel: str, node: Documentable, nested: bool) -> bool:
    """Decide whether a missing docstring is a violation.

    Args:
        rel: Repository-relative path.
        node: Module, class or function without a docstring.
        nested: True for a closure.

    Returns:
        False for closures, and in tests for ``test_*`` methods and fixture
        hooks; True otherwise.
    """
    if nested:
        return False
    name = getattr(node, "name", "")
    if is_test(rel) and (name.startswith("test") or name in FIXTURE_HOOKS):
        return False
    return not (is_test(rel) and name.startswith("__"))


def _layout(doc: str) -> list[str]:
    """Check one docstring against the Google layout.

    Args:
        doc: Raw docstring text.

    Returns:
        Human-readable problems; empty when the layout is fine.
    """
    lines = doc.split("\n")
    problems: list[str] = []
    summary = lines[0].strip()
    if not summary:
        problems.append("summary must start on the first line")
    elif not summary.endswith(SUMMARY_END):
        problems.append("summary line must end with punctuation")
    if len(lines) > 1 and lines[1].strip():
        problems.append("blank line required after the summary")
    for line in lines[1:]:
        match = SECTION_LINE.match(line)
        if match and match[1] not in GOOGLE_SECTIONS:
            problems.append(f"unknown section {match[1]!r}")
    return problems


def check_comments(rel: str, source: str) -> list[Violation]:
    """Apply S5: no design, spec or work-unit label in text a reader sees.

    Args:
        rel: Repository-relative path.
        source: File text.

    Returns:
        One violation per offending line.
    """
    return [
        Violation(rel, n, "S5", f"cites an internal label: {m[0]!r}")
        for n, line in enumerate(source.splitlines(), start=1)
        if (m := CITATION.search(line))
    ]
