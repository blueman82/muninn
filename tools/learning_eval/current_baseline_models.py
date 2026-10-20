"""Bind pilot originals and the capture-only baseline schedule."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from muninn.knowledge_models import Digest, SchemaVersion, StrictModel, Text
from muninn.review_json import canonical_json
from muninn.review_selectors import fields, selected
from tools.learning_eval.cases import digest, strict_json, validate_partition
from tools.learning_eval.current_baseline_child import (
    PIN_SHA256,
    confined,
    read_file,
    verify_pin,
)
from tools.learning_eval.pilot_models import PilotMaterial, SourceMaterial


class FamilyPaths(StrictModel):
    """Map one original project scope into one disposable family home."""

    root: Path
    original_cwd: Text


class BaselineCase(StrictModel):
    """Declare the chronological query without any save instructions."""

    case_id: Text
    family_id: Text
    chronology: Annotated[int, Field(ge=0)]
    query: Annotated[str, Field(min_length=1, max_length=16384)]


class BaselineManifest(StrictModel):
    """Freeze source, material, isolation paths and operation bounds."""

    schema_version: SchemaVersion
    condition: Literal["capture-only-no-automatic-saver"]
    boundary: Path
    source_root: Path
    pin_path: Path
    pin_sha256: Digest
    material_path: Path
    material_sha256: Digest
    family_digest: Digest
    run_root: Path
    output_root: Path
    families: dict[Text, FamilyPaths]
    cases: Annotated[
        tuple[BaselineCase, ...], Field(min_length=1, max_length=3)
    ]
    recall_enabled: bool
    search_limit: Annotated[int, Field(ge=1, le=100)]
    input_limit: Annotated[int, Field(ge=1024, le=1024 * 1024)]
    output_limit: Annotated[int, Field(ge=1024, le=1024 * 1024)]
    timeout_seconds: Annotated[float, Field(gt=0, le=120, allow_inf_nan=False)]

    @model_validator(mode="after")
    def boundaries(self) -> Self:
        """Refuse overlapping executable and writable paths."""
        if self.recall_enabled or self.pin_sha256 != PIN_SHA256:
            raise ValueError("baseline_condition")
        roots = (self.source_root, self.run_root, self.output_root)
        for path in (*roots, self.pin_path, self.material_path):
            confined(path, self.boundary)
        for left in roots:
            for right in roots:
                if left != right and left.is_relative_to(right):
                    raise ValueError("path_overlap")
        if len(set(roots)) != len(roots):
            raise ValueError("path_overlap")
        for path in (self.pin_path, self.material_path):
            if any(path.is_relative_to(r) for r in roots):
                raise ValueError("path_overlap")
        _family_paths(self)
        return self


def _family_paths(manifest: BaselineManifest) -> None:
    """Require distinct direct children for disposable family stores."""
    family_roots = [f.root for f in manifest.families.values()]
    if len(set(family_roots)) != len(family_roots):
        raise ValueError("family_overlap")
    for root in family_roots:
        confined(root, manifest.run_root)
        if root.parent != manifest.run_root:
            raise ValueError("family_overlap")


def load_manifest(path: Path) -> BaselineManifest:
    """Parse bounded JSON without duplicate keys or nonfinite values."""
    raw = read_file(path, 1024 * 1024)
    strict_json(raw)
    return BaselineManifest.model_validate_json(raw)


def validate_material(manifest: BaselineManifest) -> PilotMaterial:
    """Validate pilot chronology and original bytes without using labels."""
    raw = read_file(manifest.material_path, 8 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != manifest.material_sha256:
        raise ValueError("material_binding")
    strict_json(raw)
    material = PilotMaterial.model_validate_json(raw)
    validate_partition(material.cases)
    expected = tuple((c.id, c.family_id, c.chronology) for c in material.cases)
    actual = tuple(
        (c.case_id, c.family_id, c.chronology) for c in manifest.cases
    )
    if (
        actual != expected
        or not actual
        or any(c.partition != "pilot" for c in material.cases)
        or set(manifest.families) != set(material.families)
        or digest(material.families) != manifest.family_digest
        or len(material.sources) != len(material.cases)
        or {s.case_id for s in material.sources}
        != {c.id for c in material.cases}
    ):
        raise ValueError("material_schedule_binding")
    _sources(material)
    return material


def rendered_source(source: SourceMaterial, family: FamilyPaths) -> bytes:
    """Map recorded cwd fields into the equivalent disposable scope."""
    rows: list[bytes] = []
    for line in source.content.splitlines():
        record = json.loads(line)
        payload = record.get("payload", {})
        if isinstance(payload, dict) and "cwd" in payload:
            if payload["cwd"] != family.original_cwd:
                raise ValueError("source_scope_binding")
            payload["cwd"] = str(family.root / "cwd")
        rows.append(json.dumps(record, separators=(",", ":")).encode() + b"\n")
    first = json.loads(rows[0])["payload"]
    if first.get("cwd") != str(family.root / "cwd"):
        raise ValueError("source_scope_binding")
    return b"".join(rows)


def validate_inputs(manifest: BaselineManifest) -> tuple[PilotMaterial, bytes]:
    """Revalidate pins and freeze the source and configuration schedule."""
    verify_pin(manifest.pin_path, manifest.source_root, manifest.boundary)
    material = validate_material(manifest)
    sources = {s.case_id: s for s in material.sources}
    schedule: list[dict[str, object]] = []
    for case in manifest.cases:
        source = sources[case.case_id]
        family = manifest.families[case.family_id]
        rendered = rendered_source(source, family)
        schedule.append(
            {
                **case.model_dump(mode="json"),
                "source_sha256": source.sha256,
                "rendered_sha256": hashlib.sha256(rendered).hexdigest(),
                "source_path": str(
                    family.root / "originals" / source.relative_path
                ),
                "mutation": source.mutation,
            }
        )
    binding = {
        "manifest": manifest.model_dump(mode="json"),
        "schedule": schedule,
        "adapter_sha256": {
            name: hashlib.sha256(
                read_file(Path(__file__).with_name(name), 1024 * 1024)
            ).hexdigest()
            for name in (
                "current_baseline.py",
                "current_baseline_child.py",
                "current_baseline_models.py",
            )
        },
    }
    return material, json.dumps(binding, sort_keys=True).encode()


def _check_source(source: SourceMaterial) -> None:
    """Bind full source bytes, header identity and captured line window."""
    raw = source.content.encode()
    lines = raw.splitlines()
    window = source.window
    if (
        source.relative_path.is_absolute()
        or ".." in source.relative_path.parts
        or not source.relative_path.parts
        or not raw.endswith(b"\n")
        or hashlib.sha256(raw).hexdigest() != source.sha256
        or len(lines) != window.through_line
        or not lines
        or window.source.provider != source.provider
        or window.source.thread_id != source.thread_id
        or window.boundary_hash != hashlib.sha256(lines[-1]).hexdigest()
    ):
        raise ValueError("source_binding")
    for line in lines:
        strict_json(line)
    first = json.loads(lines[0])["payload"]
    if (
        first.get("id") != source.thread_id
        or first.get("session_id") != source.session_root
    ):
        raise ValueError("source_binding")


def _sources(material: PilotMaterial) -> None:
    """Bind evidence and prevent shared source identities across families."""
    sources = {s.case_id: s for s in material.sources}
    owners: dict[str, str] = {}
    prefixes: dict[tuple[str, str], bytes] = {}
    for case in material.cases:
        source = sources[case.id]
        _check_source(source)
        for identity in (source.thread_id, source.session_root):
            if owners.setdefault(identity, case.family_id) != case.family_id:
                raise ValueError("source_family_binding")
        key = (case.family_id, str(source.relative_path))
        raw = source.content.encode()
        if not raw.startswith(prefixes.get(key, b"")):
            raise ValueError("source_chronology")
        prefixes[key] = raw
        for record in case.evidence:
            lines = raw.splitlines()
            if record.line > len(lines):
                raise ValueError("source_evidence_binding")
            line = lines[record.line - 1]
            _, excerpt = selected(
                fields(source.provider, json.loads(line)),
                record.part,
                record.selector,
            )
            if (
                record.provider != source.provider
                or record.thread_id != source.thread_id
                or record.session_root != source.session_root
                or record.line_sha256 != hashlib.sha256(line).hexdigest()
                or record.selector_digest
                != hashlib.sha256(
                    canonical_json(record.selector).encode()
                ).hexdigest()
                or record.excerpt != excerpt
            ):
                raise ValueError("source_evidence_binding")
