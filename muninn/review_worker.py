"""Run one fixed CLI with a bounded parent-pipe and deadline watchdog."""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

from muninn import review_managed_files
from muninn.review_json import canonical_json_value
from muninn.review_process_identity import identify
from muninn.review_process_protocol import (
    activation_size,
    decode_control,
    packet,
)
from muninn.review_process_windows import NativeChild, launch
from muninn.review_runtime_models import RuntimeIdentity
from muninn.review_runtime_records import identity_digest


class _Worker:
    """Keep parent liveness independent of nonblocking CLI and output pipes."""

    def __init__(
        self, deadline: float, input_limit: int, output_limit: int
    ) -> None:
        """Establish all bounds before sending readiness or accepting input."""
        self.deadline, self.input_limit, self.output_limit = (
            deadline,
            input_limit,
            output_limit,
        )
        self.incoming = bytearray()
        self.outgoing = bytearray(
            packet(
                b"R",
                canonical_json_value(
                    identify(os.getpid()).model_dump()
                ).encode(),
            )
        )
        self.expected = 12
        self.prompt = b""
        self.prompt_at = 0
        self.cli: subprocess.Popen[bytes] | NativeChild | None = None
        self.closed: set[int] = set()
        self.output_count = 0
        self.completed = False
        self.refused = False
        self.refresh_used = False
        self.execution: RuntimeIdentity | None = None
        os.set_blocking(0, False)
        os.set_blocking(1, False)

    def abort(self) -> None:
        """Kill the still-owned group, including this finite wrapper."""
        if isinstance(self.cli, NativeChild):
            self.cli.close()
        if os.name != "nt":
            os.killpg(os.getpgrp(), signal.SIGKILL)
        raise SystemExit(1)

    def _parent(self) -> None:
        """Detect EOF and reject duplicate or incomplete activation data."""
        amount = (
            1
            if self.cli is not None
            else min(4096, self.expected - len(self.incoming))
        )
        try:
            raw = os.read(0, max(1, amount))
        except BlockingIOError:
            return
        if not raw or self.cli is not None:
            self.abort()
        self.incoming.extend(raw)
        if len(self.incoming) >= 12:
            self.expected = activation_size(self.incoming, self.input_limit)
        if len(self.incoming) == self.expected and self.expected > 12:
            self._launch()

    def _launch(self) -> None:
        """Start the sole parent-selected CLI without inherited control FDs."""
        control, self.prompt = decode_control(
            bytes(self.incoming), self.input_limit
        )
        if control.replacement != self.refused or (
            self.refused and self.refresh_used
        ):
            raise ValueError("invalid_worker_phase")
        if control.replacement and control.refresh_count != 1:
            raise ValueError("invalid_worker_phase")
        self.refresh_used = bool(control.refresh_count)
        self.execution = control.execution
        argv, cwd, env = control.argv, control.cwd, control.env
        with contextlib.suppress(BlockingIOError):
            # EOF or a second frame invalidates the first before creation.
            os.read(0, 1)
            self.abort()
        self.incoming.clear()
        if self.execution is not None and not review_managed_files.matches(
            self.execution
        ):
            self._refuse("runtime_changed")
            return
        try:
            self._create(argv, cwd, env)
        except FileNotFoundError:
            if self.execution is None:
                raise
            self._refuse("exec_enoent")
            return
        assert self.cli is not None
        for pipe in (self.cli.stdin, self.cli.stdout, self.cli.stderr):
            assert pipe is not None
            os.set_blocking(pipe.fileno(), False)
        data: dict[str, object] = {
            "launched_at": time.time(),
            "identity": identify(self.cli.pid).model_dump(),
        }
        if self.execution is not None:
            data["identity_digest"] = identity_digest(self.execution)
        self.outgoing.extend(packet(b"L", canonical_json_value(data).encode()))

    def _create(
        self, argv: tuple[str, ...], cwd: Path, env: dict[str, str]
    ) -> None:
        """Keep native creation separate from proved pre-exec refusal."""
        self.cli = (
            launch(argv, cwd, env)
            if os.name == "nt"
            else subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                close_fds=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )

    def _refuse(self, code: Literal["runtime_changed", "exec_enoent"]) -> None:
        """Acknowledge no native creation before awaiting one replacement."""
        assert self.execution is not None and self.cli is None
        self.refused = True
        self.expected = 12
        self.prompt = b""
        self.outgoing.extend(
            packet(
                b"P",
                canonical_json_value(
                    {
                        "identity_digest": identity_digest(self.execution),
                        "code": code,
                    }
                ).encode(),
            )
        )

    def _input(self) -> None:
        """Feed bounded raw prompt bytes without blocking parent liveness."""
        assert self.cli is not None and self.cli.stdin is not None
        if self.cli.stdin.closed:
            return
        with contextlib.suppress(BlockingIOError):
            self.prompt_at += os.write(
                self.cli.stdin.fileno(),
                self.prompt[self.prompt_at : self.prompt_at + 4096],
            )
        if self.prompt_at == len(self.prompt):
            self.cli.stdin.close()
            self.prompt = b""

    def _output(self) -> None:
        """Drain bounded stdout and stderr chunks."""
        assert self.cli is not None
        if self.outgoing:
            return
        for tag, pipe in ((b"O", self.cli.stdout), (b"E", self.cli.stderr)):
            assert pipe is not None
            fd = pipe.fileno()
            if fd in self.closed:
                continue
            try:
                raw = os.read(fd, min(4096, self.output_limit + 1))
            except BlockingIOError:
                continue
            if not raw:
                self.closed.add(fd)
            else:
                self.output_count += len(raw)
                self.outgoing.extend(packet(tag, raw))
        if len(self.closed) == 2 and not self.outgoing:
            code = self.cli.poll()
            if code is not None:
                self.outgoing.extend(
                    packet(
                        b"T",
                        canonical_json_value({"exit_code": code}).encode(),
                    )
                )
                self.completed = True

    def run(self) -> None:
        """Check EOF and monotonic expiry between every bounded pipe turn."""
        while True:
            if time.monotonic() >= self.deadline:
                self.abort()
            if self.output_count > self.output_limit and not self.outgoing:
                self.abort()
            self._parent()
            if self.cli is not None and not self.completed:
                self._input()
                self._output()
            if self.outgoing:
                with contextlib.suppress(BlockingIOError):
                    sent = os.write(1, self.outgoing[:4096])
                    del self.outgoing[:sent]
            elif self.completed:
                if isinstance(self.cli, NativeChild):
                    self.cli.close()
                return
            time.sleep(0.005)


def main() -> int:
    """Require explicit finite limits without leaking payload errors."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--max-input-bytes", type=int, required=True)
    parser.add_argument("--max-output-bytes", type=int, required=True)
    args = parser.parse_args()
    if (
        not math.isfinite(args.deadline)
        or args.deadline <= time.monotonic()
        or args.max_input_bytes <= 0
        or args.max_output_bytes <= 0
    ):
        return 1
    worker = _Worker(
        args.deadline, args.max_input_bytes, args.max_output_bytes
    )
    try:
        worker.run()
        return 0
    except (OSError, ValueError, RecursionError):
        worker.abort()
        return 1


if __name__ == "__main__":
    sys.exit(main())
