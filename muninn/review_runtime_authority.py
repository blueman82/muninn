"""Read private managed authority through a dependency-free file boundary."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

from muninn.review_codex_output import output_contract_digest
from muninn.review_json import canonical_json, canonical_json_value
from muninn.review_runtime_models import (
    EnrollmentApproval,
    ManagedCapability,
    ManagedInterfaceBinding,
)


def canonical_path(path: Path) -> None:
    """Reject relative, aliased and symlink-containing paths."""
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("capability_path")


def check_private_path(path: Path, *, directory: bool) -> None:
    """Verify private same-user POSIX ownership at a trusted boundary."""
    if sys.platform == "win32":
        raise ValueError("capability_platform")
    canonical_path(path)
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or not kind(info.st_mode)
    ):
        raise ValueError("capability_storage")


def file_identity(info: os.stat_result) -> tuple[int, ...]:
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
            or file_identity(before) != file_identity(before_path)
        ):
            raise ValueError("capability_storage")
        raw = stream.read(before.st_size)
        if len(raw) != before.st_size or file_identity(
            os.fstat(stream.fileno())
        ) != file_identity(before):
            raise ValueError("capability_changed")
    if file_identity(path.lstat()) != file_identity(before):
        raise ValueError("capability_changed")
    return raw


def wire_schema() -> bytes:
    """Describe the fixed strict outer JSON envelope."""
    return canonical_json_value(
        {
            "type": "object",
            "properties": {"payload_json": {"type": "string"}},
            "required": ["payload_json"],
            "additionalProperties": False,
        }
    ).encode()


def fingerprint(binding: ManagedInterfaceBinding) -> str:
    """Bind authority without depending on a changing runtime version."""
    return hashlib.sha256(
        canonical_json_value(
            {
                "binding": binding.model_dump(mode="json"),
                "recipe": "codex-json-review-v1",
                "validator": "codex-managed-validator-v1",
                "wire_schema": wire_schema().decode(),
                "empty_cwd": str(binding.cli_cwd),
            }
        ).encode()
    ).hexdigest()


def artifact(home: Path, directory: str, digest: str) -> bytes:
    """Read a private content-addressed bounded authority artifact."""
    parent = home / "review-capabilities" / directory
    for path in (home, home / "review-capabilities", parent):
        check_private_path(path, directory=True)
    raw = read_private_file(parent / (digest + ".json"), 256 * 1024)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("capability_changed")
    return raw


def checked_binding(
    home: Path, capability: ManagedCapability
) -> tuple[ManagedInterfaceBinding, EnrollmentApproval]:
    """Check stable proof and the exact original enrollment decision."""
    path = capability.proof_path
    if path.parent != home / "review-capabilities" or path.suffix != ".json":
        raise ValueError("capability_storage")
    check_private_path(home, directory=True)
    check_private_path(path.parent, directory=True)
    raw = read_private_file(path, 65536)
    if hashlib.sha256(raw).hexdigest() != path.stem:
        raise ValueError("capability_changed")
    binding = ManagedInterfaceBinding.model_validate_json(raw)
    approval_raw = artifact(
        home, "managed-approvals", binding.enrollment_approval
    )
    approval = EnrollmentApproval.model_validate_json(approval_raw)
    if (
        canonical_json(binding).encode() != raw
        or canonical_json(approval).encode() != approval_raw
        or binding.requested != capability.requested
        or binding.host_identity != approval.host_identity
        or binding.auth_home != approval.auth_home
        or binding.initial_bridge != approval.bridge_digest
        or binding.cli_cwd != home / "review-runner-cwd"
        or binding.schema_path != home / "review-codex-schema.json"
        or not binding.auth_home.is_absolute()
        or binding.auth_home.resolve() != binding.auth_home
        or not binding.auth_home.is_dir()
        or binding.auth_home == home
        or home in binding.auth_home.parents
        or binding.output_contract_digest != output_contract_digest()
        or fingerprint(binding) != capability.config_digest
    ):
        raise ValueError("capability_changed")
    check_private_path(binding.cli_cwd, directory=True)
    if (
        tuple(binding.cli_cwd.iterdir())
        or read_private_file(
            binding.schema_path,
            4096,
        )
        != wire_schema()
    ):
        raise ValueError("capability_changed")
    return binding, approval


def selected_capability(home: Path, policy_digest: str) -> ManagedCapability:
    """Read a policy-keyed selector; its proof authenticates authority."""
    directory = home / "review-capabilities" / "selected"
    for path in (home, directory.parent, directory):
        check_private_path(path, directory=True)
    raw = read_private_file(directory / (policy_digest + ".json"), 65536)
    selected = ManagedCapability.model_validate_json(raw)
    if canonical_json(selected).encode() != raw:
        raise ValueError("capability_changed")
    return selected
