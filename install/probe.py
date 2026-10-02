"""Ask the real Codex app-server which hooks it resolves."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from typing import IO, Any, cast

from install.context import Ctx


def _call(
    stdin: IO[bytes],
    lines: queue.Queue[bytes],
    request: tuple[int, str, dict[str, Any]],
    timeout: float,
) -> dict[str, Any]:
    """Send one JSON-RPC request and wait for its answer.

    Args:
        stdin: The app-server's stdin.
        lines: Lines read from its stdout by a background thread.
        request: ``(id, method, params)``.
        timeout: Seconds to wait for each line.

    Returns:
        The response message carrying the same id.

    Raises:
        queue.Empty: If no line arrives within ``timeout``.
        OSError: If the server has exited and its stdin is closed.
    """
    msg_id, method, params = request
    payload = {"id": msg_id, "method": method, "params": params}
    stdin.write(json.dumps(payload).encode() + b"\n")
    stdin.flush()
    while True:
        try:
            msg = json.loads(lines.get(timeout=timeout))
        except ValueError:
            continue  # the server may log non-JSON lines; skip them
        if msg.get("id") == msg_id:
            return msg


def codex_probe(ctx: Ctx, timeout: float = 60) -> list[dict[str, Any]]:
    """Ask the real Codex app-server which hooks it resolves (read-only).

    Args:
        ctx: The run context.
        timeout: Seconds to wait for each line of the server's output.

    Returns:
        The hook entries Codex reports for ``ctx.home``.

    Raises:
        OSError: If ``codex`` is missing or its stdin is closed.
        queue.Empty: If the server stays silent past ``timeout``.
        KeyError: If a reply lacks ``result``, ``data`` or ``hooks``.
        subprocess.TimeoutExpired: If the server will not exit when closed.
    """
    env = dict(os.environ, CODEX_HOME=str(ctx.codex_home))
    p = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=ctx.home,
    )
    # Popen types the pipes as optional; both were requested as PIPE above.
    stdin = cast(IO[bytes], p.stdin)
    stdout = cast(IO[bytes], p.stdout)
    lines: queue.Queue[bytes] = queue.Queue()
    # Read on a thread so a silent server cannot block us past the timeout.
    threading.Thread(
        target=lambda: [lines.put(x) for x in stdout], daemon=True
    ).start()
    try:
        client = {"clientInfo": {"name": "pctx-install", "version": "1"}}
        _call(stdin, lines, (1, "initialize", client), timeout)
        stdin.write(b'{"method": "initialized"}\n')
        listing = (2, "hooks/list", {"cwds": [str(ctx.home)]})
        result = _call(stdin, lines, listing, timeout)["result"]
    finally:
        p.terminate()
        p.wait(timeout=10)
    return [h for entry in result["data"] for h in entry["hooks"]]
