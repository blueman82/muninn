# Engineering standards

These rules are enforced by code, not by goodwill. `python3.13 -m tools.check --full` must pass before any commit, any
upgrade and any claim that work is done. If a rule seems wrong, change it in `tools/` or `pyproject.toml` with an ADR; do
not work around it. There is no `noqa`, no `# type: ignore` and no exemption list.

## The gate

| Gate | Command | What it checks |
|---|---|---|
| Stdlib rules S1 to S8 | `python3.13 -m tools.check` (also run by `tests/test_standards.py`) | size, docstrings, comments, imports, annotations |
| Everything | `python3.13 -m tools.check --full` | the above plus ruff, black, pyright strict, shellcheck |
| Set up the tools | `python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt` | pinned versions in `requirements-dev.txt` |

A missing tool fails the gate; it never skips.

## Limits

| Rule | Limit | Applies to |
|---|---|---|
| S1 file length | 400 non-blank lines | every `.py` file in `pctx/`, `install/`, `tools/`, `tests/` |
| S2 function length | 100 lines (aim for 40) | every function and method |
| Ruff complexity | McCabe 10, 7 arguments, 12 branches, 6 returns, 50 statements | `pctx/`, `install/`, `tools/`, `tests/` |

Split by responsibility, not by line count: a new module has one reason to change and a name that says it. Do not shuffle
code into `utils.py`.

## PEPs followed

| PEP | Meaning here | Enforced by |
|---|---|---|
| 8 | style, 79 columns, naming (exceptions end in `Error`) | ruff `E`, `W`, `N`; black |
| 257 | docstring conventions | ruff `D` |
| 484, 526 | full type hints on every parameter and return | S7; pyright strict (tests: standard) |
| 563 | `from __future__ import annotations` in every module | S6; ruff `FA` |
| 585, 604, 695 | `list[int]`, `str \| None`, modern generics | ruff `UP` |

Pathlib over `os.path` (ruff `PTH`), no commented-out code (`ERA`), comprehensions and builtins over manual loops
(`C4`, `SIM`, `PERF`, `PIE`, `RET`).

## Python rules

- Imports are explicit and at the top of the file: no star imports, no imports inside functions, no `TYPE_CHECKING`
  (fix the architecture instead of a cycle). (S6)
- No mutable default arguments. (S8)
- EAFP over look-before-you-leap; context managers for every resource; f-strings only; early returns over nesting.
- Untrusted text is sanitised where it is inserted into output.
- Standard library only in `pctx/` and `install/`.

## Docstrings: Google style, everywhere

Every module, class and function in `pctx/`, `install/` and `tools/` has one. In `tests/`, modules, classes and helper
functions do; `test_*` methods and fixture hooks (`setUp`, `tearDown`) do not, because their names are their
documentation. A one-line docstring is enough for a trivial private helper; anything longer uses the sections below, and
the sections must match the code (ruff `D417`, `DOC201`, `DOC402`, `DOC501`).

```python
def fetch(root: Path, name: str, *, limit: int = 10) -> list[Row]:
    """Return up to ``limit`` rows for ``name`` under ``root``.

    The summary is one line, imperative, ending in a period. Add a paragraph
    only for what the signature cannot say.

    Args:
        root: Directory that holds the store.
        name: Provider name, such as ``claude``.
        limit: Upper bound on rows returned.

    Returns:
        Rows newest first; empty when nothing matches.

    Raises:
        StoreUnavailableError: If the store cannot be opened read-only.
    """
```

Types live in the annotations, not in the docstring. Allowed section names: Args, Returns, Yields, Raises, Attributes,
Example(s), Note(s), Warning, See Also, Todo. (S4)

## Comments: write them as a senior Python developer

A comment says why, never what. Add one wherever a reader would otherwise have to rediscover a constraint:

- an invariant the code relies on, or an ordering that must not change (locks, transactions, journal modes);
- a non-obvious choice and the alternative that was rejected, with the reason;
- a trap or edge case (an off-by-one, a platform quirk, a SQLite pragma, a regex that must stay linear);
- a security or privacy reason (why text is redacted, why a file is 0600);
- a deliberate limit, with the point at which it should be revisited.

Never comment the obvious, never restate the next line, and never cite where the comment came from. A comment must make
sense to someone with no access to any conversation, review or planning document: no design, spec, work-unit, finding or
ticket labels (S5). A public name, a file path or a PR number is fine because anyone can follow it.

## Bash

`bin/pctx` is POSIX `sh`: `set -eu`, every expansion quoted, `shellcheck` clean, no bashisms.

## When these rules conflict with another instruction

A skill or guideline that says "public docstrings only" or "no comments unless needed" does not override this document:
the owner asked for Google docstrings across the codebase and for why-comments. Follow this file.
