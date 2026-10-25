"""Validate pilot-only inputs and create synthetic originals."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

from muninn import ingest, review_pause_state, store
from muninn.hook_learn import capture_boundary
from muninn.review_activation import bootstrap_policy
from muninn.review_json import canonical_json
from muninn.review_request_views import request_row, request_view
from muninn.review_runner import (
    check_private_path,
    read_private_file,
)
from muninn.review_selectors import fields, selected
from muninn.review_supervisor_store import database
from tools.learning_eval.cases import (
    digest,
    input_digests,
    strict_json,
    validate_partition,
)
from tools.learning_eval.pilot_evidence import immutable
from tools.learning_eval.pilot_models import (
    PilotManifest,
    PilotMaterial,
    PlannedTrial,
    SourceMaterial,
)


def load_material(manifest: PilotManifest) -> PilotMaterial:
    """Read only the separately bound file, rejecting mixed partitions."""
    check_private_path(manifest.material_path, directory=False)
    raw = read_private_file(manifest.material_path, 8 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != manifest.material_digest:
        raise ValueError("pilot_material_binding")
    strict_json(raw)
    material = PilotMaterial.model_validate_json(raw)
    cases = material.cases
    validate_partition(cases)
    hashes = input_digests(cases)
    if (
        any(c.partition != "pilot" for c in cases)
        or len(cases) != manifest.assessment.sample_size
        or hashes["cases_digest"] != manifest.assessment.cases_digest
        or hashes["labels_digest"] != manifest.assessment.labels_digest
        or digest(material.families) != manifest.family_digest
        or set(material.families) != {c.family_id for c in cases}
        or set(manifest.exposures) != {c.id for c in cases}
        or len(material.sources) != len(cases)
        or {s.case_id for s in material.sources} != {c.id for c in cases}
    ):
        raise ValueError("pilot_material_binding")
    for source in material.sources:
        _check_source(source)
        case = next(c for c in cases if c.id == source.case_id)
        for record in case.evidence:
            lines = source.content.encode().splitlines()
            if record.line > len(lines):
                raise ValueError("pilot_source_binding")
            raw = lines[record.line - 1]
            _, excerpt = selected(
                fields(source.provider, json.loads(raw)),
                record.part,
                record.selector,
            )
            if (
                record.provider != source.provider
                or record.thread_id != source.thread_id
                or record.session_root != source.session_root
                or record.line_sha256 != hashlib.sha256(raw).hexdigest()
                or record.selector_digest
                != hashlib.sha256(
                    canonical_json(record.selector).encode()
                ).hexdigest()
                or record.excerpt != excerpt
            ):
                raise ValueError("pilot_source_binding")
    return material


def _check_source(source: SourceMaterial) -> None:
    """Verify exact declared complete coordinates before creating originals."""
    raw = source.content.encode()
    lines = raw.splitlines(keepends=True)
    window = source.window
    if (
        source.relative_path.is_absolute()
        or ".." in source.relative_path.parts
        or hashlib.sha256(raw).hexdigest() != source.sha256
        or not raw.endswith(b"\n")
        or len(lines) != window.through_line
        or window.source.provider != source.provider
        or window.source.thread_id != source.thread_id
        or window.boundary_hash
        != hashlib.sha256(lines[-1].rstrip(b"\r\n")).hexdigest()
    ):
        raise ValueError("pilot_source_binding")
    for value in lines:
        strict_json(value)
    first = json.loads(lines[0])["payload"]
    if (
        first.get("id") != source.thread_id
        or first.get("session_id") != source.session_root
    ):
        raise ValueError("pilot_source_binding")


@contextmanager
def source_environment(home: Path) -> Generator[None]:
    """Restore process-local source roots after serialized disposable work."""
    values = {
        "MUNINN_HOME": str(home),
        "MUNINN_ROOTS": json.dumps(
            {"codex-sessions": str(home / "originals")}
        ),
    }
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def create_home(
    manifest: PilotManifest, trial: PlannedTrial, prompt: bytes
) -> None:
    """Seed only a positively new private store using public lifecycle APIs."""
    home = trial.store_home
    marker = manifest.artifact_path / ("store-" + digest(str(home)) + ".json")
    if marker.exists():
        raise ValueError("pilot_store_missing")
    immutable(
        manifest.artifact_path,
        marker.name,
        _home_binding(manifest, trial),
        65536,
    )
    home.parent.mkdir(mode=0o700, exist_ok=True)
    check_private_path(home.parent, directory=True)
    home.mkdir(mode=0o700)
    fd = review_pause_state.acquire(home)
    try:
        with (
            store.writer_lock(home, wait_s=0),
            closing(store.connect_rw(store.db_path(home))) as conn,
        ):
            review_pause_state.seed_new(home, fd)
            conn.execute("BEGIN IMMEDIATE")
            bootstrap_policy(
                conn, manifest.assessment.policies[0], time.time()
            )
            conn.commit()
    finally:
        os.close(fd)
    policy = manifest.assessment.policies[0]
    selection = home / "review-capabilities" / "selected"
    selection.mkdir(parents=True, mode=0o700)
    selection.parent.chmod(0o700)
    prompts = home / "review-prompts"
    prompts.mkdir(mode=0o700)
    immutable(
        selection,
        digest(policy.model_dump(mode="json")) + ".json",
        manifest.learning[trial.comparator]
        .capability.model_dump_json()
        .encode(),
        1024 * 1024,
    )
    immutable(
        prompts, policy.prompt_digest + ".json", prompt, policy.max_input_bytes
    )
    (home / "originals").mkdir(mode=0o700)


def capture_source(
    home: Path, source: SourceMaterial, manifest: PilotManifest
) -> tuple[str, ...]:
    """Ingest originals, capture, then apply declared mutation."""
    root = home / "originals"
    path = root / source.relative_path
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.resolve() != path:
        raise ValueError("pilot_source_path")
    if path.exists():
        check_private_path(path, directory=False)
        before = path.read_bytes()
        if not source.content.encode().startswith(before):
            raise ValueError("pilot_source_chronology")
    path.write_bytes(source.content.encode())
    path.chmod(0o600)
    with database(home, transaction=False) as conn:
        ingest.ingest(conn, {"codex-sessions": root})
    metadata = json.loads(source.content.splitlines()[0])["payload"]
    payload = {
        "hook_event_name": "SessionEnd",
        "session_id": source.session_root,
        "cwd": metadata["cwd"],
        "transcript_path": str(path),
        "reason": "other",
    }
    result = capture_boundary(
        home,
        "codex",
        "session-end",
        json.dumps(payload).encode(),
        manifest.assessment.policies[0],
    )
    if not result.accepted:
        raise ValueError(result.code)
    with database(home, transaction=False) as conn:
        windows = tuple(
            request_view(request_row(conn, ident)).window
            for ident in result.request_ids
        )
    if windows != (source.window,):
        raise ValueError("pilot_boundary_mismatch")
    if source.mutation == "missing":
        path.unlink()
    elif source.mutation == "changed":
        path.write_bytes(
            source.content.replace(
                '"role":"user"', '"role":"assistant"'
            ).encode()
            + b"\n"
        )
    return result.request_ids


def _home_binding(manifest: PilotManifest, trial: PlannedTrial) -> bytes:
    """Bind store provenance independently of any individual family case."""
    return json.dumps(
        [
            str(trial.store_home),
            manifest.assessment.config_digest,
            trial.family_id,
            trial.comparator,
            trial.repetition,
        ]
    ).encode()


def check_home(manifest: PilotManifest, trial: PlannedTrial) -> None:
    """Require exact existing provenance without replacing or reseeding."""
    check_private_path(trial.store_home, directory=True)
    marker = manifest.artifact_path / (
        "store-" + digest(str(trial.store_home)) + ".json"
    )
    check_private_path(marker, directory=False)
    if read_private_file(marker, 65536) != _home_binding(manifest, trial):
        raise ValueError("pilot_store_binding")
