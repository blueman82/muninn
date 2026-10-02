"""Install steps that read or edit provider configuration.

``record`` snapshots the before-values; ``claude`` and ``codex`` apply the
edits. Every edit goes through ``configedit.edit_file`` with a check that
proves nothing outside our own keys changed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from install import configedit as ce
from install.constants import CODEX_VERIFIED, OWNER_STEP, PLUGIN_ID
from install.context import Ctx, StepFailedError, dry, link_text, must
from install.record import Record, codex_record, json_entry, save
from install.transforms import (
    claude_paths,
    codex_check,
    drop_legacy,
    drop_trust,
    edit_settings,
    enable,
    repoint,
    write_trust,
)
from install.trust import codex_hooks

CODEX_HOOKS_PIN = "current/integrations/codex/hooks/hooks.json"


def record(ctx: Ctx, rec: Record) -> None:
    """Snapshot the before-values of our config keys and the links."""
    settings = (
        ce.load_json(ctx.settings.read_bytes()) if rec["has_claude"] else {}
    )
    rec["claude"] = {
        "settings": [json_entry(settings, p) for p in claude_paths(settings)]
    }
    rec["codex"] = (
        codex_record(ctx.config.read_text()) if rec["has_codex"] else []
    )
    links = (ctx.lib / "current", ctx.lib / "python", ctx.muninn)
    rec["links"] = {
        str(p): link_text(p) if p.is_symlink() else None for p in links
    }
    plan = (
        "would not touch Claude or Codex settings"
        if ctx.upgrade and not ctx.legacy
        else "would note the current links and the Claude and Codex "
        "settings entries this install changes, so a failure can be undone"
    )
    if dry(ctx, plan):
        return
    try:
        version = ctx.run(["codex", "--version"]).stdout.decode().strip()
    except OSError:  # no codex on this machine
        version = ""
    # Only a Codex whose trust hash we have verified may be pre-trusted.
    rec["trust"] = "auto" if version in CODEX_VERIFIED else "owner"
    ctx.rdir.mkdir(mode=0o700, parents=True)
    save(ctx, rec)


def claude(ctx: Ctx, rec: Record) -> None:
    """Merge our two hooks into Claude settings."""
    if not rec["has_claude"]:
        ctx.say(f"{ctx.settings} not found: Claude Code left unconfigured")
        return
    added = "add the muninn SessionStart and UserPromptSubmit hooks to"
    if dry(
        ctx,
        (
            f"would remove the old pctx hook entries from {ctx.settings} and "
            f"{added} it"
            if ctx.legacy
            else f"would {added} {ctx.settings}"
        ),
    ):
        return
    frag = ctx.lib / "current/integrations/claude/settings-hooks.json"
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
    before = ctx.config.read_bytes()
    env = dict(os.environ, CODEX_HOME=str(ctx.codex_home))
    argv = ["codex", "plugin", "add", PLUGIN_ID, "--json"]
    out = json.loads(must(ctx, argv, env=env, quiet=True).stdout)
    # Codex rewrites config.toml itself; it may only have touched our keys.
    codex_check(before, ctx.config.read_bytes())
    # Read before the location check: if the pinned file is missing, that
    # failure must surface ahead of the "unexpected place" one.
    pinned = (ctx.lib / CODEX_HOOKS_PIN).read_bytes()
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
    source = f"{ctx.lib}/current/integrations/codex"
    if not rec["has_codex"]:
        ctx.say(f"{ctx.config} not found: Codex left unconfigured")
        return
    gone = (
        "remove the old provenance-context marketplace, plugin and hook "
        "trust entries, then "
        if ctx.legacy
        else ""
    )
    if dry(
        ctx,
        f"would {gone}add the muninn Codex plugin, enable it and trust its "
        f"hooks in {ctx.config}",
    ):
        return
    if ctx.legacy:
        _edit_config(ctx, drop_legacy)
    _edit_config(ctx, drop_trust)
    _edit_config(ctx, lambda text: repoint(text, source))
    pinned = _add_plugin(ctx, rec)
    _edit_config(ctx, enable)
    if rec["trust"] == "auto":
        _edit_config(ctx, lambda text: write_trust(text, codex_hooks(pinned)))
    else:
        ctx.say(OWNER_STEP)
