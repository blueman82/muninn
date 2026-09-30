"""Isolated headless launcher for trial readers and graders.

Covers trial-protocol §1.4 (reader configuration), §3.3(b) (one headless
``claude -p`` per unit) and §4.2 (unit close).  Every flag, environment
variable and setting comes from the frozen ``launcher_config.json``; the
"isolated" profile is the candidate trial configuration and the "default"
profile exists only for negative controls.

Each run writes, under its unit directory (mode 0700): prompt.txt,
command.json, binding.json and mcp.json (readers), stream.jsonl (the
stream-json output), stderr.txt, wrapper-log.jsonl (readers) and
transcript.jsonl (a copy of Claude Code's own session transcript).

``close_unit`` scans the copied transcript and records the isolation
verdict label.  An owner waiver path (``--waiver`` or the config key
``waiver``) is handed to that scan only, never to a child session.
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from . import mcp_reader
else:  # run as a script: python3.13 -B trial_harness/launch.py ...
    import mcp_reader

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "launcher_config.json"
SERVER = HERE / "mcp_reader.py"
TOOL_FQN = "mcp__trial__trial_tool"
ANSWER_KEYS = {"answer": str, "citations": list, "abstained": bool}


def load_config(path: Path = CONFIG_PATH) -> dict:
    return json.loads(Path(path).read_text())


def utc_ms() -> str:
    stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
    return stamp.replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_private(path: Path, text: str) -> Path:
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
    os.chmod(path, 0o600)
    return path


def protocol_code_blocks(text: str) -> list[str]:
    """Fenced blocks of the frozen protocol, in order (template first)."""
    return re.findall(r"^```[^\n]*\n(.*?)\n```$", text, flags=re.S | re.M)


def render_prompt(template: str, fields: dict[str, str]) -> str:
    """Replace each {NAME} exactly once, in one pass (no re-expansion)."""
    pattern = re.compile("|".join(re.escape("{" + k + "}") for k in fields))
    return pattern.sub(lambda match: fields[match.group(0)[1:-1]], template)


def reader_prompt(
    cfg: dict, tool_description: str, question: str, token: str
) -> str:
    return render_prompt(
        cfg["reader_prompt_template"],
        {
            "TOOL_DESCRIPTION": tool_description,
            "QUESTION": question,
            "READER_TOKEN": token,
        },
    )


def child_env(cfg: dict, profile: str, base: dict) -> dict:
    """Allowlisted environment for the child ``claude`` process."""
    spec, never = cfg["profiles"][profile], tuple(cfg["env_never_inherit"])
    env = {
        key: base[key]
        for key in spec["env_passthrough"]
        if key in base and not key.startswith(never)
    }
    return env | spec["env_set"]


def claude_argv(
    cfg: dict,
    profile: str,
    *,
    role: str,
    model: str,
    effort: str | None,
    session_id: str,
    mcp_config: str | None,
) -> list[str]:
    spec = cfg["profiles"][profile]
    argv = [cfg["claude_bin"], *spec["flags"]]
    argv += ["--model", model, "--session-id", session_id]
    if effort:
        argv += ["--effort", effort]
    if spec.get("settings") is not None:
        settings = json.dumps(spec["settings"], separators=(",", ":"))
        argv += ["--settings", settings]
    if role == "reader":
        argv += ["--mcp-config", mcp_config, "--allowedTools", TOOL_FQN]
    return argv


def mcp_config(cfg: dict, binding_path: Path) -> dict:
    server = {
        "type": "stdio",
        "command": cfg["python_bin"],
        "args": ["-I", "-B", str(SERVER), str(binding_path)],
        "env": {},
        "alwaysLoad": True,
    }
    return {"mcpServers": {"trial": server}}


def check_claude_version(cfg: dict) -> None:
    done = subprocess.run(
        [cfg["claude_bin"], "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]},
    )
    if done.stdout.strip() != cfg["claude_version"]:
        raise RuntimeError(f"claude version drift: {done.stdout.strip()!r}")


def project_slug(path: str) -> str:
    """Claude Code's projects/ directory name for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def frozen_old_cli(cfg: dict) -> Path:
    """First available copy of the OLD CLI whose sha256 is the frozen one."""
    for source in map(Path, cfg["old_cli_sources"]):
        if source.is_file() and sha256_file(source) == cfg["old_cli_sha256"]:
            return source
    raise RuntimeError("no byte-identical copy of the frozen OLD CLI")


def stream_session_id(stream: Path) -> str | None:
    """Session id reported by the run's system/init event, if any."""
    for line in Path(stream).read_text().splitlines():
        event = json.loads(line)
        if event.get("type") == "system" and event.get("subtype") == "init":
            return event.get("session_id")
    return None


def find_transcript(cfg: dict, session_id: str) -> Path | None:
    root = Path(cfg["projects_root"]).expanduser()
    return next(root.glob(f"*/{session_id}.jsonl"), None)


