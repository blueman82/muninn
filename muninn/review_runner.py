"""Prepare fixed reviewer launches from trusted static-host proof."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import ValidationError, field_validator

from muninn import review_codex_launch
from muninn.knowledge_models import Digest, SchemaVersion, StrictModel, Text
from muninn.platform_io import open_regular
from muninn.review_json import (
    canonical_json_value,
    finite_float,
    reject_constant,
    result_schema,
    unique_object,
)
from muninn.review_launch_spec import LaunchSpec as LaunchSpec
from muninn.review_launch_spec import cli_argv, launch_spec
from muninn.review_models import (
    ReviewContext,
    RunnerConfig,
)
from muninn.review_output import output_contract_digest
from muninn.review_output import reported_metadata as reported_metadata
from muninn.review_rounds import check_round, round_schema
from muninn.store import data_home

_RECIPE = "claude-static-posix-v1"
_COVERAGE = {"cwd", "ancestors", "project", "plugins", "managed", "auth"}
_DISABLED = {"tools", "mcp", "hooks", "plugins", "session_persistence"}
_MIB = 1024 * 1024


class RunnerCapability(StrictModel):
    """Bind requested execution to independently issued native trial proof."""

    runner: Text
    executable: Text
    version: Text
    status: Literal["verified", "unsupported", "unverified"]
    model: Text
    reasoning: Text
    schema_digest: Digest
    config_digest: Digest
    proof_path: Path


class HostProof(StrictModel):
    """Describe a controlled native trial of an exhaustive static recipe."""

    schema_version: SchemaVersion
    provenance: Literal["native_trial", "synthetic"]
    recipe: Literal["claude-static-posix-v1"]
    requested: RunnerConfig
    output_contract_digest: Digest
    schema_digest: Digest
    cli_argv: tuple[str, ...]
    executable_sha256: Digest
    cli_cwd: Path
    config_roots: tuple[Path, ...]
    environment_sha256: dict[str, Digest]
    config_digest: Digest
    coverage: tuple[str, ...]
    disabled: tuple[str, ...]
    complete: Literal[True]

    @field_validator("complete", mode="before")
    @classmethod
    def literal_complete(cls, value: object) -> object:
        """Require literal true without accepting a numerically equal value."""
        if value is not True:
            raise ValueError("capability_unverified")
        return value


def _digest(raw: bytes) -> str:
    """Hash exact bytes without retaining their contents."""
    return hashlib.sha256(raw).hexdigest()


def _canonical(path: Path) -> None:
    """Reject relative, aliased and symlink-containing paths."""
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("capability_path")


def check_private_path(path: Path, *, directory: bool) -> None:
    """Verify private same-user POSIX ownership at a trusted boundary."""
    if sys.platform == "win32":
        raise ValueError("capability_platform")
    _canonical(path)
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or not kind(info.st_mode)
    ):
        raise ValueError("capability_storage")


def _identity(info: os.stat_result) -> tuple[int, ...]:
    """Compare content identity without the access time changed by reading."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
        info.st_gid,
    )


def read_private_file(path: Path, limit: int) -> bytes:
    """Read a bounded regular proof without following a final symlink."""
    if sys.platform == "win32":
        raise ValueError("capability_platform")
    before_path = path.lstat()
    with os.fdopen(
        os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK),
        "rb",
        buffering=0,
    ) as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("capability_limit")
        if (
            before.st_uid != os.getuid()
            or before.st_mode & 0o077
            or _identity(before) != _identity(before_path)
        ):
            raise ValueError("capability_storage")
        raw = stream.read(before.st_size)
        if len(raw) != before.st_size or _identity(
            os.fstat(stream.fileno())
        ) != _identity(before):
            raise ValueError("capability_changed")
    if _identity(path.lstat()) != _identity(before):
        raise ValueError("capability_changed")
    return raw


def file_digest(path: Path, limit: int) -> tuple[str, int]:
    """Stream a bounded file and refuse observable concurrent changes."""
    _canonical(path)
    with (
        open_regular(path) as checked,
        os.fdopen(os.dup(checked.fileno()), "rb", buffering=0) as stream,
    ):
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("capability_limit")
        remaining = before.st_size
        digest = hashlib.sha256()
        while remaining:
            block = stream.read(min(remaining, 65536))
            if not block:
                raise ValueError("capability_changed")
            remaining -= len(block)
            digest.update(block)
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError("capability_changed")
    return digest.hexdigest(), before.st_size


def _roots(roots: tuple[Path, ...]) -> None:
    """Require disjoint canonical inventory roots."""
    for root in roots:
        _canonical(root)
        if any(other in root.parents for other in roots):
            raise ValueError("capability_config")


