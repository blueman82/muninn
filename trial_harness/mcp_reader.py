"""One-reader stdio MCP server exposing ``trial_tool``.

Implements the wrapper duties of trial-protocol §1.4, the §1.5 safety
ceiling and the §4.1 hash-chained per-call log.  One process serves one
reader session; requests are handled strictly one at a time, so a reader
never has more than one call in flight.

Usage: python3.13 -I -B mcp_reader.py BINDING_JSON

The binding file (mode 0600, written by launch.py) binds this process to a
single unit: unit_id, arm, qid, scope, token_sha256, agent_id, log_path,
t_launch, max_calls, max_seconds, call_timeout_s, exec_cwd, and either
``old`` {python, cli, env} or ``new`` {argv, env, command_classes}.
Standard library only.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import importlib.util
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

TOOL = "trial_tool"
GENESIS = "0" * 64
CEILING = '{"error":"call limit reached; give your final answer now"}'
OLD_CLASSES = {"files": "listing", "events": "listing", "open": "open"}
OLD_FIELDS = (
    "provider",
    "source_path",
    "source_line",
    "source_ordinal",
    "source_hash",
)
NEW_ENV_KEYS = frozenset({"PATH", "HOME", "TRIAL_INDEX"})
NEW_ID_KEYS = ("provider", "source_path", "line", "byte_offset")
REQUIRED = (
    "unit_id",
    "arm",
    "qid",
    "scope",
    "token_sha256",
    "agent_id",
    "log_path",
    "t_launch",
    "max_calls",
    "max_seconds",
    "call_timeout_s",
    "exec_cwd",
)
# Identical for both arms: usage lives only in the frozen prompt (§1.4).
TOOL_SPEC = {
    "name": TOOL,
    "description": (
        "Evidence tool for this task. Its commands are described in the "
        "task prompt."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "token": {"type": "string", "description": "Session token."},
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Command name, then its arguments.",
            },
        },
        "required": ["token", "args"],
        "additionalProperties": False,
    },
}


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def utc_ms() -> str:
    stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
    return stamp.replace("+00:00", "Z")


def error_json(message: str) -> str:
    return json.dumps({"error": message})


def validate(binding: dict) -> dict:
    missing = [key for key in REQUIRED if key not in binding]
    if missing:
        raise ValueError(f"binding lacks {missing}")
    arm = binding["arm"]
    if arm == "OLD":
        if not Path(binding["old"]["cli"]).is_file():
            raise ValueError("OLD cli missing")
    elif arm == "NEW":
        keys = set(binding["new"]["env"])
        if keys != NEW_ENV_KEYS:
            raise ValueError(f"NEW env must be exactly {sorted(NEW_ENV_KEYS)}")
    else:
        raise ValueError(f"unknown arm {arm!r}")
    return binding


def last_record(log_path: Path) -> tuple[int, str]:
    """Return (seq, sha256) of the last log line, for chain continuity."""
    if not log_path.exists():
        return 0, GENESIS
    lines = log_path.read_bytes().splitlines()
    if not lines:
        return 0, GENESIS
    return json.loads(lines[-1])["seq"], sha256_hex(lines[-1])


def verify_chain(log_path: Path) -> int:
    """Check seq order and prev_sha256 links; return the record count."""
    previous = GENESIS
    lines = Path(log_path).read_bytes().splitlines()
    for index, line in enumerate(lines, 1):
        record = json.loads(line)
        if record["seq"] != index or record["prev_sha256"] != previous:
            raise ValueError(f"hash chain broken at record {index}")
        previous = sha256_hex(line)
    return len(lines)


def native_trace_count(trace: Path, qid: str, offset: int) -> int:
    """Count the frozen CLI's own trace lines for qid after byte offset."""
    if not Path(trace).exists():
        return 0
    with open(trace, "rb") as handle:
        handle.seek(offset)
        return sum(
            json.loads(line)["question"] == qid
            for line in handle.read().splitlines()
            if line.strip()
        )


