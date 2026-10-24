"""Opt-in disposable Codex resolution and direct-hook proof; no model calls.

Requires a frozen manifest; never proves interactive or launchd delivery.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

from install.constants import PLUGIN_ID
from install.context import Ctx, run_real
from install.provider_paths import render_pinned
from install.transforms import enable, repoint, write_trust
from install.trust import codex_hooks
from tools.learning_eval.mac_provider_models import (
    PROVIDER_SHA256,
    PROVIDER_VERSION,
    artifacts,
    canonical,
    check_hooks,
    codex_probe,
    command,
    copy_private,
    environment,
    file_hash,
    learning_state,
    manifest,
    provider_environment,
    write_private,
)

ROOT = Path(__file__).resolve().parents[2]
HOOKS = "integrations/codex/hooks/hooks.json"
WORDS = "syntheticalpha syntheticbeta syntheticgamma"

EVENT_NAMES = {
    "session_start": "SessionStart",
    "pre_compact": "PreCompact",
    "session_end": "SessionEnd",
    "user_prompt_submit": "UserPromptSubmit",
}


def synthetic_transcripts(ctx: Ctx) -> None:
    """Write minimal primary, subagent and reviewer provider-shaped records."""
    for kind in ("user", "subagent", "guardian_review"):
        records: list[dict[str, Any]] = [
            {
                "type": "session_meta",
                "timestamp": "2026-01-02T03:04:05Z",
                "payload": {
                    "id": f"synthetic-{kind}",
                    "session_id": f"synthetic-{kind}",
                    "thread_source": kind,
                    "source": "cli",
                    "cwd": str(ctx.home),
                    "timestamp": "2026-01-02T03:04:05Z",
                },
            }
        ]
        if kind == "user":
            records.append(
                {
                    "type": "response_item",
                    "timestamp": "2026-01-02T03:04:06Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": WORDS,
                            }
                        ],
                    },
                }
            )
        write_private(
            ctx.home / "transcripts" / f"rollout-{kind}.jsonl",
            b"".join(canonical(row) + b"\n" for row in records),
        )


def prepare(
    ctx: Ctx, candidate: Path, python: Path
) -> tuple[dict[str, str], bytes]:
    """Prepare synthetic homes and initialize the disabled store by public CLI.

    Args:
        ctx: Context within a newly created disposable root.
        candidate: Exact frozen candidate source tree.
        python: Explicit interpreter with the candidate runtime dependencies.

    Returns:
        Sanitized environment and rendered hook template bytes.

    Raises:
        ValueError: If public ingest fails or admits model work.
    """
    for directory in (
        ctx.data,
        ctx.codex_home,
        ctx.settings.parent,
        ctx.home / "tmp",
        ctx.home / "provider-bin",
        ctx.muninn.parent,
    ):
        for parent in reversed((directory, *directory.parents)):
            if not parent.exists():
                parent.mkdir(mode=0o700)
    copy_private(candidate / "muninn", ctx.release / "muninn")
    copy_private(
        candidate / "integrations/codex", ctx.release / "integrations/codex"
    )
    launcher = ctx.release / "bin/muninn"
    write_private(launcher, (candidate / "bin/muninn").read_bytes(), 0o700)
    ctx.muninn.symlink_to(launcher)
    rendered = render_pinned(ctx, HOOKS, (candidate / HOOKS).read_bytes())
    write_private(ctx.release / HOOKS, rendered)
    config = enable(repoint("", str(ctx.release / "integrations/codex")))
    write_private(ctx.config, config.encode())
    synthetic_transcripts(ctx)
    env = environment(ctx, python)
    result = command([str(ctx.muninn), "ingest"], env, ctx.home, timeout=30)
    write_private(ctx.home / "ingest.json", result.stdout)
    if result.returncode or learning_state(ctx) != {
        "admission": 0,
        "policy_present": False,
        "requests": 0,
        "calls": 0,
    }:
        raise ValueError("disabled_bootstrap_failed")
    refusal = command(
        [
            str(ctx.muninn),
            "learn",
            "request",
            "--session",
            "codex:synthetic-user",
        ],
        env,
        ctx.home,
    )
    write_private(ctx.home / "learning-refusal.json", refusal.stdout)
    if (
        refusal.returncode != 2
        or json.loads(refusal.stdout).get("error") != "review_policy_required"
    ):
        raise ValueError("disabled_request_not_refused")
    return env, rendered


def direct_hooks(
    ctx: Ctx, env: dict[str, str], rendered: bytes
) -> list[dict[str, Any]]:
    """Invoke rendered commands with bounded synthetic lifecycle input."""
    hooks = json.loads(rendered)["hooks"]
    cases = [
        ("session_start", source, "user", "read_without_policy")
        for source in ("startup", "resume", "compact")
    ]
    cases += [
        ("pre_compact", "auto", "user", "empty"),
        ("session_end", "other", "user", "empty"),
        ("user_prompt_submit", "prompt", "user", "archive_context"),
    ]
    cases += [
        (event, "startup", kind, "suppressed")
        for kind in ("subagent", "guardian_review")
        for event in ("session_start", "user_prompt_submit")
    ]
    results: list[dict[str, Any]] = []
    for event, source, kind, expectation in cases:
        handler = hooks[EVENT_NAMES[event]][0]["hooks"][0]
        payload = {
            "cwd": str(ctx.home),
            "session_id": (
                "synthetic-caller" if kind == "user" else f"synthetic-{kind}"
            ),
            "transcript_path": str(
                ctx.home / "transcripts" / f"rollout-{kind}.jsonl"
            ),
            "hook_event_name": EVENT_NAMES[event],
            "source": source,
            "model": "synthetic-no-model-call",
            "permission_mode": "default",
            "reason": "other",
            "trigger": "auto",
            "turn_id": "synthetic-turn",
            "prompt": WORDS,
        }
        result = command(
            ["/bin/sh", "-c", handler["command"]],
            env,
            ctx.home,
            payload=canonical(payload),
            timeout=handler["timeout"],
            limit=65536,
        )
        body = json.loads(result.stdout)
        if (
            result.returncode
            or result.stderr
            or b"\n" in result.stdout.strip()
            or json.dumps(body, separators=(",", ":")).encode()
            != result.stdout.strip()
        ):
            raise ValueError("hook_protocol_failed")
        if expectation in {"empty", "suppressed"} and body != {}:
            raise ValueError("hook_suppression_failed")
        if expectation == "read_without_policy" and (
            body.get("hookSpecificOutput", {}).get("hookEventName")
            != EVENT_NAMES[event]
            or "review_policy_required"
            in body.get("hookSpecificOutput", {}).get("additionalContext", "")
        ):
            raise ValueError("session_start_policy_coupling")
        if expectation == "archive_context" and WORDS not in body.get(
            "hookSpecificOutput", {}
        ).get("additionalContext", ""):
            raise ValueError("archive_context_not_observed")
        results.append(
            {
                "event": event,
                "source": source,
                "thread_class": kind,
                "expectation": expectation,
                "payload": payload,
                "output": body,
                "returncode": result.returncode,
                "elapsed_ms": result.elapsed_ms,
                "deadline_ms": handler["timeout"] * 1000,
            }
        )
    return results


def provider(
    ctx: Ctx, executable: Path, env: dict[str, str], rendered: bytes
) -> dict[str, Any]:
    """Resolve the pinned local plugin, checking actual hashes before trust."""
    version = command([str(executable), "--version"], env, ctx.home)
    if (
        version.returncode
        or version.stdout.decode().strip() != PROVIDER_VERSION
    ):
        raise ValueError("provider_version_mismatch")
    (ctx.home / "provider-bin/codex").symlink_to(executable)
    added = command(
        [str(executable), "plugin", "add", PLUGIN_ID, "--json"],
        env,
        ctx.home,
        timeout=30,
    )
    if added.returncode:
        raise ValueError("plugin_add_failed")
    result = json.loads(added.stdout)
    plugin_version = result["version"]
    if (
        not isinstance(plugin_version, str)
        or not plugin_version
        or Path(plugin_version).name != plugin_version
        or plugin_version in {".", ".."}
    ):
        raise ValueError("plugin_version_invalid")
    installed = Path(result["installedPath"])
    cached = installed / "hooks/hooks.json"
    if (
        installed != ctx.cache / plugin_version
        or not installed.resolve().is_relative_to(ctx.home)
        or cached.resolve() != cached
        or cached.read_bytes() != rendered
    ):
        raise ValueError("plugin_cache_mismatch")
    wanted = codex_hooks(rendered, platform="darwin")
    with provider_environment(env):
        raw = codex_probe(ctx, timeout=10)
    write_private(ctx.home / "hooks-before-trust.json", canonical(raw))
    before = check_hooks(raw, wanted, cached)
    write_private(
        ctx.config, write_trust(ctx.config.read_text(), wanted).encode()
    )
    with provider_environment(env):
        after = check_hooks(codex_probe(ctx, timeout=10), wanted, cached)
    version_after = command([str(executable), "--version"], env, ctx.home)
    if (
        version_after.returncode
        or version_after.stdout != version.stdout
        or cached.read_bytes() != rendered
    ):
        raise ValueError("provider_changed")
    if any(entry["trustStatus"] != "trusted" for entry in after):
        raise ValueError("private_trust_not_observed")
    return {
        "version": PROVIDER_VERSION,
        "cache_path": str(cached),
        "rendered_sha256": hashlib.sha256(rendered).hexdigest(),
        "before_private_trust": before,
        "after_private_trust": after,
    }


def run(
    private: Path, executable: Path, expected_digest: str
) -> dict[str, Any]:
    """Run only explicitly frozen local proof, retaining private artifacts.

    Args:
        private: New absolute root; existing directories are refused.
        executable: Absolute installed Codex binary pinned by byte hash.
        expected_digest: SHA256 of canonical manifest(ROOT), frozen externally.

    Returns:
        Exact scope, evidence and hashes of the private report artifacts.

    Raises:
        ValueError: If identities, preconditions or checks fail.
    """
    if (
        sys.platform != "darwin"
        or not private.is_absolute()
        or not executable.is_absolute()
    ):
        raise ValueError("native_mac_absolute_paths_required")
    if (
        private.exists()
        or private.is_symlink()
        or private.parent.resolve() != private.parent
    ):
        raise ValueError("new_unaliased_private_root_required")
    candidate = manifest(ROOT)
    if hashlib.sha256(canonical(candidate)).hexdigest() != expected_digest:
        raise ValueError("candidate_digest_mismatch")
    executable = executable.resolve(strict=True)
    if file_hash(executable) != PROVIDER_SHA256:
        raise ValueError("provider_bytes_mismatch")
    python = Path(sys.executable).absolute()
    python_hash = file_hash(python)
    private.mkdir(mode=0o700)
    previous_umask = os.umask(0o077)
    try:
        ctx = Ctx(private, run_real, "mac-provider-proof", platform="darwin")
        env, rendered = prepare(ctx, ROOT, python)
        head = command(
            ["/usr/bin/git", "-C", str(ROOT), "rev-parse", "HEAD"],
            env,
            private,
        )
        if head.returncode:
            raise ValueError("candidate_head_unavailable")
        resolved = provider(ctx, executable, env, rendered)
        direct = direct_hooks(ctx, env, rendered)
        state = learning_state(ctx)
        if state != {
            "admission": 0,
            "policy_present": False,
            "requests": 0,
            "calls": 0,
        }:
            raise ValueError("learning_state_changed")
        if (
            manifest(ROOT) != candidate
            or file_hash(executable) != PROVIDER_SHA256
            or file_hash(python) != python_hash
        ):
            raise ValueError("outside_fingerprint_changed")
        report = {
            "host": {
                "platform": sys.platform,
                "machine": platform.machine(),
                "macos": platform.mac_ver()[0],
            },
            "candidate": candidate,
            "candidate_head": head.stdout.decode().strip(),
            "candidate_digest": expected_digest,
            "provider_executable": str(executable),
            "provider_sha256": PROVIDER_SHA256,
            "python": str(python),
            "python_sha256": python_hash,
            "config_sha256": file_hash(ctx.config),
            "provider": resolved,
            "direct_hooks": direct,
            "learning_state": state,
            "outside_paths": {
                "observed_unchanged": [
                    "candidate manifest files",
                    "provider executable",
                    "Python executable",
                ],
                "other_paths": "unverified",
            },
            "native_interactive_delivery": "unverified",
            "native_service": "unverified",
            "model_isolation": "unverified",
            "runtime_dependency_provisioning": "unverified",
            "model_endpoint_operations_requested": 0,
            "proof_kind": "plugin_resolution_and_direct_invocation",
        }
        return artifacts(private, report)
    finally:
        os.umask(previous_umask)


def main() -> None:
    """Require a new private root, executable and candidate freeze."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--candidate-digest", required=True)
    args = parser.parse_args()
    result = run(args.private_root, args.codex, args.candidate_digest)
    print(canonical(result).decode())


if __name__ == "__main__":
    main()
