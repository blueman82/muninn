"""The installer's run context and the process helpers built on it.

Every external effect (launchctl, ps, git, muninn, codex) goes through
``Ctx.run``, so tests can rehearse a whole install in a temp HOME with a fake
runner and a fake clock.
"""

from __future__ import annotations

import dataclasses
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypedDict

from install.constants import (
    LABEL,
    MKT_NAME,
    PLIST,
    REMOVED_PREFIX,
)
from install.installer_log import Runner as Runner
from install.installer_log import install_log as install_log
from install.installer_log import run_real as run_real
from muninn.obs_linux_service import query_unit
from muninn.obs_service import (
    command_names_path,
    parse_process,
    process_command,
)
from muninn.obs_status import read_status
from muninn.platform_paths import read_selection, windows_base


class StepFailedError(Exception):
    """An install step could not complete; rollback follows."""


class Job(TypedDict):
    """A loaded launchd job.

    Attributes:
        pid: The job's process id, or None when loaded but not running.
        cmd: The process's command line, empty when there is no pid.
    """

    pid: int | None
    cmd: str


@dataclasses.dataclass
class Ctx:
    """Everything one install or rollback run needs.

    Attributes:
        home: The HOME being installed into.
        run: Command runner; every external effect goes through it.
        ts: UTC timestamp that names this run's directories.
        platform: Native platform, overridden only by isolated tests.
        default_home: Use native environment paths when HOME was omitted.
        log_path: Optional CLI log activated only after successful preflight.
        uid: User id for launchd's per-user ``gui/<uid>`` domain.
        dry_run: Plan and report without changing anything.
        now: Clock; replaced in tests.
        sleep: Sleep; replaced in tests.
        say: Progress sink.
        probe: Asks Codex which hooks it resolves; None skips the check.
        fresh: A new machine: no data, plist or release yet.
        upgrade: Already on muninn: re-pin, restart, prune.
        data: The muninn data directory.
        rdir: This run's rollback-record directory.
        failed: Where a rollback moves new artefacts instead of deleting.
        removed: Where an uninstall moves the data dir instead of deleting.
        lib: Directory holding pinned releases and the links.
        muninn: The ``muninn`` command link.
        plist: The launchd plist path.
        settings: Claude Code's settings.json.
        cursor_settings: Cursor's hooks.json.
        cursor_hooks: Whether this install manages Cursor's preCompact hook.
        codex_home: Codex's home directory.
        config: Codex's config.toml.
        cache: Codex's plugin cache for our plugin.
        target: The launchd service target.
    """

    home: Path
    run: Runner
    ts: str
    uid: int = getattr(os, "getuid", lambda: 0)()
    platform: str = sys.platform
    default_home: bool = False
    log_path: Path | None = None
    dry_run: bool = False
    cursor_hooks: bool = False
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    say: Callable[[str], None] = print
    probe: Callable[[Ctx], list[dict[str, Any]]] | None = None
    fresh: bool = False
    upgrade: bool = False
    data: Path = dataclasses.field(init=False, repr=False, compare=False)
    rdir: Path = dataclasses.field(init=False, repr=False, compare=False)
    failed: Path = dataclasses.field(init=False, repr=False, compare=False)
    removed: Path = dataclasses.field(init=False, repr=False, compare=False)
    lib: Path = dataclasses.field(init=False, repr=False, compare=False)
    muninn: Path = dataclasses.field(init=False, repr=False, compare=False)
    plist: Path = dataclasses.field(init=False, repr=False, compare=False)
    settings: Path = dataclasses.field(init=False, repr=False, compare=False)
    cursor_settings: Path = dataclasses.field(
        init=False, repr=False, compare=False
    )
    codex_home: Path = dataclasses.field(init=False, repr=False, compare=False)
    config: Path = dataclasses.field(init=False, repr=False, compare=False)
    cache: Path = dataclasses.field(init=False, repr=False, compare=False)
    target: str = dataclasses.field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Derive every path from ``home`` and ``ts`` once."""
        h, share = self.home, self.home / ".local/share"
        self.data = share / "muninn"
        self.rdir = share / f"muninn-install-{self.ts}"
        self.failed = share / f"muninn-failed-{self.ts}"
        self.removed = share / f"{REMOVED_PREFIX}{self.ts}"
        self.lib = h / ".local/lib/muninn"
        self.muninn = h / ".local/bin/muninn"
        self.plist = h / PLIST
        self.settings = h / ".claude/settings.json"
        self.cursor_settings = h / ".cursor/hooks.json"
        self.codex_home = h / ".codex"
        if self.default_home:
            if claude_home := os.environ.get("CLAUDE_CONFIG_DIR"):
                self.settings = Path(claude_home) / "settings.json"
            if codex_home := os.environ.get("CODEX_HOME"):
                self.codex_home = Path(codex_home)
        self.config = self.codex_home / "config.toml"
        self.cache = self.codex_home / "plugins/cache" / MKT_NAME / "muninn"
        self.target = f"gui/{self.uid}/{LABEL}"
        if self.platform == "linux":
            self.plist = h / ".config/systemd/user/muninn.service"
            self.target = "muninn.service"
        elif self.platform == "win32":
            base = windows_base(
                os.environ, home=None if self.default_home else h
            )
            self.data, self.lib = base / "data", base / "lib"
            self.rdir = base / f"install-{self.ts}"
            self.failed = base / f"failed-{self.ts}"
            self.removed = base / f"{REMOVED_PREFIX}{self.ts}"
            self.muninn = base / "bin/muninn.cmd"
            self.plist = base / "task.xml"
            self.target = "Muninn"

    @property
    def release(self) -> Path:
        """The current confined release without requiring a symlink.

        Raises:
            StepFailedError: If the native release selection is absent.
        """
        if self.platform != "win32":
            return self.lib / "current"
        selected = read_selection(self.lib.parent)
        if selected is None:
            raise StepFailedError("no installed release selection")
        return selected[0]


def must(
    ctx: Ctx,
    argv: Sequence[str | Path],
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,
    quiet: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Run a command and require exit status 0.

    Args:
        ctx: The run context.
        argv: Program and arguments.
        env: Full environment, or None to inherit.
        input: Bytes for stdin.
        quiet: Keep stderr out of the error message.

    Returns:
        The finished process.

    Raises:
        StepFailedError: If the command exits non-zero.
    """
    r = ctx.run(argv, env=env, input=input)
    if r.returncode:
        tail = "" if quiet else r.stderr.decode(errors="replace")[-300:]
        name = f"{Path(str(argv[0])).name} {argv[1]}"
        raise StepFailedError(f"{name} exited {r.returncode} {tail}".strip())
    return r


