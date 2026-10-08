"""Mandatory hosted provider proof using synthetic homes and real commands."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from install import configedit
from install.context import Ctx, run_real
from install.provider_paths import hook_invocations, render_pinned
from install.trust import codex_hooks
from muninn import ingest, knowledge, obs, platform_io, platform_windows
from tests.ingest_support import ROOT, TID, line, rollout
from tests.native_diagnostics import write
from tests.provider_native_codex import prove_codex
from tests.test_classify import codex_meta, user_msg

_SHA = "a" * 40
_WORDS = "nativealpha nativebeta nativegamma"


def fixture(ctx: Ctx, checkpoint: Callable[[str], None]) -> dict[str, str]:
    """Build a private synthetic installation and a cited knowledge note.

    Args:
        ctx: Temporary installation context.
        checkpoint: Persist an explicit fixture phase before each operation.

    Returns:
        Isolated environment for the real provider commands.
    """
    for phase, path in (
        ("fixture_home", ctx.home),
        ("fixture_data", ctx.data),
        ("fixture_lib", ctx.lib),
        ("fixture_bin", ctx.muninn.parent),
        ("fixture_codex", ctx.codex_home),
        ("fixture_claude", ctx.settings.parent),
    ):
        checkpoint(phase)
        platform_io.ensure_private_dir(path)
    checkpoint("fixture_release")
    release = ctx.lib / _SHA
    platform_io.ensure_private_dir(release)
    checkpoint("fixture_copy")
    shutil.copytree(
        ROOT / "muninn",
        release / "muninn",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for name in (
        ("muninn.cmd", "muninn.ps1")
        if sys.platform == "win32"
        else ("muninn",)
    ):
        shutil.copyfile(ROOT / "bin" / name, ctx.muninn.parent / name)
    checkpoint("fixture_selection")
    if sys.platform == "win32":
        record = json.dumps({"sha": _SHA, "python": sys.executable}).encode()
        configedit.atomic_write(ctx.lib / "selection.json", record, 0o600)
    else:
        (ctx.lib / "current").symlink_to(release)
        (ctx.lib / "python").symlink_to(sys.executable)
        ctx.muninn.unlink()
        ctx.muninn.symlink_to(ROOT / "bin/muninn")
    source = ctx.home / "synthetic transcripts"
    source.mkdir()
    records = [codex_meta("user", TID), user_msg(1, _WORDS)]
    records[0]["payload"]["cwd"] = str(ctx.home)
    path = source / rollout()
    path.parent.mkdir(parents=True)
    path.write_bytes(b"".join(map(line, records)))
    roots = {"codex-sessions": source}
    checkpoint("fixture_ingest")
    stats = ingest.run_pass(ctx.data, roots)
    assert stats.events_added == 1
    checkpoint("fixture_knowledge")
    knowledge.run_add(
        ctx.data,
        kind="decision",
        text=_WORDS,
        cites=[(f"codex:{TID}:2.1", _WORDS)],
        cwd=str(ctx.home),
        actor="user",
        roots=roots,
        env={},
    )
    checkpoint("fixture_status")
    obs.write_status(ctx.data, {"last_pass_at": time.time(), "interval_s": 60})
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MUNINN_")
    }
    env.update(
        HOME=str(ctx.home),
        USERPROFILE=str(ctx.home),
        CODEX_HOME=str(ctx.codex_home),
        CLAUDE_CONFIG_DIR=str(ctx.settings.parent),
        MUNINN_HOME=str(ctx.data),
        MUNINN_PYTHON=sys.executable,
        MUNINN_ROOTS=json.dumps(
            {name: str(root) for name, root in roots.items()}
        ),
    )
    return env


def prove_hooks(ctx: Ctx, env: dict[str, str]) -> list[dict[str, Any]]:
    """Measure process-cold real hook routes under the unchanged timeout.

    Args:
        ctx: Temporary provider installation.
        env: Isolated provider and data homes.

    Returns:
        Per-route elapsed milliseconds and compact JSON proof.
    """
    write("provider", "claude_render")
    relative = "integrations/claude/settings-hooks.json"
    configedit.atomic_write(
        ctx.settings,
        render_pinned(ctx, relative, (ROOT / relative).read_bytes()),
        0o600,
    )
    results: list[dict[str, Any]] = []
    payload = json.dumps(
        {
            "cwd": str(ctx.home),
            "session_id": "different-session",
            "prompt": _WORDS,
            "hook_event_name": "SessionStart",
        }
    ).encode()
    calls: list[str | list[str]] = []
    calls.extend(hook_invocations(ctx)[:2])
    for hooks_path in sorted(ctx.cache.glob("*/hooks/hooks.json")):
        for hook in codex_hooks(
            hooks_path.read_bytes(), platform=ctx.platform
        ):
            if sys.platform == "win32":
                comspec = env.get("COMSPEC", "cmd.exe")
                platform_windows.assert_executable(Path(comspec))
                calls.append(
                    subprocess.list2cmdline([comspec])
                    + ' /C "'
                    + hook["command"]
                    + '"'
                )
            else:
                calls.append(
                    [env.get("SHELL", "/bin/sh"), "-lc", hook["command"]]
                )
    for index, argv in enumerate(calls):
        write(
            "provider", "routes", metrics={"route": index, "budget_ms": 5000}
        )
        started = time.monotonic()
        done = subprocess.run(
            argv,
            input=payload,
            env=env,
            cwd=ctx.home,
            capture_output=True,
            timeout=90,
            check=False,
        )
        elapsed = time.monotonic() - started
        write(
            "provider",
            None,
            metrics={
                "route": index,
                "elapsed_ms": round(elapsed * 1000),
                "budget_ms": 5000,
                "returncode": done.returncode,
            },
        )
        assert done.returncode == 0, (
            index,
            done.returncode,
            done.stderr.decode(errors="replace"),
        )
        assert done.stderr == b"", (index, "unexpected stderr")
        output = json.loads(done.stdout)
        assert _WORDS in output["hookSpecificOutput"]["additionalContext"], (
            index,
            "missing cited synthetic memory",
        )
        assert b"\n" not in done.stdout.rstrip(b"\n"), (
            index,
            "noncompact JSON",
        )
        results.append(
            {
                "route": index,
                "elapsed_ms": round(elapsed * 1000),
                "budget_ms": 5000,
            }
        )
        assert elapsed < 5, ("provider_timeout_exceeded", results[-1])
    return results


def main() -> None:
    """Run native provider proof without touching real provider homes."""
    with tempfile.TemporaryDirectory(prefix="muninn-provider-proof-") as tmp:

        def checkpoint(phase: str) -> None:
            write("provider", phase)

        try:
            checkpoint("fixture_home")
            home = Path(tmp).resolve() / "space café 雪 owner's $ % home"
            ctx = Ctx(home, run_real, "synthetic", platform=sys.platform)
            env = fixture(ctx, checkpoint)
            codex = prove_codex(ctx, env, checkpoint)
            timings = prove_hooks(ctx, env)
            write(
                "provider",
                "complete",
                metrics={
                    "hooks_count": len(timings),
                    "codex_hooks": codex["hooks"],
                },
                completed=True,
            )
            print(
                json.dumps(
                    {
                        "platform": sys.platform,
                        "codex": codex,
                        "hooks": timings,
                    },
                    separators=(",", ":"),
                )
            )
        except Exception as error:
            write("provider", None, error=error)
            raise


if __name__ == "__main__":
    main()
