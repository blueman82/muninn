"""Select rendered native hook routes independently of desktop discovery."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from install.context import Ctx
from install.trust import codex_hooks
from muninn import platform_windows


def context_calls(ctx: Ctx, env: dict[str, str]) -> list[str | list[str]]:
    """Select only startup and prompt routes that return cited context."""
    settings: dict[str, Any] = json.loads(ctx.settings.read_bytes())
    calls: list[str | list[str]] = [
        (
            [handler["command"], *handler["args"]]
            if "args" in handler
            else shlex.split(handler["command"])
        )
        for event in ("SessionStart", "UserPromptSubmit")
        for group in settings["hooks"][event]
        for handler in group["hooks"]
    ]
    assert len(calls) == 2, "rendered Claude context routes missing"
    for hooks_path in sorted(ctx.cache.glob("*/hooks/hooks.json")):
        for hook in codex_hooks(
            hooks_path.read_bytes(), platform=ctx.platform
        ):
            if hook["suffix"].split(":")[0] not in {
                "session_start",
                "user_prompt_submit",
            }:
                continue
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
    return calls