def wait(
    ctx: Ctx, cond: Callable[[], bool], seconds: float, what: str
) -> None:
    """Poll once a second until ``cond`` holds.

    Args:
        ctx: The run context (supplies the clock and sleep).
        cond: Returns True when the wait is over.
        seconds: Give up after this long.
        what: The error message if the deadline passes.

    Raises:
        StepFailedError: If ``cond`` is still false at the deadline.
    """
    deadline = ctx.now() + seconds
    while not cond():
        if ctx.now() >= deadline:
            raise StepFailedError(what)
        ctx.sleep(1)


def job(ctx: Ctx, target: str | None = None) -> Job | None:
    """Describe a launchd job, by default the one for our label.

    Args:
        ctx: The run context.
        target: The service target.

    Returns:
        None when the label is not loaded, otherwise its pid and command.
    """
    if ctx.platform == "linux":
        return _linux_job(ctx, target)
    if ctx.platform == "win32":
        return _windows_job(ctx)
    r = ctx.run(["launchctl", "print", target or ctx.target])
    if r.returncode:
        return None
    m = re.search(rb"^\s*pid = (\d+)\s*$", r.stdout, re.M)
    if not m:
        return {"pid": None, "cmd": ""}
    ps = ctx.run(["ps", "-ww", "-o", "command=", "-p", m[1].decode()])
    return {
        "pid": int(m[1]),
        "cmd": ps.stdout.decode(errors="replace").strip(),
    }


# Referenced as a value, not called as ``os.readlink(...)``: the lint rule
# that rewrites that call to ``Path.readlink`` would change the stored text.
_readlink: Callable[[Path], str] = os.readlink


def link_text(link: Path) -> str:
    """Return a symlink's target exactly as stored.

    ``Path.readlink`` normalises the text (``a//b/`` becomes ``a/b``); the
    rollback record and the prune and undo comparisons need the raw value.
    """
    return _readlink(link)


def is_new(ctx: Ctx, j: Job | None) -> bool:
    """Say whether a job is running from the new pinned release."""
    if not (j and j["pid"]):
        return False
    if ctx.platform == "win32":
        return command_names_path(j["cmd"], ctx.lib)
    return str(ctx.lib) in j["cmd"]


def dry(ctx: Ctx, text: str) -> bool:
    """Report a planned action in dry-run mode.

    Args:
        ctx: The run context.
        text: Description of what the step would do.

    Returns:
        True in dry-run mode, so a step can ``return`` early; steps skip
        their real work exactly when this is true.
    """
    if ctx.dry_run:
        ctx.say(text)
    return ctx.dry_run


def _linux_job(ctx: Ctx, target: str | None) -> Job | None:
    """Distinguish an owned loaded unit from validated native absence."""
    try:
        values = query_unit(ctx.run, target or ctx.target)
        if values["LoadState"] == "not-found":
            if values != {
                "LoadState": "not-found",
                "ActiveState": "inactive",
                "MainPID": "0",
                "FragmentPath": "",
                "DropInPaths": "",
            }:
                raise ValueError("service_absence_unknown")
            return None
        if (
            values["LoadState"] != "loaded"
            or not ctx.plist.is_file()
            or values["FragmentPath"] != str(ctx.plist)
            or values["DropInPaths"]
        ):
            raise ValueError("service_registration_unowned")
        pid = int(values["MainPID"])
    except (OSError, ValueError) as exc:
        raise StepFailedError(
            "cannot establish the owned user service"
        ) from exc
    if not pid:
        return {"pid": None, "cmd": ""}
    ps = ctx.run(["ps", "-ww", "-o", "command=", "-p", str(pid)])
    return {"pid": pid, "cmd": ps.stdout.decode(errors="replace").strip()}


def _windows_job(ctx: Ctx) -> Job | None:
    """Inspect the actual heartbeat process, never the task engine PID."""
    status = read_status(ctx.data)
    pid = status.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    result = ctx.run(process_command(pid))
    if result.returncode:
        return None
    process = parse_process(result.stdout)
    command = process["cmd"]
    return {"pid": pid, "cmd": command if isinstance(command, str) else ""}
