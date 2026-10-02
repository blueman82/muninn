"""The verify step: machine-readable checks after an install.

Any failed check raises, which triggers rollback. A check that could not be
run records ``ok: null`` and never fails the install.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from install import configedit as ce
from install.constants import CLAUDE_EVENTS, OWNER_STEP, PLUGIN_ID
from install.context import Ctx, StepFailedError, dry, is_new, job
from install.record import Record
from install.steps_release import fresh, pctx_env
from install.transforms import ours
from install.trust import TrustedHook, codex_hooks

Check = Callable[[str, bool | None, str], None]


def hook_commands(ctx: Ctx) -> list[str]:
    """List every hook command that will run: Claude's and Codex's.

    Args:
        ctx: The run context.

    Returns:
        Commands from our Claude hook groups and every cached Codex
        hooks.json.
    """
    exists = ctx.settings.exists()
    settings = ce.load_json(ctx.settings.read_bytes()) if exists else {}
    cmds: list[str] = []
    for event in CLAUDE_EVENTS:
        groups: list[dict[str, Any]] = ce.jget(settings, ("hooks", event))[1]
        cmds += [
            h["command"] for g in groups or [] if ours(g) for h in g["hooks"]
        ]
    for path in sorted(ctx.cache.glob("*/hooks/hooks.json")):
        cmds += [h["command"] for h in codex_hooks(path.read_bytes())]
    return cmds


def _resolves(
    got: Mapping[str, Mapping[str, Any]],
    want: Mapping[str, TrustedHook],
    active: Path,
) -> bool:
    """Say whether Codex resolves exactly our hooks from the active cache.

    Args:
        got: Hooks Codex reports for our plugin, by key.
        want: Hooks we expect, by key.
        active: The cached hooks.json that must be the source.

    Returns:
        True when the key sets match and each command and source file does.
    """
    return set(got) == set(want) and all(
        got[k]["command"] == want[k]["command"]
        and Path(got[k]["sourcePath"]).resolve() == active.resolve()
        for k in want
    )


def _expected_hash(want: Mapping[str, TrustedHook], key: str) -> str | None:
    """Return the hash we expect for ``key``, or None for an unknown key."""
    return want[key]["hash"] if key in want else None


def codex_checks(ctx: Ctx, rec: Record, check: Check) -> None:
    """Ask Codex which hooks it resolves and record the outcome.

    A wrong answer fails verify; no answer, or an untrusted hook, only
    leaves the owner step.

    Args:
        ctx: The run context.
        rec: The run record; gains ``codex_hooks`` and may drop ``trust`` to
            ``owner``.
        check: Records one named check.
    """
    probe = ctx.probe
    if probe is None:
        return
    hooks = ctx.lib / "current/integrations/codex/hooks/hooks.json"
    key = f"{PLUGIN_ID}:hooks/hooks.json:"
    want = {key + h["suffix"]: h for h in codex_hooks(hooks.read_bytes())}
    try:
        entries = probe(ctx)
    except Exception as exc:  # no answer is not a wrong answer
        check("codex_resolves_new_hooks", None, f"probe: {type(exc).__name__}")
        rec["trust"] = "owner"
        ctx.say(f"WARNING: Codex hooks/list probe failed; {OWNER_STEP}")
        return
    got = {e["key"]: e for e in entries if e.get("pluginId") == PLUGIN_ID}
    active = ctx.cache / rec["codex_plugin_version"] / "hooks/hooks.json"
    check(
        "codex_resolves_new_hooks",
        _resolves(got, want, active),
        ", ".join(sorted(got)),
    )
    summary: dict[str, dict[str, Any]] = {
        k: {
            "trust": e["trustStatus"],
            "hash_match": e.get("currentHash") == _expected_hash(want, k),
        }
        for k, e in got.items()
    }
    rec["codex_hooks"] = summary
    if not all(v["trust"] == "trusted" for v in summary.values()):
        rec["trust"] = "owner"
        ctx.say(OWNER_STEP)


def verify(ctx: Ctx, rec: Record) -> None:
    """Run the machine-readable checks; any failure triggers rollback.

    Args:
        ctx: The run context.
        rec: The run record.

    Raises:
        StepFailedError: If any check came back definitely false.
    """
    if dry(ctx, "verify: PID, heartbeat, hooks, doctor"):
        return
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool | None, detail: str = "") -> None:
        # None means "could not verify", which must not fail the install.
        checks.append(
            {
                "check": name,
                "ok": None if ok is None else bool(ok),
                "detail": detail,
            }
        )

    j = job(ctx)
    check("new_pid_alive", is_new(ctx, j), f"pid {j and j['pid']}")
    check("heartbeat_fresh", fresh(ctx, 0))
    # PCTX_HOOK_DISABLE makes the hook echo ``{}`` without recording
    # anything, so running it here cannot pollute the new store.
    env = pctx_env(ctx.data, PCTX_HOOK_DISABLE="1")
    for cmd in hook_commands(ctx):
        r = ctx.run(shlex.split(cmd), env=env, input=b"{}")
        check(
            "hook_runs", r.returncode == 0 and r.stdout.strip() == b"{}", cmd
        )
    if ctx.probe and rec["has_codex"]:
        codex_checks(ctx, rec, check)
    r = ctx.run([ctx.pctx, "doctor"], env=pctx_env(ctx.data))
    check("doctor", r.returncode == 0)
    report = json.dumps({"checks": checks}, indent=1).encode()
    ce.atomic_write(ctx.rdir / "verify.json", report, 0o600)
    failed = [c["check"] for c in checks if c["ok"] is False]
    if failed:
        raise StepFailedError("verify failed: " + ", ".join(failed))