def old_pool(cli: str, qid: str) -> tuple[list[dict], list[list[int]]]:
    """Load (refs, groups) with the frozen CLI's own case() (§4.1)."""
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("reader_trial_frozen", cli)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _, refs = module.case(qid)
    groups: list[list[int]] = []
    keys: dict[tuple[str, str], int] = {}
    for index, ref in enumerate(refs):
        key = ref["provider"], ref["source_path"]
        if key not in keys:
            keys[key] = len(groups)
            groups.append([])
        groups[keys[key]].append(index)
    return refs, groups


def extract_old_ids(
    command: str,
    args: list[str],
    text: str,
    refs: list[dict],
    groups: list[list[int]],
) -> list[dict]:
    """Canonical identities surfaced by one successful OLD call."""
    data = json.loads(text)
    if command == "open":
        return [
            {
                "class": "open",
                "command": command,
                "pool_index": int(args[1]),
                "identity": {
                    field: data["source"][field] for field in OLD_FIELDS
                },
                "chars": len(data["text"]),
            }
        ]
    found = []
    for item in data["items"]:
        if command == "files":
            index = groups[item["file_id"]][0]
        else:
            index = item["event_id"]
        found.append(
            {
                "class": "listing",
                "command": command,
                "pool_index": index,
                "identity": {
                    field: refs[index][field] for field in OLD_FIELDS
                },
                "chars": len(item["hint"]),
            }
        )
    return found


def extract_new_ids(command: str, text: str, classes: dict) -> list[dict]:
    """Raw event references in a NEW result; canonicalised later (§1.2)."""
    found: list[dict] = []

    def is_ref(value: object) -> bool:
        return (
            isinstance(value, dict)
            and "record_sha256" in value
            and "provider" in value
            and "source_path" in value
            and ("line" in value or "byte_offset" in value)
        )

    def chars(holder: dict) -> int:
        for key in ("text", "hint"):
            if isinstance(holder.get(key), str):
                return len(holder[key])
        return 0

    def walk(value: object, parent: dict | None) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item, None)
        elif isinstance(value, dict):
            if is_ref(value):
                keys = (*NEW_ID_KEYS, "record_sha256")
                holder = value if chars(value) else (parent or value)
                found.append(
                    {
                        "class": classes.get(command, "undeclared"),
                        "command": command,
                        "identity": {k: value[k] for k in keys if k in value},
                        "chars": chars(holder),
                    }
                )
                return
            for item in value.values():
                walk(item, value)

    walk(json.loads(text), None)
    return found