def run_claude(
    cfg: dict,
    argv: list[str],
    env: dict,
    prompt: str,
    out_dir: Path,
    session_id: str,
) -> dict:
    """Run one headless session; the prompt goes in on stdin."""
    check_claude_version(cfg)
    cwd = cfg["sandbox_dir"]
    write_private(out_dir / "prompt.txt", prompt)
    command = {
        "argv": argv,
        "env": env,
        "cwd": cwd,
        "stdin": "prompt.txt",
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
    }
    write_private(out_dir / "command.json", json.dumps(command, indent=1))
    t_start, killed = utc_ms(), False
    stream = open(write_private(out_dir / "stream.jsonl", ""), "wb")
    stderr = open(write_private(out_dir / "stderr.txt", ""), "wb")
    with stream, stderr:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=stream,
            stderr=stderr,
        )
        try:
            process.communicate(
                prompt.encode(), timeout=cfg["ceilings"]["kill_after_s"]
            )
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            killed = True
    session_id = stream_session_id(out_dir / "stream.jsonl") or session_id
    source = find_transcript(cfg, session_id)
    if source is not None:
        shutil.copyfile(source, out_dir / "transcript.jsonl")
        os.chmod(out_dir / "transcript.jsonl", 0o600)
    return {
        "session_id": session_id,
        "exit_code": process.returncode,
        "killed": killed,
        "t_start": t_start,
        "t_end": utc_ms(),
        "transcript_source": str(source) if source else None,
    }


def scanner():
    """The scanner module; it imports this one, so load it on first use."""
    if __package__:
        from . import canaries as module
    else:
        import canaries as module
    return module


