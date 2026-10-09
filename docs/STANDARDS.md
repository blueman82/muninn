# Engineering standards

Automated checks enforce these rules. `python3.13 -m tools.check --full` must pass
before any commit, any upgrade and any claim that work is done. If a rule seems wrong,
change it in `tools/` or `pyproject.toml` with an ADR; do not work around it. There is
no `noqa`, no `# type: ignore` and no exemption list. The operator must run the full
gate before an upgrade; the installer itself enforces only the stdlib rules S1 to S8.

## The gate

| Gate | Command | What it checks |
|---|---|---|
| Stdlib rules S1 to S8 | `python3.13 -m tools.check` (also run by `tests/test_standards.py`) | size, docstrings, comments, imports, annotations |
| Everything | `python3.13 -m tools.check --full` | the above plus ruff, black, pyright strict, shellcheck, the git-hook switch and the whole test suite (run in parallel) |
| Tests, fast | `python3.13 -m tools.run_tests` (`-j N` for N processes) | the whole suite with each test module in its own process, about 5x faster than `unittest discover` (roughly 9 s against 42 s on 15 cores) |
| Set up the tools | `python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt` | pinned versions in `requirements-dev.txt` |

A missing tool, failing test or disabled Git-hook configuration fails the gate. No
required check is skipped.

One-time setup per clone (the first two are checked by the gate and the tests):

    git config core.hooksPath .githooks                  # pre-commit and pre-merge-commit run the gate
    python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
    cp tools/claude-settings.json .claude/settings.json  # Claude Code: gate after every edit, no stopping on a failure

If Codex cannot write Git metadata, the owner or Claude session configures the Git
hooks. Codex project hooks are tracked in `.codex/hooks.json`; the owner must trust this
checkout and approve the exact Stop definition in Codex CLI `/hooks`. Editing a
definition requires approval again. Hooks are enabled by default
(`features.hooks = true`); disabling them disables this enforcement. Do not write a
user's global Codex configuration to enable project hooks. For manual setup, launch
`codex -C <checkout>`, trust the project during onboarding or through `/permissions`,
then use `/hooks` to inspect, enable and approve the project's Stop hook.

Layers include Claude Code's hooks, Codex's approved project Stop hook, the git hooks,
`tests/test_standards.py` (stdlib rules and enforcement-configuration checks), and the
installer (it refuses to pin a commit that breaks the stdlib rules S1 to S8; it does not
run ruff, pyright or the tests).

## Codex Stop contract

The Stop command invokes `tools/codex_stop_gate.py` directly with Python 3.13. Codex
provides JSON on stdin, including `cwd`, `session_id`, `hook_event_name`, `turn_id`,
`stop_hook_active` and `last_assistant_message`. The wrapper ignores that payload: it
derives the worktree from its own file and always runs one
`python -m tools.check --full` subprocess using the same Python interpreter. The gate
receives closed stdin, so it cannot consume the hook payload.

A passing gate returns compact `{}` on stdout and exit 0. A failed gate, launch error or
timeout returns exit 2 with a blocking reason on stderr. The gate timeout is 540
seconds, below the hook's 600-second timeout. No transcript payload is logged or echoed.
These are Codex hook responses; muninn provider hooks retain their separate
compact-JSON, exit-0 contract.

`stop_hook_active` means Codex already continued after a blocked Stop. It never permits
completion without a passing gate. There are no recursive hook calls, internal retries
or retry-count bypasses. A timeout bounds each invocation; it does not prevent repeated
blocked turns. Fix a persistent failure or ask the owner for help before retrying
unchanged work.

If the host lacks hooks, or trust, approval or feature enablement is missing, run the
full gate manually after the last edit and report its result verbatim. Another agent's
later change to a checked file invalidates that result. Local wrapper tests do not
establish that a real trusted Codex turn is blocked. The hook contract is documented at
https://learn.chatgpt.com/docs/hooks.

## Limits

| Rule | Limit | Applies to |
|---|---|---|
| S1 file length | 400 non-blank lines | every `.py` file in `muninn/`, `install/`, `tools/`, `tests/` |
| S2 function length | 100 lines (aim for 40) | every function and method |
| Ruff complexity | McCabe 10, 7 arguments, 12 branches, 6 returns, 50 statements | `muninn/`, `install/`, `tools/`, `tests/` |

Split modules by responsibility. Each module must have a single purpose and a name that
identifies it. Do not shuffle code into `utils.py`.

## PEPs followed

| PEP | Meaning here | Enforced by |
|---|---|---|
| 8 | style, 79 columns, naming (exceptions end in `Error`) | ruff `E`, `W`, `N`; black |
| 257 | docstring conventions | ruff `D` |
| 484, 526 | full type hints on every parameter and return | S7; pyright strict (tests: standard) |
| 563 | `from __future__ import annotations` in every module | S6; ruff `FA` |
| 585, 604, 695 | `list[int]`, `str \| None`, modern generics | ruff `UP` |

Pathlib over `os.path` (ruff `PTH`), no commented-out code (`ERA`), comprehensions and
builtins over manual loops (`C4`, `SIM`, `PERF`, `PIE`, `RET`).

## Python rules

- Imports are explicit and at the top of the file: no star imports, no imports inside
  functions, no `TYPE_CHECKING` (fix the architecture instead of a cycle). (S6)
- No mutable default arguments. (S8)
- EAFP over look-before-you-leap; context managers for every resource; f-strings only;
  early returns over nesting.
- Untrusted text is sanitised where it is inserted into output.
- Standard library only in `muninn/` and `install/`.

## Docstrings: Google style, everywhere

Every module, class and function in `muninn/`, `install/` and `tools/` has one. In
`tests/`, modules, classes and helper functions require docstrings. `test_*` methods and
fixture hooks (`setUp`, `tearDown`) are exempt because their names identify their
purpose. A closure (a function defined inside another function) needs none anywhere. A
one-line docstring is enough for a trivial private helper; anything longer uses the
sections below, and the sections must match the code (ruff `D417`, `DOC201`, `DOC402`,
`DOC501`).

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

Types live in the annotations, not in the docstring. Allowed section names: Args,
Returns, Yields, Raises, Attributes, Example(s), Note(s), Warning, See Also, Todo. (S4)

## Comments: document constraints and rationale

Comments explain constraints and rationale rather than restating operations. Document:

- an invariant the code relies on, or an ordering that must not change (locks,
  transactions, journal modes);
- a non-obvious choice and the alternative that was rejected, with the reason;
- a trap or edge case (an off-by-one, a platform quirk, a SQLite pragma, a regex that
  must stay linear);
- a security or privacy reason (why text is redacted, why a file is 0600);
- a deliberate limit, with the point at which it should be revisited.

Omit redundant comments and authoring provenance. A comment must make sense to someone
with no access to any conversation, review or planning document: no design, spec,
work-unit, finding or ticket labels (S5). A public name, a file path or a PR number is
fine because anyone can follow it.

## Bash

`bin/muninn` is POSIX `sh`: `set -eu`, every expansion quoted, `shellcheck` clean, no
bashisms.

## When these rules conflict with another instruction

A skill or guideline that says "public docstrings only" or "no comments unless needed"
does not override this document: Google-style docstrings and comments explaining
constraints are mandatory under this policy.