def config_fingerprint(
    roots: tuple[Path, ...], environment: Mapping[str, str], cwd: Path
) -> str:
    """Fingerprint attested inputs without establishing completeness."""
    if not roots or len(set(roots)) != len(roots) or cwd not in roots:
        raise ValueError("capability_config")
    if len(roots) > 4096:
        raise ValueError("capability_limit")
    _roots(roots)
    pending = sorted(roots, reverse=True)
    entries: list[tuple[str, str, str]] = []
    remaining = 16 * _MIB
    while pending:
        if len(entries) + len(pending) > 4096:
            raise ValueError("capability_limit")
        path = pending.pop()
        _canonical(path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            entries.append((str(path), "absent", ""))
            continue
        if stat.S_ISREG(info.st_mode):
            hashed, size = file_digest(path, min(_MIB, remaining))
            remaining -= size
            entries.append((str(path), "file", hashed))
        elif stat.S_ISDIR(info.st_mode):
            entries.append((str(path), "directory", ""))
            with os.scandir(path) as children:
                for child in children:
                    pending.append(Path(child.path))
                    if len(entries) + len(pending) > 4096:
                        raise ValueError("capability_limit")
        else:
            raise ValueError("capability_config")
    return _digest(
        canonical_json_value(
            {
                "recipe": _RECIPE,
                "cwd": str(cwd),
                "roots": sorted(map(str, roots)),
                "inventory": sorted(entries),
                "environment": dict(environment),
            }
        ).encode()
    )


def _proof(capability: RunnerCapability) -> HostProof:
    """Load content-addressed proof only from configured private storage."""
    if os.name != "posix":
        raise ValueError("capability_platform")
    home = data_home(os.environ)
    directory = home / "review-capabilities"
    path = capability.proof_path
    if (
        path.parent != directory
        or re.fullmatch(r"[0-9a-f]{64}\.json", path.name) is None
    ):
        raise ValueError("capability_storage")
    check_private_path(home, directory=True)
    check_private_path(directory, directory=True)
    check_private_path(path, directory=False)
    raw = read_private_file(path, _MIB)
    if _digest(raw) != path.stem:
        raise ValueError("capability_changed")
    try:
        json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except (ValueError, RecursionError):
        raise ValueError("capability_invalid") from None
    proof = HostProof.model_validate_json(raw)
    if proof.provenance != "native_trial":
        raise ValueError("capability_unverified")
    if proof.cli_cwd != home / "review-runner-cwd":
        raise ValueError("capability_config")
    check_private_path(proof.cli_cwd, directory=True)
    return proof


def _configuration(
    proof: HostProof, capability: RunnerCapability, requested: RunnerConfig
) -> dict[str, str]:
    """Match native assertions and freshly observed static local inputs."""
    if (
        proof.requested != requested
        or proof.output_contract_digest != output_contract_digest()
        or proof.cli_argv != cli_argv(requested)
        or proof.schema_digest != capability.schema_digest
        or proof.config_digest != capability.config_digest
        or set(proof.coverage) != _COVERAGE
        or len(proof.coverage) != len(_COVERAGE)
        or set(proof.disabled) != _DISABLED
        or len(proof.disabled) != len(_DISABLED)
    ):
        raise ValueError("capability_changed")
    if "HOME" not in proof.environment_sha256:
        raise ValueError("capability_config")
    environment: dict[str, str] = {}
    for key, expected in proof.environment_sha256.items():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            raise ValueError("capability_config")
        value = os.environ.get(key)
        if (
            value is None
            or _digest(value.encode()) != expected
            or "\0" in value
        ):
            raise ValueError("capability_changed")
        environment[key] = value
    executable = Path(requested.executable)
    hashed, _ = file_digest(executable, 256 * _MIB)
    if hashed != proof.executable_sha256 or not os.access(executable, os.X_OK):
        raise ValueError("capability_changed")
    if (
        config_fingerprint(
            proof.config_roots, proof.environment_sha256, proof.cli_cwd
        )
        != proof.config_digest
    ):
        raise ValueError("capability_changed")
    return environment


def prepare_launch(
    context: ReviewContext, capability: RunnerCapability, prompt: bytes
) -> LaunchSpec:
    """Prepare fixed data after fresh checks, without reserving or spawning."""
    try:
        context = ReviewContext.model_validate_json(context.model_dump_json())
        capability = RunnerCapability.model_validate_json(
            capability.model_dump_json()
        )
        check_round(context)
        if (
            type(prompt) is not bytes
            or len(prompt) > context.policy.max_input_bytes
        ):
            raise ValueError("invalid_prompt_size")
        if (
            capability.runner == "codex_cli"
            and context.policy.experimental_codex is not None
        ):
            return review_codex_launch.prepare(context, capability, prompt)
        if capability.runner != "claude_cli":
            raise ValueError("runner_unsupported")
        if capability.status != "verified":
            raise ValueError("capability_unverified")
        requested = context.policy.runner
        if any(
            getattr(capability, key) != value
            for key, value in requested.model_dump().items()
        ):
            raise ValueError("capability_changed")
        if capability.schema_digest != _digest(
            canonical_json_value(round_schema()).encode()
        ) or context.policy.result_schema_digest != _digest(
            canonical_json_value(result_schema()).encode()
        ):
            raise ValueError("capability_changed")
        proof = _proof(capability)
        environment = _configuration(proof, capability, requested)
        check_round(context)
        return launch_spec(context, proof.cli_cwd, environment, prompt)
    except (ValidationError, json.JSONDecodeError):
        raise ValueError("capability_invalid") from None
    except (OSError, UnicodeError):
        raise ValueError("capability_unavailable") from None
