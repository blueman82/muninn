"""Capture immutable legacy ingestion and retrieval evidence without models."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import selectors
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from tools.learning_eval.current_baseline_child import (
    OPERATIONS,
    PIN_SHA256,
    SOURCE_COMMIT,
    confined,
    decode,
    read_file,
    verify_pin,
)
from tools.learning_eval.current_baseline_models import (
    BaselineCase,
    BaselineManifest,
    FamilyPaths,
    rendered_source,
    validate_inputs,
)
from tools.learning_eval.pilot_evidence import immutable
from tools.learning_eval.pilot_models import SourceMaterial

CHILD = Path(__file__).with_name("current_baseline_child.py").resolve()


def _process(
    executable: str, request: bytes, cwd: Path, timeout: float, limit: int
) -> dict[str, Any]:
    """Drain finite child output and kill/reap on timeout or byte overflow."""
    started = time.monotonic()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    error: str | None = None
    with tempfile.TemporaryFile(dir=cwd) as incoming:
        incoming.write(request)
        incoming.seek(0)
        with (
            subprocess.Popen(
                [executable, "-I", "-S", "-B", str(CHILD)],
                stdin=incoming,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env={"HOME": str(cwd), "TMPDIR": str(cwd)},
            ) as process,
            selectors.DefaultSelector() as poller,
        ):
            assert process.stdout is not None and process.stderr is not None
            for name, pipe in (
                ("stdout", process.stdout),
                ("stderr", process.stderr),
            ):
                poller.register(pipe, selectors.EVENT_READ, name)
            while poller.get_map():
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    error = "timeout"
                    break
                for key, _ in poller.select(min(remaining, 0.05)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        poller.unregister(key.fileobj)
                        continue
                    target = buffers[key.data]
                    available = limit - len(target)
                    target.extend(chunk[:available])
                    if len(chunk) > available:
                        error = "output_limit"
                        break
                if error:
                    break
            if error:
                process.kill()
            try:
                process.wait(
                    timeout=max(0.01, timeout - (time.monotonic() - started))
                )
            except subprocess.TimeoutExpired:
                error = "timeout"
                process.kill()
                process.wait()
    return {
        "raw_outputs": {
            name: {
                "base64": base64.b64encode(raw).decode(),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
            for name, raw in buffers.items()
        },
        "stdout": buffers["stdout"].decode("utf-8", errors="replace"),
        "stderr": buffers["stderr"].decode("utf-8", errors="replace"),
        "exit_code": process.returncode,
        "error": error,
        "elapsed_seconds": time.monotonic() - started,
    }


def _operation(
    manifest: BaselineManifest, case: BaselineCase, operation: str
) -> dict[str, Any]:
    """Launch one program-owned operation and retain transport failures."""
    pin = verify_pin(
        manifest.pin_path, manifest.source_root, manifest.boundary
    )
    family = manifest.families[case.family_id]
    request = {
        "operation": operation,
        "pin_path": str(manifest.pin_path),
        "source_root": str(manifest.source_root),
        "boundary": str(manifest.boundary),
        "family_root": str(family.root),
        "query": case.query,
        "session": "baseline-later-task",
        "search_limit": manifest.search_limit,
        "output_limit": manifest.output_limit,
    }
    raw = json.dumps(request).encode()
    if len(raw) > manifest.input_limit:
        raise ValueError("input_limit")
    transport = _process(
        pin["runtime"]["executable"],
        raw,
        family.root / "tmp",
        manifest.timeout_seconds,
        8 * manifest.output_limit + 65536,
    )
    result: dict[str, Any] | None = None
    code = transport["error"]
    if code is None:
        try:
            result = decode(
                transport["stdout"].encode(), 8 * manifest.output_limit + 65536
            )
            if result.get("operation") != operation:
                code = "child_refused"
            elif result.get("error") or result.get("exit_code") != 0:
                code = "operation_failed"
        except (ValueError, RecursionError):
            code = "child_output_invalid"
    return {
        "operation": operation,
        "code": code or "ok",
        "transport": transport,
        "result": result,
    }


def _create_family(family: FamilyPaths) -> None:
    """Create an empty isolated store and all explicitly declared roots."""
    family.root.mkdir(mode=0o700)
    for name in (
        "home",
        "codex",
        "claude",
        "store",
        "cwd",
        "tmp",
        "originals",
        "archived",
        "projects",
        "empty-bin",
    ):
        (family.root / name).mkdir(mode=0o700)
    # A private scope marker stops legacy ancestor discovery at this cwd.
    (family.root / "cwd" / ".git").mkdir(mode=0o700)
    (family.root / "store" / "recall.off").write_bytes(b"")
    (family.root / "store" / "recall.off").chmod(0o600)


def _capture(family: FamilyPaths, source: SourceMaterial) -> Path:
    """Append only validated synthetic originals without replacing history."""
    path = confined(
        family.root / "originals" / source.relative_path, family.root
    )
    raw = rendered_source(source, family)
    if path.exists() and not raw.startswith(read_file(path, 8 * 1024 * 1024)):
        raise ValueError("source_chronology")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def _body(operation: dict[str, Any]) -> dict[str, Any] | None:
    """Expose parsed output only when the real operation succeeded."""
    if operation["code"] != "ok":
        return None
    try:
        result = decode(operation["result"]["stdout"].encode())
        trace = operation["result"].get("trace", {})
        if trace.get("error"):
            operation["code"] = "operation_failed"
            return None
    except (ValueError, RecursionError):
        operation["code"] = "operation_output_invalid"
        return None
    if "error" in result:
        operation["code"] = "operation_failed"
        return None
    return result


def _summarize(operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep retrieval channels and unavailable measurements distinct."""
    bodies = {op["operation"]: _body(op) for op in operations}
    ledger, search, hook = (
        bodies.get("list"),
        bodies.get("search"),
        bodies.get("session-start"),
    )
    ingestion = bodies.get("ingest")
    count = len(ledger["entries"]) if ledger is not None else None
    if ingestion and ingestion.get("ingest", {}).get("failed"):
        operations[0]["code"] = "ingest_failed"
    output = hook.get("hookSpecificOutput", {}) if hook is not None else None
    context = (
        output.get("additionalContext", "") if output is not None else None
    )
    return {
        "knowledge_count": count,
        "automatic_saves": count,
        "automatic_saver_available": False,
        "transcript_hits": search.get("hits") if search is not None else None,
        "knowledge_hits": (
            search.get("knowledge") if search is not None else None
        ),
        "search_has_more": (
            search.get("has_more") if search is not None else None
        ),
        "session_start_context": context,
        "legacy_context_truncation": "not_reported_by_legacy_api",
        "session_start_context_bytes": (
            len(context.encode()) if context is not None else None
        ),
        "eligible_knowledge_count": 0 if count == 0 else None,
        "pushed_knowledge_count": (
            0 if count == 0 and hook is not None else None
        ),
        "prompt_recall_output": bodies.get("prompt"),
        "task_answer": None,
        "learning_performance_grade": None,
    }


