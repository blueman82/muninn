"""Measure the Cursor preCompact hook with synthetic local databases."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing, suppress
from pathlib import Path
from typing import Any

from muninn import store

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin" / "muninn"
HOOKS = ROOT / "integrations" / "cursor" / "hooks.json"


def _hook_timeout() -> int:
    """Read the configured Cursor hook timeout."""
    document: Any = json.loads(HOOKS.read_text(encoding="utf-8"))
    [handler] = document["hooks"]["preCompact"]
    return int(handler["timeout"])


def _timeouts(override: int | None) -> tuple[int, int]:
    """Return the configured and benchmark process timeouts in seconds."""
    configured = _hook_timeout()
    return configured, configured if override is None else override


def _insert_conversation(
    conn: sqlite3.Connection,
    thread_id: str,
    bubbles: int,
    message_chars: int,
) -> None:
    """Insert one synthetic Cursor composer and its visible bubbles."""
    headers = [{"bubbleId": f"b{index}"} for index in range(bubbles)]
    composer = json.dumps({"fullConversationHeadersOnly": headers})
    conn.execute(
        "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)",
        (f"composerData:{thread_id}", composer),
    )
    text = "x" * message_chars
    rows = [
        (
            f"bubbleId:{thread_id}:b{index}",
            json.dumps({"type": 1 + index % 2, "text": text}),
        )
        for index in range(bubbles)
    ]
    conn.executemany(
        "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)", rows
    )


def _create_database(
    path: Path,
    background_conversations: int,
    messages: int,
    message_chars: int,
) -> None:
    """Build a synthetic Cursor database; no provider files are read."""
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)"
        )
        for index in range(background_conversations):
            _insert_conversation(conn, f"background-{index}", 2, 64)
        _insert_conversation(conn, "active", messages, message_chars)


def _lock_holder(path: Path, delay_ms: int, hold_ms: int) -> int:
    """Acquire Muninn's writer lock after a delay, then hold it briefly."""
    if delay_ms:
        print("ready", flush=True)
        time.sleep(delay_ms / 1000)
    with store.writer_lock(path.parent):
        if not delay_ms:
            print("ready", flush=True)
        time.sleep(hold_ms / 1000)
    return 0


def _hold_lock_child(path: Path, delay_ms: int, hold_ms: int) -> None:
    """Run the private lock-holder subprocess mode."""
    raise SystemExit(_lock_holder(path, delay_ms, hold_ms))


def _environment(home: Path, store_home: Path) -> dict[str, str]:
    """Return a process environment isolated from user data."""
    return {
        **{
            k: os.environ[k]
            for k in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP")
            if k in os.environ
        },
        "USERPROFILE": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MUNINN_HOME": str(store_home),
        "MUNINN_PYTHON": sys.executable,
        "CURSOR_PROJECT_DIR": str(home / "project"),
    }


def _run_sample(
    database: Path,
    root: Path,
    sample: int,
    messages: int,
    lock_delay_ms: int,
    lock_hold_ms: int,
    hook_timeout: int,
) -> tuple[float, str]:
    """Time one real hook subprocess with a fresh isolated store."""
    store_home = root / f"store-{sample}"
    env = _environment(root / "home", store_home) | {
        "MUNINN_CURSOR_DB": str(database)
    }
    Path(env["CURSOR_PROJECT_DIR"]).mkdir(parents=True, exist_ok=True)
    with closing(store.connect_rw(store.db_path(store_home))):
        pass
    holder: subprocess.Popen[bytes] | None = None
    if lock_hold_ms:
        holder = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tools.benchmark_cursor_precompact",
                "--hold-lock",
                str(store_home / "writer.lock"),
                str(lock_delay_ms),
                str(lock_hold_ms),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if holder.stdout is None or holder.stdout.readline() != b"ready\n":
            _, error = holder.communicate()
            raise RuntimeError(
                f"writer lock holder failed: {error.decode()[:200]}"
            )
    payload = json.dumps(
        {
            "cwd": env["CURSOR_PROJECT_DIR"],
            "workspace_roots": [env["CURSOR_PROJECT_DIR"]],
            "conversation_id": "active",
            "message_count": messages,
        }
    ).encode()
    started = time.perf_counter()
    result: subprocess.CompletedProcess[bytes] | None = None
    try:
        with suppress(subprocess.TimeoutExpired):
            result = subprocess.run(
                [
                    str(
                        LAUNCHER.with_suffix(".cmd")
                        if sys.platform == "win32"
                        else LAUNCHER
                    ),
                    "hook",
                    "pre-compact",
                    "--provider",
                    "cursor",
                ],
                input=payload,
                capture_output=True,
                env=env,
                timeout=hook_timeout,
            )
        elapsed = time.perf_counter() - started
    finally:
        if holder is not None:
            holder.wait(
                timeout=max(
                    hook_timeout, (lock_delay_ms + lock_hold_ms) / 1000 + 1
                )
            )
    if result is None:
        return elapsed, "timed_out"
    if result.returncode != 0:
        raise RuntimeError(f"hook exited {result.returncode}")
    output: Any = json.loads(result.stdout)
    if not isinstance(output, dict):
        raise RuntimeError("hook returned a non-object response")
    return elapsed, "hook_notice" if "user_message" in output else "completed"


def _percentile95(samples: list[float]) -> float:
    """Return the nearest-rank 95th percentile in seconds."""
    if not samples:
        raise ValueError("at least one timing sample is required")
    ordered = sorted(samples)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def _positive(parser: argparse.ArgumentParser, value: str) -> int:
    """Parse a positive integer benchmark setting."""
    try:
        parsed = int(value)
    except ValueError:
        parser.error(f"expected an integer: {value}")
    if parsed < 1:
        parser.error("value must be at least 1")
    return parsed


def _nonnegative(parser: argparse.ArgumentParser, value: str) -> int:
    """Parse a non-negative integer benchmark setting."""
    try:
        parsed = int(value)
    except ValueError:
        parser.error(f"expected an integer: {value}")
    if parsed < 0:
        parser.error("value must not be negative")
    return parsed


def _parser() -> argparse.ArgumentParser:
    """Create the benchmark command parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs", type=lambda v: _positive(parser, v), required=True
    )
    parser.add_argument(
        "--background-conversations",
        type=lambda v: _nonnegative(parser, v),
        required=True,
    )
    parser.add_argument(
        "--messages", type=lambda v: _positive(parser, v), required=True
    )
    parser.add_argument(
        "--message-chars", type=lambda v: _positive(parser, v), required=True
    )
    parser.add_argument(
        "--lock-delay-ms", type=lambda v: _nonnegative(parser, v), default=0
    )
    parser.add_argument(
        "--lock-hold-ms", type=lambda v: _nonnegative(parser, v), default=0
    )
    parser.add_argument(
        "--timeout-s", type=lambda v: _positive(parser, v), default=None
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run repeated synthetic preCompact timings and print JSON results."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 4 and args[0] == "--hold-lock":
        _hold_lock_child(Path(args[1]), int(args[2]), int(args[3]))
    parsed = _parser().parse_args(args)
    configured_timeout, timeout = _timeouts(parsed.timeout_s)
    if parsed.lock_delay_ms and not parsed.lock_hold_ms:
        _parser().error("--lock-delay-ms requires --lock-hold-ms")
    with tempfile.TemporaryDirectory(prefix="muninn-cursor-bench-") as tmp:
        root = Path(tmp)
        home = root / "home"
        database = home / (
            "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
        )
        database.parent.mkdir(parents=True)
        _create_database(
            database,
            parsed.background_conversations,
            parsed.messages,
            parsed.message_chars,
        )
        outcomes = [
            _run_sample(
                database,
                root,
                sample,
                parsed.messages,
                parsed.lock_delay_ms,
                parsed.lock_hold_ms,
                timeout,
            )
            for sample in range(parsed.runs)
        ]
        database_bytes = database.stat().st_size
    samples = [elapsed for elapsed, _ in outcomes]
    p95 = _percentile95(samples)
    result_counts = {
        name: sum(status == name for _, status in outcomes)
        for name in ("completed", "hook_notice", "timed_out")
    }
    result = {
        "synthetic_only": True,
        "filesystem_cache": "uncontrolled; database reused across runs",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "runs": parsed.runs,
        "background_conversations": parsed.background_conversations,
        "active_messages": parsed.messages,
        "message_chars": parsed.message_chars,
        "synthetic_database_bytes": database_bytes,
        "lock_delay_ms": parsed.lock_delay_ms,
        "lock_hold_ms": parsed.lock_hold_ms,
        "configured_timeout_s": configured_timeout,
        "tested_timeout_s": timeout,
        "elapsed_s": [round(sample, 3) for sample in samples],
        "p95_s": round(p95, 3),
        "max_s": round(max(samples), 3),
        "runs_at_or_over_timeout": sum(
            status == "timed_out" or elapsed >= timeout
            for elapsed, status in outcomes
        ),
        "runs_completed": result_counts["completed"],
        "runs_with_hook_notice": result_counts["hook_notice"],
        "runs_timed_out": result_counts["timed_out"],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
