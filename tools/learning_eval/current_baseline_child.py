"""Run only pinned legacy commands in an isolated standard-library child."""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import os
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import Any, cast

PIN_SHA256 = "18810960e37312324ff27554576e4122fbe65f3354df69a90f9c9a15a675b262"
SOURCE_COMMIT = "5c1a69b0edff81df80836d72a80971a91a8f9520"
INPUT_LIMIT = 1024 * 1024
OPERATIONS = ("ingest", "list", "check", "session-start", "prompt", "search")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate keys before an object loses their provenance."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    """Reject the nonfinite extensions accepted by the JSON decoder."""
    raise ValueError("nonfinite_json")


def _float(value: str) -> float:
    """Reject finite-looking JSON numbers that overflow Python floats."""
    result = float(value)
    if result in (float("inf"), -float("inf")):
        raise ValueError("nonfinite_json")
    return result


def decode(raw: bytes, limit: int = INPUT_LIMIT) -> dict[str, Any]:
    """Decode a bounded strict JSON object without permissive extensions."""
    if len(raw) > limit:
        raise ValueError("input_limit")
    value = json.loads(
        raw,
        object_pairs_hook=_pairs,
        parse_constant=_constant,
        parse_float=_float,
    )
    if not isinstance(value, dict):
        raise ValueError("object_required")
    return cast(dict[str, Any], value)


def confined(path: Path, boundary: Path) -> Path:
    """Reject aliases, symlinks and paths outside an absolute boundary."""
    if (
        not path.is_absolute()
        or not boundary.is_absolute()
        or path.resolve() != path
        or boundary.resolve() != boundary
        or not path.is_relative_to(boundary)
        or path == boundary
    ):
        raise ValueError("path_boundary")
    return path


def read_file(path: Path, limit: int) -> bytes:
    """Read a bounded regular file without following its final symlink."""
    if sys.platform == "win32":
        raise OSError("posix_baseline_required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("regular_file_required")
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("input_limit")
    return raw


def verify_snapshot(
    source: Path, boundary: Path, files: dict[str, str]
) -> None:
    """Bind the entire executable source tree, rejecting extra files."""
    confined(source, boundary)
    actual: set[str] = set()
    for path in source.rglob("*"):
        confined(path, boundary)
        if path.is_file():
            actual.add(path.relative_to(source).as_posix())
        elif not path.is_dir():
            raise ValueError("source_binding")
    if actual != set(files):
        raise ValueError("source_binding")
    for name, expected in files.items():
        path = confined(source / name, source)
        if (
            hashlib.sha256(read_file(path, INPUT_LIMIT)).hexdigest()
            != expected
        ):
            raise ValueError("source_binding")


def verify_pin(pin_path: Path, source: Path, boundary: Path) -> dict[str, Any]:
    """Verify fixed source/runtime authority before legacy imports."""
    confined(pin_path, boundary)
    raw = read_file(pin_path, INPUT_LIMIT)
    if hashlib.sha256(raw).hexdigest() != PIN_SHA256:
        raise ValueError("pin_binding")
    pin = decode(raw)
    executable = Path(pin["runtime"]["executable"])
    if (
        pin["source_commit"] != SOURCE_COMMIT
        or hashlib.sha256(read_file(executable, 128 * INPUT_LIMIT)).hexdigest()
        != pin["runtime"]["executable_sha256"]
    ):
        raise ValueError("runtime_binding")
    verify_snapshot(source, boundary, pin["tracked_runtime_files"])
    return pin


class BoundedOutput(io.StringIO):
    """Retain a bounded UTF-8 prefix and refuse further command output."""

    def __init__(self, limit: int) -> None:
        """Set a positive byte ceiling for captured CLI output."""
        super().__init__()
        self.limit = limit
        self.used = 0
        self.truncated = False

    def write(self, value: str) -> int:
        """Write complete UTF-8 characters up to the byte ceiling."""
        raw = value.encode()
        remaining = self.limit - self.used
        super().write(raw[:remaining].decode("utf-8", errors="ignore"))
        self.used += min(remaining, len(raw))
        if len(raw) > remaining:
            self.truncated = True
            raise ValueError("output_limit")
        return len(value)


def _deny_launch(event: str, args: tuple[object, ...]) -> None:
    """Deny network and process creation throughout legacy execution."""
    if event.startswith(
        ("socket.", "os.exec", "os.spawn", "os.fork")
    ) or event in {"subprocess.Popen", "os.system", "os.posix_spawn"}:
        raise PermissionError("child_launch_forbidden")


def _request(request: dict[str, Any]) -> None:
    """Require exactly the fixed operation and bounded scalar protocol."""
    required = {
        "operation",
        "pin_path",
        "source_root",
        "boundary",
        "family_root",
        "query",
        "session",
        "search_limit",
        "output_limit",
    }
    if set(request) != required or request.get("operation") not in OPERATIONS:
        raise ValueError("operation_invalid")
    for key in required - {"search_limit", "output_limit"}:
        if not isinstance(request[key], str) or len(request[key]) > 16384:
            raise ValueError("request_invalid")
    for key, maximum in (("search_limit", 100), ("output_limit", INPUT_LIMIT)):
        if type(request[key]) is not int or not 1 <= request[key] <= maximum:
            raise ValueError("request_invalid")


def _environment(request: dict[str, Any]) -> dict[str, str]:
    """Construct every provider, data and working path from one new root."""
    root = confined(Path(request["family_root"]), Path(request["boundary"]))
    paths = {
        "HOME": "home",
        "USERPROFILE": "home",
        "CODEX_HOME": "codex",
        "CLAUDE_CONFIG_DIR": "claude",
        "MUNINN_HOME": "store",
        "PWD": "cwd",
        "TMPDIR": "tmp",
    }
    env = {
        key: str(confined(root / name, root)) for key, name in paths.items()
    }
    env["MUNINN_ROOTS"] = json.dumps(
        {
            "codex-sessions": str(confined(root / "originals", root)),
            "codex-archived": str(confined(root / "archived", root)),
            "claude-projects": str(confined(root / "projects", root)),
        }
    )
    env["PATH"] = str(confined(root / "empty-bin", root))
    return env


def execute(request: dict[str, Any]) -> dict[str, Any]:
    """Execute one fixed public CLI command and preserve its exact output."""
    _request(request)
    source, boundary = Path(request["source_root"]), Path(request["boundary"])
    pin = verify_pin(Path(request["pin_path"]), source, boundary)
    if (
        Path(sys.executable).resolve() != Path(pin["runtime"]["executable"])
        or sys.version != pin["runtime"]["python"]
        or sqlite3.sqlite_version != pin["runtime"]["sqlite"]
        or any(k == "muninn" or k.startswith("muninn.") for k in sys.modules)
    ):
        raise ValueError("runtime_binding")
    env = _environment(request)
    os.environ.clear()
    os.environ.update(env)
    os.chdir(env["PWD"])
    sys.addaudithook(_deny_launch)
    sys.path.insert(0, str(source))
    cli = importlib.import_module("muninn.cli")
    module = str(Path(cli.__file__ or "").resolve())
    if module != str(source / "muninn" / "cli.py"):
        raise ValueError("import_binding")
    operation = request["operation"]
    commands = {
        "ingest": ["ingest"],
        "list": ["know", "list"],
        "check": ["know", "check"],
        "search": [
            "search",
            request["query"],
            "--limit",
            str(request["search_limit"]),
            "--include-current",
        ],
        "session-start": ["hook", "session-start", "--provider", "codex"],
        "prompt": ["hook", "prompt", "--provider", "codex"],
    }
    payload = json.dumps(
        {
            "cwd": env["PWD"],
            "session_id": request["session"],
            "prompt": request["query"],
        }
    ).encode()
    output, errors = BoundedOutput(request["output_limit"]), BoundedOutput(
        4096
    )
    started, code, error = time.monotonic(), None, None
    with (
        contextlib.redirect_stdout(output),
        contextlib.redirect_stderr(errors),
    ):
        original = sys.stdin
        try:
            sys.stdin = io.TextIOWrapper(io.BytesIO(payload))
            code = cli.main(commands[operation])
        except Exception as exc:
            error = (
                "output_limit"
                if isinstance(exc, ValueError) and str(exc) == "output_limit"
                else type(exc).__name__
            )
        finally:
            sys.stdin = original
    log_path = Path(env["MUNINN_HOME"]) / "calls.jsonl"
    trace = (
        decode(read_file(log_path, INPUT_LIMIT).splitlines()[-1])
        if log_path.exists()
        else {}
    )
    return {
        "trace": trace,
        "operation": operation,
        "exit_code": code,
        "error": error,
        "stdout": output.getvalue(),
        "stdout_bytes": len(output.getvalue().encode()),
        "stdout_sha256": hashlib.sha256(
            output.getvalue().encode()
        ).hexdigest(),
        "stderr": errors.getvalue(),
        "truncated": output.truncated,
        "elapsed_seconds": time.monotonic() - started,
        "module_path": module,
        "python": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "source_commit": SOURCE_COMMIT,
        "executable_sha256": pin["runtime"]["executable_sha256"],
    }


def main() -> int:
    """Read a single bounded request and emit one result or refusal."""
    try:
        result = execute(decode(sys.stdin.buffer.read(INPUT_LIMIT + 1)))
    except Exception as exc:
        result = {"error": type(exc).__name__, "code": "child_refused"}
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0 if "operation" in result else 2


if __name__ == "__main__":
    raise SystemExit(main())
