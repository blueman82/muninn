# 0006: Engineering standards are enforced by a gate, not by instructions

Status: Accepted, 2026-10-02. Decided by: owner ("make it mechanically impossible for any LLM to not follow the principles").

**Context.** Instructions in a prompt, a skill or a style guide get skipped, forgotten or overridden by a later
instruction. Before this record the code had files up to 1,004 lines, 181 of 340 runtime functions without full type
hints, docstrings in no consistent style, and comments citing internal design labels that mean nothing to a reader.

**Decision.** The rules in `docs/STANDARDS.md` are checked by code, in layers that each fail closed:
- `tools/standards.py` (stdlib only) checks size, docstrings, comments, imports, annotations and defaults;
  `python3.13 -m tools.check --full` adds ruff (PEP 8, 257, 484, 563, 585, 604 and complexity), black, pyright strict and
  shellcheck. A missing tool fails the gate; it never skips.
- `tests/test_standards.py` runs the checker inside the normal test suite and asserts that the hooks, the pinned tools and
  the limits are still in place.
- `.githooks/pre-commit` blocks a commit that fails the gate; `.claude/settings.json` makes Claude Code run the gate after
  every edit and refuse to stop while it fails; the installer refuses to pin a commit that fails the stdlib rules.
- There is no `noqa`, no `# type: ignore` and no exemption list. A rule is changed in `tools/` or `pyproject.toml`, with an
  ADR, never bypassed in the code it governs.

**Consequences.** Every change pays the cost of the gate, which is the point. The owner's instruction overrides a skill
that says "public docstrings only". `git commit --no-verify` still works for a human, but the test suite, the Claude
Code hook and the installer each catch what it skips. A determined actor can delete all of them in one change; the
protection is against drift and forgetfulness, not against sabotage.
