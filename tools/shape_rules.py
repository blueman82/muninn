"""Shape rules S1, S2, S6, S7 and S8: size, imports, annotations, defaults."""

from __future__ import annotations

import ast

from tools.model import MAX_FILE_LINES, MAX_FUNCTION_LINES, Violation

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
MUTABLE = (ast.List, ast.Dict, ast.Set)


def functions(tree: ast.AST) -> list[FunctionNode]:
    """Collect every function and method in a module.

    Args:
        tree: Parsed module.

    Returns:
        All function definitions, nested ones included.
    """
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef)
    return [n for n in ast.walk(tree) if isinstance(n, kinds)]


def check_length(rel: str, source: str) -> list[Violation]:
    """Apply S1 to one file.

    Args:
        rel: Repository-relative path.
        source: File text.

    Returns:
        A violation when the file has too many non-blank lines.
    """
    count = sum(1 for line in source.splitlines() if line.strip())
    if count <= MAX_FILE_LINES:
        return []
    msg = f"{count} lines; split the module (limit {MAX_FILE_LINES})"
    return [Violation(rel, 1, "S1", msg)]


def check_function_shape(rel: str, tree: ast.AST) -> list[Violation]:
    """Apply S2, S7 and S8 to every function.

    Args:
        rel: Repository-relative path.
        tree: Parsed module.

    Returns:
        Violations for long, unannotated or mutable-default functions.
    """
    out: list[Violation] = []
    for fn in functions(tree):
        size = (fn.end_lineno or fn.lineno) - fn.lineno + 1
        if size > MAX_FUNCTION_LINES:
            msg = f"{fn.name} is {size} lines (limit {MAX_FUNCTION_LINES})"
            out.append(Violation(rel, fn.lineno, "S2", msg))
        out += _annotation_gaps(rel, fn)
        defaults = [*fn.args.defaults, *fn.args.kw_defaults]
        if any(isinstance(d, MUTABLE) for d in defaults):
            msg = f"{fn.name} has a mutable default argument; use None"
            out.append(Violation(rel, fn.lineno, "S8", msg))
    return out


def _annotation_gaps(rel: str, fn: FunctionNode) -> list[Violation]:
    """Report parameters or a return value without an annotation.

    Args:
        rel: Repository-relative path.
        fn: The function to inspect.

    Returns:
        At most one S7 violation for the function.
    """
    a = fn.args
    params = [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]
    named = [p for p in params if p and p.arg not in ("self", "cls")]
    missing = [p.arg for p in named if p.annotation is None]
    if fn.returns is None:
        missing.append("return")
    if not missing:
        return []
    msg = f"{fn.name} lacks annotations for: {', '.join(missing)}"
    return [Violation(rel, fn.lineno, "S7", msg)]


def check_imports(rel: str, tree: ast.Module) -> list[Violation]:
    """Apply S6 to one module.

    Args:
        rel: Repository-relative path.
        tree: Parsed module.

    Returns:
        Violations for star, lazy, typing-guarded or missing-future imports.
    """
    out: list[Violation] = []
    for fn in functions(tree):
        for node in ast.walk(fn):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                msg = f"import inside {fn.name}; move it to the top"
                out.append(Violation(rel, node.lineno, "S6", msg))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "*" for alias in node.names
        ):
            out.append(Violation(rel, node.lineno, "S6", "star import"))
        if isinstance(node, ast.Name) and node.id == "TYPE_CHECKING":
            out.append(Violation(rel, node.lineno, "S6", "TYPE_CHECKING"))
    if any(not _is_docstring(n) for n in tree.body) and not _has_future(tree):
        msg = "missing `from __future__ import annotations`"
        out.append(Violation(rel, 1, "S6", msg))
    return out


def _is_docstring(node: ast.stmt) -> bool:
    """Say whether a statement is a bare string expression.

    Args:
        node: A module-level statement.

    Returns:
        True for a docstring-shaped expression statement.
    """
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)


def _has_future(tree: ast.Module) -> bool:
    """Say whether the module imports annotations from ``__future__``.

    Args:
        tree: Parsed module.

    Returns:
        True when ``from __future__ import annotations`` is present.
    """
    return any(
        isinstance(n, ast.ImportFrom)
        and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )
