"""Actual isolated Codex plugin installation and effective hooks/list proof."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

from install import configedit
from install.constants import PLUGIN_ID
from install.context import Ctx
from install.probe import codex_probe
from install.provider_paths import render_pinned
from install.transforms import drop_trust, enable, repoint, write_trust
from install.trust import codex_hooks
from muninn import platform_io
from tests.ingest_support import ROOT


def prove_codex(ctx: Ctx, env: dict[str, str]) -> dict[str, Any]:
    """Prove plugin cache, effective Windows override and trust with Codex.

    Args:
        ctx: Private synthetic installation context.
        env: Isolated provider homes and native executable PATH.

    Returns:
        Provider version, count and source/cache/trust equality booleans.
    """
    executable = shutil.which("codex.exe") or shutil.which("codex")
    assert executable is not None, "pinned native Codex missing"
    version = (
        subprocess.run(
            [executable, "--version"], env=env, capture_output=True, check=True
        )
        .stdout.decode()
        .strip()
    )
    assert version == "codex-cli 0.159.2", version
    source = ctx.release / "integrations/codex"
    shutil.copytree(ROOT / "integrations/codex", source)
    hooks = source / "hooks/hooks.json"
    rendered = render_pinned(
        ctx, "integrations/codex/hooks/hooks.json", hooks.read_bytes()
    )
    configedit.atomic_write(hooks, rendered, 0o600)
    text = enable(repoint("", str(source)))
    configedit.atomic_write(ctx.config, text.encode(), 0o600)
    done = subprocess.run(
        [executable, "plugin", "add", PLUGIN_ID, "--json"],
        env=env,
        cwd=ctx.home,
        capture_output=True,
        check=True,
    )
    result = json.loads(done.stdout)
    installed = Path(result["installedPath"]).resolve()
    assert installed == (ctx.cache / result["version"]).resolve()
    cached = installed / "hooks/hooks.json"
    assert cached.read_bytes() == rendered
    wanted = codex_hooks(rendered, platform=ctx.platform)
    platform_io.ensure_private_dir(ctx.codex_home)
    configedit.edit_file(
        ctx.config,
        lambda data: write_trust(
            enable(drop_trust(data.decode())), wanted
        ).encode(),
        lambda before, after: None,
    )
    with mock.patch.dict("os.environ", env, clear=True):
        entries = codex_probe(ctx)
    got = [entry for entry in entries if entry.get("pluginId") == PLUGIN_ID]
    expected = {
        PLUGIN_ID + ":hooks/hooks.json:" + hook["suffix"]: hook
        for hook in wanted
    }
    assert {entry["key"] for entry in got} == set(expected)
    for entry in got:
        hook = expected[entry["key"]]
        assert entry["command"] == hook["command"]
        assert entry["currentHash"] == hook["hash"]
        assert entry["trustStatus"] == "trusted"
        assert Path(entry["sourcePath"]).resolve() == cached.resolve()
    return {
        "version": version,
        "hooks": len(got),
        "cache_matches": True,
        "effective_hashes_match": True,
        "trusted": True,
    }