def new_unit_dir(out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    out_dir.mkdir(mode=0o700)
    return out_dir


def launch_reader(
    cfg: dict,
    *,
    out_dir: Path,
    unit_id: str,
    arm: str,
    qid: str,
    scope: str,
    question: str,
    tool_description: str,
    backend: dict,
    model: str,
    effort: str | None,
    profile: str = "isolated",
    extra_flags: tuple[str, ...] = (),
) -> dict:
    """Launch one single-shot reader bound to one token (§1.4, §2).

    ``extra_flags`` exist only for negative controls (e.g. --resume).
    """
    out_dir = new_unit_dir(out_dir)
    token, session_id = secrets.token_hex(16), str(uuid.uuid4())
    ceilings = cfg["ceilings"]
    binding = {
        "unit_id": unit_id,
        "arm": arm,
        "qid": qid,
        "scope": scope,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "agent_id": session_id,
        "log_path": str(out_dir / "wrapper-log.jsonl"),
        "t_launch": time.time(),
        "max_calls": ceilings["max_calls"],
        "max_seconds": ceilings["max_seconds"],
        "call_timeout_s": ceilings["call_timeout_s"],
        "exec_cwd": cfg["exec_cwd"],
    } | backend
    if arm == "OLD":  # §4.1: native trace counts are reconciled per unit
        trace = native_trace_path(backend["old"]["cli"])
        binding["native_trace_offset"] = (
            trace.stat().st_size if trace.exists() else 0
        )
    binding_path = write_private(out_dir / "binding.json", json.dumps(binding))
    mcp_path = write_private(
        out_dir / "mcp.json", json.dumps(mcp_config(cfg, binding_path))
    )
    argv = claude_argv(
        cfg,
        profile,
        role="reader",
        model=model,
        effort=effort,
        session_id=session_id,
        mcp_config=str(mcp_path),
    )
    argv += list(extra_flags)
    prompt = reader_prompt(cfg, tool_description, question, token)
    env = child_env(cfg, profile, dict(os.environ))
    return run_claude(cfg, argv, env, prompt, out_dir, session_id) | {
        "unit_id": unit_id,
        "profile": profile,
        "model_requested": model,
        "effort_requested": effort,
    }


def launch_grader(
    cfg: dict,
    *,
    out_dir: Path,
    unit_id: str,
    prompt: str,
    model: str,
    effort: str | None,
    profile: str = "isolated",
) -> dict:
    """Launch one tool-less grader or adjudicator on one packet (§5.3)."""
    out_dir = new_unit_dir(out_dir)
    session_id = str(uuid.uuid4())
    argv = claude_argv(
        cfg,
        profile,
        role="grader",
        model=model,
        effort=effort,
        session_id=session_id,
        mcp_config=None,
    )
    env = child_env(cfg, profile, dict(os.environ))
    return run_claude(cfg, argv, env, prompt, out_dir, session_id) | {
        "unit_id": unit_id,
        "profile": profile,
        "model_requested": model,
        "effort_requested": effort,
    }


def final_message(stream_path: Path) -> str | None:
    """The reader's final message: the result event, else last text."""
    last_text = None
    for line in Path(stream_path).read_text().splitlines():
        event = json.loads(line)
        if event.get("type") == "result" and isinstance(
            event.get("result"), str
        ):
            return event["result"]
        if event.get("type") == "assistant":
            content = event.get("message", {}).get("content") or []
            texts = [
                block["text"]
                for block in content
                if block.get("type") == "text"
            ]
            if texts:
                last_text = "".join(texts)
    return last_text


def extract_final_answer(text: str) -> dict | None:
    """Last top-level JSON object with exactly the answer keys (§1.4)."""
    decoder, found, index = json.JSONDecoder(), None, 0
    while (index := text.find("{", index)) != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index += 1
            continue
        if (
            isinstance(value, dict)
            and set(value) == set(ANSWER_KEYS)
            and all(isinstance(value[k], t) for k, t in ANSWER_KEYS.items())
        ):
            found = value
        index = end
    return found


def native_trace_path(cli: str) -> Path:
    """The frozen CLI appends its own trace next to itself."""
    return Path(cli).with_name("reader_trace.jsonl")


def reconcile(unit_dir: Path) -> dict:
    """Verify the wrapper log chain and, for OLD, the native trace count."""
    binding = json.loads((unit_dir / "binding.json").read_text())
    log = unit_dir / "wrapper-log.jsonl"
    records = mcp_reader.verify_chain(log) if log.exists() else 0
    result = {"wrapper_records": records, "chain_ok": True}
    if binding["arm"] == "OLD":
        lines = log.read_text().splitlines() if log.exists() else []
        executed = sum(json.loads(line)["exit_code"] == 0 for line in lines)
        native = mcp_reader.native_trace_count(
            native_trace_path(binding["old"]["cli"]),
            binding["qid"],
            binding["native_trace_offset"],
        )
        result |= {"wrapper_ok_calls": executed, "native_trace": native}
        result["native_trace_match"] = executed == native
    return result


def close_unit(
    unit_dir: Path, index_path: Path, unit_id: str, waiver: Path | None = None
) -> dict:
    """Write final message and extracted JSON; append hashes (§4.2).

    The entry also records the §3.6 isolation verdict label; a bad
    ``waiver`` raises before the unit directory or the index is touched.
    """
    unit_dir = Path(unit_dir)
    isolation = scanner().unit_isolation(unit_dir, waiver)
    message = final_message(unit_dir / "stream.jsonl")
    answer = extract_final_answer(message) if message is not None else None
    write_private(unit_dir / "final_message.txt", message or "")
    payload = answer if answer is not None else {"status": "FORMAT_FAIL"}
    write_private(unit_dir / "answer.json", json.dumps(payload))
    entry = {"unit_id": unit_id, "t_close": utc_ms()}
    entry["status"] = "ANSWERED" if answer is not None else "FORMAT_FAIL"
    entry["isolation"] = isolation
    for name in (
        "final_message.txt",
        "answer.json",
        "transcript.jsonl",
        "stream.jsonl",
        "wrapper-log.jsonl",
    ):
        path = unit_dir / name
        key = name.split(".")[0].replace("-", "_") + "_sha256"
        entry[key] = sha256_file(path) if path.exists() else None
    if (unit_dir / "binding.json").exists():
        entry["reconcile"] = reconcile(unit_dir)
    with open(index_path, "a") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    os.chmod(index_path, 0o600)
    return entry


def main(argv: list[str] | None = None) -> int:
    """Operator entry point: launch one reader or grader unit."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("role", choices=["reader", "grader"])
    parser.add_argument("--unit-id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--arm", choices=["OLD", "NEW"])
    parser.add_argument("--qid")
    parser.add_argument("--scope")
    parser.add_argument("--question-file", type=Path)
    parser.add_argument("--tool-description-file", type=Path)
    parser.add_argument("--backend-file", type=Path)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--index", type=Path)
    parser.add_argument("--waiver", type=Path)
    args = parser.parse_args(argv)
    cfg = load_config()
    waiver = args.waiver or cfg.get("waiver") or None
    if waiver is not None:  # refuse a bad waiver before a unit is spent
        scanner().verify_waiver(waiver)
    if args.role == "reader":
        result = launch_reader(
            cfg,
            out_dir=args.out,
            unit_id=args.unit_id,
            arm=args.arm,
            qid=args.qid,
            scope=args.scope,
            question=args.question_file.read_text(),
            tool_description=args.tool_description_file.read_text(),
            backend=json.loads(args.backend_file.read_text()),
            model=args.model or cfg["reader"]["model"],
            effort=args.effort or cfg["reader"]["effort"],
        )
    else:
        result = launch_grader(
            cfg,
            out_dir=args.out,
            unit_id=args.unit_id,
            prompt=args.prompt_file.read_text(),
            model=args.model,
            effort=args.effort,
        )
    if args.index:
        result["close"] = close_unit(
            args.out, args.index, args.unit_id, waiver
        )
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