def _case(
    manifest: BaselineManifest, case: BaselineCase, source: SourceMaterial
) -> dict[str, Any]:
    """Retain operation rows and errors after capture."""
    family = manifest.families[case.family_id]
    operations: list[dict[str, Any]] = []
    failure: str | None = None
    try:
        path = _capture(family, source)
        for operation in OPERATIONS:
            operations.append(_operation(manifest, case, operation))
            if operation == "ingest" and source.mutation == "missing":
                path.unlink()
            elif operation == "ingest" and source.mutation == "changed":
                path.write_bytes(
                    path.read_bytes().replace(
                        b'"role":"user"', b'"role":"assistant"'
                    )
                    + b"\n"
                )
    except Exception as exc:
        failure = type(exc).__name__
    operations.extend(
        {
            "operation": operation,
            "code": "not_run",
            "error": failure,
            "result": None,
            "transport": None,
        }
        for operation in OPERATIONS[len(operations) :]
    )
    summary = _summarize(operations)
    return {
        **case.model_dump(mode="json"),
        "operations": operations,
        "summary": summary,
        "code": (
            "failed"
            if failure or any(o["code"] != "ok" for o in operations)
            else "ok"
        ),
        "error": failure,
    }


def _restore(manifest: BaselineManifest, binding: bytes) -> dict[str, Any]:
    """Return exact previously sealed evidence without repeating mutations."""
    root = manifest.output_root
    if read_file(root / "inputs.json", 16 * 1024 * 1024) != binding:
        raise ValueError("replay_binding")
    seal = decode(read_file(root / "seal.json", 1024 * 1024))
    result = read_file(root / "result.json", 256 * 1024 * 1024)
    if hashlib.sha256(result).hexdigest() != seal["result_sha256"]:
        raise ValueError("replay_output_binding")
    for name, expected in seal["files"].items():
        raw = read_file(confined(root / name, root), 16 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("replay_output_binding")
    return decode(result, 256 * 1024 * 1024)


def run(manifest: BaselineManifest) -> dict[str, Any]:
    """Run a new frozen capture-only comparison, or restore sealed evidence."""
    manifest = BaselineManifest.model_validate_json(manifest.model_dump_json())
    material, binding = validate_inputs(manifest)
    if manifest.output_root.exists():
        return _restore(manifest, binding)
    if manifest.run_root.exists():
        raise ValueError("disposable_root_exists")
    manifest.output_root.mkdir(mode=0o700)
    immutable(manifest.output_root, "inputs.json", binding, 16 * 1024 * 1024)
    immutable(
        manifest.output_root,
        "pin.json",
        read_file(manifest.pin_path, 1024 * 1024),
        1024 * 1024,
    )
    manifest.run_root.mkdir(mode=0o700)
    for family in manifest.families.values():
        _create_family(family)
    sources = {s.case_id: s for s in material.sources}
    for index, case in enumerate(manifest.cases):
        raw = rendered_source(
            sources[case.case_id], manifest.families[case.family_id]
        )
        immutable(
            manifest.output_root, f"source-{index}.jsonl", raw, 8 * 1024 * 1024
        )
    rows = [_case(manifest, c, sources[c.case_id]) for c in manifest.cases]
    result = {
        "condition": manifest.condition,
        "source_commit": SOURCE_COMMIT,
        "source_runtime_pin_sha256": PIN_SHA256,
        "recall_enabled": False,
        "model_calls": 0,
        "model_cost": None,
        "task_answers_measured": False,
        "native_host_verified": False,
        "cases": rows,
    }
    raw = json.dumps(result, sort_keys=True, allow_nan=False).encode()
    immutable(manifest.output_root, "result.json", raw, 256 * 1024 * 1024)
    files = {
        p.name: hashlib.sha256(read_file(p, 16 * 1024 * 1024)).hexdigest()
        for p in manifest.output_root.iterdir()
        if p.name != "result.json"
    }
    seal = {"result_sha256": hashlib.sha256(raw).hexdigest(), "files": files}
    immutable(
        manifest.output_root,
        "seal.json",
        json.dumps(seal, sort_keys=True).encode(),
        1024 * 1024,
    )
    return result
