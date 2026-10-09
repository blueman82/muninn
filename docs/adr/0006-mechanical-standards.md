# 0006: Enforce engineering standards with automated gates

## Status

Accepted

Date: 2026-10-02

## Context

Prompt instructions, skills and style guides do not reliably enforce standards; later
instructions can override them. Before this record the code had files up to 1,004 lines,
181 of 340 runtime functions without full type hints, docstrings in no consistent style,
and comments citing internal design labels without standalone meaning.

## Decision

The rules in `docs/STANDARDS.md` are checked by code, in layers that each fail closed:

- `tools/standards.py` (stdlib only) checks size, docstrings, comments, imports,
  annotations and defaults; `python3.13 -m tools.check --full` adds ruff (PEP 8, 257,
  484, 563, 585, 604 and complexity), black, pyright strict, shellcheck and the whole
  test suite (in parallel, `tools.run_tests`). A missing tool fails the gate; it never
  skips.
- `tests/test_standards.py` runs the checker inside the normal test suite and asserts
  that the hooks, the pinned tools and the limits are still in place.
- `.githooks/pre-commit` blocks a commit that fails the gate; `.claude/settings.json`
  makes Claude Code run the gate after every edit and refuse to stop while it fails; the
  installer refuses to pin a commit that fails the stdlib rules.
- `.codex/hooks.json` registers a project Stop command that delegates to the existing
  full gate through `tools/codex_stop_gate.py`. It blocks on failure, launch error or
  timeout, including repeated Stops; no retry bypass exists. Enforcement requires
  enabled Codex hooks, project trust and approval of the exact definition. Without
  those, the agent must run and report the final gate manually. Hook approval and a live
  trusted turn are owner checks.
- There is no `noqa`, no `# type: ignore` and no exemption list. A rule is changed in
  `tools/` or `pyproject.toml`, with an ADR, never bypassed in the code it governs.

## Consequences

Every change must pass the gate. The documented standards take precedence over skills
that require only public docstrings. A human can bypass the Git hook with
`git commit --no-verify`; the test suite, Claude Code hook and installer provide
additional checks. These mechanisms prevent accidental drift. They do not protect
against an actor who removes or disables the enforcement layers.
