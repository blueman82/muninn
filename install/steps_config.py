"""Install steps that read or edit provider configuration.

``record`` snapshots the before-values; ``claude`` and ``codex`` apply the
edits. Every edit goes through ``configedit.edit_file`` with a check that
proves nothing outside our own keys changed.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from install import configedit as ce
from install import legacy_state
from install.constants import (
    CODEX_VERIFIED,
    CURSOR_DOC_URL,
    OWNER_STEP,
    PLUGIN_ID,
    PRIVATE_DIR_MODE,
)
from install.context import Ctx, StepFailedError, dry, link_text, must
from install.provider_apps import installed
from install.provider_paths import codex_argv, codex_identity, cursor_commands
from install.record import Record, codex_record, json_entry, save
from install.transforms import (
    claude_paths,
    codex_check,
    cursor_handlers,
    cursor_ours,
    cursor_paths,
    drop_trust,
    edit_cursor_settings,
    edit_settings,
    enable,
    repoint,
    write_trust,
)
from install.trust import codex_hooks
from muninn.platform_io import ensure_private_dir

CURSOR_HOOKS_PIN = "integrations/cursor/hooks.json"
EMPTY_CURSOR_CONFIG = b'{"version":1,"hooks":{}}\n'


def choose_cursor_hooks(ctx: Ctx) -> bool:
    """Ask whether this install should configure Cursor automatically."""
    if not installed(ctx, "cursor"):
        return False
    ctx.say(f"Cursor hook target: {ctx.cursor_settings}")
    ctx.say(f"Manual setup instructions: {CURSOR_DOC_URL}")
    if ctx.dry_run:
        ctx.say(
            "DRY-RUN: Cursor hook choice skipped; no settings will change."
        )
        return False
    if not sys.stdin.isatty():
        ctx.say("Non-interactive install: Cursor hooks left for manual setup.")
        return False
    try:
        choice = input(
            f"Set up Muninn's Cursor lifecycle hooks in "
            f"{ctx.cursor_settings}? "
            "[a]utomatic or [m]anual (default m)? "
        )
    except (EOFError, OSError):
        choice = ""
    automatic = choice.strip().casefold() in ("a", "automatic")
    ctx.say(
        "Cursor hook setup: automatic."
        if automatic
        else "Cursor hook setup: manual; existing settings left unchanged."
    )
    return automatic


def record(ctx: Ctx, rec: Record) -> None:
    """Snapshot the before-values of our config keys and the links."""
    if "legacy" in rec:
        legacy_state.validate(ctx, rec)
        if rec.get("config_snapshot_complete"):
            return
    settings = (
        ce.load_json(ce.read_file(ctx.settings)) if rec["has_claude"] else {}
    )
    rec["claude"] = {
        "settings": [json_entry(settings, p) for p in claude_paths(settings)]
    }
    rec["codex"] = (
        codex_record(ce.read_file(ctx.config).decode())
        if rec["has_codex"]
        else []
    )
    rec["cursor_file_present"] = ctx.cursor_settings.exists()
    cursor_obj = (
        ce.load_json(ce.read_file(ctx.cursor_settings))
        if rec["cursor_hooks"] and rec["cursor_file_present"]
        else {}
    )
    rec["cursor"] = (
        [json_entry(cursor_obj, path) for path in cursor_paths()]
        if rec["cursor_hooks"]
        else []
    )
    rec["cursor_ours"] = [
        {
            "event": event,
            "event_index": ce.jget(cursor_obj, ("hooks", event))[2],
            "index": index,
            "value": handler,
        }
        for event, command in cursor_commands(ctx).items()
        for index, handler in enumerate(cursor_handlers(cursor_obj, event))
        if rec["cursor_hooks"] and cursor_ours(handler, command)
    ]
    links = (ctx.lib / "current", ctx.lib / "python", ctx.muninn)
    rec["links"] = {
        str(p): link_text(p) if p.is_symlink() else None for p in links
    }
    plan = (
        "would note the current links and the provider settings entries "
        "this install changes, so a failure can be undone"
    )
    if dry(ctx, plan):
        return
    version, identity = (
        _codex_observation(ctx) if installed(ctx, "codex") else ("", ())
    )
    rec["codex_version"] = version
    rec["codex_identity"] = list(identity)
    rec["trust"] = "auto" if version else "owner"
    if "legacy" not in rec:
        ctx.rdir.mkdir(mode=PRIVATE_DIR_MODE, parents=True)
    if rec["has_codex"]:
        _snapshot_cache(ctx, rec)
    rec["config_snapshot_complete"] = True
    save(ctx, rec)


def _snapshot_cache(ctx: Ctx, rec: Record) -> None:
    """Complete the owned plugin-cache snapshot before any cache mutation."""
    manifest = ce.tree_snapshot(ctx.cache)
    backup = ctx.rdir / "codex-cache"
    if ctx.cache.exists():
        shutil.copytree(ctx.cache, backup)
        if (
            ce.tree_snapshot(backup) != manifest
            or ce.tree_snapshot(ctx.cache) != manifest
        ):
            raise StepFailedError("Codex cache changed during snapshot")
    rec["codex_cache"] = {
        "complete": True,
        "present": ctx.cache.exists(),
        "manifest": {key: list(value) for key, value in manifest.items()},
    }


def claude(ctx: Ctx, rec: Record) -> None:
    """Reconcile our lifecycle and prompt hooks in Claude settings."""
    if not rec["has_claude"]:
        if not installed(ctx, "claude"):
            ctx.say("Claude app not installed: setup skipped")
        elif ctx.upgrade:
            ctx.say("Claude installed: existing settings retained")
        else:
            ctx.say(
                "Claude installed; manual setup: settings not found; "
                "Claude Code left unconfigured. Merge "
                f"{ctx.release / 'integrations/claude/settings-hooks.json'} "
                f"into {ctx.settings} after initializing Claude Code."
            )
        return
    added = "reconcile the muninn lifecycle and prompt hooks in"
    if dry(ctx, f"would {added} {ctx.settings}"):
        return
    frag = ctx.release / "integrations/claude/settings-hooks.json"
    fragment = json.loads(frag.read_bytes())["hooks"]

    def check(before: bytes, after: bytes) -> None:
        ce.json_check(before, after, claude_paths(ce.load_json(before)))

    ce.edit_file(ctx.settings, lambda b: edit_settings(b, fragment), check)


def _edit_config(ctx: Ctx, transform: Callable[[str], str]) -> None:
    """Apply a text transform to config.toml under the usual safeguards."""
    ce.edit_file(
        ctx.config, lambda b: transform(b.decode()).encode(), codex_check
    )


def _add_plugin(ctx: Ctx, rec: Record) -> bytes:
    """Have Codex install our plugin and check what it produced.

    Args:
        ctx: The run context.
        rec: The run record; gains ``codex_plugin_version``.

    Returns:
        The pinned hooks.json bytes, which the cached copy matched.

    Raises:
        StepFailedError: If Codex put the plugin somewhere unexpected, or
            its cached hooks.json is not the pinned one.
    """
    snapshot = rec["codex_cache"]
    current = {
        key: list(value) for key, value in ce.tree_snapshot(ctx.cache).items()
    }
    backup = {
        key: list(value)
        for key, value in ce.tree_snapshot(ctx.rdir / "codex-cache").items()
    }
    if (
        snapshot["complete"] is not True
        or current != snapshot["manifest"]
        or backup != snapshot["manifest"]
    ):
        raise StepFailedError("Codex cache changed before plugin install")
    rec["codex_cache_changed"] = True
    save(ctx, rec)
    before = ce.read_file(ctx.config)
    env = dict(os.environ, CODEX_HOME=str(ctx.codex_home))
    argv = codex_argv(ctx, "plugin", "add", PLUGIN_ID, "--json")
    out = json.loads(must(ctx, argv, env=env, quiet=True).stdout)
    # Codex rewrites config.toml itself; it may only have touched our keys.
    codex_check(before, ce.read_file(ctx.config))
    # Read before the location check: if the pinned file is missing, that
    # failure must surface ahead of the "unexpected place" one.
    pinned = (ctx.release / "integrations/codex/hooks/hooks.json").read_bytes()
    rec["codex_plugin_version"] = out["version"]
    installed = Path(out["installedPath"]).resolve()
    if installed != (ctx.cache / out["version"]).resolve():
        raise StepFailedError(
            "codex installed the plugin somewhere unexpected"
        )
    if (installed / "hooks/hooks.json").read_bytes() != pinned:
        raise StepFailedError(
            "codex cache hooks.json differs from the pinned copy"
        )
    return pinned


def codex(ctx: Ctx, rec: Record) -> None:
    """Configure Codex in a fixed order: untrust, install, enable, trust.

    Trust is dropped first so Codex never holds a hash for hooks that are
    about to change, and written last, once the installed hooks are proven
    identical to the pinned ones.
    """
    source = str(ctx.release / "integrations/codex")
    if not rec["has_codex"]:
        if not installed(ctx, "codex"):
            ctx.say("Codex app not installed: setup skipped")
        elif ctx.upgrade:
            ctx.say("Codex installed: existing settings retained")
        else:
            ctx.say(
                "Codex installed; manual setup: config not found; "
                "Codex left unconfigured. Initialize Codex, then add "
                f"the plugin from {source} and trust its hooks in /hooks."
            )
        return
    if dry(
        ctx,
        f"would add the muninn Codex plugin, enable it and trust its "
        f"hooks in {ctx.config}",
    ):
        return
    _edit_config(ctx, drop_trust)
    _edit_config(ctx, lambda text: repoint(text, source))
    pinned = _add_plugin(ctx, rec)
    _edit_config(ctx, enable)
    if rec["trust"] == "auto" and not _can_trust(ctx, rec):
        rec["trust"] = "owner"
    if rec["trust"] == "auto":
        _edit_config(
            ctx,
            lambda text: write_trust(
                text, codex_hooks(pinned, platform=ctx.platform)
            ),
        )
    else:
        ctx.say(OWNER_STEP)


def _codex_observation(ctx: Ctx) -> tuple[str, tuple[str, ...]]:
    """Observe a successful verified version without a candidate change.

    Args:
        ctx: Provider executable selection and process runner.

    Returns:
        Verified version and opaque identity, or empty values for owner trust.
    """
    try:
        before = codex_identity(ctx)
        env = dict(
            os.environ,
            HOME=str(ctx.home),
            USERPROFILE=str(ctx.home),
            CODEX_HOME=str(ctx.codex_home),
        )
        done = ctx.run(codex_argv(ctx, "--version"), env=env)
        if done.returncode != 0:
            return "", ()
        version = done.stdout.decode().strip()
        verified = (
            ("codex-cli 0.159.2",)
            if ctx.platform == "win32"
            else CODEX_VERIFIED
        )
        if version not in verified or before != codex_identity(ctx):
            return "", ()
    except (OSError, UnicodeError):
        return "", ()
    return version, before


def _can_trust(ctx: Ctx, rec: Record) -> bool:
    """Recheck selected provider identity and verified version before trust.

    Args:
        ctx: Current executable selection and runner.
        rec: Original version and opaque candidate observation.

    Returns:
        Whether the currently selected provider matches the original proof.
    """
    version, identity = _codex_observation(ctx)
    return (
        bool(version)
        and version == rec.get("codex_version")
        and list(identity) == rec.get("codex_identity")
    )


def cursor(ctx: Ctx, rec: Record) -> None:
    """Merge Muninn's lifecycle hooks into Cursor's user hooks.json."""
    if not rec["cursor_hooks"]:
        return
    if dry(
        ctx,
        f"would merge Muninn's lifecycle hooks into {ctx.cursor_settings}",
    ):
        return
    fragment = json.loads((ctx.release / CURSOR_HOOKS_PIN).read_bytes())
    ensure_private_dir(ctx.cursor_settings.parent)
    ce.create_file(ctx.cursor_settings, EMPTY_CURSOR_CONFIG, 0o600)

    def edit(data: bytes) -> bytes:
        return edit_cursor_settings(
            data, cursor_commands(ctx), fragment["hooks"]
        )

    def check(before: bytes, after: bytes) -> None:
        ce.json_check(before, after, cursor_paths())

    ce.edit_file(ctx.cursor_settings, edit, check)
