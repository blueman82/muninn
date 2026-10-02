"""The installer's run context and the process helpers built on it.

Every external effect (launchctl, ps, git, pctx, codex) goes through
``Ctx.run``, so tests can rehearse a whole install in a temp HOME with a fake
runner and a fake clock.
"""

from __future__ import annotations

import dataclasses
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, TypedDict

from install.constants import LABEL, MKT_NAME, PLIST


class StepFailedError(Exception):
    """An install step could not complete; rollback follows."""


class Runner(Protocol):
    """A command runner: real subprocesses, or a fake in tests."""

    def __call__(
        self,
        argv: Sequence[str | Path],
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run ``argv`` and return the finished process."""
        ...


class Job(TypedDict):
    """A loaded launchd job.

    Attributes:
        pid: The job's process id, or None when loaded but not running.
        cmd: The process's command line, empty when there is no pid.
    """

    pid: int | None
    cmd: str


def run_real(
    argv: Sequence[str | Path],
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a command for real, capturing output.

    Args:
        argv: Program and arguments; paths are converted to strings.
        env: Full environment, or None to inherit.
        input: Bytes for stdin.

    Returns:
        The finished process; the caller inspects the exit status.
    """
    text_argv = [str(a) for a in argv]
    # The timeout is a backstop against a hung tool, not a tuned deadline.
    return subprocess.run(
        text_argv, env=env, input=input, capture_output=True, timeout=600
    )


@dataclasses.dataclass
class Ctx:
    """Everything one install or rollback run needs.

    Attributes:
        home: The HOME being installed into.
        run: Command runner; every external effect goes through it.
        ts: UTC timestamp that names this run's directories.
        uid: User id for launchd's per-user ``gui/<uid>`` domain.
        dry_run: Plan and report without changing anything.
        now: Clock; replaced in tests.
        sleep: Sleep; replaced in tests.
        say: Progress sink.
        probe: Asks Codex which hooks it resolves; None skips the check.
        fresh: A new machine: no data, plist or release yet.
        upgrade: Already on pctx: re-pin, restart, prune.
        data: The pctx data directory.
        rdir: This run's rollback-record directory.
        failed: Where a rollback moves new artefacts instead of deleting.
        lib: Directory holding pinned releases and the links.
        pctx: The ``pctx`` command link.
        plist: The launchd plist path.
        settings: Claude Code's settings.json.
        codex_home: Codex's home directory.
        config: Codex's config.toml.
        cache: Codex's plugin cache for our plugin.
        target: The launchd service target.
    """

    home: Path
    run: Runner
    ts: str
    uid: int = os.getuid()
    dry_run: bool = False
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    say: Callable[[str], None] = print
    probe: Callable[[Ctx], list[dict[str, Any]]] | None = None
    fresh: bool = False
    upgrade: bool = False
    data: Path = dataclasses.field(init=False)
    rdir: Path = dataclasses.field(init=False)
    failed: Path = dataclasses.field(init=False)
    lib: Path = dataclasses.field(init=False)
    pctx: Path = dataclasses.field(init=False)
    plist: Path = dataclasses.field(init=False)
    settings: Path = dataclasses.field(init=False)
    codex_home: Path = dataclasses.field(init=False)
    config: Path = dataclasses.field(init=False)
    cache: Path = dataclasses.field(init=False)
    target: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        """Derive every path from ``home`` and ``ts`` once."""
        h, share = self.home, self.home / ".local/share"
        self.data = share / "provenance-context"
        self.rdir = share / f"provenance-context-install-{self.ts}"
        self.failed = share / f"provenance-context-failed-{self.ts}"
        self.lib = h / ".local/lib/provenance-context"
        self.pctx = h / ".local/bin/pctx"
        self.plist = h / PLIST
        self.settings = h / ".claude/settings.json"
        self.codex_home = h / ".codex"
        self.config = h / ".codex/config.toml"
        self.cache = (
            h / ".codex/plugins/cache" / MKT_NAME / "provenance-context"
        )
        self.target = f"gui/{self.uid}/{LABEL}"


def install_log(
    path: Path, run: Runner = run_real
) -> tuple[Callable[[str], None], Runner]:
    """Build a ``say`` and a ``run`` that also append to a log file.

    The log is 0600 and timestamped. It records messages, each command's
    name and exit status, and a failed command's stderr tail. It never
    records stdout, which can carry transcript paths or text.

    Args:
        path: The log file; its directory is created on demand.
        run: The runner to wrap.

    Returns:
        ``(say, run)``: a printer that also logs, and the logging runner.
    """

    def say(text: str) -> None:
        print(text)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create with 0600 atomically; chmod afterwards would leave a window.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(f"{stamp} {text}\n")

    def logged(
        argv: Sequence[str | Path],
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        r = run(argv, env=env, input=input)
        name = " ".join(str(a) for a in argv[:2])
        say(f"run {name} -> {r.returncode}")
        if r.returncode:
            say("  stderr: " + r.stderr.decode(errors="replace")[-300:])
        return r

    return say, logged


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


def job(ctx: Ctx) -> Job | None:
    """Describe the launchd job for our label.

    Args:
        ctx: The run context.

    Returns:
        None when the label is not loaded, otherwise its pid and command.
    """
    r = ctx.run(["launchctl", "print", ctx.target])
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


def is_new(ctx: Ctx, j: Job | None) -> bool:
    """Say whether a job is running from the new pinned release."""
    return bool(j and j["pid"] and f"{ctx.lib}/" in j["cmd"])


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
        ctx.say(f"DRY-RUN {text}")
    return ctx.dry_run