class Wrapper:
    """Executes and logs trial_tool calls for one bound reader."""

    def __init__(self, binding: dict) -> None:
        self.b = validate(binding)
        self.log_path = Path(binding["log_path"])
        self.seq, self.prev = last_record(self.log_path)
        self.pool: tuple[list[dict], list[list[int]]] | None = None

    def call(self, arguments: dict) -> tuple[str, bool]:
        record = {
            "unit_id": self.b["unit_id"],
            "arm": self.b["arm"],
            "qid": self.b["qid"],
            "agent_id": self.b["agent_id"],
            "t_start": utc_ms(),
            "argv": arguments.get("args"),
            "reader_token_sha256": None,
            "exec_argv": None,
            "exit_code": None,
            "ids": [],
            "error": None,
        }
        text, is_error = self._dispatch(arguments, record)
        record["t_end"] = utc_ms()
        record["bytes_out"] = len(text.encode())
        self._append(record)
        return text, is_error

    def _dispatch(self, arguments: dict, record: dict) -> tuple[str, bool]:
        token, args = arguments.get("token"), arguments.get("args")
        if isinstance(token, str):
            record["reader_token_sha256"] = sha256_hex(token)
        elapsed = time.time() - self.b["t_launch"]
        if self.seq >= self.b["max_calls"] or elapsed >= self.b["max_seconds"]:
            return self._fail(record, "CEILING_HIT", CEILING, "ceiling")
        if not (
            isinstance(token, str)
            and isinstance(args, list)
            and all(isinstance(arg, str) for arg in args)
        ):
            message = "expected token (string) and args (array of strings)"
            return self._fail(record, "BAD_ARGS", error_json(message))
        if not hmac.compare_digest(sha256_hex(token), self.b["token_sha256"]):
            return self._fail(
                record, "UNKNOWN_TOKEN", error_json("unknown token")
            )
        if self.b["arm"] == "OLD":
            return self._old(args, record)
        return self._new(args, record)

    def _fail(
        self, record: dict, event: str, text: str, note: str | None = None
    ) -> tuple[str, bool]:
        record["event"] = event
        record["error"] = note or json.loads(text)["error"]
        return text, True

    def _old(self, args: list[str], record: dict) -> tuple[str, bool]:
        command = args[0] if args else ""
        if command not in OLD_CLASSES:
            return self._fail(
                record, "REFUSED", error_json("command not available")
            )
        old = self.b["old"]
        argv = [old["python"], old["cli"], command, self.b["qid"], *args[1:]]
        text, is_error = self._run(argv, old["env"], record)
        if record["exit_code"] == 0:
            try:
                if self.pool is None:
                    self.pool = old_pool(old["cli"], self.b["qid"])
                ids = extract_old_ids(command, args, text, *self.pool)
            except (ValueError, KeyError, IndexError, TypeError) as error:
                record["ids_error"] = repr(error)[:300]
            else:
                record["ids"] = ids
        return text, is_error

    def _new(self, args: list[str], record: dict) -> tuple[str, bool]:
        if any(arg == "--scope" or arg.startswith("--scope=") for arg in args):
            return self._fail(
                record, "REFUSED", error_json("scope is fixed by the harness")
            )
        new = self.b["new"]
        argv = [*new["argv"], "--scope", self.b["scope"], *args]
        text, is_error = self._run(argv, new["env"], record)
        if record["exit_code"] == 0:
            command = args[0] if args else ""
            classes = new.get("command_classes", {})
            try:
                record["ids"] = extract_new_ids(command, text, classes)
            except ValueError as error:  # output was not JSON
                record["ids_error"] = repr(error)[:300]
        return text, is_error

    def _run(
        self, argv: list[str], env: dict, record: dict
    ) -> tuple[str, bool]:
        record["exec_argv"] = argv
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                env=env,
                cwd=self.b["exec_cwd"],
                stdin=subprocess.DEVNULL,
                timeout=self.b["call_timeout_s"],
            )
        except subprocess.TimeoutExpired:
            return self._fail(
                record, "TIMEOUT", error_json("call timed out; try again")
            )
        record["exit_code"] = done.returncode
        try:
            text = done.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return self._fail(
                record, "DECODE_ERROR", error_json("tool output not UTF-8")
            )
        record["event"] = "OK" if done.returncode == 0 else "TOOL_ERROR"
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", "replace")[-500:]
            record["error"] = detail or text.strip()[-500:]
        return text, done.returncode != 0

    def _append(self, record: dict) -> None:
        with open(self.log_path, "ab") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.seq, self.prev = last_record(self.log_path)
            record["seq"] = self.seq + 1
            record["prev_sha256"] = self.prev
            line = json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
            handle.write(line + b"\n")
            handle.flush()
            self.seq, self.prev = record["seq"], sha256_hex(line)


def reply(output, message_id: object, **body: object) -> None:
    message = {"jsonrpc": "2.0", "id": message_id} | body
    output.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
    output.flush()


def serve(wrapper: Wrapper, stdin, stdout) -> None:
    """Newline-delimited JSON-RPC 2.0 loop; one request at a time."""
    for raw in stdin:
        if not raw.strip():
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            error = {"code": -32700, "message": "parse error"}
            reply(stdout, None, error=error)
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue  # notifications need no reply
        method, params = message.get("method"), message.get("params") or {}
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "trial", "version": "1"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [TOOL_SPEC]}
        elif method == "tools/call" and params.get("name") == TOOL:
            text, is_error = wrapper.call(params.get("arguments") or {})
            content = [{"type": "text", "text": text}]
            result = {"content": content, "isError": is_error}
        else:
            error = {"code": -32601, "message": f"unsupported: {method}"}
            reply(stdout, message["id"], error=error)
            continue
        reply(stdout, message["id"], result=result)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: mcp_reader.py BINDING_JSON", file=sys.stderr)
        return 2
    try:
        wrapper = Wrapper(json.loads(Path(argv[1]).read_text()))
    except (OSError, ValueError, KeyError) as error:
        print(f"mcp_reader: invalid binding: {error}", file=sys.stderr)
        return 2
    serve(wrapper, sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
